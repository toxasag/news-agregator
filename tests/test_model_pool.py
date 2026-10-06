"""The fallback pool: chains, cooldowns and what counts as a model failure.

The property that matters: one provider hiccup must not stop the pipeline, and a
model that just failed must not be retried into the ground. The subtler property is
the opposite one — a *configuration* error must fail immediately rather than being
retried against every model in the chain, because no other model will supply a
missing API key and cycling the chain hides the real cause behind two more
identical failures.
"""

# Operator-facing messages under test are Russian.

from __future__ import annotations

from typing import Any

import pytest

from app.services import model_pool
from app.services.ai_client import (
    AIRateLimitError,
    AIResponseError,
    AIStatusError,
    AITransportError,
)
from app.services.model_pool import (
    COOLDOWN_SECONDS,
    Attempt,
    ModelPoolExhaustedError,
    PoolResult,
    ProbeResult,
    probe_model,
    run_with_fallback,
)
from app.services.model_routing import CHAT_ROLES, MODEL_ROLES, ModelRouting


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the pool without Redis.

    Cooldowns are an optimisation, not a guarantee, and the pool is written to keep
    working when the cache is unavailable — these tests hold it to that.
    """
    async def no_cooldowns(role: str, models: Any) -> dict[str, int]:
        return {}

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(model_pool, "cooldown_state", no_cooldowns)
    monkeypatch.setattr(model_pool, "mark_failed", noop)


# ================================================================== chains


async def test_the_primary_model_serves_when_it_works() -> None:
    calls: list[str] = []

    async def call(model: str) -> str:
        calls.append(model)
        return f"ok from {model}"

    result = await run_with_fallback("writer", ["a/primary", "b/fallback"], call)

    assert result.model == "a/primary"
    assert result.used_fallback is False
    # The fallback is never called when the primary answers: paying for both would
    # double the cost of every successful request.
    assert calls == ["a/primary"]


async def test_a_rate_limited_primary_falls_through() -> None:
    async def call(model: str) -> str:
        if model == "a/primary":
            raise AIRateLimitError("429 too many requests", status_code=429)
        return "ok"

    result = await run_with_fallback("writer", ["a/primary", "b/fallback"], call)

    assert result.model == "b/fallback"
    assert result.used_fallback is True
    assert [attempt.ok for attempt in result.attempts] == [False, True]


@pytest.mark.parametrize(
    "error",
    [
        AIRateLimitError("429", status_code=429),
        AITransportError("connection reset"),
        AIResponseError("truncated JSON"),
        AIStatusError("502 bad gateway", status_code=502),
        AIStatusError("500 server error", status_code=500),
    ],
)
async def test_transient_failures_move_to_the_next_model(error: Exception) -> None:
    """All of these say the same thing: this model cannot do the job right now.

    An unusable reply is included deliberately — from the caller's side a truncated
    document is indistinguishable from a 500, and this gateway produces exactly that
    when a reasoning model spends its whole budget thinking.
    """

    async def call(model: str) -> str:
        if model == "a/primary":
            raise error
        return "ok"

    result = await run_with_fallback("arbiter", ["a/primary", "b/fallback"], call)

    assert result.model == "b/fallback"


@pytest.mark.parametrize("status", [404, 402, 401, 403])
async def test_model_unavailable_errors_fall_back(status: int) -> None:
    """Missing models, unpaid models, or bad credentials fall back to next model."""
    calls: list[str] = []

    async def call(model: str) -> str:
        calls.append(model)
        if model == "a/primary":
            raise AIStatusError(f"{status} error", status_code=status)
        return "fallback succeeded"

    result = await run_with_fallback("writer", ["a/primary", "b/fallback"], call)
    assert result.value == "fallback succeeded"
    assert result.model == "b/fallback"
    assert calls == ["a/primary", "b/fallback"]


async def test_fatal_non_ai_error_fails_immediately() -> None:
    """A coding error (e.g. AssertionError) fails immediately without fallback."""
    calls: list[str] = []

    async def call(model: str) -> str:
        calls.append(model)
        raise AssertionError("fatal internal bug")

    with pytest.raises(AssertionError, match="fatal internal bug"):
        await run_with_fallback("writer", ["a/primary", "b/fallback"], call)

    assert calls == ["a/primary"]


def test_classify_model_error_all_categories() -> None:
    """Verify classify_model_error maps status codes and exceptions to categories."""
    c_429 = model_pool.classify_model_error(AIRateLimitError("limit", status_code=429))
    assert c_429.category == model_pool.ErrorCategory.TRANSIENT
    assert c_429.should_fallback is True
    assert c_429.should_deactivate_model is False

    c_502 = model_pool.classify_model_error(
        AIStatusError("bad gateway", status_code=502)
    )
    assert c_502.category == model_pool.ErrorCategory.TRANSIENT
    assert c_502.should_fallback is True

    c_404 = model_pool.classify_model_error(
        AIStatusError("not found", status_code=404)
    )
    assert c_404.category == model_pool.ErrorCategory.MODEL_UNAVAILABLE
    assert c_404.should_fallback is True
    assert c_404.should_deactivate_model is True

    c_402 = model_pool.classify_model_error(
        AIStatusError("payment required", status_code=402)
    )
    assert c_402.category == model_pool.ErrorCategory.MODEL_UNAVAILABLE
    assert c_402.should_fallback is True
    assert c_402.should_deactivate_model is True

    c_json = model_pool.classify_model_error(AIResponseError("invalid json"))
    assert c_json.category == model_pool.ErrorCategory.FORMAT_REJECTION
    assert c_json.should_fallback is True

    c_fatal = model_pool.classify_model_error(AssertionError("bug"))
    assert c_fatal.category == model_pool.ErrorCategory.UNKNOWN
    assert c_fatal.should_fallback is False


async def test_deactivate_broken_model_with_session() -> None:
    """Verify deactivate_broken_model marks is_working=False."""
    executed = []

    class _MockDBSession:
        async def execute(self, stmt: Any) -> None:
            executed.append(str(stmt))

        async def commit(self) -> None:
            pass

    await model_pool.deactivate_broken_model(
        "custom:gpt-4o", "Model not found", session=_MockDBSession()
    )
    assert len(executed) == 1
    assert "update ai_provider_models" in executed[0].lower()


async def test_auto_heal_role_routing_logic() -> None:
    """Verify auto_heal_role_routing promotes fallback model to primary."""
    saved = []

    async def _mock_save(session: Any, overrides: dict[str, str]) -> ModelRouting:
        saved.append(overrides)
        return ModelRouting(
            arbiter=overrides.get("arbiter", "a"),
            writer=overrides.get("writer", "w"),
            seo=overrides.get("seo", "s"),
            embedding="local:fastembed",
            image="disabled",
            overridden=frozenset(),
        )

    class _MockDBSession:
        pass

    async def _mock_load(session: Any) -> ModelRouting:
        return ModelRouting(
            arbiter="failed_model",
            writer="writer_model",
            seo="seo_model",
            embedding="local:fastembed",
            image="disabled",
            overridden=frozenset(),
        )

    orig_load = model_pool.load_model_routing
    orig_save = model_pool.save_model_routing
    model_pool.load_model_routing = _mock_load
    model_pool.save_model_routing = _mock_save
    try:
        await model_pool.auto_heal_role_routing(
            "arbiter",
            "failed_model",
            "working_model",
            "Auto-switch",
            session=_MockDBSession(),
        )
        assert len(saved) == 1
        assert saved[0]["arbiter"] == "working_model"
    finally:
        model_pool.load_model_routing = orig_load
        model_pool.save_model_routing = orig_save


async def test_an_exhausted_chain_names_every_failure() -> None:
    """The error has to say what each model did, or debugging is guesswork."""

    async def call(model: str) -> str:
        raise AITransportError(f"{model} unreachable")

    with pytest.raises(ModelPoolExhaustedError) as caught:
        await run_with_fallback("writer", ["a/one", "b/two"], call)

    message = str(caught.value)
    assert "a/one" in message
    assert "b/two" in message


async def test_an_empty_chain_is_refused() -> None:
    async def call(model: str) -> str:
        return "unreachable"

    with pytest.raises(ModelPoolExhaustedError, match="не настроено"):
        await run_with_fallback("writer", ["", "   "], call)


async def test_a_rejected_reply_counts_as_a_model_failure() -> None:
    """`validate` lets a caller reject a syntactically fine but unusable answer."""

    def validate(value: str) -> None:
        if value == "garbage":
            raise AIResponseError("not parseable")

    async def call(model: str) -> str:
        return "garbage" if model == "a/primary" else "good"

    result = await run_with_fallback(
        "writer", ["a/primary", "b/fallback"], call, validate=validate
    )

    assert result.value == "good"
    assert result.model == "b/fallback"


async def test_a_cooling_model_is_skipped_without_a_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model on cooldown must not be paid for again while it recovers."""
    async def cooling(role: str, models: Any) -> dict[str, int]:
        return {"a/primary": 42}

    monkeypatch.setattr(model_pool, "cooldown_state", cooling)
    calls: list[str] = []

    async def call(model: str) -> str:
        calls.append(model)
        return "ok"

    result = await run_with_fallback("writer", ["a/primary", "b/fallback"], call)

    assert calls == ["b/fallback"]
    assert result.model == "b/fallback"
    skipped = result.attempts[0]
    # Reported as skipped, not failed: the model was never asked, and blaming it
    # for a call it did not receive would mislead whoever reads the timeline.
    assert skipped.skipped is True
    assert skipped.ok is False
    assert "42" in skipped.error


