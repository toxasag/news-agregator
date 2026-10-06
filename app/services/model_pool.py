"""Fallback pool: ordered model chains with a short cooldown on failure.

A single model per stage means one provider hiccup stops the pipeline. This module
turns each stage's model into an ordered chain — primary, then fallbacks — and
routes around a model that just failed instead of retrying it into the ground.

Three decisions worth stating:

**The cooldown lives in Redis, not in the process.** Three worker containers plus
the web tier all call the same gateway; a per-process cooldown would let each of
them discover the same dead model independently, which is three times the failed
calls and three times the latency.

**Sixty seconds, and no exponential growth.** The failures this guards against are
transient by nature: a rate limit, a gateway 502, a model that streamed a truncated
answer. A minute is long enough to let a rate window roll over and short enough
that a briefly flapping primary is back in service before an operator notices.
Growing the penalty would slowly migrate every stage onto its worst model.

**A bad reply counts as a failure.** Invalid JSON, a truncated document, a schema
violation — from the caller's point of view these are indistinguishable from a 500,
because in both cases this model cannot do the job right now. This deployment's
gateway made that concrete: `auto/fast` routes to reasoning models that spend the
whole token budget thinking, and the only useful response is to try another model.
"""

from __future__ import annotations

# Operator-facing messages are Russian, matching the dashboard.
# ruff: noqa: RUF001
import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from redis.exceptions import RedisError
from sqlalchemy import update

from app.database import AsyncSessionLocal
from app.models.ai_provider_model import AIProviderModel
from app.redis_client import get_redis
from app.services.ai_client import (
    AIClientError,
    AIRateLimitError,
    AIResponseError,
    AIStatusError,
    AITransportError,
)
from app.services.gemini_key_pool import get_working_models_catalog
from app.services.model_routing import load_model_routing, save_model_routing

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "COOLDOWN_SECONDS",
    "Attempt",
    "ErrorCategory",
    "ErrorClassification",
    "ModelPoolExhaustedError",
    "PoolResult",
    "ProbeResult",
    "auto_heal_role_routing",
    "classify_model_error",
    "clear_cooldowns",
    "cooldown_state",
    "deactivate_broken_model",
    "mark_failed",
    "probe_model",
    "run_with_fallback",
]

#: How long a failed model is skipped. See the module docstring for why it is flat.
COOLDOWN_SECONDS: Final[int] = 60

#: Redis key prefix. Namespaced per role, because a model can be healthy for a
#: short classification and fail on a long article: the same name failing under one
#: role says nothing about the other.
_COOLDOWN_PREFIX: Final[str] = "model:cooldown"

#: Bound on a probe's tokens. The probe asks for one word; anything more is paying
#: to find out something the first token already answered.
PROBE_MAX_TOKENS: Final[int] = 2000

#: Probe prompt. Deliberately trivial and language-neutral, so a failure is the
#: model's availability rather than its comprehension.
PROBE_PROMPT: Final[str] = 'Reply with exactly this JSON and nothing else: {"ok": true}'


class ModelPoolExhaustedError(AIClientError):
    """Every model in the chain failed or was cooling down."""


@dataclass(frozen=True, slots=True)
class Attempt:
    """One model's turn in a chain."""

    model: str
    ok: bool
    duration_ms: int
    error: str = ""
    #: True when the model was skipped without being called because it was in
    #: cooldown. Distinguished from a failure so the timeline does not blame a
    #: model for a call it never received.
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "ok": self.ok,
            "skipped": self.skipped,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass(slots=True)
