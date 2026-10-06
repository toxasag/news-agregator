"""Stage contract and artifact storage for observable pipeline runs.

The pipeline already worked end to end, but only as a black box: an operator saw
counters and a finished article, never *why* a post was merged into an existing
cluster or which prompt produced a given headline. That is the difference between
a system you trust and one you hope about.

Every stage now returns a :class:`StageResult` and writes it to disk as a JSON
artifact under ``storage/runs/<run_id>/``. The artifact layout is borrowed
deliberately from the Excalibur pipeline (``research-context.json``,
``article.meta.json``, ``article-qa.md`` per article): files rather than database
columns, because the payload shape changes with every stage the operator wants to
inspect, and a JSONB column would turn each UI tweak into a migration.

Test mode and production mode run the *same* stages. The only differences are who
triggers them (a button versus cron), whether the run persists articles, and that
test mode always records artifacts.
"""

from __future__ import annotations

# Stage titles and operator-facing notes are Russian, matching the dashboard.
# ruff: noqa: RUF001
import json
import logging
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from app.config import settings

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "STAGE_TITLES",
    "ArtifactStore",
    "RunMode",
    "RunSummary",
    "StageName",
    "StageOutcome",
    "StageResult",
    "new_run_id",
]


class StageName(StrEnum):
    """The pipeline stages an operator can inspect, in execution order."""

    RESET = "reset"
    COLLECT = "collect"
    DEDUP = "dedup"
    WATERMARK = "watermark"
    CLUSTER = "cluster"
    MODERATION = "moderation"
    RESEARCH = "research"
    ARTICLE = "article"
    MEDIA = "media"
    TELEGRAM = "telegram"
    DZEN = "dzen"
    VK = "vk"


#: Operator-facing titles, so the timeline reads as a newsroom process rather
#: than as function names.
STAGE_TITLES: Final[dict[StageName, str]] = {
    StageName.RESET: "Сброс очереди",
    StageName.COLLECT: "Сбор новостей",
    StageName.DEDUP: "Дедупликация",
    StageName.WATERMARK: "Проверка медиа на вотермарки",
    StageName.CLUSTER: "Кластер",
    StageName.MODERATION: "Модерация",
    StageName.RESEARCH: "Ресерч",
    StageName.ARTICLE: "Статья для сайта",
    StageName.MEDIA: "Обработка медиа",
    StageName.TELEGRAM: "Модуль Telegram",
    StageName.DZEN: "Модуль Дзен",
    StageName.VK: "Модуль ВКонтакте",
}


class StageOutcome(StrEnum):
    """How a stage ended.

    ``SKIPPED`` is distinct from ``FAILED`` on purpose: a run with no fresh posts
    and a run whose collector crashed look identical in a counter, and only one of
    them is a problem.
    """

    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"


class RunMode(StrEnum):
    """Test runs never publish and never write articles; production runs do."""

    TEST = "test"
    LIVE = "live"


@dataclass(slots=True)
class StageResult:
    """What one stage did, in the form the dashboard renders.

    ``details`` is free-form per stage because the useful evidence differs: the
    dedup stage needs similarity scores and the losing candidates, the article
    stage needs the prompt actually sent. Forcing both into one schema would
    flatten exactly the detail an operator opens the page for.
    """

    stage: StageName
    outcome: StageOutcome = StageOutcome.OK
    #: One-line verdict, always populated — this is the timeline's headline.
    summary: str = ""
    duration_ms: int = 0
    #: Model identifier when the stage called an LLM, empty when it cost nothing.
    #: Surfaced so the zero-token stages stay auditable.
    model: str = ""
    tokens_spent: bool = False
    #: The exact prompt sent, when there was one. Kept verbatim: a paraphrase
    #: would defeat the purpose of showing it.
    prompt: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def title(self) -> str:
        """Operator-facing stage title."""
        return STAGE_TITLES[self.stage]

    def warn(self, message: str) -> None:
        """Record a non-fatal problem without changing the outcome."""
        self.warnings.append(message)
        logger.warning("Stage %s: %s", self.stage.value, message)

    def fail(self, message: str) -> None:
        """Mark the stage failed with an operator-facing reason."""
        self.outcome = StageOutcome.FAILED
        self.error = message
        if not self.summary:
            self.summary = message

    def skip(self, reason: str) -> None:
        """Mark the stage skipped, which is not a failure."""
        self.outcome = StageOutcome.SKIPPED
        self.summary = reason

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the artifact file and the template context."""
        return {
            "stage": self.stage.value,
            "title": self.title,
            "outcome": self.outcome.value,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "model": self.model,
            "tokens_spent": self.tokens_spent,
            "prompt": self.prompt,
            "details": self.details,
            "warnings": list(self.warnings),
            "error": self.error,
        }


@dataclass(slots=True)
class RunSummary:
    """One pipeline run: its stages and how it ended."""

    run_id: str
    mode: RunMode
    started_at: datetime
    finished_at: datetime | None = None
    stages: list[StageResult] = field(default_factory=list)
    #: Set when the run produced an article, so the UI can link to it.
    article_id: int | None = None
    cluster_id: str | None = None

    @property
    def duration_ms(self) -> int:
        if self.finished_at is None:
            return 0
        delta = self.finished_at - self.started_at
        return int(delta.total_seconds() * 1000)

    @property
    def failed(self) -> bool:
        return any(stage.outcome is StageOutcome.FAILED for stage in self.stages)

    @property
    def tokens_spent(self) -> bool:
        """Whether any stage called a paid model."""
        return any(stage.tokens_spent for stage in self.stages)

    def stage(self, name: StageName) -> StageResult | None:
        """Return one stage's result, if it ran."""
        for result in self.stages:
            if result.stage is name:
                return result
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode.value,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": (
                self.finished_at.isoformat(timespec="seconds")
                if self.finished_at
                else None
            ),
            "duration_ms": self.duration_ms,
            "failed": self.failed,
            "tokens_spent": self.tokens_spent,
            "article_id": self.article_id,
            "cluster_id": self.cluster_id,
            "stages": [stage.to_dict() for stage in self.stages],
        }


