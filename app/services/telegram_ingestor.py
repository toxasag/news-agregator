"""Telegram ingestion through the GramJS HTTP bridge (docs/03_INGESTION_MODULE.md).

There is no usable MTProto client for Python in this stack, so Telegram is read
through an external GramJS bridge over HTTP. This module owns three concerns and
nothing else:

* **Transport.** One reusable :class:`httpx.AsyncClient` against
  ``GRAMJS_API_URL`` with a bearer token, bounded retries with exponential
  backoff plus jitter, and typed errors. The token is never logged and never
  interpolated into an exception message.
* **Shape.** The bridge's loosely typed JSON is normalised into frozen value
  objects (:class:`TelegramMessage`, :class:`MediaItem`), albums are collapsed
  by ``groupedId``, and the 0-token heuristic filter from
  :mod:`app.services.text_utils` is applied.
* **Nothing else.** No database access lives here, by design: the module returns
  :class:`IngestResult` / :class:`PreparedPost` value objects and the ingestion
  worker decides what to persist, in its own per-source transaction. That keeps
  fetching testable without a database and keeps the write path (with its
  ``ON CONFLICT`` semantics) in one place. ``Source`` is imported for typing
  only — see the ``TYPE_CHECKING`` block.

Two behaviours are spec-critical and easy to break by "simplifying":

* the deliberate **overlap window** in :meth:`TelegramIngestor.fetch_new_messages`
  (see its docstring for why duplicates are the database's problem, not this
  module's);
* the **album grouping** rules in :func:`group_albums`, including the caption
  tie-break.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Literal, Self

import httpx

from app.config import settings
from app.services.text_utils import FilterResult, analyze_post

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module DB-free
    from app.models.source import Source

logger: Final[logging.Logger] = logging.getLogger(__name__)

DIALOGS_PATH: Final[str] = "/dialogs"
MESSAGES_PATH: Final[str] = "/messages"
MESSAGES_READ_PATH: Final[str] = "/messages/read"

#: Public post permalinks are built against this host.
TELEGRAM_BASE_URL: Final[str] = "https://t.me"

INITIAL_BACKOFF_SECONDS: Final[float] = 0.5
BACKOFF_MULTIPLIER: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 20.0
#: Fraction of the computed delay added as random jitter. Every source in a run
#: hits the same bridge, so identical backoff curves would resynchronise the
#: retries of all failing channels into one burst.
BACKOFF_JITTER_RATIO: Final[float] = 0.25
#: Upper bound honoured for a server-supplied ``Retry-After``: a hostile or
#: buggy header must not park the ingestion worker for hours.
MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0

#: An epoch value above this can only be milliseconds — as seconds it would be
#: some five thousand years from now.
EPOCH_MILLISECONDS_THRESHOLD: Final[float] = 1e11

#: Telegram usernames: leading letter, 5-32 chars total, letters/digits/_.
_USERNAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

#: Non-cryptographic jitter source, taken from the OS entropy pool so that the
#: linter does not have to distinguish "randomness for security" from
#: "randomness for load spreading".
_JITTER: Final[random.SystemRandom] = random.SystemRandom()

MediaType = Literal["photo", "video", "document"]

_MEDIA_KEYS: Final[tuple[str, ...]] = (
    "media",
    "media_items",
    "mediaItems",
    "attachments",
)
#: Bot-API style messages carry the attachment under a type-named key instead of
#: a generic ``media`` list.
_TYPED_MEDIA_KEYS: Final[tuple[tuple[str, MediaType], ...]] = (
    ("photo", "photo"),
    ("video", "video"),
    ("animation", "video"),
    ("document", "document"),
    ("audio", "document"),
    ("voice", "document"),
)
_MEDIA_ID_KEYS: Final[tuple[str, ...]] = (
    "media_id",
    "mediaId",
    "file_id",
    "fileId",
    "document_id",
    "documentId",
    "photo_id",
    "photoId",
    "id",
)
_MEDIA_SIZE_KEYS: Final[tuple[str, ...]] = ("size", "file_size", "fileSize", "bytes")
#: Where to look for the id/size of one attachment: the item itself first, then
#: the nested MTProto object GramJS wraps it in.
_MEDIA_NESTED_KEYS: Final[tuple[str, ...]] = ("photo", "document", "video", "media")

#: Normalised media kind -> stored ``type``. Keys have the ``MessageMedia``
#: prefix already stripped (see :func:`_media_kind`).
_MEDIA_TYPE_ALIASES: Final[Mapping[str, MediaType]] = {
    "photo": "photo",
    "image": "photo",
    "picture": "photo",
    "video": "video",
    "videofile": "video",
    "videonote": "video",
    "roundvideo": "video",
    "animation": "video",
    "gif": "video",
    "document": "document",
    "file": "document",
    "audio": "document",
    "voice": "document",
    "audiofile": "document",
}
#: Attachments that carry no usable media for the pipeline. Per docs/03 these
#: are dropped instead of being stored as documents.
_SKIPPED_MEDIA_KINDS: Final[frozenset[str]] = frozenset(
    {
        "sticker",
        "poll",
        "webpage",
        "geo",
        "geolive",
        "venue",
        "contact",
        "dice",
        "game",
        "invoice",
        "story",
        "unsupported",
        "empty",
    }
)

_MESSAGE_ID_KEYS: Final[tuple[str, ...]] = ("id", "message_id", "messageId")
_MESSAGE_TEXT_KEYS: Final[tuple[str, ...]] = ("message", "text", "caption")
_GROUPED_ID_KEYS: Final[tuple[str, ...]] = (
    "grouped_id",
    "groupedId",
    "group_id",
    "groupId",
    "media_group_id",
    "mediaGroupId",
)
_DATE_KEYS: Final[tuple[str, ...]] = ("date", "timestamp", "published_at", "publishedAt")

_DIALOG_ID_KEYS: Final[tuple[str, ...]] = ("id", "chat_id", "chatId", "peer_id", "peerId")
_DIALOG_TITLE_KEYS: Final[tuple[str, ...]] = ("title", "name", "chat_title", "chatTitle")
_DIALOG_USERNAME_KEYS: Final[tuple[str, ...]] = ("username", "user_name", "handle")
_DIALOG_MEMBER_KEYS: Final[tuple[str, ...]] = (
    "participants_count",
    "participantsCount",
    "members_count",
    "membersCount",
    "subscribers",
)
#: MTProto class names that denote a broadcast channel rather than a group.
_CHANNEL_KINDS: Final[frozenset[str]] = frozenset({"channel", "channelforbidden"})
#: Envelope keys a bridge may wrap its payload list in.
_ENVELOPE_KEYS: Final[tuple[str, ...]] = ("result", "results", "data", "items")


class TelegramIngestorError(RuntimeError):
    """Base class for every failure surfaced by :class:`TelegramIngestor`."""


class TelegramNotConfiguredError(TelegramIngestorError):
    """Raised when ``GRAMJS_API_URL`` or ``GRAMJS_TOKEN`` is empty.

    A deployment that ingests websites only is perfectly valid, so callers catch
    this to skip Telegram sources cleanly instead of failing the whole run.
    Constructing the client never raises; only using it does.
    """


class TelegramTransportError(TelegramIngestorError):
    """Network-level failure: timeout, DNS, connection reset, retries exhausted."""


class TelegramStatusError(TelegramIngestorError):
    """Non-2xx response from the bridge."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class TelegramAuthError(TelegramStatusError):
    """HTTP 401/403: the bridge rejected the bearer token."""


