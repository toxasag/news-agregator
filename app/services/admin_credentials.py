"""Runtime-editable administrator credentials.

The dashboard's single operator account is bootstrapped from ``ADMIN_USERNAME``
and ``ADMIN_PASSWORD_HASH`` in the environment, but the profile page has to be
able to change both without a redeploy. Two things make the environment unusable
as the live source:

* ``.env`` is handed to the container through Docker's ``env_file``, so the file
  does not exist inside the container — there is nothing to rewrite.
* :class:`~app.config.Settings` is ``frozen=True`` behind an ``lru_cache``, so
  even a successful write would not change the running process until it restarts,
  and a restart drops every session and any in-flight work.

So the credentials live in ``system_settings`` under one key, and the environment
remains the bootstrap default. A deployment with no row behaves exactly as before;
the first password change writes the row and takes effect on the next request.

Only a bcrypt hash is ever stored. The row is read on every authentication rather
than cached, because a cache would let a changed password keep working until it
expired — the opposite of what changing a password is for.
"""

from __future__ import annotations

# The rejection messages are Russian, matching the dashboard the operator
# reads them in. Cyrillic characters resembling Latin ones are the subject
# matter here, not a typo, so RUF001 would flag every message.
# ruff: noqa: RUF001
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import hash_password, verify_password
from app.models.setting import SystemSetting

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "ADMIN_CREDENTIALS_KEY",
    "MIN_PASSWORD_LENGTH",
    "AdminCredentials",
    "CredentialError",
    "authenticate_admin_db",
    "change_password",
    "change_username",
    "load_credentials",
]

#: Single ``system_settings`` key holding the credential document.
ADMIN_CREDENTIALS_KEY: Final[str] = "admin_credentials"

#: Minimum length for a new password set through the dashboard. The login form has
#: no rate limiting, so a short password is brute-forceable once the dashboard is
#: publicly reachable; 8 is the floor, and the UI says longer is better.
MIN_PASSWORD_LENGTH: Final[int] = 8

#: Bound on a username, matching the column width used elsewhere for short names.
MAX_USERNAME_LENGTH: Final[int] = 64


class CredentialError(ValueError):
    """A credential change was rejected. The message is operator-facing."""


@dataclass(frozen=True, slots=True)
class AdminCredentials:
    """The active administrator credentials and where they came from."""

    username: str
    password_hash: str
    #: True when these came from ``system_settings`` rather than the environment.
    #: Surfaced on the profile page so an operator can tell whether a change has
    #: ever been made.
    from_database: bool


def _environment_credentials() -> AdminCredentials:
    """The bootstrap credentials from the environment."""
    return AdminCredentials(
        username=settings.admin_username,
        password_hash=settings.admin_password_hash,
        from_database=False,
    )


async def load_credentials(session: AsyncSession) -> AdminCredentials:
    """Return the active credentials, preferring the database row.

    A malformed or partial row falls back to the environment rather than raising:
    locking the operator out of the dashboard because one JSONB document is
    unexpected would be a far worse outcome than using the bootstrap credentials.
    """
    try:
        result = await session.execute(
            select(SystemSetting.value).where(
                SystemSetting.key == ADMIN_CREDENTIALS_KEY
            )
        )
        stored = result.scalar_one_or_none()
    except SQLAlchemyError:
        logger.warning(
            "Could not read stored admin credentials; using the environment",
            exc_info=True,
        )
        return _environment_credentials()

    if not isinstance(stored, dict):
        return _environment_credentials()

    username = str(stored.get("username") or "").strip()
    password_hash = str(stored.get("password_hash") or "").strip()
    if not username or not password_hash:
        logger.warning(
            "Stored admin credentials are incomplete; using the environment"
        )
        return _environment_credentials()

    return AdminCredentials(
        username=username, password_hash=password_hash, from_database=True
    )


async def _save(
    session: AsyncSession, *, username: str, password_hash: str
) -> None:
    """Upsert the credential document.

    ``ON CONFLICT DO UPDATE`` rather than read-modify-write: two concurrent
    changes would otherwise race, and the loser's write would silently vanish.
    """
    document: dict[str, Any] = {
        "username": username,
        "password_hash": password_hash,
    }
    statement = (
        pg_insert(SystemSetting)
        .values(
            key=ADMIN_CREDENTIALS_KEY,
            value=document,
            description="Dashboard administrator login, editable from /profile.",
        )
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": document},
        )
    )
    await session.execute(statement)
    await session.commit()


async def authenticate_admin_db(
    session: AsyncSession, username: str, password: str
) -> bool:
    """Verify a login against the active credentials.

    Mirrors :func:`app.core.security.authenticate_admin`, including verifying the
    password even when the username is wrong: returning early on an unknown name
    would make the response measurably faster for a wrong name than for a wrong
    password, which tells an attacker when they have found the account.
    """
    credentials = await load_credentials(session)
    user_ok = secrets.compare_digest(
        username.strip().encode("utf-8"), credentials.username.encode("utf-8")
    )
    password_ok = verify_password(password, credentials.password_hash)
    return user_ok and password_ok


async def change_username(
    session: AsyncSession, *, new_username: str, current_password: str
) -> AdminCredentials:
    """Rename the administrator account.

    The current password is required. Without it, a stolen session cookie could
    rename the account, and the rename is what an attacker needs to make the
    real operator's login stop working.
    """
    candidate = new_username.strip()
    if not candidate:
        raise CredentialError("Имя пользователя не может быть пустым.")
    if len(candidate) > MAX_USERNAME_LENGTH:
        raise CredentialError(
            f"Имя пользователя не длиннее {MAX_USERNAME_LENGTH} символов."
        )
    if any(character.isspace() for character in candidate):
        raise CredentialError("Имя пользователя не должно содержать пробелов.")

    credentials = await load_credentials(session)
    if not verify_password(current_password, credentials.password_hash):
        raise CredentialError("Текущий пароль неверен.")

    await _save(
        session, username=candidate, password_hash=credentials.password_hash
    )
    logger.info("Admin username changed from %s to %s", credentials.username, candidate)
    return AdminCredentials(
        username=candidate,
        password_hash=credentials.password_hash,
        from_database=True,
    )


async def change_password(
    session: AsyncSession, *, current_password: str, new_password: str
) -> AdminCredentials:
    """Replace the administrator password.

    Only the bcrypt hash is stored, at the cost factor
    :func:`app.core.security.hash_password` uses, so a value written here verifies
    identically to one generated by the CLI.
    """
    credentials = await load_credentials(session)
    if not verify_password(current_password, credentials.password_hash):
        raise CredentialError("Текущий пароль неверен.")

    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise CredentialError(
            f"Новый пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов."
        )
    if new_password == current_password:
        raise CredentialError("Новый пароль совпадает с текущим.")

    new_hash = hash_password(new_password)
    await _save(session, username=credentials.username, password_hash=new_hash)
    logger.info("Admin password changed for %s", credentials.username)
    return AdminCredentials(
        username=credentials.username, password_hash=new_hash, from_database=True
    )
