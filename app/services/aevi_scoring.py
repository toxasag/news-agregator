# ruff: noqa: RUF001, E501
"""Audience Engagement & Virality Index (AEVI) Scoring Engine.

Calculates an algorithmic score (0.0 to 100.0) determining which news stories
are the most engaging, virally potent, and commercially viable for readers.

Operates according to the 6-parameter Autonomous Systems Standard:
1. Status: Active calculation toggle
2. Interactions: PostgreSQL (AudienceCategoryMetric), Wordstat, Crypta, RawNews
3. Operation: Deterministic 4-vector algorithmic scoring
4. Goal: Prioritize highest-converting stories in the Laboratory
5. Verification: Post-publish delta comparison (Fact vs Plan)
6. Feedback: Extraction and persistence of winning editorial patterns
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audience_metric import AudienceCategoryMetric
from app.models.cluster import NewsCluster
from app.models.raw_news import RawNews

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "AEVIBreakdown",
    "CampaignQualityReport",
    "HypothesisRecord",
    "calculate_cluster_aevi",
    "calculate_clusters_aevi_batch",
    "evaluate_campaign_quality",
    "evaluate_hypothesis_outcome",
    "sort_clusters_by_aevi",
]

# Core Crypta interest clusters matched against our verified reader demographics
CRYPTA_CORE_INTERESTS: Final[dict[str, list[str]]] = {
    "finance": [
        "банк", "рубл", "валют", "инфляц", "кредит", "вклад", "нал", "акци", "фонд",
    ],
    "property": [
        "недвижим", "жиль", "ипотек", "квартир", "дом", "застройщ", "аренд",
    ],
    "business": [
        "бизнес", "предприним", "компани", "рынок", "экспорт", "импорт", "торговл",
    ],
    "auto": [
        "авто", "машин", "транспорт", "дорог", "гибдд", "водител", "трасс",
    ],
    "tech": [
        "технолог", "ии", "нейросет", "it", "софт", "смартфон", "робот",
    ],
    "health": [
        "здоров", "врач", "медицин", "лекарств", "больниц", "минздрав",
    ],
    "family": [
        "семь", "дет", "школ", "пособи", "выплат", "родител", "детсад",
    ],
}


@dataclass(slots=True)
class AEVIBreakdown:
    """Algorithmic breakdown of Audience Engagement & Virality Index (0-100)."""

    total_score: float
    level: str  # "viral", "high", "medium", "low"
    badge_text: str
    badge_color: str  # Tailwind color classes
    search_demand_score: float  # 0 to 30
    historical_engagement_score: float  # 0 to 30
    crypta_affinity_score: float  # 0 to 25
    fact_richness_score: float  # 0 to 15
    target_audience: str
    predicted_views: int
    predicted_reading_time_sec: int
    predicted_ctr_pct: float
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        """Convert breakdown to serializable dictionary."""
        return asdict(self)

    def to_hypothesis(self, cluster_id: str) -> HypothesisRecord:
        """Create a registered hypothesis record for closed-loop evaluation."""
        return HypothesisRecord(
            cluster_id=cluster_id,
            predicted_views=self.predicted_views,
            predicted_reading_time_sec=self.predicted_reading_time_sec,
            predicted_ctr_pct=self.predicted_ctr_pct,
            target_audience=self.target_audience,
            rationale=self.rationale,
        )


@dataclass(slots=True)
class HypothesisRecord:
    """Pre-publish editorial hypothesis registered for closed-loop verification."""

    cluster_id: str
    predicted_views: int
    predicted_reading_time_sec: int
    predicted_ctr_pct: float
    target_audience: str
    rationale: str
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compute_search_demand_score(
    cluster: NewsCluster, text_sample: str, posts_count: int
) -> float:
    """Calculate Search Demand score (0.0 to 30.0) based on topic volume and urgency."""
    score = 10.0
    text_lower = text_sample.lower()

    hot_markers = [
        "закон", "выплат", "указ", "правительств", "путин", "минфин", "цб", "ставк",
        "пенси", "тариф", "налог", "штраф", "льгот", "госуслуг", "новый", "срочн",
    ]
    matched_markers = sum(1 for m in hot_markers if m in text_lower)
    if matched_markers >= 4:
        score += 15.0
    elif matched_markers >= 2:
        score += 10.0
    elif matched_markers >= 1:
        score += 5.0

    if cluster.title and len(cluster.title) > 20:
        score += 3.0
    if posts_count >= 3:
        score += 2.0

    return min(round(score, 1), 30.0)


def _compute_historical_engagement(
    category_slug: str | None,
    metrics_map: Mapping[str, AudienceCategoryMetric] | None,
) -> float:
    """Calculate Historical Engagement score (0.0 to 30.0) from actual metrics."""
    if not category_slug or not metrics_map or category_slug not in metrics_map:
        return 16.0

    metric = metrics_map[category_slug]
    avg_sec = metric.avg_time_on_page
    bounce = metric.bounce_rate

    if avg_sec >= 140.0:
        depth_score = 22.0
    elif avg_sec >= 100.0:
        depth_score = 18.0
    elif avg_sec >= 70.0:
        depth_score = 14.0
    else:
        depth_score = 9.0

    share_bonus = min(metric.audience_share * 0.4, 8.0)

    penalty = 0.0
    if bounce > 0.35:
        penalty = 4.0
    elif bounce > 0.25:
        penalty = 2.0

    total = depth_score + share_bonus - penalty
    return max(min(round(total, 1), 30.0), 5.0)


def _compute_crypta_affinity(text_sample: str) -> tuple[float, str]:
    """Calculate demographic affinity (0.0 to 25.0) with core audience interests."""
    text_lower = text_sample.lower()
    matched_categories: list[str] = []

    for cat, keywords in CRYPTA_CORE_INTERESTS.items():
        if any(kw in text_lower for kw in keywords):
            matched_categories.append(cat)

    if len(matched_categories) >= 3:
        score = 25.0
        aud_label = "Ядро 35-54 лет: Бизнес, Недвижимость, Финансы (высокий чек)"
    elif len(matched_categories) == 2:
        score = 20.0
        aud_label = "Мужчины и женщины 25-45: Деловая активность, Авто, Семья"
    elif len(matched_categories) == 1:
        score = 15.0
        aud_label = f"Широкая аудитория: интерес {matched_categories[0]}"
    else:
        score = 8.0
        aud_label = "Общая новостная аудитория"

    return (score, aud_label)


def _compute_fact_richness(posts: Sequence[RawNews], cluster: NewsCluster) -> float:
    """Calculate fact richness and urgency (0.0 to 15.0)."""
    score = 4.0
    unique_sources = {p.source_id for p in posts if p.source_id}
    num_sources = len(unique_sources)

    if num_sources >= 4:
        score += 6.0
    elif num_sources >= 2:
        score += 4.0
    elif num_sources == 1:
        score += 2.0

    all_text = " ".join(p.raw_text for p in posts if p.raw_text)
    # Check for digits and factual numbers
    has_digits = bool(re.search(r"\b\d+([.,]\d+)?\b", all_text))
    has_quotes = '"' in all_text or "«" in all_text

    if has_digits:
        score += 2.5
    if has_quotes:
        score += 1.5

    if cluster.created_at:
        age_hours = (datetime.now(UTC) - cluster.created_at).total_seconds() / 3600.0
        if age_hours <= 1.5:
            score += 1.0

    return min(round(score, 1), 15.0)


def calculate_cluster_aevi(
    cluster: NewsCluster,
    posts: Sequence[RawNews],
    category_metrics: Mapping[str, AudienceCategoryMetric] | None = None,
) -> AEVIBreakdown:
    """Calculate the complete AEVI index for a single news cluster."""
    text_sample = cluster.title or ""
    if posts:
        text_sample += " " + " ".join(p.raw_text[:300] for p in posts[:3] if p.raw_text)

    search_score = _compute_search_demand_score(cluster, text_sample, len(posts))
    hist_score = _compute_historical_engagement(cluster.category, category_metrics)
    crypta_score, target_aud = _compute_crypta_affinity(text_sample)
    fact_score = _compute_fact_richness(posts, cluster)

    total = round(search_score + hist_score + crypta_score + fact_score, 1)
    total = max(min(total, 100.0), 0.0)

    if total >= 75.0:
        level = "viral"
        badge_text = "🔥 Топ-интерес (AEVI)"
        badge_color = "bg-emerald-500/15 text-emerald-700 border-emerald-300"
    elif total >= 55.0:
        level = "high"
        badge_text = "📈 Высокий спрос"
        badge_color = "bg-blue-500/15 text-blue-700 border-blue-300"
    elif total >= 35.0:
        level = "medium"
        badge_text = "💬 Умеренный"
        badge_color = "bg-slate-500/15 text-slate-700 border-slate-300"
    else:
        level = "low"
        badge_text = "💤 Низкий спрос"
        badge_color = "bg-zinc-500/15 text-zinc-600 border-zinc-300"

    predicted_views = int(750 + total * 35)
    predicted_time = int(65 + hist_score * 2.8)
    predicted_ctr = round(1.5 + (crypta_score / 25.0) * 3.6, 1)

    rationale = (
        f"Поисковый спрос: {search_score}/30, Удержание рубрики: {hist_score}/30, "
        f"ЦА Крипта: {crypta_score}/25, Фактура: {fact_score}/15. "
        f"Целевой сегмент: {target_aud}."
    )

    return AEVIBreakdown(
        total_score=total,
        level=level,
        badge_text=badge_text,
        badge_color=badge_color,
        search_demand_score=search_score,
        historical_engagement_score=hist_score,
        crypta_affinity_score=crypta_score,
        fact_richness_score=fact_score,
        target_audience=target_aud,
        predicted_views=predicted_views,
        predicted_reading_time_sec=predicted_time,
        predicted_ctr_pct=predicted_ctr,
        rationale=rationale,
    )


async def calculate_clusters_aevi_batch(
    session: AsyncSession,
    clusters: Sequence[NewsCluster],
    posts_by_cluster: Mapping[uuid.UUID, Sequence[RawNews]],
) -> dict[uuid.UUID, AEVIBreakdown]:
    """Batch calculation of AEVI scores for multiple clusters with a single DB query."""
    metrics_map: dict[str, AudienceCategoryMetric] = {}
    try:
        stmt = select(AudienceCategoryMetric).where(
            AudienceCategoryMetric.platform == "website"
        )
        res = await session.execute(stmt)
        for m in res.scalars().all():
            metrics_map[m.category_slug] = m
    except SQLAlchemyError as exc:
        logger.warning("Could not fetch audience category metrics for AEVI: %s", exc)

    scores: dict[uuid.UUID, AEVIBreakdown] = {}
    for cluster in clusters:
        posts = posts_by_cluster.get(cluster.id, ())
        scores[cluster.id] = calculate_cluster_aevi(
            cluster, posts, category_metrics=metrics_map
        )

    return scores


def sort_clusters_by_aevi(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort news groups by AEVI score descending (highest interest first)."""
    return sorted(
        groups,
        key=lambda g: float(g.get("aevi_score", 0.0)),
        reverse=True,
    )


