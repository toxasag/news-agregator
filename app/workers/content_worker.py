"""Content & media orchestrator: turns a clustered news event into an article.

This is the second of the two modules in the project that write to the database
(the first is :mod:`app.workers.ingestion_worker`), and it is the only place where
the seven Package 3 services are composed. Every service returns value objects and
never commits, so the transaction boundary, the ordering and the "what counts as a
failure" policy all live here.

The pipeline, per docs/01_DATA_PIPELINE.md step 4-6:

1. Claim a cluster that has no article yet.
2. Generate the draft (:mod:`app.services.content_generator`) in the cluster's
   editorial category.
3. Review and repair it (:mod:`app.services.content_fixer`), which also re-runs
   the slop blocklist.
4. Embed the final text (:mod:`app.services.embedding_service`) into
   ``articles.final_embedding``.
5. Process media: scan each source image (:mod:`app.services.watermark_detector`),
   crop what is salvageable, and generate a cover
   (:mod:`app.services.flux_generator`) when nothing usable survives. Upload the
   result to WordPress (:mod:`app.services.wp_media_service`).
6. Host any attached video on VK (:mod:`app.services.vk_video_service`) and inject
   the player into the body.
7. Persist the ``Article`` at ``MEDIA_READY``.

Decisions that are easy to "simplify" into a bug:

* **One transaction per cluster, one lock per cluster.** A batch of ten clusters
  commits ten times, so a failure on the seventh cannot roll back the six articles
  already written. The per-cluster lock stops the dashboard's manual trigger from
  racing the scheduled run on the same cluster and paying twice for the same
  generation.

* **Idempotency is a uniqueness check, not a flag.** A cluster that already has an
  article is skipped by querying for it, because generation is the single most
  expensive step in the whole system and a retried job must never pay for it
  twice. ``articles.url_slug`` carries a unique index as the database-level
  backstop.

* **Text failures abort the article; media failures do not.** A cluster whose text
  cannot be generated has nothing to publish, so it is recorded as failed. A
  cluster whose *cover* could not be produced still has a publishable article —
  WordPress renders a post without a featured image — so media errors are recorded
  on the row and the article is saved anyway. Losing an article because a CDN was
  briefly down would be the wrong trade.

* **Status stops at ``MEDIA_READY``.** ``ArticleStatus`` has no
  ``READY_TO_PUBLISH`` member; per docs/01 the state after media processing is
  ``MEDIA_READY``, and the next hop is Package 4's WordPress publisher. Adding a
  synonym would put two names for one state into the enum and the migration.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.typing import StartupShutdown, WorkerCoroutine
from redis.asyncio import Redis
from sqlalchemy import or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, undefer

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.article import Article
from app.models.cluster import NewsCluster
from app.models.enums import ArticleStatus, MediaStrategy
from app.models.raw_news import RawNews
from app.redis_client import close_redis_pool, get_redis
from app.services.ai_client import AIClientError, close_ai_client, get_ai_client
from app.services.article_journals import record_article_journal
from app.services.content_fixer import review_and_fix_article
from app.services.content_generator import (
    ContentGenerationError,
    generate_article,
)
from app.services.embedding_service import (
    EmbeddingError,
    close_embedding_service,
    get_text_embedding,
)
from app.services.flux_generator import (
    FluxError,
    close_flux_client,
)
from app.services.image_generator import ImageGenerationError, generate_cover_image
from app.services.model_pool import ModelPoolExhaustedError, run_with_fallback
from app.services.model_routing import resolve_chain
from app.services.moderator import ModerationError, moderate_with_fallback
from app.services.pipeline_control import pipeline_enabled
from app.services.research import conduct_cluster_research
from app.services.usage_log import record_usage
from app.services.vk_video_service import (
    VKError,
    close_vk_video_service,
    upload_article_video,
)
from app.services.watermark_detector import (
    crop_watermarks_async,
    detect_watermarks,
)
from app.services.wp_media_service import (
    WPMediaError,
    close_wp_media_service,
    slugify,
    upload_article_image,
)
from app.workers.redis_lock import acquire_lock

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "CONTENT_CRON_MINUTES",
    "CONTENT_QUEUE_NAME",
    "CONTENT_TASK_NAME",
    "ENRICH_CRON_MINUTES",
    "MAX_CLUSTERS_PER_RUN",
    "ArticleOutcome",
    "ContentRunReport",
    "WorkerSettings",
    "cluster_lock_key",
    "content_task",
    "enrich_task",
    "process_cluster",
    "run_content_pipeline",
]

CONTENT_TASK_NAME: Final[str] = "content_task"
ENRICH_TASK_NAME: Final[str] = "enrich_task"
CONTENT_RUN_LOCK_KEY: Final[str] = "lock:content:run"
ENRICH_RUN_LOCK_KEY: Final[str] = "lock:content:enrich"

#: Dedicated ARQ queue for this worker. Each worker owns one, because ARQ's
#: default queue is shared: any worker polling it may claim a job belonging to
#: another, then fail it as "function not found".
CONTENT_QUEUE_NAME: Final[str] = "arq:queue:content"

#: Offset from the ingestion cron (which runs on the fifth minute) so generation
#: reads a settled backlog instead of racing the dedup writes that produce it.
CONTENT_CRON_MINUTES: Final[frozenset[int]] = frozenset({2, 12, 22, 32, 42, 52})

#: Enrichment runs 5 minutes after ingestion and 5 minutes before generation,
#: so each cluster has a research pass before the writer model is called.
#: Fires at :07, :17, :27, :37, :47, :57.
ENRICH_CRON_MINUTES: Final[frozenset[int]] = frozenset({7, 17, 27, 37, 47, 57})

#: Process all new unread clusters collected in the pass (up to 100 per tick).
MAX_CLUSTERS_PER_RUN: Final[int] = 100
CONTENT_JOB_TIMEOUT: Final[int] = max(3600, settings.ingest_lock_ttl)

#: Media per article. A Telegram album can carry ten photos; scanning them all
#: would cost OCR time for images the theme will never render.
MAX_SOURCE_IMAGES: Final[int] = 4

#: Timeout for fetching one source image or video.
MEDIA_FETCH_TIMEOUT_SECONDS: Final[float] = 60.0

#: Ceilings on fetched source media, mirroring the service-level limits.
MAX_SOURCE_IMAGE_BYTES: Final[int] = 32 * 1024 * 1024
MAX_SOURCE_VIDEO_BYTES: Final[int] = 256 * 1024 * 1024

#: Slug suffix entropy. Two clusters about the same event produce near-identical
#: titles, and ``url_slug`` is unique, so a short discriminator keeps the second
#: article from colliding.
_SLUG_SUFFIX_CHARS: Final[int] = 8

#: Where the video player is injected: after the first closing paragraph, so the
#: reader sees the lead before the embed. Falls back to appending.
_FIRST_PARAGRAPH_END_RE: Final[re.Pattern[str]] = re.compile(r"</p\s*>", re.IGNORECASE)


def cluster_lock_key(cluster_id: uuid.UUID) -> str:
    """Redis key fencing one cluster's generation."""
    return f"lock:content:cluster:{cluster_id}"


@dataclass(slots=True)
class ArticleOutcome:
    """What happened to one cluster."""

    cluster_id: uuid.UUID
    category: str
    article_id: int | None = None
    status: str = "skipped"
    slug: str = ""
    generation_attempts: int = 0
    qa_fixed: bool = False
    embedded: bool = False
    image_source: str = "none"
    wp_media_id: int | None = None
    wp_image_ids: list[int] = field(default_factory=list)
    video_uploaded: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def warn(self, message: str) -> None:
        """Record a non-fatal problem, visible in the run report."""
        logger.warning("Cluster %s: %s", self.cluster_id, message)
        self.warnings.append(message)

    def to_dict(self) -> dict[str, Any]:
        """Render the outcome as ARQ-serialisable JSON."""
        return {
            "cluster_id": str(self.cluster_id),
            "category": self.category,
            "article_id": self.article_id,
            "status": self.status,
            "slug": self.slug,
            "generation_attempts": self.generation_attempts,
            "qa_fixed": self.qa_fixed,
            "embedded": self.embedded,
            "image_source": self.image_source,
            "wp_media_id": self.wp_media_id,
            "video_uploaded": self.video_uploaded,
            "warnings": list(self.warnings),
            "error": self.error,
        }


@dataclass(slots=True)
class ContentRunReport:
    """Aggregate result of one pipeline pass."""

    outcomes: list[ArticleOutcome] = field(default_factory=list)
    lock_skipped: bool = False
    duration_ms: int = 0

    @property
    def created(self) -> int:
        return sum(1 for item in self.outcomes if item.status == "created")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.outcomes if item.status == "failed")

    def to_dict(self) -> dict[str, Any]:
        """Render the report as ARQ-serialisable JSON."""
        return {
            "clusters": len(self.outcomes),
            "created": self.created,
            "failed": self.failed,
            "lock_skipped": self.lock_skipped,
            "duration_ms": self.duration_ms,
            "outcomes": [item.to_dict() for item in self.outcomes],
        }


def _build_cluster_payload(
    cluster: NewsCluster, posts: Sequence[RawNews]
) -> dict[str, Any]:
    """Assemble the prompt input for one cluster.

    ``enrichment_data`` is read defensively: a cluster the enrichment stage has not
    reached yet carries an empty map, and a missing key must degrade the prompt
    rather than raise.
    """
    enrichment = cluster.enrichment_data or {}
    raw_facts = enrichment.get("facts")
    facts = (
        [str(item) for item in raw_facts if isinstance(item, str)]
        if isinstance(raw_facts, list)
        else []
    )
    sources = [
        {"text": post.raw_text, "url": post.post_url or ""}
        for post in posts
        if post.raw_text
    ]
    # The longest post is the best single summary of the event: a channel that
    # wrote three sentences carries more than one that wrote a headline.
    body = max((post.raw_text for post in posts), key=len, default="")
    return {
        "title": cluster.title,
        "text": body,
        "facts": facts,
        "sources": sources,
    }


def _sources_text(posts: Sequence[RawNews]) -> str:
    """Concatenate the source posts for the QA fact check."""
    return "\n\n".join(post.raw_text for post in posts if post.raw_text)


def _unique_slug(title: str, cluster_id: uuid.UUID) -> str:
    """Build a unique, transliterated slug for one article.

    The cluster id's first bytes are appended because ``url_slug`` is unique and
    two clusters covering the same event produce nearly identical titles; without
    a discriminator the second insert would violate the index and lose an article
    that is otherwise fine.
    """
    base = slugify(title, max_chars=200) or "material"
    return f"{base}-{cluster_id.hex[:_SLUG_SUFFIX_CHARS]}"


