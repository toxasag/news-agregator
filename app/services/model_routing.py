"""Runtime-editable AI model routing.

Each pipeline stage picks its own model: the deduplication arbiter, the article
writer, the SEO pass and the embedding call. Those defaults live in the
environment (``AI_MODEL_WRITER`` and friends), but which models a gateway offers
changes without warning — a provider is added, a name is retired, an account
loses access to one family. Waiting for a redeploy to follow that is the wrong
trade, so the dashboard can override each stage and the override wins.

The same pattern as :mod:`app.services.admin_credentials`, and for the same two
reasons: ``.env`` does not exist inside the container, and :class:`Settings` is
frozen behind an ``lru_cache``, so a file write would not reach the running
process.

Overrides are read per call rather than cached. A cached value would keep routing
to a model the operator had just replaced, which is the opposite of what changing
it is for.
"""

from __future__ import annotations

# Operator-facing rejection messages are Russian, matching the dashboard.
import logging
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.setting import SystemSetting

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "AI_CHAINS_KEY",
    "AI_MODELS_KEY",
    "CHAIN_LENGTH",
    "CHAT_ROLES",
    "MODEL_ROLES",
    "ModelError",
    "ModelRouting",
    "load_chains",
    "load_model_routing",
    "resolve_chain",
    "resolve_model",
    "save_chains",
    "save_model_routing",
]

#: Single ``system_settings`` key holding the override document.
AI_MODELS_KEY: Final[str] = "ai_model_routing"

#: The four routable stages: form field, settings attribute, operator label.
#: Ordered as the pipeline runs them, which is how the settings page lists them.
MODEL_ROLES: Final[tuple[tuple[str, str, str], ...]] = (
    ("arbiter", "ai_model_arbiter", "Арбитр дедупликации"),
    ("writer", "ai_model_writer", "Генератор текста"),
    ("seo", "ai_model_seo", "SEO и корректура"),
    ("embedding", "ai_model_embedding", "Эмбеддинги"),
    ("image", "ai_model_image", "Генератор обложек и иллюстраций"),
)

#: Roles answered by the gateway's chat-completions endpoint, and therefore the
#: only ones a chat probe can say anything about. `embedding` is excluded: it is
#: served locally by fastembed, so probing it with a chat completion reports a
#: dead model no matter how well local inference is working — a red row that
#: means nothing is worse than no row.
CHAT_ROLES: Final[frozenset[str]] = frozenset({"arbiter", "writer", "seo"})

#: Bound on a model identifier. Gateway names are short; anything longer is a
#: paste accident, and the value ends up in an HTTP payload.
MAX_MODEL_LENGTH: Final[int] = 128


class ModelError(ValueError):
    """A model override was rejected. The message is operator-facing."""


@dataclass(frozen=True, slots=True)
class ModelRouting:
    """The active model per stage, and whether it was overridden."""

    arbiter: str
    writer: str
    seo: str
    embedding: str
    image: str
    #: Roles whose value came from the database rather than the environment.
    overridden: frozenset[str]

    def for_role(self, role: str) -> str:
        """Return the model routed to ``role``."""
        return str(getattr(self, role))


def _environment_routing() -> ModelRouting:
    """Model routing as configured in the environment."""
    return ModelRouting(
        arbiter=settings.ai_model_arbiter,
        writer=settings.ai_model_writer,
        seo=settings.ai_model_seo,
        embedding=settings.ai_model_embedding,
        image=settings.ai_model_image,
        overridden=frozenset(),
    )


async def load_model_routing(session: AsyncSession) -> ModelRouting:
    """Return the active routing, preferring stored overrides.

    A malformed document falls back to the environment rather than raising: an
    unexpected JSONB value must not stop the pipeline from generating anything.
    Each role falls back independently, so one bad entry does not discard the
    other three.
    """
    try:
        result = await session.execute(
            select(SystemSetting.value).where(SystemSetting.key == AI_MODELS_KEY)
        )
        stored = result.scalar_one_or_none()
    except SQLAlchemyError:
        logger.warning(
            "Could not read AI model overrides; using the environment",
            exc_info=True,
        )
        return _environment_routing()

    if not isinstance(stored, dict):
        return _environment_routing()

    values: dict[str, str] = {}
    overridden: set[str] = set()
    for role, attribute, _label in MODEL_ROLES:
        candidate = str(stored.get(role) or "").strip()
        if candidate:
            values[role] = candidate
            overridden.add(role)
        else:
            values[role] = str(getattr(settings, attribute, ""))

    return ModelRouting(
        arbiter=values["arbiter"],
        writer=values["writer"],
        seo=values["seo"],
        embedding=values["embedding"],
        image=values.get("image") or settings.ai_model_image,
        overridden=frozenset(overridden),
    )


