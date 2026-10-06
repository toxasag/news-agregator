"""Durable manual laboratory items and stage attempts.

Revision ID: 0012_lab_attempts
Revises: 0011_provider_health
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_lab_attempts"
down_revision: str | None = "0011_provider_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lab_run_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lab_run_id", sa.BigInteger(), nullable=False),
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title_snapshot", sa.String(length=512), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["lab_run_id"],
            ["lab_runs.id"],
            name=op.f("fk_lab_run_items_lab_run_id_lab_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lab_run_items")),
        sa.UniqueConstraint("lab_run_id", "cluster_id", name=op.f("uq_lab_run_items_lab_run_id")),
    )
    op.create_index(
        "ix_lab_run_items_run_position",
        "lab_run_items",
        ["lab_run_id", "position"],
        unique=False,
    )

    op.create_table(
        "lab_stage_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lab_run_item_id", sa.BigInteger(), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("input_snapshot", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("result", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("artifact_path", sa.String(length=512), nullable=True),
        sa.Column("job_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(length=24), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["lab_run_item_id"],
            ["lab_run_items.id"],
            name=op.f("fk_lab_stage_attempts_lab_run_item_id_lab_run_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lab_stage_attempts")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_lab_stage_attempts_idempotency_key")),
        sa.UniqueConstraint(
            "lab_run_item_id",
            "stage",
            "attempt_number",
            name=op.f("uq_lab_stage_attempts_lab_run_item_id"),
        ),
    )
    op.create_index(
        "ix_lab_stage_attempts_item_stage",
        "lab_stage_attempts",
        ["lab_run_item_id", "stage"],
        unique=False,
    )
    op.create_index(
        "ix_lab_stage_attempts_status", "lab_stage_attempts", ["status"], unique=False
    )
    op.create_index(
        "ix_lab_stage_attempts_job_id", "lab_stage_attempts", ["job_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_lab_stage_attempts_job_id", table_name="lab_stage_attempts")
    op.drop_index("ix_lab_stage_attempts_status", table_name="lab_stage_attempts")
    op.drop_index("ix_lab_stage_attempts_item_stage", table_name="lab_stage_attempts")
    op.drop_table("lab_stage_attempts")
    op.drop_index("ix_lab_run_items_run_position", table_name="lab_run_items")
    op.drop_table("lab_run_items")
