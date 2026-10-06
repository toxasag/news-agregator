"""WordPress publisher: creates and updates posts through the REST API.

The last hop of the pipeline. Everything before this produced an article row; this
module turns it into a live post with its taxonomy, its featured image and its
Rank Math metadata.

Four behaviours carry the weight:

**Publishing is idempotent through ``wp_post_id``.** An article that already
carries a post id is *updated*, never re-created. A retried job that created a
second post would leave the first orphaned and live, which is the one failure mode
a news site cannot tolerate — duplicate articles are what search engines penalise.

**Tags resolve through the local cache first.** ``wp_tags_cache`` mirrors
WordPress term ids keyed by ``lower(tag_name)``. A run that publishes ten articles
about the same event would otherwise issue ten identical term searches and, worse,
race itself into creating duplicate terms. The cache is consulted first, WordPress
is searched second, and a term is created only as a last resort. When two workers
create the same term concurrently, the loser's unique-index violation is caught and
resolved by re-reading the winner's row rather than by failing the publish.

**Rank Math fields go through ``meta``, and are verified.** The REST API silently
drops ``meta`` keys that are not registered with ``show_in_rest``, returning 200
with the post created and the SEO fields absent. That failure is invisible without
a check, so the response is inspected and a warning is recorded when the fields did
not stick — the post is still published, because an article without meta
descriptions is worth far more than no article.

**Only 429 and 5xx are retried.** A ``POST`` that timed out may have created the
post server-side, so a blind retry risks a duplicate; timeouts are surfaced instead
of retried, and the caller recovers by re-reading ``wp_post_id``.

The Application Password is never logged and never interpolated into an exception.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, Self

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tag_cache import WPTagCache
from app.services.wp_media_service import build_basic_auth_header, slugify

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "CATEGORIES_ENDPOINT",
    "MAX_ATTEMPTS",
    "POSTS_ENDPOINT",
    "RANK_MATH_ROBOTS_DEFAULT",
    "TAGS_ENDPOINT",
    "PublishedPost",
    "TagResolution",
    "WPPublishAuthError",
    "WPPublishError",
    "WPPublishNotConfiguredError",
    "WPPublishRateLimitError",
    "WPPublishResponseError",
    "WPPublisher",
    "build_rank_math_meta",
    "close_wp_publisher",
    "get_wp_publisher",
    "publish_article",
]

POSTS_ENDPOINT: Final[str] = "/wp-json/wp/v2/posts"
TAGS_ENDPOINT: Final[str] = "/wp-json/wp/v2/tags"
CATEGORIES_ENDPOINT: Final[str] = "/wp-json/wp/v2/categories"
RANK_MATH_UPDATE_META_ENDPOINT: Final[str] = "/wp-json/rankmath/v1/updateMeta"

MAX_ATTEMPTS: Final[int] = 3
INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
BACKOFF_MULTIPLIER: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 20.0

REQUEST_TIMEOUT_SECONDS: Final[float] = 60.0

#: Rank Math robots directives. ``index,follow`` is correct for original editorial
#: content and is what the ROADMAP specifies; it is stored as a list because Rank
#: Math persists the field as a serialised array.
RANK_MATH_ROBOTS_DEFAULT: Final[tuple[str, ...]] = ("index", "follow")

#: Rank Math truncates these in the SERP preview at roughly these widths, and the
#: content engine already enforces the same limits, so this is a backstop for a
#: manually edited article rather than the primary check.
MAX_RANK_MATH_TITLE_CHARS: Final[int] = 70
MAX_RANK_MATH_DESCRIPTION_CHARS: Final[int] = 160

#: Tags per post. WordPress accepts more, but a post with thirty tags reads as
#: keyword stuffing to a search engine and creates a thicket of near-empty archive
#: pages.
MAX_TAGS_PER_POST: Final[int] = 8

#: WordPress rejects a slug longer than its column, and a long slug carries no
#: additional ranking value.
MAX_SLUG_CHARS: Final[int] = 190


class WPPublishError(RuntimeError):
    """Base class for every failure surfaced by this module."""


class WPPublishNotConfiguredError(WPPublishError):
    """WordPress credentials or base URL are missing."""


class WPPublishAuthError(WPPublishError):
    """WordPress rejected the Application Password (401/403).

    Never retried: the credential is wrong, or the user lacks ``publish_posts``.
    """


class WPPublishRateLimitError(WPPublishError):
    """The host rate-limited the request after the retry budget."""


class WPPublishResponseError(WPPublishError):
    """WordPress answered with an error or an unusable payload."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class TagResolution:
    """Outcome of resolving one batch of tag names to WordPress term ids.

    The three counters exist so the dashboard can show whether the cache is
    earning its keep: a run that reports mostly ``created`` on its tenth pass over
    the same topic means the cache is not being written.
    """

    tag_ids: tuple[int, ...] = ()
    from_cache: int = 0
    from_wordpress: int = 0
    created: int = 0

    @property
    def total(self) -> int:
        return len(self.tag_ids)


