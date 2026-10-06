"""The observable pipeline: artifacts, stage verdicts and the production switch.

These tests protect the two properties that make the Лаборатория trustworthy:

* **The timeline reports what actually happened.** A stage that skipped must not
  read as a success, a run that spent tokens must say so, and the dedup verdict
  must state *why* — a similarity number with no threshold beside it explains
  nothing.
* **The production switch cannot silently pause the pipeline.** It fails open on
  purpose: a paused pipeline still reports healthy, so the missing news is only
  noticed hours later, which is worse than a tick that ran when it should not
  have.
"""

# Operator-facing summaries under test are Russian.
# ruff: noqa: RUF001

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import DedupStatus
from app.services.pipeline_control import (
    DEFAULT_INTERVAL_MINUTES,
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    PipelineControl,
    ScheduleError,
    load_control,
    save_control,
)
from app.services.pipeline_runner import PipelineRunner
from app.services.pipeline_stages import (
    STAGE_TITLES,
    ArtifactStore,
    RunMode,
    RunSummary,
    StageName,
    StageOutcome,
    StageResult,
    new_run_id,
    preview,
)

# ============================================================ stage results


def test_every_stage_has_an_operator_facing_title() -> None:
    """A timeline row labelled `dedup` tells an operator nothing."""
    for stage in StageName:
        assert stage in STAGE_TITLES
        assert STAGE_TITLES[stage].strip()


def test_a_skipped_stage_is_not_a_failure() -> None:
    """"Nothing to collect" and "the collector crashed" must not look alike.

    Both produce zero new posts, and only one of them needs an operator.
    """
    skipped = StageResult(stage=StageName.COLLECT)
    skipped.skip("Нет активных каналов")
    failed = StageResult(stage=StageName.COLLECT)
    failed.fail("Telegram недоступен")

    assert skipped.outcome is StageOutcome.SKIPPED
    assert failed.outcome is StageOutcome.FAILED
    assert skipped.summary == "Нет активных каналов"
    # A failure always carries its reason into the summary, so the timeline row
    # is never blank.
    assert failed.summary == "Telegram недоступен"


def test_a_warning_does_not_change_the_outcome() -> None:
    result = StageResult(stage=StageName.ARTICLE)
    result.summary = "Статья готова"
    result.warn("AI-slop: канцелярит")

    assert result.outcome is StageOutcome.OK
    assert result.warnings == ["AI-slop: канцелярит"]


def test_a_run_reports_whether_it_spent_tokens() -> None:
    """The zero-token guarantee is only auditable if the run says so."""
    summary = RunSummary(
        run_id="20260825-120000-abcdef",
        mode=RunMode.TEST,
        started_at=datetime.now(UTC),
    )
    summary.stages.append(StageResult(stage=StageName.DEDUP))
    assert summary.tokens_spent is False

    paid = StageResult(stage=StageName.ARTICLE, tokens_spent=True, model="auto/fast")
    summary.stages.append(paid)
    assert summary.tokens_spent is True
    assert summary.stage(StageName.ARTICLE) is paid
    assert summary.stage(StageName.MEDIA) is None


def test_preview_marks_that_it_trimmed() -> None:
    """A silently truncated body reads as a complete one."""
    assert preview("короткий текст") == "короткий текст"
    trimmed = preview("а" * 500, limit=100)
    assert len(trimmed) == 101
    assert trimmed.endswith("…")


# ============================================================== artifacts


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ArtifactStore]:
    yield ArtifactStore(root=tmp_path / "runs")


def test_a_run_id_is_sortable_and_safe(store: ArtifactStore) -> None:
    """Timestamp-prefixed so the directory lists chronologically."""
    first = new_run_id()
    second = new_run_id()

    assert first[:8].isdigit()
    # Both ids are accepted by the path guard, which is what makes them usable
    # as URL parameters.
    assert store.run_dir(first).is_dir()
    assert store.run_dir(second).is_dir()


@pytest.mark.parametrize(
    "unsafe",
    ["../../etc", "run/../../../tmp", "..", "", "20260825-120000-ZZZZZZ", "a/b"],
)
def test_a_forged_run_id_is_refused(store: ArtifactStore, unsafe: str) -> None:
    """`run_id` arrives from a URL path, so it cannot be trusted as a filename.

    Without this guard a value like `../../etc` would read and write outside the
    storage root.
    """
    with pytest.raises(ValueError, match="unsafe run id"):
        store.run_dir(unsafe)


