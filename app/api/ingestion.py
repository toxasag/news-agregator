"""HTTP layer for ingestion configuration and manual runs (Package 2).

The endpoints here are the admin surface of the ingestion engine: register a
Telegram channel, build and validate the CSS/XPath rules of a website source,
list what is configured, and trigger a collection pass by hand.

Three decisions are load-bearing and documented at their call sites as well:

* **Upserts, not blind inserts.** ``sources`` carries a unique constraint on
  ``(source_type, identifier)``, so "add a channel" and "edit a channel" are the
  same request. Both source endpoints issue
  ``INSERT ... ON CONFLICT (source_type, identifier) DO UPDATE``, which satisfies
  the idempotency rule from ``AGENTS.md``: re-sending a form (a double click, an
  HTMX retry, a replayed request) updates the row instead of raising
  ``IntegrityError`` or creating a second source that would ingest the same feed
  twice.
* **Soft delete.** ``DELETE`` deactivates a source rather than removing it; see
  :func:`deactivate_source` for why a hard delete is unrecoverable here.
* **An interim auth guard.** :func:`require_api_access` is a shared-secret bearer
  check applied to the whole router. Package 5 replaces it with the JWT cookie
  session from ROADMAP.md; until then these endpoints are not public.

Read paths hold a database session, mutating paths commit through the
:func:`~app.database.get_db` dependency, and the dry-run endpoint touches no
database at all.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.enums import MediaStrategy, SourceType
from app.models.source import Source
from app.schemas.ingestion import (
    CrawlPreviewResponse,
    IngestionRunResponse,
    IngestTriggerRequest,
    SourceResponse,
    TelegramDialogResponse,
    TelegramSourceCreate,
    WebsiteSourceSave,
    WebsiteTestRequest,
)
from app.services.telegram_ingestor import (
    TelegramAuthError,
    TelegramIngestorError,
    TelegramNotConfiguredError,
    get_telegram_ingestor,
)
from app.services.web_scraper import (
    NavigationTimeoutError,
    ScraperError,
    test_crawl_url,
)
from app.workers.ingestion_worker import run_ingestion

logger: Final[logging.Logger] = logging.getLogger(__name__)

#: Name of the unique constraint fencing one feed to one row, declared in
#: :class:`app.models.source.Source`. Referenced by name so the upsert breaks
#: loudly if the constraint is ever renamed, instead of silently inferring a
#: different arbiter index.
SOURCE_CONFLICT_CONSTRAINT: Final[str] = "uq_sources_type_identifier"

#: ``CrawlPreview.strategy`` value meaning the page could not be read at all.
FAILED_STRATEGY: Final[str] = "failed"

#: Substring identifying a navigation timeout inside a failed preview's
#: warnings. ``test_crawl_url`` deliberately never raises — it reports the
#: reason as operator-facing text — so the wording of
#: :class:`~app.services.web_scraper.NavigationTimeoutError` is the only signal
#: available for telling a timeout (504) apart from any other gateway failure
#: (502). Typed exceptions raised past ``test_crawl_url`` are still mapped
#: directly, below.
_TIMEOUT_MARKER: Final[str] = "timed out"

_bearer_scheme: Final[HTTPBearer] = HTTPBearer(
    scheme_name="InterimSharedSecret",
    description=(
        "Interim shared-secret bearer token: send `Authorization: Bearer "
        "<SECRET_KEY>`. Replaced by the JWT cookie session in Package 5."
    ),
    auto_error=False,
)

_UNAUTHORIZED_HEADERS: Final[dict[str, str]] = {"WWW-Authenticate": "Bearer"}


async def require_api_access(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
) -> None:
    """Interim authentication guard for the ingestion API.

    **This is a deliberate placeholder, and Package 5 replaces it.** Proper
    authentication for the dashboard — login form, JWT in an HttpOnly Secure
    cookie, per-user records in ``users`` — is Module 5.1 in ROADMAP.md. Shipping
    these endpoints unauthenticated until then is not an option: they rewrite
    ingestion configuration and can start a collection run, so an open
    deployment would let anyone repoint the pipeline at arbitrary sites and
    drive the Playwright pool.

    Until that lands, the caller must present the deployment's ``SECRET_KEY`` as
    a bearer token. It is compared with :func:`secrets.compare_digest` so the
    check does not leak the secret's length through timing, and no new setting
    is introduced: ``SECRET_KEY`` is the value Package 5 will sign JWTs with, and
    :class:`app.config.Settings` already refuses to boot production while it is
    still the published placeholder.

    Applied to the entire router rather than to the mutating endpoints only. The
    source list and the Telegram dialog list are operational configuration —
    which feeds are watched, which channels the reader account can see — and
    that is not public information either.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Missing bearer token. Send 'Authorization: Bearer <SECRET_KEY>' "
                "until the Package 5 dashboard login replaces this guard."
            ),
            headers=_UNAUTHORIZED_HEADERS,
        )
    # Both sides are encoded because `compare_digest` rejects non-ASCII `str`
    # inputs with a TypeError, which would surface as a 500 instead of a 401.
    if not secrets.compare_digest(
        credentials.credentials.encode("utf-8"), settings.secret_key.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
            headers=_UNAUTHORIZED_HEADERS,
        )


