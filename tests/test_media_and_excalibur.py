# ruff: noqa: RUF001, E501, SIM117, F401, I001
"""Tests for media improvements, prompt generation, and Excalibur Schema.org standards."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.cluster import NewsCluster
from app.services.lab_pipeline import _clean_text_for_prompt, generate_editorial_image_prompt
from app.services.schema_org import (
    evaluate_lead_hook,
    extract_faq_from_html,
    generate_schema_org_json_ld,
)
from app.services.web_researcher import fetch_clean_article


def test_clean_text_for_prompt():
    """Verify that emojis and speech clichés are stripped from image prompts."""
    raw = '⚡ Срочно! "Путин заявил о переговорах" в Кремле 💥'
    clean = _clean_text_for_prompt(raw)
    assert "⚡" not in clean
    assert "💥" not in clean
    assert '"' not in clean
    assert "Путин заявил о переговорах в Кремле" in clean


@pytest.mark.asyncio
async def test_generate_editorial_image_prompt_fallback():
    """Fallback produces high-grade English journalistic prompt if LLM fails."""
    cluster = NewsCluster(
        title="⚡ Переговоры правительственной делегации в Кремле",
        category="politika",
        enrichment_data={"web_facts": ["Встреча на высшем уровне", "Подписаны соглашения"]},
    )
    session = AsyncMock()

    with patch("app.services.lab_pipeline.resolve_chain", return_value=["test_model"]):
        with patch("app.services.lab_pipeline.run_with_fallback", side_effect=RuntimeError("LLM unavailable")):
            prompt = await generate_editorial_image_prompt(session, cluster)

    assert len(prompt) > 20
    assert "Editorial press photograph" in prompt
    assert "35mm" in prompt
    assert "⚡" not in prompt


def test_evaluate_lead_hook_optimal():
    """Evaluate lead hook on high-impact lead paragraph with numbers and no clichés."""
    lead = (
        "<p>Центральный банк России 26 сентября повысил ключевую ставку сразу на 100 базисных пунктов — "
        "до 19% годовых, объяснив решение сохраняющимся высоким инфляционным давлением. "
        "По оценке регулятора, к концу года рост цен превысит базовый прогноз в 6,5-7,0% и составит около 7,7%, "
        "что требует более жестких денежно-кредитных условий.</p>"
    )
    res = evaluate_lead_hook(lead)
    assert res["is_optimal"] is True
    assert res["score"] == 100
    assert len(res["cliches"]) == 0
    assert 300 <= res["length"] <= 550


def test_evaluate_lead_hook_with_cliches():
    """Evaluate lead hook flags clichés and short length."""
    lead = "<p>Как стало известно, в последнее время цены растут.</p>"
    res = evaluate_lead_hook(lead)
    assert res["is_optimal"] is False
    assert res["score"] < 100
    assert "как стало известно" in res["cliches"]
    assert len(res["suggestions"]) > 0


def test_extract_faq_from_html():
    """Extract FAQ items from generated HTML."""
    html = (
        "<h2>Обзор событий</h2><p>Текст статьи</p>"
        "<h3>Что изменится для граждан?</h3>"
        "<p>Ставки по вкладам вырастут до 20%, а кредиты станут менее доступными.</p>"
        "<h3>Когда решение вступит в силу?</h3>"
        "<p>Новые параметры начнут действовать со следующего понедельника.</p>"
    )
    faqs = extract_faq_from_html(html)
    assert len(faqs) == 2
    assert faqs[0]["question"] == "Что изменится для граждан?"
    assert "Ставки по вкладам" in faqs[0]["answer"]
    assert faqs[1]["question"] == "Когда решение вступит в силу?"


def test_generate_schema_org_json_ld():
    """Schema.org generator builds valid NewsArticle and FAQPage graph."""
    faqs = [{"question": "Вопрос 1", "answer": "Ответ 1"}]
    ld = generate_schema_org_json_ld(
        headline="Заголовок новости",
        description="Краткое описание события",
        url="https://svodkapress.ru/news/test-slug",
        keywords=["новости", "россия"],
        faq_items=faqs,
    )
    assert ld["@context"] == "https://schema.org"
    graph = ld["@graph"]
    assert len(graph) == 2

    news_art = graph[0]
    assert news_art["@type"] == "NewsArticle"
    assert news_art["headline"] == "Заголовок новости"
    assert news_art["publisher"]["name"] == "SvodkaPress"

    faq_page = graph[1]
    assert faq_page["@type"] == "FAQPage"
    assert len(faq_page["mainEntity"]) == 1
    assert faq_page["mainEntity"][0]["name"] == "Вопрос 1"


@pytest.mark.asyncio
async def test_fetch_clean_article_extracts_og_image():
    """fetch_clean_article parses og:image from HTML metadata."""
    html_doc = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Новость СМИ</title>
        <meta property="og:image" content="https://images.example.com/articles/photo123.jpg">
    </head>
    <body>
        <main>
            <p>Президент Российской Федерации провел расширенное заседание правительственной комиссии по вопросам развития экономики и поддержки промышленных предприятий регионов.</p>
            <p>В ходе совещания обсуждались конкретные меры стимулирования высокотехнологичных производств, выделение целевых субсидий и создание новых рабочих мест для специалистов отрасли.</p>
            <p>По итогам встречи профильным министерствам было дано поручение подготовить детальный план реализации принятых решений до конца текущего месяца.</p>
        </main>
    </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.url = httpx.URL("https://example.com/news/1")
    mock_resp.content = html_doc.encode("utf-8")
    mock_resp.text = html_doc
    mock_resp.encoding = "utf-8"
    mock_resp.headers = {"content-type": "text/html"}

    with patch("app.services.web_researcher.is_safe_web_url", return_value=True):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            article = await fetch_clean_article("https://example.com/news/1")

    assert article is not None
    assert article.image_url == "https://images.example.com/articles/photo123.jpg"


@pytest.mark.asyncio
async def test_media_photo_endpoint_fallback():
    """GET /api/ui/lab/media/photo/{post_id} returns 200 SVG fallback on missing media."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/ui/lab/media/photo/999999999")
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers["content-type"]
        assert "<svg" in resp.text
