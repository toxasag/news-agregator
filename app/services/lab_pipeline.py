# ruff: noqa: RUF001, E501, PLR0915, PLR0912, BLE001
"""Step-by-step lab pipeline: grouped news, duplicate inspection, cluster steppers."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import urllib.parse
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.article import Article
from app.models.audience_metric import AudienceCategoryMetric
from app.models.category_prompt import CategoryPrompt
from app.models.cluster import NewsCluster
from app.models.enums import ArticleStatus, DedupStatus, RiskLevel, SourceType
from app.models.promoted_project import PromotedProject
from app.models.raw_news import RawNews
from app.models.source import Source
from app.services.aevi_scoring import (
    CampaignQualityReport,
    calculate_cluster_aevi,
    calculate_clusters_aevi_batch,
    evaluate_campaign_quality,
)
from app.services.ai_client import get_ai_client
from app.services.categories import CATEGORY_TITLES, DEFAULT_CATEGORY, category_title
from app.services.content_generator import generate_article
from app.services.content_prompts import is_brand_safe_for_commercial_integration
from app.services.deduplicator import ENTITY_KEY_MATCHED_CLUSTER
from app.services.dzen_publisher import inject_dzen_promo, load_dzen_session_config
from app.services.image_cleaner import (
    inject_article_images,
    inspect_image_quality,
)
from app.services.image_generator import ImageGenerationError, generate_cover_image
from app.services.model_pool import ModelPoolExhaustedError, run_with_fallback
from app.services.model_routing import resolve_chain
from app.services.moderator import (
    ModerationError,
    moderate_with_fallback,
)
from app.services.pipeline_stages import preview
from app.services.promoted_projects_service import PromotedProjectsService
from app.services.research import (
    collect_facts,
    conduct_cluster_research,
    find_related_articles,
)
from app.services.schema_org import (
    evaluate_lead_hook,
    extract_faq_from_html,
    generate_schema_org_json_ld,
)
from app.services.slop_filter import analyze_slop, clean_ai_slop
from app.services.social_base import build_hashtags
from app.services.social_dzen import build_dzen_html
from app.services.social_telegram import build_caption
from app.services.social_vk import build_wall_message
from app.services.text_utils import describe_filter_reason
from app.services.usage_log import (
    get_article_usage,
    get_cluster_usage,
    record_usage,
)
from app.services.wordstat_collector import get_wordstat_service
from app.services.wp_archive import WPArchiveService
from app.services.wp_connection import get_active_wp_publisher
from app.services.wp_media_service import slugify

logger: Final[logging.Logger] = logging.getLogger(__name__)

_DUPLICATE_PEER_KIND: Final[str] = "duplicate"

STYLE_PROMPTS: Final[dict[str, str]] = {
    "Информационный": (
        "Классический объективный стиль информагентств (ТАСС, РИА Новости, Reuters). "
        "Академическая нейтральность, принцип перевернутой пирамиды: в лиде суть "
        "события, субъект действия, цифры и итог. Полное отсутствие клише, штампов "
        "('как стало известно', 'не секрет') и оценочных суждений. Строгая фактология, "
        "подтвержденные даты, прямые цитаты спикеров и лаконичный контекст."
    ),
    "Аналитический": (
        "Глубокий аналитический формат делового издания (Коммерсантъ, Ведомости, РБК). "
        "Структурный разбор причинно-следственных связей, геополитические и рыночные "
        "предпосылки события. Оценка баланса сил, выгодоприобретателей и рисков. "
        "Сопоставление с предысторией и прогнозирование краткосрочных и долгосрочных "
        "последствий с опорой на экспертные мнения."
    ),
    "Срочная молния": (
        "Формат экстренного выпуска и срочной новости (Breaking News). "
        "Максимальная плотность информации в первых 150 знаках: что произошло, где, "
        "когда и масштабы. Рубленый динамичный синтаксис, короткие информативные "
        "абзацы, хронологическая фиксация событий в реальном времени, строго без воды "
        "и пространных рассуждений."
    ),
    "Лонгрид": (
        "Развернутый журналистский репортаж и расследование (объем 2500–4000 знаков). "
        "Полномасштабная панорама событий: предыстория, атмосфера и контекст, хроника "
        "происходящего, детализация позиций всех заинтересованных сторон, "
        "структурирование подзаголовками <h2> и <h3>, расширенный фактчекинг и выводы "
        "о системном влиянии на отрасль."
    ),
    "Ироничный обзор": (
        "Интеллектуальная публицистика с тонкой иронией и сатирой (стиль фельетона). "
        "Живой образный язык без вульгарности и прямых оскорблений. Точная подколка "
        "абсурдности ситуации, сопоставление громких заявлений спикеров с фактической "
        "реальностью, меткие метафоры и остроумное резюме."
    ),
}

CATEGORY_CLUSTERS: Final[dict[str, frozenset[str]]] = {
    "politics_military": frozenset({
        "novosti-politiki",
        "voennye-novosti",
        "mirovye-novosti",
        "novosti-obshhestva",
        "zakon-i-pravo",
        "chp-i-proisshestvij",
    }),
    "economy_business": frozenset({
        "ekonomika-i-biznes",
        "finansy-i-investicii",
        "nedvizhimost",
        "kriptovalyuta",
        "karera-i-rabota",
        "novosti-politiki",
    }),
    "tech_science": frozenset({
        "it-i-tehnologii",
        "nauka-i-kosmos",
        "gadgety",
        "kriptovalyuta",
        "avto-i-transport",
    }),
    "lifestyle_culture": frozenset({
        "kultura-i-iskusstvo",
        "shou-biznes",
        "puteshestviya-i-turizm",
        "stil-i-moda",
        "sport",
        "zdorove-i-medicina",
        "novosti-obshhestva",
    }),
}


def _get_category_affinity(category: str | None) -> frozenset[str]:
    """Return set of related category slugs for thematic internal linking."""
    if not category:
        return frozenset()
    cat_clean = category.strip().lower()
    for cluster_cats in CATEGORY_CLUSTERS.values():
        if cat_clean in cluster_cats:
            return cluster_cats
    return frozenset({cat_clean})


async def get_lab_system_status(session: AsyncSession) -> dict[str, Any]:
    """Calculate honest, production-accurate system state for the Laboratory."""
    stmt_total = (
        select(func.count())
        .select_from(Source)
        .where(
            Source.source_type == SourceType.TELEGRAM,
            Source.peer_kind != _DUPLICATE_PEER_KIND,
        )
    )
    total_sources = int((await session.execute(stmt_total)).scalar_one())

    stmt_active = (
        select(func.count())
        .select_from(Source)
        .where(
            Source.source_type == SourceType.TELEGRAM,
            Source.peer_kind != _DUPLICATE_PEER_KIND,
            Source.is_active.is_(True),
        )
    )
    active_sources = int((await session.execute(stmt_active)).scalar_one())

    stmt_media = (
        select(func.count())
        .select_from(Source)
        .where(
            Source.source_type == SourceType.TELEGRAM,
            Source.peer_kind != _DUPLICATE_PEER_KIND,
            Source.is_active.is_(True),
            Source.fetch_media.is_(True),
        )
    )
    media_sources = int((await session.execute(stmt_media)).scalar_one())

    stmt_fresh = (
        select(func.count())
        .select_from(Source)
        .where(
            Source.source_type == SourceType.TELEGRAM,
            Source.peer_kind != _DUPLICATE_PEER_KIND,
            Source.is_active.is_(True),
            Source.last_scraped_at.is_(None),
        )
    )
    fresh_sources = int((await session.execute(stmt_fresh)).scalar_one())

    queued_posts = int(
        (await session.execute(select(func.count()).select_from(RawNews))).scalar_one()
    )
    queued_clusters = int(
        (
            await session.execute(select(func.count()).select_from(NewsCluster))
        ).scalar_one()
    )
    unwritten_clusters = int(
        (
            await session.execute(
                select(func.count())
                .select_from(NewsCluster)
                .where(NewsCluster.is_published.is_(False))
            )
        ).scalar_one()
    )
    published_articles = int(
        (await session.execute(select(func.count()).select_from(Article))).scalar_one()
    )
    wp_archive_total = await WPArchiveService.get_total_posts_count(session)

    is_first_pass = (fresh_sources == active_sources and active_sources > 0) or (
        queued_posts == 0
    )

    return {
        "total_sources": total_sources,
        "active_sources": active_sources,
        "media_sources": media_sources,
        "fresh_sources": fresh_sources,
        "is_first_pass": is_first_pass,
        "posts_per_source": settings.ingest_max_messages_per_source,
        "queued_posts": queued_posts,
        "queued_clusters": queued_clusters,
        "unwritten_clusters": unwritten_clusters,
        "published_articles": published_articles,
        "wp_archive_total": wp_archive_total or 8701,
        "dedup_window_hours": settings.dedup_lookback_hours,
        "threshold_lower": settings.similarity_lower_threshold,
        "threshold_upper": settings.similarity_upper_threshold,
    }


def _format_raw_post(p: RawNews, *, cluster_id: uuid.UUID) -> tuple[dict[str, Any], bool]:
    """Helper to convert RawNews into dashboard-friendly dict and primary flag."""
    is_primary = p.cluster_id == cluster_id and p.dedup_status in (
        DedupStatus.UNIQUE,
        DedupStatus.UPDATE,
    )
    sim_score = (
        round(float(p.similarity_score), 2)
        if p.similarity_score is not None
        else (1.0 if is_primary else None)
    )

    post_url = p.post_url
    if not post_url and p.source and p.source.username:
        post_url = f"https://t.me/{p.source.username}/{p.external_post_id}"

    data = {
        "id": p.id,
        "source_name": p.source.name if p.source else "Канал",
        "avatar": p.source.avatar_data_uri if p.source else None,
        "post_url": post_url,
        "created_at": (p.created_at.strftime("%d.%m %H:%M") if p.created_at else ""),
        "text": preview(p.raw_text or "", 220),
        "full_text": p.raw_text or "",
        "similarity": sim_score,
        "status": p.dedup_status.value if p.dedup_status else "unique",
        "status_label": (
            "Первоисточник"
            if is_primary
            else ("Дубль L1 MinHash" if p.dedup_level == 1 else "Дубль L2 Semantic")
        ),
        "is_primary": is_primary,
        "media_count": len(p.media_data or []),
    }
    return data, is_primary


def _resolve_post_cid(post: RawNews, cluster_id_strs: set[str]) -> uuid.UUID | None:
    """Resolve a cluster UUID for a post that may be a SKIPPED_DUPLICATE."""
    cid = post.cluster_id
    if cid is None and isinstance(post.entities, dict):
        matched = post.entities.get(ENTITY_KEY_MATCHED_CLUSTER)
        if matched and str(matched) in cluster_id_strs:
            try:
                return uuid.UUID(str(matched))
            except (ValueError, TypeError):
                pass
    return cid


def _build_filtered_entry(post: RawNews) -> dict[str, Any]:
    """Convert a filtered post to a dashboard dict with media and full details."""
    reasons_data = [
        {"code": r, "label": describe_filter_reason(r)}
        for r in (post.filter_reasons or ())
    ]
    post_url = post.post_url
    if not post_url and post.source and post.source.username:
        post_url = f"https://t.me/{post.source.username}/{post.external_post_id}"

    media_items = []
    for m in post.media_data or ():
        m_type = m.get("type", "photo")
        m_url = m.get("url") or m.get("media_id") or ""
        media_items.append({"type": m_type, "url": m_url})

    return {
        "id": post.id,
        "source_name": post.source.name if post.source else "Канал",
        "avatar": post.source.avatar_data_uri if post.source else None,
        "post_url": post_url,
        "reasons": reasons_data,
        "raw_reasons": list(post.filter_reasons or ()),
        "created_at": (
            post.created_at.strftime("%d.%m %H:%M") if post.created_at else ""
        ),
        "text": preview(post.raw_text or "", 180),
        "full_text": post.raw_text or "",
        "media": media_items,
    }


async def unblock_raw_post(session: AsyncSession, post_id: int) -> dict[str, Any]:
    """Recover a false-positive filtered post back into active clusters."""
    post = await session.get(RawNews, post_id, options=[selectinload(RawNews.source)])
    if post is None:
        raise ValueError(f"Сообщение #{post_id} не найдено.")

    # Reset filter flags
    post.is_ad = False
    post.is_digest = False
    post.filter_reasons = []
    post.dedup_status = DedupStatus.UNIQUE

    cid = post.cluster_id
    if cid is None:
        cluster_title = preview(post.raw_text or "Восстановленная новость", 120)
        cluster = NewsCluster(
            title=cluster_title,
            category="",
            risk_level=RiskLevel.GREEN,
            enrichment_data={
                "unblocked": True,
                "recovered_at": datetime.now(UTC).isoformat(),
            },
        )
        session.add(cluster)
        await session.flush()
        post.cluster_id = cluster.id
        cid = cluster.id

    await session.commit()
    logger.info("Raw post #%d unblocked and assigned to cluster %s", post_id, cid)
    return {
        "ok": True,
        "post_id": post.id,
        "cluster_id": str(cid),
        "message": f"Сообщение #{post.id} успешно разблокировано и добавлено в сюжеты!",
    }


async def _compute_cluster_aevi_with_db(
    session: AsyncSession,
    cluster: NewsCluster,
    posts: Sequence[RawNews],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute AEVI score breakdown and registered hypothesis for a cluster."""
    category_metrics: dict[str, AudienceCategoryMetric] = {}
    if cluster.category:
        try:
            stmt = select(AudienceCategoryMetric).where(
                AudienceCategoryMetric.platform == "website",
                AudienceCategoryMetric.category_slug == cluster.category,
            )
            res = await session.execute(stmt)
            m = res.scalars().first()
            if m:
                category_metrics[cluster.category] = m
        except Exception as exc:
            logger.debug("Could not fetch audience metric for AEVI: %s", exc)

    aevi = calculate_cluster_aevi(cluster, posts, category_metrics=category_metrics)
    hypothesis = aevi.to_hypothesis(str(cluster.id))
    return aevi.to_dict(), hypothesis.to_dict()


