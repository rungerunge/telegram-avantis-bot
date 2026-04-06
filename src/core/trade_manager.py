"""
Trade Manager for Avantis DEX.

Simplified vs. Binance version:
- No WebSocket fill monitoring (keepers handle TP/SL on-chain)
- No position monitor (reconciliation loop polls Avantis API)
- TP/SL set in the openTrade struct, auto-executed by keepers
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..config import get_settings
from ..avantis.client import AvantisClient
from ..avantis.pairs import get_pair_index, get_price_symbol
from ..avantis.prices import get_price
from ..database.models import Trade, TradeStatus, TradeDirection
from ..database.repository import TradeRepository, get_db_session

logger = logging.getLogger(__name__)


class TradeManager:
    """
    Manages trade lifecycle on Avantis DEX.

    Flow:
    1. Signal arrives → create_trade()
    2. Map pair, fetch price, build openTrade tx
    3. Submit on-chain (TP/SL baked into the trade struct)
    4. Reconciliation loop checks positions every 15s
    5. When keepers close a position (TP/SL hit), mark trade closed
    """

    def __init__(self):
        self.client: Optional[AvantisClient] = None
        self._running = False
        self._active_trades: Dict[str, Trade] = {}
        self._reconcile_task: Optional[asyncio.Task] = None

    async def initialize(self) -> None:
        settings = get_settings()
        self.client = AvantisClient(settings)

        # Log balances
        try:
            usdc = await self.client.get_usdc_balance()
            eth = await self.client.get_eth_balance()
            logger.info("Wallet balances: %.2f USDC, %.6f ETH", usdc, eth)
        except Exception as e:
            logger.warning("Could not fetch balances: %s", e)

        # Load active trades
        await self._load_active_trades()

        # Start reconciliation loop
        interval = settings.reconciliation_interval
        self._reconcile_task = asyncio.create_task(self._reconciliation_loop(interval))

        self._running = True
        logger.info("Trade Manager initialized (Avantis DEX, dry_run=%s)", settings.dry_run)

    async def shutdown(self) -> None:
        self._running = False
        if self._reconcile_task and not self._reconcile_task.done():
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
        logger.info("Trade Manager shut down")

    async def _load_active_trades(self) -> None:
        async with get_db_session() as session:
            repo = TradeRepository(session)
            trades = await repo.get_active_trades()
            for trade in trades:
                self._active_trades[trade.id] = trade
            logger.info("Loaded %d active trades", len(trades))

    # ── Trade Creation ───────────────────────────────────────

    async def create_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        targets: List[float],
        stop_loss: float,
        suggested_leverage: Optional[int] = None,
        notes: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Trade:
        settings = get_settings()

        # Map pair
        pair_index = get_pair_index(symbol)
        if pair_index is None:
            raise ValueError(f"Unknown pair: {symbol}. Supported: ETHUSDT, BTCUSDT")

        # Check max trades
        if len(self._active_trades) >= settings.max_open_trades:
            raise ValueError(f"Max open trades ({settings.max_open_trades}) reached")

        # Direction
        is_long = direction.lower() == "long"
        trade_direction = TradeDirection.LONG if is_long else TradeDirection.SHORT

        # Leverage
        leverage = suggested_leverage or settings.default_leverage

        # Sort targets
        if is_long:
            targets = sorted(targets)
        else:
            targets = sorted(targets, reverse=True)

        # Use first target as on-chain TP (Avantis supports single TP in struct)
        tp_price = targets[0] if targets else (entry_price * 1.05 if is_long else entry_price * 0.95)

        # Fetch current price
        price_symbol = get_price_symbol(pair_index)
        try:
            current_price = await get_price(price_symbol)
        except Exception as e:
            logger.error("Failed to fetch price for %s: %s", price_symbol, e)
            raise ValueError(f"Could not get current price for {symbol}")

        position_size_usd = settings.position_size_usd

        # Create trade in DB
        trade_id = str(uuid.uuid4())
        async with get_db_session() as session:
            repo = TradeRepository(session)
            trade = await repo.create_trade(
                trade_id=trade_id,
                symbol=symbol.upper(),
                direction=trade_direction,
                leverage=leverage,
                entry_price=entry_price,
                position_size_usd=position_size_usd,
                targets=targets,
                stop_loss=stop_loss,
                notes=notes,
                source=source,
                pair_index=pair_index,
            )

        logger.info(
            "Trade created: %s %s %s %dx @ %.2f, TP=%.2f, SL=%.2f, size=$%.2f",
            trade.id[:8], symbol, direction, leverage, entry_price, tp_price, stop_loss, position_size_usd,
        )

        # Execute async
        asyncio.create_task(self._execute_trade(trade, current_price, tp_price))

        return trade

    async def _execute_trade(self, trade: Trade, current_price: float, tp_price: float) -> None:
        settings = get_settings()

        try:
            pair_index = trade.pair_index if hasattr(trade, "pair_index") and trade.pair_index is not None else get_pair_index(trade.symbol)

            if settings.dry_run:
                logger.info(
                    "[DRY RUN] Would open: %s %s %dx, $%.2f collateral, price=%.2f, tp=%.2f, sl=%.2f",
                    trade.symbol,
                    trade.direction.value,
                    trade.leverage,
                    trade.position_size_usd,
                    current_price,
                    tp_price,
                    trade.stop_loss,
                )
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.update_trade_status(trade.id, TradeStatus.ACTIVE)
                    await repo.update_trade_field(trade.id, "tx_hash", "DRY_RUN")
                self._active_trades[trade.id] = trade
                return

            # Get next trade index
            trade_index = await self.client.get_next_trade_index(pair_index)

            is_long = trade.direction == TradeDirection.LONG

            tx_hash = await self.client.open_trade(
                pair_index=pair_index,
                position_size_usd=trade.position_size_usd,
                open_price=current_price,
                is_long=is_long,
                leverage=trade.leverage,
                tp_price=tp_price,
                sl_price=trade.stop_loss,
                trade_index=trade_index,
            )

            async with get_db_session() as session:
                repo = TradeRepository(session)
                await repo.update_trade_status(trade.id, TradeStatus.ACTIVE)
                await repo.update_trade_field(trade.id, "tx_hash", tx_hash)
                await repo.update_trade_field(trade.id, "onchain_index", trade_index)
                await repo.update_trade_field(trade.id, "opened_at", datetime.now(timezone.utc))

            self._active_trades[trade.id] = trade
            logger.info("Trade opened on-chain: %s tx=%s", trade.id[:8], tx_hash)

        except Exception as e:
            logger.exception("Failed to execute trade %s: %s", trade.id[:8], e)
            async with get_db_session() as session:
                repo = TradeRepository(session)
                await repo.update_trade_status(trade.id, TradeStatus.ERROR, error_message=str(e))

    # ── Reconciliation ───────────────────────────────────────

    async def _reconciliation_loop(self, interval: int) -> None:
        """
        Poll Avantis API for open positions.
        If a trade in our DB is ACTIVE but no longer on-chain, mark it closed.
        """
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running or not self.client:
                    break
                await self._reconcile()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Reconciliation error: %s", e)

    async def _reconcile(self) -> None:
        if not self._active_trades:
            return

        positions = await self.client.get_open_positions()

        # Build set of (pairIndex, index) that are still open on-chain
        open_on_chain = set()
        for pos in positions:
            pi = int(pos.get("pairIndex", -1))
            idx = int(pos.get("index", -1))
            if pi >= 0 and idx >= 0:
                open_on_chain.add((pi, idx))

        # Check each active trade
        closed_ids = []
        for trade_id, trade in list(self._active_trades.items()):
            onchain_index = getattr(trade, "onchain_index", None)
            pair_index = getattr(trade, "pair_index", None)
            if pair_index is None:
                pair_index = get_pair_index(trade.symbol)
            if onchain_index is None or pair_index is None:
                continue

            if (pair_index, onchain_index) not in open_on_chain:
                # Position closed on-chain (TP or SL hit by keepers)
                logger.info(
                    "Trade %s closed on-chain (pair=%d, index=%d)",
                    trade_id[:8], pair_index, onchain_index,
                )
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.update_trade_status(trade_id, TradeStatus.CLOSED)
                    await repo.update_trade_field(trade_id, "closed_at", datetime.now(timezone.utc))
                closed_ids.append(trade_id)

        for tid in closed_ids:
            self._active_trades.pop(tid, None)

        if closed_ids:
            logger.info("Reconciliation: %d trades marked closed", len(closed_ids))

    # ── Queries ──────────────────────────────────────────────

    async def get_active_trades(self) -> List[Trade]:
        async with get_db_session() as session:
            repo = TradeRepository(session)
            return await repo.get_active_trades()

    async def get_trade(self, trade_id: str) -> Optional[Trade]:
        async with get_db_session() as session:
            repo = TradeRepository(session)
            return await repo.get_trade(trade_id)

    async def cancel_trade(self, trade_id: str) -> bool:
        """Cancel a trade — close on-chain if active."""
        trade = self._active_trades.get(trade_id)
        if not trade:
            async with get_db_session() as session:
                repo = TradeRepository(session)
                trade = await repo.get_trade(trade_id)

        if not trade:
            return False

        settings = get_settings()
        if not settings.dry_run and self.client:
            pair_index = getattr(trade, "pair_index", None) or get_pair_index(trade.symbol)
            onchain_index = getattr(trade, "onchain_index", None)
            if pair_index is not None and onchain_index is not None:
                try:
                    await self.client.close_trade(pair_index, onchain_index)
                except Exception as e:
                    logger.error("Failed to close trade on-chain: %s", e)

        async with get_db_session() as session:
            repo = TradeRepository(session)
            await repo.update_trade_status(trade_id, TradeStatus.CANCELLED)
            await repo.update_trade_field(trade_id, "closed_at", datetime.now(timezone.utc))

        self._active_trades.pop(trade_id, None)
        return True

    async def emergency_close_all(self) -> None:
        """Close all active positions on-chain."""
        settings = get_settings()
        for trade_id, trade in list(self._active_trades.items()):
            try:
                await self.cancel_trade(trade_id)
            except Exception as e:
                logger.error("Emergency close failed for %s: %s", trade_id[:8], e)

    def get_stats(self) -> dict:
        return {"active_trades": len(self._active_trades)}
