"""spec alignment: 384-dim vectors, traffic-light risk, media_planned, source flags

Revision ID: 0004_spec_alignment
Revises: 0003_telegram_accounts
Create Date: 2026-08-24 17:30:00.000000

Four schema changes, all driven by the pipeline specification:

**Vectors 1536 -> 384.** Embeddings are now produced locally by fastembed
(``paraphrase-multilingual-MiniLM-L12-v2``) for zero tokens, and that model emits
384 dimensions. The existing values cannot be converted — a 1536-dim vector from a
different model is not a projection of the same space — so they are dropped and
will be regenerated. That is safe: the embedding is a derived index, not source
data, and every row is re-embeddable from its stored text.

**``risk_level`` low/medium/high/critical -> green/yellow/red/spam.** The moderator
returns these labels directly. ``spam`` is not a degree of risk: it drops the
cluster before any model is called.

**``article_status`` gains ``media_planned``**, between ``draft_tag`` and
``media_ready``. Without it a retry cannot distinguish "media never planned" from
"planned but the build failed".

**``sources`` gains four columns** for the channel manager: ``fetch_media``,
``telegram_id``, ``avatar_data_uri`` and ``posts_collected``.

The HNSW indexes are dropped before the type change and rebuilt after: an index
cannot survive its column's dimension changing, and rebuilding on an empty column
is instant.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_spec_alignment"
down_revision: str | None = "0003_telegram_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Vector width of the pinned local embedding model.
NEW_DIMENSIONS = 384
OLD_DIMENSIONS = 1536

#: HNSW build parameters, carried over from the initial schema so the rebuilt
#: indexes match the originals rather than silently taking pgvector defaults.
HNSW_OPTS = {"m": 16, "ef_construction": 64}


def _rebuild_vector_column(
    table: str, column: str, index: str, dimensions: int
) -> None:
    """Resize one pgvector column and rebuild its HNSW index.

    The values are discarded rather than cast: vectors from two different models
    are not comparable, so keeping them would make cosine distance meaningless
    while looking perfectly valid.
    """
    op.drop_index(index, table_name=table)
    # Table and column names are module constants, never request data; pgvector
    # dimensions cannot be parameterised in DDL anyway.
    op.execute(f"UPDATE {table} SET {column} = NULL")  # noqa: S608
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE vector({dimensions})"
    )
    op.create_index(
        index,
        table,
        [sa.text(f"{column} vector_cosine_ops")],
        postgresql_using="hnsw",
        postgresql_with=HNSW_OPTS,
    )


def upgrade() -> None:
    _rebuild_vector_column(
        "news_clusters", "embedding", "ix_news_clusters_embedding_hnsw", NEW_DIMENSIONS
    )
    _rebuild_vector_column(
        "articles",
        "final_embedding",
        "ix_articles_final_embedding_hnsw",
        NEW_DIMENSIONS,
    )

    # PostgreSQL cannot remove a value from an enum, so the type is replaced.
    # Existing rows are all `low`, which maps to `green`: a cluster is publishable
    # until a moderator says otherwise.
    op.execute("ALTER TYPE risk_level RENAME TO risk_level_old")
    op.execute(
        "CREATE TYPE risk_level AS ENUM ('green', 'yellow', 'red', 'spam')"
    )
    op.execute(
        """
        ALTER TABLE news_clusters
            ALTER COLUMN risk_level DROP DEFAULT,
            ALTER COLUMN risk_level TYPE risk_level
                USING CASE risk_level::text
                    WHEN 'low' THEN 'green'
                    WHEN 'medium' THEN 'yellow'
                    WHEN 'high' THEN 'red'
                    WHEN 'critical' THEN 'red'
                    ELSE 'green'
                END::risk_level,
            ALTER COLUMN risk_level SET DEFAULT 'green'
        """
    )
    op.execute("DROP TYPE risk_level_old")

    # Adding a value to an enum is in-place and keeps every existing row valid.
    op.execute(
        "ALTER TYPE article_status ADD VALUE IF NOT EXISTS 'media_planned' "
        "AFTER 'draft_tag'"
    )

    op.add_column(
        "sources",
        sa.Column(
            "fetch_media",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
    )
    op.add_column(
        "sources", sa.Column("telegram_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "sources", sa.Column("avatar_data_uri", sa.Text(), nullable=True)
    )
    op.add_column(
        "sources",
        sa.Column(
            "posts_collected",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    # Backfilled from the rows that already exist, so the sources table shows a
    # real count on the first render rather than zeros for every channel.
    op.execute(
        """
        UPDATE sources s
           SET posts_collected = COALESCE(counts.total, 0)
          FROM (
              SELECT source_id, COUNT(*) AS total
                FROM raw_news
               GROUP BY source_id
          ) AS counts
         WHERE counts.source_id = s.id
        """
    )


def downgrade() -> None:
    op.drop_column("sources", "posts_collected")
    op.drop_column("sources", "avatar_data_uri")
    op.drop_column("sources", "telegram_id")
    op.drop_column("sources", "fetch_media")

    # `media_planned` is left in `article_status`: removing an enum value requires
    # recreating the type, and any row still holding it would block the downgrade.
    # A spare value is harmless.

    op.execute("ALTER TYPE risk_level RENAME TO risk_level_new")
    op.execute(
        "CREATE TYPE risk_level AS ENUM ('low', 'medium', 'high', 'critical')"
    )
    op.execute(
        """
        ALTER TABLE news_clusters
            ALTER COLUMN risk_level DROP DEFAULT,
            ALTER COLUMN risk_level TYPE risk_level
                USING CASE risk_level::text
                    WHEN 'green' THEN 'low'
                    WHEN 'yellow' THEN 'medium'
                    WHEN 'red' THEN 'high'
                    WHEN 'spam' THEN 'critical'
                    ELSE 'low'
                END::risk_level,
            ALTER COLUMN risk_level SET DEFAULT 'low'
        """
    )
    op.execute("DROP TYPE risk_level_new")

    _rebuild_vector_column(
        "articles",
        "final_embedding",
        "ix_articles_final_embedding_hnsw",
        OLD_DIMENSIONS,
    )
    _rebuild_vector_column(
        "news_clusters", "embedding", "ix_news_clusters_embedding_hnsw", OLD_DIMENSIONS
    )
