"""ingestion & dedup fields: dedup_status enum, raw_news audit columns

Revision ID: 0002_ingestion_dedup_fields
Revises: 0001_initial_schema
Create Date: 2026-08-22 21:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_ingestion_dedup_fields"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Labels must stay in the same order as app.models.enums.DedupStatus: the
# ordinal positions define the native enum's sort order, so reordering them
# later would require a type rebuild.
DEDUP_STATUS_LABELS: tuple[str, ...] = (
    "pending",
    "unique",
    "duplicate",
    "skipped_duplicate",
    "update",
    "filtered_digest",
    "filtered_ad",
)


def upgrade() -> None:
    # --------------------------------------------------------- dedup_status
    dedup_status = postgresql.ENUM(
        *DEDUP_STATUS_LABELS, name="dedup_status", create_type=False
    )
    dedup_status.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------- raw_news
    # NOTE: every NOT NULL column is added WITH a server_default, so PostgreSQL
    # backfills the existing rows in place (attribute-level default, no table
    # rewrite since PG 11) instead of rejecting the ALTER. Dropping the default
    # afterwards is deliberately avoided: the ORM relies on the same defaults
    # for rows inserted by raw SQL and by COPY-based backfills.
    op.add_column(
        "raw_news",
        sa.Column(
            "is_ad", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "raw_news",
        sa.Column(
            "dedup_status",
            dedup_status,
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column(
        "raw_news",
        sa.Column(
            "filter_reasons",
            postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'::character varying[]"),
            nullable=False,
        ),
    )
    op.add_column("raw_news", sa.Column("similarity_score", sa.Float(), nullable=True))
    op.add_column("raw_news", sa.Column("dedup_level", sa.Integer(), nullable=True))

    op.create_index("ix_raw_news_dedup_status", "raw_news", ["dedup_status"])


def downgrade() -> None:
    op.drop_index("ix_raw_news_dedup_status", table_name="raw_news")

    op.drop_column("raw_news", "dedup_level")
    op.drop_column("raw_news", "similarity_score")
    op.drop_column("raw_news", "filter_reasons")
    op.drop_column("raw_news", "dedup_status")
    op.drop_column("raw_news", "is_ad")

    postgresql.ENUM(name="dedup_status").drop(op.get_bind(), checkfirst=True)
