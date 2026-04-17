"""
Database models for Avantis trade persistence.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime,
    ForeignKey, Text, Index, Enum as SQLEnum, JSON
)
from sqlalchemy.orm import DeclarativeBase, relationship, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TradeStatus(str, Enum):
    PENDING = "pending"
    OPENING = "opening"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    ERROR = "error"


class TradeDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    direction: Mapped[TradeDirection] = mapped_column(SQLEnum(TradeDirection), nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False)

    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    position_size_usd: Mapped[float] = mapped_column(Float, nullable=False)

    targets: Mapped[str] = mapped_column(JSON, nullable=False)
    current_target_idx: Mapped[int] = mapped_column(Integer, default=0)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)

    # Avantis-specific
    pair_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    onchain_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tx_hash: Mapped[Optional[str]] = mapped_column(String(66), nullable=True)

    status: Mapped[TradeStatus] = mapped_column(
        SQLEnum(TradeStatus), default=TradeStatus.PENDING, index=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Realized PnL — accumulated across partial closes, finalized at full close
    close_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    closed_qty_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("ix_trades_status_symbol", "status", "symbol"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "leverage": self.leverage,
            "entry_price": self.entry_price,
            "position_size_usd": self.position_size_usd,
            "targets": self.targets,
            "stop_loss": self.stop_loss,
            "pair_index": self.pair_index,
            "onchain_index": self.onchain_index,
            "tx_hash": self.tx_hash,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "notes": self.notes,
            "source": self.source,
            "close_price": self.close_price,
            "realized_pnl_usd": self.realized_pnl_usd,
            "closed_qty_usd": self.closed_qty_usd,
        }


class TelegramSignalMessage(Base):
    __tablename__ = "telegram_signal_messages"

    channel_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    message_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class TelegramMessage(Base):
    """Log of all messages from the watched channel."""
    __tablename__ = "telegram_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    is_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_tg_messages_channel_msg", "channel_id", "message_id"),
    )
