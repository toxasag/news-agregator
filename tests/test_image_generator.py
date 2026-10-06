"""Tests for unified image generator supporting Google Imagen, DALL-E, and Fal AI."""

from __future__ import annotations

import base64
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_provider_key import AIProviderKey
from app.services.image_generator import (
    ImageGenerationError,
    ImageQuotaExceededError,
    generate_cover_image,
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
    def __init__(self) -> None:
        self._data: list[Any] = []

    def add(self, obj: Any) -> None:
        self._data.append(obj)

    async def execute(self, stmt: Any) -> _ScalarVal:
        # Return all added objects matching query
        return _ScalarVal(list(self._data))

    async def commit(self) -> None:
        pass


def cast_session(s: Any) -> AsyncSession:
    return cast(AsyncSession, s)


@pytest.mark.asyncio
async def test_generate_cover_image_disabled() -> None:
    session = MockSession()
    result = await generate_cover_image(
        cast_session(session), "News title", model_override="disabled"
    )
    assert result is None


@pytest.mark.asyncio
async def test_generate_cover_image_empty_prompt() -> None:
    session = MockSession()
    with pytest.raises(ImageGenerationError, match="пуст"):
        await generate_cover_image(
            cast_session(session), "   ", model_override="google/imagen-3.0-generate-002"
        )


@pytest.mark.asyncio
async def test_generate_cover_image_google_imagen_success() -> None:
    session = MockSession()
    key = AIProviderKey(
        id=1,
        provider_name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_encrypted="enc_key",
        is_active=True,
    )
    session.add(key)

    fake_bytes = b"fake_jpeg_image_data_here"
    b64_str = base64.b64encode(fake_bytes).decode("ascii")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert ":predict" in str(request.url)
        return httpx.Response(
            200, json={"predictions": [{"bytesBase64Encoded": b64_str}]}
        )

    transport = httpx.MockTransport(mock_handler)
    with (
        patch("app.services.image_generator._decrypt_key", return_value="ai-key-123"),
        patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
    ):
        result = await generate_cover_image(
            cast_session(session),
            "Editorial press photo",
            model_override="google/imagen-3.0-generate-002",
        )

    assert result == fake_bytes


@pytest.mark.asyncio
async def test_generate_cover_image_google_imagen_quota_429() -> None:
    session = MockSession()
    key = AIProviderKey(
        id=1,
        provider_name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_encrypted="enc_key",
        is_active=True,
    )
    session.add(key)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": {"code": 429, "message": "Resource exhausted"}}
        )

    transport = httpx.MockTransport(mock_handler)
    with (
        patch("app.services.image_generator._decrypt_key", return_value="ai-key-123"),
        patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
        pytest.raises(ImageQuotaExceededError, match="Billing"),
    ):
        await generate_cover_image(
            cast_session(session),
            "Editorial press photo",
            model_override="google/imagen-3.0-generate-002",
        )


@pytest.mark.asyncio
async def test_generate_cover_image_openai_dalle_success() -> None:
    session = MockSession()
    key = AIProviderKey(
        id=2,
        provider_name="custom",
        custom_name="api.dollariuy.fun",
        base_url="https://api.dollariuy.fun/v1",
        api_key_encrypted="enc_key",
        is_active=True,
    )
    session.add(key)

    fake_bytes = b"fake_dalle_image_data"
    b64_str = base64.b64encode(fake_bytes).decode("ascii")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert "/images/generations" in str(request.url)
        return httpx.Response(200, json={"data": [{"b64_json": b64_str}]})

    transport = httpx.MockTransport(mock_handler)
    with (
        patch("app.services.image_generator._decrypt_key", return_value="sk-test-key"),
        patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
    ):
        result = await generate_cover_image(
            cast_session(session),
            "Press photo of tech conference",
            model_override="dall-e-3",
        )

    assert result == fake_bytes


@pytest.mark.asyncio
async def test_generate_cover_image_fal_ai_flux() -> None:
    session = MockSession()
    fake_bytes = b"flux_image_data"

    with patch(
        "app.services.image_generator.generate_flux_image",
        new=AsyncMock(return_value=fake_bytes),
    ) as mock_flux:
        result = await generate_cover_image(
            cast_session(session),
            "Photorealistic city street",
            model_override="fal-ai/flux/schnell",
        )

    assert result == fake_bytes
    mock_flux.assert_awaited_once_with(
        "Photorealistic city street", model="fal-ai/flux/schnell"
    )


@pytest.mark.asyncio
async def test_generate_cover_image_gemini_native_image_success() -> None:
    session = MockSession()
    key = AIProviderKey(
        id=1,
        provider_name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_encrypted="enc_key",
        is_active=True,
    )
    session.add(key)

    fake_bytes = b"fake_gemini_native_image_data"
    b64_str = base64.b64encode(fake_bytes).decode("ascii")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert "/interactions" in str(request.url)
        return httpx.Response(
            200,
            json={
                "id": "inter-123",
                "output_image": {"data": b64_str, "mime_type": "image/jpeg"},
            },
        )

    transport = httpx.MockTransport(mock_handler)
    with (
        patch("app.services.image_generator._decrypt_key", return_value="ai-key-123"),
        patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
    ):
        result = await generate_cover_image(
            cast_session(session),
            "News cover photo",
            model_override="gemini-3.1-flash-image",
        )

    assert result == fake_bytes


@pytest.mark.asyncio
async def test_generate_cover_image_gemini_native_image_quota_429() -> None:
    session = MockSession()
    key = AIProviderKey(
        id=1,
        provider_name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_encrypted="enc_key",
        is_active=True,
    )
    session.add(key)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert "/interactions" in str(request.url)
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "limit: 0 requests per day on Free Tier"
                }
            },
        )

    transport = httpx.MockTransport(mock_handler)
    with (
        patch("app.services.image_generator._decrypt_key", return_value="ai-key-123"),
        patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
        pytest.raises(ImageQuotaExceededError, match="Billing"),
    ):
        await generate_cover_image(
            cast_session(session),
            "News cover photo",
            model_override="gemini-3.1-flash-image",
        )

