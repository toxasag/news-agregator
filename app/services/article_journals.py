"""Small persistent index for per-article pipeline artifacts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_journal import ArticleJournal
from app.services.pipeline_stages import (
    ArtifactStore,
    RunMode,
    RunSummary,
    StageName,
    StageResult,
    new_run_id,
)

__all__ = [
    "index_article_journal",
    "load_article_journals",
    "record_article_journal",
]

_ARTIFACT_ROOT_NAME: Final[str] = "runs"


def _artifact_path(store: ArtifactStore, run_id: str) -> str:
    """Return a stable relative path after validating the generated identifier."""
    return str(Path(_ARTIFACT_ROOT_NAME) / store.run_dir(run_id).name)


async def index_article_journal(
    session: AsyncSession,
    *,
    article_id: int,
    summary: RunSummary,
    store: ArtifactStore,
) -> None:
    """Upsert the compact article-to-artifact index for one completed run."""
    cluster_id = uuid.UUID(summary.cluster_id) if summary.cluster_id else None
    statement = (
        pg_insert(ArticleJournal)
        .values(
            article_id=article_id,
            cluster_id=cluster_id,
            run_id=summary.run_id,
            mode=summary.mode.value,
            outcome="failed" if summary.failed else "completed",
            artifact_path=_artifact_path(store, summary.run_id),
            started_at=summary.started_at,
            finished_at=summary.finished_at,
        )
        .on_conflict_do_update(
            index_elements=[ArticleJournal.run_id],
            set_={
                "article_id": article_id,
                "cluster_id": cluster_id,
                "mode": summary.mode.value,
                "outcome": "failed" if summary.failed else "completed",
                "artifact_path": _artifact_path(store, summary.run_id),
                "started_at": summary.started_at,
                "finished_at": summary.finished_at,
            },
        )
    )
    await session.execute(statement)


async def record_article_journal(
    session: AsyncSession,
    *,
    article_id: int,
    cluster_id: uuid.UUID,
    details: dict[str, Any],
    warnings: list[str],
) -> str:
    """Create evidence for one production article and persist its compact index.

    The article is committed before this function is called. A journal failure must
    never roll back paid content generation, but callers receive the exception so
    they can expose the missing audit trail as an explicit warning.
    """
    started_at = datetime.now(UTC)
    summary = RunSummary(
        run_id=new_run_id(),
        mode=RunMode.LIVE,
        started_at=started_at,
        article_id=article_id,
        cluster_id=str(cluster_id),
    )
    result = StageResult(stage=StageName.ARTICLE)
    result.summary = "Материал сохранён в журнале production-прогона."
    result.details = details
    result.warnings = list(warnings)
    summary.stages.append(result)
    summary.finished_at = datetime.now(UTC)

    store = ArtifactStore()
    store.write_stage(summary.run_id, 1, result)
    store.write_summary(summary)
    await index_article_journal(
        session, article_id=article_id, summary=summary, store=store
    )
    await session.commit()
    return summary.run_id


async def load_article_journals(
    session: AsyncSession, article_id: int
) -> list[ArticleJournal]:
    """Return newest journals for an article without reading artifact payloads."""
    result = await session.execute(
        select(ArticleJournal)
        .where(ArticleJournal.article_id == article_id)
        .order_by(ArticleJournal.started_at.desc(), ArticleJournal.id.desc())
    )
    return list(result.scalars().all())