def evaluate_hypothesis_outcome(
    hypothesis: HypothesisRecord,
    actual_views: int,
    actual_reading_time_sec: float,
    actual_ctr_pct: float,
) -> dict[str, Any]:
    """Verify pre-publish hypothesis against live analytics data (Closed Loop)."""
    delta_views = actual_views - hypothesis.predicted_views
    delta_time = actual_reading_time_sec - hypothesis.predicted_reading_time_sec
    delta_ctr = actual_ctr_pct - hypothesis.predicted_ctr_pct

    is_winner = (delta_views >= 0) and (delta_time >= -10.0 or delta_ctr >= 0.0)

    return {
        "is_winner": is_winner,
        "delta_views": delta_views,
        "delta_reading_time_sec": round(delta_time, 1),
        "delta_ctr_pct": round(delta_ctr, 2),
        "verdict": (
            "🏆 Паттерн-победитель: факт превзошел прогноз"
            if is_winner
            else "Стандартная отдача: гипотеза в пределах нормы"
        ),
    }


@dataclass(slots=True)
class CampaignQualityReport:
    """Comprehensive evaluation of a published native/editorial campaign."""

    cqi_score: float  # 0.0 to 100.0
    quality_level: str  # 'exceptional', 'good', 'needs_tuning', 'poor', 'prohibited'
    badge_color: str
    badge_label: str
    is_brand_safe: bool
    brand_safety_reason: str
    media_reading_score: float  # 0 to 100
    media_bounce_score: float  # 0 to 100
    link_ctr_score: float  # 0 to 100
    target_conversion_score: float  # 0 to 100
    details: dict[str, Any]
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cqi_score": self.cqi_score,
            "quality_level": self.quality_level,
            "badge_color": self.badge_color,
            "badge_label": self.badge_label,
            "is_brand_safe": self.is_brand_safe,
            "brand_safety_reason": self.brand_safety_reason,
            "media_reading_score": self.media_reading_score,
            "media_bounce_score": self.media_bounce_score,
            "link_ctr_score": self.link_ctr_score,
            "target_conversion_score": self.target_conversion_score,
            "details": self.details,
            "verdict": self.verdict,
        }


