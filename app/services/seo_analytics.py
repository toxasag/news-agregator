# ruff: noqa: BLE001, PLR0912, PLR0915, S110, SIM105
"""SEO analytics: Yandex Metrika, Google Search Console, and the optimisation queue.

The pipeline publishes; this module measures the result and says what to fix. Two
data sources, because they answer different questions:

* **Search Console** knows what a page ranks for — impressions, clicks, average
  position and CTR per URL. This is what identifies an article that is *almost*
  ranking.
* **Metrika** knows what visitors did once they arrived — pageviews, bounce rate,
  time on page. This is what distinguishes an article that attracts clicks and
  satisfies readers from one that attracts clicks and disappoints them.

**The watcher targets positions 4-15 with low CTR, and that band is the whole
point.** A page at position 1-3 has little headroom; a page past 20 will not be
rescued by a better headline, because nobody sees it. Positions 4-15 are already
on page one or two, so the impressions exist and the *only* thing suppressing
clicks is the snippet — the headline and the meta description, which are precisely
what this pipeline can regenerate cheaply. Optimising anything else first would
spend AI budget where it cannot pay back.

**CTR is compared against a position-aware expectation, not a flat threshold.**
A 3% CTR at position 4 is a failure; the same 3% at position 15 is normal. A single
flat cut-off would flood the queue with deep-ranking pages that are performing as
expected and bury the genuinely underperforming ones. :data:`EXPECTED_CTR_BY_POSITION`
encodes the industry-observed curve, and the requirement's 3% is kept as an
absolute ceiling so nothing above it is ever queued.

**Both clients degrade rather than fail.** An unconfigured or unreachable analytics
API returns an empty report, because the dashboard must still render: SEO data is
an input to a decision, not a dependency of the application.

Neither client writes to the database. The queue is returned as values; scheduling
the regeneration is the caller's job.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "EXPECTED_CTR_BY_POSITION",
    "GSC_API_BASE_URL",
    "MAX_CTR_FOR_OPTIMISATION",
    "METRIKA_API_BASE_URL",
    "MIN_IMPRESSIONS",
    "POSITION_WINDOW",
    "GSCClient",
    "LandingPage",
    "MetrikaClient",
    "OptimisationCandidate",
    "SEOAnalyticsError",
    "SEOReport",
    "build_optimisation_queue",
    "close_seo_clients",
    "collect_seo_report",
    "collect_seo_report_from_db",
    "expected_ctr",
    "needs_optimisation",
]

METRIKA_API_BASE_URL: Final[str] = "https://api-metrika.yandex.net"
GSC_API_BASE_URL: Final[str] = "https://www.googleapis.com"

REQUEST_TIMEOUT_SECONDS: Final[float] = 45.0

#: The optimisation window, inclusive. Below 4 there is little headroom; past 15 a
#: better snippet cannot help because the page is not being seen.
POSITION_WINDOW: Final[tuple[float, float]] = (4.0, 15.0)

#: Absolute CTR ceiling from the requirement. Nothing at or above this is queued
#: even if the position-aware expectation would flag it.
MAX_CTR_FOR_OPTIMISATION: Final[float] = 0.03

#: Minimum impressions before a page is judged. A URL with nine impressions and
#: zero clicks has a 0% CTR that means nothing: the sample is too small to
#: distinguish a bad snippet from chance, and regenerating on that signal would
#: spend tokens on noise.
MIN_IMPRESSIONS: Final[int] = 10

#: Observed average CTR by average position. Used to decide whether a page is
#: underperforming *for where it ranks*, which a flat threshold cannot express.
EXPECTED_CTR_BY_POSITION: Final[Mapping[int, float]] = {
    1: 0.270,
    2: 0.155,
    3: 0.110,
    4: 0.080,
    5: 0.060,
    6: 0.048,
    7: 0.040,
    8: 0.033,
    9: 0.028,
    10: 0.025,
    11: 0.021,
    12: 0.018,
    13: 0.016,
    14: 0.014,
    15: 0.012,
}

#: A page is flagged when its CTR falls below this fraction of the expectation for
#: its position. At 0.6 a page must be meaningfully behind its peers, not merely
#: on the wrong side of a rounding error.
CTR_SHORTFALL_RATIO: Final[float] = 0.6

#: Default reporting window. Four weeks smooths weekday/weekend swings while
#: staying recent enough that the data reflects the current snippet.
DEFAULT_WINDOW_DAYS: Final[int] = 28

#: Rows requested per query. Well inside GSC's 25 000 ceiling and Metrika's
#: practical limit, and far more than one operator will read.
DEFAULT_ROW_LIMIT: Final[int] = 500

#: Candidates returned per run. The queue is a work list for an operator and a
#: budget bound for the regeneration loop, not an export.
MAX_QUEUE_SIZE: Final[int] = 25


class SEOAnalyticsError(RuntimeError):
    """An analytics API could not be read.

    Raised internally and converted to an empty report at the boundary: a failed
    metrics fetch must not take the dashboard down with it.
    """


@dataclass(frozen=True, slots=True)
class LandingPage:
    """Search performance for one URL over the reporting window."""

    url: str
    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0
    position: float = 0.0
    #: Metrika metrics, present only when the two sources could be joined.
    pageviews: int = 0
    bounce_rate: float = 0.0
    avg_time_seconds: float = 0.0

    @property
    def path(self) -> str:
        """The URL's path, which is how the two sources are joined.

        Metrika reports a bare path while GSC reports an absolute URL, so the path
        is the only key both agree on.
        """
        return urlparse(self.url).path or "/"


@dataclass(frozen=True, slots=True)
class OptimisationCandidate:
    """One article worth a snippet refresh, with the reason it was chosen."""

    url: str
    position: float
    ctr: float
    impressions: int
    clicks: int
    expected_ctr: float
    #: Clicks per window this page would gain at the expected CTR. This is what
    #: orders the queue: fixing a page with 10 000 impressions matters more than
    #: fixing one with 60, at the same shortfall.
    potential_clicks: int
    reason: str
    bounce_rate: float = 0.0

    @property
    def shortfall(self) -> float:
        """How far below expectation this page's CTR sits, as a fraction."""
        if self.expected_ctr <= 0:
            return 0.0
        return max(0.0, 1.0 - (self.ctr / self.expected_ctr))

    def to_dict(self) -> dict[str, Any]:
        """Render for the dashboard and for an ARQ job payload."""
        return {
            "url": self.url,
            "position": round(self.position, 1),
            "ctr": round(self.ctr, 4),
            "expected_ctr": round(self.expected_ctr, 4),
            "shortfall": round(self.shortfall, 3),
            "impressions": self.impressions,
            "clicks": self.clicks,
            "potential_clicks": self.potential_clicks,
            "bounce_rate": round(self.bounce_rate, 3),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SEOReport:
    """Everything the SEO page renders."""

    pages: tuple[LandingPage, ...] = ()
    queue: tuple[OptimisationCandidate, ...] = ()
    metrika_available: bool = False
    gsc_available: bool = False
    metrika_dzen_available: bool = False
    dzen_pageviews: int = 0
    site_pageviews: int = 0
    yandex_available: bool = False
    yandex_sqi: int = 0
    yandex_pages_in_search: int = 0
    bing_available: bool = False
    indexnow_active: bool = False
    queries: tuple[dict[str, Any], ...] = ()
    ga4_available: bool = False
    ga4_users: int = 0
    ga4_sessions: int = 0
    ga4_pageviews: int = 0
    ga4_bounce_rate_pct: float = 0.0
    ga4_avg_duration_sec: float = 0.0
    yandex_recrawl_quota: int = 0
    yandex_recrawl_remainder: int = 0
    yandex_diagnostics: dict[str, Any] = field(default_factory=dict)
    yandex_sitemaps: tuple[dict[str, Any], ...] = ()
    window_days: int = DEFAULT_WINDOW_DAYS
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_clicks(self) -> int:
        return sum(page.clicks for page in self.pages)

    @property
    def total_impressions(self) -> int:
        return sum(page.impressions for page in self.pages)

    @property
    def average_ctr(self) -> float:
        """Site-wide CTR, computed from totals rather than averaged per page.

        Averaging per-page CTRs would weight a page with 10 impressions the same
        as one with 10 000 and produce a number that matches no report anywhere.
        """
        impressions = self.total_impressions
        return (self.total_clicks / impressions) if impressions else 0.0

    @property
    def average_position(self) -> float:
        """Impression-weighted average position, which is how GSC reports it."""
        impressions = self.total_impressions
        if not impressions:
            return 0.0
        weighted = sum(page.position * page.impressions for page in self.pages)
        return weighted / impressions

    def to_dict(self) -> dict[str, Any]:
        """Render the summary the dashboard displays."""
        return {
            "window_days": self.window_days,
            "pages": len(self.pages),
            "total_clicks": self.total_clicks,
            "total_impressions": self.total_impressions,
            "average_ctr": round(self.average_ctr, 4),
            "average_position": round(self.average_position, 1),
            "queue_size": len(self.queue),
            "metrika_available": self.metrika_available,
            "gsc_available": self.gsc_available,
            "metrika_dzen_available": self.metrika_dzen_available,
            "dzen_pageviews": self.dzen_pageviews,
            "site_pageviews": self.site_pageviews,
            "yandex_available": self.yandex_available,
            "yandex_sqi": self.yandex_sqi,
            "yandex_pages_in_search": self.yandex_pages_in_search,
            "bing_available": self.bing_available,
            "indexnow_active": self.indexnow_active,
            "queries_count": len(self.queries),
            "ga4_available": self.ga4_available,
            "ga4_users": self.ga4_users,
            "ga4_sessions": self.ga4_sessions,
            "ga4_pageviews": self.ga4_pageviews,
            "ga4_bounce_rate_pct": self.ga4_bounce_rate_pct,
            "ga4_avg_duration_sec": self.ga4_avg_duration_sec,
            "yandex_recrawl_quota": self.yandex_recrawl_quota,
            "yandex_recrawl_remainder": self.yandex_recrawl_remainder,
            "yandex_diagnostics": self.yandex_diagnostics,
            "yandex_sitemaps": self.yandex_sitemaps,
            "warnings": list(self.warnings),
        }


def expected_ctr(position: float) -> float:
    """Return the CTR normally observed at ``position``.

    Interpolated between the tabulated integers, because an average position is a
    real number: a page at 4.5 sits between the position-4 and position-5
    expectations, and snapping it to either would misjudge it. Outside the table
    the nearest endpoint is used.
    """
    if position <= 1:
        return EXPECTED_CTR_BY_POSITION[1]
    lowest = min(EXPECTED_CTR_BY_POSITION)
    highest = max(EXPECTED_CTR_BY_POSITION)
    if position >= highest:
        return EXPECTED_CTR_BY_POSITION[highest]

    lower = int(position)
    upper = min(lower + 1, highest)
    lower = max(lower, lowest)
    if lower == upper:
        return EXPECTED_CTR_BY_POSITION[lower]
    weight = position - lower
    return EXPECTED_CTR_BY_POSITION[lower] * (1 - weight) + (
        EXPECTED_CTR_BY_POSITION[upper] * weight
    )


def needs_optimisation(page: LandingPage) -> str | None:
    """Decide whether one page belongs in the queue, and why.

    Returns the reason as a human-readable string, or ``None`` when the page is
    performing acceptably. Four gates, in the order that rejects most cheaply:

    1. The position must sit inside :data:`POSITION_WINDOW`.
    2. Impressions must reach :data:`MIN_IMPRESSIONS`, or the CTR is noise.
    3. The CTR must be below the absolute :data:`MAX_CTR_FOR_OPTIMISATION`.
    4. The CTR must be below :data:`CTR_SHORTFALL_RATIO` of what its position
       normally earns — so a page performing normally for where it ranks is not
       queued merely for ranking deep.
    """
    low, high = POSITION_WINDOW
    if not low <= page.position <= high:
        return None
    if page.impressions < MIN_IMPRESSIONS:
        return None
    if page.ctr >= MAX_CTR_FOR_OPTIMISATION:
        return None

    baseline = expected_ctr(page.position)
    if page.ctr >= baseline * CTR_SHORTFALL_RATIO:
        return None

    return (
        f"позиция {page.position:.1f}, CTR {page.ctr * 100:.1f}% против "
        f"ожидаемых {baseline * 100:.1f}% — сниппет не отрабатывает показы"
    )


def build_optimisation_queue(
    pages: Sequence[LandingPage], *, limit: int = MAX_QUEUE_SIZE
) -> tuple[OptimisationCandidate, ...]:
    """Select and rank the articles worth a snippet refresh.

    Ordered by **potential clicks gained**, not by shortfall percentage. A page
    missing 90% of its expected clicks on 60 impressions is a worse use of the
    regeneration budget than one missing 40% on 20 000, and ranking by percentage
    would put them the wrong way round.
    """
    candidates: list[OptimisationCandidate] = []
    for page in pages:
        reason = needs_optimisation(page)
        if reason is None:
            continue
        baseline = expected_ctr(page.position)
        potential = max(int(page.impressions * baseline) - page.clicks, 0)
        candidates.append(
            OptimisationCandidate(
                url=page.url,
                position=page.position,
                ctr=page.ctr,
                impressions=page.impressions,
                clicks=page.clicks,
                expected_ctr=baseline,
                potential_clicks=potential,
                reason=reason,
                bounce_rate=page.bounce_rate,
            )
        )

    candidates.sort(key=lambda item: item.potential_clicks, reverse=True)
    return tuple(candidates[:limit])


def _window(days: int) -> tuple[str, str]:
    """Return an inclusive ``(start, end)`` date pair.

    Ends *yesterday*: both APIs finalise a day's data after it closes, so
    including today would report a partial figure and make every fresh article
    look like it is underperforming.
    """
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=max(days, 1) - 1)
    return start.isoformat(), end.isoformat()


