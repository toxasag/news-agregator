# ruff: noqa: RUF001, E501, PLR0911, PLR0912, PLR0915
"""Multi-Provider AI Key Pool & Failover Manager.

Supports:
1. Google Gemini (Google AI Studio) with dynamic ListModels discovery,
   independent capability testing (Text, Vision, Embeddings), Round-Robin dispatch,
   and instant failover on HTTP 429/503.
2. OpenRouter, OpenAI, Anthropic, DeepSeek, Fal AI, and Custom providers.
3. Batch key import without artificial key count limits, encrypted with Fernet at rest.
4. Intelligent fallback: if an embedding model is not supported or returns 404,
   text/vision remain fully functional and the key is marked healthy (using local fastembed).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.security import get_secret_box
from app.models.ai_provider_key import AIProviderKey
from app.models.ai_provider_model import AIProviderModel
from app.services.model_routing import save_chains

logger: Final[logging.Logger] = logging.getLogger(__name__)

GEMINI_BASE_URL: Final[str] = "https://generativelanguage.googleapis.com/v1beta"
COOLDOWN_RATE_LIMIT_SECONDS: Final[int] = 60
DEFAULT_TIMEOUT: Final[float] = 25.0

# 1x1 transparent PNG for Vision capability testing
TEST_PNG_BASE64: Final[str] = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkY"
    "AAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# Provider specifications and metadata
PROVIDER_SPECS: Final[dict[str, dict[str, Any]]] = {
    "google": {
        "name": "Google Gemini (AI Studio)",
        "short_name": "Gemini",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "description": (
            "Пул ключей aistudio.google.com с авторотацией Round-Robin, "
            "динамическим опросом моделей (ListModels) и раздельным тестированием возможностей."
        ),
        "color": "#4285F4",
        "badge": "Google",
        "icon": "google",
        "placeholder": "AIzaSyA...\nAIzaSyB...\nAIzaSyC...",
    },
    "openrouter": {
        "name": "OpenRouter",
        "short_name": "OpenRouter",
        "default_base_url": "https://openrouter.ai/api/v1",
        "description": (
            "Универсальный шлюз OpenRouter: доступ к Claude 3.5 Sonnet, "
            "GPT-4o, DeepSeek V3, Llama 3 и сотням других моделей."
        ),
        "color": "#6366F1",
        "badge": "OpenRouter",
        "icon": "openrouter",
        "placeholder": "sk-or-v1-...\nsk-or-v1-...",
    },
    "openai": {
        "name": "OpenAI",
        "short_name": "OpenAI",
        "default_base_url": "https://api.openai.com/v1",
        "description": "Прямое подключение к OpenAI API: GPT-4o, GPT-4o-mini, o1, o3-mini и эмбеддинги.",
        "color": "#10A37F",
        "badge": "OpenAI",
        "icon": "openai",
        "placeholder": "sk-proj-...\nsk-proj-...",
    },
    "anthropic": {
        "name": "Anthropic Claude",
        "short_name": "Anthropic",
        "default_base_url": "https://api.anthropic.com/v1",
        "description": "Прямое подключение к Anthropic API: Claude 3.5 Sonnet v2, Claude 3.5 Haiku и Opus.",
        "color": "#D97706",
        "badge": "Anthropic",
        "icon": "anthropic",
        "placeholder": "sk-ant-...\nsk-ant-...",
    },
    "deepseek": {
        "name": "DeepSeek",
        "short_name": "DeepSeek",
        "default_base_url": "https://api.deepseek.com/v1",
        "description": "Прямой доступ к API DeepSeek: deepseek-chat (V3) и deepseek-reasoner (R1).",
        "color": "#0284C7",
        "badge": "DeepSeek",
        "icon": "deepseek",
        "placeholder": "sk-...\nsk-...",
    },
    "fal": {
        "name": "Fal AI (Flux / Imagen)",
        "short_name": "Fal AI",
        "default_base_url": "https://fal.run",
        "description": "Генерация чистых обложек и медиа через Flux Schnell / Dev без водяных знаков.",
        "color": "#EC4899",
        "badge": "Fal AI",
        "icon": "fal",
        "placeholder": "fal_key_...",
    },
    "custom": {
        "name": "Кастомный OpenAI-шлюз",
        "short_name": "Custom",
        "default_base_url": "http://localhost:8000/v1",
        "description": "Любой локальный сервер или совместимый шлюз (vLLM, Ollama, LiteLLM, LM Studio).",
        "color": "#6B7280",
        "badge": "Custom",
        "icon": "custom",
        "placeholder": "custom-key-...",
    },
}

# Per-provider in-memory round-robin cursors
_rr_cursors: dict[str, int] = {}
_cursor_lock = asyncio.Lock()


def _encrypt_key(raw_key: str) -> str:
    return get_secret_box().encrypt(raw_key.strip())


def _decrypt_key(encrypted: str) -> str:
    return get_secret_box().decrypt(encrypted).strip()


def mask_key(raw_key: str) -> str:
    """Return a masked representation of an API key, e.g. AIzaSy...7X9a."""
    clean = raw_key.strip()
    if len(clean) <= 10:
        return "***"
    return f"{clean[:6]}...{clean[-4:]}"


def _extract_host(url: str | None) -> str:
    """Extract clean domain/host from URL, e.g. 'https://vsllm.cc/v1' -> 'vsllm.cc'."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path.split("/")[0]
        if ":" in host:
            host = host.split(":")[0]
        return host
    except (ValueError, AttributeError):
        return ""


def _normalize_provider(name: str | None) -> str:
    """Normalize provider name aliases (e.g. gemini -> google)."""
    if not name:
        return "google"
    p = name.strip().lower()
    if p in ("gemini", "google_ai_studio"):
        return "google"
    return p


def _format_key_dict(k: AIProviderKey, now: datetime) -> dict[str, Any]:
    """Format an AIProviderKey into a serializable dictionary for UI."""
    try:
        raw = _decrypt_key(k.api_key_encrypted)
        masked = mask_key(raw)
    except (ValueError, KeyError):
        masked = "Ошибка расшифровки"

    in_cooldown = bool(k.cooldown_until and k.cooldown_until > now)
    remaining_cd = (
        int((k.cooldown_until - now).total_seconds())
        if (k.cooldown_until is not None and k.cooldown_until > now)
        else 0
    )

    models_list = getattr(k, "models", None) or []
    models_map = {m.model_id: m for m in models_list}

    # Find working capabilities
    has_text = any(
        m.is_working
        for m in models_list
        if "embed" not in m.model_id and "vision" not in m.model_id
    )
    has_vision = any(
        m.is_working
        for m in models_list
        if "vision" in m.model_id
        or m.model_id in ("gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.0-flash")
    )
    has_embed = any(
        m.is_working for m in models_list if "embed" in m.model_id
    )

    norm_provider = _normalize_provider(k.provider_name)
    # For google keys, check specific models if available
    if norm_provider == "google":
        text_m = (
            models_map.get("gemini-2.5-flash")
            or models_map.get("gemini-1.5-flash")
            or models_map.get("gemma-4-26b-a4b-it")
        )
        if text_m and text_m.is_working:
            has_text = True
        vision_m = models_map.get("gemini-vision") or text_m
        if vision_m and vision_m.is_working:
            has_vision = True
        embed_m = models_map.get("text-embedding-004") or models_map.get("gemini-embedding-001")
        if embed_m and embed_m.is_working:
            has_embed = True

    # Deduplicate working models preserving order
    seen_wm: set[str] = set()
    uniq_working_models: list[str] = []
    for m in models_list:
        if m.is_working and m.model_id not in seen_wm:
            seen_wm.add(m.model_id)
            uniq_working_models.append(m.model_id)

    # Fallback default working models for active keys if individual models not logged
    if k.health == "healthy" and not uniq_working_models:
        if norm_provider == "google":
            uniq_working_models = ["gemma-4-26b-a4b-it", "gemini-embedding-001"]
        elif norm_provider == "custom":
            uniq_working_models = ["deepseek-v4-flash"]

    all_models = [
        {
            "id": m.model_id,
            "db_id": m.id,
            "is_working": m.is_working,
            "error": m.error_message,
        }
        for m in models_list
    ]

    return {
        "id": k.id,
        "provider_name": norm_provider,
        "custom_name": getattr(k, "custom_name", None),
        "base_url": k.base_url,
        "masked_key": masked,
        "is_active": k.is_active,
        "health": k.health,
        "in_cooldown": in_cooldown,
        "cooldown_remaining": remaining_cd,
        "last_error": k.last_error,
        "last_checked_at": (
            k.last_checked_at.strftime("%d.%m %H:%M")
            if k.last_checked_at
            else "Не проверялся"
        ),
        "capabilities": {
            "text": has_text,
            "vision": has_vision,
            "embeddings": has_embed,
        },
        "models_count": k.models_count or len(models_list),
        "working_models_count": len(uniq_working_models),
        "working_models": uniq_working_models,
        "all_models": all_models,
    }


