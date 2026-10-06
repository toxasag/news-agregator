"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Final, Literal

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api.auth import router as auth_router
from app.api.dashboard import pages_router, partials_router
from app.api.feeds import router as feeds_router
from app.api.ingestion import router as ingestion_router
from app.api.logs_sse import install_log_handler
from app.api.logs_sse import router as logs_router
from app.config import settings
from app.database import check_database_connection, dispose_engine
from app.models import Base  # noqa: F401  (registers ORM metadata)
from app.redis_client import check_redis_connection, close_redis_pool
from app.services.ai_client import close_ai_client
from app.services.embedding_service import close_embedding_service
from app.services.flux_generator import close_flux_client
from app.services.telegram_ingestor import close_telegram_ingestor
from app.services.vk_video_service import close_vk_video_service
from app.services.web_scraper import close_browser_pool
from app.services.wp_media_service import close_wp_media_service
from app.services.wp_publisher import close_wp_publisher

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger: Final[logging.Logger] = logging.getLogger("svodkapress")

#: Long-lived resources released on shutdown, in teardown order: the ones that
#: may still be doing work (Chromium, the HTTP clients) go before the pools they
#: could otherwise try to use.
_CLEANUP_TASKS: Final[tuple[tuple[str, Callable[[], Awaitable[None]]], ...]] = (
    ("Chromium browser pool", close_browser_pool),
    ("GramJS HTTP client", close_telegram_ingestor),
    ("OpenRouter HTTP client", close_ai_client),
    ("Fal AI HTTP client", close_flux_client),
    ("VK video HTTP client", close_vk_video_service),
    ("WordPress media HTTP client", close_wp_media_service),
    ("WordPress publisher HTTP client", close_wp_publisher),
    ("embedding cache client", close_embedding_service),
    ("Redis pool", close_redis_pool),
    ("PostgreSQL engine", dispose_engine),
)


