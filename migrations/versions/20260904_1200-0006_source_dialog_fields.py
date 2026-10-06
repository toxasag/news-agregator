"""source dialog fields: peer kind, username, description, invite link

Revision ID: 0006_source_dialog_fields
Revises: 0005_article_journals
Create Date: 2026-09-04 12:00:00.000000

The sources page now lists every Telegram dialog (channel, group, chat, bot)
with a public link and the about-text, so the operator can check a peer before
marking it for collection. These four columns hold that metadata; website rows
leave them NULL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_source_dialog_fields"
down_revision: str | None = "0005_article_journals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("peer_kind", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "sources",
        sa.Column("username", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "sources",
        sa.Column("description", sa.Text(), nullable=True),
    )
    op.add_column(
        "sources",
        sa.Column("invite_link", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sources", "invite_link")
    op.drop_column("sources", "description")
    op.drop_column("sources", "username")
    op.drop_column("sources", "peer_kind")
