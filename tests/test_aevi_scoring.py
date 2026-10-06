"""Unit tests for AEVI scoring and closed-loop hypothesis evaluation."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.audience_metric import AudienceCategoryMetric
from app.models.cluster import NewsCluster
from app.models.enums import RiskLevel
from app.models.raw_news import RawNews
from app.services.aevi_scoring import (
    HypothesisRecord,
    calculate_cluster_aevi,
    calculate_clusters_aevi_batch,
    evaluate_hypothesis_outcome,
    sort_clusters_by_aevi,
)


def test_calculate_cluster_aevi_high_intent() -> None:
    """Cluster with strong financial intent and facts should yield a high AEVI score."""
    cluster = NewsCluster(
        id=uuid.uuid4(),
        title="ЦБ РФ повысил ключевую ставку: банки меняют тарифы по кредитам и вкладам",
        category="finance",
        risk_level=RiskLevel.GREEN,
    )

    p1 = RawNews(
        source_id=1,
        raw_text="Банк России принял решение поднять ставку до 21% годовых.",
    )
    p2 = RawNews(
        source_id=2,
        raw_text="Банки заявили об изменении ставок по кредитам на 1.5%.",  # noqa: RUF001
    )
    p3 = RawNews(
        source_id=3,
        raw_text="«Решение направлено на инфляцию», — заявило руководство.",
    )

    metric = AudienceCategoryMetric(
        platform="website",
        category_slug="finance",
        category_title="Финансы",
        avg_time_on_page=150.0,
        bounce_rate=0.18,
        audience_share=12.5,
    )

    breakdown = calculate_cluster_aevi(
        cluster, [p1, p2, p3], category_metrics={"finance": metric}
    )

    assert breakdown.total_score >= 60.0
    assert breakdown.level in ("viral", "high")
    assert breakdown.search_demand_score > 15.0
    assert breakdown.historical_engagement_score > 20.0
    assert breakdown.crypta_affinity_score >= 20.0
    assert breakdown.fact_richness_score > 10.0
    assert breakdown.predicted_views > 2000
    assert breakdown.predicted_reading_time_sec >= 120
    assert breakdown.predicted_ctr_pct >= 3.0


def test_calculate_cluster_aevi_baseline() -> None:
    """Cluster with minimal text and no category metrics should yield medium/low score."""
    cluster = NewsCluster(
        id=uuid.uuid4(),
        title="Короткая заметка",
        category="misc",
        risk_level=RiskLevel.GREEN,
    )
    p1 = RawNews(source_id=1, raw_text="Просто факт без деталей.")

    breakdown = calculate_cluster_aevi(cluster, [p1], category_metrics={})
    assert 0.0 <= breakdown.total_score <= 100.0
    assert breakdown.level in ("medium", "low")


@pytest.mark.asyncio
async def test_calculate_clusters_aevi_batch() -> None:
    """Batch calculation should process multiple clusters in one session query."""
    c1 = NewsCluster(
        id=uuid.uuid4(),
        title="Срочная новость про недвижимость и ипотеку",
        category="realty",
    )
    c2 = NewsCluster(
        id=uuid.uuid4(),
        title="Обычное сообщение культуры",
        category="culture",
    )

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [
        AudienceCategoryMetric(
            platform="website",
            category_slug="realty",
            category_title="Недвижимость",
            avg_time_on_page=130.0,
            bounce_rate=0.20,
            audience_share=8.0,
        )
    ]
    session.execute.return_value = mock_result

    scores = await calculate_clusters_aevi_batch(
        session, [c1, c2], posts_by_cluster={c1.id: [], c2.id: []}
    )

    assert len(scores) == 2
    assert scores[c1.id].total_score > scores[c2.id].total_score


def test_sort_clusters_by_aevi() -> None:
    """Groups should be sorted descending by AEVI score."""
    groups = [
        {"cluster_id": "1", "aevi_score": 42.5},
        {"cluster_id": "2", "aevi_score": 88.0},
        {"cluster_id": "3", "aevi_score": 64.1},
    ]
    sorted_groups = sort_clusters_by_aevi(groups)
    assert [g["cluster_id"] for g in sorted_groups] == ["2", "3", "1"]


def test_evaluate_hypothesis_outcome_winner() -> None:
    """When actual metrics beat hypothesis, verdict must be winner."""
    hyp = HypothesisRecord(
        cluster_id="clust_1",
        predicted_views=1500,
        predicted_reading_time_sec=110,
        predicted_ctr_pct=3.0,
        target_audience="Бизнес",
        rationale="Тест",
    )

    outcome = evaluate_hypothesis_outcome(
        hyp,
        actual_views=1850,
        actual_reading_time_sec=135.0,
        actual_ctr_pct=3.8,
    )
    assert outcome["is_winner"] is True
    assert outcome["delta_views"] == 350
    assert "🏆 Паттерн-победитель" in outcome["verdict"]
