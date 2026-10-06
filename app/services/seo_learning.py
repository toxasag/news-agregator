# ruff: noqa: RUF001, E501, BLE001
"""Self-learning SEO engine: tracking snippet decisions, before/after metrics, and pattern extraction."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlparse

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.seo_optimization import SeoOptimization
from app.services.ai_client import get_ai_client
from app.services.bing_connection import load_bing_connection, submit_to_indexnow
from app.services.model_pool import run_with_fallback
from app.services.model_routing import resolve_chain
from app.services.wp_connection import get_active_wp_publisher, load_wp_connection
from app.services.yandex_connection import recrawl_yandex_url

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "apply_snippet_to_wordpress_and_track",
    "evaluate_pending_optimizations",
    "generate_optimized_snippet",
    "get_learned_patterns",
    "record_optimization_decision",
    "rollback_snippet_optimization",
]


async def record_optimization_decision(
    session: AsyncSession,
    *,
    url: str,
    before_title: str,
    before_description: str,
    before_position: float,
    before_ctr: float,
    before_impressions: int,
    before_clicks: int,
    after_title: str,
    after_description: str,
    after_focus_keyword: str = "",
    category: str = "general",
    strategy: str = "ctr_booster",
    model_used: str = "",
    reasoning_prompt: str = "",
    applied_by: str = "admin",
    article_id: int | None = None,
    wp_post_id: int | None = None,
) -> SeoOptimization:
    """Record an applied SEO modification to establish the before/after learning loop."""
    opt = SeoOptimization(
        article_id=article_id,
        url=url.strip(),
        category=category.strip() or "general",
        focus_keyword=after_focus_keyword.strip(),
        strategy=strategy,
        before_title=before_title.strip(),
        before_description=before_description.strip(),
        before_position=float(before_position),
        before_ctr=float(before_ctr),
        before_impressions=int(before_impressions),
        before_clicks=int(before_clicks),
        after_title=after_title.strip(),
        after_description=after_description.strip(),
        after_focus_keyword=after_focus_keyword.strip(),
        model_used=model_used.strip(),
        reasoning_prompt=reasoning_prompt.strip(),
        wp_post_id=wp_post_id,
        applied_at=datetime.now(UTC),
        applied_by=applied_by,
        status="evaluating",
        outcome="pending",
        metrics_7d={},
        metrics_14d={},
        metrics_28d={},
    )
    session.add(opt)
    await session.commit()
    await session.refresh(opt)
    logger.info(
        "Recorded SEO optimization #%d for %s (hypothesis: %s)",
        opt.id,
        opt.url,
        opt.strategy,
    )
    return opt


async def evaluate_pending_optimizations(
    session: AsyncSession,
    fresh_pages: dict[str, dict[str, Any]],
) -> int:
    """Compare recent performance of modified URLs against their pre-change baseline.

    Args:
        session: Active DB session.
        fresh_pages: Dict mapping URL or URL path to current search metrics:
                     {"position": float, "ctr": float, "clicks": int, "impressions": int}.
    Returns:
        Number of evaluated records.
    """
    stmt = (
        select(SeoOptimization)
        .where(SeoOptimization.status.in_(("applied", "evaluating")))
        .order_by(desc(SeoOptimization.applied_at))
    )
    records = list((await session.execute(stmt)).scalars().all())
    if not records or not fresh_pages:
        return 0

    now = datetime.now(UTC)
    updated_count = 0

    for opt in records:
        url_path = urlparse(opt.url).path.rstrip("/") or "/"
        current = fresh_pages.get(opt.url) or fresh_pages.get(url_path)
        if not current:
            # Try matching with or without trailing slash
            current = fresh_pages.get(url_path + "/")

        if not current:
            continue

        days_since = (now - opt.applied_at).total_seconds() / 86400.0
        cur_pos = float(current.get("position", 0.0))
        cur_ctr = float(current.get("ctr", 0.0))
        cur_clicks = int(current.get("clicks", 0))
        cur_impr = int(current.get("impressions", 0))

        # Check 7-day milestone
        if days_since >= 7.0 and not opt.metrics_7d:
            opt.metrics_7d = {
                "position": round(cur_pos, 1),
                "ctr": round(cur_ctr, 4),
                "clicks": cur_clicks,
                "impressions": cur_impr,
                "evaluated_at": now.isoformat(),
            }
            updated_count += 1

        # Check 14-day milestone or conclusion
        if days_since >= 14.0:
            if not opt.metrics_14d:
                opt.metrics_14d = {
                    "position": round(cur_pos, 1),
                    "ctr": round(cur_ctr, 4),
                    "clicks": cur_clicks,
                    "impressions": cur_impr,
                    "evaluated_at": now.isoformat(),
                }
            ctr_lift = cur_ctr - opt.before_ctr
            clicks_lift = cur_clicks - opt.before_clicks
            pos_delta = opt.before_position - cur_pos  # Positive is improvement (e.g. 8.0 -> 4.0 = +4.0)

            opt.ctr_lift = round(ctr_lift, 4)
            opt.clicks_lift = clicks_lift
            opt.position_delta = round(pos_delta, 1)

            # Classify outcome based on statistically meaningful changes
            if (ctr_lift >= 0.005 and clicks_lift >= 0) or clicks_lift >= 15 or pos_delta >= 1.5:
                opt.outcome = "win"
                insight = (
                    f"УСПЕХ: В рубрике '{opt.category}' оптимизация сниппета повысила CTR на "
                    f"+{ctr_lift * 100:.1f}% (было {opt.before_ctr * 100:.1f}%, стало {cur_ctr * 100:.1f}%), "
                    f"клики: +{clicks_lift}, позиция: {opt.before_position:.1f} -> {cur_pos:.1f}."
                )
            elif (ctr_lift <= -0.005 and clicks_lift <= 0) or pos_delta <= -2.0:
                opt.outcome = "loss"
                insight = (
                    f"НЕУДАЧА: Снижение CTR на {ctr_lift * 100:.1f}% и позиции на {pos_delta:.1f}. "
                    f"Заголовок '{opt.after_title[:50]}' показал слабую кликабельность."
                )
            else:
                opt.outcome = "neutral"
                insight = (
                    f"НЕЙТРАЛЬНО: Без существенных изменений CTR ({ctr_lift * 100:+.1f}%) "
                    f"и позиции ({pos_delta:+.1f})."
                )

            opt.learned_insight = insight
            opt.status = "concluded"
            updated_count += 1
            logger.info("Concluded SEO optimization #%d: %s (%s)", opt.id, opt.outcome, insight)

    if updated_count > 0:
        await session.commit()

    return updated_count


async def get_learned_patterns(
    session: AsyncSession,
    *,
    category: str | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    """Retrieve synthesized winning patterns and negative anti-patterns."""
    stmt_wins = (
        select(SeoOptimization)
        .where(SeoOptimization.outcome == "win")
        .order_by(desc(SeoOptimization.ctr_lift), desc(SeoOptimization.applied_at))
        .limit(limit)
    )
    stmt_losses = (
        select(SeoOptimization)
        .where(SeoOptimization.outcome == "loss")
        .order_by(desc(SeoOptimization.applied_at))
        .limit(5)
    )

    if category and category.strip():
        stmt_wins = stmt_wins.where(SeoOptimization.category == category.strip())

    wins = list((await session.execute(stmt_wins)).scalars().all())
    losses = list((await session.execute(stmt_losses)).scalars().all())

    win_summaries: list[dict[str, Any]] = []
    total_title_len = 0
    total_desc_len = 0

    for w in wins:
        total_title_len += len(w.after_title)
        total_desc_len += len(w.after_description)
        win_summaries.append({
            "id": w.id,
            "category": w.category,
            "focus_keyword": w.focus_keyword,
            "before_title": w.before_title,
            "after_title": w.after_title,
            "ctr_lift": f"+{w.ctr_lift * 100:.1f}%" if w.ctr_lift else "+0.0%",
            "clicks_lift": f"+{w.clicks_lift}" if w.clicks_lift else "+0",
            "position_delta": f"+{w.position_delta:.1f}" if w.position_delta else "+0.0",
            "insight": w.learned_insight or "",
        })

    avg_title_len = int(total_title_len / len(wins)) if wins else 58
    avg_desc_len = int(total_desc_len / len(wins)) if wins else 145

    loss_summaries: list[dict[str, Any]] = []
    for l_opt in losses:
        loss_summaries.append({
            "id": l_opt.id,
            "category": l_opt.category,
            "after_title": l_opt.after_title,
            "ctr_lift": f"{l_opt.ctr_lift * 100:.1f}%" if l_opt.ctr_lift else "0.0%",
            "insight": l_opt.learned_insight or "",
        })

    # Global insights synthesized for the LLM
    guidelines = [
        f"Оптимальная длина Title: 52–65 символов (средняя у победителей: {avg_title_len} симв.).",
        f"Оптимальная длина Description: 135–155 символов (средняя у победителей: {avg_desc_len} симв.).",
        "Фокусный ключевик обязан стоять в первой трети Title (первые 3-4 слова).",
        "Включать фактологические маркеры (цифры, даты, имена, суммы), избегая шаблонного кликбейта ('шок', 'вы не поверите').",
        "Для рубрик Политика/Общество эффективны интригующие подзаголовки с причиной/следствием ('почему...', 'что изменится').",
    ]

    # Closed-Loop integration with /ads: infuse active advertiser goals and target segments
    try:
        from app.services.promoted_projects_service import (  # noqa: PLC0415
            PromotedProjectsService,
        )

        projects = await PromotedProjectsService.get_all_projects(session)
        for proj in projects:
            if not proj.is_active:
                continue
            if (
                category
                and proj.target_categories
                and category not in proj.target_categories
            ):
                continue
            goal_names = (
                ", ".join(
                    f"{g.name} (план CR {g.target_cr_pct}%)"
                    for g in proj.conversion_goals[:3]
                )
                if proj.conversion_goals
                else "целевые визиты по UTM"
            )
            counter_label = (
                f"Метрика #{proj.client_metrika_counter_id}"
                if proj.client_metrika_counter_id
                else "UTM-трекинг"
            )
            guidelines.append(
                f"🎯 Таргетинг рекламодателя «{proj.brand_name or proj.name}» ({counter_label}, цели: {goal_names}): "
                f"акцентировать внимание платежеспособной ЦА на практической пользе и решении задачи «{proj.problem_solved or proj.usp[:70]}»."
            )
    except Exception as ads_err:
        logger.debug("Skipping advertiser rules in get_learned_patterns: %s", ads_err)

    return {
        "wins_count": len(wins),
        "losses_count": len(losses),
        "avg_winning_title_length": avg_title_len,
        "avg_winning_desc_length": avg_desc_len,
        "winning_cases": win_summaries,
        "anti_patterns": loss_summaries,
        "guidelines": guidelines,
    }


def _extract_json_from_response(text: str) -> dict[str, Any]:
    """Robustly extract JSON payload from LLM markdown response."""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate_str = match.group(1) if match else text.strip()
    try:
        return json.loads(candidate_str)
    except json.JSONDecodeError:
        # Fallback regex extraction
        title_m = re.search(r'"seo_title"\s*:\s*"([^"]+)"', candidate_str)
        desc_m = re.search(r'"seo_description"\s*:\s*"([^"]+)"', candidate_str)
        key_m = re.search(r'"focus_keyword"\s*:\s*"([^"]+)"', candidate_str)
        hypo_m = re.search(r'"hypothesis"\s*:\s*"([^"]+)"', candidate_str)
        return {
            "seo_title": title_m.group(1) if title_m else "",
            "seo_description": desc_m.group(1) if desc_m else "",
            "focus_keyword": key_m.group(1) if key_m else "",
            "hypothesis": hypo_m.group(1) if hypo_m else "",
        }


async def generate_optimized_snippet(
    session: AsyncSession,
    *,
    url: str,
    current_title: str,
    current_description: str = "",
    position: float = 0.0,
    ctr: float = 0.0,
    impressions: int = 0,
    category: str = "general",
    focus_keyword: str = "",
) -> dict[str, Any]:
    """Generate an improved CTR-optimized Title and Description infused with learned patterns."""
    patterns = await get_learned_patterns(session, category=category)
    winning_cases = patterns.get("winning_cases", [])[:3]

    wins_context = ""
    if winning_cases:
        wins_context = "\nУСПЕШНЫЕ ПРИМЕРЫ ИЗ НАШЕЙ РЕДАКЦИИ, ПОКАЗАВШИЕ РОСТ CTR:\n"
        for idx, w in enumerate(winning_cases, 1):
            wins_context += (
                f"{idx}. Исходный: \"{w['before_title']}\" -> Победитель: \"{w['after_title']}\" "
                f"(Рост CTR: {w['ctr_lift']})\n"
            )

    guidelines_str = "\n".join(f"- {g}" for g in patterns.get("guidelines", []))

    system_instruction = (
        "Ты — ведущий SEO-редактор новостного издания и эксперт по поведенческим факторам Яндекса и Google.\n"
        "Твоя цель: переписать заголовок (Title) и описание (Description) для страницы, ранжирующейся "
        "на позициях 4–15 с низким CTR, чтобы сделать сниппет максимально кликабельным в поисковой выдаче, "
        "строго соблюдая правила журналистской этики и избегая желтизны и клише.\n\n"
        "ПРАВИЛА ИЗ БАЗЫ ЗНАНИЙ СИСТЕМЫ САМООБУЧЕНИЯ:\n"
        f"{guidelines_str}\n"
        f"{wins_context}\n"
        "ОГРАНИЧЕНИЯ ДЛИНЫ:\n"
        "- seo_title: от 50 до 65 символов. Без обрезки многоточием.\n"
        "- seo_description: от 135 до 160 символов. Конкретная суть, факты, без воды.\n"
        "- focus_keyword: главный поисковый ключ (2-4 слова).\n"
        "- hypothesis: краткое обоснование (1 предложение), за счет чего вырастет CTR.\n\n"
        "Ответ верни СТРОГО в формате JSON:\n"
        "{\n"
        '  "seo_title": "...",\n'
        '  "seo_description": "...",\n'
        '  "focus_keyword": "...",\n'
        '  "hypothesis": "..."\n'
        "}"
    )

    user_prompt = (
        f"URL страницы: {url}\n"
        f"Рубрика: {category}\n"
        f"Текущая средняя позиция: {position:.1f}\n"
        f"Текущий CTR: {ctr * 100:.2f}%\n"
        f"Показов за период: {impressions}\n"
        f"Текущий Title: {current_title}\n"
        f"Текущий Description: {current_description or 'отсутствует'}\n"
        f"Предпочтительный ключевик: {focus_keyword or 'определи по контексту'}\n\n"
        "Сгенерируй оптимизированный сниппет прямо сейчас."
    )

    writer_chain = await resolve_chain(session, "writer")

    async def _generate(model_name: str) -> dict[str, Any]:
        client = get_ai_client()
        resp_text = await client.generate_text(
            prompt=user_prompt,
            system_instruction=system_instruction,
            model_name=model_name,
            temperature=0.4,
            max_tokens=500,
        )
        parsed = _extract_json_from_response(resp_text)
        return {
            "seo_title": parsed.get("seo_title") or current_title,
            "seo_description": parsed.get("seo_description") or current_description,
            "focus_keyword": parsed.get("focus_keyword") or focus_keyword,
            "hypothesis": parsed.get("hypothesis") or "Оптимизация на базе выученных паттернов CTR",
            "model_used": model_name,
        }

    try:
        pool_res = await run_with_fallback("writer", writer_chain, _generate)
        result = pool_res.value
        result["used_fallback"] = pool_res.used_fallback
        result["model_used"] = pool_res.model
    except Exception as exc:
        logger.warning("AI generation failed for snippet, using heuristic: %s", exc)
        result = {
            "seo_title": current_title[:65].strip(),
            "seo_description": (current_description or current_title)[:155].strip(),
            "focus_keyword": focus_keyword or current_title[:30],
            "hypothesis": "Эвристическая нормализация длины",
            "model_used": "heuristic-fallback",
            "used_fallback": True,
        }

    result["learned_patterns_applied"] = patterns.get("guidelines", [])[:3]
    return result


async def apply_snippet_to_wordpress_and_track(
    session: AsyncSession,
    *,
    url: str,
    new_title: str,
    new_description: str,
    new_focus_keyword: str,
    before_title: str,
    before_description: str,
    before_position: float,
    before_ctr: float,
    before_impressions: int,
    before_clicks: int,
    category: str = "general",
    model_used: str = "",
    reasoning_hypothesis: str = "",
    applied_by: str = "admin",
) -> dict[str, Any]:
    """Update WordPress post meta via REST, ping IndexNow, and persist optimization record."""
    conn = await load_wp_connection(session)
    publisher = await get_active_wp_publisher(session)

    # Resolve local article by URL slug or path if possible
    url_path = urlparse(url).path.strip("/").split("/")[-1]
    stmt_art = select(Article).where(
        (Article.url_slug == url_path) | (Article.url_slug == url_path.removesuffix(".html"))
    )
    article = (await session.execute(stmt_art)).scalars().first()

    wp_post_id = article.wp_post_id if article and article.wp_post_id else None

    # If post_id is not known locally, try finding it via WP REST if configured
    wp_updated = False
    wp_message = ""
    if publisher.is_configured and wp_post_id:
        try:
            await publisher.update_post_seo_snippet(
                wp_post_id,
                seo_title=new_title,
                seo_description=new_description,
                focus_keyword=new_focus_keyword,
                post_title=new_title,
            )
            wp_updated = True
            wp_message = f"Метатеги поста #{wp_post_id} в WordPress успешно обновлены."
        except Exception as wp_err:
            logger.warning("Failed to update post #%s in WordPress: %s", wp_post_id, wp_err)
            wp_message = f"Не удалось обновить WordPress: {wp_err}"
    elif not wp_post_id:
        wp_message = "URL зарегистрирован в системе трекинга (пост WordPress не привязан)."

    # Update local Article record if exists
    if article:
        article.seo_title = new_title
        article.seo_description = new_description
        if new_focus_keyword and new_focus_keyword not in article.seo_keywords:
            article.seo_keywords = [new_focus_keyword, *article.seo_keywords[:5]]

    full_url = url if url.startswith("http") else (
        f"{conn.wp_base_url.rstrip('/')}/{url.lstrip('/')}" if conn else url
    )

    # Ping IndexNow for instant search engine recrawl
    indexnow_ok = False
    try:
        b_cfg = await load_bing_connection(session)
        if b_cfg.is_indexnow_active and b_cfg.has_indexnow_key:
            await submit_to_indexnow([full_url], b_cfg)
            indexnow_ok = True
    except Exception as in_err:
        logger.warning("IndexNow ping failed during SEO apply: %s", in_err)

    # Priority recrawl in Yandex Webmaster
    yandex_recrawl_ok = False
    try:
        from app.services.yandex_connection import load_yandex_connection  # noqa: PLC0415
        y_cfg = await load_yandex_connection(session)
        res = await recrawl_yandex_url(y_cfg, full_url)
        if res.get("ok"):
            yandex_recrawl_ok = True
            logger.info("Yandex recrawl queued for %s (task_id=%s)", full_url, res.get("task_id"))
    except Exception as y_err:
        logger.warning("Yandex Webmaster recrawl queue failed: %s", y_err)

    # Persist the optimization snapshot and decision
    opt = await record_optimization_decision(
        session,
        url=url,
        before_title=before_title,
        before_description=before_description,
        before_position=before_position,
        before_ctr=before_ctr,
        before_impressions=before_impressions,
        before_clicks=before_clicks,
        after_title=new_title,
        after_description=new_description,
        after_focus_keyword=new_focus_keyword,
        category=category or (article.category if article else "general"),
        strategy="ctr_booster",
        model_used=model_used,
        reasoning_prompt=reasoning_hypothesis,
        applied_by=applied_by,
        article_id=article.id if article else None,
        wp_post_id=wp_post_id,
    )

    return {
        "ok": True,
        "optimization_id": opt.id,
        "wp_updated": wp_updated,
        "indexnow_pinged": indexnow_ok,
        "yandex_recrawl_queued": yandex_recrawl_ok,
        "message": f"Оптимизация сохранена. {wp_message}",
        "record": opt.to_dict(),
    }


async def rollback_snippet_optimization(
    session: AsyncSession,
    optimization_id: int,
    *,
    reason: str = "Ручной откат по решению оператора (отрицательная динамика)",
) -> dict[str, Any]:
    """Revert an applied snippet optimization back to its baseline in WordPress."""
    opt = await session.get(SeoOptimization, optimization_id)
    if not opt:
        return {
            "ok": False,
            "message": f"Запись оптимизации #{optimization_id} не найдена.",
        }

    publisher = await get_active_wp_publisher(session)
    wp_updated = False
    wp_message = ""
    if publisher.is_configured and opt.wp_post_id:
        try:
            await publisher.update_post_seo_snippet(
                opt.wp_post_id,
                seo_title=opt.before_title,
                seo_description=opt.before_description,
                focus_keyword=opt.focus_keyword,
                post_title=opt.before_title,
            )
            wp_updated = True
            wp_message = f"Метатеги поста #{opt.wp_post_id} в WordPress возвращены к исходным значениям."
        except Exception as wp_err:
            logger.warning("Failed to rollback post #%s in WordPress: %s", opt.wp_post_id, wp_err)
            wp_message = f"Не удалось обновить WordPress: {wp_err}"

    # Revert local Article if linked
    if opt.article_id:
        art = await session.get(Article, opt.article_id)
        if art:
            art.seo_title = opt.before_title
            art.seo_description = opt.before_description

    # Ping IndexNow and Yandex Recrawl for the rollback
    conn = await load_wp_connection(session)
    full_url = opt.url if opt.url.startswith("http") else (
        f"{conn.wp_base_url.rstrip('/')}/{opt.url.lstrip('/')}" if conn else opt.url
    )
    try:
        b_cfg = await load_bing_connection(session)
        if b_cfg.is_indexnow_active and b_cfg.has_indexnow_key:
            await submit_to_indexnow([full_url], b_cfg)
    except Exception as exc:
        logger.warning("IndexNow ping failed during rollback: %s", exc)

    try:
        from app.services.yandex_connection import load_yandex_connection  # noqa: PLC0415
        y_cfg = await load_yandex_connection(session)
        await recrawl_yandex_url(y_cfg, full_url)
    except Exception as exc:
        logger.warning("Yandex recrawl failed during rollback: %s", exc)

    opt.status = "rolled_back"
    opt.outcome = "loss"
    opt.learned_insight = f"ОТКАТ: {reason}. Исходный сниппет восстановлен."
    await session.commit()
    await session.refresh(opt)

    return {
        "ok": True,
        "optimization_id": opt.id,
        "wp_updated": wp_updated,
        "message": f"Оптимизация #{opt.id} отозвана. {wp_message}",
        "record": opt.to_dict(),
    }
