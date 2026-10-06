"""Ingestion orchestrator: collect every active source, filter, dedup, persist.

This module is the only place in Package 2 that writes to the database. The two
collectors (:mod:`app.services.telegram_ingestor`,
:mod:`app.services.web_scraper`) return value objects and the dedup engine
(:mod:`app.services.deduplicator`) mutates ORM objects without committing, so the
transaction boundary, the ``ON CONFLICT`` semantics and the "what counts as an
error" policy all live here.

Design decisions worth stating outright, because each of them is easy to
"simplify" into a bug:

* **Skip on lock contention, never wait.** Two lock keys fence a run: a global
  ``lock:ingestion:run`` so overlapping scheduled ticks cannot double-ingest, and
  a per-source ``lock:ingestion:source:{id}`` so a manual trigger from the
  dashboard cannot race the scheduled run on the same source. Both are acquired
  non-blockingly and a held lock is logged and skipped. Queueing behind the
  current holder would be worse than skipping: the scheduler fires on a fixed
  period, so waiting runs pile up, and by the time a waiter starts, the run it
  waited for has already collected the posts it would have collected.

* **``INSERT ... ON CONFLICT (source_id, external_post_id) DO NOTHING``.** This is
  the idempotency guarantee required by ``AGENTS.md``: the Telegram collector
  deliberately re-reads the tail of every feed (``INGEST_OVERLAP_MESSAGES``), so
  re-seeing a post is normal operation, not an error. ``RETURNING id``
  distinguishes a real insert from a conflict, which is what separates the
  ``inserted`` and ``skipped_existing`` counters. Existing rows are left
  untouched: a later edit at the source must not rewrite a row whose article has
  already been generated and published.

* **Filtered posts bypass the LSH index.** A digest or an advert is stored with
  ``dedup_status = FILTERED_DIGEST`` / ``FILTERED_AD`` and ``is_processed =
  True``, and :func:`~app.services.deduplicator.deduplicate` is never called for
  it. Indexing such posts would poison the dedup index: one "Главное к этому
  часу" digest shares shingles with every story it summarises, so it would start
  matching unrelated real news at level 1.

* **One transaction per source.** Each source commits on its own, so a channel
  that fails at message 40 cannot roll back the 39 posts already collected from
  the previous source. Failures are recorded in the per-source report and the run
  continues; the session is rolled back before moving on so the next source
  starts clean.

* **``last_scraped_at`` marks success only.** A source whose pass raised keeps its
  previous timestamp, so the scheduler naturally retries it first (sources are
  ordered by ``last_scraped_at NULLS FIRST``, per docs/03).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from typing import Any, Final, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.typing import StartupShutdown, WorkerCoroutine
from redis.asyncio import Redis
from sqlalchemy import BigInteger, case, func, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.enums import DedupStatus, SourceType
from app.models.raw_news import RawNews
from app.models.source import Source
from app.redis_client import close_redis_pool, get_redis
from app.services.ai_client import close_ai_client, get_ai_client
from app.services.deduplicator import Deduplicator
from app.services.embedding_service import close_embedding_service
from app.services.flux_generator import close_flux_client
from app.services.ingest_freshness import (
    is_on_or_after_moscow_today,
    needs_freshness_cutoff,
)
from app.services.pipeline_control import pipeline_enabled
from app.services.telegram_ingestor import (
    IngestResult,
    PreparedPost,
    TelegramIngestorError,
    TelegramNotConfiguredError,
    close_telegram_ingestor,
    get_telegram_ingestor,
)
from app.services.telegram_mtproto import MTProtoIngestor
from app.services.text_utils import FilterResult, analyze_post
from app.services.web_scraper import (
    ScrapedArticle,
    ScraperError,
    close_browser_pool,
    discover_links,
    get_browser_pool,
    scrape_article,
)
from app.workers.redis_lock import acquire_lock

logger: Final[logging.Logger] = logging.getLogger(__name__)

#: Global run lock: only one ingestion pass may be in flight per deployment.
RUN_LOCK_KEY: Final[str] = "lock:ingestion:run"

#: Per-source lock prefix, so a manual trigger and the scheduled run cannot
#: collect the same source simultaneously.
SOURCE_LOCK_PREFIX: Final[str] = "lock:ingestion:source"

#: Name the ARQ task is registered under (also its cron job name).
INGESTION_TASK_NAME: Final[str] = "ingestion_task"


class TelegramReader(Protocol):
    """The single method this worker needs from a Telegram transport.

    Both the MTProto client and the GramJS bridge satisfy it, so the worker can
    prefer pooled accounts and fall back to the bridge without either module
    importing the other or a shared base class being invented for two
    implementations.
    """

    async def ingest_source(
        self, source: Source, *, last_max_id: int | None = ...
    ) -> IngestResult:
        """Collect one source and return its prepared candidates."""
        ...

#: Dedicated ARQ queue for this worker. Each worker owns one, because ARQ's
#: default queue is shared: any worker polling it may claim a job belonging to
#: another, then fail it as "function not found".
INGESTION_QUEUE_NAME: Final[str] = "arq:queue:ingestion"

#: Minutes of the hour the cron job fires on: every five minutes. A mutable
#: ``set`` because that is the type ``arq.cron`` accepts; it is never mutated.
#: Overlap is harmless, since the run is lock-fenced, so a tick landing on a
#: still-running pass is skipped rather than doubling the work.
INGESTION_CRON_MINUTES: Final[set[int]] = set(range(0, 60, 5))

#: Hex characters kept from the sha256 of a canonical URL when deriving a web
#: post's ``external_post_id``. 32 hex chars (128 bits) makes an accidental
#: collision impossible in practice while staying far inside the
#: ``String(255)`` column, which a long URL with query parameters would not.
URL_HASH_LENGTH: Final[int] = 32

#: Prefix marking a web ``external_post_id`` as a hashed URL, so an operator
#: reading the column knows the value is derived rather than upstream-assigned.
URL_ID_PREFIX: Final[str] = "url"

#: Query parameters dropped when canonicalising an article URL. Campaign tags
#: differ between the listing page and a social share of the same article, so
#: keeping them would create a second ``raw_news`` row for one article.
_TRACKING_PARAM_PREFIXES: Final[tuple[str, ...]] = ("utm_",)
_TRACKING_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "_openstat",
        "fbclid",
        "from",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "yclid",
    }
)


def source_lock_key(source_id: int) -> str:
    """Redis key fencing one source's collection pass."""
    return f"{SOURCE_LOCK_PREFIX}:{source_id}"


