# ruff: noqa: RUF001, E501
"""Tests for v2.34.0: Relevant category interlinking, transliterated slugs, multi-photo research, and Dzen cover."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.article import Article
from app.services.dzen_publisher import DzenBrowserPublisher, DzenSessionConfig
from app.services.lab_pipeline import (
    _ensure_sources_block,
    _get_category_affinity,
    _inject_internal_links,
    _upsert_article_from_generated,
)
from app.services.web_researcher import WebArticleContent, fetch_clean_article


def test_category_affinity_grouping() -> None:
    """Category affinity should group related rubrics and isolate distinct themes."""
    mil_affinity = _get_category_affinity("voennye-novosti")
    assert "novosti-politiki" in mil_affinity
    assert "chp-i-proisshestvij" in mil_affinity
    assert "it-i-tehnologii" not in mil_affinity
    assert "kultura-i-iskusstvo" not in mil_affinity

    tech_affinity = _get_category_affinity("it-i-tehnologii")
    assert "gadgety" in tech_affinity
    assert "nauka-i-kosmos" in tech_affinity
    assert "voennye-novosti" not in tech_affinity


@pytest.mark.asyncio
async def test_inject_internal_links_affinity_and_canonical_urls() -> None:
    """Internal links must use full canonical URLs and reject off-topic articles."""
    cluster_id = uuid.uuid4()
    cluster_title = "Премьер-министр урезает финансирование строек ради обороны"
    cluster_category = "novosti-politiki"
    html_content = (
        "<p>Глава правительства заявил, что все ресурсы направляются на финансирование обороны страны.</p>"
        "<p>Приоритеты бюджета пересмотрены в пользу безопасности.</p>"
    )

    # Candidate 1: Political/Military article with overlap
    art_military = MagicMock(spec=Article)
    art_military.id = 101
    art_military.title = "Правительство пересмотрело финансирование и бюджетные приоритеты"
    art_military.category = "novosti-politiki"
    art_military.url_slug = "pravitelstvo-peresmotrelo-finansirovanie"
    art_military.platform_statuses = {
        "wp": {
            "url": "https://svodka.press/novosti-politiki/pravitelstvo-peresmotrelo-finansirovanie/"
        }
    }

    # Candidate 2: Off-topic IT article
    art_tech = MagicMock(spec=Article)
    art_tech.id = 102
    art_tech.title = "Эпоха чат-ботов закончилась: новый виток генеративных нейросетей"
    art_tech.category = "it-i-tehnologii"
    art_tech.url_slug = "epoha-chat-botov-zakonchilas"
    art_tech.platform_statuses = None

    session = AsyncMock()
    exec_mock = MagicMock()
    exec_mock.scalars.return_value.all.return_value = [art_military]
    session.execute.return_value = exec_mock

    with patch("app.services.wp_archive.WPArchiveService.get_archive_posts", return_value=[]):
        updated_html, links = await _inject_internal_links(
            session,
            cluster_id=cluster_id,
            html_content=html_content,
            cluster_title=cluster_title,
            cluster_category=cluster_category,
        )

    assert len(links) >= 1
    # Ensure link uses full canonical URL with domain
    assert links[0]["url"].startswith("https://svodka.press/")
    # Ensure anchor is in updated HTML
    assert "https://svodka.press/novosti-politiki/pravitelstvo-peresmotrelo-finansirovanie/" in updated_html
    # Ensure no relative or Cyrillic URLs
    assert "/эпоха-чат-ботов" not in updated_html


@pytest.mark.asyncio
async def test_inject_internal_links_no_offtopic_fallback() -> None:
    """When candidates have zero keyword overlap and different category, no link is injected."""
    cluster_id = uuid.uuid4()
    cluster_title = "Премьер-министр урезает финансирование строек ради обороны"
    cluster_category = "novosti-politiki"
    html_content = "<p>Короткая новость о ремонте дорог.</p>"

    # Completely unrelated candidate with no keyword overlap
    art_unrelated = MagicMock(spec=Article)
    art_unrelated.id = 200
    art_unrelated.title = "Фестиваль джаза открылся в Суздале под открытым небом"
    art_unrelated.category = "kultura-i-iskusstvo"
    art_unrelated.url_slug = "festival-dzhaza-suzdal"
    art_unrelated.platform_statuses = None

    session = AsyncMock()
    exec_mock = MagicMock()
    # First query (affinity) returns empty, general query returns unrelated
    exec_mock.scalars.return_value.all.side_effect = [[], [art_unrelated]]
    session.execute.return_value = exec_mock

    with patch("app.services.wp_archive.WPArchiveService.get_archive_posts", return_value=[]):
        updated_html, links = await _inject_internal_links(
            session,
            cluster_id=cluster_id,
            html_content=html_content,
            cluster_title=cluster_title,
            cluster_category=cluster_category,
        )

    # Should NOT inject unrelated jazz festival into military budget article!
    assert len(links) == 0
    assert "Читайте по теме" not in updated_html


@pytest.mark.asyncio
async def test_upsert_article_transliterates_slug() -> None:
    """Article slugs must be transliterated to clean Latin URLs, fixing Cyrillic."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit.return_value = None
    session.refresh.return_value = None
    exec_mock = MagicMock()
    exec_mock.scalars.return_value.first.return_value = None
    session.execute.return_value = exec_mock

    cid = uuid.uuid4()
    generated = {
        "title": "Премьер Украины заявил о резке расходов",
        "seo_title": "Премьер Украины урезает стройки ради обороны.",
        "seo_description": "Описание статьи",
        "seo_keywords": ["оборона", "бюджет"],
        "category_slug": "novosti-politiki",
    }

    art = await _upsert_article_from_generated(
        session,
        cluster_id=cid,
        fallback_title="Запасной заголовок",
        fallback_category="novosti-politiki",
        generated=generated,
        cleaned_html="<p>Текст статьи</p>",
    )

    assert art.url_slug != ""
    # Must NOT contain Russian letters or trailing period
    assert not any("а" <= ch <= "я" or "А" <= ch <= "Я" for ch in art.url_slug)
    assert not art.url_slug.endswith(".")
    assert "premer" in art.url_slug


