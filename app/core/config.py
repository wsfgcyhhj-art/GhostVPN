from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Bot & Webhook ---
    BOT_TOKEN: str = Field(default="123456789:AAH_test_token_placeholder_for_ci")
    BOT_USERNAME: str = Field(default="GhostVPN_Bot")
    WEBHOOK_BASE_URL: str = Field(default="https://ghostvpn.onrender.com")
    WEBHOOK_SECRET: str = Field(default="super_secret_telegram_webhook_token")
    SUPPORT_URL: str = Field(default="https://t.me/ghostvpn_support")
    TERMS_URL: str = Field(default="https://telegra.ph/Terms-of-Service-GhostVPN-01-01")

    # --- Admins & Alerts ---
    ADMIN_IDS: List[int] = Field(default_factory=list)
    ADMIN_CHAT_ID: int | None = Field(default=None)

    # --- Trial (1 RUB / 10 Stars), Devices & Limits ---
    REQUIRED_CHANNEL_ID: int | str | None = Field(default=None)
    REQUIRED_CHANNEL_URL: str = Field(default="https://t.me/ghostvpn_news")
    TRIAL_DAYS: int = Field(default=3)
    TRIAL_PRICE_RUB: float = Field(default=1.0)
    TRIAL_PRICE_STARS: int = Field(default=10)
    DEFAULT_LIMIT_IP: int = Field(default=3)
    MAX_LIMIT_IP: int = Field(default=10)
    EXTRA_DEVICE_PRICE_MONTH_RUB: float = Field(default=69.0)
    KEY_RESET_COOLDOWN_HOURS: int = Field(default=24)

    # --- Database (Neon / Supabase PostgreSQL) ---
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/ghostvpn"
    )

    # --- 3X-UI v3.11.0 Default Server ---
    XUI_HOST: str = Field(default="https://127.0.0.1:2053")
    XUI_WEB_BASE_PATH: str = Field(default="/secretpath")
    XUI_USERNAME: str = Field(default="admin")
    XUI_PASSWORD: str = Field(default="admin")
    XUI_INBOUND_ID: int = Field(default=1)
    XUI_MOBILE_INBOUND_ID: int | None = Field(default=None)
    XUI_SUB_BASE_URL: str = Field(default="https://127.0.0.1:2096/sub")
    XUI_VERIFY_SSL: bool = Field(default=False)
    XUI_SERVER_NAME: str = Field(default="🇳🇱 Нидерланды (VLESS Reality)")
    XUI_SNI_DOMAINS: str = Field(default="dl.google.com,ya.ru,vk.com")

    # --- Payments & Referral ---
    REFERRAL_PERCENT: int = Field(default=15)
    STARS_RUB_RATE: float = Field(default=1.8)  # 1 Star ~= 1.8 RUB
    USDT_RUB_RATE: float = Field(default=95.0)

    CRYPTOBOT_API_TOKEN: str = Field(default="")
    CRYPTOBOT_API_URL: str = Field(default="https://pay.crypt.bot/api")
    CRYPTOBOT_FIAT_CURRENCY: str = Field(default="RUB")

    # --- FreeKassa SCI (СБП и карты РФ) ---
    FREEKASSA_MERCHANT_ID: str = Field(default="")
    FREEKASSA_SECRET_1: str = Field(default="")
    FREEKASSA_SECRET_2: str = Field(default="freekassa_secret_2_placeholder")
    FREEKASSA_SCI_URL: str = Field(default="https://pay.fk.money/")

    # --- Anti-Fraud Rate Limit ---
    RATE_LIMIT_MIN_INTERVAL_SEC: float = Field(default=0.35)

    # --- Server Runtime ---
    PORT: int = Field(default=8000)
    LOG_LEVEL: str = Field(default="INFO")

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: object) -> List[int]:
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                v = v[1:-1]
            return [int(item.strip()) for item in v.split(",") if item.strip()]
        return []

    @field_validator("REQUIRED_CHANNEL_ID", mode="before")
    @classmethod
    def parse_channel_id(cls, v: object) -> int | str | None:
        if v is None or v == "":
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            v = v.strip()
            if not v or v.lower() == "none":
                return None
            if v.lstrip("-").isdigit():
                return int(v)
            return v
        return None

    @field_validator("ADMIN_CHAT_ID", "XUI_MOBILE_INBOUND_ID", mode="before")
    @classmethod
    def parse_optional_int(cls, v: object) -> int | None:
        if v is None or v == "" or (isinstance(v, str) and v.strip().lower() in ("", "none", "null")):
            return None
        return int(v)

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        if v.startswith("postgresql://") and "+asyncpg" not in v:
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
