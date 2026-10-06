# ruff: noqa: RUF001, PLR0912, PLR0915
"""VK Video service: hosts article video on VK and returns an embeddable player.

Why VK rather than self-hosting: a news site on a 4 GB VPS cannot serve video.
Transcoding, bandwidth and storage all sit outside what this deployment can
absorb, while VK accepts the upload, transcodes it and serves an iframe player
for free. The article stores only the embed coordinates.

The VK upload is a three-step protocol, and each step fails differently:

1. ``video.save`` reserves a slot and returns a one-shot ``upload_url`` together
   with the ``owner_id`` / ``video_id`` / ``access_key`` triple that identifies
   the finished video.
2. The bytes are POSTed as multipart to that URL — a different host, without the
   API token, because the URL itself carries the authorisation.
3. The player markup is built from the triple returned in step 1.

Two properties are worth stating because both are easy to get wrong:

**VK reports errors with HTTP 200.** A failed API call returns a JSON body with
an ``error`` object and a normal status code, so a client that only checks
``response.status_code`` treats an expired token as success and then uploads to a
URL that does not exist. Every response is therefore inspected for ``error``
before its ``response`` payload is read, and the numeric ``error_code`` is mapped
onto typed exceptions so callers can distinguish "retry later" (rate limit) from
"stop and alert an operator" (token revoked).

**The upload URL is single-use and time-limited.** It cannot be persisted and
retried later: a retry has to start again from ``video.save``. The retry policy
here reflects that — transport failures during the upload are retried against the
*same* URL immediately, but an expired URL surfaces as a
:class:`VKUploadError` for the caller to restart, rather than being silently
re-reserved and billed twice against the group's video quota.

The access token is never logged and never interpolated into an exception message.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Final, Self

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import get_secret_box
from app.models.setting import SystemSetting

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_EMBED_HEIGHT",
    "DEFAULT_EMBED_WIDTH",
    "MAX_ATTEMPTS",
    "MAX_VIDEO_BYTES",
    "VK_API_BASE_URL",
    "VK_VIDEO_CONFIG_KEY",
    "SavedVideo",
    "UploadedVideo",
    "VKAuthError",
    "VKError",
    "VKPermissionError",
    "VKRateLimitError",
    "VKResponseError",
    "VKUploadError",
    "VKVideoConfig",
    "VKVideoService",
    "build_video_embed",
    "close_vk_video_service",
    "get_vk_video_service",
    "load_vk_video_config",
    "normalise_group_id",
    "normalize_vk_storage_state",
    "probe_vk_browser_session",
    "raise_for_vk_error",
    "save_vk_video_config",
    "upload_article_video",
]

VK_API_BASE_URL: Final[str] = "https://api.vk.com/method"

#: One initial attempt plus three retries, matching the other API clients.
MAX_ATTEMPTS: Final[int] = 4
INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
BACKOFF_MULTIPLIER: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 30.0

#: Ceiling on an accepted upload. VK's own limit is far higher, but a news clip
#: past this size is a misconfiguration, and buffering it would exhaust the
#: target VPS's memory before VK ever rejected it.
MAX_VIDEO_BYTES: Final[int] = 256 * 1024 * 1024

#: A video upload is a long transfer, so it gets its own generous timeout rather
#: than the API-call timeout: `AI_REQUEST_TIMEOUT` is sized for a JSON round trip.
UPLOAD_TIMEOUT_SECONDS: Final[float] = 600.0

#: Default player box. 16:9 at a width the WordPress content column can hold; the
#: markup is responsive regardless, so these are the intrinsic ratio hints rather
#: than fixed pixels.
DEFAULT_EMBED_WIDTH: Final[int] = 853
DEFAULT_EMBED_HEIGHT: Final[int] = 480

#: VK error codes this client understands. Anything else becomes a generic
#: :class:`VKResponseError` carrying the code, so an unmapped failure is still
#: legible in the logs.
VK_ERROR_TOO_MANY_REQUESTS: Final[int] = 6
VK_ERROR_RATE_LIMIT_REACHED: Final[int] = 29
VK_ERROR_AUTH_FAILED: Final[int] = 5
VK_ERROR_USER_AUTH_FAILED: Final[int] = 27
VK_ERROR_ACCESS_DENIED: Final[int] = 15
VK_ERROR_PERMISSION_DENIED: Final[int] = 7
VK_ERROR_CAPTCHA_NEEDED: Final[int] = 14
VK_ERROR_VALIDATION_REQUIRED: Final[int] = 17

_RATE_LIMIT_CODES: Final[frozenset[int]] = frozenset(
    {VK_ERROR_TOO_MANY_REQUESTS, VK_ERROR_RATE_LIMIT_REACHED}
)
_AUTH_CODES: Final[frozenset[int]] = frozenset(
    {
        VK_ERROR_AUTH_FAILED,
        VK_ERROR_USER_AUTH_FAILED,
        VK_ERROR_CAPTCHA_NEEDED,
        VK_ERROR_VALIDATION_REQUIRED,
    }
)
_PERMISSION_CODES: Final[frozenset[int]] = frozenset(
    {VK_ERROR_ACCESS_DENIED, VK_ERROR_PERMISSION_DENIED}
)

#: VK truncates a video title past this; trimming here keeps the stored metadata
#: identical to what VK will actually show.
MAX_TITLE_CHARS: Final[int] = 128
MAX_DESCRIPTION_CHARS: Final[int] = 5000


class VKError(RuntimeError):
    """Base class for every failure surfaced by this module."""


class VKNotConfiguredError(VKError):
    """``VK_ACCESS_TOKEN`` is unset, so video hosting is unavailable.

    Raised rather than degraded: an article whose video silently vanished would
    publish with a dangling reference, which is worse than not publishing.
    """


class VKAuthError(VKError):
    """The token was rejected, expired, or VK demands a captcha.

    Never retried: every attempt would fail identically, and a captcha or
    validation request needs a human.
    """


class VKPermissionError(VKError):
    """The token lacks the ``video`` scope, or cannot post to that group."""


class VKRateLimitError(VKError):
    """Rate limit or quota exhausted after the retry budget."""


class VKResponseError(VKError):
    """VK answered with an error object or an unusable payload."""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class VKUploadError(VKError):
    """The multipart upload itself failed.

    Distinct from :class:`VKResponseError` because the remedy differs: the
    reserved ``upload_url`` is single-use, so recovering means starting again from
    ``video.save`` rather than re-posting to the same URL.
    """


@dataclass(frozen=True, slots=True)
class SavedVideo:
    """The slot reserved by ``video.save``.

    ``upload_url`` is single-use and time-limited; the other three fields are the
    permanent coordinates of the video and are what the article stores.
    """

    upload_url: str
    owner_id: int
    video_id: int
    access_key: str = ""
    title: str = ""

    @property
    def embed_url(self) -> str:
        """The ``video_ext.php`` player URL for this video."""
        return build_embed_url(
            owner_id=self.owner_id,
            video_id=self.video_id,
            access_key=self.access_key,
        )


@dataclass(frozen=True, slots=True)
class UploadedVideo:
    """A completed upload, shaped for ``articles.video_links``."""

    owner_id: int
    video_id: int
    access_key: str
    embed_url: str
    embed_html: str
    title: str
    size_bytes: int

    def to_link(self) -> dict[str, Any]:
        """Render the JSONB entry persisted in ``articles.video_links``."""
        return {
            "platform": "vk",
            "owner_id": self.owner_id,
            "video_id": self.video_id,
            "access_key": self.access_key,
            "embed_url": self.embed_url,
            "title": self.title,
            "size_bytes": self.size_bytes,
        }


def build_embed_url(
    *, owner_id: int, video_id: int, access_key: str = ""
) -> str:
    """Build the VK external-player URL for one video.

    ``video_ext.php`` is the documented embed endpoint. ``hash`` carries the
    ``access_key`` and is **required for a private or unlisted video** — without
    it the player renders an access error for everyone but the uploader, so it is
    included whenever VK returned one.
    """
    url = f"https://vk.com/video_ext.php?oid={owner_id}&id={video_id}"
    if access_key:
        url = f"{url}&hash={access_key}"
    return f"{url}&hd=2&js_api=1"


def build_video_embed(
    *,
    owner_id: int,
    video_id: int,
    access_key: str = "",
    title: str = "",
    width: int = DEFAULT_EMBED_WIDTH,
    height: int = DEFAULT_EMBED_HEIGHT,
) -> str:
    """Build responsive iframe markup for the Jinja2 article template.

    The player is wrapped in a padding-ratio box rather than given fixed pixel
    dimensions: WordPress content columns are fluid and a fixed-width iframe
    overflows on a phone. The intrinsic ratio is derived from ``width``/``height``
    so a vertical clip is not letterboxed into a landscape frame.

    ``title`` is HTML-escaped: it reaches here from a source post, so an
    unescaped quote would break out of the attribute.
    """
    safe_width = max(int(width), 1)
    safe_height = max(int(height), 1)
    ratio = safe_height / safe_width * 100
    src = html.escape(
        build_embed_url(
            owner_id=owner_id, video_id=video_id, access_key=access_key
        ),
        quote=True,
    )
    label = html.escape(title or "Видео", quote=True)
    return (
        '<figure class="svodka-video">'
        f'<div class="svodka-video__frame" style="position:relative;'
        f'padding-bottom:{ratio:.4f}%;height:0;overflow:hidden;">'
        f'<iframe src="{src}" title="{label}" '
        f'width="{safe_width}" height="{safe_height}" '
        'style="position:absolute;top:0;left:0;width:100%;height:100%;border:0;" '
        'loading="lazy" allow="autoplay; encrypted-media; fullscreen; '
        'picture-in-picture" allowfullscreen frameborder="0" '
        'scrolling="no"></iframe>'
        "</div>"
        "</figure>"
    )


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter.

    Jitter matters when a batch of articles is processed together: without it
    every retry from the same tick lands on the same instant and re-triggers the
    limit that caused it.
    """
    base = min(
        INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1)),
        MAX_BACKOFF_SECONDS,
    )
    return base * (0.75 + random.random() * 0.5)  # noqa: S311 - jitter, not crypto


