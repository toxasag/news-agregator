"""Token-usage and provider-stability ledger: one row per model call.

Tracks tokens, latency, provider stability, and costs across articles and
the entire system.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_usage import AIUsage

logger: Final[logging.Logger] = logging.getLogger(__name__)

USD_TO_RUB_RATE: Final[Decimal] = Decimal("92.5")

#: Rates per 1M tokens in USD: (prompt_rate_usd, completion_rate_usd)
MODEL_PRICE_PER_MILLION: Final[dict[str, tuple[Decimal, Decimal]]] = {
    # Gemini
    "gemini-2.5-flash": (Decimal("0.10"), Decimal("0.40")),
    "gemini-3.1-flash": (Decimal("0.10"), Decimal("0.40")),
    "gemini-2.5-pro": (Decimal("1.25"), Decimal("5.00")),
    "gemini-3-pro": (Decimal("1.25"), Decimal("5.00")),
    "gemini-1.5-flash": (Decimal("0.075"), Decimal("0.30")),
    "gemini-1.5-pro": (Decimal("1.25"), Decimal("5.00")),
    # Anthropic Claude
    "claude-3-5-sonnet": (Decimal("3.00"), Decimal("15.00")),
    "claude-3-7-sonnet": (Decimal("3.00"), Decimal("15.00")),
    "claude-sonnet-4": (Decimal("3.00"), Decimal("15.00")),
    "claude-3-5-haiku": (Decimal("0.80"), Decimal("4.00")),
    "claude-3-opus": (Decimal("15.00"), Decimal("75.00")),
    # OpenAI
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4.5": (Decimal("5.00"), Decimal("20.00")),
    "gpt-5": (Decimal("5.00"), Decimal("20.00")),
    "o1": (Decimal("15.00"), Decimal("60.00")),
    "o1-mini": (Decimal("3.00"), Decimal("12.00")),
    "o3-mini": (Decimal("1.10"), Decimal("4.40")),
    # DeepSeek
    "deepseek-chat": (Decimal("0.14"), Decimal("0.28")),
    "deepseek-v3": (Decimal("0.14"), Decimal("0.28")),
    "deepseek-reasoner": (Decimal("0.55"), Decimal("2.19")),
    "deepseek-r1": (Decimal("0.55"), Decimal("2.19")),
    # Grok
    "grok-beta": (Decimal("2.00"), Decimal("10.00")),
    "grok-2": (Decimal("2.00"), Decimal("10.00")),
    "grok-3": (Decimal("2.50"), Decimal("10.00")),
    "grok-4": (Decimal("2.50"), Decimal("10.00")),
}

DEFAULT_PROMPT_PRICE_PER_M: Final[Decimal] = Decimal("0.50")
DEFAULT_COMPLETION_PRICE_PER_M: Final[Decimal] = Decimal("1.50")

__all__ = [
    "USD_TO_RUB_RATE",
    "estimate_cost_usd",
    "get_article_usage",
    "get_cluster_usage",
    "get_system_usage_report",
    "record_usage",
]


def estimate_cost_usd(
    model: str, prompt_tokens: int, completion_tokens: int
) -> Decimal:
    """Estimate the cost of a model call in USD based on token counts."""
    clean_model = model.lower()
    if ":" in clean_model:
        clean_model = clean_model.split(":", 1)[1]

    matched_rates = (DEFAULT_PROMPT_PRICE_PER_M, DEFAULT_COMPLETION_PRICE_PER_M)
    for model_key, rates in MODEL_PRICE_PER_MILLION.items():
        if model_key in clean_model:
            matched_rates = rates
            break

    prompt_cost = (Decimal(prompt_tokens) * matched_rates[0]) / Decimal(1_000_000)
    completion_cost = (Decimal(completion_tokens) * matched_rates[1]) / Decimal(
        1_000_000
    )
    return round(prompt_cost + completion_cost, 6)


def _resolve_provider_name(model: str, given_name: str | None) -> str:
    if given_name:
        return given_name
    if ":" in model and not model.startswith("http"):
        return model.split(":", 1)[0]
    if model.startswith(("gemini-", "gemma-")):
        return "Google Gemini"
    return "OpenRouter"


def _resolve_cluster_id(raw_id: str | uuid.UUID | None) -> uuid.UUID | None:
    if raw_id is None:
        return None
    if isinstance(raw_id, uuid.UUID):
        return raw_id
    try:
        return uuid.UUID(str(raw_id))
    except ValueError:
        return None


async def record_usage(
    session: AsyncSession,
    *,
    role: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    provider_name: str | None = None,
    latency_ms: int | None = None,
    status: str = "success",
    error_message: str | None = None,
    cost_usd: float | Decimal | None = None,
    cluster_id: str | uuid.UUID | None = None,
    article_id: int | None = None,
) -> None:
    """Persist one paid or attempted model call to the usage ledger.

    Silently skips when the row cannot be written: the spend it describes is
    real, but a failed insert must not fail the generation that already spent
    the tokens.
    """
    effective_total = total_tokens
    if effective_total <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
        effective_total = prompt_tokens + completion_tokens

    if status == "success" and effective_total <= 0:
        return

    p_name = _resolve_provider_name(model, provider_name)

    calculated_cost_usd: Decimal | None = None
    if cost_usd is not None:
        calculated_cost_usd = Decimal(str(cost_usd))
    elif effective_total > 0:
        calculated_cost_usd = estimate_cost_usd(model, prompt_tokens, completion_tokens)

    cid_val = _resolve_cluster_id(cluster_id)

    try:
        session.add(
            AIUsage(
                role=role[:16],
                model=model[:128],
                provider_name=p_name[:64],
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=effective_total,
                cost_usd=calculated_cost_usd,
                latency_ms=latency_ms,
                status=status[:24],
                error_message=error_message,
                cluster_id=cid_val,
                article_id=article_id,
            )
        )
        await session.commit()
    except SQLAlchemyError:
        with contextlib.suppress(Exception):
            await session.rollback()
        logger.warning(
            "Could not record AI usage for %s/%s (%d tokens)",
            role,
            model,
            effective_total,
            exc_info=True,
        )


async def get_article_usage(
    session: AsyncSession, article_id: int
) -> dict[str, Any]:
    """Calculate token spend, cost, and provider details for a specific article."""
    stmt = (
        select(AIUsage)
        .where(AIUsage.article_id == article_id)
        .order_by(AIUsage.created_at.asc())
    )
    rows: Sequence[AIUsage] = (await session.execute(stmt)).scalars().all()

    return _aggregate_usage_rows(rows)


async def get_cluster_usage(
    session: AsyncSession, cluster_id: uuid.UUID | str
) -> dict[str, Any]:
    """Calculate token spend, cost, and provider details for a cluster."""
    cid_val = _resolve_cluster_id(cluster_id)
    if cid_val is None:
        return _empty_usage_summary()

    stmt = (
        select(AIUsage)
        .where(AIUsage.cluster_id == cid_val)
        .order_by(AIUsage.created_at.asc())
    )
    rows: Sequence[AIUsage] = (await session.execute(stmt)).scalars().all()

    return _aggregate_usage_rows(rows)


def _empty_usage_summary() -> dict[str, Any]:
    return {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "cost_rub": 0.0,
        "calls_count": 0,
        "avg_latency_ms": 0,
        "providers": [],
        "models": [],
        "breakdown_by_role": {},
        "details": [],
    }


def _aggregate_usage_rows(rows: Sequence[AIUsage]) -> dict[str, Any]:
    if not rows:
        return _empty_usage_summary()

    total_tokens = sum(r.total_tokens for r in rows)
    prompt_tokens = sum(r.prompt_tokens for r in rows)
    completion_tokens = sum(r.completion_tokens for r in rows)
    cost_usd = sum((r.cost_usd or Decimal("0")) for r in rows)
    cost_rub = round(cost_usd * USD_TO_RUB_RATE, 4)

    latencies = [r.latency_ms for r in rows if r.latency_ms is not None]
    avg_latency = round(sum(latencies) / len(latencies)) if latencies else 0

    providers = sorted({r.provider_name for r in rows if r.provider_name})
    models = sorted({r.model for r in rows if r.model})

    breakdown_by_role: dict[str, dict[str, Any]] = {}
    details: list[dict[str, Any]] = []

    for r in rows:
        r_cost = float(r.cost_usd or 0.0)
        role = r.role or "unknown"
        if role not in breakdown_by_role:
            breakdown_by_role[role] = {
                "role": role,
                "tokens": 0,
                "cost_usd": 0.0,
                "calls": 0,
                "models": set(),
                "providers": set(),
            }
        breakdown_by_role[role]["tokens"] += r.total_tokens
        breakdown_by_role[role]["cost_usd"] += r_cost
        breakdown_by_role[role]["calls"] += 1
        if r.model:
            breakdown_by_role[role]["models"].add(r.model)
        if r.provider_name:
            breakdown_by_role[role]["providers"].add(r.provider_name)

        details.append(
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else "",
                "role": r.role,
                "model": r.model,
                "provider_name": r.provider_name or "Unknown",
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "latency_ms": r.latency_ms or 0,
                "status": r.status,
                "cost_usd": r_cost,
                "cost_rub": round(r_cost * float(USD_TO_RUB_RATE), 3),
            }
        )

    for b in breakdown_by_role.values():
        b["models"] = sorted(b["models"])
        b["providers"] = sorted(b["providers"])
        b["cost_usd"] = round(b["cost_usd"], 5)
        b["cost_rub"] = round(b["cost_usd"] * float(USD_TO_RUB_RATE), 3)

    return {
        "total_tokens": total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": float(round(cost_usd, 5)),
        "cost_rub": float(cost_rub),
        "calls_count": len(rows),
        "avg_latency_ms": avg_latency,
        "providers": providers,
        "models": models,
        "breakdown_by_role": breakdown_by_role,
        "details": details,
    }


def _build_providers_report(
    period_rows: Sequence[AIUsage],
) -> list[dict[str, Any]]:
    provider_groups: dict[str, list[AIUsage]] = {}
    for r in period_rows:
        pname = r.provider_name or "Неизвестный"
        provider_groups.setdefault(pname, []).append(r)

    providers_report: list[dict[str, Any]] = []
    for pname, prows in sorted(
        provider_groups.items(), key=lambda item: len(item[1]), reverse=True
    ):
        p_total = len(prows)
        p_success = sum(1 for r in prows if r.status == "success")
        p_failed = p_total - p_success
        p_uptime = round((p_success / p_total) * 100, 1) if p_total > 0 else 100.0
        p_lats = [r.latency_ms for r in prows if r.latency_ms is not None]
        p_avg_lat = round(sum(p_lats) / len(p_lats)) if p_lats else 0
        p_tokens = sum(r.total_tokens for r in prows)
        p_prompt_tokens = sum(r.prompt_tokens for r in prows)
        p_comp_tokens = sum(r.completion_tokens for r in prows)
        p_cost = sum(float(r.cost_usd or 0.0) for r in prows)
        p_models = sorted({r.model for r in prows if r.model})
        last_dt = max((r.created_at for r in prows if r.created_at), default=None)

        providers_report.append(
            {
                "provider_name": pname,
                "total_calls": p_total,
                "success_calls": p_success,
                "failed_calls": p_failed,
                "uptime_pct": p_uptime,
                "avg_latency_ms": p_avg_lat,
                "prompt_tokens": p_prompt_tokens,
                "completion_tokens": p_comp_tokens,
                "total_tokens": p_tokens,
                "cost_usd": round(p_cost, 4),
                "cost_rub": round(p_cost * float(USD_TO_RUB_RATE), 2),
                "models_count": len(p_models),
                "models_used": p_models,
                "last_call_at": last_dt.strftime("%d.%m %H:%M") if last_dt else "—",
            }
        )
    return providers_report


def _build_models_report(
    period_rows: Sequence[AIUsage],
) -> list[dict[str, Any]]:
    model_groups: dict[str, list[AIUsage]] = {}
    for r in period_rows:
        mname = r.model or "unknown"
        model_groups.setdefault(mname, []).append(r)

    models_report: list[dict[str, Any]] = []
    for mname, mrows in sorted(
        model_groups.items(), key=lambda item: len(item[1]), reverse=True
    ):
        m_total = len(mrows)
        m_success = sum(1 for r in mrows if r.status == "success")
        m_uptime = round((m_success / m_total) * 100, 1) if m_total > 0 else 100.0
        m_lats = [r.latency_ms for r in mrows if r.latency_ms is not None]
        m_avg_lat = round(sum(m_lats) / len(m_lats)) if m_lats else 0
        m_tokens = sum(r.total_tokens for r in mrows)
        m_cost = sum(float(r.cost_usd or 0.0) for r in mrows)
        p_name = mrows[0].provider_name or "Unknown"

        models_report.append(
            {
                "model": mname,
                "provider_name": p_name,
                "calls": m_total,
                "uptime_pct": m_uptime,
                "avg_latency_ms": m_avg_lat,
                "tokens": m_tokens,
                "cost_usd": round(m_cost, 4),
                "cost_rub": round(m_cost * float(USD_TO_RUB_RATE), 2),
            }
        )
    return models_report


def _build_daily_trends(
    period_rows: Sequence[AIUsage], now: datetime, days: int
) -> list[dict[str, Any]]:
    daily_stats: dict[str, dict[str, Any]] = {}
    for i in range(days - 1, -1, -1):
        d_day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_stats[d_day] = {
            "date": d_day,
            "label": (now - timedelta(days=i)).strftime("%d.%m"),
            "tokens": 0,
            "cost_usd": 0.0,
            "calls": 0,
        }

    for r in period_rows:
        if r.created_at:
            d_key = r.created_at.strftime("%Y-%m-%d")
            if d_key in daily_stats:
                daily_stats[d_key]["tokens"] += r.total_tokens
                daily_stats[d_key]["cost_usd"] += float(r.cost_usd or 0.0)
                daily_stats[d_key]["calls"] += 1

    daily_trends = list(daily_stats.values())
    for d in daily_trends:
        d["cost_usd"] = round(d["cost_usd"], 4)
        d["cost_rub"] = round(d["cost_usd"] * float(USD_TO_RUB_RATE), 2)
    return daily_trends


def _parse_all_time_aggregate(res: Any) -> tuple[int, int, float]:
    if res is None:
        return 0, 0, 0.0
    if isinstance(res, tuple) and len(res) >= 3:
        return int(res[0] or 0), int(res[1] or 0), float(res[2] or 0.0)
    return (
        int(getattr(res, "calls", 0) or 0),
        int(getattr(res, "tokens", 0) or 0),
        float(getattr(res, "cost", 0.0) or 0.0),
    )


async def get_system_usage_report(
    session: AsyncSession, days: int = 7
) -> dict[str, Any]:
    """Generate system-wide AI usage report with stability and token breakdown."""
    now = datetime.now(UTC)
    period_start = now - timedelta(days=days)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    stmt_period = select(AIUsage).where(AIUsage.created_at >= period_start)
    period_rows: Sequence[AIUsage] = (await session.execute(stmt_period)).scalars().all()

    stmt_all_time = select(
        func.count(AIUsage.id).label("calls"),
        func.coalesce(func.sum(AIUsage.total_tokens), 0).label("tokens"),
        func.coalesce(func.sum(AIUsage.cost_usd), 0).label("cost"),
    )
    res_exec = await session.execute(stmt_all_time)
    res_obj = (
        res_exec.first()
        if hasattr(res_exec, "first")
        else (res_exec.one() if hasattr(res_exec, "one") else None)
    )
    all_time_calls, all_time_tokens, all_time_cost = _parse_all_time_aggregate(res_obj)

    period_calls = len(period_rows)
    period_tokens = sum(r.total_tokens for r in period_rows)
    period_cost = sum(float(r.cost_usd or 0.0) for r in period_rows)

    today_rows = [r for r in period_rows if r.created_at and r.created_at >= today_start]
    today_calls = len(today_rows)
    today_tokens = sum(r.total_tokens for r in today_rows)
    today_cost = sum(float(r.cost_usd or 0.0) for r in today_rows)

    success_calls = sum(1 for r in period_rows if r.status == "success")
    overall_uptime = (
        round((success_calls / period_calls) * 100, 1) if period_calls > 0 else 100.0
    )
    latencies = [r.latency_ms for r in period_rows if r.latency_ms is not None]
    avg_latency = round(sum(latencies) / len(latencies)) if latencies else 0

    providers_report = _build_providers_report(period_rows)
    models_report = _build_models_report(period_rows)
    daily_trends = _build_daily_trends(period_rows, now, days)

    return {
        "summary": {
            "today_tokens": today_tokens,
            "today_cost_usd": round(today_cost, 4),
            "today_cost_rub": round(today_cost * float(USD_TO_RUB_RATE), 2),
            "today_calls": today_calls,
            "period_tokens": period_tokens,
            "period_cost_usd": round(period_cost, 4),
            "period_cost_rub": round(period_cost * float(USD_TO_RUB_RATE), 2),
            "period_calls": period_calls,
            "all_time_tokens": all_time_tokens,
            "all_time_cost_usd": round(all_time_cost, 4),
            "all_time_cost_rub": round(all_time_cost * float(USD_TO_RUB_RATE), 2),
            "all_time_calls": all_time_calls,
            "overall_uptime_pct": overall_uptime,
            "avg_latency_ms": avg_latency,
        },
        "providers": providers_report,
        "models": models_report[:15],
        "daily_trends": daily_trends,
    }
