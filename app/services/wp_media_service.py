"""WordPress media uploader: puts article images into the WP media library.

WordPress is the canonical destination for every generated cover, and the upload
has to carry more than bytes. Three things are set here because none of them can
be fixed afterwards without an extra API round trip:

* **A transliterated filename.** ``новости_дня.jpg`` uploaded verbatim becomes a
  percent-encoded URL (``%D0%BD%D0%BE%D0%B2...``) that is unreadable in a
  browser, unusable in a share preview and worthless for image search. The
  filename is transliterated to ``novosti-dnya.jpg`` before upload, because
  WordPress derives the attachment slug and the final URL from it and renaming
  later leaves the original URL live.
* **``alt_text``.** Required for accessibility and the single strongest image-SEO
  signal. It is built from the article title, not from the keyword list, because
  alt text describes the picture to someone who cannot see it — a keyword dump
  fails both purposes.
* **``title`` and ``caption``.** The media-library label an editor searches by,
  and the figure caption the theme renders.

**Authentication is an Application Password over Basic auth.** That is the
WordPress-supported way to authenticate REST writes without a plugin. It is only
safe over HTTPS, so a non-HTTPS ``WP_BASE_URL`` is rejected outside development
rather than silently transmitting credentials in the clear.

**Uploads are not retried blindly.** A ``POST`` to ``/wp/v2/media`` that times out
may well have succeeded server-side, so retrying it can create a duplicate
attachment and orphan the first. Transport failures *before* a response are
retried; a timeout is retried at most once and only after checking whether the
filename already exists, which is what keeps the media library from filling with
near-identical copies.

The application password is never logged and never interpolated into an exception.
"""

# The transliteration table is Cyrillic by definition, so letters that look like
# Latin ones are the subject matter.
# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx

from app.config import settings

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MAX_ALT_TEXT_CHARS",
    "MAX_ATTEMPTS",
    "MAX_SLUG_CHARS",
    "MAX_UPLOAD_BYTES",
    "MEDIA_ENDPOINT",
    "UploadedMedia",
    "WPAuthError",
    "WPMediaError",
    "WPMediaService",
    "WPNotConfiguredError",
    "WPRateLimitError",
    "WPResponseError",
    "build_basic_auth_header",
    "close_wp_media_service",
    "get_wp_media_service",
    "slugify_filename",
    "transliterate",
    "upload_article_image",
]

MEDIA_ENDPOINT: Final[str] = "/wp-json/wp/v2/media"

MAX_ATTEMPTS: Final[int] = 3
INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
BACKOFF_MULTIPLIER: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 20.0

#: An image upload is a transfer, not a JSON round trip, so it gets its own
#: timeout rather than the API default.
UPLOAD_TIMEOUT_SECONDS: Final[float] = 180.0

#: Ceiling on an accepted image. Above this the upload is a misconfiguration:
#: WordPress installations commonly cap ``upload_max_filesize`` at 8-64 MB, and
#: buffering more would strain the 4 GB target VPS before PHP rejected it.
MAX_UPLOAD_BYTES: Final[int] = 32 * 1024 * 1024

#: Filenames longer than this are truncated. Some filesystems and many CDNs
#: struggle past ~100 characters, and a slug that long carries no extra SEO value.
MAX_SLUG_CHARS: Final[int] = 96

#: Alt text past this is truncated by screen readers and ignored by search
#: engines, so a longer value is pure payload.
MAX_ALT_TEXT_CHARS: Final[int] = 125

MAX_CAPTION_CHARS: Final[int] = 300

#: Content types WordPress accepts for an image attachment, mapped to the
#: extension the filename must carry. WordPress validates the extension against
#: the MIME type and rejects a mismatch, so the two are derived together.
_MIME_EXTENSIONS: Final[Mapping[str, str]] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
}

DEFAULT_CONTENT_TYPE: Final[str] = "image/jpeg"

#: Cyrillic to Latin, following the GOST 7.79-2000 System B conventions that
#: Russian sites use for URLs. Digraph letters are spelled out (``ж`` -> ``zh``)
#: rather than approximated, because ``novosti-dnya`` is readable and
#: ``novosti-dna`` is a different word.
_CYRILLIC_MAP: Final[Mapping[str, str]] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    # Hard and soft signs carry no Latin equivalent and are dropped rather than
    # transliterated to an apostrophe, which is not URL-safe.
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Ukrainian and Belarusian letters appear in reposted source material.
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",
}