def raise_for_vk_error(payload: Mapping[str, Any], method: str) -> None:
    """Translate a VK ``error`` object into a typed exception.

    VK returns HTTP 200 for application errors, so this is the only place a
    failure is detected. The message is truncated and the token never appears in
    it: VK echoes request parameters in some error bodies.

    Public because :mod:`app.services.social_vk` talks to the same API: a second
    copy of the error-code table would drift from this one, and the two services
    would then disagree about whether a given failure is retryable.
    """
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return
    raw_code = error.get("error_code")
    code = raw_code if isinstance(raw_code, int) else None
    message = str(error.get("error_msg") or "unspecified VK error")[:300]

    if code in _RATE_LIMIT_CODES:
        raise VKRateLimitError(f"{method}: VK rate limit ({code}): {message}")
    if code in _AUTH_CODES:
        raise VKAuthError(
            f"{method}: VK rejected the access token ({code}): {message}. "
            "Re-issue VK_ACCESS_TOKEN with the `video` scope."
        )
    if code in _PERMISSION_CODES:
        raise VKPermissionError(
            f"{method}: VK denied access ({code}): {message}. The token needs the "
            "`video` scope and admin rights on the target group."
        )
    raise VKResponseError(f"{method}: VK error {code}: {message}", error_code=code)


