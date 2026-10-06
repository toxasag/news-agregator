"""Tests for Dzen Web Publisher.

Tests session management, promo injection, and Playwright automation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.enums import PlatformStatus
from app.services.dzen_publisher import (
    DzenBrowserPublisher,
    DzenSessionConfig,
    inject_dzen_promo,
    linkify_dzen_promo,
    load_dzen_session_config,
    probe_dzen_session,
    save_dzen_session_config,
)
from app.services.lab_pipeline import calculate_rubric_potential
from app.services.social_dzen import DzenAdapter


def test_dzen_session_config_serialization() -> None:
    """Config must correctly serialize to DB dict and restore with decryption."""
    raw_storage = {
        "cookies": [
            {"name": "Session_id", "value": "test_session_token_123"},
            {"name": "sessionid2", "value": "test_session_token_456"},
        ],
        "origins": [],
    }
    cfg = DzenSessionConfig(
        storage_state=raw_storage,
        channel_url="https://dzen.ru/svodkapress",
        channel_name="Сводка Пресс",
        is_active=True,
        promo_mode="native_banner",
        promo_snippet="Подписывайтесь на наш Telegram: @svodkapress",
        publication_mode="draft",
    )

    assert cfg.has_session is True
    db_dict = cfg.to_db_dict()
    assert "encrypted_storage_state" in db_dict
    assert db_dict["channel_url"] == "https://dzen.ru/svodkapress"
    assert db_dict["channel_name"] == "Сводка Пресс"
    assert db_dict["is_active"] is True
    assert db_dict["promo_mode"] == "native_banner"

    restored = DzenSessionConfig.from_db_dict(db_dict)
    assert restored.channel_url == cfg.channel_url
    assert restored.channel_name == cfg.channel_name
    assert restored.is_active is True
    assert restored.promo_mode == "native_banner"
    assert restored.promo_snippet == cfg.promo_snippet
    assert restored.publication_mode == "draft"
    assert restored.storage_state is not None
    assert restored.storage_state["cookies"][0]["value"] == "test_session_token_123"


def test_inject_dzen_promo_modes() -> None:
    """Promo snippet injection should adapt to chosen format cleanly."""
    body = (
        "<p>Paragraph 1 news events</p>\n"
        "<p>Paragraph 2 detailed facts</p>\n"
        "<p>Paragraph 3 conclusion</p>"
    )

    # 1. Mode: none -> unchanged
    assert inject_dzen_promo(body, "none", "Реклама") == body

    # 2. Mode: native_banner -> inserted after 2nd paragraph
    ad_text = "Тестовый нативный баннер"
    res_native = inject_dzen_promo(body, "native_banner", ad_text)
    assert "dzen-native-banner" in res_native
    assert "Партнерский материал" in res_native
    assert ad_text in res_native
    p2_pos = res_native.find("Paragraph 2")
    banner_pos = res_native.find("dzen-native-banner")
    p3_pos = res_native.find("Paragraph 3")
    assert p2_pos < banner_pos < p3_pos

    # 3. Mode: promo_box
    res_box = inject_dzen_promo(body, "promo_box", "Цитата промо")
    assert "dzen-promo-box" in res_box
    assert "Цитата промо" in res_box

    # 4. Mode: custom_html
    res_custom = inject_dzen_promo(body, "custom_html", "<div class='my-ad'>Banner</div>")
    assert "<div class='my-ad'>Banner</div>" in res_custom

    # 5. Short body with 1 paragraph -> appended cleanly
    short_body = "<p>Brief short news.</p>"
    res_short = inject_dzen_promo(short_body, "native_banner", "Ad text")
    assert res_short.startswith("<p>Brief short news.</p>")
    assert "dzen-native-banner" in res_short


@pytest.mark.asyncio
async def test_load_and_save_dzen_session_config() -> None:
    """Session loading and saving must query SystemSetting properly."""
    session = AsyncMock()
    # Mocking select result
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = {
        "channel_url": "https://dzen.ru/testchannel",
        "channel_name": "Тест Канал",
        "is_active": True,
        "status": "valid",
    }
    session.execute.return_value = result_mock

    loaded = await load_dzen_session_config(session)
    assert loaded.channel_url == "https://dzen.ru/testchannel"
    assert loaded.channel_name == "Тест Канал"
    assert loaded.is_active is True

    # Test saving
    await save_dzen_session_config(session, loaded)
    assert session.execute.call_count >= 2
    assert session.commit.called


@pytest.mark.asyncio
async def test_probe_dzen_session_not_configured() -> None:
    """Probing without session must immediately return not_configured."""
    empty_cfg = DzenSessionConfig()
    probe = await probe_dzen_session(empty_cfg)
    assert probe.is_valid is False
    assert probe.status == "not_configured"


@pytest.mark.asyncio
async def test_probe_dzen_session_expired() -> None:
    """Redirect to passport.yandex.ru must report expired session."""
    cfg = DzenSessionConfig(
        storage_state={"cookies": [{"name": "foo", "value": "bar"}]},
        is_active=True,
    )

    page_mock = AsyncMock()
    page_mock.url = "https://passport.yandex.ru/auth?retpath=..."
    context_manager_mock = AsyncMock()
    context_manager_mock.__aenter__.return_value = page_mock

    with patch("app.services.dzen_publisher.get_browser_pool") as pool_mock:
        pool_mock.return_value.page.return_value = context_manager_mock
        probe = await probe_dzen_session(cfg)
        assert probe.is_valid is False
        assert probe.status == "expired"
        assert "перенаправление на страницу входа" in probe.message


@pytest.mark.asyncio
async def test_probe_dzen_session_valid() -> None:
    """Navigating to Studio without auth redirect must report valid session."""
    cfg = DzenSessionConfig(
        storage_state={"cookies": [{"name": "foo", "value": "bar"}]},
        channel_name="Канал Сводка",
        is_active=True,
    )

    page_mock = AsyncMock()
    page_mock.url = "https://dzen.ru/profile/editor"
    page_mock.title.return_value = "Студия — Канал Сводка"
    page_mock.query_selector = AsyncMock(return_value=None)
    context_manager_mock = AsyncMock()
    context_manager_mock.__aenter__.return_value = page_mock

    with patch("app.services.dzen_publisher.get_browser_pool") as pool_mock:
        pool_mock.return_value.page.return_value = context_manager_mock
        probe = await probe_dzen_session(cfg)
        assert probe.is_valid is True
        assert probe.status == "valid"
        assert "Сессия активна" in probe.message


@pytest.mark.asyncio
async def test_dzen_browser_publisher_publish() -> None:
    """DzenBrowserPublisher should execute page automation and return published result."""
    cfg = DzenSessionConfig(
        storage_state={"cookies": [{"name": "Session_id", "value": "valid_token"}]},
        channel_url="https://dzen.ru/svodkapress",
        is_active=True,
        promo_mode="native_banner",
        promo_snippet="Рекламный блок Дзена",
        publication_mode="publish",
    )
    publisher = DzenBrowserPublisher(cfg)
    assert publisher.is_ready is True

    article = MagicMock()
    article.id = "art_123"
    article.title = "Тестовая статья для Дзена"
    article.content_html = "<p>Лид статьи.</p><p>Факты.</p><p>Итоги.</p>"

    async def fake_query_selector(selector: str):
        if "Войти" in selector or "login-button" in selector:
            return None
        return AsyncMock()

    page_mock = AsyncMock()
    page_mock.url = "https://dzen.ru/profile/editor/abc123xyz"
    page_mock.query_selector = AsyncMock(side_effect=fake_query_selector)
    page_mock.query_selector_all = AsyncMock(return_value=[AsyncMock(), AsyncMock()])
    context_manager_mock = AsyncMock()
    context_manager_mock.__aenter__.return_value = page_mock

    with patch("app.services.dzen_publisher.get_browser_pool") as pool_mock:
        pool_mock.return_value.page.return_value = context_manager_mock
        res = await publisher.publish_article(
            article,
            wp_post_url="https://svodka.press/news/test-123/",
            cover_url="https://svodka.press/wp-content/uploads/cover.jpg",
            publish_now=True,
        )

        assert res.status == PlatformStatus.PUBLISHED.value
        assert res.external_id == "abc123xyz"
        assert res.url == "https://dzen.ru/a/abc123xyz"


@pytest.mark.asyncio
async def test_dzen_adapter_delegation_to_browser_publisher() -> None:
    """DzenAdapter should delegate publish to browser_publisher when configured."""
    mock_browser_pub = MagicMock()
    mock_browser_pub.is_ready = True
    mock_browser_pub.publish_article = AsyncMock(
        return_value=MagicMock(
            status=PlatformStatus.PUBLISHED.value,
            external_id="dzen_999",
            url="https://dzen.ru/media/id/999",
        )
    )

    adapter = DzenAdapter(browser_publisher=mock_browser_pub)
    assert adapter.is_configured is True

    article = MagicMock()
    res = await adapter.publish(article, wp_post_url="https://svodka.press/article/1/")
    assert res.status == PlatformStatus.PUBLISHED.value
    assert res.external_id == "dzen_999"
    assert mock_browser_pub.publish_article.called


def test_linkify_dzen_promo_converts_site_and_telegram_to_live_links() -> None:
    """linkify_dzen_promo must convert site and Telegram mentions into live links."""
    raw = "Читайте новости на сайте или в телеграм канале"
    res = linkify_dzen_promo(raw)
    assert 'href="https://svodka.press/"' in res
    assert 'href="https://t.me/svodka_press"' in res
    assert "на <a " in res
    assert "в <a " in res


@pytest.mark.asyncio
async def test_calculate_rubric_potential_120h_and_semantic_expansion() -> None:
    """calculate_rubric_potential should use 120h window and match semantic stems."""
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [
        "Фанаты «Друзей», это вам — вышел первый трейлер",
        "Новый сезон популярного шоу стартует в мае",
        "Центробанк сохранил ключевую ставку",
    ]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute.return_value = result_mock

    potential = await calculate_rubric_potential(
        session,
        title="Кино и сериалы",
        description="Новости киноиндустрии, премьеры фильмов и сериалов",
    )
    assert potential["lookback_hours"] == 120
    assert potential["matching_clusters_count"] == 2
    assert len(potential["matching_titles"]) == 2

