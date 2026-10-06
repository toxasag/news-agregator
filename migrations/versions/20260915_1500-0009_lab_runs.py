"""lab runs: stepwise pipeline execution state

Revision ID: 0009_lab_runs
Revises: 0008_ai_usage
Create Date: 2026-09-15 15:00:00.000000

Tracks the state of a stepwise lab run: which step is active, which clusters
have been processed, and the current status of each cluster at each step.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_lab_runs"
down_revision: str | None = "0008_ai_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lab_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_clusters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_clusters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cluster_states", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "started_at",
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lab_runs")),
        sa.UniqueConstraint("run_id", name=op.f("uq_lab_runs_run_id")),
    )
    op.create_index("ix_lab_runs_status", "lab_runs", ["status"], unique=False)
    op.create_index("ix_lab_runs_created_at", "lab_runs", ["started_at"], unique=False)
    op.create_index("ix_lab_runs_run_id", "lab_runs", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_lab_runs_run_id", table_name="lab_runs")
    op.drop_index("ix_lab_runs_created_at", table_name="lab_runs")
    op.drop_index("ix_lab_runs_status", table_name="lab_runs")
    op.drop_table("lab_runs")
