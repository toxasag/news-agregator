# ruff: noqa: E501
"""Dashboard routes: pipeline overview, articles, sources, settings and logs.

Server-rendered Jinja2 with HTMX for interactivity and Tailwind from a CDN — no
node_modules, no build step, per the stack constraint in ``AGENTS.md``. The whole
dashboard is one operator's control panel, so it optimises for being obvious and
cheap to run rather than for front-end sophistication.

Two conventions run through every route here:

**Pages redirect, partials 401.** Full-page routes depend on
:func:`~app.api.auth.require_admin_page`, which bounces an unauthenticated browser
to the login form. HTMX fragment routes depend on
:func:`~app.api.auth.get_current_admin_user`, which returns 401 — an HTML redirect
would otherwise be pasted into whatever table cell triggered the request.

**Counters are aggregated in SQL, never in Python.** The overview needs counts per
status across four tables; fetching rows to count them would read the entire
pipeline history into memory on every page load. Each panel is one ``GROUP BY``.

The templates live in ``app/templates/dashboard/`` and the shared Jinja environment
is created once at import: compiling templates per request is measurable, and this
page is polled.
"""

from __future__ import annotations

# The operator-facing strings in the HTMX action handlers are Russian, matching the
# dashboard templates. Cyrillic characters that resemble Latin ones are the subject
# matter here, not a typo, so RUF001 would flag every message.
# ruff: noqa: RUF001
import contextlib
import html
import logging
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final

import httpx
from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi import (
    status as http_status,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from redis.exceptions import RedisError
from sqlalchemy import BigInteger, desc, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import (
    AdminUser,
    clear_session_cookie,
    get_current_admin_user,
    require_admin_page,
)
from app.config import settings
from app.core.security import get_secret_box
from app.database import get_db
from app.models.ai_provider_key import AIProviderKey
from app.models.article import Article
from app.models.cluster import NewsCluster
from app.models.enums import (
    ArticleStatus,
    DedupStatus,
    MediaStrategy,
    Platform,
    SourceType,
)
from app.models.lab_attempt import LabRunItem
from app.models.lab_run import LabRun
from app.models.promoted_project import PromotedProject
from app.models.raw_news import RawNews
from app.models.seo_optimization import SeoOptimization
from app.models.source import Source
from app.models.telegram_account import TelegramAccount
from app.services.admin_credentials import (
    MIN_PASSWORD_LENGTH,
    CredentialError,
    change_password,
    change_username,
    load_credentials,
)
from app.services.ai_client import get_ai_client
from app.services.article_journals import load_article_journals
from app.services.audience_analytics import (
    AudienceAnalyticsReport,
    AudienceAnalyticsService,
)
from app.services.categories import (
    CATEGORY_MAP,
    CATEGORY_TITLES,
    DEFAULT_CATEGORY,
    category_badge,
    category_title,
    wp_category_id,
)
from app.services.gemini_key_pool import (
    auto_select_best_models,
    batch_add_keys_for_provider,
    delete_key_from_pool,
    get_working_models_catalog,
    list_all_provider_pools,
    list_gemini_keys,
    test_all_keys_in_pool,
    test_all_models_across_all_keys,
    test_all_models_for_key,
    test_individual_model_by_id,
    test_key_in_pool,
    test_top_models_for_key,
)
from app.services.incidents import get_active_incidents
from app.services.lab_pipeline import (
    classify_cluster_category,
    create_custom_rubric,
    generate_editorial_image_prompt,
    generate_fallback_image_for_cluster,
    get_cluster_detail,
    get_lab_system_status,
    get_news_groups_summary,
    run_cluster_generate_step,
    run_cluster_media_step,
    run_cluster_publish_step,
    run_cluster_research_step,
    unblock_raw_post,
)
from app.services.model_pool import (
    clear_cooldowns,
    cooldown_state,
    probe_models,
)
from app.services.model_routing import (
    CHAIN_LENGTH,
    CHAT_ROLES,
    MODEL_ROLES,
    ModelError,
    load_chains,
    load_model_routing,
    save_chains,
    save_model_routing,
)
from app.services.overview_analytics import get_overview_data
from app.services.pipeline_control import (
    DEFAULT_INTERVAL_MINUTES,
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    ScheduleError,
    load_control,
    save_control,
)
from app.services.pipeline_inspection import (
    run_inspection,
)
from app.services.pipeline_reset import (
    ResetBlockedError,
    UnsafeWipeTargetError,
    delete_day_from_queue,
    preview_pipeline_wipe,
    reset_pipeline_queue,
    wipe_pipeline_history,
)
from app.services.pipeline_runner import PipelineRunner
from app.services.pipeline_stages import ArtifactStore, RunMode
from app.services.promoted_projects_service import PromotedProjectsService
from app.services.seo_analytics import (
    DEFAULT_WINDOW_DAYS as SEO_DEFAULT_WINDOW_DAYS,
)
from app.services.seo_analytics import (
    MAX_CTR_FOR_OPTIMISATION as SEO_MAX_CTR,
)
from app.services.seo_analytics import (
    POSITION_WINDOW as SEO_POSITION_WINDOW,
)
from app.services.seo_analytics import (
    collect_seo_report,
    collect_seo_report_from_db,
)
from app.services.seo_learning import (
    apply_snippet_to_wordpress_and_track,
    evaluate_pending_optimizations,
    generate_optimized_snippet,
    get_learned_patterns,
    rollback_snippet_optimization,
)
from app.services.telegram_accounts import (
    TelegramAccountPool,
    account_state,
)
from app.services.telegram_auth import TelegramAuthError, build_client
from app.services.telegram_ingestor import TelegramIngestorError
from app.services.telegram_login import (
    abandon_auth,
    start_phone_auth,
    start_qr_auth,
    submit_pending_code,
    submit_pending_password,
    take_completed,
)
from app.services.telegram_mtproto import (
    DialogDiscovery,
    MTProtoIngestor,
    kind_label,
)
from app.services.vk_video_service import (
    VKError,
    upload_article_video,
)
from app.services.web_scraper import (
    NavigationTimeoutError,
    ScraperError,
    test_crawl_url,
)
from app.services.wp_media_service import get_wp_media_service
from app.workers.ingestion_worker import run_ingestion, telegram_watermark

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "TEMPLATES_DIR",
    "pages_router",
    "partials_router",
    "templates",
]

TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "templates"

#: Telegram usernames: a leading letter, then letters/digits/underscore, 5-32
#: characters overall. Used to tell a public handle apart from a ``-100…`` peer
#: id when rebuilding a link for a legacy row.
_TELEGRAM_USERNAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")

#: ``peer_kind`` marker for a row the importer retired because another row is the
#: same Telegram peer. Retired rather than deleted: ``raw_news.source_id``
#: cascades, so deleting would take the collected posts with it.
_DUPLICATE_PEER_KIND: Final[str] = "merged_duplicate"

#: "Not a merged duplicate" as SQL. Written with an explicit NULL check because
#: `peer_kind != 'merged_duplicate'` is NULL — and therefore false — for every
#: row imported before `peer_kind` existed, which would hide them all.
_NOT_MERGED_DUPLICATE: Final[Any] = or_(
    Source.peer_kind.is_(None), Source.peer_kind != _DUPLICATE_PEER_KIND
)

#: Autoescaping is on by default in `Jinja2Templates`, which is what keeps a
#: source name or an article title from becoming markup in the page.
templates: Final[Jinja2Templates] = Jinja2Templates(
    directory=str(TEMPLATES_DIR / "dashboard")
)

# Registered as a global rather than threaded through every route's context: the
# layout needs it on every page, and a forgotten key would raise at render time.
templates.env.globals["env"] = settings.app_env
# Category helpers: every list that shows a rubric needs its Russian title
# and badge colour, and threading them through each route's context invited a
# forgotten key to raise at render time.
templates.env.globals["category_title"] = category_title
templates.env.globals["category_badge"] = category_badge

#: Rows per page in the articles table, per the specification. Twenty fills
#: a laptop viewport with thumbnails without making the render wait on twenty
#: cover lookups.
ARTICLES_PAGE_SIZE: Final[int] = 20


#: Third-party credentials the settings page reports on. The value itself is never
#: rendered — only whether it is configured and a masked tail.
_MANAGED_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("OpenRouter", "openrouter_api_key"),
    ("Fal AI (Flux)", "fal_key"),
    ("GramJS bridge", "gramjs_token"),
    ("VK", "vk_access_token"),
    ("OK", "ok_access_token"),
    ("Dzen", "dzen_token"),
    ("Telegram Bot", "telegram_bot_token"),
    ("WordPress", "wp_app_password"),
    ("Yandex Metrika", "yandex_metrika_token"),
)

pages_router: Final[APIRouter] = APIRouter(tags=["dashboard"])
partials_router: Final[APIRouter] = APIRouter(prefix="/api/ui", tags=["dashboard"])

