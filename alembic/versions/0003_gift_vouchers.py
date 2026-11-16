"""Add gift_vouchers table for admin and user-to-user gift subscriptions

Revision ID: 0003_gift_vouchers
Revises: 0002_v2_upgrade
Create Date: 2026-11-16 18:50:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_gift_vouchers"
down_revision: Union[str, None] = "0002_v2_upgrade"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "gift_vouchers" in inspector.get_table_names():
        return

    op.create_table(
        "gift_vouchers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("creator_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "is_admin_gift",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("limit_ip", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column("redeemed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["creator_user_id"],
            ["users.id"],
            name="fk_gift_vouchers_creator_user_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["redeemed_by_user_id"],
            ["users.id"],
            name="fk_gift_vouchers_redeemed_by_user_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_gift_vouchers_code"),
    )
    op.create_index("ix_gift_vouchers_code", "gift_vouchers", ["code"], unique=True)
    op.create_index(
        "ix_gift_vouchers_creator_user_id",
        "gift_vouchers",
        ["creator_user_id"],
        unique=False,
    )
    op.create_index("ix_gift_vouchers_status", "gift_vouchers", ["status"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "gift_vouchers" not in inspector.get_table_names():
        return

    op.drop_index("ix_gift_vouchers_status", table_name="gift_vouchers")
    op.drop_index("ix_gift_vouchers_creator_user_id", table_name="gift_vouchers")
    op.drop_index("ix_gift_vouchers_code", table_name="gift_vouchers")
    op.drop_table("gift_vouchers")