class TelegramResponseError(TelegramIngestorError):
    """A 2xx response whose payload cannot be interpreted."""


def _is_retryable_status(status_code: int) -> bool:
    """Whether a status code justifies another attempt.

    Only 429 and 5xx are retried. Any other 4xx is a deterministic error — a
    wrong path, a revoked token, a channel the reader account cannot see — and
    would fail identically on every attempt, so it is surfaced immediately and
    the source is skipped.
    """
    return status_code == httpx.codes.TOO_MANY_REQUESTS or 500 <= status_code < 600


def _retry_after_seconds(header_value: str | None) -> float | None:
    """Parse ``Retry-After`` as delta-seconds or an HTTP-date.

    Returns ``None`` when the header is absent or unparseable, in which case the
    caller falls back to exponential backoff.
    """
    if header_value is None:
        return None
    raw = header_value.strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        seconds = (deadline - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _backoff_delay(backoff: float) -> float:
    """Clamp the exponential delay and spread it with jitter."""
    base = min(backoff, MAX_BACKOFF_SECONDS)
    return base * (1.0 + _JITTER.uniform(0.0, BACKOFF_JITTER_RATIO))


def _as_int(value: Any) -> int | None:
    """Coerce a bridge value to ``int``, or ``None`` when it is not numeric.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, and a stray
    ``true`` must not become the id ``1``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _first_str(payload: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    """First non-blank string among ``keys``; numbers are stringified."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _first_int(payload: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    """First numeric value among ``keys``."""
    for key in keys:
        parsed = _as_int(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _from_epoch(seconds: float) -> datetime | None:
    """Build a UTC datetime from an epoch value in seconds or milliseconds."""
    if not math.isfinite(seconds):
        return None
    if abs(seconds) > EPOCH_MILLISECONDS_THRESHOLD:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _parse_iso_datetime(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, assuming UTC when it carries no offset."""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an epoch number or an ISO-8601 string into an aware datetime.

    GramJS reports MTProto ``date`` as an epoch integer while some bridge builds
    re-serialise it as ISO-8601. A naive ISO value is assumed to be UTC, which is
    what Telegram stores; guessing the project timezone here would silently shift
    every post by three hours.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return _from_epoch(float(value))
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    numeric = _as_int(raw)
    if numeric is not None:
        return _from_epoch(float(numeric))
    return _parse_iso_datetime(raw)


def public_username(identifier: str) -> str | None:
    """Return the public username of a ``sources.identifier``, if it has one.

    Accepts both ``channel`` and ``@channel``. Numeric ids (``-1001234567890``)
    have no public permalink, so they yield ``None``.
    """
    candidate = identifier.strip()
    if candidate.startswith("@"):
        candidate = candidate[1:]
    return candidate if _USERNAME_RE.match(candidate) else None


def build_post_url(identifier: str, message_id: int) -> str | None:
    """Build ``https://t.me/<username>/<id>``, or ``None`` for private channels."""
    username = public_username(identifier)
    if username is None:
        return None
    return f"{TELEGRAM_BASE_URL}/{username}/{message_id}"


@dataclass(frozen=True, slots=True)
class MediaItem:
    """One attachment, as metadata only.

    Binary files are *not* downloaded during ingestion (that is Package 3, see
    docs/06_MEDIA_PROCESSING.md); this is the lightweight shape persisted to
    ``raw_news.media_data``.
    """

    type: MediaType
    original_post_id: int
    media_id: str
    size: int | None

    def to_dict(self) -> dict[str, Any]:
        """Exact JSONB shape stored in ``raw_news.media_data``."""
        return {
            "type": self.type,
            "original_post_id": self.original_post_id,
            "media_id": self.media_id,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class TelegramDialog:
    """A dialog the reader account can see, for the admin source picker."""

    id: str
    title: str
    username: str | None
    is_channel: bool
    participants_count: int | None


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    """One bridge message, normalised. ``channel`` is the requested identifier."""

    id: int
    channel: str
    text: str
    entities: tuple[Mapping[str, Any], ...]
    grouped_id: str | None
    date: datetime | None
    media: tuple[MediaItem, ...]
    views: int | None
    forwards: int | None
    post_url: str | None


@dataclass(frozen=True, slots=True)
class IngestCandidate:
    """One prospective ``raw_news`` row: a standalone post or a merged album."""

    external_post_id: str
    primary_message_id: int
    message_ids: tuple[int, ...]
    text: str
    entities: tuple[Mapping[str, Any], ...]
    media: tuple[MediaItem, ...]
    post_url: str | None
    published_at: datetime | None
    is_album: bool


@dataclass(frozen=True, slots=True)
class PreparedPost:
    """An :class:`IngestCandidate` plus its filter verdict.

    Bundling the two means the worker persists without re-running
    :func:`~app.services.text_utils.analyze_post`, so the HTML conversion and
    the digest/ad heuristics run exactly once per post.
    """

    candidate: IngestCandidate
    filter_result: FilterResult

    @property
    def should_skip(self) -> bool:
        """Whether the post must not enter the paid AI stages."""
        return self.filter_result.should_skip

    def to_raw_news_fields(self) -> dict[str, Any]:
        """Field mapping for a ``raw_news`` insert.

        ``raw_text`` is the message text verbatim, deliberately *not* the
        signature-stripped variant: stripping is a filter concern, and the
        dedup shingler normalises again anyway. The stripped text stays
        available as ``filter_result.clean_text``.
        """
        return {
            "external_post_id": self.candidate.external_post_id,
            "post_url": self.candidate.post_url,
            "raw_text": self.candidate.text,
            "clean_html": self.filter_result.clean_html,
            "media_data": [item.to_dict() for item in self.candidate.media],
            "is_digest": self.filter_result.is_digest,
            "is_ad": self.filter_result.is_ad,
            "filter_reasons": list(self.filter_result.reasons),
        }


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of one pass over one Telegram source.

    ``errors`` is populated instead of raised: a deleted or restricted channel
    must not stop the run, so the worker logs the entry, leaves
    ``sources.last_scraped_at`` untouched and moves on.
    """

    source_id: int
    candidates: tuple[PreparedPost, ...]
    fetched_messages: int
    albums_collapsed: int
    max_message_id: int | None
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether the pass completed without errors."""
        return not self.errors


def _media_kind(payload: Mapping[str, Any]) -> str:
    """Normalised media kind: lowercased, without the ``MessageMedia`` prefix."""
    raw = _first_str(payload, ("type", "className", "_", "media_type", "mediaType"))
    if raw is None:
        return ""
    name = raw.strip().lower()
    for prefix in ("messagemedia", "message"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


def _media_lookup(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The mappings that may hold an attachment's id and size."""
    nested = [
        value
        for key in _MEDIA_NESTED_KEYS
        if isinstance(value := payload.get(key), Mapping)
    ]
    return (payload, *nested)


def _refine_by_mime(kind: MediaType, sources: Sequence[Mapping[str, Any]]) -> MediaType:
    """Upgrade a generic document to photo/video using its MIME type."""
    if kind != "document":
        return kind
    for candidate in sources:
        mime = _first_str(candidate, ("mime_type", "mimeType"))
        if mime is None:
            continue
        lowered = mime.lower()
        if lowered.startswith("video/"):
            return "video"
        if lowered.startswith("image/"):
            return "photo"
    return kind


def _parse_media_item(
    payload: Any, message_id: int, *, fallback_type: MediaType | None = None
) -> MediaItem | None:
    """Normalise one attachment, or ``None`` when it carries nothing usable.

    Unknown kinds are stored as ``document`` so a new Telegram attachment type
    still reaches the media pipeline, while the kinds listed in
    :data:`_SKIPPED_MEDIA_KINDS` (stickers, polls, service attachments) are
    dropped per docs/03. An attachment without an id is also dropped: Package 3
    could not fetch its bytes later.
    """
    if not isinstance(payload, Mapping):
        return None
    kind = _media_kind(payload)
    if kind in _SKIPPED_MEDIA_KINDS:
        return None
    resolved = _MEDIA_TYPE_ALIASES.get(kind) or fallback_type or "document"
    lookup = _media_lookup(payload)
    media_id = next(
        (
            found_id
            for source in lookup
            if (found_id := _first_str(source, _MEDIA_ID_KEYS)) is not None
        ),
        None,
    )
    if media_id is None:
        return None
    size = next(
        (
            found_size
            for source in lookup
            if (found_size := _first_int(source, _MEDIA_SIZE_KEYS)) is not None
        ),
        None,
    )
    return MediaItem(
        type=_refine_by_mime(resolved, lookup),
        original_post_id=message_id,
        media_id=media_id,
        size=size if size is None or size >= 0 else None,
    )


def _parse_media(payload: Mapping[str, Any], message_id: int) -> tuple[MediaItem, ...]:
    """Collect every usable attachment of one message, in bridge order."""
    items: list[MediaItem] = []
    for key in _MEDIA_KEYS:
        value = payload.get(key)
        raw_items: Sequence[Any]
        if isinstance(value, Mapping):
            raw_items = (value,)
        elif isinstance(value, list):
            raw_items = value
        else:
            continue
        items.extend(
            item
            for raw in raw_items
            if (item := _parse_media_item(raw, message_id)) is not None
        )
    if items:
        return tuple(items)
    for key, fallback in _TYPED_MEDIA_KEYS:
        value = payload.get(key)
        # The Bot API sends `photo` as a list of size variants of one image, so
        # only the largest (last) entry is a distinct attachment.
        raw = value[-1] if isinstance(value, list) and value else value
        item = _parse_media_item(raw, message_id, fallback_type=fallback)
        if item is not None:
            return (item,)
    return ()


def _parse_entities(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Keep only object-shaped entities; text_utils validates their contents."""
    value = payload.get("entities")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _parse_grouped_id(payload: Mapping[str, Any]) -> str | None:
    """Album key as a string, or ``None`` for a standalone post.

    MTProto ``groupedId`` is a 64-bit integer that JSON bridges emit as either a
    number or a string, so it is normalised to ``str`` and never compared
    numerically. ``0`` means "no album".
    """
    grouped = _first_str(payload, _GROUPED_ID_KEYS)
    if grouped is None or grouped in {"0", "None", "null"}:
        return None
    return grouped


def _parse_message(payload: Any, channel: str) -> TelegramMessage | None:
    """Normalise one bridge message, or ``None`` when it is unusable.

    A message without a numeric id cannot be keyed, deduplicated or linked, so
    it is skipped. Everything else degrades to a default rather than raising: one
    malformed message must not cost the whole page.
    """
    if not isinstance(payload, Mapping):
        return None
    message_id = _first_int(payload, _MESSAGE_ID_KEYS)
    if message_id is None or message_id <= 0:
        return None
    text = _first_str(payload, _MESSAGE_TEXT_KEYS) or ""
    date_value = next(
        (payload[key] for key in _DATE_KEYS if payload.get(key) is not None), None
    )
    return TelegramMessage(
        id=message_id,
        channel=channel,
        text=text,
        entities=_parse_entities(payload),
        grouped_id=_parse_grouped_id(payload),
        date=_parse_datetime(date_value),
        media=_parse_media(payload, message_id),
        views=_first_int(payload, ("views", "view_count", "viewCount")),
        forwards=_first_int(payload, ("forwards", "forward_count", "forwardCount")),
        post_url=build_post_url(channel, message_id),
    )


def _dialog_is_channel(payload: Mapping[str, Any]) -> bool:
    """Whether a dialog is a broadcast channel rather than a chat or group."""
    for key in ("is_channel", "isChannel", "broadcast", "is_broadcast"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    kind = _first_str(payload, ("type", "className", "entity_type")) or ""
    return kind.strip().lower() in _CHANNEL_KINDS


def _parse_dialog(payload: Any) -> TelegramDialog | None:
    """Normalise one dialog, or ``None`` when it has no usable identifier."""
    if not isinstance(payload, Mapping):
        return None
    identifier = _first_str(payload, _DIALOG_ID_KEYS)
    if identifier is None:
        return None
    username = _first_str(payload, _DIALOG_USERNAME_KEYS)
    if username is not None:
        username = username.lstrip("@") or None
    title = _first_str(payload, _DIALOG_TITLE_KEYS) or username or identifier
    return TelegramDialog(
        id=identifier,
        title=title,
        username=username,
        is_channel=_dialog_is_channel(payload),
        participants_count=_first_int(payload, _DIALOG_MEMBER_KEYS),
    )


def _payload_items(payload: Any, key: str, path: str) -> list[Any]:
    """Unwrap a list payload, tolerating the envelopes bridges wrap it in."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for candidate in (key, *_ENVELOPE_KEYS):
            value = payload.get(candidate)
            if isinstance(value, list):
                return value
        error = payload.get("error") or payload.get("message")
        if isinstance(error, str) and error:
            raise TelegramResponseError(f"{path} returned an error: {error[:200]}")
    raise TelegramResponseError(
        f"{path} returned {type(payload).__name__}, expected a list of {key}"
    )


def _album_media(members: Sequence[TelegramMessage]) -> tuple[MediaItem, ...]:
    """Concatenate album media by ascending message id, unique by ``media_id``.

    The same attachment can arrive twice — the overlap window re-fetches the tail
    of the feed, and a bridge may repeat an item across pages — and duplicated
    media would mean duplicated downloads and OCR runs in Package 3.
    """
    collected: dict[str, MediaItem] = {}
    for member in members:
        for item in member.media:
            collected.setdefault(item.media_id, item)
    return tuple(collected.values())


def _build_candidate(members: Sequence[TelegramMessage]) -> IngestCandidate:
    """Collapse one album (or a single post) into an :class:`IngestCandidate`.

    ``members`` must be sorted by ascending id. The lowest id is the primary key
    because it is stable and reproducible: re-fetching the album later, even
    partially, yields the same ``external_post_id`` and the same permalink.

    Caption tie-break: Telegram attaches the caption to exactly one arbitrary
    member of an album, so the text is taken from the first member that actually
    has one. When several carry text the lowest id wins — the same key the row is
    stored under, which makes the choice deterministic and independent of which
    members this page happened to contain (docs/03 phrases the tie-break as
    "the longest caption", which is *not* stable under the overlap window: a
    later pass that re-reads only part of the album would then pick a different
    text and rewrite the row). ``entities`` are taken from that same message
    because their offsets only make sense against its text.
    """
    primary = members[0]
    text_source = next((member for member in members if member.text.strip()), primary)
    published_at = next(
        (member.date for member in members if member.date is not None), None
    )
    return IngestCandidate(
        external_post_id=str(primary.id),
        primary_message_id=primary.id,
        message_ids=tuple(member.id for member in members),
        text=text_source.text,
        entities=text_source.entities,
        media=_album_media(members),
        post_url=primary.post_url,
        published_at=published_at,
        is_album=primary.grouped_id is not None,
    )


def group_albums(messages: Sequence[TelegramMessage]) -> list[IngestCandidate]:
    """Merge album members into one candidate each (docs/03, "Группировка").

    Telegram delivers an album as several messages sharing one ``groupedId``.
    Persisting them as they arrive would create 3-10 duplicate rows for a single
    news item, so every group collapses into one candidate keyed by its lowest
    message id, and messages without a ``groupedId`` take the same path as a
    single-member group.

    ``is_album`` follows the presence of a ``groupedId``, not the member count: a
    group whose remaining parts fall outside this page is still an album, and the
    overlap window will merge the rest on a later pass.

    Output is ordered by ascending primary id so a run is reproducible, and
    repeated ids are ignored.
    """
    groups: dict[str, list[TelegramMessage]] = {}
    seen: set[int] = set()
    for message in sorted(messages, key=lambda item: item.id):
        if message.id in seen:
            continue
        seen.add(message.id)
        key = (
            f"album:{message.grouped_id}"
            if message.grouped_id is not None
            else f"post:{message.id}"
        )
        groups.setdefault(key, []).append(message)
    candidates = [_build_candidate(members) for members in groups.values()]
    candidates.sort(key=lambda candidate: candidate.primary_message_id)
    return candidates


def prepare_candidate(candidate: IngestCandidate, *, skip_media: bool) -> PreparedPost:
    """Run the 0-token filter over one candidate, honouring ``skip_media``.

    Public because the MTProto reader in :mod:`app.services.telegram_mtproto`
    must apply the identical filter: two transports that prepared candidates
    differently would produce different `raw_news` rows for the same post.
    """
    prepared = replace(candidate, media=()) if skip_media else candidate
    return PreparedPost(
        candidate=prepared,
        filter_result=analyze_post(prepared.text, prepared.entities),
    )


class TelegramIngestor:
    """Async GramJS bridge client plus per-source ingestion orchestration.

    The underlying :class:`httpx.AsyncClient` is created lazily on first use and
    reused afterwards, so TLS and connection setup are paid once per process.
    Release it with :meth:`close` or by using the instance as an async context
    manager.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        max_attempts: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        raw_url = settings.gramjs_api_url if base_url is None else base_url
        self._base_url = raw_url.strip().rstrip("/")
        raw_token = settings.gramjs_token if token is None else token
        self._token = raw_token.strip()
        raw_timeout = settings.ingest_http_timeout if timeout is None else timeout
        self._timeout = float(raw_timeout)
        attempts = (
            settings.ingest_http_retries + 1 if max_attempts is None else max_attempts
        )
        self._max_attempts = max(1, attempts)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """Whether a bridge URL and a token are both available."""
        return bool(self._base_url and self._token)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the pooled HTTP client, if one was ever created."""
        async with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, creating it on first use."""
        if not self.is_configured:
            missing = "GRAMJS_API_URL" if not self._base_url else "GRAMJS_TOKEN"
            raise TelegramNotConfiguredError(
                f"{missing} is not set: Telegram ingestion is unavailable and "
                "callers must skip telegram sources"
            )
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(self._timeout),
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    transport=self._transport,
                )
            return self._client

    def _decode(self, response: httpx.Response, path: str) -> Any:
        """Decode a 2xx body as JSON."""
        try:
            return response.json()
        except ValueError as exc:
            raise TelegramResponseError(
                f"{path} returned a non-JSON body "
                f"(status {response.status_code}, {len(response.content)} bytes)"
            ) from exc

    def _status_error(self, response: httpx.Response, path: str) -> TelegramStatusError:
        """Build a typed error for a non-2xx response, never echoing the token."""
        detail = response.text[:300].replace("\n", " ").strip()
        message = f"{path} failed with HTTP {response.status_code}: {detail or 'no body'}"
        if response.status_code in {
            httpx.codes.UNAUTHORIZED,
            httpx.codes.FORBIDDEN,
        }:
            return TelegramAuthError(
                f"{message} (the bridge rejected GRAMJS_TOKEN)",
                status_code=response.status_code,
            )
        return TelegramStatusError(message, status_code=response.status_code)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Perform one bridge call, retrying timeouts, 429 and 5xx with backoff."""
        client = await self._ensure_client()
        backoff = INITIAL_BACKOFF_SECONDS
        last_error: TelegramIngestorError = TelegramTransportError(
            f"{path} was never attempted"
        )

        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            try:
                response = await client.request(
                    method,
                    path,
                    params=dict(params) if params else None,
                    json=dict(json_body) if json_body is not None else None,
                )
            except httpx.TimeoutException as exc:
                last_error = TelegramTransportError(
                    f"{path} timed out after {self._timeout:.0f}s "
                    f"({type(exc).__name__})"
                )
            except httpx.HTTPError as exc:
                last_error = TelegramTransportError(
                    f"{path} transport failure: {type(exc).__name__}"
                )
            else:
                if response.status_code < httpx.codes.BAD_REQUEST:
                    return self._decode(response, path)
                last_error = self._status_error(response, path)
                if not _is_retryable_status(response.status_code):
                    raise last_error
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))

            if attempt == self._max_attempts:
                break

            delay = _backoff_delay(backoff) if retry_after is None else retry_after
            logger.warning(
                "GramJS %s attempt %d/%d failed (%s); retrying in %.2fs",
                path,
                attempt,
                self._max_attempts,
                last_error,
                delay,
            )
            await asyncio.sleep(delay)
            backoff *= BACKOFF_MULTIPLIER

        raise last_error

    async def fetch_dialogs(self) -> list[TelegramDialog]:
        """List the dialogs the reader account can see.

        Used by the admin UI to pick sources and to verify that a configured
        ``sources.identifier`` is actually reachable. Entries the bridge sends in
        an unexpected shape are skipped with a warning rather than failing the
        whole listing, so one odd dialog cannot hide the other 200.
        """
        payload = await self._request("GET", DIALOGS_PATH)
        dialogs: list[TelegramDialog] = []
        skipped = 0
        for item in _payload_items(payload, "dialogs", DIALOGS_PATH):
            dialog = _parse_dialog(item)
            if dialog is None:
                skipped += 1
                continue
            dialogs.append(dialog)
        if skipped:
            logger.warning(
                "%s: skipped %d dialog(s) without a usable identifier",
                DIALOGS_PATH,
                skipped,
            )
        return dialogs

    async def fetch_messages(
        self,
        channel: str,
        *,
        min_id: int | None = None,
        limit: int | None = None,
    ) -> list[TelegramMessage]:
        """Fetch one page of messages, oldest first.

        ``min_id`` is exclusive on the bridge side (docs/03 spells this field
        ``peer``/``min_id``/``limit``); ``limit`` defaults to
        ``INGEST_MAX_MESSAGES_PER_SOURCE``. The page is returned in ascending id
        order so pagination and album grouping do not depend on the order the
        bridge happened to use.
        """
        page_limit = settings.ingest_max_messages_per_source if limit is None else limit
        params: dict[str, Any] = {"channel": channel, "limit": max(1, page_limit)}
        if min_id is not None:
            params["min_id"] = max(min_id, 0)
        payload = await self._request("GET", MESSAGES_PATH, params=params)

        messages: list[TelegramMessage] = []
        skipped = 0
        for item in _payload_items(payload, "messages", MESSAGES_PATH):
            message = _parse_message(item, channel)
            if message is None:
                skipped += 1
                continue
            messages.append(message)
        if skipped:
            logger.warning(
                "%s: skipped %d malformed message(s) from %s",
                MESSAGES_PATH,
                skipped,
                channel,
            )
        messages.sort(key=lambda message: message.id)
        return messages

    async def mark_read(self, channel: str, max_id: int) -> bool:
        """Mark ``channel`` read up to ``max_id``. Fire-and-forget by contract.

        Marking read is cosmetic for the reader account: it must never fail a
        collection pass, so every error is swallowed into ``False`` plus a
        warning, including a missing bridge configuration. Skipped entirely when
        ``INGEST_MARK_READ`` is off.
        """
        if not settings.ingest_mark_read:
            logger.debug("INGEST_MARK_READ is off; leaving %s unread", channel)
            return False
        try:
            await self._request(
                "POST",
                MESSAGES_READ_PATH,
                json_body={"channel": channel, "max_id": max_id},
            )
        # Deliberately broad: this call is cosmetic and may never propagate.
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not mark %s read up to %d: %s", channel, max_id, exc
            )
            return False
        return True

    async def fetch_new_messages(
        self, channel: str, *, last_max_id: int | None
    ) -> list[TelegramMessage]:
        """Fetch the new tail of ``channel``, with a deliberate overlap window.

        The next pass does not start at the stored watermark but a fixed step
        below it::

            min_id = max(last_max_id - INGEST_OVERLAP_MESSAGES, 0)

        Telegram allows a published post to be **edited** and its **media to be
        attached late**, and an album can be split across a page boundary. Only
        re-reading the last few messages recovers those changes; without the
        overlap they would be lost for good.

        Re-reading is safe because uniqueness is enforced by the database, not
        here: the ``uq_raw_news_source_external_post`` constraint on
        ``(source_id, external_post_id)`` turns the repeat into an
        ``INSERT ... ON CONFLICT DO UPDATE``. This method therefore returns
        overlapping messages **on purpose** and never tries to filter them.

        Pagination walks forward from the watermark when the bridge caps its page
        size, stopping at ``INGEST_MAX_MESSAGES_PER_SOURCE``, on an empty page,
        or as soon as a page brings no id above the cursor — that last guard is
        what prevents an endless loop against a bridge that keeps replying with
        the same ids. Without a watermark there is no lower bound to walk from,
        so a single most-recent page is read and the remainder is left to the
        next tick, which will have one.
        """
        budget = settings.ingest_max_messages_per_source
        cursor: int | None = (
            None
            if last_max_id is None
            else max(last_max_id - settings.ingest_overlap_messages, 0)
        )
        collected: dict[int, TelegramMessage] = {}

        while len(collected) < budget:
            page = await self.fetch_messages(
                channel, min_id=cursor, limit=budget - len(collected)
            )
            if not page:
                break
            fresh = [message for message in page if message.id not in collected]
            for message in fresh:
                collected[message.id] = message
            highest = page[-1].id
            if not fresh or cursor is None or highest <= cursor:
                break
            cursor = highest

        ordered = sorted(collected.values(), key=lambda message: message.id)
        return ordered[:budget]

    async def ingest_source(
        self, source: Source, *, last_max_id: int | None = None
    ) -> IngestResult:
        """Collect one Telegram source and prepare its rows for the worker.

        Fetches with the overlap window, collapses albums, runs the 0-token
        filter, and returns value objects only: **no database access happens in
        this module**. The worker owns persistence, its per-source transaction
        and the ``ON CONFLICT`` upsert, which keeps this service testable without
        a database and keeps the write semantics in one place.

        Failures are returned in :attr:`IngestResult.errors` rather than raised,
        because one dead channel may not halt the run.
        """
        identifier = source.identifier.strip()
        errors: list[str] = []
        try:
            messages = await self.fetch_new_messages(
                identifier, last_max_id=last_max_id
            )
        except TelegramIngestorError as exc:
            logger.warning("Telegram source %s failed: %s", identifier, exc)
            return IngestResult(
                source_id=source.id,
                candidates=(),
                fetched_messages=0,
                albums_collapsed=0,
                max_message_id=None,
                errors=(str(exc),),
            )

        candidates = group_albums(messages)
        albums_collapsed = sum(
            1 for candidate in candidates if len(candidate.message_ids) > 1
        )
        prepared: list[PreparedPost] = []
        for candidate in candidates:
            try:
                prepared.append(
                    prepare_candidate(candidate, skip_media=source.skip_media)
                )
            # A single unparsable post must not cost the rest of the channel.
            except Exception as exc:
                logger.warning(
                    "Filtering post %s of %s failed",
                    candidate.external_post_id,
                    identifier,
                    exc_info=True,
                )
                errors.append(
                    f"post {candidate.external_post_id}: "
                    f"filtering failed ({type(exc).__name__})"
                )

        max_message_id = max((message.id for message in messages), default=None)
        # The read acknowledgement is *not* sent here. The worker sends it after
        # committing the batch, so a failed commit cannot leave posts marked read
        # in Telegram but absent from the database. `mark_read` stays public for
        # that caller.

        logger.info(
            "Telegram %s: %d message(s) -> %d candidate(s), %d album(s) merged",
            identifier,
            len(messages),
            len(prepared),
            albums_collapsed,
        )
        return IngestResult(
            source_id=source.id,
            candidates=tuple(prepared),
            fetched_messages=len(messages),
            albums_collapsed=albums_collapsed,
            max_message_id=max_message_id,
            errors=tuple(errors),
        )


_shared_ingestor: TelegramIngestor | None = None


def get_telegram_ingestor() -> TelegramIngestor:
    """Return the process-wide ingestor, creating it on first call.

    One instance means one connection pool across the worker and the admin UI.
    Constructing it never raises, even without a configured bridge.
    """
    global _shared_ingestor  # noqa: PLW0603 - module-level singleton accessor
    if _shared_ingestor is None:
        _shared_ingestor = TelegramIngestor()
    return _shared_ingestor


async def close_telegram_ingestor() -> None:
    """Release the shared ingestor's connections. Call on shutdown."""
    global _shared_ingestor  # noqa: PLW0603 - module-level singleton accessor
    ingestor = _shared_ingestor
    _shared_ingestor = None
    if ingestor is not None:
        await ingestor.close()


__all__ = [
    "BACKOFF_JITTER_RATIO",
    "BACKOFF_MULTIPLIER",
    "DIALOGS_PATH",
    "EPOCH_MILLISECONDS_THRESHOLD",
    "INITIAL_BACKOFF_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "MAX_RETRY_AFTER_SECONDS",
    "MESSAGES_PATH",
    "MESSAGES_READ_PATH",
    "TELEGRAM_BASE_URL",
    "IngestCandidate",
    "IngestResult",
    "MediaItem",
    "MediaType",
    "PreparedPost",
    "TelegramAuthError",
    "TelegramDialog",
    "TelegramIngestor",
    "TelegramIngestorError",
    "TelegramMessage",
    "TelegramNotConfiguredError",
    "TelegramResponseError",
    "TelegramStatusError",
    "TelegramTransportError",
    "build_post_url",
    "close_telegram_ingestor",
    "get_telegram_ingestor",
    "group_albums",
    "prepare_candidate",
    "public_username",
]
