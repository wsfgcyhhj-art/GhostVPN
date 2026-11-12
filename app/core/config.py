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

    # --- Channel Subscription for Trial ---
    REQUIRED_CHANNEL_ID: int | str | None = Field(default=None)
    REQUIRED_CHANNEL_URL: str = Field(default="https://t.me/ghostvpn_news")
    TRIAL_DAYS: int = Field(default=3)
    DEFAULT_LIMIT_IP: int = Field(default=3)
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
    XUI_SUB_BASE_URL: str = Field(default="https://127.0.0.1:2096/sub")
    XUI_VERIFY_SSL: bool = Field(default=True)
    XUI_SERVER_NAME: str = Field(default="🇳🇱 Нидерланды (VLESS Reality)")

    # --- Payments & Referral ---
    REFERRAL_PERCENT: int = Field(default=15)
    STARS_RUB_RATE: float = Field(default=1.8)  # 1 Star ~= 1.8 RUB

    CRYPTOBOT_API_TOKEN: str = Field(default="")
    CRYPTOBOT_API_URL: str = Field(default="https://pay.crypt.bot/api")
    CRYPTOBOT_FIAT_CURRENCY: str = Field(default="RUB")

    RUB_GATEWAY_SHOP_ID: str = Field(default="")
    RUB_GATEWAY_SECRET_KEY: str = Field(default="")
    RUB_GATEWAY_WEBHOOK_SECRET: str = Field(default="rub_webhook_secret_key")

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

    @field_validator("ADMIN_CHAT_ID", mode="before")
    @classmethod
    def parse_admin_chat_id(cls, v: object) -> int | None:
        if v is None or v == "" or (isinstance(v, str) and v.lower() == "none"):
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
