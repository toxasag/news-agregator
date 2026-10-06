# ruff: noqa: E501
"""Google services connection: Google Analytics 4 (GA4) and Google Search Console (GSC).

Manages authentication, encrypted credential storage in database (Fernet),
asynchronous OAuth2 RS256 JWT minting for Google Service Accounts without external SDKs,
and diagnostic probes for GA4 Data API v1beta and Search Console API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import quote, urlparse

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import SecretBox, get_secret_box
from app.models.setting import SystemSetting

logger: Final[logging.Logger] = logging.getLogger(__name__)

GOOGLE_CONNECTION_KEY: Final[str] = "google_services_connection"
DEFAULT_TEST_TIMEOUT: Final[float] = 12.0
GOOGLE_TOKEN_URI: Final[str] = "https://oauth2.googleapis.com/token"

# Scopes needed for read-only analytics and search console
GA4_SCOPE: Final[str] = "https://www.googleapis.com/auth/analytics.readonly"
GSC_SCOPE: Final[str] = "https://www.googleapis.com/auth/webmasters.readonly"
ALL_GOOGLE_SCOPES: Final[str] = f"{GA4_SCOPE} {GSC_SCOPE}"

__all__ = [
    "ALL_GOOGLE_SCOPES",
    "GA4_SCOPE",
    "GOOGLE_CONNECTION_KEY",
    "GSC_SCOPE",
    "GoogleConnectionConfig",
    "fetch_ga4_report",
    "get_google_access_token",
    "inspect_google_url",
    "load_google_connection",
    "normalize_ga4_property_id",
    "probe_google_analytics",
    "probe_google_search_console",
    "resolve_gsc_site_url",
    "save_google_connection",
]

# In-memory cache for minted tokens: {cache_key: (token, expiry_timestamp)}
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = asyncio.Lock()


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


def normalize_ga4_property_id(raw_id: str) -> str:
    """Ensure GA4 Property ID is prefixed with 'properties/'."""
    cleaned = raw_id.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("properties/"):
        return cleaned
    return f"properties/{cleaned}"


def _extract_hostname(url_or_prop: str) -> str:
    """Extract normalized bare hostname/domain from URL or sc-domain property."""
    cleaned = url_or_prop.strip().lower()
    if cleaned.startswith("sc-domain:"):
        cleaned = cleaned[len("sc-domain:") :].strip("/")
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    try:
        parsed = urlparse(cleaned)
        host = (parsed.netloc or parsed.path).split(":")[0].strip("/")
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return cleaned.strip("/")


def resolve_gsc_site_url(
    input_url: str,
    available_sites: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Match user-provided site input against Search Console accessible site properties.

    Handles Domain Properties (e.g. ``sc-domain:svodka.press``) and URL-prefix
    properties (e.g. ``https://svodka.press/``), even if the user only entered
    a bare domain like ``svodka.press``.
    """
    cleaned = input_url.strip()

    # 1. Exact match (case insensitive, stripped)
    for s in available_sites:
        site_id = str(s.get("siteUrl", "")).strip()
        if site_id and site_id.rstrip("/").lower() == cleaned.rstrip("/").lower():
            return site_id, s.get("permissionLevel")

    # 2. Hostname/domain match
    if cleaned:
        target_host = _extract_hostname(cleaned)
        for s in available_sites:
            site_id = str(s.get("siteUrl", "")).strip()
            s_host = _extract_hostname(site_id)
            if target_host and s_host and (
                s_host in {target_host, f"www.{target_host}"}
                or target_host == f"www.{s_host}"
            ):
                return site_id, s.get("permissionLevel")

    # 3. Auto-select if there is exactly 1 site available and input was empty
    if len(available_sites) == 1 and not cleaned:
        only = available_sites[0]
        return str(only.get("siteUrl", "")), only.get("permissionLevel")

    return None, None


