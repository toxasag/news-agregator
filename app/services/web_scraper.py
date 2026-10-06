"""Playwright web crawler for `website` sources (see docs/03_INGESTION_MODULE.md).

Three responsibilities live here:

* :class:`BrowserPool` — the process-wide Chromium singleton. Chromium is the
  heaviest component of the pipeline and the target VPS has 4 GB of RAM, so the
  memory-safety rule from ``AGENTS.md`` applies: one browser process per
  worker, page concurrency capped by a semaphore, and one throwaway
  ``BrowserContext`` per scrape so cookies never leak between sources.
* Resource blocking — images, fonts, stylesheets, media and known
  analytics/ads hosts are aborted at the network layer. Only the DOM is needed;
  image *URLs* stay readable because they are read from DOM attributes rather
  than from downloaded bytes.
* Two-stage extraction — per-source CSS/XPath rules first, Trafilatura as the
  fallback, with the winning strategy reported back to the caller. Both stages
  return the *article* subtree as sanitised HTML
  (:attr:`ScrapedArticle.content_html`) next to the plain body text, so the
  downstream heuristic filter can see the markup of the story without the
  surrounding site chrome. The rules path reads the container's ``inner_html``;
  the fallback renders Trafilatura's own extraction tree. Both go through
  :func:`_sanitize_html`, and a body that yields no markup at all is wrapped in
  escaped ``<p>`` paragraphs so ``content_html`` is never empty for a non-empty
  article.

Nothing in this module touches the database: it returns value objects and the
ingestion worker decides what to persist.

Optional dependency: ``playwright-stealth`` is loaded defensively through
:func:`importlib.import_module`. It is a fast-moving package that regularly
breaks against new Playwright releases and may be absent from a given
environment, while the crawler must keep working either way. When it is missing
(or raises) we fall back to a small built-in init script that hides the obvious
automation markers, plus the ``--disable-blink-features`` launch flag.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import importlib
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lxml import etree
from lxml import html as lxml_html
from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    Route,
    ViewportSize,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from trafilatura import bare_extraction
from trafilatura.htmlprocessing import build_html_output
from trafilatura.settings import Document as TrafilaturaDocument
from trafilatura.settings import set_date_params

from app.config import settings

logger: Final[logging.Logger] = logging.getLogger(__name__)

#: Bodies shorter than this are treated as a failed extraction and handed to the
#: Trafilatura fallback: a news page whose text fits in a tweet is almost always
#: a cookie wall, a paywall teaser or a mis-aimed selector.
MIN_CONTENT_LENGTH: Final[int] = 200

#: How much body text the admin preview returns. Enough to eyeball the selector,
#: small enough to keep the HTMX response cheap.
PREVIEW_LENGTH: Final[int] = 1200

_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

_BLOCKED_RESOURCE_TYPES: Final[frozenset[str]] = frozenset(
    {"image", "font", "stylesheet", "media"}
)

#: Analytics, ad-exchange and session-replay endpoints. Matched as substrings
#: against ``netloc + path`` (lowercased), which is why a few entries carry a
#: path component: ``yandex.ru/metrika`` and ``vk.com/rtrg`` must not blocklist
#: the whole domain, since those hosts also serve legitimate article pages.
_BLOCKED_HOST_FRAGMENTS: Final[frozenset[str]] = frozenset(
    {
        "adriver.ru",
        "adservice.google.com",
        "amplitude.com",
        "an.yandex.ru",
        "clarity.ms",
        "connect.facebook.net",
        "criteo.com",
        "criteo.net",
        "doubleclick.net",
        "facebook.com/tr",
        "facebook.net",
        "google-analytics.com",
        "googleadservices.com",
        "googlesyndication.com",
        "googletagmanager.com",
        "googletagservices.com",
        "hotjar.com",
        "hotjar.io",
        "luckyorange.com",
        "matomo.cloud",
        "mc.yandex.ru",
        "outbrain.com",
        "pubmatic.com",
        "quantserve.com",
        "rubiconproject.com",
        "scorecardresearch.com",
        "smartadserver.com",
        "taboola.com",
        "top-fwz1.mail.ru",
        "top-mailru.net",
        "vk.com/rtrg",
        "yandex.ru/metrika",
    }
)

#: Container-friendly flags. ``--no-sandbox`` and ``--disable-dev-shm-usage``
#: are mandatory inside Docker (no user namespaces, 64 MB /dev/shm);
#: ``--disable-gpu`` and the Blink flag trim memory and the automation marker.
_LAUNCH_ARGS: Final[tuple[str, ...]] = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--disable-extensions",
    "--disable-background-networking",
    "--mute-audio",
)

_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_VIEWPORT: Final[ViewportSize] = ViewportSize(width=1920, height=1080)

#: Minimal built-in replacement for ``playwright-stealth``: it covers the checks
#: that actually gate Russian news sites behind bot walls.
_STEALTH_INIT_SCRIPT: Final[str] = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""

_MEDIA_ATTRIBUTES: Final[tuple[str, ...]] = ("src", "data-src", "srcset", "content")
_DATE_ATTRIBUTES: Final[tuple[str, ...]] = (
    "datetime",
    "content",
    "data-date",
    "data-time",
    "title",
)

_SKIPPED_URL_PREFIXES: Final[tuple[str, ...]] = (
    "#",
    "about:",
    "blob:",
    "data:",
    "javascript:",
    "mailto:",
    "tel:",
)

_JS_MOUNT_MARKERS: Final[tuple[str, ...]] = (
    'id="root"',
    'id="app"',
    'id="__next"',
    'id="__nuxt"',
    "data-reactroot",
    "<noscript",
)

#: Elements dropped from :attr:`ScrapedArticle.content_html`, subtree and all.
#: ``content_html`` is persisted to ``raw_news.clean_html`` and rendered later by
#: the dashboard and the publishers, so the article body is treated as hostile
#: input: a scraped page can carry an injected ``<script>`` or a tracking
#: ``<iframe>``, and neither has any business surviving into our own output.
#: ``<style>`` goes too — it can restyle the whole rendering page — and
#: ``<noscript>`` because its content is markup that a browser may reveal.
_UNSAFE_ELEMENTS: Final[tuple[str, ...]] = (
    "script",
    "style",
    "iframe",
    "noscript",
    "object",
    "embed",
    "template",
)

#: Attributes carrying a URL, checked against :data:`_UNSAFE_URL_SCHEMES`.
_URL_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {"href", "src", "action", "formaction", "data", "poster", "srcdoc"}
)

#: Schemes that execute rather than locate. Rejected in URL attributes so a
#: ``javascript:`` link cannot survive sanitisation.
_UNSAFE_URL_SCHEMES: Final[tuple[str, ...]] = ("javascript:", "vbscript:", "data:")

#: XPath predicate matching any element named in :data:`_UNSAFE_ELEMENTS`.
#: Built once because it is evaluated on every scraped page.
_UNSAFE_ELEMENT_PREDICATE: Final[str] = " or ".join(
    f"self::{name}" for name in _UNSAFE_ELEMENTS
)

#: Characters a browser ignores inside an attribute value but which break a
#: naive scheme check (``java&#9;script:``).
_CONTROL_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x20\x7f]")

#: Trafilatura renders a link as ``[anchor text](target)`` in its plain-text
#: output. The body text must read like prose (the rules path produces exactly
#: that, via ``inner_text``), and the target is preserved in ``content_html``
#: anyway, so the markdown wrapper is unwound in the text.
_MARKDOWN_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"\[([^\]\n]+)\]\((?:https?://[^\s()]+)\)"
)


class ScraperError(Exception):
    """Any failure that makes a page unusable for ingestion."""


class NavigationTimeoutError(ScraperError):
    """The page did not reach ``domcontentloaded`` within the configured budget."""


class EmptyContentError(ScraperError):
    """Neither the source rules nor Trafilatura produced a body."""


@dataclass(frozen=True, slots=True)
class ScrapedArticle:
    """One article page reduced to the fields the ingestion worker persists.

    Two HTML fields, with different scopes and different purposes — mixing them
    up is a bug, so the split is spelled out here:

    * :attr:`content_html` — the **article body only**, sanitised. This is the
      field for filtering and storage: it is what
      :func:`~app.services.text_utils.analyze_post` must see (a full-page DOM
      would count the site navigation and the related-articles rail as links and
      trip the ``ad:link_density`` rule on ordinary short news) and what lands in
      ``raw_news.clean_html``.
    * :attr:`html` — the **whole page DOM** as ``page.content()`` returned it,
      unsanitised. Diagnostics only: the JavaScript-shell heuristic and the
      admin selector preview read it to explain *why* an extraction came out
      empty. It must never be persisted or handed to the filter.
    """

    url: str
    title: str
    text: str
    #: Sanitised article-body HTML. For filtering and for ``clean_html``.
    content_html: str
    #: Full unsanitised page DOM. For diagnostics only.
    html: str
    published_at: datetime | None
    media_urls: tuple[str, ...]
    strategy: str
    extracted_at: datetime


@dataclass(frozen=True, slots=True)
class CrawlPreview:
    """Result of a dry run for the admin selector builder (never persisted)."""

    url: str
    title: str
    text_preview: str
    text_length: int
    published_at: datetime | None
    media_urls: tuple[str, ...]
    strategy: str
    warnings: tuple[str, ...]
    duration_ms: int
    #: Length of the captured article-body HTML. Surfaced so an operator can see
    #: that a selector produced markup and not only text, since that markup is
    #: what ``raw_news.clean_html`` stores and the publishers later render.
    content_html_length: int = 0


def _load_stealth() -> Callable[[Page], Awaitable[None]] | None:
    """Import ``playwright-stealth`` if this environment happens to have it.

    Imported by name instead of with a plain ``import`` statement so that a
    missing or half-broken package degrades to the built-in init script instead
    of breaking module import for the whole worker.
    """
    try:
        module = importlib.import_module("playwright_stealth")
    except ImportError:
        logger.info(
            "playwright-stealth is not installed; using the built-in "
            "anti-automation init script"
        )
        return None
    hook = getattr(module, "stealth_async", None)
    if not callable(hook):
        logger.warning(
            "playwright-stealth is installed but exposes no stealth_async(); "
            "using the built-in anti-automation init script"
        )
        return None
    return cast("Callable[[Page], Awaitable[None]]", hook)


_STEALTH_HOOK: Final[Callable[[Page], Awaitable[None]] | None] = _load_stealth()

#: Whether the optional stealth package is in play. Surfaced for the dashboard's
#: diagnostics panel.
STEALTH_AVAILABLE: Final[bool] = _STEALTH_HOOK is not None


def _project_timezone() -> ZoneInfo:
    """Timezone used to make naive scraped dates aware.

    Russian news sites publish local wall-clock time without an offset, so the
    configured project timezone is the only sane interpretation.
    """
    try:
        return ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown TIMEZONE %r; falling back to UTC", settings.timezone)
        return ZoneInfo("UTC")


_TIMEZONE: Final[ZoneInfo] = _project_timezone()


async def _apply_stealth(page: Page) -> None:
    """Hide automation markers before the first navigation."""
    await page.add_init_script(script=_STEALTH_INIT_SCRIPT)
    if _STEALTH_HOOK is None:
        return
    try:
        await _STEALTH_HOOK(page)
    # The patch set targets browser internals and breaks on Chromium updates;
    # a stealth failure must degrade the scrape, never abort it.
    except Exception:
        logger.warning(
            "playwright-stealth failed; continuing with the built-in init script",
            exc_info=True,
        )


def _should_block(resource_type: str, url: str) -> bool:
    """Decide whether a single request is worth its bytes."""
    if not settings.playwright_block_resources:
        return False
    if resource_type in _BLOCKED_RESOURCE_TYPES:
        return True
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False
    return any(
        fragment in f"{parsed.netloc}{parsed.path}".lower()
        for fragment in _BLOCKED_HOST_FRAGMENTS
    )


async def _block_route(route: Route) -> None:
    """Route handler that must never raise.

    Playwright raises ``Error: Route is already handled!`` when a route is
    answered twice, which happens naturally when navigation is cancelled or the
    page closes while requests are still in flight. An exception escaping here
    would surface as an unhandled task error and abort an otherwise healthy
    scrape, so both terminal calls are guarded.
    """
    request = route.request
    blocked = _should_block(request.resource_type, request.url)
    try:
        if blocked:
            await route.abort()
        else:
            await route.continue_()
    except PlaywrightError:
        logger.debug("Route already handled or page gone: %s", request.url)


class BrowserPool:
    """Process-wide Chromium singleton with a hard cap on concurrent pages."""

    __slots__ = ("_active_pages", "_browser", "_lock", "_playwright", "_semaphore")

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock: Final[asyncio.Lock] = asyncio.Lock()
        #: The OOM guard from ``AGENTS.md``: it bounds concurrent *pages*, not
        #: browsers. One Chromium process is cheap; every open page costs tens
        #: of megabytes of renderer memory, so unbounded page creation is what
        #: actually kills a 4 GB VPS.
        self._semaphore: Final[asyncio.Semaphore] = asyncio.Semaphore(
            settings.playwright_max_concurrent_pages
        )
        self._active_pages = 0

    @property
    def is_running(self) -> bool:
        """Whether a live, connected browser process is currently held."""
        return self._browser is not None and self._browser.is_connected()

    @property
    def active_pages(self) -> int:
        """Pages currently checked out. Never exceeds the semaphore bound."""
        return self._active_pages

    async def start(self) -> None:
        """Launch Chromium eagerly, e.g. from a FastAPI/ARQ startup hook."""
        await self._ensure_browser()

    async def close(self) -> None:
        """Release Chromium and the Playwright driver. Idempotent."""
        async with self._lock:
            await self._shutdown_locked()

    async def _ensure_browser(self) -> Browser:
        """Return the live browser, launching it at most once.

        Double-checked locking: the cheap unlocked read serves the hot path,
        while the re-check *inside* the lock is what actually enforces the
        singleton. Without it, two coroutines that both observed ``None`` would
        queue on the lock and launch two Chromium processes, doubling memory
        and silently leaking the first one. A crashed browser (``is_connected``
        false) is torn down and relaunched here as well.
        """
        browser = self._browser
        if browser is not None and browser.is_connected():
            return browser
        async with self._lock:
            browser = self._browser
            if browser is not None and browser.is_connected():
                return browser
            await self._shutdown_locked()
            driver = await async_playwright().start()
            self._playwright = driver
            try:
                self._browser = await driver.chromium.launch(
                    headless=settings.playwright_headless,
                    args=list(_LAUNCH_ARGS),
                )
            except PlaywrightError as exc:
                await self._shutdown_locked()
                raise ScraperError(f"Chromium failed to launch: {exc}") from exc
            logger.info(
                "Chromium launched (headless=%s, max_pages=%d, stealth=%s)",
                settings.playwright_headless,
                settings.playwright_max_concurrent_pages,
                STEALTH_AVAILABLE,
            )
            return self._browser

    async def _shutdown_locked(self) -> None:
        """Tear down browser and driver. Caller must hold ``self._lock``."""
        browser, self._browser = self._browser, None
        driver, self._playwright = self._playwright, None
        if browser is not None:
            try:
                await browser.close()
            except PlaywrightError:
                logger.debug("Browser close failed (already gone)", exc_info=True)
        if driver is not None:
            try:
                await driver.stop()
            except PlaywrightError:
                logger.debug("Playwright stop failed (already gone)", exc_info=True)

    @asynccontextmanager
    async def page(
        self,
        storage_state: dict[str, Any] | str | None = None,
        block_resources: bool = True,
    ) -> AsyncIterator[Page]:
        """Yield a stealthed page in a throwaway or authenticated context.

        The semaphore is acquired first and released in the outermost
        ``finally`` exactly once, so a launch failure, a navigation exception or
        a cancellation can never leak a permit and wedge the pool. Page and
        context are closed on every path; contexts are never reused, which is
        what keeps cookies and per-site storage from bleeding across sources.
        """
        await self._semaphore.acquire()
        self._active_pages += 1
        context: BrowserContext | None = None
        try:
            browser = await self._ensure_browser()
            context_kwargs: dict[str, Any] = {
                "user_agent": _USER_AGENT,
                "locale": "ru-RU",
                "timezone_id": settings.timezone,
                "viewport": _VIEWPORT,
                "ignore_https_errors": True,
            }
            if storage_state is not None:
                context_kwargs["storage_state"] = storage_state
            context = await browser.new_context(**context_kwargs)
            context.set_default_navigation_timeout(
                settings.playwright_navigation_timeout
            )
            context.set_default_timeout(settings.playwright_navigation_timeout)
            page = await context.new_page()
            await _apply_stealth(page)
            if block_resources:
                await page.route("**/*", _block_route)
            try:
                yield page
            finally:
                try:
                    await page.close()
                except PlaywrightError:
                    logger.debug("Page close failed", exc_info=True)
        except PlaywrightError as exc:
            raise ScraperError(f"Browser page could not be prepared: {exc}") from exc
        finally:
            if context is not None:
                try:
                    await context.close()
                except PlaywrightError:
                    logger.debug("Context close failed", exc_info=True)
            self._active_pages -= 1
            self._semaphore.release()

    @asynccontextmanager
    async def page_with_storage(
        self,
        storage_state: dict[str, Any] | str | None = None,
        block_resources: bool = False,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[Page]:
        """Yield an authenticated page with block_resources=False."""
        async with self.page(
            storage_state=storage_state, block_resources=block_resources
        ) as p:
            if timeout_seconds is not None:
                timeout_ms = int(timeout_seconds * 1000)
                p.set_default_timeout(timeout_ms)
                p.set_default_navigation_timeout(timeout_ms)
            yield p




#: Module-level singleton, mirroring the shared-resource pattern used by
#: ``app.redis_client``. ``asyncio`` primitives bind to the running loop lazily
#: on Python 3.12, so constructing it at import time is safe.
_BROWSER_POOL: Final[BrowserPool] = BrowserPool()


def get_browser_pool() -> BrowserPool:
    """Return the process-wide :class:`BrowserPool`."""
    return _BROWSER_POOL


async def close_browser_pool() -> None:
    """Shut down Chromium on application/worker shutdown."""
    await _BROWSER_POOL.close()


def _as_selector(value: Any) -> str | None:
    """Accept a selector only if it is a non-blank string.

    ``crawl_rules`` is operator-supplied JSONB, so every field has to survive
    nulls, numbers and empty strings without raising.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _as_selector_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    selectors = [_as_selector(item) for item in value]
    return tuple(item for item in selectors if item is not None)


