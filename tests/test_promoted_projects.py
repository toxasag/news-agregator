# ruff: noqa: RUF001, E501, S105, S106
"""Unit tests for Promoted Projects, Client Metrika stats, and Campaign Quality Index (CQI)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.models.promoted_project import ConversionGoal, PromotedProject
from app.services.aevi_scoring import evaluate_campaign_quality
from app.services.promoted_projects_service import PromotedProjectsService


def test_promoted_project_model_and_utm_builder() -> None:
    """PromotedProject must correctly serialize, deserialize, and build tracked UTM URLs."""
    goal = ConversionGoal(
        goal_id="300400",
        name="Покупка в 1 клик",
        target_cr_pct=4.5,
    )
    project = PromotedProject(
        id="proj_1",
        name="Экспресс-доставка за 15 минут",
        brand_name="Магнит Доставка",
        usp="Быстро и свежо",
        problem_solved="Нехватка времени на готовку и покупки",
        target_url="https://client-shop.ru/promo",
        conversion_goals=[goal],
        target_categories=["novosti-it-i-tehnologij", "obshchestvo"],
        target_segments=["Занятые горожане 25-45 лет"],
        cta_text="Закажите с доставкой за 15 минут",
        utm_source="svodkapress",
        utm_medium="native_article",
    )

    data = project.to_dict()
    assert data["id"] == "proj_1"
    assert data["brand_name"] == "Магнит Доставка"
    assert len(data["conversion_goals"]) == 1

    restored = PromotedProject.from_dict(data)
    assert restored.id == project.id
    assert restored.brand_name == project.brand_name
    assert restored.conversion_goals[0].goal_id == "300400"

    # Test UTM tracked URL builder
    tracked = project.build_tracked_url("cluster_abc12345")
    assert "https://client-shop.ru/promo?" in tracked
    assert "utm_source=svodkapress" in tracked
    assert "utm_medium=native_article" in tracked
    assert "utm_campaign=cluster_abc12345" in tracked


def test_get_matching_projects_brand_safety() -> None:
    """get_matching_projects must reject projects when brand safety is violated."""
    p1 = PromotedProject(
        id="p1",
        brand_name="Brand A",
        target_categories=["novosti-it-i-tehnologij"],
        is_active=True,
    )
    p2 = PromotedProject(
        id="p2",
        brand_name="Brand B",
        target_categories=[],  # Any brand-safe category
        is_active=True,
    )
    p3 = PromotedProject(
        id="p3",
        brand_name="Brand C",
        target_categories=[],
        is_active=False,  # Inactive
    )

    # When brand safe
    matched = PromotedProjectsService.get_matching_projects(
        [p1, p2, p3],
        category_slug="novosti-it-i-tehnologij",
        is_brand_safe=True,
    )
    assert len(matched) == 2
    assert {p.id for p in matched} == {"p1", "p2"}

    # When NOT brand safe (e.g. military or accident)
    unsafe_matched = PromotedProjectsService.get_matching_projects(
        [p1, p2, p3],
        category_slug="voennye-novosti",
        is_brand_safe=False,
    )
    assert unsafe_matched == []


@pytest.mark.asyncio
async def test_fetch_client_metrika_stats() -> None:
    """fetch_client_metrika_stats must parse totals and goals from Metrika API response."""
    project = PromotedProject(
        id="p_test",
        brand_name="Test Brand",
        client_metrika_counter_id="998877",
        client_metrika_token="oauth_test_token",
        conversion_goals=[ConversionGoal(goal_id="111", name="Order")],
    )

    mock_resp_totals = MagicMock()
    mock_resp_totals.status_code = 200
    mock_resp_totals.json.return_value = {
        "totals": [450, 410, 22.5, 84.0]  # visits, users, bounceRate, avgDuration
    }

    mock_resp_goals = MagicMock()
    mock_resp_goals.status_code = 200
    mock_resp_goals.json.return_value = {"totals": [27]}  # 27 reached goals

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.side_effect = [mock_resp_totals, mock_resp_goals]
        stats = await PromotedProjectsService.fetch_client_metrika_stats(
            project, "test_cid_123"
        )

        assert stats["available"] is True
        assert stats["visits"] == 450
        assert stats["users"] == 410
        assert stats["bounce_rate"] == 0.225
        assert stats["avg_duration_sec"] == 84.0
        assert stats["conversions_count"] == 27
        assert stats["cr_pct"] == 6.0  # (27 / 450) * 100


def test_evaluate_campaign_quality_prohibited() -> None:
    """evaluate_campaign_quality must zero out scores when brand safety is violated."""
    report = evaluate_campaign_quality(
        is_brand_safe=False,
        brand_safety_reason="Тема военной операции",
        media_reading_sec=120.0,
        media_reads=1000,
    )
    assert report.cqi_score == 0.0
    assert report.quality_level == "prohibited"
    assert "Brand Safety" in report.badge_label
    assert report.media_reading_score == 0.0


def test_evaluate_campaign_quality_exceptional() -> None:
    """Winning retention and high target CR should yield an exceptional CQI score."""
    report = evaluate_campaign_quality(
        is_brand_safe=True,
        media_reading_sec=95.0,  # >75s benchmark
        media_bounce_rate=0.18,  # very low bounce
        media_reads=1500,
        link_clicks=75,  # 5% CTR
        target_visits=75,
        target_conversions=6,  # 8% CR
        target_bounce_rate=0.20,
    )
    assert report.cqi_score >= 80.0
    assert report.quality_level == "exceptional"
    assert "🌟" in report.badge_label
    assert report.media_reading_score > 90.0


def test_parse_goals_from_form_and_token_masking() -> None:
    """parse_goals_from_form must extract valid goals and deduplicate by goal_id."""

    class DummyForm:
        def __init__(self, data: dict[str, list[str]]) -> None:
            self._data = data

        def getlist(self, key: str) -> list[str]:
            return self._data.get(key, [])

    form = DummyForm(
        {
            "goal_ids": [" 401001 ", "401002", "401001", ""],
            "goal_names": ["Регистрация API", "Оплата тарифа", "Дубль", "Пустая"],
            "goal_target_crs": ["5.5", "2.0", "1.0", "3.0"],
        }
    )
    goals = PromotedProjectsService.parse_goals_from_form(form)
    assert len(goals) == 2
    assert goals[0].goal_id == "401001"
    assert goals[0].name == "Регистрация API"
    assert goals[0].target_cr_pct == 5.5
    assert goals[1].goal_id == "401002"
    assert goals[1].target_cr_pct == 2.0

    proj = PromotedProject(
        id="freeapi_ai",
        brand_name="FreeAPI AI",
        client_metrika_counter_id="113062256",
        client_metrika_token="y0_AgAAAAABcde1234567890xyz",
        conversion_goals=goals,
    )
    assert proj.has_client_metrika_token is True
    assert proj.client_metrika_token_masked == "••••••••0xyz"
    serialized = proj.to_dict()
    assert serialized["has_client_metrika_token"] is True
    assert serialized["client_metrika_token_masked"] == "••••••••0xyz"


@pytest.mark.asyncio
async def test_discover_client_metrika_goals() -> None:
    """discover_client_metrika_goals must fetch counter metadata and goals list from Metrika Management API."""
    mock_counter_resp = MagicMock()
    mock_counter_resp.status_code = 200
    mock_counter_resp.json.return_value = {
        "counter": {"name": "FreeAPI AI", "site": "freeapi.ai"}
    }

    mock_goals_resp = MagicMock()
    mock_goals_resp.status_code = 200
    mock_goals_resp.json.return_value = {
        "goals": [
            {
                "id": 500101,
                "name": "Регистрация и получение API-ключа",
                "type": "action",
            },
            {
                "id": 500102,
                "name": "Пополнение баланса",
                "type": "number",
            },
        ]
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.side_effect = [mock_counter_resp, mock_goals_resp]
        result = await PromotedProjectsService.discover_client_metrika_goals(
            "113062256", "y0_test_token"
        )

        assert result["ok"] is True
        assert result["counter_name"] == "FreeAPI AI"
        assert result["site_domain"] == "freeapi.ai"
        assert len(result["goals"]) == 2
        assert result["goals"][0]["goal_id"] == "500101"
        assert result["goals"][1]["type"] == "number"


def test_extract_attribution_tokens() -> None:
    """extract_attribution_tokens must extract referral slugs and query params from target URLs."""
    # 1. Referral path slug (e.g. FreeAPI)
    p1 = PromotedProject(
        id="freeapiai",
        target_url="https://freeapiai.ru/r/tdCxTE7k",
    )
    tokens1 = PromotedProjectsService.extract_attribution_tokens(p1)
    assert "tdCxTE7k" in tokens1

    # 2. Query param ref / r
    p2 = PromotedProject(
        id="freeapiai_ref",
        target_url="https://freeapiai.ru/?ref=tdCxTE7k",
    )
    tokens2 = PromotedProjectsService.extract_attribution_tokens(p2)
    assert "tdCxTE7k" in tokens2

    # 3. Path ending slug
    p3 = PromotedProject(
        id="promo",
        target_url="https://example.com/promo-offer-2026",
    )
    tokens3 = PromotedProjectsService.extract_attribution_tokens(p3)
    assert "promo-offer-2026" in tokens3