def evaluate_campaign_quality(
    *,
    is_brand_safe: bool = True,
    brand_safety_reason: str = "",
    media_reading_sec: float = 0.0,
    media_bounce_rate: float = 0.0,
    media_reads: int = 0,
    link_clicks: int = 0,
    target_visits: int = 0,
    target_conversions: int = 0,
    target_bounce_rate: float = 0.0,
    target_benchmark_cr: float = 5.0,
) -> CampaignQualityReport:
    """Calculate the integral Campaign Quality Index (CQI) across media and client targets.

    Formula:
    CQI = BrandSafety * (0.25 * Q_read + 0.25 * Q_bounce + 0.25 * Q_ctr + 0.25 * Q_target)
    """
    if not is_brand_safe:
        return CampaignQualityReport(
            cqi_score=0.0,
            quality_level="prohibited",
            badge_color="bg-rose-500/15 text-rose-700 border-rose-300",
            badge_label="🛡️ Заблокировано Brand Safety",
            is_brand_safe=False,
            brand_safety_reason=brand_safety_reason or "Тематика исключена из рекламы",
            media_reading_score=0.0,
            media_bounce_score=0.0,
            link_ctr_score=0.0,
            target_conversion_score=0.0,
            details={},
            verdict=f"Кампания аннулирована: {brand_safety_reason}",
        )

    # 1. Reading depth score (benchmark 75 sec = 80 pts, 95+ sec = 100 pts)
    q_read = (
        min((media_reading_sec / 75.0) * 80.0, 100.0)
        if media_reading_sec > 0
        else 50.0
    )

    # 2. Bounce score (bounce rate < 20% = 80+ pts, bounce > 50% = < 50 pts)
    q_bounce = max(0.0, (1.0 - media_bounce_rate)) * 100.0

    # 3. In-text link CTR score (benchmark 4.0% CTR = 80 pts)
    ctr_pct = (
        (link_clicks / max(media_reads, 1)) * 100.0 if media_reads > 0 else 0.0
    )
    q_ctr = min((ctr_pct / 4.0) * 80.0, 100.0) if ctr_pct > 0 else 40.0

    # 4. Target site quality score (bounce on client site + conversion rate)
    if target_visits > 0:
        cr_pct = (target_conversions / target_visits) * 100.0
        target_retention = max(0.0, 1.0 - target_bounce_rate) * 50.0
        target_cr_score = min(
            (cr_pct / max(target_benchmark_cr, 1.0)) * 50.0, 50.0
        )
        q_target = round(target_retention + target_cr_score, 1)
    else:
        # If client counter has no data yet, evaluate based on expected resonance
        cr_pct = 0.0
        q_target = round((q_read + q_ctr) / 2.0, 1)

    cqi = round(
        0.25 * q_read + 0.25 * q_bounce + 0.25 * q_ctr + 0.25 * q_target, 1
    )
    cqi = max(min(cqi, 100.0), 0.0)

    if cqi >= 75.0:
        level = "exceptional"
        badge_color = "bg-emerald-500/15 text-emerald-700 border-emerald-300"
        badge_label = "🌟 Топ-эффективность (CQI ≥ 75)"
        verdict = (
            "Эталонная кампания: глубокое вовлечение и высокая конверсия на сайте проекта."
        )
    elif cqi >= 55.0:
        level = "good"
        badge_color = "bg-blue-500/15 text-blue-700 border-blue-300"
        badge_label = "📈 Хороший результат (CQI 55–74)"
        verdict = (
            "Качественная нативная интеграция: показатели в пределах запланированной нормы."
        )
    elif cqi >= 35.0:
        level = "needs_tuning"
        badge_color = "bg-amber-500/15 text-amber-700 border-amber-300"
        badge_label = "⚠️ Требует калибровки (CQI 35–54)"
        verdict = (
            "Слабый отклик: рекомендуется усилить выгоду в лиде и сместить CTA-ссылку выше."
        )
    else:
        level = "poor"
        badge_color = "bg-rose-500/15 text-rose-700 border-rose-300"
        badge_label = "❌ Низкая эффективность (CQI < 35)"
        verdict = (
            "Признаки баннерного отторжения: высокий отказ или отсутствие переходов."
        )

    return CampaignQualityReport(
        cqi_score=cqi,
        quality_level=level,
        badge_color=badge_color,
        badge_label=badge_label,
        is_brand_safe=True,
        brand_safety_reason="",
        media_reading_score=round(q_read, 1),
        media_bounce_score=round(q_bounce, 1),
        link_ctr_score=round(q_ctr, 1),
        target_conversion_score=round(q_target, 1),
        details={
            "media_reads": media_reads,
            "media_reading_sec": round(media_reading_sec, 1),
            "media_bounce_pct": round(media_bounce_rate * 100.0, 1),
            "link_clicks": link_clicks,
            "link_ctr_pct": round(ctr_pct, 2),
            "target_visits": target_visits,
            "target_conversions": target_conversions,
            "target_cr_pct": round(cr_pct, 2),
            "target_bounce_pct": round(target_bounce_rate * 100.0, 1),
        },
        verdict=verdict,
    )
