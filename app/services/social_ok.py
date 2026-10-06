"""OK (Одноклассники) adapter: posts an article via ``mediatopic.post``.

OK has the most intricate request format of the four platforms. Two things make it
unlike the others:

**Every request is signed.** OK requires an ``sig`` parameter computed as
``MD5(sorted_params_joined + secret)``, and the rules have sharp edges that produce
a generic "invalid signature" error when broken:

1. ``access_token`` is **excluded** from the signed parameter set — including it is
   the single most common mistake and the error message does not hint at it.
2. Remaining parameters are sorted **lexicographically by key** and joined as
   ``key=value`` with **no separator** between pairs.
3. The secret depends on the session. With a session token it is
   ``MD5(access_token + application_secret_key)`` lowercased, not the application
   secret directly.
4. The resulting digest is lowercased.

The signature is therefore implemented as one documented function,
:func:`calculate_signature`, and tested against a hand-computed vector rather than
against itself.

**The post body is a JSON array of typed blocks.** ``attachment`` carries a
``media`` list where each entry is a block: ``{"type": "text", ...}``,
``{"type": "link", ...}``, ``{"type": "photo", ...}``. The link block is what
produces a preview card, so it is what carries the click.

**MD5 here is not a security choice.** It is the signature algorithm OK's API
mandates; a different digest is rejected. It is used for request authentication
against a third-party API, never for storing or verifying our own secrets.

The session secret and the access token are never logged and never interpolated
into an exception message.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
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
    build_hashtags,
    build_teaser,
)

if TYPE_CHECKING:
    from app.models.article import Article

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MAX_TOPIC_LENGTH",
    "OK_API_BASE_URL",
    "OKAdapter",
    "build_media_topic",
    "calculate_signature",
    "derive_session_secret",
]

OK_API_BASE_URL: Final[str] = "https://api.ok.ru"

#: ``fb.do`` is the documented entry point that accepts every REST method by name.
_METHOD_PATH: Final[str] = "/fb.do"

MAX_ATTEMPTS: Final[int] = 3
INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
BACKOFF_MULTIPLIER: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 20.0

REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0

#: OK truncates a topic behind a fold past roughly this length.
MAX_TOPIC_LENGTH: Final[int] = 800

#: OK error codes worth distinguishing. The API returns them inside a JSON body
#: with a 200 status in some cases and with a 4xx in others, so both paths are
#: inspected.
_AUTH_ERROR_CODES: Final[frozenset[int]] = frozenset({102, 103, 104, 298})
_RATE_LIMIT_CODES: Final[frozenset[int]] = frozenset({4, 454})


def derive_session_secret(
    *, access_token: str, application_secret_key: str, session_secret_key: str = ""
) -> str:
    """Return the secret used to sign a request.

    Per OK's documentation: when a ``session_secret_key`` is available it is used
    directly; otherwise a session call signs with
    ``MD5(access_token + application_secret_key)`` lowercased. Using the
    application secret directly in a session call is rejected with the same opaque
    "invalid signature" error as a malformed parameter string, which is why the
    derivation is explicit here rather than inlined.
    """
    if session_secret_key:
        return session_secret_key
    digest = hashlib.md5(  # noqa: S324 - OK's API mandates MD5 for request signing
        f"{access_token}{application_secret_key}".encode()
    ).hexdigest()
    return digest.lower()


def calculate_signature(params: Mapping[str, Any], *, secret: str) -> str:
    """Compute the ``sig`` value for an OK API call.

    The algorithm, exactly as documented:

    * ``access_token`` is removed from the signed set. Leaving it in is the most
      common cause of a rejected signature.
    * The remaining pairs are sorted lexicographically **by key**.
    * They are joined as ``key=value`` with no separator between pairs.
    * ``sig = MD5(joined + secret)``, lowercased.

    ``sig`` itself is excluded too, so the function is safe to call with a params
    map that already carries one.
    """
    signable = {
        key: value
        for key, value in params.items()
        if key not in {"access_token", "sig"} and value is not None
    }
    joined = "".join(f"{key}={signable[key]}" for key in sorted(signable))
    digest = hashlib.md5(  # noqa: S324 - OK's API mandates MD5 for request signing
        f"{joined}{secret}".encode()
    ).hexdigest()
    return digest.lower()


def build_media_topic(
    *,
    title: str,
    body_html: str,
    url: str,
    description: str = "",
    keywords: Sequence[str] = (),
    image_urls: Sequence[str] = (),
    limit: int = MAX_TOPIC_LENGTH,
) -> dict[str, Any]:
    """Build the ``attachment`` payload for ``mediatopic.post``.

    The block order is deliberate: text first so the teaser is what a reader sees
    in the feed, then photos, then the link. OK builds its preview card from the
    link block, and placing it last keeps the card beneath the copy rather than
    displacing it.

    A photo block references an **external URL**; OK fetches it itself, which is
    why no upload step is needed for a cover already hosted on the site.
    """
    headline = " ".join(title.split())
    tags = build_hashtags(keywords)
    tag_line = " ".join(tags)

    overhead = len(headline) + len(tag_line) + 4
    teaser = build_teaser(
        title=title,
        body_html=body_html,
        limit=max(limit - overhead, 80),
        description=description,
    )

    text_parts = [headline, "", teaser]
    if tag_line:
        text_parts.extend(["", tag_line])

    media: list[dict[str, Any]] = [
        {"type": "text", "text": "\n".join(text_parts).strip()}
    ]
    for image_url in image_urls:
        if image_url:
            media.append({"type": "photo", "list": [{"existingPhotoId": image_url}]})
    if url:
        media.append({"type": "link", "url": url})

    return {"media": media}


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, so a batch does not retry in lockstep."""
    base = min(
        INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1)),
        MAX_BACKOFF_SECONDS,
    )
    return base * (0.75 + random.random() * 0.5)  # noqa: S311 - jitter, not crypto


