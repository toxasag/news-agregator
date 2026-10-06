"""Add provider_name, latency_ms, status, error_message to ai_usage.

Revision ID: 0016_ai_usage_provider_stability
Revises: 0015_audience_category_metrics
Create Date: 2026-09-28 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_ai_usage_provider_stability"
down_revision: str | None = "0015_audience_category_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_usage",
        sa.Column("provider_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_usage",
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ai_usage",
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="success",
            nullable=False,
        ),
    )
    op.add_column(
        "ai_usage",
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_ai_usage_provider",
        "ai_usage",
        ["provider_name"],
        unique=False,
    )
    op.create_index(
        "ix_ai_usage_article",
        "ai_usage",
        ["article_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_article", table_name="ai_usage")
    op.drop_index("ix_ai_usage_provider", table_name="ai_usage")
    op.drop_column("ai_usage", "error_message")
    op.drop_column("ai_usage", "status")
    op.drop_column("ai_usage", "latency_ms")
    op.drop_column("ai_usage", "provider_name")
