"""category prompts: operator-editable rubric voices

Revision ID: 0007_category_prompts
Revises: 0006_source_dialog_fields
Create Date: 2026-09-11 12:00:00.000000

The twenty editorial voices were hardcoded in ``content_prompts.py``, which
made prompt tuning a redeploy. This table stores operator overrides: one row
per rubric whose prompt was edited from the dashboard, plus custom rubrics the
operator created. The built-in defaults still live in code — the table holds
only what differs, mirroring how ``ai_model_routing`` overrides the
environment.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_category_prompts"
down_revision: str | None = "0006_source_dialog_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "category_prompts",
        sa.Column(
            "slug",
            sa.String(length=128),
            nullable=False,
            # Rubric slugs are persisted to ``articles.category`` and
            # ``news_clusters.category``; the same value keys the prompt.
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("voice", sa.Text(), nullable=False),
        sa.Column("lead", sa.Text(), nullable=False),
        sa.Column("avoid", sa.Text(), nullable=False),
        sa.Column("structure", sa.Text(), nullable=False),
        #: WordPress term id, optional until the site is connected via the panel.
        sa.Column("wp_term_id", sa.BigInteger(), nullable=True),
        #: False: an override of a built-in rubric. True: an operator-created
        #: rubric the moderation prompt must offer as a classification target.
        sa.Column(
            "is_custom", sa.Boolean(), nullable=False, server_default="false"
        ),
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
        sa.PrimaryKeyConstraint("slug", name=op.f("pk_category_prompts")),
    )
    op.create_index(
        "ix_category_prompts_custom",
        "category_prompts",
        ["is_custom"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_category_prompts_custom", table_name="category_prompts")
    op.drop_table("category_prompts")