#: Characters that survive into a slug. Everything else becomes a hyphen.
_SLUG_ALLOWED_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_HYPHEN_RUN_RE: Final[re.Pattern[str]] = re.compile(r"-{2,}")


class WPMediaError(RuntimeError):
    """Base class for every failure surfaced by this module."""


class WPNotConfiguredError(WPMediaError):
    """WordPress credentials or base URL are missing.

    Raised rather than degraded: an article published without its cover would
    have to be repaired by hand afterwards.
    """


class WPAuthError(WPMediaError):
    """WordPress rejected the Application Password (401/403).

    Never retried: the credential is wrong, or the user lacks ``upload_files``.
    """


class WPRateLimitError(WPMediaError):
    """The host rate-limited the upload after the retry budget."""


class WPResponseError(WPMediaError):
    """WordPress answered with an error or an unusable payload."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class UploadedMedia:
    """One attachment as WordPress created it."""

    media_id: int
    source_url: str
    thumbnail_url: str
    slug: str
    alt_text: str
    mime_type: str
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Render the fields the worker persists and the dashboard displays."""
        return {
            "media_id": self.media_id,
            "source_url": self.source_url,
            "thumbnail_url": self.thumbnail_url,
            "slug": self.slug,
            "alt_text": self.alt_text,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
        }


def transliterate(text: str) -> str:
    """Transliterate Cyrillic to Latin, leaving Latin text untouched.

    Two stages, in this order. Cyrillic is mapped explicitly, because Unicode
    normalisation cannot romanise it — ``NFKD`` leaves ``н`` as ``н``. Whatever
    remains is then normalised and stripped of combining marks, which folds the
    accented Latin that arrives in transliterated European names (``café`` ->
    ``cafe``).
    """
    mapped: list[str] = []
    for char in text:
        lowered = char.lower()
        replacement = _CYRILLIC_MAP.get(lowered)
        if replacement is None:
            mapped.append(char)
            continue
        # Case is preserved so an acronym stays legible before the slug lowercases
        # everything; a title-cased digraph reads as `Zh`, not `ZH`.
        if char.isupper():
            replacement = replacement.capitalize()
        mapped.append(replacement)
    decomposed = unicodedata.normalize("NFKD", "".join(mapped))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def slugify(text: str, *, max_chars: int = MAX_SLUG_CHARS) -> str:
    """Reduce arbitrary text to a lowercase hyphenated ASCII slug.

    Truncation is applied at a hyphen boundary where possible, so a slug is never
    cut mid-word: ``novosti-dn`` reads as a typo, ``novosti`` reads as a slug.
    """
    latin = transliterate(text)
    slug = _SLUG_ALLOWED_RE.sub("-", latin.lower())
    slug = _HYPHEN_RUN_RE.sub("-", slug).strip("-")
    if len(slug) <= max_chars:
        return slug
    clipped = slug[:max_chars]
    boundary = clipped.rfind("-")
    if boundary >= max_chars // 2:
        clipped = clipped[:boundary]
    return clipped.strip("-")


def slugify_filename(
    filename: str, *, content_type: str = DEFAULT_CONTENT_TYPE
) -> str:
    """Build an SEO-clean upload filename from an arbitrary source name.

    ``новости_дня.jpg`` becomes ``novosti-dnya.jpg``. The extension is taken from
    the **content type**, not from the input: WordPress validates the two against
    each other and rejects a mismatch, and a scraped filename routinely lies about
    its format (a ``.jpg`` that is really a PNG). A name that transliterates to
    nothing — an emoji, or pure punctuation — falls back to ``image``, because an
    empty filename makes WordPress reject the request.
    """
    mime = content_type.split(";", maxsplit=1)[0].strip().lower()
    extension = _MIME_EXTENSIONS.get(mime)
    if extension is None:
        raise ValueError(
            f"unsupported image content type {content_type!r}; supported: "
            + ", ".join(sorted(_MIME_EXTENSIONS))
        )
    stem = filename.rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    slug = slugify(stem, max_chars=MAX_SLUG_CHARS - len(extension) - 1)
    return f"{slug or 'image'}.{extension}"