async def list_gemini_keys(session: AsyncSession) -> list[dict[str, Any]]:
    """List all Google AI Studio keys (backward-compatible function)."""
    stmt = (
        select(AIProviderKey)
        .where(AIProviderKey.provider_name.in_(("google", "gemini")))
        .options(selectinload(AIProviderKey.models))
        .order_by(AIProviderKey.id.asc())
    )
    keys = list((await session.execute(stmt)).scalars().all())
    now = datetime.now(UTC)
    return [_format_key_dict(k, now) for k in keys]


async def list_all_provider_pools(session: AsyncSession) -> list[dict[str, Any]]:
    """List all AI provider pools with their keys, metrics, and models."""
    stmt = (
        select(AIProviderKey)
        .options(selectinload(AIProviderKey.models))
        .order_by(AIProviderKey.provider_name.asc(), AIProviderKey.id.asc())
    )
    keys = list((await session.execute(stmt)).scalars().all())
    now = datetime.now(UTC)

    # Group keys: standard providers by provider_name, custom providers by custom_name / host
    grouped: dict[str, list[dict[str, Any]]] = {}
    for k in keys:
        pname = _normalize_provider(k.provider_name)
        if pname == "custom":
            cname = (getattr(k, "custom_name", None) or "").strip() or _extract_host(k.base_url) or "custom"
            pool_id = f"custom:{cname}"
        else:
            pool_id = pname
        grouped.setdefault(pool_id, []).append(_format_key_dict(k, now))

    pools: list[dict[str, Any]] = []

    # Always ensure google pool is present first, followed by known standard providers (except custom)
    provider_order = ["google"] + [
        p for p in PROVIDER_SPECS if p not in ("google", "custom")
    ]

    for pname in provider_order:
        spec = PROVIDER_SPECS.get(
            pname,
            {
                "name": pname.capitalize(),
                "short_name": pname.capitalize(),
                "default_base_url": "",
                "description": f"Провайдер {pname}",
                "color": "#6B7280",
                "badge": pname.upper(),
                "icon": "custom",
                "placeholder": "api-key...",
            },
        )
        pkeys = grouped.get(pname, [])

        # Include pool if it is Google or if it has keys
        if pname == "google" or len(pkeys) > 0:
            total = len(pkeys)
            healthy = sum(1 for k in pkeys if k["health"] == "healthy")
            cooldown = sum(1 for k in pkeys if k["in_cooldown"])
            errors = sum(1 for k in pkeys if k["health"] == "error")

            pools.append(
                {
                    "pool_id": pname,
                    "provider_name": pname,
                    "custom_name": None,
                    "name": spec["name"],
                    "short_name": spec["short_name"],
                    "description": spec["description"],
                    "default_base_url": spec["default_base_url"],
                    "endpoint_url": spec["default_base_url"],
                    "color": spec["color"],
                    "badge": spec["badge"],
                    "icon": spec["icon"],
                    "placeholder": spec["placeholder"],
                    "keys": pkeys,
                    "key_list": pkeys,
                    "total_keys": total,
                    "healthy_keys": healthy,
                    "cooldown_keys": cooldown,
                    "error_keys": errors,
                    # All pools are collapsed by default
                    "is_open": False,
                }
            )

    # Process custom provider groups
    for pool_id, pkeys in grouped.items():
        if pool_id.startswith("custom:"):
            cname = pool_id.split("custom:", 1)[1]
            first_key = pkeys[0] if pkeys else {}
            base_url = first_key.get("base_url") or ""
            host = _extract_host(base_url)

            # Build human-friendly title
            if cname and cname != "custom":
                display_name = cname
            elif host:
                display_name = f"{host} (OpenAI-шлюз)"
            else:
                display_name = "Кастомный OpenAI-шлюз"

            badge_text = (cname[:2] if cname and cname != "custom" else (host[:2] if host else "CU")).upper()
            total = len(pkeys)
            healthy = sum(1 for k in pkeys if k["health"] == "healthy")
            cooldown = sum(1 for k in pkeys if k["in_cooldown"])
            errors = sum(1 for k in pkeys if k["health"] == "error")

            pools.append(
                {
                    "pool_id": pool_id,
                    "provider_name": "custom",
                    "custom_name": cname if cname != "custom" else host,
                    "name": display_name,
                    "short_name": cname if cname != "custom" else (host or "Custom"),
                    "description": f"Эндпоинт: {base_url}" if base_url else "Пользовательский OpenAI-совместимый сервер",
                    "default_base_url": base_url,
                    "endpoint_url": base_url,
                    "color": "#475569",
                    "badge": badge_text,
                    "icon": "custom",
                    "placeholder": "sk-...",
                    "keys": pkeys,
                    "key_list": pkeys,
                    "total_keys": total,
                    "healthy_keys": healthy,
                    "cooldown_keys": cooldown,
                    "error_keys": errors,
                    # All pools are collapsed by default
                    "is_open": False,
                }
            )

    return pools


async def batch_add_keys_for_provider(
    session: AsyncSession,
    provider_name: str,
    raw_keys_text: str,
    base_url: str | None = None,
    custom_name: str | None = None,
) -> dict[str, Any]:
    """Parse unlimited API keys (one per line) and insert them into the database."""
    clean_pname = _normalize_provider(provider_name)
    lines = [line.strip() for line in raw_keys_text.splitlines() if line.strip()]
    if not lines:
        return {"added": 0, "errors": ["Список ключей пуст."], "provider": clean_pname}

    seen: set[str] = set()
    cleaned_keys: list[str] = []
    for line in lines:
        if line.startswith("#"):
            continue
        key = line.split()[0].strip()
        if key and key not in seen:
            seen.add(key)
            cleaned_keys.append(key)

    spec = PROVIDER_SPECS.get(clean_pname, {})
    resolved_base_url = (
        base_url.strip()
        if base_url and base_url.strip()
        else spec.get("default_base_url")
    )
    if resolved_base_url:
        resolved_base_url = resolved_base_url.rstrip("/")
        if clean_pname in (
            "custom",
            "openai",
            "openrouter",
            "deepseek",
        ) and not resolved_base_url.endswith("/v1") and "/v1/" not in resolved_base_url:
            resolved_base_url = f"{resolved_base_url}/v1"

    clean_custom_name = (custom_name or "").strip() or None
    if clean_pname == "custom" and not clean_custom_name and resolved_base_url:
        clean_custom_name = _extract_host(resolved_base_url)

    existing_stmt = select(AIProviderKey).where(
        AIProviderKey.provider_name == clean_pname
    )
    if clean_custom_name:
        existing_stmt = existing_stmt.where(AIProviderKey.custom_name == clean_custom_name)

    existing = list((await session.execute(existing_stmt)).scalars().all())
    existing_raw: set[str] = set()
    for ek in existing:
        with contextlib.suppress(ValueError, KeyError):
            existing_raw.add(_decrypt_key(ek.api_key_encrypted))

    added_count = 0
    for key in cleaned_keys:
        if key in existing_raw:
            continue
        provider_key = AIProviderKey(
            provider_name=clean_pname,
            custom_name=clean_custom_name,
            api_key_encrypted=_encrypt_key(key),
            base_url=resolved_base_url,
            is_active=True,
            health="untested",
            models_count=0,
        )
        session.add(provider_key)
        await session.flush()

        # Seed initial model entries based on provider
        initial_models: list[str] = []
        if clean_pname == "google":
            initial_models = ["gemini-2.5-flash", "gemini-1.5-flash", "text-embedding-004"]
        elif clean_pname == "openrouter":
            initial_models = ["google/gemini-2.5-flash", "anthropic/claude-3.5-sonnet"]
        elif clean_pname == "openai":
            initial_models = ["gpt-4o-mini", "gpt-4o", "text-embedding-3-small"]
        elif clean_pname == "anthropic":
            initial_models = ["claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022"]
        elif clean_pname == "deepseek":
            initial_models = ["deepseek-chat", "deepseek-reasoner"]

        for mid in initial_models:
            session.add(
                AIProviderModel(
                    provider_key_id=provider_key.id,
                    model_id=mid,
                    is_working=False,
                )
            )
        provider_key.models_count = len(initial_models)
        added_count += 1

    await session.commit()
    logger.info(
        "Batch added %d new key(s) for provider '%s' (no limit)",
        added_count,
        clean_pname,
    )
    return {
        "added": added_count,
        "total_submitted": len(cleaned_keys),
        "provider": clean_pname,
    }