PageAdmin = Annotated[AdminUser, Depends(require_admin_page)]
PartialAdmin = Annotated[AdminUser, Depends(get_current_admin_user)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


async def _count_by(session: AsyncSession, column: Any) -> dict[str, int]:
    """Return ``{value: count}`` for one enum column.

    A single ``GROUP BY`` per panel. Counting in Python would mean selecting every
    row of ``raw_news`` — the highest-volume table in the schema — on every page
    load.
    """
    result = await session.execute(select(column, func.count()).group_by(column))
    counts: dict[str, int] = {}
    for value, total in result.all():
        key = value.value if hasattr(value, "value") else str(value)
        counts[key] = int(total)
    return counts


async def _pipeline_stats(session: AsyncSession) -> dict[str, Any]:
    """Gather the overview counters."""
    raw_total = await session.scalar(select(func.count()).select_from(RawNews))
    cluster_total = await session.scalar(select(func.count()).select_from(NewsCluster))
    article_total = await session.scalar(select(func.count()).select_from(Article))
    source_total = await session.scalar(select(func.count()).select_from(Source))
    unprocessed_stmt = (
        select(func.count(RawNews.id))
        .select_from(RawNews)
        .outerjoin(NewsCluster, RawNews.cluster_id == NewsCluster.id)
        .where(
            RawNews.is_processed.is_(False),
            or_(
                RawNews.cluster_id.is_(None),
                NewsCluster.is_published.is_(False),
            ),
        )
    )
    unprocessed = await session.scalar(unprocessed_stmt)
    return {
        "raw_news_total": int(raw_total or 0),
        "raw_news_unprocessed": int(unprocessed or 0),
        "clusters_total": int(cluster_total or 0),
        "articles_total": int(article_total or 0),
        "sources_total": int(source_total or 0),
        "dedup_status": await _count_by(session, RawNews.dedup_status),
        "article_status": await _count_by(session, Article.status),
        "article_statuses": [status.value for status in ArticleStatus],
        "dedup_statuses": [status.value for status in DedupStatus],
        "platforms": [platform.value for platform in Platform],
    }


async def _worker_health(session: AsyncSession) -> list[dict[str, str]]:
    """Report which pipeline stages are configured to run.

    This is a *configuration* view, not a liveness probe: the dashboard process
    cannot see whether a separate ARQ container is alive without a heartbeat key,
    and claiming otherwise would be worse than saying nothing. Labelled honestly so
    an operator does not read it as uptime.

    Ingestion is judged by the account pool, not by ``GRAMJS_TOKEN``. Since the
    pool became the primary transport, a deployment with authorised accounts and
    no bridge token was reported as unconfigured while collecting perfectly well —
    the panel was describing a fallback nobody uses.
    """
    pool = TelegramAccountPool(session)
    try:
        usable = len(await pool.available_accounts())
        total = len(await pool.all_accounts())
    except SQLAlchemyError:
        logger.warning("Could not read the Telegram account pool", exc_info=True)
        usable, total = 0, 0

    if usable:
        ingestion_state = f"{usable} аккаунт(ов) готово"
    elif total:
        # Accounts exist but none can be used: a flood wait or a revoked session.
        # Distinguished from "none configured" because the operator action differs.
        ingestion_state = f"{total} аккаунт(ов), ни один не готов"
    elif settings.gramjs_token:
        ingestion_state = "через GramJS-мост"
    else:
        ingestion_state = "нет аккаунтов Telegram"

    return [
        {
            "name": "Ingestion",
            "detail": "MTProto-пул + Playwright",
            "state": ingestion_state,
        },
        {
            "name": "Content",
            "detail": "generation, QA, embeddings",
            "state": "configured" if settings.has_ai_credentials else "no AI key",
        },
        {
            "name": "Media",
            "detail": "OCR + Flux covers",
            "state": "configured" if settings.fal_key else "no Fal key",
        },
        {
            "name": "Publisher",
            "detail": "WordPress + social fan-out",
            "state": "configured" if settings.wp_base_url else "no WP base URL",
        },
    ]


@pages_router.get("/", response_class=HTMLResponse, summary="Pipeline overview")
async def dashboard_index(  # noqa: PLR0917
    request: Request,
    admin: PageAdmin,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Render the pipeline overview."""
    overview = await get_overview_data(
        session,
        days=days,
        date_from=date_from,
        date_to=date_to,
    )
    stats = await _pipeline_stats(session)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "admin": admin,
            "overview": overview,
            "stats": stats,
            "workers": await _worker_health(session),
            "days_filter": days,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "active": "index",
        },
    )


@pages_router.get("/articles", response_class=HTMLResponse, summary="Articles editor")
async def dashboard_articles(  # noqa: PLR0917 - FastAPI injects each argument
    # by name; the count is the filter set, not a wide call signature.
    request: Request,
    admin: PageAdmin,
    session: SessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    period: str = "all",
    media: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Render the articles table with its filters and pagination."""
    listing = await _load_articles(
        session,
        status_filter=status_filter,
        search=search,
        category=category,
        period=period,
        media=media,
        page=page,
    )
    return templates.TemplateResponse(
        request,
        "articles.html",
        {
            "admin": admin,
            # The page renders the same block partial the HTMX requests use, so
            # both paths need the prepared row contexts rather than ORM objects.
            "article_rows": [_article_row(a) for a in listing.rows],
            "categories": sorted(CATEGORY_MAP),
            "page": listing.page,
            "total_pages": listing.total_pages,
            "total_count": listing.total_count,
            "statuses": [status.value for status in ArticleStatus],
            "status_filter": status_filter or "",
            "search": search or "",
            "category_filter": category or "",
            "period_filter": period,
            "media_filter": media or "",
            "active": "articles",
        },
    )


_PERIODS: Final[dict[str, timedelta | None]] = {
    "24h": timedelta(hours=24),
    "3d": timedelta(days=3),
    "all": None,
}


@dataclass(frozen=True, slots=True)
class ArticleListing:
    """One page of articles plus everything the pager template needs."""

    rows: list[Article]
    page: int
    total_pages: int
    total_count: int


async def _load_articles(
    session: AsyncSession,
    *,
    status_filter: str | None,
    search: str | None,
    category: str | None = None,
    period: str = "all",
    media: str | None = None,
    page: int = 1,
) -> ArticleListing:
    """Load one page of articles, newest first.

    Every filter degrades to "no filter" when its value is unknown rather than
    rejecting the request: the values arrive from a query string an operator can
    edit, and a 422 on a mistyped filter is a worse experience than showing
    everything.

    The count runs against the same predicates so the pager reflects what the
    filters actually leave, not the unfiltered total.
    """
    query = select(Article)
    if status_filter:
        valid = {status.value for status in ArticleStatus}
        if status_filter in valid:
            query = query.where(Article.status == ArticleStatus(status_filter))
    if search and search.strip():
        # Bound as a parameter, so `%` or `_` in the operator's text is data
        # rather than a wildcard injected into the SQL.
        query = query.where(Article.title.ilike(f"%{search.strip()}%"))
    if category:
        known = set(CATEGORY_MAP)
        if category in known:
            query = query.where(Article.category == category)
    window = _PERIODS.get(period, _PERIODS["all"])
    if window is not None:
        query = query.where(Article.created_at >= datetime.now(UTC) - window)
    if media == "yes":
        query = query.where(Article.wp_image_id.is_not(None))
    elif media == "no":
        query = query.where(Article.wp_image_id.is_(None))

    total = await session.execute(select(func.count()).select_from(query.subquery()))
    total_count = int(total.scalar_one())

    total_pages = max(1, -(-total_count // ARTICLES_PAGE_SIZE))
    page = min(max(page, 1), total_pages)

    paged = query.order_by(Article.created_at.desc()).limit(ARTICLES_PAGE_SIZE)
    if page > 1:
        paged = paged.offset((page - 1) * ARTICLES_PAGE_SIZE)

    result = await session.execute(paged)
    return ArticleListing(
        rows=list(result.scalars().all()),
        page=page,
        total_pages=total_pages,
        total_count=total_count,
    )


@pages_router.get("/sources", response_class=HTMLResponse, summary="Sources manager")
async def dashboard_sources(
    request: Request, admin: PageAdmin, session: SessionDep
) -> HTMLResponse:
    """Render the Telegram channel manager and the crawler selector builder."""
    result = await session.execute(select(Source).order_by(Source.name))
    sources = list(result.scalars().all())
    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "admin": admin,
            # `rows` is the name the table partial iterates, so the initial
            # render and every HTMX re-render share one contract.
            "rows": [
                await _source_row(session, source)
                for source in sources
                if source.source_type is SourceType.TELEGRAM
                and source.peer_kind != _DUPLICATE_PEER_KIND
            ],
            "website_sources": [
                source for source in sources if source.source_type is SourceType.WEBSITE
            ],
            "telegram_accounts": await _telegram_account_rows(session),
            "active": "sources",
        },
    )


async def _telegram_account_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Build the Telegram account panel's rows.

    Each row carries the account plus its derived state and pre-formatted
    timestamps. Formatting here rather than in the template keeps the timezone
    conversion in one place, and deliberately omits ``session_string``: that value
    is a full account credential and must never reach a rendered page.
    """
    pool = TelegramAccountPool(session)
    accounts = await pool.all_accounts()
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for account in accounts:
        rows.append(
            {
                "account": account,
                "state": account_state(account, now=now).value,
                "failure_count": account.failure_count,
                "last_used": (
                    account.last_used_at.strftime("%d.%m %H:%M")
                    if account.last_used_at
                    else ""
                ),
                "flood_wait_until": (
                    account.flood_wait_until.strftime("%H:%M")
                    if account.flood_wait_until
                    else ""
                ),
            }
        )
    return rows

async def _load_settings_services_context(session: AsyncSession) -> dict[str, Any]:
    """Load unified service connections, keys, and control state for settings views."""
    try:
        box = get_secret_box()
    except ValueError:
        # An unconfigured FERNET_KEY must not make the settings page unreachable —
        # that page is where an operator would go to understand the problem.
        box = None

    from app.services.bing_connection import load_bing_connection  # noqa: PLC0415
    from app.services.dzen_publisher import load_dzen_session_config  # noqa: PLC0415
    from app.services.google_connection import load_google_connection  # noqa: PLC0415
    from app.services.social_browser_publisher import (  # noqa: PLC0415
        load_all_social_browser_configs,
    )
    from app.services.telegram_connection import (  # noqa: PLC0415
        load_telegram_channel_config,
    )
    from app.services.vk_video_service import load_vk_video_config  # noqa: PLC0415
    from app.services.wp_connection import load_wp_connection  # noqa: PLC0415
    from app.services.yandex_connection import load_yandex_connection  # noqa: PLC0415

    wp_conn = await load_wp_connection(session)
    yandex_conn = await load_yandex_connection(session)
    google_conn = await load_google_connection(session)
    bing_conn = await load_bing_connection(session)
    dzen_config = await load_dzen_session_config(session)
    vk_conn = await load_vk_video_config(session)
    telegram_conn = await load_telegram_channel_config(session)
    social_browser_cfgs = await load_all_social_browser_configs(session)

    keys: list[dict[str, str]] = []
    for label, attribute in _MANAGED_KEYS:
        raw = str(getattr(settings, attribute, "") or "")
        if not raw and attribute == "vk_access_token" and vk_conn.has_token:
            raw = vk_conn.access_token
        if not raw:
            keys.append({"label": label, "env": attribute.upper(), "masked": ""})
            continue
        # Values in the environment are plaintext; only values an operator saves
        # through this page are Fernet-encrypted at rest.
        masked = f"{'•' * 8}{raw[-4:]}" if len(raw) > 8 else "•" * 8
        keys.append({"label": label, "env": attribute.upper(), "masked": masked})

    control = await load_control(session)

    return {
        "control": control,
        "keys": keys,
        "encryption_ready": box is not None,
        "wp_conn": wp_conn.to_public_dict(),
        "yandex_conn": yandex_conn.to_public_dict(),
        "dzen_config": dzen_config,
        "google_conn": google_conn.to_public_dict(),
        "bing_conn": bing_conn.to_public_dict(),
        "vk_conn": vk_conn.to_public_dict(),
        "telegram_conn": telegram_conn,
        "social_browser_conns": [
            cfg.to_public_dict() for cfg in social_browser_cfgs.values()
        ],
    }


@pages_router.get("/settings", response_class=HTMLResponse, summary="Settings")
async def dashboard_settings(
    request: Request, admin: PageAdmin, session: SessionDep
) -> HTMLResponse:
    """Render the model matrix and the API key manager.

    Keys are shown **masked**. The page reports whether each credential is
    configured and the last four characters so an operator can tell which key is
    installed, without putting the credential into a page that could be
    screenshotted, cached by a proxy or captured in a browser session recording.
    """
    ctx = await _load_settings_services_context(session)

    routing = await load_model_routing(session)
    chains = await load_chains(session)

    # The catalogue is best-effort: an unreachable gateway must not make the one
    # page an operator would use to fix it unrenderable.
    catalogue: list[str] = []
    if settings.has_ai_credentials:
        client = get_ai_client()
        catalogue = await client.list_models()

    ctx.update(
        {
            "admin": admin,
            # Built from MODEL_ROLES rather than the old _MODEL_MATRIX so the
            # form field names, the stored document and the settings attributes
            # cannot drift apart: one definition drives all three.
            "matrix": [
                {
                    "role": role,
                    "task": label,
                    "env": attribute.upper(),
                    "model": routing.for_role(role),
                    "default": getattr(settings, attribute, ""),
                    "overridden": role in routing.overridden,
                }
                for role, attribute, label in MODEL_ROLES
            ],
            "catalogue": catalogue,
            "chain_rows": [
                {"role": role, "label": label, "models": chains.get(role, [])}
                for role, _attribute, label in MODEL_ROLES
            ],
            "chain_length": CHAIN_LENGTH,
            "interval_min": MIN_INTERVAL_MINUTES,
            "interval_max": MAX_INTERVAL_MINUTES,
            "active": "settings",
        }
    )
    return templates.TemplateResponse(request, "settings.html", ctx)


@pages_router.get(
    "/settings/ai",
    response_class=HTMLResponse,
    summary="AI provider settings and key pool",
)
async def dashboard_ai_settings(
    request: Request, admin: PageAdmin, session: SessionDep
) -> HTMLResponse:
    """Render dedicated AI Settings page with multi-provider key pools."""
    pools = await list_all_provider_pools(session)
    keys = await list_gemini_keys(session)
    routing = await load_model_routing(session)
    catalog = await get_working_models_catalog(session)
    chains = await load_chains(session)
    from app.services.usage_log import get_system_usage_report  # noqa: PLC0415
    usage_report = await get_system_usage_report(session, days=7)
    return templates.TemplateResponse(
        request,
        "ai_settings.html",
        {
            "admin": admin,
            "pools": pools,
            "keys": keys,
            "routing": routing,
            "catalog": catalog,
            "chains": chains,
            "usage_report": usage_report,
            "active": "ai_settings",
        },
    )


@pages_router.get(
    "/ads",
    response_class=HTMLResponse,
    summary="Commercial advertorials and promoted projects",
)
async def dashboard_ads(
    request: Request, admin: PageAdmin, session: SessionDep
) -> HTMLResponse:
    """Render ads and commercial integration settings with live Metrika goal tracking."""
    projects = await PromotedProjectsService.get_all_projects(session)
    active_count = sum(1 for p in projects if p.is_active)
    categories = CATEGORY_TITLES
    try:
        adv_report = await PromotedProjectsService.collect_advertiser_campaigns_report(
            session, days=28
        )
    except Exception as adv_err:  # noqa: BLE001
        logger.warning("Failed to collect advertiser report for /ads: %s", adv_err)
        adv_report = {
            "campaigns": [],
            "stats_by_project_id": {},
            "cqi_by_project_id": {},
            "connected_counters_count": 0,
            "total_target_visits": 0,
            "total_goal_conversions": 0,
            "shared_token_available": False,
        }
    return templates.TemplateResponse(
        request,
        "ads.html",
        {
            "admin": admin,
            "projects": projects,
            "active_projects_count": active_count,
            "categories": categories,
            "adv_report": adv_report,
            "stats_by_project_id": adv_report.get("stats_by_project_id", {}),
            "cqi_by_project_id": adv_report.get("cqi_by_project_id", {}),
            "shared_token_available": adv_report.get("shared_token_available", False),
            "feedback_by_project": {},
            "open_metrika_project_id": "",
            "active": "ads",
        },
    )


@pages_router.get("/seo", response_class=HTMLResponse, summary="SEO analytics")
async def dashboard_seo(
    request: Request,
    admin: PageAdmin,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=180)] = SEO_DEFAULT_WINDOW_DAYS,
) -> HTMLResponse:
    """Render search performance and the self-learning optimisation queue."""
    report = await collect_seo_report_from_db(session, days=days)

    fresh_pages_map: dict[str, dict[str, Any]] = {}
    for page in report.pages:
        fresh_pages_map[page.url] = {
            "position": page.position,
            "ctr": page.ctr,
            "clicks": page.clicks,
            "impressions": page.impressions,
        }
        fresh_pages_map[page.path] = fresh_pages_map[page.url]

    try:
        await evaluate_pending_optimizations(session, fresh_pages_map)
    except (SQLAlchemyError, ValueError) as eval_err:
        await session.rollback()
        logger.warning("Failed to evaluate pending SEO optimizations: %s", eval_err)

    try:
        stmt_hist = (
            select(SeoOptimization)
            .order_by(desc(SeoOptimization.applied_at))
            .limit(30)
        )
        history = list((await session.execute(stmt_hist)).scalars().all())
    except SQLAlchemyError as hist_err:
        await session.rollback()
        logger.warning("Failed to load SEO optimization history: %s", hist_err)
        history = []

    try:
        patterns = await get_learned_patterns(session)
    except SQLAlchemyError as pat_err:
        await session.rollback()
        logger.warning("Failed to load SEO learned patterns: %s", pat_err)
        patterns = {"wins": [], "losses": [], "avg_title_len": 58, "avg_desc_len": 145}

    try:
        audience_report = await AudienceAnalyticsService.collect_audience_report(
            session, days=days, queries_sample=list(report.queries)
        )
    except Exception as aud_err:  # noqa: BLE001
        if hasattr(session, "rollback"):
            await session.rollback()
        logger.warning("Failed to collect audience analytics: %s", aud_err)
        audience_report = AudienceAnalyticsReport(window_days=days)

    try:
        advertiser_report = (
            await PromotedProjectsService.collect_advertiser_campaigns_report(
                session,
                days=days,
                audience_categories=audience_report.categories,
            )
        )
    except Exception as adv_err:  # noqa: BLE001
        logger.warning("Failed to collect advertiser report for /seo: %s", adv_err)
        advertiser_report = {
            "campaigns": [],
            "stats_by_project_id": {},
            "cqi_by_project_id": {},
            "connected_counters_count": 0,
            "total_target_visits": 0,
            "total_goal_conversions": 0,
            "total_integrated_articles": 0,
            "learning_directives": [],
        }

    return templates.TemplateResponse(
        request,
        "seo.html",
        {
            "admin": admin,
            "report": report,
            "summary": report.to_dict(),
            "queue": [candidate.to_dict() for candidate in report.queue],
            "history": [h.to_dict() for h in history],
            "patterns": patterns,
            "queries": report.queries,
            "audience": audience_report.to_dict(),
            "advertiser_report": advertiser_report,
            "position_window": SEO_POSITION_WINDOW,
            "ctr_ceiling": SEO_MAX_CTR,
            "days": days,
            "active": "seo",
        },
    )


@partials_router.post(
    "/seo/candidates/preview-optimize",
    response_class=HTMLResponse,
    summary="Preview AI-optimized snippet",
)
async def seo_preview_optimize(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    *,
    url: Annotated[str, Form()],
    current_title: Annotated[str, Form()],
    current_description: Annotated[str, Form()] = "",
    position: Annotated[float, Form()] = 0.0,
    ctr: Annotated[float, Form()] = 0.0,
    impressions: Annotated[int, Form()] = 0,
    clicks: Annotated[int, Form()] = 0,
    category: Annotated[str, Form()] = "general",
    focus_keyword: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Generate an improved CTR-optimized snippet infused with learned patterns."""
    generated = await generate_optimized_snippet(
        session,
        url=url,
        current_title=current_title,
        current_description=current_description,
        position=position,
        ctr=ctr,
        impressions=impressions,
        category=category,
        focus_keyword=focus_keyword,
    )
    return templates.TemplateResponse(
        request,
        "partials/seo_optimize_modal.html",
        {
            "admin": admin,
            "url": url,
            "current_title": current_title,
            "current_description": current_description,
            "position": position,
            "ctr": ctr,
            "impressions": impressions,
            "clicks": clicks,
            "category": category,
            "generated": generated,
            "model_used": generated.get("model_used", ""),
            "learned_patterns_applied": generated.get("learned_patterns_applied", []),
        },
    )


@partials_router.post(
    "/seo/candidates/apply-wp",
    response_class=HTMLResponse,
    summary="Apply SEO snippet to WordPress and start learning tracking",
)
async def seo_apply_snippet(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    *,
    url: Annotated[str, Form()],
    new_title: Annotated[str, Form()],
    new_description: Annotated[str, Form()],
    new_focus_keyword: Annotated[str, Form()] = "",
    before_title: Annotated[str, Form()] = "",
    before_description: Annotated[str, Form()] = "",
    before_position: Annotated[float, Form()] = 0.0,
    before_ctr: Annotated[float, Form()] = 0.0,
    before_impressions: Annotated[int, Form()] = 0,
    before_clicks: Annotated[int, Form()] = 0,
    category: Annotated[str, Form()] = "general",
    model_used: Annotated[str, Form()] = "",
    reasoning_hypothesis: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Apply the optimized snippet to WordPress, ping IndexNow, and track in DB."""
    result = await apply_snippet_to_wordpress_and_track(
        session,
        url=url,
        new_title=new_title,
        new_description=new_description,
        new_focus_keyword=new_focus_keyword,
        before_title=before_title,
        before_description=before_description,
        before_position=before_position,
        before_ctr=before_ctr,
        before_impressions=before_impressions,
        before_clicks=before_clicks,
        category=category,
        model_used=model_used,
        reasoning_hypothesis=reasoning_hypothesis,
        applied_by=admin.username,
    )
    return templates.TemplateResponse(
        request,
        "partials/seo_apply_result.html",
        {
            "admin": admin,
            "message": result.get("message", "Оптимизация сохранена."),
            "record": result.get("record", {}),
            "indexnow_pinged": result.get("indexnow_pinged", False),
        },
    )


@partials_router.post(
    "/seo/optimizations/{opt_id}/rollback",
    response_class=HTMLResponse,
    summary="Rollback an applied snippet optimization to baseline",
)
async def seo_rollback_snippet(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    opt_id: int,
) -> HTMLResponse:
    """Revert an applied snippet optimization back to its baseline in WordPress."""
    result = await rollback_snippet_optimization(
        session,
        opt_id,
        reason=f"Откат оператором {admin.username}",
    )
    # Reload history
    stmt_hist = (
        select(SeoOptimization)
        .order_by(desc(SeoOptimization.applied_at))
        .limit(30)
    )
    history = list((await session.execute(stmt_hist)).scalars().all())
    return templates.TemplateResponse(
        request,
        "partials/seo_history_table.html",
        {
            "admin": admin,
            "history": [h.to_dict() for h in history],
            "rollback_message": result.get("message"),
        },
    )


@partials_router.post(
    "/seo/optimise", response_class=HTMLResponse, summary="Queue a snippet refresh"
)
async def seo_optimise(
    request: Request,
    admin: PartialAdmin,
    days: Annotated[int, Query(ge=1, le=180)] = SEO_DEFAULT_WINDOW_DAYS,
) -> HTMLResponse:
    """Queue the underperforming articles for a headline and meta refresh."""
    report = await collect_seo_report(days=days)
    logger.info(
        "%s requested a soft SEO optimisation for %d article(s)",
        admin.username,
        len(report.queue),
    )
    return templates.TemplateResponse(
        request,
        "partials/seo_optimise.html",
        {
            "admin": admin,
            "queue": [candidate.to_dict() for candidate in report.queue],
        },
    )


@partials_router.post(
    "/seo/yandex/recrawl",
    response_class=HTMLResponse,
    summary="Submit URL to Yandex priority recrawl queue (HTMX)",
)
async def seo_yandex_recrawl(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    url: Annotated[str, Form()],
) -> HTMLResponse:
    """Submit a single URL to Yandex Webmaster priority recrawl queue."""
    from app.services.yandex_connection import (  # noqa: PLC0415
        load_yandex_connection,
        recrawl_yandex_url,
    )

    target_url = url.strip()
    if not target_url:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-xs'
            ' text-red-300">Укажите URL для переобхода.</div>'
        )

    y_cfg = await load_yandex_connection(session)
    res = await recrawl_yandex_url(y_cfg, target_url)
    if res.get("ok"):
        quota_rem = res.get("quota_remainder", "н/д")
        task_id = res.get("task_id", "")
        return HTMLResponse(
            '<div class="rounded-lg border border-emerald-500/40 bg-emerald-500/10'
            ' p-3 text-xs text-emerald-300 flex items-center justify-between">'
            f'<span>✅ В очереди Яндекса! ID: <code>{task_id[:8]}...</code></span>'
            f'<span class="font-mono bg-emerald-950/60 px-2 py-0.5 rounded border'
            f' border-emerald-700/50">Остаток квоты: {quota_rem}</span>'
            '</div>'
        )
    err = res.get("error", "Неизвестная ошибка")
    return HTMLResponse(
        '<div class="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-xs'
        f' text-red-300">❌ Ошибка отправки: {err}</div>'
    )


@partials_router.post(
    "/seo/google/inspect",
    response_class=HTMLResponse,
    summary="Inspect URL in Google Search Console (HTMX)",
)
async def seo_google_inspect(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    url: Annotated[str, Form()],
) -> HTMLResponse:
    """Inspect a single URL via Google Search Console URL Inspection API."""
    from app.services.google_connection import (  # noqa: PLC0415
        inspect_google_url,
        load_google_connection,
    )

    target_url = url.strip()
    if not target_url:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-xs'
            ' text-red-300">Укажите URL для проверки.</div>'
        )

    g_cfg = await load_google_connection(session)
    res = await inspect_google_url(g_cfg, target_url)
    if not res.get("ok"):
        err = res.get("error", "Не удалось проверить")
        return HTMLResponse(
            '<div class="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-xs'
            f' text-red-300">❌ Ошибка Google Inspection: {err}</div>'
        )

    verdict = res.get("verdict", "UNKNOWN")
    coverage = res.get("coverage_state", "Неизвестно")
    last_crawl = res.get("last_crawl_time", "Никогда")
    robots = res.get("robots_txt_state", "—")
    indexing = res.get("indexing_state", "—")

    is_pass = verdict in ("PASS", "INDEXED")
    badge_bg = (
        "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
        if is_pass
        else "border-amber-500/40 bg-amber-500/10 text-amber-300"
    )

    return HTMLResponse(
        f'<div class="rounded-lg border {badge_bg} p-3 text-xs space-y-1.5">'
        f'<div class="flex items-center justify-between font-semibold">'
        f'<span>Вердикт Google: {verdict}</span>'
        f'<span class="font-mono text-[10px] text-gray-400">Обход: {last_crawl}</span>'
        f'</div>'
        f'<div class="text-gray-300">Покрытие: <b>{coverage}</b></div>'
        '<div class="text-[11px] text-gray-400 flex flex-wrap gap-x-4">'
        f'<span>Robots: {robots}</span><span>Индексация: {indexing}</span></div>'
        '</div>'
    )


@partials_router.post(
    "/seo/serp/inspect",
    response_class=HTMLResponse,
    summary="Inspect TOP-10 SERP competitors for keyword (HTMX)",
)
async def seo_serp_inspect(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    keyword: Annotated[str, Form()],
) -> HTMLResponse:
    """Inspect top 10 search competitors for a query via WebResearcher."""
    import html as pyhtml  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    from urllib.parse import urlparse  # noqa: PLC0415

    from app.services.web_researcher import WebResearcher  # noqa: PLC0415

    q = keyword.strip()
    if not q:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-xs text-red-300">'
            "Введите поисковый запрос для анализа ТОП-10.</div>"
        )

    try:
        researcher = WebResearcher()
        docs = await researcher.research(q, max_results=10)
        if not docs:
            return HTMLResponse(
                '<div class="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-300">'
                f"По запросу «{pyhtml.escape(q)}» не удалось получить результаты поиска. "
                "Попробуйте переформулировать запрос.</div>"
            )

        items_html = []
        for idx, doc in enumerate(docs[:10], start=1):
            domain = urlparse(doc.url).netloc.removeprefix("www.")
            title = pyhtml.escape(doc.title or "Без заголовка")
            snippet = pyhtml.escape(doc.snippet or "")
            clean_url = pyhtml.escape(doc.url)

            items_html.append(
                '<div class="rounded-lg border border-slate-800 bg-slate-950/70 p-3.5 space-y-1.5 '
                'hover:border-slate-700 transition">'
                '  <div class="flex items-center justify-between gap-2">'
                '    <div class="flex items-center gap-2">'
                '      <span class="w-5 h-5 rounded bg-amber-500/20 text-amber-300 font-bold '
                f'font-mono text-[11px] flex items-center justify-center">#{idx}</span>'
                f'      <span class="text-xs font-mono font-semibold text-sky-400">{domain}</span>'
                "    </div>"
                f'    <a href="{clean_url}" target="_blank" rel="noopener noreferrer" '
                'class="text-[11px] text-slate-400 hover:text-white underline font-mono">открыть ↗</a>'
                "  </div>"
                f'  <div class="text-xs font-bold text-slate-100">{title}</div>'
                f'  <p class="text-[11px] text-slate-300 leading-relaxed">{snippet}</p>'
                "</div>"
            )

        header_msg = (
            f"ТОП-10 конкурентов в поисковой выдаче по запросу: "
            f'<span class="font-bold text-white">«{pyhtml.escape(q)}»</span>'
        )
        count_label = f"Найдено: {len(docs[:10])} сайтов"
        body_items = "".join(items_html)
        tip_text = (
            "Изучите структуру заголовков лидеров и добавьте в статью "
            "более глубокую аналитику с цитатами экспертов и инфографикой."
        )

        encoded_q = urllib.parse.quote(q)
        wordcraft_link = (
            "https://webmaster.yandex.ru/site/https:svodka.press:443/efficiency/wordcraft/"
            f"?device=ALL_DEVICES&query={encoded_q}"
            "&urls=https%3A%2F%2Fsvodka.press&userQueries=SITES&rivals=GENERAL&mode=QUERY&tab=GENERAL"
        )
        detailed_link = (
            "https://webmaster.yandex.ru/site/https:svodka.press:443/efficiency/detailed-analysis/"
        )

        result_html = (
            '<div class="space-y-3 mt-4">'
            '  <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 '
            'border-b border-slate-800 pb-3">'
            f'    <div class="text-xs text-slate-300 font-medium">{header_msg}</div>'
            '    <div class="flex items-center gap-2">'
            f'      <a href="{wordcraft_link}" target="_blank" rel="noopener noreferrer" '
            '         class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded bg-amber-500/15 '
            'hover:bg-amber-500/25 border border-amber-500/30 text-amber-300 font-mono text-[11px] transition">'
            "        <span>📊 В Яндекс.Wordcraft</span> ↗"
            "      </a>"
            f'      <span class="text-[11px] font-mono text-emerald-400">{count_label}</span>'
            "    </div>"
            "  </div>"
            f'  <div class="space-y-2">{body_items}</div>'
            '  <div class="rounded-lg border border-indigo-900/50 bg-indigo-950/30 '
            'p-3 text-xs text-indigo-200 leading-relaxed flex flex-col sm:flex-row items-start '
            'sm:items-center justify-between gap-3">'
            "    <div>"
            '      💡 <span class="font-semibold text-indigo-300">Редакционный вывод для попадания в ТОП-3:</span> '
            f"{tip_text}"
            "    </div>"
            f'    <a href="{detailed_link}" target="_blank" rel="noopener noreferrer" '
            '       class="shrink-0 px-3 py-1.5 rounded bg-indigo-600 hover:bg-indigo-500 text-white '
            'font-medium text-xs transition">'
            "      Детальный анализ конкурентов ↗"
            "    </a>"
            "  </div>"
            "</div>"
        )
        return HTMLResponse(result_html)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SERP inspection failed: %s", exc)
        err_msg = pyhtml.escape(str(exc))
        return HTMLResponse(
            f'<div class="rounded-lg border border-red-500/40 bg-red-500/10 '
            f'p-3 text-xs text-red-300">Ошибка при получении поисковой выдачи: '
            f"{err_msg}</div>"
        )


@partials_router.post(
    "/seo/yandex/sitemap/add",
    response_class=HTMLResponse,
    summary="Add Sitemap to Yandex Webmaster (HTMX)",
)
async def seo_yandex_sitemap_add(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    url: Annotated[str, Form()],
) -> HTMLResponse:
    """Register a new Sitemap in Yandex Webmaster."""
    from app.services.yandex_connection import (  # noqa: PLC0415
        add_yandex_sitemap,
        load_yandex_connection,
    )

    target_url = url.strip()
    if not target_url:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-xs'
            ' text-red-300">Укажите URL карты сайта.</div>'
        )

    y_cfg = await load_yandex_connection(session)
    res = await add_yandex_sitemap(y_cfg, target_url)
    if res.get("ok"):
        sm_id = res.get("sitemap_id", "новый")
        return HTMLResponse(
            '<div class="rounded-lg border border-emerald-500/40 bg-emerald-500/10'
            ' p-3 text-xs text-emerald-300">'
            f'✅ Sitemap успешно добавлен в Яндекс.Вебмастер! ID: <code>{sm_id}</code>'
            '</div>'
        )
    err = res.get("error", "Сбой")
    return HTMLResponse(
        '<div class="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-xs'
        f' text-red-300">❌ Ошибка добавления: {err}</div>'
    )


@pages_router.get("/profile", response_class=HTMLResponse, summary="Admin profile")
async def dashboard_profile(
    request: Request, admin: PageAdmin, session: SessionDep
) -> HTMLResponse:
    """Render the profile page for changing the login and password."""
    credentials = await load_credentials(session)
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "admin": admin,
            "credentials": credentials,
            "min_password_length": MIN_PASSWORD_LENGTH,
            "active": "profile",
        },
    )


@pages_router.get("/logs", response_class=HTMLResponse, summary="Live logs")
async def dashboard_logs(request: Request, admin: PageAdmin) -> HTMLResponse:
    """Render the live SSE terminal."""
    return templates.TemplateResponse(
        request,
        "logs.html",
        {"admin": admin, "active": "logs"},
    )


@pages_router.get("/login", response_class=HTMLResponse, summary="Login form")
async def login_page(
    request: Request,
    error: Annotated[str | None, Query()] = None,
    next_url: Annotated[str | None, Query(alias="next")] = None,
) -> HTMLResponse:
    """Render the login form.

    Unauthenticated by definition — this is the one dashboard page without a guard.
    ``configured`` surfaces a missing ``ADMIN_PASSWORD_HASH`` on the page itself,
    because the alternative is an operator typing a correct password into a form
    that can only ever reject it.

    Returns 401 alongside the form when the previous attempt failed, so the status
    code matches what happened rather than reporting a rejected login as 200.
    """
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": bool(error),
            "next_url": next_url or "/",
            "configured": bool(settings.admin_password_hash.strip()),
            "active": "login",
            "admin": None,
        },
        status_code=401 if error else 200,
    )


# ------------------------------------------------------------------- partials


@partials_router.get(
    "/stats", response_class=HTMLResponse, summary="Overview counters (HTMX)"
)
async def stats_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Return the counter grid and analytics telemetry for HTMX polling.

    A fragment rather than a full page so the overview can refresh its numbers
    without re-rendering the layout, and without the flicker a full reload causes.
    """
    overview = await get_overview_data(
        session,
        days=days,
        date_from=date_from,
        date_to=date_to,
    )
    stats = await _pipeline_stats(session)
    return templates.TemplateResponse(
        request,
        "partials/stats.html",
        {
            "admin": admin,
            "overview": overview,
            "stats": stats,
            "workers": await _worker_health(session),
            "days_filter": days,
            "date_from": date_from or "",
            "date_to": date_to or "",
        },
    )


@partials_router.get(
    "/incidents",
    response_class=HTMLResponse,
    summary="Operational incidents banner (HTMX)",
)
async def incidents_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Return operational incidents banner (429, session expirations) for HTMX."""
    incidents = await get_active_incidents(session)
    return templates.TemplateResponse(
        request,
        "partials/incidents_banner.html",
        {
            "admin": admin,
            "incidents": incidents,
        },
    )


@partials_router.get(
    "/articles/table", response_class=HTMLResponse, summary="Articles block (HTMX)"
)
async def articles_table_partial(  # noqa: PLR0917 - FastAPI injects each
    # argument by name; the count is the filter set.
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    period: str = "all",
    media: Annotated[str | None, Query(alias="media")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Return the whole table-plus-pager block for filtering without a reload."""
    listing = await _load_articles(
        session,
        status_filter=status_filter,
        search=search,
        category=category,
        period=period,
        media=media,
        page=page,
    )
    return templates.TemplateResponse(
        request,
        "partials/articles_block.html",
        {
            "admin": admin,
            "article_rows": [_article_row(a) for a in listing.rows],
            "page": listing.page,
            "total_pages": listing.total_pages,
            "total_count": listing.total_count,
        },
    )


def _extract_ad_name(content_html: str) -> str | None:
    """Detect if an ad/commercial card was injected into content_html."""
    if not content_html:
        return None
    m = re.search(r'data-project-name=["\']([^"\']+)["\']', content_html)
    if m:
        return m.group(1).strip()
    m = re.search(r'class=["\'][^"\']*ad-sponsor[^"\']*["\'][^>]*>([^<]+)<', content_html)
    if m:
        return m.group(1).strip()
    if "erid:" in content_html:
        m = re.search(r'erid:\s*([a-zA-Z0-9_-]+)', content_html)
        return f"Реклама (erid: {m.group(1)})" if m else "Реклама"
    return None


_DISTRIBUTION_CHANNELS: Final[list[tuple[str, str, str]]] = [
    ("wp", "Сайт", "Сайт (WordPress)"),
    ("dzen", "Дзен", "Яндекс Дзен"),
    ("vk", "ВК", "ВКонтакте"),
    ("tg", "TG", "Telegram"),
    ("setka", "Сетка", "Сетка (hh.ru)"),
    ("spark", "Spark", "Spark.ru (Блог стартапа)"),
    ("tenchat", "TenChat", "TenChat"),
    ("youtube", "YouTube", "YouTube Сообщество"),
    ("ok", "ОК", "Одноклассники"),
    ("threads", "Threads", "Threads"),
    ("facebook", "FB", "Facebook"),
    ("pinterest", "Pinterest", "Pinterest"),
]


def _clean_channel_error(raw_error: str) -> str:
    """Format technical error messages into clear user explanations."""
    err_lower = raw_error.lower()
    if any(k in err_lower for k in ("сессия истекла", "перенаправлено", "redirect")):
        return "Сессия истекла — обновите куки в Настройках"
    if "поле ввода" in err_lower or "not found" in err_lower:
        return "Поле ввода не найдено — нужна авторизация владельца"
    if "another ip" in err_lower or "given to another ip" in err_lower:
        return "Токен отклонен по IP — укажите Ключ доступа сообщества"
    if "not configured" in err_lower or "unconfigured" in err_lower:
        return "Площадка не подключена в Настройках"
    return raw_error[:100]


def _parse_channel_item(
    target: tuple[str, str, str],
    article: Article,
    st: dict[str, Any],
    est_time: str = "",
) -> dict[str, Any]:
    """Derive state, URL and reason for a single distribution channel."""
    code, label, full_name = target
    st_status = str(st.get("status") or "")
    url = str(st.get("url") or "")
    raw_error = str(st.get("error") or "")
    published_at = str(st.get("published_at") or "")

    if code == "wp":
        if not url and article.wp_post_id:
            url = f"https://svodka.press/?p={article.wp_post_id}"
        if (
            article.wp_post_id
            or st_status == "published"
            or article.status
            in (ArticleStatus.WP_PUBLISHED, ArticleStatus.DISTRIBUTED)
        ):
            state = "published"
            reason = f"Опубликовано на сайте (ID {article.wp_post_id or ''})".strip()
        elif article.status == ArticleStatus.MEDIA_READY:
            state = "queued"
            reason = f"В очереди на публикацию ({est_time or 'план ~10 мин'})"
        elif article.status == ArticleStatus.FAILED:
            state = "failed"
            reason = f"Сбой публикации: {article.error_message or 'ошибка REST API'}"
        else:
            state = "draft"
            reason = "Черновик (материал ещё не отправлен)"

        return {
            "code": code,
            "label": label,
            "full_name": full_name,
            "state": state,
            "url": url if state == "published" else None,
            "reason": reason,
            "published_at": published_at,
            "is_primary": True,
        }

    # Social networks
    if st_status in ("published", "sent") or (url and not raw_error):
        state = "published"
        reason = f"Опубликовано на {full_name} ✅"
    elif article.status == ArticleStatus.MEDIA_READY:
        state = "queued"
        reason = f"В очереди на публикацию ({est_time or 'план ~10 мин'})"
    elif st_status in ("failed", "error") or (raw_error and st_status != "disabled"):
        state = "failed"
        reason = f"Ошибка: {_clean_channel_error(raw_error)}"
    elif st_status == "disabled" or "not configured" in raw_error.lower():
        state = "disabled"
        reason = "Отключено в Настройках (/settings)"
    elif article.status == ArticleStatus.DRAFT:
        state = "draft"
        reason = "Черновик (материал ещё не готов к отправке)"
    else:
        state = "disabled"
        reason = "Отключено / не настроено"

    is_primary = (
        code in ("wp", "dzen", "vk", "tg", "setka", "tenchat", "youtube", "spark")
        or state in ("published", "failed")
    )
    return {
        "code": code,
        "label": label,
        "full_name": full_name,
        "state": state,
        "url": url if state == "published" else None,
        "reason": reason,
        "published_at": published_at,
        "is_primary": is_primary,
    }


def _build_distribution_items(
    article: Article, estimated_publish_time: str = ""
) -> list[dict[str, Any]]:
    """Build status, link and reason details for all distribution channels."""
    statuses = dict(article.platform_statuses or {})
    return [
        _parse_channel_item(
            target,
            article,
            dict(statuses.get(target[0]) or {}),
            estimated_publish_time,
        )
        for target in _DISTRIBUTION_CHANNELS
    ]


def _article_row(article: Article) -> dict[str, Any]:
    """Build one table row's context with published links, promo ads, and stats."""
    statuses = article.platform_statuses or {}
    wp_stat = statuses.get("wp") or {}
    dzen_stat = statuses.get("dzen") or {}
    vk_stat = statuses.get("vk") or {}
    tg_stat = statuses.get("tg") or {}

    wp_url = str(wp_stat.get("url") or "")
    if not wp_url and article.wp_post_id:
        wp_url = f"https://svodka.press/?p={article.wp_post_id}"

    dzen_url = str(dzen_stat.get("url") or "")
    vk_url = str(vk_stat.get("url") or "")
    tg_url = str(tg_stat.get("url") or "")

    # Estimated publish time for queued items
    estimated_time = ""
    if article.status == ArticleStatus.MEDIA_READY:
        now_dt = datetime.now(UTC)
        minute = now_dt.minute
        for slot in (5, 15, 25, 35, 45, 55):
            if slot > minute:
                estimated_time = f"~{now_dt.strftime('%H')}:{slot:02d}"
                break
        if not estimated_time:
            estimated_time = f"~{(now_dt.hour + 1) % 24:02d}:05"

    distribution_items = _build_distribution_items(
        article, estimated_publish_time=estimated_time
    )

    return {
        "id": article.id,
        "title": article.title,
        "status": article.status.value,
        "category_title": category_title(article.category),
        "badge": category_badge(article.category),
        "created_at": (
            article.created_at.strftime("%d.%m.%Y %H:%M") if article.created_at else ""
        ),
        "wp_image_id": article.wp_image_id,
        "wp_post_id": article.wp_post_id,
        "wp_url": wp_url,
        "dzen_url": dzen_url,
        "vk_url": vk_url,
        "tg_url": tg_url,
        "tag_count": len(article.wp_tag_ids or []),
        "platform_statuses": statuses,
        "distribution_items": distribution_items,
        "has_unresolved_socials": (
            article.status == ArticleStatus.WP_PUBLISHED
            and any(
                item.get("state") in ("failed", "queued")
                for item in distribution_items
                if item.get("code") != "wp" and item.get("state") != "disabled"
            )
        ),
        "error_message": article.error_message,
        "ad_name": _extract_ad_name(article.content_html or ""),
        "estimated_publish_time": estimated_time,
    }



@partials_router.get(
    "/articles/{article_id}/edit",
    response_class=HTMLResponse,
    summary="Article edit form (HTMX)",
)
async def article_edit_partial(
    request: Request, admin: PartialAdmin, session: SessionDep, article_id: int
) -> HTMLResponse:
    """Return the inline edit form for one article."""
    article = await session.get(Article, article_id)
    return templates.TemplateResponse(
        request,
        "partials/article_edit.html",
        {"admin": admin, "article": article, "article_id": article_id},
        status_code=200 if article is not None else 404,
    )


# --------------------------------------------------------------- HTMX actions
#
# The dashboard's action buttons live here rather than on the `/api` router,
# because that router is guarded by `require_api_access` (a bearer token holding
# `SECRET_KEY`) which a browser carrying only the session cookie cannot satisfy.
# Pointing HTMX at it produced a 401 that was then pasted into the page.
#
# These endpoints re-use the same service functions as the JSON API — the locking,
# validation and idempotency all still live in one place — and differ only in
# returning an HTML fragment and authenticating from the cookie.


@partials_router.post(
    "/ingestion/run",
    response_class=HTMLResponse,
    summary="Run one ingestion pass (HTMX)",
)
async def ingestion_run_partial(request: Request, admin: PartialAdmin) -> HTMLResponse:
    """Run one ingestion pass inline and report its counters.

    Inline rather than enqueued, matching ``POST /api/pipeline/ingest-now``: the
    operator pressing this wants to know whether the sources actually work, and a
    job id does not answer that.

    Every outcome renders the same fragment, because an HTMX target replaced by a
    raw error body is worse than a sentence saying what happened. A held run lock
    is reported as "already running" rather than as zero counters, which would
    read as "the sources are empty".
    """
    logger.info("%s triggered a manual ingestion pass", admin.username)
    context: dict[str, Any] = {"admin": admin}
    try:
        report = await run_ingestion()
    except RedisError:
        logger.warning("Manual ingestion could not reach Redis", exc_info=True)
        context["error"] = (
            "Redis недоступен: запуск не был выполнен, потому что блокировку "
            "прохода получить нельзя."
        )
    # Deliberately broad: this is a page fragment, and any unhandled failure here
    # would otherwise land in the operator's browser as a traceback. The cause is
    # logged server-side.
    except Exception:
        logger.exception("Manual ingestion failed")
        context["error"] = "Сбор завершился ошибкой. Подробности — в журнале сервера."
    else:
        context["report"] = report.to_dict()
        context["lock_skipped"] = report.lock_skipped
    return templates.TemplateResponse(request, "partials/ingestion_result.html", context)


@partials_router.post(
    "/sources/{source_id}/skip-media",
    response_class=HTMLResponse,
    summary="Toggle skip_media for one source (HTMX)",
)
async def source_skip_media_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    source_id: int,
    skip_media: Annotated[bool, Form()] = False,
) -> HTMLResponse:
    """Set ``skip_media`` on one source.

    An unchecked HTML checkbox submits nothing at all, so the default of ``False``
    is what makes un-ticking the box work: without it the field would be missing
    and the toggle could only ever switch on.

    Scoped to this single column on purpose. Routing the toggle through the full
    upsert would require the template to echo back every other field, and a stale
    page would then silently revert whatever changed in between.
    """
    source = await session.get(Source, source_id)
    if source is None:
        return templates.TemplateResponse(
            request,
            "partials/source_result.html",
            {"admin": admin, "error": f"Источник {source_id} не найден."},
            status_code=404,
        )

    try:
        source.skip_media = skip_media
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.warning("skip_media toggle failed for source %s", source_id)
        return templates.TemplateResponse(
            request,
            "partials/source_result.html",
            {"admin": admin, "error": "База данных недоступна: изменение не сохранено."},
            status_code=503,
        )

    logger.info(
        "%s set skip_media=%s on source %s", admin.username, skip_media, source_id
    )
    return templates.TemplateResponse(
        request,
        "partials/source_result.html",
        {
            "admin": admin,
            "source_name": source.name,
            "skip_media": source.skip_media,
        },
    )


@partials_router.post(
    "/sources/website/test",
    response_class=HTMLResponse,
    summary="Dry-run website selectors (HTMX)",
)
async def website_test_partial(
    request: Request,
    admin: PartialAdmin,
    url: Annotated[str, Form()],
    title_selector: Annotated[str, Form()] = "",
    content_selector: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Open one page with the supplied selectors and show what came back.

    Pure read: it touches neither ``sources`` nor ``raw_news``, and takes no
    database session at all, which is the structural guarantee behind that claim.

    A partial extraction renders as a success with warnings rather than an error,
    because "which selector matched nothing" is the question the selector builder
    exists to answer.
    """
    rules: dict[str, str] = {}
    if title_selector.strip():
        rules["title_selector"] = title_selector.strip()
    if content_selector.strip():
        rules["content_selector"] = content_selector.strip()

    context: dict[str, Any] = {"admin": admin, "url": url}
    try:
        preview = await test_crawl_url(url, rules or None)
    except NavigationTimeoutError:
        context["error"] = (
            "Навигация превысила таймаут: страница не открылась за отведённое время."
        )
    except ScraperError as exc:
        context["error"] = f"Страницу не удалось прочитать: {exc}"
    # Chromium and the Playwright driver fail in ways the scraper does not wrap —
    # a missing browser binary, a driver handshake error. Logged server-side; the
    # operator gets a sentence rather than a traceback naming internal paths.
    except Exception as exc:
        logger.warning("Crawl preview for %s failed", url, exc_info=True)
        context["error"] = (
            f"Краулер не смог обработать страницу ({type(exc).__name__}); "
            "подробности — в журнале сервера."
        )
    else:
        context["preview"] = preview
    return templates.TemplateResponse(request, "partials/selector_preview.html", context)


async def _website_panel(
    request: Request,
    admin: AdminUser,
    session: AsyncSession,
    *,
    message: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    result = await session.execute(
        select(Source)
        .where(Source.source_type == SourceType.WEBSITE)
        .order_by(Source.name)
    )
    return templates.TemplateResponse(
        request,
        "partials/website_sources.html",
        {
            "admin": admin,
            "website_sources": list(result.scalars().all()),
            "website_message": message,
            "website_error": error,
        },
        status_code=status_code,
    )


@partials_router.post(
    "/sources/website/save",
    response_class=HTMLResponse,
    summary="Save a website source (HTMX)",
)
async def website_save_partial(  # noqa: PLR0917 - FastAPI injects by name
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    url: Annotated[str, Form()],
    name: Annotated[str, Form()],
    title_selector: Annotated[str, Form()] = "",
    content_selector: Annotated[str, Form()] = "",
) -> HTMLResponse:
    identifier = url.strip()
    label = name.strip()
    if not identifier or not label:
        return await _website_panel(
            request,
            admin,
            session,
            error="Нужны название и URL ленты.",
            status_code=400,
        )
    rules: dict[str, str] = {}
    if title_selector.strip():
        rules["title_selector"] = title_selector.strip()
    if content_selector.strip():
        rules["content_selector"] = content_selector.strip()
    result = await session.execute(
        select(Source).where(
            Source.source_type == SourceType.WEBSITE, Source.identifier == identifier
        )
    )
    source = result.scalar_one_or_none()
    if source is None:
        session.add(
            Source(
                source_type=SourceType.WEBSITE,
                name=label,
                identifier=identifier,
                is_active=True,
                crawl_rules=rules,
                media_strategy=MediaStrategy.OCR_CHECK,
            )
        )
        created = True
    else:
        source.name = label
        source.is_active = True
        if rules:
            source.crawl_rules = rules
        created = False
    await session.commit()
    logger.info(
        "%s %s website source %s",
        admin.username,
        "created" if created else "updated",
        identifier,
    )
    return await _website_panel(
        request,
        admin,
        session,
        message=("Сайт добавлен." if created else "Сайт обновлён."),
    )


@partials_router.post(
    "/sources/website/{source_id}/deactivate",
    response_class=HTMLResponse,
    summary="Soft-deactivate a website source (HTMX)",
)
async def website_deactivate_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    source_id: int,
) -> HTMLResponse:
    source = await session.get(Source, source_id)
    if source is None or source.source_type is not SourceType.WEBSITE:
        return await _website_panel(
            request,
            admin,
            session,
            error=f"Сайт {source_id} не найден.",
            status_code=404,
        )
    source.is_active = False
    await session.commit()
    logger.info("%s deactivated website source %s", admin.username, source.identifier)
    return await _website_panel(
        request, admin, session, message=f"«{source.name}» выключен."
    )


@partials_router.post(
    "/profile/username",
    response_class=HTMLResponse,
    summary="Change the admin username (HTMX)",
)
async def profile_username_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    new_username: Annotated[str, Form()],
    current_password: Annotated[str, Form()],
) -> HTMLResponse:
    """Rename the administrator account.

    The current password is required: a stolen session cookie must not be enough
    to rename the account, since the rename is precisely what would stop the real
    operator from signing in.

    The rename invalidates this session, because every guard checks the token's
    subject against the current name. Rather than leave the operator on a page
    whose next request 401s, the cookie is cleared and the fragment says to sign
    in again.
    """
    try:
        credentials = await change_username(
            session,
            new_username=new_username,
            current_password=current_password,
        )
    except CredentialError as error:
        return templates.TemplateResponse(
            request,
            "partials/profile_result.html",
            {"admin": admin, "error": str(error)},
            status_code=400,
        )

    logger.info("Admin username changed to %s", credentials.username)
    response = templates.TemplateResponse(
        request,
        "partials/profile_result.html",
        {
            "admin": admin,
            "message": (
                f"Логин изменён на «{credentials.username}». "
                "Войдите заново с новым именем."
            ),
            "reload_hint": True,
        },
    )
    clear_session_cookie(response)
    return response


@partials_router.post(
    "/profile/password",
    response_class=HTMLResponse,
    summary="Change the admin password (HTMX)",
)
async def profile_password_partial(  # noqa: PLR0917 - FastAPI injects each of
    # these by name: three are dependencies and three are form fields, so the
    # count is dictated by the framework rather than by a wide call signature.
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
) -> HTMLResponse:
    """Replace the administrator password.

    The confirmation field is compared here rather than trusted from the browser:
    the `minlength` attribute and any client-side check are advisory, and a
    mismatched pair would otherwise store a password the operator never intended
    and cannot reproduce.

    The session survives, because the token's subject is the username and that has
    not changed. Other sessions survive too — revoking them would need a token
    version in the credential document, which is worth doing only once there is
    more than one operator.
    """
    if new_password != confirm_password:
        return templates.TemplateResponse(
            request,
            "partials/profile_result.html",
            {"admin": admin, "error": "Новые пароли не совпадают."},
            status_code=400,
        )

    try:
        await change_password(
            session,
            current_password=current_password,
            new_password=new_password,
        )
    except CredentialError as error:
        return templates.TemplateResponse(
            request,
            "partials/profile_result.html",
            {"admin": admin, "error": str(error)},
            status_code=400,
        )

    logger.info("Admin password changed for %s", admin.username)
    return templates.TemplateResponse(
        request,
        "partials/profile_result.html",
        {
            "admin": admin,
            "message": (
                "Пароль изменён. Текущая сессия продолжает работать, "
                "новый пароль потребуется при следующем входе."
            ),
        },
    )


@partials_router.post(
    "/settings/models",
    response_class=HTMLResponse,
    summary="Save per-stage model routing (HTMX)",
)
async def settings_models_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Store the model chosen for each pipeline stage.

    Read straight from the form rather than through typed parameters because the
    field set is derived from ``MODEL_ROLES``: declaring each one here would mean
    editing this signature every time a stage is added, and the two lists would
    drift.

    An empty field clears that stage's override and restores the environment
    default, so an operator can back out a change without knowing what the
    default was.
    """
    form = await request.form()
    overrides = {
        role: str(form.get(role) or "")
        for role, _attribute, _label in MODEL_ROLES
        if role in form
    }

    try:
        routing = await save_model_routing(session, overrides)
    except ModelError as error:
        return templates.TemplateResponse(
            request,
            "partials/models_result.html",
            {"admin": admin, "error": str(error)},
            status_code=400,
        )

    logger.info("%s updated the AI model routing", admin.username)
    return templates.TemplateResponse(
        request,
        "partials/models_result.html",
        {
            "admin": admin,
            "routing": [
                {"task": label, "model": routing.for_role(role)}
                for role, _attribute, label in MODEL_ROLES
            ],
        },
    )


# ------------------------------------------------------- Telegram channel manager


@partials_router.get(
    "/sources/telegram/accounts",
    response_class=HTMLResponse,
    summary="Telegram accounts panel (HTMX)",
)
async def telegram_accounts_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/telegram_accounts.html",
        {
            "admin": admin,
            "telegram_accounts": await _telegram_account_rows(session),
        },
    )


@partials_router.get(
    "/sources/telegram/auth/form",
    response_class=HTMLResponse,
    summary="Telegram sign-in form (HTMX)",
)
async def telegram_auth_form_partial(
    request: Request,
    admin: PartialAdmin,
    method: Annotated[str, Query()] = "qr",
) -> HTMLResponse:
    chosen = method if method in {"qr", "phone"} else "qr"
    return templates.TemplateResponse(
        request,
        "partials/telegram_auth.html",
        {"admin": admin, "method": chosen},
    )


@partials_router.post(
    "/sources/telegram/auth/qr",
    response_class=HTMLResponse,
    summary="Start Telegram QR sign-in (HTMX)",
)
async def telegram_auth_qr_start(
    request: Request,
    admin: PartialAdmin,
    api_id: Annotated[int, Form()],
    api_hash: Annotated[str, Form()],
    label: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        pending = await start_qr_auth(
            api_id=api_id, api_hash=api_hash.strip(), label=label.strip()
        )
    except TelegramAuthError as error:
        return templates.TemplateResponse(
            request,
            "partials/telegram_auth.html",
            {"admin": admin, "method": "qr", "error": str(error)},
            status_code=400,
        )
    logger.info("%s started a Telegram QR sign-in", admin.username)
    return templates.TemplateResponse(
        request,
        "partials/telegram_auth.html",
        {"admin": admin, "pending": pending},
    )


@partials_router.post(
    "/sources/telegram/auth/phone",
    response_class=HTMLResponse,
    summary="Start Telegram phone sign-in (HTMX)",
)
async def telegram_auth_phone_start(  # noqa: PLR0917 - FastAPI injects by name
    request: Request,
    admin: PartialAdmin,
    phone: Annotated[str, Form()],
    api_id: Annotated[int, Form()],
    api_hash: Annotated[str, Form()],
    label: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        pending = await start_phone_auth(
            phone_number=phone,
            api_id=api_id,
            api_hash=api_hash.strip(),
            label=label.strip(),
        )
    except (TelegramAuthError, ValueError) as error:
        return templates.TemplateResponse(
            request,
            "partials/telegram_auth.html",
            {"admin": admin, "method": "phone", "error": str(error)},
            status_code=400,
        )
    logger.info("%s started a Telegram phone sign-in", admin.username)
    return templates.TemplateResponse(
        request,
        "partials/telegram_auth.html",
        {"admin": admin, "pending": pending},
    )


@partials_router.post(
    "/sources/telegram/auth/{login_id}/code",
    response_class=HTMLResponse,
    summary="Submit Telegram login code (HTMX)",
)
async def telegram_auth_code(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    login_id: str,
    code: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        status = await submit_pending_code(login_id, code)
    except TelegramAuthError as error:
        return templates.TemplateResponse(
            request,
            "partials/telegram_auth.html",
            {"admin": admin, "error": str(error)},
            status_code=400,
        )
    return await _finish_or_continue_auth(request, admin, session, status)


@partials_router.post(
    "/sources/telegram/auth/{login_id}/password",
    response_class=HTMLResponse,
    summary="Submit Telegram 2FA password (HTMX)",
)
async def telegram_auth_password(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    login_id: str,
    password: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        status = await submit_pending_password(login_id, password)
    except TelegramAuthError as error:
        return templates.TemplateResponse(
            request,
            "partials/telegram_auth.html",
            {"admin": admin, "error": str(error)},
            status_code=400,
        )
    return await _finish_or_continue_auth(request, admin, session, status)


@partials_router.get(
    "/sources/telegram/auth/{login_id}/status",
    response_class=HTMLResponse,
    summary="Poll Telegram QR sign-in (HTMX)",
)
async def telegram_auth_status(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    login_id: str,
) -> HTMLResponse:
    try:
        status = await take_completed(login_id)
    except TelegramAuthError as error:
        return templates.TemplateResponse(
            request,
            "partials/telegram_auth.html",
            {"admin": admin, "error": str(error)},
            status_code=400,
        )
    return await _finish_or_continue_auth(request, admin, session, status)


async def _finish_or_continue_auth(
    request: Request,
    admin: AdminUser,
    session: AsyncSession,
    status: Any,
) -> HTMLResponse:
    """Store a completed session, or keep rendering the current step."""
    if status.result is None:
        return templates.TemplateResponse(
            request,
            "partials/telegram_auth.html",
            {"admin": admin, "pending": status.pending},
        )
    pending = status.pending
    pool = TelegramAccountPool(session)
    await pool.store_session(
        phone_number=status.result.phone_number,
        api_id=pending.api_id,
        api_hash=pending.api_hash,
        session_string=status.result.session_string,
        label=pending.label or status.result.display_name,
    )
    await abandon_auth(pending.login_id)
    logger.info(
        "%s connected Telegram account %s",
        admin.username,
        status.result.phone_number,
    )
    return templates.TemplateResponse(
        request,
        "partials/telegram_accounts.html",
        {
            "admin": admin,
            "telegram_accounts": await _telegram_account_rows(session),
            "auth_cleared": True,
        },
        headers={"HX-Retarget": "#telegram-accounts-panel", "HX-Reswap": "innerHTML"},
    )


@partials_router.post(
    "/sources/telegram/accounts/{account_id}/disconnect",
    response_class=HTMLResponse,
    summary="Disconnect a Telegram account (HTMX)",
)
async def telegram_account_disconnect(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    account_id: int,
) -> HTMLResponse:
    pool = TelegramAccountPool(session)
    account = await session.get(TelegramAccount, account_id)
    if account is None:
        return templates.TemplateResponse(
            request,
            "partials/telegram_accounts.html",
            {
                "admin": admin,
                "telegram_accounts": await _telegram_account_rows(session),
            },
            status_code=404,
        )
    creds = pool.credentials_for(account)
    if creds is not None:
        client = build_client(creds.api_id, creds.api_hash, creds.session_string)
        try:
            await client.connect()
            await client.log_out()
        except Exception:
            logger.info(
                "Telegram log-out for %s failed; local session still cleared",
                account.phone_number,
                exc_info=True,
            )
        finally:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("disconnect after log-out failed", exc_info=True)
    await pool.disconnect(account_id)
    logger.info("%s disconnected Telegram account %s", admin.username, account_id)
    return templates.TemplateResponse(
        request,
        "partials/telegram_accounts.html",
        {
            "admin": admin,
            "telegram_accounts": await _telegram_account_rows(session),
        },
    )


@partials_router.post(
    "/sources/telegram/import",
    response_class=HTMLResponse,
    summary="Import dialogs from every account (HTMX)",
)
async def sources_import_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    """Walk every healthy account's dialog list and upsert the union."""
    return await _import_channels_into(request, admin, session)


async def _import_channels_into(
    request: Request, admin: PartialAdmin, session: AsyncSession
) -> HTMLResponse:
    """Union of dialogs across all pooled accounts."""
    pool = TelegramAccountPool(session)
    accounts = await pool.available_accounts()
    if not accounts:
        return templates.TemplateResponse(
            request,
            "partials/import_result.html",
            {
                "admin": admin,
                "error": "Нет активного авторизованного Telegram-аккаунта.",
            },
            status_code=503,
        )

    discovered: dict[str, DialogDiscovery] = {}
    errors: list[str] = []
    for account in accounts:
        creds = pool.credentials_for(account)
        if creds is None:
            continue
        mtproto = MTProtoIngestor(session)
        try:
            await mtproto.start_with(creds)
            for dialog in await mtproto.list_dialogs():
                discovered[dialog.identifier] = dialog
        except TelegramIngestorError as error:
            errors.append(f"{account.display_name}: {error}")
        finally:
            await mtproto.close()

    if not discovered and errors:
        return templates.TemplateResponse(
            request,
            "partials/import_result.html",
            {"admin": admin, "error": "; ".join(errors)},
            status_code=503,
        )

    existing_rows = await session.execute(
        select(Source).where(Source.source_type == SourceType.TELEGRAM)
    )
    existing = list(existing_rows.scalars().all())

    plan, merged = _plan_dialog_import(existing, discovered.values())
    if merged:
        # Retiring flushes on its own, *before* any keeper takes the canonical
        # identifier. `(source_type, identifier)` is unique and SQLAlchemy orders
        # the UPDATE statements itself, so renaming both rows in one flush made
        # the keeper claim an identifier the duplicate had not released yet —
        # the `uq_sources_type_identifier` violation this import hit in production.
        await session.flush()

    created, updated = _write_dialog_plan(session, plan)

    # A public channel is readable without joining it, so a row added by hand or
    # seeded from configuration never appears in `iter_dialogs`. Those rows are
    # resolved one by one, or they would keep no avatar, link, description or
    # kind while ingestion reads them perfectly well.
    described = await _describe_unlisted_sources(
        session,
        pool,
        accounts,
        [row for row in existing if row.peer_kind is None],
    )

    try:
        await session.commit()
    except SQLAlchemyError:
        # Reading Telegram succeeded, so a write failure is worth naming rather
        # than raising: an unhandled 500 replaced the whole panel section with a
        # traceback page and lost the reason.
        await session.rollback()
        logger.exception("Dialog import could not be stored")
        return templates.TemplateResponse(
            request,
            "partials/import_result.html",
            {
                "admin": admin,
                "error": (
                    "Диалоги прочитаны, но сохранить их не удалось: база отклонила "
                    "запись. Список не изменён, подробности — в журнале сервера."
                ),
            },
            status_code=503,
        )

    logger.info(
        "%s imported %d dialog(s): %d new, %d updated",
        admin.username,
        len(discovered),
        created,
        updated,
    )
    return templates.TemplateResponse(
        request,
        "partials/import_result.html",
        {
            "admin": admin,
            "discovered": len(discovered),
            "created": created,
            "updated": updated,
            "merged": merged,
            "described": described,
            "rows": await _telegram_source_rows(session),
        },
    )


async def _telegram_source_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Row contexts for the dialogs table, merged duplicates excluded."""
    result = await session.execute(
        select(Source)
        .where(
            Source.source_type == SourceType.TELEGRAM,
            _NOT_MERGED_DUPLICATE,
        )
        .order_by(Source.name)
    )
    return [await _source_row(session, source) for source in result.scalars().all()]


async def _describe_unlisted_sources(
    session: AsyncSession,
    pool: TelegramAccountPool,
    accounts: Sequence[TelegramAccount],
    rows: list[Source],
) -> int:
    """Fill in metadata for sources absent from every account's dialog list.

    Telegram lets a user account read a **public** channel it never joined, so
    "not in the dialog list" does not mean "not collectable". Each row is
    resolved by identifier; a row that cannot be resolved is left untouched
    rather than flagged, because the reason (renamed, deleted, private) is not
    knowable from a failed lookup alone.
    """
    if not rows or not accounts:
        return 0
    creds = next(
        (
            found
            for account in accounts
            if (found := pool.credentials_for(account)) is not None
        ),
        None,
    )
    if creds is None:
        return 0

    described = 0
    mtproto = MTProtoIngestor(session)
    try:
        await mtproto.start_with(creds)
        for row in rows:
            dialog = await mtproto.describe_peer(row.identifier.strip())
            if dialog is None:
                continue
            _apply_dialog_metadata(row, dialog)
            described += 1
    except TelegramIngestorError as error:
        logger.info("Could not describe unlisted sources: %s", error)
    finally:
        await mtproto.close()
    return described


def _plan_dialog_import(
    existing: list[Source], dialogs: Iterable[DialogDiscovery]
) -> tuple[list[tuple[DialogDiscovery, Source | None]], int]:
    """Decide, per dialog, which row to update — and retire the rest.

    One Telegram peer can already own several rows: an early import stored
    ``rian_ru``, a later one ``@rian_ru``, and the private form is ``-100<id>``.
    The kept row is the one carrying history or the operator's tick, because
    ``raw_news.source_id`` hangs off it. Duplicates are retired rather than
    deleted: deleting cascades the collected posts away.
    """
    plan: list[tuple[DialogDiscovery, Source | None]] = []
    merged = 0
    for dialog in dialogs:
        matches = _matching_sources(existing, dialog)
        if not matches:
            plan.append((dialog, None))
            continue
        keeper = _preferred_source(matches)
        plan.append((dialog, keeper))
        for duplicate in matches:
            if duplicate is keeper:
                continue
            duplicate.is_active = False
            duplicate.identifier = f"retired:{duplicate.id}:{duplicate.identifier}"[:512]
            duplicate.peer_kind = _DUPLICATE_PEER_KIND
            merged += 1
    return plan, merged


def _write_dialog_plan(
    session: AsyncSession, plan: list[tuple[DialogDiscovery, Source | None]]
) -> tuple[int, int]:
    """Insert new dialogs and refresh kept ones. Returns (created, updated)."""
    created = updated = 0
    for dialog, keeper in plan:
        if keeper is None:
            session.add(
                Source(
                    source_type=SourceType.TELEGRAM,
                    name=dialog.title,
                    identifier=dialog.identifier,
                    telegram_id=dialog.telegram_id,
                    avatar_data_uri=dialog.avatar_data_uri,
                    is_active=False,
                    crawl_rules={},
                    peer_kind=dialog.kind.value,
                    username=dialog.username,
                    description=dialog.description or None,
                    invite_link=dialog.invite_link,
                )
            )
            created += 1
        else:
            _apply_dialog_metadata(keeper, dialog)
            updated += 1
    return created, updated


def _matching_sources(existing: list[Source], dialog: DialogDiscovery) -> list[Source]:
    """Every stored row that is really the same Telegram peer.

    Matched three ways because the identifier alone is not stable across
    imports: the numeric peer id is authoritative, a username matches
    case-insensitively with or without ``@``, and the raw identifier catches
    rows imported before ``telegram_id`` was recorded.
    """
    username = (dialog.username or "").lstrip("@").lower()
    candidates: list[Source] = []
    for row in existing:
        if row.peer_kind == _DUPLICATE_PEER_KIND:
            continue
        if dialog.telegram_id and row.telegram_id == dialog.telegram_id:
            candidates.append(row)
            continue
        if username:
            row_names = {
                (row.username or "").lstrip("@").lower(),
                row.identifier.lstrip("@").lower(),
            }
            if username in row_names:
                candidates.append(row)
                continue
        if row.identifier == dialog.identifier:
            candidates.append(row)
    return candidates


def _preferred_source(matches: list[Source]) -> Source:
    """Pick which duplicate row survives a merge.

    Priority: the row with collected posts, then one the operator enabled, then
    the oldest id. That keeps ``raw_news`` history and the collection flags
    attached to the row that already carries them.
    """
    return sorted(
        matches,
        key=lambda row: (
            -(row.posts_collected or 0),
            not row.is_active,
            row.id or 0,
        ),
    )[0]


def _apply_dialog_metadata(source: Source, dialog: DialogDiscovery) -> None:
    """Refresh display fields without touching collection flags."""
    source.name = dialog.title or source.name
    source.identifier = dialog.identifier
    source.telegram_id = dialog.telegram_id
    source.peer_kind = dialog.kind.value
    source.username = dialog.username
    if dialog.description:
        source.description = dialog.description
    source.invite_link = dialog.invite_link
    if dialog.avatar_data_uri:
        source.avatar_data_uri = dialog.avatar_data_uri


@partials_router.post(
    "/sources/{source_id}/flags",
    response_class=HTMLResponse,
    summary="Toggle one source's collection flags (HTMX)",
)
async def source_flags_partial(  # noqa: PLR0917 - FastAPI injects each
    # argument by name: three dependencies and two form fields.
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    source_id: int,
    is_active: Annotated[bool, Form()] = False,
    fetch_media: Annotated[bool, Form()] = False,
) -> HTMLResponse:
    """Set both checkboxes for one source in one request.

    Both default to ``False`` because an unchecked HTML checkbox submits no field
    at all; without the defaults, un-ticking could never reach the server.

    Returns the row fragment rather than a message: the table stays where it is
    and the two checkboxes re-render from what the database now holds.
    """
    source = await session.get(Source, source_id)
    if source is None:
        return templates.TemplateResponse(
            request,
            "partials/source_flags_result.html",
            {"admin": admin, "error": f"Источник {source_id} не найден."},
            status_code=404,
        )

    source.is_active = is_active
    if source.source_type is SourceType.TELEGRAM:
        source.fetch_media = fetch_media
    await session.commit()

    logger.info(
        "%s set source %s (%s): active=%s media=%s",
        admin.username,
        source.id,
        source.identifier,
        is_active,
        fetch_media,
    )
    row = await _source_row(session, source)
    return templates.TemplateResponse(
        request,
        "partials/source_row.html",
        {"admin": admin, "row": row},
    )


def _telegram_link(source: Source) -> str:
    """Clickable ``t.me`` URL for one source row.

    The import stores ``invite_link`` for every dialog it sees. A source the
    account is no longer subscribed to never appears in that list, so it keeps
    the identifier an operator typed years ago and would otherwise render as
    "no public link" — even though ``tass_agency`` is perfectly reachable at
    ``t.me/tass_agency``. The username is therefore rebuilt from the identifier
    as a fallback, which is exactly the case the owner needs in order to open
    the channel and check whether the account still belongs there.

    Numeric peer ids are left alone: ``-100…`` has no ``t.me`` form that resolves
    for a non-subscriber, and inventing one would produce a dead link.
    """
    if source.invite_link:
        return source.invite_link
    if source.source_type is not SourceType.TELEGRAM:
        return ""
    handle = (source.username or source.identifier or "").strip().lstrip("@")
    if not handle or not _TELEGRAM_USERNAME_RE.fullmatch(handle):
        return ""
    return f"https://t.me/{handle}"


async def _source_row(session: AsyncSession, source: Source) -> dict[str, Any]:
    """Build the per-row context for the sources table."""
    return {
        "id": source.id,
        "name": source.name,
        "identifier": source.identifier,
        "telegram_id": source.telegram_id,
        "avatar": source.avatar_data_uri,
        "is_active": source.is_active,
        "fetch_media": source.fetch_media,
        "is_telegram": source.source_type is SourceType.TELEGRAM,
        "posts_collected": source.posts_collected,
        "kind_label": kind_label(source.peer_kind),
        "description": source.description or "",
        "link": _telegram_link(source),
        "unread_label": await _unread_label(session, source),
        "last_scraped_at": (
            source.last_scraped_at.strftime("%d.%m.%Y %H:%M")
            if source.last_scraped_at
            else ""
        ),
    }


async def _unread_label(session: AsyncSession, source: Source) -> str:
    """Posts newer than our watermark, not Telegram's unread badge.

    No watermark means the next collection is a first pass of today's posts.
    """
    watermark = await telegram_watermark(session, source)
    if watermark is None:
        return "первый проход"
    numeric_only = RawNews.external_post_id.op("~")("^[0-9]{1,18}$")
    statement = select(func.count()).where(
        RawNews.source_id == source.id,
        numeric_only,
        sa_cast(RawNews.external_post_id, BigInteger) > watermark,
    )
    return str(int((await session.execute(statement)).scalar_one()))


@partials_router.post(
    "/sources/telegram/bulk",
    response_class=HTMLResponse,
    summary="Bulk-manage Telegram channel flags (HTMX)",
)
async def sources_bulk_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    action: Annotated[str, Form()],
) -> HTMLResponse:
    """Apply one bulk action to every Telegram source and return the whole table.

    ``all`` enables everything, ``none`` disables everything, ``media_only``
    enables exactly the channels that carry media — the operator reading the
    point of a news aggregator wants photos, and picking those by hand across a
    hundred rows is not a human task.
    """
    if action not in {"all", "none", "media_only"}:
        return templates.TemplateResponse(
            request,
            "partials/import_result.html",
            {"admin": admin, "error": f"Неизвестное действие: {action}"},
            status_code=400,
        )

    result = await session.execute(
        # `.is_()` compiles to IS, which PostgreSQL rejects for an enum parameter;
        # equality is the correct operator here. Merged duplicates are excluded so
        # a bulk action cannot re-enable a row the import retired.
        select(Source).where(
            Source.source_type == SourceType.TELEGRAM,
            _NOT_MERGED_DUPLICATE,
        )
    )
    telegram_sources = list(result.scalars().all())

    for source in telegram_sources:
        if action == "all":
            source.is_active = True
        elif action == "none":
            source.is_active = False
        else:  # media_only
            source.is_active = True
            source.fetch_media = True

    await session.commit()
    logger.info(
        "%s applied bulk action %s to %d telegram source(s)",
        admin.username,
        action,
        len(telegram_sources),
    )

    rows = [await _source_row(session, s) for s in telegram_sources]
    return templates.TemplateResponse(
        request,
        "partials/sources_table.html",
        {"admin": admin, "rows": rows, "bulk_done": action},
    )


@partials_router.get(
    "/articles/{article_id}/preview",
    response_class=HTMLResponse,
    summary="Article preview modal (HTMX)",
)
async def article_preview_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    article_id: int,
) -> HTMLResponse:
    """Render the preview-and-edit modal for one article.

    The body is rendered as stored HTML inside a sandboxed container. It is the
    pipeline's own validated output — scripts and inline handlers are rejected at
    generation time — but ``sandbox`` keeps that an enforced property of the page
    rather than a promise.
    """
    article = await session.get(Article, article_id)
    if article is None:
        return templates.TemplateResponse(
            request,
            "partials/article_preview.html",
            {"admin": admin, "error": f"Материал {article_id} не найден."},
            status_code=404,
        )

    context = await _build_preview_context(session, article, admin=admin)
    return templates.TemplateResponse(
        request,
        "partials/article_preview.html",
        context,
    )


async def _build_preview_context(
    session: AsyncSession,
    article: Article,
    *,
    admin: AdminUser,
    saved: bool = False,
    approval_notice: str | None = None,
    rewrite_notice: str | None = None,
    media_notice: str | None = None,
) -> dict[str, Any]:
    """Assemble all related facts, cluster info, and source feeds for the modal."""
    cluster = await session.get(NewsCluster, article.cluster_id)
    raw_items: list[RawNews] = []
    sources_map: dict[int, str] = {}
    if cluster:
        stmt = (
            select(RawNews)
            .where(RawNews.cluster_id == cluster.id)
            .order_by(RawNews.created_at.desc())
            .limit(10)
        )
        raw_res = await session.execute(stmt)
        raw_items = list(raw_res.scalars().all())
        source_ids = {r.source_id for r in raw_items if r.source_id}
        if source_ids:
            s_res = await session.execute(
                select(Source.id, Source.name).where(Source.id.in_(source_ids))
            )
            sources_map = {row[0]: row[1] for row in s_res.all()}

    return {
        "admin": admin,
        "article": article,
        "cluster": cluster,
        "raw_items": raw_items,
        "sources_map": sources_map,
        "categories": sorted(CATEGORY_MAP),
        "saved": saved,
        "approval_notice": approval_notice,
        "rewrite_notice": rewrite_notice,
        "media_notice": media_notice,
        "distribution_items": _build_distribution_items(article),
    }


@partials_router.post(
    "/articles/publish-ready",
    response_class=HTMLResponse,
    summary="Trigger immediate publication of all ready articles (HTMX)",
)
async def articles_publish_ready_partial(
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Launch background publication of all MEDIA_READY articles.

    Also triggers social retries immediately.
    """
    import asyncio  # noqa: PLC0415

    from app.workers.publisher_worker import run_publisher  # noqa: PLC0415

    ready_cnt = (
        await session.execute(
            select(func.count(Article.id)).where(
                Article.status == ArticleStatus.MEDIA_READY
            )
        )
    ).scalar_one()

    wp_published_cnt = (
        await session.execute(
            select(func.count(Article.id)).where(
                Article.status == ArticleStatus.WP_PUBLISHED,
                Article.retry_count < 5,
            )
        )
    ).scalar_one()

    if not ready_cnt and not wp_published_cnt:
        return HTMLResponse(
            '<div class="rounded-xl border border-blue-200 bg-blue-50 '
            'px-4 py-2.5 text-xs text-blue-900">'
            "ℹ️ Сейчас нет статей в очереди на публикацию или дожим соцсетей."
            "</div>"
        )

    logger.info(
        "%s triggered publication of %d ready articles and %d social retry articles",
        admin.username,
        ready_cnt,
        wp_published_cnt,
    )
    asyncio.create_task(run_publisher(limit=100))  # noqa: RUF006
    return HTMLResponse(
        '<div class="rounded-xl border border-emerald-200 bg-emerald-50 '
        'px-4 py-2.5 text-xs font-medium text-emerald-900">'
        f"🚀 Публикация: {ready_cnt} готовых"
        f" и дожим {wp_published_cnt} в соцсети! "
        "Обновите страницу через 30–60 сек — "
        "статусы сменятся на «Опубликован»."
        "</div>"
    )


@partials_router.post(
    "/articles/{article_id}/approve_publish",
    response_class=HTMLResponse,
    summary="Approve and trigger publication for an article (HTMX)",
)
async def article_approve_publish_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    article_id: int,
) -> HTMLResponse:
    """Operator approval of one article for immediate site and social publishing."""
    article = await session.get(Article, article_id)
    if article is None:
        return templates.TemplateResponse(
            request,
            "partials/article_preview.html",
            {"admin": admin, "error": f"Материал {article_id} не найден."},
            status_code=404,
        )

    try:
        from app.workers.publisher_worker import run_publisher  # noqa: PLC0415

        article.status = ArticleStatus.MEDIA_READY
        await session.commit()
        report = await run_publisher(article_ids=[article.id], limit=1)
        if report.publications:
            pub = report.publications[0]
            notice = f"Публикация завершена: статус {pub.status}."
            if pub.wp_post_url:
                notice += f" Ссылка: {pub.wp_post_url}"
            if pub.error:
                notice += f" ({pub.error})"
        elif report.lock_skipped:
            notice = (
                "Фоновый публикатор уже публикует очередь статей прямо сейчас. "
                "Эта статья поставлена в очередь и выйдет автоматически."
            )
        else:
            notice = "Статья отправлена в публикатор."
    except Exception as exc:
        logger.exception("Publish attempt failed for article %s", article.id)
        notice = f"Ошибка при публикации: {exc}"

    await session.refresh(article)
    context = await _build_preview_context(
        session,
        article,
        admin=admin,
        approval_notice=notice,
    )
    return templates.TemplateResponse(request, "partials/article_preview.html", context)


@partials_router.post(
    "/articles/{article_id}/republish-social",
    response_class=HTMLResponse,
    summary="Retry social distribution for an article (HTMX)",
)
async def article_republish_social_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    article_id: int,
) -> HTMLResponse:
    """Retry failed/pending social channels.

    Does not recreate the WP post.
    """
    article = await session.get(Article, article_id)
    if article is None:
        if request.headers.get("hx-target", "").strip() != "article-modal":
            return HTMLResponse(
                '<div class="rounded-xl border border-rose-300 bg-rose-50 '
                'p-3 text-xs text-rose-900 shadow-2xs mb-2">'
                f'Материал #{article_id} не найден.</div>',
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "partials/article_preview.html",
            {"admin": admin, "error": f"Материал {article_id} не найден."},
            status_code=404,
        )

    try:
        from app.workers.publisher_worker import run_publisher  # noqa: PLC0415

        report = await run_publisher(article_ids=[article.id], limit=1)
        if report.publications:
            pub = report.publications[0]
            notice = (
                f"Допостинг завершен: статус {pub.status}. "
                f"Соцсетей: {pub.social_published}, "
                f"сбоев: {pub.social_failed}."
            )
            if pub.error:
                notice += f" ({pub.error})"
        elif report.lock_skipped:
            notice = "Фоновый публикатор сейчас занят. Повторите через минуту."
        else:
            notice = "Повтор публикации запущен."
    except Exception as exc:
        logger.exception("Social republish attempt failed for article %s", article.id)
        notice = f"Ошибка при повторе публикации: {exc}"

    await session.refresh(article)

    # When triggered from the table row (target is not the preview modal),
    # return a concise toast notification and trigger an automatic table refresh.
    target = request.headers.get("hx-target", "").strip()
    if target != "article-modal":
        is_error = "Ошибка" in notice
        border_cls = (
            "border-rose-300 bg-rose-50 text-rose-900"
            if is_error
            else "border-emerald-300 bg-emerald-50 text-emerald-900"
        )
        title_disp = html.escape((article.title or f"#{article.id}")[:60])
        safe_notice = html.escape(notice)
        html_content = (
            f'<div class="rounded-xl border {border_cls} px-3.5 py-2.5 text-xs '
            f'flex items-center justify-between shadow-2xs mb-2.5 transition-all">\n'
            f'  <div class="flex items-center gap-2">\n'
            f'    <span class="font-semibold whitespace-nowrap">'
            f'↻ Допостинг «{title_disp}»:</span>\n'
            f'    <span>{safe_notice}</span>\n'
            f'  </div>\n'
            f'  <button type="button" onclick="this.closest(\'div\').remove()" '
            f'class="text-neutral-500 hover:text-neutral-800 font-bold ml-3 '
            f'text-sm leading-none cursor-pointer">&times;</button>\n'
            f'</div>'
        )
        response = HTMLResponse(html_content)
        response.headers["HX-Trigger"] = "refreshArticles"
        return response

    context = await _build_preview_context(
        session,
        article,
        admin=admin,
        approval_notice=notice,
    )
    return templates.TemplateResponse(request, "partials/article_preview.html", context)


@partials_router.post(
    "/articles/{article_id}/rewrite",
    response_class=HTMLResponse,
    summary="Rewrite article with prompt style (HTMX)",
)
async def article_rewrite_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    article_id: int,
    style: Annotated[str, Form()] = "analytical",
    instruction: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Operator prompt rewrite and Anti-Slop verification pass."""
    article = await session.get(Article, article_id)
    if article is None:
        return templates.TemplateResponse(
            request,
            "partials/article_preview.html",
            {"admin": admin, "error": f"Материал {article_id} не найден."},
            status_code=404,
        )

    style_labels = {
        "analytical": "Аналитический разбор",
        "breaking": "Срочная молния",
        "longread": "Развернутый лонгрид",
        "ironic": "Ироничный обзор",
    }
    chosen_label = style_labels.get(style, style)

    from app.services.ai_client import AIClientError, get_ai_client  # noqa: PLC0415
    from app.services.slop_filter import clean_ai_slop  # noqa: PLC0415

    verdict = "очищено от штампов"
    if settings.openrouter_api_key:
        try:
            client = get_ai_client()
            notes = instruction or "сохранить факты"
            prompt = (
                f"Перепиши новостной текст в стиле «{chosen_label}».\n"
                f"Требования:\n"
                f"- Чистый информационный стиль, без вводных клише и канцеляризмов\n"
                f"- Сохрани форматирование HTML параграфов\n"
                f"- Дополнительные указания редактора: {notes}\n\n"
                f"Исходный текст:\n{article.content_html}"
            )
            rewritten = await client.complete(
                prompt,
                model=settings.ai_model_writer,
                max_tokens=2500,
                temperature=0.3,
            )
            cleaned = clean_ai_slop(rewritten)
            if cleaned and len(cleaned) > 100:
                article.content_html = cleaned
                verdict = "переписано с помощью нейросети и очищено"
        except (AIClientError, RuntimeError, ValueError) as exc:
            logger.warning("AI rewrite failed, using slop cleaner: %s", exc)
            article.content_html = clean_ai_slop(article.content_html or "")
            verdict = "очищено локально (фильтр штампов)"
        except Exception as exc:  # noqa: BLE001 - resilience against unforeseen API failures
            logger.warning("Unexpected rewrite failure: %s", exc)
            article.content_html = clean_ai_slop(article.content_html or "")
            verdict = "очищено локально (фильтр штампов)"
    else:
        article.content_html = clean_ai_slop(article.content_html or "")
        verdict = "локальная очистка (без ключа API)"

    await session.commit()
    await session.refresh(article)

    logger.info(
        "%s requested rewrite for article %s (style=%s)",
        admin.username,
        article.id,
        style,
    )
    context = await _build_preview_context(
        session,
        article,
        admin=admin,
        rewrite_notice=(
            f"Стиль «{chosen_label}» применен. Anti-slop проверка: {verdict}."
        ),
    )
    return templates.TemplateResponse(request, "partials/article_preview.html", context)


@partials_router.post(
    "/articles/{article_id}/toggle_watermark",
    response_class=HTMLResponse,
    summary="Toggle permission to use media with watermark (HTMX)",
)
async def article_toggle_watermark_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    article_id: int,
    allow_watermark: Annotated[int, Form()] = 1,
) -> HTMLResponse:
    """Explicit owner permission to use original image with detected watermark."""
    article = await session.get(Article, article_id)
    if article is None:
        return templates.TemplateResponse(
            request,
            "partials/article_preview.html",
            {"admin": admin, "error": f"Материал {article_id} не найден."},
            status_code=404,
        )

    statuses = dict(article.platform_statuses or {})
    statuses["allow_watermark"] = bool(allow_watermark)
    article.platform_statuses = statuses
    await session.commit()
    await session.refresh(article)

    if allow_watermark:
        notice = "Использование медиа с водяным знаком разрешено владельцем."
    else:
        notice = "Использование медиа с водяным знаком запрещено."

    logger.info(
        "%s set allow_watermark=%s on article %s",
        admin.username,
        bool(allow_watermark),
        article.id,
    )
    context = await _build_preview_context(
        session,
        article,
        admin=admin,
        media_notice=notice,
    )
    return templates.TemplateResponse(request, "partials/article_preview.html", context)


@partials_router.get(
    "/articles/{article_id}/journal",
    response_class=HTMLResponse,
    summary="Article pipeline journal (HTMX)",
)
async def article_journal_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    article_id: int,
) -> HTMLResponse:
    """Render the durable evidence attached to one generated material."""
    article = await session.get(Article, article_id)
    if article is None:
        return templates.TemplateResponse(
            request,
            "partials/article_journal.html",
            {"admin": admin, "error": f"Материал {article_id} не найден."},
            status_code=404,
        )
    journals = await load_article_journals(session, article.id)
    store = ArtifactStore()
    run_summaries = {
        journal.run_id: summary
        for journal in journals
        if (summary := store.read_summary(journal.run_id)) is not None
    }
    return templates.TemplateResponse(
        request,
        "partials/article_journal.html",
        {
            "admin": admin,
            "article": article,
            "journals": journals,
            "run_summaries": run_summaries,
        },
    )


@partials_router.post(
    "/articles/{article_id}/edit",
    response_class=HTMLResponse,
    summary="Save quick edits to one article (HTMX)",
)
async def article_edit_save_partial(  # noqa: PLR0917 - FastAPI injects each
    # argument by name; four form fields is what the edit modal has.
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    article_id: int,
    title: Annotated[str, Form()],
    seo_title: Annotated[str, Form()] = "",
    seo_description: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = DEFAULT_CATEGORY,
) -> HTMLResponse:
    """Persist the modal's editable fields.

    The category select offers only known slugs, so an unknown value here means a
    hand-crafted request rather than a UI mistake; it is refused rather than
    silently mapped, because silently re-filing an article teaches nobody anything.
    """
    article = await session.get(Article, article_id)
    if article is None:
        return templates.TemplateResponse(
            request,
            "partials/article_preview.html",
            {"admin": admin, "error": f"Материал {article_id} не найден."},
            status_code=404,
        )
    if category not in CATEGORY_MAP:
        return templates.TemplateResponse(
            request,
            "partials/article_preview.html",
            {"admin": admin, "error": "Неизвестная рубрика."},
            status_code=400,
        )

    title = title.strip()
    if not title:
        return templates.TemplateResponse(
            request,
            "partials/article_preview.html",
            {"admin": admin, "error": "Заголовок не может быть пустым."},
            status_code=400,
        )

    article.title = title
    article.seo_title = seo_title.strip() or title
    article.seo_description = seo_description.strip()
    # Keep the WordPress term id in step with the slug: publishing uses the id,
    # and a stale pair would file the post under the old section.
    article.category = category
    article.wp_category_id = wp_category_id(category)
    try:
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.warning("Article %s edit failed", article_id, exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/article_preview.html",
            {"admin": admin, "error": "База данных недоступна: изменение не сохранено."},
            status_code=503,
        )

    logger.info("%s edited article %s", admin.username, article.id)
    await session.refresh(article)
    context = await _build_preview_context(session, article, admin=admin, saved=True)
    return templates.TemplateResponse(
        request,
        "partials/article_preview.html",
        context,
    )


async def _resolve_wp_thumbnail_url(article: Article, session: AsyncSession) -> str:
    media_id = getattr(article, "wp_image_id", None)
    if not media_id:
        return ""
    cached = getattr(article, "cover_thumbnail_url", None)
    if cached:
        return str(cached)
    try:
        from app.services.wp_connection import load_wp_connection  # noqa: PLC0415
        conn = await load_wp_connection(session)
        if conn.has_rest_credentials:
            from app.services.wp_media_service import WPMediaService  # noqa: PLC0415
            async with WPMediaService(
                base_url=conn.wp_base_url,
                username=conn.wp_app_user,
                app_password=conn.wp_app_password,
            ) as wp_svc:
                url = await wp_svc.get_media_url(int(media_id))
                if url:
                    return url
    except Exception as wp_exc:  # noqa: BLE001
        logger.debug(
            "Failed to resolve WP media %s via db conn: %s", media_id, wp_exc
        )
    try:
        return await get_wp_media_service().get_media_url(int(media_id)) or ""
    except Exception as wp_exc:  # noqa: BLE001
        logger.debug(
            "Failed to resolve WP media %s via env conn: %s", media_id, wp_exc
        )
    return ""


async def _resolve_tg_fallback_url(
    article: Article,
    article_id: int,
    session: AsyncSession,
) -> str:
    if not article.cluster_id:
        return ""
    try:
        from app.models.cluster import NewsCluster  # noqa: PLC0415
        from app.models.raw_news import RawNews  # noqa: PLC0415
        from app.workers.content_worker import (  # noqa: PLC0415
            _resolve_tg_embed_photo,
        )

        cluster = await session.get(NewsCluster, article.cluster_id)
        if cluster and cluster.enrichment_data:
            sel = cluster.enrichment_data.get("selected_cover_url")
            if sel and isinstance(sel, str) and _SAFE_MEDIA_URL.match(sel):
                return sel

        rn_stmt = (
            select(RawNews.post_url)
            .where(
                RawNews.cluster_id == article.cluster_id,
                RawNews.post_url.is_not(None),
            )
            .order_by(RawNews.id.desc())
            .limit(1)
        )
        post_url = (await session.execute(rn_stmt)).scalar_one_or_none()
        if post_url:
            async with httpx.AsyncClient(timeout=4.0) as http_client:
                tg_photo = await _resolve_tg_embed_photo(http_client, str(post_url))
                if tg_photo and _SAFE_MEDIA_URL.match(tg_photo):
                    return tg_photo
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Thumbnail fallback resolution failed for article %d: %s",
            article_id,
            exc,
        )
    return ""


async def _resolve_article_thumbnail_url(
    article: Article | None,
    article_id: int,
    session: AsyncSession,
) -> str:
    if article is None:
        return ""
    url = await _resolve_wp_thumbnail_url(article, session)
    if url:
        return url
    return await _resolve_tg_fallback_url(article, article_id, session)


@partials_router.get(
    "/articles/{article_id}/thumbnail",
    summary="Cover thumbnail for one article",
)
async def article_thumbnail_partial(article_id: int, session: SessionDep) -> Response:
    """Redirect to the cover's WordPress URL.

    The bytes live in WordPress after upload; proxying them through this app would
    double the bandwidth and add latency to every table render, so the endpoint
    answers with a 302 and lets the browser fetch directly. A missing or failed
    lookup falls through to a small inline SVG placeholder rather than a broken
    image icon, because a table of twenty rows should never show twenty errors.
    """
    article = await session.get(Article, article_id)
    url = await _resolve_article_thumbnail_url(article, article_id, session)

    if url and _SAFE_MEDIA_URL.match(url):
        return RedirectResponse(url, status_code=http_status.HTTP_302_FOUND)

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="56">'
        '<rect width="100%" height="100%" fill="#1e293b"/>'
        '<text x="50%" y="54%" fill="#64748b" font-family="sans-serif" '
        'font-size="11" text-anchor="middle">нет фото</text></svg>'
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )



#: Only https URLs are redirected to: the value comes from a WordPress response,
#: and a compromised upstream must not be able to turn the table into a
#: `javascript:` or `data:` delivery channel.
_SAFE_MEDIA_URL: Final[re.Pattern[str]] = re.compile(r"^https://[^\s\"'<>]+$")


# ------------------------------------------------------------------ Лаборатория


@pages_router.get("/lab", response_class=HTMLResponse, summary="Pipeline lab")
async def dashboard_lab(
    request: Request,
    admin: PageAdmin,
    session: SessionDep,
    *,
    limit: int = 50,
    offset: int = 0,
    cluster: uuid.UUID | None = None,
    record: uuid.UUID | None = None,
) -> HTMLResponse:
    """Render the lab page with honest system state, news groups, and stepwise studio."""
    status = await get_lab_system_status(session)
    groups_data = await get_news_groups_summary(session, limit=limit, offset=offset)

    target_cluster_id = cluster or record
    selected_cluster = None
    if target_cluster_id is not None:
        selected_cluster = await get_cluster_detail(session, target_cluster_id)

    return templates.TemplateResponse(
        request,
        "lab.html",
        {
            "admin": admin,
            "status": status,
            "groups_data": groups_data,
            "selected_cluster": selected_cluster,
            "selected_cluster_id": str(target_cluster_id) if target_cluster_id else None,
            "active": "lab",
        },
    )


@pages_router.get(
    "/lab/groups",
    response_class=HTMLResponse,
    summary="Filter news groups limit (HTMX)",
)
async def lab_groups_partial(
    request: Request,
    admin: PageAdmin,
    session: SessionDep,
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse:
    """Return filtered news groups list with specified limit."""
    groups_data = await get_news_groups_summary(session, limit=limit, offset=offset)
    return templates.TemplateResponse(
        request,
        "partials/lab_groups_list.html",
        {
            "admin": admin,
            "groups_data": groups_data,
        },
    )


@partials_router.post(
    "/lab/wipe/preview",
    response_class=HTMLResponse,
    summary="Preview clean-slate wipe (HTMX)",
)
async def lab_wipe_preview_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    """Show an exact local wipe target and counts without deleting anything."""
    logger.info("%s requested a clean-slate wipe preview", admin.username)
    try:
        report = await preview_pipeline_wipe(session)
        # The preview is intentionally available even when the target is refused;
        # the confirm endpoint performs the hard safety check before mutation.
    except SQLAlchemyError:
        logger.exception("Could not preview pipeline wipe")
        return templates.TemplateResponse(
            request,
            "partials/lab_wipe.html",
            {"admin": admin, "error": "Не удалось прочитать состояние базы."},
            status_code=503,
        )
    return templates.TemplateResponse(
        request, "partials/lab_wipe.html", {"admin": admin, "report": report.to_dict()}
    )


@partials_router.post(
    "/lab/wipe",
    response_class=HTMLResponse,
    summary="Wipe generated pipeline history (HTMX)",
)
async def lab_wipe_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    confirm: Annotated[int, Query(ge=0, le=1)] = 0,
) -> HTMLResponse:
    """Perform the explicit, scratch-database-only clean-slate operation."""
    logger.warning("%s requested a confirmed clean-slate wipe", admin.username)
    try:
        report = await wipe_pipeline_history(session, confirm=bool(confirm))
    except (ResetBlockedError, UnsafeWipeTargetError) as error:
        return templates.TemplateResponse(
            request,
            "partials/lab_wipe.html",
            {"admin": admin, "error": str(error)},
            status_code=409,
        )
    except SQLAlchemyError:
        logger.exception("Full pipeline wipe failed")
        return templates.TemplateResponse(
            request,
            "partials/lab_wipe.html",
            {"admin": admin, "error": "База данных отказала: данные не удалены."},
            status_code=503,
        )
    return templates.TemplateResponse(
        request,
        "partials/lab_reset.html",
        {
            "admin": admin,
            "report": report.to_dict(),
            "posts_per_source": settings.ingest_max_messages_per_source,
        },
    )


@partials_router.post(
    "/lab/reset", response_class=HTMLResponse, summary="Clear the queue (HTMX)"
)
async def lab_reset_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    articles: Annotated[int, Query(ge=0, le=1)] = 0,
) -> HTMLResponse:
    """Clear the collected queue so the next pass collects a full first batch.

    Available in both modes deliberately. In test mode it is how an operator
    re-checks the current thresholds against the same channels; in production it is
    how a misconfigured run is undone without waiting out the dedup window. The
    production switch is not consulted: a paused pipeline is exactly when an
    operator needs to clear what the last run collected.
    """
    logger.info(
        "%s requested a queue reset (articles=%s)", admin.username, bool(articles)
    )
    try:
        report = await reset_pipeline_queue(session, include_articles=bool(articles))
    except ResetBlockedError as error:
        return templates.TemplateResponse(
            request,
            "partials/lab_reset.html",
            {"admin": admin, "error": str(error)},
            status_code=409,
        )
    except SQLAlchemyError:
        logger.exception("Queue reset failed")
        return templates.TemplateResponse(
            request,
            "partials/lab_reset.html",
            {
                "admin": admin,
                "error": (
                    "База данных отказала: очередь не тронута. Подробности — в логах."
                ),
            },
            status_code=500,
        )
    if request.headers.get("hx-target") == "lab-main-container":
        status = await get_lab_system_status(session)
        groups_data = await get_news_groups_summary(session)
        return templates.TemplateResponse(
            request,
            "partials/lab_main_container.html",
            {
                "admin": admin,
                "status": status,
                "groups_data": groups_data,
                "selected_cluster": None,
                "alert_message": report.summary(),
            },
        )
    return templates.TemplateResponse(
        request,
        "partials/lab_reset.html",
        {
            "admin": admin,
            "report": report.to_dict(),
            "posts_per_source": settings.ingest_max_messages_per_source,
        },
    )


@partials_router.post(
    "/lab/reset/day", response_class=HTMLResponse, summary="Clear one day's queue (HTMX)"
)
async def lab_reset_day_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    day: Annotated[str, Form()],
) -> HTMLResponse:
    """Delete posts collected on one Moscow calendar day.

    Articles are never touched: a day's posts may already have produced material.
    The operator who wants to delete the article must do that explicitly.
    """
    try:
        target = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return templates.TemplateResponse(
            request,
            "partials/lab_reset.html",
            {"admin": admin, "error": "Неверный формат даты. Используйте ГГГГ-ММ-ДД."},
            status_code=400,
        )

    logger.info("%s requested deletion of day %s", admin.username, target.isoformat())
    try:
        report = await delete_day_from_queue(session, day=target)
    except ResetBlockedError as error:
        return templates.TemplateResponse(
            request,
            "partials/lab_reset.html",
            {"admin": admin, "error": str(error)},
            status_code=409,
        )
    except SQLAlchemyError:
        logger.exception("Day deletion failed")
        return templates.TemplateResponse(
            request,
            "partials/lab_reset.html",
            {
                "admin": admin,
                "error": ("База данных отказала: день не удалён. Подробности — в логах."),
            },
            status_code=500,
        )
    return templates.TemplateResponse(
        request,
        "partials/lab_reset.html",
        {
            "admin": admin,
            "report": report.to_dict(),
            "posts_per_source": settings.ingest_max_messages_per_source,
        },
    )


@partials_router.post(
    "/lab/inspect",
    response_class=HTMLResponse,
    summary="Zero-token intake inspection (HTMX)",
)
async def lab_inspect_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    reset: Annotated[int, Query(ge=0, le=1)] = 0,
) -> HTMLResponse:
    """Run the intake inspection: collect, dedup, watermarks, research.

    Never calls a paid model, so it is safe to repeat while tuning thresholds. With
    ``reset=1`` the queue is cleared first, which is what makes the collection a
    first pass over every channel rather than the overlap window.
    """
    logger.info("%s started an inspection run (reset=%s)", admin.username, bool(reset))
    summary = await run_inspection(session, reset=bool(reset))
    ArtifactStore().prune(keep=50)
    return templates.TemplateResponse(
        request,
        "partials/lab_run.html",
        {"admin": admin, "run": summary.to_dict()},
    )


@partials_router.post(
    "/lab/collect-reset",
    response_class=HTMLResponse,
    summary="Reset queue and collect 50 posts per channel (HTMX)",
)
async def lab_collect_reset_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    """Clear queue and run full first-pass collection & MinHash deduplication."""
    logger.info("%s requested lab queue reset and collect", admin.username)
    alert = "Очередь сброшена! Сбор по 50 сообщений и дедупликация успешно завершены."
    try:
        await run_inspection(session, reset=True)
    except Exception as exc:
        logger.exception("Lab collect-reset failed: %s", exc)
        alert = f"Сбор завершён с предупреждением: {exc}"

    status = await get_lab_system_status(session)
    groups_data = await get_news_groups_summary(session)
    return templates.TemplateResponse(
        request,
        "partials/lab_main_container.html",
        {
            "admin": admin,
            "status": status,
            "groups_data": groups_data,
            "selected_cluster": None,
            "alert_message": alert,
        },
    )


@partials_router.post(
    "/lab/collect-only",
    response_class=HTMLResponse,
    summary="Collect news without queue reset (HTMX)",
)
async def lab_collect_only_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    """Collect latest posts and deduplicate into clusters without resetting queue."""
    logger.info("%s requested lab collect without reset", admin.username)
    alert = "Сбор новых сообщений и дедупликация успешно завершены."
    try:
        await run_inspection(session, reset=False)
    except Exception as exc:
        logger.exception("Lab collect-only failed: %s", exc)
        alert = f"Сбор завершён с предупреждением: {exc}"

    status = await get_lab_system_status(session)
    groups_data = await get_news_groups_summary(session)
    return templates.TemplateResponse(
        request,
        "partials/lab_main_container.html",
        {
            "admin": admin,
            "status": status,
            "groups_data": groups_data,
            "selected_cluster": None,
            "alert_message": alert,
        },
    )


@partials_router.get(
    "/lab/cluster/{cluster_id}",
    response_class=HTMLResponse,
    summary="Get cluster detail for stepwise studio (HTMX)",
)
async def lab_cluster_detail_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    cluster_id: uuid.UUID,
) -> HTMLResponse:
    """Load the stepwise studio inspector for a selected news cluster."""
    cluster = await get_cluster_detail(session, cluster_id)
    if cluster is None:
        return HTMLResponse(
            "<div class='p-4 text-xs text-red-600'>Кластер не найден.</div>",
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "partials/lab_cluster_detail.html",
        {"admin": admin, "cluster": cluster},
    )


@partials_router.post(
    "/lab/cluster/{cluster_id}/research",
    response_class=HTMLResponse,
    summary="Run research step for cluster (HTMX)",
)
async def lab_cluster_research_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    cluster_id: uuid.UUID,
) -> HTMLResponse:
    """Run token-free fact extraction and archive interlinks."""
    res = await run_cluster_research_step(session, cluster_id)
    return templates.TemplateResponse(
        request,
        "partials/lab_research_result.html",
        {
            "admin": admin,
            "facts": res["facts"],
            "related_articles": res["related_articles"],
            "web_sources": res.get("web_sources", []),
            "web_facts": res.get("web_facts", []),
            "search_query": res.get("search_query", ""),
        },
    )



@partials_router.post(
    "/lab/unblock-post/{post_id}",
    response_class=HTMLResponse,
    summary="Unblock false-positive filtered post (HTMX)",
)
async def lab_unblock_post_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    post_id: int,
) -> HTMLResponse:
    """Recover a false-positive filtered post into active story clusters."""
    res = await unblock_raw_post(session, post_id)
    status = await get_lab_system_status(session)
    groups_data = await get_news_groups_summary(session)
    return templates.TemplateResponse(
        request,
        "partials/lab_main_container.html",
        {
            "admin": admin,
            "status": status,
            "groups_data": groups_data,
            "selected_cluster": None,
            "alert_message": res["message"],
        },
    )


@partials_router.post(
    "/lab/cluster/{cluster_id}/classify-category",
    response_class=HTMLResponse,
    summary="Intelligent AI rubrication for cluster (HTMX)",
)
async def lab_cluster_classify_category_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    cluster_id: uuid.UUID,
) -> HTMLResponse:
    """Classify cluster into exact rubric with AI justification."""
    await classify_cluster_category(session, cluster_id)
    cluster = await get_cluster_detail(session, cluster_id)
    return templates.TemplateResponse(
        request,
        "partials/lab_rubric_block.html",
        {"admin": admin, "cluster": cluster},
    )


@partials_router.post(
    "/lab/cluster/{cluster_id}/add-category",
    response_class=HTMLResponse,
    summary="Create custom rubric and bind to cluster (HTMX)",
)
async def lab_cluster_add_category_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    *,
    cluster_id: uuid.UUID,
    slug: Annotated[str, Form()],
    title: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Create a new custom rubric on the fly and bind it to the cluster."""
    await create_custom_rubric(
        session, cluster_id=cluster_id, slug=slug, title=title, description=description
    )
    cluster = await get_cluster_detail(session, cluster_id)
    return templates.TemplateResponse(
        request,
        "partials/lab_rubric_block.html",
        {"admin": admin, "cluster": cluster},
    )


_TELEGRAM_SVG_FALLBACK: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270" '
    'viewBox="0 0 480 270" fill="none">'
    '<rect width="480" height="270" fill="#F8FAFC"/>'
    '<circle cx="240" cy="110" r="28" fill="#E2E8F0"/>'
    '<path d="M228 116L236 106L246 118L252 110L260 120H220L228 116Z" fill="#94A3B8"/>'
    '<text x="50%" y="165" text-anchor="middle" fill="#64748B" '
    'font-family="system-ui, -apple-system, sans-serif" font-size="13" '
    'font-weight="600">Фото Telegram</text>'
    '<text x="50%" y="185" text-anchor="middle" fill="#94A3B8" '
    'font-family="system-ui, -apple-system, sans-serif" font-size="11">'
    'Превью в источнике недоступно</text>'
    '</svg>'
)


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


async def _fetch_telegram_photo(
    session: AsyncSession, channel: str, msg_id: int
) -> bytes | None:
    """Download photo on-demand via MTProto client."""
    client = MTProtoIngestor(session)
    try:
        await client.start()
        return await client.download_photo(
            channel, msg_id, limit_bytes=10 * 1024 * 1024
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Telegram on-demand download failed for %s (msg %s): %s",
            channel,
            msg_id,
            e,
        )
        return None
    finally:
        with contextlib.suppress(Exception):
            await client.close()


@partials_router.get(
    "/lab/media/photo/{post_id}",
    summary="Serve cached or on-demand Telegram photo",
)
async def lab_media_photo_endpoint(
    post_id: int,
    session: SessionDep,
) -> Response:
    """Serve photo for post_id from disk cache or fetch on-demand via MTProto."""
    cache_dir = settings.media_storage_path / "telegram"
    cache_file = cache_dir / f"{post_id}.jpg"
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return FileResponse(
            cache_file,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    try:
        post = await session.get(
            RawNews, post_id, options=[selectinload(RawNews.source)]
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Database lookup skipped in photo endpoint for #%s: %s", post_id, exc
        )
        post = None

    if post and post.source:
        channel = post.source.identifier or (
            f"@{post.source.username}" if post.source.username else ""
        )
        msg_id = _extract_post_msg_id(post)
        if channel and msg_id:
            data = await _fetch_telegram_photo(session, channel, msg_id)
            if data:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(data)
                return Response(
                    content=data,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"},
                )

    return Response(
        content=_TELEGRAM_SVG_FALLBACK,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ---------------------------------------------------------------------------
# VK Video Upload from Lab — download via MTProto + upload to VK Video API
# ---------------------------------------------------------------------------

#: Maximum video size accepted for Lab→VK upload (500 MB).
#: A higher ceiling is the VK API limit (2 GB), but 500 MB keeps the container
#: memory safe: the bytes sit in RAM while being streamed to VK's upload URL.
_VK_VIDEO_LAB_LIMIT_BYTES: Final[int] = 500 * 1024 * 1024


async def _fetch_telegram_video(
    session: AsyncSession, channel: str, msg_id: int, *, limit_bytes: int
) -> bytes:
    """Download a video document from Telegram via MTProto."""
    client = MTProtoIngestor(session)
    try:
        await client.start()
        return await client.download_video(channel, msg_id, limit_bytes=limit_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "MTProto video download failed for %s (msg %s): %s",
            channel,
            msg_id,
            exc,
        )
        return b""
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def _resolve_post_video_bytes(
    session: AsyncSession, post: RawNews
) -> tuple[bytes, str]:
    """Retrieve video bytes either from direct URL or via MTProto bridge."""
    import httpx  # noqa: PLC0415

    # Check media_data for a direct HTTP URL (e.g. from a web source).
    for entry in (post.media_data or ()):
        if entry.get("type") != "video":
            continue
        cand = str(entry.get("url") or entry.get("media_id") or "")
        if not cand.startswith(("http://", "https://")):
            continue
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0), follow_redirects=True
            ) as hc:
                resp = await hc.get(cand)
                if (
                    resp.status_code < http_status.HTTP_400_BAD_REQUEST
                    and len(resp.content) <= _VK_VIDEO_LAB_LIMIT_BYTES
                ):
                    fn = cand.rsplit("/", 1)[-1] or "video.mp4"
                    target_fn = fn if fn.lower().endswith(".mp4") else "video.mp4"
                    return resp.content, target_fn
        except Exception:  # noqa: BLE001
            logger.info("Direct URL video fetch failed for post %d", post.id)
        break

    # Fall back to MTProto download from Telegram.
    if post.source:
        channel = post.source.identifier or (
            f"@{post.source.username}" if post.source.username else ""
        )
        msg_id = _extract_post_msg_id(post)
        if channel and msg_id:
            data = await _fetch_telegram_video(
                session, channel, msg_id, limit_bytes=_VK_VIDEO_LAB_LIMIT_BYTES
            )
            if data:
                return data, "video.mp4"

    return b"", "video.mp4"


@partials_router.get(
    "/lab/media/video/{post_id}",
    summary="Serve cached or on-demand Telegram video file",
)
async def lab_media_video_endpoint(
    post_id: int,
    session: SessionDep,
) -> Response:
    """Serve video for post_id from disk cache or fetch on-demand via MTProto."""
    cache_dir = settings.media_storage_path / "telegram"
    cache_file = cache_dir / f"{post_id}.mp4"
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return FileResponse(
            cache_file,
            media_type="video/mp4",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    try:
        post = await session.get(
            RawNews, post_id, options=[selectinload(RawNews.source)]
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Database lookup skipped in video endpoint for #%s: %s", post_id, exc
        )
        post = None

    if post:
        video_bytes, _ = await _resolve_post_video_bytes(session, post)
        if video_bytes:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(video_bytes)
            return FileResponse(
                cache_file,
                media_type="video/mp4",
                headers={"Cache-Control": "public, max-age=86400"},
            )

    return Response(status_code=404, content="Видео недоступно")



async def _persist_vk_video_metadata(
    session: AsyncSession,
    post: RawNews,
    cluster_id: uuid.UUID,
    uploaded: Any,
) -> None:
    """Save VK video embed coords to post.media_data and article.video_links."""
    from app.models.article import Article  # noqa: PLC0415

    vk_link_entry: dict[str, Any] = {
        "owner_id": uploaded.owner_id,
        "video_id": uploaded.video_id,
        "access_key": uploaded.access_key,
        "embed_url": uploaded.embed_url,
        "title": uploaded.title,
        "size_bytes": uploaded.size_bytes,
    }

    updated_media: list[dict[str, Any]] = []
    patched = False
    for entry in (post.media_data or []):
        item = dict(entry)
        if item.get("type") == "video" and not patched:
            item["vk_owner_id"] = uploaded.owner_id
            item["vk_video_id"] = uploaded.video_id
            item["vk_embed_url"] = uploaded.embed_url
            item["vk_access_key"] = uploaded.access_key
            patched = True
        updated_media.append(item)
    post.media_data = updated_media  # type: ignore[assignment]

    result = await session.execute(
        select(Article).where(Article.cluster_id == cluster_id).limit(1)
    )
    article = result.scalar_one_or_none()
    if article is not None:
        existing_links: list[dict[str, Any]] = list(article.video_links or [])
        already_present = any(
            e.get("video_id") == uploaded.video_id
            and e.get("owner_id") == uploaded.owner_id
            for e in existing_links
        )
        if not already_present:
            existing_links.append(vk_link_entry)
            article.video_links = existing_links  # type: ignore[assignment]

    try:
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Could not persist VK video link for post %d", post.id)


@partials_router.post(
    "/lab/cluster/{cluster_id}/video/{post_id}/upload-to-vk",
    response_class=HTMLResponse,
    summary="Download Telegram video and upload to VK Video (HTMX)",
)
async def lab_video_upload_to_vk(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    cluster_id: uuid.UUID,
    post_id: int,
) -> HTMLResponse:
    """Download a Telegram video and host it on VK Video.

    **Closed-Loop (v2.35.0):**
    1. Status: enabled when VK_ACCESS_TOKEN is set in settings.
    2. Interaction: MTProtoIngestor (Telegram), VKVideoService (VK API), RawNews
       and Article tables (PostgreSQL).
    3. Operation: Resolves Telegram channel + message ID, downloads video bytes,
       calls ``video.save`` + multipart upload to VK, stores ``vk_owner_id``,
       ``vk_video_id``, ``vk_embed_url`` in ``media_data`` and ``video_links``.
    4. Goal: VK Video player embedded in articles for higher engagement.
    5. KPI: VK video in post -> >= 2x reach vs. plain text, measured via VK stats.
    6. Feedback: ``article.video_links`` row written -> publisher uses it for VK
       wall post attachment; Metrika measures dwell time on VK.
    7. Hypothesis: Videos uploaded at ingestion time vs. on-demand upload
       increase engagement when the article is still trending (< 2h after event).
    """
    post = await session.get(RawNews, post_id, options=[selectinload(RawNews.source)])
    if post is None:
        return HTMLResponse(
            '<div class="mac-pill mac-pill-error text-[10px]">Пост не найден</div>',
            status_code=404,
        )

    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is None:
        return HTMLResponse(
            '<div class="mac-pill mac-pill-error text-[10px]">Кластер не найден</div>',
            status_code=404,
        )

    from app.services.vk_video_service import (  # noqa: PLC0415
        load_vk_video_config,
        probe_vk_browser_session,
    )

    vk_cfg = await load_vk_video_config(session)
    if not vk_cfg.has_token and vk_cfg.has_session:
        await probe_vk_browser_session(vk_cfg, session=session)

    token = vk_cfg.access_token or settings.vk_access_token
    if not token and not vk_cfg.has_session:
        return HTMLResponse(
            '<div class="mac-pill mac-pill-warn text-[10px]">'
            "⚠️ Сессия браузера или VK_ACCESS_TOKEN не задан в настройках. Перейдите в "
            '<a href="/settings" class="underline text-[#0077FF]">Настройки</a> '
            "и подключите аккаунт VK."
            "</div>"
        )

    video_bytes, filename = await _resolve_post_video_bytes(session, post)
    if not video_bytes:
        return HTMLResponse(
            '<div id="vk-upload-zone-error" class="mac-pill mac-pill-error text-[10px]">'
            "❌ Не удалось скачать видео из Telegram. "
            "Проверьте MTProto-настройки или используйте прямую ссылку."
            "</div>"
        )

    title = str(cluster.title or post.raw_text or "Видео")[:128]
    desc_val = str((cluster.enrichment_data or {}).get("seo_description") or "")[:512]

    try:
        uploaded = await upload_article_video(
            video_bytes,
            name=title,
            description=desc_val,
            filename=filename,
            session=session,
        )
    except VKError as exc:
        logger.warning("VK video upload failed for post %d: %s", post_id, exc)
        return HTMLResponse(
            f'<div class="mac-pill mac-pill-error text-[10px]">'
            f"❌ VK отклонил загрузку: {exc}"
            f"</div>"
        )

    await _persist_vk_video_metadata(session, post, cluster_id, uploaded)

    embed_url = uploaded.embed_url
    logger.info(
        "Lab VK upload: post=%d owner=%d video=%d size=%d bytes",
        post_id,
        uploaded.owner_id,
        uploaded.video_id,
        uploaded.size_bytes,
    )
    return HTMLResponse(
        f'<div class="pt-1 space-y-2">'
        f'  <span class="mac-pill mac-pill-good text-[10px] font-semibold">'
        f"    ✅ Загружено в VK Video (owner={uploaded.owner_id}, id={uploaded.video_id})"
        f"  </span>"
        f'  <div class="rounded-lg overflow-hidden aspect-video border '
        f'border-black/[0.08]">'
        f'    <iframe src="{embed_url}"'
        f'            class="w-full h-full"'
        f'            allow="encrypted-media; fullscreen; picture-in-picture"'
        f"            allowfullscreen"
        f'            frameborder="0"></iframe>'
        f"  </div>"
        f"</div>"
    )


@partials_router.post(
    "/lab/cluster/{cluster_id}/regenerate-prompt",
    response_class=HTMLResponse,
    summary="Regenerate editorial image prompt (HTMX)",
)
async def lab_cluster_regenerate_prompt_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    cluster_id: uuid.UUID,
) -> HTMLResponse:
    """Regenerate high-quality photorealistic editorial prompt using AI."""
    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is None:
        return HTMLResponse(
            '<div class="text-xs text-rose-500">Кластер не найден</div>',
            status_code=404,
        )
    new_prompt = await generate_editorial_image_prompt(session, cluster)
    if cluster.enrichment_data is None:
        cluster.enrichment_data = {}
    cluster.enrichment_data["fallback_image_prompt"] = new_prompt
    await session.commit()
    return HTMLResponse(
        '<textarea id="media-prompt-input" name="prompt" rows="2" '
        'class="w-full rounded-lg border border-black/[0.12] bg-white p-2.5 '
        'text-xs text-mac-ink focus:border-[#0071E3] focus:outline-none" '
        f'placeholder="Промт для генерации обложки…">{new_prompt}</textarea>'
    )


@partials_router.post(
    "/lab/cluster/{cluster_id}/media",
    response_class=HTMLResponse,
    summary="Run media step for cluster (HTMX)",
)
async def lab_cluster_media_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    cluster_id: uuid.UUID,
) -> HTMLResponse:
    """Run OCR watermark inspection and media evaluation."""
    res = await run_cluster_media_step(session, cluster_id)
    return templates.TemplateResponse(
        request,
        "partials/lab_media_result.html",
        {
            "admin": admin,
            "images": res["images"],
            "web_images": res.get("web_images", []),
            "rejected_web_images": res.get("rejected_web_images", []),
            "cluster_id": cluster_id,
            "fallback_needed": res.get("fallback_needed", False),
            "fallback_prompt": res.get(
                "fallback_prompt", res.get("suggested_prompt", "")
            ),
            "generation_error": "",
        },
    )


@partials_router.post(
    "/lab/cluster/{cluster_id}/generate-image",
    response_class=HTMLResponse,
    summary="Generate fallback cover image (HTMX)",
)
async def lab_cluster_generate_image_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    cluster_id: uuid.UUID,
    prompt: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Generate cover image for a cluster using Flux Schnell."""
    res = await generate_fallback_image_for_cluster(session, cluster_id, prompt)
    cluster = await session.get(NewsCluster, cluster_id)
    web_images = (
        list((cluster.enrichment_data or {}).get("web_images", []))
        if cluster
        else []
    )
    return templates.TemplateResponse(
        request,
        "partials/lab_media_result.html",
        {
            "admin": admin,
            "images": res["images"],
            "web_images": web_images,
            "cluster_id": cluster_id,
            "fallback_needed": False,
            "suggested_prompt": prompt,
            "generation_error": res.get("generation_error"),
        },
    )


@partials_router.get(
    "/lab/media/web-image",
    summary="Proxy and cache external web image without Referer header",
)
async def lab_web_image_proxy(
    url: str,
) -> Response:
    """Fetch and cache external news article images, bypassing anti-hotlink blocks."""
    import hashlib  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from app.services.web_researcher import is_safe_web_url  # noqa: PLC0415

    if not is_safe_web_url(url):
        return Response(content=b"", status_code=400, media_type="image/jpeg")

    # Local disk cache under settings.media_storage_path / "web_cache"
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    cache_dir = settings.media_storage_path / "web_cache"
    cache_file = cache_dir / f"{url_hash}.img"

    if cache_file.exists():
        try:
            cached_data = cache_file.read_bytes()
            if len(cached_data) > 0:
                mime = "image/jpeg"
                if cached_data.startswith(b"\x89PNG"):
                    mime = "image/png"
                elif cached_data.startswith(b"GIF"):
                    mime = "image/gif"
                elif cached_data.startswith(b"RIFF") and b"WEBP" in cached_data[:12]:
                    mime = "image/webp"
                return Response(
                    content=cached_data,
                    media_type=mime,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        except OSError:
            pass

    # Fetch from source without Referer header to bypass hotlink protection
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200 and resp.content:
                content_type = resp.headers.get("content-type", "image/jpeg")
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_file.write_bytes(resp.content)
                except OSError:
                    pass
                return Response(
                    content=resp.content,
                    media_type=content_type,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to proxy web image %s: %s", url, exc)

    fallback_svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200">'
        b'<rect width="300" height="200" fill="#f3f4f6"/>'
        b'<text x="50%" y="50%" text-anchor="middle" fill="#9ca3af" '
        b'font-size="12" font-family="sans-serif">&#x1F4F7; Media</text>'
        b"</svg>"
    )
    return Response(content=fallback_svg, media_type="image/svg+xml")


@partials_router.post(
    "/lab/cluster/{cluster_id}/generate",
    response_class=HTMLResponse,
    summary="Run generate step for cluster (HTMX)",
)
async def lab_cluster_generate_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    cluster_id: uuid.UUID,
    *,
    style_angle: Annotated[str, Form()] = "Информационный",
    custom_instructions: Annotated[str, Form()] = "",
    prompt_override: Annotated[str, Form()] = "",
    selected_cover_url: Annotated[str, Form()] = "",
    promoted_project_id: Annotated[str, Form()] = "",
    image_layout: Annotated[str, Form()] = "inline",
    override_brand_safety: Annotated[bool, Form()] = False,
) -> HTMLResponse:
    """Run AI moderation, generation, mandatory internal links and SEO/Wordstat."""
    res = await run_cluster_generate_step(
        session,
        cluster_id,
        style_angle=style_angle,
        custom_instructions=custom_instructions,
        prompt_override=prompt_override,
        selected_cover_url=selected_cover_url,
        promoted_project_id=promoted_project_id,
        image_layout=image_layout,
        override_brand_safety=override_brand_safety,
    )
    return templates.TemplateResponse(
        request,
        "partials/lab_generate_result.html",
        {
            "admin": admin,
            "title": res["title"],
            "content_html": res["content_html"],
            "image_layout": res.get("image_layout", "inline"),
            "seo_title": res.get("seo_title"),
            "seo_description": res["seo_description"],
            "seo_keywords": res["seo_keywords"],
            "focus_keyword": res.get("focus_keyword"),
            "wordstat_queries": res.get("wordstat_queries", []),
            "wordstat_source": res.get("wordstat_source", "yandex_api"),
            "schema_json_ld": res.get("schema_json_ld"),
            "lead_evaluation": res.get("lead_evaluation"),
            "faq_items": res.get("faq_items", []),
            "llm_txt": res.get("llm_txt", ""),
            "internal_links": res.get("internal_links", []),
            "article_id": res["article_id"],
            "slop_clean": res["slop_clean"],
            "slop_density": res["slop_density"],
            "usage_stats": res.get("usage_stats"),
            "aevi": res.get("aevi"),
            "hypothesis": res.get("hypothesis"),
            "dzen_preview_html": res.get("dzen_preview_html", ""),
            "tg_caption": res.get("tg_caption", ""),
            "tg_hashtags": res.get("tg_hashtags", []),
            "vk_wall_message": res.get("vk_wall_message", ""),
            "cover_url": res.get("cover_url", ""),
            "cluster_id": res.get("cluster_id"),
            "is_brand_safe": res.get("is_brand_safe", True),
            "brand_safety_reason": res.get("brand_safety_reason", ""),
            "promoted_project_id": res.get("promoted_project_id", ""),
            "promoted_brand_name": res.get("promoted_brand_name", ""),
            "tracked_url": res.get("tracked_url", ""),
            "cqi": res.get("cqi"),
        },
    )


@partials_router.post(
    "/lab/cluster/{cluster_id}/publish",
    response_class=HTMLResponse,
    summary="Run publish step for cluster (HTMX)",
)
async def lab_cluster_publish_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    cluster_id: uuid.UUID,
) -> HTMLResponse:
    """Publish generated article to WordPress via REST API."""
    res = await run_cluster_publish_step(session, cluster_id)
    return templates.TemplateResponse(
        request,
        "partials/lab_publish_result.html",
        {
            "admin": admin,
            "cluster_id": str(cluster_id),
            "ok": res["ok"],
            "post_id": res.get("post_id"),
            "post_url": res.get("post_url"),
            "message": res["message"],
            "warnings": res.get("warnings", []),
            "is_dzen": False,
        },
    )


@partials_router.post(
    "/lab/cluster/{cluster_id}/publish-dzen",
    response_class=HTMLResponse,
    summary="Publish article to Dzen Studio (HTMX)",
)
async def lab_cluster_publish_dzen_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    cluster_id: uuid.UUID,
) -> HTMLResponse:
    """Publish generated article to Dzen Studio via Playwright."""
    from app.services.lab_pipeline import run_cluster_publish_dzen_step  # noqa: PLC0415

    res = await run_cluster_publish_dzen_step(session, cluster_id)
    return templates.TemplateResponse(
        request,
        "partials/lab_publish_result.html",
        {
            "admin": admin,
            "cluster_id": str(cluster_id),
            "ok": res["ok"],
            "post_id": res.get("post_id"),
            "post_url": res.get("post_url"),
            "message": res["message"],
            "warnings": res.get("warnings", []),
            "is_dzen": True,
        },
    )


@partials_router.post(
    "/lab/run", response_class=HTMLResponse, summary="Run one observable pass (HTMX)"
)
async def lab_run_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    collect: Annotated[int, Query(ge=0, le=1)] = 1,
    cluster: Annotated[str | None, Query(max_length=36)] = None,
) -> HTMLResponse:
    """Execute one test pass and return its timeline.

    Synchronous by design: the operator pressed a button to watch this happen, and
    a background job would mean polling for a result they are already waiting for.
    Collection is optional because re-running generation on an existing cluster is
    the common case while tuning prompts, and pulling fresh posts every time would
    add Telegram traffic for no benefit.

    ``cluster`` writes the story the operator picked from the inspection timeline
    instead of the newest unwritten one. A malformed id is refused rather than
    ignored: falling back to "newest" would spend a writer call on a different
    story than the one chosen.
    """
    chosen: uuid.UUID | None = None
    if cluster:
        try:
            chosen = uuid.UUID(cluster)
        except ValueError:
            return templates.TemplateResponse(
                request,
                "partials/lab_run.html",
                {
                    "admin": admin,
                    "error": f"Некорректный идентификатор кластера: {cluster}",
                },
                status_code=422,
            )

    logger.info(
        "%s started a lab run (collect=%s, cluster=%s)",
        admin.username,
        bool(collect),
        chosen or "newest",
    )
    runner = PipelineRunner(session, mode=RunMode.TEST)
    summary = await runner.run(collect=bool(collect), cluster_id=chosen)
    # Bounded history: a run holds prompts and a full article, and the volume is
    # shared with the media pipeline.
    ArtifactStore().prune(keep=50)
    return templates.TemplateResponse(
        request,
        "partials/lab_run.html",
        {"admin": admin, "run": summary.to_dict()},
    )


@partials_router.get(
    "/lab/runs", response_class=HTMLResponse, summary="Recent lab runs (HTMX)"
)
async def lab_runs_partial(request: Request, admin: PartialAdmin) -> HTMLResponse:
    """Return the recent-run list."""
    return templates.TemplateResponse(
        request,
        "partials/lab_runs.html",
        {"admin": admin, "runs": ArtifactStore().recent(limit=20)},
    )


@partials_router.get(
    "/lab/runs/{run_id}",
    response_class=HTMLResponse,
    summary="One lab run timeline (HTMX)",
)
async def lab_run_detail_partial(
    request: Request, admin: PartialAdmin, run_id: str
) -> HTMLResponse:
    """Replay one stored run from its artifacts."""
    try:
        stored = ArtifactStore().read_summary(run_id)
    except ValueError:
        # `run_id` arrives from the URL; the store rejects anything that is not a
        # generated id rather than letting it walk the filesystem.
        stored = None
    if stored is None:
        return templates.TemplateResponse(
            request,
            "partials/lab_run.html",
            {"admin": admin, "error": f"Прогон {run_id} не найден."},
            status_code=404,
        )
    return templates.TemplateResponse(
        request, "partials/lab_run.html", {"admin": admin, "run": stored}
    )


@partials_router.post(
    "/lab/step/start",
    response_class=HTMLResponse,
    summary="Start a new durable manual laboratory run (HTMX)",
)
async def lab_step_start_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    collect: Annotated[int, Query(ge=0, le=1)] = 1,
) -> HTMLResponse:
    """Create a snapshot run; the collection/dedup decision stops for review."""
    from app.services.lab_runner import (  # noqa: PLC0415
        LabWorkflowError,
        LabWorkflowService,
    )

    service = LabWorkflowService(session)
    try:
        run = await service.create_run(collect=bool(collect))
    except LabWorkflowError as error:
        return templates.TemplateResponse(
            request,
            "partials/lab_stepwise.html",
            {"admin": admin, "error": str(error)},
            status_code=400,
        )
    except Exception:
        logger.exception("Manual lab run start failed")
        return templates.TemplateResponse(
            request,
            "partials/lab_stepwise.html",
            {
                "admin": admin,
                "error": "Не удалось создать прогон. Подробности — в журнале сервера.",
            },
            status_code=500,
        )
    return templates.TemplateResponse(
        request,
        "partials/lab_stepwise.html",
        {"admin": admin, "run": run},
    )


@partials_router.post(
    "/lab/step/next",
    response_class=HTMLResponse,
    summary="Advance to next step in stepwise lab run (HTMX)",
)
async def lab_step_next_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    run_id: Annotated[str, Form()],
    parallel: Annotated[int, Form()] = 0,
) -> HTMLResponse:
    """Advance or refresh the current manual laboratory run."""
    from app.services.lab_runner import LabWorkflowService  # noqa: PLC0415

    service = LabWorkflowService(session)
    try:
        run = await service.load_run(run_id)
    except Exception:
        logger.exception("Failed to advance lab run %s", run_id)
        return templates.TemplateResponse(
            request,
            "partials/lab_stepwise.html",
            {"admin": admin, "error": f"Прогон {run_id} не найден."},
            status_code=404,
        )

    return templates.TemplateResponse(
        request,
        "partials/lab_stepwise.html",
        {"admin": admin, "run": run},
    )


@partials_router.post(
    "/lab/items/{item_id}/stages/{stage}/run",
    response_class=HTMLResponse,
    summary="Queue a selected manual laboratory stage (HTMX)",
)
async def lab_stage_queue_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    item_id: int,
    stage: str,
) -> HTMLResponse:
    """Queue only an explicitly selected eligible item and stage."""
    from app.models.lab_attempt import LabStage  # noqa: PLC0415
    from app.services.lab_runner import (  # noqa: PLC0415
        LabWorkflowError,
        LabWorkflowService,
    )

    try:
        chosen_stage = LabStage(stage)
    except ValueError:
        return templates.TemplateResponse(
            request,
            "partials/lab_stepwise.html",
            {"admin": admin, "error": "Неизвестный этап."},
            status_code=404,
        )
    service = LabWorkflowService(session)
    try:
        attempt = await service.queue_stage(item_id, chosen_stage)
        item = await session.get(LabRunItem, attempt.lab_run_item_id)
        if item is None:
            raise LabWorkflowError("Новость прогона не найдена.")
        run_row = await session.get(LabRun, item.lab_run_id)
        if run_row is None:
            raise LabWorkflowError("Прогон не найден.")
        run = await service.load_run(run_row.run_id)
    except LabWorkflowError as error:
        return templates.TemplateResponse(
            request,
            "partials/lab_stepwise.html",
            {"admin": admin, "error": str(error)},
            status_code=409,
        )
    return templates.TemplateResponse(
        request,
        "partials/lab_stepwise.html",
        {"admin": admin, "run": run},
    )


@partials_router.post(
    "/lab/attempts/{attempt_id}/{decision}",
    response_class=HTMLResponse,
    summary="Approve or reject an exact manual laboratory result version (HTMX)",
)
async def lab_attempt_decision_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    attempt_id: int,
    decision: str,
) -> HTMLResponse:
    """Persist an operator decision only for the rendered current attempt version."""
    from app.services.lab_runner import (  # noqa: PLC0415
        LabWorkflowError,
        LabWorkflowService,
    )

    service = LabWorkflowService(session)
    try:
        if decision == "approve":
            attempt = await service.approve_attempt(attempt_id)
        elif decision == "reject":
            attempt = await service.reject_attempt(attempt_id)
        else:
            raise LabWorkflowError("Неизвестное решение.")
        item = await session.get(LabRunItem, attempt.lab_run_item_id)
        if item is None:
            raise LabWorkflowError("Новость прогона не найдена.")
        run_row = await session.get(LabRun, item.lab_run_id)
        if run_row is None:
            raise LabWorkflowError("Прогон не найден.")
        run = await service.load_run(run_row.run_id)
    except LabWorkflowError as error:
        return templates.TemplateResponse(
            request,
            "partials/lab_stepwise.html",
            {"admin": admin, "error": str(error)},
            status_code=409,
        )
    return templates.TemplateResponse(
        request,
        "partials/lab_stepwise.html",
        {"admin": admin, "run": run},
    )


@partials_router.get(
    "/lab/step/status",
    response_class=HTMLResponse,
    summary="Get durable manual laboratory run status (HTMX)",
)
async def lab_step_status_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    run_id: Annotated[str, Query()],
) -> HTMLResponse:
    """Refresh a manual run without starting new work."""
    from app.services.lab_runner import (  # noqa: PLC0415
        LabWorkflowError,
        LabWorkflowService,
    )

    try:
        run = await LabWorkflowService(session).load_run(run_id)
    except LabWorkflowError as error:
        return templates.TemplateResponse(
            request,
            "partials/lab_stepwise.html",
            {"admin": admin, "error": str(error)},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "partials/lab_stepwise.html",
        {"admin": admin, "run": run},
    )


# ------------------------------------------------------- AI provider keys


@partials_router.get(
    "/lab/providers",
    response_class=HTMLResponse,
    summary="List AI provider keys (HTMX)",
)
async def lab_providers_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    """Render the AI provider keys list grouped by provider name."""
    from app.services.ai_provider_keys import list_provider_keys  # noqa: PLC0415

    providers = await list_provider_keys(session)
    grouped: dict[str, list[AIProviderKey]] = {}
    for provider in providers:
        grouped.setdefault(provider.provider_name, []).append(provider)

    return templates.TemplateResponse(
        request,
        "partials/ai_providers.html",
        {"admin": admin, "grouped_providers": grouped},
    )


@partials_router.post(
    "/lab/providers/add",
    response_class=HTMLResponse,
    summary="Add an AI provider key (HTMX)",
)
async def lab_provider_add_partial(  # noqa: PLR0917 - FastAPI injects each
    # argument by name: three dependencies and three form fields.
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    provider_name: Annotated[str, Form()],
    api_key: Annotated[str, Form()],
    base_url: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Add a new provider key and discover models."""
    from app.services.ai_provider_keys import (  # noqa: PLC0415
        ProviderKeyError,
        add_provider_key,
        list_provider_keys,
    )

    try:
        await add_provider_key(
            session,
            provider_name=provider_name,
            api_key=api_key,
            base_url=base_url or None,
        )
    except ProviderKeyError as error:
        return templates.TemplateResponse(
            request,
            "partials/ai_providers.html",
            {"admin": admin, "error": str(error), "grouped_providers": {}},
            status_code=400,
        )

    providers = await list_provider_keys(session)
    grouped: dict[str, list[AIProviderKey]] = {}
    for provider in providers:
        grouped.setdefault(provider.provider_name, []).append(provider)

    return templates.TemplateResponse(
        request,
        "partials/ai_providers.html",
        {"admin": admin, "grouped_providers": grouped},
    )


@partials_router.post(
    "/lab/providers/{key_id}/delete",
    response_class=HTMLResponse,
    summary="Delete an AI provider key (HTMX)",
)
async def lab_provider_delete_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    key_id: int,
) -> HTMLResponse:
    """Delete a provider key and all its models."""
    from app.services.ai_provider_keys import (  # noqa: PLC0415
        delete_provider_key,
        list_provider_keys,
    )

    await delete_provider_key(session, key_id)

    providers = await list_provider_keys(session)
    grouped: dict[str, list[AIProviderKey]] = {}
    for provider in providers:
        grouped.setdefault(provider.provider_name, []).append(provider)

    return templates.TemplateResponse(
        request,
        "partials/ai_providers.html",
        {"admin": admin, "grouped_providers": grouped},
    )


@partials_router.post(
    "/lab/providers/{key_id}/discover",
    response_class=HTMLResponse,
    summary="Discover models for an AI provider key (HTMX)",
)
async def lab_provider_discover_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    key_id: int,
) -> HTMLResponse:
    """Re-discover models for a provider key."""
    from app.services.ai_provider_keys import (  # noqa: PLC0415
        ProviderKeyError,
        discover_models,
        list_provider_keys,
    )

    try:
        await discover_models(session, key_id)
    except ProviderKeyError as error:
        return templates.TemplateResponse(
            request,
            "partials/ai_providers.html",
            {"admin": admin, "error": str(error), "grouped_providers": {}},
            status_code=400,
        )

    providers = await list_provider_keys(session)
    grouped: dict[str, list[AIProviderKey]] = {}
    for provider in providers:
        grouped.setdefault(provider.provider_name, []).append(provider)

    return templates.TemplateResponse(
        request,
        "partials/ai_providers.html",
        {"admin": admin, "grouped_providers": grouped},
    )


