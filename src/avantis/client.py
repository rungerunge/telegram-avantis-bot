"""
Avantis DEX on-chain client.
Handles openTrade, closeTradeMarket, position queries via web3.py on Base.
"""

import logging
from typing import Any

import httpx
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from ..config import Settings

logger = logging.getLogger(__name__)

# 10-decimal precision used by Avantis for prices and leverage
PRECISION = 10 ** 10

# Minimal ABIs — only the functions we need
TRADING_ABI = [
    {
        "name": "openTrade",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "t",
                "type": "tuple",
                "components": [
                    {"name": "trader", "type": "address"},
                    {"name": "pairIndex", "type": "uint256"},
                    {"name": "index", "type": "uint256"},
                    {"name": "initialPosToken", "type": "uint256"},
                    {"name": "positionSizeUSDC", "type": "uint256"},
                    {"name": "openPrice", "type": "uint256"},
                    {"name": "buy", "type": "bool"},
                    {"name": "leverage", "type": "uint256"},
                    {"name": "tp", "type": "uint256"},
                    {"name": "sl", "type": "uint256"},
                    {"name": "timestamp", "type": "uint256"},
                ],
            },
            {"name": "_type", "type": "uint8"},
            {"name": "_slippageP", "type": "uint256"},
        ],
        "outputs": [{"name": "orderId", "type": "uint256"}],
    },
    {
        "name": "closeTradeMarket",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "_pairIndex", "type": "uint256"},
            {"name": "_index", "type": "uint256"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [],
    },
]

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