@dataclass(slots=True)
class GoogleConnectionConfig:
    """Configuration parameters for Google Analytics 4 and Google Search Console."""

    # Google Analytics 4
    ga4_property_id: str = ""

    # Google Search Console
    gsc_site_url: str = ""

    # Credentials (Service Account JSON takes precedence over manual OAuth token)
    service_account_json: str = ""
    oauth_access_token: str = ""

    # Toggles
    is_ga4_active: bool = True
    is_gsc_active: bool = True

    from_database: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_credentials(self) -> bool:
        """Whether any Google credential is provided."""
        return bool(self.service_account_json.strip() or self.oauth_access_token.strip())

    @property
    def has_service_account(self) -> bool:
        """Whether a valid-looking service account JSON is provided."""
        text = self.service_account_json.strip()
        return bool(text.startswith("{") and "private_key" in text)

    @property
    def has_ga4_configured(self) -> bool:
        """Whether GA4 Property ID and credentials are present."""
        return bool(self.ga4_property_id.strip() and self.has_credentials)

    @property
    def has_gsc_configured(self) -> bool:
        """Whether GSC Site URL and credentials are present."""
        return bool(self.gsc_site_url.strip() and self.has_credentials)

    @property
    def is_ready_for_gsc(self) -> bool:
        """Whether GSC is configured and enabled."""
        return bool(self.has_gsc_configured and self.is_gsc_active)

    def service_account_email(self) -> str:
        """Extract client_email from service account JSON if parseable."""
        if not self.has_service_account:
            return ""
        try:
            data = json.loads(self.service_account_json)
            return str(data.get("client_email") or "")
        except Exception:
            return ""

    def to_encrypted_document(self) -> dict[str, Any]:
        """Serialize configuration for database storage with Fernet encryption."""
        return {
            "ga4_property_id": normalize_ga4_property_id(self.ga4_property_id),
            "gsc_site_url": self.gsc_site_url.strip(),
            "service_account_json_enc": _encrypt_val(self.service_account_json.strip()),
            "oauth_access_token_enc": _encrypt_val(self.oauth_access_token.strip()),
            "is_ga4_active": bool(self.is_ga4_active),
            "is_gsc_active": bool(self.is_gsc_active),
            "extra": self.extra,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Render safe masked configuration for UI display."""
        sa_email = self.service_account_email()
        return {
            "ga4_property_id": self.ga4_property_id,
            "gsc_site_url": self.gsc_site_url,
            "has_service_account": self.has_service_account,
            "service_account_email": sa_email,
            "has_oauth_token": bool(self.oauth_access_token),
            "oauth_access_token_masked": _mask_secret(self.oauth_access_token),
            "has_ga4": self.has_ga4_configured,
            "has_gsc": self.has_gsc_configured,
            "is_ga4_active": self.is_ga4_active,
            "is_gsc_active": self.is_gsc_active,
            "from_database": self.from_database,
        }


async def _mint_service_account_token(
    sa_json: str, scope: str, client: httpx.AsyncClient | None = None
) -> str:
    """Generate OAuth2 Bearer token from Service Account JSON using async RS256 JWT."""
    data = json.loads(sa_json)
    client_email = data["client_email"]
    private_key = data["private_key"]
    token_uri = data.get("token_uri") or GOOGLE_TOKEN_URI

    now = int(time.time())
    payload = {
        "iss": client_email,
        "scope": scope,
        "aud": token_uri,
        "exp": now + 3600,
        "iat": now,
    }

    # RS256 sign with pyjwt
    assertion = jwt.encode(payload, private_key, algorithm="RS256")

    req_data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }

    managed_client = client is None
    c = client if client is not None else httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT)
    try:
        resp = await c.post(token_uri, data=req_data)
        if resp.status_code != 200:
            err_text = resp.text[:200].replace("\n", " ")
            raise ValueError(f"Google OAuth2 error ({resp.status_code}): {err_text}")
        token_data = resp.json()
        token = token_data.get("access_token")
        if not token:
            raise ValueError("No access_token returned by Google OAuth2 endpoint")
        return str(token)
    finally:
        if managed_client:
            await c.aclose()


async def get_google_access_token(
    config: GoogleConnectionConfig,
    scope: str = ALL_GOOGLE_SCOPES,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Obtain valid Google access token with caching."""
    if not config.has_credentials:
        raise ValueError("Google credentials are not configured")

    if config.has_service_account:
        sa_email = config.service_account_email()
        cache_key = f"{sa_email}:{scope}"
        now = time.time()

        async with _TOKEN_LOCK:
            if cache_key in _TOKEN_CACHE:
                token, expires_at = _TOKEN_CACHE[cache_key]
                if now < expires_at - 180:  # 3 minute safety buffer
                    return token

            # Mint new token
            token = await _mint_service_account_token(
                config.service_account_json, scope, client=client
            )
            # Cache for 55 minutes
            _TOKEN_CACHE[cache_key] = (token, now + 3300)
            return token

    # Fallback to direct OAuth access token
    return config.oauth_access_token.strip()


async def load_google_connection(session: AsyncSession) -> GoogleConnectionConfig:
    """Load Google configuration from database or fallback to .env settings."""
    try:
        stmt = select(SystemSetting.value).where(
            SystemSetting.key == GOOGLE_CONNECTION_KEY
        )
        result = await session.execute(stmt)
        raw_doc = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning("Could not read google connection from database: %s", exc)
        raw_doc = None

    if isinstance(raw_doc, dict):
        sa_json = _decrypt_val(str(raw_doc.get("service_account_json_enc", "")))
        oauth_token = _decrypt_val(str(raw_doc.get("oauth_access_token_enc", "")))

        return GoogleConnectionConfig(
            ga4_property_id=str(raw_doc.get("ga4_property_id", "")),
            gsc_site_url=str(raw_doc.get("gsc_site_url", "")),
            service_account_json=sa_json,
            oauth_access_token=oauth_token,
            is_ga4_active=bool(raw_doc.get("is_ga4_active", True)),
            is_gsc_active=bool(raw_doc.get("is_gsc_active", True)),
            from_database=True,
            extra=dict(raw_doc.get("extra", {})),
        )

    # Fallback to environment variables if any
    return GoogleConnectionConfig(
        ga4_property_id=getattr(settings, "google_analytics_property_id", ""),
        gsc_site_url=getattr(settings, "gsc_site_url", ""),
        service_account_json=getattr(settings, "google_service_account_json", ""),
        oauth_access_token="",
        is_ga4_active=True,
        is_gsc_active=True,
        from_database=False,
    )


async def save_google_connection(
    session: AsyncSession, config: GoogleConnectionConfig
) -> GoogleConnectionConfig:
    """Save Google configuration to database with Fernet encryption."""
    doc = config.to_encrypted_document()
    stmt = (
        pg_insert(SystemSetting)
        .values(
            key=GOOGLE_CONNECTION_KEY,
            value=doc,
            description="Параметры подключения к Google Analytics 4 и Search Console",
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


async def probe_google_analytics(config: GoogleConnectionConfig) -> dict[str, Any]:
    """Test connection to Google Analytics 4 Data API."""
    if not config.ga4_property_id.strip():
        return {
            "ok": False,
            "service": "Google Analytics 4",
            "message": "Укажите идентификатор ресурса (GA4 Property ID).",
        }
    if not config.has_credentials:
        return {
            "ok": False,
            "service": "Google Analytics 4",
            "message": "Вставьте Service Account JSON или укажите OAuth-токен.",
        }

    prop_id = normalize_ga4_property_id(config.ga4_property_id)

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            token = await get_google_access_token(config, scope=GA4_SCOPE, client=client)

            url = f"https://analyticsdata.googleapis.com/v1beta/{prop_id}:runReport"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            body = {
                "dateRanges": [{"startDate": "7daysAgo", "endDate": "today"}],
                "metrics": [
                    {"name": "activeUsers"},
                    {"name": "sessions"},
                    {"name": "screenPageViews"},
                ],
            }

            resp = await client.post(url, headers=headers, json=body)

            if resp.status_code == 200:
                data = resp.json()
                rows = data.get("rows", [])
                users = 0
                sessions = 0
                views = 0
                if rows and "metricValues" in rows[0]:
                    vals = rows[0]["metricValues"]
                    users = int(vals[0].get("value", 0)) if len(vals) > 0 else 0
                    sessions = int(vals[1].get("value", 0)) if len(vals) > 1 else 0
                    views = int(vals[2].get("value", 0)) if len(vals) > 2 else 0

                return {
                    "ok": True,
                    "service": "Google Analytics 4",
                    "message": f"Связь с GA4 установлена ({prop_id}). Данные поступают!",
                    "details": (
                        f"За последние 7 дней: активных пользователей — {users}, "
                        f"сессий — {sessions}, просмотров — {views}."
                    ),
                }

            if resp.status_code in {401, 403}:
                sa_email = config.service_account_email()
                hint = (
                    f" Убедитесь, что сервисный аккаунт {sa_email} добавлен "
                    "в ресурсе GA4 с ролью «Читатель»."
                    if sa_email
                    else ""
                )
                return {
                    "ok": False,
                    "service": "Google Analytics 4",
                    "message": (
                        f"Ошибка доступа ({resp.status_code}): нет прав на "
                        f"ресурс {prop_id}.{hint}"
                    ),
                }

            if resp.status_code == 404:
                return {
                    "ok": False,
                    "service": "Google Analytics 4",
                    "message": (
                        f"Ресурс {prop_id} не найден. Проверьте правильность Property ID."
                    ),
                }

            return {
                "ok": False,
                "service": "Google Analytics 4",
                "message": f"GA4 API вернул HTTP {resp.status_code}: {resp.text[:180]}",
            }

    except Exception as exc:
        return {
            "ok": False,
            "service": "Google Analytics 4",
            "message": f"Ошибка проверки Google Analytics: {exc}",
        }


async def probe_google_search_console(config: GoogleConnectionConfig) -> dict[str, Any]:
    """Test connection to Google Search Console API and auto-resolve site identifier."""
    if not config.has_credentials:
        return {
            "ok": False,
            "service": "Google Search Console",
            "message": "Вставьте Service Account JSON или укажите OAuth-токен.",
        }

    input_site = config.gsc_site_url.strip()

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            token = await get_google_access_token(config, scope=GSC_SCOPE, client=client)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }

            # 1. Query available sites list for this credential
            available_sites: list[dict[str, Any]] = []
            try:
                sites_resp = await client.get(
                    "https://www.googleapis.com/webmasters/v3/sites",
                    headers=headers,
                )
                if sites_resp.status_code == 200:
                    sites_data = sites_resp.json()
                    raw_entries = sites_data.get("siteEntry")
                    if isinstance(raw_entries, list):
                        available_sites = [e for e in raw_entries if isinstance(e, dict)]
                elif sites_resp.status_code in {401, 403}:
                    sa_email = config.service_account_email()
                    hint = (
                        " Убедитесь, что Google Search Console API включен в "
                        f"Google Cloud Console, а сервисный аккаунт {sa_email} "
                        "добавлен в Search Console."
                        if sa_email
                        else ""
                    )
                    return {
                        "ok": False,
                        "service": "Google Search Console",
                        "message": (
                            f"Ошибка доступа ({sites_resp.status_code}) к "
                            f"Search Console API.{hint}"
                        ),
                    }
            except Exception as exc:
                logger.warning("Could not list GSC sites: %s", exc)

            # 2. Resolve matching site identifier
            resolved_site, permission = resolve_gsc_site_url(input_site, available_sites)

            target_site = resolved_site or input_site
            if not target_site:
                sa_email = config.service_account_email()
                if not available_sites:
                    return {
                        "ok": False,
                        "service": "Google Search Console",
                        "message": (
                            f"Сервисный аккаунт {sa_email} пока не добавлен ни к "
                            "одному ресурсу в Google Search Console. Перейдите в "
                            "Search Console → Настройки → Пользователи и разрешения "
                            "и добавьте сервисный аккаунт."
                        ),
                    }
                avail_list = ", ".join(
                    str(e.get("siteUrl", "")) for e in available_sites if e.get("siteUrl")
                )
                return {
                    "ok": False,
                    "service": "Google Search Console",
                    "message": (
                        "Укажите сайт в Search Console. "
                        f"Доступные сервисному аккаунту ресурсы: {avail_list}."
                    ),
                }

            # 3. Query Search Analytics for search performance metrics
            # Note: GSC API strictly requires ISO-formatted dates (YYYY-MM-DD)
            today = datetime.now(UTC).date()
            start_date = (today - timedelta(days=7)).isoformat()
            end_date = today.isoformat()

            encoded_site = quote(target_site, safe="")
            query_url = (
                f"https://www.googleapis.com/webmasters/v3/sites/"
                f"{encoded_site}/searchAnalytics/query"
            )
            body = {
                "startDate": start_date,
                "endDate": end_date,
                "rowLimit": 5,
            }

            resp = await client.post(query_url, headers=headers, json=body)

            if resp.status_code == 200:
                data = resp.json()
                rows = data.get("rows", [])
                total_clicks = sum(
                    int(r.get("clicks", 0)) for r in rows if isinstance(r, dict)
                )
                total_impressions = sum(
                    int(r.get("impressions", 0)) for r in rows if isinstance(r, dict)
                )

                role_hint = f" (роль: {permission})" if permission else ""
                resolution_hint = ""
                if input_site and target_site != input_site:
                    resolution_hint = (
                        f" (автоматически определен тип ресурса: {target_site})"
                    )

                return {
                    "ok": True,
                    "service": "Google Search Console",
                    "site_url": target_site,
                    "message": (
                        f"Связь с Search Console установлена! Сайт {target_site} "
                        f"подтверждён{role_hint}{resolution_hint}."
                    ),
                    "details": (
                        f"Поисковая видимость (за 7 дн): показов — {total_impressions}, "
                        f"кликов — {total_clicks}, страниц в выборке — {len(rows)}."
                    ),
                }

            if resp.status_code in {401, 403}:
                sa_email = config.service_account_email()
                hint = (
                    f" Добавьте сервисный аккаунт {sa_email} в Search Console "
                    "как пользователя (Полный или Ограниченный доступ)."
                    if sa_email
                    else ""
                )
                if available_sites:
                    avail_str = ", ".join(
                        str(e.get("siteUrl", ""))
                        for e in available_sites
                        if e.get("siteUrl")
                    )
                    hint += f" Доступные ресурсы в Search Console: {avail_str}."

                return {
                    "ok": False,
                    "service": "Google Search Console",
                    "site_url": target_site,
                    "message": (
                        f"Ошибка доступа ({resp.status_code}) к {target_site}.{hint}"
                    ),
                }

            return {
                "ok": False,
                "service": "Google Search Console",
                "site_url": target_site,
                "message": (
                    f"GSC API вернул HTTP {resp.status_code}: {resp.text[:180]}"
                ),
            }

    except Exception as exc:
        return {
            "ok": False,
            "service": "Google Search Console",
            "message": f"Ошибка проверки Google Search Console: {exc}",
        }


async def inspect_google_url(
    config: GoogleConnectionConfig,
    url: str,
) -> dict[str, Any]:
    """Query Google Search Console URL Inspection API for a specific page."""
    target_url = url.strip()
    if not target_url:
        return {"ok": False, "message": "Укажите URL для проверки индексации."}

    if not config.has_credentials:
        return {"ok": False, "message": "Google Search Console не настроен."}

    target_site = config.gsc_site_url.strip() or "sc-domain:svodka.press"

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            token = await get_google_access_token(
                config, scope=GSC_SCOPE, client=client
            )
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            api_url = (
                "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
            )
            body = {
                "inspectionUrl": target_url,
                "siteUrl": target_site,
                "languageCode": "ru",
            }
            resp = await client.post(api_url, headers=headers, json=body)
            if resp.status_code == 200:
                res = resp.json().get("inspectionResult", {})
                idx_res = res.get("indexStatusResult", {})
                mob_res = res.get("mobileUsabilityResult", {})

                verdict = idx_res.get("verdict", "NEUTRAL")
                coverage = idx_res.get(
                    "coverageState", "Данные об индексации отсутствуют"
                )
                crawl_time = idx_res.get("lastCrawlTime")
                robots = idx_res.get("robotsTxtState", "ALLOWED")
                indexing = idx_res.get("indexingState", "INDEXING_ALLOWED")
                google_canonical = idx_res.get("googleCanonical")

                is_indexed = verdict == "PASS" or "проиндексирована" in coverage.lower()

                return {
                    "ok": True,
                    "url": target_url,
                    "is_indexed": is_indexed,
                    "verdict": verdict,
                    "coverage_state": coverage,
                    "last_crawl_time": crawl_time,
                    "robots_txt_state": robots,
                    "indexing_state": indexing,
                    "google_canonical": google_canonical,
                    "mobile_verdict": mob_res.get("verdict", "PASS"),
                    "message": (
                        f"🟢 Google: {coverage}"
                        if is_indexed
                        else f"🟡 Google: {coverage} (статус: {verdict})"
                    ),
                }

            if resp.status_code in {401, 403}:
                return {
                    "ok": False,
                    "message": f"Ошибка доступа к URL Inspection API (HTTP {resp.status_code}).",
                }

            return {
                "ok": False,
                "message": f"Google Inspection API вернул HTTP {resp.status_code}: {resp.text[:180]}",
            }
    except Exception as exc:
        return {"ok": False, "message": f"Ошибка проверки в Google Inspection: {exc}"}


async def fetch_ga4_report(
    config: GoogleConnectionConfig,
    *,
    days: int = 28,
) -> dict[str, Any]:
    """Fetch high-level audience and traffic performance from Google Analytics 4."""
    if not config.has_credentials or not config.is_ga4_active:
        return {"available": False, "reason": "GA4 не настроен или выключен."}

    prop_id = normalize_ga4_property_id(config.ga4_property_id)
    if not prop_id:
        return {"available": False, "reason": "Не указан GA4 Property ID."}

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
            token = await get_google_access_token(
                config, scope=GA4_SCOPE, client=client
            )
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            api_url = f"https://analyticsdata.googleapis.com/v1beta/{prop_id}:runReport"
            body = {
                "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
                "metrics": [
                    {"name": "activeUsers"},
                    {"name": "sessions"},
                    {"name": "screenPageViews"},
                    {"name": "bounceRate"},
                    {"name": "averageSessionDuration"},
                ],
            }
            resp = await client.post(api_url, headers=headers, json=body)
            if resp.status_code == 200:
                data = resp.json()
                rows = data.get("rows", [])
                users = 0
                sessions = 0
                views = 0
                bounce_rate = 0.0
                avg_duration = 0.0
                if rows and "metricValues" in rows[0]:
                    vals = rows[0]["metricValues"]
                    users = int(vals[0].get("value", 0)) if len(vals) > 0 else 0
                    sessions = int(vals[1].get("value", 0)) if len(vals) > 1 else 0
                    views = int(vals[2].get("value", 0)) if len(vals) > 2 else 0
                    bounce_rate = float(vals[3].get("value", 0.0)) if len(vals) > 3 else 0.0
                    avg_duration = float(vals[4].get("value", 0.0)) if len(vals) > 4 else 0.0

                return {
                    "available": True,
                    "property_id": prop_id,
                    "active_users": users,
                    "sessions": sessions,
                    "screen_pageviews": views,
                    "bounce_rate": round(bounce_rate, 3),
                    "bounce_rate_pct": round(bounce_rate * 100, 1),
                    "avg_duration_sec": round(avg_duration, 0),
                    "days": days,
                }

            return {
                "available": False,
                "property_id": prop_id,
                "reason": f"GA4 API HTTP {resp.status_code}",
            }
    except Exception as exc:
        logger.warning("Failed to fetch GA4 report: %s", exc)
        return {"available": False, "reason": str(exc)}

