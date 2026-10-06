"""AI provider keys: encrypted storage for API keys and model status tracking."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.ai_provider_model import AIProviderModel


class AIProviderKey(Base):
    """One API key for an AI provider (OpenRouter, Together, Anthropic, etc.).

    Keys are encrypted at rest using Fernet. The provider_name groups keys
    (e.g., "openrouter", "anthropic", "google", "custom"). The base_url is
    required for OpenAI-compatible providers, optional for Anthropic/Google.

    Models are discovered automatically via /v1/models endpoint and stored
    in :class:`AIProviderModel`. Each model has its own working status.
    """

    __tablename__ = "ai_provider_keys"
    __table_args__ = (
        Index("ix_ai_provider_keys_provider", "provider_name"),
        Index("ix_ai_provider_keys_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    """Provider: openrouter, together, deepseek, groq, anthropic, google, custom."""

    custom_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Custom display name for the provider / endpoint (e.g. 'vsllm.cc', 'My vLLM')."""

    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    """Fernet-encrypted API key."""

    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """Base URL for OpenAI-compatible providers. None for Anthropic/Google."""

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    health: Mapped[str] = mapped_column(
        String(24), nullable=False, default="untested", server_default="untested"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Last error message when testing models or fetching the model list."""

    models_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    working_models_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    models: Mapped[list[AIProviderModel]] = relationship(
        back_populates="provider_key",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