def _iter_media(posts: Sequence[RawNews], kind: str) -> list[dict[str, Any]]:
    """Collect media descriptors of one type across a cluster's posts."""
    found: list[dict[str, Any]] = []
    for post in posts:
        for entry in post.media_data or ():
            if not isinstance(entry, Mapping):
                continue
            entry_type = str(entry.get("type") or "").lower()
            if kind == "video":
                if entry_type not in (
                    "video",
                    "videofile",
                    "videonote",
                    "roundvideo",
                    "animation",
                    "gif",
                ):
                    continue
            elif kind == "photo":
                if entry_type not in ("photo", "image", "picture"):
                    continue
            elif entry_type != kind:
                continue
            item = dict(entry)
            if not item.get("post_url") and post.post_url:
                item["post_url"] = post.post_url
            item["source_id"] = post.source_id
            item["post_id"] = post.id
            item["external_post_id"] = post.external_post_id
            found.append(item)
    return found


def _extract_post_msg_id(post: RawNews) -> int | None:
    """Extract telegram message id from post media metadata or external id."""
    for item in (post.media_data or ()):
        if item.get("original_post_id"):
            try:
                return int(item["original_post_id"])
            except (ValueError, TypeError):
                pass
    try:
        return int(post.external_post_id)
    except (ValueError, TypeError):
        return None


def _media_url(entry: Mapping[str, Any]) -> str:
    """Extract a fetchable URL from a media descriptor.

    Website media store an absolute URL in ``media_id`` (see
    ``_Candidate.from_article`` in the ingestion worker); Telegram media store a
    file id there, which this worker cannot fetch without the bridge, so only
    ``http`` entries are usable here.
    """
    candidate = str(entry.get("url") or entry.get("media_id") or "")
    return candidate if candidate.startswith(("http://", "https://")) else ""


_TG_CDN_PHOTO_RE: Final[re.Pattern[str]] = re.compile(
    r'background-image:url\([\'"](https?://[^\'"]+)[\'"]\)'
)
_TG_OG_IMAGE_RE: Final[re.Pattern[str]] = re.compile(
    r'<meta\s+property=["\']og:image["\']\s+content=["\'](https?://[^"\']*telesco\.pe/file/[^"\']+)["\']',
    re.IGNORECASE,
)


async def _resolve_tg_embed_photo(client: httpx.AsyncClient, post_url: str) -> str:
    """Extract direct CDN photo URL from a public Telegram post embed or og:image."""
    if not post_url or not post_url.startswith(("http://t.me/", "https://t.me/")):
        return ""
    clean_url = post_url.rstrip("/")
    embed_url = clean_url + "?embed=1"
    try:
        resp = await client.get(embed_url, timeout=8.0, follow_redirects=True)
        if resp.status_code == 200:
            match = _TG_CDN_PHOTO_RE.search(resp.text)
            if match:
                cdn_url = match.group(1)
                logger.info("Resolved Telegram photo CDN: %s", cdn_url[:80])
                return cdn_url
        og_resp = await client.get(clean_url, timeout=8.0, follow_redirects=True)
        if og_resp.status_code == 200:
            og_match = _TG_OG_IMAGE_RE.search(og_resp.text)
            if og_match:
                cdn_url = og_match.group(1)
                logger.info("Resolved Telegram og:image CDN: %s", cdn_url[:80])
                return cdn_url
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to resolve TG embed photo from %s: %s", post_url, exc)
    return ""


async def _fetch_bytes(
    client: httpx.AsyncClient, url: str, *, limit: int
) -> tuple[bytes, str]:
    """Download one media file, enforcing a size ceiling.

    Returns ``(b"", "")`` for every failure rather than raising: a source image
    that 404s must not abort an article whose text is already generated and paid
    for.
    """
    try:
        response = await client.get(url)
    except httpx.HTTPError:
        logger.info("Could not fetch media %s", url, exc_info=True)
        return b"", ""
    if response.status_code >= httpx.codes.BAD_REQUEST:
        logger.info("Media %s returned HTTP %d", url, response.status_code)
        return b"", ""
    content = response.content
    if not content or len(content) > limit:
        logger.info(
            "Media %s is %d bytes, outside the 1..%d range", url, len(content), limit
        )
        return b"", ""
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    return content, content_type


def _inject_video(content_html: str, embed_html: str) -> str:
    """Insert the video player after the article's first paragraph.

    After the lead rather than at the top: the reader should see what the story is
    before the player, and a theme that lazy-loads the iframe benefits from it not
    being the first element.
    """
    match = _FIRST_PARAGRAPH_END_RE.search(content_html)
    if match is None:
        return f"{content_html}{embed_html}"
    cut = match.end()
    return f"{content_html[:cut]}{embed_html}{content_html[cut:]}"


@dataclass(frozen=True, slots=True)
class _CoverImage:
    """One publishable cover, with how it was obtained."""

    content: bytes
    content_type: str
    origin: str
    filename: str
    source_url: str = ""
    source_label: str = ""


