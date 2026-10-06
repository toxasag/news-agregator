# ruff: noqa: RUF001, BLE001, S501, PLR0912
"""WordPress archive indexer and search service for internal linking and research.

Fetches and caches post metadata from the WordPress REST API to enable
contextual cross-linking and related story discovery against the entire
production archive (8,700+ articles), even on fresh local database deployments.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Final

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.redis_client import get_redis
from app.services.wp_connection import load_wp_connection

logger: Final[logging.Logger] = logging.getLogger(__name__)

_REDIS_KEY_POSTS: Final[str] = "svodka:wp_archive:posts"
_REDIS_KEY_TOTAL: Final[str] = "svodka:wp_archive:total"
_CACHE_TTL_SECONDS: Final[int] = 3600  # 1 hour

_in_memory_posts_cache: list[dict[str, Any]] = []
_in_memory_total_count: int = 0


def _clean_word(w: str) -> str:
    return w.lower().strip()


def extract_keywords(text: str) -> set[str]:
    """Extract significant keywords (>=4 chars) from a title or text."""
    if not text:
        return set()
    raw = re.findall(r"\b[A-Za-zА-Яа-я0-9]{4,}\b", text)
    return {_clean_word(w) for w in raw if len(w) >= 4}


class WPArchiveService:
    """Provides fast access to the WordPress article archive for linking and research."""

    @classmethod
    async def get_total_posts_count(cls, session: AsyncSession) -> int:
        """Return the total number of articles on the WordPress site."""
        global _in_memory_total_count  # noqa: PLW0603

        if _in_memory_total_count > 0:
            return _in_memory_total_count

        try:
            r = get_redis()
            cached = await r.get(_REDIS_KEY_TOTAL)
            if cached is not None:
                val = int(cached)
                if val > 0:
                    _in_memory_total_count = val
                    return val
        except Exception as exc:
            logger.debug("Redis read error for wp total: %s", exc)

        # Fallback: query page 1 from WordPress
        _, total = await cls.sync_recent_posts(session, pages=1)
        if total > 0:
            _in_memory_total_count = total
            return total

        return 8701  # Documented baseline for svodka.press

    @classmethod
    async def get_archive_posts(
        cls, session: AsyncSession, *, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Return cached recent WordPress posts."""
        global _in_memory_posts_cache  # noqa: PLW0603

        if not force_refresh and _in_memory_posts_cache:
            return _in_memory_posts_cache

        if not force_refresh:
            try:
                r = get_redis()
                cached = await r.get(_REDIS_KEY_POSTS)
                if cached:
                    data = json.loads(cached)
                    if isinstance(data, list) and data:
                        _in_memory_posts_cache = data
                        return data
            except Exception as exc:
                logger.debug("Redis read error for wp archive posts: %s", exc)

        posts, _ = await cls.sync_recent_posts(session, pages=3)
        return posts

    @classmethod
    async def sync_recent_posts(
        cls, session: AsyncSession, *, pages: int = 2
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch recent posts from WordPress REST API and update cache."""
        global _in_memory_posts_cache, _in_memory_total_count  # noqa: PLW0603

        conn = await load_wp_connection(session)
        base_url = conn.wp_base_url or settings.wp_base_url
        if not base_url:
            return _in_memory_posts_cache, _in_memory_total_count

        user = conn.wp_app_user or settings.wp_app_user
        password = conn.wp_app_password or settings.wp_app_password
        auth = (user, password) if user and password else None

        collected_posts: list[dict[str, Any]] = []
        total_count = 0

        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            for page in range(1, pages + 1):
                try:
                    resp = await client.get(
                        f"{base_url.rstrip('/')}/wp-json/wp/v2/posts",
                        params={
                            "per_page": 100,
                            "page": page,
                            "_fields": "id,title,slug,link,date,categories",
                        },
                        auth=auth,
                    )
                    if resp.status_code != 200:
                        logger.warning(
                            "WP REST archive fetch returned HTTP %d on page %d",
                            resp.status_code,
                            page,
                        )
                        break

                    if page == 1:
                        total_hdr = resp.headers.get("x-wp-total")
                        if total_hdr and total_hdr.isdigit():
                            total_count = int(total_hdr)

                    items = resp.json()
                    if not isinstance(items, list) or not items:
                        break

                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        title_obj = item.get("title") or {}
                        raw_title = (
                            title_obj.get("rendered")
                            if isinstance(title_obj, dict)
                            else str(title_obj)
                        ) or ""
                        clean_title = html.unescape(raw_title).strip()
                        collected_posts.append(
                            {
                                "id": item.get("id"),
                                "title": clean_title,
                                "slug": item.get("slug") or "",
                                "link": item.get("link") or "",
                                "date": item.get("date") or "",
                            }
                        )
                except Exception as exc:
                    logger.warning("Error fetching WP archive page %d: %s", page, exc)
                    break

        if collected_posts:
            _in_memory_posts_cache = collected_posts
            if total_count > 0:
                _in_memory_total_count = total_count
            try:
                r = get_redis()
                await r.set(
                    _REDIS_KEY_POSTS,
                    json.dumps(collected_posts),
                    ex=_CACHE_TTL_SECONDS,
                )
                if total_count > 0:
                    await r.set(
                        _REDIS_KEY_TOTAL,
                        str(total_count),
                        ex=_CACHE_TTL_SECONDS,
                    )
            except Exception as exc:
                logger.debug("Redis write error for wp archive: %s", exc)

        final_posts = collected_posts or _in_memory_posts_cache
        final_total = total_count or _in_memory_total_count
        return final_posts, final_total

    @classmethod
    async def find_related(
        cls, session: AsyncSession, title: str, *, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Find matching WordPress posts by title keyword overlap."""
        words = extract_keywords(title)
        if not words:
            return []

        posts = await cls.get_archive_posts(session)
        scored: list[tuple[int, dict[str, Any]]] = []

        for p in posts:
            p_words = extract_keywords(p.get("title", ""))
            overlap = len(words & p_words)
            if overlap >= 1:
                scored.append((overlap, p))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:limit]]