class MetrikaClient:
    """Reads landing-page behaviour from the Yandex Metrika reporting API."""

    __slots__ = ("_base_url", "_client", "_counter_id", "_token", "_transport")

    def __init__(
        self,
        *,
        token: str | None = None,
        counter_id: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = (
            settings.yandex_metrika_token if token is None else token
        ).strip()
        self._counter_id = (
            settings.yandex_metrika_counter_id if counter_id is None else counter_id
        ).strip()
        self._base_url = (base_url or METRIKA_API_BASE_URL).rstrip("/")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        """Whether a token and a counter id are both available."""
        return bool(self._token and self._counter_id)

    async def close(self) -> None:
        """Close the pooled HTTP client, if one was created."""
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if not self.is_configured:
            raise SEOAnalyticsError(
                "YANDEX_METRIKA_TOKEN and YANDEX_METRIKA_COUNTER_ID are required"
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                # Metrika uses the legacy OAuth scheme, not Bearer.
                headers={"Authorization": f"OAuth {self._token}"},
                transport=self._transport,
            )
        return self._client

    async def top_landing_pages(
        self, *, days: int = DEFAULT_WINDOW_DAYS, limit: int = DEFAULT_ROW_LIMIT
    ) -> dict[str, LandingPage]:
        """Return behaviour metrics keyed by URL path.

        Keyed by *path* rather than by full URL because that is the only form both
        sources agree on — Metrika reports a path, GSC an absolute URL.
        """
        client = self._ensure_client()
        start, end = _window(days)
        params = {
            "ids": self._counter_id,
            "date1": start,
            "date2": end,
            "dimensions": "ym:s:startURLPath",
            "metrics": "ym:s:pageviews,ym:s:bounceRate,ym:s:avgVisitDurationSeconds",
            "sort": "-ym:s:pageviews",
            "limit": str(limit),
            # Organic only: direct and referral traffic says nothing about how a
            # search snippet is performing.
            "filters": "ym:s:trafficSource=='organic'",
        }
        try:
            response = await client.get("/stat/v1/data", params=params)
        except httpx.HTTPError as exc:
            raise SEOAnalyticsError(
                f"Metrika request failed: {type(exc).__name__}"
            ) from exc

        if response.status_code >= httpx.codes.BAD_REQUEST:
            detail = response.text[:200].replace("\n", " ")
            raise SEOAnalyticsError(
                f"Metrika returned HTTP {response.status_code}: {detail}"
            )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise SEOAnalyticsError("Metrika returned a non-JSON body") from exc
        return _parse_metrika(payload)

    async def total_pageviews(self, *, days: int = DEFAULT_WINDOW_DAYS) -> int:
        """Fetch total pageviews for the counter in the given window."""
        client = self._ensure_client()
        start, end = _window(days)
        params = {
            "ids": self._counter_id,
            "date1": start,
            "date2": end,
            "metrics": "ym:pv:pageviews",
        }
        try:
            response = await client.get("/stat/v1/data", params=params)
            if response.status_code == httpx.codes.OK:
                data = response.json()
                totals = data.get("totals", [])
                if totals:
                    return int(totals[0])
            return 0
        except (httpx.HTTPError, ValueError, KeyError):
            return 0

    async def close_if_open(self) -> None:
        """Alias kept for symmetry with the other clients' shutdown hooks."""
        await self.close()


def _parse_metrika(payload: Any) -> dict[str, LandingPage]:
    """Parse a Metrika ``stat/v1/data`` response into per-path metrics.

    Every field is read defensively. This is a third-party payload whose shape can
    change between API versions, and a malformed row must cost that row rather than
    the whole report.
    """
    if not isinstance(payload, Mapping):
        return {}
    rows = payload.get("data")
    if not isinstance(rows, list):
        return {}

    parsed: dict[str, LandingPage] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        dimensions = row.get("dimensions")
        metrics = row.get("metrics")
        if not isinstance(dimensions, list) or not dimensions:
            continue
        if not isinstance(metrics, list) or not metrics:
            continue
        first = dimensions[0]
        if not isinstance(first, Mapping):
            continue
        raw_path = first.get("name") or first.get("id") or ""
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = urlparse(raw_path).path or raw_path

        values = [value if isinstance(value, (int, float)) else 0.0 for value in metrics]
        pageviews = int(values[0]) if values else 0
        bounce = float(values[1]) / 100.0 if len(values) > 1 else 0.0
        seconds = float(values[2]) if len(values) > 2 else 0.0

        parsed[path] = LandingPage(
            url=path,
            pageviews=pageviews,
            # Metrika reports the bounce rate as a percentage; GSC-style fractions
            # are used everywhere else in this module, so it is normalised here.
            bounce_rate=bounce,
            avg_time_seconds=seconds,
        )
    return parsed


class GSCClient:
    """Reads search performance from the Google Search Console API.

    ``access_token`` is supplied by the caller rather than minted here. A service
    account JWT exchange needs the ``google-auth`` dependency, which the project
    does not carry; accepting a token keeps this client usable with whatever
    credential flow a deployment has and keeps the parsing — the part worth
    testing — independent of it.
    """

    __slots__ = ("_base_url", "_client", "_site_url", "_token", "_transport")

    def __init__(
        self,
        *,
        access_token: str = "",
        site_url: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = access_token.strip()
        self._site_url = (
            settings.gsc_site_url if site_url is None else site_url
        ).strip()
        self._base_url = (base_url or GSC_API_BASE_URL).rstrip("/")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        """Whether a site URL and an access token are both available."""
        return bool(self._site_url and self._token)

    async def close(self) -> None:
        """Close the pooled HTTP client, if one was created."""
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if not self.is_configured:
            raise SEOAnalyticsError(
                "GSC_SITE_URL and an OAuth access token are required"
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                transport=self._transport,
            )
        return self._client

    async def page_performance(
        self, *, days: int = DEFAULT_WINDOW_DAYS, limit: int = DEFAULT_ROW_LIMIT
    ) -> list[LandingPage]:
        """Return per-URL clicks, impressions, CTR and average position."""
        client = self._ensure_client()
        start, end = _window(days)
        # The property id goes in the path and can be `sc-domain:example.com`, so
        # it must be percent-encoded — including the colon.
        encoded = quote(self._site_url, safe="")
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": ["page"],
            "type": "web",
            "rowLimit": limit,
            # By page, since the whole report is per-article.
            "aggregationType": "byPage",
        }
        try:
            response = await client.post(
                f"/webmasters/v3/sites/{encoded}/searchAnalytics/query", json=body
            )
        except httpx.HTTPError as exc:
            raise SEOAnalyticsError(
                f"Search Console request failed: {type(exc).__name__}"
            ) from exc

        if response.status_code >= httpx.codes.BAD_REQUEST:
            detail = response.text[:200].replace("\n", " ")
            raise SEOAnalyticsError(
                f"Search Console returned HTTP {response.status_code}: {detail}"
            )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise SEOAnalyticsError(
                "Search Console returned a non-JSON body"
            ) from exc
        return _parse_gsc(payload)

    async def top_queries(
        self, *, days: int = DEFAULT_WINDOW_DAYS, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Return top search queries from Search Console."""
        client = self._ensure_client()
        start, end = _window(days)
        encoded = quote(self._site_url, safe="")
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": ["query"],
            "type": "web",
            "rowLimit": limit,
        }
        try:
            response = await client.post(
                f"/webmasters/v3/sites/{encoded}/searchAnalytics/query", json=body
            )
            if response.status_code >= httpx.codes.BAD_REQUEST:
                return []
            payload = response.json()
            rows = payload.get("rows", [])
            queries: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                keys = row.get("keys")
                if not isinstance(keys, list) or not keys or not isinstance(keys[0], str):
                    continue
                queries.append({
                    "query": keys[0],
                    "clicks": int(row.get("clicks", 0)),
                    "impressions": int(row.get("impressions", 0)),
                    "ctr": round(float(row.get("ctr", 0.0)), 4),
                    "position": round(float(row.get("position", 0.0)), 1),
                })
            return queries
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("GSC top_queries failed: %s", exc)
            return []


def _parse_gsc(payload: Any) -> list[LandingPage]:
    """Parse a Search Console ``searchAnalytics/query`` response.

    ``ctr`` arrives as a 0..1 fraction and is kept that way throughout this module;
    converting to a percentage here would leave two units in play and guarantee a
    factor-of-100 bug in the threshold comparison.
    """
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return []

    pages: list[LandingPage] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        keys = row.get("keys")
        if not isinstance(keys, list) or not keys:
            continue
        url = keys[0]
        if not isinstance(url, str) or not url:
            continue

        clicks = row.get("clicks")
        impressions = row.get("impressions")
        ctr = row.get("ctr")
        position = row.get("position")
        pages.append(
            LandingPage(
                url=url,
                clicks=int(clicks) if isinstance(clicks, (int, float)) else 0,
                impressions=(
                    int(impressions) if isinstance(impressions, (int, float)) else 0
                ),
                ctr=float(ctr) if isinstance(ctr, (int, float)) else 0.0,
                position=float(position) if isinstance(position, (int, float)) else 0.0,
            )
        )
    return pages


def _merge(
    search_pages: Sequence[LandingPage], behaviour: Mapping[str, LandingPage]
) -> tuple[LandingPage, ...]:
    """Join search performance with behaviour metrics on the URL path.

    GSC is the authoritative side: a page with no Metrika row still has search
    data worth acting on, so it is kept with zeroed behaviour metrics rather than
    dropped by an inner join.
    """
    merged: list[LandingPage] = []
    for page in search_pages:
        extra = behaviour.get(page.path)
        if extra is None:
            merged.append(page)
            continue
        merged.append(
            LandingPage(
                url=page.url,
                clicks=page.clicks,
                impressions=page.impressions,
                ctr=page.ctr,
                position=page.position,
                pageviews=extra.pageviews,
                bounce_rate=extra.bounce_rate,
                avg_time_seconds=extra.avg_time_seconds,
            )
        )
    return tuple(merged)


def _merge_search_engines(
    gsc_pages: Sequence[LandingPage], yandex_pages: Sequence[LandingPage]
) -> list[LandingPage]:
    """Combine page-level performance from Google Search Console and Yandex Webmaster."""
    merged_map: dict[str, dict[str, Any]] = {}
    for p in gsc_pages:
        merged_map[p.path] = {
            "url": p.url,
            "clicks": p.clicks,
            "impressions": p.impressions,
            "weighted_pos": p.position * p.impressions,
        }
    for p in yandex_pages:
        if p.path in merged_map:
            merged_map[p.path]["clicks"] += p.clicks
            merged_map[p.path]["impressions"] += p.impressions
            merged_map[p.path]["weighted_pos"] += p.position * p.impressions
        else:
            merged_map[p.path] = {
                "url": p.url,
                "clicks": p.clicks,
                "impressions": p.impressions,
                "weighted_pos": p.position * p.impressions,
            }
    res: list[LandingPage] = []
    for _path, data in merged_map.items():
        imp = data["impressions"]
        clk = data["clicks"]
        avg_p = (data["weighted_pos"] / imp) if imp > 0 else 0.0
        ctr_v = (clk / imp) if imp > 0 else 0.0
        res.append(
            LandingPage(
                url=data["url"],
                clicks=clk,
                impressions=imp,
                ctr=round(ctr_v, 4),
                position=round(avg_p, 1),
            )
        )
    return res


_shared_metrika: MetrikaClient | None = None


def get_metrika_client() -> MetrikaClient:
    """Return the process-wide Metrika client, creating it on first use."""
    global _shared_metrika  # noqa: PLW0603 - module-level singleton accessor
    if _shared_metrika is None:
        _shared_metrika = MetrikaClient()
    return _shared_metrika


async def close_seo_clients() -> None:
    """Release the shared analytics clients. Call on shutdown."""
    global _shared_metrika  # noqa: PLW0603 - module-level singleton accessor
    client = _shared_metrika
    _shared_metrika = None
    if client is not None:
        await client.close()


async def collect_seo_report(
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    metrika: MetrikaClient | None = None,
    gsc: GSCClient | None = None,
) -> SEOReport:
    """Gather both sources and build the optimisation queue.

    Never raises for an analytics failure. Each source is fetched independently and
    a failure is recorded as a warning, so an expired Metrika token degrades the
    report to search-only rather than making the SEO page unreachable — which is
    where an operator would go to find out what is wrong.
    """
    warnings: list[str] = []
    behaviour: dict[str, LandingPage] = {}
    search_pages: list[LandingPage] = []

    metrika_client = metrika if metrika is not None else get_metrika_client()
    metrika_ok = False
    if metrika_client.is_configured:
        try:
            behaviour = await metrika_client.top_landing_pages(days=days)
            metrika_ok = True
        except SEOAnalyticsError as exc:
            warnings.append(f"Метрика недоступна: {exc}")
    else:
        warnings.append("Яндекс.Метрика не настроена: нет токена или номера счётчика")

    gsc_ok = False
    if gsc is not None and gsc.is_configured:
        try:
            search_pages = await gsc.page_performance(days=days)
            gsc_ok = True
        except SEOAnalyticsError as exc:
            warnings.append(f"Search Console недоступен: {exc}")
    else:
        warnings.append(
            "Search Console не настроен: нужен GSC_SITE_URL и OAuth-токен"
        )

    pages = _merge(search_pages, behaviour)
    return SEOReport(
        pages=pages,
        queue=build_optimisation_queue(pages),
        metrika_available=metrika_ok,
        gsc_available=gsc_ok,
        window_days=days,
        warnings=tuple(warnings),
    )


async def collect_seo_report_from_db(
    session: AsyncSession,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
) -> SEOReport:
    """Gather analytics using credentials stored in PostgreSQL system_settings."""
    from app.services.bing_connection import load_bing_connection  # noqa: PLC0415
    from app.services.google_connection import (  # noqa: PLC0415
        fetch_ga4_report,
        get_google_access_token,
        load_google_connection,
    )
    from app.services.yandex_connection import (  # noqa: PLC0415
        fetch_yandex_diagnostics,
        fetch_yandex_sitemaps,
        get_yandex_recrawl_quota,
        load_yandex_connection,
    )

    warnings: list[str] = []
    y_cfg = await load_yandex_connection(session)
    g_cfg = await load_google_connection(session)
    b_cfg = await load_bing_connection(session)

    # 1. Metrika Site
    metrika_client: MetrikaClient | None = None
    site_pageviews = 0
    if y_cfg.has_metrika:
        metrika_client = MetrikaClient(
            token=y_cfg.metrika_oauth_token,
            counter_id=y_cfg.metrika_counter_id,
        )
        try:
            site_pageviews = await metrika_client.total_pageviews(days=days)
        except Exception as mp_err:
            logger.warning("Site metrika total_pageviews failed: %s", mp_err)
    elif settings.yandex_metrika_token and settings.yandex_metrika_counter_id:
        metrika_client = get_metrika_client()
        try:
            site_pageviews = await metrika_client.total_pageviews(days=days)
        except Exception:
            pass

    # 2. Metrika Dzen
    dzen_pageviews = 0
    dzen_available = False
    if y_cfg.has_metrika_dzen:
        dzen_client = MetrikaClient(
            token=y_cfg.metrika_oauth_token,
            counter_id=y_cfg.metrika_dzen_counter_id,
        )
        try:
            dzen_pageviews = await dzen_client.total_pageviews(days=days)
            dzen_available = True
        except (SEOAnalyticsError, httpx.HTTPError, ValueError):
            logger.warning("Dzen metrika query failed")

    # 3. Yandex Webmaster
    yandex_available = False
    yandex_sqi = 0
    yandex_pages_in_search = 0
    yandex_queries_list: list[dict[str, Any]] = []
    yandex_landing_pages: dict[str, dict[str, Any]] = {}
    yandex_recrawl_q = 0
    yandex_recrawl_rem = 0
    yandex_diag: dict[str, Any] = {}
    yandex_sm_list: list[dict[str, Any]] = []

    if y_cfg.has_webmaster_credentials and y_cfg.is_webmaster_active:
        token = y_cfg.webmaster_token.strip()
        host_id = y_cfg.webmaster_host_id.strip() or "https:svodka.press:443"
        try:
            headers = {"Authorization": f"OAuth {token}", "Accept": "application/json"}
            async with httpx.AsyncClient(timeout=10.0) as wm_client:
                r_usr = await wm_client.get(
                    "https://api.webmaster.yandex.net/v4/user/", headers=headers
                )
                if r_usr.status_code == 200:
                    user_id = r_usr.json().get("user_id")
                    yandex_available = True
                    r_sum = await wm_client.get(
                        f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/{host_id}/summary",
                        headers=headers,
                    )
                    if r_sum.status_code == 200:
                        sum_data = r_sum.json()
                        yandex_sqi = int(sum_data.get("sqi") or 0)
                        yandex_pages_in_search = int(
                            sum_data.get("searchable_pages_count") or 0
                        )

                    # Ingest query analytics (queries, impressions, clicks, URLs)
                    try:
                        r_qa = await wm_client.post(
                            f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/{host_id}/query-analytics/list",
                            json={"limit": 200, "offset": 0},
                            headers=headers,
                        )
                        if r_qa.status_code == 200:
                            qa_data = (
                                r_qa.json().get("text_indicator_to_statistics") or []
                            )
                            for qa_item in qa_data:
                                q_val = (
                                    (qa_item.get("text_indicator") or {}).get("value")
                                    or ""
                                )
                                comp_ind = (
                                    qa_item.get("popular_complementary_indicator") or {}
                                )
                                url_val = comp_ind.get("value") or ""
                                stats = qa_item.get("statistics") or []
                                shows = int(
                                    sum(
                                        s.get("value", 0)
                                        for s in stats
                                        if s.get("field") == "IMPRESSIONS"
                                    )
                                )
                                clicks = int(
                                    sum(
                                        s.get("value", 0)
                                        for s in stats
                                        if s.get("field") == "CLICKS"
                                    )
                                )
                                pos_list = [
                                    float(s.get("value", 0))
                                    for s in stats
                                    if s.get("field") == "POSITION"
                                    and float(s.get("value", 0)) > 0
                                ]
                                pos = (
                                    (sum(pos_list) / len(pos_list)) if pos_list else 0.0
                                )
                                ctr = (clicks / shows) if shows > 0 else 0.0

                                if q_val:
                                    yandex_queries_list.append({
                                        "query": q_val,
                                        "clicks": clicks,
                                        "impressions": shows,
                                        "ctr": round(ctr, 4),
                                        "position": round(pos, 1) if pos > 0 else 0.0,
                                        "source": "Yandex",
                                    })
                                if url_val:
                                    if url_val not in yandex_landing_pages:
                                        yandex_landing_pages[url_val] = {
                                            "impressions": 0,
                                            "clicks": 0,
                                            "positions": [],
                                        }
                                    yandex_landing_pages[url_val]["impressions"] += shows
                                    yandex_landing_pages[url_val]["clicks"] += clicks
                                    if pos > 0:
                                        yandex_landing_pages[url_val]["positions"].append(
                                            pos
                                        )
                    except Exception as qa_exc:
                        logger.warning("Yandex query-analytics failed: %s", qa_exc)

                    # Fallback to popular queries if query-analytics returned nothing
                    if not yandex_queries_list:
                        r_q = await wm_client.get(
                            f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/{host_id}/search-queries/popular?order_by=TOTAL_SHOWS",
                            headers=headers,
                        )
                        if r_q.status_code == 200:
                            q_data = r_q.json().get("queries") or []
                            for q_item in q_data:
                                inds = q_item.get("indicators") or {}
                                shows = int(inds.get("TOTAL_SHOWS") or 0)
                                clicks = int(inds.get("TOTAL_CLICKS") or 0)
                                pos = float(
                                    inds.get("AVG_SHOW_POSITION")
                                    or inds.get("AVG_CLICK_POSITION")
                                    or 0.0
                                )
                                ctr = (clicks / shows) if shows > 0 else 0.0
                                yandex_queries_list.append({
                                    "query": q_item.get("query_text") or "",
                                    "clicks": clicks,
                                    "impressions": shows,
                                    "ctr": round(ctr, 4),
                                    "position": round(pos, 1) if pos > 0 else 0.0,
                                    "source": "Yandex",
                                })

            quota_res = await get_yandex_recrawl_quota(y_cfg)
            if quota_res.get("ok"):
                yandex_recrawl_q = quota_res.get("daily_quota", 0)
                yandex_recrawl_rem = quota_res.get("quota_remainder", 0)

            diag_res = await fetch_yandex_diagnostics(y_cfg)
            if diag_res.get("ok"):
                yandex_diag = diag_res

            yandex_sm_list = await fetch_yandex_sitemaps(y_cfg)

        except Exception as wm_exc:
            logger.warning("Yandex Webmaster query failed: %s", wm_exc)

    # 4. Google Search Console
    gsc_client: GSCClient | None = None
    if g_cfg.is_ready_for_gsc:
        try:
            gsc_token = await get_google_access_token(g_cfg)
            gsc_client = GSCClient(
                access_token=gsc_token,
                site_url=g_cfg.gsc_site_url,
            )
        except (httpx.HTTPError, ValueError, KeyError) as g_err:
            warnings.append(f"Google Search Console: {g_err}")
    elif settings.gsc_site_url:
        pass

    # 5. Google Analytics 4
    ga4_data = await fetch_ga4_report(g_cfg, days=days)

    # 6. Top Queries
    top_queries_list: list[dict[str, Any]] = []
    if gsc_client and gsc_client.is_configured:
        try:
            raw_gsc_q = await gsc_client.top_queries(days=days)
            for g_item in raw_gsc_q:
                top_queries_list.append({**g_item, "source": "Google"})
        except (httpx.HTTPError, ValueError, KeyError) as q_err:
            logger.warning("GSC top queries query failed: %s", q_err)

    all_queries_list = sorted(
        [*yandex_queries_list, *top_queries_list],
        key=lambda x: (x.get("impressions") or 0, x.get("clicks") or 0),
        reverse=True,
    )

    # 7. Merge Landing Pages from Yandex Webmaster, GSC, and Metrika
    yandex_search_pages: list[LandingPage] = []
    for p_path, p_info in yandex_landing_pages.items():
        imp = p_info["impressions"]
        clk = p_info["clicks"]
        p_list = p_info["positions"]
        avg_pos = (sum(p_list) / len(p_list)) if p_list else 0.0
        ctr_val = (clk / imp) if imp > 0 else 0.0
        full_u = f"https://svodka.press{p_path}" if p_path.startswith("/") else f"https://svodka.press/{p_path}"
        yandex_search_pages.append(
            LandingPage(
                url=full_u,
                clicks=clk,
                impressions=imp,
                ctr=round(ctr_val, 4),
                position=round(avg_pos, 1),
            )
        )

    behaviour: dict[str, LandingPage] = {}
    metrika_ok = False
    if metrika_client and metrika_client.is_configured:
        try:
            behaviour = await metrika_client.top_landing_pages(days=days)
            metrika_ok = True
        except Exception as m_exc:
            warnings.append(f"Метрика недоступна: {m_exc}")

    raw_gsc_pages: list[LandingPage] = []
    gsc_ok = False
    if gsc_client and gsc_client.is_configured:
        try:
            raw_gsc_pages = await gsc_client.page_performance(days=days)
            gsc_ok = True
        except Exception as g_exc:
            warnings.append(f"Search Console недоступен: {g_exc}")

    combined_search_pages = _merge_search_engines(raw_gsc_pages, yandex_search_pages)
    final_pages = _merge(combined_search_pages, behaviour)
    final_queue = build_optimisation_queue(final_pages)

    all_warnings = warnings
    combined_warnings = list(dict.fromkeys(all_warnings))

    return SEOReport(
        pages=final_pages,
        queue=final_queue,
        metrika_available=metrika_ok or (site_pageviews > 0),
        gsc_available=gsc_ok,
        metrika_dzen_available=dzen_available,
        dzen_pageviews=dzen_pageviews,
        site_pageviews=site_pageviews,
        yandex_available=yandex_available,
        yandex_sqi=yandex_sqi,
        yandex_pages_in_search=yandex_pages_in_search,
        bing_available=b_cfg.has_api_key,
        indexnow_active=b_cfg.is_indexnow_active,
        queries=tuple(all_queries_list),
        ga4_available=bool(ga4_data.get("available")),
        ga4_users=int(ga4_data.get("active_users", 0)),
        ga4_sessions=int(ga4_data.get("sessions", 0)),
        ga4_pageviews=int(ga4_data.get("screen_pageviews", 0)),
        ga4_bounce_rate_pct=float(ga4_data.get("bounce_rate_pct", 0.0)),
        ga4_avg_duration_sec=float(ga4_data.get("avg_duration_sec", 0.0)),
        yandex_recrawl_quota=yandex_recrawl_q,
        yandex_recrawl_remainder=yandex_recrawl_rem,
        yandex_diagnostics=yandex_diag,
        yandex_sitemaps=tuple(yandex_sm_list),
        window_days=days,
        warnings=tuple(combined_warnings),
    )