@partials_router.post(
    "/lab/models/{model_id}/test",
    response_class=HTMLResponse,
    summary="Test an AI provider model (HTMX)",
)
async def lab_model_test_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    model_id: int,
) -> HTMLResponse:
    """Test one model and return updated status."""
    from app.services.ai_provider_keys import (  # noqa: PLC0415
        ProviderKeyError,
        list_provider_keys,
        test_model,
    )

    try:
        await test_model(session, model_id)
    except ProviderKeyError as error:
        return templates.TemplateResponse(
            request,
            "partials/ai_providers.html",
            {"admin": admin, "error": str(error), "grouped_providers": {}},
            status_code=400,
        )

    providers = await list_provider_keys(session)
    grouped: dict[str, list[AIProviderKey]] = {}
    for provider in providers:
        grouped.setdefault(provider.provider_name, []).append(provider)

    return templates.TemplateResponse(
        request,
        "partials/ai_providers.html",
        {"admin": admin, "grouped_providers": grouped},
    )


@partials_router.post(
    "/lab/providers/test-all",
    response_class=HTMLResponse,
    summary="Test all models for all providers (HTMX)",
)
async def lab_providers_test_all_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    """Test all models for all active provider keys."""
    from app.services.ai_provider_keys import (  # noqa: PLC0415
        list_provider_keys,
        test_all_providers,
    )

    try:
        results = await test_all_providers(session)
    except Exception as error:  # noqa: BLE001 - render operator-facing failure
        return templates.TemplateResponse(
            request,
            "partials/ai_providers.html",
            {
                "admin": admin,
                "error": f"Ошибка тестирования: {error}",
                "grouped_providers": {},
            },
            status_code=500,
        )

    providers = await list_provider_keys(session)
    grouped: dict[str, list[AIProviderKey]] = {}
    for provider in providers:
        grouped.setdefault(provider.provider_name, []).append(provider)

    return templates.TemplateResponse(
        request,
        "partials/ai_providers.html",
        {"admin": admin, "grouped_providers": grouped, "test_results": results},
    )