async def resolve_model(session: AsyncSession, role: str) -> str:
    """Return the model for one stage.

    Convenience for a worker that needs a single role and should not care whether
    the value came from the database or the environment.
    """
    routing = await load_model_routing(session)
    return routing.for_role(role)


def _validate(role: str, value: str) -> str:
    """Validate one model identifier."""
    candidate = value.strip()
    if not candidate:
        # Empty is meaningful: it clears the override and restores the default.
        return ""
    if len(candidate) > MAX_MODEL_LENGTH:
        raise ModelError(
            f"Идентификатор модели для «{role}» длиннее "
            f"{MAX_MODEL_LENGTH} символов."
        )
    if any(character.isspace() for character in candidate):
        raise ModelError(
            f"Идентификатор модели для «{role}» не должен содержать пробелов."
        )
    return candidate


async def save_model_routing(
    session: AsyncSession, overrides: dict[str, str]
) -> ModelRouting:
    """Store per-stage model overrides.

    An empty value for a role clears its override, so an operator can return a
    stage to the environment default without knowing what that default is.
    """
    known = {role for role, _attribute, _label in MODEL_ROLES}
    unknown = set(overrides) - known
    if unknown:
        raise ModelError(f"Неизвестные роли моделей: {', '.join(sorted(unknown))}")

    document: dict[str, Any] = {}
    for role, _attribute, label in MODEL_ROLES:
        if role not in overrides:
            continue
        validated = _validate(label, overrides[role])
        if validated:
            document[role] = validated

    statement = (
        pg_insert(SystemSetting)
        .values(
            key=AI_MODELS_KEY,
            value=document,
            description="Per-stage AI model routing, editable from /settings.",
        )
        .on_conflict_do_update(
            index_elements=[SystemSetting.key], set_={"value": document}
        )
    )
    await session.execute(statement)
    await session.commit()
    logger.info("AI model routing updated: %s", sorted(document))
    return await load_model_routing(session)


#: Fallback positions per role. Three is what fits a settings row and covers the
#: realistic failure: the primary is rate limited, the first fallback is a
#: different provider, the second is a cheap last resort.
CHAIN_LENGTH: Final[int] = 3

#: ``system_settings`` key holding the per-role chains.
AI_CHAINS_KEY: Final[str] = "ai_model_chains"


async def load_chains(session: AsyncSession) -> dict[str, list[str]]:
    """Return the ordered model chain per role.

    A role with no configured chain falls back to a single-element chain holding
    its resolved model, so every caller can treat routing as a chain and the
    single-model path stays working without a migration.
    """
    routing = await load_model_routing(session)
    try:
        result = await session.execute(
            select(SystemSetting.value).where(SystemSetting.key == AI_CHAINS_KEY)
        )
        stored = result.scalar_one_or_none()
    except SQLAlchemyError:
        logger.warning(
            "Could not read AI model chains; using single models", exc_info=True
        )
        stored = None

    document = stored if isinstance(stored, dict) else {}
    chains: dict[str, list[str]] = {}
    for role, _attribute, _label in MODEL_ROLES:
        raw = document.get(role)
        chain = (
            [str(item).strip() for item in raw if str(item).strip()]
            if isinstance(raw, list)
            else []
        )
        if not chain:
            chain = [routing.for_role(role)]
        # De-duplicated while preserving order: the same model twice in a chain
        # means a failure is retried against itself, which is exactly what the
        # cooldown exists to avoid.
        unique: list[str] = []
        for model in chain[:CHAIN_LENGTH]:
            if model not in unique:
                unique.append(model)
        chains[role] = unique
    return chains


async def resolve_chain(session: AsyncSession, role: str) -> list[str]:
    """Return one role's model chain."""
    return (await load_chains(session)).get(role) or []


async def save_chains(
    session: AsyncSession, chains: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Persist per-role chains, validating every model identifier."""
    known = {role for role, _attribute, _label in MODEL_ROLES}
    unknown = set(chains) - known
    if unknown:
        raise ModelError(f"Неизвестные роли моделей: {', '.join(sorted(unknown))}")

    document: dict[str, Any] = {}
    for role, _attribute, label in MODEL_ROLES:
        if role not in chains:
            continue
        cleaned: list[str] = []
        for value in chains[role][:CHAIN_LENGTH]:
            validated = _validate(label, value)
            if validated and validated not in cleaned:
                cleaned.append(validated)
        if cleaned:
            document[role] = cleaned

    statement = (
        pg_insert(SystemSetting)
        .values(
            key=AI_CHAINS_KEY,
            value=document,
            description="Ordered fallback chains per AI stage, editable from /settings.",
        )
        .on_conflict_do_update(
            index_elements=[SystemSetting.key], set_={"value": document}
        )
    )
    await session.execute(statement)
    await session.commit()
    logger.info("AI model chains updated: %s", {k: len(v) for k, v in document.items()})
    return await load_chains(session)
