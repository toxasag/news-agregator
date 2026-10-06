"""initial schema: extensions, enums, core tables and indexes

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-22 15:05:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.models.vector import EMBEDDING_DIMENSIONS, HNSW_OPTIONS, VECTOR_OPS

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: the space after every ':' is significant — sa.text() treats ':null' as
# a bind parameter, so the JSON literal must keep separators spaced out.
DEFAULT_PLATFORM_STATUSES_JSON = (
    '{"tg": {"status": "pending", "external_id": null, "url": null, '
    '"error": null, "attempts": 0, "published_at": null}, '
    '"vk": {"status": "pending", "external_id": null, "url": null, '
    '"error": null, "attempts": 0, "published_at": null}, '
    '"ok": {"status": "pending", "external_id": null, "url": null, '
    '"error": null, "attempts": 0, "published_at": null}, '
    '"dzen": {"status": "pending", "external_id": null, "url": null, '
    '"error": null, "attempts": 0, "published_at": null}}'
)


def upgrade() -> None:
    # ---------------------------------------------------------- extensions
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---------------------------------------------------------------- users
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        op.f("ix_users_username"), "users", ["username"], unique=True
    )

    # -------------------------------------------------------------- sources
    source_type = postgresql.ENUM(
        "telegram", "website", name="source_type", create_type=False
    )
    media_strategy = postgresql.ENUM(
        "original",
        "ocr_check",
        "always_regenerate",
        "text_only",
        name="media_strategy",
        create_type=False,
    )
    source_type.create(op.get_bind(), checkfirst=True)
    media_strategy.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_type", source_type, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("identifier", sa.String(length=512), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "skip_media", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "media_strategy",
            media_strategy,
            server_default="ocr_check",
            nullable=False,
        ),
        sa.Column(
            "crawl_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_scraped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint(
            "source_type", "identifier", name="uq_sources_type_identifier"
        ),
    )
    op.create_index(
        "ix_sources_active_type", "sources", ["is_active", "source_type"], unique=False
    )
    op.create_index(
        "ix_sources_last_scraped_at", "sources", ["last_scraped_at"], unique=False
    )

    # -------------------------------------------------------- news_clusters
    risk_level = postgresql.ENUM(
        "low", "medium", "high", "critical", name="risk_level", create_type=False
    )
    risk_level.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "news_clusters",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("risk_level", risk_level, server_default="low", nullable=False),
        sa.Column(
            "enrichment_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.Vector(EMBEDDING_DIMENSIONS),
            nullable=True,
        ),
        sa.Column(
            "is_published", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_clusters")),
    )
    op.create_index(
        op.f("ix_news_clusters_created_at"), "news_clusters", ["created_at"]
    )
    op.create_index("ix_news_clusters_category", "news_clusters", ["category"])
    op.create_index(
        "ix_news_clusters_unpublished",
        "news_clusters",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("is_published = false"),
    )
    op.create_index(
        "ix_news_clusters_embedding_hnsw",
        "news_clusters",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with=dict(HNSW_OPTIONS),
        postgresql_ops={"embedding": VECTOR_OPS},
    )

    # ------------------------------------------------------------- raw_news
    op.create_table(
        "raw_news",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("external_post_id", sa.String(length=255), nullable=False),
        sa.Column("post_url", sa.String(length=2048), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("clean_html", sa.Text(), nullable=True),
        sa.Column(
            "entities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "media_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "minhash_signature",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=True,
        ),
        sa.Column(
            "is_digest", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "is_processed", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("cluster_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["news_clusters.id"],
            name=op.f("fk_raw_news_cluster_id_news_clusters"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_raw_news_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_news")),
        sa.UniqueConstraint(
            "source_id", "external_post_id", name="uq_raw_news_source_external_post"
        ),
    )
    op.create_index(op.f("ix_raw_news_created_at"), "raw_news", ["created_at"])
    op.create_index("ix_raw_news_cluster_id", "raw_news", ["cluster_id"])
    op.create_index(
        "ix_raw_news_unprocessed",
        "raw_news",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("is_processed = false"),
    )
    op.create_index(
        "ix_raw_news_entities_gin",
        "raw_news",
        ["entities"],
        unique=False,
        postgresql_using="gin",
    )

    # ------------------------------------------------------------- articles
    article_status = postgresql.ENUM(
        "draft",
        "draft_tag",
        "media_ready",
        "wp_published",
        "distributed",
        "failed",
        "skipped_duplicate",
        name="article_status",
        create_type=False,
    )
    article_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "articles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("wp_category_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("content_html", sa.Text(), nullable=False),
        sa.Column("seo_title", sa.String(length=512), nullable=True),
        sa.Column("seo_description", sa.String(length=1024), nullable=True),
        sa.Column(
            "seo_keywords",
            postgresql.ARRAY(sa.String(length=128)),
            server_default=sa.text("'{}'::character varying[]"),
            nullable=False,
        ),
        sa.Column(
            "wp_tag_ids",
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'::integer[]"),
            nullable=False,
        ),
        sa.Column("url_slug", sa.String(length=255), nullable=False),
        sa.Column("wp_post_id", sa.Integer(), nullable=True),
        sa.Column("wp_image_id", sa.Integer(), nullable=True),
        sa.Column(
            "wp_image_ids",
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'::integer[]"),
            nullable=False,
        ),
        sa.Column(
            "video_links",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "final_embedding",
            pgvector.sqlalchemy.Vector(EMBEDDING_DIMENSIONS),
            nullable=True,
        ),
        sa.Column("status", article_status, server_default="draft", nullable=False),
        sa.Column(
            "platform_statuses",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(f"'{DEFAULT_PLATFORM_STATUSES_JSON}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["news_clusters.id"],
            name=op.f("fk_articles_cluster_id_news_clusters"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_articles")),
        sa.UniqueConstraint("wp_post_id", name=op.f("uq_articles_wp_post_id")),
    )
    op.create_index(op.f("ix_articles_cluster_id"), "articles", ["cluster_id"])
    op.create_index(op.f("ix_articles_created_at"), "articles", ["created_at"])
    op.create_index(op.f("ix_articles_url_slug"), "articles", ["url_slug"], unique=True)
    op.create_index(
        "ix_articles_status_created_at", "articles", ["status", "created_at"]
    )
    op.create_index(
        "ix_articles_platform_statuses_gin",
        "articles",
        ["platform_statuses"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_articles_final_embedding_hnsw",
        "articles",
        ["final_embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with=dict(HNSW_OPTIONS),
        postgresql_ops={"final_embedding": VECTOR_OPS},
    )

    # -------------------------------------------------------- wp_tags_cache
    op.create_table(
        "wp_tags_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tag_name", sa.String(length=255), nullable=False),
        sa.Column("wp_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wp_tags_cache")),
        sa.UniqueConstraint("wp_id", name=op.f("uq_wp_tags_cache_wp_id")),
    )
    op.create_index(
        "uq_wp_tags_cache_tag_name_lower",
        "wp_tags_cache",
        [sa.text("lower(tag_name)")],
        unique=True,
    )

    # ------------------------------------------------------ system_settings
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column(
            "value", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_settings")),
    )


def downgrade() -> None:
    op.drop_table("system_settings")

    op.drop_index("uq_wp_tags_cache_tag_name_lower", table_name="wp_tags_cache")
    op.drop_table("wp_tags_cache")

    op.drop_index(
        "ix_articles_final_embedding_hnsw",
        table_name="articles",
        postgresql_using="hnsw",
    )
    op.drop_index(
        "ix_articles_platform_statuses_gin",
        table_name="articles",
        postgresql_using="gin",
    )
    op.drop_index("ix_articles_status_created_at", table_name="articles")
    op.drop_index(op.f("ix_articles_url_slug"), table_name="articles")
    op.drop_index(op.f("ix_articles_created_at"), table_name="articles")
    op.drop_index(op.f("ix_articles_cluster_id"), table_name="articles")
    op.drop_table("articles")

    op.drop_index("ix_raw_news_entities_gin", table_name="raw_news", postgresql_using="gin")
    op.drop_index("ix_raw_news_unprocessed", table_name="raw_news")
    op.drop_index("ix_raw_news_cluster_id", table_name="raw_news")
    op.drop_index(op.f("ix_raw_news_created_at"), table_name="raw_news")
    op.drop_table("raw_news")

    op.drop_index(
        "ix_news_clusters_embedding_hnsw",
        table_name="news_clusters",
        postgresql_using="hnsw",
    )
    op.drop_index("ix_news_clusters_unpublished", table_name="news_clusters")
    op.drop_index("ix_news_clusters_category", table_name="news_clusters")
    op.drop_index(op.f("ix_news_clusters_created_at"), table_name="news_clusters")
    op.drop_table("news_clusters")

    op.drop_index("ix_sources_last_scraped_at", table_name="sources")
    op.drop_index("ix_sources_active_type", table_name="sources")
    op.drop_table("sources")

    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_table("users")

    postgresql.ENUM(name="article_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="risk_level").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="media_strategy").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="source_type").drop(op.get_bind(), checkfirst=True)