@partials_router.post(
    "/lab/providers/{key_id}/test-all",
    response_class=HTMLResponse,
    summary="Test all models for a provider key (HTMX)",
)
async def lab_provider_test_all_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    key_id: int,
) -> HTMLResponse:
    """Test all models for one provider key."""
    from app.services.ai_provider_keys import (  # noqa: PLC0415
        list_provider_keys,
        test_all_models,
    )

    try:
        result = await test_all_models(session, key_id)
    except Exception as error:  # noqa: BLE001 - render operator-facing failure
        return templates.TemplateResponse(
            request,
            "partials/ai_providers.html",
            {
                "admin": admin,
                "error": f"Ошибка тестирования: {error}",
                "grouped_providers": {},
            },
            status_code=500,
        )

    providers = await list_provider_keys(session)
    grouped: dict[str, list[AIProviderKey]] = {}
    for provider in providers:
        grouped.setdefault(provider.provider_name, []).append(provider)

    return templates.TemplateResponse(
        request,
        "partials/ai_providers.html",
        {
            "admin": admin,
            "grouped_providers": grouped,
            "test_results": {f"key_{key_id}": result},
        },
    )


@partials_router.post(
    "/lab/providers/{key_id}/test",
    response_class=HTMLResponse,
    summary="Test provider key (HTMX)",
)
async def lab_provider_test_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    key_id: int,
) -> HTMLResponse:
    """Test one provider key with a simple request."""
    from app.services.ai_provider_keys import (  # noqa: PLC0415
        list_provider_keys,
        test_provider_key,
    )

    try:
        is_working = await test_provider_key(session, key_id)
    except Exception as error:  # noqa: BLE001 - render operator-facing failure
        return templates.TemplateResponse(
            request,
            "partials/ai_providers.html",
            {
                "admin": admin,
                "error": f"Ошибка тестирования: {error}",
                "grouped_providers": {},
            },
            status_code=500,
        )

    providers = await list_provider_keys(session)
    grouped: dict[str, list[AIProviderKey]] = {}
    for provider in providers:
        grouped.setdefault(provider.provider_name, []).append(provider)

    return templates.TemplateResponse(
        request,
        "partials/ai_providers.html",
        {
            "admin": admin,
            "grouped_providers": grouped,
            "test_result": {"key_id": key_id, "is_working": is_working},
        },
    )


