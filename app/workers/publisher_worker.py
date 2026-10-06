"""Publisher orchestrator: takes a finished article live on every channel.

The last worker in the pipeline. :mod:`app.workers.content_worker` leaves an
article at ``MEDIA_READY``; this module publishes it to WordPress, fans the link
out to the four social adapters, records what each platform did, and moves the
article to its terminal state.

The ordering is not arbitrary. **WordPress is published first and alone**, because
every social post links back to it: a Telegram teaser sent before the article
exists would point at a 404, and there is no way to recall it. Only once the
canonical URL is known does the fan-out start.

Decisions that carry the weight:

* **Two locks, both non-blocking.** A global run lock stops overlapping cron ticks,
  and a per-article lock stops the dashboard's manual trigger racing the scheduled
  run on the same row. Contention is skipped rather than queued — the scheduler
  fires on a fixed period, so a waiter would arrive after the work was already
  done.

* **Idempotency is a stored fact, not a flag.** An article carrying ``wp_post_id``
  is *updated* on WordPress rather than re-created, and one already past
  ``MEDIA_READY`` is skipped outright. Per-platform, a ``platform_statuses`` entry
  already marked ``published`` is not re-sent: a retry after a partial failure must
  not duplicate the posts that already succeeded.

* **A WordPress failure aborts; a social failure does not.** Without the canonical
  URL there is nothing to distribute, so the article stays at ``MEDIA_READY`` for
  the next tick. A social platform that fails leaves the article live on the site
  and on the platforms that worked, which is worth far more than an all-or-nothing
  transaction.

* **The fan-out is concurrent and isolated.** ``asyncio.gather`` with
  ``return_exceptions=True`` runs the four platforms in parallel — they are
  independent HTTP calls to different hosts, and serialising them would make a slow
  Dzen publish delay Telegram. ``BaseSocialAdapter.safe_publish`` already converts
  failures into results, so ``return_exceptions=True`` is the second layer that
  catches anything it could not.

* **The terminal state is ``DISTRIBUTED`` or ``WP_PUBLISHED``.** ``ArticleStatus``
  has no ``PUBLISHED``/``PARTIALLY_PUBLISHED`` members; per docs/01 the article is
  ``wp_published`` once it is on the site and ``distributed`` once the social
  channels have run. "Partially published" is therefore expressed as
  ``WP_PUBLISHED`` plus the per-platform detail in ``platform_statuses``, which is
  where an operator looks anyway.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.typing import StartupShutdown, WorkerCoroutine
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.article import Article
from app.models.enums import ArticleStatus, PlatformStatus
from app.redis_client import close_redis_pool, get_redis
from app.services.pipeline_control import pipeline_enabled
from app.services.social_base import BaseSocialAdapter, PublishResult
from app.services.social_dzen import DzenAdapter
from app.services.social_ok import OKAdapter
from app.services.social_telegram import TelegramAdapter
from app.services.social_vk import VKWallAdapter
from app.services.wp_media_service import (
    WPMediaError,
    close_wp_media_service,
    get_wp_media_service,
)
from app.services.wp_post_formatter import FeaturedImage, VideoEmbed, render_post
from app.services.wp_publisher import (
    WPPublishAuthError,
    WPPublisher,
    WPPublishError,
    close_wp_publisher,
    get_wp_publisher,
)
from app.workers.redis_lock import acquire_lock

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MAX_ARTICLES_PER_RUN",
    "PUBLISHER_CRON_MINUTES",
    "PUBLISHER_QUEUE_NAME",
    "PUBLISHER_TASK_NAME",
    "ArticlePublication",
    "PublisherRunReport",
    "WorkerSettings",
    "article_lock_key",
    "build_adapters",
    "keepalive_sessions_task",
    "publish_article_everywhere",
    "publisher_task",
    "run_publisher",
]

PUBLISHER_TASK_NAME: Final[str] = "publisher_task"
PUBLISHER_RUN_LOCK_KEY: Final[str] = "lock:publisher:run"

#: Dedicated ARQ queue for this worker. Each worker owns one, because ARQ's
#: default queue is shared: any worker polling it may claim a job belonging to
#: another, then fail it as "function not found".
PUBLISHER_QUEUE_NAME: Final[str] = "arq:queue:publisher"

#: Offset from the content worker's cron (minute 2, 12, …) so publishing reads a
#: settled backlog rather than racing the writes that produce it.
PUBLISHER_CRON_MINUTES: Final[frozenset[int]] = frozenset({5, 15, 25, 35, 45, 55})

#: Publish all ready articles collected in the pass (up to 100 per tick).
MAX_ARTICLES_PER_RUN: Final[int] = 100
PUBLISHER_JOB_TIMEOUT: Final[int] = 600

#: Retry budget for a transient WordPress failure *inside* one run. The publisher
#: client already retries at the HTTP level; this is the outer loop for a failure
#: that survived it, and it is small because the next cron tick is itself a retry.
MAX_WP_ATTEMPTS: Final[int] = 3
INITIAL_BACKOFF_SECONDS: Final[float] = 2.0
BACKOFF_MULTIPLIER: Final[float] = 3.0
MAX_BACKOFF_SECONDS: Final[float] = 60.0

#: Retries before an article is parked at ``FAILED``. Past this the failure is not
#: transient and re-running it every five minutes only burns quota.
MAX_ARTICLE_RETRIES: Final[int] = 5


def article_lock_key(article_id: int) -> str:
    """Redis key fencing one article's publication."""
    return f"lock:publisher:article:{article_id}"


