"""Server-side Telegram sign-in state for the dashboard.

Phone login spans several HTTP requests (send code → submit code → 2FA) and
must survive gunicorn's two workers, so the in-progress StringSession lives
in Redis, Fernet-encrypted. QR login cannot hop processes: Telethon delivers
``UpdateLoginToken`` on the same live client, so the waiter is an in-process
task. Redis carries the public status the poller reads.

Nothing in Redis is a completed session the pool can use until
:meth:`TelegramAccountPool.store_session` runs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC
from io import BytesIO
from typing import Any, Final, Literal

import segno
from redis.asyncio import Redis

from app.core.security import get_secret_box
from app.redis_client import get_redis
from app.services.telegram_auth import (
    AuthResult,
    TelegramAuthError,
    TelegramAuthFlow,
    TelegramQrLogin,
    TwoFactorRequiredError,
)

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "AUTH_TTL_SECONDS",
    "AuthStatus",
    "PendingAuth",
    "abandon_auth",
    "load_pending",
    "qr_png_data_uri",
    "start_phone_auth",
    "start_qr_auth",
    "submit_pending_code",
    "submit_pending_password",
    "take_completed",
]

AUTH_TTL_SECONDS: Final[int] = 10 * 60
_KEY_PREFIX: Final[str] = "tg-auth:"

Step = Literal["code", "password", "qr", "done", "error"]


@dataclass(slots=True)
class PendingAuth:
    """Operator-facing snapshot. Never carries a session string."""

    login_id: str
    method: Literal["phone", "qr"]
    step: Step
    phone_number: str
    qr_url: str
    qr_png_data_uri: str
    error: str
    used_two_factor: bool
    api_id: int
    api_hash: str
    label: str


@dataclass(slots=True)
class AuthStatus:
    """What the HTMX poller renders."""

    pending: PendingAuth
    result: AuthResult | None = None


def _key(login_id: str) -> str:
    return f"{_KEY_PREFIX}{login_id}"


def _new_id() -> str:
    return secrets.token_urlsafe(16)


def qr_png_data_uri(url: str) -> str:
    """Render ``url`` as a PNG data URI. Empty if segno is missing."""
    if not url:
        return ""
    buffer = BytesIO()
    segno.make(url, error="m").save(buffer, kind="png", scale=6, border=2)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _dump(payload: dict[str, Any]) -> str:
    return get_secret_box().encrypt(json.dumps(payload, separators=(",", ":")))


def _load(raw: str) -> dict[str, Any]:
    plain = get_secret_box().decrypt(raw)
    if not plain:
        raise TelegramAuthError("the sign-in session could not be read; start again")
    data = json.loads(plain)
    if not isinstance(data, dict):
        raise TelegramAuthError("the sign-in session is corrupt; start again")
    return data


async def _write(redis: Redis, login_id: str, payload: dict[str, Any]) -> None:
    await redis.set(_key(login_id), _dump(payload), ex=AUTH_TTL_SECONDS)


def _to_pending(login_id: str, data: dict[str, Any]) -> PendingAuth:
    qr_url = str(data.get("qr_url") or "")
    step_raw = str(data.get("step") or "error")
    step: Step
    if step_raw in {"code", "password", "qr", "done", "error"}:
        step = step_raw  # type: ignore[assignment]
    else:
        step = "error"
    return PendingAuth(
        login_id=login_id,
        method="qr" if data.get("method") == "qr" else "phone",
        step=step,
        phone_number=str(data.get("phone_number") or ""),
        qr_url=qr_url,
        qr_png_data_uri=qr_png_data_uri(qr_url),
        error=str(data.get("error") or ""),
        used_two_factor=bool(data.get("used_two_factor")),
        api_id=int(data.get("api_id") or 0),
        api_hash=str(data.get("api_hash") or ""),
        label=str(data.get("label") or ""),
    )


async def load_pending(login_id: str, *, redis: Redis | None = None) -> PendingAuth:
    client = get_redis() if redis is None else redis
    raw = await client.get(_key(login_id))
    if not raw:
        raise TelegramAuthError("this sign-in expired; start again")
    return _to_pending(login_id, _load(str(raw)))


def _session_plain(data: dict[str, Any]) -> str:
    cipher = str(data.get("session") or "")
    if not cipher:
        raise TelegramAuthError("the sign-in session could not be read; start again")
    plain = get_secret_box().decrypt(cipher)
    if not plain:
        raise TelegramAuthError("the sign-in session could not be read; start again")
    return plain


async def start_phone_auth(
    *,
    phone_number: str,
    api_id: int,
    api_hash: str,
    label: str = "",
    redis: Redis | None = None,
) -> PendingAuth:
    """Send a login code and stash the in-progress session."""
    client = get_redis() if redis is None else redis
    login_id = _new_id()
    async with TelegramAuthFlow(phone_number, api_id, api_hash) as flow:
        await flow.send_code()
        payload = {
            "method": "phone",
            "step": "code",
            "phone_number": flow.phone_number,
            "api_id": api_id,
            "api_hash": api_hash,
            "label": label,
            "session": get_secret_box().encrypt(flow.export_session()),
            "phone_code_hash": flow.phone_code_hash,
            "used_two_factor": False,
        }
    await _write(client, login_id, payload)
    return _to_pending(login_id, payload)


async def submit_pending_code(
    login_id: str,
    code: str,
    *,
    redis: Redis | None = None,
) -> AuthStatus:
    client = get_redis() if redis is None else redis
    raw = await client.get(_key(login_id))
    if not raw:
        raise TelegramAuthError("this sign-in expired; start again")
    data = _load(str(raw))
    if data.get("method") != "phone" or data.get("step") != "code":
        raise TelegramAuthError("this sign-in is not waiting for a code")
    flow = TelegramAuthFlow.resume(
        str(data["phone_number"]),
        int(data["api_id"]),
        str(data["api_hash"]),
        session_string=_session_plain(data),
        phone_code_hash=str(data.get("phone_code_hash") or "") or None,
    )
    async with flow:
        try:
            result = await flow.submit_code(code)
        except TwoFactorRequiredError:
            data["step"] = "password"
            data["session"] = get_secret_box().encrypt(flow.export_session())
            data["used_two_factor"] = True
            await _write(client, login_id, data)
            return AuthStatus(pending=_to_pending(login_id, data))
        data["step"] = "done"
        data["session"] = ""
        await _write(client, login_id, data)
        return AuthStatus(pending=_to_pending(login_id, data), result=result)


_qr_clients: dict[str, TelegramQrLogin] = {}
_qr_waiters: dict[str, asyncio.Task[None]] = {}


async def start_qr_auth(
    *,
    api_id: int,
    api_hash: str,
    label: str = "",
    redis: Redis | None = None,
) -> PendingAuth:
    """Export a QR token and wait for the scan on this worker."""
    client = get_redis() if redis is None else redis
    login_id = _new_id()
    qr = TelegramQrLogin(api_id, api_hash)
    await qr.connect()
    url, expires = await qr.start()
    payload: dict[str, Any] = {
        "method": "qr",
        "step": "qr",
        "phone_number": "",
        "api_id": api_id,
        "api_hash": api_hash,
        "label": label,
        "session": get_secret_box().encrypt(qr.export_session()),
        "qr_url": url,
        "expires": expires.astimezone(UTC).isoformat(),
        "used_two_factor": False,
    }
    await _write(client, login_id, payload)
    _qr_clients[login_id] = qr
    _qr_waiters[login_id] = asyncio.create_task(
        _wait_qr(login_id, qr, client), name=f"tg-qr-{login_id}"
    )
    return _to_pending(login_id, payload)


async def _wait_qr(login_id: str, qr: TelegramQrLogin, redis: Redis) -> None:
    """Background waiter. Keeps the client open when 2FA is still required."""
    try:
        result = await qr.wait()
        payload = {
            "method": "qr",
            "step": "done",
            "phone_number": result.phone_number,
            "api_id": 0,
            "api_hash": "",
            "label": "",
            "session": get_secret_box().encrypt(result.session_string),
            "qr_url": "",
            "used_two_factor": result.used_two_factor,
            "user_id": result.user_id,
            "username": result.username,
            "first_name": result.first_name,
        }
        # Preserve api credentials from the existing record.
        raw = await redis.get(_key(login_id))
        if raw:
            existing = _load(str(raw))
            payload["api_id"] = existing.get("api_id") or 0
            payload["api_hash"] = existing.get("api_hash") or ""
            payload["label"] = existing.get("label") or ""
        await _write(redis, login_id, payload)
        await qr.close()
        _qr_clients.pop(login_id, None)
    except TwoFactorRequiredError:
        raw = await redis.get(_key(login_id))
        data: dict[str, Any] = _load(str(raw)) if raw else {"method": "qr"}
        data["method"] = "qr"
        data["step"] = "password"
        data["session"] = get_secret_box().encrypt(qr.export_session())
        data["used_two_factor"] = True
        await _write(redis, login_id, data)
        # Leave `_qr_clients[login_id]` connected for submit_password.
    except TelegramAuthError as error:
        raw = await redis.get(_key(login_id))
        data = _load(str(raw)) if raw else {"method": "qr"}
        data["step"] = "error"
        data["error"] = str(error)
        data["session"] = ""
        await _write(redis, login_id, data)
        await qr.close()
        _qr_clients.pop(login_id, None)
    except Exception:
        logger.exception("QR login waiter failed")
        await _write(
            redis,
            login_id,
            {
                "method": "qr",
                "step": "error",
                "error": "QR login failed unexpectedly; start again",
                "session": "",
            },
        )
        await qr.close()
        _qr_clients.pop(login_id, None)
    finally:
        _qr_waiters.pop(login_id, None)


async def submit_pending_password(
    login_id: str,
    password: str,
    *,
    redis: Redis | None = None,
) -> AuthStatus:
    client = get_redis() if redis is None else redis
    raw = await client.get(_key(login_id))
    if not raw:
        raise TelegramAuthError("this sign-in expired; start again")
    data = _load(str(raw))
    if data.get("step") != "password":
        raise TelegramAuthError("this sign-in is not waiting for a password")

    live = _qr_clients.get(login_id)
    if live is not None:
        result = await live.submit_password(password)
        await live.close()
        _qr_clients.pop(login_id, None)
        data["step"] = "done"
        data["phone_number"] = result.phone_number
        data["session"] = ""
        data["user_id"] = result.user_id
        data["username"] = result.username
        data["first_name"] = result.first_name
        await _write(client, login_id, data)
        return AuthStatus(pending=_to_pending(login_id, data), result=result)

    if data.get("method") == "qr":
        qr = TelegramQrLogin(
            int(data["api_id"]), str(data["api_hash"]), _session_plain(data)
        )
        async with qr:
            result = await qr.submit_password(password)
        data["step"] = "done"
        data["phone_number"] = result.phone_number
        data["session"] = ""
        await _write(client, login_id, data)
        return AuthStatus(pending=_to_pending(login_id, data), result=result)

    flow = TelegramAuthFlow.resume(
        str(data["phone_number"]),
        int(data["api_id"]),
        str(data["api_hash"]),
        session_string=_session_plain(data),
        phone_code_hash=str(data.get("phone_code_hash") or "") or None,
        used_two_factor=True,
    )
    async with flow:
        result = await flow.submit_password(password)
    data["step"] = "done"
    data["session"] = ""
    await _write(client, login_id, data)
    return AuthStatus(pending=_to_pending(login_id, data), result=result)


async def take_completed(
    login_id: str, *, redis: Redis | None = None
) -> AuthStatus:
    """Return a completed AuthResult once, then wipe the session blob."""
    client = get_redis() if redis is None else redis
    raw = await client.get(_key(login_id))
    if not raw:
        raise TelegramAuthError("this sign-in expired; start again")
    data = _load(str(raw))
    pending = _to_pending(login_id, data)
    if pending.step != "done":
        return AuthStatus(pending=pending)
    cipher = str(data.get("session") or "")
    if not cipher:
        return AuthStatus(pending=pending)
    session_plain = get_secret_box().decrypt(cipher)
    if not session_plain:
        return AuthStatus(pending=pending)
    result = AuthResult(
        phone_number=str(data.get("phone_number") or ""),
        session_string=session_plain,
        user_id=int(data.get("user_id") or 0),
        username=data.get("username") if isinstance(data.get("username"), str) else None,
        first_name=str(data.get("first_name") or ""),
        used_two_factor=bool(data.get("used_two_factor")),
    )
    data["session"] = ""
    await _write(client, login_id, data)
    return AuthStatus(pending=_to_pending(login_id, data), result=result)


async def abandon_auth(login_id: str, *, redis: Redis | None = None) -> None:
    client = get_redis() if redis is None else redis
    waiter = _qr_waiters.pop(login_id, None)
    if waiter is not None:
        waiter.cancel()
    live = _qr_clients.pop(login_id, None)
    if live is not None:
        await live.close()
    await client.delete(_key(login_id))
