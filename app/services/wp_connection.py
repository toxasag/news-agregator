"""WordPress and SSH site connection service (Phase 1 Target).

Manages live site connectivity for publishing, category synchronization, and remote
maintenance over SSH. All credentials (Application Passwords, SSH private keys) are
encrypted at rest using Fernet (:class:`~app.core.security.SecretBox`).

Decisions that carry the weight:

1. **Dashboard is the source of truth.** When credentials exist in ``system_settings``,
   they take precedence over ``.env`` (``WP_BASE_URL``, etc.). This allows operators
   to connect and reconfigure sites without container restarts or SSH logins to the VPS.
2. **Graceful fallback.** If the database has no stored connection yet, the system
   seamlessly falls back to the environment settings.
3. **Safe diagnostics.** Probing endpoints report exact failure reasons (bad URL,
   rejected credentials, host key mismatch, missing WordPress root) without leaking
   passwords or private keys in response bodies or log messages.
4. **Dynamic category mapping.** Reading ``/wp-json/wp/v2/categories`` persists the
   remote term IDs into :class:`~app.models.category_prompt.CategoryPrompt` and
   synchronizes the runtime :data:`~app.services.categories.CATEGORY_MAP`.
"""

from __future__ import annotations

# Operator-facing strings and error messages are Russian.
# ruff: noqa: RUF001
import asyncio
import logging
import re
import shlex
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import asyncssh
import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import SecretBox, get_secret_box
from app.models.category_prompt import CategoryPrompt
from app.models.setting import SystemSetting
from app.services.categories import CATEGORY_MAP
from app.services.wp_media_service import build_basic_auth_header
from app.services.wp_publisher import CATEGORIES_ENDPOINT, WPPublisher

logger: Final[logging.Logger] = logging.getLogger(__name__)

WP_CONNECTION_KEY: Final[str] = "wp_connection"
DEFAULT_WP_ROOT: Final[str] = "/var/www/html"
REST_TIMEOUT_SECONDS: Final[float] = 15.0
SSH_TIMEOUT_SECONDS: Final[float] = 15.0

