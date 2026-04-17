"""
Trade Manager for Avantis DEX.

Multi-target strategy:
- Open trade with SL on-chain, TP disabled (0)
- Bot monitors price every 15s
- At each target: partial close + move SL to break-even / previous target
- Keepers handle SL on-chain (safe even if bot goes down)
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..config import get_settings
from ..avantis.client import AvantisClient
from ..lighter.client import LighterClient
from ..avantis.pairs import get_pair_index, get_price_symbol
from ..avantis.prices import get_price
from ..database.models import Trade, TradeStatus, TradeDirection
from ..database.repository import TradeRepository, get_db_session

logger = logging.getLogger(__name__)


class ActiveTrade:
    """In-memory state for an active trade being monitored."""
    def __init__(self, trade: Trade):
        self.trade = trade
        self.trade_id = trade.id
        self.symbol = trade.symbol
        self.pair_index = trade.pair_index
        self.onchain_index = trade.onchain_index
        self.direction = trade.direction
        self.entry_price = trade.entry_price
        self.leverage = trade.leverage
        self.position_size_usd = trade.position_size_usd
        self.targets: List[float] = trade.targets if isinstance(trade.targets, list) else []
        self.stop_loss = trade.stop_loss
        self.current_target_idx = 0
        self.is_long = trade.direction == TradeDirection.LONG
        self.remaining_collateral = trade.position_size_usd
        self.opened_at = datetime.now(timezone.utc)  # grace period for API indexing
        # Running realized PnL — accumulated across partial closes
        self.realized_pnl_usd: float = float(trade.realized_pnl_usd or 0.0)
        self.closed_qty_usd: float = float(trade.closed_qty_usd or 0.0)


def _compute_realized_pnl(at: "ActiveTrade", close_price: float, close_qty_usd: float) -> float:
    """PnL for closing close_qty_usd of collateral at close_price.
    Matches Lighter's isolated-margin semantics: pnl = notional_delta * leverage / entry.
    """
    if close_price is None or at.entry_price <= 0 or close_qty_usd <= 0:
        return 0.0
    price_change = (close_price - at.entry_price) / at.entry_price
    if not at.is_long:
        price_change = -price_change
    return price_change * close_qty_usd * at.leverage


class TradeManager:
    """
    Manages trade lifecycle on Avantis DEX.

    Flow:
    1. Signal → open trade (TP=0, SL on-chain)
    2. Price monitor checks every 15s
    3. When target N hit → partial close (1/num_targets) + move SL
    4. When all targets hit or SL hit → trade fully closed
    """

    def __init__(self):
        self.client: Optional[AvantisClient] = None
        self._running = False
        self._active_trades: Dict[str, ActiveTrade] = {}
        # Trades waiting for price to reach signal entry before market-opening.
        # trade_id → {"trade": Trade, "registered_at": datetime, "last_log_at": datetime}
        self._pending_entries: Dict[str, dict] = {}
        self._monitor_task: Optional[asyncio.Task] = None

    async def initialize(self) -> None:
        settings = get_settings()
        exchange = os.environ.get("EXCHANGE", "avantis").lower()
        if exchange == "lighter":
            self.client = LighterClient(settings)
            logger.info("Using LIGHTER executor (0%% fees)")
        else:
            self.client = AvantisClient(settings)
            logger.info("Using AVANTIS executor")

        try:
            usdc = await self.client.get_usdc_balance()
            eth = await self.client.get_eth_balance()
            logger.info("Wallet balances: %.2f USDC, %.6f ETH", usdc, eth)
        except Exception as e:
            logger.warning("Could not fetch balances: %s", e)

        await self._load_active_trades()

        interval = settings.reconciliation_interval
        self._monitor_task = asyncio.create_task(self._price_monitor_loop(interval))

        self._running = True
        logger.info("Trade Manager initialized (Avantis DEX, dry_run=%s)", settings.dry_run)

    async def shutdown(self) -> None:
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Trade Manager shut down")

    async def _load_active_trades(self) -> None:
        async with get_db_session() as session:
            repo = TradeRepository(session)
            trades = await repo.get_active_trades()
            active_count = 0
            pending_count = 0
            now = datetime.now(timezone.utc)
            for trade in trades:
                # PENDING = not yet opened on exchange. Route to entry-wait queue
                # so price monitor resumes waiting for signal entry instead of
                # treating it as a live position.
                if trade.status == TradeStatus.PENDING:
                    self._pending_entries[trade.id] = {
                        "trade": trade,
                        "registered_at": trade.created_at or now,
                        "last_log_at": now,
                    }
                    pending_count += 1
                    continue
                at = ActiveTrade(trade)
                at.current_target_idx = trade.current_target_idx or 0
                self._active_trades[trade.id] = at
                active_count += 1
            logger.info("Loaded %d active + %d pending entries from DB", active_count, pending_count)

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

        pair_index = get_pair_index(symbol)
        if pair_index is None:
            raise ValueError(f"Unknown pair: {symbol}. Supported: ETHUSDT, BTCUSDT")

        if len(self._active_trades) >= settings.max_open_trades:
            raise ValueError(f"Max open trades ({settings.max_open_trades}) reached")

        is_long = direction.lower() == "long"
        trade_direction = TradeDirection.LONG if is_long else TradeDirection.SHORT
        leverage = suggested_leverage or settings.default_leverage

        # Sort targets
        if is_long:
            targets = sorted(targets)
        else:
            targets = sorted(targets, reverse=True)

        # Fetch current price
        price_symbol = get_price_symbol(pair_index)
        try:
            current_price = await get_price(price_symbol)
        except Exception as e:
            raise ValueError(f"Could not get price for {symbol}: {e}")

        position_size_usd = settings.position_size_usd

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
            "Trade created: %s %s %s %dx @ %.2f, targets=%s, SL=%.2f, size=$%.2f",
            trade.id[:8], symbol, direction, leverage, entry_price, targets, stop_loss, position_size_usd,
        )

        asyncio.create_task(self._execute_trade(trade, current_price))
        return trade

    def _entry_condition_met(self, trade: Trade, current_price: float) -> bool:
        """True if current price is at signal entry or BETTER (so fill beats or equals signal).
        SHORT: current >= entry (shorting higher is better)
        LONG:  current <= entry (longing lower is better)
        Applies tolerance (entry_wait_tolerance_pct) for slightly worse fills if configured.
        """
        settings = get_settings()
        if not settings.entry_wait_enabled:
            return True
        tol = max(0.0, settings.entry_wait_tolerance_pct) / 100.0
        is_long = trade.direction == TradeDirection.LONG
        if is_long:
            # Willing to pay up to entry * (1 + tol)
            return current_price <= trade.entry_price * (1 + tol)
        else:
            # Willing to short down to entry * (1 - tol)
            return current_price >= trade.entry_price * (1 - tol)

    async def _execute_trade(self, trade: Trade, current_price: float) -> None:
        settings = get_settings()

        try:
            pair_index = trade.pair_index if trade.pair_index is not None else get_pair_index(trade.symbol)

            targets = trade.targets if isinstance(trade.targets, list) else []
            is_long = trade.direction == TradeDirection.LONG

            # Entry gating: if price is worse than signal entry, defer instead of
            # market-filling at a bad price. Waits in _pending_entries; price
            # monitor re-checks every interval.
            if not self._entry_condition_met(trade, current_price):
                side = "LONG" if is_long else "SHORT"
                needed = "≤" if is_long else "≥"
                logger.info(
                    "[ENTRY WAIT] %s %s signal=%.2f current=%.2f (need %s %.2f) — queued, trade=%s",
                    trade.symbol, side, trade.entry_price, current_price, needed, trade.entry_price, trade.id[:8],
                )
                now = datetime.now(timezone.utc)
                self._pending_entries[trade.id] = {
                    "trade": trade, "registered_at": now, "last_log_at": now,
                }
                # Status stays PENDING (already set by create_trade)
                return

            if settings.dry_run:
                logger.info(
                    "[DRY RUN] Would open: %s %s %dx, $%.2f, price=%.2f, TP=0(bot-managed), SL=%.2f, targets=%s",
                    trade.symbol, trade.direction.value, trade.leverage,
                    trade.position_size_usd, current_price, trade.stop_loss, targets,
                )
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.update_trade_status(trade.id, TradeStatus.ACTIVE)
                    await repo.update_trade_field(trade.id, "tx_hash", "DRY_RUN")
                self._active_trades[trade.id] = ActiveTrade(trade)
                return

            trade_index = await self.client.get_next_trade_index(pair_index)

            # Set on-chain TP to last target as safety net
            # Bot manages partial closes at each target before that
            # SL is on-chain for safety
            last_target = targets[-1] if targets else 0
            tx_hash = await self.client.open_trade(
                pair_index=pair_index,
                position_size_usd=trade.position_size_usd,
                open_price=current_price,
                is_long=is_long,
                leverage=trade.leverage,
                tp_price=last_target,  # Last target as on-chain safety TP
                sl_price=trade.stop_loss,
                trade_index=trade_index,
            )

            async with get_db_session() as session:
                repo = TradeRepository(session)
                await repo.update_trade_status(trade.id, TradeStatus.ACTIVE)
                await repo.update_trade_field(trade.id, "tx_hash", tx_hash)
                await repo.update_trade_field(trade.id, "onchain_index", trade_index)
                await repo.update_trade_field(trade.id, "opened_at", datetime.now(timezone.utc))

            # Reload trade with onchain_index set
            async with get_db_session() as session:
                repo = TradeRepository(session)
                trade = await repo.get_trade(trade.id)

            at = ActiveTrade(trade)
            self._active_trades[trade.id] = at
            logger.info("Trade opened on-chain: %s tx=%s (TP=bot-managed, SL=%.2f)", trade.id[:8], tx_hash, trade.stop_loss)

        except Exception as e:
            logger.exception("Failed to execute trade %s: %s", trade.id[:8], e)
            async with get_db_session() as session:
                repo = TradeRepository(session)
                await repo.update_trade_status(trade.id, TradeStatus.ERROR, error_message=str(e))

    # ── Price Monitor (handles partial closes + SL movement) ─

    async def _price_monitor_loop(self, interval: int) -> None:
        """Every N seconds: check prices, trigger partial closes at targets, move SL."""
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running or not self.client:
                    break
                await self._check_pending_entries()
                await self._check_prices()
                await self._reconcile_closed()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Price monitor error: %s", e)

    async def _check_pending_entries(self) -> None:
        """For each trade waiting on entry: fill it if price reaches signal, or cancel on timeout."""
        if not self._pending_entries:
            return

        settings = get_settings()
        now = datetime.now(timezone.utc)
        max_wait = settings.entry_wait_max_minutes * 60
        price_cache: Dict[str, float] = {}

        for trade_id, info in list(self._pending_entries.items()):
            trade: Trade = info["trade"]
            price_sym = get_price_symbol(trade.pair_index)
            if price_sym not in price_cache:
                try:
                    price_cache[price_sym] = await get_price(price_sym)
                except Exception as e:
                    logger.debug("pending entry: price fetch failed for %s: %s", trade.symbol, e)
                    continue
            current_price = price_cache[price_sym]

            # Timeout — give up
            age = (now - info["registered_at"]).total_seconds()
            if age > max_wait:
                logger.warning(
                    "[ENTRY TIMEOUT] %s %s never reached signal entry %.2f within %dm — cancelling (current=%.2f)",
                    trade.symbol, trade.direction.value, trade.entry_price,
                    settings.entry_wait_max_minutes, current_price,
                )
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.update_trade_status(trade_id, TradeStatus.CANCELLED)
                    await repo.update_trade_field(trade_id, "closed_at", now)
                self._pending_entries.pop(trade_id, None)
                continue

            # Price reached signal entry (or better) — fill now
            if self._entry_condition_met(trade, current_price):
                logger.info(
                    "[ENTRY FILL] %s %s reached %.2f (signal %.2f) — executing, waited %ds",
                    trade.symbol, trade.direction.value, current_price, trade.entry_price, int(age),
                )
                self._pending_entries.pop(trade_id, None)
                # Re-fetch trade row (status, etc.) and execute
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    fresh = await repo.get_trade(trade_id)
                if fresh is None or fresh.status.value not in ("pending", "opening"):
                    logger.info("Trade %s no longer pending (%s) — skip fill", trade_id[:8], fresh.status.value if fresh else "missing")
                    continue
                asyncio.create_task(self._execute_trade(fresh, current_price))
                continue

            # Still waiting — periodic status log (every ~2 min)
            last_log_age = (now - info["last_log_at"]).total_seconds()
            if last_log_age > 120:
                side = "LONG" if trade.direction == TradeDirection.LONG else "SHORT"
                needed = "≤" if trade.direction == TradeDirection.LONG else "≥"
                remaining = int(max_wait - age)
                logger.info(
                    "[ENTRY WAIT] %s %s current=%.2f signal=%.2f (need %s %.2f) waiting %ds more",
                    trade.symbol, side, current_price, trade.entry_price, needed, trade.entry_price, remaining,
                )
                info["last_log_at"] = now

    async def _check_prices(self) -> None:
        """Check current prices against active trade targets."""
        if not self._active_trades:
            return

        settings = get_settings()

        # Group trades by price symbol to minimize API calls
        price_cache: Dict[str, float] = {}

        for trade_id, at in list(self._active_trades.items()):
            if at.current_target_idx >= len(at.targets):
                continue  # All targets hit, waiting for reconcile to close
            if at.pair_index is None or at.onchain_index is None:
                continue

            price_sym = get_price_symbol(at.pair_index)
            if not price_sym:
                continue

            # Fetch price (cached per symbol)
            if price_sym not in price_cache:
                try:
                    price_cache[price_sym] = await get_price(price_sym)
                except Exception as e:
                    logger.debug("Price fetch failed for %s: %s", price_sym, e)
                    continue

            current_price = price_cache[price_sym]
            target_price = at.targets[at.current_target_idx]

            # Check if target hit
            target_hit = False
            if at.is_long and current_price >= target_price:
                target_hit = True
            elif not at.is_long and current_price <= target_price:
                target_hit = True

            if not target_hit:
                continue

            logger.info(
                "TARGET %d HIT for %s: price=%.2f >= target=%.2f",
                at.current_target_idx + 1, at.symbol, current_price, target_price,
            )

            if settings.dry_run:
                logger.info("[DRY RUN] Would partial close + move SL for %s", trade_id[:8])
                at.current_target_idx += 1
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.update_trade_field(trade_id, "current_target_idx", at.current_target_idx)
                continue

            await self._handle_target_hit(at)

    async def _handle_target_hit(self, at: ActiveTrade) -> None:
        """Partial close at target and move SL."""
        num_targets = len(at.targets)
        target_idx = at.current_target_idx
        is_last_target = (target_idx == num_targets - 1)
        target_price = at.targets[target_idx]

        # Calculate partial close amount
        # Each target gets equal share of the original collateral
        close_amount = at.position_size_usd / num_targets

        try:
            if is_last_target:
                # Last target — close entire remaining position
                logger.info("Last target hit — closing full remaining position for %s", at.trade_id[:8])
                await self.client.close_trade(at.pair_index, at.onchain_index, 0)
                close_qty = max(0.0, at.remaining_collateral)
            else:
                # Partial close
                logger.info(
                    "Partial close $%.2f for %s (target %d/%d)",
                    close_amount, at.trade_id[:8], target_idx + 1, num_targets,
                )
                await self.client.close_trade(at.pair_index, at.onchain_index, close_amount)
                close_qty = close_amount

                # Move SL to break-even (entry price) after first target,
                # then to each subsequent target
                if target_idx == 0:
                    new_sl = at.entry_price
                else:
                    new_sl = at.targets[target_idx - 1]

                logger.info("Moving SL to %.2f for %s", new_sl, at.trade_id[:8])
                await self.client.update_sl(at.pair_index, at.onchain_index, new_sl)

            # Realized PnL for this partial/full close (uses target price as fill price)
            pnl_slice = _compute_realized_pnl(at, target_price, close_qty)
            at.realized_pnl_usd += pnl_slice
            at.closed_qty_usd += close_qty
            logger.info(
                "Realized PnL slice for %s: %+.2f (cumulative %+.2f on $%.2f closed)",
                at.trade_id[:8], pnl_slice, at.realized_pnl_usd, at.closed_qty_usd,
            )

            at.current_target_idx += 1
            at.remaining_collateral -= close_amount

            async with get_db_session() as session:
                repo = TradeRepository(session)
                await repo.update_trade_field(at.trade_id, "current_target_idx", at.current_target_idx)
                await repo.update_trade_field(at.trade_id, "realized_pnl_usd", round(at.realized_pnl_usd, 4))
                await repo.update_trade_field(at.trade_id, "closed_qty_usd", round(at.closed_qty_usd, 4))

            if is_last_target:
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.update_trade_status(at.trade_id, TradeStatus.CLOSED)
                    await repo.update_trade_field(at.trade_id, "closed_at", datetime.now(timezone.utc))
                    await repo.update_trade_field(at.trade_id, "close_price", target_price)
                self._active_trades.pop(at.trade_id, None)
                logger.info("Trade %s fully closed (all targets hit)", at.trade_id[:8])

        except Exception as e:
            logger.error("Failed to handle target hit for %s: %s", at.trade_id[:8], e)

    async def _reconcile_closed(self) -> None:
        """Check if any active trades were closed on-chain (SL hit by keepers)."""
        if not self._active_trades or not self.client:
            return

        positions = await self.client.get_open_positions()

        # None = API error — skip reconciliation to avoid false closures
        if positions is None:
            return

        # For Lighter: positions merge per pair, so check by pair_index only
        open_pairs = set()
        for pos in positions:
            pi = int(pos.get("pairIndex", -1))
            if pi >= 0:
                open_pairs.add(pi)

        closed_ids = []
        now = datetime.now(timezone.utc)
        for trade_id, at in list(self._active_trades.items()):
            if at.pair_index is None:
                continue
            # Grace period: don't reconcile trades opened < 90s ago (API indexing delay)
            age = (now - at.opened_at).total_seconds()
            if age < 90:
                continue
            # Only mark closed if NO position exists for this pair at all
            if at.pair_index not in open_pairs:
                # Fetch current market price to estimate close price + realized PnL.
                # Unknown whether SL hit exactly, got slipped, or liquidated — this is
                # an approximation using the current market at detection time.
                close_price = None
                try:
                    from ..avantis.pairs import get_price_symbol
                    price_sym = get_price_symbol(at.symbol) if hasattr(at, "symbol") else at.symbol.upper()
                    close_price = await get_price(price_sym)
                except Exception as e:
                    logger.debug("Reconcile: price fetch failed for %s: %s", at.symbol, e)

                if close_price is not None and at.remaining_collateral > 0:
                    pnl_slice = _compute_realized_pnl(at, close_price, at.remaining_collateral)
                    at.realized_pnl_usd += pnl_slice
                    at.closed_qty_usd += at.remaining_collateral
                    logger.info(
                        "Trade %s reconciled closed at ~%.2f: PnL %+.2f (total %+.2f)",
                        trade_id[:8], close_price, pnl_slice, at.realized_pnl_usd,
                    )
                else:
                    logger.info("Trade %s closed on-chain (SL hit or liquidated) — PnL unknown", trade_id[:8])

                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.update_trade_status(trade_id, TradeStatus.CLOSED)
                    await repo.update_trade_field(trade_id, "closed_at", datetime.now(timezone.utc))
                    if close_price is not None:
                        await repo.update_trade_field(trade_id, "close_price", close_price)
                        await repo.update_trade_field(trade_id, "realized_pnl_usd", round(at.realized_pnl_usd, 4))
                        await repo.update_trade_field(trade_id, "closed_qty_usd", round(at.closed_qty_usd, 4))
                closed_ids.append(trade_id)

        for tid in closed_ids:
            self._active_trades.pop(tid, None)

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
        at = self._active_trades.get(trade_id)
        if not at:
            async with get_db_session() as session:
                repo = TradeRepository(session)
                trade = await repo.get_trade(trade_id)
            if not trade:
                return False
            at = ActiveTrade(trade)

        settings = get_settings()
        if not settings.dry_run and self.client and at.pair_index is not None and at.onchain_index is not None:
            try:
                await self.client.close_trade(at.pair_index, at.onchain_index)
            except Exception as e:
                logger.error("Failed to close on-chain: %s", e)

        async with get_db_session() as session:
            repo = TradeRepository(session)
            await repo.update_trade_status(trade_id, TradeStatus.CANCELLED)
            await repo.update_trade_field(trade_id, "closed_at", datetime.now(timezone.utc))

        self._active_trades.pop(trade_id, None)
        return True

    async def emergency_close_all(self) -> None:
        for trade_id in list(self._active_trades.keys()):
            try:
                await self.cancel_trade(trade_id)
            except Exception as e:
                logger.error("Emergency close failed for %s: %s", trade_id[:8], e)

    def get_stats(self) -> dict:
        return {"active_trades": len(self._active_trades)}