def normalise_group_id(group_id: int | str | None) -> int | None:
    """Resolve a group id to a positive integer, or ``None`` for a user wall.

    VK identifies groups with negative owner ids but expects ``group_id`` to be
    the positive form, and ``VK_GROUP_ID`` is configured either way in practice.
    Normalising here means a leading ``-`` in the environment is not a silent
    upload to the wrong wall.

    Public because the wall adapter needs the identical rule: ``wall.post`` takes a
    *negative* ``owner_id``, so both services must agree on the canonical form
    before flipping the sign, or a post lands on the wrong wall.
    """
    if group_id is None or group_id == "":
        return None
    try:
        value = int(group_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"VK group id must be numeric, got {group_id!r}") from exc
    return abs(value) or None


class VKVideoService:
    """Async VK API client for the three-step video upload.

    Two HTTP clients are used deliberately: the API client carries the access
    token and a JSON-sized timeout, while uploads go through a separate client
    with a long transfer timeout and **no** ``Authorization`` header, because the
    upload URL is pre-authorised and sending the token to a third-party upload
    host would leak it.
    """

    __slots__ = (
        "_access_token",
        "_api_client",
        "_api_version",
        "_base_url",
        "_group_id",
        "_lock",
        "_timeout",
        "_transport",
        "_upload_client",
    )

    def __init__(
        self,
        *,
        access_token: str | None = None,
        group_id: int | str | None = None,
        api_version: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        raw_token = settings.vk_access_token if access_token is None else access_token
        self._access_token = raw_token.strip()
        self._group_id = normalise_group_id(
            settings.vk_group_id if group_id is None else group_id
        )
        self._api_version = (api_version or settings.vk_api_version).strip()
        self._base_url = (base_url or VK_API_BASE_URL).rstrip("/")
        raw_timeout = settings.ai_request_timeout if timeout is None else timeout
        self._timeout = float(raw_timeout)
        self._transport = transport
        self._api_client: httpx.AsyncClient | None = None
        self._upload_client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """Whether a token is available. Constructing the service never raises."""
        return bool(self._access_token)

    @property
    def group_id(self) -> int | None:
        """The configured target group, or ``None`` for a user wall."""
        return self._group_id

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
        """Close both pooled HTTP clients, if they were created."""
        async with self._lock:
            api, upload = self._api_client, self._upload_client
            self._api_client = self._upload_client = None
        for client in (api, upload):
            if client is not None:
                await client.aclose()

    async def _ensure_api_client(self) -> httpx.AsyncClient:
        """Return the API client, creating it on first use."""
        if not self._access_token:
            raise VKNotConfiguredError(
                "VK_ACCESS_TOKEN is not set: video hosting is unavailable. Issue a "
                "token with the `video` scope to enable it."
            )
        async with self._lock:
            if self._api_client is None:
                self._api_client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(self._timeout),
                    transport=self._transport,
                )
            return self._api_client

    async def _ensure_upload_client(self) -> httpx.AsyncClient:
        """Return the upload client: long timeout, no credentials."""
        async with self._lock:
            if self._upload_client is None:
                self._upload_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(UPLOAD_TIMEOUT_SECONDS),
                    transport=self._transport,
                )
            return self._upload_client

    def _decode(self, response: httpx.Response, method: str) -> dict[str, Any]:
        """Decode a VK JSON body and raise on an embedded error object."""
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise VKResponseError(
                f"{method} returned a non-JSON body "
                f"(HTTP {response.status_code}, {len(response.content)} bytes)"
            ) from exc
        if not isinstance(payload, dict):
            raise VKResponseError(
                f"{method} returned {type(payload).__name__}, expected an object"
            )
        raise_for_vk_error(payload, method)
        return payload

    async def _call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Call one VK API method, retrying transport failures and rate limits.

        The token is sent as a form field rather than a query parameter so it does
        not land in an intermediary's access log.
        """
        client = await self._ensure_api_client()
        body = {
            **{key: value for key, value in params.items() if value is not None},
            "access_token": self._access_token,
            "v": self._api_version,
        }
        last_error: VKError = VKResponseError(f"{method} was never attempted")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(f"/{method}", data=body)
            except httpx.TimeoutException:
                last_error = VKResponseError(
                    f"{method} timed out after {self._timeout:.0f}s"
                )
            except httpx.HTTPError as exc:
                last_error = VKResponseError(
                    f"{method} transport failure: {type(exc).__name__}"
                )
            else:
                if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
                    last_error = VKResponseError(
                        f"{method} failed with HTTP {response.status_code}"
                    )
                else:
                    try:
                        return self._decode(response, method)
                    except VKRateLimitError as exc:
                        # Retryable: the quota window may reopen.
                        last_error = exc
                    except VKError:
                        # Auth, permission and payload errors are deterministic.
                        raise

            if attempt == MAX_ATTEMPTS:
                break
            delay = _backoff_delay(attempt)
            logger.warning(
                "VK %s attempt %d/%d failed (%s); retrying in %.1fs",
                method,
                attempt,
                MAX_ATTEMPTS,
                last_error,
                delay,
            )
            await asyncio.sleep(delay)

        raise last_error

    async def video_save(
        self,
        name: str,
        description: str,
        group_id: int | None = None,
        *,
        is_private: bool = False,
        wallpost: bool = False,
    ) -> dict[str, Any]:
        """Reserve a video slot and return VK's raw ``response`` payload.

        Returns the dict VK sent, containing ``upload_url``, ``owner_id``,
        ``video_id`` and usually ``access_key``. Use :meth:`save_video` for the
        parsed form; this method exists because the raw payload is what the
        dashboard shows when an operator debugs a failed upload.

        ``wallpost`` is ``False`` deliberately: this pipeline publishes to the VK
        wall through its own adapter in Package 4, and letting ``video.save``
        post as a side effect would produce a duplicate, unformatted wall entry.
        """
        target_group = self._group_id if group_id is None else normalise_group_id(
            group_id
        )
        params: dict[str, Any] = {
            "name": name.strip()[:MAX_TITLE_CHARS] or "Видео",
            "description": description.strip()[:MAX_DESCRIPTION_CHARS],
            "is_private": int(bool(is_private)),
            "wallpost": int(bool(wallpost)),
        }
        if target_group is not None:
            params["group_id"] = target_group

        payload = await self._call("video.save", params)
        response = payload.get("response")
        if not isinstance(response, Mapping):
            raise VKResponseError(
                "video.save returned no response object; VK sent "
                f"{sorted(payload)[:6]}"
            )
        return dict(response)

    async def save_video(
        self,
        name: str,
        description: str,
        group_id: int | None = None,
        *,
        is_private: bool = False,
    ) -> SavedVideo:
        """Reserve a slot and return it parsed and validated."""
        response = await self.video_save(
            name, description, group_id, is_private=is_private
        )
        upload_url = str(response.get("upload_url") or "")
        if not upload_url:
            raise VKResponseError("video.save returned no upload_url")

        raw_owner = response.get("owner_id")
        raw_video = response.get("video_id") or response.get("vid")
        if not isinstance(raw_owner, int) or not isinstance(raw_video, int):
            raise VKResponseError(
                "video.save returned no usable owner_id/video_id pair; the embed "
                "cannot be addressed without both"
            )
        return SavedVideo(
            upload_url=upload_url,
            owner_id=raw_owner,
            video_id=raw_video,
            access_key=str(response.get("access_key") or ""),
            title=str(response.get("title") or name),
        )

    async def upload_video_file(
        self, upload_url: str, video_bytes: bytes, filename: str
    ) -> dict[str, Any]:
        """POST the video bytes to a reserved upload URL.

        The URL is single-use, so a transport failure is retried against the same
        URL but an HTTP rejection is not: a 4xx here means the URL has expired or
        was already consumed, and the only recovery is a fresh ``video.save``.
        Retrying a consumed URL would burn the retry budget for nothing.
        """
        if not video_bytes:
            raise VKUploadError("refusing to upload an empty video payload")
        if len(video_bytes) > MAX_VIDEO_BYTES:
            raise VKUploadError(
                f"video is {len(video_bytes)} bytes, above the "
                f"{MAX_VIDEO_BYTES}-byte ceiling"
            )
        if not upload_url:
            raise VKUploadError("no upload URL was supplied")

        client = await self._ensure_upload_client()
        files = {"video_file": (filename or "video.mp4", video_bytes, "video/mp4")}
        last_error: VKError = VKUploadError("upload was never attempted")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(upload_url, files=files)
            except httpx.TimeoutException:
                last_error = VKUploadError(
                    f"video upload timed out after {UPLOAD_TIMEOUT_SECONDS:.0f}s"
                )
            except httpx.HTTPError as exc:
                last_error = VKUploadError(
                    f"video upload transport failure: {type(exc).__name__}"
                )
            else:
                if response.status_code < httpx.codes.BAD_REQUEST:
                    return self._decode(response, "video upload")
                raise VKUploadError(
                    f"VK rejected the upload with HTTP {response.status_code}; the "
                    "upload URL is single-use and must be re-reserved with "
                    "video.save"
                )

            if attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(_backoff_delay(attempt))

        raise last_error

    async def upload(
        self,
        video_bytes: bytes,
        *,
        name: str,
        description: str = "",
        filename: str = "video.mp4",
        group_id: int | None = None,
    ) -> UploadedVideo:
        """Run the full reserve-upload-embed sequence for one video."""
        saved = await self.save_video(name, description, group_id)
        logger.info(
            "Reserved a VK video slot (owner=%d, video=%d) for %d bytes",
            saved.owner_id,
            saved.video_id,
            len(video_bytes),
        )
        await self.upload_video_file(saved.upload_url, video_bytes, filename)
        embed_html = build_video_embed(
            owner_id=saved.owner_id,
            video_id=saved.video_id,
            access_key=saved.access_key,
            title=saved.title,
        )
        logger.info(
            "Uploaded a VK video (owner=%d, video=%d)", saved.owner_id, saved.video_id
        )
        return UploadedVideo(
            owner_id=saved.owner_id,
            video_id=saved.video_id,
            access_key=saved.access_key,
            embed_url=saved.embed_url,
            embed_html=embed_html,
            title=saved.title,
            size_bytes=len(video_bytes),
        )

    async def test_connection(self) -> dict[str, Any]:
        """Test the VK credentials by probing groups.getById or users.get."""
        params: dict[str, Any] = {}
        if self._group_id:
            params["group_id"] = str(self._group_id)
            payload = await self._call("groups.getById", params)
            resp = payload.get("response")
            group_name = ""
            if isinstance(resp, list) and resp:
                group_name = str(resp[0].get("name", ""))
            elif isinstance(resp, dict) and "groups" in resp:
                groups = resp.get("groups", [])
                if groups:
                    group_name = str(groups[0].get("name", ""))
            return {
                "ok": True,
                "type": "group",
                "group_id": self._group_id,
                "name": group_name,
            }
        payload = await self._call("users.get", params)
        resp = payload.get("response")
        user_name = ""
        if isinstance(resp, list) and resp:
            user = resp[0]
            first = str(user.get("first_name", "") or "")
            last = str(user.get("last_name", "") or "")
            user_name = f"{first} {last}".strip()
        return {"ok": True, "type": "user", "name": user_name}


_shared_service: VKVideoService | None = None


def get_vk_video_service() -> VKVideoService:
    """Return the process-wide service, creating it on first call."""
    global _shared_service  # noqa: PLW0603 - module-level singleton accessor
    if _shared_service is None:
        _shared_service = VKVideoService()
    return _shared_service


async def close_vk_video_service() -> None:
    """Release the shared service's connections. Call on shutdown."""
    global _shared_service  # noqa: PLW0603 - module-level singleton accessor
    service = _shared_service
    _shared_service = None
    if service is not None:
        await service.close()


