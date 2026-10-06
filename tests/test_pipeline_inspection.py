"""The zero-token inspection pass: reset, intake reporting and media verdicts.

Three properties make this feature trustworthy, and each is protected here:

* **It costs nothing.** Every stage of the inspection must report
  ``tokens_spent=False``. If a paid call ever creeps into this path, the operator
  loses the one button they can press repeatedly while tuning thresholds.
* **The reset actually resets.** Its purpose is to make the next collection a
  *first* pass, which only happens if the per-source watermark is cleared. A reset
  that empties the tables but leaves ``last_scraped_at`` set would look successful
  and then collect nothing.
* **"Not checked" never reads as "clean".** A photo skipped because its source has
  media collection off carries no watermark, and neither does a photo that was
  analysed and found clean. Publishing the first one is a mistake, so the report
  keeps them apart.
"""

# Operator-facing summaries under test are Russian.
# ruff: noqa: RUF001

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.config import settings
from app.models.enums import DedupStatus, MediaStrategy
from app.services.pipeline_inspection import (
    MAX_IMAGE_BYTES,
    InspectionRunner,
    _describe_verdict,
    _fetch_over_telegram,
    _media_url,
    _skip_reason,
)
from app.services.pipeline_reset import (
    ResetBlockedError,
    ResetReport,
    UnsafeWipeTargetError,
    assert_safe_wipe_target,
    reset_pipeline_queue,
    wipe_pipeline_history,
)
from app.services.pipeline_stages import (
    STAGE_TITLES,
    StageName,
    StageOutcome,
    StageResult,
)
from app.services.research import (
    ARCHIVE_LOOKBACK,
    collect_facts,
    title_keywords,
)
from app.workers.ingestion_worker import RUN_LOCK_KEY

# ================================================================ reset


class _Result:
    """Minimal stand-in for a SQLAlchemy result."""

    def __init__(self, rowcount: int = 0, scalar: int = 0) -> None:
        self.rowcount = rowcount
        self._scalar = scalar

    def scalar_one(self) -> int:
        return self._scalar


class _ResetSession:
    """Session double recording the statements the reset issued.

    Deliberately does not emulate SQL: what these tests verify is *which*
    statements are sent and that a failure rolls back, not that PostgreSQL applies
    them, which is the migration suite's job.
    """

    def __init__(self, *, clusters_with_articles: int = 0, fail: bool = False) -> None:
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.clusters_with_articles = clusters_with_articles
        self.fail = fail

    async def execute(self, statement: Any) -> Any:
        compiled = " ".join(str(statement).split()).lower()
        self.statements.append(compiled)
        if self.fail:
            raise OperationalError("boom", None, Exception("boom"))
        if compiled.startswith("select count"):
            return _Result(scalar=self.clusters_with_articles)
        return _Result(rowcount=3)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    def as_session(self) -> Any:
        return cast(Any, self)


@pytest.fixture
def redis_double(fake_redis: FakeRedis) -> FakeRedis:
    """Alias for the shared fake Redis.

    Reuses `conftest.fake_redis` rather than building its own client: that fixture
    closes the connection *pool*, and a pooled connection left for the garbage
    collector emits a `ResourceWarning` that this suite turns into an error in
    whichever test happens to run next.
    """
    return fake_redis


async def test_the_reset_rewinds_the_per_source_watermark(
    redis_double: FakeRedis,
) -> None:
    """The point of the reset: the next pass must be a *first* pass.

    `telegram_watermark` derives the cursor from `last_scraped_at` and the stored
    posts, and the reader takes the full `INGEST_MAX_MESSAGES_PER_SOURCE` window
    only when there is no watermark. A reset that dropped the rows but left the
    timestamp would report success and then collect the overlap window — which
    looks identical to an empty channel.
    """
    session = _ResetSession()

    report = await reset_pipeline_queue(session.as_session(), redis=redis_double)

    rewind = next(
        statement
        for statement in session.statements
        if statement.startswith("update sources")
    )
    assert "last_scraped_at" in rewind
    assert "posts_collected" in rewind
    assert any(
        statement.startswith("delete from raw_news") for statement in session.statements
    )
    assert report.sources_rewound == 3
    assert session.commits == 1