router: Final[APIRouter] = APIRouter(
    prefix="/api",
    tags=["ingestion"],
    dependencies=[Depends(require_api_access)],
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing or invalid interim bearer token."
        }
    },
)

SessionDep = Annotated[AsyncSession, Depends(get_db)]


async def _find_source(
    session: AsyncSession, source_type: SourceType, identifier: str
) -> Source | None:
    """Return the row for one feed, or ``None`` when it is not registered yet."""
    statement = select(Source).where(
        Source.source_type == source_type, Source.identifier == identifier
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def _upsert_source(
    session: AsyncSession,
    *,
    source_type: SourceType,
    identifier: str,
    name: str,
    is_active: bool,
    skip_media: bool,
    media_strategy: MediaStrategy,
    crawl_rules: dict[str, Any] | None,
) -> tuple[Source, bool]:
    """Insert or update one source and report whether the row was created.

    Idempotency: the write is a single
    ``INSERT ... ON CONFLICT (source_type, identifier) DO UPDATE``, so calling
    this twice with the same identifier converges on one row instead of raising
    ``IntegrityError`` — which is what a resubmitted admin form, an HTMX retry or
    a replayed request actually looks like.

    ``created_at`` and ``last_scraped_at`` are deliberately left out of the
    update set: editing a channel's label must not rewrite its history or reset
    the scheduler's ordering (sources are collected ``last_scraped_at NULLS
    FIRST``, so resetting it would starve the other sources). ``crawl_rules`` is
    only updated when the caller supplies rules, so the Telegram endpoint cannot
    blank out selectors it knows nothing about.

    The returned flag drives the 201/200 status code. It comes from a preceding
    existence check rather than from the write itself, which leaves one benign
    race: if a concurrent request creates the row between the check and the
    insert, the response reports 201 for what became an update. The stored data
    is identical either way, because the upsert — not the check — is what
    decides.
    """
    created = await _find_source(session, source_type, identifier) is None

    values: dict[str, Any] = {
        "source_type": source_type,
        "identifier": identifier,
        "name": name,
        "is_active": is_active,
        "skip_media": skip_media,
        "media_strategy": media_strategy,
        "crawl_rules": {} if crawl_rules is None else crawl_rules,
    }
    updates: dict[str, Any] = {
        "name": name,
        "is_active": is_active,
        "skip_media": skip_media,
        "media_strategy": media_strategy,
    }
    if crawl_rules is not None:
        updates["crawl_rules"] = crawl_rules

    statement = (
        insert(Source)
        .values(**values)
        .on_conflict_do_update(
            constraint=SOURCE_CONFLICT_CONSTRAINT, set_=updates
        )
        .returning(Source)
    )
    result = await session.execute(
        statement, execution_options={"populate_existing": True}
    )
    source = result.scalar_one()
    logger.info(
        "%s %s source %s (id=%s)",
        "Created" if created else "Updated",
        source_type.value,
        identifier,
        source.id,
    )
    return source, created


def _apply_write_status(response: Response, *, created: bool) -> None:
    """Report 201 for a created source and 200 for an updated one."""
    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )


def _preview_failure(preview: CrawlPreviewResponse) -> None:
    """Translate a failed dry run into a gateway error, if it failed.

    A *partial* extraction is not a failure: a page whose ``content_selector``
    matched nothing still returns 200, because the warnings attached to it are
    precisely what the selector builder exists to show. Only a preview the
    crawler marked ``failed`` — navigation refused, HTTP >= 400, a non-http URL —
    becomes an error, since there is nothing to build selectors against.
    """
    if preview.strategy != FAILED_STRATEGY:
        return
    reason = "; ".join(preview.warnings) or "the page could not be loaded"
    timed_out = any(_TIMEOUT_MARKER in warning for warning in preview.warnings)
    raise HTTPException(
        status_code=(
            status.HTTP_504_GATEWAY_TIMEOUT
            if timed_out
            else status.HTTP_502_BAD_GATEWAY
        ),
        detail=f"Could not crawl {preview.url}: {reason}",
    )


@router.post(
    "/sources/telegram",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add or edit a Telegram source",
    response_description="The stored source. 201 when created, 200 when updated.",
    responses={
        status.HTTP_200_OK: {"description": "An existing source was updated."},
        status.HTTP_201_CREATED: {"description": "A new source was created."},
    },
)
async def upsert_telegram_source(
    payload: TelegramSourceCreate, response: Response, session: SessionDep
) -> Source:
    """Register a Telegram channel, or edit the one already registered.

    Add and edit are one request because ``(source_type, identifier)`` is unique:
    the write is an ``ON CONFLICT ... DO UPDATE``, so sending the same channel
    twice is a no-op update rather than an error or a duplicated feed. The
    identifier is normalised by
    :class:`~app.schemas.ingestion.TelegramSourceCreate` (a leading ``@`` is
    stripped) so two spellings of one channel cannot both be stored.
    """
    try:
        source, created = await _upsert_source(
            session,
            source_type=SourceType.TELEGRAM,
            identifier=payload.identifier,
            name=payload.name,
            is_active=payload.is_active,
            skip_media=payload.skip_media,
            media_strategy=payload.media_strategy,
            crawl_rules=None,
        )
    except SQLAlchemyError as exc:
        logger.warning("Telegram source upsert failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The source could not be stored: the database is unavailable.",
        ) from exc
    _apply_write_status(response, created=created)
    return source


@router.post(
    "/sources/website/test",
    response_model=CrawlPreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Dry-run a website source's selectors",
    response_description=(
        "What the crawler extracted, plus the warnings and the winning strategy."
    ),
    responses={
        status.HTTP_502_BAD_GATEWAY: {
            "description": "The target site could not be crawled."
        },
        status.HTTP_504_GATEWAY_TIMEOUT: {
            "description": "Navigation exceeded PLAYWRIGHT_NAVIGATION_TIMEOUT."
        },
    },
)
async def test_website_source(payload: WebsiteTestRequest) -> CrawlPreviewResponse:
    """Open one page with the supplied rules and report what came back.

    Pure read: it writes neither ``raw_news`` nor ``sources``, so the operator
    can hammer it while tuning selectors. It takes no database session at all,
    which is the structural guarantee behind that claim.

    A partial extraction is a successful response on purpose — ``warnings`` and
    ``strategy`` name the selector that missed and whether the body came from
    the rules, from Trafilatura or from both, which is the whole point of the
    selector builder. Only a page that could not be read becomes a 502, or a 504
    when navigation timed out.
    """
    rules = None if payload.crawl_rules is None else payload.crawl_rules.to_crawl_rules()
    try:
        preview = await test_crawl_url(str(payload.url), rules)
    except NavigationTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Navigation timed out: {exc}",
        ) from exc
    except ScraperError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"The page could not be crawled: {exc}",
        ) from exc
    # Deliberately broad: Chromium and the Playwright driver can fail in ways
    # the scraper does not wrap (a missing browser binary, a driver handshake
    # error). The cause is logged server-side and the caller gets a plain
    # gateway error rather than a traceback that would name internal paths.
    except Exception as exc:
        logger.warning("Crawl preview for %s failed", payload.url, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The crawler could not process this page "
                f"({type(exc).__name__}); see the server log for details."
            ),
        ) from exc

    result = CrawlPreviewResponse.from_preview(preview)
    _preview_failure(result)
    return result


