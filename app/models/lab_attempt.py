"""Durable manual laboratory runs and immutable stage attempts."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.lab_run import LabRun


class LabStage(StrEnum):
    """Operator-visible stages of a manual laboratory run."""

    INTAKE = "intake"
    RESEARCH = "research"
    MEDIA = "media"
    ARTICLE = "article"


class LabAttemptStatus(StrEnum):
    """A durable lifecycle for one version of a stage result."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class LabRunItem(Base):
    """One cluster selected into a manual run."""

    __tablename__ = "lab_run_items"
    __table_args__ = (
        UniqueConstraint("lab_run_id", "cluster_id"),
        Index("ix_lab_run_items_run_position", "lab_run_id", "position"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lab_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("lab_runs.id", ondelete="CASCADE"), nullable=False
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title_snapshot: Mapped[str] = mapped_column(String(512), nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[LabRun] = relationship(back_populates="items")
    attempts: Mapped[list[LabStageAttempt]] = relationship(
        back_populates="run_item", cascade="all, delete-orphan", passive_deletes=True
    )


class LabStageAttempt(Base):
    """One immutable execution version for an item and manual stage."""

    __tablename__ = "lab_stage_attempts"
    __table_args__ = (
        UniqueConstraint("lab_run_item_id", "stage", "attempt_number"),
        Index("ix_lab_stage_attempts_item_stage", "lab_run_item_id", "stage"),
        Index("ix_lab_stage_attempts_status", "status"),
        Index("ix_lab_stage_attempts_job_id", "job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lab_run_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("lab_run_items.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=LabAttemptStatus.PENDING.value
    )
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision: Mapped[str | None] = mapped_column(String(24), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    run_item: Mapped[LabRunItem] = relationship(back_populates="attempts")


__all__ = ["LabAttemptStatus", "LabRunItem", "LabStage", "LabStageAttempt"]