@dataclass(frozen=True, slots=True)
class PublishedPost:
    """A created or updated WordPress post."""

    post_id: int
    link: str
    slug: str
    status: str
    tag_ids: tuple[int, ...] = ()
    category_ids: tuple[int, ...] = ()
    featured_media: int | None = None
    updated: bool = False
    #: Non-fatal problems: dropped Rank Math meta, a tag that could not be
    #: created. The post exists; these are recorded for an operator.
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Render the fields the worker persists and the dashboard displays."""
        return {
            "post_id": self.post_id,
            "link": self.link,
            "slug": self.slug,
            "status": self.status,
            "tag_ids": list(self.tag_ids),
            "category_ids": list(self.category_ids),
            "featured_media": self.featured_media,
            "updated": self.updated,
            "warnings": list(self.warnings),
        }


def build_rank_math_meta(
    *,
    seo_title: str,
    seo_description: str,
    focus_keyword: str = "",
    keywords: Sequence[str] = (),
    robots: Sequence[str] = RANK_MATH_ROBOTS_DEFAULT,
) -> dict[str, Any]:
    """Build the Rank Math meta payload for a post.

    ``rank_math_focus_keyword`` takes the *first* keyword when none is given
    explicitly: Rank Math scores a single primary phrase, and passing the whole
    comma-separated list would dilute the score it computes.

    Empty fields are omitted rather than sent blank. Rank Math treats an empty
    ``rank_math_title`` as "no override" and falls back to the post title, which is
    the correct behaviour; sending an empty string instead writes an override that
    renders as a blank SERP title.
    """
    meta: dict[str, Any] = {}

    title = " ".join(seo_title.split())[:MAX_RANK_MATH_TITLE_CHARS].strip()
    if title:
        meta["rank_math_title"] = title

    description = " ".join(seo_description.split())[
        :MAX_RANK_MATH_DESCRIPTION_CHARS
    ].strip()
    if description:
        meta["rank_math_description"] = description

    primary = focus_keyword.strip()
    if not primary:
        for candidate in keywords:
            if candidate and candidate.strip():
                primary = candidate.strip()
                break
    if primary:
        meta["rank_math_focus_keyword"] = primary.lower()

    directives = [item.strip() for item in robots if item and item.strip()]
    if directives:
        meta["rank_math_robots"] = directives

    return meta


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, so a batch does not retry in lockstep."""
    base = min(
        INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1)),
        MAX_BACKOFF_SECONDS,
    )
    return base * (0.75 + random.random() * 0.5)  # noqa: S311 - jitter, not crypto