async def upload_article_video(
    video_bytes: bytes,
    *,
    name: str,
    description: str = "",
    filename: str = "video.mp4",
    group_id: int | None = None,
    service: VKVideoService | None = None,
    session: AsyncSession | None = None,
) -> UploadedVideo:
    """Upload one article video to VK and return its embed coordinates.

    If a browser session (``storage_state``) is configured and no ``access_token``
    is present (or the cached web token expired), automatically extracts a fresh
    web ``access_token`` via Playwright before uploading.
    """
    cfg: VKVideoConfig | None = None
    if service is not None:
        engine = service
    elif session is not None:
        cfg = await load_vk_video_config(session)
        if not cfg.has_token and cfg.has_session:
            await probe_vk_browser_session(cfg, session=session)
        target_group = group_id if group_id is not None else cfg.group_id
        engine = VKVideoService(
            access_token=cfg.access_token or settings.vk_access_token,
            group_id=target_group,
        )
    else:
        engine = get_vk_video_service()

    try:
        return await engine.upload(
            video_bytes,
            name=name,
            description=description,
            filename=filename,
            group_id=group_id,
        )
    except VKAuthError:
        if cfg is not None and cfg.has_session and session is not None:
            logger.info("VK web token expired; refreshing from saved browser session")
            probe = await probe_vk_browser_session(cfg, session=session)
            if probe.get("is_valid") and cfg.has_token:
                target_group = group_id if group_id is not None else cfg.group_id
                refreshed_engine = VKVideoService(
                    access_token=cfg.access_token,
                    group_id=target_group,
                )
                return await refreshed_engine.upload(
                    video_bytes,
                    name=name,
                    description=description,
                    filename=filename,
                    group_id=group_id,
                )
        raise


