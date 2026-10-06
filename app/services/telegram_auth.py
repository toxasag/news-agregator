"""Telethon sign-in flow for Telegram ingestion accounts.

Sign-in is a three-step conversation with Telegram, and each step can fail in a
way the operator must act on differently:

1. ``send_code`` asks Telegram to deliver a login code, returning the
   ``phone_code_hash`` that ties the next step to this attempt.
2. ``submit_code`` sends the code. Telegram answers with the signed-in user, or
   raises :class:`~telethon.errors.SessionPasswordNeededError` when the account
   has two-factor authentication enabled.
3. ``submit_password`` completes 2FA.

The flow is a class rather than one function because it spans several user
interactions with a live connection in between: the ``phone_code_hash`` and the
connected client have to survive from step to step, and a resumable object is the
honest shape for that. The CLI drives it interactively; a future dashboard action
can drive the same object.

**Nothing here logs a code, a password or a session string.** Each is a full
credential, and a log line is the easiest place to leak one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Final, Self

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from app.services.telegram_accounts import normalise_phone

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "AuthResult",
    "TelegramAuthError",
    "TelegramAuthFlow",
    "TelegramQrLogin",
    "TwoFactorRequiredError",
    "build_client",
]

#: Device fields sent at sign-in. Telegram shows these in the account's active
#: sessions list, so a recognisable name lets the owner tell this session apart
#: from their phone and revoke the right one.
DEVICE_MODEL: Final[str] = "SvodkaPress Ingestion"
SYSTEM_VERSION: Final[str] = "Linux"
APP_VERSION: Final[str] = "2.1.0"

#: Connection budget. Telethon retries internally; this bounds the whole attempt
#: so an unreachable data centre fails the CLI instead of hanging it.
CONNECT_RETRIES: Final[int] = 3
CONNECT_TIMEOUT_SECONDS: Final[int] = 20


class TelegramAuthError(RuntimeError):
    """Sign-in could not be completed. The message is operator-facing."""


class TwoFactorRequiredError(TelegramAuthError):
    """The account has 2FA enabled and the cloud password is required.

    A distinct type rather than a flag on the result, so a caller that forgets to
    handle it fails loudly instead of storing a session that was never completed.
    """


@dataclass(frozen=True, slots=True)
class AuthResult:
    """A completed sign-in."""

    phone_number: str
    session_string: str
    user_id: int
    username: str | None
    first_name: str
    #: Whether the 2FA password was needed. Recorded so the CLI can report which
    #: path completed, and so a test can assert the fallback actually ran.
    used_two_factor: bool

    @property
    def display_name(self) -> str:
        return f"@{self.username}" if self.username else self.first_name


def build_client(
    api_id: int, api_hash: str, session_string: str = ""
) -> TelegramClient:
    """Open an in-memory Telethon client. Empty session is a fresh sign-in."""
    return TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        device_model=DEVICE_MODEL,
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
        connection_retries=CONNECT_RETRIES,
        timeout=CONNECT_TIMEOUT_SECONDS,
    )


class TelegramAuthFlow:
    """Drives one interactive sign-in against Telegram.

    Use as an async context manager so the connection is always closed, including
    when the operator abandons the flow half-way:

        async with TelegramAuthFlow(phone, api_id, api_hash) as flow:
            await flow.send_code()
            try:
                result = await flow.submit_code(code)
            except TwoFactorRequiredError:
                result = await flow.submit_password(password)
    """

    __slots__ = (
        "_api_hash",
        "_api_id",
        "_client",
        "_phone",
        "_phone_code_hash",
        "_used_two_factor",
    )

    def __init__(self, phone_number: str, api_id: int, api_hash: str) -> None:
        self._phone = normalise_phone(phone_number)
        self._api_id = api_id
        self._api_hash = api_hash
        # An empty StringSession keeps the whole flow in memory: Telethon's
        # default writes a .session SQLite file to the working directory, which
        # would leave an unencrypted credential on disk next to the code.
        self._client = build_client(api_id, api_hash)
        self._phone_code_hash: str | None = None
        self._used_two_factor = False

    @classmethod
    def resume(
        cls,
        phone_number: str,
        api_id: int,
        api_hash: str,
        *,
        session_string: str,
        phone_code_hash: str | None,
        used_two_factor: bool = False,
    ) -> TelegramAuthFlow:
        """Rebuild a flow across HTTP requests from the exported session.

        Dashboard workers do not share memory. The phone-code hash and the
        in-progress StringSession have to travel through Redis; this constructor
        is how the next request picks them up without asking Telegram for a
        new code.
        """
        flow = cls(phone_number, api_id, api_hash)
        flow._client = build_client(api_id, api_hash, session_string)
        flow._phone_code_hash = phone_code_hash
        flow._used_two_factor = used_two_factor
        return flow

    def export_session(self) -> str:
        """Snapshot the in-memory StringSession for Redis.

        Empty is a failure: a lost session cannot complete sign-in.
        """
        session = self._client.session
        if not isinstance(session, StringSession):  # pragma: no cover
            raise TelegramAuthError("the session is not a StringSession")
        exported = str(session.save() or "")
        if not exported:
            raise TelegramAuthError("Telegram returned an empty session string")
        return exported

    @property
    def phone_code_hash(self) -> str | None:
        return self._phone_code_hash

    @property
    def phone_number(self) -> str:
        return self._phone

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the MTProto connection."""
        try:
            await self._client.connect()
        except ApiIdInvalidError as error:
            raise TelegramAuthError(
                "Telegram rejected the api_id/api_hash pair. Check both at "
                "https://my.telegram.org/apps"
            ) from error
        except OSError as error:
            raise TelegramAuthError(
                f"could not reach Telegram: {type(error).__name__}"
            ) from error

    async def close(self) -> None:
        """Disconnect, ignoring an already-closed connection."""
        try:
            await self._client.disconnect()
        # Deliberately broad: teardown runs while an earlier error may already be
        # propagating, and a disconnect failure must not replace it.
        except Exception:
            logger.debug("Telegram client disconnect failed", exc_info=True)

    async def send_code(self) -> None:
        """Ask Telegram to send a login code to the phone.

        The returned ``phone_code_hash`` is kept for :meth:`submit_code`: it binds
        the code to this attempt, and Telegram rejects a code submitted without
        the hash from the request that produced it.
        """
        try:
            sent = await self._client.send_code_request(self._phone)
        except PhoneNumberInvalidError as error:
            raise TelegramAuthError(
                f"Telegram does not recognise {self._phone} as a valid number"
            ) from error
        except FloodWaitError as error:
            raise TelegramAuthError(
                f"Telegram is rate limiting sign-in for this number: retry in "
                f"{error.seconds}s"
            ) from error
        self._phone_code_hash = sent.phone_code_hash
        logger.info("Login code requested for %s", self._phone)

    async def submit_code(self, code: str) -> AuthResult:
        """Submit the login code.

        Raises :class:`TwoFactorRequiredError` when the account has a cloud
        password, which is not a failure but the next step.
        """
        if self._phone_code_hash is None:
            raise TelegramAuthError("send_code() must be called before submit_code()")

        cleaned = "".join(character for character in code if character.isdigit())
        if not cleaned:
            raise TelegramAuthError("the login code contains no digits")

        try:
            await self._client.sign_in(
                phone=self._phone,
                code=cleaned,
                phone_code_hash=self._phone_code_hash,
            )
        except SessionPasswordNeededError as error:
            self._used_two_factor = True
            raise TwoFactorRequiredError(
                "this account has two-factor authentication enabled; "
                "the cloud password is required"
            ) from error
        except PhoneCodeInvalidError as error:
            raise TelegramAuthError("that login code is not correct") from error
        except PhoneCodeExpiredError as error:
            raise TelegramAuthError(
                "that login code has expired; request a new one"
            ) from error
        except FloodWaitError as error:
            raise TelegramAuthError(
                f"too many sign-in attempts: retry in {error.seconds}s"
            ) from error

        return await self._finish()

    async def submit_password(self, password: str) -> AuthResult:
        """Complete 2FA with the account's cloud password."""
        if not password:
            raise TelegramAuthError("the two-factor password is empty")
        try:
            await self._client.sign_in(password=password)
        except PasswordHashInvalidError as error:
            raise TelegramAuthError(
                "that two-factor password is not correct"
            ) from error
        except FloodWaitError as error:
            raise TelegramAuthError(
                f"too many password attempts: retry in {error.seconds}s"
            ) from error
        self._used_two_factor = True
        return await self._finish()

    async def _finish(self) -> AuthResult:
        """Confirm authorisation and export the session string."""
        if not await self._client.is_user_authorized():
            raise TelegramAuthError(
                "Telegram accepted the credentials but the session is not "
                "authorised; retry the sign-in"
            )
        me = await self._client.get_me()
        if me is None:
            raise TelegramAuthError("Telegram returned no account for this session")

        session = self._client.session
        if not isinstance(session, StringSession):  # pragma: no cover - defensive
            raise TelegramAuthError("the session is not a StringSession")
        session_string = session.save()
        if not session_string:
            raise TelegramAuthError("Telegram returned an empty session string")

        logger.info(
            "Signed in as user %s (2FA=%s)", me.id, self._used_two_factor
        )
        return AuthResult(
            phone_number=self._phone,
            session_string=session_string,
            user_id=int(me.id),
            username=getattr(me, "username", None),
            first_name=getattr(me, "first_name", "") or "",
            used_two_factor=self._used_two_factor,
        )


