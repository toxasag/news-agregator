# ruff: noqa: RUF001
"""Tests for syndicated feeds: Yandex News, Yandex Turbo, and RSS 2.0."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.feeds import _clean_cdata, xml_escape
from app.database import get_db
from app.main import app
from app.models.enums import ArticleStatus


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    def __init__(self, articles: list[Any]) -> None:
        self._articles = articles

    async def execute(self, statement: Any) -> _Result:
        return _Result(self._articles)

    def as_session(self) -> AsyncSession:
        return cast(AsyncSession, self)


def test_xml_escape_and_cdata_cleaning() -> None:
    """Check XML characters and nested CDATA sanitisation."""
    raw = 'Test & <special> "chars" \']]>\''
    escaped = xml_escape(raw)
    assert "&amp;" in escaped
    assert "&lt;" in escaped
    assert "&gt;" in escaped

    cdata_cleaned = _clean_cdata('Safe <p>text</p> with ]]> inside')
    assert "]]>" not in cdata_cleaned
    assert "]]&gt;" in cdata_cleaned


@pytest.mark.asyncio
async def test_feeds_endpoints_return_valid_xml() -> None:
    """Ensure feed routes respond with RSS content-type and valid headers."""
    fake_article = MagicMock()
    fake_article.id = 101
    fake_article.url_slug = "test-breaking-news"
    fake_article.title = "Тестовая новость дня"
    fake_article.seo_title = "Тестовая новость дня — Сводка"
    fake_article.seo_description = "Краткое описание события для сниппета."
    fake_article.category = "Политика"
    fake_article.content_html = '<p>Полный текст новости с картинкой <img src="https://svodka.press/img/photo.jpg"/></p>'
    fake_article.status = ArticleStatus.WP_PUBLISHED
    fake_article.published_at = datetime(2026, 10, 3, 15, 0, 0, tzinfo=UTC)
    fake_article.created_at = datetime(2026, 10, 3, 14, 55, 0, tzinfo=UTC)

    async def _mock_get_db() -> AsyncIterator[AsyncSession]:
        yield FakeSession([fake_article]).as_session()

    app.dependency_overrides[get_db] = _mock_get_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Yandex News feed
            resp_yn = await client.get("/api/feeds/yandex-news.xml")
            assert resp_yn.status_code == 200
            assert "application/rss+xml" in resp_yn.headers.get("content-type", "")
            text_yn = resp_yn.text
            assert 'xmlns:yandex="http://news.yandex.ru"' in text_yn
            assert "<yandex:full-text>" in text_yn
            assert "https://svodka.press/test-breaking-news/" in text_yn
            assert "https://svodka.press/img/photo.jpg" in text_yn

            # 2. Yandex Turbo feed
            resp_turbo = await client.get("/api/feeds/turbo.xml")
            assert resp_turbo.status_code == 200
            assert "application/rss+xml" in resp_turbo.headers.get("content-type", "")
            text_turbo = resp_turbo.text
            assert 'xmlns:turbo="http://turbo.yandex.net"' in text_turbo
            assert "<turbo:content>" in text_turbo
            assert '<item turbo="true">' in text_turbo

            # 3. Standard RSS 2.0 feed
            resp_rss = await client.get("/api/feeds/rss.xml")
            assert resp_rss.status_code == 200
            assert "application/rss+xml" in resp_rss.headers.get("content-type", "")
            text_rss = resp_rss.text
            assert '<rss version="2.0"' in text_rss
            assert "<channel>" in text_rss
            assert "<enclosure" in text_rss
    finally:
        app.dependency_overrides.clear()
