"""AI provider keys and models: encrypted API keys with model status tracking.

Revision ID: 0010_ai_provider_keys
Revises: 0009_lab_runs
Create Date: 2026-09-16 12:00:00.000000

Stores API keys for AI providers (OpenRouter, Together, Anthropic, Google, custom)
encrypted with Fernet. Models are discovered automatically via /v1/models endpoint
and tracked with working status.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_ai_provider_keys"
down_revision: str | None = "0009_lab_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_keys",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("models_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("working_models_count", sa.Integer(), nullable=False, server_default="0"),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_provider_keys")),
    )
    op.create_index("ix_ai_provider_keys_provider", "ai_provider_keys", ["provider_name"], unique=False)
    op.create_index("ix_ai_provider_keys_active", "ai_provider_keys", ["is_active"], unique=False)

    op.create_table(
        "ai_provider_models",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider_key_id", sa.BigInteger(), nullable=False),
        sa.Column("model_id", sa.String(length=256), nullable=False),
        sa.Column("is_working", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_provider_models")),
        sa.ForeignKeyConstraint(
            ["provider_key_id"],
            ["ai_provider_keys.id"],
            name=op.f("fk_ai_provider_models_provider_key_id_ai_provider_keys"),
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_ai_provider_models_key_id", "ai_provider_models", ["provider_key_id"], unique=False)
    op.create_index("ix_ai_provider_models_working", "ai_provider_models", ["is_working"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ai_provider_models_working", table_name="ai_provider_models")
    op.drop_index("ix_ai_provider_models_key_id", table_name="ai_provider_models")
    op.drop_table("ai_provider_models")
    op.drop_index("ix_ai_provider_keys_active", table_name="ai_provider_keys")
    op.drop_index("ix_ai_provider_keys_provider", table_name="ai_provider_keys")
    op.drop_table("ai_provider_keys")