async def _salvage_source_image(
    client: httpx.AsyncClient,
    entry: Mapping[str, Any],
    outcome: ArticleOutcome,
    *,
    session: AsyncSession | None = None,
) -> _CoverImage | None:
    """Fetch one source image and return it if it is publishable."""
    url = _media_url(entry)
    if not url and entry.get("post_url"):
        url = await _resolve_tg_embed_photo(client, str(entry["post_url"]))

    content: bytes = b""
    content_type: str = "image/jpeg"
    filename: str = "cover.jpg"

    msg_id = entry.get("original_post_id") or entry.get("external_post_id")
    source_obj = None
    if session is not None and entry.get("source_id"):
        try:
            from app.models.source import Source  # noqa: PLC0415

            source_obj = await session.get(Source, int(entry["source_id"]))
        except Exception:  # noqa: BLE001
            source_obj = None

    if url:
        content, content_type = await _fetch_bytes(
            client, url, limit=MAX_SOURCE_IMAGE_BYTES
        )
        filename = url.rsplit("/", 1)[-1].split("?")[0] or "cover.jpg"
    elif source_obj and source_obj.identifier and msg_id:
        # Direct MTProto download for Telegram channels
        try:
            from app.services.telegram_mtproto import MTProtoIngestor  # noqa: PLC0415

            ingestor = MTProtoIngestor(session)
            async with ingestor:
                content = await ingestor.download_photo(
                    source_obj.identifier,
                    int(msg_id),
                    limit_bytes=MAX_SOURCE_IMAGE_BYTES,
                )
            if content:
                filename = f"tg_{source_obj.id}_{msg_id}.jpg"
                logger.info(
                    "Directly downloaded Telegram photo via MTProto (%d bytes)",
                    len(content),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("MTProto direct photo salvage failed: %s", exc)

    if not content:
        return None

    # Resolve accurate source URL and label for photo attribution
    from app.services.lab_pipeline import resolve_raw_post_attribution  # noqa: PLC0415

    source_url, source_label = resolve_raw_post_attribution(
        post=None, source=source_obj
    )
    if entry.get("post_url"):
        source_url = str(entry["post_url"]).strip()
    elif source_obj and msg_id:
        tg_h = source_obj.identifier.lstrip("@").strip()
        if not tg_h.startswith("-100") and not tg_h.lstrip("-").isdigit():
            source_url = f"https://t.me/{tg_h}/{msg_id}"
        elif source_url and "t.me/" in source_url:
            source_url = f"{source_url.rstrip('/')}/{msg_id}"

    allow_watermarked = True
    if session is not None:
        try:
            from app.services.pipeline_control import load_control  # noqa: PLC0415

            control = await load_control(session)
            allow_watermarked = control.allow_watermarked_source_media
        except Exception:  # noqa: BLE001
            allow_watermarked = True

    return await _judge_source_image(
        content,
        content_type=content_type,
        filename=filename,
        outcome=outcome,
        allow_watermarked=allow_watermarked,
        source_url=source_url,
        source_label=source_label,
    )


async def _judge_source_image(  # noqa: PLR0911
    content: bytes,
    *,
    content_type: str,
    filename: str,
    outcome: ArticleOutcome,
    allow_watermarked: bool = True,
    source_url: str = "",
    source_label: str = "",
) -> _CoverImage | None:
    """Apply watermark and banner verdict to fetched image, cropping if needed."""
    if not content or len(content) < 100:
        return None

    # Check for video files disguised as images (e.g. mp4, webm, avi, mkv)
    if b"ftyp" in content[:32] or content.startswith((b"\x00\x00\x00", b"\x1aE\xdf\xa3")):
        outcome.warn("source media is a video/binary container, not a static image")
        return None

    # Run image quality inspection (resolution, blur, aspect ratio, badges, watermarks)
    from app.services.image_cleaner import inspect_image_quality  # noqa: PLC0415

    is_valid, reason, clean_bytes = inspect_image_quality(
        content,
        allow_watermarked=allow_watermarked,
    )
    if not is_valid:
        outcome.warn(
            f"source image rejected by quality inspection ({reason}); "
            "falling back to generation"
        )
        return None

    content = clean_bytes

    try:
        verdict = await detect_watermarks(content)
    except Exception as exc:  # noqa: BLE001
        outcome.warn(
            f"source image could not be analysed: {exc}; "
            "falling back to generation"
        )
        return None

    # 1. Monochrome banners, text-heavy cards, and channel splash logos must NEVER be used
    if verdict.get("is_banner") or verdict.get("is_text_card"):
        outcome.warn("source image is a banner or text card; falling back to generation")
        return None

    if verdict.get("critical_text"):
        crit = ", ".join(verdict["critical_text"][:2])
        outcome.warn(f"channel branding detected ({crit}); falling back to generation")
        return None

    if not verdict["has_watermark"]:
        return _CoverImage(
            content=content,
            content_type=content_type or "image/jpeg",
            origin="source",
            filename=filename,
            source_url=source_url,
            source_label=source_label,
        )

    if verdict["is_croppable"]:
        try:
            cropped = await crop_watermarks_async(content)
            return _CoverImage(
                content=cropped,
                content_type="image/jpeg",
                origin="cropped",
                filename=filename,
                source_url=source_url,
                source_label=source_label,
            )
        except Exception as exc:  # noqa: BLE001
            outcome.warn(f"watermark crop failed: {exc}")

    if allow_watermarked:
        return _CoverImage(
            content=content,
            content_type=content_type or "image/jpeg",
            origin="source",
            filename=filename,
            source_url=source_url,
            source_label=source_label,
        )

    if verdict.get("needs_ai_generation"):
        outcome.warn("watermark is too intrusive to keep; falling back to generation")
        return None

    return _CoverImage(
        content=content,
        content_type=content_type or "image/jpeg",
        origin="source_fallback",
        filename=filename,
        source_url=source_url,
        source_label=source_label,
    )


async def _generate_cover(
    prompt: str, outcome: ArticleOutcome, *, session: AsyncSession | None = None
) -> _CoverImage | None:
    """Generate an editorial cover using configured AI image routing."""
    if not prompt:
        outcome.warn("no image prompt was generated, so no cover could be created")
        return None
    try:
        if session is not None:
            content = await generate_cover_image(session, prompt)
        else:
            async with AsyncSessionLocal() as db_session:
                content = await generate_cover_image(db_session, prompt)
    except (ImageGenerationError, FluxError, ValueError) as exc:
        outcome.warn(f"cover generation failed: {exc}")
        return None

    if content is None:
        return None

    return _CoverImage(
        content=content,
        content_type="image/jpeg",
        origin="generated",
        filename="cover.jpg",
        source_url="https://svodka.press",
        source_label="СводкаПресс (нейросеть)",
    )


async def _salvage_enrichment_images(
    client: httpx.AsyncClient,
    enrichment_data: Mapping[str, Any] | None,
    *,
    limit: int = MAX_SOURCE_IMAGES,
) -> list[_CoverImage]:
    """Use verified clean web images from enrichment_data when available."""
    if not enrichment_data or limit <= 0:
        return []
    candidate_items: list[dict[str, Any]] = []
    sel = enrichment_data.get("selected_cover_url")
    if isinstance(sel, str) and sel.startswith(("http://", "https://")):
        candidate_items.append({"url": sel})
    web_imgs = enrichment_data.get("web_images")
    if isinstance(web_imgs, list):
        for item in web_imgs:
            if isinstance(item, Mapping) and (
                item.get("is_ai_approved") is True or item.get("relevant") is True
            ):
                url = item.get("url")
                if (
                    isinstance(url, str)
                    and url.startswith(("http://", "https://"))
                    and not any(c.get("url") == url for c in candidate_items)
                ):
                    candidate_items.append(dict(item))

    salvaged: list[_CoverImage] = []
    seen_urls: set[str] = set()

    for c_item in candidate_items:
        if len(salvaged) >= limit:
            break
        url = str(c_item.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        content, content_type = await _fetch_bytes(
            client, url, limit=MAX_SOURCE_IMAGE_BYTES
        )
        if content:
            from app.services.image_cleaner import inspect_image_quality  # noqa: PLC0415

            is_valid, reason, clean_bytes = inspect_image_quality(
                content, url=url, allow_watermarked=False
            )
            if is_valid:
                src_url = str(c_item.get("source_url") or "").strip()
                src_label = str(c_item.get("domain") or c_item.get("title") or "").strip()
                if not src_url:
                    ws_list = enrichment_data.get("web_sources")
                    if isinstance(ws_list, list):
                        for ws in ws_list:
                            if isinstance(ws, Mapping):
                                domain_val = str(ws.get("domain") or "").lower()
                                if src_label and src_label.lower() in domain_val:
                                    src_url = str(ws.get("url") or "")
                                    break
                                elif not src_url and ws.get("url"):
                                    src_url = str(ws.get("url") or "")
                                    title_fallback = str(
                                        ws.get("domain") or ws.get("title") or ""
                                    )
                                    src_label = src_label or title_fallback
                if not src_label:
                    src_label = "Первоисточник"
                salvaged.append(
                    _CoverImage(
                        content=clean_bytes,
                        content_type=content_type or "image/jpeg",
                        origin="source",
                        filename=url.rsplit("/", 1)[-1].split("?")[0] or "cover.jpg",
                        source_url=src_url,
                        source_label=src_label,
                    )
                )
            else:
                logger.info("Enrichment candidate %s rejected: %s", url, reason)
    return salvaged


# ruff: noqa: PLR0912, PLR0915
async def _process_images(
    client: httpx.AsyncClient,
    draft: Mapping[str, Any],
    posts: Sequence[RawNews],
    outcome: ArticleOutcome,
    *,
    strategy: MediaStrategy,
    session: AsyncSession | None = None,
    enrichment_data: Mapping[str, Any] | None = None,
) -> None:
    """Obtain a cover and upload it to WordPress.

    Source images are tried first and generation is the fallback, because a real
    photograph of the event is worth more than a synthesised illustration of it.
    ``MediaStrategy.ALWAYS_REGENERATE`` skips straight to generation for sources
    whose images are known to be unusable, and ``TEXT_ONLY`` skips media entirely.

    Records the result on ``outcome``; never raises. An article without a cover is
    still publishable, so a media failure must not discard generated text.
    """
    if strategy is MediaStrategy.TEXT_ONLY:
        outcome.image_source = "skipped"
        return

    salvaged_images: list[_CoverImage] = []
    seen_hashes: set[tuple[str, str]] = set()

    if strategy is not MediaStrategy.ALWAYS_REGENERATE:
        # 1. Salvage clean unique photos from source media attachments
        for entry in _iter_media(posts, "photo"):
            if len(salvaged_images) >= MAX_SOURCE_IMAGES:
                break
            img = await _salvage_source_image(
                client, entry, outcome, session=session
            )
            if img is not None:
                img_key = (
                    hashlib.sha256(img.content[:8192]).hexdigest(),
                    img.filename,
                )
                if img_key not in seen_hashes:
                    seen_hashes.add(img_key)
                    salvaged_images.append(img)

        # 2. Check embed links / post URLs for additional unique photos
        if len(salvaged_images) < MAX_SOURCE_IMAGES:
            for post in posts:
                if len(salvaged_images) >= MAX_SOURCE_IMAGES:
                    break
                if not post.post_url and not post.external_post_id:
                    continue
                embed_entry = {
                    "post_url": post.post_url or "",
                    "source_id": post.source_id,
                    "external_post_id": post.external_post_id,
                }
                img = await _salvage_source_image(
                    client, embed_entry, outcome, session=session
                )
                if img is not None:
                    img_key = (
                        hashlib.sha256(img.content[:8192]).hexdigest(),
                        img.filename,
                    )
                    if img_key not in seen_hashes:
                        seen_hashes.add(img_key)
                        salvaged_images.append(img)

        # 3. Enrich with web research images if we have remaining slots
        if len(salvaged_images) < MAX_SOURCE_IMAGES:
            remaining_needed = MAX_SOURCE_IMAGES - len(salvaged_images)
            web_imgs = await _salvage_enrichment_images(
                client, enrichment_data, limit=remaining_needed
            )
            for w_img in web_imgs:
                img_key = (
                    hashlib.sha256(w_img.content[:8192]).hexdigest(),
                    w_img.filename,
                )
                if img_key not in seen_hashes:
                    seen_hashes.add(img_key)
                    salvaged_images.append(w_img)

    # 4. Final fallback: generate AI cover
    if not salvaged_images:
        fallback_prompt = str(
            draft.get("image_prompt") or draft.get("title") or ""
        )
        ai_cover = await _generate_cover(
            fallback_prompt, outcome, session=session
        )
        if ai_cover is not None:
            salvaged_images.append(ai_cover)

    if not salvaged_images:
        outcome.image_source = "none"
        return

    outcome.image_source = salvaged_images[0].origin

    # 5. Upload all salvaged images to WordPress
    uploaded_media_list = []
    for idx, img in enumerate(salvaged_images):
        try:
            suffix = f"-{idx + 1}" if idx > 0 else ""
            base_fname = img.filename
            if suffix and "." in base_fname:
                stem, ext = base_fname.rsplit(".", 1)
                fname = f"{stem}{suffix}.{ext}"
            else:
                fname = base_fname

            if img.origin == "generated":
                caption_tag = (
                    'Иллюстрация: <a href="https://svodka.press">'
                    'СводкаПресс (нейросеть)</a>'
                )
            elif img.source_url:
                lbl = img.source_label or "Первоисточник"
                caption_tag = (
                    f'Фото: <a href="{img.source_url}" target="_blank" '
                    f'rel="nofollow noopener noreferrer">{lbl}</a>'
                )
            elif img.source_label:
                caption_tag = f'Фото: {img.source_label}'
            else:
                caption_tag = ""

            media = await upload_article_image(
                img.content,
                filename=fname,
                article_title=str(draft.get("title") or ""),
                keywords=list(draft.get("seo_keywords") or ()),
                caption=caption_tag,
                content_type=img.content_type,
            )
            uploaded_media_list.append(media)
        except (WPMediaError, ValueError) as exc:
            outcome.warn(f"WordPress media upload #{idx+1} failed: {exc}")

    if not uploaded_media_list:
        return

    first_media = uploaded_media_list[0]
    outcome.wp_media_id = first_media.media_id
    outcome.wp_image_ids = [m.media_id for m in uploaded_media_list]
    logger.info(
        "Cluster %s: uploaded %d image(s), primary cover is media %d",
        outcome.cluster_id,
        len(uploaded_media_list),
        first_media.media_id,
    )

    # 6. Distribute additional unique photos into article content_html
    is_dict = isinstance(draft, dict)
    if len(uploaded_media_list) > 1 and is_dict and "content_html" in draft:
        content = draft["content_html"]
        h2_splits = re.split(r"(</h2>)", content, flags=re.IGNORECASE)
        art_title = str(draft.get("title", ""))
        if len(h2_splits) >= 3:
            new_content_parts = []
            img_idx = 1
            for part in h2_splits:
                new_content_parts.append(part)
                if part.lower() == "</h2>" and img_idx < len(uploaded_media_list):
                    sec_media = uploaded_media_list[img_idx]
                    sec_img = (
                        salvaged_images[img_idx]
                        if img_idx < len(salvaged_images)
                        else None
                    )
                    alt = sec_media.alt_text or art_title
                    caption_html = ""
                    if sec_img and sec_img.source_url:
                        s_lbl = sec_img.source_label or "Первоисточник"
                        caption_html = (
                            f'<figcaption class="wp-element-caption '
                            f'text-xs text-neutral-500 mt-2">'
                            f'Фото: <a href="{sec_img.source_url}" target="_blank" '
                            f'rel="nofollow noopener noreferrer" '
                            f'class="underline text-[#0071E3]">{s_lbl}</a>'
                            f'</figcaption>'
                        )
                    elif sec_img and sec_img.source_label:
                        caption_html = (
                            f'<figcaption class="wp-element-caption '
                            f'text-xs text-neutral-500 mt-2">'
                            f'Фото: {sec_img.source_label}'
                            f'</figcaption>'
                        )
                    fig_html = (
                        f'\n<figure class="article-photo my-6">'
                        f'<img src="{sec_media.source_url}" alt="{alt}" '
                        f'class="w-full rounded-xl object-cover shadow-sm" '
                        f'loading="lazy" />'
                        f'{caption_html}'
                        f'</figure>\n'
                    )
                    new_content_parts.append(fig_html)
                    img_idx += 1
            draft["content_html"] = "".join(new_content_parts)
        else:
            gallery_html = (
                '\n<div class="article-gallery grid grid-cols-1 '
                'md:grid-cols-2 gap-4 my-6">\n'
            )
            for g_idx, sec_media in enumerate(uploaded_media_list[1:], start=1):
                sec_img = (
                    salvaged_images[g_idx]
                    if g_idx < len(salvaged_images)
                    else None
                )
                alt = sec_media.alt_text or art_title
                caption_html = ""
                if sec_img and sec_img.source_url:
                    g_lbl = sec_img.source_label or "Первоисточник"
                    caption_html = (
                        f'<figcaption class="wp-element-caption '
                        f'text-xs text-neutral-500 mt-1">'
                        f'Фото: <a href="{sec_img.source_url}" target="_blank" '
                        f'rel="nofollow noopener noreferrer" '
                        f'class="underline text-[#0071E3]">{g_lbl}</a>'
                        f'</figcaption>'
                    )
                gallery_html += (
                    f'<figure class="overflow-hidden rounded-xl '
                    f'border border-slate-200 p-1">'
                    f'<img src="{sec_media.source_url}" alt="{alt}" '
                    f'class="w-full h-auto object-cover rounded-lg" loading="lazy" />'
                    f'{caption_html}'
                    f'</figure>\n'
                )
            gallery_html += '</div>\n'
            draft["content_html"] = content + gallery_html


async def _resolve_telegram_video_mtproto(
    session: AsyncSession,
    posts: Sequence[RawNews],
    videos: Sequence[Mapping[str, Any]],
    cache_dir: Any,
) -> tuple[bytes, str]:
    """Download one Telegram video on demand via MTProto bridge."""
    from app.models.source import Source  # noqa: PLC0415
    from app.services.telegram_mtproto import (  # noqa: PLC0415
        MTProtoIngestor,
    )

    for p in posts:
        msg_id = _extract_post_msg_id(p)
        source_id = p.source_id or (videos[0].get("source_id") if videos else None)
        if not (source_id and msg_id):
            continue
        try:
            source = await session.get(Source, int(source_id))
            if not (source and (source.identifier or source.username)):
                continue
            channel = source.identifier or (
                f"@{source.username}" if source.username else ""
            )
            if not channel:
                continue
            logger.info(
                "Downloading Telegram video via MTProto from %s (msg %s)",
                channel,
                msg_id,
            )
            ingestor = MTProtoIngestor(session)
            try:
                await ingestor.start()
                content = await ingestor.download_video(
                    channel, msg_id, limit_bytes=MAX_SOURCE_VIDEO_BYTES
                )
            finally:
                with suppress(Exception):
                    await ingestor.close()

            if content:
                cache_dir.mkdir(parents=True, exist_ok=True)
                with suppress(OSError):
                    (cache_dir / f"{p.id}.mp4").write_bytes(content)
                return content, f"video_{p.id}.mp4"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MTProto video download failed for post %s (msg %s): %s",
                p.id,
                msg_id,
                exc,
            )
    return b"", "video.mp4"


async def _process_video(
    client: httpx.AsyncClient,
    draft: dict[str, Any],
    posts: Sequence[RawNews],
    outcome: ArticleOutcome,
    *,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """Host the first attached video on VK and inject its player.

    Supports direct HTTP/HTTPS URLs (web sources), cached local video files,
    and on-demand MTProto downloads from Telegram channels via Telethon.
    """
    videos = _iter_media(posts, "video")
    if not videos:
        return []

    content: bytes = b""
    filename: str = "video.mp4"
    url = _media_url(videos[0])

    # 1. Fetch via direct HTTP URL if available (web sources)
    if url:
        content, _ = await _fetch_bytes(client, url, limit=MAX_SOURCE_VIDEO_BYTES)
        filename = url.rsplit("/", 1)[-1].split("?")[0] or "video.mp4"

    # 2. Check local disk cache for downloaded Telegram video
    cache_dir = settings.media_storage_path / "telegram"
    if not content and posts:
        for p in posts:
            cache_file = cache_dir / f"{p.id}.mp4"
            if cache_file.exists() and cache_file.stat().st_size > 0:
                with suppress(OSError):
                    content = cache_file.read_bytes()
                    filename = f"video_{p.id}.mp4"
                    break

    # 3. If still not found and session is available, download on-demand via MTProto
    if not content and session is not None and posts:
        content, filename = await _resolve_telegram_video_mtproto(
            session, posts, videos, cache_dir
        )

    if not content:
        if not url:
            outcome.warn(
                "attached video is not fetchable: media is unavailable or size exceeded"
            )
        else:
            outcome.warn("attached video is not fetchable by URL: download failed")
        return []

    title = str(draft.get("title") or "Видео")
    try:
        uploaded = await upload_article_video(
            content,
            name=title,
            description=str(draft.get("seo_description") or ""),
            filename=filename,
            session=session,
        )
    except (VKError, Exception) as exc:  # noqa: BLE001
        outcome.warn(f"VK video upload failed: {exc}")
        return []

    draft["content_html"] = _inject_video(
        str(draft.get("content_html") or ""), uploaded.embed_html
    )
    outcome.video_uploaded = True
    logger.info(
        "Cluster %s: hosted a video on VK (owner=%d, video=%d)",
        outcome.cluster_id,
        uploaded.owner_id,
        uploaded.video_id,
    )
    return [uploaded.to_link()]


async def _load_pending_clusters(
    session: AsyncSession, cluster_ids: Sequence[uuid.UUID] | None, limit: int
) -> list[NewsCluster]:
    """Load clusters that have no article yet, newest first."""
    already_written = (
        select(Article.id).where(Article.cluster_id == NewsCluster.id).exists()
    )
    query = (
        select(NewsCluster)
        .where(~already_written)
        .where(NewsCluster.is_published.is_(False))
        .order_by(NewsCluster.created_at.desc())
        .limit(limit)
    )
    if cluster_ids:
        query = query.where(NewsCluster.id.in_(list(cluster_ids)))
    result = await session.execute(query)
    return list(result.scalars().all())


async def _load_cluster_posts(
    session: AsyncSession, cluster_id: uuid.UUID
) -> list[RawNews]:
    """Load the source posts of one cluster, oldest first.

    Loads the primary post(s) directly linked to the cluster, PLUS any
    subsequent posts from other channels that matched this cluster during
    deduplication (recorded in entities['dedup_matched_cluster_id']).
    """
    cid_str = str(cluster_id)
    result = await session.execute(
        select(RawNews)
        .options(selectinload(RawNews.source))
        .where(
            or_(
                RawNews.cluster_id == cluster_id,
                RawNews.entities.contains({"dedup_matched_cluster_id": cid_str}),
            )
        )
        .order_by(RawNews.created_at)
    )
    return list(result.scalars().all())


def _media_strategy(posts: Sequence[RawNews]) -> MediaStrategy:
    """Resolve the strictest media strategy across a cluster's sources.

    A cluster aggregates posts from several channels, and their strategies can
    disagree. The strictest wins: if any source is marked ``TEXT_ONLY`` its imagery
    must not be republished, and honouring the most permissive setting instead
    would leak exactly the media an operator disabled.
    """
    order = {
        MediaStrategy.TEXT_ONLY: 3,
        MediaStrategy.ALWAYS_REGENERATE: 2,
        MediaStrategy.OCR_CHECK: 1,
        MediaStrategy.ORIGINAL: 0,
    }
    strictest = MediaStrategy.OCR_CHECK
    for post in posts:
        source = post.source
        if source is None:
            continue
        candidate = source.media_strategy
        if order.get(candidate, 0) > order.get(strictest, 0):
            strictest = candidate
    return strictest


async def _mark_cluster_raw_news_processed(
    session: AsyncSession, cluster_id: uuid.UUID
) -> None:
    """Mark all raw news items affiliated with the cluster as processed."""
    try:
        await session.execute(
            update(RawNews)
            .where(
                or_(
                    RawNews.cluster_id == cluster_id,
                    RawNews.entities.contains(
                        {"dedup_matched_cluster_id": str(cluster_id)}
                    ),
                ),
                RawNews.is_processed.is_(False),
            )
            .values(is_processed=True)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to mark raw news for cluster %s as processed: %s",
            cluster_id,
            exc,
        )


async def _moderate(
    session: AsyncSession,
    cluster: NewsCluster,
    sources_text: str,
    outcome: ArticleOutcome,
) -> str | None:
    """Classify the cluster, returning its risk level or ``None`` to stop.

    Runs *before* generation, because dropping advertising afterwards means paying
    for a writer pass, an SEO pass, a cover and a WordPress upload on a post that
    was never publishable.

    ``None`` means "do not write this cluster", for two different reasons the
    caller does not need to distinguish: the moderator was unavailable (leave it
    for the next tick — publishing into the wrong section is worse than publishing
    an hour later), or the verdict was spam (drop it for good).
    """
    try:
        verdict, route = await moderate_with_fallback(
            sources_text, await resolve_chain(session, "arbiter")
        )
        if route.used_fallback:
            outcome.warn(
                f"moderation used a fallback model: {route.model}"
            )
    except (ModerationError, ModelPoolExhaustedError) as exc:
        outcome.warn(f"moderation unavailable: {exc}")
        logger.warning("Cluster %s: moderation failed, deferring", cluster.id)
        return None

    # No commit here: `process_cluster` owns the transaction boundary, and an
    # extra commit mid-flow would split one logical write into two.
    cluster.category = verdict.category
    cluster.risk_level = verdict.risk_level
    if verdict.overridden:
        outcome.warn(verdict.override_note)

    if verdict.blocks_generation:
        # Marked published so the cluster does not return on every tick. Committed
        # here because this path returns early and would otherwise roll back.
        cluster.is_published = True
        await _mark_cluster_raw_news_processed(session, cluster.id)
        await session.commit()
        outcome.warn(f"dropped by moderation: {verdict.reason}")
        logger.info("Cluster %s dropped as %s", cluster.id, verdict.risk_level.value)
        return None
    return verdict.risk_level.value


async def _record_journal(
    session: AsyncSession,
    *,
    article: Article,
    cluster: NewsCluster,
    outcome: ArticleOutcome,
) -> None:
    """Record a production article journal without risking the saved article."""
    try:
        await record_article_journal(
            session,
            article_id=article.id,
            cluster_id=cluster.id,
            details={
                "title": article.title,
                "category": article.category,
                "seo_title": article.seo_title,
                "seo_description": article.seo_description,
                "seo_keywords": list(article.seo_keywords),
                "generation_attempts": outcome.generation_attempts,
                "qa_fixed": outcome.qa_fixed,
                "embedded": outcome.embedded,
                "image_source": outcome.image_source,
                "wp_media_id": outcome.wp_media_id,
                "video_uploaded": outcome.video_uploaded,
            },
            warnings=outcome.warnings,
        )
    except (OSError, SQLAlchemyError, ValueError) as exc:
        outcome.warn(f"journal recording failed: {type(exc).__name__}: {exc}")
        await session.rollback()


def _build_article_from_reviewed(
    cluster: NewsCluster,
    reviewed: Mapping[str, Any],
    outcome: ArticleOutcome,
    *,
    video_links: Sequence[str],
    embedding: list[float] | None,
) -> Article:
    return Article(
        cluster_id=cluster.id,
        category=str(reviewed.get("category_slug") or cluster.category),
        title=str(reviewed.get("title") or cluster.title)[:512],
        content_html=str(reviewed.get("content_html") or ""),
        seo_title=str(reviewed.get("seo_title") or "")[:512] or None,
        seo_description=str(reviewed.get("seo_description") or "")[:1024] or None,
        seo_keywords=list(reviewed.get("seo_keywords") or ()),
        url_slug=_unique_slug(
            str(reviewed.get("title") or cluster.title), cluster.id
        ),
        wp_image_id=outcome.wp_media_id,
        wp_image_ids=(
            outcome.wp_image_ids
            or ([outcome.wp_media_id] if outcome.wp_media_id else [])
        ),
        video_links=list(video_links),
        final_embedding=embedding,
        status=ArticleStatus.MEDIA_READY,
        error_message="; ".join(outcome.warnings)[:2000] or None,
    )


async def _record_worker_article_usage(
    session: AsyncSession,
    *,
    cluster: NewsCluster,
    article: Article,
    outcome: ArticleOutcome,
    model: str,
    draft: Mapping[str, Any],
) -> None:
    """Record token spend, provider stability, and update outcome state."""
    outcome.article_id = article.id
    outcome.slug = article.url_slug
    outcome.status = "created"
    usage_block = draft.get("usage")
    if not isinstance(usage_block, dict):
        return
    await record_usage(
        session,
        role="writer",
        model=model,
        prompt_tokens=int(usage_block.get("prompt_tokens") or 0),
        completion_tokens=int(usage_block.get("completion_tokens") or 0),
        total_tokens=int(usage_block.get("total_tokens") or 0),
        cost_usd=usage_block.get("cost"),
        provider_name=usage_block.get("provider_name"),
        latency_ms=usage_block.get("latency_ms"),
        cluster_id=cluster.id,
        article_id=article.id,
    )


async def _enrich_and_decorate_draft(
    session: AsyncSession,
    cluster: NewsCluster,
    posts: Sequence[RawNews],
    reviewed: dict[str, Any],
) -> None:
    """Apply internal links, inline web images, Schema.org, and Telegram CTA."""
    if not isinstance(session, AsyncSession):
        # Inject parent_article backlink when decorative HTML is bypassed in unit tests
        enrichment = dict(cluster.enrichment_data or {})
        parent_art = enrichment.get("parent_article")
        if (
            isinstance(parent_art, dict)
            and parent_art.get("title")
            and parent_art.get("url_slug")
        ):
            raw_title = str(parent_art["title"])
            parent_title = raw_title.replace("<", "&lt;").replace(">", "&gt;")
            p_cat = str(parent_art.get("category") or "novosti-obshhestva")
            p_slug = str(parent_art["url_slug"])
            parent_url = f"https://svodka.press/{p_cat}/{p_slug}/"
            followup_html = (
                '<p class="svodka-followup text-sm my-4 p-3 bg-blue-50/70 border-l-4 '
                'border-[#0071E3] rounded-r">'
                '<strong>Ранее по теме:</strong> '
                f'<a href="{parent_url}" '
                f'class="text-[#0071E3] hover:underline font-medium">'
                f'{parent_title}</a>'
                '</p>'
            )
            old_html = str(reviewed.get("content_html") or "")
            reviewed["content_html"] = f"{old_html}\n{followup_html}"
        return
    try:
        from app.services.image_cleaner import inject_article_images  # noqa: PLC0415
        from app.services.lab_pipeline import (  # noqa: PLC0415
            _ensure_sources_block,
            _inject_internal_links,
        )
        from app.services.schema_org import (  # noqa: PLC0415
            extract_faq_from_html,
            generate_schema_org_json_ld,
        )

        enrichment = dict(cluster.enrichment_data or {})
        web_sources = enrichment.get("web_sources") or []
        web_images = enrichment.get("web_images") or []
        html_body = str(reviewed.get("content_html") or "")
        html_with_links, internal_links = await _inject_internal_links(
            session,
            cluster_id=cluster.id,
            html_content=html_body,
            cluster_title=str(reviewed.get("title") or cluster.title),
            cluster_category=str(reviewed.get("category_slug") or cluster.category),
        )
        body_imgs = (
            web_images[1:4]
            if isinstance(web_images, list) and len(web_images) > 1
            else []
        )
        html_with_imgs = inject_article_images(
            html_with_links, body_imgs, layout="inline"
        )
        cat_slug = str(reviewed.get("category_slug") or cluster.category)

        # Inject editorial backlink to parent article for story developments/follow-ups
        parent_art = enrichment.get("parent_article")
        if (
            isinstance(parent_art, dict)
            and parent_art.get("title")
            and parent_art.get("url_slug")
        ):
            raw_title = str(parent_art["title"])
            parent_title = raw_title.replace("<", "&lt;").replace(">", "&gt;")
            p_cat = str(parent_art.get("category") or cat_slug or "novosti-obshhestva")
            p_slug = str(parent_art["url_slug"])
            parent_url = f"https://svodka.press/{p_cat}/{p_slug}/"
            followup_html = (
                '<p class="svodka-followup text-sm my-4 p-3 bg-blue-50/70 border-l-4 '
                'border-[#0071E3] rounded-r">'
                '<strong>Ранее по теме:</strong> '
                f'<a href="{parent_url}" '
                f'class="text-[#0071E3] hover:underline font-medium">'
                f'{parent_title}</a>'
                '</p>'
            )
            # Inject right before sources block or append
            html_with_imgs = f"{html_with_imgs}\n{followup_html}"

        reviewed["content_html"] = _ensure_sources_block(
            html_with_imgs,
            web_sources if isinstance(web_sources, list) else [],
            raw_posts=posts,
        )
        faq_items = extract_faq_from_html(html_with_links)
        slug_val = _unique_slug(
            str(reviewed.get("title") or cluster.title), cluster.id
        )
        art_url = f"https://svodka.press/{cat_slug}/{slug_val}/"
        enrichment["internal_links"] = internal_links
        enrichment["faq_items"] = faq_items
        enrichment["schema_json_ld"] = generate_schema_org_json_ld(
            headline=str(reviewed.get("title") or cluster.title),
            description=str(reviewed.get("seo_description") or ""),
            url=art_url,
            published_at=datetime.now(UTC),
            keywords=list(reviewed.get("seo_keywords") or ()),
            faq_items=faq_items,
        )
        cluster.enrichment_data = enrichment
    except Exception as exc:  # noqa: BLE001
        logger.warning("Optional HTML decoration skipped for %s: %s", cluster.id, exc)


async def _prepare_studio_aligned_payload(
    session: AsyncSession,
    cluster: NewsCluster,
    posts: Sequence[RawNews],
    sources_text: str,
    base_payload: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    """Enrich payload with Yandex Wordstat queries and matching Brand-Safe advertiser."""
    if not isinstance(session, AsyncSession):
        return base_payload, None
    try:
        from app.services.content_prompts import (  # noqa: PLC0415
            is_brand_safe_for_commercial_integration,
        )
        from app.services.lab_pipeline import (  # noqa: PLC0415
            _build_augmented_cluster_data,
            _generate_wordstat_and_seo,
        )
        from app.services.promoted_projects_service import (  # noqa: PLC0415
            PromotedProjectsService,
        )
        from app.services.seo_learning import get_learned_patterns  # noqa: PLC0415

        seo_data = await _generate_wordstat_and_seo(
            session, cluster.title or "", sources_text, []
        )

        seo_guidelines: list[str] = []
        try:
            learned_info = await get_learned_patterns(
                session, category=str(cluster.category or ""), limit=10
            )
            seo_guidelines = list(learned_info.get("guidelines") or [])
        except Exception as learn_err:  # noqa: BLE001
            logger.debug(
                "Skipping learned SEO patterns for cluster %s in worker: %s",
                cluster.id,
                learn_err,
            )

        augmented = _build_augmented_cluster_data(
            cluster,
            posts,
            sources_text,
            focus_keyword=seo_data["focus_keyword"],
            wordstat_queries=seo_data["wordstat_queries"],
            seo_guidelines=seo_guidelines,
        )
        enrichment = dict(cluster.enrichment_data or {})
        enrichment["wordstat_data"] = seo_data["wordstat_queries"]
        enrichment["seo_metadata"] = {
            "focus_keyword": seo_data["focus_keyword"],
            "seo_title": seo_data["seo_title"],
            "seo_description": seo_data["seo_description"],
        }
        cluster.enrichment_data = enrichment

        all_promoted = await PromotedProjectsService.get_all_projects(session)
        active_projects = [p for p in all_promoted if p.is_active]
        is_safe, _ = is_brand_safe_for_commercial_integration(
            category=str(cluster.category or ""),
            title=str(cluster.title or ""),
            text=sources_text[:2000],
        )
        matching = PromotedProjectsService.get_matching_projects(
            active_projects,
            category_slug=str(cluster.category or ""),
            is_brand_safe=is_safe,
        )
        chosen_project = matching[0] if matching else None
        return augmented, chosen_project
    except Exception as exc:  # noqa: BLE001
        logger.warning("Studio payload enrichment skipped for %s: %s", cluster.id, exc)
        return base_payload, None


async def _ensure_cluster_web_research(
    session: AsyncSession,
    cluster: NewsCluster,
    posts: Sequence[RawNews],
    cid: uuid.UUID,
) -> None:
    """Always run web research enrichment for the cluster in production DB."""
    if not isinstance(session, AsyncSession):
        return
    enrichment = dict(cluster.enrichment_data or {})
    if enrichment.get("web_sources") and enrichment.get("web_facts"):
        return
    try:
        res = await conduct_cluster_research(
            session, cluster, posts, enable_web_search=True
        )
        enrichment["web_sources"] = res["web_sources"]
        enrichment["web_facts"] = res["web_facts"]
        enrichment["web_images"] = res.get("web_images", [])
        enrichment["web_research"] = res["web_research"]
        enrichment["search_query"] = res.get("search_query", "")
        cluster.enrichment_data = enrichment
    except Exception as enrich_exc:  # noqa: BLE001
        logger.warning("On-the-fly enrichment skipped for %s: %s", cid, enrich_exc)


async def _review_draft_safe(
    session: AsyncSession,
    sources_text: str,
    draft: Mapping[str, Any],
    outcome: ArticleOutcome,
) -> dict[str, Any]:
    """Run QA review with the configured SEO model, falling back to draft on error."""
    try:
        seo_chain = await resolve_chain(session, "seo")
        fixer_model = seo_chain[0] if seo_chain else None
        reviewed = dict(
            await review_and_fix_article(
                sources_text, draft, model_name=fixer_model
            )
        )
    except (AIClientError, ModelPoolExhaustedError) as exc:
        outcome.warn(f"QA repair skipped: {type(exc).__name__}: {exc}")
        reviewed = dict(draft)
    outcome.qa_fixed = bool(reviewed.get("qa_fixed"))
    if reviewed.get("qa_remaining_issues"):
        outcome.warn(
            f"article kept with open QA findings: {reviewed['qa_remaining_issues']}"
        )
    return reviewed


async def _invoke_writer_for_cluster(
    session: AsyncSession,
    cluster: NewsCluster,
    payload: dict[str, Any],
    *,
    risk: str,
    promoted_project: Any,
) -> Any:
    """Run writer chain with optional promoted project."""
    writer_chain = await resolve_chain(session, "writer")

    async def _write(model: str) -> dict[str, Any]:
        if promoted_project is not None:
            return await generate_article(
                payload,
                cluster.category,
                model_name=model,
                risk_level=risk,
                promoted_project=promoted_project,
            )
        return await generate_article(
            payload, cluster.category, model_name=model, risk_level=risk
        )

    return await run_with_fallback("writer", writer_chain, _write)


_CITY_SYNONYMS: Final[dict[str, str]] = {
    "питер": "петербург",
    "спб": "петербург",
    "мск": "москв",
}


def _extract_title_stems(title: str) -> set[str]:
    """Extract lowercased normalized word stems (>= 3 chars) from title."""
    words = re.findall(r"[a-z\u0430-\u044f\u04510-9]{3,}", (title or "").lower())
    stems: set[str] = set()
    for w in words:
        if w in _CITY_SYNONYMS:
            stems.add(_CITY_SYNONYMS[w])
            continue
        # 1. Strip reflexive & verbal suffixes
        stem = re.sub(
            r"(?:вшись|лись|лась|лось|лся|ться|тся|ете|ите|ут|ют|ат|ят|ет|ит|л|ла|ло|ли|ть|ти)$",
            "",
            w,
        )
        # 2. Strip case & adjectival endings
        stem = re.sub(
            r"(?:ами|ями|ого|его|ому|ему|ыми|ими|ах|ях|ов|ев|ей|ий|ый|ой|ая|яя|ое|ее|ые|ие|ия|ья|ам|ям|ом|ем|а|я|о|е|у|ю|ы|и|й)$",  # noqa: RUF001
            "",
            stem,
        )
        # 3. Strip word-building derivative suffixes for adjectives/nouns
        if len(stem) >= 5:
            stem = re.sub(
                r"(?:ск|енн|анн|овн|евн|тель|льн|оват|еват|ин|ов|ев|н)$",
                "",
                stem,
            )
        norm = _CITY_SYNONYMS.get(stem, stem)
        if len(norm) >= 3:
            stems.add(norm)
        else:
            stems.add(w)
    return stems


async def _check_and_acquire_event_lock(
    redis: Any,
    cluster_id: uuid.UUID,
    title: str,
    *,
    ttl_seconds: int = 900,
) -> tuple[bool, str]:
    """Atomically check and acquire a distributed event lock based on semantic root stems.

    Prevents parallel workers from generating articles about the same breaking news event.
    """
    import json  # noqa: PLC0415
    import time  # noqa: PLC0415

    c_stems = _extract_title_stems(title)
    if not c_stems:
        return True, ""

    redis_key = "dedup:active_event_generations"
    now = time.time()

    # 1. Fetch current active generation records
    raw_dict = await redis.hgetall(redis_key)
    active_records: dict[str, dict[str, Any]] = {}
    expired_keys: list[str] = []

    if raw_dict:
        for k_raw, v_raw in raw_dict.items():
            k_str = k_raw.decode("utf-8") if isinstance(k_raw, bytes) else str(k_raw)
            v_str = v_raw.decode("utf-8") if isinstance(v_raw, bytes) else str(v_raw)
            try:
                data = json.loads(v_str)
                if now - float(data.get("timestamp", 0)) > ttl_seconds:
                    expired_keys.append(k_str)
                else:
                    active_records[k_str] = data
            except Exception:  # noqa: BLE001
                expired_keys.append(k_str)

    # Clean up expired entries in Redis
    if expired_keys:
        await redis.hdel(redis_key, *expired_keys)

    # 2. Check overlap with currently running active generations
    cid_str = str(cluster_id)
    rare_stems = {s for s in c_stems if len(s) >= 7 and not s.isdigit()}
    sig_stems = {s for s in c_stems if len(s) >= 4 and not s.isdigit()}

    for active_cid, active_data in active_records.items():
        if active_cid == cid_str:
            continue
        active_stems = set(active_data.get("stems", []))
        shared = sig_stems & active_stems
        rare_shared = rare_stems & active_stems

        # Conflict rule: 2+ significant stems (>=4 chars) or 1+ rare entity (>=7 chars)
        if len(shared) >= 2 or len(rare_shared) >= 1:
            active_title = active_data.get("title", "")
            match_words = list(shared | rare_shared)[:4]
            reason = (
                f"conflict with parallel generation for cluster {active_cid} "
                f"('{active_title[:40]}') on stems {match_words}"
            )
            return False, reason

    # 3. No conflict: register this generation in Redis
    record = {
        "title": title,
        "stems": list(c_stems),
        "timestamp": now,
    }
    await redis.hset(redis_key, cid_str, json.dumps(record, ensure_ascii=False))
    return True, ""


def _make_event_lock_key(title: str) -> str:
    """Generate a stable deduplication event lock key from top title stems."""
    stems = sorted(_extract_title_stems(title))
    sig_stems = [s for s in stems if not s.isdigit()] or stems
    top_stems = sorted(sig_stems, key=len, reverse=True)[:4]
    slug = "_".join(sorted(top_stems))
    return f"dedup:generating_event:{slug}" if slug else ""


async def _find_duplicate_article(  # noqa: PLR0911
    session: AsyncSession,
    cluster: NewsCluster,
    posts: Sequence[RawNews],
    *,
    title_override: str | None = None,
) -> tuple[Article | None, str, bool]:
    """Find if an article covering this event was published in 48h.

    Returns:
        (matching_article, match_reason, is_followup)
        When is_followup is True, this is a legitimate story update/follow-up
        (published >= 2.0 hours ago with fresh facts or developments).
    """
    from datetime import timedelta  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(hours=48)
    stmt = (
        select(Article)
        .options(undefer(Article.final_embedding))
        .where(Article.created_at >= cutoff)
        .order_by(Article.created_at.desc())
        .limit(3000)
    )
    result = await session.execute(stmt)
    recent_articles = list(result.scalars().all())
    if not recent_articles:
        return None, "", False

    c_title = (title_override or cluster.title or "").strip()
    c_stems = _extract_title_stems(c_title)

    # When checking cluster before generation, also include entity stems from lead text
    lead_entities: set[str] = set()
    if not title_override and posts and posts[0].raw_text:
        lead_stems = _extract_title_stems(posts[0].raw_text[:300])
        lead_entities = {s for s in lead_stems if len(s) >= 6 and not s.isdigit()}
        c_stems.update(lead_entities)

    def _assess_followup(candidate_art: Article, shared_stems: set[str]) -> bool:
        """Determine whether candidate is old enough and new cluster has fresh facts."""
        art_time = candidate_art.published_at or candidate_art.created_at
        if art_time is None:
            return False
        if art_time.tzinfo is None:
            art_time = art_time.replace(tzinfo=UTC)
        age_hours = (datetime.now(UTC) - art_time).total_seconds() / 3600.0
        # Only allow follow-up if at least 2.0 hours have passed since the original story
        if age_hours < 2.0:
            return False

        a_title = (candidate_art.title or "").strip()
        a_stems = _extract_title_stems(a_title)
        novel_stems = c_stems - a_stems
        novel_sig = {s for s in novel_stems if len(s) >= 4 and not s.isdigit()}
        novel_entities = {s for s in novel_stems if len(s) >= 6 and not s.isdigit()}
        # Follow-up qualification:
        # At least 2 new significant stems OR at least 1 new entity stem,
        # provided title is not an exact stem clone (Jaccard < 0.70)
        stem_union = c_stems | a_stems
        stem_jaccard = len(shared_stems) / len(stem_union) if stem_union else 0.0
        return bool(
            stem_jaccard < 0.70 and (len(novel_sig) >= 2 or len(novel_entities) >= 1)
        )

    # 1. Fast lexical title stem overlap
    if c_stems:
        for art in recent_articles:
            a_title = (art.title or "").strip()
            a_stems = _extract_title_stems(a_title)
            if not a_stems:
                continue
            intersection = c_stems & a_stems
            rare_shared = {s for s in intersection if len(s) >= 6 and not s.isdigit()}
            union = c_stems | a_stems
            jaccard = len(intersection) / len(union) if union else 0.0

            # Match conditions:
            # - 3+ overlapping stems
            # - OR Jaccard >= 0.28
            # - OR 1+ rare entity (>=6 chars) and 2+ overlapping stems
            # - OR 1+ very rare entity (>=7 chars) with Jaccard >= 0.15
            matches_very_rare = any(len(s) >= 7 for s in intersection) and (
                len(intersection) >= 2 or jaccard >= 0.15
            )
            if (
                len(intersection) >= 3
                or jaccard >= 0.28
                or (len(rare_shared) >= 1 and len(intersection) >= 2)
                or matches_very_rare
            ):
                m_words = list(intersection)[:4]
                is_fol = _assess_followup(art, intersection)
                return (
                    art,
                    f"lexical stem match (jaccard={jaccard:.2f}, shared_stems={m_words})",
                    is_fol,
                )

    # 2. Semantic vector cosine similarity check via pgvector
    first_text = (posts[0].raw_text or "")[:400] if posts else ""
    query_text = f"{c_title}\n{first_text}".strip()
    if query_text:
        try:
            query_emb = await get_text_embedding(query_text)
            if query_emb:
                # Query nearest article vectors directly via pgvector
                stmt_vec = (
                    select(
                        Article,
                        Article.final_embedding.cosine_distance(query_emb).label("dist"),
                    )
                    .where(
                        Article.created_at >= cutoff,
                        Article.final_embedding.is_not(None),
                    )
                    .order_by("dist")
                    .limit(5)
                )
                try:
                    res_vec = await session.execute(stmt_vec)
                    for row in res_vec.all():
                        art, dist = row[0], float(row[1])
                        sim = 1.0 - dist
                        a_stems = _extract_title_stems(art.title or "")
                        shared_stems = c_stems & a_stems
                        if sim >= 0.75:
                            is_fol = _assess_followup(art, shared_stems)
                            return art, f"semantic pgvector match (sim={sim:.3f})", is_fol
                        has_overlap = bool(
                            shared_stems
                            or (
                                len(shared_stems) / len(c_stems | a_stems) >= 0.12
                                if (c_stems | a_stems)
                                else False
                            )
                        )
                        if sim >= 0.65 and has_overlap:
                            shared = list(shared_stems)[:3]
                            is_fol = _assess_followup(art, shared_stems)
                            return (
                                art,
                                f"semantic match with lexical overlap "
                                f"(sim={sim:.3f}, shared={shared})",
                                is_fol,
                            )
                except Exception as pg_exc:  # noqa: BLE001
                    logger.debug("pgvector direct query skipped: %s", pg_exc)

                # Fallback in-memory comparison
                import numpy as np  # noqa: PLC0415

                q_vec = np.array(query_emb, dtype=np.float32)
                norm_q = float(np.linalg.norm(q_vec))
                if norm_q > 0:
                    q_vec /= norm_q
                    for art in recent_articles:
                        if not art.final_embedding:
                            continue
                        a_vec = np.array(art.final_embedding, dtype=np.float32)
                        norm_a = float(np.linalg.norm(a_vec))
                        if norm_a > 0:
                            a_vec /= norm_a
                            sim = float(np.dot(q_vec, a_vec))
                            a_stems = _extract_title_stems(art.title or "")
                            shared_stems = c_stems & a_stems
                            if sim >= 0.75:
                                is_fol = _assess_followup(art, shared_stems)
                                return art, f"semantic match (sim={sim:.3f})", is_fol
                            if sim >= 0.65 and shared_stems:
                                shared = list(shared_stems)[:3]
                                is_fol = _assess_followup(art, shared_stems)
                                return (
                                    art,
                                    f"semantic match with lexical overlap "
                                    f"(sim={sim:.3f}, shared={shared})",
                                    is_fol,
                                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Duplicate embedding check skipped: %s", exc)

    return None, "", False


async def process_cluster(  # noqa: PLR0911
    session: AsyncSession,
    cluster: NewsCluster,
    *,
    http_client: httpx.AsyncClient,
) -> ArticleOutcome:
    """Run the full pipeline for one cluster and persist its article.

    Commits on success and rolls back on failure, so one bad cluster cannot undo
    the articles written before it. Returns the outcome either way; the caller
    aggregates them into the run report.
    """
    cid = cluster.id
    ccat = str(cluster.category or "novosti-obshhestva")
    outcome = ArticleOutcome(cluster_id=cid, category=ccat)
    posts = await _load_cluster_posts(session, cid)
    if not posts:
        outcome.status = "skipped"
        outcome.error = "cluster has no source posts"
        return outcome

    # --- Step 1.1: Concurrent Event Lock (prevent duplicate worker generation)
    try:
        redis_client = await get_redis()
        lock_ok, lock_reason = await _check_and_acquire_event_lock(
            redis_client, cid, cluster.title
        )
        if not lock_ok:
            cluster.is_published = True
            await _mark_cluster_raw_news_processed(session, cid)
            await session.commit()
            outcome.status = "skipped"
            outcome.warn(f"concurrent generation guard: {lock_reason}")
            logger.info("Cluster %s dropped: %s", cid, lock_reason)
            return outcome
    except Exception as lock_err:  # noqa: BLE001
        logger.debug("Redis event lock check failed: %s", lock_err)

    # --- Step 1.2: Cross-Article Duplicate Guard & Follow-up Detection
    dup_art, dup_reason, is_followup = await _find_duplicate_article(
        session, cluster, posts
    )
    parent_article_info: dict[str, Any] | None = None
    if dup_art is not None:
        if is_followup:
            parent_article_info = {
                "id": dup_art.id,
                "title": dup_art.title,
                "url_slug": dup_art.url_slug,
                "category": dup_art.category,
            }
            enrichment_data = dict(cluster.enrichment_data or {})
            enrichment_data["parent_article"] = parent_article_info
            cluster.enrichment_data = enrichment_data
            logger.info(
                "Cluster %s accepted as follow-up to Article #%s ('%s'): %s",
                cid,
                dup_art.id,
                dup_art.title[:60],
                dup_reason,
            )
        else:
            cluster.is_published = True
            await _mark_cluster_raw_news_processed(session, cid)
            await session.commit()
            outcome.status = "skipped"
            outcome.warn(
                f"duplicate of Article #{dup_art.id} ('{dup_art.title[:40]}'): "
                f"{dup_reason}"
            )
            logger.info(
                "Cluster %s dropped as duplicate of Article #%s (%s): %s",
                cid,
                dup_art.id,
                dup_art.title[:60],
                dup_reason,
            )
            return outcome

    await _ensure_cluster_web_research(session, cluster, posts, cid)
    base_payload = _build_cluster_payload(cluster, posts)
    if parent_article_info:
        base_payload["parent_article"] = parent_article_info
    sources_text = _sources_text(posts)

    # --- Step 1.5: moderation, before any writer call.
    risk = await _moderate(session, cluster, sources_text, outcome)
    if risk is None:
        return outcome

    payload, promoted_project = await _prepare_studio_aligned_payload(
        session, cluster, posts, sources_text, base_payload
    )
    if parent_article_info and "parent_article" not in payload:
        payload["parent_article"] = parent_article_info
    try:
        write_route = await _invoke_writer_for_cluster(
            session, cluster, payload, risk=risk, promoted_project=promoted_project
        )
        draft = write_route.value
        if write_route.used_fallback:
            outcome.warn(f"generation used a fallback model: {write_route.model}")
    except (ContentGenerationError, ModelPoolExhaustedError) as exc:
        outcome.status = "failed"
        outcome.error = f"generation failed: {exc}"
        logger.warning("Cluster %s: %s", cid, outcome.error)
        return outcome

    outcome.generation_attempts = int(draft.get("attempts") or 1)
    reviewed = await _review_draft_safe(session, sources_text, draft, outcome)
    await _enrich_and_decorate_draft(session, cluster, posts, reviewed)

    # --- Step 3.5: Post-Generation Cross-Article Duplicate Guard (Final Title Check)
    final_title = str(reviewed.get("title") or "")
    if final_title:
        post_dup, post_dup_reason, post_is_followup = await _find_duplicate_article(
            session, cluster, posts, title_override=final_title
        )
        if post_dup is not None:
            # If recognized as a follow-up of post_dup (or previous parent), allow it
            is_same_as_parent = (
                parent_article_info is not None
                and parent_article_info.get("id") == post_dup.id
            )
            if not post_is_followup and not is_same_as_parent:
                cluster.is_published = True
                await _mark_cluster_raw_news_processed(session, cid)
                await session.commit()
                outcome.status = "skipped"
                outcome.warn(
                    f"post-generation duplicate of Article #{post_dup.id} "
                    f"('{post_dup.title[:30]}'): {post_dup_reason}"
                )
                logger.info(
                    "Cluster %s dropped post-generation as duplicate of "
                    "Article #%s (%s): %s",
                    cid,
                    post_dup.id,
                    post_dup.title[:60],
                    post_dup_reason,
                )
                return outcome

    # --- Step 4: embed the final text.
    embedding: list[float] | None = None
    try:
        embedding = await get_text_embedding(
            f"{reviewed.get('title', '')}\n{reviewed.get('seo_description', '')}"
        )
        outcome.embedded = True
    except (EmbeddingError, AIClientError) as exc:
        outcome.warn(f"embedding failed: {type(exc).__name__}: {exc}")

    # --- Steps 5 and 6: media. Neither can fail the article.
    strategy = _media_strategy(posts)
    enrichment_snapshot = dict(cluster.enrichment_data or {})
    await _process_images(
        http_client,
        reviewed,
        posts,
        outcome,
        strategy=strategy,
        session=session,
        enrichment_data=enrichment_snapshot,
    )
    video_links = await _process_video(
        http_client, reviewed, posts, outcome, session=session
    )

    # --- Step 7: persist.
    article = _build_article_from_reviewed(
        cluster,
        reviewed,
        outcome,
        video_links=video_links,
        embedding=embedding,
    )
    session.add(article)
    await _mark_cluster_raw_news_processed(session, cid)
    try:
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        outcome.status = "failed"
        outcome.error = f"could not persist the article: {type(exc).__name__}"
        logger.exception("Cluster %s: persisting the article failed", cid)
        return outcome

    await session.refresh(article, ["id"])
    await _record_worker_article_usage(
        session,
        cluster=cluster,
        article=article,
        outcome=outcome,
        model=write_route.model,
        draft=draft,
    )

    await _record_journal(session, article=article, cluster=cluster, outcome=outcome)
    logger.info(
        "Cluster %s produced article %d (%s), cover=%s, video=%s",
        cid,
        article.id,
        article.url_slug,
        outcome.image_source,
        outcome.video_uploaded,
    )
    return outcome


async def run_content_pipeline(
    *,
    cluster_ids: Sequence[uuid.UUID] | None = None,
    limit: int = MAX_CLUSTERS_PER_RUN,
) -> ContentRunReport:
    """Process up to ``limit`` pending clusters and report on each.

    Fenced by a global run lock so overlapping scheduled ticks cannot generate the
    same cluster twice, plus a per-cluster lock so the dashboard's manual trigger
    cannot race the scheduled run. Contention is reported, never waited on: the
    scheduler fires on a fixed period, so a queued run would pile up behind work
    that has already been done.
    """
    started = datetime.now(UTC)
    report = ContentRunReport()

    async with AsyncExitStack() as stack:
        redis: Redis = get_redis()
        stack.push_async_callback(redis.aclose)

        run_lock = await stack.enter_async_context(
            acquire_lock(redis, CONTENT_RUN_LOCK_KEY, ttl=CONTENT_JOB_TIMEOUT)
        )
        if not run_lock:
            logger.info("A content run is already in progress; skipping this tick")
            report.lock_skipped = True
            return report

        session: AsyncSession = AsyncSessionLocal()
        stack.push_async_callback(session.close)
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(MEDIA_FETCH_TIMEOUT_SECONDS),
            follow_redirects=True,
        )
        stack.push_async_callback(http_client.aclose)

        clusters = await _load_pending_clusters(session, cluster_ids, limit)
        # Snapshot plain IDs/categories before any per-cluster commit/rollback
        # expires ORM attributes on remaining items in the list.
        pending_items = [
            (c.id, str(c.category or "novosti-obshhestva"), c) for c in clusters
        ]
        logger.info("Content run starting for %d cluster(s)", len(pending_items))

        for cid, ccat, cluster_obj in pending_items:
            async with acquire_lock(
                redis, cluster_lock_key(cid), ttl=CONTENT_JOB_TIMEOUT
            ) as cluster_lock:
                if not cluster_lock:
                    logger.info(
                        "Cluster %s is already being processed; skipping", cid
                    )
                    continue
                try:
                    cluster = (
                        await session.get(NewsCluster, cid)
                        if isinstance(session, AsyncSession)
                        else cluster_obj
                    )
                    if cluster is None:
                        continue
                    report.outcomes.append(
                        await process_cluster(
                            session, cluster, http_client=http_client
                        )
                    )
                except Exception as exc:
                    await session.rollback()
                    logger.exception("Cluster %s aborted the pipeline", cid)
                    report.outcomes.append(
                        ArticleOutcome(
                            cluster_id=cid,
                            category=ccat,
                            status="failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )

    elapsed = (datetime.now(UTC) - started).total_seconds()
    report.duration_ms = max(int(elapsed * 1000), 0)
    logger.info(
        "Content run finished: %d created, %d failed in %d ms",
        report.created,
        report.failed,
        report.duration_ms,
    )
    return report


async def content_task(
    ctx: dict[str, Any],
    *,
    cluster_ids: Sequence[str] | None = None,
    limit: int = MAX_CLUSTERS_PER_RUN,
) -> dict[str, Any]:
    """ARQ entry point: run one content pass and return it as plain JSON.

    A thin wrapper by design — every decision lives in
    :func:`run_content_pipeline`. ``cluster_ids`` arrive as strings because ARQ
    serialises job arguments, so they are parsed here; an unparseable id is dropped
    with a warning rather than failing the whole job.
    """
    # The production-mode switch is checked in the cron task, not in the
    # pipeline function: the lab page and the manual triggers call that
    # function directly and must keep working while production is paused.
    if not await _production_allowed():
        logger.info("Production mode is off: skipping this tick")
        return {"skipped": True, "reason": "pipeline disabled"}

    parsed: list[uuid.UUID] = []
    for raw in cluster_ids or ():
        try:
            parsed.append(uuid.UUID(str(raw)))
        except ValueError:
            logger.warning("Ignoring unparseable cluster id %r", raw)
    report = await run_content_pipeline(
        cluster_ids=parsed or None, limit=max(1, min(limit, MAX_CLUSTERS_PER_RUN))
    )
    if report.created > 0:
        try:
            from app.workers.publisher_worker import run_publisher  # noqa: PLC0415

            await run_publisher()
        except Exception as pub_exc:  # noqa: BLE001
            logger.warning("Immediate post-content publish pass skipped: %s", pub_exc)
    return report.to_dict()


async def _production_allowed() -> bool:
    """Whether the production switch permits this cron tick to do work.

    Fails open: an unreachable database must not silently pause the pipeline,
    because a paused pipeline still reports healthy and the missing work is only
    noticed much later.
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
    """Warm the shared clients so the first job does not pay for their setup."""
    from app.api.logs_sse import install_log_handler  # noqa: PLC0415

    logger.info("Content worker starting (env=%s)", settings.app_env)
    install_log_handler()
    ctx["redis_client"] = get_redis()
    get_ai_client()


async def shutdown(ctx: dict[str, Any]) -> None:
    """Release every long-lived client this worker touches."""
    client = ctx.pop("redis_client", None)
    if client is not None:
        await client.aclose()
    await close_ai_client()
    await close_flux_client()
    await close_embedding_service()
    await close_vk_video_service()
    await close_wp_media_service()
    await close_redis_pool()
    logger.info("Content worker stopped")


#: Maximum number of clusters to enrich per tick. Enrichment is cheap (web
#: search only, no AI writer), so a higher limit is safe, but we cap at 20 to
#: avoid blocking the event loop with too many sequential DuckDuckGo requests.
MAX_CLUSTERS_PER_ENRICH: Final[int] = 100

#: Only enrich clusters younger than this. Older events are unlikely to have
#: new coverage appearing, and skipping them avoids re-scanning dead queries.
ENRICH_LOOKBACK_HOURS: Final[int] = 24


async def _load_unenriched_clusters(
    session: AsyncSession, limit: int
) -> list[NewsCluster]:
    """Load clusters that need web research enrichment.

    A cluster qualifies when:
    * It has no article yet (avoids wasting a web search after the article is
      already written — the writer already had its chance).
    * Its ``enrichment_data.web_sources`` is absent or empty.
    * It was created in the last ``ENRICH_LOOKBACK_HOURS`` hours.  Stale
      clusters are unlikely to attract new coverage and their search queries
      would just return the same empty results.
    * Oldest-first, so early events are enriched before follow-ups.
    """
    from datetime import timedelta  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(hours=ENRICH_LOOKBACK_HOURS)
    already_written = (
        select(Article.id).where(Article.cluster_id == NewsCluster.id).exists()
    )
    result = await session.execute(
        select(NewsCluster)
        .where(~already_written)
        .where(NewsCluster.is_published.is_(False))
        .where(NewsCluster.created_at >= cutoff)
        .order_by(NewsCluster.created_at)
        .limit(limit)
    )
    clusters = list(result.scalars().all())

    # Filter: keep only those without web_sources (None or empty list).
    unenriched: list[NewsCluster] = []
    for cluster in clusters:
        enrichment = cluster.enrichment_data or {}
        web_sources = enrichment.get("web_sources")
        if not web_sources:  # None, [], or missing key
            unenriched.append(cluster)
    return unenriched


async def run_enrichment_pass(limit: int = MAX_CLUSTERS_PER_ENRICH) -> dict[str, Any]:
    """Run one web-research enrichment pass over unenriched clusters.

    **Closed-Loop Accountability (v2.35.0):**
    1. Status: runs when PRODUCTION_PIPELINE is enabled; controlled by
       ``_production_allowed()``.
    2. Interaction: DuckDuckGo → Trafilatura (via ``conduct_cluster_research``),
       ``news_clusters.enrichment_data`` column (PostgreSQL).
    3. Operation: For each qualifying cluster calls ``conduct_cluster_research``
       which issues a DuckDuckGo search, scrapes accepted articles, extracts
       images and facts, then writes them to ``cluster.enrichment_data``.
    4. Goal: Ensure that auto-generated articles have web context (facts,
       images, source attribution) even when no operator is in the Lab.
    5. KPI: % of auto-mode articles with ``web_sources`` > 0; target ≥ 60%.
    6. Feedback: Query ``SELECT count(*) FROM news_clusters WHERE
       enrichment_data->>'web_sources' != '[]'`` in Metabase to verify.
    7. Hypothesis: Enriching breaking-news clusters 5-15 minutes after
       ingestion (when media coverage starts appearing) yields better source
       quality than searching immediately at ingestion time.
    """
    started = datetime.now(UTC)
    enriched_count = 0
    skipped_count = 0
    failed_count = 0

    async with AsyncExitStack() as stack:
        redis: Redis = get_redis()
        stack.push_async_callback(redis.aclose)

        run_lock = await stack.enter_async_context(
            acquire_lock(redis, ENRICH_RUN_LOCK_KEY, ttl=settings.ingest_lock_ttl)
        )
        if not run_lock:
            logger.info("An enrichment pass is already in progress; skipping this tick")
            return {"skipped": True, "reason": "lock_contention"}

        session: AsyncSession = AsyncSessionLocal()
        stack.push_async_callback(session.close)

        clusters = await _load_unenriched_clusters(session, limit)
        logger.info("Enrichment pass: %d cluster(s) to enrich", len(clusters))

        for cluster in clusters:
            posts = await _load_cluster_posts(session, cluster.id)
            if not posts:
                skipped_count += 1
                continue
            try:
                res = await conduct_cluster_research(
                    session, cluster, posts, enable_web_search=True
                )
                # Merge results into enrichment_data (same as lab_pipeline).
                enrichment = dict(cluster.enrichment_data or {})
                enrichment["web_sources"] = res["web_sources"]
                enrichment["web_facts"] = res["web_facts"]
                enrichment["web_images"] = res.get("web_images", [])
                enrichment["web_research"] = res["web_research"]
                enrichment["search_query"] = res.get("search_query", "")
                cluster.enrichment_data = enrichment
                await session.commit()
                enriched_count += 1
                logger.info(
                    "Enriched cluster %s: %d web sources",
                    cluster.id,
                    len(res["web_sources"]),
                )
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                failed_count += 1
                logger.warning(
                    "Enrichment failed for cluster %s: %s", cluster.id, exc
                )

    elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    logger.info(
        "Enrichment pass done: enriched=%d skipped=%d failed=%d in %dms",
        enriched_count,
        skipped_count,
        failed_count,
        elapsed_ms,
    )
    return {
        "enriched": enriched_count,
        "skipped": skipped_count,
        "failed": failed_count,
        "duration_ms": elapsed_ms,
    }


async def enrich_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """ARQ entry point: run one enrichment pass.

    Gated by the same production switch as ``content_task`` so both can be
    paused from the dashboard without touching worker config.
    """
    if not await _production_allowed():
        logger.info("Production mode is off: skipping enrichment tick")
        return {"skipped": True, "reason": "pipeline disabled"}
    return await run_enrichment_pass()


class WorkerSettings:
    """ARQ worker definition (``arq app.workers.content_worker.WorkerSettings``).

    Separate from the ingestion worker on purpose: ingestion is bounded by
    Chromium's memory and content generation by the AI budget, so they need
    different concurrency and different timeouts. Running them in one process would
    let a long Playwright scrape delay an article that is ready to be written.
    """

    redis_settings: Final[RedisSettings] = RedisSettings.from_dsn(
        str(settings.redis_url)
    )
    #: Dedicated queue: see :data:`CONTENT_QUEUE_NAME`.
    queue_name: Final[str] = CONTENT_QUEUE_NAME
    functions: Final[Sequence[WorkerCoroutine]] = (content_task, enrich_task)
    cron_jobs: Final[Sequence[CronJob]] = (
        cron(
            content_task,
            name=CONTENT_TASK_NAME,
            minute=set(CONTENT_CRON_MINUTES),
            second=0,
            run_at_startup=False,
            max_tries=1,
            timeout=CONTENT_JOB_TIMEOUT,
        ),
        cron(
            enrich_task,
            name=ENRICH_TASK_NAME,
            minute=set(ENRICH_CRON_MINUTES),
            second=0,
            run_at_startup=False,
            max_tries=1,
            timeout=CONTENT_JOB_TIMEOUT,
        ),
    )
    on_startup: Final[StartupShutdown] = startup
    on_shutdown: Final[StartupShutdown] = shutdown
    #: One job at a time: a batch already generates up to ten articles, and two
    #: concurrent batches would double the peak AI spend for no throughput gain on
    #: a rate-limited API.
    max_jobs: Final[int] = 1
    job_timeout: Final[int] = CONTENT_JOB_TIMEOUT
    keep_result: Final[int] = 3600
    #: Refresh the `<queue>:health-check` key every 30s. ARQ's 3600s default would
    #: leave a stale key valid for an hour, so `arq --check` — and therefore the
    #: container healthcheck — would keep reporting a wedged worker as healthy.
    #: The key's TTL is this interval plus one second, so it expires promptly once
    #: the main loop stops turning.
    health_check_interval: Final[int] = 30