@dataclass(slots=True)
class ArticlePublication:
    """What happened to one article."""

    article_id: int
    status: str = "skipped"
    wp_post_id: int | None = None
    wp_post_url: str = ""
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def social_published(self) -> int:
        return sum(
            1
            for entry in self.platforms.values()
            if entry.get("status") == PlatformStatus.PUBLISHED.value
        )

    @property
    def social_failed(self) -> int:
        return sum(
            1
            for entry in self.platforms.values()
            if entry.get("status") == PlatformStatus.FAILED.value
        )

    def warn(self, message: str) -> None:
        """Record a non-fatal problem, visible in the run report."""
        logger.warning("Article %s: %s", self.article_id, message)
        self.warnings.append(message)

    def to_dict(self) -> dict[str, Any]:
        """Render the publication as ARQ-serialisable JSON."""
        return {
            "article_id": self.article_id,
            "status": self.status,
            "wp_post_id": self.wp_post_id,
            "wp_post_url": self.wp_post_url,
            "platforms": dict(self.platforms),
            "social_published": self.social_published,
            "social_failed": self.social_failed,
            "warnings": list(self.warnings),
            "error": self.error,
        }


@dataclass(slots=True)
class PublisherRunReport:
    """Aggregate result of one publisher pass."""

    publications: list[ArticlePublication] = field(default_factory=list)
    lock_skipped: bool = False
    duration_ms: int = 0

    @property
    def distributed(self) -> int:
        return sum(
            1
            for item in self.publications
            if item.status == ArticleStatus.DISTRIBUTED.value
        )

    @property
    def wp_published(self) -> int:
        return sum(
            1
            for item in self.publications
            if item.status == ArticleStatus.WP_PUBLISHED.value
        )

    @property
    def failed(self) -> int:
        return sum(1 for item in self.publications if item.status == "failed")

    def to_dict(self) -> dict[str, Any]:
        """Render the report as ARQ-serialisable JSON."""
        return {
            "articles": len(self.publications),
            "distributed": self.distributed,
            "wp_published": self.wp_published,
            "failed": self.failed,
            "lock_skipped": self.lock_skipped,
            "duration_ms": self.duration_ms,
            "publications": [item.to_dict() for item in self.publications],
        }


def build_adapters(
    *,
    dzen_browser_publisher: Any | None = None,
    vk_access_token: str | None = None,
    vk_group_id: str | int | None = None,
    social_browser_configs: Any | None = None,
    tg_bot_token: str | None = None,
    tg_channel_id: str | None = None,
) -> list[BaseSocialAdapter]:
    """Instantiate every social adapter.

    All platforms are always constructed; an unconfigured one reports itself as
    ``DISABLED`` through :meth:`~app.services.social_base.BaseSocialAdapter.safe_publish`
    without a wasted HTTP request. Filtering here instead would hide a
    misconfiguration — a platform silently absent from the report looks the same as
    one that was never attempted.
    """
    from app.services.social_browser_publisher import (  # noqa: PLC0415
        build_social_browser_adapters,
    )

    return [
        TelegramAdapter(bot_token=tg_bot_token, channel_id=tg_channel_id),
        VKWallAdapter(access_token=vk_access_token, group_id=vk_group_id),
        OKAdapter(),
        DzenAdapter(browser_publisher=dzen_browser_publisher),
        *build_social_browser_adapters(social_browser_configs),
    ]


def _already_published(article: Article, platform_code: str) -> bool:
    """Whether this platform already carries the article.

    Read before every send so a retry after a partial failure re-sends only what
    failed. Without this check, one Telegram error would cause a duplicate VK and
    OK post on the next tick.
    """
    statuses = article.platform_statuses or {}
    entry = statuses.get(platform_code)
    if not isinstance(entry, dict):
        return False
    return entry.get("status") == PlatformStatus.PUBLISHED.value


def _attempts_for(article: Article, platform_code: str) -> int:
    """Previous attempt count for one platform, for the stored counter."""
    statuses = article.platform_statuses or {}
    entry = statuses.get(platform_code)
    if not isinstance(entry, dict):
        return 0
    raw = entry.get("attempts")
    return raw if isinstance(raw, int) and raw >= 0 else 0


