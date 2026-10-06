# ruff: noqa: RUF001
"""Token-free research: what the archive already knows about a cluster.

For a news desk the useful research is not a web search but what the archive
holds: which facts the source posts agree on, and whether the site has covered
this story before. Both come from the database, so this module costs nothing and
cannot be rate-limited.

It exists as its own module because two callers need exactly the same answers —
the Лаборатория run and the zero-token inspection pass — and a second
implementation would eventually disagree with the first, leaving an operator
comparing two "research" reports that mean different things.

Related material is matched by title-word overlap rather than by embedding
similarity. That is a deliberate trade: the vector index would find more, but the
UI has to explain *why* two headlines are related, and "three significant words in
common" is a reason an operator can check. Recall matters less here than being
inspectable, because the output is a suggestion for interlinking rather than a
dedup verdict.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.cluster import NewsCluster
from app.models.raw_news import RawNews
from app.services.pipeline_stages import preview
from app.services.web_researcher import conduct_web_research

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "ARCHIVE_LOOKBACK",
    "MAX_INTERLINKS",
    "collect_facts",
    "conduct_cluster_research",
    "conduct_web_research",
    "find_related_articles",
    "title_keywords",
]

#: How far back the "have we covered this already" lookup goes. Wider than the
#: dedup window on purpose: dedup asks "is this the same post", the archive lookup
#: asks "have we written about this before", and the second stays useful for much
#: longer.
ARCHIVE_LOOKBACK: Final[timedelta] = timedelta(days=30)

#: Cap on interlink suggestions. Three is what fits a news body without turning
#: the text into a link farm.
MAX_INTERLINKS: Final[int] = 3

#: Articles read for the overlap comparison. Bounded because the query exists to
#: find a handful of candidates, not to scan the whole archive.
_ARCHIVE_SCAN_LIMIT: Final[int] = 200

#: Minimum shared significant words for two headlines to count as related. One
#: word matches on a city name alone, which would relate every Moscow story to
#: every other.
_MIN_OVERLAP: Final[int] = 2

#: Shortest word treated as significant. Four characters filters Russian
#: prepositions and conjunctions without a stop-word list, which would need
#: maintenance for no measurable gain here.
_MIN_WORD_LENGTH: Final[int] = 4

#: Stripped from a word before comparison, so «Пыхтино,» and «Пыхтино» match.
_PUNCTUATION: Final[str] = ".,:;!?«»\"'()"


def title_keywords(title: str) -> set[str]:
    """Significant words of a headline, lowercased and depunctuated."""
    words: set[str] = set()
    for raw in (title or "").split():
        word = raw.strip(_PUNCTUATION)
        if len(word) >= _MIN_WORD_LENGTH:
            words.add(word.lower())
    return words


def _format_money(val: str) -> str:
    """Format raw money token to human-readable string."""
    parts = val.split(":")
    amount = parts[0]
    cur = parts[1] if len(parts) > 1 else "RUB"
    cur_sym = "₽" if cur == "RUB" else ("$" if cur == "USD" else cur)
    try:
        num = float(amount)
        if num >= 1_000_000_000:
            return f"Сумма: {num / 1_000_000_000:.1f} млрд {cur_sym}"
        if num >= 1_000_000:
            return f"Сумма: {num / 1_000_000:.1f} млн {cur_sym}"
        return f"Сумма: {num:,.0f} {cur_sym}"
    except ValueError:
        return f"Сумма: {val}"


_DEDUP_PREFIXES: Final[tuple[str, ...]] = (
    "ent:", "when:", "date:", "time:", "money:", "pct:", "num:"
)


def format_fact_label(fact: str) -> str:
    """Format raw dedup fact tokens into readable Russian labels."""
    f = fact.strip()
    prefix, sep, val = f.partition(":")
    if not sep:
        return f

    val = val.strip()
    label_map = {
        "ent": f"Ключевой объект: {val.capitalize() if val else 'Не указан'}",
        "when": f"Временной маркер: {val.capitalize() if val else 'В тексте'}",
        "date": f"Дата: {val}",
        "time": f"Время: {val}",
        "pct": f"Показатель: {val}%",
    }
    if prefix in label_map:
        return label_map[prefix]
    if prefix == "num":
        try:
            return f"Количество: {int(val):,}".replace(",", " ")
        except ValueError:
            return f"Количество: {val}"
    if prefix == "money":
        return _format_money(val)
    return f


def collect_facts(
    cluster: NewsCluster, posts: Sequence[RawNews], *, limit: int = 6
) -> list[str]:
    """Return the facts the enrichment stage stored for this cluster.

    Falls back to the first sentence of each source post when enrichment has not
    run yet, and formats machine-extracted entities (ent:, when:, etc.) into
    human-readable statements for editorial inspection.
    """
    enrichment = cluster.enrichment_data or {}
    raw = enrichment.get("facts")
    stored: list[str] = []
    has_only_machine_tokens = True

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                formatted = format_fact_label(item)
                stored.append(formatted)
                if not any(item.startswith(prefix) for prefix in _DEDUP_PREFIXES):
                    has_only_machine_tokens = False

    # If we have only machine tokens or very few items, also pull key sentences from posts
    if has_only_machine_tokens or len(stored) < 2:
        for post in posts:
            text = (post.raw_text or "").strip()
            if not text:
                continue
            first_sent = preview(text.split(".")[0], 180)
            if first_sent and first_sent not in stored:
                stored.append(first_sent)
                if len(stored) >= limit:
                    break

    return stored[:limit]


async def find_related_articles(
    session: AsyncSession, cluster: NewsCluster, *, limit: int = 5
) -> list[dict[str, Any]]:
    """Find previously published articles on a similar topic.

    Answers the operator's question "have we written about this before". Returns
    rows sorted by overlap, richest first, each carrying the overlap count so the
    UI can show why the match was made.
    """
    words = title_keywords(cluster.title)
    if not words:
        return []

    window = datetime.now(UTC) - ARCHIVE_LOOKBACK
    result = await session.execute(
        select(Article)
        .where(Article.created_at >= window)
        .order_by(Article.created_at.desc())
        .limit(_ARCHIVE_SCAN_LIMIT)
    )

    scored: list[tuple[int, Article]] = []
    for article in result.scalars().all():
        overlap = len(words & title_keywords(article.title))
        if overlap >= _MIN_OVERLAP:
            scored.append((overlap, article))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    items = [
        {
            "article_id": article.id,
            "title": article.title,
            "url_slug": article.url_slug,
            "category": article.category,
            "overlap": overlap,
            "created_at": (
                article.created_at.strftime("%d.%m.%Y %H:%M")
                if article.created_at
                else ""
            ),
            "wp_post_id": article.wp_post_id,
        }
        for overlap, article in scored[:limit]
    ]

    if len(items) < limit:
        try:
            from app.services.wp_archive import WPArchiveService  # noqa: PLC0415
            wp_matches = await WPArchiveService.find_related(
                session, cluster.title or "", limit=limit - len(items)
            )
            for wp_m in wp_matches:
                if any(it["title"] == wp_m.get("title") for it in items):
                    continue
                items.append(
                    {
                        "article_id": wp_m.get("id"),
                        "title": wp_m.get("title", ""),
                        "url_slug": wp_m.get("slug", ""),
                        "category": "Архив сайта",
                        "overlap": 1,
                        "created_at": str(wp_m.get("date", ""))[:10],
                        "wp_post_id": wp_m.get("id"),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to find related WP archive articles: %s", exc)

    return items


async def conduct_cluster_research(
    session: AsyncSession,
    cluster: NewsCluster,
    posts: Sequence[RawNews],
    *,
    enable_web_search: bool = True,
) -> dict[str, Any]:
    """Execute complete research: local archive facts + external web search context."""
    facts = collect_facts(cluster, posts)
    related = await find_related_articles(session, cluster)

    web_report_data: dict[str, Any] = {
        "search_query": "",
        "sources": [],
        "background_facts": [],
        "is_success": True,
    }

    if enable_web_search:
        primary_text = posts[0].raw_text if posts else ""
        report = await conduct_web_research(
            cluster.title or "",
            fallback_text=primary_text or "",
            max_sources=3,
            session=session,
        )
        web_report_data = report.to_dict()

    return {
        "cluster_id": str(cluster.id),
        "facts": facts,
        "related_articles": related,
        "web_research": web_report_data,
        "web_sources": web_report_data.get("sources", []),
        "web_facts": web_report_data.get("background_facts", []),
        "web_images": web_report_data.get("images", []),
        "rejected_web_images": web_report_data.get("rejected_images", []),
        "search_query": web_report_data.get("search_query", ""),
    }