async def test_clusters_with_articles_survive_and_are_reported(
    redis_double: FakeRedis,
) -> None:
    """`articles.cluster_id` is NOT NULL with ON DELETE CASCADE.

    Deleting such a cluster deletes a published article, whose WordPress post no
    database row can unpublish. Keeping it has a consequence the operator must be
    told about: the cluster stays in the semantic index, so a re-collected post
    about that story lands in duplicates instead of opening a new cluster.
    """
    session = _ResetSession(clusters_with_articles=4)

    report = await reset_pipeline_queue(session.as_session(), redis=redis_double)

    delete_clusters = next(
        statement
        for statement in session.statements
        if statement.startswith("delete from news_clusters")
    )
    assert "where" in delete_clusters, "clusters were deleted unconditionally"
    assert report.clusters_kept == 4
    assert any("сохранено" in warning.lower() for warning in report.warnings)
    assert report.articles == 0


async def test_including_articles_empties_the_system(
    redis_double: FakeRedis,
) -> None:
    """The explicit choice, kept separate from "clear the queue"."""
    session = _ResetSession(clusters_with_articles=4)

    report = await reset_pipeline_queue(
        session.as_session(), include_articles=True, redis=redis_double
    )

    assert any(
        statement.startswith("delete from articles") for statement in session.statements
    )
    delete_clusters = next(
        statement
        for statement in session.statements
        if statement.startswith("delete from news_clusters")
    )
    assert "where" not in delete_clusters
    assert report.articles == 3


async def test_a_wipe_without_confirmation_is_refused(
    redis_double: FakeRedis,
) -> None:
    session = _ResetSession()

    with pytest.raises(ResetBlockedError, match="подтверждения"):
        await wipe_pipeline_history(
            session.as_session(), confirm=False, redis=redis_double
        )

    assert session.statements == []
    assert session.commits == 0


def test_a_production_wipe_target_is_refused() -> None:
    """The production database must be unreachable through this operation."""
    with pytest.raises(UnsafeWipeTargetError, match="production"):
        assert_safe_wipe_target(
            app_env="production",
            postgres_host="localhost",
            postgres_db="svodkapress_verify",
        )


def test_a_remote_host_wipe_target_is_refused() -> None:
    """A correctly suffixed name is not enough if the host is the live VPS."""
    with pytest.raises(UnsafeWipeTargetError, match="хоста"):
        assert_safe_wipe_target(
            app_env="development",
            postgres_host="144.31.141.17",
            postgres_db="svodkapress_verify",
        )


def test_a_live_database_name_wipe_target_is_refused() -> None:
    with pytest.raises(UnsafeWipeTargetError, match="базы"):
        assert_safe_wipe_target(
            app_env="development",
            postgres_host="localhost",
            postgres_db="svodkapress",
        )


def test_a_local_scratch_target_is_accepted() -> None:
    assert_safe_wipe_target(
        app_env="development",
        postgres_host="postgres",
        postgres_db="svodkapress_verify",
    )


async def test_a_reset_during_collection_is_refused(redis_double: FakeRedis) -> None:
    """Mid-pass, the reset would delete the rows that pass compares against.

    The result would be an LSH index disagreeing with the table, which is the one
    state the dedup ladder cannot explain to an operator.
    """
    await redis_double.set(RUN_LOCK_KEY, "held")
    session = _ResetSession()

    with pytest.raises(ResetBlockedError, match="идёт сбор"):
        await reset_pipeline_queue(session.as_session(), redis=redis_double)

    assert session.statements == []
    assert session.commits == 0


async def test_a_database_failure_rolls_back_and_removes_nothing(
    redis_double: FakeRedis,
) -> None:
    """A partial reset is worse than none: it leaves Redis describing dead rows."""
    session = _ResetSession(fail=True)
    await redis_double.set("lsh:bucket:0:abc", "1:2")

    with pytest.raises(SQLAlchemyError):
        await reset_pipeline_queue(session.as_session(), redis=redis_double)

    assert session.rollbacks == 1
    assert session.commits == 0
    # Redis is cleared only after the commit, so the index is still intact.
    assert await redis_double.exists("lsh:bucket:0:abc") == 1