class AvantisClient:
    """On-chain client for Avantis perpetual futures on Base."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.w3 = Web3(Web3.HTTPProvider(settings.base_rpc_http))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        self.wallet = Web3.to_checksum_address(settings.wallet_address)
        self.account = self.w3.eth.account.from_key(settings.private_key)

        self.trading = self.w3.eth.contract(
            address=Web3.to_checksum_address(settings.trading_contract),
            abi=TRADING_ABI,
        )
        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(settings.usdc_address),
            abi=ERC20_ABI,
        )
        self.storage_address = Web3.to_checksum_address(settings.trading_storage)

        self._http = httpx.AsyncClient(timeout=15.0)

    # ── Helpers ──────────────────────────────────────────────

    def _to_price(self, price: float) -> int:
        """Convert a float price to 10-decimal precision int."""
        return int(price * PRECISION)

    def _to_leverage(self, leverage: int) -> int:
        """Convert leverage integer to 10-decimal precision."""
        return leverage * PRECISION

    def _to_usdc(self, usd_amount: float) -> int:
        """Convert USD float to USDC raw (6 decimals)."""
        return int(usd_amount * 10**6)

    def _to_slippage(self, percent: float) -> int:
        """Convert slippage % to 10-decimal precision."""
        return int(percent * PRECISION)

    async def _send_tx(self, tx_data: dict) -> str:
        """Sign and send a transaction. Returns tx hash hex."""
        nonce = self.w3.eth.get_transaction_count(self.wallet)
        tx_data["nonce"] = nonce
        tx_data["from"] = self.wallet
        tx_data["chainId"] = 8453  # Base mainnet

        # Gas estimation
        try:
            gas = self.w3.eth.estimate_gas(tx_data)
            tx_data["gas"] = int(gas * 1.3)  # 30% buffer
        except Exception as e:
            logger.warning("Gas estimation failed, using default 500k: %s", e)
            tx_data["gas"] = 500_000

        # EIP-1559 gas pricing
        base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        tx_data["maxFeePerGas"] = base_fee * 2
        tx_data["maxPriorityFeePerGas"] = self.w3.to_wei(0.001, "gwei")

        signed = self.account.sign_transaction(tx_data)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        hex_hash = tx_hash.hex()
        logger.info("TX sent: %s", hex_hash)
        return hex_hash

    async def _wait_for_receipt(self, tx_hash: str, timeout: int = 60) -> dict:
        """Wait for transaction receipt."""
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        if receipt["status"] != 1:
            raise RuntimeError(f"Transaction reverted: {tx_hash}")
        logger.info("TX confirmed: %s (gas used: %s)", tx_hash, receipt["gasUsed"])
        return receipt

    # ── USDC ─────────────────────────────────────────────────

    async def get_usdc_balance(self) -> float:
        """Get USDC balance in USD."""
        raw = self.usdc.functions.balanceOf(self.wallet).call()
        return raw / 10**6

    async def get_eth_balance(self) -> float:
        """Get ETH balance."""
        raw = self.w3.eth.get_balance(self.wallet)
        return self.w3.from_wei(raw, "ether")

    async def ensure_usdc_approval(self, amount_usd: float) -> None:
        """Approve USDC to TradingStorage if allowance is insufficient."""
        raw_amount = self._to_usdc(amount_usd)
        allowance = self.usdc.functions.allowance(self.wallet, self.storage_address).call()
        if allowance >= raw_amount:
            return
        logger.info("Approving USDC: %s to TradingStorage", amount_usd)
        tx = self.usdc.functions.approve(
            self.storage_address,
            2**256 - 1  # Max approval
        ).build_transaction({"from": self.wallet})
        tx_hash = await self._send_tx(tx)
        await self._wait_for_receipt(tx_hash)
        logger.info("USDC approved")

    # ── Open Trade ───────────────────────────────────────────

    async def open_trade(
        self,
        pair_index: int,
        position_size_usd: float,
        open_price: float,
        is_long: bool,
        leverage: int,
        tp_price: float,
        sl_price: float,
        trade_index: int = 0,
    ) -> str:
        """
        Open a trade on Avantis.
        Returns tx hash.
        """
        await self.ensure_usdc_approval(position_size_usd)

        trade_tuple = (
            self.wallet,                          # trader
            pair_index,                           # pairIndex
            trade_index,                          # index (next available slot)
            0,                                    # initialPosToken (0 for market)
            self._to_usdc(position_size_usd),     # positionSizeUSDC
            self._to_price(open_price),           # openPrice
            is_long,                              # buy
            self._to_leverage(leverage),          # leverage
            self._to_price(tp_price),             # tp
            self._to_price(sl_price),             # sl
            0,                                    # timestamp (contract fills)
        )

        slippage = self._to_slippage(self.settings.slippage_percent)
        exec_fee = self.w3.to_wei(self.settings.execution_fee_eth, "ether")

        tx = self.trading.functions.openTrade(
            trade_tuple,
            0,          # _type = MARKET
            slippage,
        ).build_transaction({
            "from": self.wallet,
            "value": exec_fee,
        })

        logger.info(
            "Opening trade: pair=%d %s %dx, size=$%.2f, price=%.2f, tp=%.2f, sl=%.2f",
            pair_index, "LONG" if is_long else "SHORT", leverage,
            position_size_usd, open_price, tp_price, sl_price,
        )

        tx_hash = await self._send_tx(tx)
        receipt = await self._wait_for_receipt(tx_hash)
        return tx_hash

    # ── Close Trade ──────────────────────────────────────────

    async def close_trade(
        self,
        pair_index: int,
        trade_index: int,
        amount: int = 0,  # 0 = close full position
    ) -> str:
        """Close a trade on Avantis. Returns tx hash."""
        exec_fee = self.w3.to_wei(self.settings.execution_fee_eth, "ether")

        tx = self.trading.functions.closeTradeMarket(
            pair_index,
            trade_index,
            amount,
        ).build_transaction({
            "from": self.wallet,
            "value": exec_fee,
        })

        logger.info("Closing trade: pair=%d, index=%d", pair_index, trade_index)

        tx_hash = await self._send_tx(tx)
        receipt = await self._wait_for_receipt(tx_hash)
        return tx_hash

    # ── Position Queries ─────────────────────────────────────

    async def get_open_positions(self) -> list[dict[str, Any]]:
        """
        Fetch open positions from Avantis API.
        Returns list of position dicts.
        """
        url = f"https://core.avantisfi.com/user-data?trader={self.wallet}"
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            data = resp.json()
            # The API returns trades under various keys; normalize
            trades = data.get("trades", data.get("openTrades", []))
            if isinstance(trades, list):
                return trades
            return []
        except Exception as e:
            logger.error("Failed to fetch open positions: %s", e)
            return []

    async def get_next_trade_index(self, pair_index: int) -> int:
        """
        Get next available trade index for a pair.
        Checks open positions and returns max index + 1, or 0 if none.
        """
        positions = await self.get_open_positions()
        max_index = -1
        for pos in positions:
            pi = int(pos.get("pairIndex", -1))
            idx = int(pos.get("index", -1))
            if pi == pair_index and idx > max_index:
                max_index = idx
        return max_index + 1