async def batch_add_gemini_keys(
    session: AsyncSession, raw_keys_text: str
) -> dict[str, Any]:
    """Backward-compatible batch add for Google Gemini keys without 20-key limit."""
    return await batch_add_keys_for_provider(session, "google", raw_keys_text)


async def delete_key_from_pool(session: AsyncSession, key_id: int) -> bool:
    """Delete any provider key by ID."""
    key = await session.get(AIProviderKey, key_id)
    if key is None:
        return False
    await session.delete(key)
    await session.commit()
    logger.info("Deleted provider key #%d (%s)", key_id, key.provider_name)
    return True


async def delete_gemini_key(session: AsyncSession, key_id: int) -> bool:
    """Backward-compatible delete for Google Gemini keys."""
    return await delete_key_from_pool(session, key_id)


async def mark_key_cooldown(
    session: AsyncSession,
    key_id: int,
    reason: str,
    seconds: int = COOLDOWN_RATE_LIMIT_SECONDS,
    health: str = "rate_limited",
) -> None:
    """Mark a key as rate-limited or temporarily in cooldown."""
    until = datetime.now(UTC) + timedelta(seconds=seconds)
    await session.execute(
        update(AIProviderKey)
        .where(AIProviderKey.id == key_id)
        .values(
            health=health,
            cooldown_until=until,
            last_error=reason[:500],
        )
    )
    await session.commit()
    logger.warning("Key #%d set to cooldown for %ds (%s): %s", key_id, seconds, health, reason)


async def get_next_healthy_key(
    session: AsyncSession, provider_name: str = "google"
) -> tuple[int, str]:
    """Round-robin dispatch to the next available and non-cooled-down key for a provider."""
    pname = provider_name.strip().lower()
    now = datetime.now(UTC)
    stmt = (
        select(AIProviderKey)
        .where(
            AIProviderKey.provider_name == pname,
            AIProviderKey.is_active.is_(True),
        )
        .order_by(AIProviderKey.id.asc())
    )
    keys = list((await session.execute(stmt)).scalars().all())
    if not keys:
        raise RuntimeError(
            f"В пуле '{pname}' нет активных ключей. Добавьте ключи в /settings/ai."
        )

    available = [
        k for k in keys if not (k.cooldown_until and k.cooldown_until > now)
    ]
    if not available:
        keys.sort(key=lambda k: k.cooldown_until or datetime.min.replace(tzinfo=UTC))
        available = [keys[0]]
        logger.warning(
            "All keys for '%s' are in cooldown, falling back to key #%d",
            pname,
            keys[0].id,
        )

    async with _cursor_lock:
        cursor = _rr_cursors.get(pname, 0)
        idx = cursor % len(available)
        _rr_cursors[pname] = cursor + 1
        chosen = available[idx]

    raw_key = _decrypt_key(chosen.api_key_encrypted)
    return chosen.id, raw_key


# ------------------------------------------------ Dynamic Gemini ListModels & Capability Testing


async def query_gemini_models(
    client: httpx.AsyncClient, raw_key: str
) -> dict[str, Any]:
    """Query Google AI Studio ModelService.ListModels endpoint.

    GET https://generativelanguage.googleapis.com/v1beta/models
    Header: x-goog-api-key: raw_key
    """
    url = f"{GEMINI_BASE_URL}/models"
    headers = {"x-goog-api-key": raw_key}
    try:
        res = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        return {
            "ok": False,
            "status_code": 0,
            "error": f"Ошибка сети при обращении к Google API: {exc}",
            "models": [],
        }

    if res.status_code == 401:
        return {
            "ok": False,
            "status_code": 401,
            "error": "HTTP 401: Ключ отклонён Google API (неверный ключ или ограничения проекта в aistudio.google.com)",
            "models": [],
        }
    if res.status_code == 403:
        return {
            "ok": False,
            "status_code": 403,
            "error": "HTTP 403: Доступ запрещён Google API (проверьте права и биллинг проекта)",
            "models": [],
        }
    if res.status_code == 429:
        return {
            "ok": False,
            "status_code": 429,
            "error": "HTTP 429: Превышена квота запросов (Rate Limit Google API)",
            "models": [],
        }
    if res.status_code != 200:
        return {
            "ok": False,
            "status_code": res.status_code,
            "error": f"HTTP {res.status_code}: {res.text[:250]}",
            "models": [],
        }

    try:
        data = res.json()
    except ValueError:
        return {
            "ok": False,
            "status_code": 200,
            "error": "Google API вернул некорректный JSON",
            "models": [],
        }

    raw_models = data.get("models", [])
    return {
        "ok": True,
        "status_code": 200,
        "models": raw_models,
    }


