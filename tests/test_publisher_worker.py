"""Tests for the publisher orchestrator worker.

The worker composes the WordPress publisher and the four social adapters, so these
tests are about *orchestration policy* rather than about the collaborators, which
have their own suites: what publishes first, what happens when one channel fails,
what a retry re-sends, and which terminal status the article lands on.

Every collaborator is patched at the worker's own module boundary. The database is a
purpose-built session double; `Article.platform_statuses` is a real
`NestedMutableDict` so the in-place assignment the worker performs is exercised the
way it behaves in production rather than against a plain dict that would accept
anything.
"""

# Fixtures are Russian editorial copy, so Cyrillic letters that look like Latin
# ones are the subject matter.
# ruff: noqa: RUF001

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Final, cast

import pytest
from arq.constants import default_queue_name
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.enums import (
    ArticleStatus,
    Platform,
    PlatformStatus,
    default_platform_statuses,
)
from app.services.social_base import BaseSocialAdapter, PublishResult
from app.services.wp_publisher import (
    PublishedPost,
    WPPublishAuthError,
    WPPublishResponseError,
)
from app.workers import content_worker, ingestion_worker, publisher_worker
from app.workers.publisher_worker import (
    MAX_ARTICLE_RETRIES,
    MAX_WP_ATTEMPTS,
    ArticlePublication,
    PublisherRunReport,
    article_lock_key,
    build_adapters,
    publish_article_everywhere,
)

TITLE: Final[str] = "В Москве открыли станцию метро «Пыхтино»"
BODY: Final[str] = (
    "<h2>Что открыли</h2>"
    "<p>Мэрия Москвы открыла станцию «Пыхтино» 14 марта 2026 года.</p>"
    "<ul><li>45 тысяч пассажиров в сутки</li></ul>"
    "<h2>Как строили</h2><p>Работы велись четыре года.</p>"
)
WP_URL: Final[str] = "https://svodkapress.test/2026/08/pyhtino/"
COVER_URL: Final[str] = "https://svodkapress.test/uploads/cover.jpg"


# ============================================================ session double


class FakeSession:
    """In-memory stand-in for `AsyncSession`.

    `commit` can be made to fail, which is how the rollback path is exercised
    without PostgreSQL.
    """

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.fail_commit: Exception | None = None

    def add(self, instance: Any) -> None:
        return None

    async def commit(self) -> None:
        if self.fail_commit is not None:
            raise self.fail_commit
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, instance: Any, attrs: Sequence[str] | None = None) -> None:
        return None

    async def close(self) -> None:
        return None

    def as_session(self) -> AsyncSession:
        """Return `self` typed as `AsyncSession` for the worker under test."""
        return cast(AsyncSession, self)


def build_article(**overrides: Any) -> Article:
    """Build a transient article at MEDIA_READY. Nothing is persisted."""
    values: dict[str, Any] = {
        "id": 5150,
        "cluster_id": uuid.uuid4(),
        "category": "novosti-obshhestva",
        "title": TITLE,
        "content_html": BODY,
        "seo_title": "Станция «Пыхтино» открыта",
        "seo_description": "Мэрия открыла станцию «Пыхтино».",
        "seo_keywords": ["станция пыхтино", "метро москвы"],
        "url_slug": "v-moskve-otkryli-stantsiyu-metro-pyhtino-abc123",
        "status": ArticleStatus.MEDIA_READY,
        # A real tracked JSONB dict: the worker assigns into it in place, and a
        # plain dict would accept that silently even if the tracking were broken.
        "platform_statuses": default_platform_statuses(),
        "video_links": [],
        "wp_image_id": 4242,
        "retry_count": 0,
    }
    values.update(overrides)
    return Article(**values)


# ============================================================== adapter stubs


