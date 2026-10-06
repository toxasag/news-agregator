"""ai usage ledger: per-call token accounting

Revision ID: 0008_ai_usage
Revises: 0007_category_prompts
Create Date: 2026-09-11 12:01:00.000000

OpenRouter returns a ``usage`` block with every chat completion and the client
discarded it. One row per paid model call now records the role, the model, the
token counts and the optional cost the gateway reported, so the dashboard can
answer "how much has this cost" without guessing from article counts.

Cluster and article references are plain nullable columns rather than foreign
keys: the ledger must survive the deletion of the material it recorded (a queue
reset, a wipe), because the spend it describes already happened.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_ai_usage"
down_revision: str | None = "0007_category_prompts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        #: Pipeline role: arbiter / writer / seo / stepwise-lab roles reuse these.
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        #: Cost as reported by the gateway, in USD; NULL when it did not say.
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("cluster_id", sa.Uuid(), nullable=True),
        sa.Column("article_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_usage")),
    )
    op.create_index(
        "ix_ai_usage_created_at",
        "ai_usage",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_usage_cluster",
        "ai_usage",
        ["cluster_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_cluster", table_name="ai_usage")
    op.drop_index("ix_ai_usage_created_at", table_name="ai_usage")
    op.drop_table("ai_usage")