# ------------------------------------------------ Google AI Studio 20-Key Pool


@partials_router.get(
    "/ai/keys-pool",
    response_class=HTMLResponse,
    summary="AI keys pool partial (HTMX)",
)
async def ai_keys_pool_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    """Render AI provider keys pool partial."""
    pools = await list_all_provider_pools(session)
    keys = await list_gemini_keys(session)
    return templates.TemplateResponse(
        request,
        "partials/ai_keys_pool.html",
        {"admin": admin, "pools": pools, "keys": keys},
    )


@partials_router.get(
    "/ai/usage-stats",
    response_class=HTMLResponse,
    summary="AI token usage and stability partial (HTMX)",
)
async def ai_usage_stats_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=180)] = 7,
) -> HTMLResponse:
    """Render AI token usage and provider stability partial."""
    from app.services.usage_log import get_system_usage_report  # noqa: PLC0415

    usage_report = await get_system_usage_report(session, days=days)
    return templates.TemplateResponse(
        request,
        "partials/ai_usage_stats.html",
        {"admin": admin, "usage_report": usage_report, "selected_days": days},
    )


@partials_router.post(
    "/ai/batch-add",
    response_class=HTMLResponse,
    summary="Batch add AI provider keys (HTMX)",
)
async def ai_batch_add_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    *,
    raw_keys: Annotated[str, Form()] = "",
    provider_name: Annotated[str, Form()] = "google",
    base_url: Annotated[str, Form()] = "",
    custom_name: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Batch add API keys for a given provider without key count limits."""
    res = await batch_add_keys_for_provider(
        session,
        provider_name,
        raw_keys,
        base_url,
        custom_name=custom_name.strip() or None,
    )
    pools = await list_all_provider_pools(session)
    keys = await list_gemini_keys(session)
    p_label = res.get("provider", provider_name)
    if res.get("errors"):
        alert = f"Внимание: {', '.join(res['errors'])}"
    else:
        alert = f"Успешно добавлено {res.get('added', 0)} новых ключей в пул '{p_label}'."
    return templates.TemplateResponse(
        request,
        "partials/ai_keys_pool.html",
        {"admin": admin, "pools": pools, "keys": keys, "alert_message": alert},
    )


@partials_router.delete(
    "/ai/keys/{key_id}",
    response_class=HTMLResponse,
    summary="Delete AI provider key (HTMX)",
)
async def ai_delete_key_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    key_id: int,
) -> HTMLResponse:
    """Delete a key from the pool."""
    await delete_key_from_pool(session, key_id)
    pools = await list_all_provider_pools(session)
    keys = await list_gemini_keys(session)
    return templates.TemplateResponse(
        request,
        "partials/ai_keys_pool.html",
        {
            "admin": admin,
            "pools": pools,
            "keys": keys,
            "alert_message": f"Ключ #{key_id} удален из пула.",
        },
    )


@partials_router.post(
    "/ai/keys/{key_id}/test",
    response_class=HTMLResponse,
    summary="Test individual key capabilities (HTMX)",
)
async def ai_test_key_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    key_id: int,
) -> HTMLResponse:
    """Test capabilities dynamically for a single provider key."""
    res = await test_key_in_pool(session, key_id)
    pools = await list_all_provider_pools(session)
    keys = await list_gemini_keys(session)
    status_str = (
        "все тесты пройдены успешно"
        if res.get("all_working") or res.get("ok")
        else "обнаружены ошибки в возможностях"
    )
    alert = f"Тестирование ключа #{key_id} завершено ({status_str})."
    return templates.TemplateResponse(
        request,
        "partials/ai_keys_pool.html",
        {"admin": admin, "pools": pools, "keys": keys, "alert_message": alert},
    )


@partials_router.post(
    "/ai/keys/test-all",
    response_class=HTMLResponse,
    summary="Test all keys in provider pool (HTMX)",
)
async def ai_test_all_keys_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    provider_name: Annotated[str, Form()] = "",
    custom_name: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Test all keys in a provider pool (or across all pools)."""
    pname = provider_name.strip() if provider_name.strip() else None
    cname = custom_name.strip() if custom_name.strip() else None
    await test_all_keys_in_pool(session, pname, cname)
    pools = await list_all_provider_pools(session)
    keys = await list_gemini_keys(session)
    target_label = f"пула '{cname or pname}'" if (cname or pname) else "всех пулов"
    return templates.TemplateResponse(
        request,
        "partials/ai_keys_pool.html",
        {
            "admin": admin,
            "pools": pools,
            "keys": keys,
            "alert_message": f"Проверка ключей {target_label} завершена.",
        },
    )


