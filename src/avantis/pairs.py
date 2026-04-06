"""
Avantis pair index mapping.
Maps Telegram signal pair names (e.g. ETHUSDT) to Avantis pairIndex values.
"""

# pairIndex on Avantis Trading contract
PAIR_MAP = {
    "ETHUSD": 0,
    "ETHUSDT": 0,
    "BTCUSD": 1,
    "BTCUSDT": 1,
}

# Binance API symbol for price fetching
PRICE_SYMBOL_MAP = {
    0: "ETHUSDT",
    1: "BTCUSDT",
}


def get_pair_index(symbol: str) -> int | None:
    """Map a signal pair name to Avantis pairIndex. Returns None if unknown."""
    return PAIR_MAP.get(symbol.upper().replace("/", ""))


def get_price_symbol(pair_index: int) -> str | None:
    """Get Binance symbol for price fetching."""
    return PRICE_SYMBOL_MAP.get(pair_index)
