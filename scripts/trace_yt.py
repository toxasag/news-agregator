import asyncio
import os
import tempfile
import httpx
from sqlalchemy import select
from app.services.social_browser_publisher import persistent_platform_page, load_all_social_browser_configs
from app.database import AsyncSessionLocal
from app.models.article import Article
from app.workers.publisher_worker import _resolve_cover_url

async def trace_publish():
    async with AsyncSessionLocal() as session:
        cfgs = await load_all_social_browser_configs(session)
        yt_cfg = cfgs.get('youtube')

        res = await session.execute(
            select(Article)
            .where(Article.wp_image_id.is_not(None), Article.status.in_(['wp_published', 'distributed']))
            .order_by(Article.id.desc())
            .limit(1)
        )
        art = res.scalar_one_or_none()
        cover_url = await _resolve_cover_url(art)
        print(f"Article #{art.id}: {art.title}")
        print(f"Cover URL: {cover_url}")

        async with persistent_platform_page(yt_cfg) as page:
            target_url = yt_cfg.target_url or "https://www.youtube.com/@svodkapress/posts"
            if not target_url.endswith("/posts"):
                target_url = f"{target_url.rstrip('/ ')}/posts"
            target_url = f"{target_url}?show_create_dialog=1"

            print(f"Navigating to {target_url}...")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3.5)

            # Check img_btn
            img_btn = page.locator(
                "button:has-text('Изображение'):visible, [aria-label*='Изображение' i]:visible"
            ).first
            img_btn_count = await img_btn.count()
            print(f"img_btn count: {img_btn_count}")
            if img_btn_count > 0:
                print(f"img_btn is_visible: {await img_btn.is_visible()}")
                await img_btn.click()
                await asyncio.sleep(2.0)

                multi_input = page.locator(
                    "ytd-backstage-multi-image-select-renderer #dropzone input[type='file'], "
                    "ytd-backstage-multi-image-select-renderer input[type='file']"
                ).first
                print(f"multi_input count: {await multi_input.count()}")

                async with httpx.AsyncClient() as client:
                    resp = await client.get(cover_url)
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                        f.write(resp.content)
                        tmp_cover = f.name
                print(f"Downloaded cover ({len(resp.content)} bytes) to {tmp_cover}")

                await multi_input.set_input_files(tmp_cover)
                print("Called set_input_files on multi_input!")
                await asyncio.sleep(4.0)

                clipper = page.locator("#image-in-preview-clipper img, ytd-backstage-image-preview-renderer img:visible")
                print(f"clipper count: {await clipper.count()}")
                if await clipper.count() > 0:
                    print(f"clipper visible: {await clipper.first.is_visible()}")

            # Check textarea
            textarea = page.locator("#contenteditable-root, div[contenteditable='true'][role='textbox']").first
            print(f"textarea visible: {await textarea.is_visible()}")
            await textarea.click()
            text = f"⚡ {art.title}\n\nПодробнее: https://svodka.press/{art.url_slug}/"
            await page.evaluate("""text => {
                const root = document.querySelector('#contenteditable-root') || document.querySelector('div[contenteditable="true"][role="textbox"]');
                if (root) {
                    root.focus();
                    document.execCommand('selectAll', false, null);
                    document.execCommand('insertText', false, text);
                    root.dispatchEvent(new Event('input', { bubbles: true }));
                    root.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""", text)
            await page.keyboard.type(" ")
            await page.keyboard.press("Backspace")
            await asyncio.sleep(1.5)

            # Check pub_btn
            pub_btn = page.locator(
                "button:has-text('Опубликовать'):visible, #submit-button button:visible"
            ).first
            print(f"pub_btn count: {await pub_btn.count()}")
            if await pub_btn.count() > 0:
                cls_attr = await pub_btn.get_attribute('class')
                dis_attr = await pub_btn.get_attribute('disabled')
                txt = await pub_btn.inner_text()
                print(f"pub_btn class: {cls_attr}")
                print(f"pub_btn disabled: {dis_attr}")
                print(f"pub_btn text: {txt}")

                print("Clicking pub_btn...")
                await pub_btn.click()
                await asyncio.sleep(6.0)
                print(f"Current URL: {page.url}")

                await page.goto("https://www.youtube.com/@svodkapress/posts", wait_until="domcontentloaded")
                await asyncio.sleep(3.5)
                top_post = page.locator("ytd-backstage-post-thread-renderer #content-text").first
                if await top_post.count() > 0:
                    top_text = await top_post.text_content()
                    print(f"Top post after publish: {top_text[:80]}...")
                else:
                    print("No top post found after publish!")

if __name__ == "__main__":
    asyncio.run(trace_publish())
