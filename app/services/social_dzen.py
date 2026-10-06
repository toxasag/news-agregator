"""Dzen adapter: publishes an article to a Yandex Dzen channel.

Dzen is the odd one out. Telegram, VK and OK all expose documented, stable public
APIs; Dzen's publishing API is partner-gated and its shape varies by agreement,
which forces two design choices that would be wrong for the other three:

**The payload is rich HTML, not a teaser.** Dzen hosts the full article rather than
linking to it, so this adapter sends the body with a canonical link back to the
site. That makes the ``rel=canonical`` hint the most important field in the
request: without it Dzen's copy competes with the original in search results and
can outrank it, which costs the site the traffic the whole pipeline exists to
generate.

**The tag subset is narrower than WordPress's.** Dzen's editor accepts a small set
of block and inline tags and rejects a document containing anything else. Rather
than send the article body unchanged and have the request fail, the body is reduced
to the accepted subset here — headings collapse to ``<h2>``, everything unsupported
is unwrapped, and its text survives.

**Authentication state is cached with an expiry.** The token exchange is the slow
part of a Dzen publish, and repeating it per article would dominate the run. The
cached state carries an expiry so a token that has aged out is refreshed rather
than used and rejected; a publish that fails on auth invalidates the cache so the
next attempt re-authenticates instead of replaying a dead token.

**The publication lifecycle is explicit.** A Dzen publication is created as a draft
and then transitioned to published, and both steps can fail independently. A draft
that was created but not published is reported as a failure *with* its external id,
so a retry can complete the transition instead of creating a second draft.

The token is never logged and never interpolated into an exception message.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self

import httpx

from app.config import settings
from app.models.enums import Platform
from app.services.social_base import (
    BaseSocialAdapter,
    PublishResult,
    SocialAdapterError,
    SocialAuthError,
    SocialNotConfiguredError,
    SocialRateLimitError,
    SocialResponseError,
)

if TYPE_CHECKING:
    from app.models.article import Article

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "AUTH_STATE_TTL_SECONDS",
    "DZEN_API_BASE_URL",
    "MAX_DZEN_TITLE_CHARS",
    "DzenAdapter",
    "DzenAuthState",
    "build_dzen_html",
    "build_publication_payload",
]

DZEN_API_BASE_URL: Final[str] = "https://dzen.ru/api/v3/publisher"

MAX_ATTEMPTS: Final[int] = 3
INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
BACKOFF_MULTIPLIER: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 20.0

REQUEST_TIMEOUT_SECONDS: Final[float] = 60.0

#: How long a cached auth state is trusted. Short enough that a revoked token is
#: noticed within a publishing cycle, long enough that a batch of ten articles
#: performs one exchange rather than ten.
AUTH_STATE_TTL_SECONDS: Final[int] = 1800

#: Dzen truncates a headline in its feed past roughly this width.
MAX_DZEN_TITLE_CHARS: Final[int] = 120

#: The tag subset Dzen's editor accepts. A document containing anything else is
#: rejected outright, so unsupported tags are unwrapped rather than sent.
_ALLOWED_TAGS: Final[frozenset[str]] = frozenset(
    {
        "p", "h2", "h3", "ul", "ol", "li", "b", "strong", "i", "em", "a",
        "blockquote", "br", "figure", "img", "figcaption",
    }
)

#: Headings above h3 are collapsed: Dzen renders the article title as h1, so an
#: h1 in the body would produce two competing headings on the page.
_HEADING_REMAP: Final[Mapping[str, str]] = {
    "h1": "h2",
    "h4": "h3",
    "h5": "h3",
    "h6": "h3",
}

_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<(/?)([a-zA-Z0-9]+)([^>]*)>")
_UNSAFE_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"<\s*(script|style|iframe|object|embed|noscript|template|form)\b[^>]*>.*?"
    r"<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_INLINE_ATTR_RE: Final[re.Pattern[str]] = re.compile(
    r"\s(?:on[a-z]+|style)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)


def _transform_promo_cards_to_dzen(body_html: str) -> str:
    """Transform custom web promo cards into native Dzen blockquote callouts.

    Web cards often contain custom inline styles, flex layouts, and unstyled spans
    that break into awkward separate lines when stripped down to basic HTML.
    Converting them to a structured <blockquote> with crisp <p> paragraphs
    ensures Draft.js renders a premium, coherent callout block in Dzen Studio.
    """
    if "svodka-promo-card" not in body_html:
        return body_html

    start_tag = '<div class="svodka-promo-card"'
    idx = body_html.find(start_tag)
    if idx == -1:
        return body_html

    end_idx = -1
    for sep in (
        "</div>\n\n<h2>", "</div>\n<h2>", "</div>\n\n<p>", "</div>\n<p>",
        "</div>\n\n<h3", "</div>\n<h3", "</div>\n\n<ul>", "</div>\n<ul>",
    ):
        pos = body_html.find(sep, idx)
        if pos != -1:
            end_idx = pos
            break

    if end_idx != -1:
        card_content = body_html[idx : end_idx + 6]
        full_match = card_content
    else:
        m = re.search(
            r'<div[^>]*class=["\'][^"\']*svodka-promo-card[^"\']*["\'][^>]*>.*?</div>\s*</div>',
            body_html,
            re.DOTALL | re.IGNORECASE,
        )
        if m:
            full_match = m.group(0)
            card_content = full_match
        else:
            return body_html

    title_m = re.search(
        r"<h[23][^>]*>(.*?)</h[23]>", card_content, re.DOTALL | re.IGNORECASE
    )
    title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else "FreeAPI AI"

    p_m = re.search(r"<p[^>]*>(.*?)</p>", card_content, re.DOTALL | re.IGNORECASE)
    p_text = p_m.group(1).strip() if p_m else ""

    link_m = re.search(
        r'<a\s+[^>]*href=["\']([^"\']+)["\']', card_content, re.IGNORECASE
    )
    link_url = link_m.group(1).strip() if link_m else ""
    link_url = link_url.replace("utm_source=svodkapress", "utm_source=dzen")
    link_url = link_url.replace("utm_medium=site_callout", "utm_medium=article_promo")

    btn_text = "Подключиться к FreeAPI AI (100% бесплатно для резидентов РФ) →"

    dzen_block = (
        "\n<p>▫️ ▫️ ▫️</p>\n"
        "<p><b>🚀 Возможности для авторов | Партнерский материал</b></p>\n"
        f"<h3><b>{title}</b></h3>\n"
        f"<p>{p_text}</p>\n"
        f'<p>👉 <a href="{link_url}"><b>{btn_text}</b></a></p>\n'
        "<p>▫️ ▫️ ▫️</p>\n"
    )
    return body_html.replace(full_match, dzen_block)


def build_dzen_html(body_html: str) -> str:
    """Reduce an article body to the tag subset Dzen accepts.

    Three passes. Executable blocks go first, with their subtrees, because leaving
    a ``<script>`` body behind would put its source text into the published article
    as visible prose. Inline handlers and styles go next. Finally each remaining tag
    is either kept, remapped (``h1`` to ``h2``), or unwrapped with its text intact —
    an unsupported tag must not take its content with it, or the article loses
    paragraphs.
    """
    if not body_html or not body_html.strip():
        return ""
    pre_transformed = _transform_promo_cards_to_dzen(body_html)
    cleaned = _UNSAFE_BLOCK_RE.sub(" ", pre_transformed)
    cleaned = _INLINE_ATTR_RE.sub("", cleaned)

    def rewrite(match: re.Match[str]) -> str:
        closing, tag, attributes = match.group(1), match.group(2).lower(), match.group(3)
        remapped = _HEADING_REMAP.get(tag, tag)
        if remapped not in _ALLOWED_TAGS:
            # Unwrap: drop the tag, keep whatever it contained.
            return ""
        # `href` and `src` are the only attributes worth preserving; everything
        # else is presentational and Dzen restyles the article anyway.
        kept = ""
        for name in ("href", "src", "alt"):
            found = re.search(rf'\b{name}\s*=\s*"([^"]*)"', attributes, re.IGNORECASE)
            if found is None:
                found = re.search(rf"\b{name}\s*=\s*'([^']*)'", attributes, re.IGNORECASE)
            if found is not None:
                kept += f' {name}="{found.group(1)}"'
        return f"<{closing}{remapped}{'' if closing else kept}>"

    return _TAG_RE.sub(rewrite, cleaned).strip()


def build_publication_payload(
    *,
    title: str,
    body_html: str,
    canonical_url: str,
    description: str = "",
    keywords: Sequence[str] = (),
    cover_url: str = "",
) -> dict[str, Any]:
    """Build the publication request body.

    ``canonicalUrl`` is the field that matters most. Dzen hosts a full copy of the
    article, so without a canonical pointer the copy competes with the original in
    search and can outrank it — the site would then be donating its own traffic to
    the aggregator.
    """
    payload: dict[str, Any] = {
        "title": " ".join(title.split())[:MAX_DZEN_TITLE_CHARS],
        "content": build_dzen_html(body_html),
        "canonicalUrl": canonical_url,
        # Dzen requires the format to be declared; an HTML body sent as plain text
        # is published with its tags visible.
        "contentFormat": "html",
    }
    summary = " ".join(description.split())
    if summary:
        payload["description"] = summary[:500]
    tags = [tag.strip() for tag in keywords if tag and tag.strip()]
    if tags:
        payload["tags"] = tags[:10]
    if cover_url:
        payload["coverUrl"] = cover_url
    return payload


@dataclass(slots=True)
class DzenAuthState:
    """A cached Dzen authentication state.

    Cached because the token exchange is the slow part of a publish and a batch
    would otherwise repeat it per article. The expiry is what makes the cache safe:
    a state that has aged out is refreshed rather than used and rejected.
    """

    token: str
    channel_id: str
    expires_at: datetime

    @property
    def is_valid(self) -> bool:
        """Whether this state can still be used.

        A 60-second margin avoids the race where a token passes the check and then
        expires in flight, which would surface as a confusing auth failure on a
        request that was validated a moment earlier.
        """
        return bool(self.token) and datetime.now(UTC) < self.expires_at - timedelta(
            seconds=60
        )


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, so a batch does not retry in lockstep."""
    base = min(
        INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1)),
        MAX_BACKOFF_SECONDS,
    )
    return base * (0.75 + random.random() * 0.5)  # noqa: S311 - jitter, not crypto