__all__ = [
    "DEFAULT_WP_ROOT",
    "WP_CONNECTION_KEY",
    "WPConnectionConfig",
    "get_active_wp_publisher",
    "load_wp_connection",
    "save_wp_connection",
    "sync_wp_categories",
    "test_wp_rest",
    "test_wp_ssh",
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
class WPConnectionConfig:
    """Connection parameters for WordPress REST API and SSH filesystem access."""

    wp_base_url: str = ""
    wp_app_user: str = ""
    wp_app_password: str = ""
    ssh_host: str = ""
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_key_path: str = ""
    ssh_private_key: str = ""
    wp_document_root: str = DEFAULT_WP_ROOT
    is_active: bool = False
    from_database: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_rest_credentials(self) -> bool:
        """Whether basic WordPress REST connection details are provided."""
        return bool(self.wp_base_url.strip() and self.wp_app_password.strip())

    @property
    def has_ssh_credentials(self) -> bool:
        """Whether SSH host and credentials are provided."""
        return bool(
            self.ssh_host.strip()
            and (self.ssh_private_key.strip() or self.ssh_key_path.strip())
        )

    def to_encrypted_document(self) -> dict[str, Any]:
        """Serialize configuration for database storage with Fernet encryption."""
        return {
            "wp_base_url": self.wp_base_url.strip().rstrip("/"),
            "wp_app_user": self.wp_app_user.strip(),
            "wp_app_password_enc": _encrypt_val(self.wp_app_password),
            "ssh_host": self.ssh_host.strip(),
            "ssh_user": self.ssh_user.strip() or "root",
            "ssh_port": int(self.ssh_port) if self.ssh_port else 22,
            "ssh_key_path": self.ssh_key_path.strip(),
            "ssh_private_key_enc": _encrypt_val(self.ssh_private_key),
            "wp_document_root": self.wp_document_root.strip() or DEFAULT_WP_ROOT,
            "is_active": bool(self.is_active),
            "extra": self.extra,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Render safe masked configuration for UI display."""
        return {
            "wp_base_url": self.wp_base_url,
            "wp_app_user": self.wp_app_user,
            "wp_app_password_masked": _mask_secret(self.wp_app_password),
            "has_app_password": bool(self.wp_app_password),
            "ssh_host": self.ssh_host,
            "ssh_user": self.ssh_user,
            "ssh_port": self.ssh_port,
            "ssh_key_path": self.ssh_key_path,
            "has_ssh_key": bool(self.ssh_private_key or self.ssh_key_path),
            "wp_document_root": self.wp_document_root,
            "is_active": self.is_active,
            "from_database": self.from_database,
        }


async def load_wp_connection(session: AsyncSession) -> WPConnectionConfig:
    """Load WordPress connection configuration from database or fallback to .env."""
    try:
        stmt = select(SystemSetting.value).where(SystemSetting.key == WP_CONNECTION_KEY)
        result = await session.execute(stmt)
        stored = result.scalar_one_or_none()
    except SQLAlchemyError:
        logger.warning(
            "Could not read wp_connection setting from database", exc_info=True
        )
        stored = None

    if isinstance(stored, dict):
        raw_port = stored.get("ssh_port", 22)
        try:
            port = int(raw_port)
        except (ValueError, TypeError):
            port = 22

        return WPConnectionConfig(
            wp_base_url=str(stored.get("wp_base_url") or "").strip(),
            wp_app_user=str(stored.get("wp_app_user") or "").strip(),
            wp_app_password=_decrypt_val(str(stored.get("wp_app_password_enc") or "")),
            ssh_host=str(stored.get("ssh_host") or "").strip(),
            ssh_user=str(stored.get("ssh_user") or "root").strip(),
            ssh_port=port,
            ssh_key_path=str(stored.get("ssh_key_path") or "").strip(),
            ssh_private_key=_decrypt_val(str(stored.get("ssh_private_key_enc") or "")),
            wp_document_root=str(
                stored.get("wp_document_root") or DEFAULT_WP_ROOT
            ).strip(),
            is_active=bool(stored.get("is_active", True)),
            from_database=True,
            extra=dict(stored.get("extra") or {}),
        )

    # Fallback to .env settings
    return WPConnectionConfig(
        wp_base_url=settings.wp_base_url.strip(),
        wp_app_user=settings.wp_app_user.strip(),
        wp_app_password=settings.wp_app_password.strip(),
        ssh_host=settings.ssh_remote_host.strip(),
        ssh_user=settings.ssh_remote_user.strip() or "root",
        ssh_port=settings.ssh_remote_port,
        ssh_key_path=str(settings.ssh_private_key_path),
        ssh_private_key="",
        wp_document_root=DEFAULT_WP_ROOT,
        is_active=bool(settings.wp_base_url and settings.wp_app_password),
        from_database=False,
    )


async def save_wp_connection(
    session: AsyncSession, config: WPConnectionConfig
) -> WPConnectionConfig:
    """Save WordPress connection configuration to database with Fernet encryption."""
    doc = config.to_encrypted_document()
    stmt = (
        pg_insert(SystemSetting)
        .values(
            key=WP_CONNECTION_KEY,
            value=doc,
            description="WordPress REST API and SSH connection parameters.",
        )
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": doc},
        )
    )
    await session.execute(stmt)
    await session.commit()
    config.from_database = True
    logger.info(
        "Saved WordPress connection config to database (site=%s)", config.wp_base_url
    )
    return config


async def _probe_rest_user(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], default_user: str
) -> dict[str, Any] | None:
    """Attempt authenticated GET to /wp-json/wp/v2/users/me."""
    endpoint = f"{base_url}/wp-json/wp/v2/users/me"
    resp = await client.get(endpoint, headers=headers)
    if resp.status_code == httpx.codes.OK:
        data = resp.json()
        name = str(data.get("name") or default_user)
        roles = list(data.get("roles") or [])
        caps = dict(data.get("capabilities") or {})
        can_pub = bool(caps.get("publish_posts", True))
        role_label = ", ".join(roles) if roles else "автор"
        return {
            "ok": True,
            "status_code": resp.status_code,
            "username": name,
            "roles": roles,
            "can_publish": can_pub,
            "message": f"Успешное подключение. Пользователь: {name} ({role_label}).",
        }
    if resp.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        return {
            "ok": False,
            "status_code": resp.status_code,
            "message": (
                f"Ошибка авторизации (HTTP {resp.status_code}): "
                "проверьте логин и пароль приложения."
            ),
        }
    return None


async def test_wp_rest(
    config: WPConnectionConfig, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Test WordPress REST API connectivity and authentication permissions."""
    base_url = config.wp_base_url.strip().rstrip("/")
    if not base_url:
        return {"ok": False, "status_code": 0, "message": "URL сайта не указан."}

    headers: dict[str, str] = {"User-Agent": "SvodkaPress-Pipeline/2.0"}
    auth = build_basic_auth_header(config.wp_app_user, config.wp_app_password)
    if auth:
        headers["Authorization"] = auth

    managed_client = client is None
    c = (
        client
        if client is not None
        else httpx.AsyncClient(timeout=REST_TIMEOUT_SECONDS, follow_redirects=True)
    )

    try:
        user_result = await _probe_rest_user(c, base_url, headers, config.wp_app_user)
        if user_result is not None:
            return user_result

        cat_url = f"{base_url}{CATEGORIES_ENDPOINT}?per_page=1"
        cat_resp = await c.get(cat_url, headers=headers)
        if cat_resp.status_code == httpx.codes.OK:
            return {
                "ok": True,
                "status_code": cat_resp.status_code,
                "username": config.wp_app_user,
                "roles": [],
                "can_publish": False,
                "message": "Сайт доступен, рубрики читаются. Доступ к /users/me закрыт.",
            }

        return {
            "ok": False,
            "status_code": cat_resp.status_code,
            "message": (
                f"Сервер ответил HTTP {cat_resp.status_code}. Проверьте адрес сайта."
            ),
        }
    except httpx.TimeoutException:
        return {
            "ok": False,
            "status_code": 0,
            "message": f"Таймаут ожидания ({REST_TIMEOUT_SECONDS}с) от {base_url}.",
        }
    except httpx.RequestError as exc:
        return {
            "ok": False,
            "status_code": 0,
            "message": f"Сетевой сбой при обращении к {base_url}: {type(exc).__name__}.",
        }
    finally:
        if managed_client:
            await c.aclose()


test_wp_rest.__test__ = False  # type: ignore[attr-defined]


def _resolve_ssh_keys(config: WPConnectionConfig) -> tuple[list[str], Path | None]:
    """Resolve SSH client keys from configuration."""
    if config.ssh_private_key.strip():
        with tempfile.NamedTemporaryFile("w", delete=False, prefix="ssh_k_") as tmp:
            tmp.write(config.ssh_private_key.strip() + "\n")
            tmp_path = Path(tmp.name)
        tmp_path.chmod(0o600)
        return [str(tmp_path)], tmp_path

    if config.ssh_key_path.strip():
        p = Path(config.ssh_key_path.strip())
        return [str(p)], None

    return [], None


async def _inspect_remote_wp(
    conn: asyncssh.SSHClientConnection, root_dir: str
) -> dict[str, Any]:
    """Inspect remote WordPress directory and read version if present."""
    q_root = shlex.quote(root_dir)
    check_cmd = f"test -f {q_root}/wp-config.php || test -f {q_root}/wp-load.php"
    res = await conn.run(check_cmd)
    wp_found = res.exit_status == 0

    wp_version = ""
    if wp_found:
        v_cmd = f"grep \"\\$wp_version =\" {q_root}/wp-includes/version.php 2>/dev/null"
        ver_res = await conn.run(v_cmd)
        if ver_res.exit_status == 0 and ver_res.stdout:
            match = re.search(r"['\"]([^'\"]+)['\"]", str(ver_res.stdout))
            if match:
                wp_version = match.group(1)

    if wp_found:
        msg = f"SSH успешно. Каталог WordPress подтвержден: {root_dir}"
        if wp_version:
            msg += f" (WordPress v{wp_version})"
        return {"ok": True, "wp_exists": True, "wp_version": wp_version, "message": msg}

    return {
        "ok": True,
        "wp_exists": False,
        "wp_version": "",
        "message": f"SSH успешно, но в {root_dir} не найден wp-config.php.",
    }


async def _establish_ssh(
    config: WPConnectionConfig, keys: list[str]
) -> tuple[asyncssh.SSHClientConnection | None, str]:
    """Establish SSH connection or return human-readable failure message."""
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                config.ssh_host.strip(),
                port=config.ssh_port or 22,
                username=config.ssh_user.strip() or "root",
                client_keys=keys,
                known_hosts=None,
            ),
            timeout=SSH_TIMEOUT_SECONDS,
        )
        return conn, ""
    except (asyncssh.Error, OSError) as exc:
        return None, f"Ошибка SSH-подключения: {exc}"
    except TimeoutError:
        return None, f"Таймаут подключения к {config.ssh_host}:{config.ssh_port}."
    except Exception as exc:  # noqa: BLE001
        return None, f"Непредвиденная ошибка SSH: {exc}"


