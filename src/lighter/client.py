"""
Lighter DEX client for zklighter perpetuals.
Implements the same interface as AvantisClient so TradeManager can swap between them.

Uses the lighter-sdk Python package (pip install git+https://github.com/elliottech/lighter-python.git)
for order signing via native signer, and the REST API for account/position queries.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import lighter
from lighter import (
    AccountApi,
    ApiClient,
    Configuration,
    OrderApi,
    SignerClient,
)
from lighter.signer_client import CreateOrderTxReq

from ..config import Settings

logger = logging.getLogger(__name__)

# ── Market configuration ────────────────────────────────────────
# Lighter uses market_id 0 for ETH perps, 1 for BTC perps.
# Avantis pair_index 0 = ETH, 1 = BTC — same mapping, convenient.

MARKET_CONFIG: Dict[int, Dict[str, Any]] = {
    0: {  # ETH
        "symbol": "ETH",
        "market_id": 0,
        "sz_scale": 10_000,       # 1 ETH = 10000 base units
        "price_scale": 100,       # prices in cents ($1 = 100)
        "min_base": 50,           # minimum 50 units = 0.005 ETH
    },
    1: {  # BTC
        "symbol": "BTC",
        "market_id": 1,
        "sz_scale": 100_000,      # 1 BTC = 100000 base units
        "price_scale": 10,        # prices in deci-cents ($1 = 10)
        "min_base": 20,           # minimum 20 units = 0.0002 BTC
    },
}

BASE_URL = "https://mainnet.zklighter.elliot.ai"

# Wide slippage ceiling for market orders — the orderbook determines actual fill.
# 2% above/below best price is a safe default for perps.
DEFAULT_SLIPPAGE_PCT = 0.02


class LighterClient:
    """
    Async client for Lighter perpetual futures.

    Drop-in replacement for AvantisClient — implements the same method signatures
    so TradeManager can use either without changes.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

        # Lighter-specific env vars
        self._account_index = int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "722128"))
        api_key_private = os.environ.get("LIGHTER_API_KEY", "")
        api_key_index = int(os.environ.get("LIGHTER_API_KEY_INDEX", "4"))

        if not api_key_private:
            raise ValueError("LIGHTER_API_KEY env var is required")

        # SignerClient for order signing + submission
        self._signer = SignerClient(
            url=BASE_URL,
            account_index=self._account_index,
            api_private_keys={api_key_index: api_key_private},
        )

        # REST API client for queries (positions, balances, orders)
        self._config = Configuration(host=BASE_URL)
        self._api_client = ApiClient(self._config)
        self._account_api = AccountApi(self._api_client)
        self._order_api = OrderApi(self._api_client)

        # Serialize signer write-calls (create/cancel orders) across concurrent
        # trade flows. Without this, two trades fired from a multi-signal message
        # can race on the Lighter nonce and one fails with code=21104 'invalid
        # nonce' (observed on Slavi during #11382 multi-signal 04-22).
        self._signer_lock = asyncio.Lock()

        self._signer.check_client()
        logger.info(
            "LighterClient initialized (account=%d, api_key_index=%d)",
            self._account_index, api_key_index,
        )

    # ── Helpers ──────────────────────────────────────────────────

    def _get_market(self, pair_index: int) -> Dict[str, Any]:
        """Get market config for a pair_index (0=ETH, 1=BTC)."""
        cfg = MARKET_CONFIG.get(pair_index)
        if cfg is None:
            raise ValueError(f"Unsupported pair_index {pair_index}. Supported: 0 (ETH), 1 (BTC)")
        return cfg

    def _usd_to_base_amount(self, pair_index: int, usd_amount: float, price: float, leverage: int) -> int:
        """
        Convert a USD collateral amount + leverage into Lighter base units.

        notional = usd_amount * leverage
        size_in_asset = notional / price
        base_amount = size_in_asset * sz_scale
        """
        mkt = self._get_market(pair_index)
        notional = usd_amount * leverage
        size = notional / price
        base = int(size * mkt["sz_scale"])
        if base < mkt["min_base"]:
            logger.warning(
                "Calculated base_amount %d below minimum %d for %s, clamping up",
                base, mkt["min_base"], mkt["symbol"],
            )
            base = mkt["min_base"]
        return base

    def _price_to_scaled(self, pair_index: int, price: float) -> int:
        """Convert a float price to Lighter's integer price (price * price_scale)."""
        mkt = self._get_market(pair_index)
        return int(price * mkt["price_scale"])

    def _base_amount_to_size(self, pair_index: int, base_amount: int) -> float:
        """Convert Lighter base units back to asset quantity (e.g. ETH)."""
        mkt = self._get_market(pair_index)
        return base_amount / mkt["sz_scale"]

    def _scaled_to_price(self, pair_index: int, scaled_price: int) -> float:
        """Convert Lighter scaled price back to float USD."""
        mkt = self._get_market(pair_index)
        return scaled_price / mkt["price_scale"]

    async def _get_account_detail(self) -> Optional["lighter.DetailedAccount"]:
        """Fetch our DetailedAccount from the REST API."""
        try:
            resp = await self._account_api.account(
                by="index",
                value=str(self._account_index),
            )
            if resp.accounts:
                return resp.accounts[0]
            return None
        except Exception as e:
            logger.error("Failed to fetch account detail: %s", e)
            return None

    # ── USDC / ETH Balances ─────────────────────────────────────

    async def get_usdc_balance(self) -> float:
        """Return available USDC balance on Lighter."""
        acct = await self._get_account_detail()
        if acct is None:
            return 0.0
        # available_balance is a string representing USD value
        try:
            return float(acct.available_balance)
        except (ValueError, TypeError):
            return 0.0

    async def get_eth_balance(self) -> float:
        """Lighter doesn't use ETH for gas — always 0."""
        return 0.0

    async def ensure_usdc_approval(self, amount_usd: float) -> None:
        """No-op — Lighter doesn't need ERC-20 approvals."""
        pass

    # ── Open Trade ───────────────────────────────────────────────

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
        Open a perpetual position on Lighter.

        Strategy:
        1. Send a market order (IOC) for the position size.
        2. Attach SL as a position-tied stop-loss order via grouped OCO.
           (TP is managed by the bot's price monitor, not on-chain.)

        Returns a tx_hash string.
        """
        mkt = self._get_market(pair_index)
        base_amount = self._usd_to_base_amount(pair_index, position_size_usd, open_price, leverage)

        # is_ask: True = sell, False = buy
        # Long = buy, Short = sell
        is_ask = not is_long

        # Worst acceptable price: add slippage buffer
        if is_long:
            worst_price = open_price * (1 + DEFAULT_SLIPPAGE_PCT)
        else:
            worst_price = open_price * (1 - DEFAULT_SLIPPAGE_PCT)
        avg_execution_price = self._price_to_scaled(pair_index, worst_price)

        logger.info(
            "Opening trade: %s %s %dx, size=$%.2f, base_amount=%d, price=~%.2f, sl=%.2f",
            mkt["symbol"], "LONG" if is_long else "SHORT", leverage,
            position_size_usd, base_amount, open_price, sl_price,
        )

        # Place the market order (serialized against other signer calls)
        async with self._signer_lock:
            tx, resp, err = await self._signer.create_market_order(
                market_index=mkt["market_id"],
                client_order_index=trade_index,
                base_amount=base_amount,
                avg_execution_price=avg_execution_price,
                is_ask=is_ask,
            )
        if err is not None:
            raise RuntimeError(f"Lighter market order failed: {err}")

        tx_hash = resp.tx_hash if resp else "unknown"
        logger.info("Market order placed: tx_hash=%s", tx_hash)

        # Place a position-tied stop-loss order
        if sl_price and sl_price > 0:
            await self._place_sl_order(pair_index, sl_price, is_long, base_amount=0)

        return tx_hash

    async def _place_sl_order(
        self,
        pair_index: int,
        sl_price: float,
        is_long: bool,
        base_amount: int = 0,
    ) -> Optional[str]:
        """
        Place a stop-loss order on Lighter.

        For a long position: SL triggers when price drops to sl_price, then sells.
        For a short position: SL triggers when price rises to sl_price, then buys.

        base_amount=0 means position-tied (tracks full position size).
        """
        mkt = self._get_market(pair_index)
        trigger_price = self._price_to_scaled(pair_index, sl_price)

        # SL limit price: give some buffer beyond trigger for fill
        # Long SL: selling, so limit slightly below trigger
        # Short SL: buying, so limit slightly above trigger
        if is_long:
            limit_price = self._price_to_scaled(pair_index, sl_price * 0.995)
            is_ask = True  # selling to close long
        else:
            limit_price = self._price_to_scaled(pair_index, sl_price * 1.005)
            is_ask = False  # buying to close short

        try:
            async with self._signer_lock:
                tx, resp, err = await self._signer.create_sl_limit_order(
                    market_index=mkt["market_id"],
                    client_order_index=0,
                    base_amount=base_amount,  # 0 = position-tied
                    trigger_price=trigger_price,
                    price=limit_price,
                    is_ask=is_ask,
                    reduce_only=True,
                )
            if err is not None:
                logger.error("Failed to place SL order: %s", err)
                return None

            tx_hash = resp.tx_hash if resp else "unknown"
            logger.info("SL order placed at %.2f: tx_hash=%s", sl_price, tx_hash)
            return tx_hash
        except Exception as e:
            logger.error("Exception placing SL order: %s", e)
            return None

    # ── Close Trade ──────────────────────────────────────────────

    async def close_trade(
        self,
        pair_index: int,
        trade_index: int,
        amount_usdc: float = 0,
    ) -> str:
        """
        Close a position (full or partial) by placing a reduce-only market order.

        amount_usdc=0 means close entire position.
        For partial close, amount_usdc is the collateral portion to close.
        """
        mkt = self._get_market(pair_index)

        # Get current position to determine direction and size
        position = await self._get_position(pair_index)
        if position is None:
            raise RuntimeError(f"No open position found for market {mkt['symbol']}")

        pos_sign = int(position.sign)  # 1 = long, -1 = short (typically)
        pos_size = int(abs(float(position.position)) * mkt["sz_scale"])  # convert decimal to base units
        is_long = pos_sign > 0

        if amount_usdc > 0 and pos_size > 0:
            # Partial close: figure out what fraction of position to close
            # position_value is the notional; we close proportionally by collateral
            try:
                total_value = abs(float(position.position_value))
                # allocated_margin ~ collateral
                total_collateral = abs(float(position.allocated_margin))
                if total_collateral > 0:
                    fraction = min(amount_usdc / total_collateral, 1.0)
                else:
                    fraction = 1.0
            except (ValueError, TypeError):
                fraction = 1.0
            close_amount = max(int(pos_size * fraction), mkt["min_base"])
        else:
            close_amount = pos_size  # full close

        # To close: sell if long, buy if short
        is_ask = is_long

        # Get a worst-case price
        best_price = await self._signer.get_best_price(mkt["market_id"], is_ask)
        if best_price <= 0:
            # Fallback: use position's avg entry with wide slippage
            entry = float(position.avg_entry_price)
            if is_long:
                worst = self._price_to_scaled(pair_index, entry * 0.95)
            else:
                worst = self._price_to_scaled(pair_index, entry * 1.05)
        else:
            # Add slippage buffer to best orderbook price
            if is_ask:
                worst = int(best_price * 0.98)  # selling: accept 2% below best
            else:
                worst = int(best_price * 1.02)  # buying: accept 2% above best

        if amount_usdc > 0:
            logger.info(
                "Partial close %s: %.2f USDC (~%d base units)",
                mkt["symbol"], amount_usdc, close_amount,
            )
        else:
            logger.info("Full close %s: %d base units", mkt["symbol"], close_amount)

        async with self._signer_lock:
            tx, resp, err = await self._signer.create_market_order(
                market_index=mkt["market_id"],
                client_order_index=0,
                base_amount=close_amount,
                avg_execution_price=worst,
                is_ask=is_ask,
                reduce_only=True,
            )
        if err is not None:
            raise RuntimeError(f"Lighter close order failed: {err}")

        tx_hash = resp.tx_hash if resp else "unknown"
        logger.info("Close order filled: tx_hash=%s", tx_hash)
        return tx_hash

    # ── Update SL ────────────────────────────────────────────────

    async def update_sl(
        self,
        pair_index: int,
        trade_index: int,
        new_sl_price: float,
    ) -> str:
        """
        Move stop loss to a new price.

        Strategy: cancel existing SL orders for this market, then place a new one.
        """
        mkt = self._get_market(pair_index)

        position = await self._get_position(pair_index)
        is_long = position is not None and int(position.sign) > 0

        # Cancel existing SL/TP orders for this market
        await self._cancel_sl_orders(pair_index, is_long)

        # Place new SL
        tx_hash = await self._place_sl_order(pair_index, new_sl_price, is_long, base_amount=0)
        if tx_hash is None:
            raise RuntimeError(f"Failed to place new SL at {new_sl_price}")

        logger.info("SL updated to %.2f for %s: tx_hash=%s", new_sl_price, mkt["symbol"], tx_hash)
        return tx_hash

    async def _cancel_sl_orders(self, pair_index: int, is_long: bool) -> None:
        """Cancel all SL-type orders for a given market."""
        mkt = self._get_market(pair_index)
        try:
            orders_resp = await self._order_api.account_active_orders(
                account_index=self._account_index,
                market_id=mkt["market_id"],
            )
            if not orders_resp or not orders_resp.orders:
                return

            for order in orders_resp.orders:
                order_type = int(order.type) if order.type is not None else -1
                # SL types: 2 (stop_loss), 3 (stop_loss_limit)
                if order_type in (2, 3):
                    try:
                        async with self._signer_lock:
                            _, _, err = await self._signer.cancel_order(
                                market_index=mkt["market_id"],
                                order_index=int(order.order_index),
                            )
                        if err:
                            logger.warning("Failed to cancel SL order %s: %s", order.order_index, err)
                        else:
                            logger.info("Cancelled SL order %s", order.order_index)
                    except Exception as e:
                        logger.warning("Error cancelling SL order %s: %s", order.order_index, e)
        except Exception as e:
            logger.error("Failed to fetch active orders for SL cancel: %s", e)

    # ── Update TP (no-op) ───────────────────────────────────────

    async def update_tp(
        self,
        pair_index: int,
        trade_index: int,
        new_tp_price: float,
    ) -> str:
        """
        No-op for Lighter — TP is managed by the bot's price monitor.

        The bot does partial closes at each target price, so we don't need
        on-chain TP orders. Returns a placeholder hash.
        """
        logger.info("update_tp called (no-op on Lighter): pair=%d, tp=%.2f", pair_index, new_tp_price)
        return "lighter_tp_noop"

    # ── Position Queries ─────────────────────────────────────────

    async def _get_position(self, pair_index: int) -> Optional["lighter.AccountPosition"]:
        """Get the AccountPosition for a specific market, or None."""
        mkt = self._get_market(pair_index)
        acct = await self._get_account_detail()
        if acct is None or not acct.positions:
            return None

        for pos in acct.positions:
            if int(pos.market_id) == mkt["market_id"]:
                pos_size = abs(float(pos.position))
                if pos_size > 0:
                    return pos
        return None

    async def get_open_positions(self) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch open positions from Lighter API.

        Returns list of dicts with keys matching what TradeManager's reconciliation expects:
        - pairIndex (int)
        - index (int) — we use 0 since Lighter doesn't have per-pair trade indices
        - symbol, sign, position, avg_entry_price, unrealized_pnl, etc.

        Returns None on error (same contract as AvantisClient).
        """
        try:
            acct = await self._get_account_detail()
            if acct is None:
                return None

            positions = []
            for pos in (acct.positions or []):
                pos_size = abs(float(pos.position))
                if pos_size == 0:
                    continue

                market_id = int(pos.market_id)

                # Map market_id back to pair_index (they happen to match for ETH/BTC)
                pair_index = market_id

                positions.append({
                    "pairIndex": pair_index,
                    "index": 0,  # Lighter doesn't use per-pair sub-indices
                    "symbol": pos.symbol,
                    "sign": int(pos.sign),
                    "position": pos.position,
                    "avg_entry_price": pos.avg_entry_price,
                    "position_value": pos.position_value,
                    "unrealized_pnl": pos.unrealized_pnl,
                    "realized_pnl": pos.realized_pnl,
                    "liquidation_price": pos.liquidation_price,
                    "allocated_margin": pos.allocated_margin,
                })

            return positions

        except Exception as e:
            logger.error("Failed to fetch open positions: %s", e)
            return None

    async def get_next_trade_index(self, pair_index: int) -> int:
        """
        Get next available trade index for a pair.

        Lighter doesn't have per-pair trade indices like Avantis.
        We always return 0 — the trade_index is used as client_order_index
        which is just a client-side tag.
        """
        return 0

    # ── Cleanup ──────────────────────────────────────────────────

    async def close(self) -> None:
        """Clean up SDK clients."""
        try:
            await self._signer.close()
        except Exception:
            pass
        try:
            await self._api_client.close()
        except Exception:
            pass
