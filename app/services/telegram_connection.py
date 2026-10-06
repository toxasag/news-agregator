"""Telegram publishing bot and channel connection management.

Provides:
- Encrypted storage for Telegram Bot Token via Fernet
- Dynamic loading from database with .env fallback
- Live diagnostics: verify bot credentials and channel administrator permissions
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
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

TELEGRAM_CHANNEL_CONFIG_KEY: Final[str] = "telegram_channel_config"
DEFAULT_TEST_TIMEOUT: Final[float] = 12.0

__all__ = [
    "TELEGRAM_CHANNEL_CONFIG_KEY",
    "TelegramChannelConfig",
    "load_telegram_channel_config",
    "probe_telegram_bot_connection",
    "save_telegram_channel_config",
]


def _get_box() -> SecretBox | None:
    try:
        return get_secret_box()
    except (ValueError, TypeError):
        return None


def _encrypt_val(value: str) -> str:
    if not value:
        return ""
    box = _get_box()
    if box is not None:
        return box.encrypt(value)
    return value


def _decrypt_val(value: str) -> str:
    if not value:
        return ""
    box = _get_box()
    if box is not None:
        return box.decrypt(value)
    return value


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}••••••••{value[-4:]}"


@dataclass(slots=True)
class TelegramChannelConfig:
    """Configuration for automated Telegram channel distribution."""

    bot_token: str = ""
    channel_id: str = ""
    is_active: bool = True
    status: str = "not_configured"  # not_configured | active | error
    error_message: str | None = None
    bot_username: str = ""
    channel_title: str = ""
    last_tested_at: str | None = None

    @property
    def has_token(self) -> bool:
        return bool(self.bot_token.strip())

    @property
    def has_channel(self) -> bool:
        return bool(self.channel_id.strip())

    @property
    def is_ready(self) -> bool:
        return self.is_active and self.has_token and self.has_channel

    def masked_token(self) -> str:
        return _mask_secret(self.bot_token)

    def to_dict(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        return {
            "bot_token": self.masked_token() if mask_secrets else self.bot_token,
            "channel_id": self.channel_id,
            "is_active": self.is_active,
            "status": self.status,
            "error_message": self.error_message,
            "bot_username": self.bot_username,
            "channel_title": self.channel_title,
            "last_tested_at": self.last_tested_at,
            "has_token": self.has_token,
            "has_channel": self.has_channel,
            "is_ready": self.is_ready,
        }


async def load_telegram_channel_config(session: AsyncSession) -> TelegramChannelConfig:
    """Load Telegram channel credentials from DB with .env fallback."""
    cfg = TelegramChannelConfig(
        bot_token=settings.telegram_bot_token.strip(),
        channel_id=settings.telegram_channel_id.strip(),
        is_active=True,
    )

    try:
        stmt = select(SystemSetting.value).where(
            SystemSetting.key == TELEGRAM_CHANNEL_CONFIG_KEY
        )
        result = await session.execute(stmt)
        val = result.scalar_one_or_none()
        if isinstance(val, dict):
            raw_token = str(val.get("encrypted_bot_token") or "")
            if raw_token:
                decrypted = _decrypt_val(raw_token)
                if decrypted:
                    cfg.bot_token = decrypted
            elif val.get("bot_token"):
                cfg.bot_token = str(val["bot_token"]).strip()

            if val.get("channel_id"):
                cfg.channel_id = str(val["channel_id"]).strip()

            if "is_active" in val:
                cfg.is_active = bool(val["is_active"])

            cfg.status = str(val.get("status") or ("active" if cfg.has_token else "not_configured"))
            cfg.error_message = val.get("error_message")
            cfg.bot_username = str(val.get("bot_username") or "")
            cfg.channel_title = str(val.get("channel_title") or "")
            cfg.last_tested_at = val.get("last_tested_at")
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("Failed to load Telegram channel config from DB: %s", exc)

    if not cfg.has_token:
        cfg.status = "not_configured"
    elif cfg.status == "not_configured":
        cfg.status = "active"

    return cfg


async def save_telegram_channel_config(
    session: AsyncSession,
    config: TelegramChannelConfig,
) -> None:
    """Save Telegram channel configuration to DB with encrypted token."""
    doc: dict[str, Any] = {
        "encrypted_bot_token": _encrypt_val(config.bot_token) if config.bot_token else "",
        "channel_id": config.channel_id,
        "is_active": config.is_active,
        "status": config.status,
        "error_message": config.error_message,
        "bot_username": config.bot_username,
        "channel_title": config.channel_title,
        "last_tested_at": config.last_tested_at,
    }

    stmt = (
        pg_insert(SystemSetting)
        .values(
            key=TELEGRAM_CHANNEL_CONFIG_KEY,
            value=doc,
            description="Telegram Channel Publishing Bot Configuration",
        )
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": doc, "updated_at": datetime.now(UTC)},
        )
    )
    await session.execute(stmt)
    await session.commit()


async def probe_telegram_bot_connection(
    bot_token: str, channel_id: str = ""
) -> dict[str, Any]:
    """Test bot credentials via Telegram Bot API and verify channel rights."""
    token = bot_token.strip()
    channel = channel_id.strip()

    if not token:
        return {
            "valid": False,
            "error": "Токен бота не указан. Получите токен у @BotFather в Telegram.",
            "status": "not_configured",
        }

    async with httpx.AsyncClient(timeout=DEFAULT_TEST_TIMEOUT) as client:
        # 1. Test getMe
        try:
            r1 = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            d1 = r1.json()
        except Exception as exc:
            return {
                "valid": False,
                "error": f"Ошибка соединения с Telegram API: {exc}",
                "status": "error",
            }

        if not d1.get("ok"):
            desc = d1.get("description", "Неверный токен бота")
            return {
                "valid": False,
                "error": f"Telegram отклонил токен бота: {desc}",
                "status": "error",
            }

        bot_info = d1.get("result", {})
        bot_id = bot_info.get("id")
        bot_username = bot_info.get("username", "")
        bot_first_name = bot_info.get("first_name", "")

        result: dict[str, Any] = {
            "valid": True,
            "bot_id": bot_id,
            "bot_username": bot_username,
            "bot_first_name": bot_first_name,
            "status": "active",
        }

        # 2. Test channel access if channel_id provided
        if channel:
            if not channel.startswith("@") and not channel.startswith("-100") and not channel.lstrip("-").isdigit():
                channel = f"@{channel}"
                result["normalised_channel_id"] = channel

            try:
                r2 = await client.get(
                    f"https://api.telegram.org/bot{token}/getChat",
                    params={"chat_id": channel},
                )
                d2 = r2.json()
            except Exception as exc:
                result["warning"] = f"Не удалось проверить канал {channel}: {exc}"
                return result

            if not d2.get("ok"):
                desc2 = d2.get("description", "")
                result["valid"] = False
                result["status"] = "error"
                result["error"] = (
                    f"Бот @{bot_username} не может получить доступ к каналу {channel}: {desc2}. "
                    "Убедитесь, что канал существует и бот добавлен в него как администратор."
                )
                return result

            chat_info = d2.get("result", {})
            channel_title = chat_info.get("title", "")
            result["channel_title"] = channel_title
            result["channel_id"] = str(chat_info.get("id") or channel)

            # 3. Test administrator rights
            try:
                r3 = await client.get(
                    f"https://api.telegram.org/bot{token}/getChatMember",
                    params={"chat_id": channel, "user_id": bot_id},
                )
                d3 = r3.json()
                if d3.get("ok"):
                    member = d3.get("result", {})
                    member_status = member.get("status", "")
                    if member_status not in ("administrator", "creator"):
                        result["valid"] = False
                        result["status"] = "error"
                        result["error"] = (
                            f"Бот @{bot_username} добавлен в канал «{channel_title}», "
                            f"но имеет статус «{member_status}». Назначьте бота администратором канала с правом публикации сообщений!"
                        )
                        return result

                    can_post = member.get("can_post_messages", True)
                    if not can_post:
                        result["valid"] = False
                        result["status"] = "error"
                        result["error"] = (
                            f"Бот @{bot_username} является администратором «{channel_title}», "
                            "но у него отключено право «Публикация сообщений» (can_post_messages)!"
                        )
                        return result

                    result["is_admin"] = True
                    result["message"] = f"Бот @{bot_username} успешно авторизован с правами публикации в канале «{channel_title}»!"
            except Exception as exc:
                logger.warning("getChatMember check failed: %s", exc)

        return result
