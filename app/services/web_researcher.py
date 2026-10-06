# ruff: noqa: RUF001, PLR0912, PLR0915, E501
"""Web researcher service: search, fetch, and extract context from the internet.

Provides:
- SSRF-safe URL validation and HTML fetching
- Article extraction via Trafilatura
- DuckDuckGo news querying with query cleaning
- Factual background extraction for editorial enrichment
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urljoin, urlparse

import httpx
import lxml.html
import trafilatura
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.image_cleaner import filter_and_deduplicate_web_images
from app.services.web_search import DuckDuckGoSearchError, SearchResult, search_duckduckgo

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "WebArticleContent",
    "WebResearchReport",
    "WebSourceItem",
    "classify_web_image",
    "conduct_web_research",
    "evaluate_article_relevance",
    "extract_search_query",
    "fetch_clean_article",
    "is_safe_web_url",
]

MAX_ARTICLE_BYTES: Final[int] = 1_048_576  # 1 MB limit
DEFAULT_FETCH_TIMEOUT: Final[float] = 6.0
DEFAULT_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Social networks, aggregators and channel-index sites to skip in favour of news publishers
_DISALLOWED_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "t.me",
        "telegram.org",
        "vk.com",
        "ok.ru",
        "twitter.com",
        "x.com",
        "facebook.com",
        "instagram.com",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "pinterest.com",
        "reddit.com",
        # Telegram channel aggregators / index sites
        "tvgram.ru",
        "tgstat.ru",
        "tgstat.com",
        "telemetr.io",
        "combot.org",
        "tg.guru",
        "tgdb.ru",
        "push.ru",
        "teleg.run",
    }
)


_STOP_WORDS_RU_EN: Final[frozenset[str]] = frozenset(
    {
        "это", "как", "так", "что", "или", "если", "для", "при", "про", "без",
        "под", "над", "перед", "после", "года", "году", "годах", "было", "были",
        "будет", "будут", "стал", "стали", "стало", "очень", "также", "только",
        "всего", "всегда", "этом", "этого", "этому", "этой", "этих", "своих",
        "своей", "своего", "своем", "который", "которая", "которые", "которого",
        "сообщил", "заявил", "отметил", "сказал", "передает", "пишет",
        "about", "with", "from", "that", "this", "have", "will", "been", "were",
        "their", "there", "what", "which", "more", "when", "then", "into",
    }
)

_CRITICAL_EVENT_STEMS: Final[frozenset[str]] = frozenset(
    {
        "пожар", "возгоран", "огон", "горит", "плам", "погиб", "жертв", "пострад",
        "взрыв", "дрон", "бпла", "атак", "обстрел", "ранен", "удар",
        "дтп", "авари", "столкнов", "крушен",
        "суд", "задерж", "арест", "обыск", "уголовн", "следств", "приговор",
        "убийств", "убил", "нападен", "хищен", "краж", "мошеннич",
    }
)


def _stem(word: str) -> str:
    """Return lowercase normalized stem of word for robust Russian/English matching."""
    w = word.lower().strip(" .-_,;:!?")
    if len(w) > 4:
        return re.sub(
            r"(?:ая|ое|ые|ие|ый|ий|ой|ем|ом|ам|ами|ям|ями|ов|ев|ей|ях|ах|у|ю|а|я|е|и|ы|ь)$",
            "",
            w,
        )
    return w


def _extract_entities_and_terms(text: str) -> tuple[set[str], set[str]]:
    """Extract named entities/technical identifiers and core topical keywords.

    Returns:
        (entities, keywords)
    """
    if not text:
        return set(), set()

    entities: set[str] = set()
    keywords: set[str] = set()

    raw_tokens = re.findall(r"[A-Za-zА-Яа-я0-9]+(?:[.\-_][A-Za-zА-Яа-я0-9]+)*", text)

    for tok in raw_tokens:
        tok_clean = tok.strip(" .-_")
        if not tok_clean or len(tok_clean) < 2:
            continue
        tok_lower = tok_clean.lower()
        if tok_lower in _STOP_WORDS_RU_EN:
            continue

        is_entity = (
            any(char.isdigit() for char in tok_clean)
            or "." in tok_clean
            or "-" in tok_clean
            or (tok_clean[0].isupper() and len(tok_clean) >= 3)
            or tok_clean.isupper()
        )
        if is_entity:
            entities.add(tok_lower)
        elif len(tok_lower) >= 3:
            keywords.add(tok_lower)

    return entities, keywords


def evaluate_article_relevance(
    news_title: str,
    news_text: str,
    candidate_title: str,
    candidate_snippet: str,
    candidate_text: str = "",
) -> tuple[float, str, bool]:
    """Analyze whether a candidate web search article matches the news story.

    Returns:
        (relevance_score, verdict_label, is_accepted)
    """
    story_entities, story_keywords = _extract_entities_and_terms(
        f"{news_title} {news_text[:500]}"
    )
    cand_entities, cand_keywords = _extract_entities_and_terms(
        f"{candidate_title} {candidate_snippet} {candidate_text[:1200]}"
    )

    if not story_entities and not story_keywords:
        return 1.0, "В тему", True

    cand_all = cand_entities | cand_keywords
    cand_stems = {_stem(w) for w in cand_all}

    # 1. Entity overlap (with stemming fallback)
    entity_score = 1.0
    if story_entities:
        matched_entities = {
            e for e in story_entities
            if e in cand_all or _stem(e) in cand_stems
        }
        entity_score = len(matched_entities) / len(story_entities)

    # 2. Keyword overlap (stem-based)
    keyword_score = 0.0
    if story_keywords:
        matched_keywords = {
            k for k in story_keywords
            if k in cand_all or _stem(k) in cand_stems
        }
        keyword_score = len(matched_keywords) / min(len(story_keywords), 8)

    # 3. Title-to-title lexical overlap with stems
    st_title_stems = {
        _stem(w)
        for w in re.findall(r"[A-Za-zА-Яа-я0-9]+(?:[.\-_][A-Za-zА-Яа-я0-9]+)*", news_title)
        if len(w) >= 3 and w.lower() not in _STOP_WORDS_RU_EN
    }
    cand_title_stems = {
        _stem(w)
        for w in re.findall(r"[A-Za-zА-Яа-я0-9]+(?:[.\-_][A-Za-zА-Яа-я0-9]+)*", candidate_title)
        if len(w) >= 3 and w.lower() not in _STOP_WORDS_RU_EN
    }
    title_overlap = len(st_title_stems & cand_title_stems)
    title_score = title_overlap / max(min(len(st_title_stems), 5), 1)

    # Weighted calculation
    if story_entities:
        composite = 0.50 * entity_score + 0.30 * title_score + 0.20 * keyword_score
    else:
        composite = 0.60 * title_score + 0.40 * keyword_score

    score = min(max(composite, 0.0), 1.0)

    # Critical rejection: if story has clear strong entities (e.g. Z.ai, GLM-5.3),
    # but candidate matches none of them and has poor title match, reject immediately.
    if story_entities and entity_score == 0 and title_score < 0.25:
        return round(score, 2), "Не по теме", False

    # Event mismatch guard: if the story has an explicit incident/event marker,
    # the candidate MUST also contain an incident marker rather than off-topic news.
    story_events = {
        s for s in st_title_stems
        if any(s.startswith(ev) or ev.startswith(s) for ev in _CRITICAL_EVENT_STEMS)
    }
    if story_events:
        cand_events = {
            s for s in (cand_title_stems | cand_stems)
            if any(s.startswith(ev) or ev.startswith(s) for ev in _CRITICAL_EVENT_STEMS)
        }
        if not cand_events:
            return round(score, 2), "Не по теме (событие не совпадает)", False

    if score < 0.32:
        return round(score, 2), "Не по теме", False
    if score >= 0.55:
        return round(score, 2), "Высокая релевантность", True
    return round(score, 2), "Косвенный контекст", True


def classify_web_image(url: str, title: str = "", domain: str = "") -> tuple[str, str]:
    """Classify an external web image into kind ('photo', 'infographic', 'logo', 'graphic') and badge label."""
    url_lower = url.lower()
    title_lower = title.lower()

    path = urlparse(url_lower).path
    if path.endswith((".svg", ".ico")):
        return "logo", "Логотип"

    if any(
        m in url_lower or m in title_lower
        for m in (
            "chart", "diagram", "grafik", "график", "диаграмм", "таблиц",
            "table", "benchmark", "бенчмарк", "score", "comparison", "rating",
        )
    ):
        return "infographic", "Инфографика"

    if any(
        m in url_lower or m in title_lower
        for m in (
            "anchor", "presenter", "vedusch", "vedushaya", "diktora", "diktor",
            "futbol", "football", "match", "soccer", "hokkey", "hockey",
            "recipe", "recept", "eda", "kulinari", "syrniki",
        )
    ):
        return "offtopic", "Оффтопик"

    if any(
        m in url_lower or m in title_lower
        for m in (
            "logo", "avatar", "логотип", "favicon", "icon", "badge",
            "emblem", "brand", "brandmark", "watermark",
        )
    ):
        return "logo", "Логотип"

    if any(
        m in url_lower
        for m in (
            "banner", "баннер", "promo", "advert", "placeholder",
            "sharing", "share", "og_image", "og-image", "social_share",
            "preview_share", "splash",
        )
    ):
        return "graphic", "Баннер"

    return "photo", "Фоторепортаж"


def _is_disallowed_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Return True if IP belongs to private, loopback, or link-local ranges."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _is_safe_hostname(hostname: str) -> bool:
    """Return False if hostname is localhost, local domain, or resolves to private IP."""
    if not hostname or hostname in {"localhost", "loopback"}:
        return False
    if hostname.endswith((".local", ".internal", ".lan", ".home", ".corp")):
        return False
    try:
        ip = ipaddress.ip_address(hostname)
        return not _is_disallowed_ip(ip)
    except ValueError:
        pass
    try:
        addr_info = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
        return not any(
            _is_disallowed_ip(ipaddress.ip_address(sockaddr[0]))
            for _, _, _, _, sockaddr in addr_info
        )
    except (socket.gaierror, OSError):
        return False


def is_safe_web_url(url: str) -> bool:
    """Validate that a URL is safe to fetch and does not target private or local networks.

    Guards against Server-Side Request Forgery (SSRF) by checking schemes,
    hostnames, and resolved IP addresses against private and loopback ranges.
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False

    if parsed.scheme.lower() not in {"http", "https"}:
        return False

    hostname = (parsed.hostname or "").strip().lower()
    return _is_safe_hostname(hostname)


