from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PromoType(str, enum.Enum):
    DAYS = "DAYS"
    PERCENT = "PERCENT"


class PaymentProviderType(str, enum.Enum):
    STARS = "STARS"
    CRYPTOBOT = "CRYPTOBOT"
    RUB_GATEWAY = "RUB_GATEWAY"
    BALANCE = "BALANCE"


class TransactionStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID_PENDING_PROVISION = "PAID_PENDING_PROVISION"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"


class TransactionType(str, enum.Enum):
    SUBSCRIPTION = "SUBSCRIPTION"
    TRIAL = "TRIAL"
    EXTRA_DEVICE = "EXTRA_DEVICE"
    AUTO_RENEW = "AUTO_RENEW"
    TOPUP = "TOPUP"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="ru", nullable=False)

    referrer_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    balance_rub: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0.00"), nullable=False
    )
    pending_discount_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    has_used_trial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    subscription: Mapped[Optional["Subscription"]] = relationship(
        "Subscription", back_populates="user", uselist=False, lazy="selectin"
    )
    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction", back_populates="user", lazy="selectin"
    )


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    web_base_path: Mapped[str] = mapped_column(String(128), default="/", nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password: Mapped[str] = mapped_column(String(256), nullable=False)
    inbound_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    sub_base_url: Mapped[str] = mapped_column(String(256), nullable=False)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    subscriptions: Mapped[List["Subscription"]] = relationship(
        "Subscription", back_populates="server"
    )


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    price_rub: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    limit_ip: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    server_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("servers.id", ondelete="RESTRICT"), nullable=False
    )
    current_plan_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("plans.id", ondelete="SET NULL"), nullable=True
    )
    xui_client_uuid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    xui_email: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    sub_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    limit_ip: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    extra_devices: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_traffic_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    online_ips_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ip_violations_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    deleted_from_xui: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    notified_3d: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_1d: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_expired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_onboarding: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_winback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_renew_attempted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_key_reset_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="subscription")
    server: Mapped["Server"] = relationship("Server", back_populates="subscriptions", lazy="selectin")

    @property
    def subscription_url(self) -> str:
        base = self.server.sub_base_url.rstrip("/")
        return f"{base}/{self.sub_id}"

    @property
    def is_currently_valid(self) -> bool:
        now = utcnow()
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
        return self.is_active and exp > now

    @property
    def used_traffic_gb(self) -> float:
        return round(float(self.used_traffic_bytes or 0) / (1024**3), 2)

    @property
    def days_left(self) -> int:
        now = utcnow()
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
        if exp <= now:
            return 0
        return max(0, int((exp - now).total_seconds() // 86400) + 1)

    @property
    def total_period_days(self) -> int:
        start = self.started_at if self.started_at and self.started_at.tzinfo else utcnow()
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
        diff = int((exp - start).total_seconds() // 86400)
        return max(3, diff)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    plan_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("plans.id", ondelete="RESTRICT"), nullable=True
    )
    tx_type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, native_enum=False),
        default=TransactionType.SUBSCRIPTION,
        nullable=False,
    )
    provider: Mapped[PaymentProviderType] = mapped_column(
        Enum(PaymentProviderType, native_enum=False), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(16), default="RUB", nullable=False)
    amount_currency: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    balance_used_rub: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0.00"), nullable=False
    )
    discount_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra_devices_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
    payment_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, native_enum=False),
        default=TransactionStatus.PENDING,
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="transactions", lazy="selectin")
    plan: Mapped[Optional["Plan"]] = relationship("Plan", lazy="selectin")


class PromoCode(Base):
    __tablename__ = "promocodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    type: Mapped[PromoType] = mapped_column(Enum(PromoType, native_enum=False), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    only_new_users: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class PromoUsage(Base):
    __tablename__ = "promo_usages"
    __table_args__ = (UniqueConstraint("user_id", "promocode_id", name="uq_user_promocode"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    promocode_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("promocodes.id", ondelete="CASCADE"), nullable=False
    )
    used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    promocode: Mapped["PromoCode"] = relationship("PromoCode", lazy="selectin")


class MediaCache(Base):
    __tablename__ = "media_cache"
    __table_args__ = (UniqueConstraint("banner_key", "lang", name="uq_banner_key_lang"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    banner_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    lang: Mapped[str] = mapped_column(String(8), default="ru", nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    telegram_file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

