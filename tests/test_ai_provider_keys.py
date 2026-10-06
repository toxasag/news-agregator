"""Provider requests must not expose credentials in diagnostics."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ai_provider_keys as providers

FAKE_KEY = "synthetic-provider-credential"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class SessionDouble:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.commits = 0

    async def execute(self, statement: Any) -> Any:
        value = self.results.pop(0) if self.results else None
        return SimpleNamespace(
            scalar_one_or_none=lambda: value,
            fetchall=lambda: [],
        )

    async def commit(self) -> None:
        self.commits += 1

    def add(self, value: Any) -> None:
        pass

    def as_session(self) -> AsyncSession:
        return cast(AsyncSession, self)


@pytest.fixture
def provider_key(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(providers, "_decrypt_key", lambda _: FAKE_KEY)
    return SimpleNamespace(
        id=1,
        provider_name="gemini",
        api_key_encrypted="encrypted-test-value",
        base_url=BASE_URL,
        is_active=True,
        last_error=None,
    )


@pytest.mark.parametrize("operation", ["discover", "key", "model"])
async def test_gemini_uses_header_not_query(
    operation: str, provider_key: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    model = SimpleNamespace(id=2, provider_key_id=1, model_id="example-model")
    if operation == "model":
        session = SessionDouble(model, provider_key)
        url = f"{BASE_URL}/models/example-model:generateContent"
        method = "POST"
    else:
        session = SessionDouble(provider_key)
        url = f"{BASE_URL}/models"
        method = "GET"

    with respx.mock() as router:
        route = router.route(method=method, url=url).mock(
            return_value=httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
                if operation == "model" else {"models": []},
            )
        )
        if operation == "discover":
            await providers.discover_models(session.as_session(), 1)
        elif operation == "key":
            assert await providers.test_provider_key(session.as_session(), 1)
        else:
            await providers.test_model(session.as_session(), 2)
        request = route.calls.last.request
        assert request.headers["x-goog-api-key"] == FAKE_KEY
        assert FAKE_KEY not in str(request.url)
        assert "key" not in request.url.params
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize(
    ("data", "is_gemini", "expected"),
    [
        ({}, True, False),
        ({"error": {"message": "denied"}}, True, False),
        ({"candidates": []}, True, False),
        ({"candidates": [{"content": {"parts": [{"text": " "}]}}]}, True, False),
        ({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}, True, True),
        ({"choices": [{"message": {"content": "ok"}}]}, False, True),
        ({"choices": [{"message": {"content": ""}}]}, False, False),
        ([], True, False),
    ],
)
def test_success_requires_generated_text(
    data: object, is_gemini: bool, expected: bool
) -> None:
    assert providers._has_text_response(data, is_gemini=is_gemini) is expected


async def test_error_response_is_not_copied_into_database_or_log(
    provider_key: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    session = SessionDouble(provider_key)
    with respx.mock() as router:
        router.get(f"{BASE_URL}/models").mock(
            return_value=httpx.Response(401, text=f"Rejected credential: {FAKE_KEY}")
        )
        with pytest.raises(providers.ProviderKeyError, match="HTTP 401") as caught:
            await providers.discover_models(session.as_session(), 1)
    assert FAKE_KEY not in str(caught.value)
    assert FAKE_KEY not in provider_key.last_error
    assert FAKE_KEY not in caplog.text


async def test_transport_exception_does_not_expose_request(
    provider_key: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    session = SessionDouble(provider_key)
    with respx.mock() as router:
        router.get(f"{BASE_URL}/models").mock(
            side_effect=httpx.ConnectError(f"Connection failed with key {FAKE_KEY}")
        )
        assert not await providers.test_provider_key(session.as_session(), 1)
    assert provider_key.last_error == "Provider connection failed (ConnectError)"
    assert provider_key.is_active is True
    assert provider_key.health == "error"
    assert FAKE_KEY not in caplog.text


async def test_key_check_does_not_enable_disabled_key(
    provider_key: SimpleNamespace,
) -> None:
    provider_key.is_active = False
    session = SessionDouble(provider_key)
    with respx.mock() as router:
        router.get(f"{BASE_URL}/models").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        assert await providers.test_provider_key(session.as_session(), 1)
    assert provider_key.is_active is False
    assert provider_key.health == "catalogue_accessible"
    assert provider_key.last_checked_at is not None


async def test_empty_model_response_fails_without_exposing_error_body(
    provider_key: SimpleNamespace,
) -> None:
    model = SimpleNamespace(id=2, provider_key_id=1, model_id="example-model")
    session = SessionDouble(model, provider_key)
    with respx.mock() as router:
        router.post(f"{BASE_URL}/models/example-model:generateContent").mock(
            return_value=httpx.Response(200, json={"error": FAKE_KEY})
        )
        await providers.test_model(session.as_session(), 2)
    assert model.is_working is False
    assert model.last_tested_at is not None
    assert model.error_message == "Provider returned no generated text"