def build_alt_text(article_title: str, keywords: Sequence[str] = ()) -> str:
    """Build alt text describing the image for a reader who cannot see it.

    The article title is the description; a keyword is appended only when it adds
    a term the title does not already contain. Alt text is not a keyword slot —
    stuffing it degrades both accessibility and the ranking signal it feeds — so
    at most one keyword is added and only when it is genuinely new information.
    """
    base = " ".join(article_title.split()).strip()
    if not base:
        base = "Иллюстрация к материалу"
    lowered = base.lower()
    for keyword in keywords:
        candidate = " ".join(keyword.split()).strip()
        if candidate and candidate.lower() not in lowered:
            base = f"{base} — {candidate}"
            break
    return base[:MAX_ALT_TEXT_CHARS].strip()


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, so a batch does not retry in lockstep."""
    base = min(
        INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1)),
        MAX_BACKOFF_SECONDS,
    )
    return base * (0.75 + random.random() * 0.5)  # noqa: S311 - jitter, not crypto


def build_basic_auth_header(username: str, app_password: str) -> str:
    """Encode WordPress Application Password credentials for Basic auth.

    Shared with :mod:`app.services.wp_publisher` so the credential is assembled in
    exactly one place: an auth bug fixed in one client but not the other is the
    kind of divergence that only shows up in production.

    WordPress prints Application Passwords in space-separated groups of four for
    legibility. The spaces are presentation only — leaving them in produces a
    credential that silently fails every request — so they are stripped here
    rather than at each call site.

    Returns ``""`` when either half is missing, which callers treat as
    "not configured" rather than as an empty credential.
    """
    user = username.strip()
    secret = app_password.strip().replace(" ", "")
    if not user or not secret:
        return ""
    encoded = base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")
    return f"Basic {encoded}"


def _rendered(value: Any) -> str:
    """Read a WordPress field that may be a string or a ``{"rendered": ...}`` map."""
    if isinstance(value, Mapping):
        rendered = value.get("rendered")
        return str(rendered) if isinstance(rendered, str) else ""
    return str(value) if isinstance(value, str) else ""


def _thumbnail_url(payload: Mapping[str, Any], fallback: str) -> str:
    """Pick the best available thumbnail from ``media_details.sizes``.

    ``medium`` is preferred over ``thumbnail`` because WordPress crops
    ``thumbnail`` to a square by default, which decapitates a 16:9 cover. The full
    URL is the fallback: a freshly uploaded attachment may not have its
    intermediate sizes generated yet, and a missing thumbnail must not fail the
    upload.
    """
    details = payload.get("media_details")
    if isinstance(details, Mapping):
        sizes = details.get("sizes")
        if isinstance(sizes, Mapping):
            for name in ("medium_large", "medium", "large", "thumbnail"):
                entry = sizes.get(name)
                if isinstance(entry, Mapping):
                    url = entry.get("source_url")
                    if isinstance(url, str) and url:
                        return url
    return fallback


class WPMediaService:
    """Async WordPress REST client for the media library."""

    __slots__ = ("_auth_header", "_base_url", "_client", "_lock", "_transport")

    def __init__(
        self,
        *,
        base_url: str | None = None,
        username: str | None = None,
        app_password: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (
            settings.wp_base_url if base_url is None else base_url
        ).rstrip("/")
        self._auth_header = build_basic_auth_header(
            settings.wp_app_user if username is None else username,
            settings.wp_app_password if app_password is None else app_password,
        )
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """Whether a base URL and credentials are both available."""
        return bool(self._base_url and self._auth_header)

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
        if not self._base_url:
            raise WPNotConfiguredError(
                "WP_BASE_URL is not set: media uploads are unavailable"
            )
        if not self._auth_header:
            raise WPNotConfiguredError(
                "WP_APP_USER and WP_APP_PASSWORD are required: WordPress media "
                "uploads authenticate with an Application Password"
            )
        if not self._base_url.startswith("https://") and settings.is_production:
            # Basic auth transmits the credential on every request, so plain HTTP
            # would publish it to every hop in production.
            raise WPNotConfiguredError(
                "WP_BASE_URL must use HTTPS in production: an Application "
                "Password sent over HTTP is exposed in transit"
            )
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(UPLOAD_TIMEOUT_SECONDS),
                    headers={
                        "Authorization": self._auth_header,
                        "Accept": "application/json",
                        "User-Agent": "SvodkaPress/1.0",
                    },
                    transport=self._transport,
                    follow_redirects=True,
                )
            return self._client

    def _status_error(self, response: httpx.Response, action: str) -> WPMediaError:
        """Build a typed error from a non-2xx response, without leaking secrets."""
        detail = response.text[:300].replace("\n", " ").strip() or "no body"
        if response.status_code in (
            httpx.codes.UNAUTHORIZED,
            httpx.codes.FORBIDDEN,
        ):
            return WPAuthError(
                f"WordPress rejected the Application Password on {action} "
                f"(HTTP {response.status_code}). Confirm WP_APP_USER and that the "
                "user has the upload_files capability."
            )
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            return WPRateLimitError(f"{action} was rate-limited: {detail}")
        return WPResponseError(
            f"{action} failed with HTTP {response.status_code}: {detail}",
            status_code=response.status_code,
        )

    def _decode(self, response: httpx.Response, action: str) -> dict[str, Any]:
        """Decode a WordPress JSON object body."""
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise WPResponseError(
                f"{action} returned a non-JSON body "
                f"(HTTP {response.status_code}, {len(response.content)} bytes). "
                "A WordPress PHP error or an HTML login page is the usual cause."
            ) from exc
        if not isinstance(payload, dict):
            raise WPResponseError(
                f"{action} returned {type(payload).__name__}, expected an object"
            )
        return payload

    async def upload_image(
        self,
        image_bytes: bytes,
        *,
        filename: str,
        article_title: str,
        keywords: Sequence[str] = (),
        caption: str = "",
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> UploadedMedia:
        """Upload one image and set its alt text, title and caption.

        The filename is transliterated before upload because WordPress derives the
        attachment slug and the public URL from it, and a later rename leaves the
        original Cyrillic-encoded URL live.

        Alt text, title and caption are sent as multipart fields in the same
        request. WordPress accepts them alongside the binary, so this is one round
        trip rather than an upload followed by a ``POST`` update — which matters
        because the second call could fail and leave an untitled attachment.
        """
        if not image_bytes:
            raise WPMediaError("refusing to upload an empty image payload")
        if len(image_bytes) > MAX_UPLOAD_BYTES:
            raise WPMediaError(
                f"image is {len(image_bytes)} bytes, above the "
                f"{MAX_UPLOAD_BYTES}-byte ceiling"
            )

        safe_name = slugify_filename(filename, content_type=content_type)
        alt_text = build_alt_text(article_title, keywords)
        title = " ".join(article_title.split())[:MAX_ALT_TEXT_CHARS] or safe_name
        figure_caption = " ".join(caption.split())[:MAX_CAPTION_CHARS]

        client = await self._ensure_client()
        files = {"file": (safe_name, image_bytes, content_type)}
        data = {
            "alt_text": alt_text,
            "title": title,
            "caption": figure_caption,
            # A media attachment is not a post: `publish` is what makes it
            # visible to the REST consumers that later attach it to an article.
            "status": "publish",
        }

        logger.info(
            "Uploading %s (%d bytes) to the WordPress media library",
            safe_name,
            len(image_bytes),
        )
        payload = await self._post_media(client, files=files, data=data)
        return self._parse_media(payload, slug_hint=safe_name, alt_text=alt_text)

    async def get_media_url(self, media_id: int) -> str:
        """Resolve an attachment id to its public URL.

        Needed because ``articles`` stores only ``wp_image_id``, while the social
        adapters need a URL the platform can fetch: Telegram's ``sendPhoto``, OK's
        photo block and Dzen's cover all take an address, not an id. Without this
        lookup every social post would publish imageless, which roughly halves
        engagement on a news teaser.

        Returns ``""`` rather than raising when the attachment cannot be read: a
        missing cover degrades the post, and losing the whole distribution over it
        would be the wrong trade.
        """
        if media_id <= 0:
            return ""
        try:
            client = await self._ensure_client()
            response = await client.get(f"{MEDIA_ENDPOINT}/{media_id}")
        except (WPMediaError, httpx.HTTPError):
            logger.warning(
                "Could not resolve WordPress media %d to a URL", media_id, exc_info=True
            )
            return ""
        if response.status_code >= httpx.codes.BAD_REQUEST:
            logger.warning(
                "WordPress media %d returned HTTP %d", media_id, response.status_code
            )
            return ""
        try:
            payload: Any = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, Mapping):
            return ""
        url = payload.get("source_url")
        return url if isinstance(url, str) else ""

    async def _post_media(
        self,
        client: httpx.AsyncClient,
        *,
        files: Mapping[str, tuple[str, bytes, str]],
        data: Mapping[str, str],
    ) -> dict[str, Any]:
        """POST the multipart upload, retrying only what is safe to retry.

        A timeout is **not** retried. The request may have been processed
        server-side with only the response lost, and a blind retry would create a
        second attachment while the first stays orphaned in the library. Connection
        failures before any bytes were accepted are retried, and so is a 429.
        """
        last_error: WPMediaError = WPResponseError("upload was never attempted")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(
                    MEDIA_ENDPOINT, files=dict(files), data=dict(data)
                )
            except httpx.TimeoutException as exc:
                raise WPResponseError(
                    "the media upload timed out; it may have completed "
                    "server-side, so it is not retried automatically to avoid a "
                    "duplicate attachment"
                ) from exc
            except httpx.HTTPError as exc:
                last_error = WPResponseError(
                    f"media upload transport failure: {type(exc).__name__}"
                )
            else:
                if response.status_code < httpx.codes.BAD_REQUEST:
                    return self._decode(response, "media upload")
                last_error = self._status_error(response, "media upload")
                if not isinstance(last_error, WPRateLimitError):
                    raise last_error

            if attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(_backoff_delay(attempt))

        raise last_error

    def _parse_media(
        self, payload: Mapping[str, Any], *, slug_hint: str, alt_text: str
    ) -> UploadedMedia:
        """Extract the attachment coordinates from a created-media response."""
        media_id = payload.get("id")
        source_url = payload.get("source_url")
        if not isinstance(media_id, int) or not isinstance(source_url, str):
            raise WPResponseError(
                "WordPress returned no usable id/source_url pair for the upload; "
                "the attachment cannot be referenced without both"
            )

        details = payload.get("media_details")
        width = height = 0
        if isinstance(details, Mapping):
            raw_width, raw_height = details.get("width"), details.get("height")
            width = raw_width if isinstance(raw_width, int) else 0
            height = raw_height if isinstance(raw_height, int) else 0

        return UploadedMedia(
            media_id=media_id,
            source_url=source_url,
            thumbnail_url=_thumbnail_url(payload, source_url),
            slug=str(payload.get("slug") or slug_hint),
            alt_text=_rendered(payload.get("alt_text")) or alt_text,
            mime_type=str(payload.get("mime_type") or DEFAULT_CONTENT_TYPE),
            width=width,
            height=height,
        )


_shared_service: WPMediaService | None = None


def get_wp_media_service() -> WPMediaService:
    """Return the process-wide service, creating it on first call."""
    global _shared_service  # noqa: PLW0603 - module-level singleton accessor
    if _shared_service is None:
        _shared_service = WPMediaService()
    return _shared_service


async def close_wp_media_service() -> None:
    """Release the shared service's connections. Call on shutdown."""
    global _shared_service  # noqa: PLW0603 - module-level singleton accessor
    service = _shared_service
    _shared_service = None
    if service is not None:
        await service.close()