async def test_gemini_key_dynamic(
    session: AsyncSession, key_id: int
) -> dict[str, Any]:
    """Perform smart dynamic testing for a Google Gemini key.

    1. Polls ModelService.ListModels to verify key authenticity and enumerate supported models.
    2. Identifies generative models supporting 'generateContent' and tests text & vision.
    3. Identifies embedding models supporting 'embedContent' and tests embeddings.
    4. If embedding is unavailable or 404, embeddings is marked False (graceful fallback to
       local fastembed), but key health remains HEALTHY because text and OCR are fully working!
    """
    stmt = (
        select(AIProviderKey)
        .where(AIProviderKey.id == key_id)
        .options(selectinload(AIProviderKey.models))
        .execution_options(populate_existing=True)
    )
    key = (await session.execute(stmt)).scalar_one_or_none()
    if key is None:
        return {"ok": False, "error": "Ключ не найден"}

    raw_key = _decrypt_key(key.api_key_encrypted)
    now = datetime.now(UTC)

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        # Step 1: ListModels discovery
        discovery = await query_gemini_models(client, raw_key)
        if not discovery["ok"]:
            key.last_checked_at = now
            if discovery.get("status_code") == 429:
                key.health = "rate_limited"
                key.cooldown_until = now + timedelta(seconds=60)
            else:
                key.health = "error"
            key.last_error = discovery["error"]
            await session.commit()
            return {
                "ok": False,
                "error": discovery["error"],
                "text": {"ok": False, "error": discovery["error"]},
                "vision": {"ok": False, "error": discovery["error"]},
                "embeddings": {"ok": False, "error": discovery["error"]},
            }

        raw_models: list[dict[str, Any]] = discovery["models"]
        gen_models: list[str] = []
        embed_models: list[str] = []
        all_discovered: list[str] = []

        # Suffixes/substrings that indicate non-text-generation models
        non_text_markers = (
            "-tts", "-image", "lyria", "robotics", "veo", "aqa",
            "computer-use", "nano-banana", "transcribe",
        )

        for m in raw_models:
            mname = m.get("name", "").removeprefix("models/")
            if mname:
                all_discovered.append(mname)
            methods = m.get("supportedGenerationMethods", [])
            # Only include pure text-capable models, not TTS/image/audio
            if "generateContent" in methods and not any(
                marker in mname for marker in non_text_markers
            ):
                gen_models.append(mname)
            if "embedContent" in methods:
                embed_models.append(mname)

        # Step 2: Test Text Generation
        # Pick preferred text candidate — newest stable first
        text_candidates = [
            "gemini-2.5-flash",
            "gemini-3.5-flash",
            "gemini-3.8-flash",
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-2.5-pro",
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]
        chosen_text = next((c for c in text_candidates if c in gen_models), None)
        if not chosen_text and gen_models:
            chosen_text = gen_models[0]
        if not chosen_text:
            chosen_text = "gemini-2.5-flash"

        headers = {"x-goog-api-key": raw_key, "Content-Type": "application/json"}
        payload_text = {
            "contents": [{"parts": [{"text": "Ответь одним словом: РАБОТАЕТ"}]}],
            "generationConfig": {"maxOutputTokens": 10},
        }

        text_url = f"{GEMINI_BASE_URL}/models/{chosen_text}:generateContent"
        text_ok = False
        text_err: str | None = None
        tried_models: set[str] = set()
        try:
            res_text = await client.post(text_url, headers=headers, json=payload_text)
            tried_models.add(chosen_text)
            # If 404, walk through gen_models until one works
            if res_text.status_code == 404 and gen_models:
                for fallback_model in gen_models:
                    if fallback_model in tried_models:
                        continue
                    tried_models.add(fallback_model)
                    fb_url = f"{GEMINI_BASE_URL}/models/{fallback_model}:generateContent"
                    res_text = await client.post(fb_url, headers=headers, json=payload_text)
                    if res_text.status_code != 404:
                        chosen_text = fallback_model
                        break

            if res_text.status_code == 200:
                tdata = res_text.json()
                if "candidates" in tdata and len(tdata["candidates"]) > 0:
                    text_ok = True
                else:
                    text_err = "Ответ Google API не содержит кандидатов"
            else:
                text_err = f"HTTP {res_text.status_code}: {res_text.text[:200]}"
        except httpx.RequestError as exc:
            text_err = f"Ошибка сети при тесте текста: {exc}"

        # Step 3: Test Vision (Multimodal)
        vision_ok = False
        vision_err: str | None = None
        if text_ok:
            payload_vision = {
                "contents": [
                    {
                        "parts": [
                            {"text": "Что на изображении? Ответь одним словом."},
                            {
                                "inline_data": {
                                    "mime_type": "image/png",
                                    "data": TEST_PNG_BASE64,
                                }
                            },
                        ]
                    }
                ],
                "generationConfig": {"maxOutputTokens": 10},
            }
            try:
                vision_url = f"{GEMINI_BASE_URL}/models/{chosen_text}:generateContent"
                res_vis = await client.post(vision_url, headers=headers, json=payload_vision)
                if res_vis.status_code == 200 and "candidates" in res_vis.json():
                    vision_ok = True
                else:
                    vision_err = f"HTTP {res_vis.status_code}: {res_vis.text[:150]}"
            except httpx.RequestError as exc:
                vision_err = f"Ошибка сети при тесте Vision: {exc}"
        else:
            vision_err = "Пропущено из-за сбоя текстового ответа"

        # Step 4: Test Embeddings
        embed_candidates = [
            "text-embedding-004",
            "embedding-001",
            "gemini-embedding-exp-03-07",
            "text-embedding-preview-0812",
        ]
        chosen_embed = next((c for c in embed_candidates if c in embed_models), None)
        if not chosen_embed and embed_models:
            chosen_embed = embed_models[0]

        embed_ok = False
        embed_err: str | None = None

        if chosen_embed:
            embed_url = f"{GEMINI_BASE_URL}/models/{chosen_embed}:embedContent"
            payload_embed = {
                "content": {"parts": [{"text": "Тестовый вектор для дедупликации"}]}
            }
            try:
                res_emb = await client.post(embed_url, headers=headers, json=payload_embed)
                if res_emb.status_code == 404:
                    # Also try v1 endpoint for text-embedding-004
                    v1_url = f"https://generativelanguage.googleapis.com/v1/models/{chosen_embed}:embedContent"
                    res_v1 = await client.post(v1_url, headers=headers, json=payload_embed)
                    if res_v1.status_code == 200 and "embedding" in res_v1.json():
                        embed_ok = True
                    else:
                        embed_err = "Модель эмбеддингов недоступна (активен fastembed)"
                elif res_emb.status_code == 200 and "embedding" in res_emb.json():
                    embed_ok = True
                else:
                    embed_err = f"HTTP {res_emb.status_code}: {res_emb.text[:150]}"
            except httpx.RequestError as exc:
                embed_err = f"Ошибка сети при тесте эмбеддингов: {exc}"
        else:
            embed_err = "Модели embedContent отсутствуют (активен fastembed)"

        # Step 5: Update Database Models
        existing_models = {m.model_id: m for m in key.models}

        def _upsert_model(mid: str, working: bool, err: str | None) -> None:
            if mid in existing_models:
                m = existing_models[mid]
                m.is_working = working
                m.error_message = err
                m.last_tested_at = now
            else:
                new_m = AIProviderModel(
                    provider_key_id=key.id,
                    model_id=mid,
                    is_working=working,
                    error_message=err,
                    last_tested_at=now,
                )
                session.add(new_m)
                existing_models[mid] = new_m

        _upsert_model(chosen_text, text_ok, text_err)
        _upsert_model("gemini-vision", vision_ok, vision_err)
        if chosen_embed:
            _upsert_model(chosen_embed, embed_ok, embed_err)

        # Store all discovered models from Google ListModels
        for mid in all_discovered:
            if mid not in existing_models:
                session.add(
                    AIProviderModel(
                        provider_key_id=key.id,
                        model_id=mid,
                        is_working=False,
                        last_tested_at=None,
                    )
                )

        key.last_checked_at = now
        key.models_count = len(all_discovered) if all_discovered else len(key.models)

        # Key health decision:
        # If text is working, key is HEALTHY! (Fastembed seamlessly handles vector deduplication)
        if text_ok:
            key.health = "healthy"
            key.last_error = None
        else:
            key.health = "error"
            key.last_error = text_err

        await session.commit()

        return {
            "ok": text_ok,
            "all_working": text_ok and vision_ok and embed_ok,
            "text": {"ok": text_ok, "model": chosen_text, "error": text_err},
            "vision": {"ok": vision_ok, "error": vision_err},
            "embeddings": {"ok": embed_ok, "model": chosen_embed, "error": embed_err},
            "discovered_models_count": len(all_discovered),
        }


# ------------------------------------------------ Other Providers Testing (OpenRouter, OpenAI, etc.)


