"""End-to-end smoke test: async ORM writes, pgvector search, dedup constraints.

Destructive: this script deletes rows from every table, so it refuses to run
outside an explicitly named scratch database. Run against a live
PostgreSQL 16 + pgvector instance:

    POSTGRES_HOST=localhost POSTGRES_DB=svodkapress_verify \
        python -m scripts.smoke_test
"""

from __future__ import annotations

import asyncio
import random
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import undefer

from app.config import settings
from app.database import AsyncSessionLocal, check_database_connection, dispose_engine
from app.models import (
    Article,
    ArticleJournal,
    ArticleStatus,
    MediaStrategy,
    NewsCluster,
    RawNews,
    RiskLevel,
    Source,
    SourceType,
    SystemSetting,
    User,
    WPTagCache,
    default_platform_statuses,
)

#: A scratch database must carry one of these suffixes. This is the only guard
#: standing between `docker compose exec app python -m scripts.smoke_test` and
#: a wiped production database, so it is enforced before any query runs.
ALLOWED_DB_SUFFIXES = ("_verify", "_test", "_scratch", "_ci")

ALL_MODELS = (
    ArticleJournal,
    Article,
    RawNews,
    NewsCluster,
    Source,
    User,
    WPTagCache,
    SystemSetting,
)


class UnsafeTargetError(RuntimeError):
    """Raised when the configured database must not be wiped."""


def _assert_safe_target() -> None:
    """Refuse to run against production or any non-scratch database."""
    if settings.app_env == "production":
        raise UnsafeTargetError(
            "refusing to run a destructive smoke test with APP_ENV=production"
        )
    if not settings.postgres_db.endswith(ALLOWED_DB_SUFFIXES):
        raise UnsafeTargetError(
            f"refusing to wipe database {settings.postgres_db!r}: the smoke test "
            f"deletes all rows and only accepts a database whose name ends with "
            f"one of {', '.join(ALLOWED_DB_SUFFIXES)}"
        )