def canonical_url(url: str) -> str:
    """Normalise an article URL so one article yields one stable key.

    Drops the fragment and known tracking parameters, lowercases scheme and
    host, and removes a trailing slash from a non-empty path (docs/03,
    "Идемпотентность"). Remaining query parameters are kept — many Russian CMS
    installations still address articles as ``?id=123`` — and re-encoded in their
    original order so the output is byte-stable across runs.
    """
    parsed = urlparse(url.strip())
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
        and not key.lower().startswith(_TRACKING_PARAM_PREFIXES)
    ]
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            parsed.params,
            urlencode(kept),
            "",
        )
    )


def web_external_post_id(url: str) -> str:
    """Derive the ``external_post_id`` of a scraped article from its URL.

    docs/03 specifies the canonical URL itself as the key. It is used here as the
    *input* to a truncated sha256 rather than stored verbatim, because
    ``raw_news.external_post_id`` is ``String(255)`` and real news URLs with
    query strings overflow that, which would abort the insert instead of
    deduplicating. Hashing keeps the property that actually matters: the value is
    *derived* from the article, so a later run produces the same key and hits the
    ``uq_raw_news_source_external_post`` constraint. The readable URL is still
    persisted, in ``raw_news.post_url``.
    """
    canonical = canonical_url(url)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{URL_ID_PREFIX}:{digest[:URL_HASH_LENGTH]}"


@dataclass(frozen=True, slots=True)
class SourceIngestReport:
    """Outcome of one source's collection pass.

    ``fetched`` counts what the collector returned (messages for Telegram, links
    for a website), while ``inserted + skipped_existing`` accounts for every
    candidate the writer saw. ``errors`` is a list rather than a raised
    exception: a dead channel is expected operation for a news pipeline.
    """

    source_id: int
    source_name: str
    fetched: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    filtered_digest: int = 0
    filtered_ad: int = 0
    duplicates: int = 0
    updates: int = 0
    new_clusters: int = 0
    errors: tuple[str, ...] = ()
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """Whether the pass completed without recorded errors."""
        return not self.errors


