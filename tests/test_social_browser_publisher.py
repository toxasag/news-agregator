from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.article import Article
from app.models.enums import ArticleStatus
from app.services.social_base import PublishResult
from app.services.social_browser_publisher import (
    SOCIAL_BROWSER_PLATFORMS,
    SocialBrowserSessionConfig,
    build_social_browser_adapters,
    format_social_browser_post,
    load_all_social_browser_configs,
    load_social_browser_config,
    normalize_social_storage_state,
    save_social_browser_config,
)


def test_social_browser_platforms_metadata() -> None:
    expected = {"setka", "tenchat", "threads", "facebook", "pinterest", "youtube"}
    assert set(SOCIAL_BROWSER_PLATFORMS.keys()) == expected
    for key, meta in SOCIAL_BROWSER_PLATFORMS.items():
        assert meta["code"] == key
        assert str(meta["domain"]).startswith(".")
        assert str(meta["home_url"]).startswith("https://")


def test_normalize_social_storage_state_threads_domain_mirroring() -> None:
    raw_cookies = [
        {
            "name": "sessionid",
            "value": "threads-secret-123",
            "domain": ".threads.net",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "no_restriction",
        }
    ]
    parsed = normalize_social_storage_state(raw_cookies, "threads")
    assert len(parsed["cookies"]) == 2
    domains = {c["domain"] for c in parsed["cookies"]}
    assert ".threads.net" in domains
    assert ".threads.com" in domains
    for c in parsed["cookies"]:
        assert c["sameSite"] == "None"


def test_normalize_social_storage_state_youtube_google_mirroring() -> None:
    raw_cookies = [
        {
            "name": "SAPISID",
            "value": "yt-secret-456",
            "domain": ".google.com",
            "path": "/",
            "secure": True,
            "sameSite": "lax",
        }
    ]
    parsed = normalize_social_storage_state(raw_cookies, "youtube")
    assert len(parsed["cookies"]) == 2
    domains = {c["domain"] for c in parsed["cookies"]}
    assert ".google.com" in domains
    assert ".youtube.com" in domains


def test_format_social_browser_post() -> None:
    article = Article(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        category="economy",
        url_slug="test-slug",
        title="ЦБ сохранил ключевую ставку на уровне 21%",
        content_html=(
            "<p>Совет директоров Банка России сохранил ключевую ставку.</p>"
            "<p>Регулятор отметил снижение инфляционных ожиданий населения.</p>"
        ),
        seo_description="Банк России сохранил ключевую ставку",
        seo_keywords=["экономика", "цб рф", "ключевая ставка"],
        status=ArticleStatus.WP_PUBLISHED,
        platform_statuses={
            "wp": {
                "status": "published",
                "url": "https://svodka.press/2026/10/01/cb-key-rate/",
                "cover_url": "https://svodka.press/wp-content/uploads/cover.jpg",
            }
        },
    )
    post = format_social_browser_post(
        article=article,
        wp_post_url="https://svodka.press/2026/10/01/cb-key-rate/",
        platform="setka",
    )
    assert "ЦБ сохранил ключевую ставку" in post["title"]
    assert "https://svodka.press/2026/10/01/cb-key-rate/" in post["text"]
    assert "https://t.me/svodka_press" in post["text"]
    assert "#экономика" in post["hashtags"]
    assert post["cover_url"] == "https://svodka.press/wp-content/uploads/cover.jpg"


@pytest.mark.asyncio
async def test_save_and_load_social_browser_config_roundtrip() -> None:
    session = AsyncMock()
    stored_value: dict[str, object] = {}

    async def fake_execute(stmt):
        # Check if it's an insert/upsert or select
        compile_str = str(stmt)
        res = MagicMock()
        if "INSERT" in compile_str.upper():
            # Extract value from insert statement compile params
            params = stmt.compile().params
            if "value" in params:
                stored_value.update(params["value"])
            res.scalar_one_or_none.return_value = None
            return res
        if stored_value:
            row = MagicMock()
            row.value = stored_value
            res.scalar_one_or_none.return_value = row
        else:
            res.scalar_one_or_none.return_value = None
        return res

    session.execute.side_effect = fake_execute

    cfg = SocialBrowserSessionConfig(
        platform="tenchat",
        is_active=True,
        target_url="https://tenchat.ru/svodkapress",
        storage_state={
            "cookies": [
                {
                    "name": "tc_token",
                    "value": "123",
                    "domain": ".tenchat.ru",
                    "path": "/",
                }
            ],
            "origins": [],
        },
        account_name="Svodka TenChat",
        status="active",
    )
    await save_social_browser_config(session, cfg)
    loaded = await load_social_browser_config(session, "tenchat")
    assert loaded.is_active is True
    assert loaded.account_name == "Svodka TenChat"
    assert loaded.cookies_count == 1
    assert loaded.has_session is True

    all_configs = await load_all_social_browser_configs(session)
    assert set(all_configs.keys()) == {
        "setka",
        "tenchat",
        "threads",
        "facebook",
        "pinterest",
        "youtube",
    }
    assert all_configs["tenchat"].is_configured is True


