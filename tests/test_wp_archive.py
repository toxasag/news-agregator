# ruff: noqa: RUF001, E501, SIM117
"""Tests for the WordPress archive indexing and linking service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.wp_archive import WPArchiveService, extract_keywords


def test_extract_keywords():
    keywords = extract_keywords("Глава Nvidia впервые написал в X и вступил в спор с Вашингтоном")
    assert "nvidia" in keywords
    assert "глава" in keywords
    assert "впервые" in keywords
    assert "вашингтоном" in keywords
    # Short words (<4 chars) should be filtered
    assert "в" not in keywords
    assert "и" not in keywords
    assert "с" not in keywords
    assert "x" not in keywords


@pytest.mark.asyncio
async def test_find_related_by_keyword():
    mock_posts = [
        {"id": 1, "title": "Акции Nvidia выросли после отчета", "slug": "nvidia-shares", "link": "/nvidia-shares"},
        {"id": 2, "title": "Новый закон о налогах в России", "slug": "nalogi", "link": "/nalogi"},
        {"id": 3, "title": "Глава Вашингтона посетил форум", "slug": "washington", "link": "/washington"},
    ]

    with patch.object(WPArchiveService, "get_archive_posts", new=AsyncMock(return_value=mock_posts)):
        results = await WPArchiveService.find_related(MagicMock(), "Глава Nvidia посетил Вашингтон", limit=2)
        assert len(results) == 2
        # Nvidia or Washington matches should come first
        ids = [r["id"] for r in results]
        assert 1 in ids
        assert 3 in ids
        assert 2 not in ids


@pytest.mark.asyncio
async def test_wp_archive_total_count_cached():
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=b"8701")

    with patch("app.services.wp_archive.get_redis", return_value=fake_redis):
        with patch.object(WPArchiveService, "sync_recent_posts", new=AsyncMock(return_value=([], 0))) as mock_sync:
            total = await WPArchiveService.get_total_posts_count(MagicMock())
            assert total == 8701
            mock_sync.assert_not_called()