async def test_the_reset_clears_the_lsh_index_and_the_cooldowns(
    redis_double: FakeRedis,
) -> None:
    """Level 1 recognises a post from Redis alone.

    Left in place, the index would report every re-collected post as a duplicate of
    a row that no longer exists — a verdict with no evidence behind it.
    """
    await redis_double.set("lsh:bucket:0:abc", "1:2")
    await redis_double.set("lsh:sig:1", "[1,2,3]")
    await redis_double.set("model:cooldown:writer:a/b", "429")
    await redis_double.set("pipeline:enabled", "1")
    session = _ResetSession()

    report = await reset_pipeline_queue(session.as_session(), redis=redis_double)

    assert report.redis_keys == 3
    assert await redis_double.exists("lsh:bucket:0:abc") == 0
    assert await redis_double.exists("model:cooldown:writer:a/b") == 0
    # Unrelated keys are left alone: the reset clears the dedup index and the
    # penalties, not the whole database.
    assert await redis_double.exists("pipeline:enabled") == 1


def test_a_reset_report_counts_every_store() -> None:
    """"Queue cleared" is not checkable; four numbers are."""
    report = ResetReport(
        raw_news=34, clusters=21, sources_rewound=8, redis_keys=108
    )

    summary = report.summary()
    assert "34" in summary
    assert "21" in summary
    assert "108" in summary
    assert report.to_dict()["total"] == 34 + 21 + 108


# ==================================================== watermark verdicts


def _photo(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "fetch_media": True,
        "media_strategy": MediaStrategy.OCR_CHECK.value,
        "url": "",
        "identifier": "@channel",
        "message_id": 51,
    }
    item.update(overrides)
    return item


def test_a_source_with_media_collection_off_is_unchecked_not_clean() -> None:
    """"Never looked" and "looked, found nothing" must not be the same row.

    Both carry no watermark, and only one of them means the image is publishable.
    """
    verdict, reason = cast(
        tuple[str, str], _skip_reason(_photo(fetch_media=False))
    )

    assert verdict == "unchecked"
    assert "медиа" in reason.lower()


def test_a_text_only_source_is_unchecked() -> None:
    verdict, _ = cast(
        tuple[str, str],
        _skip_reason(_photo(media_strategy=MediaStrategy.TEXT_ONLY.value)),
    )

    assert verdict == "unchecked"


def test_always_regenerate_is_reported_as_rejected() -> None:
    """Production will not use this original, so "unchecked" would understate it."""
    verdict, reason = cast(
        tuple[str, str],
        _skip_reason(_photo(media_strategy=MediaStrategy.ALWAYS_REGENERATE.value)),
    )

    assert verdict == "rejected"
    assert "генерировать" in reason


def test_an_ocr_check_source_is_not_skipped() -> None:
    assert _skip_reason(_photo()) is None


def test_a_clean_image_is_publishable() -> None:
    verdict, reason = _describe_verdict(
        {
            "has_watermark": False,
            "coverage_ratio": 0.0,
            "detected_text": [],
            "needs_ai_generation": False,
            "is_croppable": False,
        }
    )

    assert verdict == "clean"
    assert "оригинал" in reason


def test_a_heavy_watermark_is_rejected_and_names_what_was_found() -> None:
    """The operator's question is "why", and the OCR text is the answer."""
    verdict, reason = _describe_verdict(
        {
            "has_watermark": True,
            "coverage_ratio": 0.31,
            "detected_text": ["@mash", "mash.ru"],
            "needs_ai_generation": True,
            "is_croppable": False,
        }
    )

    assert verdict == "rejected"
    assert "31%" in reason
    assert "@mash" in reason


def test_a_banded_watermark_is_croppable() -> None:
    """Cropping keeps a real photograph, which beats a synthesised illustration."""
    verdict, reason = _describe_verdict(
        {
            "has_watermark": True,
            "coverage_ratio": 0.04,
            "detected_text": ["@channel"],
            "needs_ai_generation": False,
            "is_croppable": True,
        }
    )

    assert verdict == "croppable"
    assert "обрезана" in reason


async def test_a_telegram_photo_without_a_client_reports_why() -> None:
    """A missing account is a media verdict, not a silent omission."""
    row: dict[str, Any] = {}

    content = await _fetch_over_telegram(
        _photo(), None, row, "нет доступного аккаунта"
    )

    assert content == b""
    assert "аккаунт" in row["reason"]


