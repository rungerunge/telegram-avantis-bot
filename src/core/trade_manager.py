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
            for trade in trades:
                at = ActiveTrade(trade)
                # Restore target index from DB
                at.current_target_idx = trade.current_target_idx or 0
                self._active_trades[trade.id] = at
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

    async def _execute_trade(self, trade: Trade, current_price: float) -> None:
        settings = get_settings()

        try:
            pair_index = trade.pair_index if trade.pair_index is not None else get_pair_index(trade.symbol)

            targets = trade.targets if isinstance(trade.targets, list) else []
            is_long = trade.direction == TradeDirection.LONG

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
                await self._check_prices()
                await self._reconcile_closed()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Price monitor error: %s", e)

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

        # Calculate partial close amount
        # Each target gets equal share of the original collateral
        close_amount = at.position_size_usd / num_targets

        try:
            if is_last_target:
                # Last target — close entire remaining position
                logger.info("Last target hit — closing full remaining position for %s", at.trade_id[:8])
                await self.client.close_trade(at.pair_index, at.onchain_index, 0)
            else:
                # Partial close
                logger.info(
                    "Partial close $%.2f for %s (target %d/%d)",
                    close_amount, at.trade_id[:8], target_idx + 1, num_targets,
                )
                await self.client.close_trade(at.pair_index, at.onchain_index, close_amount)

                # Move SL to break-even (entry price) after first target,
                # then to each subsequent target
                if target_idx == 0:
                    new_sl = at.entry_price
                else:
                    new_sl = at.targets[target_idx - 1]

                logger.info("Moving SL to %.2f for %s", new_sl, at.trade_id[:8])
                await self.client.update_sl(at.pair_index, at.onchain_index, new_sl)

            at.current_target_idx += 1
            at.remaining_collateral -= close_amount

            async with get_db_session() as session:
                repo = TradeRepository(session)
                await repo.update_trade_field(at.trade_id, "current_target_idx", at.current_target_idx)

            if is_last_target:
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.update_trade_status(at.trade_id, TradeStatus.CLOSED)
                    await repo.update_trade_field(at.trade_id, "closed_at", datetime.now(timezone.utc))
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

        open_on_chain = set()
        for pos in positions:
            pi = int(pos.get("pairIndex", -1))
            idx = int(pos.get("index", -1))
            if pi >= 0 and idx >= 0:
                open_on_chain.add((pi, idx))

        closed_ids = []
        now = datetime.now(timezone.utc)
        for trade_id, at in list(self._active_trades.items()):
            if at.pair_index is None or at.onchain_index is None:
                continue
            # Grace period: don't reconcile trades opened < 90s ago (API indexing delay)
            age = (now - at.opened_at).total_seconds()
            if age < 90:
                continue
            if (at.pair_index, at.onchain_index) not in open_on_chain:
                logger.info("Trade %s closed on-chain (SL hit or liquidated)", trade_id[:8])
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.update_trade_status(trade_id, TradeStatus.CLOSED)
                    await repo.update_trade_field(trade_id, "closed_at", datetime.now(timezone.utc))
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
