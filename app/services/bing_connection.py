"""Bing Webmaster & IndexNow connection service.

Manages:
- Encrypted storage for Bing Webmaster API key and IndexNow key via Fernet
- Live connection diagnostics for Bing Webmaster API (GetUserSites)
- Live test and production submissions to IndexNow API (Bing, Yandex, Seznam, Naver)
- Non-blocking push on article publication
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import SecretBox, get_secret_box
from app.models.setting import SystemSetting

logger: Final[logging.Logger] = logging.getLogger(__name__)

BING_CONNECTION_KEY: Final[str] = "bing_services_connection"
DEFAULT_TEST_TIMEOUT: Final[float] = 10.0

INDEXNOW_ENDPOINT: Final[str] = "https://api.indexnow.org/IndexNow"
BING_WEBMASTER_API_BASE: Final[str] = "https://ssl.bing.com/webmaster/api.svc/json"

__all__ = [
    "BING_CONNECTION_KEY",
    "INDEXNOW_ENDPOINT",
    "BingConnectionConfig",
    "load_bing_connection",
    "probe_bing_webmaster",
    "probe_indexnow",
    "save_bing_connection",
    "submit_to_indexnow",
]


def _get_box() -> SecretBox | None:
    """Return SecretBox if FERNET_KEY is configured, else None."""
    try:
        return get_secret_box()
    except (ValueError, TypeError):
        return None


def _encrypt_val(value: str) -> str:
    """Encrypt a secret string using Fernet."""
    if not value:
        return ""
    box = _get_box()
    if box is not None:
        return box.encrypt(value)
    return value


def _decrypt_val(value: str) -> str:
    """Decrypt a secret string using Fernet."""
    if not value:
        return ""
    box = _get_box()
    if box is not None:
        return box.decrypt(value)
    return value


def _mask_secret(value: str) -> str:
    """Produce a safe display mask for secrets."""
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return f"••••••••{value[-4:]}"


def extract_host(url: str) -> str:
    """Extract hostname from URL (e.g. 'https://svodka.press/' -> 'svodka.press')."""
    if not url:
        return ""
    url_to_parse = url if "://" in url else f"https://{url}"
    parsed = urlparse(url_to_parse)
    return (parsed.hostname or "").lower()


@dataclass(slots=True)
class BingConnectionConfig:
    """Configuration parameters for Bing Webmaster Tools and IndexNow."""

    site_url: str = ""
    api_key: str = ""
    indexnow_key: str = ""
    indexnow_key_location: str = ""

    is_active: bool = True
    is_indexnow_active: bool = True

    from_database: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_api_key(self) -> bool:
        """Whether Bing Webmaster API key is configured."""
        return bool(self.api_key.strip())

    @property
    def has_indexnow_key(self) -> bool:
        """Whether IndexNow key is configured."""
        return bool(self.indexnow_key.strip())

    @property
    def has_site_url(self) -> bool:
        """Whether site URL is configured."""
        return bool(self.site_url.strip())

    @property
    def host(self) -> str:
        """Normalized hostname of the configured site."""
        return extract_host(self.site_url)

    def to_encrypted_document(self) -> dict[str, Any]:
        """Serialize configuration for database storage with Fernet encryption."""
        return {
            "site_url": self.site_url.strip(),
            "api_key_enc": _encrypt_val(self.api_key.strip()),
            "indexnow_key_enc": _encrypt_val(self.indexnow_key.strip()),
            "indexnow_key_location": self.indexnow_key_location.strip(),
            "is_active": bool(self.is_active),
            "is_indexnow_active": bool(self.is_indexnow_active),
            "extra": self.extra,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Render safe masked configuration for UI display."""
        return {
            "site_url": self.site_url,
            "host": self.host,
            "has_api_key": self.has_api_key,
            "api_key_masked": _mask_secret(self.api_key),
            "has_indexnow_key": self.has_indexnow_key,
            "indexnow_key_masked": _mask_secret(self.indexnow_key),
            "indexnow_key_location": self.indexnow_key_location,
            "is_active": self.is_active,
            "is_indexnow_active": self.is_indexnow_active,
            "from_database": self.from_database,
        }


