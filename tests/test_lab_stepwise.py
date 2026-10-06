# ruff: noqa: RUF001, E501
"""Unit tests for the stepwise laboratory service and endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dashboard import dashboard_lab
from app.models.article import Article
from app.models.cluster import NewsCluster
from app.models.enums import ArticleStatus, DedupStatus, RiskLevel, SourceType
from app.models.raw_news import RawNews
from app.models.source import Source
from app.services.deduplicator import ENTITY_KEY_MATCHED_CLUSTER
from app.services.lab_pipeline import (
    _format_raw_post,
    _resolve_post_cid,
    get_cluster_detail,
    get_lab_system_status,
    get_news_groups_summary,
    run_cluster_generate_step,
    run_cluster_media_step,
    run_cluster_publish_step,
    run_cluster_research_step,
)


class _ScalarVal:
    def __init__(self, val: Any) -> None:
        self._val = val

    def scalar_one(self) -> Any:
        return self._val

    def scalar_one_or_none(self) -> Any:
        return self._val

    def scalars(self) -> _ScalarVal:
        return self

    def first(self) -> Any:
        if isinstance(self._val, list):
            return self._val[0] if self._val else None
        return self._val

    def all(self) -> list[Any]:
        if isinstance(self._val, list):
            return self._val
        return [self._val] if self._val is not None else []


class FakeLabSession:
    """In-memory double for AsyncSession testing lab_pipeline."""

    def __init__(self) -> None:
        self.sources: list[Source] = []
        self.raw_news: list[RawNews] = []
        self.clusters: dict[uuid.UUID, NewsCluster] = {}
        self.articles: dict[uuid.UUID, Article] = {}
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, stmt: Any) -> _ScalarVal:
        text_stmt = str(stmt).lower()
        if "count(" in text_stmt or "count_1" in text_stmt:
            counts = {
                "sources": len(self.sources),
                "raw_news": len(self.raw_news),
                "news_clusters": len(self.clusters),
                "articles": len(self.articles),
            }
            val = next((cnt for key, cnt in counts.items() if key in text_stmt), 0)
            return _ScalarVal(val)

        res: list[Any] = []
        if "news_clusters" in text_stmt:
            res = list(self.clusters.values())
        elif "raw_news" in text_stmt:
            res = list(self.raw_news)
        elif "articles" in text_stmt:
            res = list(self.articles.values())
        return _ScalarVal(res)

    async def get(self, entity: type, ident: Any) -> Any:
        if entity is NewsCluster:
            return self.clusters.get(ident)
        if entity is Article:
            return self.articles.get(ident)
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, Article):
            self.articles[obj.cluster_id] = obj

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_get_lab_system_status() -> None:
    session = FakeLabSession()
    session.sources = [
        Source(id=1, name="Mash", source_type=SourceType.TELEGRAM, is_active=True),
        Source(id=2, name="Baza", source_type=SourceType.TELEGRAM, is_active=True),
    ]
    status = await get_lab_system_status(cast_session(session))
    assert status["total_sources"] == 2
    assert status["active_sources"] == 2
    assert status["queued_posts"] == 0
    assert status["is_first_pass"] is True
    assert status["posts_per_source"] > 0


def test_format_raw_post_and_resolve_cid() -> None:
    cid = uuid.uuid4()
    source = Source(id=10, name="Readovka", username="readovkanews")
    post = RawNews(
        id=1,
        source_id=10,
        cluster_id=cid,
        raw_text="Тестовая важная новость дня",
        dedup_status=DedupStatus.UNIQUE,
        similarity_score=1.0,
        external_post_id="999",
    )
    post.source = source

    data, is_primary = _format_raw_post(post, cluster_id=cid)
    assert is_primary is True
    assert data["source_name"] == "Readovka"
    assert data["post_url"] == "https://t.me/readovkanews/999"
    assert data["similarity"] == 1.0

    # Test resolving cluster id from entities on duplicate
    dup_post = RawNews(
        id=2,
        source_id=10,
        cluster_id=None,
        raw_text="Дубликат новости",
        dedup_status=DedupStatus.DUPLICATE,
        entities={ENTITY_KEY_MATCHED_CLUSTER: str(cid)},
    )
    resolved = _resolve_post_cid(dup_post, {str(cid)})
    assert resolved == cid


@pytest.mark.asyncio
async def test_get_news_groups_summary() -> None:
    session = FakeLabSession()
    cid = uuid.uuid4()
    cluster = NewsCluster(
        id=cid,
        title="Большой сюжет",
        category="society",
        risk_level=RiskLevel.GREEN,
        created_at=datetime.now(UTC),
    )
    session.clusters[cid] = cluster

    s1 = Source(id=1, name="Канал 1")
    s2 = Source(id=2, name="Канал 2")

    p1 = RawNews(
        id=101,
        source_id=1,
        cluster_id=cid,
        raw_text="Текст из канала 1",
        dedup_status=DedupStatus.UNIQUE,
        created_at=datetime.now(UTC),
    )
    p1.source = s1

    p2 = RawNews(
        id=102,
        source_id=2,
        cluster_id=None,
        raw_text="Текст из канала 2",
        dedup_status=DedupStatus.DUPLICATE,
        entities={ENTITY_KEY_MATCHED_CLUSTER: str(cid)},
        created_at=datetime.now(UTC),
    )
    p2.source = s2

    session.raw_news = [p1, p2]

    summary = await get_news_groups_summary(cast_session(session))
    assert summary["total_clusters"] == 1
    assert summary["multi_source_clusters"] == 1
    group = summary["groups"][0]
    assert group["source_count"] == 2
    assert len(group["duplicates"]) == 1
    assert group["primary_post"]["source_name"] == "Канал 1"


@pytest.mark.asyncio
async def test_run_cluster_research_step() -> None:
    session = FakeLabSession()
    cid = uuid.uuid4()
    cluster = NewsCluster(
        id=cid,
        title="Сюжет про космос",
        category="science",
        risk_level=RiskLevel.GREEN,
        enrichment_data={},
    )
    session.clusters[cid] = cluster

    res = await run_cluster_research_step(cast_session(session), cid)
    assert res["cluster_id"] == str(cid)
    assert "facts" in res
    assert "facts" in cluster.enrichment_data
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_run_cluster_media_step() -> None:
    session = FakeLabSession()
    cid = uuid.uuid4()
    cluster = NewsCluster(
        id=cid,
        title="Сюжет с фото",
        category="city",
        risk_level=RiskLevel.GREEN,
        enrichment_data={},
    )
    session.clusters[cid] = cluster

    post = RawNews(
        id=1,
        cluster_id=cid,
        media_data=[{"type": "photo", "url": "https://example.com/p.jpg"}],
    )
    session.raw_news = [post]

    res = await run_cluster_media_step(cast_session(session), cid)
    assert len(res["images"]) == 1
    assert res["images"][0]["verdict"] == "clean"
    assert "media_verdicts" in cluster.enrichment_data


@pytest.mark.asyncio
async def test_run_cluster_generate_and_publish_step(fake_redis: Any) -> None:
    session = FakeLabSession()
    cid = uuid.uuid4()
    cluster = NewsCluster(
        id=cid,
        title="Важная новость спорта",
        category="sport",
        risk_level=RiskLevel.GREEN,
        enrichment_data={},
    )
    session.clusters[cid] = cluster

    post = RawNews(
        id=1,
        cluster_id=cid,
        raw_text="Футбольный клуб победил в финальном матче со счетом 3:1.",
    )
    session.raw_news = [post]

    with (
        patch(
            "app.services.lab_pipeline.moderate_cluster",
            new_callable=AsyncMock,
        ) as mock_mod,
        patch(
            "app.services.lab_pipeline.run_with_fallback",
            new_callable=AsyncMock,
        ) as mock_pool,
    ):
        mock_verdict = MagicMock()
        mock_verdict.blocks_generation = False
        mock_verdict.category = "sport"
        mock_verdict.risk_level = RiskLevel.GREEN
        mock_mod.return_value = mock_verdict

        mock_fallback = MagicMock()
        mock_fallback.value = {
            "title": "Победа в финале",
            "content_html": "<p>Футбольный клуб одержал уверенную победу.</p>",
            "seo_title": "Победа в финале",
            "seo_description": "Футбольный клуб победил со счетом 3:1",
            "seo_keywords": ["спорт", "футбол"],
            "category_slug": "sport",
        }
        mock_fallback.used_fallback = False
        mock_fallback.model = "test-model"
        mock_pool.return_value = mock_fallback

        gen_res = await run_cluster_generate_step(
            cast_session(session), cid, style_angle="Информационный"
        )
        assert gen_res["title"] == "Победа в финале"
        assert cid in session.articles
        assert session.articles[cid].status == ArticleStatus.DRAFT

    # Now test publish step with mocked publisher and active WP config
    with (
        patch(
            "app.services.lab_pipeline.get_active_wp_publisher",
            new_callable=AsyncMock,
        ) as mock_pub_getter,
        patch(
            "app.services.wp_connection.load_wp_connection",
            new_callable=AsyncMock,
        ) as mock_load_wp,
    ):
        mock_load_wp.return_value = MagicMock(has_rest_credentials=True)
        mock_publisher = AsyncMock()
        mock_pub_res = MagicMock()
        mock_pub_res.post_id = 777
        mock_pub_res.link = "https://svodka.press/sport/pobeda-v-finale"
        mock_pub_res.tag_ids = [10, 20]
        mock_pub_res.warnings = []
        mock_publisher.publish.return_value = mock_pub_res
        mock_pub_getter.return_value = mock_publisher

        pub_res = await run_cluster_publish_step(cast_session(session), cid)
        assert pub_res["ok"] is True
        assert pub_res["post_id"] == 777
        assert "svodka.press" in pub_res["post_url"]
        assert session.articles[cid].status == ArticleStatus.WP_PUBLISHED
        assert cluster.is_published is True


@pytest.mark.asyncio
async def test_get_cluster_detail() -> None:
    session = FakeLabSession()
    cid = uuid.uuid4()
    cluster = NewsCluster(
        id=cid,
        title="Детальный сюжет",
        category="culture",
        risk_level=RiskLevel.GREEN,
        enrichment_data={"facts": ["Факт 1"]},
    )
    session.clusters[cid] = cluster

    detail = await get_cluster_detail(cast_session(session), cid)
    assert detail is not None
    assert detail["cluster_id"] == str(cid)
    assert detail["title"] == "Детальный сюжет"
    assert "facts" in detail
    assert "aevi" in detail
    assert "hypothesis" in detail
    assert detail["aevi"]["total_score"] >= 0.0

    # Unknown cluster returns None
    missing = await get_cluster_detail(cast_session(session), uuid.uuid4())
    assert missing is None


@pytest.mark.asyncio
async def test_get_news_groups_summary_with_aevi() -> None:
    """News groups summary must batch calculate AEVI and sort highest interest first."""
    session = FakeLabSession()

    # Viral financial cluster
    c_viral = NewsCluster(
        id=uuid.uuid4(),
        title="ЦБ РФ повысил ключевую ставку: банки меняют тарифы по кредитам и вкладам",
        category="finance",
        risk_level=RiskLevel.GREEN,
    )
    # Baseline cluster
    c_base = NewsCluster(
        id=uuid.uuid4(),
        title="Обычная городская хроника",
        category="misc",
        risk_level=RiskLevel.GREEN,
    )
    session.clusters[c_viral.id] = c_viral
    session.clusters[c_base.id] = c_base

    post1 = RawNews(
        id=101,
        cluster_id=c_viral.id,
        raw_text="Банк России повысил ключевую ставку до рекордных 21% годовых.",
        dedup_status=DedupStatus.UNIQUE,
    )
    post2 = RawNews(
        id=102,
        cluster_id=c_viral.id,
        raw_text="Банки объявили об изменении ставок по кредитам и накопительным счетам на 1.5%.",
        dedup_status=DedupStatus.UNIQUE,
    )
    post3 = RawNews(
        id=103,
        cluster_id=c_base.id,
        raw_text="В парке установили новые скамейки для отдыха жителей.",
        dedup_status=DedupStatus.UNIQUE,
    )
    session.raw_news = [post1, post2, post3]

    res = await get_news_groups_summary(cast_session(session), limit=50)
    assert res["displayed_clusters"] == 2
    groups = res["groups"]

    # Verify AEVI fields are populated on all groups
    for g in groups:
        assert "aevi" in g
        assert "aevi_score" in g
        assert "aevi_level" in g
        assert "aevi_badge" in g
        assert "predicted_views" in g
        assert "predicted_reading_sec" in g
        assert "predicted_ctr_pct" in g
        assert "target_audience" in g
        assert g["aevi_score"] >= 0.0

    # High interest financial cluster must be ranked first (sorted by aevi_score desc)
    assert groups[0]["cluster_id"] == str(c_viral.id)
    assert groups[0]["aevi_score"] > groups[1]["aevi_score"]
    assert groups[0]["aevi_level"] in ("viral", "high")


@pytest.mark.asyncio
async def test_get_cluster_detail_multichannel_and_hypothesis() -> None:
    """Cluster detail with an article must render all 4 channel previews and registered hypothesis."""
    session = FakeLabSession()
    cid = uuid.uuid4()
    cluster = NewsCluster(
        id=cid,
        title="Инновации в строительстве жилья: новые правила ипотеки",
        category="realty",
        risk_level=RiskLevel.GREEN,
        enrichment_data={"facts": ["Факт об ипотеке 1", "Факт 2"]},
    )
    session.clusters[cid] = cluster

    article = Article(
        id=555,
        cluster_id=cid,
        title="Новые правила ипотеки в 2026 году: что изменится для покупателей",
        content_html="<p>В России вступают в силу обновленные правила жилищного кредитования.</p><p>Второй абзац с анализом процентных ставок и льготных программ.</p><p>Эксперты прогнозируют стабилизацию рынка недвижимости.</p>",
        seo_description="Подробный разбор новых правил ипотеки и ставок на недвижимость.",
        seo_keywords=["недвижимость", "ипотека", "ставки"],
        status=ArticleStatus.DRAFT,
    )
    session.articles[cid] = article

    detail = await get_cluster_detail(cast_session(session), cid)
    assert detail is not None
    assert detail["article"] is not None
    assert detail["aevi"] is not None
    assert detail["hypothesis"] is not None

    # Check hypothesis fields
    hypo = detail["hypothesis"]
    assert hypo["cluster_id"] == str(cid)
    assert hypo["predicted_views"] > 0
    assert hypo["predicted_reading_time_sec"] > 0
    assert hypo["predicted_ctr_pct"] > 0

    # Check multichannel previews
    assert "dzen_preview_html" in detail
    assert "tg_caption" in detail
    assert "tg_hashtags" in detail
    assert "vk_wall_message" in detail

    assert len(detail["dzen_preview_html"]) > 0
    assert "Партнерский материал" in detail["dzen_preview_html"] or "dzen-native-banner" in detail["dzen_preview_html"] or "<p>" in detail["dzen_preview_html"]
    assert len(detail["tg_caption"]) > 0
    assert len(detail["vk_wall_message"]) > 0


@pytest.mark.asyncio
async def test_dashboard_lab_drawer_query_param() -> None:
    session = FakeLabSession()
    cid = uuid.uuid4()
    cluster = NewsCluster(
        id=cid,
        title="Тест открытия шторки через параметр адреса",
        risk_level=RiskLevel.GREEN,
    )
    session.clusters[cid] = cluster

    mock_request = MagicMock()
    mock_request.scope = {"type": "http"}
    mock_request.state = MagicMock()
    mock_admin = MagicMock()

    with patch("app.api.dashboard.templates.TemplateResponse") as mock_tmpl:
        mock_tmpl.return_value = MagicMock()
        await dashboard_lab(
            mock_request,
            mock_admin,
            cast_session(session),
            cluster=cid,
        )
        assert mock_tmpl.called
        context = mock_tmpl.call_args[0][2]
        assert context["selected_cluster"] is not None
        assert context["selected_cluster_id"] == str(cid)


def cast_session(s: FakeLabSession) -> AsyncSession:
    """Helper to satisfy type checker."""
    return cast(AsyncSession, s)