async def _assert_database_empty() -> None:
    """Refuse to delete pre-existing data even in a correctly named database."""
    async with AsyncSessionLocal() as session:
        for model in ALL_MODELS:
            count = (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            if count:
                raise UnsafeTargetError(
                    f"refusing to run: {model.__tablename__} already holds "
                    f"{count} row(s). Point the smoke test at an empty scratch "
                    "database instead of deleting existing data."
                )


def _vector(seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(settings.ai_embedding_dimensions)]


async def _cleanup() -> None:
    async with AsyncSessionLocal() as session:
        for model in ALL_MODELS:
            await session.execute(delete(model))
        await session.commit()


async def _check_server_defaults() -> None:
    """Verify the DDL defaults, not the ORM's client-side ones.

    Inserting through the ORM always sends Python-side ``default=`` values, so
    the migration's ``server_default`` and ``uuid_generate_v4()`` would never be
    exercised. These Core inserts omit the columns entirely and read back what
    PostgreSQL itself supplied.
    """
    clusters = NewsCluster.__table__
    articles = Article.__table__

    async with AsyncSessionLocal() as session:
        cluster_id = (
            await session.execute(
                insert(clusters)
                .values(title="server default probe", category="test")
                .returning(clusters.c.id)
            )
        ).scalar_one()
        assert isinstance(cluster_id, uuid.UUID), "uuid_generate_v4() did not fire"

        row = (
            await session.execute(
                select(
                    clusters.c.risk_level,
                    clusters.c.is_published,
                    clusters.c.enrichment_data,
                    clusters.c.created_at,
                ).where(clusters.c.id == cluster_id)
            )
        ).one()
        assert row.risk_level == RiskLevel.GREEN.value, row.risk_level
        assert row.is_published is False
        assert row.enrichment_data == {}
        assert row.created_at is not None
        print("[ok] news_clusters server defaults (uuid_generate_v4, enum, jsonb)")

        article_id = (
            await session.execute(
                insert(articles)
                .values(
                    cluster_id=cluster_id,
                    category="test",
                    title="server default probe",
                    content_html="<p>x</p>",
                    url_slug="server-default-probe",
                )
                .returning(articles.c.id)
            )
        ).scalar_one()

        art = (
            await session.execute(
                select(
                    articles.c.status,
                    articles.c.retry_count,
                    articles.c.platform_statuses,
                    articles.c.seo_keywords,
                    articles.c.wp_tag_ids,
                    articles.c.video_links,
                ).where(articles.c.id == article_id)
            )
        ).one()
        assert art.status == ArticleStatus.DRAFT.value, art.status
        assert art.retry_count == 0
        assert art.platform_statuses == default_platform_statuses(), (
            "the JSONB server_default drifted from default_platform_statuses(): "
            f"{art.platform_statuses}"
        )
        assert art.seo_keywords == [] and art.wp_tag_ids == []
        assert art.video_links == []
        print("[ok] articles server defaults match default_platform_statuses()")

        await session.rollback()


async def _check_vector_search() -> None:
    """Verify cosine ordering across distinct vectors, not a self-match."""
    async with AsyncSessionLocal() as session:
        base = _vector(100)
        near = [value + 0.001 for value in base]
        far = [-value for value in base]

        clusters = {
            "target": NewsCluster(title="target", category="test", embedding=base),
            "near": NewsCluster(title="near", category="test", embedding=near),
            "far": NewsCluster(title="far", category="test", embedding=far),
        }
        session.add_all(list(clusters.values()))
        await session.flush()
        ids = {name: cluster.id for name, cluster in clusters.items()}

        distance = NewsCluster.embedding.cosine_distance(base).label("distance")
        ranked = (
            await session.execute(
                select(NewsCluster.id, distance).order_by(distance).limit(3)
            )
        ).all()

        order = [row.id for row in ranked]
        assert order[0] == ids["target"], "nearest neighbour is not the exact match"
        assert order[1] == ids["near"], "second neighbour is not the near vector"
        assert order[2] == ids["far"], "opposite vector should rank last"
        assert ranked[0].distance < ranked[1].distance < ranked[2].distance
        assert ranked[2].distance > 1.5, f"opposite vector distance {ranked[2].distance}"
        print(
            "[ok] pgvector cosine ordering: "
            f"exact={ranked[0].distance:.2e} near={ranked[1].distance:.4f} "
            f"far={ranked[2].distance:.4f}"
        )

        await session.rollback()


async def _check_mutation_tracking() -> None:
    """Verify in-place JSONB edits are flushed (idempotency requirement)."""
    async with AsyncSessionLocal() as session:
        cluster = NewsCluster(title="mutation probe", category="test")
        session.add(cluster)
        await session.flush()
        article = Article(
            cluster_id=cluster.id,
            category="test",
            title="mutation probe",
            content_html="<p>x</p>",
            url_slug="mutation-probe",
        )
        session.add(article)
        await session.flush()
        journal = ArticleJournal(
            article_id=article.id,
            cluster_id=cluster.id,
            run_id="20260903-120000-abcdef",
            mode="live",
            outcome="completed",
            artifact_path="runs/20260903-120000-abcdef",
            started_at=article.created_at or datetime.now(UTC),
        )
        session.add(journal)
        await session.commit()

    async with AsyncSessionLocal() as session:
        article = (await session.execute(select(Article))).scalar_one()
        article.platform_statuses["tg"]["status"] = "published"
        article.platform_statuses["tg"]["attempts"] += 1
        article.video_links.append({"platform": "vk", "url": "https://vk.com/v1"})
        await session.commit()

    async with AsyncSessionLocal() as session:
        article = (await session.execute(select(Article))).scalar_one()
        assert article.platform_statuses["tg"]["status"] == "published", (
            "in-place JSONB mutation was not persisted: retries would republish"
        )
        assert article.platform_statuses["tg"]["attempts"] == 1
        assert article.platform_statuses["vk"]["status"] == "pending"
        assert len(article.video_links) == 1
        print("[ok] in-place JSONB mutations persist (retry idempotency)")

    await _cleanup()


async def main() -> int:
    _assert_safe_target()
    print(f"[ok] target database {settings.postgres_db!r} accepted as scratch")

    assert await check_database_connection(), "database unreachable"
    print("[ok] database connection")

    await _assert_database_empty()
    await _check_server_defaults()
    await _check_vector_search()
    await _check_mutation_tracking()

    async with AsyncSessionLocal() as session:
        user = User(username="operator", password_hash="$2b$12$dummyhashvalue")
        source = Source(
            source_type=SourceType.TELEGRAM,
            name="Test Channel",
            identifier="@test_channel",
            media_strategy=MediaStrategy.OCR_CHECK,
            crawl_rules={"selector": "article", "pagination": None},
        )
        session.add_all([user, source])
        await session.flush()
        assert user.id is not None and source.id is not None
        assert user.is_active is True
        assert source.media_strategy is MediaStrategy.OCR_CHECK
        print("[ok] users + sources insert, enum roundtrip")

        cluster = NewsCluster(
            title="Test news event",
            category="politics",
            risk_level=RiskLevel.YELLOW,
            enrichment_data={"sources": ["https://example.com/a"], "facts": []},
            embedding=_vector(1),
        )
        session.add(cluster)
        await session.flush()
        assert isinstance(cluster.id, uuid.UUID)
        print(f"[ok] news_clusters insert, uuid={cluster.id}, vector written")

        raw = RawNews(
            source_id=source.id,
            external_post_id="1001",
            post_url="https://t.me/test_channel/1001",
            raw_text="Original post body.",
            clean_html="<p>Original post body.</p>",
            entities={"persons": ["Ivanov"], "orgs": []},
            media_data=[{"type": "photo", "url": "https://example.com/p.jpg"}],
            minhash_signature=[i * 7919 for i in range(settings.minhash_num_perm)],
            cluster_id=cluster.id,
        )
        session.add(raw)
        await session.flush()
        assert raw.is_processed is False and raw.is_digest is False
        assert raw.minhash_signature is not None
        assert len(raw.minhash_signature) == settings.minhash_num_perm
        print("[ok] raw_news insert, minhash bigint[] + jsonb roundtrip")

        article = Article(
            cluster_id=cluster.id,
            category="politics",
            title="Rewritten headline",
            content_html="<h2>Lead</h2><p>Body text.</p>",
            seo_title="Rewritten headline — SEO",
            seo_description="Meta description for search engines.",
            seo_keywords=["politics", "economy"],
            wp_tag_ids=[11, 22],
            url_slug="rewritten-headline",
            wp_image_ids=[301, 302],
            video_links=[{"platform": "vk", "url": "https://vk.com/video-1_1"}],
            final_embedding=_vector(2),
        )
        session.add(article)
        await session.flush()
        assert article.status is ArticleStatus.DRAFT
        assert article.retry_count == 0
        assert set(article.platform_statuses) == {"tg", "vk", "ok", "dzen"}
        print("[ok] articles insert, status enum + platform_statuses")

        session.add_all(
            [
                WPTagCache(tag_name="Politics", wp_id=11),
                SystemSetting(
                    key="editorial_styles",
                    value={"active": ["analytical", "reportage"]},
                    description="Enabled editorial styles",
                ),
            ]
        )
        await session.commit()
        print("[ok] wp_tags_cache + system_settings insert")

    # ---------------------------------------------------- idempotency guards
    async with AsyncSessionLocal() as session:
        source_id = (await session.execute(select(Source.id))).scalar_one()
        session.add(
            RawNews(source_id=source_id, external_post_id="1001", raw_text="dup")
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            print("[ok] uq_raw_news_source_external_post blocks duplicate ingestion")
        else:  # pragma: no cover - schema regression
            raise AssertionError("duplicate raw_news was accepted")

    async with AsyncSessionLocal() as session:
        session.add(WPTagCache(tag_name="pOLITICS", wp_id=99))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            print("[ok] lower(tag_name) unique index blocks case-variant tag")
        else:  # pragma: no cover - schema regression
            raise AssertionError("case-variant tag was accepted")

    # -------------------------------------------------- deferred vector load
    async with AsyncSessionLocal() as session:
        article = (await session.execute(select(Article))).scalar_one()
        assert "final_embedding" not in article.__dict__, (
            "final_embedding should be deferred, not loaded on a plain select()"
        )
        loaded = (
            await session.execute(
                select(Article).options(undefer(Article.final_embedding))
            )
        ).scalar_one()
        assert loaded.final_embedding is not None
        assert len(loaded.final_embedding) == settings.ai_embedding_dimensions
        print("[ok] embeddings deferred by default, loadable via undefer()")

        counts = {
            model.__tablename__: (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            for model in (Source, RawNews, NewsCluster, Article, ArticleJournal)
        }
        assert counts == {
            "sources": 1,
            "raw_news": 1,
            "news_clusters": 1,
            "articles": 1,
            "article_journals": 1,
        }, counts
        print(f"[ok] row counts {counts}")

    # ----------------------------------------------- cascade on cluster drop
    async with AsyncSessionLocal() as session:
        cluster_id = (await session.execute(select(NewsCluster.id))).scalar_one()
        await session.execute(delete(NewsCluster).where(NewsCluster.id == cluster_id))
        await session.commit()

        remaining_articles = (
            await session.execute(select(func.count()).select_from(Article))
        ).scalar_one()
        remaining_journals = (
            await session.execute(select(func.count()).select_from(ArticleJournal))
        ).scalar_one()
        orphan = (await session.execute(select(RawNews.cluster_id))).scalar_one()
        assert remaining_articles == 0, "articles were not cascade-deleted"
        assert remaining_journals == 0, "article journals were not cascade-deleted"
        assert orphan is None, "raw_news.cluster_id was not set to NULL"
        print("[ok] ON DELETE CASCADE (articles, journals) + SET NULL (raw_news)")

    await _cleanup()
    await dispose_engine()
    print("\nAll schema and ORM checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except UnsafeTargetError as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        sys.exit(2)