class TelegramQrLogin:
    """QR sign-in. ``wait`` must run while the code is on screen.

    Telethon delivers completion as an ``UpdateLoginToken`` on the same live
    client that exported the token, so the waiter cannot hop to another
    process. The dashboard keeps this object in the worker that started it
    and only puts the resulting session string into Redis.
    """

    __slots__ = ("_api_hash", "_api_id", "_client", "_qr", "_used_two_factor")

    def __init__(self, api_id: int, api_hash: str, session_string: str = "") -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._client = build_client(api_id, api_hash, session_string)
        self._qr: object | None = None
        self._used_two_factor = False

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            await self._client.connect()
        except ApiIdInvalidError as error:
            raise TelegramAuthError(
                "Telegram rejected the api_id/api_hash pair. Check both at "
                "https://my.telegram.org/apps"
            ) from error
        except OSError as error:
            raise TelegramAuthError(
                f"could not reach Telegram: {type(error).__name__}"
            ) from error

    async def close(self) -> None:
        try:
            await self._client.disconnect()
        except Exception:
            logger.debug("Telegram QR client disconnect failed", exc_info=True)

    async def start(self) -> tuple[str, datetime]:
        """Export a QR login token. Returns ``(tg:// url, expiry)``."""
        try:
            qr = await self._client.qr_login()
        except FloodWaitError as error:
            raise TelegramAuthError(
                f"Telegram is rate limiting QR login: retry in {error.seconds}s"
            ) from error
        self._qr = qr
        url = str(getattr(qr, "url", "") or "")
        if not url:
            raise TelegramAuthError("Telegram returned an empty QR login URL")
        expires = getattr(qr, "expires", None)
        if not isinstance(expires, datetime):
            raise TelegramAuthError("Telegram returned no QR expiry")
        logger.info("QR login token exported, expires %s", expires.isoformat())
        return url, expires

    async def wait(self, seconds: float | None = None) -> AuthResult:
        """Block until the QR is scanned or ``seconds`` elapses."""
        if self._qr is None:
            raise TelegramAuthError("start() must be called before wait()")
        timeout = seconds
        wait = getattr(self._qr, "wait")  # noqa: B009 - typed as object
        try:
            await wait(timeout=timeout)
        except SessionPasswordNeededError as error:
            self._used_two_factor = True
            raise TwoFactorRequiredError(
                "this account has two-factor authentication enabled; "
                "the cloud password is required"
            ) from error
        except TimeoutError as error:
            raise TelegramAuthError(
                "the QR code expired before it was scanned; request a new one"
            ) from error
        except FloodWaitError as error:
            raise TelegramAuthError(
                f"too many QR login attempts: retry in {error.seconds}s"
            ) from error
        return await self._finish()

    async def submit_password(self, password: str) -> AuthResult:
        if not password:
            raise TelegramAuthError("the two-factor password is empty")
        try:
            await self._client.sign_in(password=password)
        except PasswordHashInvalidError as error:
            raise TelegramAuthError(
                "that two-factor password is not correct"
            ) from error
        except FloodWaitError as error:
            raise TelegramAuthError(
                f"too many password attempts: retry in {error.seconds}s"
            ) from error
        self._used_two_factor = True
        return await self._finish()

    def export_session(self) -> str:
        session = self._client.session
        if not isinstance(session, StringSession):  # pragma: no cover
            raise TelegramAuthError("the session is not a StringSession")
        exported = str(session.save() or "")
        if not exported:
            raise TelegramAuthError("Telegram returned an empty session string")
        return exported

    async def _finish(self) -> AuthResult:
        if not await self._client.is_user_authorized():
            raise TelegramAuthError(
                "Telegram accepted the credentials but the session is not "
                "authorised; retry the sign-in"
            )
        me = await self._client.get_me()
        if me is None:
            raise TelegramAuthError("Telegram returned no account for this session")
        phone = getattr(me, "phone", None)
        if not phone:
            raise TelegramAuthError(
                "Telegram returned an account with no phone number; "
                "cannot store it in the pool"
            )
        logger.info("QR signed in as user %s (2FA=%s)", me.id, self._used_two_factor)
        return AuthResult(
            phone_number=normalise_phone(str(phone)),
            session_string=self.export_session(),
            user_id=int(me.id),
            username=getattr(me, "username", None),
            first_name=getattr(me, "first_name", "") or "",
            used_two_factor=self._used_two_factor,
        )
