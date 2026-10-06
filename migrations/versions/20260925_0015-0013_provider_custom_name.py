"""Add custom_name to ai_provider_keys.

Revision ID: 0013_provider_custom_name
Revises: 0012_lab_attempts
Create Date: 2026-09-25 00:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_provider_custom_name"
down_revision: str | None = "0012_lab_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_provider_keys",
        sa.Column("custom_name", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_provider_keys", "custom_name")