VK_VIDEO_CONFIG_KEY: Final[str] = "vk_video_config"


def _encrypt_val(raw: str) -> str:
    if not raw:
        return ""
    try:
        box = get_secret_box()
        return box.encrypt(raw.strip())
    except (ValueError, RuntimeError, TypeError):
        return raw.strip()


def _decrypt_val(val: str) -> str:
    if not val:
        return ""
    try:
        box = get_secret_box()
        return box.decrypt(val.strip())
    except (ValueError, RuntimeError, TypeError):
        return val.strip()


def normalize_vk_storage_state(
    raw_data: dict[str, Any] | list[Any],
) -> dict[str, Any]:
    """Normalize raw browser cookies or Playwright storage_state for VK.com."""
    cookies_raw = (
        raw_data
        if isinstance(raw_data, list)
        else (raw_data.get("cookies", []) if isinstance(raw_data, dict) else [])
    )
    origins_raw = raw_data.get("origins", []) if isinstance(raw_data, dict) else []

    samesite_map = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
        "unspecified": "Lax",
    }

    clean_cookies: list[dict[str, Any]] = []
    for c in cookies_raw:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).strip()
        value = str(c.get("value", ""))
        domain = str(c.get("domain", ".vk.com")).strip()
        if not name or name == "hitw429":
            continue

        cookie_entry: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(c.get("path", "/")),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
        }

        raw_ss = str(c.get("sameSite", "Lax")).lower()
        cookie_entry["sameSite"] = samesite_map.get(raw_ss, "Lax")

        exp = c.get("expires", c.get("expirationDate"))
        if isinstance(exp, (int, float)) and exp > 0:
            cookie_entry["expires"] = float(exp)

        clean_cookies.append(cookie_entry)

    return {"cookies": clean_cookies, "origins": origins_raw}


