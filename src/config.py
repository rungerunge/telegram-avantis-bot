"""
Configuration for Telegram → Avantis DEX auto-trader.
All settings from environment variables.
"""

from typing import Optional
from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    log_level: str = Field(default="INFO")

    # Base chain / Avantis
    wallet_address: str = Field(default="")
    private_key: str = Field(default="")
    base_rpc_http: str = Field(default="")
    base_rpc_wss: str = Field(default="")

    # Avantis contracts (Base mainnet)
    trading_contract: str = Field(default="0x44914408af82bC9983bbb330e3578E1105e11d4e")
    trading_storage: str = Field(default="0x8a311D7048c35985aa31C131B9A13e03a5f7422d")
    usdc_address: str = Field(default="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")

    # Trading parameters
    dry_run: bool = Field(default=True, description="Log trades but don't submit on-chain")
    position_size_usd: float = Field(default=100.0, description="Collateral per trade in USD")
    max_open_trades: int = Field(default=10)
    default_leverage: int = Field(default=20, description="Default leverage if signal doesn't specify")
    slippage_percent: float = Field(default=1.0, description="Slippage tolerance %")
    execution_fee_eth: float = Field(default=0.00001, description="ETH sent as msg.value for execution")

    # Reconciliation
    reconciliation_interval: int = Field(default=15, description="Seconds between position checks")

    # Entry gating — don't market-open at a worse price than the signal entry.
    # For SHORT: wait until current >= entry_price. For LONG: wait until current <= entry_price.
    entry_wait_enabled: bool = Field(default=True, description="Wait for price to reach signal entry before opening")
    entry_wait_tolerance_pct: float = Field(default=0.0, description="Allow entry this % worse than signal (0 = strict)")
    entry_wait_max_minutes: int = Field(default=30, description="Cancel pending entry if not reached within N minutes")

    # Telegram signal listener
    enable_telegram_listener: bool = Field(default=False)
    telegram_api_id: str = Field(default="")
    telegram_api_hash: str = Field(default="")
    telegram_channel: str = Field(default="")
    telegram_phone: str = Field(default="")
    telegram_code: str = Field(default="")
    telegram_signal_debug: bool = Field(default=False)

    # Database
    database_url: str = Field(default="sqlite+aiosqlite:///./trading_bot.db")

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
