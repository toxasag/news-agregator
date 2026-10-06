"""AI provider key management: encrypted storage, model discovery, and testing."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Final

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import get_secret_box
from app.models.ai_provider_key import AIProviderKey
from app.models.ai_provider_model import AIProviderModel

logger: Final[logging.Logger] = logging.getLogger(__name__)

type UsageResult = dict[str, int | str]

__all__ = [
    "ProviderKeyError",
    "add_provider_key",
    "delete_provider_key",
    "discover_models",
    "get_active_providers",
    "list_provider_keys",
    "test_all_models",
    "test_all_providers",
    "test_model",
    "test_provider_key",
    "update_provider_key",
]

REQUEST_TIMEOUT: Final[float] = 30.0


class ProviderKeyError(Exception):
    """Provider key operation failed."""


def _encrypt_key(api_key: str) -> str:
    """Encrypt an API key using the project's Fernet box."""
    return get_secret_box().encrypt(api_key)


def _decrypt_key(encrypted: str) -> str:
    """Decrypt an API key."""
    return get_secret_box().decrypt(encrypted)


async def _provider_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, object] | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            response = await client.request(method, url, headers=headers, json=payload)
        except httpx.RequestError as error:
            # Transport exceptions can contain credential-bearing URLs or headers.
            message = f"Provider connection failed ({type(error).__name__})"
        else:
            if response.status_code == 200:
                return response
            message = f"Provider request failed (HTTP {response.status_code})"
    raise ProviderKeyError(message)



async def add_provider_key(
    session: AsyncSession,
    *,
    provider_name: str,
    api_key: str,
    base_url: str | None = None,
) -> AIProviderKey:
    """Add a new provider key and discover models."""
    encrypted = _encrypt_key(api_key)
    key = AIProviderKey(
        provider_name=provider_name.strip().lower(),
        api_key_encrypted=encrypted,
        base_url=base_url.rstrip("/") if base_url else None,
        is_active=True,
    )
    session.add(key)
    await session.flush()

    try:
        await discover_models(session, key.id)
    except (httpx.RequestError, ProviderKeyError) as error:
        logger.warning("Could not discover models for %s: %s", provider_name, error)
        key.last_error = str(error)[:500]

    await session.commit()
    return key


