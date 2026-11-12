"""Initial GhostVPN schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-11-13 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=True),
        sa.Column("full_name", sa.String(length=256), nullable=True),
        sa.Column("referrer_id", sa.Integer(), nullable=True),
        sa.Column("balance_rub", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("pending_discount_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("has_used_trial", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_banned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["referrer_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)
    op.create_index("ix_users_username", "users", ["username"], unique=False)

    op.create_table(
        "servers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=256), nullable=False),
        sa.Column("web_base_path", sa.String(length=128), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password", sa.String(length=256), nullable=False),
        sa.Column("inbound_id", sa.Integer(), nullable=False),
        sa.Column("sub_base_url", sa.String(length=256), nullable=False),
        sa.Column("verify_ssl", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_servers_is_active", "servers", ["is_active"], unique=False)

    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("price_rub", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("limit_ip", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plans_is_active", "plans", ["is_active"], unique=False)

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("xui_client_uuid", sa.String(length=64), nullable=False),
        sa.Column("xui_email", sa.String(length=64), nullable=False),
        sa.Column("sub_id", sa.String(length=32), nullable=False),
        sa.Column("limit_ip", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_trial", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("notified_3d", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notified_1d", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notified_expired", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_key_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
        sa.UniqueConstraint("xui_client_uuid"),
    )
    op.create_index("ix_subscriptions_expires_at", "subscriptions", ["expires_at"], unique=False)
    op.create_index("ix_subscriptions_is_active", "subscriptions", ["is_active"], unique=False)
    op.create_index("ix_subscriptions_sub_id", "subscriptions", ["sub_id"], unique=True)
    op.create_index("ix_subscriptions_xui_email", "subscriptions", ["xui_email"], unique=True)

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("amount_rub", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("balance_used_rub", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("discount_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("payment_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transactions_external_id", "transactions", ["external_id"], unique=True)
    op.create_index("ix_transactions_status", "transactions", ["status"], unique=False)
    op.create_index("ix_transactions_user_id", "transactions", ["user_id"], unique=False)

    op.create_table(
        "promocodes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("only_new_users", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_promocodes_code", "promocodes", ["code"], unique=True)

    op.create_table(
        "promo_usages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("promocode_id", sa.Integer(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["promocode_id"], ["promocodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "promocode_id", name="uq_user_promocode"),
    )


def downgrade() -> None:
    op.drop_table("promo_usages")
    op.drop_table("promocodes")
    op.drop_table("transactions")
    op.drop_table("subscriptions")
    op.drop_table("plans")
    op.drop_table("servers")
    op.drop_table("users")