@dataclass(frozen=True, slots=True)
class WebArticleContent:
    """Cleaned article content extracted from an external web page."""

    url: str
    domain: str
    title: str
    clean_text: str
    char_count: int
    image_url: str | None = None
    image_kind: str = "photo"
    images: tuple[str, ...] = ()


@dataclass(slots=True)
class WebSourceItem:
    """Item representing an external source found during research."""

    title: str
    url: str
    domain: str
    snippet: str
    extracted_text: str = ""
    key_facts: list[str] = field(default_factory=list)
    image_url: str | None = None
    image_kind: str = "photo"
    image_badge: str = "Фоторепортаж"
    relevance_score: float = 1.0
    relevance_verdict: str = "В тему"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "domain": self.domain,
            "snippet": self.snippet,
            "key_facts": self.key_facts,
            "image_url": self.image_url,
            "image_kind": self.image_kind,
            "image_badge": self.image_badge,
            "relevance_score": round(self.relevance_score, 2),
            "relevance_verdict": self.relevance_verdict,
        }


@dataclass(slots=True)
class WebResearchReport:
    """Consolidated report produced by external web research."""

    search_query: str
    sources: list[WebSourceItem]
    background_facts: list[str]
    images: list[dict[str, Any]] = field(default_factory=list)
    rejected_images: list[dict[str, Any]] = field(default_factory=list)
    is_success: bool = True
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "search_query": self.search_query,
            "sources": [s.to_dict() for s in self.sources],
            "background_facts": self.background_facts,
            "images": self.images,
            "rejected_images": self.rejected_images,
            "is_success": self.is_success,
            "error_message": self.error_message,
        }