async def update_provider_key(
    session: AsyncSession,
    key_id: int,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    is_active: bool | None = None,
) -> AIProviderKey:
    """Update an existing provider key."""
    result = await session.execute(
        select(AIProviderKey).where(AIProviderKey.id == key_id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise ProviderKeyError(f"Provider key {key_id} not found")

    if api_key is not None:
        key.api_key_encrypted = _encrypt_key(api_key)
    if base_url is not None:
        key.base_url = base_url.rstrip("/") if base_url else None
    if is_active is not None:
        key.is_active = is_active

    await session.commit()
    return key


async def delete_provider_key(session: AsyncSession, key_id: int) -> bool:
    """Delete a provider key and all its models."""
    result = await session.execute(
        select(AIProviderKey).where(AIProviderKey.id == key_id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        return False
    await session.delete(key)
    await session.commit()
    return True


async def discover_models(session: AsyncSession, key_id: int) -> list[AIProviderModel]:
    """Fetch models from provider's API endpoint and store them."""
    result = await session.execute(
        select(AIProviderKey).where(AIProviderKey.id == key_id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise ProviderKeyError(f"Provider key {key_id} not found")

    api_key = _decrypt_key(key.api_key_encrypted)
    base_url = (key.base_url or "").strip() or _default_base_url(key.provider_name)

    if not base_url:
        raise ProviderKeyError(f"No base URL for provider {key.provider_name}")

    # Google Gemini uses a different endpoint format
    if key.provider_name.lower() in ("google", "gemini"):
        url = f"{base_url}/models"
        headers = {"x-goog-api-key": api_key}
    else:
        url = f"{base_url}/models"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    key.last_checked_at = datetime.now(UTC)
    try:
        response = await _provider_request("GET", url, headers=headers)
        data = response.json()
        field = "models" if key.provider_name.lower() in ("google", "gemini") else "data"
        if not isinstance(data, dict) or not isinstance(data.get(field), list):
            raise ProviderKeyError("Provider returned an invalid model catalogue")
    except ProviderKeyError as error:
        key.health = "error"
        key.last_error = str(error)
        await session.commit()
        raise
    except ValueError:
        key.health = "error"
        key.last_error = "Provider returned invalid JSON"
        await session.commit()
        raise ProviderKeyError(key.last_error) from None

    if key.provider_name.lower() in ("google", "gemini"):
        model_ids = [
            model["name"].removeprefix("models/")
            for model in data.get("models", [])
            if "name" in model
        ]
    else:
        model_ids = [model["id"] for model in data.get("data", []) if "id" in model]

    await session.execute(
        update(AIProviderKey)
        .where(AIProviderKey.id == key_id)
        .values(
            models_count=len(model_ids), last_error=None, health="catalogue_accessible"
        )
    )

    existing = await session.execute(
        select(AIProviderModel.model_id).where(AIProviderModel.provider_key_id == key_id)
    )
    existing_ids = {row[0] for row in existing.fetchall()}

    new_models = []
    for model_id in model_ids:
        if model_id not in existing_ids:
            model = AIProviderModel(
                provider_key_id=key_id,
                model_id=model_id,
                is_working=False,
            )
            session.add(model)
            new_models.append(model)

    await session.commit()
    logger.info("Discovered %d model(s) for %s", len(model_ids), key.provider_name)
    return new_models


def _has_text_response(data: object, *, is_gemini: bool) -> bool:
    if not isinstance(data, dict) or data.get("error"):
        return False
    entries = data.get("candidates" if is_gemini else "choices")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content" if is_gemini else "message")
        if not isinstance(content, dict):
            continue
        if not is_gemini:
            text = content.get("content")
            if isinstance(text, str) and text.strip():
                return True
            continue
        parts = content.get("parts")
        if isinstance(parts, list) and any(
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and part["text"].strip()
            and not part.get("thought")
            for part in parts
        ):
            return True
    return False


async def test_model(session: AsyncSession, model_id: int) -> AIProviderModel:
    """Test one model with a simple chat completion."""
    result = await session.execute(
        select(AIProviderModel).where(AIProviderModel.id == model_id)
    )
    model = result.scalar_one_or_none()
    if model is None:
        raise ProviderKeyError(f"Model {model_id} not found")

    key_result = await session.execute(
        select(AIProviderKey).where(AIProviderKey.id == model.provider_key_id)
    )
    key = key_result.scalar_one_or_none()
    if key is None:
        raise ProviderKeyError(f"Provider key for model {model_id} not found")

    api_key = _decrypt_key(key.api_key_encrypted)
    base_url = (key.base_url or "").strip() or _default_base_url(key.provider_name)

    if not base_url:
        raise ProviderKeyError(f"No base URL for provider {key.provider_name}")

    # Google Gemini uses a different API format
    is_gemini = key.provider_name.lower() in ("google", "gemini")

    payload: dict[str, object]
    if is_gemini:
        url = f"{base_url}/models/{model.model_id}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        payload = {
            "contents": [{"parts": [{"text": "Say ok in one word."}]}],
            "generationConfig": {"maxOutputTokens": 5},
        }
    else:
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model.model_id,
            "messages": [{"role": "user", "content": "Say ok in one word."}],
            "max_tokens": 5,
        }

    model.last_tested_at = datetime.now(UTC)
    try:
        response = await _provider_request("POST", url, headers=headers, payload=payload)
        data = response.json()
        if not _has_text_response(data, is_gemini=is_gemini):
            raise ProviderKeyError("Provider returned no generated text")
    except (ProviderKeyError, ValueError) as error:
        model.is_working = False
        model.error_message = (
            str(error) if isinstance(error, ProviderKeyError)
            else "Provider returned invalid JSON"
        )
        logger.warning("Model %s test failed: %s", model.model_id, model.error_message)
    else:
        model.is_working = True
        model.error_message = None
        logger.info("Model %s tested successfully", model.model_id)

    await session.commit()
    return model


async def test_provider_key(session: AsyncSession, key_id: int) -> bool:
    """Test if a provider key is working with a simple API request."""
    result = await session.execute(
        select(AIProviderKey).where(AIProviderKey.id == key_id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise ProviderKeyError(f"Provider key {key_id} not found")

    api_key = _decrypt_key(key.api_key_encrypted)
    base_url = (key.base_url or "").strip() or _default_base_url(key.provider_name)

    if not base_url:
        key.health = "error"
        key.last_checked_at = datetime.now(UTC)
        key.last_error = "No base URL configured"
        await session.commit()
        return False

    # Try a simple models request
    is_gemini = key.provider_name.lower() in ("google", "gemini")

    if is_gemini:
        url = f"{base_url}/models"
        headers = {"x-goog-api-key": api_key}
    else:
        url = f"{base_url}/models"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    key.last_checked_at = datetime.now(UTC)
    try:
        response = await _provider_request("GET", url, headers=headers)
        data = response.json()
        field = "models" if is_gemini else "data"
        if not isinstance(data, dict) or not isinstance(data.get(field), list):
            raise ProviderKeyError("Provider returned an invalid model catalogue")
    except (ProviderKeyError, ValueError) as error:
        key.health = "error"
        key.last_error = (
            str(error) if isinstance(error, ProviderKeyError)
            else "Provider returned invalid JSON"
        )
        await session.commit()
        return False
    key.health = "catalogue_accessible"
    key.last_error = None
    await session.commit()
    return True


async def test_all_models(
    session: AsyncSession, key_id: int
) -> UsageResult:
    """Test all models for a provider key. Returns {working: N, failed: N}."""
    result = await session.execute(
        select(AIProviderModel).where(AIProviderModel.provider_key_id == key_id)
    )
    models = list(result.scalars().all())

    working = 0
    failed = 0

    for model in models:
        try:
            await test_model(session, model.id)
            if model.is_working:
                working += 1
            else:
                failed += 1
        except (httpx.RequestError, ProviderKeyError) as e:
            logger.warning("Failed to test model %s: %s", model.model_id, e)
            failed += 1

    # Update key stats
    await session.execute(
        update(AIProviderKey)
        .where(AIProviderKey.id == key_id)
        .values(working_models_count=working)
    )
    await session.commit()

    return {"working": working, "failed": failed}


async def get_active_providers(session: AsyncSession) -> list[AIProviderKey]:
    """Return all active provider keys with their models loaded."""
    result = await session.execute(
        select(AIProviderKey)
        .where(AIProviderKey.is_active.is_(True))
        .options(selectinload(AIProviderKey.models))
        .order_by(AIProviderKey.provider_name, AIProviderKey.id)
    )
    return list(result.scalars().unique().all())


async def list_provider_keys(session: AsyncSession) -> list[AIProviderKey]:
    result = await session.execute(
        select(AIProviderKey)
        .options(selectinload(AIProviderKey.models))
        .order_by(AIProviderKey.provider_name, AIProviderKey.id)
    )
    return list(result.scalars().unique().all())


async def test_all_providers(
    session: AsyncSession,
) -> dict[str, UsageResult]:
    """Test all models for all active provider keys."""
    providers = await get_active_providers(session)
    results = {}

    for provider in providers:
        try:
            result = await test_all_models(session, provider.id)
            results[provider.provider_name] = result
        except (httpx.RequestError, ProviderKeyError) as e:
            logger.error(
                "Failed to test provider %s: %s", provider.provider_name, e
            )
            results[provider.provider_name] = {
                "working": 0,
                "failed": 0,
                "error": str(e),
            }

    return results


def _default_base_url(provider_name: str) -> str | None:
    """Return the default base URL for known providers."""
    defaults = {
        "openrouter": "https://openrouter.ai/api/v1",
        "together": "https://api.together.xyz/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "groq": "https://api.groq.com/openai/v1",
        "anthropic": "https://api.anthropic.com",
        "google": "https://generativelanguage.googleapis.com/v1beta",
        "gemini": "https://generativelanguage.googleapis.com/v1beta",
    }
    return defaults.get(provider_name.lower())