@partials_router.post(
    "/ai/roles",
    response_class=HTMLResponse,
    summary="Save AI model roles matrix (HTMX)",
)
async def ai_roles_update_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    *,
    role_arbiter: Annotated[str, Form()] = "",
    role_writer: Annotated[str, Form()] = "",
    role_seo: Annotated[str, Form()] = "",
    role_embedding: Annotated[str, Form()] = "",
    role_image: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Save AI model role assignments matrix and return updated partial."""
    overrides = {
        "arbiter": role_arbiter.strip() or "gemini-2.5-flash",
        "writer": role_writer.strip() or "gemini-2.5-flash",
        "seo": role_seo.strip() or "gemini-2.5-flash",
        "embedding": role_embedding.strip() or "local:fastembed",
        "image": role_image.strip() or "fal-ai/flux/schnell",
    }
    alert = "✓ Матрица моделей успешно сохранена"
    try:
        await save_model_routing(session, overrides)
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        alert = f"Ошибка сохранения: {exc}"

    routing = await load_model_routing(session)
    catalog = await get_working_models_catalog(session)
    chains = await load_chains(session)
    return templates.TemplateResponse(
        request,
        "partials/ai_roles_matrix.html",
        {
            "admin": admin,
            "routing": routing,
            "catalog": catalog,
            "chains": chains,
            "alert_message": alert,
        },
    )


@partials_router.post(
    "/ai/roles/auto",
    response_class=HTMLResponse,
    summary="Auto-select and save best AI models (HTMX)",
)
async def ai_roles_auto_select_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Auto-detect and assign the optimal verified models across all active pools."""
    auto_models = await auto_select_best_models(session)
    await save_model_routing(session, auto_models)
    routing = await load_model_routing(session)
    catalog = await get_working_models_catalog(session)
    chains = await load_chains(session)
    arb_chain = " ➔ ".join(chains.get("arbiter", [routing.arbiter]))
    wri_chain = " ➔ ".join(chains.get("writer", [routing.writer]))
    seo_chain = " ➔ ".join(chains.get("seo", [routing.seo]))
    alert = (
        f"✨ Автоподбор и цепочки отказоустойчивости применены: "
        f"Арбитр [{arb_chain}], "
        f"Генератор [{wri_chain}], "
        f"SEO [{seo_chain}], "
        f"Эмбеддинги [{routing.embedding}], "
        f"Обложки [{routing.image}]"
    )
    return templates.TemplateResponse(
        request,
        "partials/ai_roles_matrix.html",
        {
            "admin": admin,
            "routing": routing,
            "catalog": catalog,
            "chains": chains,
            "alert_message": alert,
        },
    )


@partials_router.post(
    "/ai/models/{model_id}/test",
    response_class=HTMLResponse,
    summary="Test individual AI model from discovered list (HTMX)",
)
async def ai_model_test_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    model_id: int,
) -> HTMLResponse:
    res = await test_individual_model_by_id(session, model_id)
    m = {
        "id": res.get("model_id", f"#{model_id}"),
        "db_id": model_id,
        "is_working": res.get("ok", False),
        "error": res.get("error"),
    }
    return templates.TemplateResponse(
        request,
        "partials/ai_model_row.html",
        {"admin": admin, "m": m},
    )


@partials_router.post(
    "/ai/keys/{key_id}/test-top-models",
    response_class=HTMLResponse,
    summary="Test top discovered models for provider key (HTMX)",
)
async def ai_key_test_top_models_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    key_id: int,
) -> HTMLResponse:
    """Batch test top 5 models for a specific key."""
    res = await test_top_models_for_key(session, key_id, limit=5)
    pools = await list_all_provider_pools(session)
    keys = await list_gemini_keys(session)
    alert = (
        f"Тестирование топ-моделей ключа #{key_id} завершено: "
        f"{res['working']} из {res['tested']} моделей работают."
    )
    return templates.TemplateResponse(
        request,
        "partials/ai_keys_pool.html",
        {"admin": admin, "pools": pools, "keys": keys, "alert_message": alert},
    )


@partials_router.post(
    "/ai/keys/{key_id}/test-all-models",
    response_class=HTMLResponse,
    summary="Test 100% discovered models for provider key (HTMX)",
)
async def ai_key_test_all_models_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    key_id: int,
) -> HTMLResponse:
    """Batch test 100% of discovered models for a specific key."""
    res = await test_all_models_for_key(session, key_id, concurrency=6)
    pools = await list_all_provider_pools(session)
    keys = await list_gemini_keys(session)
    tot_w = res.get("total_working", res["working"])
    alert = (
        f"Полное тестирование всех моделей ключа #{key_id} завершено: "
        f"{res['working']} из {res['tested']} моделей работают "
        f"(всего рабочих: {tot_w})."
    )
    return templates.TemplateResponse(
        request,
        "partials/ai_keys_pool.html",
        {"admin": admin, "pools": pools, "keys": keys, "alert_message": alert},
    )


@partials_router.post(
    "/ai/keys/test-all-models",
    response_class=HTMLResponse,
    summary="Test 100% models across all provider pools (HTMX)",
)
async def ai_test_all_models_global_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Batch test 100% models across all provider keys."""
    res = await test_all_models_across_all_keys(session, concurrency=6)
    pools = await list_all_provider_pools(session)
    keys = await list_gemini_keys(session)
    alert = (
        f"Глобальное тестирование 100% моделей завершено: "
        f"проверено {res['tested']} моделей, "
        f"подтверждена работа {res['working']} моделей."
    )
    return templates.TemplateResponse(
        request,
        "partials/ai_keys_pool.html",
        {"admin": admin, "pools": pools, "keys": keys, "alert_message": alert},
    )


@partials_router.post(
    "/settings/pipeline",
    response_class=HTMLResponse,
    summary="Save the production switch and schedule (HTMX)",
)
async def settings_pipeline_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    interval_minutes: Annotated[int, Form()] = DEFAULT_INTERVAL_MINUTES,
    enabled: Annotated[bool, Form()] = False,
    allow_watermarked_source_media: Annotated[bool, Form()] = False,
) -> HTMLResponse:
    """Persist the production-mode switch and the collection interval.

    ``enabled`` defaults to ``False`` because an unchecked checkbox submits no
    field at all; without the default, turning production *off* could never reach
    the server.
    """
    try:
        control = await save_control(
            session,
            enabled=enabled,
            interval_minutes=interval_minutes,
            allow_watermarked_source_media=allow_watermarked_source_media,
        )
    except ScheduleError as error:
        return templates.TemplateResponse(
            request,
            "partials/pipeline_result.html",
            {"admin": admin, "error": str(error)},
            status_code=400,
        )

    logger.info(
        "%s set production mode %s with a %d-minute interval",
        admin.username,
        "on" if control.enabled else "off",
        control.interval_minutes,
    )
    if control.enabled:
        import asyncio  # noqa: PLC0415

        from app.workers.content_worker import run_content_pipeline  # noqa: PLC0415
        from app.workers.publisher_worker import run_publisher  # noqa: PLC0415

        asyncio.create_task(run_publisher(limit=100))  # noqa: RUF006
        asyncio.create_task(run_content_pipeline(limit=100))  # noqa: RUF006
    return templates.TemplateResponse(
        request,
        "partials/pipeline_result.html",
        {"admin": admin, "control": control},
    )


@partials_router.get(
    "/settings/grid",
    response_class=HTMLResponse,
    summary="Settings services control grid (HTMX)",
)
async def settings_grid_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Render the macOS HIG service control grid for /settings."""
    ctx = await _load_settings_services_context(session)
    ctx["admin"] = admin
    return templates.TemplateResponse(
        request,
        "partials/settings_service_grid.html",
        ctx,
    )


@partials_router.post(
    "/settings/chains",
    response_class=HTMLResponse,
    summary="Save fallback model chains (HTMX)",
)
async def settings_chains_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    """Store the ordered model chain for each stage.

    Read from the raw form because the field set is derived from ``MODEL_ROLES``
    and ``CHAIN_LENGTH``: naming each one in the signature would mean editing this
    handler whenever a stage or a fallback slot is added.
    """
    form = await request.form()
    chains: dict[str, list[str]] = {}
    for role, _attribute, _label in MODEL_ROLES:
        values = [
            str(form.get(f"{role}_{position}") or "") for position in range(CHAIN_LENGTH)
        ]
        chains[role] = [value for value in values if value.strip()]

    try:
        saved = await save_chains(session, chains)
    except ModelError as error:
        return templates.TemplateResponse(
            request,
            "partials/chains_result.html",
            {"admin": admin, "error": str(error)},
            status_code=400,
        )

    logger.info("%s updated the fallback chains", admin.username)
    return templates.TemplateResponse(
        request,
        "partials/chains_result.html",
        {
            "admin": admin,
            "chains": [
                {"role": role, "label": label, "models": saved.get(role, [])}
                for role, _attribute, label in MODEL_ROLES
            ],
        },
    )


@partials_router.post(
    "/settings/probe",
    response_class=HTMLResponse,
    summary="Probe the configured models (HTMX)",
)
async def settings_probe_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    """Call every configured model once and report which ones answered.

    The gateway advertises hundreds of models and says nothing about which are
    reachable for *this* account, so the only honest way to build a chain is to try
    them. Failures are reported per model rather than raised: the point is the
    comparison, and one dead name must not hide the rest.

    Only the chat roles are probed. The embedding stage runs locally through
    fastembed, so calling its name against the chat endpoint would report a dead
    model however well local inference is working.
    """
    chains = await load_chains(session)
    unique: list[str] = []
    for role, models in chains.items():
        if role not in CHAT_ROLES:
            continue
        for model in models:
            if model not in unique:
                unique.append(model)

    results = await probe_models(unique)
    cooling: dict[str, dict[str, int]] = {}
    for role, models in chains.items():
        state = await cooldown_state(role, models)
        if state:
            cooling[role] = state

    logger.info("%s probed %d model(s)", admin.username, len(results))
    return templates.TemplateResponse(
        request,
        "partials/probe_result.html",
        {
            "admin": admin,
            "results": [result.to_dict() for result in results],
            "cooling": cooling,
            "chains": chains,
        },
    )


@partials_router.post(
    "/settings/cooldowns/clear",
    response_class=HTMLResponse,
    summary="Clear model cooldowns (HTMX)",
)
async def settings_clear_cooldowns_partial(
    request: Request, admin: PartialAdmin, session: SessionDep
) -> HTMLResponse:
    """Drop every cooldown, for the operator who has just fixed the provider."""
    chains = await load_chains(session)
    cleared = 0
    for role, models in chains.items():
        cleared += await clear_cooldowns(role, models)
    logger.info("%s cleared %d cooldown(s)", admin.username, cleared)
    return templates.TemplateResponse(
        request,
        "partials/probe_result.html",
        {"admin": admin, "cleared": cleared, "chains": chains, "results": []},
    )


@partials_router.post(
    "/settings/wordpress/save",
    response_class=HTMLResponse,
    summary="Save WordPress and SSH connection parameters (HTMX)",
)
async def settings_wp_save_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    wp_base_url: Annotated[str, Form()] = "",
    wp_app_user: Annotated[str, Form()] = "",
    wp_app_password: Annotated[str, Form()] = "",
    ssh_host: Annotated[str, Form()] = "",
    ssh_user: Annotated[str, Form()] = "root",
    ssh_port: Annotated[int, Form()] = 22,
    ssh_key_path: Annotated[str, Form()] = "",
    ssh_private_key: Annotated[str, Form()] = "",
    wp_document_root: Annotated[str, Form()] = "/var/www/html",
    is_active: Annotated[bool, Form()] = False,
) -> HTMLResponse:
    """Save WordPress credentials and SSH parameters with Fernet encryption."""
    from app.services.wp_connection import (  # noqa: PLC0415
        WPConnectionConfig,
        load_wp_connection,
        save_wp_connection,
    )

    current = await load_wp_connection(session)
    password_to_save = wp_app_password.strip() or current.wp_app_password
    key_to_save = ssh_private_key.strip() or current.ssh_private_key

    new_config = WPConnectionConfig(
        wp_base_url=wp_base_url.strip(),
        wp_app_user=wp_app_user.strip(),
        wp_app_password=password_to_save,
        ssh_host=ssh_host.strip(),
        ssh_user=ssh_user.strip() or "root",
        ssh_port=ssh_port or 22,
        ssh_key_path=ssh_key_path.strip(),
        ssh_private_key=key_to_save,
        wp_document_root=wp_document_root.strip() or "/var/www/html",
        is_active=is_active,
    )

    saved = await save_wp_connection(session, new_config)
    box = None
    try:
        box = get_secret_box()
    except (ValueError, TypeError):
        box = None

    notice = (
        "Параметры подключения WordPress и SSH успешно сохранены с шифрованием Fernet."
        if box is not None
        else "Параметры сохранены (без FERNET_KEY шифрование не активно)."
    )

    logger.info(
        "%s updated WordPress connection config (site=%s)",
        admin.username,
        saved.wp_base_url,
    )
    return templates.TemplateResponse(
        request,
        "partials/wp_connection_card.html",
        {
            "admin": admin,
            "wp_conn": saved.to_public_dict(),
            "encryption_ready": box is not None,
            "notice": notice,
        },
    )


