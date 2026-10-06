#!/usr/bin/env python
"""Interactive sign-in for a Telegram ingestion account.

Walks one account through Telegram's login: phone number, delivered code, and the
cloud password when two-factor authentication is enabled. On success the Telethon
``StringSession`` is Fernet-encrypted and stored in ``telegram_accounts``, so the
ingestion pool can use the account without ever seeing the plaintext.

    python -m scripts.auth_telegram                       # prompts for everything
    python -m scripts.auth_telegram --phone +79055342403 \
        --api-id 31332284 --api-hash <hash>               # non-interactive details
    python -m scripts.auth_telegram --list                # show configured accounts

Adding a second account is the same command with a different phone: accounts are
upserted by number, so re-running it for an existing account refreshes that
account's session instead of creating a duplicate that would be rotated as a
separate rate-limit budget.

The code and the 2FA password are read with ``getpass`` where possible and are
never logged, never echoed back and never written to disk. The session string is
printed only under ``--print-session``, which exists for the case where the
database is unreachable and the operator needs to keep the result.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from app.config import settings
from app.database import AsyncSessionLocal, dispose_engine
from app.services.telegram_accounts import (
    AccountState,
    TelegramAccountPool,
    account_state,
    normalise_phone,
)
from app.services.telegram_auth import (
    AuthResult,
    TelegramAuthError,
    TelegramAuthFlow,
    TwoFactorRequiredError,
)

logger: Final[logging.Logger] = logging.getLogger("scripts.auth_telegram")

#: Attempts allowed for the login code. Telegram itself rate-limits, and three
#: tries is enough for a mistyped digit without walking into a flood wait.
MAX_CODE_ATTEMPTS: Final[int] = 3
MAX_PASSWORD_ATTEMPTS: Final[int] = 3

_CYAN: Final[str] = "\033[0;36m"
_GREEN: Final[str] = "\033[0;32m"
_RED: Final[str] = "\033[0;31m"
_YELLOW: Final[str] = "\033[0;33m"
_RESET: Final[str] = "\033[0m"


def _say(message: str) -> None:
    sys.stdout.write(f"{_CYAN}[telegram]{_RESET} {message}\n")
    sys.stdout.flush()


def _ok(message: str) -> None:
    sys.stdout.write(f"{_GREEN}  ok{_RESET}    {message}\n")
    sys.stdout.flush()


def _warn(message: str) -> None:
    sys.stderr.write(f"{_YELLOW}  warn{_RESET}  {message}\n")
    sys.stderr.flush()


def _fail(message: str) -> None:
    sys.stderr.write(f"{_RED}  fail{_RESET}  {message}\n")
    sys.stderr.flush()


def _prompt(label: str, *, default: str = "") -> str:
    """Read a visible value, offering a default."""
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{_CYAN}?{_RESET} {label}{suffix}: ").strip()
    except EOFError:
        # A closed stdin means the script is not actually interactive; failing
        # here is clearer than looping forever on an empty read.
        raise TelegramAuthError(
            "stdin closed while waiting for input: run this in a terminal"
        ) from None
    return raw or default


def _prompt_secret(label: str) -> str:
    """Read a value without echoing it.

    Falls back to a visible prompt when the terminal has no TTY — a warning is
    better than crashing, but the operator is told the value will be visible.
    """
    if sys.stdin.isatty():
        return getpass.getpass(f"? {label}: ").strip()
    _warn("stdin is not a TTY: the value you type will be visible")
    return _prompt(label)


async def _store(result: AuthResult, api_id: int, api_hash: str, label: str) -> None:
    """Persist the session, encrypted, and report the account's new state."""
    async with AsyncSessionLocal() as session:
        pool = TelegramAccountPool(session)
        account = await pool.store_session(
            phone_number=result.phone_number,
            api_id=api_id,
            api_hash=api_hash,
            session_string=result.session_string,
            label=label,
        )
        state = account_state(account)
    _ok(
        f"account {account.display_name} stored (id={account.id}, "
        f"state={state.value}, session encrypted with FERNET_KEY)"
    )


async def _list_accounts() -> int:
    """Print every configured account and its state."""
    async with AsyncSessionLocal() as session:
        accounts = await TelegramAccountPool(session).all_accounts()

    if not accounts:
        _say("no Telegram accounts are configured yet")
        return 0

    now = datetime.now(UTC)
    _say(f"{len(accounts)} account(s) configured:")
    for account in accounts:
        state = account_state(account, now=now)
        detail = ""
        if state is AccountState.FLOOD_WAIT and account.flood_wait_until:
            detail = f" until {account.flood_wait_until.isoformat(timespec='seconds')}"
        elif state is AccountState.AUTH_REQUIRED:
            detail = " (run this script for it)"
        elif account.last_error:
            detail = f" last error: {account.last_error[:60]}"
        sys.stdout.write(
            f"    #{account.id:<3} {account.display_name:<24} "
            f"{state.value:<14}{detail}\n"
        )
    return 0


