"""article journals: durable index of per-article pipeline evidence

Revision ID: 0005_article_journals
Revises: 0004_spec_alignment
Create Date: 2026-09-03 18:00:00.000000

The operator-facing journal is already written as files under
``storage/runs/<run_id>/``. This table stores a small, queryable index so a
material in the dashboard can open its own history even after process restarts.
The large prompts and stage payloads stay on disk: putting them in PostgreSQL
would make ordinary article reads expensive and turn every UI change into a
migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_article_journals"
down_revision: str | None = "0004_spec_alignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "article_journals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("cluster_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("artifact_path", sa.String(length=512), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_article_journals_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_article_journals")),
        sa.UniqueConstraint("run_id", name="uq_article_journals_run_id"),
    )
    op.create_index(
        "ix_article_journals_article_started",
        "article_journals",
        ["article_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_article_journals_cluster_started",
        "article_journals",
        ["cluster_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_article_journals_cluster_started", table_name="article_journals")
    op.drop_index("ix_article_journals_article_started", table_name="article_journals")
    op.drop_table("article_journals")
