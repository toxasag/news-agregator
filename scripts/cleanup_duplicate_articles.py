# ruff: noqa: RUF001
"""Retrospective deduplication script for SvodkaPress articles.

Scans published articles over the last N days (default: 4 days).
Groups near-duplicate stories using title word stems and pgvector cosine similarity.
Keeps the earliest/original article intact.
Marks duplicate articles as SKIPPED_DUPLICATE in the database and moves
their remote WordPress posts to 'trash' via the WordPress REST API.

Usage:
    python scripts/cleanup_duplicate_articles.py --dry-run
    python scripts/cleanup_duplicate_articles.py --days 4
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.database import AsyncSessionLocal
from app.models.article import Article
from app.models.enums import ArticleStatus
from app.services.wp_connection import load_wp_connection
from app.services.wp_media_service import build_basic_auth_header

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cleanup_duplicates")


_CITY_SYNONYMS: Final[dict[str, str]] = {
    "питер": "петербург",
    "спб": "петербург",
    "мск": "москв",
}


def _extract_title_stems(title: str) -> set[str]:
    """Extract lowercased normalized word stems (>= 3 chars) from title."""
    words = re.findall(r"[a-z\u0430-\u044f\u04510-9]{3,}", (title or "").lower())
    stems: set[str] = set()
    for w in words:
        if w in _CITY_SYNONYMS:
            stems.add(_CITY_SYNONYMS[w])
            continue
        # 1. Strip reflexive & verbal suffixes
        stem = re.sub(
            r"(?:вшись|лись|лась|лось|лся|ться|тся|ете|ите|ут|ют|ат|ят|ет|ит|л|ла|ло|ли|ть|ти)$",
            "",
            w,
        )
        # 2. Strip case & adjectival endings
        stem = re.sub(
            r"(?:ами|ями|ого|его|ому|ему|ыми|ими|ах|ях|ов|ев|ей|ий|ый|ой|ая|яя|ое|ее|ые|ие|ия|ья|ам|ям|ом|ем|а|я|о|е|у|ю|ы|и|й)$",
            "",
            stem,
        )
        # 3. Strip word-building derivative suffixes for adjectives/nouns
        if len(stem) >= 5:
            stem = re.sub(
                r"(?:ск|енн|анн|овн|евн|тель|льн|оват|еват|ин|ов|ев|н)$",
                "",
                stem,
            )
        norm = _CITY_SYNONYMS.get(stem, stem)
        if len(norm) >= 3:
            stems.add(norm)
        else:
            stems.add(w)
    return stems


COMMON_STOP_STEMS: frozenset[str] = frozenset(
    {
        "росс", "миноборон", "показал", "заявил", "сообщ", "рассказал",
        "раскрыл", "планир", "детал", "кадр", "уровен", "старт", "сегодн",
        "врем", "нов", "цен", "взлетел", "стал", "москв", "подмосковь",
        "происшеств", "инцидент", "назвал", "приговор", "погиб", "число",
        "главн", "оцен", "горо", "стра", "человек", "рубл", "млрд", "милл",
    }
)


async def _resolve_wp_client() -> tuple[str, str] | None:
    """Resolve WordPress base URL and auth header from settings or database."""
    async with AsyncSessionLocal() as session:
        cfg = await load_wp_connection(session)
        if cfg.has_rest_credentials:
            return cfg.wp_base_url, build_basic_auth_header(
                cfg.wp_app_user, cfg.wp_app_password
            )
    return None


async def trash_wp_post(
    client: httpx.AsyncClient, base_url: str, auth_header: str, wp_post_id: int
) -> bool:
    """Move WordPress post to trash via REST API."""
    url = f"{base_url}/wp-json/wp/v2/posts/{wp_post_id}"
    try:
        resp = await client.delete(
            url,
            headers={"Authorization": auth_header, "Accept": "application/json"},
            timeout=15.0,
        )
        if resp.status_code in (200, 201, 204):
            logger.info("WordPress post #%s moved to trash", wp_post_id)
            return True
        logger.warning(
            "WordPress trash failed for post #%s (HTTP %s): %s",
            wp_post_id,
            resp.status_code,
            resp.text[:100],
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to trash WP post #%s: %s", wp_post_id, exc)
        return False


async def run_cleanup(  # noqa: PLR0912, PLR0915
    days: int = 4,
    dry_run: bool = True,
) -> None:
    """Perform deduplication scan and cleanup."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    logger.info("Scanning articles created after %s (lookback: %d days)...", cutoff, days)

    wp_config = await _resolve_wp_client()
    if not wp_config:
        logger.warning("WordPress credentials not found. Remote deletion skipped.")

    async with AsyncSessionLocal() as session:
        stmt = (
            select(Article)
            .options(undefer(Article.final_embedding))
            .where(
                Article.created_at >= cutoff,
                Article.status.in_(
                    [ArticleStatus.WP_PUBLISHED, ArticleStatus.DISTRIBUTED]
                ),
            )
            .order_by(Article.created_at.asc())
        )
        res = await session.execute(stmt)
        articles = list(res.scalars().all())

    logger.info("Loaded %d published articles.", len(articles))

    canonical_articles: list[tuple[Article, set[str]]] = []
    duplicates_to_remove: list[tuple[Article, Article, str]] = []

    for art in articles:
        title = art.title or ""
        stems = _extract_title_stems(title) - COMMON_STOP_STEMS

        is_dup = False
        matched_original = None
        match_reason = ""

        for orig_art, orig_stems in canonical_articles:
            # 1. Lexical title stem overlap
            if stems and orig_stems:
                intersection = stems & orig_stems
                rare_shared = {s for s in intersection if len(s) >= 6 and not s.isdigit()}
                union = stems | orig_stems
                jaccard = len(intersection) / len(union) if union else 0.0
                matches_rare = (
                    len(rare_shared) >= 1
                    and len(intersection) >= 2
                    and jaccard >= 0.25
                )
                if len(intersection) >= 3 or jaccard >= 0.40 or matches_rare:
                    is_dup = True
                    matched_original = orig_art
                    m_words = list(intersection)[:4]
                    match_reason = f"lexical stem overlap: {m_words} (j={jaccard:.2f})"
                    break

            # 2. Vector cosine similarity if available
            if (
                art.final_embedding is not None
                and orig_art.final_embedding is not None
            ):
                try:
                    import numpy as np  # noqa: PLC0415

                    v1 = np.array(art.final_embedding, dtype=np.float32)
                    v2 = np.array(orig_art.final_embedding, dtype=np.float32)
                    norm1 = float(np.linalg.norm(v1))
                    norm2 = float(np.linalg.norm(v2))
                    if norm1 > 0 and norm2 > 0:
                        sim = float(np.dot(v1, v2) / (norm1 * norm2))
                        if sim >= 0.82:
                            is_dup = True
                            matched_original = orig_art
                            match_reason = f"semantic cosine sim: {sim:.3f}"
                            break
                except Exception as sim_exc:  # noqa: BLE001
                    logger.debug("Cosine similarity calc skipped: %s", sim_exc)

        if is_dup and matched_original is not None:
            duplicates_to_remove.append((art, matched_original, match_reason))
        else:
            canonical_articles.append((art, set(stems)))

    logger.info(
        "Analysis complete: %d unique canonical articles, %d duplicates detected.",
        len(canonical_articles),
        len(duplicates_to_remove),
    )

    if dry_run:
        logger.info("=== DRY RUN MODE: No changes will be applied ===")
        for dup, orig, reason in duplicates_to_remove[:25]:
            logger.info(
                "DUPLICATE Article #%s (%s)\n  -> Duplicate of #%s (%s)\n  -> Reason: %s",
                dup.id,
                dup.title[:50],
                orig.id,
                orig.title[:50],
                reason,
            )
        if len(duplicates_to_remove) > 25:
            logger.info("... and %d more duplicates.", len(duplicates_to_remove) - 25)
        return

    logger.info("=== APPLYING CLEANUP: Trashing duplicate posts in WordPress and DB ===")
    trashed_wp_count = 0
    updated_db_count = 0

    http_client = httpx.AsyncClient() if wp_config else None
    try:
        async with AsyncSessionLocal() as session:
            for dup, _orig, _reason in duplicates_to_remove:
                db_dup = await session.get(Article, dup.id)
                if not db_dup:
                    continue

                db_dup.status = ArticleStatus.SKIPPED_DUPLICATE
                updated_db_count += 1

                if db_dup.wp_post_id and http_client and wp_config:
                    base_url, auth_header = wp_config
                    ok = await trash_wp_post(
                        http_client, base_url, auth_header, int(db_dup.wp_post_id)
                    )
                    if ok:
                        trashed_wp_count += 1
                    # Small delay to prevent hammering WordPress REST API
                    await asyncio.sleep(0.05)

            await session.commit()
    finally:
        if http_client:
            await http_client.aclose()

    logger.info(
        "Cleanup completed: %d database records updated to SKIPPED_DUPLICATE, "
        "%d WordPress posts moved to trash.",
        updated_db_count,
        trashed_wp_count,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cleanup duplicate articles")
    parser.add_argument(
        "--days", type=int, default=4, help="Number of past days to check"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply changes (default is dry-run)"
    )
    args = parser.parse_args()

    asyncio.run(run_cleanup(days=args.days, dry_run=not args.apply))