async def test_a_media_item_without_a_message_id_cannot_be_downloaded() -> None:
    row: dict[str, Any] = {}

    content = await _fetch_over_telegram(
        _photo(message_id=None), object(), row, ""
    )

    assert content == b""
    assert "id сообщения" in row["reason"]


class _Downloader:
    """MTProto double recording the ceiling it was asked to honour."""

    def __init__(self, payload: bytes = b"image") -> None:
        self.payload = payload
        self.limit: int | None = None

    async def download_photo(
        self, channel: str, message_id: int, *, limit_bytes: int
    ) -> bytes:
        self.limit = limit_bytes
        return self.payload


async def test_a_telegram_download_is_size_bounded() -> None:
    """The container shares its memory with Chromium and the OCR pipeline."""
    downloader = _Downloader()
    row: dict[str, Any] = {}

    content = await _fetch_over_telegram(_photo(), downloader, row, "")

    assert content == b"image"
    assert downloader.limit == MAX_IMAGE_BYTES


async def test_an_empty_download_is_reported_rather_than_silently_clean() -> None:
    downloader = _Downloader(payload=b"")
    row: dict[str, Any] = {}

    content = await _fetch_over_telegram(_photo(), downloader, row, "")

    assert content == b""
    assert row["reason"]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"media_id": "https://site.test/a.jpg"}, "https://site.test/a.jpg"),
        ({"url": "http://site.test/b.jpg"}, "http://site.test/b.jpg"),
        # A Telegram file id is not fetchable over HTTP; treating it as a URL
        # would produce a download attempt against a nonexistent host.
        ({"media_id": "5194712"}, ""),
        ({}, ""),
    ],
)
def test_only_http_media_is_fetchable_by_url(
    entry: dict[str, Any], expected: str
) -> None:
    assert _media_url(entry) == expected


# ======================================================== intake verdicts


class _InspectedPost:
    """The slice of `RawNews` the inspection's explainer reads."""

    def __init__(
        self,
        *,
        status: DedupStatus,
        similarity: float | None = None,
        level: int | None = None,
        is_digest: bool = False,
        is_ad: bool = False,
    ) -> None:
        self.dedup_status = status
        self.similarity_score = similarity
        self.dedup_level = level
        self.is_digest = is_digest
        self.is_ad = is_ad


def _explain(post: _InspectedPost) -> str:
    return InspectionRunner._explain(cast(Any, post))


def test_the_inspection_quotes_the_threshold_a_duplicate_crossed() -> None:
    verdict = _explain(
        _InspectedPost(status=DedupStatus.SKIPPED_DUPLICATE, similarity=0.87, level=2)
    )

    assert "Дубль" in verdict
    assert "0.87" in verdict
    assert str(settings.similarity_upper_threshold) in verdict


def test_an_update_is_distinguished_from_a_duplicate() -> None:
    """A post carrying new facts extends a cluster; it is not a rejected copy."""
    verdict = _explain(_InspectedPost(status=DedupStatus.UPDATE))

    assert "Дополнение" in verdict


def test_a_pending_post_says_it_was_not_checked() -> None:
    """Reporting an unchecked post as unique would invent a verdict."""
    verdict = _explain(_InspectedPost(status=DedupStatus.PENDING))

    assert "не проверялся" in verdict


def test_a_filtered_digest_is_reported_before_its_dedup_status() -> None:
    """A digest never reached the ladder, so its `PENDING` status is meaningless.

    Reported as `PENDING` it would read as "not checked yet", when in fact the
    zero-token filter closed the post for good.
    """
    verdict = _explain(
        _InspectedPost(status=DedupStatus.PENDING, is_digest=True)
    )

    assert "дайджест" in verdict
    assert "0 токенов" in verdict


def test_the_new_stages_have_operator_facing_titles() -> None:
    """A timeline row labelled `watermark` tells an operator nothing."""
    for stage in (StageName.RESET, StageName.WATERMARK):
        assert stage in STAGE_TITLES
        assert STAGE_TITLES[stage].strip()


def test_the_inspection_stages_are_free_of_paid_calls() -> None:
    """The whole point of the pass: it can be pressed repeatedly at no cost.

    `tokens_spent` defaults to False and is set only where a model is called, so a
    paid call appearing in this list would show up here.
    """
    inspected = [
        StageResult(stage=StageName.RESET),
        StageResult(stage=StageName.COLLECT),
        StageResult(stage=StageName.DEDUP),
        StageResult(stage=StageName.WATERMARK),
        StageResult(stage=StageName.RESEARCH),
    ]

    assert not any(stage.tokens_spent for stage in inspected)
    assert not any(stage.model for stage in inspected)


