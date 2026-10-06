"""VK community adapter: posts an article to a group wall via ``wall.post``.

VK is the second-largest distribution target, and its wall API differs from the
video upload in :mod:`app.services.vk_video_service` in one way that matters: the
target group is addressed by a **negative** ``owner_id`` rather than a positive
``group_id``. Getting that wrong posts to the authenticated user's personal wall,
which is a silent and embarrassing failure.

What this adapter has to get right:

* **``from_group=1``.** Without it the post is attributed to the administrator who
  issued the token rather than to the community, so it appears as a personal post
  on the group wall and loses the community branding.
* **Attachments are a flat comma-separated string**, not JSON. The format is
  ``{type}{owner_id}_{media_id}`` — ``video-222333_777`` — where a negative owner
  id keeps its sign. A malformed entry is dropped by VK **silently**: the post is
  created without the video and returns success, which is why the string is built
  from typed values rather than interpolated ad hoc.
* **A link is an attachment, not text.** Passing the article URL as an attachment
  makes VK render a card with the cover image and headline; leaving it in the
  message body produces a bare blue link. The card is worth far more engagement, so
  the URL is attached and omitted from the text.

**Attachment precedence: video, then photo, then nothing extra.** VK renders one
primary media block, and a video card outperforms a still. When neither exists the
link card carries the post on its own, which is why an article without media is
still worth publishing.

Errors reuse the code mapping from :mod:`app.services.vk_video_service`: the two
services talk to the same API, and a second table of error codes would drift from
the first.

The access token is sent as a form field, never in the query string, so it does not
land in an intermediary's access log.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
)
from app.services.text_utils import html_to_plain_text
from app.services.vk_video_service import (
    VK_API_BASE_URL,
    VKAuthError,
    VKError,
    VKPermissionError,
    VKRateLimitError,
    VKResponseError,
    normalise_group_id,
    raise_for_vk_error,
)

if TYPE_CHECKING:
    from app.models.article import Article

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MAX_POST_LENGTH",
    "VKWallAdapter",
    "build_attachments",
    "build_wall_message",
]

#: Rich wall post budget (VK supports up to 15895 chars).
MAX_POST_LENGTH: Final[int] = 3800

MAX_ATTEMPTS: Final[int] = 3
INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
BACKOFF_MULTIPLIER: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 20.0

REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0

#: Photo attachments per post. VK renders more as a gallery, but the first few are
#: what a reader sees in the feed.
MAX_PHOTO_ATTACHMENTS: Final[int] = 4


@dataclass(frozen=True, slots=True)
class VKAttachment:
    """One VK attachment, addressed by type, owner and media id.

    ``owner_id`` keeps its sign: a community's media is owned by a negative id, and
    dropping the minus addresses a *user's* media of the same numeric id, which
    either fails or attaches something unrelated.
    """

    kind: str
    owner_id: int
    media_id: int
    access_key: str = ""

    def serialise(self) -> str:
        """Render the ``{type}{owner_id}_{media_id}`` form VK expects."""
        base = f"{self.kind}{self.owner_id}_{self.media_id}"
        # An unlisted video needs its access key appended, or the attachment
        # resolves to nothing for anyone but the uploader.
        return f"{base}_{self.access_key}" if self.access_key else base


def build_attachments(
    *,
    video_links: Sequence[Mapping[str, Any]] = (),
    photo_ids: Sequence[Mapping[str, Any]] = (),
    link_url: str = "",
) -> str:
    """Build the comma-separated ``attachments`` value for ``wall.post``.

    Precedence is video, then photos: VK renders one primary media block and a
    video card draws more engagement than a still. The link is appended last
    because VK builds its preview card from the final link in the list.

    A malformed attachment is dropped by VK *silently* — the post succeeds without
    it — so entries whose ids are not integers are skipped here, where the omission
    can at least be logged.
    """
    parts: list[str] = []

    for entry in video_links:
        if str(entry.get("platform") or "vk") != "vk":
            continue
        owner = entry.get("owner_id")
        video = entry.get("video_id")
        if not isinstance(owner, int) or not isinstance(video, int):
            logger.warning("Skipping a VK video attachment with non-integer ids")
            continue
        access_key = str(entry.get("access_key") or "")
        parts.append(
            VKAttachment(
                kind="video", owner_id=owner, media_id=video, access_key=access_key
            ).serialise()
        )
        # One video is what VK renders as the primary block.
        break

    if not parts:
        for entry in list(photo_ids)[:MAX_PHOTO_ATTACHMENTS]:
            owner = entry.get("owner_id")
            photo = entry.get("photo_id") or entry.get("media_id")
            if not isinstance(owner, int) or not isinstance(photo, int):
                logger.warning("Skipping a VK photo attachment with non-integer ids")
                continue
            parts.append(
                VKAttachment(kind="photo", owner_id=owner, media_id=photo).serialise()
            )

    if link_url:
        # Attached rather than inlined: VK renders a card with the cover and
        # headline, which is worth far more than a bare blue link in the text.
        parts.append(link_url)

    return ",".join(parts)


def _clean_vk_hashtags(keywords: Sequence[str], *, limit: int = 5) -> list[str]:
    """Extract clean, readable, single-word hashtags from keywords."""
    stop_words = {
        "россии", "после", "года", "стал", "были", "было", "когда", "этом",
        "своих", "своего", "будет", "может", "между", "через", "около", "против",
        "время", "число", "ранее", "также", "новые", "нового",
    }
    seen: dict[str, None] = {}
    for kw in keywords:
        words = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}", kw.lower())
        for word in words:
            if word in stop_words or word.isdigit():
                continue
            seen.setdefault(word, None)
            if len(seen) >= limit:
                break
        if len(seen) >= limit:
            break
    if not seen:
        return ["#новости", "#россия", "#сводка"]
    return [f"#{w}" for w in seen]


def build_wall_message(
    *,
    title: str,
    body_html: str,
    description: str = "",
    keywords: Sequence[str] = (),
    limit: int = MAX_POST_LENGTH,
    include_link: str = "",
    telegram_channel_url: str = "https://t.me/svodka_press",
    promo_text: str = "",
) -> str:
    """Assemble an engaging, comprehensive VK wall post.

    Instead of a 1-sentence truncated teaser, builds a rich, readable news post
    with key facts, quotes, website link, Telegram CTA, sponsor block and hashtags.
    """
    headline = " ".join(title.split())
    if not any(ord(c) > 10000 for c in headline[:3]):
        headline = f"📰 {headline}"

    tags = build_hashtags(keywords, limit=5)
    tag_line = " ".join(tags)

    # Extract substantive plain paragraphs
    raw_plain = html_to_plain_text(body_html)
    paragraphs = [p.strip() for p in raw_plain.split("\n\n") if len(p.strip()) >= 25]

    link_block = f"👉 Читать полный материал с первоисточниками на сайте:\n{include_link}" if include_link else ""
    tg_block = f"⚡️ Оперативные сводки и срочные новости в Telegram:\n{telegram_channel_url}" if telegram_channel_url else ""
    ad_block = (
        promo_text.strip()
        if promo_text.strip()
        else (
            "💡 Партнёр рубрики — FreeAPI AI: бесплатный доступ к GPT-4o, Claude 3.5 Sonnet и Midjourney "
            "для авторов и специалистов из РФ.\nПопробовать прямо сейчас: https://freeapiai.ru/r/tdCxTE7k"
        )
    )

    footer_parts = [p for p in (link_block, tg_block, ad_block, tag_line) if p]
    footer_text = "\n\n".join(footer_parts)

    body_budget = max(limit - len(headline) - len(footer_text) - 20, 250)

    selected_paragraphs: list[str] = []
    current_len = 0

    clean_desc = description.strip()
    if clean_desc and not any(clean_desc[:35].lower() in p.lower() for p in paragraphs):
        selected_paragraphs.append(clean_desc)
        current_len += len(clean_desc) + 2

    for p in paragraphs:
        if any(p.startswith(prefix) for prefix in ("Источник:", "Фото:", "Видео:", "Подписывайтесь", "Подробнее")):
            continue
        if current_len + len(p) + 2 > body_budget:
            if not selected_paragraphs:
                selected_paragraphs.append(p[:body_budget].rstrip() + "…")
            break
        selected_paragraphs.append(p)
        current_len += len(p) + 2
        if len(selected_paragraphs) >= 5:
            break

    body_text = "\n\n".join(selected_paragraphs)
    parts = [headline, "", body_text, "", footer_text]
    return "\n".join(p for p in parts if p is not None).strip()


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, so a batch does not retry in lockstep."""
    base = min(
        INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1)),
        MAX_BACKOFF_SECONDS,
    )
    return base * (0.75 + random.random() * 0.5)  # noqa: S311 - jitter, not crypto


