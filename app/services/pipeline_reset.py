"""Queue reset: return the pipeline to the state of a first-ever run.

An operator tuning the thresholds needs to re-collect the *same* posts and watch
the same decisions under the new settings. Without a reset that is impossible: the
collector stores a watermark per source, deduplication indexes every post it has
seen, and both make a second pass over the same channel return nothing at all.
Re-reading a feed is normal operation, but the pipeline is deliberately built so
that re-reading finds nothing new — which is exactly what makes it impossible to
inspect by hand.

**What the reset removes, and why each item is load-bearing:**

* ``raw_news`` rows, so the ``(source_id, external_post_id)`` uniqueness constraint
  stops turning every re-collected post into ``skipped_existing``.
* ``news_clusters`` without an article, because a surviving cluster would attract
  the re-collected posts as duplicates of themselves.
* ``sources.last_scraped_at`` and ``posts_collected``, so
  :func:`~app.workers.ingestion_worker.telegram_watermark` returns ``None`` and
  the next pass takes ``INGEST_MAX_MESSAGES_PER_SOURCE`` posts from **every**
  channel instead of just the overlap window.
* The Redis LSH buckets and cached signatures, since level 1 deduplication would
  otherwise recognise every re-collected post from the index alone, with no
  database row left to explain the verdict.
* The model cooldowns, so a run after the reset is not silently skipping models
  penalised before it.

**Published clusters are kept, and that is not a technicality.**
``articles.cluster_id`` is ``NOT NULL`` with ``ON DELETE CASCADE``, so deleting a
cluster deletes its article — and the article is the pipeline's output, with a live
WordPress post behind it that no row here can unpublish. The reset therefore keeps
clusters that produced an article, which has a visible consequence the report
states out loud: a re-collected post about an already-published story is still
matched against those clusters by level 2, so it lands in duplicates rather than
opening a new cluster. ``include_articles=True`` is the separate, explicit decision
for an operator who wants a genuinely empty system.

**The run lock is respected, never cleared.** A reset during a collection pass
would delete the rows that pass is deduplicating against.
"""

from __future__ import annotations

# Operator-facing messages are Russian, matching the dashboard.
# ruff: noqa: RUF001
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Final, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.article import Article
from app.models.article_journal import ArticleJournal
from app.models.cluster import NewsCluster
from app.models.raw_news import RawNews
from app.models.source import Source
from app.redis_client import get_redis
from app.services.deduplicator import (
    LSH_BUCKET_PREFIX,
    LSH_SIGNATURE_PREFIX,
    bucket_keys,
    signature_from_list,
    signature_key,
)
from app.services.pipeline_stages import ArtifactStore
from app.workers.ingestion_worker import RUN_LOCK_KEY

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_WIPE_DB_SUFFIXES",
    "ResetBlockedError",
    "ResetReport",
    "UnsafeWipeTargetError",
    "assert_safe_wipe_target",
    "delete_day_from_queue",
    "preview_pipeline_wipe",
    "reset_pipeline_queue",
    "wipe_pipeline_history",
]

#: Databases that a full wipe may target without a separate production procedure.
#: Mirrors ``scripts.smoke_test`` so the two destructive tools cannot disagree.
ALLOWED_WIPE_DB_SUFFIXES: Final[tuple[str, ...]] = (
    "_verify",
    "_test",
    "_scratch",
    "_ci",
)

#: Tables a wipe never touches: configuration, not generated content.
PRESERVED_TABLES: Final[tuple[str, ...]] = (
    "sources",
    "telegram_accounts",
    "users",
    "system_settings",
    "wp_tags_cache",
    "alembic_version",
)

#: Redis key patterns the reset deletes. Scanned rather than listed because a
#: bucket key carries a band index and a hash, so the names are not predictable.
_SCAN_PATTERNS: Final[tuple[str, ...]] = (
    f"{LSH_BUCKET_PREFIX}:*",
    f"{LSH_SIGNATURE_PREFIX}:*",
    "model:cooldown:*",
)

#: Hosts a full wipe may target. The production database lives on a remote VPS,
#: so restricting the host is what keeps this operation local by construction.
_LOCAL_HOSTS: Final[frozenset[str]] = frozenset(
    {"postgres", "localhost", "127.0.0.1", "::1"}
)

#: Batch size for ``SCAN`` and for the batched ``DEL``. Large enough to keep the
#: round trips down on a busy index, small enough that one call cannot block Redis
#: for a noticeable time.
_SCAN_COUNT: Final[int] = 500


class ResetBlockedError(RuntimeError):
    """The reset was refused. The message is operator-facing."""


class UnsafeWipeTargetError(RuntimeError):
    """A full wipe was refused because the configured database is not a scratch target."""


