"""
Database repository for Avantis trade operations.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator, List, Optional, Any

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select, update

from .models import Base, Trade, TradeStatus, TradeDirection, TelegramSignalMessage, TelegramMessage
from ..config import get_settings

logger = logging.getLogger(__name__)

_engine = None
_session_factory = None


async def init_database() -> None:
    global _engine, _session_factory
    settings = get_settings()

    _engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Database initialized")


async def close_database() -> None:
    global _engine
    if _engine:
        await _engine.dispose()


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized")
    session = _session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


class TradeRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_trade(
        self,
        trade_id: str,
        symbol: str,
        direction: TradeDirection,
        leverage: int,
        entry_price: float,
        position_size_usd: float,
        targets: list,
        stop_loss: float,
        notes: Optional[str] = None,
        source: Optional[str] = None,
        pair_index: Optional[int] = None,
    ) -> Trade:
        trade = Trade(
            id=trade_id,
            symbol=symbol,
            direction=direction,
            leverage=leverage,
            entry_price=entry_price,
            position_size_usd=position_size_usd,
            targets=targets,
            stop_loss=stop_loss,
            pair_index=pair_index,
            notes=notes,
            source=source,
        )
        self.session.add(trade)
        await self.session.flush()
        return trade

    async def get_trade(self, trade_id: str) -> Optional[Trade]:
        result = await self.session.execute(
            select(Trade).where(Trade.id == trade_id)
        )
        return result.scalar_one_or_none()

    async def get_active_trades(self) -> List[Trade]:
        result = await self.session.execute(
            select(Trade).where(
                Trade.status.in_([TradeStatus.ACTIVE, TradeStatus.OPENING, TradeStatus.PENDING])
            )
        )
        return list(result.scalars().all())

    async def get_recent_trades(self, limit: int = 50) -> List[Trade]:
        result = await self.session.execute(
            select(Trade).order_by(Trade.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def update_trade_status(
        self, trade_id: str, status: TradeStatus, error_message: Optional[str] = None
    ) -> None:
        values: dict = {"status": status}
        if error_message:
            values["error_message"] = error_message
        await self.session.execute(
            update(Trade).where(Trade.id == trade_id).values(**values)
        )

    async def update_trade_field(self, trade_id: str, field: str, value: Any) -> None:
        await self.session.execute(
            update(Trade).where(Trade.id == trade_id).values(**{field: value})
        )

    async def get_daily_pnl(self) -> float:
        """Placeholder — Avantis PnL tracking is on-chain."""
        return 0.0

    # ── Telegram message tracking ────────────────────────────

    async def get_telegram_message_status(self, channel_id: str, message_id: int) -> Optional[str]:
        result = await self.session.execute(
            select(TelegramSignalMessage.status).where(
                TelegramSignalMessage.channel_id == channel_id,
                TelegramSignalMessage.message_id == message_id,
            )
        )
        row = result.scalar_one_or_none()
        return row

    async def set_telegram_message_status(
        self, channel_id: str, message_id: int, status: str, trade_id: Optional[str] = None
    ) -> None:
        existing = await self.get_telegram_message_status(channel_id, message_id)
        if existing is not None:
            values: dict = {"status": status, "updated_at": datetime.now(timezone.utc)}
            if trade_id:
                values["trade_id"] = trade_id
            await self.session.execute(
                update(TelegramSignalMessage)
                .where(
                    TelegramSignalMessage.channel_id == channel_id,
                    TelegramSignalMessage.message_id == message_id,
                )
                .values(**values)
            )
        else:
            msg = TelegramSignalMessage(
                channel_id=channel_id,
                message_id=message_id,
                status=status,
                trade_id=trade_id,
            )
            self.session.add(msg)
        await self.session.flush()

    # ── Telegram message log ─────────────────────────────────

    async def log_telegram_message(
        self, channel_id: str, message_id: int, text: str,
        is_edit: bool = False, is_signal: bool = False, block_reason: str = None,
    ) -> None:
        msg = TelegramMessage(
            channel_id=channel_id,
            message_id=message_id,
            text=text,
            is_edit=is_edit,
            is_signal=is_signal,
            block_reason=block_reason,
        )
        self.session.add(msg)
        await self.session.flush()

    async def get_telegram_messages(self, limit: int = 50) -> list:
        result = await self.session.execute(
            select(TelegramMessage).order_by(TelegramMessage.received_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
