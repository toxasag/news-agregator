from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.tenchat_auth import TenChatAuthManager


@pytest.mark.asyncio
async def test_tenchat_auth_manager_pending_state() -> None:
    manager = TenChatAuthManager()
    assert not manager.is_pending
    assert manager.phone is None
    assert manager.status_message is None

    # Cancel on idle is safe
    await manager.cancel()
    assert not manager.is_pending


@pytest.mark.asyncio
async def test_tenchat_auth_manager_verify_invalid_code() -> None:
    manager = TenChatAuthManager()
    session = AsyncMock()

    # When no session is active
    res = await manager.verify_code("12345", session=session)
    assert res["status"] == "error"
    assert "истекла" in res["message"]

    # When code length is wrong
    manager._page = MagicMock()
    manager._context = MagicMock()
    res2 = await manager.verify_code("123", session=session)
    assert res2["status"] == "error"
    assert "5 цифр" in res2["message"]