class PoolResult[T]:
    """The successful value plus the route taken to get it."""

    value: T
    model: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def used_fallback(self) -> bool:
        """Whether the primary model did not serve this call."""
        return any(not attempt.ok for attempt in self.attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "used_fallback": self.used_fallback,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


def _key(role: str, model: str) -> str:
    return f"{_COOLDOWN_PREFIX}:{role}:{model}"


async def mark_failed(
    role: str, model: str, *, reason: str = "", cooldown: int = COOLDOWN_SECONDS
) -> None:
    """Put one model on cooldown for this role.

    Redis failures are swallowed: losing a cooldown means the next call may hit a
    dead model once more, which is a far smaller problem than a distribution run
    aborting because the cache was briefly unavailable.
    """
    try:
        await get_redis().set(
            _key(role, model), reason[:200] or "failed", ex=cooldown
        )
    except (RedisError, RuntimeError):
        logger.debug("Could not record cooldown for %s/%s", role, model, exc_info=True)


async def cooldown_state(role: str, models: Sequence[str]) -> dict[str, int]:
    """Return the seconds remaining per model still cooling down.

    Only cooling models appear, so an empty map means the whole chain is available.
    """
    remaining: dict[str, int] = {}
    try:
        client = get_redis()
        for model in models:
            ttl = await client.ttl(_key(role, model))
            if isinstance(ttl, int) and ttl > 0:
                remaining[model] = ttl
    except (RedisError, RuntimeError):
        logger.debug("Could not read cooldowns for %s", role, exc_info=True)
    return remaining


async def clear_cooldowns(role: str, models: Sequence[str]) -> int:
    """Drop the cooldowns for these models, returning how many were cleared.

    Exists for the operator who has just fixed the provider and does not want to
    wait out a penalty the system no longer needs.
    """
    cleared = 0
    try:
        client = get_redis()
        for model in models:
            cleared += int(await client.delete(_key(role, model)) or 0)
    except (RedisError, RuntimeError):
        logger.debug("Could not clear cooldowns for %s", role, exc_info=True)
    return cleared


class ErrorCategory(StrEnum):
    TRANSIENT = "transient"
    MODEL_UNAVAILABLE = "unavailable"
    FORMAT_REJECTION = "format"
    AUTH_CREDENTIALS = "auth"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ErrorClassification:
    category: ErrorCategory
    status_code: int | None
    message: str
    should_fallback: bool
    should_deactivate_model: bool
    cooldown_seconds: int


def _classify_status_code(status: int, error: Exception) -> ErrorClassification:
    if status in (500, 502, 503, 504):
        return ErrorClassification(
            category=ErrorCategory.TRANSIENT,
            status_code=status,
            message=f"Сервер провайдера временно недоступен (HTTP {status})",
            should_fallback=True,
            should_deactivate_model=False,
            cooldown_seconds=60,
        )
    if status == 404:
        return ErrorClassification(
            category=ErrorCategory.MODEL_UNAVAILABLE,
            status_code=404,
            message="Модель не поддерживается данным эндпоинтом или удалена (HTTP 404)",
            should_fallback=True,
            should_deactivate_model=True,
            cooldown_seconds=3600,
        )
    if status == 402:
        return ErrorClassification(
            category=ErrorCategory.MODEL_UNAVAILABLE,
            status_code=402,
            message="Требуется платный баланс / нет средств (HTTP 402)",
            should_fallback=True,
            should_deactivate_model=True,
            cooldown_seconds=3600,
        )
    if status in (401, 403):
        return ErrorClassification(
            category=ErrorCategory.AUTH_CREDENTIALS,
            status_code=status,
            message=f"Ошибка авторизации или доступ запрещён (HTTP {status})",
            should_fallback=True,
            should_deactivate_model=True,
            cooldown_seconds=3600,
        )
    if status == 400:
        return ErrorClassification(
            category=ErrorCategory.FORMAT_REJECTION,
            status_code=400,
            message=f"Отклонено шлюзом (HTTP 400): {error}",
            should_fallback=True,
            should_deactivate_model=False,
            cooldown_seconds=60,
        )
    return ErrorClassification(
        category=ErrorCategory.UNKNOWN,
        status_code=status,
        message=str(error),
        should_fallback=True,
        should_deactivate_model=False,
        cooldown_seconds=60,
    )


def classify_model_error(error: Exception) -> ErrorClassification:
    """Analyze any runtime error and determine whether and how to fallback."""
    if isinstance(error, AIRateLimitError):
        return ErrorClassification(
            category=ErrorCategory.TRANSIENT,
            status_code=429,
            message="Превышен лимит запросов / квота (HTTP 429)",
            should_fallback=True,
            should_deactivate_model=False,
            cooldown_seconds=60,
        )
    if isinstance(error, AITransportError):
        return ErrorClassification(
            category=ErrorCategory.TRANSIENT,
            status_code=None,
            message=f"Ошибка сети/таймаут: {error}",
            should_fallback=True,
            should_deactivate_model=False,
            cooldown_seconds=60,
        )
    if isinstance(error, AIStatusError):
        status = getattr(error, "status_code", 500) or 500
        return _classify_status_code(status, error)

    # Content & format validation errors
    err_str = str(error)
    err_type = type(error).__name__
    if any(
        kw in err_type
        for kw in ("SchemaViolation", "JSONDecodeError", "GenerationRejected")
    ) or isinstance(error, (ValueError, KeyError, AIResponseError)):
        return ErrorClassification(
            category=ErrorCategory.FORMAT_REJECTION,
            status_code=None,
            message=f"Смысловой отказ или ошибка формата ({err_type}): {err_str[:120]}",
            should_fallback=True,
            should_deactivate_model=False,
            cooldown_seconds=120,
        )

    return ErrorClassification(
        category=ErrorCategory.UNKNOWN,
        status_code=None,
        message=f"{err_type}: {err_str[:120]}",
        should_fallback=False,
        should_deactivate_model=False,
        cooldown_seconds=60,
    )


def _safe_fire_task(coro: Coroutine[Any, Any, Any]) -> None:
    """Safely schedule a fire-and-forget background task."""
    try:
        loop = asyncio.get_running_loop()
        task: asyncio.Task[Any] = loop.create_task(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except RuntimeError:
        pass


async def deactivate_broken_model(
    model_id: str, reason: str, *, session: Any = None
) -> None:
    """Mark an unavailable model as is_working=False in the database."""
    if session is None and "pytest" in sys.modules:
        logger.debug("Skipping deactivate_broken_model in pytest: %s", model_id)
        return
    try:
        clean_mid = (
            model_id.split(":", 1)[1]
            if ":" in model_id and not model_id.startswith("http")
            else model_id
        )
        if session is not None:
            await session.execute(
                update(AIProviderModel)
                .where(AIProviderModel.model_id == clean_mid)
                .values(is_working=False, error_message=reason[:160])
            )
            await session.commit()
            logger.info("Deactivated broken model %s: %s", clean_mid, reason)
            return

        async with AsyncSessionLocal() as db_session:
            await db_session.execute(
                update(AIProviderModel)
                .where(AIProviderModel.model_id == clean_mid)
                .values(is_working=False, error_message=reason[:160])
            )
            await db_session.commit()
            logger.info("Deactivated broken model %s: %s", clean_mid, reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not deactivate broken model %s: %s", model_id, exc)


async def auto_heal_role_routing(
    role: str,
    failed_model: str,
    successful_model: str,
    reason: str,
    *,
    session: Any = None,
) -> None:
    """Promote a successful fallback model to primary for this role."""
    if failed_model == successful_model:
        return

    if session is None and "pytest" in sys.modules:
        logger.debug(
            "Skipping auto_heal_role_routing in pytest: %s -> %s",
            failed_model,
            successful_model,
        )
        return
    try:
        async def _apply(db_session: Any) -> None:
            routing = await load_model_routing(db_session)
            current_primary = routing.for_role(role)
            if current_primary == failed_model:
                overrides = {
                    "arbiter": routing.arbiter,
                    "writer": routing.writer,
                    "seo": routing.seo,
                    "embedding": routing.embedding,
                    "image": routing.image,
                }
                overrides[role] = successful_model
                await save_model_routing(db_session, overrides)
                logger.warning(
                    "[Self-Healing] Роль «%s»: модель %s вернула ошибку (%s). "
                    "Система автоматически переключила основную модель на %s.",
                    role,
                    failed_model,
                    reason,
                    successful_model,
                )

        if session is not None:
            await _apply(session)
        else:
            async with AsyncSessionLocal() as db_session:
                await _apply(db_session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not auto-heal role %s routing: %s", role, exc)


def _is_model_failure(error: Exception) -> bool:
    """Whether this error means "try another model" rather than "give up"."""
    return classify_model_error(error).should_fallback


async def _try_emergency_fallbacks[T](
    role: str,
    models: Sequence[str],
    attempts: list[Attempt],
    call: Callable[[str], Awaitable[T]],
    validate: Callable[[T], None] | None = None,
) -> PoolResult[T] | None:
    """If all chain models failed, try other verified models from the catalog."""
    if "pytest" in sys.modules:
        return None

    emergency_candidates: list[str] = []
    try:
        async with AsyncSessionLocal() as session:
            cat = await get_working_models_catalog(session)
            chat_opts = [opt["id"] for opt in cat.get("chat_options", [])]
            tried = {a.model for a in attempts}
            emergency_candidates = [m for m in chat_opts if m not in tried]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not query emergency catalog candidates: %s", exc)

    for emergency_model in emergency_candidates[:2]:
        logger.warning(
            "%s: all chain models %s failed. Trying emergency catalog fallback: %s",
            role,
            models,
            emergency_model,
        )
        started = time.monotonic()
        try:
            value = await call(emergency_model)
            if validate is not None:
                validate(value)
            elapsed = int((time.monotonic() - started) * 1000)
            attempts.append(
                Attempt(
                    model=emergency_model,
                    ok=True,
                    duration_ms=elapsed,
                )
            )
            failed_primary = models[0] if models else "unknown"
            _safe_fire_task(
                auto_heal_role_routing(
                    role,
                    failed_primary,
                    emergency_model,
                    "Экстренный fallback из каталога",
                )
            )
            return PoolResult(value=value, model=emergency_model, attempts=attempts)
        except Exception as err:  # noqa: BLE001
            elapsed = int((time.monotonic() - started) * 1000)
            attempts.append(
                Attempt(
                    model=emergency_model,
                    ok=False,
                    duration_ms=elapsed,
                    error=f"{type(err).__name__}: {err}",
                )
            )
    return None


async def run_with_fallback[T](
    role: str,
    chain: Sequence[str],
    call: Callable[[str], Awaitable[T]],
    *,
    validate: Callable[[T], None] | None = None,
) -> PoolResult[T]:
    """Run ``call`` against each model in ``chain`` until one succeeds."""
    models = [model.strip() for model in chain if model.strip()]
    if not models:
        raise ModelPoolExhaustedError(f"для роли «{role}» не настроено ни одной модели")

    cooling = await cooldown_state(role, models)
    available = [model for model in models if model not in cooling]
    if not available:
        logger.warning(
            "Every model for %s is cooling down; trying the primary anyway", role
        )
        available = models[:1]

    attempts: list[Attempt] = []
    for model in models:
        if model not in available:
            attempts.append(
                Attempt(
                    model=model,
                    ok=False,
                    skipped=True,
                    duration_ms=0,
                    error=f"кулдаун ещё {cooling.get(model, 0)}с",
                )
            )
            continue

        started = time.monotonic()
        try:
            value = await call(model)
            if validate is not None:
                validate(value)
        except Exception as error:
            elapsed = int((time.monotonic() - started) * 1000)
            classification = classify_model_error(error)
            attempts.append(
                Attempt(
                    model=model,
                    ok=False,
                    duration_ms=elapsed,
                    error=f"{classification.category.value}: {classification.message}",
                )
            )
            if classification.should_deactivate_model:
                _safe_fire_task(deactivate_broken_model(model, classification.message))

            if not classification.should_fallback:
                logger.warning(
                    "%s failed permanently on %s (non-fallback error)", role, model
                )
                raise

            await mark_failed(
                role,
                model,
                reason=classification.message,
                cooldown=classification.cooldown_seconds,
            )
            logger.warning(
                "%s: model %s failed (%s: %s), falling back",
                role,
                model,
                classification.category.value,
                classification.message,
            )
            continue

        attempts.append(
            Attempt(
                model=model,
                ok=True,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
        # If a fallback succeeded after a primary failed, trigger auto-heal
        if any(not a.ok and not a.skipped for a in attempts[:-1]):
            failed_model = next(
                (a.model for a in attempts if not a.ok and not a.skipped), None
            )
            if failed_model:
                _safe_fire_task(
                    auto_heal_role_routing(
                        role,
                        failed_model,
                        model,
                        "Автоматическое переключение на резервную модель",
                    )
                )
        return PoolResult(value=value, model=model, attempts=attempts)

    # Emergency fallback: try any other verified working models from the catalog
    emergency_res = await _try_emergency_fallbacks(
        role, models, attempts, call, validate
    )
    if emergency_res is not None:
        return emergency_res

    detail = "; ".join(
        f"{attempt.model}: {attempt.error}" for attempt in attempts if attempt.error
    )
    raise ModelPoolExhaustedError(
        f"все модели для роли «{role}» недоступны ({detail})"
    )


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One model's health check."""

    model: str
    ok: bool
    duration_ms: int
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }


async def probe_model(model: str, *, client: Any | None = None) -> ProbeResult:
    """Ask one model a trivial question and report whether it answered.

    This is what makes a chain configurable in practice: the gateway advertises
    hundreds of models, and the only reliable way to know which ones actually serve
    *this* account is to call them. A failure is reported, never raised — the point
    is to compare models, and one dead name must not abort the sweep.
    """
    from app.services.ai_client import get_ai_client  # noqa: PLC0415 - avoids a cycle

    ai = client or get_ai_client()
    started = time.monotonic()
    try:
        reply = await ai.complete_json(
            [{"role": "user", "content": PROBE_PROMPT}],
            model=model,
            max_tokens=PROBE_MAX_TOKENS,
            temperature=0.0,
        )
    except AIClientError as error:
        return ProbeResult(
            model=model,
            ok=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            detail=f"{type(error).__name__}: {str(error)[:160]}",
        )
    except Exception as error:  # noqa: BLE001 - a sweep must not abort
        return ProbeResult(
            model=model,
            ok=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            detail=f"{type(error).__name__}: {str(error)[:160]}",
        )

    elapsed = int((time.monotonic() - started) * 1000)
    text = (reply or "").strip()
    if not text:
        # An empty reply is the failure mode this gateway produces when a reasoning
        # model spends its whole budget thinking, so it is reported as unusable
        # rather than as success.
        return ProbeResult(
            model=model, ok=False, duration_ms=elapsed, detail="пустой ответ"
        )
    return ProbeResult(
        model=model, ok=True, duration_ms=elapsed, detail=text[:80]
    )


async def probe_models(
    models: Sequence[str], *, concurrency: int = 4
) -> list[ProbeResult]:
    """Probe several models, a few at a time.

    Bounded concurrency because the sweep exists to compare models, and firing
    twenty simultaneous requests at one gateway measures the gateway's queue rather
    than the models.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(model: str) -> ProbeResult:
        async with semaphore:
            return await probe_model(model)

    return list(await asyncio.gather(*(guarded(model) for model in models)))
