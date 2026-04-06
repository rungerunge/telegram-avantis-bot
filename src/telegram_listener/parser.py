"""
Parse Telegram signal messages into bot trade format.
From tg experiment/get_recent.py — Entry, Target, SL, Gain, Loss; no pending; no ... in prices.
"""

import re
from typing import Any, Dict, Optional, Tuple


REQUIRED_KEYWORDS = ("Entry", "Target", "SL", "Loss")
# Note: "Gain" checked separately to handle "Gian" typo


def is_signal_message(text: str) -> bool:
    """True if message looks like a trade signal (has Entry, Target, SL, Gain/Gian, Loss)."""
    if not text or not text.strip():
        return False
    lower = text.lower()
    has_gain = "gain" in lower or "gian" in lower  # Handle common typo
    return has_gain and all(kw.lower() in lower for kw in REQUIRED_KEYWORDS)


def _get_entry_line(lines: list) -> str:
    """Line that starts with 'Entry' (optional colon)."""
    return next(
        (line for line in lines if re.match(r"^Entry\s*:?\s*", line, re.IGNORECASE)),
        "",
    )


def is_pending_signal(text: str) -> bool:
    """True if this is a PENDING signal we should filter OUT."""
    if not text or not text.strip():
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    title_line = lines[0]
    entry_line = _get_entry_line(lines)
    return "pending" in title_line.lower() or "pending" in entry_line.lower()


def has_abbreviated_prices(text: str) -> bool:
    """True if message has "..." in price lines — filter these out."""
    if not text or "..." not in text:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if "..." not in line:
            continue
        if re.match(r"^Entry\s*:?\s*", line, re.IGNORECASE):
            return True
        if re.match(r"^Target\s*\d+\s*:", line, re.IGNORECASE):
            return True
        if re.search(r"^SL\s*:", line, re.IGNORECASE):
            return True
    return False


def _extract_number(s: str):
    """Extract first number from a string (handles $, commas, trailing usd). Returns int or float or None."""
    if not s:
        return None
    m = re.search(r"\$?([\d,]+\.?\d*)", s)
    if not m:
        return None
    try:
        raw = m.group(1).replace(",", "")
        return int(raw) if "." not in raw else float(raw)
    except (ValueError, TypeError):
        return None


def parse_signal_to_json(text: str) -> dict:
    """
    Parse a signal message into JSON matching CreateTradeRequest:
    {"pair": "ETHUSDT", "direction": "long", "entry": 3215, "targets": [...], "stop_loss": 3192, "suggested_leverage": 100, "notes": ""}
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {}

    title = lines[0].lower()
    pair_match = re.search(r"([A-Za-z0-9]+)/USDT", lines[0], re.IGNORECASE)
    pair = (pair_match.group(1).upper() + "USDT") if pair_match else ""

    direction = "short" if "short" in title else ("long" if "long" in title else "")

    lev_match = re.search(r"(\d+)\s*x\b", lines[0], re.IGNORECASE)
    suggested_leverage = int(lev_match.group(1)) if lev_match else None

    entry_line = _get_entry_line(lines)
    entry = _extract_number(entry_line)

    target_lines = []
    for l in lines:
        m = re.match(r"target\s*(\d+)\s*:(.*)", l, re.IGNORECASE | re.DOTALL)
        if m:
            target_lines.append((int(m.group(1)), m.group(2).strip()))
    target_lines.sort(key=lambda x: x[0])
    targets = [_extract_number(rest) for _, rest in target_lines]
    targets = [t for t in targets if t is not None]

    sl_line = next((l for l in lines if re.search(r"SL\s*:", l, re.IGNORECASE)), "")
    stop_loss = _extract_number(sl_line)

    return {
        "pair": pair,
        "direction": direction,
        "entry": entry,
        "targets": targets,
        "stop_loss": stop_loss,
        "suggested_leverage": suggested_leverage,
        "notes": "",
    }


def get_block_reason(text: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    For debugging: determine why a message would be blocked, or return (None, parsed) if it would pass.
    Returns (block_reason, parsed_obj). block_reason is a short code; parsed_obj is from parse_signal_to_json when applicable.
    """
    if not text or not text.strip():
        return ("empty_message", None)
    lower = text.lower()
    missing_kw = [kw for kw in REQUIRED_KEYWORDS if kw.lower() not in lower]
    if missing_kw:
        return (f"missing_keywords ({', '.join(missing_kw)})", None)
    # Check for Gain/Gian (common typo)
    if "gain" not in lower and "gian" not in lower:
        return ("missing_keywords (Gain)", None)
    if is_pending_signal(text):
        return ("pending_signal", None)
    if has_abbreviated_prices(text):
        return ("abbreviated_prices_in_entry_target_or_sl", None)
    obj = parse_signal_to_json(text)
    if not obj.get("pair") or not obj.get("direction") or obj.get("entry") is None:
        return ("parse_missing_pair_direction_or_entry", obj)
    if not obj.get("targets") or obj.get("stop_loss") is None:
        return ("parse_missing_targets_or_stop_loss", obj)
    pair = (obj.get("pair") or "").upper()
    if not (pair.endswith("USDT") or pair.endswith("USD")):
        return ("pair_must_end_with_USDT_or_USD", obj)
    direction = (obj.get("direction") or "").lower()
    if direction not in ("long", "short"):
        return ("direction_must_be_long_or_short", obj)
    return (None, obj)
