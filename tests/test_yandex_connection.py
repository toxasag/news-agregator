"""Unit tests for Yandex services connection, encryption, and testing."""

from __future__ import annotations

from typing import Any, cast

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import SystemSetting
from app.services.yandex_connection import (
    YANDEX_CONNECTION_KEY,
    YandexConnectionConfig,
    load_yandex_connection,
    probe_yandex_metrika,
    probe_yandex_metrika_dzen,
    probe_yandex_webmaster,
    probe_yandex_wordstat,
    save_yandex_connection,
)


class _Scalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _Empty:
    def scalar_one_or_none(self) -> Any:
        return None


class FakeYandexSession:
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
                    self.settings[YANDEX_CONNECTION_KEY] = SystemSetting(
                        key=YANDEX_CONNECTION_KEY, value=doc
                    )
            return _Empty()
        row = self.settings.get(YANDEX_CONNECTION_KEY)
        return _Scalar(row.value if row else None)

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_yandex_config_properties_and_masking() -> None:
    config = YandexConnectionConfig(
        wordstat_token="y0_AgAAAAWordstatSecret",
        wordstat_api_key="AQVNCloudApiKey",
        wordstat_folder_id="b1gfolder123",
        metrika_counter_id="98765432",
        metrika_dzen_counter_id="11223344",
        metrika_token="y0_AgAAAAMetrikaSecret",
        webmaster_host_id="https:svodka.press:443",
        webmaster_token="y0_AgAAAAWebmasterSecret",
    )
    assert config.has_wordstat_credentials is True
    assert config.has_metrika_credentials is True
    assert config.has_metrika_dzen is True
    assert config.has_webmaster_credentials is True

    pub = config.to_public_dict()
    assert "y0_AgAAAAWordstatSecret" not in str(pub.values())
    assert pub["has_wordstat_token"] is True
    assert pub["has_metrika_token"] is True
    assert pub["has_webmaster_token"] is True
    assert pub["metrika_counter_id"] == "98765432"
    assert pub["metrika_dzen_counter_id"] == "11223344"
    assert pub["has_metrika_dzen"] is True


@pytest.mark.asyncio
async def test_save_and_load_yandex_connection() -> None:
    session = FakeYandexSession()
    config = YandexConnectionConfig(
        wordstat_token="secret_wordstat_token_123",
        wordstat_folder_id="folder_abc",
        metrika_counter_id="12345678",
        metrika_dzen_counter_id="87654321",
        metrika_token="secret_metrika_token_456",
        webmaster_token="secret_webmaster_token_789",
    )

    saved = await save_yandex_connection(cast(AsyncSession, session), config)
    assert saved.from_database is True
    assert session.commits == 1

    loaded = await load_yandex_connection(cast(AsyncSession, session))
    assert loaded.wordstat_token == "secret_wordstat_token_123"
    assert loaded.wordstat_folder_id == "folder_abc"
    assert loaded.metrika_counter_id == "12345678"
    assert loaded.metrika_dzen_counter_id == "87654321"
    assert loaded.metrika_token == "secret_metrika_token_456"
    assert loaded.webmaster_token == "secret_webmaster_token_789"
    assert loaded.from_database is True


@pytest.mark.asyncio
@respx.mock
async def test_probe_yandex_apis() -> None:
    # 1. Wordstat probe
    respx.post("https://api.wordstat.yandex.net/v1/topRequests").respond(
        200, json={"topRequests": [{"phrase": "новости", "count": 1000}]}
    )
    w_cfg = YandexConnectionConfig(wordstat_token="test_token")
    w_res = await probe_yandex_wordstat(w_cfg)
    assert w_res["ok"] is True
    assert "Связь с API Wordstat установлена" in w_res["message"]

    # 2. Metrika site probe
    respx.get(
        "https://api-metrika.yandex.net/stat/v1/data?ids=998877&metrics=ym%3As%3Avisits&date1=today&date2=today"
    ).respond(200, json={"data": [{"metrics": [42]}]})
    m_cfg = YandexConnectionConfig(
        metrika_counter_id="998877", metrika_token="token_m"
    )
    m_res = await probe_yandex_metrika(m_cfg)
    assert m_res["ok"] is True
    assert "Счётчик Метрика №998877 найден" in m_res["message"]

    # 3. Metrika Dzen probe
    respx.get(
        "https://api-metrika.yandex.net/stat/v1/data?ids=554433&metrics=ym%3As%3Avisits&date1=today&date2=today"
    ).respond(200, json={"data": [{"metrics": [108]}]})
    dzen_cfg = YandexConnectionConfig(
        metrika_dzen_counter_id="554433", metrika_token="token_m"
    )
    dzen_res = await probe_yandex_metrika_dzen(dzen_cfg)
    assert dzen_res["ok"] is True
    assert "Счётчик Метрика (Дзен) №554433 найден" in dzen_res["message"]

    # 4. Webmaster probe
    respx.get("https://api.webmaster.yandex.net/v4/user/").respond(
        200, json={"user_id": 102030}
    )
    respx.get("https://api.webmaster.yandex.net/v4/user/102030/hosts/").respond(
        200, json={"hosts": [{"unicode_host_url": "svodka.press"}]}
    )
    wb_cfg = YandexConnectionConfig(webmaster_token="token_wb")
    wb_res = await probe_yandex_webmaster(wb_cfg)
    assert wb_res["ok"] is True
    assert "User ID: 102030" in wb_res["message"]
