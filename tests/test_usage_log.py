"""Tests for token usage logging, cost estimation, and stability reporting."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.ai_usage import AIUsage
from app.services.usage_log import (
    USD_TO_RUB_RATE,
    estimate_cost_usd,
    get_article_usage,
    get_cluster_usage,
    get_system_usage_report,
    record_usage,
)


def test_estimate_cost_usd() -> None:
    # Gemini Flash (0.10 / 0.40 per 1M)
    cost = estimate_cost_usd("gemini-2.5-flash", 1_000_000, 1_000_000)
    assert cost == Decimal("0.500000")

    # Claude 3.5 Sonnet (3.00 / 15.00 per 1M)
    cost_claude = estimate_cost_usd("claude-3-5-sonnet", 10_000, 2_000)
    # 10000 * 3.0 / 1M = 0.03, 2000 * 15.0 / 1M = 0.03 -> 0.06
    assert cost_claude == Decimal("0.060000")

    # Custom model fallback
    cost_fallback = estimate_cost_usd("custom-provider:unknown-model", 1000, 500)
    assert cost_fallback > 0


@pytest.mark.asyncio
async def test_record_usage_success() -> None:
    session = AsyncMock()
    added_objects: list[Any] = []
    session.add = MagicMock(side_effect=added_objects.append)

    cid = uuid.uuid4()
    await record_usage(
        session,
        role="writer",
        model="gemini-2.5-flash",
        prompt_tokens=1500,
        completion_tokens=800,
        provider_name="Google Gemini",
        latency_ms=1250,
        cluster_id=str(cid),
        article_id=42,
    )

    assert len(added_objects) == 1
    row: AIUsage = added_objects[0]
    assert row.role == "writer"
    assert row.model == "gemini-2.5-flash"
    assert row.provider_name == "Google Gemini"
    assert row.prompt_tokens == 1500
    assert row.completion_tokens == 800
    assert row.total_tokens == 2300
    assert row.latency_ms == 1250
    assert row.status == "success"
    assert row.cluster_id == cid
    assert row.article_id == 42
    assert row.cost_usd is not None
    assert row.cost_usd > 0
    assert session.commit.called


@pytest.mark.asyncio
async def test_record_usage_error_and_provider_resolution() -> None:
    session = AsyncMock()
    added_objects: list[Any] = []
    session.add = MagicMock(side_effect=added_objects.append)

    await record_usage(
        session,
        role="arbiter",
        model="dollariuy:claude-3-5-sonnet",
        prompt_tokens=0,
        completion_tokens=0,
        status="rate_limited",
        error_message="HTTP 429 Too Many Requests",
        latency_ms=450,
    )

    assert len(added_objects) == 1
    row: AIUsage = added_objects[0]
    assert row.role == "arbiter"
    assert row.provider_name == "dollariuy"
    assert row.status == "rate_limited"
    assert row.error_message == "HTTP 429 Too Many Requests"
    assert row.latency_ms == 450


@pytest.mark.asyncio
async def test_get_article_and_cluster_usage() -> None:
    cid = uuid.uuid4()
    now = datetime.now(UTC)

    mock_row1 = AIUsage(
        id=1,
        created_at=now,
        role="writer",
        model="gemini-2.5-flash",
        provider_name="Google Gemini",
        prompt_tokens=1000,
        completion_tokens=500,
        total_tokens=1500,
        cost_usd=Decimal("0.000300"),
        latency_ms=800,
        status="success",
        cluster_id=cid,
        article_id=10,
    )
    mock_row2 = AIUsage(
        id=2,
        created_at=now,
        role="image_prompt",
        model="dollariuy:claude-3-5-sonnet",
        provider_name="dollariuy",
        prompt_tokens=200,
        completion_tokens=50,
        total_tokens=250,
        cost_usd=Decimal("0.000100"),
        latency_ms=400,
        status="success",
        cluster_id=cid,
        article_id=10,
    )

    class MockResult:
        def scalars(self) -> MockResult:
            return self

        def all(self) -> list[AIUsage]:
            return [mock_row1, mock_row2]

    session = AsyncMock()
    session.execute.return_value = MockResult()

    res = await get_article_usage(session, 10)

    assert res["total_tokens"] == 1750
    assert res["prompt_tokens"] == 1200
    assert res["completion_tokens"] == 550
    assert res["calls_count"] == 2
    assert res["cost_usd"] == 0.0004
    assert res["cost_rub"] == round(0.0004 * float(USD_TO_RUB_RATE), 4)
    assert "Google Gemini" in res["providers"]
    assert "dollariuy" in res["providers"]
    assert "writer" in res["breakdown_by_role"]
    assert "image_prompt" in res["breakdown_by_role"]
    assert len(res["details"]) == 2

    # Cluster usage
    cluster_res = await get_cluster_usage(session, cid)
    assert cluster_res["total_tokens"] == 1750


@pytest.mark.asyncio
async def test_get_system_usage_report() -> None:
    now = datetime.now(UTC)
    mock_rows = [
        AIUsage(
            id=1,
            created_at=now,
            role="writer",
            model="gemini-2.5-flash",
            provider_name="Google Gemini",
            prompt_tokens=2000,
            completion_tokens=1000,
            total_tokens=3000,
            cost_usd=Decimal("0.000600"),
            latency_ms=1000,
            status="success",
        ),
        AIUsage(
            id=2,
            created_at=now - timedelta(days=2),
            role="arbiter",
            model="dollariuy:claude-3-5-sonnet",
            provider_name="dollariuy",
            prompt_tokens=500,
            completion_tokens=100,
            total_tokens=600,
            cost_usd=Decimal("0.000200"),
            latency_ms=500,
            status="error",
        ),
    ]

    class MockScalarResult:
        def scalars(self) -> MockScalarResult:
            return self

        def all(self) -> list[AIUsage]:
            return mock_rows

    mock_all_time_tuple = MagicMock()
    mock_all_time_tuple.calls = 50
    mock_all_time_tuple.tokens = 150_000
    mock_all_time_tuple.cost = Decimal("0.05")

    class MockOneResult:
        def one(self) -> Any:
            return mock_all_time_tuple

    session = AsyncMock()
    session.execute.side_effect = [MockScalarResult(), MockOneResult()]

    report = await get_system_usage_report(session, days=7)

    assert "summary" in report
    assert "providers" in report
    assert "models" in report
    assert "daily_trends" in report

    summary = report["summary"]
    assert summary["all_time_calls"] == 50
    assert summary["all_time_tokens"] == 150_000
    assert summary["period_calls"] == 2
    assert summary["period_tokens"] == 3600

    # Providers list
    providers = {p["provider_name"]: p for p in report["providers"]}
    assert "Google Gemini" in providers
    assert providers["Google Gemini"]["uptime_pct"] == 100.0
    assert "dollariuy" in providers
    assert providers["dollariuy"]["failed_calls"] == 1