async def upload_article_image(
    image_bytes: bytes,
    *,
    filename: str,
    article_title: str,
    keywords: Sequence[str] = (),
    caption: str = "",
    content_type: str = DEFAULT_CONTENT_TYPE,
    service: WPMediaService | None = None,
) -> UploadedMedia:
    """Upload one article image to WordPress and return its coordinates.

    Raises :class:`WPNotConfiguredError` without credentials,
    :class:`WPAuthError` on a rejected Application Password,
    :class:`WPRateLimitError` after a sustained 429, and
    :class:`WPResponseError` for any other failure. ``ValueError`` signals an
    unsupported content type, which is a programming error rather than an outage.
    """
    engine = get_wp_media_service() if service is None else service
    if service is None and not engine.is_configured:
        try:
            from app.database import AsyncSessionLocal  # noqa: PLC0415
            from app.services.wp_connection import load_wp_connection  # noqa: PLC0415

            async with AsyncSessionLocal() as db_session:
                conn = await load_wp_connection(db_session)
                if conn.has_rest_credentials:
                    async with WPMediaService(
                        base_url=conn.wp_base_url,
                        username=conn.wp_app_user,
                        app_password=conn.wp_app_password,
                    ) as db_engine:
                        return await db_engine.upload_image(
                            image_bytes,
                            filename=filename,
                            article_title=article_title,
                            keywords=keywords,
                            caption=caption,
                            content_type=content_type,
                        )
        except Exception:  # noqa: BLE001, S110
            pass

    return await engine.upload_image(
        image_bytes,
        filename=filename,
        article_title=article_title,
        keywords=keywords,
        caption=caption,
        content_type=content_type,
    )

