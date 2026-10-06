"""Durable orchestration for the operator-approved laboratory workflow."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.cluster import NewsCluster
from app.models.lab_attempt import (
    LabAttemptStatus,
    LabRunItem,
    LabStage,
    LabStageAttempt,
)
from app.models.lab_run import LabRun
from app.models.raw_news import RawNews
from app.services.pipeline_inspection import InspectionRunner
from app.services.pipeline_runner import PipelineRunner
from app.services.pipeline_stages import RunMode

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "LAB_QUEUE_NAME",
    "LabWorkflowError",
    "LabWorkflowService",
    "execute_stage_attempt",
    "recover_unenqueued_attempts",
]

LAB_QUEUE_NAME: Final[str] = "arq:queue:lab"
_LEASE_SECONDS: Final[int] = 15 * 60
_STAGE_ORDER: Final[tuple[LabStage, ...]] = (
    LabStage.INTAKE,
    LabStage.RESEARCH,
    LabStage.MEDIA,
    LabStage.ARTICLE,
)
_ACTIVE_STATUSES: Final[tuple[str, ...]] = (
    LabAttemptStatus.QUEUED.value,
    LabAttemptStatus.RUNNING.value,
    LabAttemptStatus.AWAITING_REVIEW.value,
)


class LabWorkflowError(ValueError):
    """A requested manual workflow action is not valid in its current state."""


class LabWorkflowService:
    """Create, queue and approve versioned manual laboratory work."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(self, *, collect: bool) -> LabRun:
        """Take a fixed snapshot of eligible clusters after optional collection."""
        if collect:
            runner = PipelineRunner(self._session, mode=RunMode.TEST)
            await runner._stage_collect()

        run = LabRun(
            run_id=f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
            status="awaiting_review",
            current_step=0,
            total_clusters=0,
            processed_clusters=0,
            cluster_states={},
        )
        self._session.add(run)
        await self._session.flush()

        rows = await self._session.execute(
            select(NewsCluster, func.count(RawNews.id))
            .outerjoin(RawNews, RawNews.cluster_id == NewsCluster.id)
            .where(NewsCluster.is_published.is_(False))
            .group_by(NewsCluster.id)
            .order_by(NewsCluster.created_at.desc())
        )
        item_count = 0
        for position, (cluster, source_count) in enumerate(rows.all(), start=1):
            item_count = position
            item = LabRunItem(
                lab_run_id=run.id,
                cluster_id=cluster.id,
                position=position,
                title_snapshot=cluster.title[:512],
                source_count=int(source_count),
            )
            self._session.add(item)
            await self._session.flush()
            self._session.add(
                LabStageAttempt(
                    lab_run_item_id=item.id,
                    stage=LabStage.INTAKE.value,
                    attempt_number=1,
                    status=LabAttemptStatus.AWAITING_REVIEW.value,
                    input_snapshot={
                        "cluster_id": str(cluster.id),
                        "title": cluster.title,
                        "source_count": int(source_count),
                    },
                    result={
                        "title": cluster.title,
                        "source_count": int(source_count),
                        "reason": (
                            "intake snapshot created after collection and deduplication"
                        ),
                    },
                    idempotency_key=f"{run.run_id}:{item.id}:intake:1",
                    finished_at=datetime.now(UTC),
                )
            )

        run.total_clusters = item_count
        await self._session.commit()
        return await self.load_run(run.run_id)

    async def load_run(self, run_id: str) -> LabRun:
        """Load a full run tree for server rendering without lazy I/O."""
        result = await self._session.execute(
            select(LabRun)
            .where(LabRun.run_id == run_id)
            .options(
                selectinload(LabRun.items).selectinload(LabRunItem.attempts)
            )
        )
        run = result.scalar_one_or_none()
        if run is None:
            raise LabWorkflowError(f"Прогон {run_id} не найден.")
        return run

    async def approve_attempt(self, attempt_id: int) -> LabStageAttempt:
        """Approve the exact awaiting-review version selected by the operator."""
        result = await self._session.execute(
            select(LabStageAttempt)
            .where(LabStageAttempt.id == attempt_id)
            .with_for_update()
        )
        attempt = result.scalar_one_or_none()
        if attempt is None:
            raise LabWorkflowError("Результат этапа не найден.")
        if attempt.status != LabAttemptStatus.AWAITING_REVIEW.value:
            raise LabWorkflowError("Эта версия уже не ожидает одобрения.")
        latest = await self._latest_attempt(attempt.lab_run_item_id, attempt.stage)
        if latest is None or latest.id != attempt.id:
            raise LabWorkflowError("Показана устаревшая версия результата.")

        attempt.status = LabAttemptStatus.APPROVED.value
        attempt.decision = "approved"
        attempt.decided_at = datetime.now(UTC)
        await self._session.commit()
        return attempt

    async def reject_attempt(self, attempt_id: int) -> LabStageAttempt:
        """Reject a reviewed version while preserving it for the run history."""
        result = await self._session.execute(
            select(LabStageAttempt)
            .where(LabStageAttempt.id == attempt_id)
            .with_for_update()
        )
        attempt = result.scalar_one_or_none()
        if attempt is None:
            raise LabWorkflowError("Результат этапа не найден.")
        if attempt.status != LabAttemptStatus.AWAITING_REVIEW.value:
            raise LabWorkflowError("Эта версия уже не ожидает решения.")
        attempt.status = LabAttemptStatus.CANCELLED.value
        attempt.decision = "rejected"
        attempt.decided_at = datetime.now(UTC)
        await self._session.commit()
        return attempt

    async def queue_stage(self, item_id: int, stage: LabStage) -> LabStageAttempt:
        """Create one idempotent queued stage attempt for an eligible run item."""
        item = await self._load_item_for_update(item_id)
        self._assert_stage_ready(item, stage)
        existing = await self._active_attempt(item.id, stage)
        if existing is not None:
            return existing

        attempt_number = await self._next_attempt_number(item.id, stage)
        await self._mark_dependents_stale(item.id, stage)
        attempt = LabStageAttempt(
            lab_run_item_id=item.id,
            stage=stage.value,
            attempt_number=attempt_number,
            status=LabAttemptStatus.QUEUED.value,
            input_snapshot=await self._input_snapshot(item, stage),
            idempotency_key=f"{item.lab_run_id}:{item.id}:{stage.value}:{attempt_number}",
        )
        self._session.add(attempt)
        await self._session.commit()
        await self._enqueue(attempt)
        return attempt

    async def _load_item_for_update(self, item_id: int) -> LabRunItem:
        result = await self._session.execute(
            select(LabRunItem)
            .where(LabRunItem.id == item_id)
            .options(selectinload(LabRunItem.attempts))
            .with_for_update()
        )
        item = result.scalar_one_or_none()
        if item is None:
            raise LabWorkflowError("Новость этого прогона не найдена.")
        return item

    async def _latest_attempt(
        self, item_id: int, stage: str
    ) -> LabStageAttempt | None:
        result = await self._session.execute(
            select(LabStageAttempt)
            .where(
                LabStageAttempt.lab_run_item_id == item_id,
                LabStageAttempt.stage == stage,
            )
            .order_by(LabStageAttempt.attempt_number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _active_attempt(
        self, item_id: int, stage: LabStage
    ) -> LabStageAttempt | None:
        result = await self._session.execute(
            select(LabStageAttempt)
            .where(
                LabStageAttempt.lab_run_item_id == item_id,
                LabStageAttempt.stage == stage.value,
                LabStageAttempt.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(LabStageAttempt.attempt_number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _next_attempt_number(self, item_id: int, stage: LabStage) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.max(LabStageAttempt.attempt_number), 0)).where(
                LabStageAttempt.lab_run_item_id == item_id,
                LabStageAttempt.stage == stage.value,
            )
        )
        return int(result.scalar_one()) + 1

    async def _mark_dependents_stale(self, item_id: int, stage: LabStage) -> None:
        stage_index = _STAGE_ORDER.index(stage)
        later_stages = [member.value for member in _STAGE_ORDER[stage_index + 1 :]]
        if not later_stages:
            return
        await self._session.execute(
            update(LabStageAttempt)
            .where(
                LabStageAttempt.lab_run_item_id == item_id,
                LabStageAttempt.stage.in_(later_stages),
                LabStageAttempt.status.in_(
                    (
                        LabAttemptStatus.AWAITING_REVIEW.value,
                        LabAttemptStatus.APPROVED.value,
                    )
                ),
            )
            .values(status=LabAttemptStatus.STALE.value)
        )

    def _assert_stage_ready(self, item: LabRunItem, stage: LabStage) -> None:
        stage_index = _STAGE_ORDER.index(stage)
        if stage_index == 0:
            raise LabWorkflowError("Intake creates a run and cannot start here.")
        predecessor = _STAGE_ORDER[stage_index - 1]
        attempts = [
            attempt
            for attempt in item.attempts
            if attempt.stage == predecessor.value
        ]
        if not any(
            attempt.status == LabAttemptStatus.APPROVED.value for attempt in attempts
        ):
            raise LabWorkflowError("Сначала одобрите предыдущий этап этой новости.")

    async def _input_snapshot(
        self, item: LabRunItem, stage: LabStage
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "cluster_id": str(item.cluster_id),
            "title": item.title_snapshot,
        }
        if stage is LabStage.RESEARCH:
            return snapshot
        previous = _STAGE_ORDER[_STAGE_ORDER.index(stage) - 1]
        previous_attempt = await self._latest_attempt(item.id, previous.value)
        if previous_attempt is not None and previous_attempt.result is not None:
            snapshot[previous.value] = previous_attempt.result
        return snapshot

    async def _enqueue(self, attempt: LabStageAttempt) -> None:
        try:
            pool = await create_pool(RedisSettings.from_dsn(str(settings.redis_url)))
            try:
                job: Job | None = await pool.enqueue_job(
                    "lab_stage_task",
                    attempt_id=attempt.id,
                    _queue_name=LAB_QUEUE_NAME,
                    _job_id=attempt.idempotency_key,
                )
            finally:
                await pool.close()
        except RedisError:
            logger.warning("Lab attempt %s awaits dispatcher recovery", attempt.id)
            return
        if job is not None:
            attempt.job_id = job.job_id
            await self._session.commit()


async def execute_stage_attempt(attempt_id: int) -> None:
    """Claim and execute one queued attempt in its own short-lived DB session."""
    async with AsyncSessionLocal() as session:
        attempt = await _claim_attempt(session, attempt_id)
        if attempt is None:
            return
        try:
            result = await _run_stage(session, attempt)
        except Exception as error:
            attempt.status = LabAttemptStatus.FAILED.value
            attempt.error_message = f"{type(error).__name__}: {error}"[:2000]
            attempt.finished_at = datetime.now(UTC)
            await session.commit()
            logger.exception("Lab stage attempt %s failed", attempt.id)
            return

        attempt.result = result
        attempt.status = LabAttemptStatus.AWAITING_REVIEW.value
        attempt.finished_at = datetime.now(UTC)
        attempt.lease_until = None
        await session.commit()


async def _claim_attempt(
    session: AsyncSession, attempt_id: int
) -> LabStageAttempt | None:
    result = await session.execute(
        select(LabStageAttempt)
        .where(
            LabStageAttempt.id == attempt_id,
            LabStageAttempt.status == LabAttemptStatus.QUEUED.value,
        )
        .with_for_update(skip_locked=True)
    )
    attempt = result.scalar_one_or_none()
    if attempt is None:
        return None
    now = datetime.now(UTC)
    attempt.status = LabAttemptStatus.RUNNING.value
    attempt.started_at = now
    attempt.lease_until = now + timedelta(seconds=_LEASE_SECONDS)
    await session.commit()
    return attempt


async def _run_stage(
    session: AsyncSession, attempt: LabStageAttempt
) -> dict[str, Any]:
    item = await session.get(LabRunItem, attempt.lab_run_item_id)
    if item is None:
        raise LabWorkflowError("Новость прогона была удалена.")
    cluster = await session.get(NewsCluster, item.cluster_id)
    if cluster is None:
        raise LabWorkflowError("Кластер был удалён после создания прогона.")
    posts_result = await session.execute(
        select(RawNews).where(RawNews.cluster_id == cluster.id).order_by(RawNews.id)
    )
    posts = list(posts_result.scalars().all())
    stage = LabStage(attempt.stage)

    if stage is LabStage.RESEARCH:
        runner = PipelineRunner(session, mode=RunMode.TEST)
        return await runner._stage_research(cluster, posts)
    if stage is LabStage.MEDIA:
        inspector = InspectionRunner(session)
        report = await inspector._stage_watermarks(posts)
        return dict(report.details)
    if stage is LabStage.ARTICLE:
        research = attempt.input_snapshot.get(LabStage.RESEARCH.value)
        if not isinstance(research, dict):
            raise LabWorkflowError("Одобренный ресерч не найден для генерации.")
        runner = PipelineRunner(session, mode=RunMode.TEST)
        draft = await runner._stage_article(cluster, posts, research)
        if draft is None:
            raise LabWorkflowError("Генерация не вернула пригодный черновик.")
        return draft
    raise LabWorkflowError(f"Этап {stage.value} не исполняется отдельной задачей.")


async def recover_unenqueued_attempts() -> int:
    """Requeue committed attempts that could not reach Redis during the HTTP request."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LabStageAttempt)
            .where(
                LabStageAttempt.status == LabAttemptStatus.QUEUED.value,
                LabStageAttempt.job_id.is_(None),
            )
            .order_by(LabStageAttempt.created_at)
            .limit(20)
            .with_for_update(skip_locked=True)
        )
        attempts = list(result.scalars().all())
        pool = await create_pool(RedisSettings.from_dsn(str(settings.redis_url)))
        try:
            for attempt in attempts:
                try:
                    job: Job | None = await pool.enqueue_job(
                        "lab_stage_task",
                        attempt_id=attempt.id,
                        _queue_name=LAB_QUEUE_NAME,
                        _job_id=attempt.idempotency_key,
                    )
                except RedisError:
                    logger.warning("Lab attempt %s still cannot be enqueued", attempt.id)
                    continue
                if job is not None:
                    attempt.job_id = job.job_id
        finally:
            await pool.close()
        await session.commit()
        return len(attempts)
