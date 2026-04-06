"""
API routes for Avantis bot.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from ..core.trade_manager import TradeManager
from ..database.repository import get_db_session, TradeRepository
from ..database.models import TradeStatus

logger = logging.getLogger(__name__)
router = APIRouter()

_trade_manager: Optional[TradeManager] = None


def set_trade_manager(tm: TradeManager) -> None:
    global _trade_manager
    _trade_manager = tm


def get_trade_manager() -> TradeManager:
    if _trade_manager is None:
        raise HTTPException(status_code=503, detail="Not initialized")
    return _trade_manager


# ── Health ───────────────────────────────────────────────

@router.get("/health", tags=["System"])
async def health():
    tm = get_trade_manager()
    db_ok = True
    try:
        async with get_db_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    return {
        "status": "healthy" if db_ok else "degraded",
        "active_trades": len(tm._active_trades),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Trades ───────────────────────────────────────────────

@router.get("/trades", tags=["Trading"])
async def get_trades(limit: int = 50):
    async with get_db_session() as session:
        repo = TradeRepository(session)
        trades = await repo.get_recent_trades(limit=limit)
        return {"trades": [t.to_dict() for t in trades], "total": len(trades)}


@router.get("/trades/active", tags=["Trading"])
async def get_active_trades():
    tm = get_trade_manager()
    trades = await tm.get_active_trades()
    return {"trades": [t.to_dict() for t in trades], "total": len(trades)}


@router.get("/trade/{trade_id}", tags=["Trading"])
async def get_trade(trade_id: str):
    tm = get_trade_manager()
    trade = await tm.get_trade(trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade.to_dict()


@router.post("/trade/{trade_id}/cancel", tags=["Trading"])
async def cancel_trade(trade_id: str):
    tm = get_trade_manager()
    ok = await tm.cancel_trade(trade_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to cancel")
    return {"status": "cancelled", "trade_id": trade_id}


class EmergencyCloseRequest(BaseModel):
    confirm: bool = False


@router.post("/emergency-close", tags=["Risk"])
async def emergency_close(req: EmergencyCloseRequest):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true")
    tm = get_trade_manager()
    await tm.emergency_close_all()
    return {"status": "all positions closed"}


# ── Wallet ───────────────────────────────────────────────

@router.get("/wallet", tags=["Wallet"])
async def wallet_info():
    tm = get_trade_manager()
    if not tm.client:
        return {"error": "No client"}
    usdc = await tm.client.get_usdc_balance()
    eth = await tm.client.get_eth_balance()
    return {"usdc_balance": round(usdc, 2), "eth_balance": round(float(eth), 6)}


# ── Positions (on-chain) ────────────────────────────────

@router.get("/positions", tags=["Positions"])
async def get_positions():
    tm = get_trade_manager()
    if not tm.client:
        return {"positions": []}
    positions = await tm.client.get_open_positions()
    return {"positions": positions, "count": len(positions)}