class OKAdapter(BaseSocialAdapter):
    """Publishes an article to an OK group feed."""

    platform = Platform.OK

    __slots__ = (
        "_app_key",
        "_base_url",
        "_client",
        "_group_id",
        "_lock",
        "_secret",
        "_token",
        "_transport",
    )

    def __init__(
        self,
        *,
        access_token: str | None = None,
        application_key: str | None = None,
        session_secret_key: str | None = None,
        group_id: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = (
            settings.ok_access_token if access_token is None else access_token
        ).strip()
        self._app_key = (
            settings.ok_application_key if application_key is None else application_key
        ).strip()
        self._secret = (
            settings.ok_session_secret_key
            if session_secret_key is None
            else session_secret_key
        ).strip()
        self._group_id = (
            settings.ok_group_id if group_id is None else group_id
        ).strip()
        self._base_url = (base_url or OK_API_BASE_URL).rstrip("/")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """Whether the token, application key, secret and group are all present."""
        return bool(
            self._token and self._app_key and self._secret and self._group_id
        )

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
                "OK_ACCESS_TOKEN, OK_APPLICATION_KEY, OK_SESSION_SECRET_KEY and "
                "OK_GROUP_ID are all required to publish to OK"
            )
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                    transport=self._transport,
                )
            return self._client

    def _sign(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Return ``params`` with ``application_key``, ``sig`` and the token added.

        The signature is computed *before* the token is attached, because the token
        must not be part of the signed set.
        """
        signable = {
            **{key: value for key, value in params.items() if value is not None},
            "application_key": self._app_key,
            "format": "json",
        }
        secret = derive_session_secret(
            access_token=self._token,
            application_secret_key=self._secret,
            session_secret_key=self._secret,
        )
        return {
            **signable,
            "sig": calculate_signature(signable, secret=secret),
            "access_token": self._token,
        }

    def _raise_for_error(self, payload: Mapping[str, Any], method: str) -> None:
        """Translate an OK error body into a typed exception."""
        code = payload.get("error_code")
        if code is None:
            return
        message = str(payload.get("error_msg") or "unspecified OK error")[:300]
        numeric = code if isinstance(code, int) else None

        if numeric in _RATE_LIMIT_CODES:
            raise SocialRateLimitError(f"{method}: OK rate limit ({code}): {message}")
        if numeric in _AUTH_ERROR_CODES:
            raise SocialAuthError(
                f"{method}: OK rejected the session ({code}): {message}. Re-issue "
                "OK_ACCESS_TOKEN with the VALUABLE_ACCESS and GROUP_CONTENT scopes."
            )
        raise SocialResponseError(
            f"{method}: OK error {code}: {message}", retryable=False
        )

    async def _call(self, method: str, params: Mapping[str, Any]) -> Any:
        """Call one OK REST method, retrying transport failures and rate limits."""
        client = await self._ensure_client()
        body = self._sign({**params, "method": method})
        last_error: Exception = SocialResponseError(
            f"{method} was never attempted", retryable=True
        )

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(_METHOD_PATH, data=body)
            except httpx.TimeoutException:
                last_error = SocialResponseError(
                    f"{method} timed out after {REQUEST_TIMEOUT_SECONDS:.0f}s",
                    retryable=True,
                )
            except httpx.HTTPError as exc:
                last_error = SocialResponseError(
                    f"{method} transport failure: {type(exc).__name__}",
                    retryable=True,
                )
            else:
                try:
                    payload: Any = response.json()
                except ValueError as exc:
                    raise SocialResponseError(
                        f"{method} returned a non-JSON body "
                        f"(HTTP {response.status_code})",
                        retryable=True,
                    ) from exc
                if isinstance(payload, Mapping):
                    try:
                        # OK reports some failures with a 200 status and an error
                        # body, so the payload is inspected before the status.
                        self._raise_for_error(payload, method)
                    except SocialRateLimitError as exc:
                        last_error = exc
                    else:
                        if response.status_code < httpx.codes.BAD_REQUEST:
                            return payload
                        raise SocialResponseError(
                            f"{method} failed with HTTP {response.status_code}",
                            retryable=response.status_code >= 500,
                        )
                elif response.status_code < httpx.codes.BAD_REQUEST:
                    # `mediatopic.post` answers with a bare quoted topic id.
                    return payload
                else:
                    raise SocialResponseError(
                        f"{method} failed with HTTP {response.status_code}",
                        retryable=response.status_code >= 500,
                    )

            if attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(_backoff_delay(attempt))

        raise last_error

    async def publish(
        self, article: Article, wp_post_url: str, **kwargs: Any
    ) -> PublishResult:
        """Post the article to the group feed as a media topic.

        ``image_urls`` may be passed for articles with a hosted cover; OK fetches
        the URL itself, so no upload step is needed.
        """
        image_urls = tuple(kwargs.get("image_urls") or ())
        warnings: list[str] = []
        if not image_urls:
            warnings.append("no cover image: the topic carries text and a link only")

        attachment = build_media_topic(
            title=article.title,
            body_html=article.content_html,
            url=wp_post_url,
            description=article.seo_description or "",
            keywords=tuple(article.seo_keywords or ()),
            image_urls=image_urls,
        )

        payload = await self._call(
            "mediatopic.post",
            {
                "gid": self._group_id,
                "type": "GROUP_THEME",
                # OK expects the attachment as a JSON *string*, not as nested form
                # fields; separators are compacted because the value is signed and
                # any whitespace change alters the digest.
                "attachment": json.dumps(
                    attachment, ensure_ascii=False, separators=(",", ":")
                ),
            },
        )

        topic_id = self._extract_topic_id(payload)
        if not topic_id:
            raise SocialResponseError("mediatopic.post returned no topic id")

        return PublishResult.published(
            self.platform_code,
            external_id=topic_id,
            url=f"https://ok.ru/group/{self._group_id}/topic/{topic_id}",
            warnings=warnings,
        )

    def _extract_topic_id(self, payload: Any) -> str:
        """Pull the topic id out of an OK response.

        ``mediatopic.post`` answers with a bare quoted id rather than an object, but
        some gateway configurations wrap it, so both shapes are accepted.
        """
        if isinstance(payload, str):
            return payload.strip().strip('"')
        if isinstance(payload, int):
            return str(payload)
        if isinstance(payload, Mapping):
            for key in ("id", "topic_id", "result"):
                value = payload.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip().strip('"')
        return ""