@dataclass(slots=True)
class VKVideoConfig:
    """Configuration for VK Video hosting and community integration."""

    access_token: str = ""
    group_id: str = "236626422"
    storage_state: dict[str, Any] | None = None
    is_active: bool = True
    status: str = "not_configured"
    error_message: str | None = None
    group_name: str = ""
    last_checked_at: str | None = None

    @property
    def has_token(self) -> bool:
        return bool(self.access_token and self.access_token.strip())

    @property
    def has_session(self) -> bool:
        if not self.storage_state or not isinstance(self.storage_state, dict):
            return False
        cookies = self.storage_state.get("cookies")
        return bool(isinstance(cookies, list) and len(cookies) > 0)

    @property
    def is_ready(self) -> bool:
        return bool(self.is_active and (self.has_token or self.has_session))

    def to_public_dict(self) -> dict[str, Any]:
        has_tok = self.has_token
        tok = self.access_token.strip()
        masked = (
            f"{'•' * 8}{tok[-4:]}"
            if len(tok) > 8
            else ("•" * 8 if has_tok else "")
        )
        cookies_list = (
            self.storage_state.get("cookies", [])
            if isinstance(self.storage_state, dict)
            else []
        )
        return {
            "has_token": has_tok,
            "has_session": self.has_session,
            "cookies_count": len(cookies_list) if isinstance(cookies_list, list) else 0,
            "masked_token": masked,
            "group_id": self.group_id or "236626422",
            "is_active": self.is_active,
            "status": self.status,
            "error_message": self.error_message,
            "group_name": self.group_name,
            "last_checked_at": self.last_checked_at,
        }

    def to_db_dict(self) -> dict[str, Any]:
        raw_state = json.dumps(self.storage_state) if self.storage_state else ""
        return {
            "encrypted_token": _encrypt_val(self.access_token),
            "encrypted_storage_state": _encrypt_val(raw_state),
            "group_id": self.group_id,
            "is_active": self.is_active,
            "status": self.status,
            "error_message": self.error_message,
            "group_name": self.group_name,
            "last_checked_at": self.last_checked_at,
        }

    @classmethod
    def from_db_dict(cls, data: dict[str, Any]) -> VKVideoConfig:
        enc_token = str(data.get("encrypted_token", "") or "")
        token = (
            _decrypt_val(enc_token)
            if enc_token
            else str(data.get("access_token", "") or "")
        )
        enc_state = str(data.get("encrypted_storage_state", "") or "")
        dec_state = _decrypt_val(enc_state)
        storage_state: dict[str, Any] | None = None
        if dec_state:
            try:
                parsed = json.loads(dec_state)
                if isinstance(parsed, (dict, list)):
                    storage_state = normalize_vk_storage_state(parsed)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.warning("Failed to parse decrypted VK storage_state: %s", exc)

        return cls(
            access_token=token or settings.vk_access_token,
            group_id=str(data.get("group_id") or settings.vk_group_id or "236626422"),
            storage_state=storage_state,
            is_active=bool(data.get("is_active", True)),
            status=str(data.get("status", "not_configured")),
            error_message=data.get("error_message"),
            group_name=str(data.get("group_name", "") or ""),
            last_checked_at=data.get("last_checked_at"),
        )


