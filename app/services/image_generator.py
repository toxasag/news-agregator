# ruff: noqa: PLR0911, PLR0912, PLR0915, E501
"""Unified AI cover image generator: supports Fal AI, Google, and OpenAI/Custom.

Selects the generator implementation based on the active 'image' role in `system_settings`
or fallback setting `settings.ai_model_image`.
"""

from __future__ import annotations

import base64
import contextlib
import logging
from typing import Any, Final

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ai_provider_key import AIProviderKey
from app.services.flux_generator import generate_flux_image
from app.services.gemini_key_pool import (
    GEMINI_BASE_URL,
    _decrypt_key,
    _normalize_provider,
)
from app.services.model_routing import resolve_model

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "ImageGenerationDisabledError",
    "ImageGenerationError",
    "ImageQuotaExceededError",
    "generate_cover_image",
]


class ImageGenerationError(Exception):
    """Base error when generating an AI image."""


class ImageGenerationDisabledError(ImageGenerationError):
    """Raised when image generation is disabled in settings."""


class ImageQuotaExceededError(ImageGenerationError):
    """Raised when provider returns HTTP 429 quota or requires paid billing."""


async def _get_all_keys_for_provider(
    session: AsyncSession, provider_name: str
) -> list[tuple[str, str | None]]:
    """Return list of decrypted (api_key, base_url) tuples for an active provider."""
    stmt = (
        select(AIProviderKey)
        .where(AIProviderKey.is_active.is_(True))
        .order_by(AIProviderKey.id.asc())
    )
    keys = list((await session.execute(stmt)).scalars().all())
    result: list[tuple[str, str | None]] = []
    for k in keys:
        norm = _normalize_provider(k.provider_name)
        if norm == provider_name or (
            provider_name == "custom" and norm in ("custom", "openai")
        ):
            raw_key = _decrypt_key(k.api_key_encrypted)
            if raw_key:
                result.append((raw_key, k.base_url))
    return result


async def _get_active_key_for_provider(
    session: AsyncSession, provider_name: str
) -> tuple[str, str | None] | None:
    """Return decrypted api key and optional base_url for an active provider."""
    all_keys = await _get_all_keys_for_provider(session, provider_name)
    return all_keys[0] if all_keys else None