async def test_a_fully_cooling_chain_still_tries_the_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled pipeline is worse than one wasted call.

    The cooldown is an optimisation; when every model is penalised the pool must
    still attempt the work rather than refusing it.
    """
    async def all_cooling(role: str, models: Any) -> dict[str, int]:
        return {"a/primary": 10, "b/fallback": 10}

    monkeypatch.setattr(model_pool, "cooldown_state", all_cooling)

    async def call(model: str) -> str:
        return f"served by {model}"

    result = await run_with_fallback("writer", ["a/primary", "b/fallback"], call)

    assert result.model == "a/primary"


def test_the_cooldown_is_a_flat_minute() -> None:
    """Flat on purpose: a growing penalty migrates every stage onto its worst model.

    The failures this guards against — a rate window, a gateway 502, a truncated
    stream — are transient, and a minute is enough for them to pass.
    """
    assert COOLDOWN_SECONDS == 60


def test_a_pool_result_serialises_its_route() -> None:
    result = PoolResult(
        value="x",
        model="b/fallback",
        attempts=[
            Attempt(model="a/primary", ok=False, duration_ms=120, error="429"),
            Attempt(model="b/fallback", ok=True, duration_ms=800),
        ],
    )

    payload = result.to_dict()

    assert payload["used_fallback"] is True
    assert [attempt["model"] for attempt in payload["attempts"]] == [
        "a/primary",
        "b/fallback",
    ]


# =================================================================== probe


class _ProbeClient:
    def __init__(self, reply: str | None = None, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error

    async def complete_json(self, messages: Any, **kwargs: Any) -> str:
        if self.error is not None:
            raise self.error
        return self.reply or ""


async def test_a_probe_reports_a_working_model() -> None:
    result = await probe_model("a/model", client=_ProbeClient('{"ok": true}'))

    assert result.ok is True
    assert "ok" in result.detail


async def test_an_empty_reply_is_a_failed_probe() -> None:
    """The failure mode this gateway actually produces.

    A reasoning model can consume its whole token budget thinking and return
    nothing; reporting that as success would put a useless model into a chain.
    """
    result = await probe_model("a/model", client=_ProbeClient(""))

    assert result.ok is False
    assert "пустой" in result.detail


async def test_a_probe_never_raises() -> None:
    """One dead model must not abort a sweep across the others."""
    result = await probe_model(
        "a/model", client=_ProbeClient(error=AITransportError("unreachable"))
    )

    assert result.ok is False
    assert "AITransportError" in result.detail
    assert isinstance(result, ProbeResult)


def test_the_embedding_role_is_not_probed_over_chat() -> None:
    """Embeddings run locally, so a chat probe says nothing about them.

    Probing the fastembed model name against the gateway's chat endpoint reports a
    dead model however well local inference is working, and a red row that means
    nothing is worse than no row at all.
    """
    assert "embedding" not in CHAT_ROLES
    assert {"arbiter", "writer", "seo"} <= CHAT_ROLES
    # Every chat role must still be a real role, or the probe would silently skip
    # a stage that does call the gateway.
    assert {role for role, _attribute, _label in MODEL_ROLES} >= CHAT_ROLES