@pytest.mark.asyncio
async def test_fetch_clean_article_extracts_multiple_images() -> None:
    """fetch_clean_article should extract multiple reportage photos from article HTML."""
    html_sample = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Репортаж о встрече лидеров</title>
        <meta property="og:image" content="https://example.com/photos/lead.jpg">
    </head>
    <body>
        <main>
            <h1>Заголовок</h1>
            <figure>
                <img src="/photos/speaker.jpg" alt="Спикер">
                <figcaption>Выступление спикера</figcaption>
            </figure>
            <p>Первый абзац статьи с подробностями встречи.</p>
            <p>Второй абзац статьи с цитатами и фактами переговоров сторон.</p>
            <img data-src="/photos/delegation.jpg" alt="Делегация">
            <img srcset="/photos/signing_400.jpg 400w, /photos/signing_800.jpg 800w" src="/photos/signing_800.jpg" alt="Подписание">
        </main>
    </body>
    </html>
    """

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = html_sample.encode("utf-8")
    mock_resp.encoding = "utf-8"
    mock_resp.url = "https://example.com/news/123"

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    with patch("app.services.web_researcher.is_safe_web_url", return_value=True):
        art_res = await fetch_clean_article("https://example.com/news/123", client=mock_client)

    assert art_res is not None
    assert isinstance(art_res, WebArticleContent)
    assert len(art_res.images) >= 2
    assert any("lead.jpg" in img for img in art_res.images)
    assert any("speaker.jpg" in img for img in art_res.images)


@pytest.mark.asyncio
async def test_dzen_publisher_includes_cover_image() -> None:
    """Dzen publisher should prepend cover image when cover_url is provided."""
    cfg = DzenSessionConfig(
        storage_state={"cookies": [{"name": "Session_id", "value": "test"}]},
        is_active=True,
    )
    publisher = DzenBrowserPublisher(cfg)

    article = MagicMock()
    article.id = 555
    article.title = "Статья с фотообложкой"
    article.content_html = "<p>Абзац 1 новости.</p><p>Абзац 2 фактов.</p>"

    async def fake_query_selector(selector: str):
        if "Войти" in selector or "login-button" in selector:
            return None
        return AsyncMock()

    page_mock = AsyncMock()
    page_mock.url = "https://dzen.ru/profile/editor/draft123"
    page_mock.query_selector = AsyncMock(side_effect=fake_query_selector)
    page_mock.query_selector_all = AsyncMock(return_value=[AsyncMock(), AsyncMock()])

    context_manager_mock = AsyncMock()
    context_manager_mock.__aenter__.return_value = page_mock

    cover = "https://svodka.press/wp-content/uploads/2026/09/cover.jpg"
    with patch("app.services.dzen_publisher.get_browser_pool") as pool_mock:
        pool_mock.return_value.page.return_value = context_manager_mock
        res = await publisher.publish_article(
            article,
            wp_post_url="https://svodka.press/novosti/statya-s-foto/",
            cover_url=cover,
            publish_now=False,
        )

    assert res.external_id == "draft123"
    # Verify that _fill_editor was called with HTML containing the cover image
    fill_calls = [
        call[0][1]
        for call in page_mock.evaluate.call_args_list
        if len(call[0]) > 1 and isinstance(call[0][1], str)
    ]
    assert fill_calls, "No evaluate call with HTML string payload found"
    assert any(f'<img src="{cover}"' in s for s in fill_calls)


def test_ensure_sources_block() -> None:
    """_ensure_sources_block appends clean attribution block with external links and TG source."""
    html = "<p>Основной текст статьи о происшествии.</p>"
    web_sources = [
        {"domain": "vesti.ru", "url": "https://www.vesti.ru/article/123", "title": "Вести.Ru"},
        {"domain": "rg.ru", "url": "https://rg.ru/article/456", "title": "Российская газета"},
    ]

    p_mock = MagicMock()
    p_mock.source.name = "Топор Live"
    p_mock.post_url = "https://t.me/topor/9999"

    res = _ensure_sources_block(html, web_sources, [p_mock])
    assert "Источники информации:" in res
    assert "https://www.vesti.ru/article/123" in res
    assert "https://rg.ru/article/456" in res
    assert "https://t.me/topor/9999" in res
    assert "Telegram-канал «Топор Live»" in res

    # Calling again should not duplicate
    res_dup = _ensure_sources_block(res, web_sources, [p_mock])
    assert res_dup.count("Источники информации:") == 1

