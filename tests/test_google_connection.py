"""Unit tests for Google services connection (GA4, GSC), JWT minting and diagnostics."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import SystemSetting
from app.services.google_connection import (
    GOOGLE_CONNECTION_KEY,
    GoogleConnectionConfig,
    _mint_service_account_token,
    load_google_connection,
    normalize_ga4_property_id,
    probe_google_analytics,
    probe_google_search_console,
    resolve_gsc_site_url,
    save_google_connection,
)


class _Scalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _Empty:
    def scalar_one_or_none(self) -> Any:
        return None


class FakeGoogleSession:
    """AsyncSession double storing SystemSetting in memory."""

    def __init__(self) -> None:
        self.settings: dict[str, SystemSetting] = {}
        self.commits = 0

    async def execute(self, statement: Any) -> Any:
        compiled = str(statement).lower()
        if compiled.startswith("insert"):
            for key, val in (getattr(statement, "_values", {}) or {}).items():
                attr = getattr(key, "name", str(key))
                if attr == "value":
                    doc = getattr(val, "value", val)
                    self.settings[GOOGLE_CONNECTION_KEY] = SystemSetting(
                        key=GOOGLE_CONNECTION_KEY, value=doc
                    )
            return _Empty()
        row = self.settings.get(GOOGLE_CONNECTION_KEY)
        return _Scalar(row.value if row else None)

    async def commit(self) -> None:
        self.commits += 1


def _generate_test_sa_json() -> str:
    """Generate a valid RSA private key formatted as Google Service Account JSON."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    sa_dict = {
        "type": "service_account",
        "project_id": "test-project-123",
        "private_key_id": "key123",
        "private_key": pem,
        "client_email": "svodka-sa@test-project-123.iam.gserviceaccount.com",
        "client_id": "999888777",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return json.dumps(sa_dict)


@pytest.mark.asyncio
async def test_google_config_properties_and_normalization() -> None:
    sa_json = _generate_test_sa_json()
    config = GoogleConnectionConfig(
        ga4_property_id="123456789",
        gsc_site_url="https://svodka.press/",
        service_account_json=sa_json,
    )

    assert config.has_credentials is True
    assert config.has_service_account is True
    assert (
        config.service_account_email()
        == "svodka-sa@test-project-123.iam.gserviceaccount.com"
    )
    assert config.has_ga4_configured is True
    assert config.has_gsc_configured is True
    assert normalize_ga4_property_id("123456789") == "properties/123456789"
    assert normalize_ga4_property_id("properties/123456789") == "properties/123456789"

    pub = config.to_public_dict()
    assert pub["has_service_account"] is True
    assert (
        pub["service_account_email"]
        == "svodka-sa@test-project-123.iam.gserviceaccount.com"
    )
    assert pub["has_ga4"] is True
    assert pub["has_gsc"] is True


def test_resolve_gsc_site_url() -> None:
    available = [
        {"siteUrl": "sc-domain:svodka.press", "permissionLevel": "siteFullUser"},
        {"siteUrl": "https://other-news.ru/", "permissionLevel": "siteOwner"},
    ]

    # 1. Bare domain matching sc-domain
    site, perm = resolve_gsc_site_url("svodka.press", available)
    assert site == "sc-domain:svodka.press"
    assert perm == "siteFullUser"

    # 2. URL prefix matching sc-domain
    site, perm = resolve_gsc_site_url("https://svodka.press/", available)
    assert site == "sc-domain:svodka.press"
    assert perm == "siteFullUser"

    # 3. Exact sc-domain match
    site, perm = resolve_gsc_site_url("sc-domain:svodka.press", available)
    assert site == "sc-domain:svodka.press"
    assert perm == "siteFullUser"

    # 4. Matching the other URL-prefix property
    site, perm = resolve_gsc_site_url("other-news.ru", available)
    assert site == "https://other-news.ru/"
    assert perm == "siteOwner"

    # 5. Non-matching domain
    site, perm = resolve_gsc_site_url("https://nonexistent.org", available)
    assert site is None
    assert perm is None

    # 6. Auto-select single available site if input empty
    single = [{"siteUrl": "sc-domain:svodka.press", "permissionLevel": "siteFullUser"}]
    site, perm = resolve_gsc_site_url("", single)
    assert site == "sc-domain:svodka.press"


