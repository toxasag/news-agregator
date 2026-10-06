"""Add seo_optimizations table for tracking snippet modifications and self-learning loop.

Revision ID: 0014_seo_optimizations
Revises: 0013_provider_custom_name
Create Date: 2026-09-27 20:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_seo_optimizations"
down_revision: str | None = "0013_provider_custom_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seo_optimizations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column(
            "category", sa.String(length=128), server_default="general", nullable=False
        ),
        sa.Column(
            "focus_keyword", sa.String(length=255), server_default="", nullable=False
        ),
        sa.Column(
            "strategy", sa.String(length=64), server_default="ctr_booster", nullable=False
        ),
        sa.Column(
            "before_title", sa.String(length=512), server_default="", nullable=False
        ),
        sa.Column(
            "before_description",
            sa.String(length=1024),
            server_default="",
            nullable=False,
        ),
        sa.Column(
            "before_position", sa.Float(), server_default="0.0", nullable=False
        ),
        sa.Column("before_ctr", sa.Float(), server_default="0.0", nullable=False),
        sa.Column(
            "before_impressions", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("before_clicks", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "after_title", sa.String(length=512), server_default="", nullable=False
        ),
        sa.Column(
            "after_description",
            sa.String(length=1024),
            server_default="",
            nullable=False,
        ),
        sa.Column(
            "after_focus_keyword",
            sa.String(length=255),
            server_default="",
            nullable=False,
        ),
        sa.Column(
            "model_used", sa.String(length=128), server_default="", nullable=False
        ),
        sa.Column("reasoning_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("wp_post_id", sa.Integer(), nullable=True),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "applied_by", sa.String(length=128), server_default="admin", nullable=False
        ),
        sa.Column(
            "status", sa.String(length=32), server_default="applied", nullable=False
        ),
        sa.Column(
            "outcome", sa.String(length=32), server_default="pending", nullable=False
        ),
        sa.Column(
            "metrics_7d",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "metrics_14d",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "metrics_28d",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("ctr_lift", sa.Float(), nullable=True),
        sa.Column("clicks_lift", sa.Integer(), nullable=True),
        sa.Column("position_delta", sa.Float(), nullable=True),
        sa.Column("learned_insight", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["article_id"], ["articles.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_seo_optimizations_article_id", "seo_optimizations", ["article_id"]
    )
    op.create_index("ix_seo_optimizations_url", "seo_optimizations", ["url"])
    op.create_index("ix_seo_optimizations_status", "seo_optimizations", ["status"])
    op.create_index("ix_seo_optimizations_outcome", "seo_optimizations", ["outcome"])
    op.create_index(
        "ix_seo_opt_url_applied", "seo_optimizations", ["url", "applied_at"]
    )
    op.create_index(
        "ix_seo_opt_category_outcome", "seo_optimizations", ["category", "outcome"]
    )
    op.create_index(
        "ix_seo_opt_status_applied", "seo_optimizations", ["status", "applied_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_seo_opt_status_applied", table_name="seo_optimizations")
    op.drop_index("ix_seo_opt_category_outcome", table_name="seo_optimizations")
    op.drop_index("ix_seo_opt_url_applied", table_name="seo_optimizations")
    op.drop_index("ix_seo_optimizations_outcome", table_name="seo_optimizations")
    op.drop_index("ix_seo_optimizations_status", table_name="seo_optimizations")
    op.drop_index("ix_seo_optimizations_url", table_name="seo_optimizations")
    op.drop_index("ix_seo_optimizations_article_id", table_name="seo_optimizations")
    op.drop_table("seo_optimizations")
