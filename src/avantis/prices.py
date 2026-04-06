"""
Price fetching via Binance public API.
"""

import logging
import httpx

logger = logging.getLogger(__name__)

_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=10.0)
    return _http


async def get_price(symbol: str) -> float:
    """Fetch current price from Binance. Raises on failure."""
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    resp = await _get_http().get(url)
    resp.raise_for_status()
    return float(resp.json()["price"])
