"""Add pending_username_gifts table for admin direct grants by @username

Revision ID: 0004_admin_username_grants
Revises: 0003_gift_vouchers
Create Date: 2026-11-17 00:48:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_admin_username_grants"
down_revision: Union[str, None] = "0003_gift_vouchers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "pending_username_gifts" in inspector.get_table_names():
        return

    op.create_table(
        "pending_username_gifts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("limit_ip", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("granted_by_admin_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "is_claimed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pending_username_gifts_username",
        "pending_username_gifts",
        ["username"],
        unique=False,
    )
    op.create_index(
        "ix_pending_username_gifts_is_claimed",
        "pending_username_gifts",
        ["is_claimed"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "pending_username_gifts" in inspector.get_table_names():
        op.drop_index(
            "ix_pending_username_gifts_is_claimed",
            table_name="pending_username_gifts",
        )
        op.drop_index(
            "ix_pending_username_gifts_username",
            table_name="pending_username_gifts",
        )
        op.drop_table("pending_username_gifts")
