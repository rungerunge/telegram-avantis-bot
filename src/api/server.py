"""
FastAPI server for Telegram → Avantis bot.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..database.repository import init_database, close_database
from ..core.trade_manager import TradeManager

logger = logging.getLogger(__name__)

trade_manager: Optional[TradeManager] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global trade_manager

    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Starting Telegram → Avantis Bot...")

    await init_database()

    trade_manager = TradeManager()
    await trade_manager.initialize()

    # Import routes here to avoid circular imports
    from .routes import set_trade_manager
    set_trade_manager(trade_manager)

    # Telegram listener
    telegram_client = None
    telegram_task = None
    if settings.enable_telegram_listener:
        try:
            from ..telegram_listener import run_telegram_listener
            telegram_client, telegram_task = await run_telegram_listener(trade_manager, settings)
            if telegram_client:
                app.state.telegram_client = telegram_client
                app.state.telegram_listener_task = telegram_task
        except Exception as e:
            logger.exception("Failed to start Telegram listener: %s", e)

    logger.info("Bot started (dry_run=%s)", settings.dry_run)

    yield

    # Shutdown
    if getattr(app.state, "telegram_client", None):
        try:
            await app.state.telegram_client.disconnect()
        except Exception:
            pass
        task = getattr(app.state, "telegram_listener_task", None)
        if task and not task.done():
            task.cancel()

    if trade_manager:
        await trade_manager.shutdown()

    await close_database()
    logger.info("Bot shut down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Telegram → Avantis Bot",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})

    from .routes import router
    from .dashboard import router as dashboard_router

    app.include_router(router, prefix="/api/v1")
    app.include_router(dashboard_router)

    @app.get("/")
    async def root():
        return {"service": "Telegram → Avantis Bot", "dashboard": "/dashboard"}

    return app