async def fetch_clean_article(
    url: str,
    *,
    request_timeout: float = DEFAULT_FETCH_TIMEOUT,
    client: httpx.AsyncClient | None = None,
) -> WebArticleContent | None:
    """Fetch an external web page safely and extract its clean text using Trafilatura."""
    if not is_safe_web_url(url):
        logger.warning("fetch_clean_article rejected unsafe URL: %s", url)
        return None

    domain = urlparse(url).netloc.lower()
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=request_timeout, follow_redirects=True)

    try:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            logger.debug("Web page %s returned status %d", url, response.status_code)
            return None

        # Guard against oversized payloads
        content_bytes = response.content[:MAX_ARTICLE_BYTES]
        encoding = response.encoding or "utf-8"
        try:
            html_text = content_bytes.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            html_text = content_bytes.decode("utf-8", errors="replace")

        # Extract primary textual content using Trafilatura
        extracted = trafilatura.extract(
            html_text,
            url=url,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
        if not extracted or len(extracted.strip()) < 100:
            return None

        # Extract title from HTML metadata or fallback
        title_match = re.search(
            r"<title[^>]*>([^<]+)</title>", html_text, re.IGNORECASE
        )
        title = title_match.group(1).strip() if title_match else domain

        # Extract selective candidate editorial images from HTML
        candidate_images: list[str] = []
        try:
            tree = lxml.html.fromstring(html_text)
            meta_nodes = tree.xpath(
                '//meta[@property="og:image"]/@content | '
                '//meta[@name="twitter:image"]/@content | '
                '//meta[@name="twitter:image:src"]/@content | '
                '//link[@rel="image_src"]/@href'
            )
            figure_nodes = tree.xpath(
                '//article//figure//img/@src | '
                '//article//figure//img/@data-src | '
                '//main//figure//img/@src | '
                '//main//figure//img/@data-src | '
                '//div[contains(@class, "article__body") or contains(@class, "article-body") or contains(@class, "post-content") or contains(@class, "entry-content")]//img/@src | '
                '//div[contains(@class, "article__body") or contains(@class, "article-body") or contains(@class, "post-content") or contains(@class, "entry-content")]//img/@data-src | '
                '//article//img/@src | '
                '//article//img/@data-src | '
                '//article//figure//img/@srcset | '
                '//main//figure//img/@srcset'
            )
            raw_cands = list(meta_nodes) + list(figure_nodes)
            for cand in raw_cands:
                cand_str = str(cand).strip()
                if not cand_str:
                    continue
                # If srcset format like 'url1 400w, url2 800w', pick the largest/last URL
                if "," in cand_str and (" " in cand_str or "w" in cand_str or "x" in cand_str):
                    parts = [p.strip().split()[0] for p in cand_str.split(",") if p.strip()]
                    cand_str = parts[-1] if parts else cand_str
                resolved_img = urljoin(str(response.url), cand_str)
                if not is_safe_web_url(resolved_img):
                    continue

                r_lower = resolved_img.lower()
                if any(
                    h in r_lower
                    for h in (
                        "avatar", "author", "logo", "banner", "icon",
                        "share", "sharing", "social", "pixel", "advert", "rekla",
                        "vedusch", "anchor", "presenter", "diktora", "og_image", "og-image",
                    )
                ):
                    continue

                if resolved_img not in candidate_images:
                    candidate_images.append(resolved_img)
                if len(candidate_images) >= 3:
                    break
        except Exception as img_err:  # noqa: BLE001
            logger.debug("Failed to extract images from %s: %s", url, img_err)

        lead_image_url: str | None = None
        lead_image_kind: str = "photo"
        valid_photos: list[str] = []
        if candidate_images:
            for u in candidate_images:
                k, _ = classify_web_image(u, title, domain)
                if k == "photo" and u not in valid_photos:
                    valid_photos.append(u)

            if valid_photos:
                lead_image_url = valid_photos[0]
                lead_image_kind = "photo"
            else:
                lead_image_url = candidate_images[0]
                lead_image_kind, _ = classify_web_image(lead_image_url, title, domain)

        clean_text = extracted.strip()
        return WebArticleContent(
            url=str(response.url),
            domain=domain,
            title=title,
            clean_text=clean_text,
            char_count=len(clean_text),
            image_url=lead_image_url,
            image_kind=lead_image_kind,
            images=tuple(valid_photos[:3]),
        )
    except (TimeoutError, httpx.RequestError) as exc:
        logger.debug("Failed to fetch %s: %s", url, exc)
        return None
    finally:
        if close_client:
            await client.aclose()




def extract_search_query(title: str, fallback_text: str = "") -> str:
    """Derive an effective, concise search query from a news title or lead text."""
    raw = (title or "").strip()
    if not raw or len(raw) < 10:
        raw = (fallback_text or "").strip()

    # Strip all Unicode emojis and decorative symbols first
    raw = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]", " ", raw).strip()

    # Repeatedly strip leading tags, punctuation, and breaking prefixes
    prefix_re = re.compile(
        r"^(?:breaking|срочно|молния|важно|внимание|эксклюзив)[:!\s\-]*",
        flags=re.IGNORECASE,
    )
    while prefix_re.search(raw):
        raw = prefix_re.sub("", raw).strip().lstrip(" :!?-")

    # Split only on genuine sentence boundaries (dot/exclamation/question
    # followed by space, or newline). Crucially never split on internal
    # dots like Z.ai, GLM-5.3, 3.5, or domain names.
    sentences = re.split(r"(?<=[.!?])\s+(?=[А-ЯA-Z0-9«\"])|\n+", raw)
    first_sentence = sentences[0].strip() if sentences else raw

    # Normalize punctuation while preserving dots and hyphens inside words (e.g. Z.ai)
    cleaned_tokens: list[str] = []
    for token in first_sentence.split():
        t = token.strip(" ,;:!?'\"()[]{}«»<>`~*#&—–-").rstrip(".")
        if not t:
            continue
        cleaned_tokens.append(t)

    # Use up to top 8 words for search query precision and relevance
    selected = cleaned_tokens[:8] if len(cleaned_tokens) > 8 else cleaned_tokens
    query = " ".join(selected).strip()
    return query or first_sentence[:60].strip()



