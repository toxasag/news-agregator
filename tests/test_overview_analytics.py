"""Unit tests for the overview analytics service and data models."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.overview_analytics import (
    AISpendRadar,
    DailyTimelineRow,
    FailureIssue,
    ModelSpendRow,
    OverviewData,
    PlatformFailures,
    ProviderSpendRow,
    RoleSpendRow,
    _format_day_label,
    humanize_failure_reason,
)


def test_humanize_failure_reason_vk_limit() -> None:
    raw = "wall.post: VK error 214: Access to adding post denied: you can only add 500 posts a day"
    reason = humanize_failure_reason("vk", raw)
    assert "500" in reason
    assert "VK API" in reason


def test_humanize_failure_reason_tenchat_session() -> None:
    raw = "TenChat: сессия истекла (перенаправлено на авторизацию). Обновите куки в настройках."
    reason = humanize_failure_reason("tenchat", raw)
    assert "Сессия истекла" in reason
    assert "Настройках" in reason


def test_humanize_failure_reason_youtube_selector() -> None:
    raw = "YouTube: поле ввода поста не найдено на странице @genadich_com/posts (проверьте авторизацию)"
    reason = humanize_failure_reason("youtube", raw)
    assert "Поле ввода поста не найдено" in reason


def test_humanize_failure_reason_timeout() -> None:
    raw = "ScraperError: Browser page could not be prepared: Timeout 30000ms exceeded."
    reason = humanize_failure_reason("dzen", raw)
    assert "Таймаут" in reason


def test_humanize_failure_reason_empty() -> None:
    assert humanize_failure_reason("vk", None) == "Неизвестная ошибка публикации"
    assert humanize_failure_reason("vk", "   ") == "Неизвестная ошибка публикации"


def test_format_day_label() -> None:
    today = date(2026, 10, 3)
    assert _format_day_label(today, today) == "Сегодня, 03.10"
    yesterday = date(2026, 10, 2)
    assert _format_day_label(yesterday, today) == "Вчера, 02.10"
    older = date(2026, 10, 1)
    label = _format_day_label(older, today)
    assert "01.10" in label


def test_overview_data_structures() -> None:
    timeline_row = DailyTimelineRow(
        date_str="2026-10-03",
        date_label="Сегодня, 03.10",
        raw_total=100,
        raw_filtered_dups=70,
        raw_unique=30,
        dedup_pct=70.0,
        articles_total=25,
        articles_published=25,
        published_pct=100.0,
        platforms_published={"wp": 25, "dzen": 24, "vk": 20},
        platforms_failed={"vk": 5, "tenchat": 25},
        ai_calls=80,
        ai_tokens=400000,
        ai_cost_usd=0.15,
    )

    issue = FailureIssue(
        reason="Лимит VK API: не более 500 записей в сутки на стену",
        raw_error="VK error 214",
        count=5,
        sample_articles=[{"id": 101, "title": "Новость 1", "category": "Экономика"}],
    )

    plat_failure = PlatformFailures(
        platform_code="vk",
        platform_name="ВКонтакте",
        total_failures=5,
        issues=[issue],
    )

    ai_radar = AISpendRadar(
        total_cost_usd=1.50,
        total_cost_rub=142.5,
        total_tokens=5000000,
        prompt_tokens=3500000,
        completion_tokens=1500000,
        total_calls=900,
        avg_latency_ms=12000,
        cost_per_article=0.06,
        models=[
            ModelSpendRow(
                model="gemini-3.1-flash-lite",
                provider_name="Google Gemini",
                calls=500,
                prompt_tokens=2000000,
                completion_tokens=500000,
                total_tokens=2500000,
                cost_usd=0.45,
                avg_latency_ms=5000,
                error_count=0,
            )
        ],
        providers=[
            ProviderSpendRow(
                provider_name="Google Gemini",
                calls=500,
                total_tokens=2500000,
                cost_usd=0.45,
                cost_pct=30.0,
                error_count=0,
            )
        ],
        roles=[
            RoleSpendRow(
                role="writer",
                role_label="Генерация статей",
                calls=300,
                total_tokens=3000000,
                cost_usd=1.20,
            )
        ],
    )

    data = OverviewData(
        days_window=7,
        summary_cards={"raw_news_total": 5000, "articles_total": 2000},
        timeline=[timeline_row],
        failures=[plat_failure],
        ai_radar=ai_radar,
    )

    assert data.days_window == 7
    assert len(data.timeline) == 1
    assert data.timeline[0].dedup_pct == 70.0
    assert len(data.failures) == 1
    assert data.ai_radar.total_cost_rub == 142.5