@partials_router.post(
    "/settings/wordpress/test",
    response_class=HTMLResponse,
    summary="Probe WordPress REST and SSH connections (HTMX)",
)
async def settings_wp_test_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    wp_base_url: Annotated[str, Form()] = "",
    wp_app_user: Annotated[str, Form()] = "",
    wp_app_password: Annotated[str, Form()] = "",
    ssh_host: Annotated[str, Form()] = "",
    ssh_user: Annotated[str, Form()] = "root",
    ssh_port: Annotated[int, Form()] = 22,
    ssh_key_path: Annotated[str, Form()] = "",
    ssh_private_key: Annotated[str, Form()] = "",
    wp_document_root: Annotated[str, Form()] = "/var/www/html",
) -> HTMLResponse:
    """Test both REST API and SSH filesystem access with live diagnostic feedback."""
    from app.services.wp_connection import (  # noqa: PLC0415
        WPConnectionConfig,
        load_wp_connection,
        test_wp_rest,
        test_wp_ssh,
    )

    current = await load_wp_connection(session)
    config = WPConnectionConfig(
        wp_base_url=wp_base_url.strip() or current.wp_base_url,
        wp_app_user=wp_app_user.strip() or current.wp_app_user,
        wp_app_password=wp_app_password.strip() or current.wp_app_password,
        ssh_host=ssh_host.strip() or current.ssh_host,
        ssh_user=ssh_user.strip() or current.ssh_user,
        ssh_port=ssh_port or current.ssh_port,
        ssh_key_path=ssh_key_path.strip() or current.ssh_key_path,
        ssh_private_key=ssh_private_key.strip() or current.ssh_private_key,
        wp_document_root=wp_document_root.strip() or current.wp_document_root,
    )

    rest_res = await test_wp_rest(config)
    ssh_res = await test_wp_ssh(config)

    logger.info(
        "%s tested WP connection (REST: %s, SSH: %s)",
        admin.username,
        rest_res.get("ok"),
        ssh_res.get("ok"),
    )
    return templates.TemplateResponse(
        request,
        "partials/wp_test_result.html",
        {
            "admin": admin,
            "rest": rest_res,
            "ssh": ssh_res,
        },
    )


@partials_router.post(
    "/settings/wordpress/sync_categories",
    response_class=HTMLResponse,
    summary="Synchronize taxonomy categories from WordPress (HTMX)",
)
async def settings_wp_sync_categories_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Synchronize categories and term IDs from remote WordPress site."""
    from app.services.wp_connection import (  # noqa: PLC0415
        load_wp_connection,
        sync_wp_categories,
    )

    config = await load_wp_connection(session)
    if not config.wp_base_url:
        return templates.TemplateResponse(
            request,
            "partials/wp_sync_result.html",
            {
                "admin": admin,
                "error": "URL WordPress сайта не настроен. Укажите адрес в форме выше.",
                "categories": [],
            },
            status_code=400,
        )

    try:
        categories = await sync_wp_categories(session, config)
        notice = f"Синхронизировано {len(categories)} рубрик ({config.wp_base_url})."
        error = None
    except Exception as exc:
        logger.exception("Category synchronization failed")
        categories = []
        notice = ""
        error = f"Ошибка синхронизации рубрик: {exc}"

    logger.info("%s synced %d categories from WordPress", admin.username, len(categories))
    return templates.TemplateResponse(
        request,
        "partials/wp_sync_result.html",
        {
            "admin": admin,
            "notice": notice,
            "error": error,
            "categories": categories,
        },
    )


@partials_router.post(
    "/settings/yandex/save",
    response_class=HTMLResponse,
    summary="Save Yandex services connection parameters (HTMX)",
)
async def settings_yandex_save_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    wordstat_token: Annotated[str, Form()] = "",
    wordstat_api_key: Annotated[str, Form()] = "",
    wordstat_folder_id: Annotated[str, Form()] = "",
    metrika_counter_id: Annotated[str, Form()] = "",
    metrika_dzen_counter_id: Annotated[str, Form()] = "",
    metrika_token: Annotated[str, Form()] = "",
    webmaster_host_id: Annotated[str, Form()] = "",
    webmaster_token: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Save Yandex credentials with Fernet encryption."""
    from app.services.yandex_connection import (  # noqa: PLC0415
        YandexConnectionConfig,
        load_yandex_connection,
        save_yandex_connection,
    )

    current = await load_yandex_connection(session)
    new_config = YandexConnectionConfig(
        wordstat_token=wordstat_token.strip() or current.wordstat_token,
        wordstat_api_key=wordstat_api_key.strip() or current.wordstat_api_key,
        wordstat_folder_id=wordstat_folder_id.strip() or current.wordstat_folder_id,
        metrika_counter_id=metrika_counter_id.strip() or current.metrika_counter_id,
        metrika_dzen_counter_id=(
            metrika_dzen_counter_id.strip() or current.metrika_dzen_counter_id
        ),
        metrika_token=metrika_token.strip() or current.metrika_token,
        webmaster_host_id=webmaster_host_id.strip() or current.webmaster_host_id,
        webmaster_token=webmaster_token.strip() or current.webmaster_token,
    )

    saved = await save_yandex_connection(session, new_config)
    box = None
    try:
        box = get_secret_box()
    except (ValueError, TypeError):
        box = None

    notice = (
        "Параметры сервисов Яндекса успешно сохранены с шифрованием Fernet."
        if box is not None
        else "Параметры сохранены (без FERNET_KEY шифрование не активно)."
    )

    logger.info("%s updated Yandex services config", admin.username)
    return templates.TemplateResponse(
        request,
        "partials/yandex_services_card.html",
        {
            "admin": admin,
            "yandex_conn": saved.to_public_dict(),
            "notice": notice,
        },
    )


@partials_router.post(
    "/settings/yandex/test-wordstat",
    response_class=HTMLResponse,
    summary="Probe Yandex Wordstat API connection (HTMX)",
)
async def settings_yandex_test_wordstat_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    wordstat_token: Annotated[str, Form()] = "",
    wordstat_api_key: Annotated[str, Form()] = "",
    wordstat_folder_id: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Test Wordstat API credentials."""
    from app.services.yandex_connection import (  # noqa: PLC0415
        YandexConnectionConfig,
        load_yandex_connection,
        probe_yandex_wordstat,
    )

    current = await load_yandex_connection(session)
    config = YandexConnectionConfig(
        wordstat_token=wordstat_token.strip() or current.wordstat_token,
        wordstat_api_key=wordstat_api_key.strip() or current.wordstat_api_key,
        wordstat_folder_id=wordstat_folder_id.strip() or current.wordstat_folder_id,
    )
    result = await probe_yandex_wordstat(config)
    return templates.TemplateResponse(
        request,
        "partials/yandex_test_result.html",
        {"admin": admin, "result": result},
    )


@partials_router.post(
    "/settings/yandex/test-metrika",
    response_class=HTMLResponse,
    summary="Probe Yandex Metrika API connection (HTMX)",
)
async def settings_yandex_test_metrika_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    metrika_counter_id: Annotated[str, Form()] = "",
    metrika_token: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Test Metrika API credentials."""
    from app.services.yandex_connection import (  # noqa: PLC0415
        YandexConnectionConfig,
        load_yandex_connection,
        probe_yandex_metrika,
    )

    current = await load_yandex_connection(session)
    config = YandexConnectionConfig(
        metrika_counter_id=metrika_counter_id.strip() or current.metrika_counter_id,
        metrika_token=metrika_token.strip() or current.metrika_token,
    )
    result = await probe_yandex_metrika(config)
    return templates.TemplateResponse(
        request,
        "partials/yandex_test_result.html",
        {"admin": admin, "result": result},
    )


@partials_router.post(
    "/settings/yandex/test-metrika-dzen",
    response_class=HTMLResponse,
    summary="Probe Yandex Metrika Dzen channel API connection (HTMX)",
)
async def settings_yandex_test_metrika_dzen_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    metrika_dzen_counter_id: Annotated[str, Form()] = "",
    metrika_token: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Test Metrika Dzen channel API credentials."""
    from app.services.yandex_connection import (  # noqa: PLC0415
        YandexConnectionConfig,
        load_yandex_connection,
        probe_yandex_metrika_dzen,
    )

    current = await load_yandex_connection(session)
    config = YandexConnectionConfig(
        metrika_dzen_counter_id=(
            metrika_dzen_counter_id.strip() or current.metrika_dzen_counter_id
        ),
        metrika_token=metrika_token.strip() or current.metrika_token,
    )
    result = await probe_yandex_metrika_dzen(config)
    return templates.TemplateResponse(
        request,
        "partials/yandex_test_result.html",
        {"admin": admin, "result": result},
    )


@partials_router.post(
    "/settings/yandex/test-webmaster",
    response_class=HTMLResponse,
    summary="Probe Yandex Webmaster API connection (HTMX)",
)
async def settings_yandex_test_webmaster_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    webmaster_host_id: Annotated[str, Form()] = "",
    webmaster_token: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Test Webmaster API credentials."""
    from app.services.yandex_connection import (  # noqa: PLC0415
        YandexConnectionConfig,
        load_yandex_connection,
        probe_yandex_webmaster,
    )

    current = await load_yandex_connection(session)
    config = YandexConnectionConfig(
        webmaster_host_id=webmaster_host_id.strip() or current.webmaster_host_id,
        webmaster_token=webmaster_token.strip() or current.webmaster_token,
    )
    result = await probe_yandex_webmaster(config)
    return templates.TemplateResponse(
        request,
        "partials/yandex_test_result.html",
        {"admin": admin, "result": result},
    )


# ------------------------------------------------------------------ Google routes


@partials_router.post(
    "/settings/google/save",
    response_class=HTMLResponse,
    summary="Save Google services connection parameters (HTMX)",
)
async def settings_google_save_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    ga4_property_id: Annotated[str, Form()] = "",
    gsc_site_url: Annotated[str, Form()] = "",
    service_account_json: Annotated[str, Form()] = "",
    oauth_access_token: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Save Google credentials with Fernet encryption."""
    from app.services.google_connection import (  # noqa: PLC0415
        GoogleConnectionConfig,
        load_google_connection,
        save_google_connection,
    )

    current = await load_google_connection(session)
    sa_to_save = service_account_json.strip() or current.service_account_json
    token_to_save = oauth_access_token.strip() or current.oauth_access_token
    raw_site = gsc_site_url.strip() or current.gsc_site_url

    # Auto-resolve bare domain or un-prefixed site
    # (e.g. svodka.press -> sc-domain:svodka.press)
    if raw_site and not raw_site.startswith(("http://", "https://", "sc-domain:")):
        test_cfg = GoogleConnectionConfig(
            service_account_json=sa_to_save,
            oauth_access_token=token_to_save,
            gsc_site_url=raw_site,
        )
        if test_cfg.has_credentials:
            try:
                import httpx  # noqa: PLC0415

                from app.services.google_connection import (  # noqa: PLC0415
                    probe_google_search_console,
                )

                probe = await probe_google_search_console(test_cfg)
                if probe.get("ok") and probe.get("site_url"):
                    raw_site = str(probe["site_url"]).strip()
            except (httpx.HTTPError, ValueError) as exc:
                logger.debug("Auto-resolving GSC property skipped: %s", exc)

    new_config = GoogleConnectionConfig(
        ga4_property_id=ga4_property_id.strip() or current.ga4_property_id,
        gsc_site_url=raw_site,
        service_account_json=sa_to_save,
        oauth_access_token=token_to_save,
        is_ga4_active=True,
        is_gsc_active=True,
    )
    saved = await save_google_connection(session, new_config)

    try:
        box = get_secret_box()
    except (ValueError, TypeError):
        box = None

    notice = (
        "Параметры сервисов Google успешно сохранены с шифрованием Fernet."
        if box is not None
        else "Параметры Google сохранены (без FERNET_KEY шифрование не активно)."
    )

    logger.info("%s updated Google services config", admin.username)
    return templates.TemplateResponse(
        request,
        "partials/google_services_card.html",
        {
            "admin": admin,
            "google_conn": saved.to_public_dict(),
            "notice": notice,
        },
    )


@partials_router.post(
    "/settings/google/test-ga4",
    response_class=HTMLResponse,
    summary="Probe Google Analytics 4 Data API connection (HTMX)",
)
async def settings_google_test_ga4_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    ga4_property_id: Annotated[str, Form()] = "",
    service_account_json: Annotated[str, Form()] = "",
    oauth_access_token: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Test GA4 Data API connection."""
    from app.services.google_connection import (  # noqa: PLC0415
        GoogleConnectionConfig,
        load_google_connection,
        probe_google_analytics,
    )

    current = await load_google_connection(session)
    config = GoogleConnectionConfig(
        ga4_property_id=ga4_property_id.strip() or current.ga4_property_id,
        service_account_json=service_account_json.strip() or current.service_account_json,
        oauth_access_token=oauth_access_token.strip() or current.oauth_access_token,
    )
    result = await probe_google_analytics(config)
    return templates.TemplateResponse(
        request,
        "partials/service_test_result.html",
        {"admin": admin, "result": result},
    )


@partials_router.post(
    "/settings/google/test-gsc",
    response_class=HTMLResponse,
    summary="Probe Google Search Console API connection (HTMX)",
)
async def settings_google_test_gsc_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    gsc_site_url: Annotated[str, Form()] = "",
    service_account_json: Annotated[str, Form()] = "",
    oauth_access_token: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Test Google Search Console API connection."""
    from app.services.google_connection import (  # noqa: PLC0415
        GoogleConnectionConfig,
        load_google_connection,
        probe_google_search_console,
        save_google_connection,
    )

    current = await load_google_connection(session)
    config = GoogleConnectionConfig(
        gsc_site_url=gsc_site_url.strip() or current.gsc_site_url,
        service_account_json=service_account_json.strip() or current.service_account_json,
        oauth_access_token=oauth_access_token.strip() or current.oauth_access_token,
    )
    result = await probe_google_search_console(config)
    # If probe succeeded and resolved site is known, update stored gsc_site_url
    if result.get("ok") and result.get("site_url") and current.from_database:
        resolved = str(result["site_url"]).strip()
        if resolved and resolved != current.gsc_site_url:
            current.gsc_site_url = resolved
            try:
                await save_google_connection(session, current)
            except SQLAlchemyError as exc:
                logger.warning("Could not persist auto-resolved GSC site: %s", exc)
    return templates.TemplateResponse(
        request,
        "partials/service_test_result.html",
        {"admin": admin, "result": result},
    )


# ----------------------------------------------------------------- Bing / IndexNow


@partials_router.post(
    "/settings/bing/save",
    response_class=HTMLResponse,
    summary="Save Bing & IndexNow parameters (HTMX)",
)
async def settings_bing_save_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    site_url: Annotated[str, Form()] = "",
    api_key: Annotated[str, Form()] = "",
    indexnow_key: Annotated[str, Form()] = "",
    indexnow_key_location: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Save Bing Webmaster and IndexNow parameters."""
    from app.services.bing_connection import (  # noqa: PLC0415
        BingConnectionConfig,
        load_bing_connection,
        save_bing_connection,
    )

    current = await load_bing_connection(session)
    key_to_save = api_key.strip() or current.api_key
    in_key_to_save = indexnow_key.strip() or current.indexnow_key

    new_config = BingConnectionConfig(
        site_url=site_url.strip() or current.site_url,
        api_key=key_to_save,
        indexnow_key=in_key_to_save,
        indexnow_key_location=(
            indexnow_key_location.strip() or current.indexnow_key_location
        ),
        is_active=True,
        is_indexnow_active=True,
    )
    saved = await save_bing_connection(session, new_config)

    try:
        box = get_secret_box()
    except (ValueError, TypeError):
        box = None

    notice = (
        "Параметры Bing и IndexNow успешно сохранены с шифрованием Fernet."
        if box is not None
        else "Параметры Bing сохранены (без FERNET_KEY шифрование не активно)."
    )

    logger.info("%s updated Bing services config", admin.username)
    return templates.TemplateResponse(
        request,
        "partials/bing_services_card.html",
        {
            "admin": admin,
            "bing_conn": saved.to_public_dict(),
            "notice": notice,
        },
    )


@partials_router.post(
    "/settings/bing/test-bing",
    response_class=HTMLResponse,
    summary="Probe Bing Webmaster API connection (HTMX)",
)
async def settings_bing_test_bing_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    site_url: Annotated[str, Form()] = "",
    api_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Test Bing Webmaster API connection."""
    from app.services.bing_connection import (  # noqa: PLC0415
        BingConnectionConfig,
        load_bing_connection,
        probe_bing_webmaster,
    )

    current = await load_bing_connection(session)
    config = BingConnectionConfig(
        site_url=site_url.strip() or current.site_url,
        api_key=api_key.strip() or current.api_key,
    )
    result = await probe_bing_webmaster(config)
    return templates.TemplateResponse(
        request,
        "partials/service_test_result.html",
        {"admin": admin, "result": result},
    )


@partials_router.post(
    "/settings/bing/test-indexnow",
    response_class=HTMLResponse,
    summary="Probe IndexNow submission API (HTMX)",
)
async def settings_bing_test_indexnow_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    site_url: Annotated[str, Form()] = "",
    indexnow_key: Annotated[str, Form()] = "",
    indexnow_key_location: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Test IndexNow submission."""
    from app.services.bing_connection import (  # noqa: PLC0415
        BingConnectionConfig,
        load_bing_connection,
        probe_indexnow,
    )

    current = await load_bing_connection(session)
    config = BingConnectionConfig(
        site_url=site_url.strip() or current.site_url,
        indexnow_key=indexnow_key.strip() or current.indexnow_key,
        indexnow_key_location=(
            indexnow_key_location.strip() or current.indexnow_key_location
        ),
    )
    result = await probe_indexnow(config)
    return templates.TemplateResponse(
        request,
        "partials/service_test_result.html",
        {"admin": admin, "result": result},
    )


