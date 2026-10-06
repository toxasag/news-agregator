"""Production-mode switch and cron schedule, editable at runtime.

Two operator controls that used to require a redeploy:

**The switch.** Stopping the pipeline meant stopping worker containers over SSH,
which also stopped the healthchecks and made the dashboard report the system as
broken rather than paused. The flag lets a cron tick end immediately instead, so a
paused pipeline is a *state* rather than an outage.

**The schedule.** Collection intervals were literals inside ``WorkerSettings``.
Wanting posts every 15 minutes instead of every 5 meant editing code, rebuilding
the image and recreating three containers.

Both live in ``system_settings`` following the same pattern as the admin
credentials and the model routing, and both are cached in Redis for a few seconds:
a worker reads the flag on every tick, and hitting PostgreSQL for that is wasteful
while a stale-by-seconds answer is harmless.
"""

from __future__ import annotations

# Operator-facing messages are Russian, matching the dashboard.
# ruff: noqa: RUF001
import logging
from dataclasses import dataclass
from typing import Any, Final

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import SystemSetting
from app.redis_client import get_redis

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "PIPELINE_CONTROL_KEY",
    "PipelineControl",
    "ScheduleError",
    "cron_minutes",
    "load_control",
    "pipeline_enabled",
    "save_control",
]

#: Single ``system_settings`` key holding the control document.
PIPELINE_CONTROL_KEY: Final[str] = "pipeline_control"

#: Redis cache key and TTL for the enabled flag. Short enough that flipping the
#: switch takes effect within one tick, long enough that a busy worker does not
#: query PostgreSQL per source.
_CACHE_KEY: Final[str] = "pipeline:enabled"
_CACHE_TTL: Final[int] = 10

#: Bounds on the collection interval. Below 5 minutes Telegram rate limits start
#: dominating; above 120 a news feed goes stale enough that the pipeline stops
#: being a news pipeline.
MIN_INTERVAL_MINUTES: Final[int] = 5
MAX_INTERVAL_MINUTES: Final[int] = 120

#: Default collection cadence, matching the specification's 15 minutes.
DEFAULT_INTERVAL_MINUTES: Final[int] = 15


class ScheduleError(ValueError):
    """A schedule change was rejected. The message is operator-facing."""


@dataclass(frozen=True, slots=True)
class PipelineControl:
    """The live production-mode settings."""

    enabled: bool
    interval_minutes: int
    #: True when these came from the database rather than the defaults.
    from_database: bool
    #: Allow real source media with watermarks instead of forcing AI image generation
    allow_watermarked_source_media: bool = True

    @property
    def cron_minutes(self) -> tuple[int, ...]:
        """Minutes of the hour at which collection fires.

        Derived from the interval rather than stored, so the two can never
        disagree. An interval that does not divide 60 (25, say) still produces a
        valid schedule; the last gap of the hour is simply shorter.
        """
        return tuple(range(0, 60, self.interval_minutes))


def _defaults() -> PipelineControl:
    return PipelineControl(
        enabled=False,
        interval_minutes=DEFAULT_INTERVAL_MINUTES,
        from_database=False,
        allow_watermarked_source_media=True,
    )


async def load_control(session: AsyncSession) -> PipelineControl:
    """Return stored settings, remaining paused when configuration is unavailable."""
    try:
        result = await session.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == PIPELINE_CONTROL_KEY
            )
        )
        stored = result.scalar_one_or_none()
    except SQLAlchemyError:
        logger.warning("Could not read pipeline control; using defaults", exc_info=True)
        return _defaults()

    if not isinstance(stored, dict):
        return _defaults()

    raw_interval = stored.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
    try:
        interval = int(str(raw_interval))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_MINUTES
    interval = min(max(interval, MIN_INTERVAL_MINUTES), MAX_INTERVAL_MINUTES)

    allow_wm = stored.get("allow_watermarked_source_media", True)

    return PipelineControl(
        enabled=stored.get("enabled") is True,
        interval_minutes=interval,
        from_database=True,
        allow_watermarked_source_media=bool(allow_wm),
    )


async def save_control(
    session: AsyncSession,
    *,
    enabled: bool,
    interval_minutes: int,
    allow_watermarked_source_media: bool = True,
) -> PipelineControl:
    """Persist the control settings and invalidate the cached flag."""
    if not MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES:
        raise ScheduleError(
            f"Интервал сбора должен быть от {MIN_INTERVAL_MINUTES} до "
            f"{MAX_INTERVAL_MINUTES} минут."
        )

    document: dict[str, Any] = {
        "enabled": bool(enabled),
        "interval_minutes": int(interval_minutes),
        "allow_watermarked_source_media": bool(allow_watermarked_source_media),
    }
    statement = (
        pg_insert(SystemSetting)
        .values(
            key=PIPELINE_CONTROL_KEY,
            value=document,
            description=(
                "Production-mode switch, collection"
                " interval and media preferences."
            ),
        )
        .on_conflict_do_update(
            index_elements=[SystemSetting.key], set_={"value": document}
        )
    )
    await session.execute(statement)
    await session.commit()

    # The cache is dropped rather than rewritten: a worker mid-tick should read
    # the new value from the source of truth, not a value this request guessed.
    try:
        await get_redis().delete(_CACHE_KEY)
    except RedisError:
        logger.debug("Could not invalidate the pipeline flag cache", exc_info=True)

    logger.info(
        "Pipeline control updated: enabled=%s interval=%dm allow_watermarked=%s",
        enabled,
        interval_minutes,
        allow_watermarked_source_media,
    )
    return PipelineControl(
        enabled=bool(enabled),
        interval_minutes=int(interval_minutes),
        from_database=True,
        allow_watermarked_source_media=bool(allow_watermarked_source_media),
    )


async def pipeline_enabled(session: AsyncSession) -> bool:
    """Return a short-lived cached switch, failing closed without configuration."""
    try:
        client = get_redis()
        cached = await client.get(_CACHE_KEY)
        if cached is not None:
            return cached in (b"1", "1")
    except RedisError:
        logger.debug("Pipeline flag cache unavailable", exc_info=True)

    control = await load_control(session)
    try:
        await get_redis().set(
            _CACHE_KEY, "1" if control.enabled else "0", ex=_CACHE_TTL
        )
    except RedisError:
        logger.debug("Could not cache the pipeline flag", exc_info=True)
    return control.enabled


async def cron_minutes(session: AsyncSession) -> tuple[int, ...]:
    """Minutes of the hour at which collection should fire."""
    control = await load_control(session)
    return control.cron_minutes