async def load_bing_connection(session: AsyncSession) -> BingConnectionConfig:
    """Load Bing & IndexNow configuration from database or fallback to .env settings."""
    try:
        stmt = select(SystemSetting.value).where(SystemSetting.key == BING_CONNECTION_KEY)
        result = await session.execute(stmt)
        raw_doc = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning("Could not read bing connection from database: %s", exc)
        raw_doc = None

    if isinstance(raw_doc, dict):
        api_key = _decrypt_val(str(raw_doc.get("api_key_enc", "")))
        indexnow_key = _decrypt_val(str(raw_doc.get("indexnow_key_enc", "")))

        return BingConnectionConfig(
            site_url=str(raw_doc.get("site_url", "")),
            api_key=api_key,
            indexnow_key=indexnow_key,
            indexnow_key_location=str(raw_doc.get("indexnow_key_location", "")),
            is_active=bool(raw_doc.get("is_active", True)),
            is_indexnow_active=bool(raw_doc.get("is_indexnow_active", True)),
            from_database=True,
            extra=dict(raw_doc.get("extra", {})),
        )

    # Fallback to config / settings
    fallback_site = getattr(settings, "bing_site_url", "") or getattr(
        settings, "wp_base_url", ""
    )
    return BingConnectionConfig(
        site_url=fallback_site,
        api_key=getattr(settings, "bing_api_key", ""),
        indexnow_key=getattr(settings, "indexnow_key", ""),
        indexnow_key_location=getattr(settings, "indexnow_key_location", ""),
        is_active=True,
        is_indexnow_active=True,
        from_database=False,
    )


async def save_bing_connection(
    session: AsyncSession, config: BingConnectionConfig
) -> BingConnectionConfig:
    """Save Bing & IndexNow configuration to database with Fernet encryption."""
    doc = config.to_encrypted_document()
    stmt = (
        pg_insert(SystemSetting)
        .values(
            key=BING_CONNECTION_KEY,
            value=doc,
            description="Параметры подключения к Bing Webmaster API и IndexNow",
        )
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": doc},
        )
    )
    await session.execute(stmt)
    await session.commit()
    config.from_database = True
    return config


async def probe_bing_webmaster(config: BingConnectionConfig) -> dict[str, Any]:
    """Test connection to Bing Webmaster Tools API."""
    if not config.has_api_key:
        return {
            "ok": False,
            "service": "Bing Webmaster",
            "message": "Укажите API-ключ Bing Webmaster (32 символа).",
        }

    api_key = config.api_key.strip()
    url = f"{BING_WEBMASTER_API_BASE}/GetUserSites?apikey={api_key}"

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            resp = await client.get(url)

            if resp.status_code == 200:
                data = resp.json()
                sites = data.get("d", [])
                if not isinstance(sites, list):
                    sites = []

                site_urls = [
                    str(s.get("Url", "")).rstrip("/")
                    for s in sites
                    if isinstance(s, dict)
                ]
                target_host = config.host

                # Check if target site is in list
                matched = any(target_host and target_host in u for u in site_urls)

                if matched:
                    return {
                        "ok": True,
                        "service": "Bing Webmaster",
                        "message": (
                            f"Связь с Bing Webmaster установлена! "
                            f"Сайт '{config.site_url}' подтверждён."
                        ),
                        "details": (
                            f"Всего подтверждённых сайтов в аккаунте: {len(sites)}."
                        ),
                    }
                elif sites:
                    sample = ", ".join(site_urls[:3])
                    return {
                        "ok": True,
                        "service": "Bing Webmaster",
                        "message": (
                            "API-ключ Bing валиден, но указанный сайт не найден "
                            "в списке ваших сайтов."
                        ),
                        "details": f"Сайты в вашем кабинете Bing: {sample}.",
                    }
                else:
                    return {
                        "ok": True,
                        "service": "Bing Webmaster",
                        "message": (
                            "API-ключ Bing валиден! (Список сайтов в аккаунте пока пуст)."
                        ),
                    }

            if resp.status_code in {400, 401, 403}:
                return {
                    "ok": False,
                    "service": "Bing Webmaster",
                    "message": (
                        f"Ошибка авторизации ({resp.status_code}): "
                        "неверный API-ключ Bing."
                    ),
                }

            return {
                "ok": False,
                "service": "Bing Webmaster",
                "message": (
                    f"Bing API вернул HTTP {resp.status_code}: {resp.text[:180]}"
                ),
            }

    except Exception as exc:
        return {
            "ok": False,
            "service": "Bing Webmaster",
            "message": f"Ошибка проверки Bing Webmaster: {exc}",
        }


async def probe_indexnow(
    config: BingConnectionConfig, test_url: str | None = None
) -> dict[str, Any]:
    """Test connection and URL submission to IndexNow API."""
    if not config.has_indexnow_key:
        return {
            "ok": False,
            "service": "IndexNow",
            "message": (
                "Укажите ключ IndexNow (например, сгенерированный в кабинете Bing)."
            ),
        }

    host = config.host
    if not host:
        return {
            "ok": False,
            "service": "IndexNow",
            "message": "Укажите URL сайта (например, https://svodka.press/).",
        }

    url_to_submit = test_url or (config.site_url.rstrip("/") + "/")
    payload: dict[str, Any] = {
        "host": host,
        "key": config.indexnow_key.strip(),
        "urlList": [url_to_submit],
    }
    if config.indexnow_key_location.strip():
        payload["keyLocation"] = config.indexnow_key_location.strip()

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            resp = await client.post(
                INDEXNOW_ENDPOINT,
                json=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )

            # IndexNow status codes:
            # 200: OK (URL submitted)
            # 202: Accepted (key pending verification)
            if resp.status_code in {200, 202}:
                status_text = (
                    "принят успешно"
                    if resp.status_code == 200
                    else "принят (ожидает проверки ключа)"
                )
                return {
                    "ok": True,
                    "service": "IndexNow",
                    "message": (
                        f"IndexNow активен! Тестовый URL {status_text} "
                        f"(HTTP {resp.status_code})."
                    ),
                    "details": "Поисковые системы Bing, Yandex, Seznam уведомлены.",
                }

            if resp.status_code == 400:
                return {
                    "ok": False,
                    "service": "IndexNow",
                    "message": "Ошибка 400: неверный формат запроса к IndexNow.",
                }

            if resp.status_code == 403:
                return {
                    "ok": False,
                    "service": "IndexNow",
                    "message": (
                        "Ошибка 403 (Forbidden): ключ недействителен или файл ключа "
                        f"не найден в корне сайта ({host}/{config.indexnow_key}.txt)."
                    ),
                }

            if resp.status_code == 422:
                return {
                    "ok": False,
                    "service": "IndexNow",
                    "message": (
                        f"Ошибка 422 (Unprocessable): URL {url_to_submit} "
                        f"не принадлежит хосту {host}."
                    ),
                }

            return {
                "ok": False,
                "service": "IndexNow",
                "message": (
                    f"IndexNow API вернул HTTP {resp.status_code}: {resp.text[:180]}"
                ),
            }

    except Exception as exc:
        return {
            "ok": False,
            "service": "IndexNow",
            "message": f"Ошибка отправки в IndexNow: {exc}",
        }


async def submit_to_indexnow(
    urls: list[str], config: BingConnectionConfig
) -> dict[str, Any]:
    """Submit a list of published article URLs to IndexNow protocol.

    Designed for non-blocking invocation in article publication pipeline.
    Errors are logged but never raised to prevent failing the publishing flow.
    """
    if not config.is_indexnow_active or not config.has_indexnow_key:
        return {"ok": False, "reason": "IndexNow is inactive or key is missing"}

    clean_urls = [u.strip() for u in urls if u and u.strip().startswith("http")]
    if not clean_urls:
        return {"ok": False, "reason": "No valid URLs to submit"}

    host = config.host or extract_host(clean_urls[0])
    if not host:
        return {"ok": False, "reason": "Could not determine host"}

    payload: dict[str, Any] = {
        "host": host,
        "key": config.indexnow_key.strip(),
        "urlList": clean_urls,
    }
    if config.indexnow_key_location.strip():
        payload["keyLocation"] = config.indexnow_key_location.strip()

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            resp = await client.post(
                INDEXNOW_ENDPOINT,
                json=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            if resp.status_code in {200, 202}:
                logger.info(
                    "IndexNow submitted %d URLs for host %s (status %d)",
                    len(clean_urls),
                    host,
                    resp.status_code,
                )
                return {
                    "ok": True,
                    "count": len(clean_urls),
                    "status_code": resp.status_code,
                }

            logger.warning(
                "IndexNow submission returned HTTP %d: %s",
                resp.status_code,
                resp.text[:200],
            )
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error": resp.text[:200],
            }

    except Exception as exc:
        logger.error("Failed to submit URLs to IndexNow: %s", exc)
        return {"ok": False, "error": str(exc)}
