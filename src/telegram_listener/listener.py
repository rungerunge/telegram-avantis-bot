"""
Telegram channel listener: new messages -> wait -> parse -> create_trade on Avantis.

Ported from Binance version. Same delay approach (60s for edits), same parser.
Only the trade_manager.create_trade call changed (now targets Avantis DEX).
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from ..database.repository import TradeRepository, get_db_session
from .parser import get_block_reason, parse_signal_to_json

SIGNAL_DELAY_SECONDS = 60

logger = logging.getLogger(__name__)
logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if getattr(sys, "frozen", False):
    SESSION_PATH = str(Path(sys.executable).resolve().parent / "tg_signal_session")
else:
    SESSION_PATH = str(PROJECT_ROOT / "tg_signal_session")


def _prompt_phone() -> str:
    print("Enter your Telegram phone (e.g. +1234567890): ", end="", flush=True)
    return (input() or "").strip()


def _prompt_code() -> str:
    print("Enter the code Telegram sent you: ", end="", flush=True)
    return (input() or "").strip()


def _prompt_password() -> str:
    print("Enter your 2FA password: ", end="", flush=True)
    return (input() or "").strip()


async def run_telegram_listener(trade_manager, settings):
    """Start Telethon client, listen for signals, call trade_manager.create_trade."""
    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
        from telethon.events import NewMessage, MessageEdited
        from telethon.sessions import StringSession
    except ImportError:
        logger.error("telethon not installed. pip install telethon")
        return None, None

    api_id = (settings.telegram_api_id or "").strip()
    api_hash = (settings.telegram_api_hash or "").strip()
    channel = (settings.telegram_channel or "").strip()

    if not api_id or not api_hash or not channel:
        logger.warning("Telegram listener: missing API_ID, API_HASH, or CHANNEL. Skipping.")
        return None, None

    # Use StringSession from env var if available (for Railway/containers)
    session_string = os.environ.get("TELEGRAM_SESSION_STRING", "").strip()
    if session_string:
        session = StringSession(session_string)
        logger.info("Using TELEGRAM_SESSION_STRING for Telegram auth")
    else:
        session = SESSION_PATH

    client = TelegramClient(
        session, int(api_id), api_hash,
        connection_retries=5, retry_delay=1, auto_reconnect=True,
    )
    debug_blocks = getattr(settings, "telegram_signal_debug", False)
    pending_signals = {}

    def _channel_and_id(event):
        msg = event.message
        cid = getattr(event.chat_id, "value", event.chat_id) if hasattr(event.chat_id, "value") else event.chat_id
        return str(cid), msg.id

    def _looks_like_signal(text: str) -> bool:
        if not text:
            return False
        lower = text.lower()
        return ("entry" in lower and "target" in lower and "sl" in lower
                and ("gain" in lower or "gian" in lower) and "loss" in lower)

    async def _process_signal_after_delay(entity, channel_id: str, message_id: int):
        try:
            logger.info("Signal detected (msg %s), waiting %ds for edits...", message_id, SIGNAL_DELAY_SECONDS)
            await asyncio.sleep(SIGNAL_DELAY_SECONDS)

            pending_signals.pop((channel_id, message_id), None)

            async with get_db_session() as session:
                repo = TradeRepository(session)
                status = await repo.get_telegram_message_status(channel_id, message_id)
            if status == "complete":
                return

            try:
                msg = await client.get_messages(entity, ids=message_id)
            except Exception as e:
                logger.error("Could not fetch message %s: %s", message_id, e)
                return

            if not msg:
                return

            text = (msg.text or getattr(msg, "message", None) or "").strip()
            if not text:
                return

            block_reason, obj = get_block_reason(text)
            if block_reason is not None:
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.set_telegram_message_status(channel_id, message_id, "incomplete")
                if debug_blocks:
                    logger.info("Signal BLOCKED: %s", block_reason)
                return

            pair = obj["pair"].upper()
            direction = obj["direction"].lower()
            entry = float(obj["entry"])
            targets = [float(t) for t in obj["targets"]]
            stop_loss = float(obj["stop_loss"])
            suggested_leverage = obj.get("suggested_leverage")
            if suggested_leverage is not None:
                suggested_leverage = int(suggested_leverage)

            try:
                trade = await trade_manager.create_trade(
                    symbol=pair,
                    direction=direction,
                    entry_price=entry,
                    targets=targets,
                    stop_loss=stop_loss,
                    suggested_leverage=suggested_leverage,
                    notes=(obj.get("notes") or "").strip() or None,
                    source="telegram",
                )
                async with get_db_session() as session:
                    repo = TradeRepository(session)
                    await repo.set_telegram_message_status(channel_id, message_id, "complete", trade_id=trade.id)
                logger.info(
                    "Signal -> trade: %s %s @ %s [%d targets]",
                    pair, direction, trade.id[:8], len(targets),
                )
            except Exception as e:
                logger.exception("create_trade failed: %s", e)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Signal processing error: %s", e)

    async def on_new_message(event):
        msg = event.message
        text = (msg.text or getattr(msg, "message", None) or "").strip()
        if not _looks_like_signal(text):
            return

        channel_id, message_id = _channel_and_id(event)
        key = (channel_id, message_id)
        if key in pending_signals:
            return

        async with get_db_session() as session:
            repo = TradeRepository(session)
            status = await repo.get_telegram_message_status(channel_id, message_id)
        if status == "complete":
            return

        async with get_db_session() as session:
            repo = TradeRepository(session)
            await repo.set_telegram_message_status(channel_id, message_id, "pending")

        entity = event.chat
        task = asyncio.create_task(_process_signal_after_delay(entity, channel_id, message_id))
        pending_signals[key] = task

    async def on_message_edited(event):
        msg = event.message
        text = (msg.text or getattr(msg, "message", None) or "").strip()
        if not _looks_like_signal(text):
            return
        channel_id, message_id = _channel_and_id(event)
        if (channel_id, message_id) in pending_signals:
            logger.info("Signal message %s edited, will use updated content", message_id)

    async def _do_login():
        await client.connect()
        if await client.is_user_authorized():
            return
        loop = asyncio.get_event_loop()
        phone = (getattr(settings, "telegram_phone", None) or "").strip()
        if not phone:
            phone = await loop.run_in_executor(None, _prompt_phone)
        if not phone:
            raise ValueError("Phone required for Telegram login")
        await client.send_code_request(phone.replace(" ", "").strip())
        code = (getattr(settings, "telegram_code", None) or "").strip()
        if not code:
            code = await loop.run_in_executor(None, _prompt_code)
        if not code:
            raise ValueError("Code required for Telegram login")
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = await loop.run_in_executor(None, _prompt_password)
            await client.sign_in(password=password)

    async def connect_and_listen():
        handlers_added = False
        entity = None
        consecutive_failures = 0

        while True:
            try:
                if not client.is_connected():
                    await _do_login()

                me = await client.get_me()
                logger.info("Telegram: logged in as %s", getattr(me, "first_name", me))

                if entity is None:
                    channel_id = channel.strip()
                    if channel_id.lstrip("-").isdigit():
                        channel_id = int(channel_id)
                    try:
                        entity = await client.get_entity(channel_id)
                    except Exception:
                        await client.get_dialogs()
                        entity = await client.get_entity(channel_id)

                if not handlers_added:
                    client.add_event_handler(on_new_message, NewMessage(chats=[entity]))
                    client.add_event_handler(on_message_edited, MessageEdited(chats=[entity]))
                    handlers_added = True
                    name = getattr(entity, "title", None) or str(entity)
                    logger.info("Telegram: watching %s", name)

                consecutive_failures = 0
                await client.run_until_disconnected()

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures >= 10:
                    logger.critical("Telegram: %d failures, stopping", consecutive_failures)
                    return
                logger.error("Telegram error (%d): %s", consecutive_failures, e)

            await asyncio.sleep(15)

    task = asyncio.create_task(connect_and_listen())
    return client, task