def build_web_telegram_subscription_card(
    channel_url: str = "https://t.me/svodka_press",
) -> str:
    """Build a modern, high-CTR Telegram subscription card for WordPress."""
    return (
        '<div class="svodka-tg-callout" '
        'style="margin: 32px 0 20px 0; padding: 20px 24px; '
        "background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); "
        "border-radius: 14px; border: 1px solid rgba(56, 189, 248, 0.25); "
        "display: flex; align-items: center; justify-content: space-between; "
        'flex-wrap: wrap; gap: 16px; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.25); '
        'color: #ffffff;">\n'
        '  <div style="flex: 1 1 300px;">\n'
        '    <div style="display: flex; align-items: center; '
        'gap: 8px; margin-bottom: 6px;">\n'
        '      <span style="font-size: 22px;">⚡️</span>\n'
        '      <span style="font-size: 16px; font-weight: 700; color: #ffffff;">'
        "СводкаПресс в Telegram</span>\n"
        "    </div>\n"
        '    <p style="margin: 0; font-size: 14px; color: #94a3b8; line-height: 1.5;">\n'
        "      Оперативные новости без цензуры, эксклюзивные кадры "
        "и срочные сводки 24/7 в нашем официальном канале.\n"
        "    </p>\n"
        "  </div>\n"
        f'  <a href="{channel_url}" target="_blank" rel="noopener noreferrer" '
        'style="display: inline-flex; align-items: center; gap: 8px; '
        "background: #229ED9; color: #ffffff; text-decoration: none; "
        'font-weight: 600; font-size: 14px; padding: 11px 22px; border-radius: 10px; '
        'box-shadow: 0 4px 14px rgba(34, 158, 217, 0.4); '
        'transition: transform 0.2s ease;">\n'
        "    <span>Подписаться на канал</span>\n"
        '    <span style="font-size: 16px;">→</span>\n'
        "  </a>\n"
        "</div>"
    )