def _heuristic_extract_facts(text: str, max_facts: int = 4) -> list[str]:
    """Extract factual sentences without spending LLM tokens."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    scored: list[tuple[int, str]] = []

    news_markers = (
        "сообщил",
        "заявил",
        "отметил",
        "ранее",
        "напомним",
        "по данным",
    )

    for s in sentences:
        s_clean = s.strip()
        if len(s_clean) < 30 or len(s_clean) > 250:
            continue
        # Score higher if contains digits (stats, dates, amounts)
        score = 0
        if re.search(r"\d+", s_clean):
            score += 2
        if re.search(r"[«»\"']", s_clean):
            score += 1
        if any(marker in s_clean.lower() for marker in news_markers):
            score += 2
        if score > 0:
            scored.append((score, s_clean))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [fact for _, fact in scored[:max_facts]]



def _filter_candidates(
    search_results: list[SearchResult], max_sources: int
) -> list[SearchResult]:
    """Filter out social media, non-news domains, and duplicate URLs."""
    candidates: list[SearchResult] = []
    seen_urls: set[str] = set()
    for res in search_results:
        clean_url = res.url.split("#")[0].rstrip("/")
        if clean_url in seen_urls:
            continue
        domain = urlparse(res.url).netloc.lower()
        if any(domain == d or domain.endswith("." + d) for d in _DISALLOWED_DOMAINS):
            continue
        seen_urls.add(clean_url)
        candidates.append(res)
        if len(candidates) >= max_sources:
            break
    return candidates


async def conduct_web_research(
    query_or_title: str,
    *,
    fallback_text: str = "",
    max_sources: int = 3,
    session: AsyncSession | None = None,
) -> WebResearchReport:
    """Execute comprehensive external web research for a news story with topic relevance check."""
    search_query = extract_search_query(query_or_title, fallback_text)
    if not search_query:
        return WebResearchReport(
            search_query="",
            sources=[],
            background_facts=[],
            is_success=False,
            error_message="Пустой поисковый запрос",
        )

    try:
        search_results: list[SearchResult] = await search_duckduckgo(
            search_query, max_results=10
        )
    except DuckDuckGoSearchError as exc:
        logger.warning("DuckDuckGo search error for %r: %s", search_query, exc)
        return WebResearchReport(
            search_query=search_query,
            sources=[],
            background_facts=[],
            is_success=False,
            error_message=f"Ошибка поиска: {exc}",
        )

    # Allow scanning more candidates so relevance filtering picks best matching articles
    candidate_limit = max_sources * 2
    candidates = _filter_candidates(search_results, candidate_limit)

    if not candidates:
        logger.info("Web research: no suitable news sources found for %r", search_query)
        return WebResearchReport(
            search_query=search_query,
            sources=[],
            background_facts=[],
            is_success=True,
            error_message="Внешние публикации по теме не найдены",
        )

    # Concurrently fetch candidate articles
    async with httpx.AsyncClient(
        timeout=DEFAULT_FETCH_TIMEOUT, follow_redirects=True
    ) as client:
        fetch_tasks = [
            fetch_clean_article(c.url, client=client) for c in candidates
        ]
        articles = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    items: list[WebSourceItem] = []
    all_facts: list[str] = []
    all_images: list[dict[str, str]] = []

    for candidate, article_res in zip(candidates, articles, strict=False):
        domain = urlparse(candidate.url).netloc.lower()
        extracted_text = ""
        lead_img = None
        img_kind = "photo"
        cand_title = candidate.title

        if isinstance(article_res, WebArticleContent):
            extracted_text = article_res.clean_text
            cand_title = candidate.title or article_res.title
            lead_img = article_res.image_url
            img_kind = article_res.image_kind

        # Primary topic relevance analysis
        rel_score, rel_verdict, is_accepted = evaluate_article_relevance(
            query_or_title,
            fallback_text,
            cand_title,
            candidate.snippet,
            extracted_text,
        )

        if not is_accepted:
            logger.info(
                "Web research rejected off-topic article (%s, score=%.2f): %r",
                rel_verdict,
                rel_score,
                cand_title,
            )
            continue

        facts = _heuristic_extract_facts(extracted_text) if extracted_text else (
            [candidate.snippet] if candidate.snippet else []
        )
        _, badge_label = classify_web_image(lead_img or "", cand_title, domain)

        item = WebSourceItem(
            title=cand_title,
            url=candidate.url,
            domain=domain,
            snippet=candidate.snippet,
            extracted_text=extracted_text[:600] if extracted_text else candidate.snippet,
            key_facts=facts,
            image_url=lead_img,
            image_kind=img_kind,
            image_badge=badge_label,
            relevance_score=rel_score,
            relevance_verdict=rel_verdict,
        )
        items.append(item)
        all_facts.extend(facts)

        cand_photos = (
            list(article_res.images)
            if isinstance(article_res, WebArticleContent) and article_res.images
            else ([lead_img] if lead_img else [])
        )
        for p_url in cand_photos:
            if p_url and not any(it.get("url") == p_url for it in all_images):
                p_kind, p_badge = classify_web_image(p_url, cand_title, domain)
                all_images.append({
                    "url": p_url,
                    "domain": domain,
                    "source_url": candidate.url,
                    "title": cand_title,
                    "kind": p_kind,
                    "badge_label": p_badge,
                })

    # Sort sources by relevance descending and truncate to requested max_sources
    items.sort(key=lambda s: s.relevance_score, reverse=True)
    selected_items = items[:max_sources]

    # Deduplicate facts preserving order
    unique_facts: list[str] = []
    seen_facts: set[str] = set()
    for f in all_facts:
        key = f.strip().lower()
        if key not in seen_facts:
            seen_facts.add(key)
            unique_facts.append(f.strip())

    # Clean and deduplicate web images (resolution, watermark check, perceptual dHash + AI relevance)
    rejected_images: list[dict[str, Any]] = []
    try:
        clean_images, rejected_images = await filter_and_deduplicate_web_images(
            all_images,
            story_title=query_or_title,
            story_text=fallback_text,
            session=session,
            return_rejected=True,
        )
    except (RuntimeError, ValueError, httpx.HTTPError, OSError) as exc:
        logger.warning("Failed to filter web images: %s", exc)
        clean_images = all_images

    err_msg = None
    if not selected_items and candidates:
        err_msg = "Найденные внешние статьи не соответствуют теме сюжета"

    return WebResearchReport(
        search_query=search_query,
        sources=selected_items,
        background_facts=unique_facts[:6],
        images=clean_images,
        rejected_images=rejected_images,
        is_success=True,
        error_message=err_msg,
    )

