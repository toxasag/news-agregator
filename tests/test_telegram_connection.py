"""Tests for Telegram connection service and live bot validation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app.services.telegram_connection import (
    TelegramChannelConfig,
    probe_telegram_bot_connection,
    save_telegram_channel_config,
)


@pytest.mark.asyncio
async def test_telegram_config_properties() -> None:
    cfg = TelegramChannelConfig(
        bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        channel_id="@test_svodka_channel",
        is_active=True,
        status="active",
        bot_username="test_svodka_bot",
        channel_title="Сводка Тест",
    )
    assert cfg.has_token is True
    assert cfg.has_channel is True
    assert cfg.masked_token() == "1234••••••••wxyz"

    empty_cfg = TelegramChannelConfig()
    assert empty_cfg.has_token is False
    assert empty_cfg.has_channel is False
    assert empty_cfg.masked_token() == ""


@pytest.mark.asyncio
async def test_telegram_config_session_save() -> None:
    session = AsyncMock()
    cfg = TelegramChannelConfig(
        bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        channel_id="@test_svodka_channel",
        is_active=True,
        status="active",
        bot_username="test_svodka_bot",
        channel_title="Сводка Тест",
    )
    await save_telegram_channel_config(session, cfg)
    assert session.execute.called
    assert session.commit.called


@pytest.mark.asyncio
@respx.mock
async def test_probe_telegram_bot_connection_success() -> None:
    token = "123456:valid_token"
    channel = "@svodka_press"

    respx.get(f"https://api.telegram.org/bot{token}/getMe").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"id": 999111, "is_bot": True, "username": "svodkapress_bot"},
            },
        )
    )

    respx.get(f"https://api.telegram.org/bot{token}/getChat").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": -1001234567890,
                    "title": "СводкаПресс",
                    "type": "channel",
                },
            },
        )
    )

    respx.get(f"https://api.telegram.org/bot{token}/getChatMember").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "status": "administrator",
                    "can_post_messages": True,
                },
            },
        )
    )

    res = await probe_telegram_bot_connection(token, channel)
    assert res["valid"] is True
    assert res["status"] == "active"
    assert res["bot_username"] == "svodkapress_bot"
    assert res["is_admin"] is True
    assert res["channel_title"] == "СводкаПресс"


@pytest.mark.asyncio
@respx.mock
async def test_probe_telegram_bot_connection_invalid_token() -> None:
    token = "invalid_token"
    respx.get(f"https://api.telegram.org/bot{token}/getMe").mock(
        return_value=httpx.Response(
            401,
            json={"ok": False, "description": "Unauthorized"},
        )
    )

    res = await probe_telegram_bot_connection(token, "@some_channel")
    assert res["valid"] is False
    assert res["status"] == "error"
    assert "Unauthorized" in res["error"]