async def test_openai_compatible_key(
    session: AsyncSession, key_id: int
) -> dict[str, Any]:
    """Test an OpenAI-compatible provider key (OpenRouter, OpenAI, DeepSeek, Custom)."""
    stmt = (
        select(AIProviderKey)
        .where(AIProviderKey.id == key_id)
        .options(selectinload(AIProviderKey.models))
        .execution_options(populate_existing=True)
    )
    key = (await session.execute(stmt)).scalar_one_or_none()
    if key is None:
        return {"ok": False, "error": "Ключ не найден"}

    raw_key = _decrypt_key(key.api_key_encrypted)
    now = datetime.now(UTC)
    spec = PROVIDER_SPECS.get(key.provider_name, {})
    raw_base = (
        (key.base_url or "").strip()
        or spec.get("default_base_url")
        or "https://openrouter.ai/api/v1"
    ).rstrip("/")
    if not raw_base.endswith("/v1") and "/v1/" not in raw_base:
        base_url = f"{raw_base}/v1"
    else:
        base_url = raw_base

    # Update in DB if base_url was missing /v1
    if key.base_url and key.base_url != base_url:
        key.base_url = base_url

    headers = {
        "Authorization": f"Bearer {raw_key}",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/event-stream",
    }

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        # 1. Discover models via GET /models
        models_url = f"{base_url}/models"
        discovered_ids: list[str] = []
        try:
            res_m = await client.get(models_url, headers=headers)
            if res_m.status_code == 200:
                try:
                    mdata = res_m.json()
                    items = mdata.get("data", []) if isinstance(mdata, dict) else []
                    discovered_ids = [
                        item["id"] for item in items if isinstance(item, dict) and "id" in item
                    ]
                except (ValueError, KeyError):
                    discovered_ids = []
            elif res_m.status_code in (401, 403):
                key.health = "error"
                key.last_error = f"HTTP {res_m.status_code}: Недействительный API-ключ"
                key.last_checked_at = now
                await session.commit()
                return {"ok": False, "error": key.last_error}
        except httpx.RequestError as exc:
            logger.warning("Could not fetch models for #%d: %s", key_id, exc)

        # 2. Pick a model to test chat completion
        test_model_id = "gpt-4o-mini"
        if key.provider_name == "openrouter":
            test_model_id = (
                "google/gemini-2.5-flash"
                if "google/gemini-2.5-flash" in discovered_ids
                else (discovered_ids[0] if discovered_ids else "auto")
            )
        elif key.provider_name == "deepseek":
            test_model_id = "deepseek-chat"
        elif discovered_ids:
            preferred = [
                "grok-4.5",
                "mistral-large-2512",
                "deepseek-v4-flash",
                "gemini-3.5-flash-lite",
                "grok-4.7",
                "grok-4.3",
                "glm-4.7-flash",
                "deepseek-chat",
                "gpt-4o-mini",
                "gpt-4o",
                "claude-3-5-haiku",
                "claude-haiku-4-5-20251001",
            ]
            chosen = next((p for p in preferred if p in discovered_ids), None)
            test_model_id = chosen or discovered_ids[0]

        chat_url = f"{base_url}/chat/completions"
        payload = {
            "model": test_model_id,
            "messages": [{"role": "user", "content": "Ping"}],
            "max_tokens": 5,
        }

        try:
            res_chat = await client.post(chat_url, headers=headers, json=payload)
            if res_chat.status_code == 200:
                key.health = "healthy"
                key.last_error = None
                ok = True
                err = None
            elif res_chat.status_code in (500, 502, 503, 504):
                # Fallback to streaming request: some reverse proxies/gateways only support SSE
                stream_payload = dict(payload)
                stream_payload["stream"] = True
                async with client.stream("POST", chat_url, headers=headers, json=stream_payload) as sres:
                    if sres.status_code == 200:
                        key.health = "healthy"
                        key.last_error = None
                        ok = True
                        err = None
                    else:
                        key.health = "error"
                        err_body = (await sres.aread()).decode("utf-8", errors="ignore")
                        key.last_error = _clean_error_message(sres.status_code, err_body)
                        ok = False
                        err = key.last_error
            else:
                key.health = "error"
                key.last_error = _clean_error_message(res_chat.status_code, res_chat.text)
                ok = False
                err = key.last_error
        except httpx.RequestError as exc:
            key.health = "error"
            key.last_error = f"Ошибка соединения: {exc}"
            ok = False
            err = key.last_error

        # 3. Update Database Models (up to 300)
        existing_models = {m.model_id: m for m in key.models}
        for mid in discovered_ids[:300]:
            is_work = bool(mid == test_model_id and ok)
            if mid in existing_models:
                m = existing_models[mid]
                if is_work:
                    m.is_working = True
                    m.error_message = None
                    m.last_tested_at = now
            else:
                new_m = AIProviderModel(
                    provider_key_id=key.id,
                    model_id=mid,
                    is_working=is_work,
                    error_message=None,
                    last_tested_at=now if is_work else None,
                )
                session.add(new_m)

        key.last_checked_at = now
        key.models_count = len(discovered_ids)
        key.working_models_count = 1 if ok else 0

        await session.commit()
        return {
            "ok": ok,
            "error": err,
            "text": {"ok": ok, "model": test_model_id, "error": err},
            "vision": {"ok": ok, "error": None},
            "embeddings": {"ok": False, "error": "Эмбеддинги через fastembed"},
            "discovered_models_count": len(discovered_ids),
        }


async def test_anthropic_key(session: AsyncSession, key_id: int) -> dict[str, Any]:
    """Test an Anthropic Claude API key."""
    stmt = (
        select(AIProviderKey)
        .where(AIProviderKey.id == key_id)
        .options(selectinload(AIProviderKey.models))
        .execution_options(populate_existing=True)
    )
    key = (await session.execute(stmt)).scalar_one_or_none()
    if key is None:
        return {"ok": False, "error": "Ключ не найден"}

    raw_key = _decrypt_key(key.api_key_encrypted)
    now = datetime.now(UTC)
    base_url = "https://api.anthropic.com/v1"
    headers = {
        "x-api-key": raw_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "Ping"}],
    }

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        try:
            res = await client.post(f"{base_url}/messages", headers=headers, json=payload)
            if res.status_code == 200:
                key.health = "healthy"
                key.last_error = None
                ok = True
                err = None
            else:
                key.health = "error"
                key.last_error = f"HTTP {res.status_code}: {res.text[:200]}"
                ok = False
                err = key.last_error
        except httpx.RequestError as exc:
            key.health = "error"
            key.last_error = f"Ошибка сети: {exc}"
            ok = False
            err = key.last_error

        key.last_checked_at = now
        await session.commit()
        return {
            "ok": ok,
            "error": err,
            "text": {"ok": ok, "model": "claude-3-5-haiku", "error": err},
            "vision": {"ok": ok, "error": None},
            "embeddings": {"ok": False, "error": "Локальный fastembed"},
        }


async def test_key_in_pool(session: AsyncSession, key_id: int) -> dict[str, Any]:
    """Universal capability tester: dispatches to appropriate provider tester."""
    stmt = (
        select(AIProviderKey)
        .where(AIProviderKey.id == key_id)
        .options(selectinload(AIProviderKey.models))
        .execution_options(populate_existing=True)
    )
    key = (await session.execute(stmt)).scalar_one_or_none()
    if key is None:
        return {"ok": False, "error": "Ключ не найден"}

    pname = _normalize_provider(key.provider_name)
    if pname == "google":
        return await test_gemini_key_dynamic(session, key_id)
    if pname == "anthropic":
        return await test_anthropic_key(session, key_id)
    # Default: OpenAI-compatible (OpenRouter, OpenAI, DeepSeek, Fal, Custom)
    return await test_openai_compatible_key(session, key_id)


async def test_all_capabilities_for_key(
    session: AsyncSession, key_id: int
) -> dict[str, Any]:
    """Backward-compatible capability testing for a single key."""
    return await test_key_in_pool(session, key_id)


async def test_key_capability(
    session: AsyncSession, key_id: int, capability: str
) -> dict[str, Any]:
    """Backward-compatible test for single capability."""
    res = await test_key_in_pool(session, key_id)
    if capability in res:
        return cast(dict[str, Any], res[capability])
    return {"ok": res.get("ok", False), "error": res.get("error")}


async def test_all_keys_in_pool(
    session: AsyncSession,
    provider_name: str | None = None,
    custom_name: str | None = None,
) -> dict[str, Any]:
    """Test all keys in a specific provider pool (or all keys if provider_name is None)."""
    stmt = select(AIProviderKey.id)
    if provider_name:
        clean = _normalize_provider(provider_name)
        stmt = stmt.where(AIProviderKey.provider_name == clean)
    if custom_name and custom_name.strip():
        stmt = stmt.where(AIProviderKey.custom_name == custom_name.strip())
    key_ids = list((await session.execute(stmt)).scalars().all())

    results = []
    healthy_count = 0
    error_count = 0
    for kid in key_ids:
        try:
            r = await test_key_in_pool(session, kid)
            results.append({"key_id": kid, "result": r})
            if r.get("ok"):
                healthy_count += 1
            else:
                error_count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error testing key #%d: %s", kid, exc)
            results.append({"key_id": kid, "result": {"ok": False, "error": str(exc)}})
            error_count += 1

    return {
        "total": len(key_ids),
        "tested": len(results),
        "healthy": healthy_count,
        "errors": error_count,
        "results": results,
    }


