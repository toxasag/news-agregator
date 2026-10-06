# ruff: noqa: RUF001
"""Unit tests for Lab features: unblocking, rubrication, linking, and key pool."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_provider_key import AIProviderKey
from app.models.article import Article
from app.models.category_prompt import CategoryPrompt
from app.models.cluster import NewsCluster
from app.models.enums import ArticleStatus, DedupStatus, RiskLevel
from app.models.raw_news import RawNews
from app.models.source import Source
from app.services.gemini_key_pool import (
    batch_add_gemini_keys,
    delete_gemini_key,
    get_next_healthy_key,
    list_gemini_keys,
    mark_key_cooldown,
)
from app.services.lab_pipeline import (
    _inject_internal_links,
    classify_cluster_category,
    create_custom_rubric,
    generate_fallback_image_for_cluster,
    unblock_raw_post,
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


class FakeFeatureSession:
    """In-memory async session double for lab features tests."""

    def __init__(self) -> None:
        self.sources: list[Source] = []
        self.raw_news: list[RawNews] = []
        self.clusters: dict[uuid.UUID, NewsCluster] = {}
        self.articles: dict[uuid.UUID, Article] = {}
        self.category_prompts: dict[str, CategoryPrompt] = {}
        self.provider_keys: list[AIProviderKey] = []
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.commits = 0

    async def execute(self, stmt: Any) -> _ScalarVal:
        text_stmt = str(stmt).lower()
        if text_stmt.strip().startswith("update") and "ai_provider_key" in text_stmt:
            for k in self.provider_keys:
                if f"id = {k.id}" in text_stmt or k.id == 1:
                    k.cooldown_until = datetime.now(UTC) + timedelta(seconds=60)
                    break
            return _ScalarVal(1)

        result_map: dict[str, list[Any]] = {
            "category_prompt": list(self.category_prompts.values()),
            "ai_provider_key": list(self.provider_keys),
            "article": list(self.articles.values()),
            "raw_news": list(self.raw_news),
            "news_cluster": list(self.clusters.values()),
        }
        for key, val in result_map.items():
            if key in text_stmt:
                return _ScalarVal(val)
        return _ScalarVal([])

    async def get(self, entity: type, ident: Any, *args: Any, **kwargs: Any) -> Any:
        mapping: dict[type, dict[Any, Any]] = {
            NewsCluster: self.clusters,
            Article: self.articles,
            CategoryPrompt: self.category_prompts,
        }
        if entity in mapping:
            return mapping[entity].get(ident)
        if entity is RawNews:
            return next((p for p in self.raw_news if p.id == ident), None)
        if entity is AIProviderKey:
            return next((k for k in self.provider_keys if k.id == ident), None)
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, Article):
            self.articles[obj.cluster_id] = obj
        elif isinstance(obj, CategoryPrompt):
            self.category_prompts[obj.slug] = obj
        elif isinstance(obj, NewsCluster):
            if not getattr(obj, "id", None):
                obj.id = uuid.uuid4()
            self.clusters[obj.id] = obj
        elif isinstance(obj, RawNews):
            self.raw_news.append(obj)
        elif isinstance(obj, AIProviderKey):
            if not obj.id:
                obj.id = len(self.provider_keys) + 1
            self.provider_keys.append(obj)

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)
        if isinstance(obj, AIProviderKey) and obj in self.provider_keys:
            self.provider_keys.remove(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        pass


def cast_session(s: FakeFeatureSession) -> AsyncSession:
    return cast(AsyncSession, s)


@pytest.mark.asyncio
async def test_unblock_raw_post() -> None:
    session = FakeFeatureSession()
    post = RawNews(
        id=555,
        source_id=1,
        raw_text="⚡️ Хоттабыч — вышел новый официальный трейлер приключенческой комедии.",
        is_digest=True,
        is_ad=False,
        filter_reasons=["digest_structure"],
        dedup_status=DedupStatus.FILTERED_DIGEST,
        created_at=datetime.now(UTC),
    )
    session.raw_news.append(post)

    result = await unblock_raw_post(cast_session(session), 555)
    assert result["ok"] is True
    assert result["post_id"] == 555
    assert post.is_digest is False
    assert post.is_ad is False
    assert not post.filter_reasons
    assert post.dedup_status == DedupStatus.UNIQUE
    assert post.cluster_id is not None
    assert post.cluster_id in session.clusters
    created_cluster = session.clusters[post.cluster_id]
    assert created_cluster.enrichment_data is not None


@pytest.mark.asyncio
async def test_classify_cluster_category_heuristic() -> None:
    session = FakeFeatureSession()
    cid = uuid.uuid4()
    cluster = NewsCluster(
        id=cid,
        title="Новый фильм Хоттабыч",
        category="society",
        risk_level=RiskLevel.GREEN,
        enrichment_data={},
    )
    session.clusters[cid] = cluster

    post = RawNews(
        id=1,
        cluster_id=cid,
        raw_text=(
            "Вышел трейлер фильма Хоттабыч, кинотеатры готовятся к премьере комедии."
        ),
        dedup_status=DedupStatus.UNIQUE,
    )
    session.raw_news.append(post)

    # Force AI failure to test heuristic fallback
    with patch(
        "app.services.lab_pipeline.get_ai_client",
        side_effect=RuntimeError("AI client unavailable in test"),
    ):
        result = await classify_cluster_category(cast_session(session), cid)

    assert result["cluster_id"] == str(cid)
    # Heuristic matches "фильм", "кино", "трейлер"
    assert result["category_slug"] in ("kino-i-serialy", "novosti-shou-biznesa")
    assert cluster.category == result["category_slug"]
    assert cluster.enrichment_data["ai_classified"] is True
    assert (
        "обоснование" in result["justification"].lower()
        or len(result["justification"]) > 5
    )


@pytest.mark.asyncio
async def test_create_custom_rubric() -> None:
    session = FakeFeatureSession()
    cid = uuid.uuid4()
    cluster = NewsCluster(
        id=cid,
        title="Презентация новой нейросети",
        category="society",
        risk_level=RiskLevel.GREEN,
        enrichment_data={},
    )
    session.clusters[cid] = cluster

    res = await create_custom_rubric(
        cast_session(session),
        cluster_id=cid,
        slug="ai-tech",
        title="Искусственный интеллект",
        description="События и релизы нейросетей",
    )

    assert res["slug"] == "ai-tech"
    assert res["title"] == "Искусственный интеллект"
    assert cluster.category == "ai-tech"
    assert "ai-tech" in session.category_prompts
    prompt_obj = session.category_prompts["ai-tech"]
    assert prompt_obj.title == "Искусственный интеллект"
    assert prompt_obj.is_custom is True


@pytest.mark.asyncio
async def test_generate_fallback_image_for_cluster() -> None:
    session = FakeFeatureSession()
    cid = uuid.uuid4()
    cluster = NewsCluster(
        id=cid,
        title="Космическая миссия на Марс",
        category="science",
        risk_level=RiskLevel.GREEN,
        enrichment_data={},
    )
    session.clusters[cid] = cluster

    with patch(
        "app.services.lab_pipeline.generate_cover_image",
        new_callable=AsyncMock,
        return_value=b"\xff\xd8\xff\xe0\x00\x10JFIFfakeimagebytes",
    ):
        res = await generate_fallback_image_for_cluster(
            cast_session(session),
            cluster_id=cid,
            prompt="Марсоход на красной планете",
        )

    assert len(res["images"]) == 1
    image_dict = res["images"][0]
    assert image_dict["is_generated"] is True
    assert image_dict["url"].startswith("data:image/jpeg;base64,")
    assert (
        cluster.enrichment_data["fallback_image_prompt"]
        == "Марсоход на красной планете"
    )
    assert len(cluster.enrichment_data["media_verdicts"]) == 1


@pytest.mark.asyncio
async def test_inject_internal_links() -> None:
    session = FakeFeatureSession()
    past_art = Article(
        id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        title="Рекордные кассовые сборы в кинотеатрах России",
        url_slug="rekordnye-sbory-kinoteatry",
        category="culture",
        status=ArticleStatus.WP_PUBLISHED,
        content_html="<p>Прошлые новости киноиндустрии.</p>",
    )
    session.articles[past_art.cluster_id] = past_art

    cid = uuid.uuid4()
    html_input = (
        "<p>Российские кинотеатры готовятся к новогодним праздникам.</p>"
    )

    html_out, injected = await _inject_internal_links(
        cast_session(session),
        cluster_id=cid,
        html_content=html_input,
        cluster_title="Кинотеатры показывают новые трейлеры",
    )

    assert len(injected) >= 1
    assert "rekordnye-sbory-kinoteatry" in html_out
    assert "class=\"internal-link" in html_out or "class=\"text-[#0071E3]" in html_out


@pytest.mark.asyncio
async def test_gemini_key_pool_operations() -> None:
    session = FakeFeatureSession()

    # 1. Batch add keys
    raw_keys = """
    AIzaSyDummyKeyNumberOneForTesting111
    AIzaSyDummyKeyNumberTwoForTesting222
    AIzaSyDummyKeyNumberOneForTesting111
    """
    batch_res = await batch_add_gemini_keys(cast_session(session), raw_keys)
    assert batch_res["added"] == 2
    assert len(session.provider_keys) == 2

    # 2. List keys
    listed = await list_gemini_keys(cast_session(session))
    assert len(listed) == 2
    assert "AIzaSy" in listed[0]["masked_key"]
    assert listed[0]["is_active"] is True

    # 3. Rotate and retrieve key
    id1, key1 = await get_next_healthy_key(cast_session(session))
    assert "AIzaSy" in key1

    # 4. Trigger rate limit cooldown on key1
    await mark_key_cooldown(
        cast_session(session), id1, reason="429 Rate Limit", seconds=60
    )
    assert session.provider_keys[0].cooldown_until is not None
    assert session.provider_keys[0].cooldown_until > datetime.now(UTC)

    # 5. Failover to key2
    id2, key2 = await get_next_healthy_key(cast_session(session))
    assert id2 != id1
    assert "AIzaSy" in key2

    # 6. Delete key
    del_ok = await delete_gemini_key(cast_session(session), id2)
    assert del_ok is True
    assert len(session.provider_keys) == 1