def test_stage_artifacts_keep_execution_order(store: ArtifactStore) -> None:
    """The timeline is rebuilt from the directory listing after a reload."""
    run_id = new_run_id()
    store.write_stage(run_id, 1, StageResult(stage=StageName.COLLECT))
    store.write_stage(run_id, 2, StageResult(stage=StageName.DEDUP))

    names = sorted(path.name for path in store.run_dir(run_id).iterdir())
    assert names == ["01-collect.json", "02-dedup.json"]


def test_a_stage_artifact_round_trips(store: ArtifactStore) -> None:
    run_id = new_run_id()
    result = StageResult(
        stage=StageName.ARTICLE,
        summary="Статья готова",
        model="auto/fast",
        tokens_spent=True,
        prompt="СИСТЕМНЫЙ ПРОМПТ",
        details={"title": "Заголовок", "seo_keywords": ["метро", "Москва"]},
    )
    path = store.write_stage(run_id, 1, result)

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["stage"] == "article"
    assert loaded["title"] == STAGE_TITLES[StageName.ARTICLE]
    # The prompt is stored verbatim: showing a paraphrase would defeat the point.
    assert loaded["prompt"] == "СИСТЕМНЫЙ ПРОМПТ"
    assert loaded["details"]["seo_keywords"] == ["метро", "Москва"]


def test_recent_runs_are_newest_first(store: ArtifactStore) -> None:
    for run_id in ("20260825-100000-aaaaaa", "20260825-120000-bbbbbb"):
        summary = RunSummary(
            run_id=run_id, mode=RunMode.TEST, started_at=datetime.now(UTC)
        )
        store.write_summary(summary)

    recent = store.recent()
    assert [run["run_id"] for run in recent] == [
        "20260825-120000-bbbbbb",
        "20260825-100000-aaaaaa",
    ]


def test_an_unreadable_summary_is_skipped_not_raised(store: ArtifactStore) -> None:
    """One corrupt artifact must not make the history page unreachable."""
    run_id = "20260825-130000-cccccc"
    (store.run_dir(run_id) / "run.json").write_text("{ broken", encoding="utf-8")

    assert store.read_summary(run_id) is None
    assert store.recent() == []


def test_pruning_keeps_the_newest_runs(store: ArtifactStore) -> None:
    """A run holds prompts and a full article; history must stay bounded.

    Unbounded, it would fill the volume the media pipeline shares.
    """
    for hour in range(5):
        run_id = f"20260825-1{hour}0000-abcdef"
        store.write_summary(
            RunSummary(run_id=run_id, mode=RunMode.TEST, started_at=datetime.now(UTC))
        )
        store.write_stage(run_id, 1, StageResult(stage=StageName.DEDUP))

    removed = store.prune(keep=2)

    assert removed == 3
    assert len(store.recent()) == 2


def test_pruning_keeps_article_journals_even_when_they_are_old(
    store: ArtifactStore,
) -> None:
    for hour in range(5):
        run_id = f"20260825-1{hour}0000-abcdef"
        store.write_summary(
            RunSummary(
                run_id=run_id,
                mode=RunMode.LIVE,
                started_at=datetime.now(UTC),
                article_id=hour + 1,
            )
        )

    assert store.prune(keep=1) == 0
    assert len(store.recent()) == 5


def test_purge_all_removes_every_run_for_a_confirmed_clean_start(
    store: ArtifactStore,
) -> None:
    for run_id in ("20260825-100000-aaaaaa", "20260825-110000-bbbbbb"):
        store.write_summary(
            RunSummary(run_id=run_id, mode=RunMode.TEST, started_at=datetime.now(UTC))
        )

    assert store.purge_all() == 2
    assert store.recent() == []


# ========================================================= dedup verdicts


class _Post:
    """The slice of `RawNews` the verdict explainer reads."""

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


def _explain(post: _Post) -> str:
    return PipelineRunner._explain(cast(Any, post))