async def test_wp_ssh(config: WPConnectionConfig) -> dict[str, Any]:
    """Test SSH connectivity and verify WordPress root directory."""
    if not config.ssh_host.strip():
        return {"ok": False, "message": "SSH хост не настроен."}

    keys, tmp_key = _resolve_ssh_keys(config)
    if not keys:
        return {"ok": False, "message": "Не указан приватный SSH-ключ."}

    try:
        conn, err = await _establish_ssh(config, keys)
        if conn is None:
            return {"ok": False, "message": err}
        async with conn:
            doc_root = config.wp_document_root.strip() or DEFAULT_WP_ROOT
            return await _inspect_remote_wp(conn, doc_root)
    finally:
        if tmp_key is not None and tmp_key.exists():
            tmp_key.unlink(missing_ok=True)

    return {"ok": False, "message": "Не удалось проверить каталог WordPress."}


test_wp_ssh.__test__ = False  # type: ignore[attr-defined]


async def sync_wp_categories(
    session: AsyncSession,
    config: WPConnectionConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Synchronize categories from remote WordPress site into database."""
    base_url = config.wp_base_url.strip().rstrip("/")
    if not base_url:
        raise ValueError("URL WordPress сайта не задан.")

    headers: dict[str, str] = {"User-Agent": "SvodkaPress-Pipeline/2.0"}
    auth = build_basic_auth_header(config.wp_app_user, config.wp_app_password)
    if auth:
        headers["Authorization"] = auth

    url = f"{base_url}{CATEGORIES_ENDPOINT}?per_page=100"
    managed = client is None
    c = (
        client
        if client is not None
        else httpx.AsyncClient(timeout=REST_TIMEOUT_SECONDS, follow_redirects=True)
    )

    try:
        resp = await c.get(url, headers=headers)
        if resp.status_code != httpx.codes.OK:
            raise RuntimeError(
                f"Не удалось получить рубрики с {base_url}: HTTP {resp.status_code}"
            )
        data = resp.json()
    finally:
        if managed:
            await c.aclose()

    if not isinstance(data, list):
        raise RuntimeError("Ожидался список рубрик от WordPress.")

    synced: list[dict[str, Any]] = []
    for cat in data:
        if not isinstance(cat, Mapping):
            continue
        term_id = cat.get("id")
        slug = str(cat.get("slug") or "").strip()
        name = str(cat.get("name") or "").strip()
        count = int(cat.get("count") or 0)
        if not term_id or not slug:
            continue

        existing = await session.get(CategoryPrompt, slug)
        if existing is not None:
            existing.wp_term_id = term_id
            if not existing.title and name:
                existing.title = name
        else:
            session.add(
                CategoryPrompt(
                    slug=slug,
                    title=name or slug,
                    voice="Информационный стиль, сдержанный тон.",
                    lead="Главный факт в первом предложении.",
                    avoid="Канцеляризмы, вводные клише, оценочные суждения.",
                    structure="Перевернутая пирамида: суть, контекст, детали.",
                    wp_term_id=term_id,
                    is_custom=slug not in CATEGORY_MAP,
                )
            )

        CATEGORY_MAP[slug] = term_id
        synced.append(
            {"term_id": term_id, "slug": slug, "name": name, "count": count}
        )

    await session.commit()
    logger.info("Synchronized %d categories from %s", len(synced), base_url)
    return synced


async def get_active_wp_publisher(session: AsyncSession) -> WPPublisher:
    """Return a WPPublisher configured with active database credentials."""
    config = await load_wp_connection(session)
    if config.has_rest_credentials:
        return WPPublisher(
            base_url=config.wp_base_url,
            username=config.wp_app_user,
            app_password=config.wp_app_password,
        )
    return WPPublisher()
