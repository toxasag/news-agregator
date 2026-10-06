# ruff: noqa: RUF001, E501, S105, PLC0415
"""Tests for v2.35.0: VK Video upload from Lab and auto-mode web research enrichment."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.cluster import NewsCluster
from app.models.raw_news import RawNews
from app.services.lab_pipeline import run_cluster_media_step
from app.services.telegram_mtproto import MTProtoIngestor
from app.services.vk_video_service import UploadedVideo
from app.workers.content_worker import (
    ENRICH_CRON_MINUTES,
    _load_unenriched_clusters,
    enrich_task,
    run_enrichment_pass,
)


def test_enrich_cron_minutes_configuration() -> None:
    """Enrichment cron runs every 10 min offset from ingestion."""
    assert isinstance(ENRICH_CRON_MINUTES, frozenset)
    assert len(ENRICH_CRON_MINUTES) == 6
    assert 7 in ENRICH_CRON_MINUTES
    assert 17 in ENRICH_CRON_MINUTES


@pytest.mark.asyncio
async def test_load_unenriched_clusters_filtering() -> None:
    """Only clusters with missing or empty web_sources should be picked for enrichment."""
    session = AsyncMock()

    c_enriched = MagicMock(spec=NewsCluster)
    c_enriched.id = uuid.uuid4()
    c_enriched.enrichment_data = {"web_sources": [{"title": "News", "url": "https://a.com"}]}

    c_unenriched_empty = MagicMock(spec=NewsCluster)
    c_unenriched_empty.id = uuid.uuid4()
    c_unenriched_empty.enrichment_data = {"web_sources": []}

    c_unenriched_none = MagicMock(spec=NewsCluster)
    c_unenriched_none.id = uuid.uuid4()
    c_unenriched_none.enrichment_data = None

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [
        c_enriched,
        c_unenriched_empty,
        c_unenriched_none,
    ]
    session.execute.return_value = mock_result

    selected = await _load_unenriched_clusters(session, limit=10)
    assert len(selected) == 2
    assert c_unenriched_empty in selected
    assert c_unenriched_none in selected
    assert c_enriched not in selected


@pytest.mark.asyncio
async def test_run_enrichment_pass_success() -> None:
    """run_enrichment_pass updates cluster enrichment_data with research findings."""
    cluster = MagicMock(spec=NewsCluster)
    cluster.id = uuid.uuid4()
    cluster.enrichment_data = {}

    post = MagicMock(spec=RawNews)
    post.raw_text = "Новость дня о событии"

    research_output = {
        "web_sources": [{"title": "Источник 1", "url": "https://smi.ru/1"}],
        "web_facts": ["Факт 1", "Факт 2"],
        "web_images": [{"url": "https://smi.ru/img.jpg", "title": "Фото"}],
        "web_research": {"sources": [{"title": "Источник 1"}]},
        "search_query": "событие дня",
    }

    with (
        patch("app.workers.content_worker.get_redis") as mock_get_redis,
        patch("app.workers.content_worker.acquire_lock") as mock_acquire_lock,
        patch("app.workers.content_worker.AsyncSessionLocal") as mock_session_cls,
        patch("app.workers.content_worker._load_unenriched_clusters", return_value=[cluster]),
        patch("app.workers.content_worker._load_cluster_posts", return_value=[post]),
        patch("app.workers.content_worker.conduct_cluster_research", return_value=research_output),
    ):
        mock_redis = AsyncMock()
        mock_get_redis.return_value = mock_redis
        mock_lock_ctx = AsyncMock()
        mock_lock_ctx.__aenter__.return_value = True
        mock_acquire_lock.return_value = mock_lock_ctx

        mock_session = AsyncMock()
        mock_session_cls.return_value = mock_session

        res = await run_enrichment_pass(limit=5)

        assert res["enriched"] == 1
        assert res["failed"] == 0
        assert cluster.enrichment_data["web_sources"] == research_output["web_sources"]
        assert cluster.enrichment_data["web_facts"] == research_output["web_facts"]
        assert cluster.enrichment_data["web_images"] == research_output["web_images"]
        assert cluster.enrichment_data["search_query"] == "событие дня"
        mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_enrich_task_skips_when_production_disabled() -> None:
    """enrich_task skips gracefully when production mode is turned off."""
    with patch("app.workers.content_worker._production_allowed", return_value=False):
        res = await enrich_task({})
        assert res.get("skipped") is True
        assert res.get("reason") == "pipeline disabled"


@pytest.mark.asyncio
async def test_download_video_skips_non_documents() -> None:
    """download_video returns empty bytes if media is not a MessageMediaDocument."""
    from telethon.tl.patched import Message
    from telethon.tl.types import MessageMediaPhoto

    session = AsyncMock()
    ingestor = MTProtoIngestor(session)

    mock_client = AsyncMock()
    mock_entity = MagicMock()
    mock_client.get_messages.return_value = MagicMock(
        spec=Message,
        media=MagicMock(spec=MessageMediaPhoto),
    )
    ingestor._client = mock_client

    with patch.object(MTProtoIngestor, "_resolve_peer", return_value=mock_entity):
        data = await ingestor.download_video("test_channel", 123, limit_bytes=1000)
        assert data == b""
        mock_client.download_media.assert_not_called()


@pytest.mark.asyncio
async def test_lab_video_inspection_includes_vk_metadata() -> None:
    """run_cluster_media_step passes vk_embed_url and vk video IDs."""
    cluster_id = uuid.uuid4()
    cluster = MagicMock(spec=NewsCluster)
    cluster.id = cluster_id
    cluster.title = "Тестовая новость"
    cluster.enrichment_data = {}

    post = MagicMock(spec=RawNews)
    post.id = 999
    post.post_url = "https://t.me/news/42"
    post.source = MagicMock()
    post.source.name = "ТестТГ"
    post.source.username = "test_tg"
    post.media_data = [
        {
            "type": "video",
            "url": "https://t.me/news/42",
            "duration": 45,
            "vk_embed_url": "https://vk.com/video_ext.php?oid=123&id=456&hash=abc",
            "vk_owner_id": 123,
            "vk_video_id": 456,
        }
    ]

    session = AsyncMock()
    session.get.return_value = cluster
    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = [post]
    session.execute.return_value = mock_res

    with patch(
        "app.services.lab_pipeline.generate_editorial_image_prompt",
        return_value="photo of press conference",
    ):
        res = await run_cluster_media_step(session, cluster_id)

    images = res["images"]
    assert len(images) == 1
    video_item = images[0]
    assert video_item["type"] == "video"
    assert video_item["vk_embed_url"] == "https://vk.com/video_ext.php?oid=123&id=456&hash=abc"
    assert video_item["vk_owner_id"] == 123
    assert video_item["vk_video_id"] == 456


@pytest.mark.asyncio
async def test_lab_video_upload_to_vk_endpoint_success() -> None:
    """lab_video_upload_to_vk downloads, uploads to VK and returns player HTML."""
    from app.api.dashboard import lab_video_upload_to_vk

    cluster_id = uuid.uuid4()
    cluster = MagicMock(spec=NewsCluster)
    cluster.id = cluster_id
    cluster.title = "Сюжет с видео"
    cluster.enrichment_data = {}

    post = MagicMock(spec=RawNews)
    post.id = 101
    post.media_data = [{"type": "video", "url": "https://t.me/news/101"}]
    post.source = None

    uploaded_vid = UploadedVideo(
        owner_id=12345,
        video_id=67890,
        access_key="sec123",
        embed_url="https://vk.com/video_ext.php?oid=12345&id=67890&hash=sec123",
        embed_html="<iframe></iframe>",
        title="Сюжет с видео",
        size_bytes=1024,
    )

    session = AsyncMock()
    # First get is post, second get is cluster
    session.get.side_effect = [post, cluster]
    mock_art_res = MagicMock()
    mock_art_res.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_art_res

    req = MagicMock()
    admin = MagicMock()

    mock_settings = MagicMock()
    mock_settings.vk_access_token = "test_vk_token"

    with (
        patch("app.api.dashboard.settings", mock_settings),
        patch(
            "app.api.dashboard._resolve_post_video_bytes",
            return_value=(b"fake_video_bytes", "video.mp4"),
        ),
        patch(
            "app.api.dashboard.upload_article_video",
            return_value=uploaded_vid,
        ),
    ):
        resp = await lab_video_upload_to_vk(req, admin, session, cluster_id, 101)

    assert resp.status_code == 200
    assert "Загружено в VK Video" in resp.body.decode("utf-8")
    assert "https://vk.com/video_ext.php?oid=12345&id=67890&hash=sec123" in resp.body.decode("utf-8")
    # Verify post media_data was updated with vk fields
    assert post.media_data[0]["vk_owner_id"] == 12345
    assert post.media_data[0]["vk_video_id"] == 67890


@pytest.mark.asyncio
async def test_lab_video_upload_to_vk_missing_token() -> None:
    """lab_video_upload_to_vk warns when VK token is missing."""
    from app.api.dashboard import lab_video_upload_to_vk

    cluster_id = uuid.uuid4()
    cluster = MagicMock(spec=NewsCluster)
    post = MagicMock(spec=RawNews)

    session = AsyncMock()
    session.get.side_effect = [post, cluster]
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_res

    mock_settings = MagicMock()
    mock_settings.vk_access_token = ""
    mock_settings.vk_group_id = "236626422"

    with patch("app.api.dashboard.settings", mock_settings):
        resp = await lab_video_upload_to_vk(MagicMock(), MagicMock(), session, cluster_id, 101)

    assert "VK_ACCESS_TOKEN не задан" in resp.body.decode("utf-8")


def test_vk_video_config_browser_session_serialization() -> None:
    """VKVideoConfig should normalize, encrypt and restore browser session cookies."""
    from app.services.vk_video_service import VKVideoConfig, normalize_vk_storage_state

    raw_cookies = [
        {
            "name": "remixsid",
            "value": "vk_session_cookie_123",
            "domain": ".vk.com",
            "sameSite": "no_restriction",
        },
        {
            "name": "remixnsid",
            "value": "vk_nsid_456",
            "domain": ".vk.com",
        },
    ]
    norm = normalize_vk_storage_state(raw_cookies)
    assert len(norm["cookies"]) == 2
    assert norm["cookies"][0]["sameSite"] == "None"

    cfg = VKVideoConfig(
        group_id="236626422",
        storage_state=norm,
        is_active=True,
    )
    assert cfg.has_session is True
    assert cfg.is_ready is True

    db_doc = cfg.to_db_dict()
    assert "encrypted_storage_state" in db_doc
    restored = VKVideoConfig.from_db_dict(db_doc)
    assert restored.has_session is True
    assert restored.to_public_dict()["cookies_count"] == 2


@pytest.mark.asyncio
async def test_probe_vk_browser_session_extracts_web_token() -> None:
    """probe_vk_browser_session should extract vk1.a... token via Playwright."""
    from app.services.vk_video_service import VKVideoConfig, probe_vk_browser_session

    cfg = VKVideoConfig(
        group_id="236626422",
        storage_state={"cookies": [{"name": "remixsid", "value": "sec_val"}]},
    )

    page_mock = AsyncMock()
    page_mock.on = MagicMock()
    page_mock.url = "https://vk.com/club236626422"
    page_mock.title.return_value = "СводкаПресс | ВКонтакте"
    page_mock.evaluate.return_value = "vk1.a.test_extracted_web_token_1234567890abcdef"
    page_mock.query_selector.return_value = None
    page_mock.context = AsyncMock()
    page_mock.context.storage_state.return_value = cfg.storage_state

    ctx_mgr = AsyncMock()
    ctx_mgr.__aenter__.return_value = page_mock

    with (
        patch("app.services.web_scraper.get_browser_pool") as pool_mock,
        patch(
            "app.services.vk_video_service.VKVideoService.test_connection",
            AsyncMock(return_value={"ok": True, "name": "СводкаПресс"}),
        ),
    ):
        pool_mock.return_value.page.return_value = ctx_mgr
        res = await probe_vk_browser_session(cfg)
        assert res["is_valid"] is True
        assert cfg.access_token == "vk1.a.test_extracted_web_token_1234567890abcdef"
        assert cfg.group_name == "СводкаПресс"