def test_a_duplicate_verdict_names_the_threshold_it_crossed() -> None:
    """A bare similarity number explains nothing without its threshold.

    The operator's question is "is this correct", and that needs the comparison,
    not just the measurement.
    """
    verdict = _explain(
        _Post(status=DedupStatus.SKIPPED_DUPLICATE, similarity=0.87, level=2)
    )

    assert "Дубль" in verdict
    assert "0.87" in verdict
    assert str(settings.similarity_upper_threshold) in verdict


def test_a_unique_verdict_explains_why_it_is_not_a_duplicate() -> None:
    verdict = _explain(_Post(status=DedupStatus.UNIQUE, similarity=0.41, level=2))

    assert "Новая новость" in verdict
    assert "0.41" in verdict


def test_a_unique_post_with_no_neighbour_says_so() -> None:
    """Level 1 finding nothing is a different story from a low score."""
    verdict = _explain(_Post(status=DedupStatus.UNIQUE))

    assert "похожих не найдено" in verdict


@pytest.mark.parametrize(
    ("post", "expected"),
    [
        (_Post(status=DedupStatus.FILTERED_DIGEST, is_digest=True), "дайджест"),
        (_Post(status=DedupStatus.FILTERED_AD, is_ad=True), "реклама"),
    ],
)
def test_the_zero_token_filters_are_reported_as_such(
    post: _Post, expected: str
) -> None:
    """The free filters are the economics of the pipeline; they must be visible."""
    verdict = _explain(post)

    assert expected in verdict
    assert "0 токенов" in verdict


# ===================================================== production switch


class ControlSession:
    """Session double holding one JSONB document."""

    def __init__(self, document: Any = None) -> None:
        self.document = document
        self.commits = 0

    async def execute(self, statement: Any) -> Any:
        compiled = str(statement).lower()
        if compiled.startswith("insert"):
            for key, value in (getattr(statement, "_values", {}) or {}).items():
                if getattr(key, "name", str(key)) == "value":
                    self.document = getattr(value, "value", value)
            return _Empty()
        return _Scalar(self.document)

    async def commit(self) -> None:
        self.commits += 1

    def as_session(self) -> AsyncSession:
        return cast(AsyncSession, self)


class _Scalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _Empty:
    def scalar_one_or_none(self) -> Any:
        return None


async def test_production_is_paused_by_default() -> None:
    control = await load_control(ControlSession().as_session())

    assert control.enabled is False
    assert control.interval_minutes == DEFAULT_INTERVAL_MINUTES
    assert control.from_database is False


async def test_a_stored_document_wins() -> None:
    session = ControlSession({"enabled": False, "interval_minutes": 30})

    control = await load_control(session.as_session())

    assert control.enabled is False
    assert control.interval_minutes == 30
    assert control.from_database is True


@pytest.mark.parametrize(
    "document", ["not-a-dict", {}, {"enabled": "false"}, {"enabled": 1}]
)
async def test_a_malformed_document_keeps_pipeline_paused(document: Any) -> None:
    control = await load_control(ControlSession(document).as_session())

    assert control.enabled is False


async def test_an_out_of_range_stored_interval_is_clamped() -> None:
    """A hand-edited row must not produce a per-second cron."""
    session = ControlSession({"enabled": True, "interval_minutes": 1})

    control = await load_control(session.as_session())

    assert control.interval_minutes == MIN_INTERVAL_MINUTES


async def test_saving_round_trips_the_document() -> None:
    session = ControlSession()

    control = await save_control(
        session.as_session(), enabled=False, interval_minutes=20
    )

    assert control.enabled is False
    assert control.interval_minutes == 20
    assert session.document == {"enabled": False, "interval_minutes": 20}


@pytest.mark.parametrize(
    "bad", [0, 1, MIN_INTERVAL_MINUTES - 1, MAX_INTERVAL_MINUTES + 1]
)
async def test_an_invalid_interval_is_refused(bad: int) -> None:
    session = ControlSession()

    with pytest.raises(ScheduleError, match="Интервал"):
        await save_control(session.as_session(), enabled=True, interval_minutes=bad)


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        (15, (0, 15, 30, 45)),
        (30, (0, 30)),
        (5, (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)),
    ],
)
def test_the_cron_schedule_is_derived_from_the_interval(
    interval: int, expected: tuple[int, ...]
) -> None:
    """Derived, not stored, so the two can never disagree."""
    control = PipelineControl(
        enabled=True, interval_minutes=interval, from_database=True
    )

    assert control.cron_minutes == expected