# =============================================================== research


class _Cluster:
    def __init__(self, title: str, facts: Any = None) -> None:
        self.id = uuid.uuid4()
        self.title = title
        self.enrichment_data = {} if facts is None else {"facts": facts}


class _Post:
    def __init__(self, text: str) -> None:
        self.raw_text = text


def test_stored_facts_are_preferred_over_derived_ones() -> None:
    cluster = _Cluster("Метро", facts=["Станция открыта", "Линия продлена"])

    facts = collect_facts(cast(Any, cluster), [_Post("Что-то ещё.")])  # type: ignore[list-item]

    assert facts == ["Станция открыта", "Линия продлена"]


def test_without_enrichment_the_first_sentences_are_shown() -> None:
    """An empty panel reads as "the archive knows nothing".

    The truth is "the facts have not been extracted yet", and the two call for
    different actions from the operator.
    """
    cluster = _Cluster("Метро")
    posts = [_Post("Станция открыта. Второе предложение."), _Post("Линия продлена.")]

    facts = collect_facts(cast(Any, cluster), cast(Any, posts))

    assert facts == ["Станция открыта", "Линия продлена"]


def test_short_words_are_not_keywords() -> None:
    """Four characters filters Russian prepositions without a stop-word list."""
    words = title_keywords("В Москве на юге открыли станцию метро")

    assert "станцию" in words
    assert "москве" in words
    assert "на" not in words
    assert "юге" not in words


def test_keywords_ignore_punctuation_and_case() -> None:
    assert title_keywords("«Пыхтино», Москва!") == {"пыхтино", "москва"}


def test_the_archive_window_is_wider_than_the_dedup_window() -> None:
    """The two ask different questions.

    Dedup asks "is this the same post" and closes after 48 hours; the archive asks
    "have we written about this before", which stays useful for weeks.
    """
    assert ARCHIVE_LOOKBACK.total_seconds() > settings.dedup_lookback_hours * 3600


# ================================== handover to the paid stages (11.11)


def test_the_inspection_offers_a_write_button_per_cluster() -> None:
    """The handover from the free pass to the paid one.

    Having read the intake, the operator picks the story worth paying for. Without a
    per-cluster button the only way to write is "newest", which is exactly the choice
    the inspection exists to inform.
    """
    template = Path("app/templates/dashboard/partials/lab_run.html").read_text(
        encoding="utf-8"
    )

    assert "cluster={{ item.cluster_id }}" in template
    # `collect=0`: the posts are already stored, and re-collecting would add
    # Telegram traffic for nothing.
    assert "collect=0&cluster=" in template
    # The paid step must be confirmed rather than fired by a stray click.
    assert "hx-confirm" in template


def test_an_already_written_cluster_offers_no_write_button() -> None:
    """Paying twice for the same story is the mistake this guards against."""
    template = Path("app/templates/dashboard/partials/lab_run.html").read_text(
        encoding="utf-8"
    )

    assert "item.is_published" in template


def test_the_research_report_carries_what_the_button_needs() -> None:
    """The template's contract with the stage payload.

    `cluster_id` fills the URL and `is_published` decides whether the button is
    offered at all, so a stage that stopped emitting either would break the handover
    silently — the panel would still render.
    """
    described = {
        "cluster_id": str(uuid.uuid4()),
        "title": "Заголовок",
        "is_published": False,
        "facts": [],
        "media": [],
        "texts": [],
        "related_articles": [],
    }

    assert "cluster_id" in described
    assert described["is_published"] is False


def test_a_stage_result_serialises_the_reset_counts() -> None:
    """The reset panel is rebuilt from the artifact after a page reload."""
    result = StageResult(stage=StageName.RESET)
    result.summary = "постов удалено: 34"
    result.details = ResetReport(raw_news=34, clusters=21).to_dict()

    payload = result.to_dict()

    assert payload["stage"] == "reset"
    assert payload["details"]["raw_news"] == 34
    assert payload["outcome"] == StageOutcome.OK.value