@dataclass(frozen=True, slots=True)
class IngestionRunReport:
    """Aggregate of one run, returned to the scheduler and the dashboard API."""

    sources: tuple[SourceIngestReport, ...]
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    lock_skipped: bool = False

    @property
    def fetched(self) -> int:
        return sum(report.fetched for report in self.sources)

    @property
    def inserted(self) -> int:
        return sum(report.inserted for report in self.sources)

    @property
    def skipped_existing(self) -> int:
        return sum(report.skipped_existing for report in self.sources)

    @property
    def filtered_digest(self) -> int:
        return sum(report.filtered_digest for report in self.sources)

    @property
    def filtered_ad(self) -> int:
        return sum(report.filtered_ad for report in self.sources)

    @property
    def duplicates(self) -> int:
        return sum(report.duplicates for report in self.sources)

    @property
    def updates(self) -> int:
        return sum(report.updates for report in self.sources)

    @property
    def new_clusters(self) -> int:
        return sum(report.new_clusters for report in self.sources)

    @property
    def errors(self) -> tuple[str, ...]:
        """Every per-source error, prefixed with the source it came from."""
        return tuple(
            f"source {report.source_id}: {message}"
            for report in self.sources
            for message in report.errors
        )

    @property
    def failed_sources(self) -> int:
        return sum(1 for report in self.sources if not report.ok)

    @property
    def ok(self) -> bool:
        """Whether the run touched every source without an error."""
        return not self.lock_skipped and self.failed_sources == 0

    def summary(self) -> str:
        """One compact line for logs and for the API response."""
        if self.lock_skipped:
            return "ingestion skipped: another run holds the lock"
        return (
            f"ingestion: {len(self.sources)} source(s) in {self.duration_ms} ms — "
            f"fetched={self.fetched} inserted={self.inserted} "
            f"existing={self.skipped_existing} digest={self.filtered_digest} "
            f"ads={self.filtered_ad} duplicates={self.duplicates} "
            f"updates={self.updates} new_clusters={self.new_clusters} "
            f"failed_sources={self.failed_sources}"
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for the ARQ result and the HTMX dashboard."""
        return {
            "summary": self.summary(),
            "lock_skipped": self.lock_skipped,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": self.duration_ms,
            "totals": {
                "sources": len(self.sources),
                "fetched": self.fetched,
                "inserted": self.inserted,
                "skipped_existing": self.skipped_existing,
                "filtered_digest": self.filtered_digest,
                "filtered_ad": self.filtered_ad,
                "duplicates": self.duplicates,
                "updates": self.updates,
                "new_clusters": self.new_clusters,
                "failed_sources": self.failed_sources,
            },
            "sources": [
                {
                    "source_id": report.source_id,
                    "source_name": report.source_name,
                    "fetched": report.fetched,
                    "inserted": report.inserted,
                    "skipped_existing": report.skipped_existing,
                    "filtered_digest": report.filtered_digest,
                    "filtered_ad": report.filtered_ad,
                    "duplicates": report.duplicates,
                    "updates": report.updates,
                    "new_clusters": report.new_clusters,
                    "duration_ms": report.duration_ms,
                    "errors": list(report.errors),
                }
                for report in self.sources
            ],
            "errors": list(self.errors),
        }


@dataclass(slots=True)
class _Counters:
    """Mutable tally for one source, frozen into a report when the pass ends."""

    fetched: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    filtered_digest: int = 0
    filtered_ad: int = 0
    duplicates: int = 0
    updates: int = 0
    new_clusters: int = 0
    errors: list[str] = field(default_factory=list)
    #: Highest Telegram message id seen in this pass, carried out of the collector
    #: so the read acknowledgement can be sent *after* the batch commits. Left
    #: ``None`` for website sources and for a pass that fetched nothing.
    max_message_id: int | None = None

    def freeze(
        self, source: Source, *, duration_ms: int
    ) -> SourceIngestReport:
        return SourceIngestReport(
            source_id=source.id,
            source_name=source.name,
            fetched=self.fetched,
            inserted=self.inserted,
            skipped_existing=self.skipped_existing,
            filtered_digest=self.filtered_digest,
            filtered_ad=self.filtered_ad,
            duplicates=self.duplicates,
            updates=self.updates,
            new_clusters=self.new_clusters,
            errors=tuple(self.errors),
            duration_ms=duration_ms,
        )


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One prospective row, normalised across the Telegram and website paths."""

    external_post_id: str
    post_url: str | None
    raw_text: str
    filter_result: FilterResult
    media_data: tuple[dict[str, Any], ...]

    @classmethod
    def from_prepared(cls, prepared: PreparedPost) -> _Candidate:
        """Adapt a :class:`~app.services.telegram_ingestor.PreparedPost`."""
        fields = prepared.to_raw_news_fields()
        media: list[dict[str, Any]] = list(fields["media_data"])
        return cls(
            external_post_id=str(fields["external_post_id"]),
            post_url=fields["post_url"],
            raw_text=str(fields["raw_text"]),
            filter_result=prepared.filter_result,
            media_data=tuple(media),
        )

    @classmethod
    def from_article(cls, article: ScrapedArticle, *, skip_media: bool) -> _Candidate:
        """Adapt a scraped page and run the same 0-token filter over its body.

        The title is prepended because a web article keeps it in a separate node
        while a Telegram post keeps it in the first line, and both the digest/ad
        heuristics and the dedup shingler read a single text field: the two paths
        have to agree on what that field contains.

        **The filter reads** ``ScrapedArticle.content_html`` **, never**
        ``ScrapedArticle.html``. The latter is ``page.content()`` — the whole page
        DOM — so handing it to ``analyze_post(html=...)`` would count the site's
        navigation, related-articles rail and social buttons as links and trip
        the ``ad:link_density`` rule (>= 5 links in < 400 chars) on perfectly
        ordinary short news items, and any page carrying a real advertising block
        outside the article would be filed as an advert. It would also store the
        entire DOM, scripts included, in ``raw_news.clean_html``.

        ``content_html`` is the sanitised *article body*, which is what
        ``clean_html`` is for: it preserves the paragraph structure, headings and
        links of the story for Package 3 and the publishers, while the link
        density seen by the filter is the article's own. Detection still runs on
        text derived from that same fragment, so markup and plain text can never
        describe different content. A media-only page with no markup falls back
        to the text path.

        Media metadata mirrors the Telegram shape (docs/03) with ``media_id``
        holding the absolute image URL and no originating post id; Package 3 can
        tell the two apart by the ``http`` scheme.
        """
        body = article.text
        if article.title and not body.startswith(article.title):
            body = f"{article.title}\n\n{body}" if body else article.title
        media = (
            ()
            if skip_media
            else tuple(
                {
                    "type": "photo",
                    "original_post_id": None,
                    "media_id": url,
                    "size": None,
                }
                for url in article.media_urls
            )
        )
        # The heading carries the story's own headline, so the markup handed to
        # the filter is the same content as `raw_text` rather than the body alone.
        content_html = article.content_html
        if content_html and article.title:
            content_html = f"<h1>{escape(article.title, quote=False)}</h1>{content_html}"
        filter_result = (
            analyze_post(body, html=content_html)
            if content_html
            else analyze_post(body)
        )
        return cls(
            external_post_id=web_external_post_id(article.url),
            post_url=article.url,
            raw_text=body,
            filter_result=filter_result,
            media_data=media,
        )

    @property
    def filtered_status(self) -> DedupStatus | None:
        """Terminal status for a heuristically rejected post, else ``None``.

        Digest wins over advert when both fire, matching
        :meth:`Deduplicator._record_filtered` so the two code paths cannot
        disagree about a post that is both.
        """
        if self.filter_result.is_digest:
            return DedupStatus.FILTERED_DIGEST
        if self.filter_result.is_ad:
            return DedupStatus.FILTERED_AD
        return None

    def insert_values(self, source_id: int) -> dict[str, Any]:
        """Column values for the ``INSERT`` of this candidate."""
        status = self.filtered_status
        return {
            "source_id": source_id,
            "external_post_id": self.external_post_id,
            "post_url": self.post_url,
            "raw_text": self.raw_text,
            "clean_html": self.filter_result.clean_html,
            "media_data": [dict(item) for item in self.media_data],
            "is_digest": self.filter_result.is_digest,
            "is_ad": self.filter_result.is_ad,
            "filter_reasons": list(self.filter_result.reasons),
            "dedup_status": DedupStatus.PENDING if status is None else status,
            # A filtered post is terminal: no clustering, no enrichment, no
            # article. Marking it processed here keeps it out of every
            # downstream queue in the same statement that creates it.
            "is_processed": status is not None,
            "entities": {},
        }

    @property
    def dedup_text(self) -> str:
        """Text handed to the dedup ladder.

        ``clean_text`` has signatures and subscribe footers stripped, so two
        channels republishing one wire story compare on the story alone.
        """
        return self.filter_result.clean_text or self.raw_text


async def telegram_watermark(session: AsyncSession, source: Source) -> int | None:
    """Highest numeric post ID or album media message ID stored for a Telegram source.

    Restoring the cursor from PostgreSQL (rather than Redis) is what makes the
    feed position survive a Redis flush, per docs/03.

    ``external_post_id`` is a ``String`` column shared with website sources,
    whose keys are hashed URLs, so an unguarded ``CAST(... AS BIGINT)`` raises
    ``invalid input syntax for type bigint`` the moment it meets one. Two guards
    make the query safe:

    * the ``source_id`` predicate keeps the scan on this one telegram source;
    * the cast sits inside a ``CASE`` whose regex test gates it, so a stray
      non-numeric key on a telegram source (a manual insert, a migration
      artefact, a future key scheme) is skipped rather than failing the run.

    The ``CASE`` is what makes it robust: putting the regex in ``WHERE`` and the
    cast in the target list would also work today, but it relies on the planner
    filtering before evaluating the aggregate's argument. ``CASE`` never
    evaluates an untaken branch, so correctness no longer depends on the plan.
    The bound is 18 digits because a 19-digit value can overflow ``BIGINT``.
    """
    numeric_only = RawNews.external_post_id.op("~")("^[0-9]{1,18}$")
    numeric_id = case(
        (numeric_only, sa_cast(RawNews.external_post_id, BigInteger)),
        else_=None,
    )
    statement = select(func.max(numeric_id)).where(RawNews.source_id == source.id)
    watermark = (await session.execute(statement)).scalar_one_or_none()

    # Also inspect recent media attachments to find trailing message IDs in albums
    stmt_media = (
        select(RawNews.media_data)
        .where(RawNews.source_id == source.id)
        .order_by(RawNews.id.desc())
        .limit(30)
    )
    media_rows = (await session.execute(stmt_media)).scalars().all()
    max_media_id: int | None = None
    for mdata in media_rows:
        if isinstance(mdata, list):
            for item in mdata:
                if isinstance(item, dict):
                    opid = item.get("original_post_id")
                    if isinstance(opid, int) and (
                        max_media_id is None or opid > max_media_id
                    ):
                        max_media_id = opid

    candidates = [int(v) for v in (watermark, max_media_id) if v is not None]
    return max(candidates) if candidates else None


async def _existing_post_urls(
    session: AsyncSession, source: Source, urls: Sequence[str]
) -> set[str]:
    """Return which of ``urls`` this source already stored.

    One ``IN`` query instead of a lookup per link: a listing page yields dozens
    of URLs, nearly all of them already known, and a round trip each would
    dominate the pass.
    """
    if not urls:
        return set()
    statement = select(RawNews.post_url).where(
        RawNews.source_id == source.id, RawNews.post_url.in_(list(urls))
    )
    rows = await session.execute(statement)
    return {value for value in rows.scalars() if value is not None}


async def _insert_candidate(
    session: AsyncSession, source: Source, candidate: _Candidate
) -> RawNews | None:
    """Insert one candidate, or return ``None`` when the row already exists.

    ``ON CONFLICT (source_id, external_post_id) DO NOTHING ... RETURNING id`` is
    the idempotency contract from ``AGENTS.md``: a re-collected post is a no-op,
    and the empty ``RETURNING`` is how a conflict is detected without a second
    query. Existing rows are deliberately not updated — a post whose article is
    already published must not be silently rewritten by a late source edit.

    The freshly inserted row is then loaded as an ORM object, because the dedup
    engine mutates a mapped instance and needs the generated primary key as its
    LSH index member.
    """
    # Guard: if candidate has no text and carries media, verify it is not an orphan
    # slice of an already-stored album
    if not candidate.raw_text.strip() and candidate.media_data:
        op_ids = {
            item.get("original_post_id")
            for item in candidate.media_data
            if isinstance(item, dict) and item.get("original_post_id") is not None
        }
        if op_ids:
            stmt_recent = (
                select(RawNews.media_data)
                .where(RawNews.source_id == source.id)
                .order_by(RawNews.id.desc())
                .limit(30)
            )
            for mdata in (await session.execute(stmt_recent)).scalars():
                if isinstance(mdata, list):
                    for mitem in mdata:
                        if (
                            isinstance(mitem, dict)
                            and mitem.get("original_post_id") in op_ids
                        ):
                            logger.info(
                                "Skipping orphan album slice %s for source %s",
                                candidate.external_post_id,
                                source.name,
                            )
                            return None

    values = candidate.insert_values(source.id)
    statement = (
        insert(RawNews)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_raw_news_source_external_post")
        .returning(RawNews.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    if inserted_id is None:
        return None
    return await session.get(RawNews, inserted_id)


async def _apply_dedup(
    engine: Deduplicator,
    session: AsyncSession,
    row: RawNews,
    candidate: _Candidate,
    counters: _Counters,
) -> None:
    """Run the dedup ladder for one surviving post and record its outcome.

    The decision is written onto the row by the engine itself; this only tallies
    it and marks a terminal duplicate as processed. A ``UNIQUE`` or ``UPDATE``
    row is deliberately left unprocessed: the enrichment stage (Package 3) polls
    exactly that backlog.
    """
    decision = await engine.deduplicate(session, row, text=candidate.dedup_text)
    row.dedup_status = decision.status
    row.similarity_score = decision.similarity
    row.dedup_level = decision.level
    if decision.status is DedupStatus.SKIPPED_DUPLICATE:
        row.cluster_id = None
        row.is_processed = True
        counters.duplicates += 1
    else:
        row.cluster_id = decision.cluster_id
    if decision.status is DedupStatus.UPDATE:
        counters.updates += 1
    if decision.is_new_cluster:
        counters.new_clusters += 1


async def _persist(
    engine: Deduplicator,
    session: AsyncSession,
    source: Source,
    candidates: Sequence[_Candidate],
    counters: _Counters,
) -> None:
    """Write every candidate of one source, deduplicating the survivors.

    Filtered posts are stored and closed here and never reach
    :meth:`Deduplicator.deduplicate`, so they are never published into the Redis
    LSH index. That is deliberate: a digest shares shingles with every story it
    summarises, so indexing one would make unrelated real news look like a
    duplicate of it at level 1.
    """
    for candidate in candidates:
        row = await _insert_candidate(session, source, candidate)
        if row is None:
            counters.skipped_existing += 1
            continue
        counters.inserted += 1
        status = candidate.filtered_status
        if status is DedupStatus.FILTERED_DIGEST:
            counters.filtered_digest += 1
            continue
        if status is DedupStatus.FILTERED_AD:
            counters.filtered_ad += 1
            continue
        await _apply_dedup(engine, session, row, candidate, counters)


async def _collect_telegram(
    session: AsyncSession,
    source: Source,
    counters: _Counters,
    *,
    ingestor: TelegramReader | None = None,
) -> list[_Candidate]:
    """Collect one Telegram source.

    ``ingestor`` is supplied by the run so a single MTProto connection — and a
    single claimed account — serves every Telegram source in the pass. Opening one
    per source would multiply the sign-in handshakes and, worse, claim a different
    account for each channel, defeating the rotation the pool exists to provide.

    Falling back to the GramJS bridge keeps a deployment that has no Telegram
    accounts configured working exactly as before.
    """
    reader: TelegramReader = (
        get_telegram_ingestor() if ingestor is None else ingestor
    )
    watermark = await telegram_watermark(session, source)
    result = await reader.ingest_source(source, last_max_id=watermark)
    counters.fetched += result.fetched_messages
    counters.errors.extend(result.errors)
    # Recorded rather than acted on here: the acknowledgement must wait until the
    # rows are committed, which happens in `ingest_single_source`.
    counters.max_message_id = result.max_message_id
    return [_Candidate.from_prepared(prepared) for prepared in result.candidates]


async def _collect_website(
    session: AsyncSession, source: Source, counters: _Counters
) -> list[_Candidate]:
    """Discover and scrape the new articles of one website source.

    Already-stored URLs are filtered out before any page is opened: scraping is
    the most expensive step in the pipeline (one Chromium page each), and a
    listing page is mostly articles from previous runs. The ``ON CONFLICT`` guard
    still backs this up for the race where the same URL is collected twice
    concurrently.
    """
    links = await discover_links(source.identifier, source.crawl_rules)
    counters.fetched += len(links)
    known = await _existing_post_urls(session, source, links)
    apply_cutoff = needs_freshness_cutoff(
        source.last_scraped_at, now=datetime.now(UTC)
    )
    keep_limit = settings.ingest_max_messages_per_source
    candidates: list[_Candidate] = []
    for url in links:
        if url in known:
            counters.skipped_existing += 1
            continue
        try:
            article = await scrape_article(url, source.crawl_rules)
        except ScraperError as exc:
            logger.warning("Scraping %s failed: %s", url, exc)
            counters.errors.append(f"{url}: {exc}")
            continue
        if apply_cutoff and not is_on_or_after_moscow_today(
            article.published_at, now=datetime.now(UTC)
        ):
            continue
        candidates.append(
            _Candidate.from_article(article, skip_media=source.skip_media)
        )
        if apply_cutoff and len(candidates) >= keep_limit:
            break
    return candidates


async def _acknowledge_reads(
    source: Source,
    counters: _Counters,
    *,
    telegram: TelegramReader | None,
) -> None:
    """Mark a Telegram source read, after its batch has been committed.

    Silent no-op for a website source, for a pass that fetched nothing, and for a
    transport that cannot acknowledge (the GramJS bridge does its own marking
    internally). Never raises: the rows are already committed, so a failure here
    must not turn a successful pass into a reported error.
    """
    if source.source_type is not SourceType.TELEGRAM:
        return
    max_id = counters.max_message_id
    if max_id is None or telegram is None:
        return
    acknowledge = getattr(telegram, "mark_read", None)
    if acknowledge is None:
        return
    await acknowledge(source.identifier.strip(), max_id)


async def _collect(
    session: AsyncSession,
    source: Source,
    counters: _Counters,
    *,
    telegram: TelegramReader | None = None,
) -> list[_Candidate]:
    """Dispatch to the collector matching this source's type."""
    if source.source_type is SourceType.TELEGRAM:
        return await _collect_telegram(
            session, source, counters, ingestor=telegram
        )
    return await _collect_website(session, source, counters)


async def ingest_single_source(
    session: AsyncSession,
    source: Source,
    *,
    engine: Deduplicator | None = None,
    telegram: TelegramReader | None = None,
) -> SourceIngestReport:
    """Collect, filter, deduplicate and persist one source, then commit.

    Commits on its own so a later source's failure cannot roll this source's work
    back, and updates ``sources.last_scraped_at`` only when the pass got as far
    as a successful commit. Raises nothing for source-level failures: they are
    returned inside :attr:`SourceIngestReport.errors`, after a rollback that
    leaves the session usable for the next source.

    ``engine`` is injectable so a whole run can share one
    :class:`~app.services.deduplicator.Deduplicator` (and therefore one Redis
    client) instead of building one per source.
    """
    started = time.perf_counter()
    counters = _Counters()
    owns_engine = engine is None
    dedup_engine = Deduplicator() if engine is None else engine
    try:
        candidates = await _collect(session, source, counters, telegram=telegram)
        await _persist(dedup_engine, session, source, candidates, counters)
        source.last_scraped_at = datetime.now(UTC)
        await session.commit()
        # Only now, with the batch durably stored, is the channel marked read.
        # Acknowledging before the commit would leave posts read in Telegram but
        # missing from the database — and an operator scanning the channel for what
        # the pipeline skipped would have no way to see them.
        await _acknowledge_reads(source, counters, telegram=telegram)
    except TelegramNotConfiguredError as exc:
        # A website-only deployment is valid: skip telegram sources cleanly
        # instead of failing the run.
        await session.rollback()
        logger.warning("Skipping telegram source %s: %s", source.name, exc)
        counters.errors.append(f"telegram not configured: {exc}")
    except (SQLAlchemyError, ScraperError, OSError, ValueError) as exc:
        await session.rollback()
        logger.warning(
            "Source %s (%s) failed: %s",
            source.name,
            source.source_type.value,
            exc,
            exc_info=True,
        )
        counters.errors.append(f"{type(exc).__name__}: {exc}")
    # Deliberately broad: a collector or the dedup ladder may raise anything
    # (a bridge shape change, a Playwright internal error), and one bad source
    # must never abort a run that still has other sources to collect.
    except Exception as exc:
        await session.rollback()
        logger.warning(
            "Source %s (%s) raised an unexpected error",
            source.name,
            source.source_type.value,
            exc_info=True,
        )
        counters.errors.append(f"unexpected {type(exc).__name__}: {exc}")
    finally:
        if owns_engine:
            await dedup_engine.close()

    duration_ms = max(int((time.perf_counter() - started) * 1000), 0)
    report = counters.freeze(source, duration_ms=duration_ms)
    logger.info(
        "Source %s (%s): fetched=%d inserted=%d existing=%d digest=%d ads=%d "
        "duplicates=%d updates=%d new_clusters=%d in %d ms",
        source.name,
        source.source_type.value,
        report.fetched,
        report.inserted,
        report.skipped_existing,
        report.filtered_digest,
        report.filtered_ad,
        report.duplicates,
        report.updates,
        report.new_clusters,
        duration_ms,
    )
    return report


async def _load_sources(
    session: AsyncSession, source_ids: Sequence[int] | None
) -> list[Source]:
    """Load the active sources of this run, in a deterministic order.

    Ordered by ``last_scraped_at NULLS FIRST`` (index
    ``ix_sources_last_scraped_at``) so a new or long-neglected source is
    collected first, with the primary key as a tie-break to keep two runs over
    identical data in the same order.
    """
    statement = select(Source).where(Source.is_active.is_(True))
    if source_ids is not None:
        statement = statement.where(Source.id.in_(list(source_ids)))
    statement = statement.order_by(
        Source.last_scraped_at.asc().nullsfirst(), Source.id.asc()
    )
    return list((await session.execute(statement)).scalars().all())


async def run_ingestion(
    *,
    source_ids: Sequence[int] | None = None,
    session_factory: Callable[[], AsyncSession] = AsyncSessionLocal,
) -> IngestionRunReport:
    """Collect every active source (or just ``source_ids``) exactly once.

    Fenced by the global run lock plus one lock per source, both acquired
    without blocking: a contended lock is logged and skipped, because the
    scheduler will fire again and waiting would only stack up runs (see the
    module docstring). A global-lock conflict returns immediately with
    ``lock_skipped=True`` and zero work done.

    ``session_factory`` is injectable so the whole flow can be exercised against
    a fake session without PostgreSQL.
    """
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    reports: list[SourceIngestReport] = []
    lock_skipped = False
    redis = get_redis()

    try:
        async with acquire_lock(
            redis, RUN_LOCK_KEY, ttl=settings.ingest_lock_ttl
        ) as run_lock:
            if not run_lock:
                lock_skipped = True
                logger.info(
                    "Ingestion skipped: %s is held by another run", RUN_LOCK_KEY
                )
            else:
                reports = await _run_locked(redis, source_ids, session_factory)
    finally:
        await redis.aclose()

    finished_at = datetime.now(UTC)
    report = IngestionRunReport(
        sources=tuple(reports),
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=max(int((time.perf_counter() - started) * 1000), 0),
        lock_skipped=lock_skipped,
    )
    logger.info("%s", report.summary())
    return report


async def _run_locked(
    redis: Redis,
    source_ids: Sequence[int] | None,
    session_factory: Callable[[], AsyncSession],
) -> list[SourceIngestReport]:
    """Body of a run that holds the global lock.

    One session and one :class:`Deduplicator` are shared by every source (the
    session is committed per source, so sharing it does not widen the
    transaction boundary), which keeps a run to a single database connection and
    a single Redis client.
    """
    reports: list[SourceIngestReport] = []
    async with AsyncExitStack() as stack:
        session = session_factory()
        stack.push_async_callback(session.close)
        engine = Deduplicator(redis=redis)
        stack.push_async_callback(engine.close)

        sources = await _load_sources(session, source_ids)
        logger.info(
            "Ingestion run starting for %d active source(s)%s",
            len(sources),
            "" if source_ids is None else " (manual trigger)",
        )

        # One MTProto client for the whole pass, claiming one pooled account.
        # Opened only when a Telegram source is actually due, so a website-only
        # run never touches the account pool.
        telegram = await _open_telegram_reader(session, sources, stack)

        for source in sources:
            async with acquire_lock(
                redis, source_lock_key(source.id), ttl=settings.ingest_lock_ttl
            ) as source_lock:
                if not source_lock:
                    logger.info(
                        "Source %s (id=%d) is already being collected; skipping",
                        source.name,
                        source.id,
                    )
                    continue
                reports.append(
                    await ingest_single_source(
                        session, source, engine=engine, telegram=telegram
                    )
                )

        # A pass that read Telegram without a transport error clears the
        # account's failure counter, so three unrelated failures spread over
        # weeks cannot retire a healthy account.
        if isinstance(telegram, MTProtoIngestor) and not any(
            report.errors for report in reports
        ):
            await telegram.report_success()
    return reports


async def _open_telegram_reader(
    session: AsyncSession,
    sources: Sequence[Source],
    stack: AsyncExitStack,
) -> TelegramReader | None:
    """Open the MTProto reader for this pass, or fall back to the bridge.

    Returns ``None`` when no Telegram source is due, when no account is available,
    or when the connection fails. ``None`` makes ``_collect_telegram`` fall back to
    the GramJS bridge, which then reports its own "not configured" error per source
    — the same behaviour a deployment without Telegram had before the pool existed.
    """
    if not any(source.source_type is SourceType.TELEGRAM for source in sources):
        return None

    reader = MTProtoIngestor(session)
    try:
        await reader.start()
    except TelegramNotConfiguredError as exc:
        logger.info(
            "No Telegram account available (%s); falling back to the GramJS bridge",
            exc,
        )
        return None
    except TelegramIngestorError:
        logger.warning(
            "Could not open an MTProto session; falling back to the GramJS bridge",
            exc_info=True,
        )
        return None

    stack.push_async_callback(reader.close)
    return reader


async def ingestion_task(
    ctx: dict[str, Any], *, source_ids: Sequence[int] | None = None
) -> dict[str, Any]:
    """ARQ entry point: run one ingestion pass and return it as plain JSON.

    A thin wrapper by design — every decision lives in :func:`run_ingestion`, so
    the task itself has nothing to test. ``ctx`` is ARQ's job metadata (job id,
    enqueue time, retry count) and is not read here.

    ``source_ids`` is an ARQ job keyword, so a caller can restrict a pass to
    specific sources. ``_queue_name`` is mandatory: this worker listens on
    :data:`INGESTION_QUEUE_NAME`, not ARQ's default queue, so a job enqueued
    without it would sit on ``arq:queue`` where nothing is listening::

        await pool.enqueue_job(
            "ingestion_task", source_ids=[7], _queue_name=INGESTION_QUEUE_NAME
        )

    The dashboard's manual trigger does not use this path — ``POST
    /api/pipeline/ingest-now`` calls :func:`run_ingestion` inline so the operator
    gets the real report rather than a job id.

    The cron job supplies no arguments and therefore collects every active
    source. The return value is a plain ``dict`` because ARQ persists it as the
    job result, which must be serialisable.
    """
    # The production-mode switch is checked here rather than inside
    # `run_ingestion`: the lab page calls that function directly, and a paused
    # pipeline must still allow a manual test run.
    if not await _production_allowed():
        logger.info("Production mode is off: skipping this collection tick")
        return {"skipped": True, "reason": "pipeline disabled"}

    report = await run_ingestion(source_ids=source_ids)
    return report.to_dict()


async def _production_allowed() -> bool:
    """Whether the production switch permits this cron tick to do work.

    Fails open: an unreachable database must not silently pause the pipeline,
    because a paused pipeline still reports healthy and the missing news is only
    noticed hours later.
    """
    try:
        async with AsyncSessionLocal() as session:
            return await pipeline_enabled(session)
    except Exception:
        logger.warning(
            "Could not read the production switch; assuming enabled", exc_info=True
        )
        return True


async def startup(ctx: dict[str, Any]) -> None:
    """Warm the shared clients so the first job does not pay for their setup.

    Chromium is launched eagerly because a cold launch inside the first scrape
    would usually exceed the navigation timeout. A launch failure is logged, not
    raised: a Telegram-only deployment must still start.
    """
    from app.api.logs_sse import install_log_handler  # noqa: PLC0415

    logger.info("Ingestion worker starting (env=%s)", settings.app_env)
    install_log_handler()
    ctx["redis_client"] = get_redis()
    get_ai_client()
    get_telegram_ingestor()
    try:
        await get_browser_pool().start()
    except ScraperError:
        logger.warning(
            "Chromium could not be launched at startup; website sources will "
            "retry on demand",
            exc_info=True,
        )


async def shutdown(ctx: dict[str, Any]) -> None:
    """Release Chromium, the HTTP clients and the Redis pool."""
    client = ctx.pop("redis_client", None)
    if client is not None:
        await client.aclose()
    await close_browser_pool()
    await close_telegram_ingestor()
    await close_ai_client()
    await close_flux_client()
    await close_embedding_service()
    await close_redis_pool()
    logger.info("Ingestion worker stopped")


class WorkerSettings:
    """ARQ worker definition (``arq app.workers.ingestion_worker.WorkerSettings``).

    The cron job runs at the top of every fifth minute. Overlap is harmless
    because :func:`run_ingestion` fences itself with the run lock, so a tick that
    fires while the previous one is still collecting returns
    ``lock_skipped=True`` instead of double-ingesting. ``max_tries=1``: a failed
    pass is not retried, since the next tick is a retry by construction and every
    source already reports its own failure.
    """

    redis_settings: Final[RedisSettings] = RedisSettings.from_dsn(
        str(settings.redis_url)
    )
    #: A dedicated queue. The three workers run as separate processes against one
    #: Redis, and on ARQ's shared default queue whichever worker polls first claims
    #: the job regardless of which functions it registered — so a publisher tick
    #: taken by this worker dies as "function not found" and that pass is lost.
    queue_name: Final[str] = INGESTION_QUEUE_NAME
    #: Registered by name so a caller can enqueue a pass onto this worker's queue.
    #: The dashboard trigger does not use it: it runs the pass inline instead.
    functions: Final[Sequence[WorkerCoroutine]] = (ingestion_task,)
    cron_jobs: Final[Sequence[CronJob]] = (
        cron(
            ingestion_task,
            name=INGESTION_TASK_NAME,
            minute=set(INGESTION_CRON_MINUTES),
            second=0,
            run_at_startup=False,
            max_tries=1,
            timeout=settings.ingest_lock_ttl,
        ),
    )
    on_startup: Final[StartupShutdown] = startup
    on_shutdown: Final[StartupShutdown] = shutdown
    #: One job at a time: Chromium plus the dedup ladder are the memory-heavy
    #: parts of the pipeline, and the 4 GB target VPS cannot host two passes.
    max_jobs: Final[int] = 1
    job_timeout: Final[int] = settings.ingest_lock_ttl
    keep_result: Final[int] = 3600
    #: Refresh the `<queue>:health-check` key every 30s. ARQ's 3600s default would
    #: leave a stale key valid for an hour, so `arq --check` — and therefore the
    #: container healthcheck — would keep reporting a wedged worker as healthy.
    #: The key's TTL is this interval plus one second, so it expires promptly once
    #: the main loop stops turning.
    health_check_interval: Final[int] = 30


__all__ = [
    "INGESTION_CRON_MINUTES",
    "INGESTION_QUEUE_NAME",
    "INGESTION_TASK_NAME",
    "RUN_LOCK_KEY",
    "SOURCE_LOCK_PREFIX",
    "URL_HASH_LENGTH",
    "URL_ID_PREFIX",
    "IngestionRunReport",
    "SourceIngestReport",
    "WorkerSettings",
    "canonical_url",
    "ingest_single_source",
    "ingestion_task",
    "run_ingestion",
    "shutdown",
    "source_lock_key",
    "startup",
    "web_external_post_id",
]
