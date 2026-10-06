"""Unit tests for Bing Webmaster & IndexNow connection, encryption, and testing."""

from __future__ import annotations

from typing import Any, cast

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import SystemSetting
from app.services.bing_connection import (
    BING_CONNECTION_KEY,
    INDEXNOW_ENDPOINT,
    BingConnectionConfig,
    load_bing_connection,
    probe_bing_webmaster,
    probe_indexnow,
    save_bing_connection,
    submit_to_indexnow,
)


class _Scalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _Empty:
    def scalar_one_or_none(self) -> Any:
        return None


class FakeBingSession:
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
                    self.settings[BING_CONNECTION_KEY] = SystemSetting(
                        key=BING_CONNECTION_KEY, value=doc
                    )
            return _Empty()
        row = self.settings.get(BING_CONNECTION_KEY)
        return _Scalar(row.value if row else None)

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_bing_config_properties_and_masking() -> None:
    config = BingConnectionConfig(
        site_url="https://svodka.press/",
        api_key="1234567890abcdef1234567890abcdef",
        indexnow_key="fedcba0987654321fedcba0987654321",
    )
    assert config.has_api_key is True
    assert config.has_indexnow_key is True
    assert config.host == "svodka.press"

    pub = config.to_public_dict()
    assert "1234567890abcdef" not in pub["api_key_masked"]
    assert pub["has_api_key"] is True
    assert pub["has_indexnow_key"] is True
    assert pub["host"] == "svodka.press"


@pytest.mark.asyncio
async def test_save_and_load_bing_connection() -> None:
    session = FakeBingSession()
    config = BingConnectionConfig(
        site_url="https://svodka.press/",
        api_key="bing_secret_key_12345",
        indexnow_key="indexnow_secret_key_67890",
        indexnow_key_location="https://svodka.press/my-key.txt",
    )

    saved = await save_bing_connection(cast(AsyncSession, session), config)
    assert saved.from_database is True
    assert session.commits == 1

    loaded = await load_bing_connection(cast(AsyncSession, session))
    assert loaded.site_url == "https://svodka.press/"
    assert loaded.api_key == "bing_secret_key_12345"
    assert loaded.indexnow_key == "indexnow_secret_key_67890"
    assert loaded.indexnow_key_location == "https://svodka.press/my-key.txt"
    assert loaded.from_database is True


@pytest.mark.asyncio
@respx.mock
async def test_probe_bing_webmaster() -> None:
    respx.get(
        "https://ssl.bing.com/webmaster/api.svc/json/GetUserSites?apikey=test_bing_key"
    ).respond(
        200,
        json={
            "d": [
                {"Url": "https://svodka.press/", "Role": "Administrator"},
                {"Url": "https://example.com/", "Role": "ReadOnly"},
            ]
        },
    )

    cfg = BingConnectionConfig(site_url="https://svodka.press", api_key="test_bing_key")
    res = await probe_bing_webmaster(cfg)
    assert res["ok"] is True
    assert "Bing Webmaster установлена" in res["message"]


@pytest.mark.asyncio
@respx.mock
async def test_probe_indexnow_and_submit() -> None:
    respx.post(INDEXNOW_ENDPOINT).respond(200, text="OK")

    cfg = BingConnectionConfig(
        site_url="https://svodka.press",
        indexnow_key="test_indexnow_key",
    )

    probe_res = await probe_indexnow(cfg)
    assert probe_res["ok"] is True
    assert "IndexNow активен" in probe_res["message"]

    submit_res = await submit_to_indexnow(
        ["https://svodka.press/article-1", "https://svodka.press/article-2"],
        cfg,
    )
    assert submit_res["ok"] is True
    assert submit_res["count"] == 2