@router.post(
    "/sources/website/save",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a website source with confirmed crawl rules",
    response_description="The stored source. 201 when created, 200 when updated.",
    responses={
        status.HTTP_200_OK: {"description": "An existing source was updated."},
        status.HTTP_201_CREATED: {"description": "A new source was created."},
    },
)
async def save_website_source(
    payload: WebsiteSourceSave, response: Response, session: SessionDep
) -> Source:
    """Persist the selectors an operator validated with the dry-run endpoint.

    Same idempotency contract as the Telegram endpoint: the listing URL is the
    identifier, ``(source_type, identifier)`` is unique, and the write is an
    ``ON CONFLICT ... DO UPDATE``, so re-saving refined selectors edits the one
    row instead of creating a second source for the same site.
    """
    try:
        source, created = await _upsert_source(
            session,
            source_type=SourceType.WEBSITE,
            identifier=str(payload.identifier),
            name=payload.name,
            is_active=payload.is_active,
            skip_media=payload.skip_media,
            media_strategy=payload.media_strategy,
            crawl_rules=payload.crawl_rules.to_crawl_rules(),
        )
    except SQLAlchemyError as exc:
        logger.warning("Website source upsert failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The source could not be stored: the database is unavailable.",
        ) from exc
    _apply_write_status(response, created=created)
    return source


@router.post(
    "/pipeline/ingest-now",
    response_model=IngestionRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Trigger one ingestion pass and wait for its report",
    response_description="Aggregate report of the run that just finished.",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Another ingestion run holds the global lock."
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Redis is unreachable, so the run cannot be fenced."
        },
    },
)
async def ingest_now(payload: IngestTriggerRequest) -> IngestionRunResponse:
    """Run ingestion synchronously and return the resulting report.

    The pass is executed inline rather than enqueued, so the caller gets real
    counters instead of a job id: the dashboard's "Collect now" button is a
    diagnostic tool, and "did this source actually work" is the question it has
    to answer. A full run over every source can take a while; pass
    ``source_ids`` to bound it to the sources being debugged.

    ``run_ingestion`` fences itself with the global run lock and skips rather
    than queueing. That skip is reported as **409 Conflict**, not as a 200 with
    zeroes, because a caller told "0 fetched" would reasonably conclude the
    sources are empty when in fact no work was attempted.

    Runs are safe to repeat: every write goes through
    ``INSERT ... ON CONFLICT DO NOTHING`` on ``(source_id, external_post_id)``.
    """
    try:
        report = await run_ingestion(source_ids=payload.source_ids)
    except RedisError as exc:
        logger.warning("Manual ingestion could not reach Redis", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Redis is unreachable, so the run lock cannot be acquired and "
                "ingestion was not started."
            ),
        ) from exc

    if report.lock_skipped:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "An ingestion run is already in progress; nothing was "
                "collected by this request. Retry once it finishes."
            ),
        )
    return IngestionRunResponse.from_report(report)


@router.get(
    "/sources",
    response_model=list[SourceResponse],
    status_code=status.HTTP_200_OK,
    summary="List configured sources",
    response_description="Sources matching the filters, ordered by id.",
)
async def list_sources(
    session: SessionDep,
    source_type: Annotated[
        SourceType | None,
        Query(description="Keep only `telegram` or only `website` sources."),
    ] = None,
    is_active: Annotated[
        bool | None,
        Query(description="Keep only active (`true`) or only paused (`false`) sources."),
    ] = None,
) -> list[Source]:
    """Return the configured sources, newest last.

    Ordered by primary key so the dashboard's table is stable between polls;
    both filters are optional and combine with AND.
    """
    statement = select(Source)
    if source_type is not None:
        statement = statement.where(Source.source_type == source_type)
    if is_active is not None:
        statement = statement.where(Source.is_active.is_(is_active))
    statement = statement.order_by(Source.id.asc())
    try:
        result = await session.execute(statement)
    except SQLAlchemyError as exc:
        logger.warning("Listing sources failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sources could not be listed: the database is unavailable.",
        ) from exc
    return list(result.scalars().all())