def _collect_credentials(
    args: argparse.Namespace,
) -> tuple[str, int, str, str]:
    """Resolve phone, api_id, api_hash and label from flags or prompts.

    Split out from :func:`_authenticate` so validation failures raise one
    exception type instead of scattering early returns through the sign-in flow.
    """
    phone_raw = args.phone or _prompt("Phone number (E.164, e.g. +79055342403)")
    try:
        phone = normalise_phone(phone_raw)
    except ValueError as error:
        raise TelegramAuthError(str(error)) from error

    api_id_raw = args.api_id or _prompt("API id (my.telegram.org/apps)")
    try:
        api_id = int(str(api_id_raw).strip())
    except ValueError as error:
        raise TelegramAuthError(
            f"api_id must be a number, got {api_id_raw!r}"
        ) from error

    api_hash = (args.api_hash or _prompt_secret("API hash")).strip()
    if not api_hash:
        raise TelegramAuthError("api_hash is required")

    return phone, api_id, api_hash, args.label or ""


async def _authenticate(args: argparse.Namespace) -> int:
    """Run the interactive sign-in."""
    try:
        phone, api_id, api_hash, label = _collect_credentials(args)
    except TelegramAuthError as error:
        _fail(str(error))
        return 2

    _say(f"signing in as {phone} (api_id={api_id})")
    try:
        async with TelegramAuthFlow(phone, api_id, api_hash) as flow:
            await flow.send_code()
            _ok("Telegram sent a login code to that number")

            result = await _submit_code_with_retries(flow)
            if result is None:
                return 1
    except TelegramAuthError as error:
        _fail(str(error))
        return 1

    _ok(
        f"signed in as {result.display_name} (user id {result.user_id})"
        + (" using the 2FA password" if result.used_two_factor else "")
    )

    if args.print_session:
        _warn("the session string below is a full credential; store it safely")
        sys.stdout.write(f"\n{result.session_string}\n\n")

    try:
        await _store(result, api_id, api_hash, label)
    except Exception as error:  # noqa: BLE001 - the session must not be lost silently
        _fail(f"sign-in succeeded but storing it failed: {type(error).__name__}: {error}")
        if not args.print_session:
            _warn(
                "re-run with --print-session to recover the session string "
                "without repeating the sign-in"
            )
        return 1
    return 0


async def _submit_code_with_retries(flow: TelegramAuthFlow) -> AuthResult | None:
    """Prompt for the code, then the 2FA password when Telegram asks for it.

    Retries are bounded and only cover a *wrong* value. An expired code or a
    flood wait is not retried, because another attempt cannot succeed and would
    only deepen the rate limit.
    """
    for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
        code = _prompt_secret(f"Login code from Telegram (attempt {attempt})")
        try:
            return await flow.submit_code(code)
        except TwoFactorRequiredError:
            _say("this account has two-factor authentication enabled")
            return await _submit_password_with_retries(flow)
        except TelegramAuthError as error:
            message = str(error)
            if "not correct" not in message:
                raise
            if attempt == MAX_CODE_ATTEMPTS:
                _fail(f"{message}; out of attempts")
                return None
            _warn(f"{message}; try again")
    return None


async def _submit_password_with_retries(
    flow: TelegramAuthFlow,
) -> AuthResult | None:
    """Prompt for the cloud password until it is accepted or attempts run out."""
    for attempt in range(1, MAX_PASSWORD_ATTEMPTS + 1):
        password = _prompt_secret(f"Two-factor password (attempt {attempt})")
        try:
            return await flow.submit_password(password)
        except TelegramAuthError as error:
            message = str(error)
            if "not correct" not in message:
                raise
            if attempt == MAX_PASSWORD_ATTEMPTS:
                _fail(f"{message}; out of attempts")
                return None
            _warn(f"{message}; try again")
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.auth_telegram",
        description=(
            "Sign a Telegram account in for ingestion and store its encrypted "
            "session. Re-running it for an existing phone number refreshes that "
            "account rather than adding a duplicate."
        ),
    )
    parser.add_argument("--phone", help="Phone number in E.164 form")
    parser.add_argument("--api-id", help="Telegram application id")
    parser.add_argument("--api-hash", help="Telegram application hash")
    parser.add_argument("--label", help="Operator-facing name for the account")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List configured accounts and their states, then exit",
    )
    parser.add_argument(
        "--print-session",
        action="store_true",
        help=(
            "Print the session string. It is a full account credential; use only "
            "when the database is unreachable."
        ),
    )
    return parser


async def _main(argv: Sequence[str]) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if not settings.fernet_key.strip():
        _fail(
            "FERNET_KEY is not set, so the session could not be encrypted. "
            "Generate one with: python -m app.core.security genkey"
        )
        return 2

    try:
        if args.list:
            return await _list_accounts()
        return await _authenticate(args)
    except KeyboardInterrupt:
        _warn("cancelled; nothing was stored")
        return 130
    finally:
        # The engine holds a pool of live connections; leaving it open makes the
        # script hang on exit instead of returning to the shell.
        await dispose_engine()


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
