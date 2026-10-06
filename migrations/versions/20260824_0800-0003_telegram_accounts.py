"""telegram accounts: multi-account MTProto ingestion pool

Revision ID: 0003_telegram_accounts
Revises: 0002_ingestion_dedup_fields
Create Date: 2026-08-24 08:00:00.000000

Ingestion moves from a single GramJS bridge to a pool of real Telegram user
accounts. Rate limits are per account, so pooling several is what keeps one
``FloodWait`` from stalling the pipeline.

``session_string`` holds a Fernet ciphertext, never a plaintext Telethon session:
that string is a bearer credential granting full account access without the phone
or the 2FA password.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_telegram_accounts"
down_revision: str | None = "0002_ingestion_dedup_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("phone_number", sa.String(length=32), nullable=False),
        sa.Column(
            "label", sa.String(length=128), server_default="", nullable=False
        ),
        # BigInteger: Telegram's api_id is a 64-bit type even though issued
        # values currently fit in 32 bits.
        sa.Column("api_id", sa.BigInteger(), nullable=False),
        sa.Column("api_hash", sa.String(length=64), nullable=False),
        # Text, not a bounded String: Fernet output grows with its payload and a
        # truncated session string cannot be recovered.
        sa.Column(
            "session_string", sa.Text(), server_default="", nullable=False
        ),
        sa.Column(
            "is_active", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flood_wait_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "failure_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_accounts"),
        # Unique so one account cannot be added twice and then be rotated as if
        # it carried two independent rate-limit budgets. The constraint's index
        # also serves phone-number lookups, so no separate index is created.
        sa.UniqueConstraint("phone_number", name="uq_telegram_accounts_phone"),
    )
    # Covers the pool's hot query: filter on is_active, order by last_used_at.
    op.create_index(
        "ix_telegram_accounts_active_last_used",
        "telegram_accounts",
        ["is_active", "last_used_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_accounts_active_last_used", table_name="telegram_accounts"
    )
    op.drop_table("telegram_accounts")