def _render_body(
    article: Article,
    *,
    cover_url: str = "",
    extra_footer_html: str = "",
) -> str:
    """Render the publishable HTML body for one article.

    The formatter runs at publication rather than at generation time because the
    body it produces embeds the JSON-LD, and that payload's ``datePublished``
    belongs to the moment the article goes live.
    """
    resolved_cover = cover_url or (
        str((article.platform_statuses or {}).get("wp", {}).get("cover_url") or "")
    )
    featured = (
        FeaturedImage(media_id=article.wp_image_id, source_url=resolved_cover)
        if article.wp_image_id and resolved_cover
        else None
    )
    videos = [
        VideoEmbed(
            owner_id=int(entry.get("owner_id", 0)),
            video_id=int(entry.get("video_id", 0)),
            access_key=str(entry.get("access_key") or ""),
            title=str(entry.get("title") or article.title),
            embed_html=str(entry.get("embed_html") or ""),
        )
        for entry in (article.video_links or ())
        if isinstance(entry, dict) and entry.get("video_id")
    ]
    body_content = article.content_html
    if extra_footer_html:
        body_content = f"{body_content}\n{extra_footer_html}"

    rendered = render_post(
        title=article.title,
        body_html=body_content,
        seo_description=article.seo_description or "",
        keywords=tuple(article.seo_keywords or ()),
        category=article.category,
        videos=videos,
        featured_image=featured,
        published_at=article.published_at,
        publisher_name="СводкаПресс",
        include_schema=True,
    )
    return rendered.content_html


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff for an outer WordPress retry."""
    return min(
        INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1)),
        MAX_BACKOFF_SECONDS,
    )


async def _publish_to_wordpress(
    session: AsyncSession,
    article: Article,
    publication: ArticlePublication,
    *,
    publisher: WPPublisher,
) -> bool:
    """Publish or update the article on WordPress. Returns whether it succeeded."""
    # Single promo rotation (1 promo per post, matching home page) is rendered
    # by single.php via svodka_setka_render_rotating_promo($post_id).
    cover_url = await _resolve_cover_url(article)
    if cover_url and isinstance(article.platform_statuses, dict):
        wp_entry = dict(article.platform_statuses.get("wp") or {})
        wp_entry["cover_url"] = cover_url
        article.platform_statuses["wp"] = wp_entry

    body = _render_body(
        article, cover_url=cover_url, extra_footer_html=""
    )
    last_error: str = "not attempted"

    for attempt in range(1, MAX_WP_ATTEMPTS + 1):
        try:
            post = await publisher.publish(
                session,
                title=article.title,
                content_html=body,
                slug=article.url_slug,
                excerpt=article.seo_description or "",
                category_slug=article.category,
                tags=tuple(article.seo_keywords or ()),
                featured_media=article.wp_image_id,
                seo_title=article.seo_title or article.title,
                seo_description=article.seo_description or "",
                existing_post_id=article.wp_post_id,
            )
        except WPPublishAuthError as exc:
            # Deterministic: no amount of retrying fixes a bad credential.
            publication.error = f"WordPress rejected our credentials: {exc}"
            logger.error("Article %s: %s", article.id, publication.error)
            return False
        except WPPublishError as exc:
            last_error = str(exc)
            if attempt < MAX_WP_ATTEMPTS:
                delay = _backoff_delay(attempt)
                logger.warning(
                    "Article %s: WordPress publish attempt %d/%d failed (%s); "
                    "retrying in %.0fs",
                    article.id,
                    attempt,
                    MAX_WP_ATTEMPTS,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
        else:
            article.wp_post_id = post.post_id
            article.wp_category_id = (
                post.category_ids[0] if post.category_ids else article.wp_category_id
            )
            article.wp_tag_ids = list(post.tag_ids)
            publication.wp_post_id = post.post_id
            publication.wp_post_url = post.link
            if isinstance(session, AsyncSession) and article.cluster_id:
                from app.models.cluster import NewsCluster  # noqa: PLC0415

                cluster_row = await session.get(NewsCluster, article.cluster_id)
                if cluster_row is not None:
                    cluster_row.is_published = True
            for warning in post.warnings:
                publication.warn(f"WordPress: {warning}")
            return True

    publication.error = f"WordPress publish failed: {last_error}"
    return False


async def _resolve_cover_url(article: Article) -> str:
    """Resolve the article's cover to a URL the social platforms can fetch.

    ``articles`` stores only ``wp_image_id``, but Telegram's ``sendPhoto``, OK's
    photo block and Dzen's cover all need an address. Without this lookup every
    social post would go out imageless, which roughly halves engagement on a news
    teaser.

    A failure returns ``""``: a text-only post is still worth publishing.
    """
    wp_status = (article.platform_statuses or {}).get("wp", {})
    if isinstance(wp_status, dict) and wp_status.get("cover_url"):
        return str(wp_status["cover_url"]).strip()

    if not article.wp_image_id:
        return ""
    try:
        svc = get_wp_media_service()
        if not svc.is_configured:
            from app.services.wp_connection import load_wp_connection  # noqa: PLC0415
            from app.services.wp_media_service import WPMediaService  # noqa: PLC0415

            async with AsyncSessionLocal() as db_session:
                conn = await load_wp_connection(db_session)
                if conn.has_rest_credentials:
                    async with WPMediaService(
                        base_url=conn.wp_base_url,
                        username=conn.wp_app_user,
                        app_password=conn.wp_app_password,
                    ) as db_svc:
                        return await db_svc.get_media_url(article.wp_image_id)
        return await svc.get_media_url(article.wp_image_id)
    except (WPMediaError, OSError, ValueError, KeyError) as exc:
        logger.warning(
            "Could not resolve WordPress media %s: %s", article.wp_image_id, exc
        )
        return ""


async def _fan_out(  # noqa: PLR0912
    article: Article,
    wp_post_url: str,
    publication: ArticlePublication,
    *,
    adapters: Sequence[BaseSocialAdapter],
) -> None:
    """Publish to every social platform concurrently and record each verdict.

    The platforms are independent HTTP calls to different hosts, so they run in
    parallel: serialising them would let a slow Dzen publish delay Telegram for no
    reason. ``return_exceptions=True`` is a second safety net under
    :meth:`~app.services.social_base.BaseSocialAdapter.safe_publish`, which already
    converts failures into results — if a bug escapes it, the gather still returns
    and the other three platforms keep their outcomes.

    A platform already marked ``published`` is skipped, so a retry after a partial
    failure does not duplicate the posts that worked.
    """
    pending: list[BaseSocialAdapter] = []
    for adapter in adapters:
        code = adapter.platform_code
        if _already_published(article, code):
            publication.warn(
                f"{code} already published; not re-sending"
            )
            continue
        statuses = article.platform_statuses or {}
        entry = statuses.get(code)
        if isinstance(entry, dict):
            err = str(entry.get("error") or "")
            if code == "vk" and ("214" in err or "500 posts a day" in err.lower()):
                publication.warn("VK 500 posts/day limit already reached; skipping retry")
                continue
            if code == "setka" and "429" in err:
                publication.warn("Setka rate limit (429) active; skipping retry")
                continue
        pending.append(adapter)

    if not pending:
        return

    cover_url = await _resolve_cover_url(article)
    if cover_url and isinstance(article.platform_statuses, dict):
        wp_entry = dict(article.platform_statuses.get("wp") or {})
        wp_entry["cover_url"] = cover_url
        article.platform_statuses["wp"] = wp_entry
    if article.wp_image_id and not cover_url:
        publication.warn(
            f"could not resolve media {article.wp_image_id} to a URL; social posts "
            "go out without a cover image"
        )

    # Resolve sponsor promo text for social distribution
    promo_text = ""
    try:
        from app.database import AsyncSessionLocal  # noqa: PLC0415
        from app.services.promoted_projects_service import (  # noqa: PLC0415
            PromotedProjectsService,
        )

        async with AsyncSessionLocal() as s_promo:
            all_promoted = await PromotedProjectsService.get_all_projects(s_promo)
            active_promoted = [p for p in all_promoted if p.is_active]
            matching = PromotedProjectsService.get_matching_projects(
                active_promoted,
                category_slug=article.category or "",
            )
            candidates = matching if matching else active_promoted
            chosen = (
                candidates[(article.id or 0) % len(candidates)]
                if candidates
                else None
            )
            if chosen:
                promo_target = chosen.build_tracked_url("social", "post_footer")
                usp = chosen.usp or "бесплатный доступ к передовым нейросетям"
                promo_text = (
                    f"💡 Партнёр рубрики — {chosen.name}: {usp}.\n"
                    f"Подробнее: {promo_target}"
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Article %s: failed to load promo for social posts: %s", article.id, exc
        )

    results = await asyncio.gather(
        *(
            asyncio.wait_for(
                adapter.safe_publish(
                    article,
                    wp_post_url,
                    photo_url=cover_url,
                    cover_url=cover_url,
                    image_urls=(cover_url,) if cover_url else (),
                    hashtags=(),
                    promo_text=promo_text,
                ),
                timeout=60.0,
            )
            for adapter in pending
        ),
        return_exceptions=True,
    )

    for adapter, outcome in zip(pending, results, strict=True):
        code = adapter.platform_code
        attempts = _attempts_for(article, code) + 1
        if isinstance(outcome, BaseException):
            # `safe_publish` should have caught this; reaching here means a bug in
            # the adapter's own error handling, which must still not lose the run.
            logger.exception(
                "Article %s: %s adapter escaped safe_publish", article.id, code
            )
            entry = PublishResult.failed(
                code,
                error=f"{type(outcome).__name__}: {outcome}",
                retryable=False,
            ).to_platform_status(attempts=attempts)
        else:
            for warning in outcome.warnings:
                publication.warn(f"{code}: {warning}")
            entry = outcome.to_platform_status(attempts=attempts)

        # Assigning into the tracked JSONB dict marks the article dirty, which is
        # what makes the update actually flush (see `app.models.types`).
        article.platform_statuses[code] = entry
        publication.platforms[code] = entry


def _terminal_status(publication: ArticlePublication) -> ArticleStatus:
    """Decide the article's terminal state from what actually published.

    ``DISTRIBUTED`` when no social platform failed with a retryable error.
    VK daily limit (error 214 / 500 posts a day) is non-retryable and should not keep
    an otherwise published article in WP_PUBLISHED indefinitely.
    ``WP_PUBLISHED`` when the site has the article but a retryable channel did not.
    """
    for code, entry in publication.platforms.items():
        if entry.get("status") == PlatformStatus.FAILED.value:
            err = str(entry.get("error") or "").lower()
            if code == "vk" and ("214" in err or "500 posts a day" in err):
                continue
            if code == "setka" and "429" in err:
                continue
            if code == "youtube" and (
                "лимит" in err
                or "развивайте свой канал" in err
                or "продвинутые функции" in err
            ):
                continue
            return ArticleStatus.WP_PUBLISHED
    return ArticleStatus.DISTRIBUTED


async def publish_article_everywhere(
    session: AsyncSession,
    article: Article,
    *,
    publisher: WPPublisher,
    adapters: Sequence[BaseSocialAdapter],
    allow_social_retry: bool = False,
) -> ArticlePublication:
    """Publish one article to WordPress and every social channel.

    Commits on success and rolls back on a persistence failure. WordPress runs
    first and alone: every social post links back to it, so a teaser sent before the
    article exists would point at a 404 that cannot be recalled.
    """
    publication = ArticlePublication(article_id=article.id)

    if article.status not in (ArticleStatus.MEDIA_READY, ArticleStatus.WP_PUBLISHED):
        publication.status = "skipped"
        publication.error = (
            f"article is {article.status.value}, "
            f"not {ArticleStatus.MEDIA_READY.value} or {ArticleStatus.WP_PUBLISHED.value}"
        )
        return publication

    if article.status == ArticleStatus.WP_PUBLISHED and not allow_social_retry:
        publication.status = "skipped"
        publication.error = (
            "article is already published on WordPress; social retry not requested"
        )
        return publication

    is_wp_already_published = (article.status == ArticleStatus.WP_PUBLISHED)
    wp_url = ""

    if is_wp_already_published:
        # Article is already on WordPress; skip creating a duplicate post
        wp_status = (article.platform_statuses or {}).get("wp", {})
        if isinstance(wp_status, dict):
            wp_url = str(wp_status.get("url") or "")
        if not wp_url and article.url_slug:
            base_site = (settings.wordpress_url or settings.wp_base_url or "https://svodka.press").rstrip("/")
            wp_url = f"{base_site}/{article.url_slug.strip('/')}/"
        publication.wp_post_id = article.wp_post_id
        publication.wp_post_url = wp_url
        logger.info(
            "Article %s: WordPress already published at %s; executing social republish",
            article.id,
            wp_url,
        )
    else:
        if not await _publish_to_wordpress(
            session, article, publication, publisher=publisher
        ):
            # Without a canonical URL there is nothing to distribute. The article keeps
            # its status so the next tick retries it.
            article.retry_count += 1
            publication.status = "failed"
            if article.retry_count >= MAX_ARTICLE_RETRIES:
                article.status = ArticleStatus.FAILED
                article.error_message = (publication.error or "")[:2000]
                publication.warn(
                    f"parked at FAILED after {article.retry_count} attempts"
                )
            await _commit(session, article, publication)
            return publication

        wp_url = publication.wp_post_url
        if not wp_url:
            # A post without a link cannot be distributed, but it *is* live, so the
            # article moves forward rather than being retried into a duplicate.
            publication.warn(
                "WordPress returned no permalink; social distribution was skipped"
            )
            article.status = ArticleStatus.WP_PUBLISHED
            article.published_at = datetime.now(UTC)
            publication.status = ArticleStatus.WP_PUBLISHED.value
            await _commit(session, article, publication)
            return publication

        # Checkpoint: commit WordPress post ID and status immediately to DB
        # so any subsequent social failure, worker crash, or timeout never leaves
        # the article in MEDIA_READY, eliminating WordPress retry duplicates.
        article.status = ArticleStatus.WP_PUBLISHED
        article.published_at = article.published_at or datetime.now(UTC)
        await session.commit()

        try:
            from app.services.bing_connection import (  # noqa: PLC0415
                load_bing_connection,
                submit_to_indexnow,
            )

            b_cfg = await load_bing_connection(session)
            if b_cfg.is_indexnow_active and b_cfg.has_indexnow_key:
                await submit_to_indexnow([wp_url], b_cfg)
        except (SQLAlchemyError, OSError) as in_err:
            logger.warning("IndexNow notification skipped: %s", in_err)

    await _fan_out(article, wp_url, publication, adapters=adapters)

    article.status = _terminal_status(publication)
    article.published_at = article.published_at or datetime.now(UTC)
    article.error_message = "; ".join(publication.warnings)[:2000] or None
    publication.status = article.status.value

    # If still in WP_PUBLISHED (some socials failed), increment retry_count
    if article.status == ArticleStatus.WP_PUBLISHED:
        article.retry_count += 1

    await _commit(session, article, publication)
    logger.info(
        "Article %s is %s: WP post %s, %d social published, %d failed",
        article.id,
        article.status.value,
        publication.wp_post_id,
        publication.social_published,
        publication.social_failed,
    )
    return publication


async def _commit(
    session: AsyncSession, article: Article, publication: ArticlePublication
) -> None:
    """Commit the article's new state, recording a failure on the publication."""
    try:
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        publication.status = "failed"
        publication.error = f"could not persist the publication: {type(exc).__name__}"
        logger.exception("Article %s: persisting the publication failed", article.id)