@dataclass(slots=True)
class ResetReport:
    """What the reset removed, per store.

    Counted rather than summarised: "queue cleared" is not a verifiable claim,
    while "34 posts, 21 clusters, 108 Redis keys" is.
    """

    raw_news: int = 0
    clusters: int = 0
    #: Clusters left in place because they carry an article. These stay in the
    #: level 2 semantic index, so the operator is told about them explicitly.
    clusters_kept: int = 0
    articles: int = 0
    journals: int = 0
    run_directories: int = 0
    sources_rewound: int = 0
    redis_keys: int = 0
    target_database: str = ""
    target_host: str = ""
    #: Non-fatal problems. A stale LSH bucket makes deduplication *over*-report
    #: duplicates, which is visible in the next run; refusing the whole reset
    #: because the cache blinked would leave the operator with no way forward.
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Everything removed, for the one-line summary."""
        return self.raw_news + self.clusters + self.articles + self.redis_keys

    def summary(self) -> str:
        """Operator-facing one-liner."""
        parts = [
            f"постов удалено: {self.raw_news}",
            f"кластеров: {self.clusters}",
            f"каналов сброшено: {self.sources_rewound}",
            f"ключей Redis: {self.redis_keys}",
        ]
        if self.clusters_kept:
            parts.append(f"сохранено кластеров со статьями: {self.clusters_kept}")
        if self.articles:
            parts.append(f"статей удалено: {self.articles}")
        if self.journals:
            parts.append(f"журналов удалено: {self.journals}")
        if self.run_directories:
            parts.append(f"каталогов прогонов: {self.run_directories}")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_news": self.raw_news,
            "clusters": self.clusters,
            "clusters_kept": self.clusters_kept,
            "articles": self.articles,
            "journals": self.journals,
            "run_directories": self.run_directories,
            "sources_rewound": self.sources_rewound,
            "redis_keys": self.redis_keys,
            "target_database": self.target_database,
            "target_host": self.target_host,
            "preserved": list(PRESERVED_TABLES),
            "total": self.total,
            "summary": self.summary(),
            "warnings": list(self.warnings),
        }


async def _delete_post_index_entries(
    redis: Redis,
    posts: list[tuple[int, list[int] | None, Any]],
    report: ResetReport,
) -> None:
    """Remove LSH entries for deleted posts without disturbing other days."""
    for raw_news_id, signature_values, cluster_id in posts:
        if signature_values:
            try:
                signature = signature_from_list(signature_values)
                member = f"{raw_news_id}:{'' if cluster_id is None else cluster_id}"
                for key in bucket_keys(signature):
                    await cast(Awaitable[int], redis.srem(key, member))
            except (RedisError, ValueError):
                logger.warning("Could not remove LSH buckets for post %s", raw_news_id)
                report.warnings.append(
                    f"Индекс дублей для поста {raw_news_id} может сохраниться до TTL."
                )
        try:
            report.redis_keys += int(
                await cast(Awaitable[int], redis.delete(signature_key(raw_news_id)))
                or 0
            )
        except RedisError:
            logger.warning("Could not remove cached signature for post %s", raw_news_id)
            report.warnings.append(
                f"Кэш подписи поста {raw_news_id} не удалось удалить."
            )


async def _delete_redis_keys(redis: Redis, report: ResetReport) -> None:
    """Delete every LSH and cooldown key, reporting what could not be removed.

    ``scan_iter`` rather than ``keys``: the index holds one bucket key per band
    per post, so a busy deployment has tens of thousands of them and ``KEYS``
    would block Redis while it built the whole list.
    """
    for pattern in _SCAN_PATTERNS:
        removed = 0
        try:
            batch: list[str] = []
            async for key in redis.scan_iter(match=pattern, count=_SCAN_COUNT):
                batch.append(key)
                if len(batch) >= _SCAN_COUNT:
                    removed += int(await redis.delete(*batch) or 0)
                    batch.clear()
            if batch:
                removed += int(await redis.delete(*batch) or 0)
        except RedisError:
            logger.warning("Could not clear Redis keys %s", pattern, exc_info=True)
            report.warnings.append(
                f"Ключи Redis «{pattern}» не удалось очистить: дедупликация может "
                "какое-то время считать заново собранные посты дублями."
            )
            continue
        report.redis_keys += removed
        logger.info("Reset removed %d Redis key(s) matching %s", removed, pattern)


async def _refuse_if_running(redis: Redis) -> None:
    """Raise when a collection pass holds the run lock.

    A reset mid-pass would delete the rows that pass is deduplicating against, and
    the pass would then write posts whose LSH entries the reset has already
    removed — an index disagreeing with the table, which is the one state the
    dedup ladder cannot explain to an operator.
    """
    try:
        held = await redis.get(RUN_LOCK_KEY)
    except RedisError:
        # Cannot verify: allow the reset rather than blocking the operator on a
        # cache failure. The worst case is the race above, which a second reset
        # fixes, while refusing forever on an unreachable Redis has no way out.
        logger.warning("Could not read the ingestion lock", exc_info=True)
        return
    if held:
        raise ResetBlockedError(
            "Сейчас идёт сбор новостей. Дождитесь окончания прохода и повторите "
            "сброс — иначе он удалит посты, с которыми этот проход сравнивает."
        )


def assert_safe_wipe_target(
    *,
    app_env: str | None = None,
    postgres_host: str | None = None,
    postgres_db: str | None = None,
) -> None:
    """Refuse a full wipe against production or a database without a scratch suffix.

    The three values default to the running configuration and are parameters only
    so the guard itself is testable: :class:`~app.config.Settings` is frozen, and
    a guard that cannot be exercised is a guard nobody trusts.
    """
    environment = settings.app_env if app_env is None else app_env
    host_value = settings.postgres_host if postgres_host is None else postgres_host
    database = settings.postgres_db if postgres_db is None else postgres_db

    if environment == "production":
        raise UnsafeWipeTargetError(
            "Полная очистка запрещена при APP_ENV=production. "
            "Это защита от случайного удаления рабочей базы."
        )
    if host_value.strip().lower() not in _LOCAL_HOSTS:
        raise UnsafeWipeTargetError(
            f"Полная очистка запрещена для хоста {host_value!r}: "
            "разрешены только локальный контейнер и loopback."
        )
    if not database.endswith(ALLOWED_WIPE_DB_SUFFIXES):
        raise UnsafeWipeTargetError(
            f"Полная очистка запрещена для базы {database!r}. "
            "Имя должно заканчиваться на "
            f"{', '.join(ALLOWED_WIPE_DB_SUFFIXES)}."
        )


async def preview_pipeline_wipe(session: AsyncSession) -> ResetReport:
    """Count what a full wipe would remove, without deleting anything."""
    report = ResetReport(
        target_database=settings.postgres_db,
        target_host=settings.postgres_host,
    )
    report.raw_news = int(
        (await session.execute(select(func.count()).select_from(RawNews))).scalar_one()
    )
    report.clusters = int(
        (
            await session.execute(select(func.count()).select_from(NewsCluster))
        ).scalar_one()
    )
    report.articles = int(
        (await session.execute(select(func.count()).select_from(Article))).scalar_one()
    )
    report.journals = int(
        (
            await session.execute(select(func.count()).select_from(ArticleJournal))
        ).scalar_one()
    )
    store = ArtifactStore()
    if store.root.is_dir():
        report.run_directories = sum(1 for path in store.root.iterdir() if path.is_dir())
    rewound = await session.execute(
        select(func.count())
        .select_from(Source)
        .where((Source.last_scraped_at.is_not(None)) | (Source.posts_collected != 0))
    )
    report.sources_rewound = int(rewound.scalar_one())
    return report


async def reset_pipeline_queue(
    session: AsyncSession,
    *,
    include_articles: bool = False,
    redis: Redis | None = None,
) -> ResetReport:
    """Clear the queue so the next pass collects as if it were the first.

    Returns a per-store count. Raises :class:`ResetBlockedError` when a collection
    pass is in flight and :class:`~sqlalchemy.exc.SQLAlchemyError` when the database
    work fails; the caller renders both as an operator-facing message.

    Order matters. PostgreSQL is emptied first, in one transaction, so a failure
    leaves both stores consistent. Redis is cleared afterwards: a leftover bucket
    whose row is gone makes deduplication over-report duplicates, which the report
    says out loud, whereas clearing the index while the rows survive would make it
    *under*-report them silently.
    """
    client = get_redis() if redis is None else redis
    await _refuse_if_running(client)

    report = ResetReport()
    try:
        rewound = await session.execute(
            update(Source)
            .where(
                (Source.last_scraped_at.is_not(None)) | (Source.posts_collected != 0)
            )
            .values(last_scraped_at=None, posts_collected=0)
        )
        report.sources_rewound = int(rewound.rowcount or 0)

        # Posts go first and unconditionally: they are the queue itself, and
        # `raw_news.cluster_id` is ON DELETE SET NULL, so removing them cannot
        # take a cluster with them.
        posts = await session.execute(delete(RawNews))
        report.raw_news = int(posts.rowcount or 0)

        has_article = exists().where(Article.cluster_id == NewsCluster.id)
        if include_articles:
            # Explicitly asked for an empty system. Journals cascade from
            # articles, but they are counted first so the report can name them.
            journals = await session.execute(
                select(func.count()).select_from(ArticleJournal)
            )
            report.journals = int(journals.scalar_one())
            articles = await session.execute(delete(Article))
            report.articles = int(articles.rowcount or 0)
            clusters = await session.execute(delete(NewsCluster))
        else:
            kept = await session.execute(
                select(func.count()).select_from(NewsCluster).where(has_article)
            )
            report.clusters_kept = int(kept.scalar_one())
            if report.clusters_kept:
                report.warnings.append(
                    f"Кластеров со статьями сохранено: {report.clusters_kept}. "
                    "Они остаются в семантическом индексе, поэтому заново "
                    "собранный пост об уже опубликованной новости попадёт в дубли, "
                    "а не откроет новый кластер."
                )
            clusters = await session.execute(
                delete(NewsCluster).where(~has_article)
            )
        report.clusters = int(clusters.rowcount or 0)

        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("Queue reset failed; nothing was removed")
        raise

    await _delete_redis_keys(client, report)

    logger.info("Queue reset by request: %s", report.summary())
    return report


async def wipe_pipeline_history(
    session: AsyncSession,
    *,
    confirm: bool,
    redis: Redis | None = None,
    store: ArtifactStore | None = None,
) -> ResetReport:
    """Delete generated content so the next collection is a first pass.

    ``confirm`` must be True: a preview and a wipe are different operations, and
    mixing them would make an accidental click irreversible. Sources, accounts,
    settings and Alembic history are left in place.
    """
    if not confirm:
        raise ResetBlockedError(
            "Полная очистка требует явного подтверждения: вызовите с confirm=True."
        )
    assert_safe_wipe_target()
    report = await reset_pipeline_queue(
        session, include_articles=True, redis=redis
    )
    report.target_database = settings.postgres_db
    report.target_host = settings.postgres_host
    artifact_store = store or ArtifactStore()
    try:
        report.run_directories = artifact_store.purge_all()
    except OSError:
        logger.warning("Could not purge artifact directories", exc_info=True)
        report.warnings.append(
            "Каталоги журналов не удалось удалить полностью: проверьте storage/runs."
        )
    return report


async def delete_day_from_queue(
    session: AsyncSession,
    *,
    day: date,
    redis: Redis | None = None,
) -> ResetReport:
    """Delete posts collected on one Moscow calendar day.

    ``day`` is interpreted in ``Europe/Moscow`` regardless of the server's local
    timezone, because the freshness gate the owner chose is a Moscow-day rule and
    the operator sees Moscow dates on the dashboard.

    Articles are never touched: a day's posts may already have produced material,
    and deleting the material would orphan the WordPress post. The operator who
    wants to delete the article must do that explicitly.

    Orphaned clusters (no article, no remaining posts) are removed so the
    deduplication index does not keep a ghost event.
    """
    client = get_redis() if redis is None else redis
    await _refuse_if_running(client)

    from app.services.ingest_freshness import MOSCOW_TZ  # noqa: PLC0415

    start = datetime(day.year, day.month, day.day, tzinfo=MOSCOW_TZ)
    end = start + timedelta(days=1)

    report = ResetReport()
    deleted_posts: list[tuple[int, list[int] | None, object | None]] = []
    try:
        rows = await session.execute(
            select(RawNews.id, RawNews.minhash_signature, RawNews.cluster_id).where(
                RawNews.created_at >= start,
                RawNews.created_at < end,
            )
        )
        deleted_posts = list(rows.tuples().all())
        affected_cluster_ids = {
            cluster_id
            for _raw_news_id, _signature, cluster_id in deleted_posts
            if cluster_id is not None
        }
        posts = await session.execute(
            delete(RawNews).where(
                RawNews.created_at >= start,
                RawNews.created_at < end,
            )
        )
        report.raw_news = int(posts.rowcount or 0)

        if affected_cluster_ids:
            has_article = exists().where(Article.cluster_id == NewsCluster.id)
            has_remaining_post = exists().where(RawNews.cluster_id == NewsCluster.id)
            kept = await session.execute(
                select(func.count())
                .select_from(NewsCluster)
                .where(NewsCluster.id.in_(affected_cluster_ids), has_article)
            )
            report.clusters_kept = int(kept.scalar_one())
            clusters = await session.execute(
                delete(NewsCluster).where(
                    NewsCluster.id.in_(affected_cluster_ids),
                    ~has_article,
                    ~has_remaining_post,
                )
            )
            report.clusters = int(clusters.rowcount or 0)

        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("Day deletion failed; nothing was removed")
        raise

    await _delete_post_index_entries(client, deleted_posts, report)

    report.target_database = settings.postgres_db
    report.target_host = settings.postgres_host
    logger.info(
        "Deleted day %s: %s", day.isoformat(), report.summary()
    )
    return report