async def load_vk_video_config(session: AsyncSession) -> VKVideoConfig:
    """Load VK video config from system_settings with fallback to .env."""
    try:
        stmt = select(SystemSetting.value).where(
            SystemSetting.key == VK_VIDEO_CONFIG_KEY
        )
        result = await session.execute(stmt)
        raw = result.scalar_one_or_none()
        if isinstance(raw, dict):
            cfg = VKVideoConfig.from_db_dict(raw)
            if not cfg.access_token and settings.vk_access_token:
                cfg.access_token = settings.vk_access_token
            return cfg
    except SQLAlchemyError as exc:
        logger.error("Failed to load VK video config from DB: %s", exc)
    return VKVideoConfig(
        access_token=settings.vk_access_token,
        group_id=str(settings.vk_group_id or "236626422"),
        status="active" if settings.vk_access_token else "not_configured",
    )


async def save_vk_video_config(session: AsyncSession, config: VKVideoConfig) -> None:
    """Save VK video config to system_settings."""
    doc = config.to_db_dict()
    stmt = (
        pg_insert(SystemSetting)
        .values(
            key=VK_VIDEO_CONFIG_KEY,
            value=doc,
            description="VK Video API credentials, browser session and group settings",
        )
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": doc, "updated_at": datetime.now(UTC)},
        )
    )
    await session.execute(stmt)
    await session.commit()


_VK_WEB_EXTRACT_JS: Final[str] = """async () => {
    const endpoints = [
        "https://login.vk.ru/?act=web_token&version=1&app_id=6287487",
        "https://login.vk.com/?act=web_token&version=1&app_id=6287487"
    ];
    for (const u of endpoints) {
        try {
            const resp = await fetch(u, {
                method: "POST",
                credentials: "include",
                headers: {"Content-Type": "application/x-www-form-urlencoded"},
                body: "version=1&app_id=6287487"
            });
            const text = await resp.text();
            const m = text.match(/vk1\\.a\\.[A-Za-z0-9_\\-]{20,}/);
            if (m) return m[0];
        } catch (e) {}
    }
    try {
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            const v = localStorage.getItem(k) || "";
            const m = v.match(/vk1\\.a\\.[A-Za-z0-9_\\-]{20,}/);
            if (m) return m[0];
        }
    } catch (e) {}
    return "";
}"""


async def _extract_vk_token_in_page(page: Any, captured_tokens: list[str]) -> None:
    """Attempt to mint or read a vk1.a... web token inside an active VK page."""
    if captured_tokens:
        return
    try:
        js_token = await page.evaluate(_VK_WEB_EXTRACT_JS)
        if isinstance(js_token, str) and js_token.startswith("vk1.a."):
            captured_tokens.append(js_token)
    except Exception as eval_exc:  # noqa: BLE001
        logger.debug("VK in-page token extraction skipped: %s", eval_exc)


async def _verify_vk_group_api(
    config: VKVideoConfig, target_group_id: str
) -> str:
    """Verify API connection with the extracted token and update group_name."""
    if not config.has_token:
        return ""
    try:
        svc = VKVideoService(
            access_token=config.access_token,
            group_id=target_group_id,
        )
        res = await svc.test_connection()
        if res.get("name"):
            config.group_name = str(res["name"])
        return " Web-токен VK API (vk1.a...) успешно получен и проверен."
    except Exception as api_exc:  # noqa: BLE001
        logger.info("VK web token API check note: %s", api_exc)
        return " Браузерная сессия активна."


async def _fail_vk_probe(
    config: VKVideoConfig,
    session: AsyncSession | None,
    *,
    status: str,
    message: str,
    now_str: str,
) -> dict[str, Any]:
    """Persist failed probe state and return formatted response dict."""
    config.status = status
    config.error_message = message
    config.last_checked_at = now_str
    if session is not None:
        await save_vk_video_config(session, config)
    return {
        "is_valid": False,
        "status": status,
        "message": message,
        "timestamp": now_str,
    }


