"""Diagnostic script to check Bessent article, VK attachments, and YouTube/Setka sessions."""

import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.article import Article
from app.services.social_browser_publisher import load_all_social_browser_configs

async def main():
    async with AsyncSessionLocal() as session:
        # 1. Bessent / recent articles
        res = await session.execute(
            select(Article).where(Article.title.ilike("%Бессент%")).order_by(Article.id.desc()).limit(3)
        )
        articles = list(res.scalars().all())
        if not articles:
            res = await session.execute(select(Article).order_by(Article.id.desc()).limit(5))
            articles = list(res.scalars().all())
            print("Recent articles:")
        else:
            print("Found Bessent articles:")

        for a in articles:
            vk_st = (a.platform_statuses or {}).get("vk")
            wp_st = (a.platform_statuses or {}).get("wp")
            print(f"Article #{a.id}: {a.title}")
            print(f"  wp_image_id: {a.wp_image_id}")
            print(f"  wp status: {wp_st}")
            print(f"  vk status: {vk_st}")
            print(f"  cluster_id: {a.cluster_id}")

        # 2. Social browser configs
        cfgs = await load_all_social_browser_configs(session)
        for code in ("youtube", "setka", "tenchat"):
            cfg = cfgs.get(code)
            if cfg:
                cookies = (cfg.storage_state or {}).get("cookies", [])
                print(f"Config [{code}]: active={cfg.is_active}, status={cfg.status}, target={cfg.target_url}, cookies_count={len(cookies)}")

if __name__ == "__main__":
    asyncio.run(main())
