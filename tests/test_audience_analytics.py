"""Unit tests for AudienceAnalyticsService and Ad Potential calculation."""

from __future__ import annotations

from typing import Any, cast

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audience_analytics import (
    METRIKA_API_URL,
    AudienceAnalyticsReport,
    AudienceAnalyticsService,
    AudienceDemographics,
    _extract_category_slug_from_path,
    calculate_ad_potential_score,
)
from app.services.categories import CATEGORY_MAP
from app.services.yandex_connection import _encrypt_val


class _Scalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class FakeSession:
    """AsyncSession double for testing audience collection."""

    def __init__(self, yandex_doc: dict[str, Any] | None = None) -> None:
        self.yandex_doc = yandex_doc
        self.executed_stmts: list[Any] = []
        self.rolled_back = False

    async def execute(self, statement: Any) -> Any:
        self.executed_stmts.append(statement)
        compiled = str(statement).lower()
        if "from system_settings" in compiled:
            return _Scalar(self.yandex_doc)
        return _Scalar(None)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        self.rolled_back = True


def test_extract_category_slug_from_path() -> None:
    # Standard URL pattern on svodka.press
    assert (
        _extract_category_slug_from_path("/novosti-it-i-tehnologij/glava-nvidia-x/")
        == "novosti-it-i-tehnologij"
    )
    assert _extract_category_slug_from_path("/ekonomika/kurs-rublya-2026/") == "ekonomika"
    assert (
        _extract_category_slug_from_path("novosti-kriptovalyut/bitcoin-record")
        == "novosti-kriptovalyut"
    )
    # Trailing slashes and query strings
    assert (
        _extract_category_slug_from_path("/avtonovosti/lada-aura/?utm_source=dzen")
        == "avtonovosti"
    )
    # Unmatched / unknown paths
    assert _extract_category_slug_from_path("/unknown-rubric/some-post/") is None
    assert _extract_category_slug_from_path("/") is None
    assert _extract_category_slug_from_path("") is None


def test_calculate_ad_potential_score() -> None:
    # High engagement + commercial intent
    score_high, level_high, rec_high = calculate_ad_potential_score(
        category_slug="novosti-it-i-tehnologij",
        pageviews=1000,
        avg_time_sec=120.0,
        bounce_rate=0.20,
        max_views_in_sample=1000,
    )
    assert 60.0 <= score_high <= 100.0
    assert level_high == "high"
    # Recommendation contains in-article banner keywords
    banner_kws = ("Баннеры", "слот", "Слот", "728x90", "IT", "SaaS")
    assert any(kw in rec_high for kw in banner_kws)

    # Low engagement / non-commercial category
    score_low, level_low, _ = calculate_ad_potential_score(
        category_slug="chp-i-proisshestvij",
        pageviews=5,
        avg_time_sec=15.0,
        bounce_rate=0.85,
        max_views_in_sample=1000,
    )
    assert score_low < score_high
    assert level_low in {"medium", "low"}
    assert 0.0 <= score_low <= 100.0


def test_audience_report_defaults() -> None:
    rep = AudienceAnalyticsReport(window_days=30)
    assert rep.window_days == 30
    assert rep.website_pageviews == 0
    assert rep.dzen_available is False
    assert len(rep.categories) == 0
    assert rep.top_monetization_categories == []
    # demographics should be an empty but safe AudienceDemographics object
    assert isinstance(rep.demographics, AudienceDemographics)
    assert rep.demographics.available is False
    d = rep.demographics.to_dict()
    assert d["available"] is False
    assert d["gender"] == {}


@pytest.mark.asyncio
async def test_collect_audience_report_cold() -> None:
    """When no API tokens are configured, returns baseline categories without crashing."""
    session = cast(AsyncSession, FakeSession(yandex_doc=None))
    report = await AudienceAnalyticsService.collect_audience_report(session, days=30)

    assert isinstance(report, AudienceAnalyticsReport)
    assert len(report.categories) == len(CATEGORY_MAP)
    assert len(report.top_monetization_categories) > 0
    assert report.website_pageviews == 0
    assert report.dzen_available is False
    # cold run has no token — summary falls back to generic text
    assert "Анализ читательских предпочтений" in report.ai_ads_summary
    # demographics is empty but present
    assert isinstance(report.demographics, AudienceDemographics)
    assert report.demographics.available is False


@pytest.mark.asyncio
@respx.mock
async def test_collect_audience_report_with_metrika() -> None:
    """When Metrika is configured, aggregates pageviews into the 20 categories."""
    yandex_doc = {
        "metrika_counter_id": "106439629",
        "metrika_token_enc": _encrypt_val("fake_token_12345"),
        "metrika_dzen_counter_id": "",
    }
    session = cast(AsyncSession, FakeSession(yandex_doc=yandex_doc))

    mock_metrika_response = {
        "data": [
            {
                "dimensions": [{"name": "/novosti-it-i-tehnologij/post-1/"}],
                "metrics": [150, 120, 22.5, 95.0],
            },
            {
                "dimensions": [{"name": "/novosti-it-i-tehnologij/post-2/"}],
                "metrics": [80, 65, 18.0, 110.0],
            },
            {
                "dimensions": [{"name": "/ekonomika/ruble-stat/"}],
                "metrics": [200, 180, 25.0, 75.0],
            },
        ]
    }

    respx.get(url__startswith=METRIKA_API_URL).respond(
        200, json=mock_metrika_response
    )

    queries = [
        {"query": "яндекс иски", "clicks": 25},
        {"query": "nvidia акции", "clicks": 18},
    ]

    report = await AudienceAnalyticsService.collect_audience_report(
        session, days=30, queries_sample=queries
    )

    assert report.website_pageviews == 430
    assert report.website_visitors == 365
    assert len(report.categories) == len(CATEGORY_MAP)

    it_cat = next(
        c for c in report.categories if c.category_slug == "novosti-it-i-tehnologij"
    )
    assert it_cat.pageviews == 230
    assert it_cat.visitors == 185
    assert it_cat.ad_readiness_level in {"high", "medium"}

    econ_cat = next(c for c in report.categories if c.category_slug == "ekonomika")
    assert econ_cat.pageviews == 200

    # Ensure all 20 categories are present
    assert len(report.categories) == 20

    # demographics field is present in the report
    assert isinstance(report.demographics, AudienceDemographics)