def _build_post_body(
    *,
    title: str,
    content_html: str,
    status: str,
    slug: str,
    excerpt: str,
    category_ids: Sequence[int],
    tag_ids: Sequence[int],
    featured_media: int | None,
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the REST payload for one post.

    Optional keys are omitted rather than sent empty. WordPress treats a present
    ``featured_media: 0`` as "remove the featured image" and an empty ``categories``
    array as "clear the categories", so sending placeholders on an *update* would
    silently strip what a previous publish set.
    """
    body: dict[str, Any] = {
        "title": title,
        "content": content_html,
        "status": status,
        "slug": slugify(slug or title, max_chars=MAX_SLUG_CHARS) or "material",
    }
    if excerpt:
        body["excerpt"] = excerpt
    if category_ids:
        body["categories"] = list(category_ids)
    if tag_ids:
        body["tags"] = list(tag_ids)
    if featured_media is not None:
        body["featured_media"] = featured_media
    if meta:
        body["meta"] = dict(meta)
    return body


def _normalise_tag(name: str) -> str:
    """Collapse a tag to its comparison form.

    Lowercased and whitespace-collapsed, matching the ``lower(tag_name)``
    functional index. "Метро Москвы" and "метро  москвы" are one tag, and treating
    them as two produces duplicate WordPress terms and split archive pages.
    """
    return " ".join(name.split()).lower()


class WPPublisher:
    """Async WordPress REST client for posts and their taxonomy."""

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
            raise WPPublishNotConfiguredError(
                "WP_BASE_URL is not set: publishing is unavailable"
            )
        if not self._auth_header:
            raise WPPublishNotConfiguredError(
                "WP_APP_USER and WP_APP_PASSWORD are required: the REST API "
                "authenticates with an Application Password"
            )
        if not self._base_url.startswith("https://") and settings.is_production:
            raise WPPublishNotConfiguredError(
                "WP_BASE_URL must use HTTPS in production: an Application "
                "Password sent over HTTP is exposed in transit"
            )
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                    headers={
                        "Authorization": self._auth_header,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "SvodkaPress/1.0",
                    },
                    transport=self._transport,
                    follow_redirects=True,
                )
            return self._client

    def _status_error(self, response: httpx.Response, action: str) -> WPPublishError:
        """Build a typed error from a non-2xx response, without leaking secrets."""
        detail = response.text[:300].replace("\n", " ").strip() or "no body"
        if response.status_code in (
            httpx.codes.UNAUTHORIZED,
            httpx.codes.FORBIDDEN,
        ):
            return WPPublishAuthError(
                f"WordPress rejected the Application Password on {action} "
                f"(HTTP {response.status_code}). Confirm WP_APP_USER and that the "
                "user has the publish_posts capability."
            )
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            return WPPublishRateLimitError(f"{action} was rate-limited: {detail}")
        return WPPublishResponseError(
            f"{action} failed with HTTP {response.status_code}: {detail}",
            status_code=response.status_code,
        )

    def _decode(self, response: httpx.Response, action: str) -> Any:
        """Decode a WordPress JSON body, which may be an object or a list."""
        try:
            return response.json()
        except ValueError as exc:
            raise WPPublishResponseError(
                f"{action} returned a non-JSON body "
                f"(HTTP {response.status_code}, {len(response.content)} bytes). "
                "A WordPress PHP error or an HTML login page is the usual cause."
            ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Issue one REST request, retrying only what is safe to retry.

        A timeout on a write is **not** retried: WordPress may have created the
        post with only the response lost, and a second attempt would publish a
        duplicate. Read timeouts are equally not retried because the caller can
        re-read cheaply, and treating the two differently would add a branch whose
        only effect is to make write duplication possible.
        """
        client = await self._ensure_client()
        last_error: WPPublishError = WPPublishResponseError(
            f"{path} was never attempted"
        )

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.request(
                    method,
                    path,
                    json=None if json_body is None else dict(json_body),
                    params=None if params is None else dict(params),
                )
            except httpx.TimeoutException as exc:
                raise WPPublishResponseError(
                    f"{method} {path} timed out after "
                    f"{REQUEST_TIMEOUT_SECONDS:.0f}s; it may have completed "
                    "server-side, so it is not retried automatically to avoid a "
                    "duplicate post"
                ) from exc
            except httpx.HTTPError as exc:
                last_error = WPPublishResponseError(
                    f"{method} {path} transport failure: {type(exc).__name__}"
                )
            else:
                if response.status_code < httpx.codes.BAD_REQUEST:
                    return self._decode(response, f"{method} {path}")
                last_error = self._status_error(response, f"{method} {path}")
                if not isinstance(last_error, WPPublishRateLimitError):
                    raise last_error

            if attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(_backoff_delay(attempt))

        raise last_error

    # ------------------------------------------------------------------- tags
    async def _cached_tag_ids(
        self, session: AsyncSession, names: Sequence[str]
    ) -> dict[str, int]:
        """Read known term ids for these tags from ``wp_tags_cache``.

        One query for the whole batch, matched on ``lower(tag_name)`` so the
        functional unique index is used rather than a sequential scan.
        """
        if not names:
            return {}
        result = await session.execute(
            select(WPTagCache.tag_name, WPTagCache.wp_id).where(
                func.lower(WPTagCache.tag_name).in_(list(names))
            )
        )
        return {_normalise_tag(row[0]): row[1] for row in result.all()}

    async def _remember_tag(
        self, session: AsyncSession, name: str, wp_id: int
    ) -> None:
        """Persist one resolved tag, tolerating a concurrent writer.

        A ``SAVEPOINT`` isolates the insert: without it, a unique-index violation
        would poison the outer transaction and roll back the whole publish, losing
        an article over a cache write that was already satisfied by another worker.
        """
        try:
            async with session.begin_nested():
                session.add(WPTagCache(tag_name=name, wp_id=wp_id))
        except IntegrityError:
            # Another worker cached the same term first. Its row is as good as
            # ours, so this is expected operation rather than an error.
            logger.debug("Tag %r was already cached by another worker", name)

    async def _find_wp_tag(self, name: str) -> int | None:
        """Search WordPress for an existing term by name.

        ``search`` is a substring match, so the results are filtered by exact
        normalised name: searching "метро" would otherwise return "метро москвы"
        and attach the wrong term.
        """
        payload = await self._request(
            "GET", TAGS_ENDPOINT, params={"search": name, "per_page": 20}
        )
        if not isinstance(payload, list):
            return None
        target = _normalise_tag(name)
        for entry in payload:
            if not isinstance(entry, Mapping):
                continue
            found = entry.get("name")
            term_id = entry.get("id")
            if (
                isinstance(found, str)
                and isinstance(term_id, int)
                and _normalise_tag(found) == target
            ):
                return term_id
        return None

    async def _create_wp_tag(self, name: str) -> int | None:
        """Create a WordPress term, resolving a lost race by re-reading.

        WordPress answers a duplicate slug with ``term_exists`` and includes the
        existing id, so a concurrent creation is recovered from the error body
        rather than failing the publish.
        """
        try:
            payload = await self._request(
                "POST", TAGS_ENDPOINT, json_body={"name": name}
            )
        except WPPublishResponseError as exc:
            if exc.status_code == httpx.codes.BAD_REQUEST:
                existing = await self._find_wp_tag(name)
                if existing is not None:
                    return existing
            raise
        if isinstance(payload, Mapping):
            term_id = payload.get("id")
            if isinstance(term_id, int):
                return term_id
        return None

    async def resolve_tags(
        self, session: AsyncSession, names: Sequence[str]
    ) -> TagResolution:
        """Resolve tag names to WordPress term ids, using the cache first.

        Three tiers, cheapest first: the local cache (one query for the batch),
        a WordPress search, then creation. Every id learned from WordPress is
        written back to the cache, which is what makes the second run over the same
        topic free.

        Duplicates within the input collapse to one id, so ``["Метро", "метро"]``
        does not attach the same term twice.
        """
        seen: dict[str, None] = {}
        for name in names:
            normalised = _normalise_tag(name)
            if normalised:
                seen.setdefault(normalised, None)
        wanted = list(seen)[:MAX_TAGS_PER_POST]
        if not wanted:
            return TagResolution()

        cached = await self._cached_tag_ids(session, wanted)
        tag_ids: list[int] = []
        from_cache = from_wordpress = created = 0

        for name in wanted:
            hit = cached.get(name)
            if hit is not None:
                tag_ids.append(hit)
                from_cache += 1
                continue

            found = await self._find_wp_tag(name)
            if found is not None:
                tag_ids.append(found)
                from_wordpress += 1
                await self._remember_tag(session, name, found)
                continue

            made = await self._create_wp_tag(name)
            if made is None:
                logger.warning("Could not resolve or create the tag %r", name)
                continue
            tag_ids.append(made)
            created += 1
            await self._remember_tag(session, name, made)

        logger.info(
            "Resolved %d tag(s): %d cached, %d found in WordPress, %d created",
            len(tag_ids),
            from_cache,
            from_wordpress,
            created,
        )
        return TagResolution(
            tag_ids=tuple(dict.fromkeys(tag_ids)),
            from_cache=from_cache,
            from_wordpress=from_wordpress,
            created=created,
        )

    # -------------------------------------------------------------- categories
    async def resolve_category(self, slug: str) -> int | None:
        """Resolve a category slug to its term id, creating it when absent.

        Categories are matched on **slug**, not name: the slug is what the content
        engine stores in ``articles.category`` and what appears in the permalink,
        so matching on a display name would attach the wrong term whenever an
        editor renamed one in the WordPress admin.
        """
        target = slug.strip().lower()
        if not target:
            return None
        payload = await self._request(
            "GET", CATEGORIES_ENDPOINT, params={"slug": target, "per_page": 10}
        )
        if isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, Mapping):
                    term_id = entry.get("id")
                    if isinstance(term_id, int):
                        return term_id
        created = await self._request(
            "POST",
            CATEGORIES_ENDPOINT,
            json_body={"name": target.replace("-", " ").title(), "slug": target},
        )
        if isinstance(created, Mapping):
            term_id = created.get("id")
            if isinstance(term_id, int):
                return term_id
        return None

    # -------------------------------------------------------------------- post
    def _verify_meta(
        self, payload: Mapping[str, Any], expected: Mapping[str, Any]
    ) -> list[str]:
        """Check whether the Rank Math fields survived the write.

        The REST API silently discards ``meta`` keys that are not registered with
        ``show_in_rest``, answering 200 with the post created and the SEO fields
        absent. Without this check that failure is invisible until someone looks at
        a SERP snippet weeks later.
        """
        if not expected:
            return []
        returned = payload.get("meta")
        if not isinstance(returned, Mapping):
            return [
                "WordPress returned no meta object: the Rank Math fields were not "
                "stored. Register them with show_in_rest or install the Rank Math "
                "REST extension."
            ]
        missing = [key for key in expected if not returned.get(key)]
        if missing:
            return [
                "WordPress dropped the Rank Math field(s) "
                f"{', '.join(sorted(missing))}: they are not registered for REST "
                "writes, so the SEO metadata was not saved."
            ]
        return []

    async def _update_rank_math_meta(
        self, post_id: int, meta: Mapping[str, Any]
    ) -> bool:
        """Save Rank Math metadata via Rank Math's dedicated REST API endpoint."""
        if not meta:
            return True
        payload = {
            "objectType": "post",
            "objectID": post_id,
            "meta": dict(meta),
        }
        try:
            res = await self._request(
                "POST", RANK_MATH_UPDATE_META_ENDPOINT, json_body=payload
            )
            if isinstance(res, Mapping) and res.get("slug") is not None:
                logger.info(
                    "Rank Math SEO meta updated successfully for post %d via %s",
                    post_id,
                    RANK_MATH_UPDATE_META_ENDPOINT,
                )
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Rank Math REST meta update failed for post %d: %s", post_id, exc
            )
        return False

    async def find_post_by_slug(self, slug: str) -> dict[str, Any] | None:
        """Find an existing post by slug across all statuses."""
        target = slug.strip().lower()
        if not target:
            return None
        try:
            payload = await self._request(
                "GET",
                POSTS_ENDPOINT,
                params={"slug": target, "status": "any", "per_page": 1},
            )
            if isinstance(payload, list) and payload:
                first = payload[0]
                if isinstance(first, Mapping) and isinstance(first.get("id"), int):
                    return dict(first)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to lookup post by slug %r: %s", target, exc)
        return None

    async def publish(  # noqa: PLR0912
        self,
        session: AsyncSession,
        *,
        title: str,
        content_html: str,
        slug: str = "",
        excerpt: str = "",
        category_slug: str = "",
        tags: Sequence[str] = (),
        featured_media: int | None = None,
        seo_title: str = "",
        seo_description: str = "",
        focus_keyword: str = "",
        status: str = "publish",
        existing_post_id: int | None = None,
    ) -> PublishedPost:
        """Create or update one WordPress post.

        When ``existing_post_id`` is given the post is updated in place; otherwise a
        new one is created. That is the idempotency guarantee: a retried publish
        must never produce a second live copy of the same article.

        Taxonomy and metadata are resolved before the write so the post is created
        complete, in one request. Creating it and then patching the tags would leave
        a live, untagged post if the second call failed.
        """
        # Idempotency check: if existing_post_id is not given, verify whether a post
        # with this exact slug already exists in WordPress. If so, update that post
        # instead of creating a duplicate with a "-2" suffix.
        if existing_post_id is None and slug:
            existing_post = await self.find_post_by_slug(slug)
            if existing_post is not None:
                found_id = existing_post.get("id")
                if isinstance(found_id, int):
                    existing_post_id = found_id
                    logger.info(
                        "Post with slug %r already exists as WP ID %d; "
                        "updating in place to prevent -2 duplicate",
                        slug,
                        existing_post_id,
                    )

        warnings: list[str] = []

        category_ids: list[int] = []
        if category_slug:
            try:
                category_id = await self.resolve_category(category_slug)
            except WPPublishError as exc:
                warnings.append(f"category {category_slug!r} unresolved: {exc}")
            else:
                if category_id is not None:
                    category_ids.append(category_id)

        resolution = TagResolution()
        if tags:
            try:
                resolution = await self.resolve_tags(session, tags)
            except WPPublishError as exc:
                # A post without tags is publishable; losing the article because a
                # term lookup failed would be the wrong trade.
                warnings.append(f"tags unresolved: {exc}")

        meta = build_rank_math_meta(
            seo_title=seo_title or title,
            seo_description=seo_description,
            focus_keyword=focus_keyword,
            keywords=tags,
        )

        body = _build_post_body(
            title=title,
            content_html=content_html,
            status=status,
            slug=slug,
            excerpt=excerpt,
            category_ids=category_ids,
            tag_ids=resolution.tag_ids,
            featured_media=featured_media,
            meta=meta,
        )

        updating = existing_post_id is not None
        path = f"{POSTS_ENDPOINT}/{existing_post_id}" if updating else POSTS_ENDPOINT
        logger.info(
            "%s WordPress post %s(%d tag(s), featured=%s)",
            "Updating" if updating else "Creating",
            f"{existing_post_id} " if updating else "",
            len(resolution.tag_ids),
            featured_media,
        )
        payload = await self._request("POST", path, json_body=body)
        if not isinstance(payload, Mapping):
            raise WPPublishResponseError(
                f"expected a post object, got {type(payload).__name__}"
            )

        post_id = payload.get("id")
        if not isinstance(post_id, int):
            raise WPPublishResponseError(
                "WordPress returned no post id; the post cannot be referenced"
            )
        rank_math_saved = False
        if meta:
            rank_math_saved = await self._update_rank_math_meta(post_id, meta)
        if not rank_math_saved:
            warnings.extend(self._verify_meta(payload, meta))

        published = PublishedPost(
            post_id=post_id,
            link=str(payload.get("link") or ""),
            slug=str(payload.get("slug") or body["slug"]),
            status=str(payload.get("status") or status),
            tag_ids=resolution.tag_ids,
            category_ids=tuple(category_ids),
            featured_media=featured_media,
            updated=updating,
            warnings=tuple(warnings),
        )
        logger.info(
            "WordPress post %d is %s at %s",
            published.post_id,
            published.status,
            published.link or "(no link returned)",
        )
        return published

    async def update_post_seo_snippet(
        self,
        post_id: int,
        *,
        seo_title: str = "",
        seo_description: str = "",
        focus_keyword: str = "",
        post_title: str = "",
    ) -> dict[str, Any]:
        """Update Rank Math metadata and post title for an existing WordPress post."""
        meta = build_rank_math_meta(
            seo_title=seo_title,
            seo_description=seo_description,
            focus_keyword=focus_keyword,
        )
        body: dict[str, Any] = {"meta": meta}
        if post_title.strip():
            body["title"] = post_title.strip()
        response = await self._request(
            "POST", f"{POSTS_ENDPOINT}/{post_id}", json_body=body
        )
        if meta:
            await self._update_rank_math_meta(post_id, meta)
        if isinstance(response, Mapping):
            return dict(response)
        return {}


_shared_publisher: WPPublisher | None = None


def get_wp_publisher() -> WPPublisher:
    """Return the process-wide publisher, creating it on first call."""
    global _shared_publisher  # noqa: PLW0603 - module-level singleton accessor
    if _shared_publisher is None:
        _shared_publisher = WPPublisher()
    return _shared_publisher


async def close_wp_publisher() -> None:
    """Release the shared publisher's connections. Call on shutdown."""
    global _shared_publisher  # noqa: PLW0603 - module-level singleton accessor
    publisher = _shared_publisher
    _shared_publisher = None
    if publisher is not None:
        await publisher.close()


async def publish_article(
    session: AsyncSession,
    *,
    title: str,
    content_html: str,
    publisher: WPPublisher | None = None,
    **options: Any,
) -> PublishedPost:
    """Publish one article to WordPress.

    Raises :class:`WPPublishNotConfiguredError` without credentials,
    :class:`WPPublishAuthError` on a rejected Application Password,
    :class:`WPPublishRateLimitError` after a sustained 429, and
    :class:`WPPublishResponseError` for any other failure.
    """
    engine = get_wp_publisher() if publisher is None else publisher
    return await engine.publish(
        session, title=title, content_html=content_html, **options
    )