@pytest.mark.asyncio
async def test_save_and_load_google_connection() -> None:
    session = FakeGoogleSession()
    sa_json = _generate_test_sa_json()
    config = GoogleConnectionConfig(
        ga4_property_id="properties/987654",
        gsc_site_url="https://svodka.press/",
        service_account_json=sa_json,
        oauth_access_token="manual_oauth_token",
    )

    saved = await save_google_connection(cast(AsyncSession, session), config)
    assert saved.from_database is True
    assert session.commits == 1

    loaded = await load_google_connection(cast(AsyncSession, session))
    assert loaded.ga4_property_id == "properties/987654"
    assert loaded.gsc_site_url == "https://svodka.press/"
    assert loaded.service_account_json == sa_json
    assert loaded.oauth_access_token == "manual_oauth_token"
    assert loaded.from_database is True


@pytest.mark.asyncio
@respx.mock
async def test_mint_service_account_token() -> None:
    respx.post("https://oauth2.googleapis.com/token").respond(
        200, json={"access_token": "ya29.test_access_token_123", "expires_in": 3600}
    )

    sa_json = _generate_test_sa_json()
    token = await _mint_service_account_token(
        sa_json, "https://www.googleapis.com/auth/analytics.readonly"
    )
    assert token == "ya29.test_access_token_123"


@pytest.mark.asyncio
@respx.mock
async def test_probe_google_analytics_and_search_console() -> None:
    # 1. GA4 probe
    respx.post(
        "https://analyticsdata.googleapis.com/v1beta/properties/123456:runReport"
    ).respond(
        200,
        json={
            "rows": [
                {
                    "metricValues": [
                        {"value": "150"},
                        {"value": "200"},
                        {"value": "550"},
                    ]
                }
            ]
        },
    )

    ga_cfg = GoogleConnectionConfig(
        ga4_property_id="properties/123456",
        oauth_access_token="test_bearer_token",
    )
    ga_res = await probe_google_analytics(ga_cfg)
    assert ga_res["ok"] is True
    assert "Связь с GA4 установлена" in ga_res["message"]
    assert "пользователей — 150" in ga_res["details"]

    # 2. GSC sites listing mock
    respx.get("https://www.googleapis.com/webmasters/v3/sites").respond(
        200,
        json={
            "siteEntry": [
                {
                    "siteUrl": "sc-domain:svodka.press",
                    "permissionLevel": "siteFullUser",
                }
            ]
        },
    )

    # 3. GSC search analytics query mock for sc-domain:svodka.press
    respx.post(
        "https://www.googleapis.com/webmasters/v3/sites/sc-domain%3Asvodka.press/searchAnalytics/query"
    ).respond(
        200,
        json={
            "rows": [
                {"clicks": 25, "impressions": 450},
                {"clicks": 10, "impressions": 120},
            ]
        },
    )

    # Test bare domain input auto-resolves to sc-domain:svodka.press
    gsc_cfg = GoogleConnectionConfig(
        gsc_site_url="svodka.press",
        oauth_access_token="test_bearer_token",
    )
    gsc_res = await probe_google_search_console(gsc_cfg)
    assert gsc_res["ok"] is True
    assert gsc_res["site_url"] == "sc-domain:svodka.press"
    assert "Связь с Search Console установлена" in gsc_res["message"]
    assert "sc-domain:svodka.press" in gsc_res["message"]
    assert "показов — 570" in gsc_res["details"]
    assert "кликов — 35" in gsc_res["details"]