@partials_router.post(
    "/settings/dzen",
    response_class=HTMLResponse,
    summary="Save Dzen browser publisher settings (HTMX)",
)
async def settings_dzen_save_partial(  # noqa: PLR0917
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    channel_url: Annotated[str, Form()] = "",
    channel_name: Annotated[str, Form()] = "",
    is_active: Annotated[str, Form()] = "",
    publication_mode: Annotated[str, Form()] = "publish",
    promo_mode: Annotated[str, Form()] = "none",
    promo_snippet: Annotated[str, Form()] = "",
    storage_state_json: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Save Dzen browser publisher configuration and session cookies."""
    import json  # noqa: PLC0415

    from app.services.dzen_publisher import (  # noqa: PLC0415
        load_dzen_session_config,
        save_dzen_session_config,
    )

    config = await load_dzen_session_config(session)
    config.channel_url = channel_url.strip()
    config.channel_name = channel_name.strip()
    config.is_active = is_active.lower() in ("true", "1", "yes", "on")
    config.publication_mode = publication_mode.strip() or "publish"
    config.promo_mode = promo_mode.strip() or "none"
    config.promo_snippet = promo_snippet.strip()

    feedback_msg = "Настройки Дзена успешно сохранены."
    raw_storage = storage_state_json.strip()
    if raw_storage:
        try:
            parsed = json.loads(raw_storage)
            from app.services.dzen_publisher import (  # noqa: PLC0415
                normalize_dzen_storage_state,
            )

            normalized = normalize_dzen_storage_state(parsed)
            if normalized["cookies"]:
                config.storage_state = normalized
                config.status = "not_verified"
                feedback_msg = (
                    f"Сохранено и зашифровано {len(normalized['cookies'])} кук. "
                    "Нажмите «Проверить статус сессии», чтобы подтвердить "
                    "авторизацию в Студии Дзена."
                )
            else:
                feedback_msg = (
                    "Внимание: в переданном JSON не найдено записей кук."
                )
        except json.JSONDecodeError as exc:
            feedback_msg = f"Ошибка разбора JSON сессии: {exc}"

    await save_dzen_session_config(session, config)
    return templates.TemplateResponse(
        request,
        "partials/dzen_publisher_card.html",
        {
            "admin": admin,
            "dzen_config": config,
            "dzen_feedback": {
                "is_valid": True,
                "status": "saved",
                "message": feedback_msg,
                "timestamp": datetime.now(UTC).strftime("%H:%M:%S UTC"),
            },
        },
    )


@partials_router.post(
    "/settings/dzen/probe",
    response_class=HTMLResponse,
    summary="Probe Dzen browser session (HTMX)",
)
async def settings_dzen_probe_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Probe Dzen browser session live using Playwright stealth."""
    from app.services.dzen_publisher import (  # noqa: PLC0415
        load_dzen_session_config,
        probe_dzen_session,
    )

    config = await load_dzen_session_config(session)
    result = await probe_dzen_session(config=config, session=session)
    target = request.headers.get("HX-Target", "")
    if target.startswith("card-test-"):
        return templates.TemplateResponse(
            request,
            "partials/service_test_result.html",
            {
                "admin": admin,
                "result": {
                    "service": "Яндекс Дзен (Playwright)",
                    "ok": (
                        bool(result.get("is_valid"))
                        or bool(result.get("studio_accessible"))
                    ),
                    "message": result.get("message", "Проверка сессии завершена"),
                    "details": (
                        f"Студия: {'Да' if result.get('studio_accessible') else 'Нет'} | "
                        f"Канал: {result.get('channel_title') or 'Не определен'}"
                    ),
                },
            },
        )
    return templates.TemplateResponse(
        request,
        "partials/dzen_publisher_card.html",
        {
            "admin": admin,
            "dzen_config": config,
            "dzen_feedback": result,
        },
    )


@partials_router.post(
    "/settings/dzen/test-publish",
    response_class=HTMLResponse,
    summary="Publish test draft to Dzen Studio (HTMX)",
)
async def settings_dzen_test_publish_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Create test draft in Dzen Studio via Playwright."""
    from app.models.article import Article  # noqa: PLC0415
    from app.services.dzen_publisher import (  # noqa: PLC0415
        DzenBrowserPublisher,
        DzenSessionProbeResult,
        load_dzen_session_config,
    )

    config = await load_dzen_session_config(session)
    if not config.has_session:
        feedback = DzenSessionProbeResult(
            is_valid=False,
            status="not_configured",
            message="Файл сессии не настроен. Загрузите куки перед отправкой теста.",
        )
    else:
        now_str = datetime.now(UTC).strftime("%d.%m %H:%M")
        test_article = Article(
            title=f"Тестовая публикация SvodkaPress ({now_str}) 🚀",
            content_html=(
                "<p>Это тестовый черновик автоматической публикации SvodkaPress.</p>"
                "<p>Проверка верстки, форматирования и интеграции промо-блоков.</p>"
            ),
        )
        pub = DzenBrowserPublisher(config)
        try:
            res = await pub.publish_article(
                test_article,
                "https://svodka.press/test-article",
                publish_now=False,  # Draft mode for safety
            )
            feedback = DzenSessionProbeResult(
                is_valid=True,
                status="valid",
                message=(
                    f"Черновик успешно создан в Студии Дзена: "
                    f"{res.url or res.external_id}!"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            feedback = DzenSessionProbeResult(
                is_valid=False,
                status="error",
                message=f"Ошибка создания черновика в Дзене: {exc}",
            )

    return templates.TemplateResponse(
        request,
        "partials/dzen_publisher_card.html",
        {
            "admin": admin,
            "dzen_config": config,
            "dzen_feedback": feedback,
        },
    )


def _apply_vk_storage_input(
    config: Any,
    raw_storage: str,
    normalize_fn: Any,
) -> tuple[bool, bool, str]:
    """Parse raw_storage (JSON cookies or token string) into config."""
    import json as _json  # noqa: PLC0415

    inline_tok = re.search(r"vk1\.a\.[A-Za-z0-9_\-]{20,}", raw_storage)
    if inline_tok and not raw_storage.startswith(("[", "{")):
        config.access_token = inline_tok.group(0)
        config.status = "active"
        return True, False, "Токен VK API (vk1.a...) успешно извлечен и сохранен!"
    try:
        parsed = _json.loads(raw_storage)
        normalized = normalize_fn(parsed)
        if normalized["cookies"]:
            config.storage_state = normalized
            config.status = "not_verified"
            msg = f"Сохранено и зашифровано {len(normalized['cookies'])} кук ВК."
            return True, True, msg
        return False, False, "Внимание: в переданном JSON не найдено записей кук."
    except _json.JSONDecodeError as exc:
        if inline_tok:
            config.access_token = inline_tok.group(0)
            config.status = "active"
            return True, False, "Токен VK API (vk1.a...) успешно извлечен и сохранен!"
        return False, False, f"Ошибка разбора JSON сессии: {exc}"


async def _verify_vk_token_live(
    session: AsyncSession, config: Any
) -> tuple[bool, str]:
    """Run live groups.getById check against api.vk.com and update config status."""
    from app.services.vk_video_service import (  # noqa: PLC0415
        VKVideoService,
        save_vk_video_config,
    )

    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    config.last_checked_at = now_str
    try:
        svc = VKVideoService(
            access_token=config.access_token, group_id=config.group_id
        )
        res = await svc.test_connection()
        g_name = str(res.get("name") or "")
        if g_name:
            config.group_name = g_name
        config.status = "active"
        config.error_message = None
        await save_vk_video_config(session, config)
        label = (
            f"«{g_name}» (ID {config.group_id})"
            if g_name
            else f"ID {config.group_id}"
        )
        return True, f"✅ Токен VK проверен и работает! Сообщество: {label}."
    except Exception as exc:  # noqa: BLE001
        err_text = str(exc)
        config.status = "error"
        if "another ip address" in err_text.lower():
            msg = (
                "⚠️ Токен привязан к вашему домашнему IP и отклонен сервером ВК. "
                "Нажмите синюю кнопку «🔑 Получить токен без привязки к IP (1 клик)» "
                "в шапке карточки и вставьте новую ссылку!"
            )
        else:
            msg = f"❌ Ошибка проверки токена VK API: {err_text}"
        config.error_message = msg
        await save_vk_video_config(session, config)
        return False, msg


@partials_router.post(
    "/settings/vk/save",
    response_class=HTMLResponse,
    summary="Save VK Video browser session, credentials and group configuration (HTMX)",
)
async def settings_vk_save_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Save VK Video browser session (Cookies JSON), access token, and community ID."""
    from app.services.vk_video_service import (  # noqa: PLC0415
        load_vk_video_config,
        normalize_vk_storage_state,
        probe_vk_browser_session,
        save_vk_video_config,
    )

    form = await request.form()
    vk_access_token = str(form.get("vk_access_token") or "")
    vk_group_id = str(form.get("vk_group_id") or "236626422")
    storage_state_json = str(form.get("storage_state_json") or "")
    is_active_raw = form.get("is_active")

    config = await load_vk_video_config(session)
    config.is_active = (
        str(is_active_raw) == "true" if is_active_raw is not None else True
    )
    if vk_access_token.strip():
        tok_match = re.search(r"vk1\.a\.[A-Za-z0-9_\-]{20,}", vk_access_token)
        config.access_token = (
            tok_match.group(0) if tok_match else vk_access_token.strip()
        )
    config.group_id = vk_group_id.strip() or config.group_id or "236626422"

    feedback_msg = "Настройки ВКонтакте успешно сохранены."
    feedback_valid = True
    should_auto_probe = False

    if storage_state_json.strip():
        feedback_valid, should_auto_probe, feedback_msg = _apply_vk_storage_input(
            config, storage_state_json.strip(), normalize_vk_storage_state
        )

    await save_vk_video_config(session, config)

    if should_auto_probe:
        probe_res = await probe_vk_browser_session(config, session=session)
        feedback_valid = bool(probe_res.get("is_valid"))
        feedback_msg = str(probe_res.get("message") or feedback_msg)
    elif config.has_token:
        feedback_valid, feedback_msg = await _verify_vk_token_live(session, config)

    return templates.TemplateResponse(
        request,
        "partials/vk_services_card.html",
        {
            "admin": admin,
            "vk_conn": config.to_public_dict(),
            "vk_saved": True,
            "vk_feedback": {
                "is_valid": feedback_valid,
                "status": config.status,
                "message": feedback_msg,
                "timestamp": datetime.now(UTC).strftime("%H:%M:%S UTC"),
            },
        },
    )


@partials_router.post(
    "/settings/vk/probe",
    response_class=HTMLResponse,
    summary="Probe VK browser session and extract web token (HTMX)",
)
async def settings_vk_probe_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Probe VK token or browser session live and report honest status."""
    from app.services.vk_video_service import (  # noqa: PLC0415
        load_vk_video_config,
        probe_vk_browser_session,
    )

    config = await load_vk_video_config(session)
    if config.has_token:
        ok, msg = await _verify_vk_token_live(session, config)
        if ok:
            return templates.TemplateResponse(
                request,
                "partials/vk_services_card.html",
                {
                    "admin": admin,
                    "vk_conn": config.to_public_dict(),
                    "vk_feedback": {
                        "is_valid": True,
                        "status": config.status,
                        "message": msg,
                        "timestamp": datetime.now(UTC).strftime("%H:%M:%S UTC"),
                    },
                },
            )
    result = await probe_vk_browser_session(config=config, session=session)
    if not result.get("is_valid") and config.error_message:
        result["message"] = config.error_message
    return templates.TemplateResponse(
        request,
        "partials/vk_services_card.html",
        {
            "admin": admin,
            "vk_conn": config.to_public_dict(),
            "vk_feedback": result,
        },
    )


@partials_router.post(
    "/settings/vk/test",
    response_class=HTMLResponse,
    summary="Test VK Video API credentials or browser session (HTMX)",
)
async def settings_vk_test_partial(
    admin: PartialAdmin,
    session: SessionDep,
    vk_access_token: Annotated[str, Form()] = "",
    vk_group_id: Annotated[str, Form()] = "236626422",
) -> HTMLResponse:
    """Probe VK Video API with provided/saved token or auto-extracted browser token."""
    from app.services.vk_video_service import (  # noqa: PLC0415
        VKError,
        VKVideoService,
        load_vk_video_config,
        probe_vk_browser_session,
        save_vk_video_config,
    )

    current = await load_vk_video_config(session)
    token = vk_access_token.strip() or current.access_token
    group_id = vk_group_id.strip() or current.group_id or "236626422"

    if not token and current.has_session:
        probe_res = await probe_vk_browser_session(current, session=session)
        token = current.access_token
        if not token:
            msg = html.escape(str(probe_res.get("message") or "Сессия проверена"))
            pill_cls = "mac-pill-good" if probe_res.get("is_valid") else "mac-pill-warn"
            return HTMLResponse(f'<div class="mac-pill {pill_cls} text-xs">{msg}</div>')

    if not token:
        return HTMLResponse(
            '<div class="mac-pill mac-pill-warn text-xs">'
            "⚠️ Вставьте Cookies JSON сессии браузера (или токен) и сохраните настройки."
            "</div>"
        )

    try:
        service = VKVideoService(access_token=token, group_id=group_id)
        result = await service.test_connection()
        group_name = result.get("name", "")
        if group_name and group_name != current.group_name:
            current.group_name = group_name
            current.status = "active"
            await save_vk_video_config(session, current)

        info = (
            f"Сообщество: «{group_name}» (ID {group_id})"
            if group_name
            else f"ID {group_id}"
        )
        msg = f"✅ Подключение к VK успешно! {html.escape(info)}"
        return HTMLResponse(f'<div class="mac-pill mac-pill-good text-xs">{msg}</div>')
    except VKError as exc:
        err_msg = f"❌ Ошибка VK API: {html.escape(str(exc))}"
        return HTMLResponse(
            f'<div class="mac-pill mac-pill-error text-xs">{err_msg}</div>'
        )
    except Exception as exc:  # noqa: BLE001
        err_msg = f"❌ Ошибка соединения: {html.escape(str(exc))}"
        return HTMLResponse(
            f'<div class="mac-pill mac-pill-error text-xs">{err_msg}</div>'
        )


@partials_router.post(
    "/settings/telegram/save",
    response_class=HTMLResponse,
    summary="Save Telegram Channel bot configuration (HTMX)",
)
async def settings_telegram_save_partial(
    admin: PartialAdmin,
    session: SessionDep,
    bot_token: Annotated[str, Form()] = "",
    channel_id: Annotated[str, Form()] = "@svodka_press",
    is_active: Annotated[str, Form()] = "false",
) -> HTMLResponse:
    """Save Telegram bot credentials and channel id to DB with Fernet encryption."""
    from app.services.telegram_connection import (  # noqa: PLC0415
        load_telegram_channel_config,
        save_telegram_channel_config,
    )

    current = await load_telegram_channel_config(session)
    cleaned_token = bot_token.strip()
    if cleaned_token and not cleaned_token.startswith("••"):
        current.bot_token = cleaned_token
    if channel_id.strip():
        current.channel_id = channel_id.strip()
    current.is_active = is_active.lower() in ("true", "1", "on")

    await save_telegram_channel_config(session, current)
    msg = f"✅ Настройки Telegram сохранены (канал: {html.escape(current.channel_id)})"
    return HTMLResponse(f'<div class="mac-pill mac-pill-good text-xs">{msg}</div>')


@partials_router.post(
    "/settings/telegram/test",
    response_class=HTMLResponse,
    summary="Test Telegram Bot API credentials and channel access (HTMX)",
)
async def settings_telegram_test_partial(
    admin: PartialAdmin,
    session: SessionDep,
    bot_token: Annotated[str, Form()] = "",
    channel_id: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Probe Telegram Bot API and check channel administrator rights."""
    from app.services.telegram_connection import (  # noqa: PLC0415
        load_telegram_channel_config,
        probe_telegram_bot_connection,
        save_telegram_channel_config,
    )

    current = await load_telegram_channel_config(session)
    token_to_test = bot_token.strip()
    if not token_to_test or token_to_test.startswith("••"):
        token_to_test = current.bot_token
    target_channel = channel_id.strip() or current.channel_id or "@svodka_press"

    if not token_to_test:
        return HTMLResponse(
            '<div class="mac-pill mac-pill-warn text-xs">'
            "⚠️ Введите токен бота из @BotFather и сохраните настройки."
            "</div>"
        )

    res = await probe_telegram_bot_connection(token_to_test, target_channel)
    if res.get("valid"):
        current.bot_username = res.get("bot_username", "")
        current.channel_title = res.get("channel_title", "")
        current.status = "active"
        current.error_message = None
        if token_to_test and not token_to_test.startswith("••"):
            current.bot_token = token_to_test
        current.channel_id = target_channel
        await save_telegram_channel_config(session, current)

        target_name = current.channel_title or target_channel
        info = f"Бот @{current.bot_username} готов к публикации в «{target_name}»!"
        return HTMLResponse(
            f'<div class="mac-pill mac-pill-good text-xs">✅ {html.escape(info)}</div>'
        )

    err = res.get("error", "Ошибка проверки Telegram")
    current.status = "error"
    current.error_message = str(err)
    await save_telegram_channel_config(session, current)
    return HTMLResponse(
        f'<div class="mac-pill mac-pill-error text-xs">❌ {html.escape(str(err))}</div>'
    )


async def _render_ads_projects_list(
    request: Request,
    admin: AdminUser,
    session: AsyncSession,
    *,
    feedback_by_project: dict[str, Any] | None = None,
    open_metrika_project_id: str = "",
) -> HTMLResponse:
    """Render partials/ads_projects_list.html with live Metrika stats and CQI."""
    projects = await PromotedProjectsService.get_all_projects(session)
    try:
        adv_report = await PromotedProjectsService.collect_advertiser_campaigns_report(
            session, days=28
        )
    except Exception as adv_err:  # noqa: BLE001
        logger.warning("Failed to collect advertiser report for partial: %s", adv_err)
        adv_report = {
            "campaigns": [],
            "stats_by_project_id": {},
            "cqi_by_project_id": {},
            "shared_token_available": False,
        }
    return templates.TemplateResponse(
        request,
        "partials/ads_projects_list.html",
        {
            "admin": admin,
            "projects": projects,
            "categories": CATEGORY_TITLES,
            "adv_report": adv_report,
            "stats_by_project_id": adv_report.get("stats_by_project_id", {}),
            "cqi_by_project_id": adv_report.get("cqi_by_project_id", {}),
            "shared_token_available": adv_report.get("shared_token_available", False),
            "feedback_by_project": feedback_by_project or {},
            "open_metrika_project_id": open_metrika_project_id,
        },
    )


@partials_router.post(
    "/ads/create",
    response_class=HTMLResponse,
    summary="Create or update advertiser project (HTMX)",
)
async def ads_create_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Create or update advertiser project and return updated list."""
    form_data = await request.form()
    project_id = str(form_data.get("project_id") or "").strip()
    existing = (
        await PromotedProjectsService.get_project_by_id(session, project_id)
        if project_id
        else None
    )

    target_cats = form_data.getlist("target_categories")
    brand = str(form_data.get("brand_name") or "").strip()
    raw_token = str(form_data.get("client_metrika_token") or "").strip()
    if (not raw_token or raw_token.startswith("••")) and existing:
        token_to_save = existing.client_metrika_token
    else:
        token_to_save = raw_token

    parsed_goals = PromotedProjectsService.parse_goals_from_form(form_data)
    goals_to_save = (
        parsed_goals
        if parsed_goals
        else (existing.conversion_goals if existing else [])
    )

    proj = PromotedProject(
        id=existing.id if existing else (project_id or str(uuid.uuid4())),
        brand_name=brand,
        name=str(form_data.get("name") or brand).strip(),
        target_url=str(form_data.get("target_url") or "").strip(),
        utm_source=str(form_data.get("utm_source") or "svodkapress").strip(),
        utm_medium=str(form_data.get("utm_medium") or "native_news").strip(),
        utm_campaign=str(form_data.get("utm_campaign") or "").strip(),
        usp=str(form_data.get("usp") or "").strip(),
        problem_solved=str(form_data.get("problem_solved") or "").strip(),
        cta_text=str(form_data.get("cta_text") or "Узнать подробности").strip(),
        target_categories=[str(c) for c in target_cats],
        client_metrika_counter_id=str(
            form_data.get("client_metrika_counter_id") or ""
        ).strip(),
        client_metrika_token=token_to_save,
        conversion_goals=goals_to_save,
        is_active=existing.is_active if existing else True,
    )
    await PromotedProjectsService.upsert_project(session, proj)
    return await _render_ads_projects_list(request, admin, session)


@partials_router.post(
    "/ads/{project_id}/metrika/save",
    response_class=HTMLResponse,
    summary="Save advertiser Metrika counter, OAuth token, and conversion goals (HTMX)",
)
async def ads_metrika_save_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    project_id: str,
) -> HTMLResponse:
    """Save Yandex Metrika counter, token, UTM tags, and goals for an advertiser."""
    from app.models.promoted_project import ConversionGoal  # noqa: PLC0415
    from app.services.yandex_connection import load_yandex_connection  # noqa: PLC0415

    project = await PromotedProjectsService.get_project_by_id(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Рекламодатель не найден")

    form_data = await request.form()
    counter_id = str(form_data.get("client_metrika_counter_id") or "").strip()
    raw_token = str(form_data.get("client_metrika_token") or "").strip()
    clear_token = str(form_data.get("clear_token") or "") == "1"

    project.client_metrika_counter_id = counter_id
    if clear_token:
        project.client_metrika_token = ""
    elif raw_token and not raw_token.startswith("••"):
        project.client_metrika_token = raw_token

    utm_source = str(form_data.get("utm_source") or "").strip()
    utm_medium = str(form_data.get("utm_medium") or "").strip()
    utm_campaign = str(form_data.get("utm_campaign") or "").strip()
    if utm_source:
        project.utm_source = utm_source
    if utm_medium:
        project.utm_medium = utm_medium
    if "utm_campaign" in form_data:
        project.utm_campaign = utm_campaign

    parsed_goals = PromotedProjectsService.parse_goals_from_form(form_data)
    project.conversion_goals = parsed_goals

    yandex_conn = await load_yandex_connection(session)
    effective_token = project.client_metrika_token or yandex_conn.metrika_token.strip()

    # Auto-discover goals if none were submitted manually and counter + token are present
    auto_discovered_count = 0
    if not project.conversion_goals and counter_id and effective_token:
        disc = await PromotedProjectsService.discover_client_metrika_goals(
            counter_id, effective_token
        )
        if disc.get("ok") and disc.get("goals"):
            project.conversion_goals = [
                ConversionGoal.from_dict(g) for g in disc["goals"]
            ]
            auto_discovered_count = len(project.conversion_goals)

    await PromotedProjectsService.upsert_project(session, project)

    # Probe live stats to give immediate feedback
    stats = await PromotedProjectsService.fetch_client_metrika_stats(
        project,
        default_token=yandex_conn.metrika_token.strip(),
        include_counter_totals=True,
    )
    if stats.get("available"):
        g_cnt = len(project.conversion_goals)
        c_cnt = stats.get("conversions_count", 0)
        cr_val = stats.get("cr_pct", 0.0)
        msg = (
            f"✅ Метрика #{counter_id} и {g_cnt} цел(ей) сохранены! "
            f"Целевых визитов по UTM: {stats.get('visits', 0)}, "
            f"достижений целей: {c_cnt} (CR {cr_val}%). "
            "Данные переданы в /seo для обучения."
        )
        if auto_discovered_count:
            msg += f" Автоматически загружено целей из Метрики: {auto_discovered_count}."
        feedback = {"ok": True, "message": msg, "stats": stats}
    else:
        msg = (
            f"⚠️ Настройки сохранены (целей: {len(project.conversion_goals)}), "
            f"но API Метрики сообщил: {stats.get('reason', 'нет связи')}."
        )
        feedback = {"ok": False, "message": msg, "stats": stats}

    return await _render_ads_projects_list(
        request,
        admin,
        session,
        feedback_by_project={project_id: feedback},
        open_metrika_project_id=project_id,
    )


@partials_router.post(
    "/ads/{project_id}/metrika/discover",
    response_class=HTMLResponse,
    summary="Auto-discover conversion goals from advertiser Yandex Metrika API (HTMX)",
)
async def ads_metrika_discover_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    project_id: str,
) -> HTMLResponse:
    """Query Yandex Metrika Management API for counter goals and merge them."""
    from app.models.promoted_project import ConversionGoal  # noqa: PLC0415
    from app.services.yandex_connection import load_yandex_connection  # noqa: PLC0415

    project = await PromotedProjectsService.get_project_by_id(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Рекламодатель не найден")

    form_data = await request.form()
    counter_id = (
        str(form_data.get("client_metrika_counter_id") or "").strip()
        or project.client_metrika_counter_id
    )
    raw_token = str(form_data.get("client_metrika_token") or "").strip()
    if raw_token and not raw_token.startswith("••"):
        project.client_metrika_token = raw_token
    if counter_id:
        project.client_metrika_counter_id = counter_id

    yandex_conn = await load_yandex_connection(session)
    effective_token = project.client_metrika_token or yandex_conn.metrika_token.strip()

    disc = await PromotedProjectsService.discover_client_metrika_goals(
        counter_id, effective_token
    )
    if disc.get("ok"):
        existing_by_id = {g.goal_id: g for g in project.conversion_goals if g.goal_id}
        for g_dict in disc.get("goals", []):
            gid = str(g_dict.get("goal_id") or "").strip()
            if not gid:
                continue
            if gid in existing_by_id:
                existing_by_id[gid].name = str(
                    g_dict.get("name") or existing_by_id[gid].name
                )
            else:
                existing_by_id[gid] = ConversionGoal.from_dict(g_dict)
        project.conversion_goals = list(existing_by_id.values())
        await PromotedProjectsService.upsert_project(session, project)

    feedback = {
        "ok": bool(disc.get("ok")),
        "message": str(disc.get("message") or ""),
        "counter_name": str(disc.get("counter_name") or ""),
        "site_domain": str(disc.get("site_domain") or ""),
    }
    return await _render_ads_projects_list(
        request,
        admin,
        session,
        feedback_by_project={project_id: feedback},
        open_metrika_project_id=project_id,
    )


@partials_router.post(
    "/ads/{project_id}/metrika/probe",
    response_class=HTMLResponse,
    summary="Probe live target visits and conversion goals for advertiser (HTMX)",
)
async def ads_metrika_probe_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    project_id: str,
) -> HTMLResponse:
    """Run live diagnostic check of advertiser Metrika counter and goals."""
    from app.services.yandex_connection import load_yandex_connection  # noqa: PLC0415

    project = await PromotedProjectsService.get_project_by_id(session, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Рекламодатель не найден")

    form_data = await request.form()
    keep_modal_open = str(form_data.get("keep_modal_open") or "") == "1"
    counter_id = (
        str(form_data.get("client_metrika_counter_id") or "").strip()
        or project.client_metrika_counter_id
    )
    raw_token = str(form_data.get("client_metrika_token") or "").strip()
    if counter_id and counter_id != project.client_metrika_counter_id:
        project.client_metrika_counter_id = counter_id
    if raw_token and not raw_token.startswith("••"):
        project.client_metrika_token = raw_token

    yandex_conn = await load_yandex_connection(session)
    stats = await PromotedProjectsService.fetch_client_metrika_stats(
        project,
        default_token=yandex_conn.metrika_token.strip(),
        days=28,
        include_counter_totals=True,
    )
    if stats.get("available"):
        c_cnt = stats.get("conversions_count", 0)
        cr_val = stats.get("cr_pct", 0.0)
        msg = (
            f"✅ Связь со счётчиком #{project.client_metrika_counter_id} установлена! "
            f"Целевых визитов с наших площадок (UTM/реф): {stats.get('visits', 0)} "
            f"(всего на сайте рекламодателя за 28д: {stats.get('all_site_visits', 0)}). "
            f"Достижений целей: {c_cnt} (CR {cr_val}%)."
        )
        feedback = {"ok": True, "message": msg, "stats": stats}
    else:
        err_reason = stats.get("reason", "Ошибка подключения к Метрике рекламодателя")
        feedback = {
            "ok": False,
            "message": f"❌ {err_reason}",
            "stats": stats,
        }

    return await _render_ads_projects_list(
        request,
        admin,
        session,
        feedback_by_project={project_id: feedback},
        open_metrika_project_id=project_id if keep_modal_open else "",
    )


@partials_router.post(
    "/ads/{project_id}/toggle",
    response_class=HTMLResponse,
    summary="Toggle advertiser project active state (HTMX)",
)
async def ads_toggle_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    project_id: str,
) -> HTMLResponse:
    """Toggle active/pause state for project and return updated list."""
    project = await PromotedProjectsService.get_project_by_id(session, project_id)
    if project:
        project.is_active = not project.is_active
        await PromotedProjectsService.upsert_project(session, project)

    return await _render_ads_projects_list(request, admin, session)


@partials_router.delete(
    "/ads/{project_id}",
    response_class=HTMLResponse,
    summary="Delete advertiser project (HTMX)",
)
async def ads_delete_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    project_id: str,
) -> HTMLResponse:
    """Delete advertiser project and return updated list."""
    await PromotedProjectsService.delete_project(session, project_id)
    return await _render_ads_projects_list(request, admin, session)


@partials_router.post(
    "/settings/social-browser/{platform}/save",
    response_class=HTMLResponse,
    summary="Save browser session cookies for Setka/TenChat/Threads/FB/Pinterest/YouTube",
)
async def settings_social_browser_save_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    platform: str,
) -> HTMLResponse:
    """Save Cookie-Editor JSON session and target URL for one social network."""
    import json as _json  # noqa: PLC0415

    from app.services.social_browser_publisher import (  # noqa: PLC0415
        SOCIAL_BROWSER_PLATFORMS,
        load_all_social_browser_configs,
        load_social_browser_config,
        normalize_social_storage_state,
        probe_social_browser_session,
        save_social_browser_config,
    )

    if platform not in SOCIAL_BROWSER_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unknown platform")

    form = await request.form()
    target_url = str(form.get("target_url") or "").strip()
    raw_cookies = str(form.get("storage_state_json") or "").strip()
    is_active_raw = form.get("is_active")

    cfg = await load_social_browser_config(session, platform)
    cfg.is_active = str(is_active_raw) == "true" if is_active_raw is not None else False
    if target_url:
        cfg.target_url = target_url

    title = str(SOCIAL_BROWSER_PLATFORMS[platform].get("short_title") or platform)
    feedback: dict[str, Any] = {
        "is_valid": True,
        "message": f"Настройки {title} сохранены.",
        "timestamp": datetime.now(UTC).strftime("%H:%M:%S UTC"),
    }

    if raw_cookies:
        try:
            parsed = _json.loads(raw_cookies)
            norm = normalize_social_storage_state(parsed, platform)
            if norm["cookies"]:
                cfg.storage_state = norm
                cfg.status = "active"
                await save_social_browser_config(session, cfg)
                feedback = await probe_social_browser_session(cfg, session=session)
            else:
                feedback = {
                    "is_valid": False,
                    "message": f"{title}: в JSON не найдено кук.",
                    "timestamp": datetime.now(UTC).strftime("%H:%M:%S UTC"),
                }
        except _json.JSONDecodeError as exc:
            feedback = {
                "is_valid": False,
                "message": f"{title}: ошибка формата JSON ({exc}).",
                "timestamp": datetime.now(UTC).strftime("%H:%M:%S UTC"),
            }
    else:
        await save_social_browser_config(session, cfg)

    all_cfgs = await load_all_social_browser_configs(session)
    return templates.TemplateResponse(
        request,
        "partials/social_browser_cards.html",
        {
            "admin": admin,
            "social_browser_conns": [c.to_public_dict() for c in all_cfgs.values()],
            "active_social_platform": platform,
            "social_browser_feedback": feedback,
        },
    )


@partials_router.post(
    "/settings/social-browser/{platform}/probe",
    response_class=HTMLResponse,
    summary="Probe saved browser session for a social network (HTMX)",
)
async def settings_social_browser_probe_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    platform: str,
) -> HTMLResponse:
    """Verify saved browser session cookies via Playwright Stealth."""
    from app.services.social_browser_publisher import (  # noqa: PLC0415
        SOCIAL_BROWSER_PLATFORMS,
        load_all_social_browser_configs,
        load_social_browser_config,
        probe_social_browser_session,
    )

    if platform not in SOCIAL_BROWSER_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unknown platform")

    cfg = await load_social_browser_config(session, platform)
    feedback = await probe_social_browser_session(cfg, session=session)
    target = request.headers.get("HX-Target", "")
    if target.startswith("card-test-"):
        title = cfg.title or platform.capitalize()
        return templates.TemplateResponse(
            request,
            "partials/service_test_result.html",
            {
                "admin": admin,
                "result": {
                    "service": title,
                    "ok": bool(feedback.get("is_valid")),
                    "message": (
                        feedback.get("message")
                        or f"Проверка сессии {title} завершена"
                    ),
                    "details": (
                        feedback.get("details")
                        or f"Кук в сессии: {feedback.get('cookies_count', 0)}"
                    ),
                },
            },
        )
    all_cfgs = await load_all_social_browser_configs(session)
    return templates.TemplateResponse(
        request,
        "partials/social_browser_cards.html",
        {
            "admin": admin,
            "social_browser_conns": [c.to_public_dict() for c in all_cfgs.values()],
            "active_social_platform": platform,
            "social_browser_feedback": feedback,
        },
    )


@partials_router.post(
    "/settings/social-browser/{platform}/test",
    response_class=HTMLResponse,
    summary="Test post formatting or live publish on a browser social network (HTMX)",
)
async def settings_social_browser_test_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
    platform: str,
) -> HTMLResponse:
    """Publish the latest article (or preview post) to the selected social network."""
    from app.services.social_browser_publisher import (  # noqa: PLC0415
        SOCIAL_BROWSER_PLATFORMS,
        load_all_social_browser_configs,
        load_social_browser_config,
        publish_via_social_browser,
    )

    if platform not in SOCIAL_BROWSER_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unknown platform")

    cfg = await load_social_browser_config(session, platform)
    title = str(SOCIAL_BROWSER_PLATFORMS[platform].get("short_title") or platform)
    now_str = datetime.now(UTC).strftime("%H:%M:%S UTC")

    stmt = select(Article).order_by(Article.created_at.desc()).limit(1)
    latest_article = (await session.execute(stmt)).scalar_one_or_none()

    if not cfg.has_session:
        feedback = {
            "is_valid": False,
            "message": (
                f"⚠️ {title}: сначала вставьте Cookies JSON "
                "из Cookie-Editor и сохраните."
            ),
            "timestamp": now_str,
        }
    elif latest_article is None:
        feedback = {
            "is_valid": False,
            "message": (
                f"⚠️ {title}: в базе пока нет готовых статей "
                "для тестовой публикации."
            ),
            "timestamp": now_str,
        }
    else:
        wp_url = (
            f"https://svodka.press/{latest_article.category}/"
            f"{latest_article.url_slug or f'post-{latest_article.id}'}/"
        )
        try:
            cfg.is_active = True
            res = await publish_via_social_browser(cfg, latest_article, wp_url)
            feedback = {
                "is_valid": res.is_published,
                "message": (
                    f"🚀 {title}: тестовый пост опубликован! URL: {res.url}"
                    if res.is_published
                    else f"⚠️ {title}: {res.error}"
                ),
                "timestamp": now_str,
            }
        except Exception as exc:  # noqa: BLE001
            feedback = {
                "is_valid": False,
                "message": f"⚠️ {title}: {exc}",
                "timestamp": now_str,
            }

    all_cfgs = await load_all_social_browser_configs(session)
    return templates.TemplateResponse(
        request,
        "partials/social_browser_cards.html",
        {
            "admin": admin,
            "social_browser_conns": [c.to_public_dict() for c in all_cfgs.values()],
            "active_social_platform": platform,
            "social_browser_feedback": feedback,
        },
    )


@partials_router.post(
    "/settings/social-browser/tenchat/request-sms",
    response_class=HTMLResponse,
    summary="Request SMS code for TenChat login via Sber ID (HTMX)",
)
async def settings_social_browser_tenchat_request_sms_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Trigger Playwright to open TenChat, enter phone number and wait for Sber ID SMS."""
    from app.services.social_browser_publisher import (  # noqa: PLC0415
        load_all_social_browser_configs,
    )
    from app.services.tenchat_auth import tenchat_auth_manager  # noqa: PLC0415

    form = await request.form()
    phone = str(form.get("tenchat_phone") or "+79918829870").strip()

    result = await tenchat_auth_manager.start_sms_flow(phone=phone)

    all_cfgs = await load_all_social_browser_configs(session)
    feedback = {
        "is_valid": result.get("status") in ("success", "sms_required"),
        "message": result.get("message", ""),
        "timestamp": datetime.now(UTC).strftime("%H:%M:%S UTC"),
    }

    return templates.TemplateResponse(
        request,
        "partials/social_browser_cards.html",
        {
            "admin": admin,
            "social_browser_conns": [c.to_public_dict() for c in all_cfgs.values()],
            "active_social_platform": "tenchat",
            "social_browser_feedback": feedback,
            "tenchat_sms_pending": tenchat_auth_manager.is_pending,
            "tenchat_sms_message": tenchat_auth_manager.status_message,
        },
    )


@partials_router.post(
    "/settings/social-browser/tenchat/verify-sms",
    response_class=HTMLResponse,
    summary="Submit 5-digit SMS code for TenChat login (HTMX)",
)
async def settings_social_browser_tenchat_verify_sms_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Submit SMS code into the open Playwright session, fetch cookies and save."""
    from app.services.social_browser_publisher import (  # noqa: PLC0415
        load_all_social_browser_configs,
    )
    from app.services.tenchat_auth import tenchat_auth_manager  # noqa: PLC0415

    form = await request.form()
    sms_code = str(form.get("sms_code") or "").strip()

    result = await tenchat_auth_manager.verify_code(sms_code, session=session)

    all_cfgs = await load_all_social_browser_configs(session)
    feedback = {
        "is_valid": result.get("status") == "success",
        "message": result.get("message", ""),
        "timestamp": datetime.now(UTC).strftime("%H:%M:%S UTC"),
    }

    response = templates.TemplateResponse(
        request,
        "partials/social_browser_cards.html",
        {
            "admin": admin,
            "social_browser_conns": [c.to_public_dict() for c in all_cfgs.values()],
            "active_social_platform": "tenchat",
            "social_browser_feedback": feedback,
            "tenchat_sms_pending": tenchat_auth_manager.is_pending,
            "tenchat_sms_message": tenchat_auth_manager.status_message,
        },
    )
    if result.get("status") == "success":
        response.headers["HX-Trigger"] = "service-updated"
    return response


@partials_router.post(
    "/settings/social-browser/tenchat/cancel-sms",
    response_class=HTMLResponse,
    summary="Cancel active SMS login for TenChat (HTMX)",
)
async def settings_social_browser_tenchat_cancel_sms_partial(
    request: Request,
    admin: PartialAdmin,
    session: SessionDep,
) -> HTMLResponse:
    """Cancel active SMS login attempt and free browser resources."""
    from app.services.social_browser_publisher import (  # noqa: PLC0415
        load_all_social_browser_configs,
    )
    from app.services.tenchat_auth import tenchat_auth_manager  # noqa: PLC0415

    await tenchat_auth_manager.cancel()

    all_cfgs = await load_all_social_browser_configs(session)
    feedback = {
        "is_valid": True,
        "message": "Вход через СМС отменён.",
        "timestamp": datetime.now(UTC).strftime("%H:%M:%S UTC"),
    }

    return templates.TemplateResponse(
        request,
        "partials/social_browser_cards.html",
        {
            "admin": admin,
            "social_browser_conns": [c.to_public_dict() for c in all_cfgs.values()],
            "active_social_platform": "tenchat",
            "social_browser_feedback": feedback,
            "tenchat_sms_pending": False,
        },
    )





