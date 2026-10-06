"""Backfill missing featured images for WordPress articles.

Finds published articles with wp_image_id IS NULL, generates editorial
cover photos via Pollinations FLUX, uploads them to WordPress media library,
links featured_media on the WordPress post, and persists wp_image_id to DB.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.database import AsyncSessionLocal
from app.models.article import Article
from app.models.enums import ArticleStatus
from app.services.image_generator import generate_cover_image
from app.services.wp_connection import get_active_wp_publisher
from app.services.wp_media_service import upload_article_image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_covers")


async def backfill_missing_covers(limit: int = 100) -> None:
    logger.info("Starting missing covers backfill (limit=%d)...", limit)

    async with AsyncSessionLocal() as session:
        # Find published articles without image, newest first
        stmt = (
            select(Article)
            .where(
                Article.status == ArticleStatus.WP_PUBLISHED,
                Article.wp_post_id.isnot(None),
                Article.wp_image_id.is_(None),
            )
            .order_by(Article.created_at.desc())
            .limit(limit)
        )
        articles = list((await session.execute(stmt)).scalars().all())
        logger.info("Found %d articles requiring covers", len(articles))

        if not articles:
            return

        wp_pub = await get_active_wp_publisher(session)
        async with wp_pub:
            client = await wp_pub._ensure_client()
            success_count = 0

            for i, art in enumerate(articles, start=1):
                logger.info(
                    "[%d/%d] Processing article %d: %s",
                    i,
                    len(articles),
                    art.id,
                    art.title[:50],
                )
                try:
                    prompt = (
                        f"{art.title}, editorial news photography, "
                        "dramatic realistic journalistic photo, 8k"
                    )
                    img_bytes = await generate_cover_image(session, prompt)
                    if not img_bytes:
                        logger.warning("Failed to generate image for article %d", art.id)
                        continue

                    slug = art.url_slug or f"article-{art.id}"
                    media = await upload_article_image(
                        img_bytes,
                        filename=f"cover_{slug}.jpg",
                        article_title=art.title,
                        keywords=list(art.seo_keywords or ()),
                        caption=art.seo_description or art.title,
                        content_type="image/jpeg",
                    )

                    # Update WP post featured_media
                    resp = await client.post(
                        f"/wp-json/wp/v2/posts/{art.wp_post_id}",
                        json={"featured_media": media.media_id},
                    )
                    if resp.status_code != 200:
                        logger.warning(
                            "WP post update returned %d for post %d",
                            resp.status_code,
                            art.wp_post_id,
                        )

                    # Save to DB
                    art.wp_image_id = media.media_id
                    art.wp_image_ids = [media.media_id]
                    if not isinstance(art.platform_statuses, dict):
                        art.platform_statuses = {}
                    art.platform_statuses.setdefault("wp", {})["cover_url"] = (
                        media.source_url
                    )
                    flag_modified(art, "platform_statuses")
                    await session.commit()

                    success_count += 1
                    logger.info(
                        "Success: article %d assigned media %d (%s)",
                        art.id,
                        media.media_id,
                        media.source_url,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to process article %d: %s", art.id, exc)

                await asyncio.sleep(1.0)

        logger.info(
            "Backfill finished! Successfully enriched %d/%d articles",
            success_count,
            len(articles),
        )


async def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    await backfill_missing_covers(limit)


if __name__ == "__main__":
    asyncio.run(main())