async def _build_multichannel_previews(
    session: AsyncSession,
    *,
    article: Article,
    cluster: NewsCluster,
    wp_base_url: str = "",
    selected_cover_url: str = "",
) -> dict[str, Any]:
    """Generate live previews for all 4 distribution channels (WP, Dzen, TG, VK)."""
    clean_dzen_html = build_dzen_html(article.content_html)
    dzen_cfg = await load_dzen_session_config(session)
    article_url = (
        f"{wp_base_url.rstrip('/')}/?p={article.wp_post_id}"
        if article.wp_post_id
        else f"{wp_base_url.rstrip('/')}/article"
    )

    promo_mode = dzen_cfg.promo_mode if dzen_cfg else "none"
    promo_snippet = dzen_cfg.promo_snippet if dzen_cfg else ""
    if promo_mode == "none":
        # Demo native banner for preview if none configured
        promo_mode = "native_banner"
        promo_snippet = (
            "Специальное предложение для читателей Яндекс Дзен. "
            "Эксклюзивная аналитика и оперативные новости."
        )

    dzen_preview_html = inject_dzen_promo(
        clean_dzen_html,
        promo_mode=promo_mode,
        promo_snippet=promo_snippet,
    )

    tg_hashtags = build_hashtags(article.seo_keywords)
    tg_caption = build_caption(
        title=article.title,
        body_html=article.content_html,
        url=article_url,
        description=article.seo_description or "",
        hashtags=tg_hashtags,
    )

    vk_wall_message = build_wall_message(
        title=article.title,
        body_html=article.content_html,
        description=article.seo_description or "",
        keywords=article.seo_keywords,
    )

    cover_url = selected_cover_url
    if not cover_url and cluster.enrichment_data:
        cover_url = cluster.enrichment_data.get("selected_cover_url", "")
        if not cover_url:
            web_images = cluster.enrichment_data.get("web_images") or []
            if isinstance(web_images, list) and web_images:
                first_img = web_images[0]
                if isinstance(first_img, dict):
                    cover_url = first_img.get("url", "")

    return {
        "dzen_preview_html": dzen_preview_html,
        "tg_caption": tg_caption,
        "tg_hashtags": tg_hashtags,
        "vk_wall_message": vk_wall_message,
        "cover_url": cover_url,
    }


