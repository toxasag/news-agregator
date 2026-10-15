# ruff: noqa: RUF001, PLR0911, PLR0912, PLR0915, BLE001, S110, E501
"""Overview analytics aggregator service.

Gathers day-by-day pipeline telemetry, failure diagnostics across distribution
platforms, and detailed AI spend (tokens, costs, models, providers).

All computations are done via fast set-based SQL queries in PostgreSQL rather than
iterating over ORM objects in Python memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.cluster import NewsCluster
from app.models.enums import ArticleStatus
from app.models.raw_news import RawNews
from app.models.source import Source
from app.services.categories import CATEGORY_TITLES

PLATFORM_LABELS: Final[dict[str, str]] = {
    "wp": "WordPress",
    "dzen": "Яндекс Дзен",
    "vk": "ВКонтакте",
    "setka": "Сетка (HH)",
    "tenchat": "TenChat",
    "youtube": "YouTube Сообщество",
    "tg": "Telegram",
    "ok": "Одноклассники",
    "threads": "Threads",
    "facebook": "Facebook",
    "pinterest": "Pinterest",
}

ROLE_LABELS: Final[dict[str, str]] = {
    "writer": "Генерация статей (Writer)",
    "arbiter": "Арбитр дедупликации (Arbiter)",
    "seo": "SEO & Мета-теги",
    "image_prompt": "Промпты для обложек",
    "cover_gen": "Генератор иллюстраций",
    "editor": "Вычитка и фактчек",
}

USD_TO_RUB_RATE: Final[float] = 95.0


def humanize_failure_reason(platform: str, error: str | None) -> str:
    """Translate raw exception or API error into clear operator explanation."""
    if not error or not error.strip():
        return "Неизвестная ошибка публикации"
    err = error.strip()
    err_lower = err.lower()

    if "214" in err or "500 posts a day" in err_lower:
        return "Лимит VK API: не более 500 записей в сутки на стену"
    if "сессия истекла" in err_lower or "перенаправлено на авторизацию" in err_lower:
        return "Сессия истекла: требуется повторный вход в браузере в Настройках"
    if "поле ввода" in err_lower and "не найдено" in err_lower:
        return "Поле ввода поста не найдено (сбой интерфейса или нет прав)"
    if "кнопка добавления" in err_lower or "кнопка публикации" in err_lower:
        return "Кнопка публикации не найдена или неактивна в веб-интерфейсе"
    if "link_photo_sizing_rule" in err_lower:
        return "VK API: пропорции или размер обложки не соответствуют правилам VK"
    if "timeout" in err_lower or "таймаут" in err_lower:
        return "Таймаут открытия страницы или ожидания селектора"
    if "not configured" in err_lower or "disabled" in err_lower:
        return "Платформа отключена в настройках издания"
    if "не найдены в редакторе" in err_lower:
        return "Поля редактора не загрузились вовремя"
    if "another ip address" in err_lower or "error (5)" in err_lower:
        return "VK API: токен был привязан к другому IP (перевыпущен)"
    if "page_with_storage" in err_lower:
        return "Ошибка браузерного пула (устранено в актуальном релизе)"

    first_line = err.split("\n")[0].strip()
    if len(first_line) > 130:
        return first_line[:127] + "..."
    return first_line


def _format_day_label(d: date, today: date) -> str:
    """Format date into human-friendly Russian label."""
    weekdays = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
    wd = weekdays[d.weekday()]
    if d == today:
        return f"Сегодня, {d.strftime('%d.%m')}"
    if d == today - timedelta(days=1):
        return f"Вчера, {d.strftime('%d.%m')}"
    return f"{d.strftime('%d.%m')}, {wd}"


@dataclass(slots=True)
class DailyTimelineRow:
    """One calendar day in the pipeline overview."""

    date_str: str
    date_label: str
    raw_total: int = 0
    raw_filtered_dups: int = 0
    raw_unique: int = 0
    raw_unprocessed: int = 0
    dedup_pct: float = 0.0
    clusters_count: int = 0
    articles_total: int = 0
    articles_published: int = 0
    articles_failed: int = 0
    articles_in_progress: int = 0
    published_pct: float = 0.0
    platforms_published: dict[str, int] = field(default_factory=dict)
    platforms_failed: dict[str, int] = field(default_factory=dict)
    ai_calls: int = 0
    ai_tokens: int = 0
    ai_cost_usd: float = 0.0


@dataclass(slots=True)
class FailureIssue:
    """Grouped failure reason with article counter and samples."""

    reason: str
    raw_error: str
    count: int
    sample_articles: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PlatformFailures:
    """All failures recorded for a specific social or publishing platform."""

    platform_code: str
    platform_name: str
    total_failures: int = 0
    issues: list[FailureIssue] = field(default_factory=list)


@dataclass(slots=True)
class ModelSpendRow:
    """AI spend aggregated by model name."""

    model: str
    provider_name: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    avg_latency_ms: int
    error_count: int


@dataclass(slots=True)
class ProviderSpendRow:
    """AI spend aggregated by upstream provider gateway."""

    provider_name: str
    calls: int
    total_tokens: int
    cost_usd: float
    cost_pct: float
    error_count: int


@dataclass(slots=True)
class RoleSpendRow:
    """AI spend aggregated by pipeline stage role."""

    role: str
    role_label: str
    calls: int
    total_tokens: int
    cost_usd: float


@dataclass(slots=True)
class AISpendRadar:
    """Comprehensive AI expenditure diagnostics."""

    total_cost_usd: float = 0.0
    total_cost_rub: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_calls: int = 0
    avg_latency_ms: int = 0
    cost_per_article: float = 0.0
    models: list[ModelSpendRow] = field(default_factory=list)
    providers: list[ProviderSpendRow] = field(default_factory=list)
    roles: list[RoleSpendRow] = field(default_factory=list)


@dataclass(slots=True)
class CategoryBreakdownItem:
    """Articles count and share for a single editorial rubric."""

    category: str
    title: str
    count: int
    percentage: float


@dataclass(slots=True)
class OverviewData:
    """Full overview dataset returned to dashboard routes and templates."""

    days_window: int
    summary_cards: dict[str, Any]
    timeline: list[DailyTimelineRow]
    failures: list[PlatformFailures]
    ai_radar: AISpendRadar
    category_breakdown: list[CategoryBreakdownItem] = field(default_factory=list)
    date_from: str = ""
    date_to: str = ""
    is_custom_range: bool = False


async def _load_summary_cards(session: AsyncSession) -> dict[str, Any]:
    """Gather high-level pipeline queue totals."""
    raw_total = int(
        (await session.scalar(select(func.count()).select_from(RawNews))) or 0
    )
    clusters_total = int(
        (await session.scalar(select(func.count()).select_from(NewsCluster))) or 0
    )
    articles_total = int(
        (await session.scalar(select(func.count()).select_from(Article))) or 0
    )
    sources_total = int(
        (await session.scalar(select(func.count()).select_from(Source))) or 0
    )
    unprocessed_stmt = (
        select(func.count(RawNews.id))
        .select_from(RawNews)
        .outerjoin(NewsCluster, RawNews.cluster_id == NewsCluster.id)
        .where(
            RawNews.is_processed.is_(False),
            or_(
                RawNews.cluster_id.is_(None),
                NewsCluster.is_published.is_(False),
            ),
        )
    )
    unprocessed = int((await session.scalar(unprocessed_stmt)) or 0)
    try:
        articles_published_total = int(
            (
                await session.scalar(
                    select(func.count())
                    .select_from(Article)
                    .where(
                        or_(
                            Article.wp_post_id.is_not(None),
                            Article.status.in_(
                                [ArticleStatus.WP_PUBLISHED, ArticleStatus.DISTRIBUTED]
                            ),
                        )
                    )
                )
            )
            or 0
        )
    except Exception:
        articles_published_total = 0

    return {
        "raw_news_total": raw_total,
        "raw_news_unprocessed": unprocessed,
        "clusters_total": clusters_total,
        "articles_total": articles_total,
        "sources_total": sources_total,
        "articles_published_total": articles_published_total,
    }


async def _load_daily_timeline(
    session: AsyncSession,
    days: int,
    today: date,
    start_ts: datetime,
) -> list[DailyTimelineRow]:
    """Build day-by-day metrics across ingestion, clusters, articles, and AI spend."""
    day_map: dict[date, DailyTimelineRow] = {
        (today - timedelta(days=i)): DailyTimelineRow(
            date_str=(today - timedelta(days=i)).isoformat(),
            date_label=_format_day_label(today - timedelta(days=i), today),
        )
        for i in range(days)
    }

    try:
        # 1. Raw news
        raw_sql = text(
            """
            SELECT
                date(r.created_at) as day,
                count(*) as total,
                count(*) FILTER (WHERE r.dedup_status IN (
                    'duplicate', 'skipped_duplicate', 'filtered_digest', 'filtered_ad'
                )) as filtered_dups,
                count(*) FILTER (WHERE r.dedup_status = 'unique') as unique_posts,
                count(*) FILTER (
                    WHERE r.is_processed = false
                    AND (c.is_published = false OR c.id IS NULL)
                ) as unprocessed
            FROM raw_news r
            LEFT JOIN news_clusters c ON r.cluster_id = c.id
            WHERE r.created_at >= :start_ts
            GROUP BY date(r.created_at)
            """
        )
        res = await session.execute(raw_sql, {"start_ts": start_ts})
        raw_rows = res.all()
        for row in raw_rows:
            day_val: date | None = getattr(row, "day", None)
            if day_val and day_val in day_map:
                r = day_map[day_val]
                r.raw_total = int(getattr(row, "total", 0) or 0)
                r.raw_filtered_dups = int(getattr(row, "filtered_dups", 0) or 0)
                r.raw_unique = int(getattr(row, "unique_posts", 0) or 0)
                r.raw_unprocessed = int(getattr(row, "unprocessed", 0) or 0)
                if r.raw_total > 0:
                    r.dedup_pct = round((r.raw_filtered_dups / r.raw_total) * 100, 1)

        # 2. Clusters
        cluster_sql = text(
            """
            SELECT
                date(created_at) as day,
                count(*) as clusters_count
            FROM news_clusters
            WHERE created_at >= :start_ts
            GROUP BY date(created_at)
            """
        )
        c_res = await session.execute(cluster_sql, {"start_ts": start_ts})
        cluster_rows = c_res.all()
        for row in cluster_rows:
            d_val = getattr(row, "day", None)
            if d_val and d_val in day_map:
                day_map[d_val].clusters_count = int(
                    getattr(row, "clusters_count", 0) or 0
                )

        # 3. Articles
        article_sql = text(
            """
            SELECT
                date(created_at) as day,
                count(*) as total_articles,
                count(*) FILTER (
                    WHERE wp_post_id IS NOT NULL OR status IN ('wp_published', 'distributed')
                ) as published,
                count(*) FILTER (WHERE status = 'failed') as failed,
                count(*) FILTER (
                    WHERE status NOT IN ('wp_published', 'distributed', 'failed')
                ) as in_progress,
                count(*) FILTER (WHERE wp_post_id IS NOT NULL) as wp_published
            FROM articles
            WHERE created_at >= :start_ts
            GROUP BY date(created_at)
            """
        )
        a_res = await session.execute(article_sql, {"start_ts": start_ts})
        article_rows = a_res.all()
        for row in article_rows:
            d_val = getattr(row, "day", None)
            if d_val and d_val in day_map:
                ar = day_map[d_val]
                ar.articles_total = int(getattr(row, "total_articles", 0) or 0)
                ar.articles_published = int(getattr(row, "published", 0) or 0)
                ar.articles_failed = int(getattr(row, "failed", 0) or 0)
                ar.articles_in_progress = int(getattr(row, "in_progress", 0) or 0)
                if ar.articles_total > 0:
                    ar.published_pct = round(
                        (ar.articles_published / ar.articles_total) * 100, 1
                    )
                wp_c = int(getattr(row, "wp_published", 0) or 0)
                if wp_c > 0:
                    ar.platforms_published["wp"] = wp_c

        # 4. Platforms breakdown from JSONB
        plat_sql = text(
            """
            SELECT
                date(a.created_at) as day,
                p.key as platform,
                p.value->>'status' as p_status,
                count(*) as count
            FROM articles a,
            LATERAL jsonb_each(a.platform_statuses) p
            WHERE a.created_at >= :start_ts
            GROUP BY date(a.created_at), p.key, p.value->>'status'
            """
        )
        p_res = await session.execute(plat_sql, {"start_ts": start_ts})
        plat_rows = p_res.all()
        for row in plat_rows:
            d_val = getattr(row, "day", None)
            if d_val and d_val in day_map:
                pr = day_map[d_val]
                plat = str(getattr(row, "platform", ""))
                p_st = str(getattr(row, "p_status", ""))
                c = int(getattr(row, "count", 0) or 0)
                if p_st == "published" and c > 0:
                    pr.platforms_published[plat] = c
                elif p_st == "failed" and c > 0:
                    pr.platforms_failed[plat] = c

        # 5. AI usage daily
        ai_sql = text(
            """
            SELECT
                date(created_at) as day,
                count(*) as calls,
                sum(total_tokens) as total_tokens,
                sum(cost_usd) as cost_usd
            FROM ai_usage
            WHERE created_at >= :start_ts
            GROUP BY date(created_at)
            """
        )
        ai_res = await session.execute(ai_sql, {"start_ts": start_ts})
        ai_rows = ai_res.all()
        for row in ai_rows:
            d_val = getattr(row, "day", None)
            if d_val and d_val in day_map:
                air = day_map[d_val]
                air.ai_calls = int(getattr(row, "calls", 0) or 0)
                air.ai_tokens = int(getattr(row, "total_tokens", 0) or 0)
                air.ai_cost_usd = round(float(getattr(row, "cost_usd", 0.0) or 0.0), 4)
    except Exception:
        pass

    return [day_map[today - timedelta(days=i)] for i in range(days)]


async def _load_failures(
    session: AsyncSession,
    start_ts: datetime,
) -> list[PlatformFailures]:
    """Diagnose publication errors across distribution channels."""
    try:
        failures_sql = text(
            """
            SELECT
                p.key as platform,
                p.value->>'error' as err_msg,
                count(*) as count,
                json_agg(json_build_object(
                    'id', a.id,
                    'title', a.title,
                    'created_at', to_char(a.created_at, 'YYYY-MM-DD HH24:MI'),
                    'category', a.category
                ) ORDER BY a.created_at DESC) as sample_articles
            FROM articles a,
            LATERAL jsonb_each(a.platform_statuses) p
            WHERE a.created_at >= :start_ts
              AND p.value->>'status' = 'failed'
              AND p.value->>'error' IS NOT NULL
              AND trim(p.value->>'error') != ''
            GROUP BY p.key, p.value->>'error'
            ORDER BY count DESC
            """
        )
        f_res = await session.execute(failures_sql, {"start_ts": start_ts})
        fail_rows = f_res.all()

        platform_failures_map: dict[str, PlatformFailures] = {}
        for row in fail_rows:
            plat = str(getattr(row, "platform", ""))
            if not plat:
                continue
            if plat not in platform_failures_map:
                platform_failures_map[plat] = PlatformFailures(
                    platform_code=plat,
                    platform_name=PLATFORM_LABELS.get(plat, plat.upper()),
                    total_failures=0,
                    issues=[],
                )
            raw_err = str(getattr(row, "err_msg", "") or "")
            count = int(getattr(row, "count", 0) or 0)
            human_reason = humanize_failure_reason(plat, raw_err)
            samples = list(getattr(row, "sample_articles", []) or [])[:5]

            existing_issue = next(
                (
                    iss
                    for iss in platform_failures_map[plat].issues
                    if iss.reason == human_reason
                ),
                None,
            )
            if existing_issue:
                existing_issue.count += count
                for s in samples:
                    if len(existing_issue.sample_articles) < 5 and not any(
                        x.get("id") == s.get("id")
                        for x in existing_issue.sample_articles
                    ):
                        existing_issue.sample_articles.append(s)
            else:
                platform_failures_map[plat].issues.append(
                    FailureIssue(
                        reason=human_reason,
                        raw_error=raw_err[:250],
                        count=count,
                        sample_articles=samples,
                    )
                )
            platform_failures_map[plat].total_failures += count

        return sorted(
            platform_failures_map.values(),
            key=lambda x: x.total_failures,
            reverse=True,
        )
    except Exception:
        return []


async def _load_ai_radar(
    session: AsyncSession,
    start_ts: datetime,
    total_articles: int,
) -> AISpendRadar:
    """Aggregate token expenditure by model, provider, and pipeline role."""
    try:
        ai_totals_sql = text(
            """
            SELECT
                count(*) as total_calls,
                sum(prompt_tokens) as prompt_tokens,
                sum(completion_tokens) as completion_tokens,
                sum(total_tokens) as total_tokens,
                sum(cost_usd) as total_cost_usd,
                avg(latency_ms) as avg_latency_ms
            FROM ai_usage
            WHERE created_at >= :start_ts
            """
        )
        ai_res = await session.execute(ai_totals_sql, {"start_ts": start_ts})
        ai_tot = ai_res.first()

        total_cost_usd = round(
            float(getattr(ai_tot, "total_cost_usd", 0.0) or 0.0), 4
        )
        total_tokens = int(getattr(ai_tot, "total_tokens", 0) or 0)
        prompt_tokens = int(getattr(ai_tot, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(ai_tot, "completion_tokens", 0) or 0)
        total_calls = int(getattr(ai_tot, "total_calls", 0) or 0)
        avg_latency_ms = round(getattr(ai_tot, "avg_latency_ms", 0) or 0)

        cost_per_article = (
            round(total_cost_usd / total_articles, 4) if total_articles > 0 else 0.0
        )

        model_sql = text(
            """
            SELECT
                model,
                coalesce(provider_name, 'Google Gemini (Direct)') as provider_name,
                count(*) as calls,
                sum(prompt_tokens) as prompt_tokens,
                sum(completion_tokens) as completion_tokens,
                sum(total_tokens) as total_tokens,
                sum(cost_usd) as cost_usd,
                avg(latency_ms) as avg_latency_ms,
                count(*) FILTER (WHERE status = 'error') as error_count
            FROM ai_usage
            WHERE created_at >= :start_ts
            GROUP BY model, coalesce(provider_name, 'Google Gemini (Direct)')
            ORDER BY sum(cost_usd) DESC NULLS LAST, count(*) DESC
            """
        )
        m_res = await session.execute(model_sql, {"start_ts": start_ts})
        model_rows = m_res.all()
        models_spend = [
            ModelSpendRow(
                model=str(getattr(r, "model", "")),
                provider_name=str(getattr(r, "provider_name", "")),
                calls=int(getattr(r, "calls", 0) or 0),
                prompt_tokens=int(getattr(r, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(r, "completion_tokens", 0) or 0),
                total_tokens=int(getattr(r, "total_tokens", 0) or 0),
                cost_usd=round(float(getattr(r, "cost_usd", 0.0) or 0.0), 4),
                avg_latency_ms=round(getattr(r, "avg_latency_ms", 0) or 0),
                error_count=int(getattr(r, "error_count", 0) or 0),
            )
            for r in model_rows
        ]

        prov_sql = text(
            """
            SELECT
                coalesce(provider_name, 'Google Gemini (Direct)') as provider_name,
                count(*) as calls,
                sum(total_tokens) as total_tokens,
                sum(cost_usd) as cost_usd,
                count(*) FILTER (WHERE status = 'error') as error_count
            FROM ai_usage
            WHERE created_at >= :start_ts
            GROUP BY coalesce(provider_name, 'Google Gemini (Direct)')
            ORDER BY sum(cost_usd) DESC NULLS LAST, count(*) DESC
            """
        )
        p_res = await session.execute(prov_sql, {"start_ts": start_ts})
        prov_rows = p_res.all()
        providers_spend = []
        for r in prov_rows:
            c_usd = round(float(getattr(r, "cost_usd", 0.0) or 0.0), 4)
            pct = (
                round((c_usd / total_cost_usd) * 100, 1)
                if total_cost_usd > 0
                else 0.0
            )
            providers_spend.append(
                ProviderSpendRow(
                    provider_name=str(getattr(r, "provider_name", "")),
                    calls=int(getattr(r, "calls", 0) or 0),
                    total_tokens=int(getattr(r, "total_tokens", 0) or 0),
                    cost_usd=c_usd,
                    cost_pct=pct,
                    error_count=int(getattr(r, "error_count", 0) or 0),
                )
            )

        role_sql = text(
            """
            SELECT
                role,
                count(*) as calls,
                sum(total_tokens) as total_tokens,
                sum(cost_usd) as cost_usd
            FROM ai_usage
            WHERE created_at >= :start_ts
            GROUP BY role
            ORDER BY sum(cost_usd) DESC NULLS LAST
            """
        )
        r_res = await session.execute(role_sql, {"start_ts": start_ts})
        role_rows = r_res.all()
        roles_spend = [
            RoleSpendRow(
                role=str(getattr(r, "role", "")),
                role_label=ROLE_LABELS.get(
                    str(getattr(r, "role", "")),
                    str(getattr(r, "role", "")).capitalize(),
                ),
                calls=int(getattr(r, "calls", 0) or 0),
                total_tokens=int(getattr(r, "total_tokens", 0) or 0),
                cost_usd=round(float(getattr(r, "cost_usd", 0.0) or 0.0), 4),
            )
            for r in role_rows
        ]

        return AISpendRadar(
            total_cost_usd=total_cost_usd,
            total_cost_rub=round(total_cost_usd * USD_TO_RUB_RATE, 2),
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_calls=total_calls,
            avg_latency_ms=avg_latency_ms,
            cost_per_article=cost_per_article,
            models=models_spend,
            providers=providers_spend,
            roles=roles_spend,
        )
    except Exception:
        return AISpendRadar()


async def _load_category_breakdown(
    session: AsyncSession,
    start_ts: datetime,
    end_ts: datetime,
) -> list[CategoryBreakdownItem]:
    """Aggregate generated articles count and percentage per rubric for the time window."""
    cat_sql = text(
        """
        SELECT
            category,
            count(*) as cnt
        FROM articles
        WHERE created_at >= :start_ts AND created_at <= :end_ts
        GROUP BY category
        ORDER BY count(*) DESC
        """
    )
    res = await session.execute(cat_sql, {"start_ts": start_ts, "end_ts": end_ts})
    rows = res.all()
    total_in_period = sum(int(getattr(r, "cnt", 0) or 0) for r in rows)

    breakdown: list[CategoryBreakdownItem] = []
    for r in rows:
        cat_slug = str(getattr(r, "category", "") or "novosti-obshhestva")
        cnt = int(getattr(r, "cnt", 0) or 0)
        pct = round((cnt / total_in_period) * 100, 1) if total_in_period > 0 else 0.0
        title = CATEGORY_TITLES.get(cat_slug, cat_slug)
        breakdown.append(
            CategoryBreakdownItem(
                category=cat_slug,
                title=title,
                count=cnt,
                percentage=pct,
            )
        )
    return breakdown


async def get_overview_data(
    session: AsyncSession,
    days: int = 7,
    date_from: str | None = None,
    date_to: str | None = None,
) -> OverviewData:
    """Collect full overview telemetry for the requested days window or custom date range."""
    now_utc = datetime.now(UTC)
    today = now_utc.date()

    is_custom = bool(date_from and date_to)
    if is_custom:
        try:
            d_from = date.fromisoformat(date_from.strip())  # type: ignore[union-attr]
            d_to = date.fromisoformat(date_to.strip())  # type: ignore[union-attr]
            if d_from > d_to:
                d_from, d_to = d_to, d_from
            # Restrict custom span to at most 90 days to protect DB memory
            span_days = min(max((d_to - d_from).days + 1, 1), 90)
            start_date = d_from
            end_date = d_to
            start_ts = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=UTC)
            end_ts = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, 999999, tzinfo=UTC)
            days = span_days
        except Exception:
            is_custom = False
            start_date = today - timedelta(days=days - 1)
            end_date = today
            start_ts = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
            end_ts = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, 999999, tzinfo=UTC)
    else:
        start_date = today - timedelta(days=days - 1)
        end_date = today
        start_ts = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
        end_ts = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, 999999, tzinfo=UTC)

    summary_cards = await _load_summary_cards(session)
    timeline = await _load_daily_timeline(session, days, end_date, start_ts)
    failures = await _load_failures(session, start_ts)

    total_articles = sum(r.articles_total for r in timeline)
    ai_radar = await _load_ai_radar(session, start_ts, total_articles)
    category_breakdown = await _load_category_breakdown(session, start_ts, end_ts)

    return OverviewData(
        days_window=days,
        summary_cards=summary_cards,
        timeline=timeline,
        failures=failures,
        ai_radar=ai_radar,
        category_breakdown=category_breakdown,
        date_from=start_date.isoformat(),
        date_to=end_date.isoformat(),
        is_custom_range=is_custom,
    )
