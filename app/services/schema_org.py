# ruff: noqa: RUF001, E501
"""Schema.org JSON-LD structured data generator and Excalibur standards.

Implements structured data standards inspired by Horosheff/excalibur-blog-cloud-public:
1. Schema.org NewsArticle (for Google News, Yandex News, Discover).
2. Schema.org FAQPage (for search rich snippets).
3. Hook & Lead quality analysis (350-500 chars, no throat-clearing clichés).
4. Organic keyword injection validation.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, Final

logger: Final[logging.Logger] = logging.getLogger(__name__)

#: Minimum recommended lead hook length (characters)
MIN_LEAD_HOOK_CHARS: Final[int] = 300

#: Maximum recommended lead hook length (characters)
MAX_LEAD_HOOK_CHARS: Final[int] = 550

#: Clichés forbidden in high-impact news leads
FORBIDDEN_LEAD_CLICHES: Final[tuple[str, ...]] = (
    "как стало известно",
    "в последнее время",
    "ни для кого не секрет",
    "в современном мире",
    "стоит отметить",
    "важно подчеркнуть",
    "нельзя не заметить",
    "в данной статье",
    "мы расскажем",
    "поговорим о том",
)


def extract_faq_from_html(html: str) -> list[dict[str, str]]:
    """Extract Q&A pairs from generated HTML if an FAQ section exists."""
    faqs: list[dict[str, str]] = []
    # Match patterns like <h3>Вопрос...</h3><p>Ответ...</p> or <strong>Вопрос:</strong> Ответ
    pattern = re.compile(
        r"<h[34][^>]*>(?:Вопрос\s*\d*[:\.\s]*)?([^<]+)<\/h[34]>\s*<p[^>]*>([^<]+)<\/p>",
        re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        q = match.group(1).strip()
        a = match.group(2).strip()
        if len(q) > 10 and len(a) > 20 and not any(k in q.lower() for k in ("читайте", "источник", "теги")):
            faqs.append({"question": q, "answer": a})

    return faqs[:4]


def generate_schema_org_json_ld(
    *,
    headline: str,
    description: str,
    url: str,
    published_at: datetime | None = None,
    modified_at: datetime | None = None,
    image_url: str | None = None,
    keywords: list[str] | None = None,
    faq_items: list[dict[str, str]] | None = None,
    publisher_name: str = "SvodkaPress",
    publisher_url: str = "https://svodkapress.ru",
) -> dict[str, Any]:
    """Generate valid Schema.org graph containing NewsArticle and optional FAQPage."""
    now_iso = datetime.now(UTC).isoformat()
    pub_iso = published_at.isoformat() if published_at else now_iso
    mod_iso = modified_at.isoformat() if modified_at else pub_iso

    graph: list[dict[str, Any]] = [
        {
            "@type": "NewsArticle",
            "@id": f"{url}#article",
            "isPartOf": {
                "@type": "WebPage",
                "@id": url,
            },
            "headline": headline[:110],
            "description": description[:200],
            "datePublished": pub_iso,
            "dateModified": mod_iso,
            "mainEntityOfPage": {
                "@type": "WebPage",
                "@id": url,
            },
            "author": {
                "@type": "Organization",
                "name": publisher_name,
                "url": publisher_url,
            },
            "publisher": {
                "@type": "Organization",
                "name": publisher_name,
                "url": publisher_url,
                "logo": {
                    "@type": "ImageObject",
                    "url": f"{publisher_url}/logo.png",
                },
            },
        }
    ]

    if image_url:
        graph[0]["image"] = [image_url]

    if keywords:
        graph[0]["keywords"] = ", ".join(keywords[:6])

    # If FAQ items are present, add FAQPage
    if faq_items:
        faq_entities = [
            {
                "@type": "Question",
                "name": item["question"],
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": item["answer"],
                },
            }
            for item in faq_items
            if item.get("question") and item.get("answer")
        ]
        if faq_entities:
            graph.append(
                {
                    "@type": "FAQPage",
                    "@id": f"{url}#faq",
                    "mainEntity": faq_entities,
                }
            )

    return {
        "@context": "https://schema.org",
        "@graph": graph,
    }


def evaluate_lead_hook(first_paragraph_html: str) -> dict[str, Any]:
    """Analyze the opening lead paragraph against Excalibur news standards."""
    clean_lead = re.sub(r"<[^>]+>", "", first_paragraph_html).strip()
    length = len(clean_lead)

    found_cliches = [
        cliche for cliche in FORBIDDEN_LEAD_CLICHES if cliche in clean_lead.lower()
    ]

    is_optimal_length = MIN_LEAD_HOOK_CHARS <= length <= MAX_LEAD_HOOK_CHARS
    has_cliches = len(found_cliches) > 0

    score = 100
    suggestions: list[str] = []

    if length < MIN_LEAD_HOOK_CHARS:
        score -= 25
        suggestions.append(f"Лид коротковат ({length} зн.). Рекомендуется 350-500 знаков с ключевыми фактами.")
    elif length > MAX_LEAD_HOOK_CHARS:
        score -= 15
        suggestions.append(f"Лид затянут ({length} зн.). Разбейте на два абзаца.")

    if has_cliches:
        score -= 30
        suggestions.append(f"Обнаружены вводные клише: {', '.join(found_cliches)}. Начинайте сразу с действия.")

    # Check for numbers / concrete facts
    has_digits = bool(re.search(r"\d", clean_lead))
    if not has_digits:
        score -= 10
        suggestions.append("В лиде нет ни одной цифры, даты или конкретного показателя.")

    return {
        "length": length,
        "is_optimal": is_optimal_length and not has_cliches,
        "score": max(0, score),
        "cliches": found_cliches,
        "suggestions": suggestions,
    }