def _attach_vk_token_listeners(page: Any, captured_tokens: list[str]) -> None:
    """Attach request/response listeners to capture vk1.a... web tokens."""

    def _scan(text: str) -> None:
        for match in re.findall(r"vk1\.a\.[A-Za-z0-9_\-]{20,}", text):
            if match not in captured_tokens:
                captured_tokens.append(match)

    async def _on_response(response: Any) -> None:
        try:
            url = response.url
            if (
                "act=web_token" in url
                or "api.vk." in url
                or "login.vk." in url
            ):
                _scan(await response.text())
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_request(request: Any) -> None:
        try:
            _scan(request.url)
            if request.post_data:
                _scan(request.post_data)
        except Exception:  # noqa: BLE001, S110
            pass

    page.on("response", _on_response)
    page.on("request", _on_request)


async def probe_vk_browser_session(
    config: VKVideoConfig,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Verify VK browser session via Playwright and auto-extract web access_token."""
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    if not config.has_session:
        return {
            "is_valid": False,
            "status": "not_configured",
            "message": (
                "Сессия браузера VK не загружена. Вставьте JSON кук из "
                "Cookie-Editor или укажите VK Access Token."
            ),
            "timestamp": now_str,
        }

    from app.services.web_scraper import get_browser_pool  # noqa: PLC0415

    clean_state = normalize_vk_storage_state(config.storage_state or {})
    target_group_id = (config.group_id or "236626422").lstrip("-")
    has_vk_ru = any(
        "vk.ru" in str(c.get("domain", ""))
        for c in clean_state.get("cookies", [])
        if isinstance(c, dict)
    )
    base_host = "vk.ru" if has_vk_ru else "vk.com"
    club_url = f"https://{base_host}/club{target_group_id}"
    captured_tokens: list[str] = []

    pool = get_browser_pool()
    try:
        async with pool.page(
            storage_state=clean_state, block_resources=False
        ) as page:
            _attach_vk_token_listeners(page, captured_tokens)
            await page.goto(club_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2.5)
            if "challenge.html" in page.url:
                await asyncio.sleep(4.0)

            current_url = page.url
            page_title = await page.title()

            if any(
                frag in current_url
                for frag in ("id.vk.com/auth", "id.vk.ru/auth", "/login", "act=login")
            ):
                return await _fail_vk_probe(
                    config,
                    session,
                    status="expired",
                    message=(
                        "Сессия ВКонтакте истекла (перенаправление на страницу входа). "
                        "Обновите куки из браузера."
                    ),
                    now_str=now_str,
                )

            await _extract_vk_token_in_page(page, captured_tokens)

            login_btn = await page.query_selector(
                '#index_login, [data-testid="enter-another-way"], .VkIdForm'
            )
            profile_el = await page.query_selector(
                '#top_profile_link, [data-testid="header-profile"], #l_pr'
            )
            if login_btn is not None and profile_el is None and not captured_tokens:
                return await _fail_vk_probe(
                    config,
                    session,
                    status="expired",
                    message=(
                        "Гостевой режим VK (кнопка «Войти» на странице). "
                        "Экспортируйте куки авторизованного аккаунта vk.com."
                    ),
                    now_str=now_str,
                )

            try:
                updated_state = await page.context.storage_state()
                if isinstance(updated_state, dict) and updated_state.get("cookies"):
                    config.storage_state = normalize_vk_storage_state(updated_state)
            except Exception:  # noqa: BLE001, S110
                pass

            if captured_tokens:
                config.access_token = captured_tokens[0]

            clean_title = (
                page_title.replace("| ВКонтакте", "")
                .replace("ВКонтакте", "")
                .strip(" —|-")
            )
            if (
                clean_title
                and "робот" not in clean_title.lower()
                and "429" not in clean_title.lower()
            ):
                config.group_name = clean_title
            elif "робот" in (config.group_name or "").lower():
                config.group_name = "СводкаПресс"

            api_note = await _verify_vk_group_api(config, target_group_id)
            if not config.access_token:
                config.status = "needs_token"
                config.error_message = (
                    "Куки сессии активны, но токен API не извлечён. "
                    "Вставьте Ключ доступа сообщества (Управление → Работа с API) "
                    "в поле «Ключ доступа / токен» для публикации постов."
                )
            else:
                config.status = "active"
                config.error_message = None
            config.last_checked_at = now_str
            if session is not None:
                await save_vk_video_config(session, config)

            group_label = (
                f"«{config.group_name}» (ID {target_group_id})"
                if config.group_name
                else f"club{target_group_id}"
            )
            return {
                "is_valid": bool(config.access_token),
                "status": config.status,
                "message": (
                    f"Сессия ВКонтакте проверена для {group_label}. "
                    + (config.error_message or f"Доступ к API подтвержден.{api_note}")
                ),
                "timestamp": now_str,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("VK browser session probe failed: %s", exc)
        return await _fail_vk_probe(
            config,
            session,
            status="error",
            message=f"Ошибка при проверке сессии VK: {exc}",
            now_str=now_str,
        )

