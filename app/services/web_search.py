"""Multi-engine web search for research stage (free, no API key required).

Combines fast Russian news search endpoints (RIA Novosti, Lenta.ru JSON API,
Bing News RSS) with DuckDuckGo HTML fallback so datacenter IP blocks on any
single search provider never leave the research pipeline empty.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from lxml import etree
from lxml import html as lxml_html

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "DuckDuckGoSearchError",
    "SearchResult",
    "search_duckduckgo",
]

DUCKDUCKGO_SEARCH_URL: Final[str] = "https://html.duckduckgo.com/html/"
LENTA_SEARCH_URL: Final[str] = "https://lenta.ru/search/v2/process"
RIA_SEARCH_URL: Final[str] = "https://ria.ru/services/search/getmore/"
BING_NEWS_RSS_URL: Final[str] = "https://www.bing.com/news/search"

USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
MAX_RESULTS: Final[int] = 10
REQUEST_TIMEOUT: Final[float] = 7.0
DDG_TIMEOUT: Final[float] = 3.0


class DuckDuckGoSearchError(Exception):
    """Web search failed across all configured providers."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One web search result."""

    title: str
    url: str
    snippet: str
    position: int


async def _search_lenta(
    client: httpx.AsyncClient, query: str, max_items: int = 5
) -> list[SearchResult]:
    """Query Lenta.ru JSON search API for relevant news articles."""
    try:
        resp = await client.get(
            LENTA_SEARCH_URL,
            params={"query": query, "size": max_items, "sort": 2},
            timeout=5.0,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        matches = data.get("matches") or []
        results: list[SearchResult] = []
        for item in matches:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("rightcol") or item.get("text") or "").strip()
            if url.startswith(("http://", "https://")) and title:
                results.append(
                    SearchResult(
                        title=title,
                        url=url,
                        snippet=snippet[:300],
                        position=len(results) + 1,
                    )
                )
        return results
    except Exception as exc:  # noqa: BLE001
        logger.debug("Lenta search failed for %r: %s", query, exc)
        return []


async def _search_ria(
    client: httpx.AsyncClient, query: str, max_items: int = 5
) -> list[SearchResult]:
    """Query RIA Novosti search endpoint and parse article links."""
    try:
        resp = await client.get(
            RIA_SEARCH_URL,
            params={"query": query},
            timeout=5.0,
        )
        if resp.status_code != 200 or not resp.text.strip():
            return []
        tree = lxml_html.fromstring(resp.text)
        items = tree.xpath('//div[contains(@class, "list-item")]')
        results: list[SearchResult] = []
        for item in items:
            if len(results) >= max_items:
                break
            a_els = item.xpath('.//a[contains(@class, "list-item__title")]')
            if not a_els:
                continue
            title = a_els[0].text_content().strip()
            href = a_els[0].get("href", "").strip()
            if not href or not title:
                continue
            url = urljoin("https://ria.ru", href)
            snip_els = item.xpath('.//div[contains(@class, "list-item__content")]')
            snippet = snip_els[0].text_content().strip() if snip_els else title
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet[:300],
                    position=len(results) + 1,
                )
            )
        return results
    except Exception as exc:  # noqa: BLE001
        logger.debug("RIA search failed for %r: %s", query, exc)
        return []


async def _search_bing_news_rss(
    client: httpx.AsyncClient, query: str, max_items: int = 5
) -> list[SearchResult]:
    """Query Bing News RSS feed and unwrap target publisher URLs."""
    try:
        resp = await client.get(
            BING_NEWS_RSS_URL,
            params={"q": query, "format": "rss", "cc": "RU", "setlang": "ru"},
            timeout=5.0,
        )
        if resp.status_code != 200 or not resp.content:
            return []
        root = etree.fromstring(resp.content)
        results: list[SearchResult] = []
        for item in root.xpath("//item"):
            if len(results) >= max_items:
                break
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if "apiclick.aspx" in link:
                qs = parse_qs(urlparse(link).query)
                link = qs.get("url", [link])[0]
            desc = (item.findtext("description") or "").strip()
            if title and link.startswith(("http://", "https://")):
                results.append(
                    SearchResult(
                        title=title,
                        url=link,
                        snippet=desc[:300],
                        position=len(results) + 1,
                    )
                )
        return results
    except Exception as exc:  # noqa: BLE001
        logger.debug("Bing News RSS search failed for %r: %s", query, exc)
        return []


