"""Separate operator enablement from provider health.

Revision ID: 0011_provider_health
Revises: 0010_ai_provider_keys
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_provider_health"
down_revision: str | None = "0010_ai_provider_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_provider_keys",
        sa.Column("health", sa.String(24), nullable=False, server_default="untested"),
    )
    op.add_column(
        "ai_provider_keys",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ai_provider_keys",
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE ai_provider_keys SET health = 'error' WHERE last_error IS NOT NULL"
    )
    op.execute(
        "UPDATE ai_provider_models SET is_working = false WHERE last_tested_at IS NULL"
    )
    op.alter_column("ai_provider_models", "is_working", server_default=sa.false())
    op.execute(
        "UPDATE ai_provider_keys AS k SET working_models_count = "
        "(SELECT count(*) FROM ai_provider_models AS m WHERE m.provider_key_id = k.id "
        "AND m.last_tested_at IS NOT NULL AND m.is_working)"
    )


def downgrade() -> None:
    op.alter_column("ai_provider_models", "is_working", server_default=sa.true())
    op.drop_column("ai_provider_keys", "cooldown_until")
    op.drop_column("ai_provider_keys", "last_checked_at")
    op.drop_column("ai_provider_keys", "health")
