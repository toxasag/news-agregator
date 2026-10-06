# ruff: noqa: PLR0912, PLR0915
"""Backfill cover photos for published articles that have wp_image_id IS NULL."""

from __future__ import annotations

import asyncio
import logging
from typing import Final

import httpx
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.article import Article
from app.models.cluster import NewsCluster
from app.models.raw_news import RawNews
from app.models.source import Source
from app.services.telegram_mtproto import MTProtoIngestor
from app.services.watermark_detector import crop_watermarks_async, detect_watermarks
from app.services.wp_connection import load_wp_connection
from app.services.wp_media_service import (
    build_basic_auth_header,
    upload_article_image,
)
from app.workers.content_worker import _resolve_tg_embed_photo

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger: Final[logging.Logger] = logging.getLogger("backfill_covers")


async def main() -> None:
    async with AsyncSessionLocal() as session:
        conn = await load_wp_connection(session)
        auth = (
            build_basic_auth_header(conn.wp_app_user, conn.wp_app_password)
            if conn.has_rest_credentials
            else None
        )

        stmt = (
            select(Article)
            .where(Article.wp_image_id.is_(None))
            .order_by(Article.id.desc())
        )
        articles = list((await session.execute(stmt)).scalars().all())
        logger.info("Found %d articles without covers", len(articles))

        fixed_count = 0
        failed_count = 0

        async with (
            httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http_client,
            MTProtoIngestor(session) as ingestor,
        ):
            for art in articles:
                logger.info("--- Processing article %d: %s ---", art.id, art.title[:50])
                photo_bytes: bytes = b""
                filename = f"cover_{art.url_slug or art.id}.jpg"

                # 1. Check cluster's raw_news
                posts_stmt = (
                    select(RawNews)
                    .where(RawNews.cluster_id == art.cluster_id)
                    .order_by(RawNews.created_at)
                )
                posts = list((await session.execute(posts_stmt)).scalars().all())

                for p in posts:
                    # 1a. Try public TG embed if post_url exists
                    if p.post_url and p.post_url.startswith(
                        ("http://t.me/", "https://t.me/")
                    ):
                        try:
                            cdn_url = await _resolve_tg_embed_photo(
                                http_client, p.post_url
                            )
                            if cdn_url:
                                resp = await http_client.get(cdn_url)
                                if resp.status_code == 200 and resp.content:
                                    photo_bytes = resp.content
                                    logger.info(
                                        "  Obtained photo from public TG embed: %d bytes",
                                        len(photo_bytes),
                                    )
                                    break
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("Embed scrape failed: %s", exc)

                    # 1b. Try MTProto direct download from channel
                    if not photo_bytes and p.media_data:
                        for item in p.media_data:
                            if item.get("type") == "photo" and item.get(
                                "original_post_id"
                            ):
                                try:
                                    source = await session.get(Source, p.source_id)
                                    if source and source.identifier:
                                        data = await ingestor.download_photo(
                                            source.identifier,
                                            int(item["original_post_id"]),
                                            limit_bytes=5 * 1024 * 1024,
                                        )
                                        if data:
                                            photo_bytes = data
                                            logger.info(
                                                "  Obtained photo from %s: %d bytes",
                                                source.identifier,
                                                len(data),
                                            )
                                            break
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug("MTProto download failed: %s", exc)
                        if photo_bytes:
                            break

                # 2. If no TG photo, check cluster enrichment_data
                if not photo_bytes and art.cluster_id:
                    cluster = await session.get(NewsCluster, art.cluster_id)
                    if cluster and cluster.enrichment_data:
                        web_imgs = cluster.enrichment_data.get("web_images") or []
                        sel = cluster.enrichment_data.get("selected_cover_url")
                        candidates = []
                        if sel and isinstance(sel, str) and sel.startswith("http"):
                            candidates.append(sel)
                        for wi in web_imgs:
                            u = wi.get("url") if isinstance(wi, dict) else wi
                            if isinstance(u, str) and u.startswith("http"):
                                candidates.append(u)
                        for u in candidates[:3]:
                            try:
                                resp = await http_client.get(u)
                                if resp.status_code == 200 and resp.content:
                                    photo_bytes = resp.content
                                    logger.info(
                                        "  Obtained photo from web enrichment: %d bytes",
                                        len(photo_bytes),
                                    )
                                    break
                            except Exception as exc:  # noqa: BLE001
                                logger.debug("Web image download failed: %s", exc)

                if not photo_bytes:
                    logger.warning(
                        "  Could not find any source photo for article %d", art.id
                    )
                    failed_count += 1
                    continue

                # Watermark detection & crop
                try:
                    verdict = await detect_watermarks(photo_bytes)
                    if verdict.get("has_watermark") and verdict.get("is_croppable"):
                        cropped = await crop_watermarks_async(photo_bytes)
                        if cropped:
                            photo_bytes = cropped
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Watermark check ignored: %s", exc)

                # Upload to WordPress
                try:
                    media = await upload_article_image(
                        photo_bytes,
                        filename=filename,
                        article_title=art.title,
                        keywords=art.seo_keywords,
                        caption=art.seo_description or "",
                        content_type="image/jpeg",
                    )
                    art.wp_image_id = media.media_id
                    logger.info(
                        "  Uploaded to WP: media_id=%d (%s)",
                        media.media_id,
                        media.source_url,
                    )

                    # Update WordPress post's featured_media
                    if art.wp_post_id and auth and conn.wp_base_url:
                        try:
                            wp_resp = await http_client.post(
                                f"{conn.wp_base_url.rstrip('/')}/wp-json/wp/v2/posts/{art.wp_post_id}",
                                headers={"Authorization": auth},
                                json={"featured_media": media.media_id},
                            )
                            logger.info(
                                "  Updated WP post %d featured_media status: %d",
                                art.wp_post_id,
                                wp_resp.status_code,
                            )
                        except Exception as wp_exc:  # noqa: BLE001
                            logger.warning(
                                "  Failed to update WP post %d: %s",
                                art.wp_post_id,
                                wp_exc,
                            )

                    await session.commit()
                    fixed_count += 1
                except Exception as exc:
                    await session.rollback()
                    logger.exception(
                        "  Upload/update failed for article %d: %s", art.id, exc
                    )
                    failed_count += 1

        logger.info(
            "Backfill finished: %d fixed, %d failed out of %d total",
            fixed_count,
            failed_count,
            len(articles),
        )


if __name__ == "__main__":
    asyncio.run(main())