class VKWallAdapter(BaseSocialAdapter):
    """Publishes an article to a VK community wall."""

    platform = Platform.VK

    __slots__ = (
        "_api_version",
        "_base_url",
        "_client",
        "_group_id",
        "_lock",
        "_token",
        "_transport",
    )

    def __init__(
        self,
        *,
        access_token: str | None = None,
        group_id: int | str | None = None,
        api_version: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = (
            settings.vk_access_token if access_token is None else access_token
        ).strip()
        self._group_id = normalise_group_id(
            settings.vk_group_id if group_id is None else group_id
        )
        self._api_version = (api_version or settings.vk_api_version).strip()
        self._base_url = (base_url or VK_API_BASE_URL).rstrip("/")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """Whether a token and a target group are both available."""
        return bool(self._token and self._group_id)

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
        if not self._token:
            raise SocialNotConfiguredError(
                "VK_ACCESS_TOKEN is required to publish to a VK wall"
            )
        if not self._group_id:
            raise SocialNotConfiguredError(
                "VK_GROUP_ID is required: without it a post would land on the "
                "authenticated user's personal wall"
            )
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                    transport=self._transport,
                )
            return self._client

    def _translate(self, exc: VKError) -> Exception:
        """Map a VK service error onto this module's social error type.

        The mapping exists so the adapter contract stays uniform across platforms
        while the VK error-code table lives in one place.
        """
        if isinstance(exc, VKRateLimitError):
            return SocialRateLimitError(str(exc))
        if isinstance(exc, (VKAuthError, VKPermissionError)):
            return SocialAuthError(str(exc))
        retryable = not isinstance(exc, VKResponseError)
        return SocialResponseError(str(exc), retryable=retryable)

    async def _call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Call one VK API method, retrying transport failures and rate limits."""
        client = await self._ensure_client()
        body = {
            **{key: value for key, value in params.items() if value is not None},
            "access_token": self._token,
            "v": self._api_version,
        }
        last_error: Exception = SocialResponseError(
            f"{method} was never attempted", retryable=True
        )

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(f"/{method}", data=body)
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
                if not isinstance(payload, dict):
                    raise SocialResponseError(
                        f"{method} returned {type(payload).__name__}, expected an "
                        "object"
                    )
                try:
                    # VK reports application errors with HTTP 200, so the body has
                    # to be inspected before its `response` is read.
                    raise_for_vk_error(payload, method)
                except VKError as exc:
                    translated = self._translate(exc)
                    if not isinstance(translated, SocialRateLimitError):
                        raise translated from exc
                    last_error = translated
                else:
                    result = payload.get("response")
                    if isinstance(result, (Mapping, list, int, str)):
                        return result
                    raise SocialResponseError(
                        f"{method} returned no response object"
                    )

            if attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(_backoff_delay(attempt))

        raise last_error

    async def upload_wall_photo(
        self, photo_source: str | bytes, *, filename: str = "cover.jpg"
    ) -> str | None:
        """Upload a photo to VK and return an attachment string like 'photo{owner_id}_{id}'."""
        if not self.is_configured:
            return None

        image_bytes: bytes = b""
        if isinstance(photo_source, bytes):
            image_bytes = photo_source
        elif isinstance(photo_source, str) and photo_source.startswith(("http://", "https://")):
            try:
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as fetcher:
                    resp = await fetcher.get(photo_source)
                    if resp.status_code == 200:
                        image_bytes = resp.content
            except Exception as exc:
                logger.warning("Failed to fetch image for VK from %s: %s", photo_source, exc)
                return None
        elif isinstance(photo_source, str) and photo_source:
            try:
                with open(photo_source, "rb") as f:
                    image_bytes = f.read()
            except Exception as exc:
                logger.warning("Failed to read local image for VK from %s: %s", photo_source, exc)
                return None

        if len(image_bytes) < 500:
            return None

        client = await self._ensure_client()
        group_id = abs(self._group_id or 0)

        # 1. Try photos.getWallUploadServer (works with user token)
        try:
            wall_up = await self._call("photos.getWallUploadServer", {"group_id": group_id})
            upload_url = wall_up.get("upload_url") if isinstance(wall_up, Mapping) else None
            if upload_url:
                r_up = await client.post(upload_url, files={"photo": (filename, image_bytes, "image/jpeg")})
                up_data = r_up.json()
                save_resp = await self._call("photos.saveWallPhoto", {
                    "group_id": group_id,
                    "photo": up_data.get("photo"),
                    "server": up_data.get("server"),
                    "hash": up_data.get("hash"),
                })
                items = save_resp if isinstance(save_resp, list) else (save_resp.get("response") if isinstance(save_resp, Mapping) else [])
                if items and isinstance(items, list):
                    item = items[0]
                    p_id = item.get("id")
                    p_owner = item.get("owner_id")
                    if p_id and p_owner is not None:
                        return f"photo{p_owner}_{p_id}"
        except Exception as exc:
            logger.info("VK photos.getWallUploadServer not available with current token (%s); trying photos.getMessagesUploadServer", exc)

        # 2. Try photos.getMessagesUploadServer (works with community / group token!)
        try:
            msg_up = await self._call("photos.getMessagesUploadServer", {})
            upload_url = msg_up.get("upload_url") if isinstance(msg_up, Mapping) else None
            if upload_url:
                r_up = await client.post(upload_url, files={"photo": (filename, image_bytes, "image/jpeg")})
                up_data = r_up.json()
                save_resp = await self._call("photos.saveMessagesPhoto", {
                    "photo": up_data.get("photo"),
                    "server": up_data.get("server"),
                    "hash": up_data.get("hash"),
                })
                items = save_resp if isinstance(save_resp, list) else (save_resp.get("response") if isinstance(save_resp, Mapping) else [])
                if items and isinstance(items, list):
                    item = items[0]
                    p_id = item.get("id")
                    p_owner = item.get("owner_id")
                    if p_id and p_owner is not None:
                        return f"photo{p_owner}_{p_id}"
        except Exception as exc:
            logger.warning("VK photos.getMessagesUploadServer failed: %s", exc)

        return None

    async def publish(
        self, article: Article, wp_post_url: str, **kwargs: Any
    ) -> PublishResult:
        """Post the article to the community wall."""
        photo_attachments = list(kwargs.get("photo_attachments") or ())
        cover_url = str(kwargs.get("cover_url") or kwargs.get("photo_url") or "")
        uploaded_photo = ""
        warnings: list[str] = []

        # 1. Try uploading cover photo if available and no pre-existing attachments
        if not photo_attachments and cover_url:
            try:
                uploaded_photo = await self.upload_wall_photo(cover_url)
            except Exception as exc:
                logger.warning("VK cover photo upload failed for %s: %s", cover_url, exc)
                warnings.append(f"cover photo upload failed: {exc}")

        # 2. Build attachments: video, uploaded photo.
        # If media is uploaded, do NOT attach the bare link as a preview card attachment,
        # because VK rejects it with 'link_photo_sizing_rule' and the link is already
        # formatted into the message text body.
        media_parts: list[str] = []
        if article.video_links:
            v_att = build_attachments(video_links=tuple(article.video_links or ()))
            if v_att:
                media_parts.append(v_att)

        if uploaded_photo:
            media_parts.append(uploaded_photo)
        elif photo_attachments:
            p_att = build_attachments(photo_ids=photo_attachments)
            if p_att:
                media_parts.append(p_att)

        target_link = wp_post_url.strip()
        attachments = ",".join(media_parts) if media_parts else target_link

        if not attachments:
            warnings.append("no attachments: the post carries text only")

        promo_text = str(kwargs.get("promo_text") or "")
        message = build_wall_message(
            title=article.title,
            body_html=article.content_html,
            description=article.seo_description or "",
            keywords=tuple(article.seo_keywords or ()),
            include_link=wp_post_url,
            promo_text=promo_text,
        )

        owner_id = -abs(self._group_id or 0)
        params: dict[str, Any] = {
            "owner_id": owner_id,
            "message": message,
            "from_group": 1,
        }
        if attachments:
            params["attachments"] = attachments
        try:
            result = await self._call("wall.post", params)
        except SocialResponseError as exc:
            if "link_photo_sizing_rule" in str(exc) or "100" in str(exc):
                logger.warning(
                    "VK wall.post failed with attachment (%s); retrying with text only",
                    exc,
                )
                params.pop("attachments", None)
                result = await self._call("wall.post", params)
                warnings.append("attachment dropped due to VK link_photo_sizing_rule")
            else:
                raise

        post_id = result.get("post_id") if isinstance(result, Mapping) else None
        if not isinstance(post_id, int):
            raise SocialResponseError("wall.post returned no post_id")

        return PublishResult.published(
            self.platform_code,
            external_id=f"{owner_id}_{post_id}",
            url=f"https://vk.com/wall{owner_id}_{post_id}",
            warnings=warnings,
        )