# ------------------------------------------------ Catalog of Working Models & Smart Matrix Selectors


def _clean_error_message(status_code: int, raw_text: str) -> str:
    """Transform raw HTTP response or HTML error into a concise, readable Russian description."""
    text_lower = raw_text.lower()
    if status_code == 502 or "<!doctype" in text_lower or "<html" in text_lower:
        return "HTTP 502: Сервер провайдера временно недоступен (Bad Gateway со стороны шлюза). Попробуйте позже."
    if status_code == 503:
        return "HTTP 503: Сервер перегружен или на обслуживании (Service Unavailable)."
    if status_code == 401:
        return "HTTP 401: Ошибка авторизации — ключ отклонён провайдером или заблокирован."
    if status_code == 402:
        return "HTTP 402: Недостаточно средств/кредитов на аккаунте (Payment Required)."

    # Try parsing JSON error message
    with contextlib.suppress(json.JSONDecodeError, ValueError, TypeError):
        data = json.loads(raw_text)
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code")
                if msg:
                    clean_msg = str(msg).strip()
                    if status_code == 404:
                        return f"HTTP 404: Модель недоступна ({clean_msg})"
                    if status_code == 429:
                        return f"HTTP 429: Превышена квота или лимит запросов ({clean_msg})"
                    return f"HTTP {status_code}: {clean_msg}"
            elif isinstance(err, str):
                return f"HTTP {status_code}: {err}"

    if status_code == 404:
        return "HTTP 404: Модель не поддерживается данным эндпоинтом или удалена провайдером."
    if status_code == 429:
        return "HTTP 429: Превышен лимит запросов к модели (Rate limit / Quota exceeded)."

    # Strip tags and take first 160 chars
    clean = re.sub(r"<[^>]+>", " ", raw_text).strip()
    return f"HTTP {status_code}: {clean[:160]}"


async def ping_model_api(
    client: httpx.AsyncClient,
    provider_name: str,
    base_url: str | None,
    raw_key: str,
    model_id: str,
) -> tuple[bool, str | None]:
    """Perform a fast ping request to verify if model_id is operational on provider.

    Returns (is_working, error_message).
    """
    clean_pname = _normalize_provider(provider_name)
    mid_lower = model_id.lower()

    if clean_pname == "google":
        if "embed" in mid_lower or "embedding" in mid_lower:
            url = f"{GEMINI_BASE_URL}/models/{model_id}:embedContent"
            headers = {"x-goog-api-key": raw_key, "Content-Type": "application/json"}
            payload: dict[str, Any] = {"content": {"parts": [{"text": "Say ok"}]}}
        elif "imagen" in mid_lower:
            url = f"{GEMINI_BASE_URL}/models/{model_id}:predict"
            headers = {"x-goog-api-key": raw_key, "Content-Type": "application/json"}
            payload = {
                "instances": [{"prompt": "test ping"}],
                "parameters": {"sampleCount": 1, "aspectRatio": "1:1"},
            }
        elif mid_lower.endswith("-image") or "banana" in mid_lower:
            url = f"{GEMINI_BASE_URL}/interactions"
            headers = {"x-goog-api-key": raw_key, "Content-Type": "application/json"}
            payload = {
                "model": model_id,
                "input": [{"type": "text", "text": "test ping"}],
                "response_format": {"type": "image", "aspect_ratio": "1:1"},
            }
        else:
            url = f"{GEMINI_BASE_URL}/models/{model_id}:generateContent"
            headers = {"x-goog-api-key": raw_key, "Content-Type": "application/json"}
            payload = {
                "contents": [{"parts": [{"text": "Say ok"}]}],
                "generationConfig": {"maxOutputTokens": 5},
            }
        try:
            res = await client.post(url, headers=headers, json=payload)
            if res.status_code == 200:
                return True, None
            if res.status_code == 429 and ("image" in mid_lower or "imagen" in mid_lower or "banana" in mid_lower):
                return False, "HTTP 429: Google AI Studio требует подключённый биллинг (Billing/Pay-As-You-Go) — 0 картинок/день на Free Tier"
            return False, _clean_error_message(res.status_code, res.text)
        except httpx.RequestError as exc:
            return False, f"Ошибка сети: {exc}"
    else:
        raw_base = (base_url or "").strip() or "https://openrouter.ai/api/v1"
        target_base = (
            raw_base
            if raw_base.endswith("/v1") or "/v1/" in raw_base
            else f"{raw_base}/v1"
        )
        is_image_model = any(w in mid_lower for w in ("dall-e", "flux"))
        if is_image_model:
            url = f"{target_base}/images/generations"
            payload = {
                "model": model_id,
                "prompt": "test ping",
                "n": 1,
                "size": "1024x1024",
            }
        else:
            url = f"{target_base}/chat/completions"
            payload = {
                "model": model_id,
                "messages": [{"role": "user", "content": "Say ok"}],
                "max_tokens": 5,
            }
        headers = {
            "Authorization": f"Bearer {raw_key}",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/event-stream",
        }
        try:
            res = await client.post(url, headers=headers, json=payload)
            if res.status_code == 200:
                return True, None
            if res.status_code in (500, 502, 503, 504):
                # Try streaming fallback (e.g. Noir gateway / Cloudflare edge)
                stream_payload = dict(payload)
                stream_payload["stream"] = True
                async with client.stream("POST", url, headers=headers, json=stream_payload) as sres:
                    if sres.status_code == 200:
                        return True, None
                    err_body = (await sres.aread()).decode("utf-8", errors="ignore")
                    return False, _clean_error_message(sres.status_code, err_body)
            return False, _clean_error_message(res.status_code, res.text)
        except httpx.RequestError as exc:
            return False, f"Ошибка сети: {exc}"


async def test_individual_model_by_id(
    session: AsyncSession, model_db_id: int
) -> dict[str, Any]:
    """Test a single model from ai_provider_models with a fast ping request."""
    model = await session.get(AIProviderModel, model_db_id)
    if model is None:
        return {"ok": False, "error": f"Модель #{model_db_id} не найдена"}

    key = await session.get(AIProviderKey, model.provider_key_id)
    if key is None:
        return {"ok": False, "error": "Ключ провайдера не найден"}

    raw_key = _decrypt_key(key.api_key_encrypted)
    now = datetime.now(UTC)

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        ok, err = await ping_model_api(
            client, key.provider_name, key.base_url, raw_key, model.model_id
        )

    model.is_working = ok
    model.error_message = err
    model.last_tested_at = now
    await session.commit()
    return {
        "ok": ok,
        "model_id": model.model_id,
        "model_db_id": model.id,
        "error": model.error_message,
    }


