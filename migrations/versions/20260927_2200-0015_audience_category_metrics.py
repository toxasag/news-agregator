"""Add audience_category_metrics table for tracking audience preferences and ad potential.

Revision ID: 0015_audience_category_metrics
Revises: 0014_seo_optimizations
Create Date: 2026-09-27 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_audience_category_metrics"
down_revision: str | None = "0014_seo_optimizations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audience_category_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "platform",
            sa.String(length=32),
            server_default="website",
            nullable=False,
        ),
        sa.Column("category_slug", sa.String(length=128), nullable=False),
        sa.Column("category_title", sa.String(length=255), nullable=False),
        sa.Column("pageviews", sa.Integer(), server_default="0", nullable=False),
        sa.Column("visitors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("avg_time_on_page", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("bounce_rate", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("audience_share", sa.Float(), server_default="0.0", nullable=False),
        sa.Column(
            "top_keywords",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "ad_potential_score",
            sa.Float(),
            server_default="50.0",
            nullable=False,
        ),
        sa.Column(
            "ad_readiness_level",
            sa.String(length=32),
            server_default="medium",
            nullable=False,
        ),
        sa.Column("ad_recommendation", sa.Text(), server_default="", nullable=False),
        sa.Column("window_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column(
            "snapshot_date",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audience_category_metrics")),
    )
    op.create_index(
        "ix_aud_metric_platform_slug",
        "audience_category_metrics",
        ["platform", "category_slug"],
        unique=False,
    )
    op.create_index(
        "ix_aud_metric_snapshot",
        "audience_category_metrics",
        ["snapshot_date"],
        unique=False,
    )
    op.create_index(
        "ix_aud_metric_ad_score",
        "audience_category_metrics",
        ["ad_potential_score"],
        unique=False,
    )
    op.create_index(
        "ix_audience_category_metrics_category_slug",
        "audience_category_metrics",
        ["category_slug"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audience_category_metrics_category_slug",
        table_name="audience_category_metrics",
    )
    op.drop_index("ix_aud_metric_ad_score", table_name="audience_category_metrics")
    op.drop_index("ix_aud_metric_snapshot", table_name="audience_category_metrics")
    op.drop_index("ix_aud_metric_platform_slug", table_name="audience_category_metrics")
    op.drop_table("audience_category_metrics")
