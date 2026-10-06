"""AI provider management: multi-key fallback and model discovery."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx

from app.config import settings

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "AIProvider",
    "ProviderError",
    "get_provider",
    "list_models",
]

REQUEST_TIMEOUT: Final[float] = 30.0
COOLDOWN_MINUTES: Final[int] = 5


class ProviderError(Exception):
    """AI provider call failed (network, auth, rate limit)."""


@dataclass(frozen=True, slots=True)
class AIProvider:
    """One AI provider with its API key and base URL."""

    name: str
    api_key: str
    base_url: str
    models: list[str]
    is_available: bool = True
    last_error: str | None = None
    cooldown_until: datetime | None = None

    def is_ready(self) -> bool:
        """Whether this provider can accept requests right now."""
        return self.is_available and not (
            self.cooldown_until is not None
            and datetime.now(UTC) < self.cooldown_until
        )

    def mark_failed(self, error: str) -> None:
        """Mark this provider as failed and set a cooldown."""
        object.__setattr__(self, "is_available", False)
        object.__setattr__(self, "last_error", error)
        object.__setattr__(
            self,
            "cooldown_until",
            datetime.now(UTC) + timedelta(minutes=COOLDOWN_MINUTES),
        )
        logger.warning(
            "AI provider %s marked failed: %s (cooldown %d min)",
            self.name,
            error,
            COOLDOWN_MINUTES,
        )

    def mark_success(self) -> None:
        """Mark this provider as available after a successful call."""
        object.__setattr__(self, "is_available", True)
        object.__setattr__(self, "last_error", None)
        object.__setattr__(self, "cooldown_until", None)


_providers: dict[str, AIProvider] = {}


def _parse_providers_from_env() -> dict[str, AIProvider]:
    """Parse AI providers from environment variables.

    Expected format:
    AI_PROVIDERS=openrouter,together,deepseek
    OPENROUTER_API_KEY=sk-or-...
    OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
    TOGETHER_API_KEY=...
    TOGETHER_BASE_URL=https://api.together.xyz/v1
    """
    provider_names = getattr(settings, "ai_providers", "")
    if not provider_names:
        return {}

    providers: dict[str, AIProvider] = {}
    for raw_name in provider_names.split(","):
        name = raw_name.strip().lower()
        if not name:
            continue

        api_key = getattr(settings, f"{name}_api_key", "")
        base_url = getattr(settings, f"{name}_base_url", "")

        if not api_key or not base_url:
            logger.warning("AI provider %s missing api_key or base_url", name)
            continue

        providers[name] = AIProvider(
            name=name,
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            models=[],
        )

    return providers


async def list_models(provider_name: str) -> list[str]:
    """Fetch available models from one provider's /v1/models endpoint.

    Returns the model list and updates the provider's models field.
    Raises :class:`ProviderError` on network failure or non-2xx response.
    """
    provider = _providers.get(provider_name)
    if provider is None:
        raise ProviderError(f"AI provider {provider_name} not configured")

    url = f"{provider.base_url}/models"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                provider.mark_failed(error_msg)
                raise ProviderError(f"Provider {provider_name} failed: {error_msg}")

            data = response.json()
            models = [model["id"] for model in data.get("data", []) if "id" in model]
            object.__setattr__(provider, "models", models)
            provider.mark_success()

            logger.info(
                "Provider %s returned %d model(s)",
                provider_name,
                len(models),
            )
            return models
        except httpx.RequestError as error:
            provider.mark_failed(str(error))
            raise ProviderError(
                f"Provider {provider_name} network error: {error}"
            ) from error


def get_provider(role: str) -> AIProvider:
    """Get the best available provider for a role.

    Tries providers in order of configuration until one is ready.
    Raises :class:`ProviderError` if no provider is available.
    """
    for provider in _providers.values():
        if provider.is_ready():
            return provider

    raise ProviderError(
        f"No AI provider available for role {role}; "
        f"all providers are failed or in cooldown"
    )


def init_providers() -> None:
    """Load providers from environment on application startup."""
    _providers.clear()
    _providers.update(_parse_providers_from_env())
    logger.info(
        "Initialized %d AI provider(s): %s",
        len(_providers),
        list(_providers.keys()),
    )