async def test_all_models_for_key(
    session: AsyncSession,
    key_id: int,
    concurrency: int = 6,
    untested_only: bool = False,
) -> dict[str, Any]:
    """Test 100% of discovered models for a specific key concurrently with rate limiting."""
    key = await session.get(AIProviderKey, key_id)
    if key is None:
        return {"tested": 0, "working": 0, "error": "Ключ не найден"}

    stmt = select(AIProviderModel).where(AIProviderModel.provider_key_id == key_id)
    if untested_only:
        stmt = stmt.where(AIProviderModel.last_tested_at.is_(None))
    models = list((await session.execute(stmt)).scalars().all())
    if not models:
        return {"tested": 0, "working": 0}

    raw_key = _decrypt_key(key.api_key_encrypted)
    clean_pname = key.provider_name
    base_url = key.base_url
    now = datetime.now(UTC)

    model_tuples = [(m.id, m.model_id) for m in models]
    results: list[tuple[int, bool, str | None]] = []
    sem = asyncio.Semaphore(max(1, concurrency))

    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, limits=limits) as client:
        async def _test_one(db_id: int, m_id: str) -> None:
            async with sem:
                ok, err = await ping_model_api(client, clean_pname, base_url, raw_key, m_id)
                results.append((db_id, ok, err))

        await asyncio.gather(*[_test_one(db_id, m_id) for db_id, m_id in model_tuples])

    # Batch update DB models in current session
    working_count = 0
    model_map = {m.id: m for m in models}
    for db_id, ok, err in results:
        m = model_map.get(db_id)
        if m:
            m.is_working = ok
            m.error_message = err
            m.last_tested_at = now
            if ok:
                working_count += 1

    # Recalculate key health and working count
    all_models_stmt = select(AIProviderModel).where(AIProviderModel.provider_key_id == key_id)
    all_key_models = list((await session.execute(all_models_stmt)).scalars().all())
    total_working = sum(1 for km in all_key_models if km.is_working)
    key.working_models_count = total_working
    key.models_count = len(all_key_models)
    key.last_checked_at = now
    if total_working > 0:
        key.health = "healthy"
        key.last_error = None
    elif all_key_models:
        key.health = "error"
        key.last_error = "Все модели вернули ошибки при проверке"

    await session.commit()
    return {
        "key_id": key_id,
        "tested": len(results),
        "working": working_count,
        "total_working": total_working,
    }


async def test_all_models_across_all_keys(
    session: AsyncSession,
    concurrency: int = 6,
    untested_only: bool = False,
) -> dict[str, Any]:
    """Run batch testing across all active keys and models."""
    stmt = (
        select(AIProviderKey)
        .where(AIProviderKey.is_active.is_(True))
        .order_by(AIProviderKey.id.asc())
    )
    keys = list((await session.execute(stmt)).scalars().all())
    total_tested = 0
    total_working = 0

    # Group Google keys: only test models on the first active Google key, don't repeat identical models 50 times
    google_tested = False
    for k in keys:
        if k.provider_name == "google":
            if google_tested:
                continue
            google_tested = True
        res = await test_all_models_for_key(
            session, k.id, concurrency=concurrency, untested_only=untested_only
        )
        total_tested += res.get("tested", 0)
        total_working += res.get("working", 0)

    return {"tested": total_tested, "working": total_working}


async def test_top_models_for_key(
    session: AsyncSession, key_id: int, limit: int = 5
) -> dict[str, Any]:
    """Test top models for a specific key. If limit <= 0, tests 100% of models."""
    if limit <= 0:
        return await test_all_models_for_key(session, key_id)

    stmt = (
        select(AIProviderModel)
        .where(AIProviderModel.provider_key_id == key_id)
    )
    all_models = list((await session.execute(stmt)).scalars().all())
    if not all_models:
        return {"tested": 0, "working": 0}

    preferred_candidates = [
        "grok-4.5",
        "mistral-large-2512",
        "deepseek-v4-flash",
        "gemini-3.5-flash-lite",
        "grok-4.7",
        "grok-4.3",
        "glm-4.7-flash",
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gpt-4o-mini",
        "deepseek-chat",
        "claude-3-5-haiku",
    ]

    def _model_priority(m: AIProviderModel) -> tuple[int, int, int]:
        mid = m.model_id.lower()
        pref_idx = 999
        for idx, pref in enumerate(preferred_candidates):
            if pref in mid:
                pref_idx = idx
                break
        status_rank = 0 if m.is_working else (1 if not m.error_message or m.error_message == "Не тестировалась" else 2)
        return (pref_idx, status_rank, m.id)

    sorted_models = sorted(all_models, key=_model_priority)[:limit]

    tested = 0
    working = 0
    for m in sorted_models:
        r = await test_individual_model_by_id(session, m.id)
        tested += 1
        if r.get("ok"):
            working += 1
    return {"tested": tested, "working": working}



async def get_working_models_catalog(
    session: AsyncSession,
) -> dict[str, Any]:
    """Retrieve all verified working models across active provider pools and format options for Matrix."""
    stmt = (
        select(AIProviderModel, AIProviderKey)
        .join(AIProviderKey, AIProviderModel.provider_key_id == AIProviderKey.id)
        .where(
            AIProviderKey.is_active.is_(True),
            AIProviderModel.is_working.is_(True),
        )
        .order_by(AIProviderKey.provider_name.asc(), AIProviderModel.model_id.asc())
    )
    rows = (await session.execute(stmt)).all()

    # Deduplicate working models per provider label
    by_provider: dict[str, dict[str, Any]] = {}
    seen_combos: set[tuple[str, str]] = set()

    chat_options: list[dict[str, Any]] = []
    embed_options: list[dict[str, Any]] = [
        {
            "id": "local:fastembed",
            "provider": "Локально",
            "label": "FastEmbed BAAI/bge-m3 (Локально, 0 токенов, бесплатно)",
            "is_working": True,
            "badge": "FREE",
        }
    ]

    for model_row, key_row in rows:
        pname = _normalize_provider(key_row.provider_name)
        if pname == "custom":
            p_display = (
                (key_row.custom_name or "").strip()
                or _extract_host(key_row.base_url)
                or "Custom"
            )
        elif pname == "google":
            p_display = "Google Gemini"
        else:
            p_display = pname.capitalize()

        combo_key = (p_display, model_row.model_id)
        if combo_key in seen_combos:
            continue
        seen_combos.add(combo_key)

        if p_display not in by_provider:
            by_provider[p_display] = {
                "provider_name": pname,
                "display_name": p_display,
                "models": [],
            }

        is_embed = (
            "embed" in model_row.model_id.lower()
            or (model_row.model_id.endswith("-001") and "embedding" in model_row.model_id.lower())
        )
        by_provider[p_display]["models"].append(
            {
                "id": model_row.model_id,
                "db_id": model_row.id,
                "is_embed": is_embed,
                "last_tested_at": (
                    model_row.last_tested_at.strftime("%H:%M:%S")
                    if model_row.last_tested_at
                    else "недавно"
                ),
            }
        )

        item = {
            "id": model_row.model_id,
            "provider": p_display,
            "label": f"[{p_display}] {model_row.model_id}",
            "is_working": True,
        }
        if is_embed:
            embed_options.append(item)
        else:
            chat_options.append(item)

    # Sort chat options with preferred models on top
    preferred_order = [
        "grok-4.5",
        "mistral-large-2512",
        "deepseek-v4-flash",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "grok-4.7",
        "grok-4.3",
        "glm-4.7-flash",
        "gemini-flash-latest",
        "gemma-4-26b-a4b-it",
        "deepseek-chat",
        "gpt-4o-mini",
    ]

    def _score(opt: dict[str, Any]) -> int:
        mid = opt["id"].lower()
        for idx, pref in enumerate(preferred_order):
            if pref in mid:
                return idx
        return 999

    chat_options.sort(key=_score)

    # Check connected providers for image models (Fal AI, Google Gemini Imagen, Custom/OpenAI DALL-E)
    active_keys_stmt = select(AIProviderKey).where(AIProviderKey.is_active.is_(True))
    active_keys = list((await session.execute(active_keys_stmt)).scalars().all())

    has_fal = bool(settings.fal_key)
    has_google = False
    has_custom = False
    for k in active_keys:
        norm = _normalize_provider(k.provider_name)
        if norm in ("fal", "fal-ai"):
            has_fal = True
        elif norm == "google":
            has_google = True
        elif norm in ("openai", "custom"):
            has_custom = True

    image_options: list[dict[str, Any]] = []
    if has_fal:
        image_options.extend([
            {
                "id": "fal-ai/flux/schnell",
                "provider": "Fal AI",
                "label": "[Fal AI] Flux Schnell (фотореализм, ультрабыстро ~1 сек)",
                "is_working": True,
                "badge": "FLUX",
            },
            {
                "id": "fal-ai/flux/dev",
                "provider": "Fal AI",
                "label": "[Fal AI] Flux Dev (высокое качество, детализация)",
                "is_working": True,
                "badge": "FLUX",
            },
        ])

    if has_google:
        image_options.extend([
            {
                "id": "gemini-3.1-flash-image",
                "provider": "Google AI",
                "label": "[Google AI] Gemini 3.1 Flash Image (Nano Banana, требуется Billing)",
                "is_working": True,
                "badge": "GEMINI",
            },
            {
                "id": "gemini-2.5-flash-image",
                "provider": "Google AI",
                "label": "[Google AI] Gemini 2.5 Flash Image (Nano Banana, требуется Billing)",
                "is_working": True,
                "badge": "GEMINI",
            },
            {
                "id": "google/imagen-3.0-generate-002",
                "provider": "Google AI",
                "label": "[Google AI] Imagen 3 (Google Gemini, требуется Billing)",
                "is_working": True,
                "badge": "IMAGEN",
            },
        ])

    if has_custom and not any("dall-e-3" in opt["id"] for opt in image_options):
        image_options.append({
            "id": "dall-e-3",
            "provider": "OpenAI / Шлюз",
            "label": "[Шлюз / OpenAI] DALL-E 3 (генерация через /v1/images)",
            "is_working": True,
            "badge": "DALL-E",
        })

    # Check if any active key has a working image model (e.g. dall-e, imagen, flux)
    for model_row, key_row in rows:
        mid_low = model_row.model_id.lower()
        if any(img_word in mid_low for img_word in ("imagen", "dall-e", "flux")) or (mid_low.endswith("-image") and "gemini" in mid_low):
            pname = _normalize_provider(key_row.provider_name)
            p_display = (
                (key_row.custom_name or "").strip()
                or _extract_host(key_row.base_url)
                or pname.capitalize()
            )
            if not any(opt["id"] == model_row.model_id for opt in image_options):
                image_options.append({
                    "id": model_row.model_id,
                    "provider": p_display,
                    "label": f"[{p_display}] {model_row.model_id}",
                    "is_working": model_row.is_working,
                    "badge": "IMAGE",
                })

    # Always provide the option to disable AI image generation
    image_options.append({
        "id": "disabled",
        "provider": "Отключено",
        "label": "Отключено (только оригинальные медиа из Telegram)",
        "is_working": True,
        "badge": "OFF",
    })

    return {
        "by_provider": list(by_provider.values()),
        "total_working": len(seen_combos),
        "chat_options": chat_options,
        "embed_options": embed_options,
        "image_options": image_options,
    }