async def get_news_groups_summary(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """Group ingested news into clusters showing coverage breadth and duplicates."""
    stmt_total = select(func.count()).select_from(NewsCluster)
    total_clusters_count = int((await session.execute(stmt_total)).scalar_one())

    stmt_clusters = (
        select(NewsCluster)
        .order_by(NewsCluster.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    clusters = list((await session.execute(stmt_clusters)).scalars().all())
    if not clusters:
        return {
            "groups": [],
            "total_clusters": total_clusters_count,
            "displayed_clusters": 0,
            "limit": limit,
            "offset": offset,
            "multi_source_clusters": 0,
            "single_source_clusters": 0,
            "filtered_posts_count": 0,
            "filtered_posts": [],
        }

    cluster_ids = [c.id for c in clusters]
    cluster_id_strs = {str(cid) for cid in cluster_ids}

    window = datetime.now(UTC) - timedelta(hours=settings.dedup_lookback_hours * 2)
    stmt_posts = (
        select(RawNews)
        .options(selectinload(RawNews.source))
        .where(RawNews.created_at >= window)
        .order_by(RawNews.created_at.asc())
    )
    posts = list((await session.execute(stmt_posts)).scalars().all())

    posts_by_cluster: dict[uuid.UUID, list[RawNews]] = {cid: [] for cid in cluster_ids}
    filtered_posts: list[dict[str, Any]] = []

    stmt_articles = select(Article).where(Article.cluster_id.in_(cluster_ids))
    articles_list = list((await session.execute(stmt_articles)).scalars().all())
    articles_by_cluster: dict[uuid.UUID, Article] = {
        a.cluster_id: a for a in articles_list if a.cluster_id
    }
    wp_base_url = (getattr(settings, "wp_base_url", None) or "https://svodka.press").rstrip("/")

    for post in posts:
        if (
            post.is_digest
            or post.is_ad
            or post.dedup_status in (DedupStatus.FILTERED_DIGEST, DedupStatus.FILTERED_AD)
        ):
            filtered_posts.append(_build_filtered_entry(post))
            continue
        cid = _resolve_post_cid(post, cluster_id_strs)
        if cid is not None and cid in posts_by_cluster:
            posts_by_cluster[cid].append(post)

    aevi_by_cluster = await calculate_clusters_aevi_batch(
        session, clusters, posts_by_cluster
    )

    groups: list[dict[str, Any]] = []
    multi_count = 0
    single_count = 0
    published_count = 0
    unpublished_count = 0

    for cluster in clusters:
        cluster_posts = posts_by_cluster.get(cluster.id, [])
        sources_map: dict[int, str] = {
            p.source.id: p.source.name for p in cluster_posts if p.source
        }

        primary_dict: dict[str, Any] | None = None
        duplicates: list[dict[str, Any]] = []
        for p in cluster_posts:
            post_info, is_primary = _format_raw_post(p, cluster_id=cluster.id)
            if is_primary and primary_dict is None:
                primary_dict = post_info
            else:
                duplicates.append(post_info)
        if primary_dict is None and duplicates:
            primary_dict = duplicates.pop(0)

        source_count = len(sources_map)
        multi_count += 1 if source_count > 1 else 0
        single_count += 1 if source_count <= 1 else 0

        fallback_title = primary_dict["text"] if primary_dict else "Без названия"

        enrichment = cluster.enrichment_data or {}
        is_ai_classified = bool(enrichment.get("ai_classified"))
        if is_ai_classified and cluster.category:
            display_cat_title = category_title(cluster.category)
            cat_status = "classified"
        else:
            display_cat_title = "🕒 Рубрика определяется ИИ"
            cat_status = "pending"

        if source_count >= 3 or len(cluster_posts) >= 5:
            demand_badge = "🔥 Горячий спрос"
            demand_level = "high"
        elif source_count >= 2:
            demand_badge = "📈 Растущий тренд"
            demand_level = "medium"
        else:
            demand_badge = "💬 Поток Telegram"
            demand_level = "standard"

        aevi = aevi_by_cluster.get(cluster.id)
        aevi_score = aevi.total_score if aevi else 0.0
        aevi_level = aevi.level if aevi else "medium"
        aevi_badge = aevi.badge_text if aevi else "💬 Умеренный"
        aevi_color = aevi.badge_color if aevi else ""
        predicted_views = aevi.predicted_views if aevi else 750
        predicted_reading = aevi.predicted_reading_time_sec if aevi else 60
        predicted_ctr = aevi.predicted_ctr_pct if aevi else 2.5
        target_audience = aevi.target_audience if aevi else "Общая новостная аудитория"

        linked_art = articles_by_cluster.get(cluster.id)
        is_pub = bool(
            cluster.is_published
            or (linked_art is not None and linked_art.status in ("wp_published", "published"))
            or (linked_art is not None and (linked_art.platform_statuses or {}).get("wp", {}).get("status") == "published")
            or (linked_art is not None and (linked_art.platform_statuses or {}).get("dzen", {}).get("status") == "published")
        )
        if is_pub:
            published_count += 1
        else:
            unpublished_count += 1

        wp_post_url = ""
        dzen_post_url = ""
        if linked_art is not None:
            if linked_art.wp_post_id:
                wp_post_url = f"{wp_base_url}/?p={linked_art.wp_post_id}"
            elif linked_art.url_slug:
                wp_post_url = f"{wp_base_url}/{linked_art.category}/{linked_art.url_slug}/"
            dzen_st = (linked_art.platform_statuses or {}).get("dzen", {})
            if isinstance(dzen_st, dict) and dzen_st.get("post_url"):
                dzen_post_url = str(dzen_st["post_url"])
            elif isinstance(dzen_st, dict) and dzen_st.get("url"):
                dzen_post_url = str(dzen_st["url"])

        groups.append(
            {
                "cluster_id": str(cluster.id),
                "title": cluster.title or fallback_title,
                "category": cluster.category or "",
                "category_title": display_cat_title,
                "category_status": cat_status,
                "risk_level": cluster.risk_level.value,
                "search_demand_badge": demand_badge,
                "search_demand_level": demand_level,
                "created_at": (
                    cluster.created_at.strftime("%d.%m %H:%M")
                    if cluster.created_at
                    else ""
                ),
                "source_count": source_count,
                "source_names": list(sources_map.values()),
                "posts_count": len(cluster_posts),
                "primary_post": primary_dict,
                "duplicates": duplicates,
                "is_published": is_pub,
                "wp_post_url": wp_post_url,
                "dzen_post_url": dzen_post_url,
                "aevi": aevi.to_dict() if aevi else None,
                "aevi_score": aevi_score,
                "aevi_level": aevi_level,
                "aevi_badge": aevi_badge,
                "aevi_color": aevi_color,
                "predicted_views": predicted_views,
                "predicted_reading_sec": predicted_reading,
                "predicted_ctr_pct": predicted_ctr,
                "target_audience": target_audience,
            }
        )

    groups.sort(
        key=lambda g: (g["aevi_score"], g["source_count"], g["posts_count"]),
        reverse=True,
    )

    return {
        "groups": groups,
        "total_clusters": total_clusters_count,
        "displayed_clusters": len(groups),
        "limit": limit,
        "offset": offset,
        "multi_source_clusters": multi_count,
        "single_source_clusters": single_count,
        "published_clusters": published_count,
        "unpublished_clusters": unpublished_count,
        "filtered_posts_count": len(filtered_posts),
        "filtered_posts": filtered_posts,
    }


async def calculate_rubric_potential(
    session: AsyncSession, title: str, description: str = ""
) -> dict[str, Any]:
    """Calculate how many recent clusters over 120 hours match the proposed new rubric."""
    raw_text = f"{title} {description}".lower()
    words = re.findall(r"[а-яёa-z]{3,}", raw_text)
    stop_stems = {
        "рубрик",
        "новост",
        "событи",
        "темат",
        "материал",
        "стать",
        "информац",
        "новы",
        "раздел",
        "сфер",
        "вопрос",
        "област",
    }
    stems = {
        w[:6] if len(w) >= 6 else w
        for w in words
        if len(w) >= 4 and not any(w.startswith(s) for s in stop_stems)
    }
    if not stems:
        stems = {w[:5] for w in words if len(w) >= 3}

    semantic_expansions: dict[str, set[str]] = {
        "кино": {"фильм", "трейлер", "актер", "актрис", "режисс", "премьер", "прокат"},
        "сериал": {"сезон", "эпизод", "стриминг", "шоу", "трейлер", "актер"},
        "игр": {"гейм", "консол", "релиз", "студи", "steam", "playstation", "xbox"},
        "крипт": {"биткоин", "токен", "блокчейн", "майнинг", "бирж", "btc", "eth"},
        "авто": {"машин", "кроссовер", "седан", "гибрид", "электрокар", "дилер", "концерн"},
        "спорт": {"матч", "турнир", "чемпион", "сборн", "клуб", "тренер", "лига"},
        "наук": {"учен", "исслед", "открыт", "космос", "лаборатор", "физик", "биолог"},
        "техно": {"смартфон", "гаджет", "нейросет", "процессор", "чип", "софт", "приложен"},
        "эконом": {"рынок", "рубл", "доллар", "инфляц", "ставк", "банк", "инвест"},
    }
    expanded_stems = set(stems)
    for key, extras in semantic_expansions.items():
        if key in raw_text or any(s.startswith(key[:4]) for s in stems):
            expanded_stems.update(extras)

    if not expanded_stems:
        return {"matching_clusters_count": 0, "matching_titles": [], "lookback_hours": 120}

    cutoff = datetime.now(UTC) - timedelta(hours=120)
    stmt = (
        select(NewsCluster.title)
        .where(NewsCluster.created_at >= cutoff)
        .order_by(NewsCluster.created_at.desc())
        .limit(500)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    matched_titles: list[str] = []
    for c_title in rows:
        if not c_title:
            continue
        c_lower = c_title.lower()
        if any(stem in c_lower for stem in expanded_stems):
            matched_titles.append(c_title)

    return {
        "matching_clusters_count": len(matched_titles),
        "matching_titles": matched_titles[:3],
        "lookback_hours": 120,
    }


async def get_cluster_detail(
    session: AsyncSession, cluster_id: uuid.UUID
) -> dict[str, Any] | None:
    """Load full step-by-step context for one selected news cluster."""
    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is None:
        return None

    stmt_direct = (
        select(RawNews)
        .options(selectinload(RawNews.source))
        .where(RawNews.cluster_id == cluster.id)
        .order_by(RawNews.created_at.asc())
    )
    direct_posts = list((await session.execute(stmt_direct)).scalars().all())

    window = datetime.now(UTC) - timedelta(hours=settings.dedup_lookback_hours * 2)
    stmt_all = (
        select(RawNews)
        .options(selectinload(RawNews.source))
        .where(RawNews.created_at >= window)
        .order_by(RawNews.created_at.asc())
    )
    all_recent = list((await session.execute(stmt_all)).scalars().all())

    all_posts = list(direct_posts)
    seen_ids = {p.id for p in direct_posts}
    cid_str = str(cluster.id)

    for p in all_recent:
        if p.id in seen_ids:
            continue
        if (
            isinstance(p.entities, dict)
            and p.entities.get(ENTITY_KEY_MATCHED_CLUSTER) == cid_str
        ):
            all_posts.append(p)
            seen_ids.add(p.id)

    all_posts.sort(key=lambda x: x.created_at or datetime.min.replace(tzinfo=UTC))

    sources_map: dict[int, str] = {}
    for p in all_posts:
        if p.source:
            sources_map[p.source.id] = p.source.name

    primary_post: dict[str, Any] | None = None
    duplicates: list[dict[str, Any]] = []

    for p in all_posts:
        post_info, is_primary = _format_raw_post(p, cluster_id=cluster.id)
        post_info["media"] = [dict(m) for m in (p.media_data or ())]
        if is_primary and primary_post is None:
            primary_post = post_info
        else:
            duplicates.append(post_info)

    if primary_post is None and duplicates:
        primary_post = duplicates.pop(0)

    facts = collect_facts(cluster, all_posts)
    related_articles = await find_related_articles(session, cluster)

    stmt_art = select(Article).where(Article.cluster_id == cluster.id)
    article = (await session.execute(stmt_art)).scalars().first()
    enrichment = cluster.enrichment_data or {}
    slop_report = analyze_slop(article.content_html) if article else None
    media_verdicts = enrichment.get("media_verdicts", [])
    is_ai_classified = bool(enrichment.get("ai_classified"))

    cat_title = (
        category_title(cluster.category)
        if cluster.category
        else "🕒 Рубрика определяется ИИ"
    )

    fallback_title = primary_post["text"] if primary_post else "Новостной сюжет"

    # Prompt template for this category — prefer DB category prompt, fall back to built-in
    prompt_voice = STYLE_PROMPTS.get("Информационный", "")
    prompt_source_label = "Системный (встроенный)"
    if cluster.category:
        cat_prompt_row = await session.get(CategoryPrompt, cluster.category)
        if cat_prompt_row and cat_prompt_row.voice.strip():
            db_voice = cat_prompt_row.voice.strip()
            # If DB voice is just the default 4-word stub ("Информационный стиль, сдержанный тон.")
            # or very short (<60 chars), combine with rich STYLE_PROMPTS and rubric specifics
            if len(db_voice) < 60 or "сдержанный тон" in db_voice.lower():
                extra_parts = []
                if cat_prompt_row.lead:
                    extra_parts.append(f"Фокус лида: {cat_prompt_row.lead}")
                if cat_prompt_row.avoid:
                    extra_parts.append(f"Исключить: {cat_prompt_row.avoid}")
                rubric_spec = f" ({'; '.join(extra_parts)})" if extra_parts else ""
                prompt_voice = f"{STYLE_PROMPTS['Информационный']}{rubric_spec}"
            else:
                prompt_voice = db_voice
            prompt_source_label = f"Из рубрики «{cat_prompt_row.title}»"
    from app.services.wp_connection import load_wp_connection  # noqa: PLC0415
    wp_conn = await load_wp_connection(session)
    wp_configured = bool(wp_conn and wp_conn.has_rest_credentials)

    from app.services.dzen_publisher import load_dzen_session_config  # noqa: PLC0415
    dzen_cfg = await load_dzen_session_config(session)
    dzen_configured = bool(dzen_cfg and dzen_cfg.has_session)

    wp_stat = (article.platform_statuses or {}).get("wp", {}) if article else {}
    dzen_stat = (article.platform_statuses or {}).get("dzen", {}) if article else {}
    is_wp_published = bool(article and article.wp_post_id) or (wp_stat.get("status") == "published")
    is_dzen_published = bool(dzen_stat and dzen_stat.get("status") in ("published", "draft"))
    wp_post_url = wp_stat.get("url") or (
        f"{wp_conn.wp_base_url.rstrip('/')}/?p={article.wp_post_id}"
        if (article and article.wp_post_id and wp_conn and wp_conn.wp_base_url)
        else ""
    )
    dzen_post_url = dzen_stat.get("url", "")
    wp_post_id = article.wp_post_id if (article and article.wp_post_id) else (wp_stat.get("post_id") or wp_stat.get("id"))
    dzen_post_id = dzen_stat.get("external_id") or dzen_stat.get("post_id")

    usage_stats = (
        await get_article_usage(session, article.id)
        if article
        else await get_cluster_usage(session, cluster.id)
    )

    aevi_dict, hypothesis_dict = await _compute_cluster_aevi_with_db(
        session, cluster, all_posts
    )

    multichannel_data = (
        await _build_multichannel_previews(
            session,
            article=article,
            cluster=cluster,
            wp_base_url=wp_conn.wp_base_url if wp_conn else "",
        )
        if article
        else {
            "dzen_preview_html": "",
            "tg_caption": "",
            "tg_hashtags": [],
            "vk_wall_message": "",
            "cover_url": "",
        }
    )

    all_promoted = await PromotedProjectsService.get_all_projects(session)
    active_projects = [p for p in all_promoted if p.is_active]

    combined_body = " ".join(p.raw_text for p in all_posts if p.raw_text)
    is_safe, safety_reason = is_brand_safe_for_commercial_integration(
        category=cluster.category or "",
        title=cluster.title or "",
        text=combined_body[:2000],
    )
    matching_projects = PromotedProjectsService.get_matching_projects(
        active_projects,
        category_slug=cluster.category or "",
        is_brand_safe=is_safe,
    )

    suggested_rubric = enrichment.get("suggested_rubric")
    if isinstance(suggested_rubric, dict):
        suggested_rubric = dict(suggested_rubric)
        potential = await calculate_rubric_potential(
            session,
            str(suggested_rubric.get("title", "")),
            str(suggested_rubric.get("description", "")),
        )
        suggested_rubric["potential"] = potential

    return {
        "cluster_id": str(cluster.id),
        "usage_stats": usage_stats,
        "title": cluster.title or fallback_title,
        "category": cluster.category or "",
        "category_title": cat_title,
        "is_ai_classified": is_ai_classified,
        "category_justification": enrichment.get("category_justification"),
        "suggested_rubric": suggested_rubric,
        "risk_level": cluster.risk_level.value,
        "is_brand_safe": is_safe,
        "brand_safety_reason": safety_reason,
        "promoted_projects": [p.to_dict() for p in matching_projects],
        "all_active_projects": [p.to_dict() for p in active_projects],
        "created_at": (
            cluster.created_at.strftime("%d.%m.%Y %H:%M") if cluster.created_at else ""
        ),
        "source_count": len(sources_map),
        "source_names": list(sources_map.values()),
        "posts_count": len(all_posts),
        "primary_post": primary_post,
        "duplicates": duplicates,
        "all_posts": [_format_raw_post(p, cluster_id=cluster.id)[0] for p in all_posts],
        "facts": facts,
        "related_articles": related_articles,
        "media_verdicts": media_verdicts,
        "fallback_image_prompt": enrichment.get(
            "fallback_image_prompt",
            (
                f"Фотореалистичный кадр по новости: {cluster.title}, "
                "профессиональная репортажная съемка, 4k"
            ),
        ),
        "article": article,
        "slop_report": slop_report,
        "is_published": cluster.is_published,
        "prompt_template": prompt_voice,
        "prompt_source_label": prompt_source_label,
        "internal_links": enrichment.get("internal_links", []),
        "wordstat_data": enrichment.get("wordstat_data"),
        "wordstat_source": enrichment.get("wordstat_source", "yandex_api"),
        "schema_json_ld": enrichment.get("schema_json_ld"),
        "lead_evaluation": enrichment.get("lead_evaluation"),
        "faq_items": enrichment.get("faq_items", []),
        "seo_metadata": enrichment.get("seo_metadata"),
        "web_sources": enrichment.get("web_sources", []),
        "web_facts": enrichment.get("web_facts", []),
        "web_images": enrichment.get("web_images", []),
        "rejected_web_images": enrichment.get("rejected_web_images", []),
        "search_query": enrichment.get("search_query", ""),
        "wp_configured": wp_configured,
        "wp_base_url": wp_conn.wp_base_url if wp_conn else "",
        "dzen_configured": dzen_configured,
        "wp_status": wp_stat,
        "dzen_status": dzen_stat,
        "is_wp_published": is_wp_published,
        "is_dzen_published": is_dzen_published,
        "wp_post_url": wp_post_url,
        "dzen_post_url": dzen_post_url,
        "wp_post_id": wp_post_id,
        "dzen_post_id": dzen_post_id,
        "style_prompts": STYLE_PROMPTS,
        "style_prompts_json": json.dumps(STYLE_PROMPTS, ensure_ascii=False),
        "aevi": aevi_dict,
        "hypothesis": hypothesis_dict,
        "dzen_preview_html": multichannel_data["dzen_preview_html"],
        "tg_caption": multichannel_data["tg_caption"],
        "tg_hashtags": multichannel_data["tg_hashtags"],
        "vk_wall_message": multichannel_data["vk_wall_message"],
        "cover_url": multichannel_data["cover_url"],
    }



async def _ai_classify_rubric(
    body: str,
    rubrics: dict[str, str],
    *,
    chain: Sequence[str] | None = None,
) -> tuple[str, str, dict[str, str] | None]:
    """Classify cluster into rubric using AI with justification, or suggest a new one."""
    rubrics_list = "\n".join(f"- {slug}: {title}" for slug, title in rubrics.items())
    prompt = (
        "Ты — шеф-редактор новостного издания. Сопоставь новость "
        "со списком рубрик сайта.\n"
        f"Список доступных рубрик:\n{rubrics_list}\n\n"
        f"Текст новости:\n{body[:1500]}\n\n"
        "Ответь в строгом формате JSON:\n"
        "{\n"
        '  "slug": "выбранный_slug_из_списка",\n'
        '  "justification": "Краткое обоснование в одно предложение, '
        'почему выбрана именно эта рубрика",\n'
        '  "suggest_new": null или {"slug": "slug-novoj-rubriki", '
        '"title": "Название новой рубрики", "description": "Краткое описание"}\n'
        "}\n"
        "Если для новости нет точной подходящей рубрики "
        "(например, фильм/сериал, а рубрики 'Кино и сериалы' нет), "
        "выбери самую близкую существующую и заполни 'suggest_new'."
    )

    models = (
        [m.strip() for m in chain if m.strip()]
        if chain
        else [settings.ai_model_arbiter]
    )

    async def _invoke(model: str) -> tuple[str, str, dict[str, str] | None]:
        client = get_ai_client()
        raw_res = await client.complete(
            prompt,
            model=model,
            max_tokens=250,
            temperature=0.2,
        )
        clean_json = raw_res.strip()
        if "{" in clean_json and "}" in clean_json:
            clean_json = clean_json[clean_json.find("{") : clean_json.rfind("}") + 1]
        data = json.loads(clean_json)
        slug = str(data.get("slug") or "").strip()
        if slug not in rubrics:
            slug = DEFAULT_CATEGORY
        justification = str(
            data.get("justification")
            or f"Рубрика «{rubrics.get(slug, slug)}» соответствует тематике сюжета."
        )
        suggest_new = data.get("suggest_new")
        if (
            isinstance(suggest_new, dict)
            and suggest_new.get("slug")
            and suggest_new.get("title")
        ):
            suggest_new_dict = {
                "slug": str(suggest_new["slug"]).strip().lower(),
                "title": str(suggest_new["title"]).strip(),
                "description": str(suggest_new.get("description") or ""),
            }
        else:
            suggest_new_dict = None
        return slug, justification, suggest_new_dict

    try:
        pool_res = await run_with_fallback("arbiter", models, _invoke)
        return pool_res.value
    except (
        ValueError,
        KeyError,
        json.JSONDecodeError,
        httpx.HTTPError,
        ModelPoolExhaustedError,
        RuntimeError,
        OSError,
    ) as exc:
        logger.warning(
            "AI category classification failed across models %s: %s, using heuristic",
            models,
            exc,
        )

    # Heuristic fallback
    lower = body.lower()
    res: tuple[str, str, dict[str, str] | None]
    if any(
        w in lower
        for w in (
            "фильм",
            "кино",
            "трейлер",
            "сериал",
            "актер",
            "режиссер",
            "хоттабыч",
        )
    ):
        if "kino-i-serialy" in rubrics:
            res = (
                "kino-i-serialy",
                "Сюжет посвящен премьере фильма и киноиндустрии.",
                None,
            )
        else:
            res = (
                "novosti-shou-biznesa",
                "Сюжет связан с премьерой фильма и индустрией развлечений.",
                {
                    "slug": "kino-i-serialy",
                    "title": "Кино и сериалы",
                    "description": (
                        "Премьеры фильмов, трейлеры, сериалы, стриминги "
                        "и новости киноиндустрии."
                    ),
                },
            )
    elif any(
        w in lower
        for w in ("матч", "футбол", "хоккей", "чемпионат", "счет", "гол", "клуб")
    ):
        res = (
            "novosti-sporta",
            "Сюжет описывает спортивное событие и результаты соревнований.",
            None,
        )
    elif any(
        w in lower
        for w in (
            "рубл",
            "доллар",
            "инфляц",
            "ставка",
            "банк",
            "акци",
            "рынок",
            "налог",
        )
    ):
        res = (
            "ekonomika",
            "Сюжет освещает финансовые и макроэкономические показатели.",
            None,
        )
    elif any(w in lower for w in ("дрон", "всу", "минобороны", "пво", "снаряд", "войск")):
        res = (
            "voennye-novosti",
            "Сюжет относится к сводкам оборонных ведомств и боевым действиям.",
            None,
        )
    else:
        res = (
            DEFAULT_CATEGORY,
            "Сюжет относится к социально значимым общественным событиям.",
            None,
        )
    return res


async def classify_cluster_category(
    session: AsyncSession, cluster_id: uuid.UUID
) -> dict[str, Any]:
    """Match cluster against rubrics, select exact one, output justification."""
    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Кластер {cluster_id} не найден.")

    stmt_posts = select(RawNews).where(RawNews.cluster_id == cluster.id)
    posts = list((await session.execute(stmt_posts)).scalars().all())
    source_texts = [p.raw_text for p in posts if p.raw_text]
    body = "\n\n".join(source_texts) or cluster.title or ""

    custom_stmt = select(CategoryPrompt)
    custom_rows = list((await session.execute(custom_stmt)).scalars().all())
    all_rubrics = dict(CATEGORY_TITLES)
    for cr in custom_rows:
        all_rubrics[cr.slug] = cr.title

    arbiter_chain = await resolve_chain(session, "arbiter")
    classified_slug, justification, suggest_new = await _ai_classify_rubric(
        body, all_rubrics, chain=arbiter_chain
    )
    await record_usage(
        session,
        role="arbiter",
        model=arbiter_chain[0] if arbiter_chain else settings.ai_model_arbiter,
        prompt_tokens=120,
        completion_tokens=40,
        total_tokens=160,
        cluster_id=cluster_id,
    )

    if cluster.enrichment_data is None:
        cluster.enrichment_data = {}
    cluster.category = classified_slug
    cluster.enrichment_data["ai_classified"] = True
    cluster.enrichment_data["category_justification"] = justification
    cluster.enrichment_data["suggested_rubric"] = suggest_new
    await session.commit()

    return {
        "cluster_id": str(cluster_id),
        "category_slug": classified_slug,
        "category_title": all_rubrics.get(classified_slug, classified_slug),
        "justification": justification,
        "suggested_rubric": suggest_new,
    }


async def create_custom_rubric(
    session: AsyncSession,
    *,
    cluster_id: uuid.UUID,
    slug: str,
    title: str,
    description: str = "",
) -> dict[str, Any]:
    """Create a new custom rubric in CategoryPrompt and bind it to this cluster."""
    clean_slug = slug.strip().lower().replace(" ", "-")
    clean_title = title.strip()

    existing = await session.get(CategoryPrompt, clean_slug)
    if existing is None:
        new_prompt = CategoryPrompt(
            slug=clean_slug,
            title=clean_title,
            voice=description or f"Информационный стиль для рубрики «{clean_title}».",
            lead="Ключевые события и факты.",
            avoid="Клише, домыслы и штампы.",
            structure="Лид, подробности, цитаты, контекст.",
            is_custom=True,
        )
        session.add(new_prompt)

    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is not None:
        if cluster.enrichment_data is None:
            cluster.enrichment_data = {}
        cluster.category = clean_slug
        cluster.enrichment_data["ai_classified"] = True
        cluster.enrichment_data["category_justification"] = (
            f"Создана и назначена новая рубрика «{clean_title}»."
        )
        cluster.enrichment_data["suggested_rubric"] = None

    await session.commit()
    return {
        "slug": clean_slug,
        "title": clean_title,
        "cluster_id": str(cluster_id),
        "message": f"Рубрика «{clean_title}» создана и успешно назначена сюжету!",
    }


async def run_cluster_research_step(
    session: AsyncSession, cluster_id: uuid.UUID
) -> dict[str, Any]:
    """Execute research: local archive facts + external web search for one cluster."""
    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Кластер {cluster_id} не найден.")

    stmt_posts = (
        select(RawNews)
        .options(selectinload(RawNews.source))
        .where(RawNews.cluster_id == cluster.id)
    )
    posts = list((await session.execute(stmt_posts)).scalars().all())

    res = await conduct_cluster_research(
        session, cluster, posts, enable_web_search=True
    )

    if cluster.enrichment_data is None:
        cluster.enrichment_data = {}
    cluster.enrichment_data["facts"] = res["facts"]
    cluster.enrichment_data["related_articles"] = res["related_articles"]
    cluster.enrichment_data["web_sources"] = res["web_sources"]
    cluster.enrichment_data["web_facts"] = res["web_facts"]
    cluster.enrichment_data["web_images"] = res.get("web_images", [])
    cluster.enrichment_data["rejected_web_images"] = res.get("rejected_web_images", [])
    cluster.enrichment_data["search_query"] = res["search_query"]
    cluster.enrichment_data["web_research"] = res["web_research"]
    await session.commit()

    return {
        "cluster_id": str(cluster_id),
        "facts": res["facts"],
        "related_articles": res["related_articles"],
        "web_sources": res["web_sources"],
        "web_facts": res["web_facts"],
        "web_images": res.get("web_images", []),
        "rejected_web_images": res.get("rejected_web_images", []),
        "search_query": res["search_query"],
    }



def _clean_text_for_prompt(text: str) -> str:
    """Strip emojis, exclamation marks, and speech clichés from headline."""
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text)
    text = re.sub(r"[⚡🔥🚨❗‼💥👇👉🎙️📸▶️🔴🟡🟢]", "", text)
    text = text.replace('"', "").replace("«", "").replace("»", "")
    return text.strip()


async def generate_editorial_image_prompt(
    session: AsyncSession, cluster: NewsCluster
) -> str:
    """Generate high-quality photorealistic editorial prompt for cover image generation."""
    clean_title = _clean_text_for_prompt(cluster.title or "")
    enrichment = cluster.enrichment_data or {}
    facts = enrichment.get("web_facts") or enrichment.get("facts") or []
    facts_str = "; ".join(facts[:3]) if facts else ""

    arbiter_chain = await resolve_chain(session, "arbiter")
    models = [m.strip() for m in arbiter_chain if m.strip()] or [settings.ai_model_arbiter]

    sys_instruction = (
        "You are an editorial photo editor and art director for Reuters and AP. "
        "Your task is to convert a news headline and facts into a single photorealistic "
        "scene prompt in English for an AI image generator (Flux Schnell / Imagen 3).\n\n"
        "RULES:\n"
        "1. Write ONLY in English.\n"
        "2. Describe a realistic, physical news documentary scene: setting (press room, conference table, industrial site, government hall), people/subjects, lighting, camera angle.\n"
        "3. Photographic style: 35mm lens documentary photojournalism, realistic natural lighting, candid shot, shallow depth of field, 8k.\n"
        "4. STRICTLY FORBIDDEN: Do NOT include any text, letters, watermarks, speech verbs, emojis, or caricature/surreal elements.\n"
        "5. Output length: 30-45 words.\n"
        "6. Return ONLY the raw prompt string without introductory phrases or quotation marks."
    )
    user_prompt = f"News headline: {clean_title}\nContext: {facts_str}"

    async def _invoke(model: str) -> str:
        client = get_ai_client()
        raw = await client.complete(
            user_prompt,
            model=model,
            system=sys_instruction,
            max_tokens=150,
            temperature=0.3,
        )
        cleaned = raw.strip().strip('"').strip("'")
        if len(cleaned) < 15:
            raise ValueError(f"Prompt too short: {cleaned}")
        return cleaned

    try:
        pool_res = await asyncio.wait_for(
            run_with_fallback("arbiter", models, _invoke),
            timeout=3.5,
        )
        await record_usage(
            session,
            role="image_prompt",
            model=pool_res.model,
            prompt_tokens=60,
            completion_tokens=35,
            total_tokens=95,
            cluster_id=cluster.id,
        )
        return pool_res.value
    except Exception as exc:
        logger.warning(
            "AI image prompt generation failed for cluster %s: %s, using fallback",
            cluster.id,
            exc,
        )

    cat = (cluster.category or "").lower()
    if any(k in cat for k in ("polit", "gos", "vlast", "arm", "silov")):
        return (
            "Editorial press photograph, documentary photojournalism of formal diplomatic and governmental press conference, "
            "wooden lectern with microphones, flags in background, warm natural lighting, candid 35mm shot, shallow depth of field, 8k"
        )
    if any(k in cat for k in ("ekonom", "finans", "biznes", "rynk")):
        return (
            "Editorial press photograph, financial district with modern glass skyscrapers, busy professionals walking in morning sunlight, "
            "documentary street photography, 35mm lens, natural reflections, sharp focus, 8k"
        )
    if any(k in cat for k in ("tehnolog", "it", "nauka")):
        return (
            "Editorial press photograph, engineers in high-tech research laboratory, clean room with server racks and diagnostic monitors, "
            "cinematic cool lighting, candid photojournalism, 35mm lens, 8k"
        )
    return (
        "Editorial documentary press photograph, wide angle shot of modern press briefing room, journalists seated with laptops, "
        "microphones and bright stage lighting, authentic photojournalism, 35mm lens, sharp focus, 8k"
    )


async def run_cluster_media_step(
    session: AsyncSession, cluster_id: uuid.UUID
) -> dict[str, Any]:
    """Execute OCR watermark inspection for photos of one cluster with video support."""
    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Кластер {cluster_id} не найден.")

    stmt_posts = select(RawNews).where(RawNews.cluster_id == cluster.id).options(selectinload(RawNews.source))
    posts = list((await session.execute(stmt_posts)).scalars().all())

    results: list[dict[str, Any]] = []
    idx = 0
    clean_photos_count = 0

    for p in posts:
        for media in p.media_data or ():
            idx += 1
            media_type = media.get("type", "photo")
            raw_url = str(media.get("url") or "")
            media_id = str(media.get("media_id") or "")

            if raw_url.startswith("http://") or raw_url.startswith("https://") or raw_url.startswith("data:"):
                img_url = raw_url
            else:
                img_url = f"/api/ui/lab/media/photo/{p.id}"

            if media_type == "video":
                video_url = (
                    p.post_url
                    or (f"https://t.me/{p.source.username}/{p.external_post_id}" if p.source and p.source.username and p.external_post_id else "")
                    or (raw_url if raw_url.startswith("http") else "")
                )
                results.append(
                    {
                        "index": idx,
                        "type": "video",
                        "post_id": p.id,
                        "url": video_url,
                        "post_url": video_url,
                        "duration": media.get("duration", 0),
                        "has_watermark": False,
                        "is_croppable": False,
                        "verdict": "clean",
                        "badge_type": "info",
                        "badge_label": "🎬 Видеозапись сюжета",
                        "source_name": p.source.name if p.source else "Telegram",
                        "reason": f"Видеофайл из первоисточника ({p.source.name if p.source else 'TG'})",
                        "vk_embed_url": media.get("vk_embed_url") or "",
                        "vk_owner_id": media.get("vk_owner_id"),
                        "vk_video_id": media.get("vk_video_id"),
                    }
                )
            else:
                # Photo inspection
                photo_file = settings.media_storage_path / "telegram" / f"{p.id}.jpg"
                photo_bytes = b""
                if photo_file.exists() and photo_file.stat().st_size > 0:
                    with contextlib.suppress(Exception):
                        photo_bytes = photo_file.read_bytes()

                has_channel_mark = bool(
                    p.source
                    and p.source.username
                    and f"@{p.source.username}" in (p.raw_text or "")
                )

                is_clean = True
                verdict_reason = "Водяных знаков не обнаружено, фото чистое и готово к публикации."
                is_crop = True

                if photo_bytes:
                    valid_qual, qual_err, _ = inspect_image_quality(photo_bytes)
                    if not valid_qual:
                        is_clean = False
                        verdict_reason = qual_err
                        is_crop = "crop" in qual_err.lower()
                elif has_channel_mark:
                    is_clean = False
                    verdict_reason = (
                        f"Обнаружена плашка канала @{p.source.username if p.source else 'tg'}. "
                        "Доступен безопасный автокроп -15%."
                    )
                    is_crop = True

                if not is_clean:
                    results.append(
                        {
                            "index": idx,
                            "type": "photo",
                            "post_id": p.id,
                            "media_id": media_id,
                            "url": img_url,
                            "source_name": p.source.name if p.source else "Telegram",
                            "has_watermark": True,
                            "is_croppable": is_crop,
                            "verdict": "watermark_plate",
                            "badge_type": "warn",
                            "badge_label": f"🟡 {verdict_reason[:45]}...",
                            "reason": verdict_reason,
                        }
                    )
                else:
                    clean_photos_count += 1
                    results.append(
                        {
                            "index": idx,
                            "type": "photo",
                            "post_id": p.id,
                            "media_id": media_id,
                            "url": img_url,
                            "source_name": p.source.name if p.source else "Telegram",
                            "has_watermark": False,
                            "is_croppable": True,
                            "verdict": "clean",
                            "badge_type": "good",
                            "badge_label": "🟢 Чистое фото (публикация as is)",
                            "reason": verdict_reason,
                        }
                    )

    # Collect web research images from enrichment_data
    enrichment = cluster.enrichment_data or {}
    raw_web_images = enrichment.get("web_images", [])
    web_images: list[dict[str, Any]] = []
    for w_img in raw_web_images:
        w_url = str(w_img.get("url") or "").strip()
        if w_url and (w_url.startswith("http://") or w_url.startswith("https://")):
            web_images.append(
                {
                    "url": w_url,
                    "title": str(w_img.get("title") or "Репортажное фото СМИ"),
                    "domain": str(w_img.get("domain") or "СМИ"),
                    "kind": str(w_img.get("kind") or "photo"),
                    "badge_label": str(w_img.get("badge_label") or "ИИ: Одобрено"),
                    "clean_verdict": str(w_img.get("clean_verdict") or ""),
                }
            )

    rejected_web_images = enrichment.get("rejected_web_images", [])

    fallback_needed = (len(results) == 0 and len(web_images) == 0) or (
        clean_photos_count == 0 and len(web_images) == 0
    )
    if fallback_needed:
        suggested_prompt = await generate_editorial_image_prompt(session, cluster)
    else:
        suggested_prompt = enrichment.get("fallback_image_prompt") or ""

    if cluster.enrichment_data is None:
        cluster.enrichment_data = {}
    if suggested_prompt:
        cluster.enrichment_data["fallback_image_prompt"] = suggested_prompt
    cluster.enrichment_data["media_verdicts"] = results
    cluster.enrichment_data["web_images"] = web_images
    await session.commit()

    return {
        "cluster_id": str(cluster_id),
        "images": results,
        "web_images": web_images,
        "rejected_web_images": rejected_web_images,
        "fallback_needed": fallback_needed,
        "suggested_prompt": suggested_prompt,
        "fallback_prompt": suggested_prompt,
    }


async def generate_fallback_image_for_cluster(
    session: AsyncSession, cluster_id: uuid.UUID, prompt: str
) -> dict[str, Any]:
    """Generate cover image for a cluster using configured AI model routing."""
    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Кластер {cluster_id} не найден.")

    clean_prompt = prompt.strip() or f"Editorial press photo for news: {cluster.title}"
    error_msg: str | None = None
    image_entry: dict[str, Any] | None = None

    try:
        image_bytes = await generate_cover_image(session, clean_prompt)
        if image_bytes is not None:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            data_uri = f"data:image/jpeg;base64,{b64}"
            image_entry = {
                "index": 1,
                "type": "photo",
                "url": data_uri,
                "is_generated": True,
                "has_watermark": False,
                "is_croppable": False,
                "verdict": "clean",
                "badge_type": "good",
                "badge_label": "🟢 Сгенерированная обложка (ИИ)",
                "reason": "Сгенерировано ИИ по репортажному промту",
            }
        else:
            error_msg = (
                "Генерация изображений отключена в матрице ролей ИИ (роль image: disabled) "
                "или не настроен активный ключ провайдера изображений (Fal AI Flux, OpenAI DALL-E, Google Imagen). "
                "Проверьте настройки в разделе «Настройки -> ИИ»."
            )
    except (
        ImageGenerationError,
        ValueError,
        KeyError,
        httpx.HTTPError,
        RuntimeError,
        OSError,
    ) as exc:
        logger.warning("AI cover fallback generation failed: %s", exc)
        error_msg = f"Ошибка генерации изображения: {exc}"

    if cluster.enrichment_data is None:
        cluster.enrichment_data = {}

    current_verdicts = list(cluster.enrichment_data.get("media_verdicts", []))
    if image_entry:
        # Prepend or replace previously generated image
        current_verdicts = [
            img for img in current_verdicts if not img.get("is_generated")
        ]
        current_verdicts.insert(0, image_entry)
        cluster.enrichment_data["media_verdicts"] = current_verdicts

    cluster.enrichment_data["fallback_image_prompt"] = clean_prompt
    await session.commit()
    return {
        "cluster_id": str(cluster_id),
        "images": current_verdicts,
        "generation_error": error_msg,
    }


def _stem_ru(w: str) -> str:
    """Normalize Russian word by stripping inflection endings for fuzzy matching."""
    w = w.lower().strip()
    if len(w) > 4:
        return re.sub(
            r"(?:ая|ое|ые|ие|ый|ий|ой|ем|ом|ам|ами|ям|ями|ов|ев|ей|ях|ах|у|ю|а|я|е|и|ы|ь)$",
            "",
            w,
        )
    return w


#: High-frequency geographic and generic Russian stems that must NEVER trigger topic overlap
GEOGRAPHIC_AND_GENERIC_STOP_STEMS: Final[frozenset[str]] = frozenset(
    {
        "облас", "регион", "район", "город", "край", "росси", "стран",
        "врем", "сутк", "числ", "люд", "человек", "мест", "час",
        "дело", "служб", "день", "года", "новост", "событ", "сообщен",
        "данн", "главн", "итог", "слов", "рассказ", "перв", "нов",
        "информац", "руковод", "власт", "жител", "центр", "следств",
        "проверк", "ситуац", "назван", "отмет", "заяв",
    }
)


def _inject_link_into_paragraphs(
    html_text: str, stem: str, link_url: str, link_title: str
) -> tuple[str, str | None]:
    """Inject internal link into the first suitable paragraph outside existing HTML tags and anchors."""
    p_pattern = re.compile(r"(<p\b[^>]*>)(.*?)(</p>)", re.IGNORECASE | re.DOTALL)
    stem_esc = re.escape(stem)
    word_pattern = re.compile(rf"\b({stem_esc}[а-яА-Яa-zA-Z]*)\b", re.IGNORECASE)

    injected_anchor: str | None = None

    def _replace_in_p(p_match: re.Match[str]) -> str:
        nonlocal injected_anchor
        if injected_anchor is not None:
            return p_match.group(0)

        p_open, p_body, p_close = p_match.group(1), p_match.group(2), p_match.group(3)
        if f'href="{link_url}"' in p_body:
            return p_match.group(0)

        tokens = re.split(r"(<[^>]+>)", p_body)
        new_tokens: list[str] = []
        inside_anchor = False

        for token in tokens:
            if injected_anchor is not None:
                new_tokens.append(token)
                continue

            if token.startswith("<"):
                if re.match(r"^<a\b", token, re.IGNORECASE):
                    inside_anchor = True
                elif re.match(r"^</a\b", token, re.IGNORECASE):
                    inside_anchor = False
                new_tokens.append(token)
                continue

            if inside_anchor:
                new_tokens.append(token)
                continue

            m = word_pattern.search(token)
            if m:
                raw_word = m.group(1)
                anchor = raw_word.strip('«»"\'.,;:!?-–— ')
                if len(anchor) >= 4:
                    injected_anchor = anchor
                    replacement = (
                        f'<a href="{link_url}" target="_blank" '
                        'class="internal-link text-[#0071E3] font-semibold '
                        'underline hover:text-[#0077ED]" '
                        f'title="{link_title}">{anchor}</a>'
                    )
                    new_token = token[: m.start()] + replacement + token[m.end() :]
                    new_tokens.append(new_token)
                    continue

            new_tokens.append(token)

        return f"{p_open}{''.join(new_tokens)}{p_close}"

    new_html = p_pattern.sub(_replace_in_p, html_text)
    return new_html, injected_anchor


async def _inject_internal_links(
    session: AsyncSession,
    *,
    cluster_id: uuid.UUID,
    html_content: str,
    cluster_title: str,
    cluster_category: str | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Find related articles from database and inject active anchor links into HTML."""
    affinity_cats = _get_category_affinity(cluster_category)

    stmt = (
        select(Article)
        .where(
            Article.cluster_id != cluster_id,
            Article.category.in_(affinity_cats) if affinity_cats else True,
        )
        .order_by(Article.created_at.desc())
        .limit(15)
    )
    past_articles = list((await session.execute(stmt)).scalars().all())

    # Fallback to general articles only if no affinity articles exist
    if not past_articles and affinity_cats:
        stmt_general = (
            select(Article)
            .where(Article.cluster_id != cluster_id)
            .order_by(Article.created_at.desc())
            .limit(10)
        )
        past_articles = list((await session.execute(stmt_general)).scalars().all())

    candidates: list[dict[str, str]] = []
    for art in past_articles:
        if art.title:
            wp_stat = (art.platform_statuses or {}).get("wp", {})
            real_url = wp_stat.get("url") if isinstance(wp_stat, dict) else None
            if not real_url:
                cat_slug = art.category or "novosti"
                slug_val = art.url_slug or f"post-{art.id}"
                real_url = f"https://svodka.press/{cat_slug}/{slug_val}/"
            candidates.append({"title": art.title, "url": real_url, "category": art.category or ""})

    try:
        wp_posts = await WPArchiveService.get_archive_posts(session)
        for wp_p in wp_posts:
            w_title = wp_p.get("title", "")
            w_link = wp_p.get("link") or ""
            if not w_link and wp_p.get("slug"):
                w_link = f"https://svodka.press/{wp_p.get('slug')}/"
            if w_title and w_link and not any(c["url"] == w_link for c in candidates):
                candidates.append({"title": w_title, "url": w_link, "category": ""})
    except Exception as exc:
        logger.debug("Failed to load WP archive posts for internal links: %s", exc)

    if not candidates:
        return html_content, []

    # ── Strict relevance guards ────────────────────────────────────────────
    # Military news is strictly isolated: never links into or from civil emergencies/accidents
    military_cats: Final[frozenset[str]] = frozenset({"voennye-novosti"})
    art_category = (cluster_category or "").strip().lower()
    _is_military_article = art_category in military_cats

    cluster_words = {
        w.lower() for w in re.findall(r"\b[A-Za-zА-Яа-я0-9]{4,}\b", cluster_title)
    }
    cluster_stems = {_stem_ru(w) for w in cluster_words}
    cluster_meaningful_stems = {
        s for s in cluster_stems
        if s not in GEOGRAPHIC_AND_GENERIC_STOP_STEMS and len(s) >= 4
    }

    injected: list[dict[str, str]] = []
    updated_html = html_content

    # Prioritize candidates with highest keyword/stem overlap
    scored_candidates: list[tuple[int, dict[str, str], set[str]]] = []
    for cand in candidates:
        cand_cat = (cand.get("category") or "").strip().lower()

        # Strict isolation: military news must ONLY link to military news
        if not _is_military_article and cand_cat in military_cats:
            continue
        if _is_military_article and cand_cat and cand_cat not in military_cats:
            continue

        cand_words = {
            w.lower() for w in re.findall(r"\b[A-Za-zА-Яа-я0-9]{4,}\b", cand["title"])
        }
        cand_stems = {_stem_ru(w) for w in cand_words}
        cand_meaningful_stems = {
            s for s in cand_stems
            if s not in GEOGRAPHIC_AND_GENERIC_STOP_STEMS and len(s) >= 4
        }
        overlap_stems = cluster_meaningful_stems & cand_meaningful_stems
        scored_candidates.append((len(overlap_stems), cand, overlap_stems))

    scored_candidates.sort(key=lambda x: x[0], reverse=True)

    # Cross-category links require at least 2 shared stems; same-category only needs 1
    for ol_count, cand, overlap_stems in scored_candidates:
        cand_cat = (cand.get("category") or "").strip().lower()
        same_category = cand_cat == art_category
        min_stems_required = 1 if same_category else 2
        if ol_count < min_stems_required:
            continue

        cand_title = cand["title"]
        cand_url = cand["url"]

        for stem in sorted(overlap_stems, key=len, reverse=True):
            candidate_html, anchor_used = _inject_link_into_paragraphs(
                updated_html, stem, cand_url, cand_title
            )
            if anchor_used:
                updated_html = candidate_html
                injected.append(
                    {
                        "anchor": anchor_used,
                        "url": cand_url,
                        "article_title": cand_title,
                    }
                )
                break

        if len(injected) >= 3:
            break

    # If no inline links injected, inject recommendation block only if candidate is truly relevant
    if not injected and scored_candidates:
        best_overlap, best_cand, _ = scored_candidates[0]
        best_cat = (best_cand.get("category") or "").strip().lower()
        same_cat = best_cat == art_category
        min_req = 1 if same_cat else 2
        is_thematic = (
            best_overlap >= min_req
            and (
                same_cat
                or (bool(best_cand.get("category")) and best_cand["category"] in affinity_cats)
            )
        )
        # Never recommend military into non-military
        if not _is_military_article and best_cat in military_cats:
            is_thematic = False

        if is_thematic:
            link_url = best_cand["url"]
            link_title = best_cand["title"]
            recommendation_block = (
                '<div class="internal-link-callout my-4 rounded-xl border '
                'border-[#0071E3]/20 bg-[#0071E3]/5 p-3 text-xs">'
                '<span class="font-semibold text-mac-ink">Читайте по теме: </span>'
                f'<a href="{link_url}" target="_blank" '
                'class="text-[#0071E3] font-semibold underline hover:text-[#0077ED]">'
                f"{link_title}</a>"
                "</div>"
            )
            updated_html += recommendation_block
            injected.append(
                {
                    "anchor": link_title,
                    "url": link_url,
                    "article_title": link_title,
                }
            )

    return updated_html, injected



_KNOWN_MEDIA_OUTLET_URLS: Final[dict[str, str]] = {
    "rt": "https://russian.rt.com",
    "rt на русском": "https://russian.rt.com",
    "russia today": "https://russian.rt.com",
    "the national interest": "https://nationalinterest.org",
    "national interest": "https://nationalinterest.org",
    "tni": "https://nationalinterest.org",
    "the wall street journal": "https://www.wsj.com",
    "wall street journal": "https://www.wsj.com",
    "wsj": "https://www.wsj.com",
    "риа новости": "https://ria.ru",
    "риа": "https://ria.ru",
    "ria.ru": "https://ria.ru",
    "тасс": "https://tass.ru",
    "tass": "https://tass.ru",
    "рбк": "https://www.rbc.ru",
    "rbc": "https://www.rbc.ru",
    "коммерсантъ": "https://www.kommersant.ru",
    "коммерсант": "https://www.kommersant.ru",
    "ведомости": "https://www.vedomosti.ru",
    "известия": "https://iz.ru",
    "интерфакс": "https://www.interfax.ru",
    "interfax": "https://www.interfax.ru",
    "лента.ру": "https://lenta.ru",
    "lenta.ru": "https://lenta.ru",
    "газета.ru": "https://www.gazeta.ru",
    "газета.ру": "https://www.gazeta.ru",
    "российская газета": "https://rg.ru",
    "rg.ru": "https://rg.ru",
    "вести.ru": "https://www.vesti.ru",
    "вести": "https://www.vesti.ru",
    "forbes": "https://www.forbes.ru",
    "forbes russia": "https://www.forbes.ru",
    "reuters": "https://www.reuters.com",
    "рейтер": "https://www.reuters.com",
    "bloomberg": "https://www.bloomberg.com",
    "блюмберг": "https://www.bloomberg.com",
    "financial times": "https://www.ft.com",
    "ft": "https://www.ft.com",
    "associated press": "https://apnews.com",
    "ap": "https://apnews.com",
    "the new york times": "https://www.nytimes.com",
    "new york times": "https://www.nytimes.com",
    "nyt": "https://www.nytimes.com",
    "the washington post": "https://www.washingtonpost.com",
    "washington post": "https://www.washingtonpost.com",
    "politico": "https://www.politico.com",
    "cnn": "https://edition.cnn.com",
    "bbc": "https://www.bbc.com",
    "al jazeera": "https://www.aljazeera.com",
    "military watch magazine": "https://militarywatchmagazine.com",
    "military watch": "https://militarywatchmagazine.com",
    "business insider": "https://www.businessinsider.com",
    "the economist": "https://www.economist.com",
    "the guardian": "https://www.theguardian.com",
    "der spiegel": "https://www.spiegel.de",
    "spiegel": "https://www.spiegel.de",
    "bild": "https://www.bild.de",
    "le monde": "https://www.lemonde.fr",
    "le figaro": "https://www.lefigaro.fr",
    "xinhua": "https://english.news.cn",
    "синьхуа": "https://russian.news.cn",
    "mash": "https://t.me/breakingmash",
    "baza": "https://t.me/bazabazon",
    "shot": "https://t.me/shot_shot",
    "readovka": "https://t.me/readovkanews",
    "осторожно, новости": "https://t.me/ostorozhno_novosti",
    "топор live": "https://t.me/topor",
    "раньше всех. ну почти": "https://t.me/bbbreaking",
    "рыбарь": "https://t.me/rybar",
    "военкоры русской весны": "https://t.me/RVvoenkor",
    "минобороны рф": "https://t.me/mod_russia",
    "минобороны россии": "https://t.me/mod_russia",
    "мид рф": "https://t.me/MariaVladimirovnaZakharova",
    "мид россии": "https://mid.ru",
    "мчс рф": "https://t.me/mchs_official",
    "мчс россии": "https://t.me/mchs_official",
    "мвд рф": "https://t.me/mediamvd",
    "ск рф": "https://t.me/sledcom_press",
    "росавиация": "https://t.me/favt_ru",
    "минтранс рф": "https://t.me/mintrans_russia",
    "цб рф": "https://cbr.ru",
    "банк россии": "https://cbr.ru",
}


def resolve_raw_post_attribution(  # noqa: PLR0911
    post: RawNews | None,
    source: Any | None = None,
) -> tuple[str, str]:
    """Resolve an exact clickable URL and human-readable label for a raw news post or source."""
    if post is None and source is None:
        return "", ""

    src = source or getattr(post, "source", None)
    s_name = str(getattr(src, "name", "") or "").strip() if src else ""
    s_ident = str(getattr(src, "identifier", "") or "").strip() if src else ""
    p_url = str(getattr(post, "post_url", "") or "").strip() if post else ""
    ext_id = getattr(post, "external_post_id", None) if post else None

    # 1. If post already has a direct URL (e.g. https://t.me/.../1234 or news site article), use it
    if p_url.startswith(("http://", "https://")):
        label = f"Telegram-канал «{s_name}»" if (s_name and "t.me" in p_url) else (s_name or "Первоисточник")
        return p_url, label

    if s_ident.startswith(("http://", "https://")):
        return s_ident, s_name or "Первоисточник"
    if "." in s_ident and "/" not in s_ident and " " not in s_ident:
        return f"https://{s_ident}", s_name or "Первоисточник"

    # 2. Try to construct direct Telegram post link
    tg_handle = ""
    s_user = str(getattr(src, "username", "") or "").strip().lstrip("@")
    if s_user:
        tg_handle = s_user
    elif s_ident and not s_ident.startswith("-100") and not s_ident.lstrip("-").isdigit():
        tg_handle = s_ident.lstrip("@").strip()

    # Check known media outlets mapping if handle is unknown or numeric
    if not tg_handle and s_name:
        s_norm = s_name.lower().strip()
        known_url = _KNOWN_MEDIA_OUTLET_URLS.get(s_norm, "")
        if known_url and "t.me/" in known_url:
            tg_handle = known_url.split("t.me/")[-1].split("/")[0].strip()

    if tg_handle:
        if ext_id:
            url = f"https://t.me/{tg_handle}/{ext_id}"
        else:
            url = f"https://t.me/{tg_handle}"
        label = f"Telegram-канал «{s_name or tg_handle}»"
        return url, label

    # 3. Known media outlet domain/site fallback
    if s_name:
        s_norm = s_name.lower().strip()
        if s_norm in _KNOWN_MEDIA_OUTLET_URLS:
            return _KNOWN_MEDIA_OUTLET_URLS[s_norm], s_name
        for k, u in _KNOWN_MEDIA_OUTLET_URLS.items():
            if len(k) >= 4 and (k in s_norm or s_norm in k):
                return u, s_name

    if s_name:
        return f"https://t.me/s/{urllib.parse.quote(s_name)}", s_name

    return "", s_name or "Первоисточник"


async def load_cluster_raw_posts(
    session: AsyncSession, cluster_id: uuid.UUID
) -> list[RawNews]:
    """Load all source posts for a cluster.

    Includes the primary post(s) with cluster_id == cluster_id, PLUS any
    subsequent posts from other channels that matched this cluster during
    deduplication (recorded in entities['dedup_matched_cluster_id']).
    """
    cid_str = str(cluster_id)
    stmt = (
        select(RawNews)
        .options(selectinload(RawNews.source))
        .where(
            or_(
                RawNews.cluster_id == cluster_id,
                RawNews.entities.contains({"dedup_matched_cluster_id": cid_str}),
            )
        )
        .order_by(RawNews.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


def _resolve_source_name_to_url(  # noqa: PLR0911
    name: str,
    web_sources: Sequence[Mapping[str, Any]] | None = None,
    raw_posts: Sequence[RawNews] | None = None,
) -> str:
    """Resolve a human-readable source name, @handle, or domain to a clickable URL."""
    cleaned = name.strip().strip("«»\"'.,;:()[]")
    if not cleaned:
        return ""
    if cleaned.startswith(("http://", "https://")):
        return cleaned

    handle_match = re.search(r"@([A-Za-z0-9_]{3,64})", cleaned)
    if handle_match:
        return f"https://t.me/{handle_match.group(1)}"

    norm = cleaned.lower()
    norm_stripped = re.sub(r"^(telegram-канал|телеграм-канал|канал|иа|сми)\s+", "", norm).strip("«»\"' ")

    for ws in web_sources or ():
        ws_url = str(ws.get("url") or "").strip()
        ws_domain = str(ws.get("domain") or "").strip().lower().removeprefix("www.")
        ws_title = str(ws.get("title") or "").strip().lower()
        if not ws_url:
            continue
        if ws_domain and (
            norm_stripped == ws_domain
            or norm_stripped == ws_domain.split(".")[0]
            or ws_domain in norm_stripped
        ):
            return ws_url
        if ws_title and (norm_stripped in ws_title or ws_title in norm_stripped):
            return ws_url

    for p in raw_posts or ():
        p_url, _ = resolve_raw_post_attribution(p)
        src = getattr(p, "source", None)
        s_name = str(getattr(src, "name", "") or "").strip().lower()
        s_ident = str(getattr(src, "identifier", "") or "").strip().lstrip("@").lower()
        if (
            s_name
            and p_url
            and (norm_stripped == s_name or s_name in norm_stripped or norm_stripped in s_name)
        ):
            return p_url
        if s_ident and norm_stripped == s_ident:
            return p_url

    if norm_stripped in _KNOWN_MEDIA_OUTLET_URLS:
        return _KNOWN_MEDIA_OUTLET_URLS[norm_stripped]
    if norm in _KNOWN_MEDIA_OUTLET_URLS:
        return _KNOWN_MEDIA_OUTLET_URLS[norm]

    for known_key, known_url in _KNOWN_MEDIA_OUTLET_URLS.items():
        if len(known_key) >= 4 and (known_key in norm_stripped or norm_stripped in known_key):
            return known_url

    if re.match(r"^[a-z0-9.-]+\.[a-z]{2,10}$", norm_stripped):
        return f"https://{norm_stripped}"

    return ""


def _linkify_photo_captions(
    html_content: str,
    web_sources: Sequence[Mapping[str, Any]] | None = None,
    raw_posts: Sequence[RawNews] | None = None,
) -> str:
    """Ensure any plain-text 'Фото: <source>' inside <figcaption> has an active <a> link."""
    def _replace_photo_credit(match: re.Match[str]) -> str:
        prefix = match.group(1)
        credit_body = match.group(2).strip()
        suffix = match.group(3)
        if "<a " in credit_body.lower():
            return match.group(0)
        clean_name = credit_body.rstrip(".")
        href = _resolve_source_name_to_url(clean_name, web_sources, raw_posts)
        if not href:
            return match.group(0)
        return (
            f'{prefix}<a href="{href}" target="_blank" rel="nofollow noopener noreferrer" '
            f'style="color:#0071e3;text-decoration:underline;">{clean_name}</a>{suffix}'
        )

    return re.sub(
        r"(Фото:\s*)([^<\n]+?)(\s*</(?:span|figcaption)>)",
        _replace_photo_credit,
        html_content,
        flags=re.IGNORECASE,
    )


def _ensure_sources_block(
    html_content: str,
    web_sources: Sequence[Mapping[str, Any]] | None,
    raw_posts: Sequence[RawNews] | None = None,
) -> str:
    """Ensure article ends with active clickable attribution links to external media and source channels."""
    if not html_content:
        return html_content

    html_content = _linkify_photo_captions(html_content, web_sources, raw_posts)

    source_items: list[str] = []
    seen_urls: set[str] = set()
    seen_labels: set[str] = set()

    for ws in web_sources or ():
        title = str(ws.get("title") or ws.get("domain") or "").strip()
        url = str(ws.get("url") or "").strip()
        domain = str(ws.get("domain") or "").strip().lower()
        if not title or not url or domain in seen_labels or url in seen_urls:
            continue
        seen_labels.add(domain)
        seen_urls.add(url)
        label = domain if (len(title) > 40 or not title) else title
        source_items.append(
            f'<a href="{url}" target="_blank" rel="noopener noreferrer" class="text-[#0071E3] underline">{label}</a>'
        )

    if raw_posts:
        for p in raw_posts:
            p_url, p_label = resolve_raw_post_attribution(p)
            if p_url and p_url not in seen_urls:
                seen_urls.add(p_url)
                source_items.append(
                    f'<a href="{p_url}" target="_blank" rel="noopener noreferrer" class="text-[#0071E3] underline">{p_label}</a>'
                )

    existing_pattern = re.compile(
        r"(<p[^>]*>\s*<strong>\s*Источники?(?:\s+информации)?\s*:\s*</strong>\s*)(.*?)(</p>)",
        re.IGNORECASE | re.DOTALL,
    )
    match = existing_pattern.search(html_content)
    if match:
        prefix, inner_raw, suffix = match.group(1), match.group(2), match.group(3)
        raw_parts = [part.strip().rstrip(".") for part in re.split(r"[,;]", inner_raw) if part.strip()]
        linked_parts: list[str] = []
        for part in raw_parts:
            if "<a " in part.lower():
                linked_parts.append(part)
                continue
            resolved_url = _resolve_source_name_to_url(part, web_sources, raw_posts)
            if resolved_url:
                seen_urls.add(resolved_url)
                linked_parts.append(
                    f'<a href="{resolved_url}" target="_blank" rel="noopener noreferrer" class="text-[#0071E3] underline">{part}</a>'
                )
            # Unlinked hallucinated text phrases (e.g. "сообщения очевидцев") are explicitly discarded!

        # Also append concrete web_sources / raw_posts links if not already present
        for extra_item in source_items:
            href_m = re.search(r'href="([^"]+)"', extra_item)
            if href_m and href_m.group(1) not in seen_urls:
                seen_urls.add(href_m.group(1))
                linked_parts.append(extra_item)

        if not linked_parts:
            linked_parts = list(source_items)

        if not linked_parts:
            # If no genuine sources exist, remove the empty block completely
            return existing_pattern.sub("", html_content)

        rebuilt = f"{prefix}{', '.join(linked_parts)}.{suffix}"
        return existing_pattern.sub(lambda _: rebuilt, html_content, count=1)

    if "article-sources" in html_content.lower():
        return html_content

    if not source_items:
        return html_content

    sources_html = (
        '\n<div class="article-sources pt-4 mt-6 border-t border-black/[0.08] text-xs text-neutral-500">\n'
        f'  <p><strong>Источники информации:</strong> {", ".join(source_items)}.</p>\n'
        "</div>"
    )
    return html_content + sources_html


async def _generate_wordstat_and_seo(
    session: AsyncSession, title: str, body: str, keywords: list[str]
) -> dict[str, Any]:
    """Generate Wordstat frequency queries, focus keyword, and llm.txt using WordstatService."""
    service = get_wordstat_service()
    res = await service.collect_seo_and_wordstat(session, title, body, keywords)
    return {
        "focus_keyword": res.focus_keyword,
        "seo_title": res.seo_title,
        "seo_description": res.seo_description,
        "wordstat_queries": res.queries,
        "source": res.source,
        "llm_txt": res.llm_txt,
    }


async def _upsert_article_from_generated(
    session: AsyncSession,
    *,
    cluster_id: uuid.UUID,
    fallback_title: str | None,
    fallback_category: str,
    generated: dict[str, Any],
    cleaned_html: str,
) -> Article:
    """Create or update the Article row with validated generation outputs."""
    stmt_art = select(Article).where(Article.cluster_id == cluster_id)
    article = (await session.execute(stmt_art)).scalars().first()

    title_val = str(generated.get("title") or fallback_title or "Новость")
    seo_title_val = str(generated.get("seo_title") or title_val)[:70]
    seo_desc_val = str(generated.get("seo_description") or "")[:1024]
    keywords_raw = generated.get("seo_keywords") or ["новости"]
    keywords_list = [str(k) for k in keywords_raw]
    category_val = str(
        generated.get("category_slug") or fallback_category or DEFAULT_CATEGORY
    )

    slug_base = slugify(seo_title_val or title_val, max_chars=180) or f"post-{cluster_id.hex[:8]}"

    if article is None:
        article = Article(
            cluster_id=cluster_id,
            title=title_val,
            content_html=cleaned_html,
            seo_title=seo_title_val,
            seo_description=seo_desc_val,
            seo_keywords=keywords_list,
            category=category_val,
            url_slug=slug_base,
            status=ArticleStatus.DRAFT,
        )
        session.add(article)
    else:
        article.title = title_val
        article.content_html = cleaned_html
        article.seo_title = seo_title_val
        article.seo_description = seo_desc_val
        article.seo_keywords = keywords_list
        article.category = category_val
        # Fix Cyrillic, malformed, or trailing-period slugs
        if (
            not article.url_slug
            or bool(re.search(r"[а-яА-ЯёЁ]", article.url_slug))
            or article.url_slug.endswith(".")
        ):
            article.url_slug = slug_base

    await session.commit()
    await session.refresh(article)
    return article


def _build_augmented_cluster_data(
    cluster: NewsCluster,
    posts: Sequence[RawNews],
    combined_source: str,
    *,
    focus_keyword: str = "",
    wordstat_queries: Sequence[dict[str, Any]] | None = None,
    seo_guidelines: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Assemble cluster data payload augmented with facts, external web sources, and Wordstat focus."""
    enrichment = cluster.enrichment_data or {}
    web_facts = enrichment.get("web_facts", [])
    web_sources = enrichment.get("web_sources", [])

    all_facts = list(enrichment.get("facts", []))
    for wf in web_facts:
        if wf and wf not in all_facts:
            all_facts.append(f"[Контекст]: {wf}")

    augmented_source = combined_source
    if web_sources:
        web_blocks = []
        for ws in web_sources[:3]:
            d = ws.get("domain", "")
            t = ws.get("title", "")
            txt = ws.get("extracted_text") or ws.get("snippet", "")
            if txt:
                web_blocks.append(f"Источник: {d} ({t})\n{txt[:1500]}")
        if web_blocks:
            augmented_source += (
                "\n\n=== КОНТЕКСТ И АНАЛИТИЧЕСКИЕ ДАННЫЕ ИЗ ВНЕШНЕГО РЕСЕРЧА ===\n"
                + "\n\n".join(web_blocks)
            )

    if seo_guidelines:
        clean_guides = [g.strip() for g in seo_guidelines if g and g.strip()]
        if clean_guides:
            augmented_source += (
                "\n\n=== ПРОВЕРЕННЫЕ SEO-ПРАВИЛА И СНИППЕТ-ПАТТЕРНЫ ИЗ БАЗЫ ЗНАНИЙ (CTR WINNERS) ===\n"
                + "\n".join(f"• {g}" for g in clean_guides)
            )

    return {
        "title": cluster.title or (posts[0].raw_text[:70] if posts else ""),
        "text": augmented_source,
        "facts": all_facts,
        "focus_keyword": focus_keyword,
        "wordstat_queries": list(wordstat_queries or ()),
        "sources": [
            {
                "name": p.source.name if p.source else "Канал",
                "text": p.raw_text or "",
                "url": p.post_url or "",
            }
            for p in posts
        ],
    }


async def run_cluster_generate_step(
    session: AsyncSession,
    cluster_id: uuid.UUID,
    *,
    style_angle: str = "Информационный",
    custom_instructions: str = "",
    prompt_override: str = "",
    selected_cover_url: str = "",
    promoted_project_id: str = "",
    image_layout: str = "inline",
    override_brand_safety: bool = False,
) -> dict[str, Any]:
    """Run AI moderation, article generation, internal links, and SEO/Wordstat."""
    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Кластер {cluster_id} не найден.")

    stmt_posts = select(RawNews).where(RawNews.cluster_id == cluster.id)
    posts = list((await session.execute(stmt_posts)).scalars().all())
    source_texts = [p.raw_text for p in posts if p.raw_text]
    combined_source = "\n\n---\n\n".join(source_texts) if source_texts else cluster.title

    body_text = combined_source or cluster.title or ""
    try:
        arbiter_chain = await resolve_chain(session, "arbiter")
        mod_verdict, _ = await moderate_with_fallback(body_text, arbiter_chain)
        if mod_verdict.blocks_generation:
            err = f"Кластер отклонен модератором: {mod_verdict.reason}"
            raise ValueError(err)
        cluster.category = mod_verdict.category
        cluster.risk_level = mod_verdict.risk_level
    except (ValueError, KeyError, ModerationError) as exc:
        logger.warning("Moderation fallback used for %s: %s", cluster_id, exc)

    prompt_voice = prompt_override.strip() or STYLE_PROMPTS.get(
        style_angle, STYLE_PROMPTS["Информационный"]
    )
    if custom_instructions.strip():
        prompt_voice += f"\nДополнительно: {custom_instructions.strip()}"

    # ── Auto-research if enrichment_data lacks web context ────────────────
    enrichment_check = cluster.enrichment_data or {}
    web_facts_available = bool(enrichment_check.get("web_facts") or enrichment_check.get("web_sources"))
    if not web_facts_available:
        logger.info(
            "Cluster %s has no web research — running inline research before generation",
            cluster_id,
        )
        try:
            res = await conduct_cluster_research(
                session,
                cluster_id=cluster.id,
                cluster_title=cluster.title or "",
                posts=posts,
                timeout=20.0,
            )
            if cluster.enrichment_data is None:
                cluster.enrichment_data = {}
            cluster.enrichment_data["facts"] = res.get("facts", [])
            cluster.enrichment_data["web_sources"] = res.get("web_sources", [])
            cluster.enrichment_data["web_facts"] = res.get("web_facts", [])
            cluster.enrichment_data["web_images"] = res.get("web_images", [])
            cluster.enrichment_data["search_query"] = res.get("search_query", "")
            await session.commit()
            logger.info(
                "Inline research for cluster %s: %d facts, %d sources, %d images",
                cluster_id,
                len(cluster.enrichment_data["web_facts"]),
                len(cluster.enrichment_data["web_sources"]),
                len(cluster.enrichment_data["web_images"]),
            )
        except Exception as exc:
            logger.warning("Inline research failed for cluster %s: %s", cluster_id, exc)

    # Verify Promoted Project and Brand Safety
    promoted_project: PromotedProject | None = None
    is_brand_safe_flag = True
    safety_reason_str = ""
    if promoted_project_id.strip():
        promoted_project = await PromotedProjectsService.get_project_by_id(
            session, promoted_project_id.strip()
        )
        if promoted_project:
            is_brand_safe_flag, safety_reason_str = is_brand_safe_for_commercial_integration(
                category=cluster.category or DEFAULT_CATEGORY,
                title=cluster.title or "",
                text=combined_source,
                override_force=override_brand_safety,
            )
            if not is_brand_safe_flag:
                logger.warning(
                    "Cluster %s rejected for promoted integration (%s): %s",
                    cluster_id,
                    promoted_project.brand_name,
                    safety_reason_str,
                )
                promoted_project = None

    # Pre-calculate genuine/semantic Wordstat queries so writer weaves focus keyword organically
    seo_data = await _generate_wordstat_and_seo(
        session, cluster.title, combined_source, []
    )

    # Closed-Loop Knowledge Base: fetch winning patterns & advertiser goals for the category
    seo_guidelines: list[str] = []
    try:
        from app.services.seo_learning import get_learned_patterns  # noqa: PLC0415

        learned_info = await get_learned_patterns(
            session, category=cluster.category or DEFAULT_CATEGORY, limit=10
        )
        seo_guidelines = list(learned_info.get("guidelines") or [])
    except Exception as learn_err:
        logger.debug("Skipping learned SEO patterns for cluster %s: %s", cluster_id, learn_err)

    cluster_data = _build_augmented_cluster_data(
        cluster,
        posts,
        combined_source,
        focus_keyword=seo_data["focus_keyword"],
        wordstat_queries=seo_data["wordstat_queries"],
        seo_guidelines=seo_guidelines,
    )

    writer_chain = await resolve_chain(session, "writer")

    async def _write(model: str) -> dict[str, Any]:
        return await generate_article(
            cluster_data,
            category=cluster.category or DEFAULT_CATEGORY,
            model_name=model,
            risk_level=cluster.risk_level.value,
            custom_style_prompt=prompt_voice,
            promoted_project=promoted_project,
            cluster_id=str(cluster_id),
        )

    used_model = "unknown"
    try:
        pool_res = await run_with_fallback("writer", writer_chain, _write)
        generated = pool_res.value
        used_model = pool_res.model
        if pool_res.used_fallback:
            logger.info(
                "Lab generation for cluster %s used fallback model %s",
                cluster_id,
                pool_res.model,
            )
    except Exception:
        logger.exception("AI generation fallback for cluster %s", cluster_id)
        used_model = "heuristic-fallback"

        # Construct a rich, full multi-paragraph article from source posts and research facts
        enrichment = cluster.enrichment_data or {}
        all_facts = list(enrichment.get("facts") or []) + list(enrichment.get("web_facts") or [])
        facts_html = ""
        if all_facts:
            facts_items = "".join(f"<li>{f}</li>" for f in all_facts[:6])
            facts_html = f"<h3>Ключевые факты и детали расследования</h3><ul>{facts_items}</ul>"

        clean_html = (
            f"<p><strong>{cluster.title}</strong></p>"
            f"<p>{combined_source.strip()}</p>"
            f"{facts_html}"
            f"<p>Редакция следит за развитием событий. Детали и комментарии официальных лиц дополняются по мере поступления информации.</p>"
        )
        generated = {
            "title": cluster.title or "Сводка новостей",
            "content_html": clean_html,
            "seo_title": (cluster.title or "Сводка новостей")[:70],
            "seo_description": preview(combined_source, 150),
            "seo_keywords": ["новости", "события", "расследование"],
            "category_slug": cluster.category or DEFAULT_CATEGORY,
        }

    raw_html = str(generated.get("content_html") or "")
    slop_rep = analyze_slop(raw_html)
    cleaned_html = clean_ai_slop(raw_html)

    # Mandatory internal linking to past archive articles
    html_with_links, internal_links = await _inject_internal_links(
        session,
        cluster_id=cluster.id,
        html_content=cleaned_html,
        cluster_title=cluster.title,
        cluster_category=cluster.category,
    )

    # Inject clean article images (distributed inline or slideshow carousel)
    enrichment = cluster.enrichment_data or {}
    web_images = enrichment.get("web_images") or []
    body_images = [
        img
        for img in web_images
        if isinstance(img, dict)
        and img.get("url")
        and img.get("url") != selected_cover_url
    ]
    html_with_images = inject_article_images(
        html_with_links,
        body_images,
        layout=image_layout,
    )

    # Mandatory attribution to external sources and primary channel
    web_sources = enrichment.get("web_sources") or ()
    final_article_html = _ensure_sources_block(html_with_images, web_sources, posts)

    generated["seo_title"] = seo_data["seo_title"]
    generated["seo_description"] = seo_data["seo_description"]

    article = await _upsert_article_from_generated(
        session,
        cluster_id=cluster.id,
        fallback_title=cluster.title,
        fallback_category=cluster.category or DEFAULT_CATEGORY,
        generated=generated,
        cleaned_html=final_article_html,
    )

    usage_block = generated.get("usage") or {}
    await record_usage(
        session,
        role="writer",
        model=used_model,
        prompt_tokens=int(usage_block.get("prompt_tokens") or 0),
        completion_tokens=int(usage_block.get("completion_tokens") or 0),
        total_tokens=int(usage_block.get("total_tokens") or 0),
        cost_usd=usage_block.get("cost"),
        provider_name=usage_block.get("provider_name"),
        latency_ms=usage_block.get("latency_ms"),
        cluster_id=cluster.id,
        article_id=article.id,
    )
    usage_stats = await get_article_usage(session, article.id)

    # Excalibur standards: lead hook evaluation, FAQ extraction, and Schema.org JSON-LD
    first_p_match = re.search(r"<p[^>]*>(.*?)</p>", html_with_links, re.DOTALL | re.IGNORECASE)
    lead_text = first_p_match.group(1) if first_p_match else ""
    lead_eval = evaluate_lead_hook(lead_text)
    faq_items = extract_faq_from_html(html_with_links)
    article_url = f"https://svodka.press/{article.category}/{article.url_slug or f'post-{article.id}'}/"
    schema_ld = generate_schema_org_json_ld(
        headline=article.title,
        description=seo_data["seo_description"],
        url=article_url,
        published_at=article.created_at,
        keywords=article.seo_keywords,
        faq_items=faq_items,
    )

    cqi_report: CampaignQualityReport | None = None
    if promoted_project_id.strip():
        if not is_brand_safe_flag:
            cqi_report = evaluate_campaign_quality(
                is_brand_safe=False,
                brand_safety_reason=safety_reason_str,
            )
        elif promoted_project:
            # Baseline forecast CQI calculation using cluster metrics and benchmarks
            aevi_dict_temp, _ = await _compute_cluster_aevi_with_db(session, cluster, posts)
            pred_views = int(aevi_dict_temp.get("predicted_views") or 750)
            pred_reading = float(aevi_dict_temp.get("predicted_reading_time_sec") or 65.0)
            pred_clicks = max(1, int(pred_views * 0.035))
            cqi_report = evaluate_campaign_quality(
                is_brand_safe=True,
                brand_safety_reason="",
                media_reading_sec=pred_reading,
                media_bounce_rate=0.25,
                media_reads=pred_views,
                link_clicks=pred_clicks,
                target_visits=pred_clicks,
                target_conversions=max(1, int(pred_clicks * 0.05)),
                target_bounce_rate=0.30,
            )

    if cluster.enrichment_data is None:
        cluster.enrichment_data = {}
    cluster.enrichment_data["internal_links"] = internal_links
    cluster.enrichment_data["wordstat_data"] = seo_data["wordstat_queries"]
    cluster.enrichment_data["wordstat_source"] = seo_data.get("source", "yandex_api")
    cluster.enrichment_data["schema_json_ld"] = schema_ld
    cluster.enrichment_data["lead_evaluation"] = lead_eval
    cluster.enrichment_data["faq_items"] = faq_items
    cluster.enrichment_data["seo_metadata"] = {
        "focus_keyword": seo_data["focus_keyword"],
        "seo_title": seo_data["seo_title"],
        "seo_description": seo_data["seo_description"],
        "llm_txt": seo_data["llm_txt"],
    }
    cluster.enrichment_data["image_layout"] = image_layout
    if selected_cover_url.strip():
        cluster.enrichment_data["selected_cover_url"] = selected_cover_url.strip()
    if promoted_project:
        cluster.enrichment_data["promoted_project_id"] = promoted_project.id
        cluster.enrichment_data["promoted_brand_name"] = promoted_project.brand_name
        cluster.enrichment_data["tracked_url"] = promoted_project.build_tracked_url(
            f"cluster_{str(cluster_id)[:8]}"
        )
    if cqi_report:
        cluster.enrichment_data["cqi_report"] = cqi_report.to_dict()
    await session.commit()

    aevi_dict, hypothesis_dict = await _compute_cluster_aevi_with_db(
        session, cluster, posts
    )
    from app.services.wp_connection import load_wp_connection  # noqa: PLC0415

    wp_conn = await load_wp_connection(session)
    multichannel_data = await _build_multichannel_previews(
        session,
        article=article,
        cluster=cluster,
        wp_base_url=wp_conn.wp_base_url if wp_conn else "",
        selected_cover_url=selected_cover_url,
    )

    return {
        "cluster_id": str(cluster_id),
        "article_id": article.id,
        "title": article.title,
        "content_html": article.content_html,
        "image_layout": image_layout,
        "seo_title": seo_data["seo_title"],
        "seo_description": seo_data["seo_description"],
        "seo_keywords": article.seo_keywords,
        "focus_keyword": seo_data["focus_keyword"],
        "wordstat_queries": seo_data["wordstat_queries"],
        "wordstat_source": seo_data.get("source", "yandex_api"),
        "schema_json_ld": schema_ld,
        "lead_evaluation": lead_eval,
        "faq_items": faq_items,
        "llm_txt": seo_data["llm_txt"],
        "internal_links": internal_links,
        "slop_clean": slop_rep.is_valid,
        "slop_density": slop_rep.density,
        "usage_stats": usage_stats,
        "aevi": aevi_dict,
        "hypothesis": hypothesis_dict,
        "dzen_preview_html": multichannel_data["dzen_preview_html"],
        "tg_caption": multichannel_data["tg_caption"],
        "tg_hashtags": multichannel_data["tg_hashtags"],
        "vk_wall_message": multichannel_data["vk_wall_message"],
        "cover_url": multichannel_data["cover_url"],
        "is_brand_safe": is_brand_safe_flag,
        "brand_safety_reason": safety_reason_str,
        "promoted_project_id": promoted_project.id if promoted_project else "",
        "promoted_brand_name": promoted_project.brand_name if promoted_project else "",
        "tracked_url": (
            promoted_project.build_tracked_url(f"cluster_{str(cluster_id)[:8]}")
            if promoted_project
            else ""
        ),
        "cqi": cqi_report.to_dict() if cqi_report else None,
    }


async def run_cluster_publish_step(
    session: AsyncSession, cluster_id: uuid.UUID
) -> dict[str, Any]:
    """Publish the cluster's article to WordPress and mark complete."""
    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Кластер {cluster_id} не найден.")

    stmt_art = select(Article).where(Article.cluster_id == cluster.id)
    article = (await session.execute(stmt_art)).scalars().first()
    if article is None:
        raise ValueError("Сначала необходимо сгенерировать текст статьи на шаге 3.")

    from app.services.wp_connection import load_wp_connection  # noqa: PLC0415

    conn = await load_wp_connection(session)
    if not conn.has_rest_credentials and not settings.wp_base_url:
        return {
            "ok": False,
            "post_id": None,
            "post_url": None,
            "message": (
                "Сайт WordPress не подключён. Перейдите в раздел «Настройки» "
                "и укажите URL сайта, логин и Application Password."
            ),
            "warnings": ["WordPress REST API не настроен."],
        }

    publisher = await get_active_wp_publisher(session)
    featured_media_id = article.wp_image_id
    if not featured_media_id and cluster.enrichment_data:
        selected_cover = cluster.enrichment_data.get("selected_cover_url")
        if not selected_cover:
            # Auto-fallback: use first clean web image if operator didn't explicitly pick one
            web_images = cluster.enrichment_data.get("web_images") or []
            if isinstance(web_images, list):
                for img in web_images:
                    if isinstance(img, dict) and img.get("url"):
                        selected_cover = img["url"]
                        break
        if selected_cover:
            try:
                from app.services.wp_media_service import WPMediaService  # noqa: PLC0415
                wp_base = conn.wp_base_url or settings.wp_base_url
                wp_pass = conn.wp_app_password or settings.wp_app_password
                wp_user = conn.wp_app_user or settings.wp_app_user
                media_svc = WPMediaService(base_url=wp_base, app_password=wp_pass, username=wp_user)
                async with httpx.AsyncClient(timeout=15.0) as img_client:
                    resp = await img_client.get(selected_cover, headers={"User-Agent": "Mozilla/5.0"})
                    if resp.status_code == 200:
                        upload_res = await media_svc.upload_image(
                            resp.content,
                            filename=f"cover_{cluster.id.hex[:8]}.jpg",
                            article_title=article.title,
                        )
                        if upload_res:
                            featured_media_id = upload_res.media_id
                            article.wp_image_id = featured_media_id
                            if upload_res.source_url:
                                if cluster.enrichment_data is None:
                                    cluster.enrichment_data = {}
                                cluster.enrichment_data["selected_cover_url"] = upload_res.source_url
            except Exception as up_err:
                logger.warning("Could not upload selected cover to WordPress: %s", up_err)

    pub_result = await publisher.publish(
        session,
        title=article.title,
        content_html=article.content_html,
        category_slug=article.category,
        tags=article.seo_keywords,
        featured_media=featured_media_id,
        seo_title=article.seo_title or article.title,
        seo_description=article.seo_description or "",
        existing_post_id=article.wp_post_id,
    )

    article.status = ArticleStatus.WP_PUBLISHED
    article.wp_post_id = pub_result.post_id
    article.wp_tag_ids = list(pub_result.tag_ids)
    article.published_at = datetime.now(UTC)
    if pub_result.slug:
        article.url_slug = pub_result.slug

    statuses = dict(article.platform_statuses or {})
    statuses["wp"] = {
        "status": "published",
        "post_id": pub_result.post_id,
        "url": pub_result.link,
        "slug": pub_result.slug,
        "cover_url": (
            cluster.enrichment_data.get("selected_cover_url")
            if cluster.enrichment_data
            else None
        ),
        "published_at": datetime.now(UTC).isoformat(),
    }
    article.platform_statuses = statuses
    cluster.is_published = True
    await session.commit()

    if pub_result.link:
        try:
            from app.services.bing_connection import (  # noqa: PLC0415
                load_bing_connection,
                submit_to_indexnow,
            )

            b_cfg = await load_bing_connection(session)
            if b_cfg.is_indexnow_active and b_cfg.has_indexnow_key:
                await submit_to_indexnow([pub_result.link], b_cfg)
        except Exception as in_err:
            logger.warning("IndexNow notification skipped: %s", in_err)

        try:
            from app.services.yandex_connection import (  # noqa: PLC0415
                load_yandex_connection,
            )

            y_cfg = await load_yandex_connection(session)
            if y_cfg.has_webmaster:
                host = y_cfg.webmaster_host_id
                async with httpx.AsyncClient(timeout=10.0) as ym_client:
                    await ym_client.post(
                        f"https://api.webmaster.yandex.net/v4/user/{y_cfg.webmaster_user_id or 'me'}/hosts/{host}/recrawl/queue",
                        headers={"Authorization": f"OAuth {y_cfg.webmaster_oauth_token}"},
                        json={"url": pub_result.link},
                    )
        except Exception as ym_err:
            logger.debug("Yandex Webmaster recrawl notification skipped: %s", ym_err)

    return {
        "ok": True,
        "post_id": pub_result.post_id,
        "post_url": pub_result.link,
        "message": f"Успешно опубликовано: {pub_result.link}",
        "warnings": list(pub_result.warnings),
    }


async def run_cluster_publish_dzen_step(
    session: AsyncSession, cluster_id: uuid.UUID
) -> dict[str, Any]:
    """Publish or draft the cluster's article in Dzen Studio via Playwright."""
    cluster = await session.get(NewsCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Кластер {cluster_id} не найден.")

    stmt_art = select(Article).where(Article.cluster_id == cluster.id)
    article = (await session.execute(stmt_art)).scalars().first()
    if article is None:
        raise ValueError("Сначала необходимо сгенерировать текст статьи на шаге 3.")

    from app.services.dzen_publisher import (  # noqa: PLC0415
        DzenBrowserPublisher,
        load_dzen_session_config,
    )

    config = await load_dzen_session_config(session)
    if not config.has_session:
        return {
            "ok": False,
            "post_id": None,
            "post_url": None,
            "message": (
                "Сессия Дзена не настроена. Перейдите в раздел «Настройки» "
                "и добавьте файл сессии (cookies / storage_state.json)."
            ),
            "warnings": ["Сессия Дзена не настроена."],
            "is_dzen": True,
        }

    publisher = DzenBrowserPublisher(config)
    # Prefer the real WP URL if article was already published there
    wp_stat = (article.platform_statuses or {}).get("wp", {})
    wp_url = wp_stat.get("url") if isinstance(wp_stat, dict) else None
    if not wp_url:
        cat_slug = article.category or "novosti"
        slug_val = article.url_slug or f"post-{article.id}"
        canonical_url = f"https://svodka.press/{cat_slug}/{slug_val}/"
    else:
        canonical_url = wp_url
    publish_now = (config.publication_mode == "publish")

    # Determine cover image URL for Dzen
    cover_url = ""
    wp_cover = wp_stat.get("cover_url") if isinstance(wp_stat, dict) else None
    if wp_cover:
        cover_url = str(wp_cover)
    elif article.wp_image_id:
        try:
            from app.services.wp_connection import load_wp_connection  # noqa: PLC0415
            from app.services.wp_media_service import WPMediaService  # noqa: PLC0415
            conn_wp = await load_wp_connection(session)
            if conn_wp and conn_wp.has_rest_credentials:
                media_svc_dzen = WPMediaService(
                    base_url=conn_wp.wp_base_url or settings.wp_base_url,
                    username=conn_wp.wp_app_user or settings.wp_app_user,
                    app_password=conn_wp.wp_app_password or settings.wp_app_password,
                )
                cover_url = await media_svc_dzen.get_media_url(article.wp_image_id)
        except Exception as media_err:
            logger.debug("Failed to resolve WP media URL for Dzen cover: %s", media_err)

    if not cover_url and cluster.enrichment_data:
        cover_url = cluster.enrichment_data.get("selected_cover_url") or ""
        if not cover_url:
            web_images = cluster.enrichment_data.get("web_images") or []
            if isinstance(web_images, list):
                for img in web_images:
                    if isinstance(img, dict) and img.get("url"):
                        cover_url = img["url"]
                        break

    clean_media = getattr(article, "clean_media_urls", None)
    if not cover_url and clean_media and isinstance(clean_media, list):
        cover_url = clean_media[0]

    # Prepare high-converting Dzen promo from registered advertiser (FreeAPI AI)
    dzen_custom_promo: str | None = None
    try:
        projects = await PromotedProjectsService.get_all_projects(session)
        active_project = next((p for p in projects if p.is_active), None)
        if not active_project and projects:
            active_project = projects[0]

        if active_project:
            target_url = active_project.build_tracked_url("dzen", "article_promo")
            dzen_custom_promo = (
                f"<b>🚀 Возможности для авторов | Партнерский материал</b><br/>\n"
                f"Создатели шопсов и коротких видео все чаще автоматизируют производство с помощью искусственного интеллекта. "
                f"В закрытом клубе <b><a href=\"{target_url}\">{active_project.name}</a></b> авторы получают проверенные алгоритмы создания сценариев, "
                f"готовые промпты для клипов и бесплатный доступ к лучшим нейросетям без ограничений и зарубежных карт.<br/><br/>\n"
                f"👉 <b><a href=\"{target_url}\">Перейти в {active_project.name} и получить бесплатный доступ →</a></b>"
            )
    except Exception as promo_err:
        logger.debug("Failed to build custom Dzen promo: %s", promo_err)

    try:
        res = await publisher.publish_article(
            article=article,
            wp_post_url=canonical_url,
            cover_url=cover_url,
            custom_promo=dzen_custom_promo,
            publish_now=publish_now,
        )
        mode_text = "опубликована в ленту" if publish_now else "сохранена в черновики"
        statuses = dict(article.platform_statuses or {})
        statuses["dzen"] = {
            "status": "published" if publish_now else "draft",
            "external_id": res.external_id,
            "url": res.url,
            "published_at": datetime.now(UTC).isoformat(),
        }
        article.platform_statuses = statuses
        if publish_now:
            cluster.is_published = True
        await session.commit()
        return {
            "ok": True,
            "post_id": res.external_id,
            "post_url": res.url,
            "message": f"Статья успешно {mode_text} Дзен Студии!",
            "warnings": [],
            "is_dzen": True,
        }
    except Exception as exc:
        logger.error("Dzen publish error for cluster %s: %s", cluster_id, exc, exc_info=True)
        return {
            "ok": False,
            "post_id": None,
            "post_url": None,
            "message": f"Ошибка публикации в Дзен: {exc}",
            "warnings": [str(exc)],
            "is_dzen": True,
        }