@router.get(
    "/sources/telegram/dialogs",
    response_model=list[TelegramDialogResponse],
    status_code=status.HTTP_200_OK,
    summary="List Telegram dialogs visible to the reader account",
    response_description="Dialogs the GramJS bridge reported, for the channel picker.",
    responses={
        status.HTTP_502_BAD_GATEWAY: {
            "description": "The GramJS bridge rejected or failed the request."
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Telegram ingestion is not configured on this deployment."
        },
    },
)
async def list_telegram_dialogs() -> list[TelegramDialogResponse]:
    """Proxy the bridge's dialog list for the admin channel picker.

    Read-only and database-free: it exists so an operator can pick a channel
    from what the reader account actually sees instead of typing a username and
    discovering the typo one ingestion tick later.

    A deployment that ingests websites only is valid, so a missing bridge
    configuration is reported as 503 with the variable to set, not as a crash.
    """
    ingestor = get_telegram_ingestor()
    try:
        dialogs = await ingestor.fetch_dialogs()
    except TelegramNotConfiguredError as exc:
        logger.info("Telegram dialog listing unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "GRAMJS_TOKEN is not configured (GRAMJS_API_URL is also "
                "required), so Telegram ingestion is unavailable. Set both in "
                "the environment and restart the API to use the channel picker."
            ),
        ) from exc
    except TelegramAuthError as exc:
        logger.warning("GramJS bridge rejected the token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The GramJS bridge rejected GRAMJS_TOKEN. Check that the token "
                "matches the one the bridge was started with."
            ),
        ) from exc
    except TelegramIngestorError as exc:
        logger.warning("GramJS dialog listing failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"The GramJS bridge could not list dialogs: {exc}",
        ) from exc
    return [TelegramDialogResponse.from_dialog(dialog) for dialog in dialogs]


@router.delete(
    "/sources/{source_id}",
    response_model=SourceResponse,
    status_code=status.HTTP_200_OK,
    summary="Deactivate a source (soft delete)",
    response_description="The source after deactivation.",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No source has that id."}
    },
)
async def deactivate_source(
    session: SessionDep,
    source_id: Annotated[
        int, Path(ge=1, description="`sources.id` of the source to deactivate.")
    ],
) -> Source:
    """Deactivate a source instead of deleting its row.

    **Soft delete is deliberate.** ``raw_news.source_id`` references
    ``sources.id`` with ``ON DELETE CASCADE``, so removing one row would also
    remove every post ever collected from that feed — and with them the
    ``external_post_id`` keys that make ingestion idempotent, the MinHash
    signatures backing the dedup index, and the audit trail explaining why a
    post was filtered. None of that is recoverable, and re-adding the source
    afterwards would re-ingest its whole visible history as new.

    Setting ``is_active = false`` achieves what a delete is asked for — the
    worker only ever loads active sources — while keeping the history intact and
    the decision reversible: re-posting the source to
    ``POST /api/sources/telegram`` or ``/website/save`` with ``is_active: true``
    resumes collection from the stored watermark.
    """
    try:
        source = await session.get(Source, source_id)
    except SQLAlchemyError as exc:
        logger.warning("Loading source %d failed", source_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The source could not be loaded: the database is unavailable.",
        ) from exc
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No source with id {source_id}.",
        )
    source.is_active = False
    await session.flush()
    logger.info("Deactivated source %s (id=%d)", source.identifier, source_id)
    return source


__all__ = [
    "FAILED_STRATEGY",
    "SOURCE_CONFLICT_CONSTRAINT",
    "require_api_access",
    "router",
]
