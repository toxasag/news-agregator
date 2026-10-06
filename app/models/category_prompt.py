"""Operator-editable rubric prompt overrides (see docs/05_CONTENT_ENGINE.md).

The built-in editorial matrix lives in :mod:`app.services.content_prompts` and
covers twenty fixed rubrics. This table stores only what differs: a row exists
for a rubric once its prompt was edited from the dashboard, or when the
operator created a rubric of their own. The resolution order is database row
first, built-in default second — the same override pattern as
:mod:`app.services.model_routing`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CategoryPrompt(Base):
    """One rubric's prompt as the operator configured it.

    ``slug`` keys into ``articles.category`` / ``news_clusters.category`` for
    built-in rubrics. For custom rubrics the slug is chosen by the operator and
    becomes a new classification target; the moderation prompt offers custom
    rubrics alongside the built-in ones.

    ``is_custom`` separates "an override of a built-in rubric" from "a rubric
    that exists only here": a custom row is deletable, an override is reset by
    deletion too, but a built-in rubric always keeps its code default.
    """

    __tablename__ = "category_prompts"
    __table_args__ = (Index("ix_category_prompts_custom", "is_custom"),)

    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The voice instruction — the core editable prompt text.
    voice: Mapped[str] = mapped_column(Text, nullable=False)
    lead: Mapped[str] = mapped_column(Text, nullable=False)
    avoid: Mapped[str] = mapped_column(Text, nullable=False)
    structure: Mapped[str] = mapped_column(Text, nullable=False)
    #: WordPress term id; NULL until the site is connected from the panel.
    wp_term_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
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


__all__ = ["CategoryPrompt"]