class StubAdapter(BaseSocialAdapter):
    """A social adapter that answers from a script, without any network."""

    def __init__(
        self,
        platform: Platform,
        *,
        result: PublishResult | None = None,
        raises: BaseException | None = None,
        configured: bool = True,
    ) -> None:
        self.platform = platform
        self._result = result
        self._raises = raises
        self._configured = configured
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def is_configured(self) -> bool:
        return self._configured

    async def publish(
        self, article: Article, wp_post_url: str, **kwargs: Any
    ) -> PublishResult:
        self.calls.append((wp_post_url, dict(kwargs)))
        if self._raises is not None:
            raise self._raises
        if self._result is not None:
            return self._result
        return PublishResult.published(
            self.platform_code, external_id="1", url="https://example.test/1"
        )


def all_ok() -> list[StubAdapter]:
    """One successful adapter per platform."""
    return [
        StubAdapter(
            platform,
            result=PublishResult.published(
                platform.value,
                external_id=f"{platform.value}-1",
                url=f"https://{platform.value}.test/1",
            ),
        )
        for platform in Platform
    ]


class StubPublisher:
    """A WordPress publisher that answers from a script."""

    def __init__(
        self,
        *,
        post: PublishedPost | None = None,
        raises: BaseException | None = None,
        fail_times: int = 0,
    ) -> None:
        self._post = post or PublishedPost(
            post_id=777, link=WP_URL, slug="pyhtino", status="publish"
        )
        self._raises = raises
        self._fail_times = fail_times
        self.calls: list[dict[str, Any]] = []

    async def publish(self, session: Any, **kwargs: Any) -> PublishedPost:
        self.calls.append(dict(kwargs))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise WPPublishResponseError("transient 503", status_code=503)
        if self._raises is not None:
            raise self._raises
        return self._post

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the worker's WordPress retry backoff so retries run instantly."""
    monkeypatch.setattr(publisher_worker, "INITIAL_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(publisher_worker, "MAX_BACKOFF_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _stub_cover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the cover URL without touching the media service."""

    async def resolve(article: Article) -> str:
        return COVER_URL if article.wp_image_id else ""

    monkeypatch.setattr(publisher_worker, "_resolve_cover_url", resolve)


async def run_one(
    article: Article,
    *,
    publisher: StubPublisher | None = None,
    adapters: Sequence[StubAdapter] | None = None,
    session: FakeSession | None = None,
) -> tuple[ArticlePublication, FakeSession]:
    """Publish one article with stubbed collaborators."""
    store = session or FakeSession()
    publication = await publish_article_everywhere(
        store.as_session(),
        article,
        publisher=cast(Any, publisher or StubPublisher()),
        adapters=adapters if adapters is not None else all_ok(),
    )
    return publication, store


# ==================================================== the happy path


async def test_a_ready_article_becomes_distributed() -> None:
    article = build_article()

    publication, session = await run_one(article)

    # `ArticleStatus` has no PUBLISHED member; per docs/01 the state after the
    # social channels have run is `distributed`.
    assert article.status is ArticleStatus.DISTRIBUTED
    assert publication.status == ArticleStatus.DISTRIBUTED.value
    assert session.commits == 1


async def test_wordpress_is_published_before_the_fan_out() -> None:
    article = build_article()
    publisher = StubPublisher()
    adapters = all_ok()

    await run_one(article, publisher=publisher, adapters=adapters)

    # Every social post links back to the article, so a teaser sent before the
    # article existed would point at a 404 that cannot be recalled.
    assert publisher.calls
    for adapter in adapters:
        assert adapter.calls[0][0] == WP_URL


async def test_the_wp_post_id_is_stored() -> None:
    article = build_article()

    publication, _ = await run_one(article)

    # Stored so a retry updates the existing post rather than creating a second.
    assert article.wp_post_id == 777
    assert publication.wp_post_id == 777
    assert publication.wp_post_url == WP_URL


async def test_every_platform_status_is_recorded() -> None:
    article = build_article()

    publication, _ = await run_one(article)

    for platform in Platform:
        entry = article.platform_statuses[platform.value]
        assert entry["status"] == PlatformStatus.PUBLISHED.value
        assert entry["external_id"] == f"{platform.value}-1"
        assert entry["url"] == f"https://{platform.value}.test/1"
        assert entry["attempts"] == 1
        assert entry["published_at"] is not None
        assert publication.platforms[platform.value] == entry


async def test_the_published_timestamp_is_set() -> None:
    article = build_article()

    await run_one(article)

    assert article.published_at is not None


async def test_the_cover_url_reaches_every_adapter() -> None:
    article = build_article()
    adapters = all_ok()

    await run_one(article, adapters=adapters)

    # `articles` stores only `wp_image_id`; without the lookup every social post
    # would go out imageless.
    for adapter in adapters:
        kwargs = adapter.calls[0][1]
        assert kwargs["photo_url"] == COVER_URL
        assert kwargs["cover_url"] == COVER_URL
        assert kwargs["image_urls"] == (COVER_URL,)


async def test_an_article_without_a_cover_still_publishes() -> None:
    article = build_article(wp_image_id=None)
    adapters = all_ok()

    publication, _ = await run_one(article, adapters=adapters)

    assert publication.status == ArticleStatus.DISTRIBUTED.value
    assert adapters[0].calls[0][1]["photo_url"] == ""


async def test_the_fan_out_runs_concurrently() -> None:
    # All four are independent HTTP calls to different hosts; serialising them
    # would let a slow Dzen publish delay Telegram.
    order: list[str] = []

    class _Ordered(StubAdapter):
        async def publish(
            self, article: Article, wp_post_url: str, **kwargs: Any
        ) -> PublishResult:
            order.append(f"start:{self.platform_code}")
            await __import__("asyncio").sleep(0)
            order.append(f"end:{self.platform_code}")
            return PublishResult.published(self.platform_code, external_id="1")

    adapters = [_Ordered(platform) for platform in Platform]
    await run_one(build_article(), adapters=adapters)

    # Interleaved rather than sequential start/end pairs back to back.
    assert order[: len(Platform)] == [
        f"start:{platform.value}" for platform in Platform
    ]


# ==================================================== partial publication


async def test_a_failed_social_channel_leaves_the_article_wp_published() -> None:
    adapters = [
        StubAdapter(
            Platform.TELEGRAM,
            result=PublishResult.failed(
                "tg", error="Telegram is down", retryable=True
            ),
        ),
        *(
            StubAdapter(
                platform,
                result=PublishResult.published(platform.value, external_id="1"),
            )
            for platform in (Platform.VK, Platform.OK, Platform.DZEN)
        ),
    ]
    article = build_article()

    publication, _ = await run_one(article, adapters=adapters)

    # "Partially published" in this schema: the site has it, a channel does not.
    assert article.status is ArticleStatus.WP_PUBLISHED
    assert publication.status == ArticleStatus.WP_PUBLISHED.value
    assert publication.social_published == 3
    assert publication.social_failed == 1


async def test_a_failed_channel_records_its_error_in_the_jsonb() -> None:
    adapters = [
        StubAdapter(
            Platform.TELEGRAM,
            result=PublishResult.failed(
                "tg", error="chat not found", retryable=False
            ),
        )
    ]
    article = build_article()

    await run_one(article, adapters=adapters)

    entry = article.platform_statuses["tg"]

    # The per-platform detail is where an operator looks, so it has to be complete.
    assert entry["status"] == PlatformStatus.FAILED.value
    assert entry["error"] == "chat not found"
    assert entry["external_id"] is None
    assert entry["url"] is None
    assert entry["attempts"] == 1


async def test_a_disabled_platform_does_not_block_distribution() -> None:
    adapters = [
        StubAdapter(Platform.TELEGRAM, configured=False),
        *(
            StubAdapter(
                platform,
                result=PublishResult.published(platform.value, external_id="1"),
            )
            for platform in (Platform.VK, Platform.OK, Platform.DZEN)
        ),
    ]
    article = build_article()

    publication, _ = await run_one(article, adapters=adapters)

    # An operator who left a token unset chose not to publish there; that is not a
    # failure, so the article still reaches DISTRIBUTED.
    assert article.status is ArticleStatus.DISTRIBUTED
    assert article.platform_statuses["tg"]["status"] == PlatformStatus.DISABLED.value
    assert publication.social_failed == 0


async def test_an_adapter_that_escapes_safe_publish_is_contained() -> None:
    # `safe_publish` should catch this; reaching the gather means a bug in the
    # adapter's own error handling, which must still not lose the run.
    class _Escaping(StubAdapter):
        async def safe_publish(
            self, article: Article, wp_post_url: str, **kwargs: Any
        ) -> PublishResult:
            raise ZeroDivisionError("escaped")

    adapters = [
        _Escaping(Platform.TELEGRAM),
        StubAdapter(
            Platform.VK, result=PublishResult.published("vk", external_id="1")
        ),
    ]
    article = build_article()

    publication, _ = await run_one(article, adapters=adapters)

    assert article.platform_statuses["tg"]["status"] == PlatformStatus.FAILED.value
    assert "ZeroDivisionError" in article.platform_statuses["tg"]["error"]
    # The other platform kept its outcome.
    assert publication.platforms["vk"]["status"] == PlatformStatus.PUBLISHED.value


async def test_warnings_from_adapters_are_recorded_on_the_article() -> None:
    adapters = [
        StubAdapter(
            Platform.TELEGRAM,
            result=PublishResult.published(
                "tg", external_id="1", warnings=("published as a text message",)
            ),
        )
    ]
    article = build_article()

    publication, _ = await run_one(article, adapters=adapters)

    assert any("text message" in warning for warning in publication.warnings)
    assert article.error_message is not None
    assert "text message" in article.error_message


# ==================================================== WordPress failures


async def test_a_wordpress_failure_leaves_the_article_ready_for_retry() -> None:
    article = build_article()
    publisher = StubPublisher(raises=WPPublishResponseError("500", status_code=500))

    publication, _ = await run_one(article, publisher=publisher)

    # Without a canonical URL there is nothing to distribute, so the article keeps
    # its status and the next tick retries it.
    assert article.status is ArticleStatus.MEDIA_READY
    assert publication.status == "failed"
    assert article.retry_count == 1


async def test_a_transient_wordpress_failure_is_retried() -> None:
    article = build_article()
    publisher = StubPublisher(fail_times=1)

    publication, _ = await run_one(article, publisher=publisher)

    assert len(publisher.calls) == 2
    assert publication.status == ArticleStatus.DISTRIBUTED.value


async def test_a_wordpress_auth_failure_is_not_retried() -> None:
    article = build_article()
    publisher = StubPublisher(raises=WPPublishAuthError("bad application password"))

    publication, _ = await run_one(article, publisher=publisher)

    # A rejected credential will be rejected identically on every attempt.
    assert len(publisher.calls) == 1
    assert publication.status == "failed"
    assert "credentials" in (publication.error or "")


async def test_a_sustained_wordpress_failure_exhausts_the_budget() -> None:
    article = build_article()
    publisher = StubPublisher(fail_times=MAX_WP_ATTEMPTS + 2)

    publication, _ = await run_one(article, publisher=publisher)

    assert len(publisher.calls) == MAX_WP_ATTEMPTS
    assert publication.status == "failed"


async def test_an_article_is_parked_after_too_many_retries() -> None:
    article = build_article(retry_count=MAX_ARTICLE_RETRIES - 1)
    publisher = StubPublisher(raises=WPPublishResponseError("500", status_code=500))

    publication, _ = await run_one(article, publisher=publisher)

    # Past the budget the failure is not transient, and re-running it every five
    # minutes only burns quota.
    assert article.status is ArticleStatus.FAILED
    assert article.error_message is not None
    assert any("parked" in warning for warning in publication.warnings)


async def test_no_social_post_is_sent_when_wordpress_fails() -> None:
    adapters = all_ok()
    publisher = StubPublisher(raises=WPPublishResponseError("500", status_code=500))

    await run_one(build_article(), publisher=publisher, adapters=adapters)

    # A teaser linking to an article that does not exist cannot be recalled.
    for adapter in adapters:
        assert adapter.calls == []


async def test_a_post_without_a_permalink_skips_distribution() -> None:
    article = build_article()
    publisher = StubPublisher(
        post=PublishedPost(post_id=777, link="", slug="x", status="publish")
    )
    adapters = all_ok()

    publication, _ = await run_one(article, publisher=publisher, adapters=adapters)

    # The post is live, so the article moves forward rather than being retried into
    # a duplicate — but there is no URL to distribute.
    assert article.status is ArticleStatus.WP_PUBLISHED
    assert publication.status == ArticleStatus.WP_PUBLISHED.value
    for adapter in adapters:
        assert adapter.calls == []


async def test_wordpress_warnings_are_surfaced() -> None:
    publisher = StubPublisher(
        post=PublishedPost(
            post_id=777,
            link=WP_URL,
            slug="x",
            status="publish",
            warnings=("WordPress dropped the Rank Math field(s) rank_math_title",),
        )
    )

    publication, _ = await run_one(build_article(), publisher=publisher)

    assert any("Rank Math" in warning for warning in publication.warnings)


# ==================================================== idempotency


@pytest.mark.parametrize(
    "status",
    [
        ArticleStatus.DISTRIBUTED,
        ArticleStatus.WP_PUBLISHED,
        ArticleStatus.DRAFT,
        ArticleStatus.FAILED,
    ],
)
async def test_an_article_not_at_media_ready_is_skipped(
    status: ArticleStatus,
) -> None:
    article = build_article(status=status)
    publisher = StubPublisher()
    adapters = all_ok()

    publication, session = await run_one(
        article, publisher=publisher, adapters=adapters
    )

    # Re-publishing a live article would create a duplicate post.
    assert publication.status == "skipped"
    assert publisher.calls == []
    assert session.commits == 0
    for adapter in adapters:
        assert adapter.calls == []


async def test_an_existing_post_is_updated_not_recreated() -> None:
    article = build_article(wp_post_id=555)
    publisher = StubPublisher(
        post=PublishedPost(post_id=555, link=WP_URL, slug="x", status="publish")
    )

    await run_one(article, publisher=publisher)

    # Duplicate live articles are what search engines penalise.
    assert publisher.calls[0]["existing_post_id"] == 555


async def test_a_platform_already_published_is_not_resent() -> None:
    article = build_article()
    article.platform_statuses["tg"] = {
        "status": PlatformStatus.PUBLISHED.value,
        "external_id": "999",
        "url": "https://t.me/c/999",
        "error": None,
        "attempts": 1,
        "published_at": "2026-08-23T10:00:00+00:00",
    }
    telegram = StubAdapter(
        Platform.TELEGRAM, result=PublishResult.published("tg", external_id="new")
    )
    vk = StubAdapter(Platform.VK, result=PublishResult.published("vk", external_id="1"))

    publication, _ = await run_one(article, adapters=[telegram, vk])

    # A retry after a partial failure must re-send only what failed.
    assert telegram.calls == []
    assert vk.calls
    assert article.platform_statuses["tg"]["external_id"] == "999"
    assert any("already published" in warning for warning in publication.warnings)


async def test_the_attempt_counter_accumulates_across_retries() -> None:
    article = build_article()
    article.platform_statuses["tg"] = {
        "status": PlatformStatus.FAILED.value,
        "external_id": None,
        "url": None,
        "error": "earlier failure",
        "attempts": 2,
        "published_at": None,
    }
    adapters = [
        StubAdapter(
            Platform.TELEGRAM,
            result=PublishResult.published("tg", external_id="1"),
        )
    ]

    await run_one(article, adapters=adapters)

    # The counter is what tells an operator a platform has been struggling.
    assert article.platform_statuses["tg"]["attempts"] == 3


async def test_a_commit_failure_is_rolled_back() -> None:
    session = FakeSession()
    session.fail_commit = SQLAlchemyError("deadlock")

    publication, store = await run_one(build_article(), session=session)

    assert publication.status == "failed"
    assert store.rollbacks == 1
    assert "persist" in (publication.error or "")


# ==================================================== reporting & wiring


def test_the_lock_key_is_scoped_per_article() -> None:
    assert article_lock_key(1) != article_lock_key(2)
    assert "5150" in article_lock_key(5150)


def test_the_report_counts_by_terminal_status() -> None:
    report = PublisherRunReport(
        publications=[
            ArticlePublication(article_id=1, status=ArticleStatus.DISTRIBUTED.value),
            ArticlePublication(article_id=2, status=ArticleStatus.DISTRIBUTED.value),
            ArticlePublication(article_id=3, status=ArticleStatus.WP_PUBLISHED.value),
            ArticlePublication(article_id=4, status="failed"),
        ]
    )

    assert report.distributed == 2
    assert report.wp_published == 1
    assert report.failed == 1

    payload = report.to_dict()
    assert payload["articles"] == 4
    assert len(payload["publications"]) == 4


def test_a_publication_serialises_for_arq() -> None:
    publication = ArticlePublication(article_id=7)
    publication.warn("something worth knowing")

    payload = publication.to_dict()

    # ARQ persists the job result as JSON.
    assert payload["article_id"] == 7
    assert payload["warnings"] == ["something worth knowing"]
    assert payload["social_published"] == 0


def test_every_platform_gets_an_adapter() -> None:
    adapters = build_adapters()

    codes = sorted(adapter.platform_code for adapter in adapters)

    # All four are always constructed: filtering unconfigured ones here would make
    # a misconfiguration indistinguishable from a platform that was never tried.
    assert codes == sorted(platform.value for platform in Platform)


def test_the_worker_is_registered_for_arq() -> None:
    settings_class = publisher_worker.WorkerSettings

    assert publisher_worker.publisher_task in settings_class.functions
    assert settings_class.cron_jobs
    # One job at a time: four platforms all rate-limit per account, so concurrent
    # batches would throttle each other rather than double throughput.
    assert settings_class.max_jobs == 1


def test_each_worker_polls_its_own_queue() -> None:
    """The three workers must not share ARQ's default queue.

    They run as separate processes against one Redis and each registers only its
    own function. On the shared default queue whichever worker polls first claims
    the job regardless, so a publisher tick picked up by the ingestion worker fails
    as "function not found" and that pass is silently lost. Observed in production
    before the queues were split, which is why this is asserted rather than assumed.
    """
    queues = {
        ingestion_worker.WorkerSettings.queue_name,
        content_worker.WorkerSettings.queue_name,
        publisher_worker.WorkerSettings.queue_name,
    }

    assert len(queues) == 3, f"workers share a queue: {queues}"
    assert default_queue_name not in queues


def test_vk_214_error_not_treated_as_unresolved_social() -> None:
    from app.workers.publisher_worker import (  # noqa: PLC0415
        _has_unresolved_socials,
        _terminal_status,
    )

    article = build_article(status=ArticleStatus.WP_PUBLISHED)
    article.platform_statuses = {
        "tg": {"status": "published", "url": "https://t.me/1"},
        "vk": {
            "status": "failed",
            "error": "wall.post: VK error 214: Access denied: 500 posts a day",
            "attempts": 1,
        },
    }
    assert _has_unresolved_socials(article) is False

    pub = ArticlePublication(article_id=article.id)
    pub.platforms = dict(article.platform_statuses)
    assert _terminal_status(pub) == ArticleStatus.DISTRIBUTED