async def _release_resources() -> None:
    """Close every long-lived resource, isolating each failure.

    Each cleanup is wrapped individually for two reasons. A raising `close()`
    must not prevent the remaining resources from being released — a wedged
    Chromium process would otherwise leak the database and Redis pools with it.
    And because this runs in the ``finally`` of the lifespan, an exception
    escaping here would replace whatever error caused the shutdown, hiding the
    real cause behind a teardown failure.
    """
    for name, close in _CLEANUP_TASKS:
        try:
            await close()
        # Deliberately broad: shutdown must be best-effort. Chromium, httpx,
        # Redis and asyncpg each fail differently when their transport is
        # already gone, and none of those failures may abort the rest.
        except Exception:
            logger.warning("Failed to release the %s", name, exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Verify backing services on boot and release them on shutdown.

    A failed PostgreSQL check aborts startup: the pipeline cannot operate
    without its state store. A failed Redis check is logged as a warning so the
    dashboard stays reachable for diagnostics while the queue is down.
    """
    logger.info(
        "Starting %s v%s (env=%s)", settings.app_name, __version__, settings.app_env
    )

    try:
        await check_database_connection()
    except Exception as exc:
        logger.critical(
            "PostgreSQL at %s is unreachable", settings.database_url_safe, exc_info=True
        )
        await dispose_engine()
        raise RuntimeError("PostgreSQL health check failed during startup") from exc
    logger.info("PostgreSQL connection established")

    try:
        await check_redis_connection()
        logger.info("Redis connection established")
        install_log_handler()
    except Exception:
        logger.warning(
            "Redis is unreachable at startup; queue and dedup index are degraded",
            exc_info=True,
        )

    try:
        yield
    finally:
        await _release_resources()
        logger.info(
            "Shutdown complete: browser pool, HTTP clients, database and Redis "
            "pools disposed"
        )


app: Final[FastAPI] = FastAPI(
    title=settings.app_name,
    version=__version__,
    description=(
        "Autonomous news aggregation, deduplication, AI enrichment, "
        "media processing and multi-platform publishing pipeline."
    ),
    debug=settings.debug,
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url=None,
    openapi_url="/openapi.json" if not settings.is_production else None,
)

# --------------------------------------------------------------------- proxying
# `X-Forwarded-Proto` / `-For` are applied by uvicorn itself, which wraps the app
# in its own ProxyHeadersMiddleware when `proxy_headers` is on (the default) and
# takes the trusted sources from `FORWARDED_ALLOW_IPS`. Adding that middleware here
# as well would apply it twice, so the deployment sets the environment variable
# from TRUSTED_PROXY_HOSTS instead — see docker-compose.prod.yml. That rewrite
# happens before the app is entered, so `request.url_for` builds `https://` links
# and the post-login redirect does not downgrade the connection.
#
# Starlette runs middleware in reverse registration order, so TrustedHost is
# registered after CORS in order to run before it: a request for a host this app
# does not serve should be rejected outright, not answered with CORS headers.

if settings.cors_allow_origins_list:
    # Only registered when origins are configured. The dashboard is server-rendered
    # and same-origin, so CORS is normally unnecessary; adding it unconditionally
    # would advertise a cross-origin surface that nothing needs.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins_list),
        # The session is a cookie, so credentialed requests must be allowed for
        # CORS to be useful at all — which is also why `*` is rejected in config.
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

if settings.allowed_hosts_list:
    # Rejects a request whose `Host` this app does not serve. Without it, a
    # forged Host is reflected into absolute URLs, which turns the post-login
    # redirect into an open redirect to an attacker's domain.
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts_list)
    )

# Order matters for the dashboard pages: `pages_router` owns `/`, so it is
# registered after the API routers whose prefixes are more specific. Auth comes
# first because every other router's guard depends on the session it establishes.
app.include_router(auth_router)
app.include_router(feeds_router)
app.include_router(ingestion_router)
app.include_router(logs_router)
app.include_router(partials_router)
app.include_router(pages_router)


async def _probe(
    name: str, check: Callable[[], Awaitable[bool]]
) -> tuple[Literal["up", "down"], str | None]:
    """Run one dependency check, logging the cause and returning a safe summary.

    The detailed exception is logged server-side only: connection errors carry
    internal hostnames, ports and DB usernames, which must not be returned to an
    unauthenticated caller.
    """
    try:
        healthy = await check()
    except Exception:
        logger.warning("%s health check failed", name, exc_info=True)
        return "down", "unreachable"
    if healthy:
        return "up", None
    logger.warning("%s health check returned false", name)
    return "down", "check failed"


@app.get("/health", tags=["system"], summary="Liveness probe")
async def health() -> JSONResponse:
    """Report whether this process is alive and can reach its state store.

    Used by the container `HEALTHCHECK` and by load balancers, so it must only
    fail when the process genuinely cannot serve. PostgreSQL is required; Redis
    being down degrades the queue but leaves the dashboard usable, matching the
    lifespan behaviour above, so it does not fail this probe.
    """
    postgres_state, postgres_error = await _probe("postgres", check_database_connection)

    alive = postgres_state == "up"
    payload: dict[str, Any] = {
        "status": "ok" if alive else "unavailable",
        "version": __version__,
        "services": {"postgres": {"status": postgres_state, "error": postgres_error}},
    }
    return JSONResponse(
        content=payload,
        status_code=(
            status.HTTP_200_OK if alive else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
    )


@app.get("/health/ready", tags=["system"], summary="Readiness probe")
async def readiness() -> JSONResponse:
    """Report whether every dependency needed for full pipeline work is up.

    Unlike `/health`, this fails when Redis is unreachable, because the ARQ
    queue and the MinHash LSH dedup index cannot operate without it. Use it to
    gate work scheduling and for alerting, not for liveness.
    """
    postgres_state, postgres_error = await _probe("postgres", check_database_connection)
    redis_state, redis_error = await _probe("redis", check_redis_connection)

    ready = postgres_state == "up" and redis_state == "up"
    payload: dict[str, Any] = {
        "status": "ready" if ready else "degraded",
        "version": __version__,
        "environment": settings.app_env,
        "services": {
            "postgres": {"status": postgres_state, "error": postgres_error},
            "redis": {"status": redis_state, "error": redis_error},
        },
    }
    return JSONResponse(
        content=payload,
        status_code=(
            status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
    )


__all__ = ["app"]