async def _generate_gemini_native_image(
    raw_key: str, prompt: str, target_model: str
) -> bytes:
    """Generate image via Gemini Native Image (Interactions API / Nano Banana)."""
    url = f"{GEMINI_BASE_URL}/interactions"
    headers = {
        "x-goog-api-key": raw_key,
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": target_model,
        "input": [{"type": "text", "text": prompt}],
        "response_format": {
            "type": "image",
            "mime_type": "image/jpeg",
            "aspect_ratio": "16:9",
            "image_size": "1K",
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            res = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise ImageGenerationError(
                f"Сетевая ошибка обращения к Gemini Image API: {exc}"
            ) from exc

        if res.status_code == 200:
            data = res.json()
            out_img = data.get("output_image")
            if isinstance(out_img, dict) and out_img.get("data"):
                return base64.b64decode(out_img["data"])

            for step in data.get("steps", []):
                if step.get("type") == "model_output":
                    for blk in step.get("content", []):
                        if blk.get("type") == "image" and blk.get("data"):
                            return base64.b64decode(blk["data"])

            raise ImageGenerationError("Gemini Image API вернул ответ без картинки.")

        if res.status_code == 429:
            raise ImageQuotaExceededError(
                f"Google AI Studio требует подключённый биллинг (Billing) "
                f"для модели {target_model} (0 картинок/день на Free Tier)."
            )

        raise ImageGenerationError(
            f"Gemini Image API вернул HTTP {res.status_code}: {res.text[:200]}"
        )


async def _generate_google_imagen_legacy(
    raw_key: str, prompt: str, clean_model: str
) -> bytes:
    """Generate image using Google AI Studio legacy Imagen 3 predict API."""
    target_model = (
        clean_model
        if clean_model and clean_model != "imagen-3"
        else "imagen-3.0-generate-002"
    )
    url = f"{GEMINI_BASE_URL}/models/{target_model}:predict"
    headers = {
        "x-goog-api-key": raw_key,
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "instances": [{"prompt": prompt}],
        "parameters": {
            "sampleCount": 1,
            "aspectRatio": "16:9",
            "outputMimeType": "image/jpeg",
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            res = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise ImageGenerationError(
                f"Сетевая ошибка обращения к Google Imagen: {exc}"
            ) from exc

        if res.status_code == 200:
            data = res.json()
            predictions = data.get("predictions", [])
            if not predictions or not isinstance(predictions, list):
                raise ImageGenerationError("Google Imagen вернул пустой список картинок.")
            b64_bytes = predictions[0].get("bytesBase64Encoded")
            if not b64_bytes:
                raise ImageGenerationError("Google Imagen не вернул bytesBase64Encoded.")
            return base64.b64decode(b64_bytes)

        if res.status_code == 429:
            raise ImageQuotaExceededError(
                "Google AI Studio требует подключённый биллинг (Billing) "
                "для генерации Imagen 3 (HTTP 429: 0 requests per day on Free Tier)."
            )

        raise ImageGenerationError(
            f"Google Imagen API вернул HTTP {res.status_code}: {res.text[:200]}"
        )


async def _generate_google_image(
    session: AsyncSession, prompt: str, model_id: str
) -> bytes:
    """Generate image using Google AI Studio with automatic key rotation on 429."""
    all_keys = await _get_all_keys_for_provider(session, "google")
    if not all_keys:
        raise ImageGenerationError("Не найден активный ключ Google Gemini в пуле ключей.")

    clean_model = model_id.removeprefix("google/")
    clean_model_lower = clean_model.lower()
    is_gemini = (
        "gemini" in clean_model_lower
        or clean_model_lower.endswith("-image")
        or "banana" in clean_model_lower
    )
    target = clean_model or "gemini-3.1-flash-image"

    last_error: Exception | None = None
    for idx, (raw_key, _) in enumerate(all_keys):
        try:
            if is_gemini:
                return await _generate_gemini_native_image(raw_key, prompt, target)
            return await _generate_google_imagen_legacy(raw_key, prompt, clean_model)
        except ImageQuotaExceededError as exc:
            last_error = exc
            logger.warning(
                "Google Image key %d/%d exhausted (429), trying next key...",
                idx + 1,
                len(all_keys),
            )
            continue
        except ImageGenerationError as exc:
            last_error = exc
            logger.warning(
                "Google Image key %d/%d failed: %s, trying next key...",
                idx + 1,
                len(all_keys),
                exc,
            )
            continue

    if last_error:
        raise last_error
    raise ImageGenerationError("Все ключи Google Gemini исчерпали квоту генерации изображений.")



async def _generate_openai_dalle(
    session: AsyncSession, prompt: str, model_id: str
) -> bytes:
    """Generate image using OpenAI-compatible /v1/images/generations endpoint with key rotation."""
    all_keys = await _get_all_keys_for_provider(session, "custom")
    if not all_keys:
        raise ImageGenerationError("Не найден активный ключ OpenAI/шлюза в пуле ключей.")

    clean_model = model_id.removeprefix("openai/").removeprefix("custom/")
    payload = {
        "model": clean_model or "dall-e-3",
        "prompt": prompt,
        "n": 1,
        "size": "1792x1024",
        "response_format": "b64_json",
    }

    last_error: Exception | None = None
    for idx, (raw_key, base_url) in enumerate(all_keys):
        raw_base = (base_url or "").strip() or "https://api.openai.com/v1"
        clean_base = (
            raw_base
            if raw_base.endswith("/v1") or "/v1/" in raw_base
            else f"{raw_base}/v1"
        )
        url = f"{clean_base}/images/generations"
        headers = {
            "Authorization": f"Bearer {raw_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                res = await client.post(url, headers=headers, json=payload)
            except httpx.RequestError as exc:
                last_error = ImageGenerationError(f"Сетевая ошибка обращения к DALL-E ({raw_base}): {exc}")
                logger.warning("Custom gateway %d/%d (%s) failed: %s, trying next...", idx + 1, len(all_keys), raw_base, exc)
                continue

            if res.status_code == 200:
                data = res.json()
                images_data = data.get("data", [])
                if not images_data or not isinstance(images_data, list):
                    last_error = ImageGenerationError(f"DALL-E ({raw_base}) вернул пустой список изображений.")
                    continue
                img_obj = images_data[0]
                if "b64_json" in img_obj:
                    return base64.b64decode(img_obj["b64_json"])
                if "url" in img_obj:
                    img_res = await client.get(img_obj["url"])
                    if img_res.status_code == 200:
                        return img_res.content
                    last_error = ImageGenerationError(
                        f"Не удалось скачать сгенерированную картинку: HTTP {img_res.status_code}"
                    )
                    continue
                last_error = ImageGenerationError("DALL-E не вернул ни b64_json, ни url изображения.")
                continue

            err_msg = f"DALL-E ({raw_base}) HTTP {res.status_code}: {res.text[:150]}"
            logger.warning("Custom gateway %d/%d error: %s, trying next...", idx + 1, len(all_keys), err_msg)
            if res.status_code == 429:
                last_error = ImageQuotaExceededError(err_msg)
            else:
                last_error = ImageGenerationError(err_msg)
            continue

    if last_error:
        raise last_error
    raise ImageGenerationError("Все шлюзы Custom/DALL-E исчерпали квоту генерации изображений.")



_TRANSLIT_TABLE = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


_SENSITIVE_CONCEPT_MAP = {
    "смертная казнь": "courtroom gavel justice legal trial",
    "казнь": "courtroom judge gavel legal trial",
    "поджог": "forest wildfire smoke emergency rescue",
    "пожар": "smoke emergency response firefighters",
    "убийств": "police investigation scene tape sirens",
    "атак": "tactical emergency response vehicles",
    "взрыв": "industrial smoke incident rescue",
    "путин": "official press conference microphones podium",
    "зеленск": "official press briefing podium room",
    "трамп": "presidential press conference microphones",
    "байден": "press room official briefing microphones",
    "войн": "military equipment defense operations",
    "теракт": "emergency services flashing lights sirens",
}


def _transliterate_prompt(text: str) -> str:
    """Convert cyrillic characters to latin and clean symbols for free AI image APIs."""
    lowered = text.lower()
    for trigger, concept in _SENSITIVE_CONCEPT_MAP.items():
        if trigger in lowered:
            lowered = lowered.replace(trigger, concept)

    result = []
    for char in lowered:
        result.append(_TRANSLIT_TABLE.get(char, char))
    clean = "".join(result)
    import re  # noqa: PLC0415
    clean = re.sub(r"[^\w\s,.-]", " ", clean)
    return " ".join(clean.split())[:250]





async def _generate_pollinations_image(prompt: str) -> bytes | None:
    """Generate editorial cover via Pollinations (free, fast, no keys needed)."""
    import random  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415

    for attempt_prompt in [
        _transliterate_prompt(prompt),
        "editorial news photo documentary journalism dramatic realistic 8k",
    ]:
        encoded = urllib.parse.quote(attempt_prompt)
        seed = random.randint(1000, 999999)  # noqa: S311
        url = (
            f"https://image.pollinations.ai/prompt/{encoded}"
            f"?width=1024&height=768&seed={seed}&nologo=true"
        )
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and len(resp.content) > 3000:
                    logger.info("Cover generated via Pollinations (%d bytes)", len(resp.content))
                    return resp.content
                logger.warning(
                    "Pollinations attempt '%s' returned HTTP %d, len=%d",
                    attempt_prompt[:40],
                    resp.status_code,
                    len(resp.content),
                )
                if resp.status_code == 402:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pollinations image generation failed: %s", exc)
    return None


async def generate_cover_image(
    session: AsyncSession,
    prompt: str,
    *,
    model_override: str | None = None,
) -> bytes | None:
    """Generate an editorial cover image based on configured AI model routing.

    Returns:
        Image bytes (JPEG/PNG) or None if image generation is disabled.
    """
    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise ImageGenerationError("Промт для генерации изображения пуст.")

    active_model = model_override
    if not active_model:
        active_model = await resolve_model(session, "image")
    if not active_model:
        active_model = getattr(settings, "ai_model_image", "disabled")

    active_model_lower = active_model.lower().strip()
    if active_model_lower == "disabled":
        logger.info("Cover generation disabled by model routing ('disabled')")
        return None

    # Pollinations direct routing
    if "pollinations" in active_model_lower or "flux-free" in active_model_lower or "turbo" in active_model_lower:
        logger.info("Generating cover via Pollinations (%s)...", active_model)
        poll_img = await _generate_pollinations_image(clean_prompt)
        if poll_img:
            return poll_img
        raise ImageGenerationError("Pollinations image generation service failed.")

    # Google Imagen & Gemini Native Image (Nano Banana)
    if (
        "imagen" in active_model_lower
        or active_model_lower.startswith("google/")
        or active_model_lower.endswith("-image")
        or "banana" in active_model_lower
    ):
        logger.info("Generating cover via Google (%s)...", active_model)
        try:
            return await _generate_google_image(session, clean_prompt, active_model)
        except (ImageQuotaExceededError, ImageGenerationError) as exc:
            logger.warning(
                "Google Image generation failed (%s), attempting fallbacks...",
                exc,
            )
            try:
                return await _generate_openai_dalle(session, clean_prompt, "dall-e-3")
            except Exception as fb_exc:  # noqa: BLE001
                logger.warning("Custom/DALL-E fallback failed (%s)", fb_exc)
                if settings.fal_key:
                    logger.info("Attempting Fal AI fallback...")
                    with contextlib.suppress(Exception):
                        return await generate_flux_image(clean_prompt)

            # Robust zero-key fallback to Pollinations FLUX
            logger.info("Attempting Pollinations FLUX fallback...")
            try:
                poll_img = await _generate_pollinations_image(clean_prompt)
                if poll_img:
                    return poll_img
            except Exception as p_exc:  # noqa: BLE001
                logger.warning("Pollinations fallback failed: %s", p_exc)
            raise

    # DALL-E / OpenAI Custom
    if "dall-e" in active_model_lower or active_model_lower.startswith("openai/"):
        logger.info("Generating cover via DALL-E (%s)...", active_model)
        try:
            return await _generate_openai_dalle(session, clean_prompt, active_model)
        except (ImageQuotaExceededError, ImageGenerationError) as exc:
            logger.warning("DALL-E generation failed (%s), trying fallbacks...", exc)
            if settings.fal_key:
                with contextlib.suppress(Exception):
                    return await generate_flux_image(clean_prompt)
            poll_img = await _generate_pollinations_image(clean_prompt)
            if poll_img:
                return poll_img
            raise

    # Fal AI Flux
    if "flux" in active_model_lower or active_model_lower.startswith("fal-ai/"):
        logger.info("Generating cover via Fal AI Flux (%s)...", active_model)
        try:
            return await generate_flux_image(clean_prompt, model=active_model)
        except Exception as exc:
            logger.warning("Fal AI failed (%s), falling back to Pollinations FLUX...", exc)
            poll_img = await _generate_pollinations_image(clean_prompt)
            if poll_img:
                return poll_img
            raise

    # Default fallback: try Custom/OpenAI first if configured, else Google, else Pollinations
    custom_keys = await _get_all_keys_for_provider(session, "custom")
    if custom_keys:
        try:
            return await _generate_openai_dalle(session, clean_prompt, "dall-e-3")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Default Custom DALL-E failed (%s), falling back...", exc)

    if settings.fal_key:
        with contextlib.suppress(Exception):
            return await generate_flux_image(clean_prompt, model=active_model)

    try:
        return await _generate_google_image(session, clean_prompt, active_model)
    except Exception as exc:
        logger.warning("Google image failed (%s), falling back to Pollinations FLUX...", exc)
        poll_img = await _generate_pollinations_image(clean_prompt)
        if poll_img:
            return poll_img
        raise