async def _search_ddg_direct(
    client: httpx.AsyncClient,
    query: str,
    region: str,
    max_results: int,
) -> list[SearchResult]:
    """Attempt DuckDuckGo HTML search with fast timeout."""
    try:
        response = await client.post(
            DUCKDUCKGO_SEARCH_URL,
            data={"q": query, "kl": region, "max_results": min(max_results, 20)},
            timeout=DDG_TIMEOUT,
        )
        if response.status_code == 200:
            return _parse_results(response.text, max_results)
    except Exception as exc:  # noqa: BLE001
        logger.debug("DuckDuckGo direct search unavailable for %r: %s", query, exc)
    return []


async def search_duckduckgo(
    query: str,
    *,
    max_results: int = MAX_RESULTS,
    region: str = "ru-ru",
    client: httpx.AsyncClient | None = None,
) -> list[SearchResult]:
    """Search news engines concurrently and return deduplicated results."""
    clean_q = query.strip()
    if not clean_q:
        return []

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers=headers,
            follow_redirects=True,
        )

    try:
        batches = await asyncio.gather(
            _search_ria(client, clean_q, max_items=5),
            _search_lenta(client, clean_q, max_items=5),
            _search_bing_news_rss(client, clean_q, max_items=5),
            _search_ddg_direct(client, clean_q, region, max_results),
            return_exceptions=True,
        )

        combined: list[SearchResult] = []
        seen_urls: set[str] = set()
        for batch in batches:
            if not isinstance(batch, list):
                continue
            for item in batch:
                norm_url = item.url.split("#")[0].rstrip("/")
                if norm_url in seen_urls:
                    continue
                seen_urls.add(norm_url)
                combined.append(
                    SearchResult(
                        title=item.title,
                        url=item.url,
                        snippet=item.snippet,
                        position=len(combined) + 1,
                    )
                )
                if len(combined) >= max_results:
                    break
            if len(combined) >= max_results:
                break

        logger.info(
            "Multi-engine search for %r returned %d result(s)",
            clean_q[:80],
            len(combined),
        )
        return combined
    except httpx.RequestError as error:
        raise DuckDuckGoSearchError(f"Web search network error: {error}") from error
    finally:
        if close_client:
            await client.aclose()


def _parse_results(html: str, max_results: int) -> list[SearchResult]:
    """Parse DuckDuckGo HTML response into SearchResult objects."""
    if not html.strip():
        return []

    try:
        tree = lxml_html.fromstring(html)
    except (etree.Error, ValueError) as exc:
        logger.warning("Failed to parse DuckDuckGo HTML: %s", exc)
        return []

    results: list[SearchResult] = []
    elements = tree.xpath('//div[contains(@class, "result")]')
    for element in elements:
        if len(results) >= max_results:
            break

        a_elements = element.xpath('.//a[contains(@class, "result__a")]')
        if not a_elements:
            continue
        title_el = a_elements[0]
        title = title_el.text_content().strip()
        raw_href = title_el.get("href", "").strip()

        clean_url = raw_href
        if "uddg=" in raw_href:
            parsed = urlparse(raw_href)
            qs = parse_qs(parsed.query)
            clean_url = qs.get("uddg", [raw_href])[0]

        if not title or not clean_url or not clean_url.startswith(("http://", "https://")):
            continue

        s_elements = element.xpath('.//a[contains(@class, "result__snippet")]')
        snippet = s_elements[0].text_content().strip() if s_elements else ""

        results.append(
            SearchResult(
                title=title,
                url=clean_url,
                snippet=snippet,
                position=len(results) + 1,
            )
        )

    return results