def _as_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        if parsed > 0:
            return parsed
    return None


@dataclass(frozen=True, slots=True)
class _CrawlRules:
    """Typed view over ``sources.crawl_rules``. Every field is optional."""

    list_selector: str | None = None
    link_selector: str | None = None
    title_selector: str | None = None
    content_selector: str | None = None
    date_selector: str | None = None
    media_selector: str | None = None
    exclude_selectors: tuple[str, ...] = ()
    max_items: int | None = None
    wait_for_selector: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> _CrawlRules:
        if not raw:
            return cls()
        return cls(
            list_selector=_as_selector(raw.get("list_selector")),
            link_selector=_as_selector(raw.get("link_selector")),
            title_selector=_as_selector(raw.get("title_selector")),
            content_selector=_as_selector(raw.get("content_selector")),
            date_selector=_as_selector(raw.get("date_selector")),
            media_selector=_as_selector(raw.get("media_selector")),
            exclude_selectors=_as_selector_list(raw.get("exclude_selectors")),
            max_items=_as_positive_int(raw.get("max_items")),
            wait_for_selector=_as_selector(raw.get("wait_for_selector")),
        )

    @property
    def item_limit(self) -> int:
        return self.max_items or settings.ingest_max_messages_per_source


def _normalize_selector(selector: str) -> str:
    """Turn a rule selector into a Playwright selector string.

    Per docs/03 a selector is XPath when it starts with ``/`` or ``(``; anything
    else is CSS. Playwright needs the explicit ``xpath=`` engine prefix for the
    former, and an already-prefixed selector is passed through untouched.
    """
    stripped = selector.strip()
    if stripped.startswith(("xpath=", "css=", "text=")):
        return stripped
    if stripped.startswith(("/", "(")):
        return f"xpath={stripped}"
    return stripped


