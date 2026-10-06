"""Lab run state for stepwise pipeline execution."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.lab_attempt import LabRunItem


class LabRun(Base):
    """State of a stepwise lab run.

    Tracks which step is active, which clusters have been processed, and the
    current status of each cluster at each step. This allows the UI to show
    progress and resume from where it left off.
    """

    __tablename__ = "lab_runs"
    __table_args__ = (
        Index("ix_lab_runs_status", "status"),
        Index("ix_lab_runs_started_at", "started_at"),
        Index("ix_lab_runs_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    """pending, running, paused, completed, failed"""

    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """0=collect, 1=research, 2=media_check, 3=generate"""

    total_clusters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_clusters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    cluster_states: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    """{cluster_id: {step: status, step_results: {...}}}"""

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list[LabRunItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )
