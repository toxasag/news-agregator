"""Enumerations shared across ORM models and Pydantic schemas."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import Enum as SAEnum


class SourceType(StrEnum):
    """Origin of an ingested post."""

    TELEGRAM = "telegram"
    WEBSITE = "website"


class TelegramPeerKind(StrEnum):
    """Kind of Telegram dialog stored as a source.

    Stored as a plain string column rather than a native PostgreSQL enum so a
    new Telegram peer type is a code change, not a migration that rewrites the
    enum. The dashboard and the importer both switch on these values.
    """

    CHANNEL = "channel"
    MEGAGROUP = "megagroup"
    GIGAGROUP = "gigagroup"
    GROUP = "group"
    BOT = "bot"
    USER = "user"


class MediaStrategy(StrEnum):
    """How media attached to a source's posts must be handled."""

    ORIGINAL = "original"
    OCR_CHECK = "ocr_check"
    ALWAYS_REGENERATE = "always_regenerate"
    TEXT_ONLY = "text_only"


class RiskLevel(StrEnum):
    """Moderation verdict for a news cluster, per the pipeline specification.

    A traffic-light scale rather than low/medium/high because the moderator
    returns these labels directly, and `SPAM` is a fourth state that is not a
    degree of risk at all: an advertisement or a self-promotion post is dropped
    **before** generation, so no article and no API spend follow it.
    """

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    SPAM = "spam"

    @property
    def blocks_generation(self) -> bool:
        """Whether this verdict stops the cluster before any model is called."""
        return self is RiskLevel.SPAM


class ArticleStatus(StrEnum):
    """State machine of `articles`, per docs/01_DATA_PIPELINE.md."""

    DRAFT = "draft"
    DRAFT_TAG = "draft_tag"
    #: Tags resolved and a media plan decided, but the assets are not built yet.
    #: A distinct state because the media stage is the slow, failure-prone one:
    #: without it a retry cannot tell "never planned" from "planned but failed".
    MEDIA_PLANNED = "media_planned"
    MEDIA_READY = "media_ready"
    WP_PUBLISHED = "wp_published"
    DISTRIBUTED = "distributed"
    FAILED = "failed"
    SKIPPED_DUPLICATE = "skipped_duplicate"


class DedupStatus(StrEnum):
    """Per-post dedup/filter outcome recorded on `raw_news`.

    Every ingested post starts at `PENDING` and is moved to a terminal value by
    the deduplication worker: `UNIQUE` opens a new cluster, `UPDATE` attaches to
    an existing one because it carries new facts, `DUPLICATE` matched an earlier
    post, and the `FILTERED_*` values record a 0-token heuristic rejection.

    `SKIPPED_DUPLICATE` from docs/01_DATA_PIPELINE.md lives on this enum rather
    than on `articles` because a skipped duplicate never produces an article
    row: the pipeline stops at `raw_news`, so the outcome has to be recorded
    here or it would be lost.
    """

    PENDING = "pending"
    UNIQUE = "unique"
    DUPLICATE = "duplicate"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    UPDATE = "update"
    FILTERED_DIGEST = "filtered_digest"
    FILTERED_AD = "filtered_ad"


class PlatformStatus(StrEnum):
    """Per-platform distribution state stored inside `platform_statuses`."""

    PENDING = "pending"
    QUEUED = "queued"
    PUBLISHED = "published"
    FAILED = "failed"
    DISABLED = "disabled"


class Platform(StrEnum):
    """Distribution targets."""

    TELEGRAM = "tg"
    VK = "vk"
    OK = "ok"
    DZEN = "dzen"
    SETKA = "setka"
    TENCHAT = "tenchat"
    THREADS = "threads"
    FACEBOOK = "facebook"
    PINTEREST = "pinterest"
    YOUTUBE = "youtube"
    SPARK = "spark"


def pg_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Build a native PostgreSQL enum whose labels are the *values*.

    SQLAlchemy persists enum *names* by default (``TELEGRAM``); the schema spec
    in docs/02_DATABASE_SPEC.md uses the lowercase values (``telegram``), so
    ``values_callable`` is required for both DDL and round-tripping.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        validate_strings=True,
        create_constraint=False,
        values_callable=lambda enum: [member.value for member in enum],
    )


def default_platform_statuses() -> dict[str, dict[str, Any]]:
    """Return a fresh per-platform status map for a new article."""
    return {
        platform.value: {
            "status": PlatformStatus.PENDING.value,
            "external_id": None,
            "url": None,
            "error": None,
            "attempts": 0,
            "published_at": None,
        }
        for platform in Platform
    }


#: Literal JSONB server default kept in sync with :func:`default_platform_statuses`.
#: A space after every ``:`` is required — SQLAlchemy's ``text()`` construct
#: would otherwise read ``:null`` as a bind parameter.
DEFAULT_PLATFORM_STATUSES_JSON: Final[str] = json.dumps(
    default_platform_statuses(), separators=(", ", ": ")
)

__all__ = [
    "DEFAULT_PLATFORM_STATUSES_JSON",
    "ArticleStatus",
    "DedupStatus",
    "MediaStrategy",
    "Platform",
    "PlatformStatus",
    "RiskLevel",
    "SourceType",
    "TelegramPeerKind",
    "default_platform_statuses",
    "pg_enum",
]