def _scope_selector(selector: str) -> str:
    """Same as :func:`_normalize_selector` but relative to an element.

    An absolute XPath (``//a``) evaluated from an element handle would escape
    back to the document root, so it is rewritten to the relative form
    (``.//a``) to stay inside the list container.
    """
    normalized = _normalize_selector(selector)
    if normalized.startswith("xpath=/") and not normalized.startswith("xpath=//."):
        return f"xpath=.{normalized.removeprefix('xpath=')}"
    return normalized


@dataclass(slots=True)
class _Report:
    """Collects operator-facing warnings and which rule fields actually fired."""

    warnings: list[str] = field(default_factory=list)
    rules_hits: set[str] = field(default_factory=set)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def hit(self, name: str) -> None:
        self.rules_hits.add(name)


@dataclass(frozen=True, slots=True)
class _TrafilaturaResult:
    title: str
    text: str
    date: str | None
    #: Article-body HTML rebuilt from Trafilatura's own extraction tree, already
    #: reduced to the simplified tag set (``<p>``, ``<h*>``, ``<ul>``, ``<a>``).
    #: Empty when the parser produced no usable body.
    content_html: str


@dataclass(frozen=True, slots=True)
class _ContentExtract:
    """Body of one article as read from the source rules: text and its markup."""

    text: str
    html: str


