# ruff: noqa: E501, PLR0912
"""Tests for multi-provider AI key pools and dynamic Gemini ListModels discovery."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.gemini_key_pool as gkp
from app.models.ai_provider_key import AIProviderKey
from app.models.ai_provider_model import AIProviderModel
from app.services.ai_client import (
    _DEAD_MODELS_CACHE,
    AIStatusError,
    _handle_provider_failure,
    _is_gateway_model_dead,
    _mark_gateway_model_dead,
)
from app.services.gemini_key_pool import (
    _build_chain_for_role,
    auto_select_best_models,
    batch_add_keys_for_provider,
    get_next_healthy_key,
    get_working_models_catalog,
    list_all_provider_pools,
    query_gemini_models,
)
from app.services.gemini_key_pool import (
    test_all_models_for_key as run_test_all_models_for_key,
)
from app.services.gemini_key_pool import (
    test_gemini_key_dynamic as run_gemini_key_dynamic,
)
from app.services.gemini_key_pool import (
    test_individual_model_by_id as run_model_test_by_id,
)


class _ScalarVal:
    def __init__(self, val: Any) -> None:
        self._val = val

    def scalar_one(self) -> Any:
        if isinstance(self._val, list):
            return self._val[0]
        return self._val

    def scalar_one_or_none(self) -> Any:
        if isinstance(self._val, list):
            return self._val[0] if self._val else None
        return self._val

    def scalars(self) -> _ScalarVal:
        return self

    def first(self) -> Any:
        if isinstance(self._val, list):
            return self._val[0] if self._val else None
        return self._val

    def all(self) -> list[Any]:
        if isinstance(self._val, list):
            return self._val
        return [self._val] if self._val is not None else []


class MockSession:
    """In-memory async session double for pool testing."""

    def __init__(self) -> None:
        self.keys: list[AIProviderKey] = []
        self.models: list[AIProviderModel] = []
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.commits: int = 0

    async def execute(self, stmt: Any) -> _ScalarVal:
        text = str(stmt).strip().lower()
        if text.startswith("update"):
            return _ScalarVal(1)

        # Check if querying models
        if "aiprovidermodel" in text or "ai_provider_models" in text:
            if "join" in text:
                matched_pairs = []
                for m in self.models:
                    key = next((k for k in self.keys if k.id == m.provider_key_id), None)
                    if key and key.is_active and m.is_working:
                        matched_pairs.append((m, key))
                return _ScalarVal(matched_pairs)

            res_models = list(self.models)
            if hasattr(stmt, "_where_criteria"):
                for crit in stmt._where_criteria:
                    crit_str = str(crit).lower()
                    if "provider_key_id" in crit_str:
                        right_val = getattr(getattr(crit, "right", None), "value", None)
                        if right_val is not None:
                            res_models = [m for m in res_models if m.provider_key_id == right_val]
            return _ScalarVal(res_models)

        matched = list(self.keys)
        if hasattr(stmt, "_where_criteria"):
            for crit in stmt._where_criteria:
                crit_str = str(crit).lower()
                left_str = crit_str.split("=")[0].strip() if "=" in crit_str else ""
                if left_str in ("ai_provider_keys.id", "id"):
                    right_val = getattr(getattr(crit, "right", None), "value", None)
                    if right_val is not None and isinstance(right_val, int):
                        matched = [k for k in matched if k.id == right_val]
                for pname in ("google", "openrouter", "openai", "anthropic", "deepseek", "fal", "custom"):
                    right_val = getattr(getattr(crit, "right", None), "value", None)
                    if f"'{pname}'" in crit_str or right_val == pname:
                        matched = [k for k in matched if k.provider_name == pname]

        return _ScalarVal(matched)

    async def get(self, entity: type, ident: Any, *args: Any, **kwargs: Any) -> Any:
        if entity is AIProviderKey:
            return next((k for k in self.keys if k.id == ident), None)
        if entity is AIProviderModel:
            return next((m for m in self.models if m.id == ident), None)
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, AIProviderKey):
            if not obj.id:
                obj.id = len(self.keys) + 1
            if not hasattr(obj, "models"):
                obj.models = []
            self.keys.append(obj)
        elif isinstance(obj, AIProviderModel):
            if not obj.id:
                obj.id = len(self.models) + 1
            self.models.append(obj)

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)
        if isinstance(obj, AIProviderKey) and obj in self.keys:
            self.keys.remove(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1


def cast_session(s: MockSession) -> AsyncSession:
    return cast(AsyncSession, s)


@pytest.mark.asyncio
async def test_no_limit_on_keys_batch_add() -> None:
    """Verify that batch_add_keys_for_provider does not cap at 20 keys."""
    session = MockSession()
    # Generate 25 distinct keys
    keys_text = "\n".join(f"AIzaSyTestKeyNumber_{i:03d}_ExtraLong" for i in range(25))

    res = await batch_add_keys_for_provider(cast_session(session), "google", keys_text)
    assert res["added"] == 25
    assert len(session.keys) == 25


@pytest.mark.asyncio
async def test_multi_provider_pools_support() -> None:
    """Verify that multiple provider pools can be added and listed."""
    session = MockSession()

    # Add 2 Google keys
    await batch_add_keys_for_provider(
        cast_session(session), "google", "AIzaSyGoogleKey1\nAIzaSyGoogleKey2"
    )

    # Add 2 OpenRouter keys
    await batch_add_keys_for_provider(
        cast_session(session), "openrouter", "sk-or-v1-key1\nsk-or-v1-key2"
    )

    pools = await list_all_provider_pools(cast_session(session))
    pool_names = [p["provider_name"] for p in pools]

    assert "google" in pool_names
    assert "openrouter" in pool_names

    google_pool = next(p for p in pools if p["provider_name"] == "google")
    assert google_pool["total_keys"] == 2

    or_pool = next(p for p in pools if p["provider_name"] == "openrouter")
    assert or_pool["total_keys"] == 2


@pytest.mark.asyncio
async def test_gemini_dynamic_list_models_success() -> None:
    """Verify query_gemini_models parses ListModels correctly."""
    mock_models_response = {
        "models": [
            {
                "name": "models/gemini-2.5-flash",
                "supportedGenerationMethods": ["generateContent", "countTokens"],
            },
            {
                "name": "models/text-embedding-004",
                "supportedGenerationMethods": ["embedContent"],
            },
        ]
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-goog-api-key") == "test-gemini-key"
        assert "/models" in str(request.url)
        return httpx.Response(200, json=mock_models_response)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await query_gemini_models(client, "test-gemini-key")

    assert res["ok"] is True
    assert len(res["models"]) == 2
    assert res["models"][0]["name"] == "models/gemini-2.5-flash"


@pytest.mark.asyncio
async def test_gemini_dynamic_capability_test_with_missing_embed_model() -> None:
    """Verify that if embedContent is missing or 404, key remains healthy for text/vision!"""
    session = MockSession()
    key = AIProviderKey(
        id=1,
        provider_name="google",
        api_key_encrypted="encrypted-key",
        is_active=True,
        health="untested",
    )
    key.models = []
    session.add(key)

    # Mock responses:
    # 1. GET /models -> only returns generative model, NO embedding model
    # 2. POST /models/gemini-2.5-flash:generateContent (text) -> 200 with candidate
    # 3. POST /models/gemini-2.5-flash:generateContent (vision) -> 200 with candidate
    models_payload = {
        "models": [
            {
                "name": "models/gemini-2.5-flash",
                "supportedGenerationMethods": ["generateContent"],
            }
        ]
    }

    gen_payload = {
        "candidates": [
            {"content": {"parts": [{"text": "РАБОТАЕТ"}]}}
        ]
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "GET" and "/models" in url_str:
            return httpx.Response(200, json=models_payload)
        if request.method == "POST" and ":generateContent" in url_str:
            return httpx.Response(200, json=gen_payload)
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(mock_handler)

    with (
        patch("app.services.gemini_key_pool._decrypt_key", return_value="raw-key"),
        patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
    ):
        result = await run_gemini_key_dynamic(cast_session(session), 1)

    assert result["text"]["ok"] is True
    assert result["vision"]["ok"] is True
    assert result["embeddings"]["ok"] is False  # Embeddings not available
    # CRITICAL: Key must be healthy, NOT error! Fastembed handles local vectors.
    assert key.health == "healthy"
    assert key.last_error is None


@pytest.mark.asyncio
async def test_gemini_401_reports_clear_error() -> None:
    """Verify that HTTP 401 returns a clear, human-readable error."""
    session = MockSession()
    key = AIProviderKey(
        id=1,
        provider_name="google",
        api_key_encrypted="encrypted-key",
        is_active=True,
        health="untested",
    )
    key.models = []
    session.add(key)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": 401, "message": "API key not valid."}},
        )

    transport = httpx.MockTransport(mock_handler)

    with (
        patch("app.services.gemini_key_pool._decrypt_key", return_value="bad-key"),
        patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
    ):
        result = await run_gemini_key_dynamic(cast_session(session), 1)

    assert result["ok"] is False
    assert "401" in result["error"]
    assert "Ключ отклонён Google API" in result["error"]
    assert key.health == "error"


@pytest.mark.asyncio
async def test_round_robin_per_provider() -> None:
    gkp._rr_cursors["google"] = 0
    session = MockSession()
    await batch_add_keys_for_provider(
        cast_session(session), "google", "AIzaKey1\nAIzaKey2\nAIzaKey3"
    )

    with patch("app.services.gemini_key_pool._decrypt_key", side_effect=lambda x: f"raw-{x}"):
        id1, _ = await get_next_healthy_key(cast_session(session), "google")
        id2, _ = await get_next_healthy_key(cast_session(session), "google")
        id3, _ = await get_next_healthy_key(cast_session(session), "google")
        id4, _ = await get_next_healthy_key(cast_session(session), "google")

    assert id1 == 1
    assert id2 == 2
    assert id3 == 3
    assert id4 == 1  # Wrapped around


@pytest.mark.asyncio
async def test_custom_provider_named_pool_and_working_models() -> None:
    """Verify custom provider custom_name, endpoint, collapsed is_open=False, and working models."""
    session = MockSession()
    # Add custom key with specific name and base_url
    await batch_add_keys_for_provider(
        cast_session(session),
        provider_name="custom",
        raw_keys_text="sk-test-custom-key",
        base_url="https://vsllm.cc",
        custom_name="vsllm.cc",
    )

    pools = await list_all_provider_pools(cast_session(session))
    # All pools must be collapsed by default (is_open == False)
    for p in pools:
        assert p["is_open"] is False

    custom_pool = next(p for p in pools if p["provider_name"] == "custom")
    assert custom_pool["name"] == "vsllm.cc"
    assert custom_pool["endpoint_url"] == "https://vsllm.cc/v1"
    assert custom_pool["badge"] == "VS"
    assert custom_pool["total_keys"] == 1


@pytest.mark.asyncio
async def test_working_models_catalog_and_auto_select() -> None:
    """Verify get_working_models_catalog groups working models and auto_select_best_models assigns roles."""
    session = MockSession()

    # Key 1: Google
    key_google = AIProviderKey(
        id=1,
        provider_name="google",
        api_key_encrypted="enc_gkey",
        is_active=True,
    )
    session.add(key_google)

    # Key 2: Custom
    key_custom = AIProviderKey(
        id=2,
        provider_name="custom",
        custom_name="vsllm.cc",
        base_url="https://vsllm.cc",
        api_key_encrypted="enc_ckey",
        is_active=True,
    )
    session.add(key_custom)

    # Models
    m1 = AIProviderModel(
        id=1,
        provider_key_id=1,
        model_id="gemini-2.5-flash",
        is_working=True,
    )
    m2 = AIProviderModel(
        id=2,
        provider_key_id=1,
        model_id="gemma-4-26b-a4b-it",
        is_working=True,
    )
    m3 = AIProviderModel(
        id=3,
        provider_key_id=1,
        model_id="text-embedding-004",
        is_working=True,
    )
    m4 = AIProviderModel(
        id=4,
        provider_key_id=2,
        model_id="deepseek-v4-flash",
        is_working=True,
    )
    m5 = AIProviderModel(
        id=5,
        provider_key_id=2,
        model_id="broken-model",
        is_working=False,
    )
    for m in (m1, m2, m3, m4, m5):
        session.add(m)

    catalog = await get_working_models_catalog(cast_session(session))
    assert catalog["total_working"] == 4
    chat_ids = [c["id"] for c in catalog["chat_options"]]
    assert "gemini-2.5-flash" in chat_ids
    assert "deepseek-v4-flash" in chat_ids
    assert "broken-model" not in chat_ids

    embed_ids = [e["id"] for e in catalog["embed_options"]]
    assert "local:fastembed" in embed_ids
    assert "text-embedding-004" in embed_ids

    image_ids = [img["id"] for img in catalog["image_options"]]
    assert "google/imagen-3.0-generate-002" in image_ids
    assert "dall-e-3" in image_ids
    assert "disabled" in image_ids

    best = await auto_select_best_models(cast_session(session))
    assert best["arbiter"] == "gemini-2.5-flash"
    assert best["writer"] == "gemini-2.5-flash"
    assert best["seo"] == "gemini-2.5-flash"
    assert best["embedding"] == "local:fastembed"
    assert best["image"] == "google/imagen-3.0-generate-002"


@pytest.mark.asyncio
async def test_run_individual_model_test() -> None:
    """Verify test_individual_model_by_id tests model and updates is_working status."""
    session = MockSession()
    key = AIProviderKey(
        id=1,
        provider_name="custom",
        custom_name="vsllm.cc",
        base_url="https://vsllm.cc",
        api_key_encrypted="enc_key",
        is_active=True,
    )
    session.add(key)
    model = AIProviderModel(
        id=10,
        provider_key_id=1,
        model_id="test-model",
        is_working=False,
    )
    session.add(model)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    transport = httpx.MockTransport(mock_handler)
    with (
        patch("app.services.gemini_key_pool._decrypt_key", return_value="sk-raw"),
        patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
    ):
        result = await run_model_test_by_id(cast_session(session), 10)

    assert result["ok"] is True
    assert model.is_working is True
    assert model.last_tested_at is not None


def test_build_chain_for_role_cross_provider() -> None:
    """Verify _build_chain_for_role prioritizes candidate models from different providers."""
    chat_options = [
        {"id": "grok-4.5", "provider": "api.dollariuy.fun"},
        {"id": "mistral-large-2512", "provider": "api.dollariuy.fun"},
        {"id": "gemini-3.5-flash", "provider": "Google Gemini"},
        {"id": "deepseek-v4-flash", "provider": "vsllm.cc"},
    ]
    candidates = ["mistral-large-2512", "gemini-3.5-flash", "deepseek-v4-flash"]

    # When primary is grok-4.5 (api.dollariuy.fun), candidate from different provider
    # (gemini-3.5-flash from Google) should be chosen as first fallback before mistral-large-2512
    chain = _build_chain_for_role("grok-4.5", candidates, chat_options, max_chain=3)
    assert chain[0] == "grok-4.5"
    assert chain[1] in ("gemini-3.5-flash", "deepseek-v4-flash")  # different provider!
    assert len(chain) == 3
    assert len(set(chain)) == 3


@pytest.mark.asyncio
async def test_batch_test_all_models_for_key() -> None:
    """Verify test_all_models_for_key tests 100% of models concurrently."""
    session = MockSession()
    key = AIProviderKey(
        id=77,
        provider_name="custom",
        custom_name="api.dollariuy.fun",
        base_url="https://api.dollariuy.fun/v1",
        api_key_encrypted="enc_dkey",
        is_active=True,
    )
    session.add(key)

    m1 = AIProviderModel(id=101, provider_key_id=77, model_id="grok-4.5", is_working=False)
    m2 = AIProviderModel(id=102, provider_key_id=77, model_id="mistral-large-2512", is_working=False)
    m3 = AIProviderModel(id=103, provider_key_id=77, model_id="broken-model-500", is_working=False)
    session.add(m1)
    session.add(m2)
    session.add(m3)

    def mock_handler(request: httpx.Request) -> httpx.Response:
        content = request.read().decode("utf-8")
        if "broken-model-500" in content:
            return httpx.Response(500, json={"error": "Internal gateway crash"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(mock_handler)
    with (
        patch("app.services.gemini_key_pool._decrypt_key", return_value="sk-test-raw"),
        patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
    ):
        res = await run_test_all_models_for_key(cast_session(session), 77, concurrency=4)

    assert res["tested"] == 3
    assert res["working"] == 2
    assert m1.is_working is True
    assert m2.is_working is True
    assert m3.is_working is False
    assert "500" in (m3.error_message or "")
    assert key.working_models_count == 2
    assert key.health == "healthy"


@pytest.mark.asyncio
async def test_dead_models_cache_and_provider_failure() -> None:
    """Verify dead models caching and signature-based key cooldown handling."""
    base = "https://api.gateway.example"
    model = "unsupported-model-x"

    assert not _is_gateway_model_dead(base, model)
    _mark_gateway_model_dead(base, model, ttl_sec=60.0)
    assert _is_gateway_model_dead(base, model)

    # Clean up cache entry
    clean_base = f"{base}/v1"
    _DEAD_MODELS_CACHE.pop(f"{clean_base}:{model}", None)

    # Test quota failure triggers 24h error cooldown
    session = MockSession()
    key = AIProviderKey(
        id=99,
        provider_name="custom",
        api_key_encrypted="enc",
        health="healthy",
    )
    session.add(key)

    with patch("app.services.ai_client.mark_key_cooldown") as mock_cooldown:
        with pytest.raises(AIStatusError) as exc_info:
            await _handle_provider_failure(
                cast_session(session),
                key,
                base,
                "some-model",
                p_label="TestGW",
                status_code=402,
                err_text='{"error": "insufficient quota balance"}',
            )
        assert exc_info.value.status_code == 402
        mock_cooldown.assert_called_once()
        assert mock_cooldown.call_args[1]["seconds"] == 86400
        assert mock_cooldown.call_args[1]["health"] == "error"