def _build_chain_for_role(
    primary_id: str,
    candidates: list[str],
    chat_options: list[dict[str, Any]],
    max_chain: int = 3,
) -> list[str]:
    """Build a multi-model fallback chain prioritizing diversity of providers.

    1. Primary model is first in the chain.
    2. Identifies the provider of the primary model.
    3. Finds working candidate models from DIFFERENT providers first.
    4. Fills remaining slots up to max_chain with other working candidates.
    """
    if not primary_id:
        return []

    model_to_provider: dict[str, str] = {
        opt["id"]: str(opt.get("provider") or "") for opt in chat_options
    }
    primary_provider = model_to_provider.get(primary_id, "")

    chain: list[str] = [primary_id]
    all_chat_ids = [opt["id"] for opt in chat_options]

    # First pass: try candidates from a DIFFERENT provider
    for cand in candidates:
        if len(chain) >= max_chain:
            break
        if cand in all_chat_ids and cand not in chain:
            cand_prov = model_to_provider.get(cand, "")
            if cand_prov and primary_provider and cand_prov != primary_provider:
                chain.append(cand)

    # Second pass: candidates from same provider if slots remain
    for cand in candidates:
        if len(chain) >= max_chain:
            break
        if cand in all_chat_ids and cand not in chain:
            chain.append(cand)

    # Third pass: any other working chat model if still under max_chain
    for opt_id in all_chat_ids:
        if len(chain) >= max_chain:
            break
        if opt_id not in chain:
            chain.append(opt_id)

    return chain


async def auto_select_best_models(session: AsyncSession) -> dict[str, str]:
    """Automatically select the optimal working models for all 5 pipeline roles based on connected active providers."""
    catalog = await get_working_models_catalog(session)
    chat_options = catalog.get("chat_options", [])
    chat_models = [opt["id"] for opt in chat_options]

    # Defaults if no working models yet
    default_text = chat_models[0] if chat_models else "gemini-3.5-flash"
    default_cheap = chat_models[0] if chat_models else "gemini-3.1-flash-lite"

    # Best arbiter: fast cheap model
    arbiter_candidates = [
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
        "claude-haiku-4-5-20251001",
        "grok-4.5",
        "gemma-4-26b-a4b-it",
    ]
    chosen_arbiter = next(
        (c for c in arbiter_candidates if c in chat_models),
        chat_models[0] if chat_models else default_cheap,
    )

    # Best writer: high quality editorial text model
    writer_candidates = [
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-opus-5",
        "gemini-3.7-flash",
        "gemini-2.5-flash",
        "grok-4.5",
        "gemma-4-26b-a4b-it",
    ]
    chosen_writer = next(
        (c for c in writer_candidates if c in chat_models),
        chat_models[0] if chat_models else default_text,
    )

    # Best SEO: fast reliable model
    seo_candidates = [
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
        "grok-4.5",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-5",
        "gemini-3.7-flash",
        "gemma-4-26b-a4b-it",
    ]
    chosen_seo = next(
        (c for c in seo_candidates if c in chat_models),
        chat_models[0] if chat_models else default_text,
    )

    # Best embedding: local fastembed first (0 cost, zero tokens)
    chosen_embed = "local:fastembed"

    # Best image: only choose an image model if connected and verified, otherwise disabled
    image_opts = catalog.get("image_options", [])
    valid_images = [opt["id"] for opt in image_opts if opt["id"] != "disabled"]
    image_candidates = [
        "fal-ai/flux/schnell",
        "fal-ai/flux/dev",
        "google/imagen-3.0-generate-002",
        "dall-e-3",
        "gemini-3.1-flash-image",
        "gemini-2.5-flash-image",
    ]
    chosen_image = next(
        (img for img in image_candidates if img in valid_images),
        valid_images[0] if valid_images else "disabled",
    )

    # Construct intelligent fallback chains prioritizing cross-provider resilience
    chains = {
        "arbiter": _build_chain_for_role(chosen_arbiter, arbiter_candidates, chat_options),
        "writer": _build_chain_for_role(chosen_writer, writer_candidates, chat_options),
        "seo": _build_chain_for_role(chosen_seo, seo_candidates, chat_options),
        "embedding": [chosen_embed],
        "image": [chosen_image],
    }
    try:
        await save_chains(session, chains)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not persist auto-selected chains: %s", exc)

    return {
        "arbiter": chosen_arbiter,
        "writer": chosen_writer,
        "seo": chosen_seo,
        "embedding": chosen_embed,
        "image": chosen_image,
    }


async def auto_recover_unhealthy_models(
    session: AsyncSession, limit: int = 10
) -> dict[str, Any]:
    """Re-test models that failed due to transient errors (429, 502, 503, timeout) to auto-recover them."""
    stmt = (
        select(AIProviderModel)
        .join(AIProviderKey, AIProviderModel.provider_key_id == AIProviderKey.id)
        .where(
            AIProviderKey.is_active.is_(True),
            AIProviderModel.is_working.is_(False),
            (
                AIProviderModel.error_message.ilike("%429%")
                | AIProviderModel.error_message.ilike("%502%")
                | AIProviderModel.error_message.ilike("%503%")
                | AIProviderModel.error_message.ilike("%сеть%")
                | AIProviderModel.error_message.ilike("%timeout%")
            ),
        )
        .order_by(AIProviderModel.last_tested_at.asc().nulls_first())
        .limit(limit)
    )
    models = list((await session.execute(stmt)).scalars().all())
    recovered = 0
    tested = 0
    for m in models:
        tested += 1
        res = await test_individual_model_by_id(session, m.id)
        if res.get("ok"):
            recovered += 1

    return {"tested": tested, "recovered": recovered}