def _has_unresolved_socials(article: Article) -> bool:
    """Whether an article has any social channels that failed or are pending."""
    statuses = article.platform_statuses or {}
    for code, entry in statuses.items():
        if code == "wp":
            continue
        if isinstance(entry, dict):
            status = entry.get("status")
            attempts = entry.get("attempts", 0)
            err = str(entry.get("error") or "").lower()
            if code == "vk" and ("214" in err or "500 posts a day" in err):
                continue
            if code == "setka" and "429" in err:
                continue
            if code == "youtube" and (
                "лимит" in err
                or "развивайте свой канал" in err
                or "продвинутые функции" in err
            ):
                continue
            if status in ("failed", "pending") and attempts < MAX_ARTICLE_RETRIES:
                return True
    return False


MAX_RETRY_ARTICLES_PER_RUN: Final[int] = 3


async def _load_ready_articles(
    session: AsyncSession, article_ids: Sequence[int] | None, limit: int
) -> list[Article]:
    """Load articles awaiting publication or social retry, newest first."""
    if article_ids:
        query = (
            select(Article)
            .where(Article.id.in_(list(article_ids)))
            .order_by(Article.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(query)
        return list(result.scalars().all())

    # 1. Fresh articles waiting for initial publication
    query_ready = (
        select(Article)
        .where(Article.status == ArticleStatus.MEDIA_READY)
        .where(Article.retry_count < MAX_ARTICLE_RETRIES)
        .order_by(Article.created_at.desc())
        .limit(limit)
    )
    result_ready = await session.execute(query_ready)
    articles = list(result_ready.scalars().all())

    # 2. Articles already on WordPress but needing social retries (capped at 3-5 per run)
    remaining_slots = limit - len(articles)
    retry_budget = min(remaining_slots, MAX_RETRY_ARTICLES_PER_RUN)
    if retry_budget > 0:
        query_retry = (
            select(Article)
            .where(Article.status == ArticleStatus.WP_PUBLISHED)
            .where(Article.retry_count < MAX_ARTICLE_RETRIES)
            .order_by(Article.created_at.desc())
            .limit(50)
        )
        result_retry = await session.execute(query_retry)
        retry_count = 0
        for cand in result_retry.scalars().all():
            if _has_unresolved_socials(cand):
                articles.append(cand)
                retry_count += 1
                if retry_count >= retry_budget:
                    break

    return articles


async def run_publisher(
    *,
    article_ids: Sequence[int] | None = None,
    limit: int = MAX_ARTICLES_PER_RUN,
) -> PublisherRunReport:
    """Publish up to ``limit`` ready articles and report on each.

    Fenced by a global run lock plus a per-article lock, both non-blocking: an
    overlapping tick is skipped rather than queued, because by the time a waiter
    started, the run it waited for would already have published its batch.
    """
    started = datetime.now(UTC)
    report = PublisherRunReport()

    async with AsyncExitStack() as stack:
        redis: Redis = get_redis()
        stack.push_async_callback(redis.aclose)

        run_lock = await stack.enter_async_context(
            acquire_lock(redis, PUBLISHER_RUN_LOCK_KEY, ttl=PUBLISHER_JOB_TIMEOUT)
        )
        if not run_lock:
            logger.info("A publisher run is already in progress; skipping this tick")
            report.lock_skipped = True
            return report

        session: AsyncSession = AsyncSessionLocal()
        stack.push_async_callback(session.close)

        from app.services.wp_connection import get_active_wp_publisher  # noqa: PLC0415

        publisher = await get_active_wp_publisher(session)
        stack.push_async_callback(publisher.close)
        from app.services.dzen_publisher import (  # noqa: PLC0415
            DzenBrowserPublisher,
            load_dzen_session_config,
        )

        dzen_cfg = await load_dzen_session_config(session)
        dzen_browser_pub = (
            DzenBrowserPublisher(dzen_cfg)
            if (dzen_cfg.is_active and dzen_cfg.has_session)
            else None
        )
        from app.services.social_browser_publisher import (  # noqa: PLC0415
            load_all_social_browser_configs,
        )
        from app.services.vk_video_service import (  # noqa: PLC0415
            load_vk_video_config,
            probe_vk_browser_session,
        )

        vk_cfg = await load_vk_video_config(session)
        if (
            vk_cfg.is_active
            and vk_cfg.has_session
            and (not vk_cfg.has_token or vk_cfg.status == "error")
        ):
            await probe_vk_browser_session(vk_cfg, session=session)
        social_browser_cfgs = await load_all_social_browser_configs(session)
        active_vk_token = (
            vk_cfg.access_token
            if (
                vk_cfg.is_active
                and vk_cfg.status not in ("error", "needs_token", "not_configured")
            )
            else ""
        )
        if not active_vk_token and vk_cfg.is_active:
            logger.info(
                "VK publishing skipped: no access token available (status=%s). "
                "Add Community Access Key in settings.",
                vk_cfg.status,
            )

        from app.services.telegram_connection import (  # noqa: PLC0415
            load_telegram_channel_config,
        )

        tg_cfg = await load_telegram_channel_config(session)
        tg_bot_token = (
            tg_cfg.bot_token if (tg_cfg.is_active and tg_cfg.has_token) else ""
        )
        tg_channel_id = (
            tg_cfg.channel_id if (tg_cfg.is_active and tg_cfg.has_channel) else ""
        )

        adapters = build_adapters(
            dzen_browser_publisher=dzen_browser_pub,
            vk_access_token=active_vk_token,
            vk_group_id=vk_cfg.group_id,
            social_browser_configs=social_browser_cfgs,
            tg_bot_token=tg_bot_token,
            tg_channel_id=tg_channel_id,
        )
        for adapter in adapters:
            stack.push_async_callback(adapter.close)

        articles = await _load_ready_articles(session, article_ids, limit)
        pending_items = [(a.id, a) for a in articles]
        logger.info("Publisher run starting for %d article(s)", len(pending_items))

        for aid, article_obj in pending_items:
            async with acquire_lock(
                redis, article_lock_key(aid), ttl=PUBLISHER_JOB_TIMEOUT
            ) as article_lock:
                if not article_lock:
                    logger.info(
                        "Article %s is already being published; skipping", aid
                    )
                    continue
                try:
                    article = (
                        await session.get(Article, aid)
                        if isinstance(session, AsyncSession)
                        else article_obj
                    )
                    if article is None:
                        continue
                    report.publications.append(
                        await publish_article_everywhere(
                            session,
                            article,
                            publisher=publisher,
                            adapters=adapters,
                            allow_social_retry=True,
                        )
                    )
                except Exception as exc:
                    # One article must not abort the batch.
                    await session.rollback()
                    logger.exception(
                        "Article %s aborted the publisher run", aid
                    )
                    report.publications.append(
                        ArticlePublication(
                            article_id=aid,
                            status="failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )

    elapsed = (datetime.now(UTC) - started).total_seconds()
    report.duration_ms = max(int(elapsed * 1000), 0)
    logger.info(
        "Publisher run finished: %d distributed, %d wp-only, %d failed in %d ms",
        report.distributed,
        report.wp_published,
        report.failed,
        report.duration_ms,
    )
    return report


async def publisher_task(
    ctx: dict[str, Any],
    *,
    article_ids: Sequence[int] | None = None,
    limit: int = MAX_ARTICLES_PER_RUN,
) -> dict[str, Any]:
    """ARQ entry point: run one publisher pass and return it as plain JSON.

    A thin wrapper by design — every decision lives in :func:`run_publisher`, so the
    task itself has nothing to test. ``article_ids`` lets the dashboard publish one
    article through the same code path as the cron tick.
    """
    # The production-mode switch is checked in the cron task, not in the
    # pipeline function: the lab page and the manual triggers call that
    # function directly and must keep working while production is paused.
    if not await _production_allowed():
        logger.info("Production mode is off: skipping this tick")
        return {"skipped": True, "reason": "pipeline disabled"}

    parsed: list[int] = []
    for raw in article_ids or ():
        try:
            parsed.append(int(raw))
        except (TypeError, ValueError):
            logger.warning("Ignoring unparseable article id %r", raw)
    report = await run_publisher(
        article_ids=parsed or None,
        limit=max(1, min(limit, MAX_ARTICLES_PER_RUN)),
    )
    with suppress(Exception):
        from app.services.social_browser_publisher import (  # noqa: PLC0415
            keep_active_browser_sessions_alive,
        )

        await keep_active_browser_sessions_alive()

    return report.to_dict()


async def keepalive_sessions_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """Dedicated cron job firing every 5 minutes to keep browser sessions hot."""
    from app.services.social_browser_publisher import (  # noqa: PLC0415
        keep_active_browser_sessions_alive,
    )

    try:
        results = await keep_active_browser_sessions_alive(max_idle_hours=0.08)
        return {"status": "ok", "results": results}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Keepalive cron failed: %s", exc)
        return {"status": "error", "error": str(exc)}


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

    logger.info("Publisher worker starting (env=%s)", settings.app_env)
    install_log_handler()
    ctx["redis_client"] = get_redis()
    get_wp_publisher()


async def shutdown(ctx: dict[str, Any]) -> None:
    """Release every long-lived client this worker touches."""
    client = ctx.pop("redis_client", None)
    if client is not None:
        await client.aclose()
    await close_wp_publisher()
    await close_wp_media_service()
    await close_redis_pool()
    logger.info("Publisher worker stopped")


class WorkerSettings:
    """ARQ worker definition (``arq app.workers.publisher_worker.WorkerSettings``).

    Separate from the content worker because the two are bounded by different
    resources: content generation by the AI budget, publishing by four platforms'
    rate limits. Sharing a process would let a long generation delay an article that
    is ready to go live.
    """

    redis_settings: Final[RedisSettings] = RedisSettings.from_dsn(
        str(settings.redis_url)
    )
    #: Dedicated queue: see :data:`PUBLISHER_QUEUE_NAME`.
    queue_name: Final[str] = PUBLISHER_QUEUE_NAME
    functions: Final[Sequence[WorkerCoroutine]] = (
        publisher_task,
        keepalive_sessions_task,
    )
    cron_jobs: Final[Sequence[CronJob]] = (
        cron(
            publisher_task,
            name=PUBLISHER_TASK_NAME,
            minute=set(PUBLISHER_CRON_MINUTES),
            second=0,
            run_at_startup=False,
            max_tries=1,
            timeout=PUBLISHER_JOB_TIMEOUT,
        ),
        cron(
            keepalive_sessions_task,
            name="keepalive_sessions_task",
            minute=set(range(0, 60, 5)),
            second=30,
            run_at_startup=True,
            max_tries=1,
            timeout=120,
        ),
    )
    on_startup: Final[StartupShutdown] = startup
    on_shutdown: Final[StartupShutdown] = shutdown
    #: One job at a time: four social platforms all rate-limit per account, so two
    #: concurrent batches would throttle each other rather than double throughput.
    max_jobs: Final[int] = 1
    job_timeout: Final[int] = PUBLISHER_JOB_TIMEOUT
    keep_result: Final[int] = 3600
    #: Refresh the `<queue>:health-check` key every 30s. ARQ's 3600s default would
    #: leave a stale key valid for an hour, so `arq --check` — and therefore the
    #: container healthcheck — would keep reporting a wedged worker as healthy.
    #: The key's TTL is this interval plus one second, so it expires promptly once
    #: the main loop stops turning.
    health_check_interval: Final[int] = 30
