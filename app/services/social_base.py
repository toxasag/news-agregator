"""Shared contract for every social distribution adapter.

Package 4 publishes one article to four platforms whose APIs agree on nothing:
Telegram wants a caption under a length cap, VK wants a flat attachment string, OK
wants an MD5-signed JSON array, Dzen wants HTML. What they *do* share is the shape
of the answer the orchestrator needs, and that is what lives here.

Three decisions make the interface usable rather than merely uniform:

**A failure is a value, not an exception.** :meth:`BaseSocialAdapter.publish`
returns a :class:`PublishResult` with ``status="failed"`` instead of raising, and
:meth:`safe_publish` guarantees it even when an adapter raises internally. Four
platforms are published independently: Telegram being rate-limited must not stop
the VK post, so the orchestrator needs a per-platform verdict it can record and
move on from. An exception-per-platform design would force a try/except ladder
around each call and make "three succeeded, one failed" awkward to express.

**The result maps onto ``articles.platform_statuses``.** That JSONB column already
stores ``status`` / ``external_id`` / ``url`` / ``error`` / ``attempts`` per
platform code, so :meth:`PublishResult.to_platform_status` writes it directly.
Inventing a different shape here would need a translation layer whose only job is
to rename fields.

**Retryable and permanent failures are distinguished.** ``retryable`` tells the
orchestrator whether a later attempt could succeed. A rate limit or a 5xx is worth
re-queueing; a revoked token or a caption the platform rejected is not, and
retrying it burns quota and delays the platforms that would have worked.

Adapters never touch the database. They receive an :class:`~app.models.article.Article`
read-only and return a value; the caller owns the transaction.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from app.models.enums import Platform, PlatformStatus
from app.services.text_utils import html_to_plain_text

if TYPE_CHECKING:
    from app.models.article import Article

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MAX_HASHTAGS",
    "BaseSocialAdapter",
    "PublishResult",
    "SocialAdapterError",
    "SocialAuthError",
    "SocialNotConfiguredError",
    "SocialRateLimitError",
    "SocialResponseError",
    "build_hashtags",
    "build_teaser",
]

#: Hashtags per post. Beyond this the caption reads as spam on every platform, and
#: VK in particular down-ranks posts that look automated.
MAX_HASHTAGS: Final[int] = 5

#: Sentence-ending punctuation, used to trim a teaser at a natural boundary.
_SENTENCE_END_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?…])\s+")

#: Characters a hashtag cannot contain. VK and OK both accept Cyrillic tags, so
#: only the separators and punctuation are stripped.
_HASHTAG_STRIP_RE: Final[re.Pattern[str]] = re.compile(r"[^\w]+", re.UNICODE)


class SocialAdapterError(RuntimeError):
    """Base class for every failure raised inside an adapter."""


class SocialNotConfiguredError(SocialAdapterError):
    """The platform's credentials are missing.

    Distinguished from an auth failure because the remedy differs: an operator has
    to *add* configuration rather than renew it, and the orchestrator should mark
    the platform ``DISABLED`` rather than ``FAILED`` — a missing token is a
    deployment choice, not an error to retry.
    """


class SocialAuthError(SocialAdapterError):
    """The platform rejected the credentials. Never retryable."""


class SocialRateLimitError(SocialAdapterError):
    """The platform throttled the request. Retryable after a delay."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SocialResponseError(SocialAdapterError):
    """The platform answered with an error or an unusable payload."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class PublishResult:
    """One platform's verdict on one article.

    ``external_id`` and ``url`` are empty on failure rather than ``None`` so the
    field types stay stable across the JSONB round trip; ``to_platform_status``
    converts them back to ``None`` for storage, which is what the column's default
    shape uses.
    """

    platform: str
    status: str = PlatformStatus.FAILED.value
    external_id: str = ""
    url: str = ""
    error: str | None = None
    #: Whether a later attempt could plausibly succeed. Read by the orchestrator
    #: to decide between re-queueing and giving up.
    retryable: bool = False
    published_at: datetime | None = None
    #: Non-fatal notes: a trimmed caption, a dropped attachment. The post exists.
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_published(self) -> bool:
        return self.status == PlatformStatus.PUBLISHED.value

    @classmethod
    def published(
        cls,
        platform: str,
        *,
        external_id: str,
        url: str = "",
        warnings: Sequence[str] = (),
    ) -> PublishResult:
        """Build a success result, stamped with the publication time."""
        return cls(
            platform=platform,
            status=PlatformStatus.PUBLISHED.value,
            external_id=external_id,
            url=url,
            published_at=datetime.now(UTC),
            warnings=tuple(warnings),
        )

    @classmethod
    def failed(
        cls,
        platform: str,
        *,
        error: str,
        retryable: bool = False,
        warnings: Sequence[str] = (),
    ) -> PublishResult:
        """Build a failure result carrying the reason and its retryability."""
        return cls(
            platform=platform,
            status=PlatformStatus.FAILED.value,
            error=error[:1000],
            retryable=retryable,
            warnings=tuple(warnings),
        )

    @classmethod
    def disabled(cls, platform: str, *, reason: str) -> PublishResult:
        """Build a result for a platform that is not configured.

        ``DISABLED`` rather than ``FAILED``: an absent token means the operator
        chose not to publish there, and reporting it as a failure would make a
        deliberately partial deployment look permanently broken on the dashboard.
        """
        return cls(
            platform=platform,
            status=PlatformStatus.DISABLED.value,
            error=reason[:1000],
            retryable=False,
        )

    def to_dict(self) -> dict[str, Any]:
        """Render the contract the requirement specifies."""
        return {
            "platform": self.platform,
            "external_id": self.external_id,
            "url": self.url,
            "status": self.status,
            "error": self.error,
        }

    def to_platform_status(self, *, attempts: int = 1) -> dict[str, Any]:
        """Render the entry stored in ``articles.platform_statuses[platform]``."""
        return {
            "status": self.status,
            "external_id": self.external_id or None,
            "url": self.url or None,
            "error": self.error,
            "attempts": attempts,
            "published_at": (
                self.published_at.isoformat() if self.published_at else None
            ),
        }


def build_teaser(
    *,
    title: str,
    body_html: str,
    limit: int,
    description: str = "",
) -> str:
    """Build a plain-text teaser that fits ``limit`` characters.

    The SEO description is preferred over the body when present: it is already a
    self-contained one-sentence summary, which is exactly what a social teaser
    needs, whereas the body's first paragraph often opens mid-context.

    Trimming happens at a **sentence boundary** where one exists inside the
    budget. A teaser cut mid-word and suffixed with an ellipsis looks broken; one
    that ends on a full stop reads as deliberate. The title is excluded from the
    returned text because every adapter renders it separately — bolded, or as a
    link — and including it here would duplicate it.
    """
    source = description.strip() or html_to_plain_text(body_html)
    text = " ".join(source.split())
    if not text:
        return " ".join(title.split())[:limit].strip()
    if len(text) <= limit:
        return text

    clipped = text[:limit]
    sentences = _SENTENCE_END_RE.split(clipped)
    if len(sentences) > 1:
        # Drop the final, incomplete sentence.
        joined = " ".join(sentences[:-1]).strip()
        if len(joined) >= limit // 3:
            return joined

    # No usable sentence boundary: cut at the last whole word instead.
    boundary = clipped.rfind(" ")
    if boundary >= limit // 3:
        clipped = clipped[:boundary]
    return f"{clipped.rstrip(' ,;:—-')}…"


def build_hashtags(keywords: Sequence[str], *, limit: int = MAX_HASHTAGS) -> list[str]:
    """Turn SEO keywords into hashtags.

    Multi-word phrases are joined rather than split: ``станция пыхтино`` becomes
    ``#станцияпыхтино``, because ``#станция #пыхтино`` creates two generic tags
    that compete with unrelated content instead of one specific tag. Duplicates
    after normalisation collapse, and anything that reduces to fewer than two
    characters is dropped — a one-character hashtag is noise.
    """
    tags: dict[str, None] = {}
    for keyword in keywords:
        collapsed = _HASHTAG_STRIP_RE.sub("", keyword.strip().lower())
        if len(collapsed) >= 2:
            tags.setdefault(collapsed, None)
        if len(tags) >= limit:
            break
    return [f"#{tag}" for tag in tags]


class BaseSocialAdapter(ABC):
    """Interface every social platform adapter implements.

    Subclasses provide :attr:`platform` and :meth:`publish`. Callers should prefer
    :meth:`safe_publish`, which converts an unexpected exception into a
    :class:`PublishResult` so one misbehaving adapter cannot abort a distribution
    run across the other three.
    """

    #: The platform code, matching a :class:`~app.models.enums.Platform` value and
    #: the key used in ``articles.platform_statuses``.
    platform: Platform

    @property
    def platform_code(self) -> str:
        """The platform's string code."""
        return self.platform.value

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this adapter has the credentials it needs.

        Checked before publishing so an unconfigured platform is reported as
        ``DISABLED`` without a wasted HTTP request.
        """

    @abstractmethod
    async def publish(
        self, article: Article, wp_post_url: str, **kwargs: Any
    ) -> PublishResult:
        """Publish one article and report the outcome.

        Implementations return a failure result rather than raising for anything
        the platform reported; only programming errors should escape.
        """

    async def close(self) -> None:
        """Release any pooled connections. Overridden by HTTP-backed adapters."""
        return None

    async def safe_publish(
        self, article: Article, wp_post_url: str, **kwargs: Any
    ) -> PublishResult:
        """Publish, converting every failure into a result.

        This is the method an orchestrator should call. It maps the typed adapter
        errors onto the right status and retryability, and catches anything
        unexpected — a distribution run must survive a bug in one adapter, because
        the alternative is that a Dzen regression silently stops Telegram posts.
        """
        if not self.is_configured:
            return PublishResult.disabled(
                self.platform_code,
                reason=f"{self.platform_code} is not configured; skipping",
            )
        try:
            return await self.publish(article, wp_post_url, **kwargs)
        except SocialAdapterError as exc:
            return self._result_for(exc)
        except Exception as exc:
            # A bug in one adapter must not take the distribution run with it.
            logger.exception("%s adapter raised unexpectedly", self.platform_code)
            return PublishResult.failed(
                self.platform_code,
                error=f"{type(exc).__name__}: {exc}",
                retryable=False,
            )

    def _result_for(self, exc: SocialAdapterError) -> PublishResult:
        """Map a typed adapter error onto a result with the right retryability."""
        if isinstance(exc, SocialNotConfiguredError):
            return PublishResult.disabled(self.platform_code, reason=str(exc))
        if isinstance(exc, SocialRateLimitError):
            logger.warning("%s rate-limited: %s", self.platform_code, exc)
            return PublishResult.failed(
                self.platform_code, error=str(exc), retryable=True
            )
        if isinstance(exc, SocialAuthError):
            logger.error("%s rejected our credentials: %s", self.platform_code, exc)
            return PublishResult.failed(
                self.platform_code, error=str(exc), retryable=False
            )
        retryable = isinstance(exc, SocialResponseError) and exc.retryable
        logger.warning("%s publish failed: %s", self.platform_code, exc)
        return PublishResult.failed(
            self.platform_code, error=str(exc), retryable=retryable
        )
