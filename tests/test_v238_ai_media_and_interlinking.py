# ruff: noqa: RUF001, E501
"""Tests for v2.38.0: AI image verification, strictly isolated interlinking, dual Dzen/WP blocks."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.image_cleaner import (
    _OFFTOPIC_URL_HINTS,
    verify_images_with_ai,
)
from app.services.lab_pipeline import (
    GEOGRAPHIC_AND_GENERIC_STOP_STEMS,
    _inject_internal_links,
    _inject_link_into_paragraphs,
)
from app.services.watermark_detector import (
    _CHANNEL_NAMES_RE,
    _ZONE_NAMES,
    _zone_rects,
)


def test_offtopic_hints_defined():
    assert "anchor" in _OFFTOPIC_URL_HINTS
    assert "presenter" in _OFFTOPIC_URL_HINTS
    assert "vedusch" in _OFFTOPIC_URL_HINTS
    assert "football" in _OFFTOPIC_URL_HINTS
    assert "soccer" in _OFFTOPIC_URL_HINTS
    assert "recipe" in _OFFTOPIC_URL_HINTS


def test_channel_watermark_and_top_center_zone():
    assert "top_center" in _ZONE_NAMES
    rects = _zone_rects(1000, 800)
    rect_names = {r.name for r in rects}
    assert "top_center" in rect_names

    # Matches channel titles like ТОПОР - Горячие новости
    assert _CHANNEL_NAMES_RE.search("ТОПОР - Горячие новости") is not None
    assert _CHANNEL_NAMES_RE.search("Новости BAZA") is not None
    assert _CHANNEL_NAMES_RE.search("Канал MASH") is not None



@pytest.mark.asyncio
async def test_verify_images_with_ai_fastpath_heuristic_rejection():
    """Candidates with obvious off-topic keywords are rejected before calling AI."""
    images = [
        {
            "url": "https://1tv.ru/news/presenter-ekaterina-andreeva.jpg",
            "title": "Новости Первого Канала",
        },
        {
            "url": "https://sport24.ru/football-match-zenit-spartak.jpg",
            "title": "Матч тура",
        },
        {
            "url": "https://mchs.gov.ru/fire-yaroslavl-warehouse.jpg",
            "title": "Ликвидация пожара на складе в Ярославле",
        },
    ]

    mock_client = AsyncMock()
    mock_client.complete = AsyncMock(
        return_value='[{"index": 0, "relevant": true, "reason": "пожар"}]'
    )

    with patch("app.services.image_cleaner.get_ai_client", return_value=mock_client):
        approved, rejected = await verify_images_with_ai(
            images,
            story_title="Пожар на складе лакокрасочных материалов в Ярославле",
            story_text="В Ярославле локализован крупный пожар на складе. МЧС задействовало 50 спасателей.",
        )

    # 2 obvious off-topics should be in rejected
    rejected_urls = [r["url"] for r in rejected]
    assert any("presenter" in u for u in rejected_urls)
    assert any("football" in u for u in rejected_urls)

    # Approved should contain the fire image
    assert len(approved) == 1
    assert "mchs.gov.ru" in approved[0]["url"]
    assert approved[0]["is_ai_approved"] is True


@pytest.mark.asyncio
async def test_verify_images_with_ai_llm_evaluation():
    """AI arbiter flags non-illustrative images as irrelevant."""
    images = [
        {
            "url": "https://yarreg.ru/uploads/fire1.jpg",
            "title": "Пожарные МЧС тушат склад",
            "domain": "yarreg.ru",
        },
        {
            "url": "https://yarreg.ru/uploads/car_traffic.jpg",
            "title": "Автомобили на Московском проспекте",
            "domain": "yarreg.ru",
        },
    ]

    mock_client = AsyncMock()
    # LLM approves image 0 (fire), rejects image 1 (traffic cars)
    mock_client.complete = AsyncMock(
        return_value="""[
            {"index": 0, "relevant": true, "reason": "место пожара на складе"},
            {"index": 1, "relevant": false, "reason": "случайные автомобили на дороге"}
        ]"""
    )

    with patch("app.services.image_cleaner.get_ai_client", return_value=mock_client):
        approved, rejected = await verify_images_with_ai(
            images,
            story_title="Пожар на складе в Ярославле",
            story_text="На складе горит продукция. Площадь пожара 2000 кв. метров.",
        )

    assert len(approved) == 1
    assert "fire1.jpg" in approved[0]["url"]
    assert approved[0]["is_ai_approved"] is True

    assert len(rejected) == 1
    assert "car_traffic.jpg" in rejected[0]["url"]
    assert rejected[0]["is_ai_approved"] is False
    assert "случайные автомобили" in rejected[0]["clean_verdict"]


@pytest.mark.asyncio
async def test_geographic_stop_stems_prevent_spurious_linking():
    """Common geographic stems like 'облас' or 'регион' must not cause cross-linking."""
    assert "облас" in GEOGRAPHIC_AND_GENERIC_STOP_STEMS
    assert "регион" in GEOGRAPHIC_AND_GENERIC_STOP_STEMS
    assert "район" in GEOGRAPHIC_AND_GENERIC_STOP_STEMS
    assert "город" in GEOGRAPHIC_AND_GENERIC_STOP_STEMS
    assert "стран" in GEOGRAPHIC_AND_GENERIC_STOP_STEMS

    html_content = "<p>В Ярославской области ликвидирован крупный пожар. Спасатели работали всю ночь.</p>"
    cluster_title = "Пожар на складе в Ярославле"

    mock_article = AsyncMock()
    mock_article.id = "00000000-0000-0000-0000-000000000002"
    mock_article.title = "Новый губернатор Самарской области вступил в должность"
    mock_article.category = "politika"
    mock_article.url_slug = "gubernator-samara"
    mock_article.platform_statuses = {}

    mock_session = AsyncMock()
    mock_scalars = AsyncMock()
    mock_scalars.all = lambda: [mock_article]
    mock_session.execute = AsyncMock(return_value=AsyncMock(scalars=lambda: mock_scalars))

    with patch("app.services.lab_pipeline.WPArchiveService.get_archive_posts", return_value=[]):
        res_html, injected = await _inject_internal_links(
            mock_session,
            cluster_id="00000000-0000-0000-0000-000000000001",
            html_content=html_content,
            cluster_title=cluster_title,
            cluster_category="chp-i-proisshestvij",
        )

    # Since only "области" matches, and it is a stop stem, no link should be injected
    assert "gubernator-samara" not in res_html
    assert len(injected) == 0


@pytest.mark.asyncio
async def test_military_category_strict_isolation():
    """Military news can only link to military news, and non-military never links to military."""
    html_content = "<p>В Ярославле продолжается ликвидация возгорания склада. Очевидцы сняли видео.</p>"
    cluster_title = "Пожар на складе в Ярославле"

    mock_mil_article = AsyncMock()
    mock_mil_article.id = "00000000-0000-0000-0000-000000000002"
    mock_mil_article.title = "Учения ВС РФ в Саратовской области: видео пусков ракет"
    mock_mil_article.category = "voennye-novosti"
    mock_mil_article.url_slug = "ucheniya-vs-rf-saratov"
    mock_mil_article.platform_statuses = {}

    mock_session = AsyncMock()
    mock_scalars = AsyncMock()
    mock_scalars.all = lambda: [mock_mil_article]
    mock_session.execute = AsyncMock(return_value=AsyncMock(scalars=lambda: mock_scalars))

    with patch("app.services.lab_pipeline.WPArchiveService.get_archive_posts", return_value=[]):
        res_html, injected = await _inject_internal_links(
            mock_session,
            cluster_id="00000000-0000-0000-0000-000000000001",
            html_content=html_content,
            cluster_title=cluster_title,
            cluster_category="chp-i-proisshestvij",
        )

    assert "ucheniya-vs-rf-saratov" not in res_html
    assert len(injected) == 0


def test_clean_paragraph_token_replacement_avoids_broken_quotes():
    """Quotes like «...» or \"...\" around matching phrases must remain outside anchor tags."""
    p_html = '<p>По данным МЧС, в «Ярославле» локализовано открытое горение складского ангара.</p>'
    res_html, anchor = _inject_link_into_paragraphs(
        p_html,
        stem="ярославл",
        link_url="https://svodka.press/sklad-v-yaroslavle/",
        link_title="Склад в Ярославле",
    )
    assert anchor is not None
    assert "«облас" not in res_html
    assert '<a href="https://svodka.press/sklad-v-yaroslavle/"' in res_html
    assert "Ярославле</a>»" in res_html


def test_dual_publish_blocks_in_lab_cluster_detail_template():
    """Verify lab_cluster_detail.html contains both independent publish containers."""
    with open("app/templates/dashboard/partials/lab_cluster_detail.html", encoding="utf-8") as f:
        html = f.read()

    assert 'id="step-publish-wp-container"' in html
    assert 'id="step-publish-dzen-container"' in html
    assert "/api/ui/lab/cluster/{{ cluster.cluster_id }}/publish" in html
    assert "/api/ui/lab/cluster/{{ cluster.cluster_id }}/publish-dzen" in html


def test_dual_publish_result_template_rendering():
    """Verify lab_publish_result.html renders distinct cards for wordpress and dzen."""
    with open("app/templates/dashboard/partials/lab_publish_result.html", encoding="utf-8") as f:
        tmpl = f.read()

    assert "is_dzen" in tmpl
    assert "step-publish-dzen-container" in tmpl
    assert "step-publish-wp-container" in tmpl

