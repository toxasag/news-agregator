"""Date gate for first-pass and post-downtime ingestion.

News ages out within a day. A first collection, or a resume after the pipeline
was stopped overnight, must not pull yesterday's posts just to fill
``INGEST_MAX_MESSAGES_PER_SOURCE``. The cutoff is calendar midnight in
``Europe/Moscow``; the numeric cap applies only to what remains.

Same-day later passes keep the watermark window and do not re-apply this gate:
those posts are already "today" from the operator's point of view.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings

__all__ = [
    "MOSCOW_TZ",
    "is_on_or_after_moscow_today",
    "moscow_today_start",
    "needs_freshness_cutoff",
]


def _timezone() -> ZoneInfo:
    """Resolve the project timezone, falling back to Moscow rather than UTC.

    UTC would shift the calendar day for the hours the owner actually cares
    about (evening in Moscow is still "today" there and already "tomorrow" in
    UTC). A missing zoneinfo database is the only reason to leave Moscow, and
    even then the name is recorded so the fallback is visible in logs.
    """
    try:
        return ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Moscow")


MOSCOW_TZ: Final[ZoneInfo] = _timezone()


def moscow_today_start(now: datetime) -> datetime:
    """Return 00:00 of *now*'s calendar day in ``Europe/Moscow``.

    The result is timezone-aware. Comparisons against Telegram ``message.date``
    (UTC) therefore do not depend on the process's local timezone.
    """
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    local = moment.astimezone(MOSCOW_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def needs_freshness_cutoff(
    last_scraped_at: datetime | None, *, now: datetime
) -> bool:
    """Whether this pass must drop posts older than today's Moscow midnight.

    ``None`` is a first pass. A timestamp whose Moscow calendar date is before
    *now*'s is a resume after downtime (or after a queue reset that rewound the
    watermark). A scrape already dated today is an ordinary later pass.
    """
    if last_scraped_at is None:
        return True
    scraped = (
        last_scraped_at
        if last_scraped_at.tzinfo is not None
        else last_scraped_at.replace(tzinfo=UTC)
    )
    return scraped.astimezone(MOSCOW_TZ).date() < now.astimezone(MOSCOW_TZ).date()


def is_on_or_after_moscow_today(
    published_at: datetime | None, *, now: datetime
) -> bool:
    """Whether *published_at* is today in Moscow.

    A missing timestamp is kept: dropping it would hide a post we cannot date,
    which is worse than admitting one undated item. Telegram always sends
    ``date`` on real messages; ``None`` is a parser gap, not an old post.
    """
    if published_at is None:
        return True
    moment = (
        published_at
        if published_at.tzinfo is not None
        else published_at.replace(tzinfo=UTC)
    )
    return moment.astimezone(MOSCOW_TZ) >= moscow_today_start(now)