def test_an_interval_that_does_not_divide_an_hour_still_works() -> None:
    """25 minutes is a legitimate choice; the last gap is simply shorter."""
    control = PipelineControl(enabled=True, interval_minutes=25, from_database=True)

    assert control.cron_minutes == (0, 25, 50)


def test_media_details_use_a_key_jinja_cannot_shadow() -> None:
    """`details["items"]` collides with `dict.items` in a Jinja attribute lookup.

    Observed in production: the media stage rendered `d.items`, Jinja resolved it
    to the bound `dict.items` method, and the whole run page returned 500 after a
    25-second pass had already succeeded. The template now subscripts the key, and
    this test pins the shape the template depends on.
    """
    template = (
        Path("app/templates/dashboard/partials/lab_run.html").read_text(
            encoding="utf-8"
        )
    )

    assert "d['items']" in template
    # The attribute form must not come back: it fails only at render time, and
    # only on runs that actually have media.
    assert "d.items" not in template


# ================================================ platform adaptation stages


def test_the_platform_stages_are_part_of_the_contract() -> None:
    """Adding an adapter should be a registration, not a redesign."""
    for stage in (StageName.TELEGRAM, StageName.DZEN, StageName.VK):
        assert stage in STAGE_TITLES


async def test_platform_stages_adapt_without_posting(store: ArtifactStore) -> None:
    """Each module reshapes the article; a test run must not publish anything.

    The reshaping is the part worth inspecting: every platform truncates, strips
    tags and places the link differently, so a caption that reads fine in the
    article can arrive on Telegram cut mid-sentence.
    """
    runner = PipelineRunner(cast(Any, object()), mode=RunMode.TEST, store=store)
    draft = {
        "title": "В Москве открыли станцию метро «Пыхтино»",
        "content_html": "<h2>Подзаголовок</h2><p>" + ("Текст новости. " * 40) + "</p>",
        "seo_description": "Короткое описание материала.",
        "seo_keywords": ["метро", "Москва", "транспорт"],
        "url_slug": "stanciya-pyhtino",
    }

    await runner._stage_platforms(draft, "https://example.test/stanciya-pyhtino")

    stages = {result.stage: result for result in runner.summary.stages}
    assert set(stages) == {StageName.TELEGRAM, StageName.DZEN, StageName.VK}

    telegram = stages[StageName.TELEGRAM]
    assert telegram.details["length"] <= telegram.details["limit"]
    assert "#метро" in " ".join(telegram.details["hashtags"])
    # No adapter was called, so no run can post from the lab.
    assert telegram.tokens_spent is False

    dzen = stages[StageName.DZEN]
    # The body survives the tag filter: an unsupported tag must not take its text.
    assert "Текст новости" in dzen.details["html"]

    vk = stages[StageName.VK]
    assert draft["title"] in vk.details["text"]
    # VK gets the link as an attachment, so the text itself carries no URL.
    assert "https://" not in vk.details["text"]


async def test_a_thin_caption_is_flagged(store: ArtifactStore) -> None:
    """The teaser builder trims hard, so a tiny caption is the signal worth showing.

    An earlier version warned when the caption hit Telegram's 1024-unit limit; that
    branch was unreachable, because `build_caption` reduces the body to a teaser
    long before the limit. A caption that came out very short usually means the
    article had little plain prose, which an operator should see before it ships.
    """
    runner = PipelineRunner(cast(Any, object()), mode=RunMode.TEST, store=store)
    draft = {
        "title": "Кратко",
        "content_html": "<p>Одно предложение.</p>",
        "seo_description": "",
        "seo_keywords": ["новости"],
        "url_slug": "short",
    }

    await runner._stage_platforms(draft, "https://example.test/short")

    telegram = next(
        result
        for result in runner.summary.stages
        if result.stage is StageName.TELEGRAM
    )
    assert telegram.warnings
    assert "короткой" in telegram.warnings[0]