@pytest.mark.asyncio
async def test_social_browser_adapters_safe_publish() -> None:
    cfg = SocialBrowserSessionConfig(
        platform="setka",
        is_active=True,
        storage_state={"cookies": [{"name": "s", "value": "1"}], "origins": []},
        status="active",
    )
    adapters = build_social_browser_adapters({"setka": cfg})
    assert len(adapters) == 6
    assert adapters[0].is_configured is True

    article = Article(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        category="economy",
        url_slug="test-slug",
        title="Тестовый заголовок для соцсетей",
        content_html="<p>Первый абзац новости для проверки адаптеров.</p>",
        seo_description="Краткое описание новости",
        seo_keywords=["новости", "россия"],
        status=ArticleStatus.WP_PUBLISHED,
        wp_post_id=777,
        platform_statuses={
            "wp": {
                "status": "published",
                "url": "https://svodka.press/test-post/",
                "cover_url": "https://svodka.press/cover.jpg",
            }
        },
    )
    with patch(
        "app.services.social_browser_publisher.publish_via_social_browser",
        new_callable=AsyncMock,
    ) as mock_pub:
        mock_pub.return_value = PublishResult.published(
            "setka",
            external_id="setka-123",
            url="https://setka.ru/posts/123",
        )
        result = await adapters[0].safe_publish(
            article, "https://svodka.press/test-post/"
        )
        assert result.is_published is True
        assert result.url == "https://setka.ru/posts/123"
        mock_pub.assert_awaited_once()


@pytest.mark.asyncio
async def test_tenchat_browser_adapter_routing() -> None:
    cfg = SocialBrowserSessionConfig(
        platform="tenchat",
        is_active=True,
        target_url="https://tenchat.ru/5304525",
        storage_state={"cookies": [{"name": "SESSION", "value": "xyz"}], "origins": []},
        status="active",
    )
    adapters = build_social_browser_adapters({"tenchat": cfg})
    tc_adapter = next(a for a in adapters if a.platform_code == "tenchat")
    assert tc_adapter.is_configured is True

    article = Article(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        category="technology",
        url_slug="tenchat-slug",
        title="Новость для TenChat",
        content_html="<p>Текст новости для TenChat.</p>",
        seo_description="Описание",
        seo_keywords=["технологии"],
        status=ArticleStatus.WP_PUBLISHED,
        wp_post_id=888,
        platform_statuses={},
    )
    with patch(
        "app.services.social_browser_publisher._publish_tenchat_post",
        new_callable=AsyncMock,
    ) as mock_tc_pub, patch(
        "app.services.web_scraper.BrowserPool.page_with_storage"
    ) as mock_page_storage:
        mock_page = AsyncMock()
        mock_page_storage.return_value.__aenter__.return_value = mock_page
        mock_tc_pub.return_value = ("tenchat-abc123", "https://tenchat.ru/5304525")

        res = await tc_adapter.safe_publish(article, "https://svodka.press/tech/tenchat-slug")
        assert res.is_published is True
        assert res.platform == "tenchat"
        assert res.external_id == "tenchat-abc123"
        mock_tc_pub.assert_awaited_once()


@pytest.mark.asyncio
async def test_youtube_publish_skips_duplicate_top_post() -> None:
    from app.services.social_browser_publisher import (  # noqa: PLC0415
        _publish_youtube_post,
    )

    cfg = SocialBrowserSessionConfig(
        platform="youtube",
        is_active=True,
        target_url="https://www.youtube.com/@svodkapress/posts",
        storage_state={"cookies": [{"name": "SAPISID", "value": "xyz"}], "origins": []},
        status="active",
    )

    mock_page = AsyncMock()
    mock_page.url = "https://www.youtube.com/@svodkapress/posts"

    # Avatar is visible (logged in)
    mock_avatar = AsyncMock()
    mock_avatar.count = AsyncMock(return_value=1)
    mock_avatar.is_visible = AsyncMock(return_value=True)
    mock_avatar.first = mock_avatar

    # Top post matches title
    mock_top_post = AsyncMock()
    mock_top_post.count = AsyncMock(return_value=1)
    mock_top_post.text_content = AsyncMock(
        return_value="⚡ В Москве запустили новую ветку метро. Подробности читайте..."  # noqa: RUF001
    )
    mock_posts_locator = AsyncMock()
    mock_posts_locator.count = AsyncMock(return_value=1)
    mock_posts_locator.nth = MagicMock(return_value=mock_top_post)

    def locator_router(selector: str) -> Any:
        if "avatar-btn" in selector:
            return mock_avatar
        if "ytd-backstage-post-thread-renderer #content-text" in selector:
            return mock_posts_locator
        loc = AsyncMock()
        loc.count = AsyncMock(return_value=0)
        loc.is_visible = AsyncMock(return_value=False)
        loc.first = loc
        return loc

    mock_page.locator = MagicMock(side_effect=locator_router)

    post_data = {
        "title": "В Москве запустили новую ветку метро сегодня",  # noqa: RUF001
        "text": "Полный текст",
        "article_id": "art-12345",
    }

    ext_id, final_url = await _publish_youtube_post(
        mock_page, cfg, post_data, cover_path=None
    )

    assert ext_id == "youtube-dup-art-12345"
    assert final_url == "https://www.youtube.com/@svodkapress/posts"


