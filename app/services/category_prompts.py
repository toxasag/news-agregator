"""Operator-editable rubric prompts, resolved over the built-in defaults.

The built-in editorial matrix lives in :mod:`app.services.content_prompts`
(twenty fixed rubrics, six voices). This module adds the two things the
dashboard needs on top of it:

* **Overrides.** A row in ``category_prompts`` for a built-in slug replaces the
  code default for the fields it carries. Editing a voice from the panel then
  takes effect on the next generation without a redeploy.
* **Custom rubrics.** ``is_custom`` rows are new classification targets: they
  appear in the catalogue the moderation prompt offers, and the response
  schema's ``category_slug`` enum accepts them.

Resolution mirrors :mod:`app.services.model_routing`: database row first,
built-in default second, and a malformed database never raises — a pipeline
that cannot read its overrides must still write with the built-in voices.

Custom rubric slugs are validated against the same pattern the built-in ones
follow (lowercase latin + dashes) because they land in
``articles.category``/``news_clusters.category`` and in the JSON schema enum.
"""

from __future__ import annotations

# Operator-facing strings are Russian, matching the dashboard.
# ruff: noqa: RUF001
import logging
import re
from collections.abc import Mapping
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category_prompt import CategoryPrompt
from app.services.content_prompts import (
    CATEGORIES,
    CATEGORY_SLUGS,
    EditorialCategory,
)

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MAX_PROMPT_CHARS",
    "PromptError",
    "catalog_slugs",
    "create_custom_prompt",
    "delete_prompt_override",
    "load_prompt_overrides",
    "resolve_category",
    "save_prompt_override",
]

#: Upper bound on any single prompt field. A textarea paste accident should be
#: rejected loudly rather than silently producing a request the gateway
#: refuses for size — and the value ends up in every generation prompt.
MAX_PROMPT_CHARS: Final[int] = 4000

#: Rubric slugs land in ``articles.category``, in the JSON-schema enum and in
#: URLs; the built-ins all follow this pattern.
_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{1,126}$")

#: Fields of :class:`EditorialCategory` the operator may override.
EDITABLE_FIELDS: Final[tuple[str, ...]] = ("voice", "lead", "avoid", "structure")


class PromptError(ValueError):
    """A prompt change was rejected. The message is operator-facing."""


def _clean(value: str, field: str) -> str:
    """Validate one prompt field."""
    candidate = value.strip()
    if not candidate:
        raise PromptError(f"Поле «{field}» не может быть пустым.")
    if len(candidate) > MAX_PROMPT_CHARS:
        raise PromptError(
            f"Поле «{field}» длиннее {MAX_PROMPT_CHARS} символов "
            f"(сейчас {len(candidate)})."
        )
    return candidate


def _validate_slug(slug: str) -> str:
    candidate = slug.strip().lower()
    if not _SLUG_RE.match(candidate):
        raise PromptError(
            "Слаг рубрики: маленькие латинские буквы, цифры и дефисы, "
            "2–127 символов."
        )
    return candidate


async def load_prompt_overrides(session: AsyncSession) -> dict[str, CategoryPrompt]:
    """Return every stored row keyed by slug. Empty on any database failure.

    Read-once-per-call rather than cached: an operator tunes a voice and presses
    "Article without collection" seconds later, and a cached override would keep writing
    with the previous prompt — the exact opposite of what editing is for.
    """
    try:
        result = await session.execute(select(CategoryPrompt))
        return {row.slug: row for row in result.scalars().all()}
    except SQLAlchemyError:
        logger.warning("Could not read prompt overrides; using built-ins", exc_info=True)
        return {}


def resolve_category(
    slug: str, overrides: dict[str, CategoryPrompt] | None = None
) -> EditorialCategory:
    """Resolve one rubric over the overrides, falling back to the built-ins.

    Falls back to the built-in default rubric when the slug is unknown at all,
    for the same reason :func:`content_prompts.get_category` does: a
    mis-classified article must still generate.
    """
    stored = (overrides or {}).get(slug.strip().lower())
    if stored is None:
        return CATEGORIES.get(slug.strip().lower()) or CATEGORIES[
            _default_slug()
        ]
    return EditorialCategory(
        slug=stored.slug,
        title_ru=stored.title,
        voice=stored.voice,
        lead=stored.lead,
        avoid=stored.avoid,
        structure=stored.structure,
    )


