# ruff: noqa: RUF001, E501, S106, SIM117, F841, F401
"""Tests for Yandex Wordstat collection and semantic SEO fallback."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.wordstat_collector import (
    WordstatQueryItem,
    WordstatResult,
    WordstatService,
    extract_search_terms,
    get_wordstat_service,
)


def test_extract_search_terms_clean_phrases():
    """Extract search terms without emojis and filler words."""
    title = "⚡ Путин встретился с Президентом Казахстана в Кремле"
    terms = extract_search_terms(title, max_terms=3)
    assert len(terms) > 0
    # Emojis stripped
    assert "⚡" not in " ".join(terms)
    assert "путин" in terms[0] or "путин" in terms[1] or "путин" in terms[2]


def test_wordstat_query_item_serialization():
    """WordstatQueryItem serializes properly to dict."""
    item = WordstatQueryItem(
        query="новости москвы сегодня",
        count=45000,
        intent="informational",
        source="yandex_api",
    )
    d = item.to_dict()
    assert d["query"] == "новости москвы сегодня"
    assert d["count"] == 45000
    assert d["intent"] == "informational"
    assert d["source"] == "yandex_api"


def test_wordstat_result_serialization():
    """WordstatResult serializes properly."""
    res = WordstatResult(
        focus_keyword="путин токаев",
        queries=[{"query": "путин токаев", "count": 30000}],
        source="semantic_ai",
        seo_title="Переговоры в Кремле",
        seo_description="Лидеры обсудили ключевые вопросы",
        llm_txt="# Заголовок",
    )
    d = res.to_dict()
    assert d["focus_keyword"] == "путин токаев"
    assert len(d["queries"]) == 1
    assert d["source"] == "semantic_ai"


@pytest.mark.asyncio
async def test_wordstat_api_success():
    """Wordstat API returns parsed search queries."""
    service = WordstatService()

    fake_response = {
        "topRequests": [
            {"phrase": "встреча в кремле", "count": "32100"},
            {"phrase": "встреча в кремле онлайн", "count": 14200},
            {"phrase": "официальный сайт кремля", "count": 8900},
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response

    with patch.object(service, "_check_and_increment_quota", return_value=True):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            items = await service.fetch_wordstat_api("встреча в кремле", token="test_token")

    assert items is not None
    assert len(items) == 3
    assert items[0].query == "встреча в кремле"
    assert items[0].count == 32100
    assert items[0].source == "yandex_api"
    # Intent detection
    assert items[2].intent == "navigational"


@pytest.mark.asyncio
async def test_wordstat_api_rate_limit_fallback():
    """Wordstat API returns None on 429 rate limit."""
    service = WordstatService()

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = "Rate limit exceeded"

    with patch.object(service, "_check_and_increment_quota", return_value=True):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            items = await service.fetch_wordstat_api("тест", token="test_token")

    assert items is None


@pytest.mark.asyncio
async def test_semantic_queries_generation():
    """LLM semantic fallback produces structured queries."""
    service = WordstatService()
    session = AsyncMock()

    fake_json = json.dumps({
        "queries": [
            {"query": "переговоры москва", "count": 28000, "intent": "informational"},
            {"query": "почему перенесли встречу", "count": 9500, "intent": "investigative"},
        ]
    })

    mock_pool_res = MagicMock()
    mock_pool_res.value = [
        WordstatQueryItem(query="переговоры москва", count=28000, intent="informational", source="semantic_ai"),
        WordstatQueryItem(query="почему перенесли встречу", count=9500, intent="investigative", source="semantic_ai"),
    ]

    with patch("app.services.wordstat_collector.resolve_chain", return_value=["test_model"]):
        with patch("app.services.wordstat_collector.run_with_fallback", return_value=mock_pool_res):
            items = await service.generate_semantic_queries(session, "Переговоры в Москве", "Текст новости")

    assert len(items) == 2
    assert items[0].query == "переговоры москва"
    assert items[0].count == 28000
    assert items[0].source == "semantic_ai"


@pytest.mark.asyncio
async def test_collect_seo_and_wordstat_end_to_end():
    """collect_seo_and_wordstat orchestrates terms, queries, and llm.txt."""
    service = WordstatService()
    session = AsyncMock()

    mock_items = [
        WordstatQueryItem(query="тестовый запрос", count=15000, intent="informational", source="semantic_ai")
    ]

    with patch.object(service, "_get_cached_queries", return_value=None):
        with patch.object(service, "fetch_wordstat_api", return_value=None):
            with patch.object(service, "generate_semantic_queries", return_value=mock_items):
                with patch.object(service, "_set_cached_queries", return_value=None):
                    res = await service.collect_seo_and_wordstat(
                        session,
                        "⚡ Важная новость сегодня",
                        "Подробное описание события для проверки генерации SEO метаданных.",
                    )

    assert isinstance(res, WordstatResult)
    assert res.focus_keyword == "тестовый запрос"
    assert len(res.queries) == 1
    assert "Важная новость сегодня" in res.seo_title
    assert "Фокусный запрос" in res.llm_txt
    assert res.source == "semantic_ai"
