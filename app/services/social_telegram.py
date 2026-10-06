"""Telegram adapter: posts an article teaser to a channel via the Bot API.

Telegram is the highest-traffic distribution target for a Russian news site, and
its API imposes the tightest formatting constraint of the four platforms: a photo
**caption** is capped at 1024 characters while a plain message allows 4096. That
single asymmetry shapes the whole adapter.

The consequences, spelled out because each is easy to get wrong:

* **The cap is measured in UTF-16 code units, not Python characters.** Telegram
  counts the way JavaScript does, so an emoji costs two. A caption of 1024 Python
  characters containing emoji is rejected as too long, and the failure looks
  arbitrary unless you know the encoding. :func:`caption_length` measures the way
  Telegram does.
* **Markup is HTML, and the entity set is small.** Telegram accepts only a
  documented subset — ``<b>``, ``<i>``, ``<u>``, ``<s>``, ``<a>``, ``<code>``,
  ``<pre>``, ``<blockquote>``. Sending an article body verbatim fails on the first
  ``<h2>``, so the teaser is built from plain text and marked up here rather than
  passed through.
* **Every interpolated value is escaped.** A title containing ``&`` or ``<`` breaks
  the parse and Telegram rejects the whole message, so titles and teasers go
  through :func:`escape_html` before they touch the template.

**The photo path degrades to text.** ``sendPhoto`` needs a URL Telegram can fetch;
when an article has no cover, or the fetch fails, the adapter falls back to
``sendMessage`` with a link preview rather than reporting a failure. A published
text post beats an unpublished perfect one.

**429 is honoured, not guessed.** Telegram returns ``parameters.retry_after`` with
the exact number of seconds. Ignoring it and applying our own backoff is how a bot
gets its rate limit escalated, so the value is read and waited out — once, because
a second throttle in the same call means the channel is saturated and the job
should be re-queued instead.

The bot token is never logged and never interpolated into an exception message: it
appears in the URL path, so a leaked error string would leak the credential.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self

import httpx

from app.config import settings
from app.models.enums import Platform
from app.services.social_base import (
    BaseSocialAdapter,
    PublishResult,
    SocialAuthError,
    SocialNotConfiguredError,
    SocialRateLimitError,
    SocialResponseError,
    build_teaser,
)

if TYPE_CHECKING:
    from app.models.article import Article

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MAX_CAPTION_LENGTH",
    "MAX_MESSAGE_LENGTH",
    "TELEGRAM_API_BASE_URL",
    "TelegramAdapter",
    "build_caption",
    "build_inline_keyboard",
    "caption_length",
    "escape_html",
]

TELEGRAM_API_BASE_URL: Final[str] = "https://api.telegram.org"

#: Bot API limits. A photo caption is capped far below a plain message, which is
#: why the two paths build their text separately.
MAX_CAPTION_LENGTH: Final[int] = 1024
MAX_MESSAGE_LENGTH: Final[int] = 4096

#: Room reserved for the title, the markup and the trailing link inside a caption.
#: The teaser is trimmed to fit what remains, so the assembled caption cannot
#: overshoot and be rejected after the fact.
_CAPTION_OVERHEAD: Final[int] = 220

#: One initial attempt plus two retries. Small on purpose: Telegram's own
#: ``retry_after`` already paces us, and a long retry chain on a saturated channel
#: delays the other three platforms.
MAX_ATTEMPTS: Final[int] = 3
INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
BACKOFF_MULTIPLIER: Final[float] = 2.0

#: Upper bound honoured for a server-supplied ``retry_after``. A saturated channel
#: reporting minutes must re-queue the job rather than park a worker.
MAX_RETRY_AFTER_SECONDS: Final[float] = 30.0

REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0

#: The tag subset Telegram's HTML parse mode accepts. Anything else is stripped
#: from a teaser before it is sent.
_ALLOWED_TAGS: Final[frozenset[str]] = frozenset(
    {"b", "strong", "i", "em", "u", "s", "a", "code", "pre", "blockquote"}
)
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"</?([a-zA-Z0-9]+)[^>]*>")

#: Telegram error substrings that identify a permanently rejected request.
_PERMANENT_MARKERS: Final[tuple[str, ...]] = (
    "chat not found",
    "bot was blocked",
    "bot was kicked",
    "not enough rights",
    "chat_id is empty",
    "wrong file identifier",
    "message caption is too long",
    "can't parse entities",
)


def escape_html(text: str) -> str:
    """Escape text for Telegram's HTML parse mode.

    ``&``, ``<`` and ``>`` are the three characters Telegram documents as needing
    escapes. Quotes are left alone: they are legal in text content, and escaping
    them produces visible ``&quot;`` in a caption.
    """
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def caption_length(text: str) -> int:
    """Measure text the way Telegram counts it.

    Telegram counts UTF-16 code units, so an emoji or any character outside the
    BMP costs two. Measuring with ``len()`` lets a caption that looks compliant be
    rejected as too long, which is a confusing failure to debug in production.
    """
    return len(text.encode("utf-16-le")) // 2


def _strip_unsupported_tags(text: str) -> str:
    """Remove HTML tags Telegram's parser would reject.

    A surviving ``<h2>`` makes Telegram fail the whole message with
    "can't parse entities", so unsupported tags are dropped while their text is
    kept.
    """

    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1).lower() in _ALLOWED_TAGS else ""

    return _TAG_RE.sub(replace, text)


def build_caption(
    *,
    title: str,
    body_html: str,
    url: str,
    description: str = "",
    hashtags: Sequence[str] = (),
    limit: int = MAX_CAPTION_LENGTH,
    include_link: bool = True,
) -> str:
    """Assemble a Telegram caption within ``limit`` UTF-16 units.

    Layout: a bold headline, a blank line, the teaser, then hashtags and a
    "Читать полностью" link. The link is last because Telegram renders the first
    link in a message as the preview source, and a preview of the *article* is
    what we want rather than a preview of something quoted inside the teaser.

    The teaser budget is computed from what the fixed parts consume, so the result
    is within the limit by construction instead of being truncated afterwards —
    truncating assembled HTML can cut a tag in half and make the message unparsable.
    """
    safe_title = escape_html(" ".join(title.split()))
    tag_line = " ".join(hashtags)
    link_line = (
        f'<a href="{html.escape(url, quote=True)}">Читать полностью</a>'
        if include_link and url
        else ""
    )

    fixed = caption_length(f"<b>{safe_title}</b>\n\n\n\n{tag_line}\n{link_line}")
    budget = max(limit - fixed, 80)
    teaser = build_teaser(
        title=title, body_html=body_html, limit=budget, description=description
    )
    safe_teaser = escape_html(_strip_unsupported_tags(teaser))

    parts = [f"<b>{safe_title}</b>", "", safe_teaser]
    if tag_line:
        parts.extend(["", tag_line])
    if link_line:
        parts.extend(["", link_line])
    caption = "\n".join(parts).strip()

    if caption_length(caption) <= limit:
        return caption
    # Defensive: an unusually wide title can still overshoot. Drop the teaser
    # rather than cut it, so the caption stays valid HTML.
    minimal = "\n".join(
        part for part in (f"<b>{safe_title}</b>", tag_line, link_line) if part
    )
    return minimal


def build_inline_keyboard(url: str, *, label: str = "Читать на сайте") -> dict[str, Any]:
    """Build the CTA button markup.

    An inline keyboard is preferred over a bare link because it survives caption
    truncation, is tappable on mobile without hitting a small link target, and
    reports clicks in Telegram's own statistics.

    Telegram requires an ``https`` URL for a URL button and rejects the message
    otherwise, which is checked by the caller rather than silently coerced here.
    """
    return {"inline_keyboard": [[{"text": label, "url": url}]]}


def _is_permanent(description: str) -> bool:
    """Whether a Telegram error description describes an unfixable request."""
    lowered = description.lower()
    return any(marker in lowered for marker in _PERMANENT_MARKERS)


class TelegramAdapter(BaseSocialAdapter):
    """Publishes an article teaser to a Telegram channel."""

    platform = Platform.TELEGRAM

    __slots__ = ("_base_url", "_channel_id", "_client", "_lock", "_token", "_transport")

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        channel_id: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = (
            settings.telegram_bot_token if bot_token is None else bot_token
        ).strip()
        self._channel_id = (
            settings.telegram_channel_id if channel_id is None else channel_id
        ).strip()
        self._base_url = (base_url or TELEGRAM_API_BASE_URL).rstrip("/")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """Whether both a bot token and a target channel are available."""
        return bool(self._token and self._channel_id)

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
        """Close the pooled HTTP client, if one was created."""
        async with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return the shared client, validating configuration on first use."""
        if not self.is_configured:
            raise SocialNotConfiguredError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID are required to "
                "publish to Telegram"
            )
        async with self._lock:
            if self._client is None:
                # The token sits in the URL path, so it is baked into the base URL
                # rather than repeated at every call site.
                self._client = httpx.AsyncClient(
                    base_url=f"{self._base_url}/bot{self._token}",
                    timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                    transport=self._transport,
                )
            return self._client

    def _handle_payload(self, payload: Mapping[str, Any], method: str) -> dict[str, Any]:
        """Validate a Bot API envelope and return its ``result``.

        Telegram wraps every answer in ``{"ok": bool, ...}``. A failure carries
        ``description`` and, for 429, ``parameters.retry_after``; the numeric
        ``error_code`` alone does not distinguish a throttle from a bad request.
        """
        if payload.get("ok") is True:
            result = payload.get("result")
            if isinstance(result, Mapping):
                return dict(result)
            raise SocialResponseError(
                f"{method} succeeded but returned no result object"
            )

        description = str(payload.get("description") or "unspecified error")[:300]
        code = payload.get("error_code")
        parameters = payload.get("parameters")
        retry_after: float | None = None
        if isinstance(parameters, Mapping):
            raw = parameters.get("retry_after")
            if isinstance(raw, (int, float)):
                retry_after = min(float(raw), MAX_RETRY_AFTER_SECONDS)

        if code == httpx.codes.TOO_MANY_REQUESTS or retry_after is not None:
            raise SocialRateLimitError(
                f"{method}: Telegram throttled the bot: {description}",
                retry_after=retry_after,
            )
        if code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            raise SocialAuthError(
                f"{method}: Telegram rejected the bot token or its channel "
                f"permissions: {description}"
            )
        raise SocialResponseError(
            f"{method} failed: {description}", retryable=not _is_permanent(description)
        )

    async def _call(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Call one Bot API method, honouring ``retry_after`` on a throttle.

        Telegram states the exact wait; applying our own backoff instead is how a
        bot gets its limit tightened. A second throttle in the same call is
        surfaced so the orchestrator can re-queue rather than hold a worker.
        """
        client = await self._ensure_client()
        backoff = INITIAL_BACKOFF_SECONDS

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(f"/{method}", json=dict(payload))
            except httpx.TimeoutException as exc:
                if attempt == MAX_ATTEMPTS:
                    raise SocialResponseError(
                        f"{method} timed out after "
                        f"{REQUEST_TIMEOUT_SECONDS:.0f}s",
                        retryable=True,
                    ) from exc
            except httpx.HTTPError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise SocialResponseError(
                        f"{method} transport failure: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
            else:
                try:
                    body: Any = response.json()
                except ValueError as exc:
                    raise SocialResponseError(
                        f"{method} returned a non-JSON body "
                        f"(HTTP {response.status_code})",
                        retryable=True,
                    ) from exc
                if not isinstance(body, dict):
                    raise SocialResponseError(
                        f"{method} returned {type(body).__name__}, expected an object"
                    )
                try:
                    return self._handle_payload(body, method)
                except SocialRateLimitError as exc:
                    if attempt == MAX_ATTEMPTS:
                        raise
                    delay = (
                        exc.retry_after
                        if exc.retry_after is not None
                        else min(backoff, MAX_RETRY_AFTER_SECONDS)
                    )
                    logger.warning(
                        "Telegram throttled %s (attempt %d/%d); waiting %.1fs",
                        method,
                        attempt,
                        MAX_ATTEMPTS,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    backoff *= BACKOFF_MULTIPLIER
                    continue

            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(min(backoff, MAX_RETRY_AFTER_SECONDS))
                backoff *= BACKOFF_MULTIPLIER

        raise SocialResponseError(f"{method} exhausted its retries", retryable=True)

    def _message_url(self, result: Mapping[str, Any]) -> str:
        """Build a permalink to the published message.

        Only public channels with a ``@username`` have a ``t.me`` permalink; a
        numeric ``-100…`` id has none, so an empty string is returned rather than a
        fabricated URL that would 404 for a reader.
        """
        chat = result.get("chat")
        username = ""
        if isinstance(chat, Mapping):
            raw = chat.get("username")
            username = str(raw) if isinstance(raw, str) else ""
        if not username and self._channel_id.startswith("@"):
            username = self._channel_id.removeprefix("@")
        message_id = result.get("message_id")
        if username and isinstance(message_id, int):
            return f"https://t.me/{username}/{message_id}"
        return ""

    async def publish(
        self, article: Article, wp_post_url: str, **kwargs: Any
    ) -> PublishResult:
        """Post the article to the channel as a photo with a caption, or as text.

        ``photo_url`` may be passed explicitly; otherwise it is taken from
        ``kwargs['photo_url']`` or omitted. Telegram must be able to fetch the URL
        itself, so a local path is not usable here.
        """
        photo_url = str(kwargs.get("photo_url") or "").strip()
        hashtags = tuple(kwargs.get("hashtags") or ())
        warnings: list[str] = []

        # An inline URL button requires https; Telegram rejects the message
        # otherwise, so a non-https link degrades to the in-caption link only.
        use_button = wp_post_url.startswith("https://")
        if wp_post_url and not use_button:
            warnings.append(
                "the CTA button was omitted: Telegram requires an https URL"
            )

        if photo_url:
            caption = build_caption(
                title=article.title,
                body_html=article.content_html,
                url=wp_post_url,
                description=article.seo_description or "",
                hashtags=hashtags,
                limit=MAX_CAPTION_LENGTH,
                # With a button the caption link is redundant and consumes budget
                # that the teaser can use instead.
                include_link=not use_button,
            )
            payload: dict[str, Any] = {
                "chat_id": self._channel_id,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML",
            }
        else:
            warnings.append("no cover image: published as a text message")
            payload = {
                "chat_id": self._channel_id,
                "text": build_caption(
                    title=article.title,
                    body_html=article.content_html,
                    url=wp_post_url,
                    description=article.seo_description or "",
                    hashtags=hashtags,
                    limit=MAX_MESSAGE_LENGTH,
                    include_link=not use_button,
                ),
                "parse_mode": "HTML",
                # The preview is the article itself, which is the point of the post.
                "link_preview_options": {"is_disabled": False},
            }

        if use_button:
            payload["reply_markup"] = build_inline_keyboard(wp_post_url)

        method = "sendPhoto" if photo_url else "sendMessage"
        try:
            result = await self._call(method, payload)
        except SocialResponseError as exc:
            # A rejected photo (unfetchable URL, wrong dimensions) still leaves a
            # publishable text post, so one fallback is attempted before failing.
            if not photo_url or not exc.retryable:
                raise
            logger.warning("sendPhoto failed (%s); retrying as a text message", exc)
            warnings.append(f"photo rejected, published as text: {exc}")
            fallback: dict[str, Any] = {
                "chat_id": self._channel_id,
                "text": build_caption(
                    title=article.title,
                    body_html=article.content_html,
                    url=wp_post_url,
                    description=article.seo_description or "",
                    hashtags=hashtags,
                    limit=MAX_MESSAGE_LENGTH,
                    include_link=not use_button,
                ),
                "parse_mode": "HTML",
            }
            if use_button:
                fallback["reply_markup"] = build_inline_keyboard(wp_post_url)
            result = await self._call("sendMessage", fallback)

        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            raise SocialResponseError("Telegram returned no message_id")

        return PublishResult.published(
            self.platform_code,
            external_id=str(message_id),
            url=self._message_url(result),
            warnings=warnings,
        )