def _default_slug() -> str:
    from app.services.categories import DEFAULT_CATEGORY  # noqa: PLC0415 - cycle-safe

    return DEFAULT_CATEGORY


def catalog_slugs(
    overrides: Mapping[str, CategoryPrompt],
) -> tuple[str, ...]:
    """Every valid classification target: built-ins plus custom rubrics.

    Sorted so the generated JSON schema stays byte-stable between runs — the
    same reason ``CATEGORY_SLUGS`` is sorted — keeping a provider-side prompt
    cache from missing on key order alone.
    """
    custom = {slug for slug, row in overrides.items() if row.is_custom}
    return tuple(sorted(set(CATEGORY_SLUGS) | custom))


async def save_prompt_override(
    session: AsyncSession, slug: str, fields: dict[str, str]
) -> CategoryPrompt:
    """Create or update the override row for a built-in slug.

    Every editable field must be present: a partial row would resolve with an
    empty ``lead`` or ``avoid``, and an empty instruction actively damages the
    generated article rather than merely failing to improve it.
    """
    candidate = slug.strip().lower()
    if candidate not in CATEGORIES:
        raise PromptError(f"Неизвестная встроенная рубрика: {slug}.")
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise PromptError(f"Неизвестные поля промта: {', '.join(sorted(unknown))}.")

    cleaned = {name: _clean(fields[name], name) for name in EDITABLE_FIELDS}

    built_in = CATEGORIES[candidate]
    result = await session.execute(
        select(CategoryPrompt).where(CategoryPrompt.slug == candidate)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = CategoryPrompt(
            slug=candidate,
            title=built_in.title_ru,
            voice=cleaned["voice"],
            lead=cleaned["lead"],
            avoid=cleaned["avoid"],
            structure=cleaned["structure"],
            is_custom=False,
        )
        session.add(row)
    else:
        if row.is_custom:
            raise PromptError(
                "Это кастомная рубрика: сохраняйте её через форму рубрики."
            )
        row.voice = cleaned["voice"]
        row.lead = cleaned["lead"]
        row.avoid = cleaned["avoid"]
        row.structure = cleaned["structure"]
    await session.commit()
    logger.info("Prompt override saved for rubric %s", candidate)
    return row


async def create_custom_prompt(
    session: AsyncSession,
    *,
    slug: str,
    title: str,
    voice: str,
    lead: str,
    avoid: str,
    structure: str,
    wp_term_id: int | None = None,
) -> CategoryPrompt:
    """Create an operator-defined rubric.

    A slug colliding with a built-in is refused: the built-in already classifies
    into that slug, and two catalog entries for one slug would make the
    moderation prompt ambiguous about which voice applies.
    """
    candidate = _validate_slug(slug)
    if candidate in CATEGORIES:
        raise PromptError(f"Слаг «{candidate}» уже занят встроенной рубрикой.")

    existing = await session.execute(
        select(CategoryPrompt).where(CategoryPrompt.slug == candidate)
    )
    if existing.scalar_one_or_none() is not None:
        raise PromptError(f"Рубрика «{candidate}» уже существует.")

    clean_title = _clean(title, "название")
    row = CategoryPrompt(
        slug=candidate,
        title=clean_title,
        voice=_clean(voice, "голос"),
        lead=_clean(lead, "лид"),
        avoid=_clean(avoid, "недопустимое"),
        structure=_clean(structure, "структура"),
        wp_term_id=wp_term_id,
        is_custom=True,
    )
    session.add(row)
    await session.commit()
    logger.info("Custom rubric %s created", candidate)
    return row


async def delete_prompt_override(session: AsyncSession, slug: str) -> bool:
    """Delete an override or custom rubric. Returns whether a row went.

    Deleting an override restores the built-in voice: that is what «сбросить к
    встроенному» means. Deleting a custom rubric keeps the built-in catalogue
    intact. Built-in rubrics themselves are never deletable — only their
    override row is.
    """
    candidate = slug.strip().lower()
    result = await session.execute(
        select(CategoryPrompt).where(CategoryPrompt.slug == candidate)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    logger.info("Prompt row %s deleted (custom=%s)", candidate, row.is_custom)
    return True