class DzenAdapter(BaseSocialAdapter):
    """Publishes an article to a Yandex Dzen channel."""

    platform = Platform.DZEN

    __slots__ = (
        "_auth_state",
        "_base_url",
        "_browser_publisher",
        "_channel_id",
        "_client",
        "_lock",
        "_token",
        "_transport",
    )

    def __init__(
        self,
        *,
        token: str | None = None,
        channel_id: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        browser_publisher: Any | None = None,
    ) -> None:
        self._token = (settings.dzen_token if token is None else token).strip()
        self._channel_id = (
            settings.dzen_channel_id if channel_id is None else channel_id
        ).strip()
        self._base_url = (base_url or DZEN_API_BASE_URL).rstrip("/")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._auth_state: DzenAuthState | None = None
        self._lock = asyncio.Lock()
        self._browser_publisher = browser_publisher

    @property
    def is_configured(self) -> bool:
        """Whether a token and a channel id OR browser publisher are available."""
        if self._browser_publisher is not None and getattr(
            self._browser_publisher, "is_ready", False
        ):
            return True
        return bool(self._token and self._channel_id)

    @property
    def auth_state(self) -> DzenAuthState | None:
        """The cached auth state, exposed for the dashboard and for tests."""
        return self._auth_state

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
        """Close the pooled HTTP client and drop the cached auth state."""
        async with self._lock:
            client = self._client
            self._client = None
            self._auth_state = None
        if client is not None:
            await client.aclose()

    def invalidate_auth(self) -> None:
        """Drop the cached auth state.

        Called when Dzen rejects a request: replaying a dead token on the next
        article would fail identically, so the cache is cleared to force a fresh
        exchange.
        """
        self._auth_state = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return the shared client, validating configuration on first use."""
        if not self.is_configured:
            raise SocialNotConfiguredError(
                "DZEN_TOKEN and DZEN_CHANNEL_ID are required to publish to Dzen"
            )
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                    headers={
                        "Authorization": f"OAuth {self._token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    transport=self._transport,
                )
            return self._client

    async def _ensure_auth(self) -> DzenAuthState:
        """Return a valid auth state, refreshing it when stale.

        The configured token is used directly; the state wrapper exists so an
        expiry is tracked and a batch does not re-validate per article.
        """
        state = self._auth_state
        if state is not None and state.is_valid:
            return state
        if not self.is_configured:
            raise SocialNotConfiguredError(
                "DZEN_TOKEN and DZEN_CHANNEL_ID are required to publish to Dzen"
            )
        refreshed = DzenAuthState(
            token=self._token,
            channel_id=self._channel_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=AUTH_STATE_TTL_SECONDS),
        )
        self._auth_state = refreshed
        logger.debug("Dzen auth state cached for %ds", AUTH_STATE_TTL_SECONDS)
        return refreshed

    def _raise_for_status(self, response: httpx.Response, action: str) -> None:
        """Translate a non-2xx Dzen response into a typed exception."""
        if response.status_code < httpx.codes.BAD_REQUEST:
            return
        detail = response.text[:300].replace("\n", " ").strip() or "no body"
        if response.status_code in (
            httpx.codes.UNAUTHORIZED,
            httpx.codes.FORBIDDEN,
        ):
            # The cached state is dead; the next attempt must re-authenticate.
            self.invalidate_auth()
            raise SocialAuthError(
                f"{action}: Dzen rejected the token (HTTP {response.status_code}). "
                "Confirm DZEN_TOKEN and that the channel grants publishing rights."
            )
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise SocialRateLimitError(f"{action}: Dzen throttled the request")
        raise SocialResponseError(
            f"{action} failed with HTTP {response.status_code}: {detail}",
            retryable=response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR,
        )

    async def _request(
        self, method: str, path: str, *, json_body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Issue one Dzen request, retrying transport failures and rate limits."""
        client = await self._ensure_client()
        last_error: Exception = SocialResponseError(
            f"{path} was never attempted", retryable=True
        )

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.request(
                    method, path, json=None if json_body is None else dict(json_body)
                )
            except httpx.TimeoutException:
                last_error = SocialResponseError(
                    f"{method} {path} timed out after "
                    f"{REQUEST_TIMEOUT_SECONDS:.0f}s",
                    retryable=True,
                )
            except httpx.HTTPError as exc:
                last_error = SocialResponseError(
                    f"{method} {path} transport failure: {type(exc).__name__}",
                    retryable=True,
                )
            else:
                try:
                    self._raise_for_status(response, f"{method} {path}")
                except SocialRateLimitError as exc:
                    last_error = exc
                else:
                    try:
                        payload: Any = response.json()
                    except ValueError as exc:
                        raise SocialResponseError(
                            f"{method} {path} returned a non-JSON body "
                            f"(HTTP {response.status_code})",
                            retryable=True,
                        ) from exc
                    if not isinstance(payload, dict):
                        raise SocialResponseError(
                            f"{method} {path} returned {type(payload).__name__}, "
                            "expected an object"
                        )
                    return payload

            if attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(_backoff_delay(attempt))

        raise last_error

    async def publish(
        self, article: Article, wp_post_url: str, **kwargs: Any
    ) -> PublishResult:
        """Publish the article to the channel.

        If a browser_publisher is configured and ready (using Playwright session
        and isolated ads), publication goes through Dzen Studio web automation.
        Otherwise falls back to the HTTP partner API.
        """
        cover_url = str(kwargs.get("cover_url") or "").strip()
        if not cover_url:
            wp_status = (article.platform_statuses or {}).get("wp", {})
            if isinstance(wp_status, dict) and wp_status.get("cover_url"):
                cover_url = str(wp_status["cover_url"]).strip()

        if self._browser_publisher is not None and getattr(
            self._browser_publisher, "is_ready", False
        ):
            return await self._browser_publisher.publish_article(
                article,
                wp_post_url,
                cover_url=cover_url,
                custom_promo=kwargs.get("custom_promo"),
                publish_now=kwargs.get("publish_now"),
            )

        state = await self._ensure_auth()
        warnings: list[str] = []
        if not cover_url:
            warnings.append("no cover image: Dzen will pick one from the content")

        payload = build_publication_payload(
            title=article.title,
            body_html=article.content_html,
            canonical_url=wp_post_url,
            description=article.seo_description or "",
            keywords=tuple(article.seo_keywords or ()),
            cover_url=cover_url,
        )
        if not payload["content"]:
            raise SocialResponseError(
                "the article body reduced to nothing after Dzen sanitisation",
                retryable=False,
            )
        if not wp_post_url:
            # Publishing a full copy without a canonical pointer would let Dzen
            # outrank the original, so the article is withheld instead.
            raise SocialResponseError(
                "refusing to publish to Dzen without a canonical URL: the copy "
                "would compete with the original in search results",
                retryable=False,
            )

        created = await self._request(
            "POST",
            f"/channel/{state.channel_id}/publication",
            json_body=payload,
        )
        publication_id = str(
            created.get("publicationId") or created.get("id") or ""
        ).strip()
        if not publication_id:
            raise SocialResponseError("Dzen returned no publication id")

        try:
            published = await self._request(
                "POST",
                f"/channel/{state.channel_id}/publication/{publication_id}/publish",
            )
        except SocialAdapterError as exc:
            # The draft exists. Reporting its id lets a retry finish the transition
            # instead of creating a duplicate publication.
            logger.warning(
                "Dzen draft %s was created but not published: %s",
                publication_id,
                exc,
            )
            return PublishResult(
                platform=self.platform_code,
                external_id=publication_id,
                error=f"draft created but not published: {exc}",
                retryable=True,
                warnings=(*warnings, "a retry should publish the existing draft"),
            )

        url = str(published.get("url") or created.get("url") or "")
        return PublishResult.published(
            self.platform_code,
            external_id=publication_id,
            url=url,
            warnings=warnings,
        )