def new_run_id() -> str:
    """Generate a sortable, human-readable run id.

    Timestamp-prefixed so the runs directory lists chronologically without
    reading every file, with a short random suffix because two runs can start
    inside the same second.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


class ArtifactStore:
    """Writes and reads run artifacts under ``storage/runs/<run_id>/``.

    Files, not database rows. The evidence an operator wants differs per stage and
    grows over time; a JSONB column would make every UI addition a migration, and
    a large prompt or a rendered article does not belong in a row the pipeline
    reads on every tick.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or (settings.media_storage_path.parent / "runs")

    @property
    def root(self) -> Path:
        return self._root

    def run_dir(self, run_id: str) -> Path:
        """Directory for one run, created on demand.

        ``run_id`` is validated rather than trusted: it reaches this method from a
        URL path parameter, and a value like ``../../etc`` would otherwise escape
        the storage root.
        """
        safe = self._safe_id(run_id)
        path = self._root / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _safe_id(run_id: str) -> str:
        """Reject anything that is not a generated run id."""
        candidate = run_id.strip()
        allowed = set("abcdef0123456789-")
        if not candidate or not set(candidate) <= allowed or ".." in candidate:
            raise ValueError(f"unsafe run id: {run_id!r}")
        return candidate

    def _write_json(self, path: Path, value: Mapping[str, Any]) -> Path:
        """Atomically persist JSON without blocking the event loop on filesystem I/O."""
        payload = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        temporary = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def write_stage(self, run_id: str, index: int, result: StageResult) -> Path:
        """Persist one stage artifact with an atomic replacement.

        Numbering preserves execution order when the timeline is rebuilt after a
        page reload. Writing a temporary sibling before replacing the final file
        prevents a crash from leaving a normal-looking partial JSON document.
        """
        path = self.run_dir(run_id) / f"{index:02d}-{result.stage.value}.json"
        return self._write_json(path, result.to_dict())

    def write_summary(self, summary: RunSummary) -> Path:
        """Persist the run index file with an atomic replacement."""
        path = self.run_dir(summary.run_id) / "run.json"
        return self._write_json(path, summary.to_dict())

    def read_summary(self, run_id: str) -> dict[str, Any] | None:
        """Read one run back, or ``None`` when it is absent or unreadable."""
        path = self.run_dir(run_id) / "run.json"
        if not path.is_file():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Run %s has an unreadable summary", run_id, exc_info=True)
            return None
        return loaded if isinstance(loaded, dict) else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent runs, newest first.

        Reads only ``run.json`` per directory: the stage artifacts can be large,
        and the history list needs headlines rather than payloads.
        """
        if not self._root.is_dir():
            return []
        directories = sorted(
            (path for path in self._root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
        runs: list[dict[str, Any]] = []
        for directory in directories[:limit]:
            summary = self.read_summary(directory.name)
            if summary is not None:
                runs.append(summary)
        return runs

    def prune(self, keep: int = 50) -> int:
        """Delete old *orphan* lab runs, returning how many went.

        A run that produced an article is the operator's journal for that
        material and must survive. Only directories whose ``run.json`` has no
        ``article_id`` are candidates, and even those stay until they fall past
        ``keep`` so a recent failed inspection remains inspectable.
        """
        if not self._root.is_dir():
            return 0
        orphans: list[Path] = []
        for directory in sorted(
            (path for path in self._root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        ):
            summary = self.read_summary(directory.name)
            if summary is None or summary.get("article_id") in (None, ""):
                orphans.append(directory)
        removed = 0
        for directory in orphans[keep:]:
            for item in directory.iterdir():
                item.unlink(missing_ok=True)
            directory.rmdir()
            removed += 1
        if removed:
            logger.info("Pruned %d orphan pipeline run(s)", removed)
        return removed

    def purge_all(self) -> int:
        """Delete every run directory. Used by an explicit clean-slate wipe."""
        if not self._root.is_dir():
            return 0
        removed = 0
        for directory in list(self._root.iterdir()):
            if not directory.is_dir():
                continue
            for item in directory.iterdir():
                item.unlink(missing_ok=True)
            directory.rmdir()
            removed += 1
        if removed:
            logger.info("Purged %d pipeline run director(y/ies)", removed)
        return removed


def preview(text: str, limit: int = 400) -> str:
    """Trim text for an artifact, marking that it was trimmed.

    Stage details are rendered in a browser; a 40 kB body in a table cell is not
    evidence, it is a scroll bar.
    """
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…"


def describe_media(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Summarise media metadata for a stage artifact."""
    described: list[dict[str, Any]] = []
    for item in items:
        described.append(
            {
                "type": str(item.get("type") or "?"),
                "media_id": str(item.get("media_id") or ""),
                "size": item.get("size"),
            }
        )
    return described