@dataclass(frozen=True, slots=True)
class _Outcome:
    article: ScrapedArticle
    warnings: tuple[str, ...]
    duration_ms: int


def _collapse(value: str) -> str:
    """Squash all whitespace runs into single spaces (titles, dates)."""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _normalize_text(value: str) -> str:
    """Trim each line and collapse blank-line runs, preserving paragraphs."""
    lines = [line.strip() for line in value.replace("\xa0", " ").splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _is_unsafe_url(value: str) -> bool:
    """Whether a URL attribute value uses an executing scheme.

    Leading whitespace and embedded control characters are stripped first:
    ``java\\tscript:`` and ``  javascript:`` are both live in a browser, so a
    naive ``startswith`` on the raw value would pass them through.
    """
    collapsed = _CONTROL_CHARS_RE.sub("", value).strip().lower()
    return collapsed.startswith(_UNSAFE_URL_SCHEMES)


def _sanitize_html(fragment: str) -> str:
    """Return `fragment` with executable markup removed.

    This is the single choke point for every HTML that reaches
    :attr:`ScrapedArticle.content_html` and therefore ``raw_news.clean_html``.
    A scraped page is hostile input: it can carry an injected ``<script>``, a
    tracking ``<iframe>``, an ``onerror`` handler or a ``javascript:`` href, and
    none of those may survive into markup the dashboard and the publishers
    render later.

    Three passes, in this order:

    * unsafe elements are dropped *with their subtree* (``<script>`` body text
      would otherwise remain as visible text);
    * every ``on*`` handler and ``style`` attribute is removed;
    * URL-bearing attributes are dropped when their scheme executes.

    Returns ``""`` when the fragment does not parse, because a fragment lxml
    cannot read is a fragment we cannot vouch for.
    """
    if not fragment.strip():
        return ""
    try:
        root = lxml_html.fragment_fromstring(fragment, create_parent="div")
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        logger.debug("Could not parse an HTML fragment for sanitisation")
        return ""

    for element in root.xpath(f"//*[{_UNSAFE_ELEMENT_PREDICATE}]"):
        parent = element.getparent()
        if parent is None:
            continue
        # `drop_tree` removes the element and its children while preserving the
        # tail text, so surrounding prose is not lost with the tag.
        element.drop_tree()

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        for name in list(element.attrib):
            lowered = name.lower()
            if lowered.startswith("on") or lowered == "style":
                del element.attrib[name]
                continue
            if lowered in _URL_ATTRIBUTES and _is_unsafe_url(element.attrib[name]):
                del element.attrib[name]

    return _inner_markup(root).strip()


def _inner_markup(node: lxml_html.HtmlElement) -> str:
    """Serialise a node's children, escaping its own text nodes.

    The leading text node **must** be escaped rather than emitted verbatim:
    ``lxml`` hands it back as decoded text, so a body whose first characters are
    ``<script>`` would otherwise be re-serialised as a live tag and defeat the
    sanitiser that just ran. Child elements are serialised by ``lxml``, which
    escapes their text itself.
    """
    parts = [html_lib.escape(node.text, quote=False) if node.text else ""]
    parts.extend(
        etree.tostring(child, encoding="unicode", method="html") for child in node
    )
    return "".join(parts)


def _unwrap_html_document(rendered: str) -> str:
    """Strip the ``<html><body>`` wrapper Trafilatura's HTML output adds.

    ``content_html`` is a *fragment* embedded into our own templates, so a
    nested document element would produce invalid markup once inserted.
    """
    try:
        root = lxml_html.fromstring(rendered)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return rendered
    body = root.find("body")
    return _inner_markup(root if body is None else body).strip()


def _is_sane_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    return "." in hostname or hostname == "localhost"


def _resolve_url(base_url: str, candidate: str | None) -> str | None:
    """Resolve a DOM-sourced URL against the page URL, rejecting junk."""
    if not candidate:
        return None
    value = candidate.strip()
    if not value or value.lower().startswith(_SKIPPED_URL_PREFIXES):
        return None
    absolute = urljoin(base_url, value)
    parsed = urlparse(absolute)
    if parsed.scheme not in _ALLOWED_SCHEMES or not _is_sane_host(parsed.hostname):
        return None
    return absolute


def _strip_fragment(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def _require_http_url(url: str) -> str:
    """Reject anything that is not an absolute http(s) URL.

    Guards against operator typos and against ``file://`` or ``chrome://``
    targets reaching the browser from source configuration.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in _ALLOWED_SCHEMES or not _is_sane_host(parsed.hostname):
        raise ScraperError(f"Refusing to crawl a non-http(s) URL: {url!r}")
    return url.strip()


_ISO_RE: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|z|[+-]\d{2}:?\d{2})?)?"
)
_DOTTED_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b(?:[\s,]+(?:\D{0,4}?)(\d{1,2}):(\d{2}))?"
)
_SLASHED_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b(?:[\s,]+(\d{1,2}):(\d{2}))?"
)
_EPOCH_RE: Final[re.Pattern[str]] = re.compile(r"^\d{10}(?:\d{3})?$")
_RUSSIAN_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d{1,2})\s+([а-яё]{3,})\s+(\d{4})"  # noqa: RUF001 - Cyrillic is intended
    r"(?:\s*(?:г\.?|года))?"  # noqa: RUF001 - Cyrillic year suffix
    r"(?:[\s,]+(?:в\s+)?(\d{1,2}):(\d{2}))?"
)

#: Nominative and genitive month names, plus the common three-letter
#: abbreviations used by Russian CMS themes.
_RUSSIAN_MONTHS: Final[dict[str, int]] = {
    "января": 1,
    "январь": 1,
    "янв": 1,
    "февраля": 2,
    "февраль": 2,
    "фев": 2,
    "марта": 3,
    "март": 3,
    "мар": 3,
    "апреля": 4,
    "апрель": 4,
    "апр": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июн": 6,
    "июля": 7,
    "июль": 7,
    "июл": 7,
    "августа": 8,
    "август": 8,
    "авг": 8,
    "сентября": 9,
    "сентябрь": 9,
    "сент": 9,
    "сен": 9,
    "октября": 10,
    "октябрь": 10,
    "окт": 10,
    "ноября": 11,
    "ноябрь": 11,
    "ноя": 11,
    "декабря": 12,
    "декабрь": 12,
    "дек": 12,
}

#: Label noise that precedes a date node's text on many templates.
_DATE_NOISE_PREFIXES: Final[tuple[str, ...]] = (
    "опубликовано",
    "обновлено",
    "дата публикации",
    "published",
    "updated",
)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_TIMEZONE)
    return value


def _is_sane_year(value: datetime) -> bool:
    """Reject 1970 epochs and far-future dates produced by bad parses."""
    upper = datetime.now(UTC) + timedelta(days=2)
    return value.year >= 1995 and _ensure_aware(value) <= upper


def _clean_date_text(raw: str) -> str:
    text = _collapse(raw)
    lowered = text.lower()
    for prefix in _DATE_NOISE_PREFIXES:
        if lowered.startswith(prefix):
            # Both dash variants are used as label separators by CMS themes.
            text = text[len(prefix) :].lstrip(" :–-—")  # noqa: RUF001
            break
    return text.strip()


def _parse_iso(text: str) -> datetime | None:
    match = _ISO_RE.search(text)
    if match is None:
        return None
    candidate = match.group(0).replace("z", "Z")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _parse_epoch(text: str) -> datetime | None:
    if not _EPOCH_RE.match(text):
        return None
    raw = int(text)
    seconds = raw / 1000 if len(text) == 13 else float(raw)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _build(
    year: int, month: int, day: int, hour: str | None, minute: str | None
) -> datetime | None:
    try:
        return datetime(
            year, month, day, int(hour or 0), int(minute or 0), tzinfo=_TIMEZONE
        )
    except ValueError:
        return None


def _parse_dotted(text: str) -> datetime | None:
    match = _DOTTED_RE.search(text)
    if match is None:
        return None
    return _build(
        int(match.group(3)),
        int(match.group(2)),
        int(match.group(1)),
        match.group(4),
        match.group(5),
    )


def _parse_slashed(text: str) -> datetime | None:
    """Read ``d/m/Y``: Russian sites use day-first, never the US order."""
    match = _SLASHED_RE.search(text)
    if match is None:
        return None
    return _build(
        int(match.group(3)),
        int(match.group(2)),
        int(match.group(1)),
        match.group(4),
        match.group(5),
    )


def _parse_russian(text: str) -> datetime | None:
    match = _RUSSIAN_RE.search(text.lower())
    if match is None:
        return None
    month = _RUSSIAN_MONTHS.get(match.group(2))
    if month is None:
        return None
    return _build(
        int(match.group(3)),
        month,
        int(match.group(1)),
        match.group(4),
        match.group(5),
    )


_DATE_PARSERS: Final[tuple[Callable[[str], datetime | None], ...]] = (
    _parse_iso,
    _parse_epoch,
    _parse_dotted,
    _parse_russian,
    _parse_slashed,
)


def _parse_datetime(raw: str | None) -> datetime | None:
    """Best-effort date parsing that returns ``None`` instead of raising.

    A wrong or missing publication date must never fail an otherwise good
    scrape, so every parser is tried in turn and an implausible year is
    discarded. Naive results are anchored to the project timezone.
    """
    if not raw:
        return None
    text = _clean_date_text(raw)
    if not text:
        return None
    for parser in _DATE_PARSERS:
        parsed = parser(text)
        if parsed is not None and _is_sane_year(parsed):
            return _ensure_aware(parsed)
    return None


async def _query_all(page: Page, selector: str) -> list[ElementHandle]:
    """Match a selector without waiting for it.

    ``query_selector_all`` returns immediately, so a selector that matches
    nothing costs no time and can be reported as a warning instead of burning
    the navigation timeout. A malformed selector is downgraded to "no match"
    for the same reason.
    """
    try:
        return await page.query_selector_all(_normalize_selector(selector))
    except PlaywrightError as exc:
        logger.debug("Invalid selector %r: %s", selector, exc)
        return []


async def _query_inside(handle: ElementHandle, selector: str) -> list[ElementHandle]:
    try:
        return await handle.query_selector_all(_scope_selector(selector))
    except PlaywrightError as exc:
        logger.debug("Invalid scoped selector %r: %s", selector, exc)
        return []


async def _inner_text(handle: ElementHandle) -> str:
    try:
        return await handle.inner_text()
    except PlaywrightError:
        try:
            return await handle.text_content() or ""
        except PlaywrightError:
            return ""


async def _attribute(handle: ElementHandle, name: str) -> str | None:
    try:
        return await handle.get_attribute(name)
    except PlaywrightError:
        return None


async def _page_title(page: Page) -> str:
    try:
        return await page.title()
    except PlaywrightError:
        return ""


async def _page_html(page: Page) -> str:
    try:
        return await page.content()
    except PlaywrightError as exc:
        raise ScraperError(f"Could not read the DOM of {page.url}: {exc}") from exc


async def _goto(page: Page, url: str) -> None:
    """Navigate, translating Playwright failures into :class:`ScraperError`.

    ``domcontentloaded`` is the right wait state here: with images, fonts and
    stylesheets blocked there is no point waiting for ``load``, and ad scripts
    would keep ``networkidle`` from ever settling.
    """
    try:
        response = await page.goto(url, wait_until="domcontentloaded")
    except PlaywrightTimeoutError as exc:
        raise NavigationTimeoutError(
            f"Navigation to {url} timed out after "
            f"{settings.playwright_navigation_timeout} ms"
        ) from exc
    except PlaywrightError as exc:
        raise ScraperError(f"Navigation to {url} failed: {exc}") from exc
    if response is not None and response.status >= 400:
        raise ScraperError(f"Navigation to {url} returned HTTP {response.status}")


async def _wait_for_rule_selector(
    page: Page, rules: _CrawlRules, report: _Report
) -> None:
    """Wait for the hydration marker, if the source declares one."""
    selector = rules.wait_for_selector
    if not selector:
        return
    try:
        await page.wait_for_selector(_normalize_selector(selector), state="attached")
    except PlaywrightTimeoutError:
        report.warn(f"wait_for_selector never appeared: {selector!r}")
    except PlaywrightError as exc:
        report.warn(f"wait_for_selector is invalid ({selector!r}): {exc}")


async def _apply_excludes(page: Page, rules: _CrawlRules, report: _Report) -> None:
    """Delete boilerplate subtrees before any text is read.

    Removal happens in the live DOM, so both the rules stage and the
    Trafilatura fallback (which reads ``page.content()`` afterwards) see the
    cleaned document.
    """
    for selector in rules.exclude_selectors:
        handles = await _query_all(page, selector)
        if not handles:
            report.warn(f"exclude_selector matched nothing: {selector!r}")
            continue
        for handle in handles:
            try:
                await handle.evaluate("element => element.remove()")
            except PlaywrightError:
                logger.debug("Could not remove node for %r", selector)


async def _extract_title(page: Page, rules: _CrawlRules, report: _Report) -> str:
    if rules.title_selector:
        for handle in await _query_all(page, rules.title_selector):
            text = _collapse(await _inner_text(handle))
            if text:
                report.hit("title")
                return text
        report.warn(f"title_selector matched no text: {rules.title_selector!r}")
    return _collapse(await _page_title(page))


async def _inner_html(handle: ElementHandle) -> str:
    """Read one element's inner HTML, tolerating a detached node."""
    try:
        return await handle.inner_html()
    except PlaywrightError:
        return ""


async def _extract_content(
    page: Page, rules: _CrawlRules, report: _Report
) -> _ContentExtract:
    """Read the article body from the source's ``content_selector``.

    Text and HTML are read from the *same* handles in one pass, so the two can
    never describe different nodes. The markup is sanitised here rather than at
    the call site because this is where it enters the pipeline.
    """
    if not rules.content_selector:
        return _ContentExtract(text="", html="")
    blocks: list[str] = []
    markup: list[str] = []
    for handle in await _query_all(page, rules.content_selector):
        text = (await _inner_text(handle)).strip()
        if text:
            blocks.append(text)
            markup.append(await _inner_html(handle))
    if not blocks:
        report.warn(f"content_selector matched no text: {rules.content_selector!r}")
        return _ContentExtract(text="", html="")
    report.hit("content")
    sanitised = _sanitize_html("\n".join(part for part in markup if part))
    return _ContentExtract(
        text=_normalize_text("\n\n".join(blocks)),
        # A container of bare text nodes sanitises to text with no tags, which is
        # not usable HTML: it carries no paragraph structure and would be
        # rendered as one run-on block. Such a body is reported as markup-less so
        # the caller wraps its paragraphs instead.
        html="" if _is_tagless(sanitised) else sanitised,
    )


def _is_tagless(fragment: str) -> bool:
    """Whether a sanitised fragment contains no element at all."""
    return "<" not in fragment


async def _extract_date(
    page: Page, rules: _CrawlRules, report: _Report
) -> datetime | None:
    if not rules.date_selector:
        return None
    for handle in await _query_all(page, rules.date_selector):
        for name in _DATE_ATTRIBUTES:
            parsed = _parse_datetime(await _attribute(handle, name))
            if parsed is not None:
                report.hit("date")
                return parsed
        parsed = _parse_datetime(await _inner_text(handle))
        if parsed is not None:
            report.hit("date")
            return parsed
    report.warn(f"date_selector produced no parsable date: {rules.date_selector!r}")
    return None


def _best_from_srcset(value: str) -> str | None:
    """Pick the widest candidate from a ``srcset`` list."""
    best: str | None = None
    best_width = -1
    for entry in value.split(","):
        parts = entry.strip().split()
        if not parts:
            continue
        width = -1
        if len(parts) > 1 and parts[1].endswith("w") and parts[1][:-1].isdigit():
            width = int(parts[1][:-1])
        if width >= best_width:
            best, best_width = parts[0], width
    return best


async def _media_candidate(handle: ElementHandle) -> str | None:
    for name in _MEDIA_ATTRIBUTES:
        value = await _attribute(handle, name)
        if not value or not value.strip():
            continue
        if name == "srcset":
            best = _best_from_srcset(value)
            if best:
                return best
            continue
        return value.strip()
    return None


async def _extract_media(
    page: Page, rules: _CrawlRules, base_url: str, report: _Report
) -> tuple[str, ...]:
    """Collect media URLs from DOM attributes.

    The bytes were never downloaded (the route blocker aborted them), but the
    attributes survive, which is exactly what Package 3 needs.
    """
    if not rules.media_selector:
        return ()
    urls: list[str] = []
    for handle in await _query_all(page, rules.media_selector):
        resolved = _resolve_url(base_url, await _media_candidate(handle))
        if resolved is not None:
            urls.append(resolved)
    if not urls:
        report.warn(f"media_selector matched no usable URL: {rules.media_selector!r}")
        return ()
    report.hit("media")
    return tuple(dict.fromkeys(urls))


def _trafilatura_html(document: TrafilaturaDocument) -> str:
    """Render Trafilatura's extraction tree as sanitised article-body HTML.

    ``build_html_output`` converts the internal XML tags to the simplified HTML
    set (``<p>``, ``<h*>``, ``<ul>``, ``<blockquote>``, ``<a>``) and strips every
    attribute except ``href``, so the result carries no inline styles, no event
    handlers and no scripts. It is passed through :func:`_sanitize_html` anyway:
    that is the single choke point every stored HTML goes through, and relying on
    a third-party renderer's tag whitelist for a security property is exactly the
    assumption that breaks on a library upgrade.

    ``with_metadata=False`` because the metadata belongs in its own columns; a
    ``<head>`` block embedded in the body would be rendered by the dashboard.
    """
    try:
        rendered = build_html_output(document, with_metadata=False)
    # lxml serialisation can fail on a pathological tree; the body text is
    # already extracted at this point, so HTML is the optional half.
    except Exception:
        logger.warning("Trafilatura HTML rendering failed", exc_info=True)
        return ""
    return _sanitize_html(_unwrap_html_document(rendered))


def _run_trafilatura(html: str, url: str) -> _TrafilaturaResult | None:
    """Synchronous, CPU-bound extraction. Only call via :func:`asyncio.to_thread`."""
    date_params: dict[str, Any] = dict(set_date_params(extensive=True))
    date_params["outputformat"] = "%Y-%m-%dT%H:%M:%S%z"
    try:
        # The `Document` form is used rather than `as_dict=True`: the dict drops
        # the extraction tree, and that tree is the only source of article-body
        # HTML. It also avoids the `as_dict` PendingDeprecationWarning.
        extracted = bare_extraction(
            html,
            url=url,
            with_metadata=True,
            include_comments=False,
            include_images=False,
            include_tables=True,
            favor_precision=True,
            date_extraction_params=date_params,
        )
    # lxml/Trafilatura can raise on malformed markup; a broken page must not
    # take down the worker task that scraped it.
    except Exception:
        logger.warning("Trafilatura raised while parsing %s", url, exc_info=True)
        return None
    if not isinstance(extracted, TrafilaturaDocument):
        return None
    text = extracted.text
    title = extracted.title
    date = extracted.date
    return _TrafilaturaResult(
        title=_collapse(title) if isinstance(title, str) else "",
        text=_normalize_text(text) if isinstance(text, str) else "",
        date=date if isinstance(date, str) else None,
        content_html=_trafilatura_html(extracted),
    )


async def _extract_with_trafilatura(html: str, url: str) -> _TrafilaturaResult | None:
    """Run Trafilatura off the event loop.

    Trafilatura is synchronous and CPU-bound: parsing a large page with lxml
    takes hundreds of milliseconds during which nothing else in the worker
    could run. Per the strict-asyncio rule in ``AGENTS.md`` it is dispatched to
    the default thread pool so concurrent scrapes and the ARQ heartbeat keep
    making progress.
    """
    return await asyncio.to_thread(_run_trafilatura, html, url)


def _looks_javascript_rendered(html: str) -> bool:
    """Heuristic: does this page ship a shell that only JS fills in?"""
    lowered = html.lower()
    if any(marker in lowered for marker in _JS_MOUNT_MARKERS):
        return True
    return lowered.count("<script") > 3 and lowered.count("<p") < 3


def _resolve_strategy(report: _Report, trafilatura_body: bool) -> str:
    content_fields = report.rules_hits & {"title", "content", "date", "media"}
    if trafilatura_body:
        return "hybrid" if content_fields else "trafilatura"
    return "rules" if content_fields else "trafilatura"


def _paragraphs_to_html(text: str) -> str:
    """Wrap plain-text paragraphs in ``<p>`` tags, escaping the content.

    Escaping is what makes this safe: the input is source-controlled text, so a
    body containing ``<script>`` must become visible ``&lt;script&gt;`` rather
    than a live tag. Blank-line runs separate paragraphs and single newlines
    become ``<br>``, matching how the text was normalised.
    """
    paragraphs = [block.strip() for block in text.split("\n\n") if block.strip()]
    return "".join(
        "<p>{}</p>".format(
            "<br>".join(
                html_lib.escape(line, quote=False)
                for line in paragraph.splitlines()
                if line.strip()
            )
        )
        for paragraph in paragraphs
    )


def _elapsed_ms(started: float) -> int:
    return max(int((time.perf_counter() - started) * 1000), 0)


async def _scrape(url: str, crawl_rules: Mapping[str, Any] | None) -> _Outcome:
    """Shared pipeline behind :func:`scrape_article` and :func:`test_crawl_url`."""
    target = _require_http_url(url)
    rules = _CrawlRules.from_mapping(crawl_rules)
    report = _Report()
    started = time.perf_counter()

    async with get_browser_pool().page() as page:
        await _goto(page, target)
        await _wait_for_rule_selector(page, rules, report)
        final_url = page.url or target
        await _apply_excludes(page, rules, report)
        html = await _page_html(page)
        title = await _extract_title(page, rules, report)
        extracted = await _extract_content(page, rules, report)
        published_at = await _extract_date(page, rules, report)
        media = await _extract_media(page, rules, final_url, report)

    body = extracted.text
    content_html = extracted.html

    # The page slot is released before the CPU-bound fallback: the DOM has been
    # serialised into `html`, so holding a renderer open would waste one of the
    # two page permits for nothing.
    trafilatura_body = False
    if len(body) < MIN_CONTENT_LENGTH:
        if rules.content_selector and body:
            report.warn(
                f"content_selector yielded only {len(body)} chars "
                f"(<{MIN_CONTENT_LENGTH}); trying Trafilatura"
            )
        fallback = await _extract_with_trafilatura(html, final_url)
        if fallback is not None and len(fallback.text) > len(body):
            body = fallback.text
            # The markup has to move with the text it describes: keeping the
            # rules' HTML next to a Trafilatura body would store two different
            # articles in one row.
            content_html = fallback.content_html
            trafilatura_body = True
            report.warn("body came from the Trafilatura fallback, not from rules")
            if "title" not in report.rules_hits and fallback.title:
                title = fallback.title
            if published_at is None:
                published_at = _parse_datetime(fallback.date)

    if not body:
        report.warn("no body extracted: neither the rules nor Trafilatura found text")
    elif len(body) < MIN_CONTENT_LENGTH:
        report.warn(f"extracted body is suspiciously short ({len(body)} chars)")
    if len(body) < MIN_CONTENT_LENGTH and _looks_javascript_rendered(html):
        report.warn(
            "page looks JavaScript-rendered; point wait_for_selector at the "
            "container that appears after hydration"
        )

    # Last resort: a body was extracted but no usable markup came with it (a
    # `<div>` of bare text nodes, or a rendering failure). Wrapping the
    # paragraphs keeps `content_html` a valid, non-empty fragment so downstream
    # consumers never have to special-case a text-only article.
    if body and not content_html:
        content_html = _paragraphs_to_html(body)

    article = ScrapedArticle(
        url=final_url,
        title=title,
        text=body,
        content_html=content_html,
        html=html,
        published_at=published_at,
        media_urls=media,
        strategy=_resolve_strategy(report, trafilatura_body),
        extracted_at=datetime.now(UTC),
    )
    return _Outcome(
        article=article,
        warnings=tuple(report.warnings),
        duration_ms=_elapsed_ms(started),
    )


async def scrape_article(
    url: str, crawl_rules: Mapping[str, Any] | None = None
) -> ScrapedArticle:
    """Scrape one article page.

    Raises :class:`ScraperError` (or one of its subclasses) when the page cannot
    be navigated or yields no body at all; the ingestion worker catches it per
    source so one broken page cannot abort a whole run.
    """
    outcome = await _scrape(url, crawl_rules)
    for warning in outcome.warnings:
        logger.debug("Scrape warning for %s: %s", url, warning)
    if not outcome.article.text:
        raise EmptyContentError(f"No article content could be extracted from {url}")
    logger.info(
        "Scraped %s in %d ms (strategy=%s, chars=%d, media=%d)",
        outcome.article.url,
        outcome.duration_ms,
        outcome.article.strategy,
        len(outcome.article.text),
        len(outcome.article.media_urls),
    )
    return outcome.article


async def discover_links(list_url: str, crawl_rules: Mapping[str, Any]) -> list[str]:
    """Collect article URLs from a listing page.

    Relative hrefs are resolved against the page's own URL (which may differ
    from ``list_url`` after a redirect), fragments are dropped, duplicates are
    removed while preserving document order, and the result is capped at
    ``max_items`` — falling back to ``INGEST_MAX_MESSAGES_PER_SOURCE`` so a
    misconfigured source cannot flood the pipeline.
    """
    target = _require_http_url(list_url)
    rules = _CrawlRules.from_mapping(crawl_rules)
    report = _Report()

    async with get_browser_pool().page() as page:
        await _goto(page, target)
        await _wait_for_rule_selector(page, rules, report)
        base_url = page.url or target
        candidates = await _collect_hrefs(page, rules, report)

    same_host_only = rules.list_selector is None and rules.link_selector is None
    base_host = urlparse(base_url).hostname
    links: list[str] = []
    for candidate in candidates:
        resolved = _resolve_url(base_url, candidate)
        if resolved is None:
            continue
        if same_host_only and urlparse(resolved).hostname != base_host:
            continue
        links.append(_strip_fragment(resolved))

    unique = list(dict.fromkeys(links))[: rules.item_limit]
    for warning in report.warnings:
        logger.debug("Link discovery warning for %s: %s", list_url, warning)
    logger.info("Discovered %d link(s) on %s", len(unique), base_url)
    return unique


async def _href_of(handle: ElementHandle, link_selector: str | None) -> list[str]:
    """Read hrefs from a node: from itself when it is an anchor, else from inside."""
    selector = link_selector or "a[href]"
    inner_nodes = await _query_inside(handle, selector)
    hrefs = [await _attribute(node, "href") for node in inner_nodes]
    found = [href for href in hrefs if href]
    if found:
        return found
    own = await _attribute(handle, "href")
    return [own] if own else []


async def _collect_hrefs(
    page: Page, rules: _CrawlRules, report: _Report
) -> list[str]:
    if rules.list_selector:
        containers = await _query_all(page, rules.list_selector)
        if not containers:
            report.warn(f"list_selector matched nothing: {rules.list_selector!r}")
            return []
        hrefs: list[str] = []
        for container in containers:
            hrefs.extend(await _href_of(container, rules.link_selector))
        if not hrefs:
            report.warn(
                "no links inside list_selector "
                f"(link_selector={rules.link_selector!r})"
            )
        return hrefs

    selector = rules.link_selector or "a[href]"
    handles = await _query_all(page, selector)
    if not handles:
        report.warn(f"link_selector matched nothing: {selector!r}")
    values = [await _attribute(handle, "href") for handle in handles]
    return [value for value in values if value]


async def test_crawl_url(
    url: str, crawl_rules: Mapping[str, Any] | None = None
) -> CrawlPreview:
    """Dry-run the crawler for the admin selector builder.

    Read-only by construction: it writes neither ``raw_news`` nor
    ``sources.last_scraped_at``, so an operator can hammer the "Check" button
    while tuning selectors. It never raises — navigation failures and dead
    selectors are reported through ``warnings`` so the UI can render them next
    to the field that caused them.
    """
    started = time.perf_counter()
    try:
        outcome = await _scrape(url, crawl_rules)
    except ScraperError as exc:
        return CrawlPreview(
            url=url,
            title="",
            text_preview="",
            text_length=0,
            published_at=None,
            media_urls=(),
            strategy="failed",
            warnings=(str(exc),),
            duration_ms=_elapsed_ms(started),
            content_html_length=0,
        )

    article = outcome.article
    preview = article.text[:PREVIEW_LENGTH]
    if len(article.text) > PREVIEW_LENGTH:
        preview = f"{preview.rstrip()}…"
    return CrawlPreview(
        url=article.url,
        title=article.title,
        text_preview=preview,
        text_length=len(article.text),
        published_at=article.published_at,
        media_urls=article.media_urls,
        strategy=article.strategy,
        warnings=outcome.warnings,
        duration_ms=outcome.duration_ms,
        content_html_length=len(article.content_html),
    )


__all__ = [
    "MIN_CONTENT_LENGTH",
    "PREVIEW_LENGTH",
    "STEALTH_AVAILABLE",
    "BrowserPool",
    "CrawlPreview",
    "EmptyContentError",
    "NavigationTimeoutError",
    "ScrapedArticle",
    "ScraperError",
    "close_browser_pool",
    "discover_links",
    "get_browser_pool",
    "scrape_article",
    "test_crawl_url",
]
