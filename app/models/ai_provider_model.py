"""AI provider models: discovered models with working status."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.ai_provider_key import AIProviderKey


class AIProviderModel(Base):
    """One model discovered from an AI provider's /v1/models endpoint.

    Tracks whether the model is working (tested successfully) or not.
    The model_id is the provider's identifier (e.g., "gpt-4o", "claude-3.5-sonnet").
    """

    __tablename__ = "ai_provider_models"
    __table_args__ = (
        Index("ix_ai_provider_models_key_id", "provider_key_id"),
        Index("ix_ai_provider_models_working", "is_working"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider_key_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ai_provider_keys.id", ondelete="CASCADE"),
        nullable=False,
    )
    model_id: Mapped[str] = mapped_column(String(256), nullable=False)
    """Provider's model identifier (e.g., 'gpt-4o', 'claude-3.5-sonnet')."""

    is_working: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(
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

    provider_key: Mapped[AIProviderKey] = relationship(back_populates="models")
