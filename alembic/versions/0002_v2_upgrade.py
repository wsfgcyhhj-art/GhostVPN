"""GhostVPN v2.0 upgrade: i18n, paid trial, extra devices, auto-renew, retention & analytics

Revision ID: 0002_v2_upgrade
Revises: 0001_initial
Create Date: 2026-11-13 15:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_v2_upgrade"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- users ---
    op.add_column(
        "users",
        sa.Column("language", sa.String(length=8), nullable=False, server_default="ru"),
    )
    op.add_column(
        "users",
        sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )

    # --- subscriptions ---
    op.add_column(
        "subscriptions",
        sa.Column("current_plan_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscriptions_current_plan_id",
        "subscriptions",
        "plans",
        ["current_plan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "subscriptions",
        sa.Column("extra_devices", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "subscriptions",
        sa.Column("used_traffic_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "subscriptions",
        sa.Column("online_ips_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "subscriptions",
        sa.Column("ip_violations_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column("deleted_from_xui", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "subscriptions",
        sa.Column("notified_onboarding", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "subscriptions",
        sa.Column("notified_winback", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "subscriptions",
        sa.Column("auto_renew_attempted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # --- transactions ---
    op.alter_column("transactions", "plan_id", existing_type=sa.Integer(), nullable=True)
    op.add_column(
        "transactions",
        sa.Column(
            "tx_type",
            sa.String(length=32),
            nullable=False,
            server_default="SUBSCRIPTION",
        ),
    )
    op.add_column(
        "transactions",
        sa.Column("currency", sa.String(length=16), nullable=False, server_default="RUB"),
    )
    op.add_column(
        "transactions",
        sa.Column(
            "amount_currency",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0.00",
        ),
    )
    op.add_column(
        "transactions",
        sa.Column("extra_devices_qty", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "media_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("banner_key", sa.String(length=64), nullable=False),
        sa.Column("lang", sa.String(length=8), nullable=False, server_default="ru"),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("telegram_file_id", sa.String(length=256), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("banner_key", "lang", name="uq_banner_key_lang"),
    )
    op.create_index("ix_media_cache_banner_key", "media_cache", ["banner_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_media_cache_banner_key", table_name="media_cache")
    op.drop_table("media_cache")
    op.drop_column("transactions", "extra_devices_qty")
    op.drop_column("transactions", "amount_currency")
    op.drop_column("transactions", "currency")
    op.drop_column("transactions", "tx_type")
    op.alter_column("transactions", "plan_id", existing_type=sa.Integer(), nullable=False)

    op.drop_column("subscriptions", "auto_renew_attempted")
    op.drop_column("subscriptions", "notified_winback")
    op.drop_column("subscriptions", "notified_onboarding")
    op.drop_column("subscriptions", "deleted_from_xui")
    op.drop_column("subscriptions", "started_at")
    op.drop_column("subscriptions", "ip_violations_count")
    op.drop_column("subscriptions", "online_ips_count")
    op.drop_column("subscriptions", "used_traffic_bytes")
    op.drop_column("subscriptions", "extra_devices")
    op.drop_constraint("fk_subscriptions_current_plan_id", "subscriptions", type_="foreignkey")
    op.drop_column("subscriptions", "current_plan_id")

    op.drop_column("users", "auto_renew")
    op.drop_column("users", "language")
