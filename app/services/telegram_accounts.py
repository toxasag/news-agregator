"""Pool of Telegram accounts for MTProto ingestion.

Telegram rate-limits per account, and a single account collecting dozens of
channels hits ``FloodWait`` quickly. This module owns the pool that spreads that
load: it hands out the least-recently-used healthy account, records the outcome,
and takes an account out of rotation for exactly as long as Telegram asked.

Three decisions are worth stating, because each rules out a simpler alternative:

**Sessions are encrypted at rest.** A Telethon ``StringSession`` is a bearer
credential: whoever holds it has full access to the account without the phone
number or the 2FA password. It is stored as a Fernet ciphertext under
``FERNET_KEY`` and never returned to a template, so a database dump — or a
dashboard bug — does not hand over the accounts.

**Least-recently-used, not round-robin.** A round-robin cursor lives in the
process, so every worker restart resets it to the same account and the first one
in the list absorbs the most load. Ordering by ``last_used_at`` in the query
keeps the decision in the database, which is also the only place several worker
processes can agree on it.

**A ``FloodWait`` penalty is persisted, not held in memory.** Telegram's wait is
frequently minutes and sometimes hours. Keeping it in a process would mean a
worker restart forgets the penalty, immediately reuses the account and earns a
longer one.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_secret_box
from app.models.telegram_account import TelegramAccount

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "AccountCredentials",
    "AccountState",
    "NoAccountAvailableError",
    "TelegramAccountPool",
    "account_state",
    "decrypt_session",
    "encrypt_session",
    "normalise_phone",
]

#: Consecutive failures after which an account is deactivated. Three rather than
#: one because a single failure is usually the network; a revoked session fails
#: every time, and leaving it active would make the pool hand out a dead account
#: forever.
MAX_CONSECUTIVE_FAILURES: Final[int] = 3

#: Upper bound on a stored ``FloodWait``. Telegram occasionally answers with a
#: wait of days, which is indistinguishable from "banned" for scheduling
#: purposes; clamping keeps one absurd value from parking an account for a week
#: while still leaving it visibly penalised.
MAX_FLOOD_WAIT: Final[timedelta] = timedelta(hours=24)


class NoAccountAvailableError(RuntimeError):
    """No active, authorised, non-rate-limited account exists.

    Raised rather than returning ``None`` so a caller cannot accidentally treat
    "no accounts" as "no channels to collect" — the two look identical in a
    report and mean very different things.
    """


class AccountState(StrEnum):
    """Operator-facing account state.

    A :class:`StrEnum` so a template can print it directly and a comparison
    against a plain string still works, without inventing a `str` subclass whose
    instances would each carry the whole namespace.
    """

    ACTIVE = "active"
    FLOOD_WAIT = "flood_wait"
    AUTH_REQUIRED = "auth_required"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class AccountCredentials:
    """Everything needed to open a client, with the session already decrypted.

    Deliberately not the ORM object: this crosses into the Telethon layer, and a
    plain frozen dataclass cannot lazy-load, cannot be mutated by accident, and
    cannot be handed to a template with a live database session attached.
    """

    account_id: int
    phone_number: str
    label: str
    api_id: int
    api_hash: str
    session_string: str

    @property
    def display_name(self) -> str:
        return self.label.strip() or self.phone_number


def encrypt_session(session_string: str) -> str:
    """Encrypt a Telethon session string for storage.

    An empty string passes through unchanged, so "never signed in" stays
    distinguishable from "signed in with an unreadable ciphertext".
    """
    return get_secret_box().encrypt(session_string)


def decrypt_session(ciphertext: str) -> str:
    """Decrypt a stored session string, returning ``""`` when unreadable.

    Fernet is authenticated, so a tampered or key-rotated value is detected
    rather than decrypting to garbage that would be sent to Telegram as a
    credential. An unreadable session is reported as "auth required", which is
    exactly the operator action it needs.
    """
    return get_secret_box().decrypt(ciphertext)


def account_state(
    account: TelegramAccount, *, now: datetime | None = None
) -> AccountState:
    """Classify an account for the dashboard.

    Order matters: a disabled account is disabled regardless of its flood wait,
    and an account that has never signed in needs authorisation before its rate
    limit is of any interest.
    """
    moment = now or datetime.now(UTC)
    if not account.is_active:
        return AccountState.DISABLED
    if not account.is_authorised:
        return AccountState.AUTH_REQUIRED
    if account.flood_wait_until is not None:
        deadline = account.flood_wait_until
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if deadline > moment:
            return AccountState.FLOOD_WAIT
    return AccountState.ACTIVE


class TelegramAccountPool:
    """Selects and scores the Telegram accounts available for ingestion.

    Stateless apart from the session it is given: every decision is a query, so
    two worker processes reach the same conclusion instead of each keeping a
    private cursor that drifts.
    """

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire(self) -> AccountCredentials:
        """Claim the least-recently-used healthy account.

        ``last_used_at`` is stamped as part of claiming, not after the work
        finishes, so two concurrent callers do not both pick the same account
        because neither had finished yet.
        """
        candidates = await self.available_accounts()
        if not candidates:
            raise NoAccountAvailableError(
                "no active, authorised Telegram account is available "
                "(all are missing a session, disabled, or in flood wait)"
            )

        for account in candidates:
            session_string = decrypt_session(account.session_string)
            if not session_string:
                # Stored but unreadable: almost always FERNET_KEY rotation. Flag
                # it rather than handing Telethon an empty session, which would
                # fail as a confusing auth error much later.
                logger.error(
                    "Telegram account %s has an unreadable session string; "
                    "re-run scripts/auth_telegram.py for it",
                    account.phone_number,
                )
                await self.record_failure(
                    account.id,
                    error="session string could not be decrypted",
                )
                continue

            await self._stamp_used(account.id)
            logger.info(
                "Ingestion will use Telegram account %s", account.display_name
            )
            return AccountCredentials(
                account_id=account.id,
                phone_number=account.phone_number,
                label=account.label,
                api_id=account.api_id,
                api_hash=account.api_hash,
                session_string=session_string,
            )

        raise NoAccountAvailableError(
            "every candidate account has an undecryptable session string; "
            "FERNET_KEY may have been rotated"
        )

    def credentials_for(self, account: TelegramAccount) -> AccountCredentials | None:
        """Decrypt one already-loaded account, or ``None`` if unreadable."""
        session_string = decrypt_session(account.session_string)
        if not session_string:
            return None
        return AccountCredentials(
            account_id=account.id,
            phone_number=account.phone_number,
            label=account.label,
            api_id=account.api_id,
            api_hash=account.api_hash,
            session_string=session_string,
        )

    async def available_accounts(self) -> Sequence[TelegramAccount]:
        """Return healthy accounts, least recently used first.

        ``NULLS FIRST`` puts a never-used account ahead of every used one, so a
        freshly added account is tried immediately instead of waiting for the
        others to age past it.
        """
        now = datetime.now(UTC)
        statement = (
            select(TelegramAccount)
            .where(
                TelegramAccount.is_active.is_(True),
                TelegramAccount.session_string != "",
            )
            .order_by(TelegramAccount.last_used_at.asc().nulls_first())
        )
        result = await self._session.execute(statement)
        accounts = list(result.scalars().all())
        # Flood-wait filtering happens here rather than in SQL so a naive
        # timezone in the column cannot silently exclude an available account.
        return [
            account
            for account in accounts
            if account_state(account, now=now) == AccountState.ACTIVE
        ]

    async def all_accounts(self) -> Sequence[TelegramAccount]:
        """Every account, for the dashboard. Ordered stably by id."""
        result = await self._session.execute(
            select(TelegramAccount).order_by(TelegramAccount.id)
        )
        return list(result.scalars().all())

    async def _stamp_used(self, account_id: int) -> None:
        """Mark an account as used now, so rotation moves on."""
        await self._session.execute(
            update(TelegramAccount)
            .where(TelegramAccount.id == account_id)
            .values(last_used_at=datetime.now(UTC))
        )
        await self._session.commit()

    async def record_success(self, account_id: int) -> None:
        """Clear the failure counter after a good run.

        Resetting on success is what makes ``MAX_CONSECUTIVE_FAILURES`` mean
        *consecutive*: without it, three unrelated failures spread over weeks
        would disable a perfectly good account.
        """
        await self._session.execute(
            update(TelegramAccount)
            .where(TelegramAccount.id == account_id)
            .values(failure_count=0, last_error=None)
        )
        await self._session.commit()

    async def record_flood_wait(self, account_id: int, seconds: float) -> datetime:
        """Park an account until its Telegram-mandated wait expires.

        A flood wait is not a failure: the account is healthy and simply used its
        quota, so the failure counter is left alone. Counting it would disable
        the busiest account after three ordinary rate limits.
        """
        wait = min(timedelta(seconds=max(seconds, 0)), MAX_FLOOD_WAIT)
        until = datetime.now(UTC) + wait
        await self._session.execute(
            update(TelegramAccount)
            .where(TelegramAccount.id == account_id)
            .values(
                flood_wait_until=until,
                last_error=f"flood wait for {int(wait.total_seconds())}s",
            )
        )
        await self._session.commit()
        logger.warning(
            "Telegram account %s is in flood wait until %s",
            account_id,
            until.isoformat(),
        )
        return until

    async def record_failure(self, account_id: int, *, error: str) -> bool:
        """Count a failure and deactivate the account once it repeats.

        Returns whether the account was deactivated. The message is truncated
        because it reaches the dashboard, and an upstream error can be enormous.
        """
        account = await self._session.get(TelegramAccount, account_id)
        if account is None:
            return False

        account.failure_count += 1
        account.last_error = error[:500]
        deactivated = account.failure_count >= MAX_CONSECUTIVE_FAILURES
        if deactivated:
            account.is_active = False
            logger.error(
                "Telegram account %s deactivated after %d consecutive failures: %s",
                account.phone_number,
                account.failure_count,
                account.last_error,
            )
        await self._session.commit()
        return deactivated

    async def store_session(
        self,
        *,
        phone_number: str,
        api_id: int,
        api_hash: str,
        session_string: str,
        label: str = "",
    ) -> TelegramAccount:
        """Create or update one account, encrypting its session.

        Upsert by phone number, so re-running the auth helper refreshes an
        existing account instead of creating a second row that would be rotated
        as a separate rate-limit budget. A successful sign-in also clears the
        failure counter and any flood wait, because both describe a session that
        no longer exists.
        """
        normalised = normalise_phone(phone_number)
        result = await self._session.execute(
            select(TelegramAccount).where(
                TelegramAccount.phone_number == normalised
            )
        )
        account = result.scalar_one_or_none()
        ciphertext = encrypt_session(session_string)

        if account is None:
            account = TelegramAccount(
                phone_number=normalised,
                label=label.strip(),
                api_id=api_id,
                api_hash=api_hash,
                session_string=ciphertext,
                is_active=True,
            )
            self._session.add(account)
        else:
            account.api_id = api_id
            account.api_hash = api_hash
            account.session_string = ciphertext
            account.is_active = True
            account.failure_count = 0
            account.flood_wait_until = None
            account.last_error = None
            if label.strip():
                account.label = label.strip()

        await self._session.commit()
        await self._session.refresh(account)
        return account

    async def disconnect(self, account_id: int) -> TelegramAccount | None:
        """Drop the stored session and take the account out of the pool.

        The session string is a bearer credential: leaving it encrypted in the
        row after the operator clicked "disconnect" would still let the next
        process decrypt it. Clearing it is the actual revocation on our side.
        Telegram log-out is best-effort and happens in the caller that still
        holds a live client; this method only mutates our store.
        """
        account = await self._session.get(TelegramAccount, account_id)
        if account is None:
            return None
        account.session_string = ""
        account.is_active = False
        account.failure_count = 0
        account.flood_wait_until = None
        account.last_error = None
        await self._session.commit()
        await self._session.refresh(account)
        logger.info("Disconnected Telegram account %s", account.phone_number)
        return account


def normalise_phone(phone_number: str) -> str:
    """Normalise a phone number to ``+`` followed by digits.

    Operators paste numbers as ``+7 905 534-24-03`` or ``8-905-...``; without
    normalising, the unique constraint would not recognise two spellings of one
    account and the pool would rotate it as if it had two separate quotas.
    """
    digits = "".join(character for character in phone_number if character.isdigit())
    if not digits:
        raise ValueError("phone number contains no digits")
    # A Russian number written with a leading 8 is the same subscriber as +7.
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits
