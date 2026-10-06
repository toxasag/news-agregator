# ruff: noqa: E501
"""Yandex services connection management: Wordstat, Metrika, and Webmaster.

Provides:
- Encrypted storage for Yandex OAuth tokens and API keys via Fernet
- Dynamic loading from database with .env fallback
- Live diagnostics and connection testing for Wordstat, Metrika, and Webmaster APIs
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import SecretBox, get_secret_box
from app.models.setting import SystemSetting

logger: Final[logging.Logger] = logging.getLogger(__name__)

YANDEX_CONNECTION_KEY: Final[str] = "yandex_services_connection"
DEFAULT_TEST_TIMEOUT: Final[float] = 10.0

__all__ = [
    "YANDEX_CONNECTION_KEY",
    "YandexConnectionConfig",
    "add_yandex_sitemap",
    "fetch_yandex_diagnostics",
    "fetch_yandex_sitemaps",
    "get_yandex_recrawl_quota",
    "load_yandex_connection",
    "probe_yandex_metrika",
    "probe_yandex_metrika_dzen",
    "probe_yandex_webmaster",
    "probe_yandex_wordstat",
    "recrawl_yandex_url",
    "save_yandex_connection",
]


def _get_box() -> SecretBox | None:
    """Return SecretBox if FERNET_KEY is configured, else None."""
    try:
        return get_secret_box()
    except (ValueError, TypeError):
        return None


def _encrypt_val(value: str) -> str:
    """Encrypt a secret value if SecretBox is available."""
    if not value:
        return ""
    box = _get_box()
    if box is not None:
        return box.encrypt(value)
    return value


def _decrypt_val(value: str) -> str:
    """Decrypt a secret value if SecretBox is available."""
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
    if len(value) <= 6:
        return "••••••"
    return f"••••••••{value[-4:]}"


@dataclass(slots=True)
class YandexConnectionConfig:
    """Configuration parameters for Yandex Wordstat, Metrika, and Webmaster."""

    # Wordstat API
    wordstat_token: str = ""
    wordstat_api_key: str = ""
    wordstat_folder_id: str = ""

    # Metrika API
    metrika_counter_id: str = ""
    metrika_dzen_counter_id: str = ""
    metrika_token: str = ""

    # Webmaster API
    webmaster_host_id: str = ""
    webmaster_token: str = ""

    # Toggles
    is_wordstat_active: bool = True
    is_metrika_active: bool = True
    is_webmaster_active: bool = True

    from_database: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_wordstat_credentials(self) -> bool:
        """Whether Wordstat credentials (token or API key) are configured."""
        return bool(self.wordstat_token.strip() or self.wordstat_api_key.strip())

    @property
    def has_metrika_credentials(self) -> bool:
        """Whether Metrika site counter and token are configured."""
        return bool(self.metrika_counter_id.strip() and self.metrika_token.strip())

    @property
    def has_metrika(self) -> bool:
        """Alias for has_metrika_credentials."""
        return self.has_metrika_credentials

    @property
    def metrika_oauth_token(self) -> str:
        """Alias for metrika_token."""
        return self.metrika_token

    @property
    def has_metrika_dzen(self) -> bool:
        """Whether Metrika Dzen counter and token are configured."""
        return bool(self.metrika_dzen_counter_id.strip() and self.metrika_token.strip())

    @property
    def has_webmaster_credentials(self) -> bool:
        """Whether Webmaster token is configured."""
        return bool(self.webmaster_token.strip())

    def to_encrypted_document(self) -> dict[str, Any]:
        """Serialize configuration for database storage with Fernet encryption."""
        return {
            "wordstat_token_enc": _encrypt_val(self.wordstat_token.strip()),
            "wordstat_api_key_enc": _encrypt_val(self.wordstat_api_key.strip()),
            "wordstat_folder_id": self.wordstat_folder_id.strip(),
            "metrika_counter_id": self.metrika_counter_id.strip(),
            "metrika_dzen_counter_id": self.metrika_dzen_counter_id.strip(),
            "metrika_token_enc": _encrypt_val(self.metrika_token.strip()),
            "webmaster_host_id": self.webmaster_host_id.strip(),
            "webmaster_token_enc": _encrypt_val(self.webmaster_token.strip()),
            "is_wordstat_active": bool(self.is_wordstat_active),
            "is_metrika_active": bool(self.is_metrika_active),
            "is_webmaster_active": bool(self.is_webmaster_active),
            "extra": self.extra,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Render safe masked configuration for UI display."""
        return {
            "wordstat_token_masked": _mask_secret(self.wordstat_token),
            "has_wordstat_token": bool(self.wordstat_token),
            "wordstat_api_key_masked": _mask_secret(self.wordstat_api_key),
            "has_wordstat_api_key": bool(self.wordstat_api_key),
            "wordstat_folder_id": self.wordstat_folder_id,
            "has_wordstat": self.has_wordstat_credentials,
            "metrika_counter_id": self.metrika_counter_id,
            "metrika_dzen_counter_id": self.metrika_dzen_counter_id,
            "metrika_token_masked": _mask_secret(self.metrika_token),
            "has_metrika_token": bool(self.metrika_token),
            "has_metrika": self.has_metrika_credentials,
            "has_metrika_dzen": self.has_metrika_dzen,
            "webmaster_host_id": self.webmaster_host_id,
            "webmaster_token_masked": _mask_secret(self.webmaster_token),
            "has_webmaster_token": bool(self.webmaster_token),
            "has_webmaster": self.has_webmaster_credentials,
            "is_wordstat_active": self.is_wordstat_active,
            "is_metrika_active": self.is_metrika_active,
            "is_webmaster_active": self.is_webmaster_active,
            "from_database": self.from_database,
        }


async def load_yandex_connection(session: AsyncSession) -> YandexConnectionConfig:
    """Load Yandex configuration from database or fallback to .env settings."""
    try:
        stmt = select(SystemSetting.value).where(
            SystemSetting.key == YANDEX_CONNECTION_KEY
        )
        result = await session.execute(stmt)
        stored = result.scalar_one_or_none()
    except SQLAlchemyError:
        logger.warning(
            "Could not read yandex_services_connection setting from database",
            exc_info=True,
        )
        stored = None

    if isinstance(stored, dict):
        return YandexConnectionConfig(
            wordstat_token=_decrypt_val(str(stored.get("wordstat_token_enc") or "")),
            wordstat_api_key=_decrypt_val(str(stored.get("wordstat_api_key_enc") or "")),
            wordstat_folder_id=str(stored.get("wordstat_folder_id") or "").strip(),
            metrika_counter_id=str(stored.get("metrika_counter_id") or "").strip(),
            metrika_dzen_counter_id=str(
                stored.get("metrika_dzen_counter_id") or ""
            ).strip(),
            metrika_token=_decrypt_val(str(stored.get("metrika_token_enc") or "")),
            webmaster_host_id=str(stored.get("webmaster_host_id") or "").strip(),
            webmaster_token=_decrypt_val(str(stored.get("webmaster_token_enc") or "")),
            is_wordstat_active=bool(stored.get("is_wordstat_active", True)),
            is_metrika_active=bool(stored.get("is_metrika_active", True)),
            is_webmaster_active=bool(stored.get("is_webmaster_active", True)),
            from_database=True,
            extra=dict(stored.get("extra") or {}),
        )

    # Fallback to environment variables
    return YandexConnectionConfig(
        wordstat_token=settings.yandex_wordstat_token.strip(),
        wordstat_api_key=settings.yandex_wordstat_api_key.strip(),
        wordstat_folder_id=settings.yandex_wordstat_folder_id.strip(),
        metrika_counter_id=settings.yandex_metrika_counter_id.strip(),
        metrika_dzen_counter_id=getattr(
            settings, "yandex_metrika_dzen_counter_id", ""
        ).strip(),
        metrika_token=settings.yandex_metrika_token.strip(),
        webmaster_host_id="",
        webmaster_token=settings.yandex_metrika_token.strip(),  # Often shared OAuth
        is_wordstat_active=bool(
            settings.yandex_wordstat_token or settings.yandex_wordstat_api_key
        ),
        is_metrika_active=bool(
            settings.yandex_metrika_counter_id and settings.yandex_metrika_token
        ),
        is_webmaster_active=bool(settings.yandex_metrika_token),
        from_database=False,
    )


async def save_yandex_connection(
    session: AsyncSession, config: YandexConnectionConfig
) -> YandexConnectionConfig:
    """Save Yandex configuration to database with Fernet encryption."""
    doc = config.to_encrypted_document()
    stmt = (
        pg_insert(SystemSetting)
        .values(
            key=YANDEX_CONNECTION_KEY,
            value=doc,
            description="Параметры подключения к API Яндекс (Wordstat, Метрика)",
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


async def probe_yandex_wordstat(config: YandexConnectionConfig) -> dict[str, Any]:
    """Test Yandex Wordstat API connection."""
    if not config.has_wordstat_credentials:
        return {
            "ok": False,
            "service": "Wordstat",
            "message": (
                "Токен или API-ключ не указаны. При генерации будет "
                "использоваться ИИ-эмуляция поисковых запросов."
            ),
        }

    token = config.wordstat_token.strip()
    api_key = config.wordstat_api_key.strip()
    folder_id = config.wordstat_folder_id.strip()

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            if api_key and folder_id:
                # Yandex Cloud Search API
                url = "https://searchapi.api.cloud.yandex.net/v2/wordstat/topRequests"
                headers = {
                    "Authorization": f"Api-Key {api_key}",
                    "x-folder-id": folder_id,
                    "Content-Type": "application/json",
                }
                body = {
                    "folderId": folder_id,
                    "phrase": "новости",
                    "numPhrases": 1,
                }
                resp = await client.post(url, headers=headers, json=body)
            else:
                # Direct Wordstat API
                url = "https://api.wordstat.yandex.net/v1/topRequests"
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                body = {
                    "phrase": "новости",
                    "numPhrases": 1,
                }
                resp = await client.post(url, headers=headers, json=body)

            if resp.status_code == 200:
                data = resp.json()
                top_requests = data.get("topRequests") or data.get("requests") or []
                n_items = len(top_requests)
                return {
                    "ok": True,
                    "service": "Wordstat",
                    "message": "Связь с API Wordstat установлена! Доступ подтвержден.",
                    "details": f"Ответ API: статус 200, получено фраз: {n_items}",
                }
            if resp.status_code in {401, 403}:
                return {
                    "ok": False,
                    "service": "Wordstat",
                    "message": (
                        f"Ошибка авторизации ({resp.status_code}): проверьте "
                        "правильность токена или прав доступа к сервису."
                    ),
                }
            return {
                "ok": False,
                "service": "Wordstat",
                "message": f"API вернул статус {resp.status_code}: {resp.text[:180]}",
            }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "service": "Wordstat",
            "message": f"Сетевая ошибка при обращении к Wordstat API: {exc}",
        }


async def probe_yandex_metrika(
    config: YandexConnectionConfig,
    counter_id: str | None = None,
    service_label: str = "Метрика",
) -> dict[str, Any]:
    """Test Yandex Metrika API connection."""
    target_counter = (
        counter_id if counter_id is not None else config.metrika_counter_id
    ).strip()
    token = config.metrika_token.strip()

    if not target_counter or not token:
        return {
            "ok": False,
            "service": service_label,
            "message": (
                f"Укажите номер счётчика ({service_label}) и OAuth-токен "
                "Метрики для проверки."
            ),
        }

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            url = "https://api-metrika.yandex.net/stat/v1/data"
            params = {
                "ids": target_counter,
                "metrics": "ym:s:visits",
                "date1": "today",
                "date2": "today",
            }
            headers = {
                "Authorization": f"OAuth {token}",
                "Accept": "application/json",
            }
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                return {
                    "ok": True,
                    "service": service_label,
                    "message": (
                        f"Счётчик {service_label} №{target_counter} найден и доступен! "
                        "Данные посещаемости поступают."
                    ),
                }
            if resp.status_code in {401, 403}:
                return {
                    "ok": False,
                    "service": service_label,
                    "message": (
                        f"Ошибка авторизации ({resp.status_code}): токен не имеет "
                        f"доступа к счётчику {service_label} №{target_counter}."
                    ),
                }
            return {
                "ok": False,
                "service": service_label,
                "message": (
                    f"API Метрики вернул {resp.status_code}: {resp.text[:180]}"
                ),
            }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "service": service_label,
            "message": f"Сетевая ошибка при обращении к API Метрики: {exc}",
        }


async def probe_yandex_metrika_dzen(
    config: YandexConnectionConfig,
) -> dict[str, Any]:
    """Test Yandex Metrika connection for Dzen channel counter."""
    return await probe_yandex_metrika(
        config,
        counter_id=config.metrika_dzen_counter_id,
        service_label="Метрика (Дзен)",
    )


async def probe_yandex_webmaster(config: YandexConnectionConfig) -> dict[str, Any]:
    """Test Yandex Webmaster API connection."""
    if not config.has_webmaster_credentials:
        return {
            "ok": False,
            "service": "Вебмастер",
            "message": "Укажите OAuth-токен Яндекс Вебмастера для проверки.",
        }

    token = config.webmaster_token.strip()

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            url = "https://api.webmaster.yandex.net/v4/user/"
            headers = {
                "Authorization": f"OAuth {token}",
                "Accept": "application/json",
            }
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                user_data = resp.json()
                user_id = user_data.get("user_id", "")

                # Try fetching hosts list
                hosts_url = f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/"
                hosts_resp = await client.get(hosts_url, headers=headers)
                hosts_info = ""
                if hosts_resp.status_code == 200:
                    hosts = hosts_resp.json().get("hosts", [])
                    host_names = [h.get("unicode_host_url", "") for h in hosts[:3]]
                    hosts_info = (
                        f" Подключено сайтов: {len(hosts)} "
                        f"({', '.join(filter(None, host_names))})"
                    )

                return {
                    "ok": True,
                    "service": "Вебмастер",
                    "message": (
                        f"Связь с Вебмастером установлена (User ID: {user_id})."
                        f"{hosts_info}"
                    ),
                }
            if resp.status_code in {401, 403}:
                return {
                    "ok": False,
                    "service": "Вебмастер",
                    "message": (
                        f"Ошибка авторизации ({resp.status_code}): "
                        "неверный OAuth-токен Вебмастера."
                    ),
                }
            return {
                "ok": False,
                "service": "Вебмастер",
                "message": (
                    f"API Вебмастера вернул {resp.status_code}: {resp.text[:180]}"
                ),
            }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "service": "Вебмастер",
            "message": f"Сетевая ошибка при обращении к API Вебмастера: {exc}",
        }


async def _resolve_yandex_user_and_host(
    config: YandexConnectionConfig, client: httpx.AsyncClient
) -> tuple[str | None, str]:
    """Fetch Yandex Webmaster User ID and determine target host_id."""
    token = config.webmaster_token.strip()
    host_id = config.webmaster_host_id.strip() or "https:svodka.press:443"
    headers = {"Authorization": f"OAuth {token}", "Accept": "application/json"}
    try:
        r_usr = await client.get(
            "https://api.webmaster.yandex.net/v4/user/", headers=headers
        )
        if r_usr.status_code == 200:
            user_id = str(r_usr.json().get("user_id") or "")
            return user_id, host_id
    except Exception as exc:
        logger.warning("Could not resolve Yandex Webmaster user_id: %s", exc)
    return None, host_id


async def get_yandex_recrawl_quota(
    config: YandexConnectionConfig,
) -> dict[str, Any]:
    """Fetch daily quota and remainder for priority URL recrawl."""
    if not config.has_webmaster_credentials:
        return {
            "ok": False,
            "daily_quota": 0,
            "quota_remainder": 0,
            "message": "Вебмастер не настроен.",
        }

    token = config.webmaster_token.strip()
    headers = {"Authorization": f"OAuth {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            user_id, host_id = await _resolve_yandex_user_and_host(config, client)
            if not user_id:
                return {
                    "ok": False,
                    "daily_quota": 0,
                    "quota_remainder": 0,
                    "message": "Не удалось авторизоваться в Вебмастере.",
                }

            url = f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/{host_id}/recrawl/quota"
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "ok": True,
                    "daily_quota": int(data.get("daily_quota") or 0),
                    "quota_remainder": int(data.get("quota_remainder") or 0),
                    "message": f"Остаток квоты: {data.get('quota_remainder')} из {data.get('daily_quota')}",
                }
            return {
                "ok": False,
                "daily_quota": 0,
                "quota_remainder": 0,
                "message": f"Ошибка квоты переобхода: HTTP {resp.status_code}",
            }
    except Exception as exc:
        return {
            "ok": False,
            "daily_quota": 0,
            "quota_remainder": 0,
            "message": f"Сетевая ошибка: {exc}",
        }


async def recrawl_yandex_url(
    config: YandexConnectionConfig,
    url: str,
) -> dict[str, Any]:
    """Submit a single URL to Yandex Webmaster priority recrawl queue."""
    target_url = url.strip()
    if not target_url:
        return {"ok": False, "message": "Укажите URL для переобхода."}

    if not config.has_webmaster_credentials:
        return {"ok": False, "message": "Яндекс.Вебмастер не подключён."}

    token = config.webmaster_token.strip()
    headers = {
        "Authorization": f"OAuth {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            user_id, host_id = await _resolve_yandex_user_and_host(config, client)
            if not user_id:
                return {
                    "ok": False,
                    "message": "Не удалось авторизовать пользователя в Вебмастере.",
                }

            endpoint = f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/{host_id}/recrawl/queue"
            resp = await client.post(endpoint, headers=headers, json={"url": target_url})
            if resp.status_code in {200, 202}:
                data = resp.json()
                task_id = str(data.get("task_id") or "")
                remainder = data.get("quota_remainder", "?")
                return {
                    "ok": True,
                    "task_id": task_id,
                    "quota_remainder": remainder,
                    "message": (
                        f"✅ URL успешно добавлен в очередь переобхода роботом Яндекса! "
                        f"(Остаток квоты на сегодня: {remainder})"
                    ),
                }
            if resp.status_code == 429:
                return {
                    "ok": False,
                    "message": "Исчерпана дневная квота переобхода в Яндекс.Вебмастере.",
                }
            return {
                "ok": False,
                "message": f"Ошибка Вебмастера (HTTP {resp.status_code}): {resp.text[:180]}",
            }
    except Exception as exc:
        return {
            "ok": False,
            "message": f"Сетевая ошибка при отправке в Вебмастер: {exc}",
        }


async def fetch_yandex_diagnostics(
    config: YandexConnectionConfig,
) -> dict[str, Any]:
    """Fetch site health and crawler diagnostic problems from Yandex Webmaster."""
    if not config.has_webmaster_credentials:
        return {"ok": False, "problems": {}, "summary": "Вебмастер не подключён."}

    token = config.webmaster_token.strip()
    headers = {"Authorization": f"OAuth {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            user_id, host_id = await _resolve_yandex_user_and_host(config, client)
            if not user_id:
                return {"ok": False, "problems": {}, "summary": "Нет доступа к аккаунту."}

            url = f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/{host_id}/diagnostics"
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                raw_probs = resp.json().get("problems", {})
                present_problems: list[dict[str, Any]] = []
                for p_key, p_val in raw_probs.items():
                    if isinstance(p_val, dict) and p_val.get("state") != "ABSENT":
                        present_problems.append({
                            "key": p_key,
                            "severity": p_val.get("severity", "POSSIBLE_PROBLEM"),
                            "state": p_val.get("state"),
                            "updated": p_val.get("last_state_update"),
                        })
                return {
                    "ok": True,
                    "problems": raw_probs,
                    "active_issues": present_problems,
                    "has_fatal": any(
                        p.get("severity") == "FATAL" for p in present_problems
                    ),
                    "summary": (
                        f"Обнаружено предупреждений робота: {len(present_problems)}"
                        if present_problems
                        else "Критических ошибок и санкций не обнаружено (Зелёный статус)!"
                    ),
                }
            return {
                "ok": False,
                "problems": {},
                "summary": f"Диагностика вернула HTTP {resp.status_code}",
            }
    except Exception as exc:
        return {"ok": False, "problems": {}, "summary": f"Сбой диагностики: {exc}"}


async def fetch_yandex_sitemaps(
    config: YandexConnectionConfig,
) -> list[dict[str, Any]]:
    """List registered sitemaps and their processing status in Yandex Webmaster."""
    if not config.has_webmaster_credentials:
        return []

    token = config.webmaster_token.strip()
    headers = {"Authorization": f"OAuth {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            user_id, host_id = await _resolve_yandex_user_and_host(config, client)
            if not user_id:
                return []

            url = f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/{host_id}/sitemaps"
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                raw = resp.json().get("sitemaps", [])
                return [
                    {
                        "sitemap_id": s.get("sitemap_id"),
                        "sitemap_url": s.get("sitemap_url"),
                        "last_access_date": s.get("last_access_date"),
                        "errors_count": s.get("errors_count", 0),
                        "urls_count": s.get("urls_count", 0),
                        "sitemap_type": s.get("sitemap_type"),
                    }
                    for s in raw
                    if isinstance(s, dict)
                ]
            return []
    except Exception as exc:
        logger.warning("Failed to fetch Yandex sitemaps: %s", exc)
        return []


async def add_yandex_sitemap(
    config: YandexConnectionConfig,
    sitemap_url: str,
) -> dict[str, Any]:
    """Register a new sitemap file in Yandex Webmaster."""
    target_sm = sitemap_url.strip()
    if not target_sm:
        return {"ok": False, "message": "Укажите URL карты сайта."}

    if not config.has_webmaster_credentials:
        return {"ok": False, "message": "Яндекс.Вебмастер не подключён."}

    token = config.webmaster_token.strip()
    headers = {
        "Authorization": f"OAuth {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            user_id, host_id = await _resolve_yandex_user_and_host(config, client)
            if not user_id:
                return {"ok": False, "message": "Нет доступа к аккаунту Вебмастера."}

            endpoint = f"https://api.webmaster.yandex.net/v4/user/{user_id}/hosts/{host_id}/sitemaps"
            resp = await client.post(endpoint, headers=headers, json={"url": target_sm})
            if resp.status_code in {200, 201}:
                data = resp.json()
                return {
                    "ok": True,
                    "sitemap_id": data.get("sitemap_id"),
                    "message": f"✅ Карта сайта {target_sm} успешно добавлена в Яндекс.Вебмастер!",
                }
            return {
                "ok": False,
                "message": f"Ошибка Вебмастера (HTTP {resp.status_code}): {resp.text[:180]}",
            }
    except Exception as exc:
        return {"ok": False, "message": f"Сетевая ошибка при добавлении Sitemap: {exc}"}

