# Fixture test passwords.
# ruff: noqa: S105, S106

from __future__ import annotations

from typing import Any, cast

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.category_prompt import CategoryPrompt
from app.models.setting import SystemSetting
from app.services.categories import CATEGORY_MAP
from app.services.wp_connection import (
    WP_CONNECTION_KEY,
    WPConnectionConfig,
    load_wp_connection,
    save_wp_connection,
    sync_wp_categories,
    test_wp_rest,
    test_wp_ssh,
)


class _Scalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _Empty:
    def scalar_one_or_none(self) -> Any:
        return None


class FakeWPSession:
    """AsyncSession double storing SystemSetting and CategoryPrompt in memory."""

    def __init__(self) -> None:
        self.settings: dict[str, SystemSetting] = {}
        self.categories: dict[str, CategoryPrompt] = {}
        self.commits = 0

    async def execute(self, statement: Any) -> Any:
        compiled = str(statement).lower()
        if compiled.startswith("insert"):
            for key, val in (getattr(statement, "_values", {}) or {}).items():
                attr = getattr(key, "name", str(key))
                if attr == "value":
                    doc = getattr(val, "value", val)
                    self.settings[WP_CONNECTION_KEY] = SystemSetting(
                        key=WP_CONNECTION_KEY, value=doc
                    )
            return _Empty()
        # Assume select for system setting
        row = self.settings.get(WP_CONNECTION_KEY)
        return _Scalar(row.value if row else None)

    async def get(self, entity: Any, ident: Any) -> Any:
        if entity is SystemSetting:
            return self.settings.get(ident)
        if entity is CategoryPrompt:
            return self.categories.get(ident)
        return None

    def add(self, instance: Any) -> None:
        if isinstance(instance, CategoryPrompt):
            self.categories[instance.slug] = instance
        elif isinstance(instance, SystemSetting):
            self.settings[instance.key] = instance

    async def commit(self) -> None:
        self.commits += 1

    def as_session(self) -> AsyncSession:
        return cast(AsyncSession, self)


@pytest.fixture
def async_session() -> AsyncSession:
    return FakeWPSession().as_session()


@pytest.mark.asyncio
async def test_load_wp_connection_defaults_to_settings(
    async_session: AsyncSession,
) -> None:
    config = await load_wp_connection(async_session)
    assert config.from_database is False
    assert config.wp_base_url == settings.wp_base_url.strip()
    assert config.wp_app_user == settings.wp_app_user.strip()


@pytest.mark.asyncio
async def test_save_and_load_wp_connection_roundtrip(
    async_session: AsyncSession,
) -> None:
    raw_key = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----"
    )
    initial = WPConnectionConfig(
        wp_base_url="https://svodka.press",
        wp_app_user="editorial_bot",
        wp_app_password="secret-app-password-123",
        ssh_host="144.31.141.17",
        ssh_user="deployer",
        ssh_port=2222,
        ssh_private_key=raw_key,
        wp_document_root="/var/www/svodkapress",
        is_active=True,
    )

    saved = await save_wp_connection(async_session, initial)
    assert saved.from_database is True

    # Verify that stored DB row is encrypted (not plaintext)
    stored = await async_session.get(SystemSetting, WP_CONNECTION_KEY)
    assert stored is not None
    doc = stored.value
    assert isinstance(doc, dict)
    assert doc["wp_base_url"] == "https://svodka.press"
    # Password and private key must not be plaintext in database
    if settings.fernet_key.strip():
        assert "secret-app-password-123" not in str(doc["wp_app_password_enc"])
        assert "OPENSSH" not in str(doc["ssh_private_key_enc"])

    # Reload through service and verify transparent decryption
    loaded = await load_wp_connection(async_session)
    assert loaded.from_database is True
    assert loaded.wp_base_url == "https://svodka.press"
    assert loaded.wp_app_user == "editorial_bot"
    assert loaded.wp_app_password == "secret-app-password-123"
    assert loaded.ssh_host == "144.31.141.17"
    assert loaded.ssh_port == 2222
    assert "OPENSSH" in loaded.ssh_private_key
    assert loaded.is_active is True


@pytest.mark.asyncio
@respx.mock
async def test_wp_rest_diagnostic_success() -> None:
    respx.get("https://svodka.press/wp-json/wp/v2/users/me").respond(
        status_code=200,
        json={
            "name": "Главный редактор",
            "roles": ["administrator", "editor"],
            "capabilities": {"publish_posts": True},
        },
    )

    config = WPConnectionConfig(
        wp_base_url="https://svodka.press",
        wp_app_user="editor",
        wp_app_password="test-password",
    )

    report = await test_wp_rest(config)
    assert report["ok"] is True
    assert report["status_code"] == 200
    assert report["username"] == "Главный редактор"
    assert report["can_publish"] is True
    assert "administrator" in report["roles"]


@pytest.mark.asyncio
@respx.mock
async def test_wp_rest_diagnostic_auth_failure() -> None:
    respx.get("https://svodka.press/wp-json/wp/v2/users/me").respond(
        status_code=401,
        json={"code": "incorrect_password", "message": "The password is invalid."},
    )

    config = WPConnectionConfig(
        wp_base_url="https://svodka.press",
        wp_app_user="editor",
        wp_app_password="wrong-password",
    )

    report = await test_wp_rest(config)
    assert report["ok"] is False
    assert report["status_code"] == 401
    assert "Ошибка авторизации" in report["message"]


@pytest.mark.asyncio
async def test_wp_ssh_validation_failures() -> None:
    # No host
    report = await test_wp_ssh(WPConnectionConfig())
    assert report["ok"] is False
    assert "хост" in report["message"]

    # Host but no key
    report = await test_wp_ssh(WPConnectionConfig(ssh_host="1.2.3.4"))
    assert report["ok"] is False
    assert "ключ" in report["message"]


@pytest.mark.asyncio
@respx.mock
async def test_sync_wp_categories(async_session: AsyncSession) -> None:
    respx.get("https://svodka.press/wp-json/wp/v2/categories?per_page=100").respond(
        status_code=200,
        json=[
            {
                "id": 101,
                "name": "Политика и власть",
                "slug": "novosti-politiki",
                "count": 42,
            },
            {
                "id": 202,
                "name": "Новая кастомная рубрика",
                "slug": "custom-new-rubric",
                "count": 5,
            },
        ],
    )

    config = WPConnectionConfig(
        wp_base_url="https://svodka.press",
        wp_app_user="editor",
        wp_app_password="test-password",
    )

    orig_map = dict(CATEGORY_MAP)
    try:
        synced = await sync_wp_categories(async_session, config)
        assert len(synced) == 2
        assert CATEGORY_MAP["novosti-politiki"] == 101
        assert CATEGORY_MAP["custom-new-rubric"] == 202

        # Verify persisted in database
        p1 = await async_session.get(CategoryPrompt, "novosti-politiki")
        assert p1 is not None
        assert p1.wp_term_id == 101

        p2 = await async_session.get(CategoryPrompt, "custom-new-rubric")
        assert p2 is not None
        assert p2.wp_term_id == 202
        assert p2.is_custom is True
        assert p2.title == "Новая кастомная рубрика"
    finally:
        CATEGORY_MAP.clear()
        CATEGORY_MAP.update(orig_map)
