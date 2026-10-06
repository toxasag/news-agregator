# ruff: noqa: RUF001
"""Incident and error monitoring service for the web dashboard.

Gathers active operational alerts:
- Rate limits (e.g. Setka HTTP 429)
- Social session expirations (e.g. TenChat, Spark)
- Platform daily quotas (e.g. VK 500 posts/day error 214)
- Duplicate interception metrics
Surfaces them directly as rich, actionable alert banners in the Web UI.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.enums import ArticleStatus

logger: Final[logging.Logger] = logging.getLogger(__name__)


async def get_active_incidents(  # noqa: PLR0912, PLR0915
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Collect active system incidents and alerts from the last 12 hours."""
    incidents: list[dict[str, Any]] = []
    cutoff = datetime.now(UTC) - timedelta(hours=12)

    # 1. Check duplicate articles intercepted
    dup_stmt = (
        select(func.count(Article.id))
        .where(
            Article.created_at >= cutoff,
            Article.status == ArticleStatus.SKIPPED_DUPLICATE,
        )
    )
    dup_res = await session.execute(dup_stmt)
    dup_count = dup_res.scalar() or 0
    if dup_count > 0:
        incidents.append({
            "id": "dedup_guard",
            "level": "info",
            "platform": "Антидубль",
            "badge": f"{dup_count} перехвачено",
            "badge_color": "blue",
            "title": "Активная защита от дублей",
            "message": (
                f"За 12 часов фильтр предотвратил публикацию {dup_count} "
                "повторных материалов по уже освещенным сюжетам."
            ),
            "time": "12ч",
            "link": "/articles?status=skipped_duplicate",
            "link_text": "Материалы",
        })

    # 2. Inspect platform errors across recent articles
    stmt = (
        select(Article.id, Article.title, Article.platform_statuses, Article.created_at)
        .where(Article.created_at >= cutoff)
        .order_by(Article.created_at.desc())
        .limit(200)
    )
    res = await session.execute(stmt)
    articles = res.all()

    setka_429_count = 0
    vk_214_count = 0
    tenchat_auth_count = 0
    spark_err_count = 0

    latest_setka_time: datetime | None = None
    latest_vk_time: datetime | None = None
    latest_tenchat_time: datetime | None = None
    latest_spark_time: datetime | None = None

    for a in articles:
        ps = a.platform_statuses or {}
        # Setka
        setka_entry = ps.get("setka")
        if isinstance(setka_entry, dict):
            err = str(setka_entry.get("error") or setka_entry.get("last_error") or "")
            if "429" in err or "слишком много" in err:
                setka_429_count += 1
                if not latest_setka_time:
                    latest_setka_time = a.created_at

        # VK 214
        vk_entry = ps.get("vk")
        if isinstance(vk_entry, dict):
            err = str(vk_entry.get("error") or vk_entry.get("last_error") or "")
            if "214" in err or "Access to adding post denied" in err:
                vk_214_count += 1
                if not latest_vk_time:
                    latest_vk_time = a.created_at

        # TenChat session
        tc_entry = ps.get("tenchat")
        if isinstance(tc_entry, dict):
            err = str(tc_entry.get("error") or tc_entry.get("last_error") or "")
            if any(w in err for w in ("сессия истекла", "авторизац", "login", "cookie")):
                tenchat_auth_count += 1
                if not latest_tenchat_time:
                    latest_tenchat_time = a.created_at

        # Spark
        spark_entry = ps.get("spark")
        if isinstance(spark_entry, dict):
            err = str(spark_entry.get("error") or spark_entry.get("last_error") or "")
            if any(w in err for w in ("ограничен", "сесси", "доступ")):
                spark_err_count += 1
                if not latest_spark_time:
                    latest_spark_time = a.created_at

    # Build incident records
    if setka_429_count > 0:
        time_str = latest_setka_time.strftime("%H:%M") if latest_setka_time else "недавно"
        incidents.append({
            "id": "setka_429",
            "level": "warn",
            "platform": "Сетка",
            "badge": f"429 Лимит ({setka_429_count} ст.)",
            "badge_color": "amber",
            "title": "Сетка: Временный лимит частоты (429 Too Many Requests)",
            "message": (
                f"Сетка вернула ограничение частоты на {setka_429_count} "
                f"материалах (последний: {time_str} UTC). Пауза anti-flood активна."
            ),
            "time": time_str,
            "link": "/settings",
            "link_text": "Настройки",
        })

    if vk_214_count > 0:
        time_str = latest_vk_time.strftime("%H:%M") if latest_vk_time else "недавно"
        incidents.append({
            "id": "vk_quota",
            "level": "warn",
            "platform": "ВКонтакте",
            "badge": f"Лимит 500/сутки ({vk_214_count} ст.)",
            "badge_color": "amber",
            "title": "ВКонтакте: Исчерпан суточный лимит 500 записей",
            "message": (
                "API VK вернул ошибку 214 (Access denied): достигнут лимит 500 постов "
                "в сутки. Публикация в VK возобновится со следующих суток."
            ),
            "time": time_str,
            "link": "/logs",
            "link_text": "Логи",
        })

    if tenchat_auth_count > 0:
        time_str = (
            latest_tenchat_time.strftime("%H:%M") if latest_tenchat_time else "недавно"
        )
        incidents.append({
            "id": "tenchat_session",
            "level": "error",
            "platform": "TenChat",
            "badge": "Сессия истекла",
            "badge_color": "rose",
            "title": "TenChat: Требуется обновление сессии",
            "message": (
                "Сессия браузерного профиля TenChat завершилась. "
                "Требуется повторный вход в Настройках."
            ),
            "time": time_str,
            "link": "/settings",
            "link_text": "Обновить",
        })

    if spark_err_count > 0:
        time_str = latest_spark_time.strftime("%H:%M") if latest_spark_time else "недавно"
        incidents.append({
            "id": "spark_disabled",
            "level": "warn",
            "platform": "Spark.ru",
            "badge": "Доступ ограничен",
            "badge_color": "amber",
            "title": "Spark.ru: Ограничение раздела площадки",
            "message": (
                "Аккаунт Spark.ru получил ограничение доступа. Публикация отключена."
            ),
            "time": time_str,
            "link": "/settings",
            "link_text": "Настройки",
        })

    return incidents
