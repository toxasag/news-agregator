"""Script to publish all existing drafts in Dzen Studio.

Iterates through draft publications in Dzen Studio (state=draft),
opens each draft editor, clicks publish, resolves VK SmartCaptcha,
and transitions drafts to live published articles.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from playwright.async_api import Page

from app.database import AsyncSessionLocal
from app.services.dzen_publisher import (
    DzenBrowserPublisher,
    load_dzen_session_config,
    normalize_dzen_storage_state,
)
from app.services.web_scraper import get_browser_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("publish_dzen_drafts")


async def get_draft_links(page: Page, drafts_url: str) -> list[str]:
    """Extract all draft edit links from the publications?state=draft page."""
    logger.info("Navigating to drafts list: %s", drafts_url)
    await page.goto(drafts_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    # Scroll down a few times to lazy load drafts
    for _ in range(5):
        await page.evaluate("window.scrollBy(0, 1000)")
        await page.wait_for_timeout(1000)

    # Find edit links or cards
    links = await page.evaluate("""() => {
        const sel = 'a[href$="/edit"], a[class*="publication-preview"]';
        const anchors = Array.from(document.querySelectorAll(sel));
        const hrefs = [];
        for (const a of anchors) {
            const h = a.getAttribute('href');
            if (h && h.endsWith('/edit') && h.includes('/profile/editor/id/')) {
                const full = h.startsWith('http') ? h : 'https://dzen.ru' + h;
                if (!hrefs.includes(full)) {
                    hrefs.push(full);
                }
            }
        }
        return hrefs;
    }""")
    logger.info("Found %d draft link(s) on current view", len(links))
    return links


async def publish_one_draft(
    publisher: DzenBrowserPublisher, page: Page, edit_url: str
) -> str | None:
    """Open draft in editor and trigger publishing."""
    logger.info("Opening draft editor: %s", edit_url)
    try:
        await page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3500)
        final_url = await publisher._trigger_publish(page)
        logger.info("Publish result for %s -> %s", edit_url, final_url)
        return final_url
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to publish draft %s: %s", edit_url, exc)
        return None


async def main() -> None:
    max_to_publish = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    logger.info("Starting Dzen drafts publisher (max=%d)...", max_to_publish)

    async with AsyncSessionLocal() as session:
        cfg = await load_dzen_session_config(session)
        if not cfg.is_active or not cfg.has_session:
            logger.error("Dzen session is not active or configured!")
            return

        publisher = DzenBrowserPublisher(cfg)
        clean_state = normalize_dzen_storage_state(cfg.storage_state)
        channel_name = cfg.channel_name.strip() or "svodka_press"
        drafts_url = (
            f"https://dzen.ru/profile/editor/{channel_name}/publications?state=draft"
        )

        pool = get_browser_pool()
        total_published = 0
        total_errors = 0

        async with pool.page(storage_state=clean_state, block_resources=False) as page:
            while total_published < max_to_publish:
                draft_links = await get_draft_links(page, drafts_url)
                if not draft_links:
                    logger.info("No more draft links found on page. Exiting.")
                    break

                published_in_batch = 0
                for _i, link in enumerate(draft_links, start=1):
                    if total_published >= max_to_publish:
                        break
                    logger.info(
                        "[%d/%d] Publishing draft: %s",
                        total_published + 1,
                        max_to_publish,
                        link,
                    )
                    pub_url = await publish_one_draft(publisher, page, link)
                    if pub_url and "/edit" not in pub_url:
                        total_published += 1
                        published_in_batch += 1
                    else:
                        total_errors += 1
                    await page.wait_for_timeout(3000)

                if published_in_batch == 0:
                    logger.warning("Could not publish any drafts in current batch.")
                    break

        logger.info(
            "Batch publishing finished! Total published: %d, Total errors: %d",
            total_published,
            total_errors,
        )


if __name__ == "__main__":
    asyncio.run(main())
