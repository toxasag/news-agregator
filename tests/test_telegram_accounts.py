"""Multi-account Telegram ingestion and the runtime admin credential store.

Three properties are worth asserting here, because each protects something that
fails silently otherwise:

* **Sessions are encrypted at rest.** A Telethon session string is a bearer
  credential. If it were ever stored in the clear, nothing in the pipeline would
  behave differently — the leak only becomes visible after the account is stolen.
* **Rotation actually rotates.** A pool that always returns the first account
  looks identical to a working one until Telegram rate-limits that account and
  the whole pipeline stops.
* **2FA is a distinct branch.** An account with a cloud password fails sign-in in
  a way that is indistinguishable from a wrong code unless the fallback is
  handled explicitly.

The suite runs with no Telegram, no Redis and no PostgreSQL: the pool talks to a
minimal session double and Telethon is replaced at the seam.
"""

# The operator-facing messages under test are Russian, so Cyrillic characters
# resembling Latin ones are the subject matter rather than a typo.

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, Message, PeerChannel, User

from app.api.dashboard import (
    _matching_sources,
    _preferred_source,
    _telegram_link,
)
from app.config import settings
from app.core import security
from app.core.security import hash_password, verify_password
from app.models.enums import SourceType, TelegramPeerKind
from app.models.telegram_account import TelegramAccount
from app.services.admin_credentials import (
    MIN_PASSWORD_LENGTH,
    CredentialError,
    authenticate_admin_db,
    change_password,
    change_username,
    load_credentials,
)
from app.services.ingest_freshness import (
    is_on_or_after_moscow_today,
    moscow_today_start,
    needs_freshness_cutoff,
)
from app.services.model_routing import (
    ModelError,
    load_model_routing,
    resolve_model,
    save_model_routing,
)
from app.services.telegram_accounts import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_FLOOD_WAIT,
    AccountState,
    NoAccountAvailableError,
    TelegramAccountPool,
    account_state,
    decrypt_session,
    encrypt_session,
    normalise_phone,
)
from app.services.telegram_auth import (
    TelegramAuthError,
    TelegramAuthFlow,
    TelegramQrLogin,
    TwoFactorRequiredError,
)
from app.services.telegram_mtproto import (
    DialogDiscovery,
    MTProtoIngestor,
    describe_entity,
    is_broadcast_channel,
    peer_identifier,
    peer_invite_link,
)

#: A plausible Telethon session string. Length matters only in that it must not
#: round-trip by accident, so it is not a short token.
SESSION_STRING: Final[str] = "1BVtsOHwBu" + "x" * 300


# ================================================================== fixtures


def _override_setting(name: str, value: object) -> object:
    """Set one field on the frozen settings instance, returning the old value."""
    previous = getattr(settings, name)
    object.__setattr__(settings, name, value)
    return previous


@pytest.fixture(autouse=True)
def _fernet_key() -> Iterator[None]:
    """Install a real Fernet key, and reset the module-level secret box.

    `get_secret_box` caches a box built from whatever key was configured first, so
    without clearing it a later test would encrypt under a key this one replaced.
    """
    previous = _override_setting("fernet_key", Fernet.generate_key().decode("ascii"))
    security._shared_box = None
    try:
        yield
    finally:
        _override_setting("fernet_key", previous)
        security._shared_box = None


def build_account(**overrides: Any) -> TelegramAccount:
    """Build a transient account. Nothing is persisted."""
    values: dict[str, Any] = {
        "id": 1,
        "phone_number": "+79055342403",
        "label": "",
        "api_id": 31332284,
        "api_hash": "0" * 32,
        "session_string": encrypt_session(SESSION_STRING),
        "is_active": True,
        "last_used_at": None,
        "flood_wait_until": None,
        "failure_count": 0,
        "last_error": None,
    }
    values.update(overrides)
    return TelegramAccount(**values)


class _Scalars:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        return list(self._rows)


class _Result:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class PoolSession:
    """Session double for the account pool.

    Holds accounts in a list and applies the pool's `update()` statements by hand,
    which is enough to assert rotation and penalty bookkeeping without a database.
    """

    def __init__(self, accounts: Sequence[TelegramAccount] = ()) -> None:
        self.accounts = list(accounts)
        self.commits = 0
        self.added: list[Any] = []
        self.updates: list[dict[str, Any]] = []

    async def execute(self, statement: Any) -> _Result:
        compiled = str(statement).lower()
        if compiled.startswith("update"):
            # The pool updates one account by primary key; the values are read off
            # the statement so a test can assert what was written.
            values = {
                key.name if hasattr(key, "name") else str(key): value
                for key, value in getattr(statement, "_values", {}).items()
            }
            self.updates.append(values)
            target = statement.whereclause.right.value
            for account in self.accounts:
                if account.id == target:
                    for name, value in values.items():
                        setattr(account, name, getattr(value, "value", value))
            return _Result([])
        # Any select returns the accounts; the pool filters and orders in Python
        # for the flood-wait check, so ordering here would be redundant.
        ordered = sorted(
            self.accounts,
            key=lambda account: (
                account.last_used_at is not None,
                account.last_used_at or datetime.min.replace(tzinfo=UTC),
            ),
        )
        return _Result(ordered)

    async def get(self, entity: Any, ident: Any) -> Any:
        for account in self.accounts:
            if account.id == ident:
                return account
        return None

    def add(self, instance: Any) -> None:
        self.added.append(instance)
        self.accounts.append(instance)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, instance: Any) -> None:
        return None

    def as_session(self) -> AsyncSession:
        return cast(AsyncSession, self)


# ======================================================== session encryption


def test_a_session_string_round_trips_through_fernet() -> None:
    ciphertext = encrypt_session(SESSION_STRING)

    assert ciphertext != SESSION_STRING
    assert SESSION_STRING not in ciphertext
    assert decrypt_session(ciphertext) == SESSION_STRING


def test_an_empty_session_stays_empty() -> None:
    """"Never signed in" must stay distinguishable from a stored ciphertext.

    If an empty string encrypted to a real ciphertext, an account that had never
    authorised would look authorised and the pool would hand it out.
    """
    assert encrypt_session("") == ""
    assert decrypt_session("") == ""


def test_a_tampered_session_decrypts_to_empty() -> None:
    """Fernet is authenticated, so tampering is detected rather than mangled.

    Returning empty makes the account report "auth required", which is the correct
    operator action, instead of sending Telegram a corrupted credential.
    """
    ciphertext = encrypt_session(SESSION_STRING)
    tampered = ciphertext[:-6] + "AAAAAA"

    assert decrypt_session(tampered) == ""


def test_a_session_encrypted_under_another_key_is_unreadable() -> None:
    """Rotating FERNET_KEY must invalidate stored sessions, not silently pass."""
    ciphertext = encrypt_session(SESSION_STRING)

    _override_setting("fernet_key", Fernet.generate_key().decode("ascii"))
    security._shared_box = None

    assert decrypt_session(ciphertext) == ""


def test_two_encryptions_of_one_session_differ() -> None:
    """Fernet embeds a timestamp and IV, so ciphertexts must not be comparable.

    Identical ciphertexts would let anyone with database read access tell that two
    accounts share a session without decrypting either.
    """
    assert encrypt_session(SESSION_STRING) != encrypt_session(SESSION_STRING)


# ============================================================= phone numbers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+79055342403", "+79055342403"),
        ("79055342403", "+79055342403"),
        ("8 905 534-24-03", "+79055342403"),
        ("+7 (905) 534 24 03", "+79055342403"),
    ],
)
def test_phone_numbers_normalise_to_one_form(raw: str, expected: str) -> None:
    """Two spellings of one number must not become two pool entries.

    The unique constraint is on the stored string, so without normalising, the
    same account could be added twice and rotated as if it had two independent
    rate-limit budgets.
    """
    assert normalise_phone(raw) == expected


def test_a_number_without_digits_is_rejected() -> None:
    with pytest.raises(ValueError, match="no digits"):
        normalise_phone("not-a-phone")


# =================================================================== states


def test_an_account_without_a_session_needs_authorisation() -> None:
    account = build_account(session_string="")

    assert account_state(account) is AccountState.AUTH_REQUIRED
    assert account.is_authorised is False


def test_a_rate_limited_account_reports_flood_wait() -> None:
    account = build_account(
        flood_wait_until=datetime.now(UTC) + timedelta(minutes=5)
    )

    assert account_state(account) is AccountState.FLOOD_WAIT


def test_an_expired_flood_wait_is_active_again() -> None:
    """The penalty must lapse on its own, without an operator clearing it."""
    account = build_account(
        flood_wait_until=datetime.now(UTC) - timedelta(seconds=1)
    )

    assert account_state(account) is AccountState.ACTIVE


def test_a_naive_flood_wait_timestamp_is_treated_as_utc() -> None:
    """A naive datetime must not crash the comparison or read as expired.

    PostgreSQL returns timezone-aware values, but a fixture or a driver change can
    produce a naive one, and an unguarded comparison raises TypeError mid-run.
    """
    account = build_account(
        flood_wait_until=(datetime.now(UTC) + timedelta(minutes=5)).replace(
            tzinfo=None
        )
    )

    assert account_state(account) is AccountState.FLOOD_WAIT


def test_a_disabled_account_reports_disabled_over_flood_wait() -> None:
    account = build_account(
        is_active=False, flood_wait_until=datetime.now(UTC) + timedelta(hours=1)
    )

    assert account_state(account) is AccountState.DISABLED


def test_the_label_falls_back_to_the_phone_number() -> None:
    assert build_account(label="").display_name == "+79055342403"
    assert build_account(label="  Основной  ").display_name == "Основной"


# ================================================================= rotation


async def test_the_pool_hands_out_the_least_recently_used_account() -> None:
    """Load must spread instead of hammering whichever account sorts first."""
    now = datetime.now(UTC)
    older = build_account(
        id=1, phone_number="+79000000001", last_used_at=now - timedelta(minutes=30)
    )
    newer = build_account(
        id=2, phone_number="+79000000002", last_used_at=now - timedelta(minutes=1)
    )
    session = PoolSession([newer, older])

    claimed = await TelegramAccountPool(session.as_session()).acquire()

    assert claimed.account_id == older.id


async def test_a_never_used_account_is_tried_first() -> None:
    """NULLS FIRST: a freshly added account should not wait for the others to age."""
    used = build_account(
        id=1, phone_number="+79000000001", last_used_at=datetime.now(UTC)
    )
    fresh = build_account(id=2, phone_number="+79000000002", last_used_at=None)
    session = PoolSession([used, fresh])

    claimed = await TelegramAccountPool(session.as_session()).acquire()

    assert claimed.account_id == fresh.id


async def test_claiming_stamps_last_used_so_rotation_advances() -> None:
    """Stamped when claimed, not when the work finishes.

    Otherwise two concurrent callers both pick the same account, because neither
    has finished yet.
    """
    account = build_account(last_used_at=None)
    session = PoolSession([account])

    await TelegramAccountPool(session.as_session()).acquire()

    assert account.last_used_at is not None
    assert session.commits >= 1


async def test_successive_claims_alternate_between_accounts() -> None:
    """The observable point of rotation: two passes must not reuse one account."""
    first = build_account(id=1, phone_number="+79000000001", last_used_at=None)
    second = build_account(id=2, phone_number="+79000000002", last_used_at=None)
    session = PoolSession([first, second])
    pool = TelegramAccountPool(session.as_session())

    claimed = [(await pool.acquire()).account_id for _ in range(4)]

    assert claimed == [1, 2, 1, 2]


async def test_a_rate_limited_account_is_skipped() -> None:
    limited = build_account(
        id=1,
        phone_number="+79000000001",
        last_used_at=None,
        flood_wait_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    healthy = build_account(
        id=2, phone_number="+79000000002", last_used_at=datetime.now(UTC)
    )
    session = PoolSession([limited, healthy])

    claimed = await TelegramAccountPool(session.as_session()).acquire()

    assert claimed.account_id == healthy.id


async def test_an_empty_pool_raises_rather_than_returning_none() -> None:
    """"No accounts" must not be mistakable for "no channels to collect".

    The two produce identical zero counters in a report and mean very different
    things, so the pool refuses to return a falsy value.
    """
    session = PoolSession([])

    with pytest.raises(NoAccountAvailableError):
        await TelegramAccountPool(session.as_session()).acquire()


async def test_an_undecryptable_session_is_skipped_and_recorded() -> None:
    """A key rotation must not hand Telethon an empty session string."""
    broken = build_account(
        id=1, phone_number="+79000000001", session_string="gAAAAABnotvalid"
    )
    good = build_account(
        id=2, phone_number="+79000000002", last_used_at=datetime.now(UTC)
    )
    session = PoolSession([broken, good])

    claimed = await TelegramAccountPool(session.as_session()).acquire()

    assert claimed.account_id == good.id
    assert broken.failure_count == 1
    assert broken.last_error is not None and "decrypt" in broken.last_error


async def test_the_claimed_credentials_carry_a_decrypted_session() -> None:
    """The Telethon layer needs plaintext; the database must never hold it."""
    account = build_account()
    session = PoolSession([account])

    claimed = await TelegramAccountPool(session.as_session()).acquire()

    assert claimed.session_string == SESSION_STRING
    assert account.session_string != SESSION_STRING


# ====================================================== failure bookkeeping


async def test_a_flood_wait_does_not_count_as_a_failure() -> None:
    """A rate limit is a healthy account using its quota.

    Counting it would retire the busiest account after three ordinary flood waits.
    """
    account = build_account()
    session = PoolSession([account])
    pool = TelegramAccountPool(session.as_session())

    until = await pool.record_flood_wait(account.id, 300)

    assert account.flood_wait_until == until
    assert account.failure_count == 0
    assert account_state(account) is AccountState.FLOOD_WAIT


async def test_an_absurd_flood_wait_is_clamped() -> None:
    """A multi-day wait must not park an account for a week."""
    account = build_account()
    session = PoolSession([account])
    pool = TelegramAccountPool(session.as_session())

    until = await pool.record_flood_wait(account.id, 60 * 60 * 24 * 30)

    assert until - datetime.now(UTC) <= MAX_FLOOD_WAIT + timedelta(seconds=5)


async def test_repeated_failures_deactivate_the_account() -> None:
    """A revoked session fails every time; the pool must stop handing it out."""
    account = build_account()
    session = PoolSession([account])
    pool = TelegramAccountPool(session.as_session())

    for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
        assert await pool.record_failure(account.id, error="boom") is False
    assert account.is_active is True

    assert await pool.record_failure(account.id, error="boom") is True
    assert account.is_active is False
    assert account_state(account) is AccountState.DISABLED


async def test_a_success_clears_the_failure_counter() -> None:
    """Makes the threshold mean *consecutive* failures.

    Without the reset, three unrelated failures spread over weeks would retire a
    healthy account.
    """
    account = build_account(failure_count=2, last_error="earlier problem")
    session = PoolSession([account])

    await TelegramAccountPool(session.as_session()).record_success(account.id)

    assert account.failure_count == 0
    assert account.last_error is None


async def test_a_long_error_message_is_truncated() -> None:
    """The message reaches the dashboard, and upstream errors can be enormous."""
    account = build_account()
    session = PoolSession([account])

    await TelegramAccountPool(session.as_session()).record_failure(
        account.id, error="x" * 5000
    )

    assert account.last_error is not None
    assert len(account.last_error) <= 500


# ============================================================ storing sessions


async def test_storing_a_session_encrypts_it() -> None:
    session = PoolSession([])
    pool = TelegramAccountPool(session.as_session())

    account = await pool.store_session(
        phone_number="8 905 534-24-03",
        api_id=31332284,
        api_hash="0" * 32,
        session_string=SESSION_STRING,
        label="Основной",
    )

    assert account.phone_number == "+79055342403"
    assert account.session_string != SESSION_STRING
    assert decrypt_session(account.session_string) == SESSION_STRING


async def test_re_authorising_updates_the_existing_account() -> None:
    """Upsert by phone, so a refreshed session does not create a second budget."""
    existing = build_account(
        failure_count=2,
        is_active=False,
        flood_wait_until=datetime.now(UTC) + timedelta(hours=1),
        last_error="session revoked",
    )
    session = PoolSession([existing])
    pool = TelegramAccountPool(session.as_session())

    account = await pool.store_session(
        phone_number="+79055342403",
        api_id=999,
        api_hash="f" * 32,
        session_string="fresh-session-string",
    )

    assert len(session.accounts) == 1
    assert account is existing
    # A successful sign-in describes a session that failed before, so the penalty
    # and the counter belong to a state that no longer exists.
    assert account.failure_count == 0
    assert account.flood_wait_until is None
    assert account.last_error is None
    assert account.is_active is True
    assert decrypt_session(account.session_string) == "fresh-session-string"


async def test_disconnect_clears_the_session_and_disables_the_account() -> None:
    existing = build_account(id=1, session_string=encrypt_session(SESSION_STRING))
    session = PoolSession([existing])
    pool = TelegramAccountPool(session.as_session())

    account = await pool.disconnect(1)

    assert account is existing
    assert account.session_string == ""
    assert account.is_active is False


async def test_qr_login_completes_without_2fa() -> None:
    class _Qr:
        url = "tg://login?token=abc"
        expires = datetime.now(UTC) + timedelta(minutes=5)

        async def wait(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
            return None

    class _Client(_FakeTelethonClient):
        async def qr_login(self) -> _Qr:
            self.authorized = True
            return _Qr()

        async def get_me(self) -> Any:
            return type(
                "User",
                (),
                {
                    "id": 777,
                    "username": "operator",
                    "first_name": "Оператор",
                    "phone": "79055342403",
                },
            )()

    qr = TelegramQrLogin(31332284, "0" * 32)
    object.__setattr__(qr, "_client", _Client())
    await qr.connect()
    url, _expires = await qr.start()
    result = await qr.wait()

    assert url.startswith("tg://login")
    assert result.phone_number == "+79055342403"
    assert result.used_two_factor is False


# ==================================================================== 2FA


class _FakeTelethonClient:
    """Stands in for `TelegramClient` at the sign-in seam.

    Scripts the three outcomes that matter: a plain code sign-in, an account with
    a cloud password, and a wrong value.
    """

    def __init__(self, *, needs_password: bool = False, bad_password: bool = False):
        self.needs_password = needs_password
        self.bad_password = bad_password
        self.session = _FakeStringSession()
        self.authorized = False
        self.disconnected = False
        self.password_attempts = 0

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send_code_request(self, phone: str) -> Any:
        return type("SentCode", (), {"phone_code_hash": "hash-123"})()

    async def sign_in(
        self,
        phone: str | None = None,
        code: str | int | None = None,
        *,
        password: str | None = None,
        phone_code_hash: str | None = None,
        **_: Any,
    ) -> Any:
        if password is not None:
            self.password_attempts += 1
            if self.bad_password:
                raise PasswordHashInvalidError(request=None)
            self.authorized = True
            return object()

        if code == "000000":
            raise PhoneCodeInvalidError(request=None)
        if self.needs_password:
            raise SessionPasswordNeededError(request=None)
        self.authorized = True
        return object()

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> Any:
        return type(
            "User", (), {"id": 777, "username": "operator", "first_name": "Оператор"}
        )()


class _FakeStringSession(StringSession):
    """A real StringSession whose export is fixed.

    Subclassing rather than duck-typing because `_finish` deliberately verifies
    the session type before exporting it: a plain stub would make the test pass
    against a check that protects against exactly that mistake.
    """

    def save(self) -> str:
        return SESSION_STRING


def _flow_with(client: _FakeTelethonClient) -> TelegramAuthFlow:
    """Build a flow whose Telethon client is the double."""
    flow = TelegramAuthFlow("+79055342403", 31332284, "0" * 32)
    object.__setattr__(flow, "_client", client)
    return flow


async def test_a_code_only_account_signs_in() -> None:
    client = _FakeTelethonClient()
    flow = _flow_with(client)

    await flow.send_code()
    result = await flow.submit_code("12345")

    assert result.session_string == SESSION_STRING
    assert result.used_two_factor is False
    assert result.user_id == 777


async def test_an_account_with_2fa_asks_for_the_password() -> None:
    """The 2FA branch must be a distinct signal, not a generic failure.

    A caller that treats it as "wrong code" would loop forever on a correct code.
    """
    client = _FakeTelethonClient(needs_password=True)
    flow = _flow_with(client)
    await flow.send_code()

    with pytest.raises(TwoFactorRequiredError):
        await flow.submit_code("12345")


async def test_the_2fa_password_completes_the_sign_in() -> None:
    client = _FakeTelethonClient(needs_password=True)
    flow = _flow_with(client)
    await flow.send_code()

    with pytest.raises(TwoFactorRequiredError):
        await flow.submit_code("12345")
    result = await flow.submit_password("cloud-password")

    assert result.used_two_factor is True
    assert result.session_string == SESSION_STRING
    assert client.password_attempts == 1


async def test_a_wrong_2fa_password_is_reported_as_such() -> None:
    client = _FakeTelethonClient(needs_password=True, bad_password=True)
    flow = _flow_with(client)
    await flow.send_code()
    with pytest.raises(TwoFactorRequiredError):
        await flow.submit_code("12345")

    with pytest.raises(TelegramAuthError, match="not correct"):
        await flow.submit_password("wrong")


async def test_a_wrong_code_is_reported_as_such() -> None:
    client = _FakeTelethonClient()
    flow = _flow_with(client)
    await flow.send_code()

    with pytest.raises(TelegramAuthError, match="not correct"):
        await flow.submit_code("000000")


async def test_an_empty_2fa_password_is_refused_before_the_network() -> None:
    client = _FakeTelethonClient(needs_password=True)
    flow = _flow_with(client)

    with pytest.raises(TelegramAuthError, match="empty"):
        await flow.submit_password("")
    assert client.password_attempts == 0


async def test_submitting_a_code_before_requesting_one_is_refused() -> None:
    """Telegram rejects a code without the hash from its own request."""
    flow = _flow_with(_FakeTelethonClient())

    with pytest.raises(TelegramAuthError, match="send_code"):
        await flow.submit_code("12345")


async def test_a_non_numeric_code_is_refused_before_the_network() -> None:
    client = _FakeTelethonClient()
    flow = _flow_with(client)
    await flow.send_code()

    with pytest.raises(TelegramAuthError, match="no digits"):
        await flow.submit_code("abcdef")


async def test_the_code_is_stripped_of_formatting() -> None:
    """Telegram shows the code as `123 45`; pasting it must still work."""
    client = _FakeTelethonClient()
    flow = _flow_with(client)
    await flow.send_code()

    result = await flow.submit_code("123 45")

    assert result.session_string == SESSION_STRING


# ================================================== admin credential store


class CredentialSession:
    """Session double for the credential store.

    Holds one JSONB document, so `load_credentials` sees what `_save` wrote and a
    test can assert the whole change round-trips rather than just that a statement
    was issued.
    """

    def __init__(self, document: Any = None) -> None:
        self.document = document
        self.commits = 0

    async def execute(self, statement: Any) -> _Result:
        compiled = str(statement).lower()
        if compiled.startswith("insert"):
            values = getattr(statement, "_values", {}) or {}
            for key, value in values.items():
                name = getattr(key, "name", str(key))
                if name == "value":
                    self.document = getattr(value, "value", value)
            return _Result([])
        return _Result([self.document] if self.document is not None else [])

    async def commit(self) -> None:
        self.commits += 1

    def as_session(self) -> AsyncSession:
        return cast(AsyncSession, self)


async def test_credentials_fall_back_to_the_environment() -> None:
    """A deployment that has never used the profile page must keep working."""
    _override_setting("admin_username", "envadmin")
    _override_setting("admin_password_hash", hash_password("env-password"))
    session = CredentialSession()

    credentials = await load_credentials(session.as_session())

    assert credentials.username == "envadmin"
    assert credentials.from_database is False


async def test_a_stored_row_wins_over_the_environment() -> None:
    _override_setting("admin_username", "envadmin")
    session = CredentialSession(
        {"username": "dbadmin", "password_hash": hash_password("db-password")}
    )

    credentials = await load_credentials(session.as_session())

    assert credentials.username == "dbadmin"
    assert credentials.from_database is True


async def test_an_incomplete_row_falls_back_instead_of_locking_us_out() -> None:
    """A malformed document must not make the dashboard unreachable.

    Failing closed here would mean one bad JSONB value locks the only operator out
    permanently, which is far worse than using the bootstrap credentials.
    """
    _override_setting("admin_username", "envadmin")
    _override_setting("admin_password_hash", hash_password("env-password"))
    session = CredentialSession({"username": "dbadmin"})  # no password_hash

    credentials = await load_credentials(session.as_session())

    assert credentials.username == "envadmin"
    assert credentials.from_database is False


async def test_changing_the_password_stores_only_a_hash() -> None:
    session = CredentialSession(
        {"username": "admin", "password_hash": hash_password("old-password")}
    )

    await change_password(
        session.as_session(),
        current_password="old-password",
        new_password="a-much-longer-password",
    )

    stored = session.document["password_hash"]
    assert stored.startswith("$2b$")
    assert "a-much-longer-password" not in str(session.document)
    assert verify_password("a-much-longer-password", stored)
    assert verify_password("old-password", stored) is False


async def test_changing_the_password_requires_the_current_one() -> None:
    """A stolen session cookie must not be enough to take over the account."""
    session = CredentialSession(
        {"username": "admin", "password_hash": hash_password("old-password")}
    )

    with pytest.raises(CredentialError, match="Текущий пароль"):
        await change_password(
            session.as_session(),
            current_password="not-the-password",
            new_password="a-much-longer-password",
        )


async def test_a_short_new_password_is_refused() -> None:
    """The login form has no rate limiting, so a short password is brute-forceable."""
    session = CredentialSession(
        {"username": "admin", "password_hash": hash_password("old-password")}
    )

    with pytest.raises(CredentialError, match=str(MIN_PASSWORD_LENGTH)):
        await change_password(
            session.as_session(),
            current_password="old-password",
            new_password="short",
        )


async def test_reusing_the_current_password_is_refused() -> None:
    session = CredentialSession(
        {"username": "admin", "password_hash": hash_password("old-password")}
    )

    with pytest.raises(CredentialError, match="совпадает"):
        await change_password(
            session.as_session(),
            current_password="old-password",
            new_password="old-password",
        )


async def test_changing_the_username_keeps_the_password_hash() -> None:
    """Renaming must not silently reset the password."""
    original = hash_password("the-password")
    session = CredentialSession({"username": "admin", "password_hash": original})

    credentials = await change_username(
        session.as_session(),
        new_username="operator",
        current_password="the-password",
    )

    assert credentials.username == "operator"
    assert session.document["password_hash"] == original
    assert verify_password("the-password", session.document["password_hash"])


@pytest.mark.parametrize("candidate", ["", "   ", "with space", "x" * 65])
async def test_an_invalid_username_is_refused(candidate: str) -> None:
    session = CredentialSession(
        {"username": "admin", "password_hash": hash_password("the-password")}
    )

    with pytest.raises(CredentialError):
        await change_username(
            session.as_session(),
            new_username=candidate,
            current_password="the-password",
        )


async def test_authentication_uses_the_stored_credentials() -> None:
    session = CredentialSession(
        {"username": "dbadmin", "password_hash": hash_password("db-password")}
    )

    assert await authenticate_admin_db(session.as_session(), "dbadmin", "db-password")
    assert not await authenticate_admin_db(session.as_session(), "dbadmin", "wrong")
    # A wrong username with the right password must also fail.
    assert not await authenticate_admin_db(session.as_session(), "admin", "db-password")


# ==================================================== AI model routing


async def test_model_routing_falls_back_to_the_environment() -> None:
    """A deployment that never touched the settings page keeps its env models."""
    _override_setting("ai_model_writer", "env/writer")
    session = CredentialSession()

    routing = await load_model_routing(session.as_session())

    assert routing.writer == "env/writer"
    assert routing.overridden == frozenset()


async def test_a_stored_model_overrides_the_environment() -> None:
    _override_setting("ai_model_writer", "env/writer")
    session = CredentialSession({"writer": "auto/fast"})

    routing = await load_model_routing(session.as_session())

    assert routing.writer == "auto/fast"
    assert "writer" in routing.overridden


async def test_each_role_falls_back_independently() -> None:
    """One overridden stage must not silently reset the other three."""
    _override_setting("ai_model_arbiter", "env/arbiter")
    _override_setting("ai_model_seo", "env/seo")
    session = CredentialSession({"writer": "auto/smart"})

    routing = await load_model_routing(session.as_session())

    assert routing.writer == "auto/smart"
    assert routing.arbiter == "env/arbiter"
    assert routing.seo == "env/seo"
    assert routing.overridden == frozenset({"writer"})


async def test_saving_a_model_round_trips() -> None:
    session = CredentialSession()

    routing = await save_model_routing(
        session.as_session(), {"writer": "auto/fast", "arbiter": "auto/cheap"}
    )

    assert routing.writer == "auto/fast"
    assert routing.arbiter == "auto/cheap"
    assert session.document == {"writer": "auto/fast", "arbiter": "auto/cheap"}


async def test_an_empty_value_clears_the_override() -> None:
    """Clearing a field must restore the default without the operator knowing it."""
    _override_setting("ai_model_writer", "env/writer")
    session = CredentialSession({"writer": "auto/fast"})

    routing = await save_model_routing(session.as_session(), {"writer": "  "})

    assert routing.writer == "env/writer"
    assert routing.overridden == frozenset()


async def test_an_unknown_role_is_refused() -> None:
    """Guards against a renamed form field silently writing a dead key."""
    session = CredentialSession()

    with pytest.raises(ModelError, match="Неизвестные роли"):
        await save_model_routing(session.as_session(), {"nonexistent": "auto/fast"})


@pytest.mark.parametrize("candidate", ["has space", "x" * 129])
async def test_an_invalid_model_identifier_is_refused(candidate: str) -> None:
    session = CredentialSession()

    with pytest.raises(ModelError):
        await save_model_routing(session.as_session(), {"writer": candidate})


async def test_a_malformed_document_falls_back_rather_than_raising() -> None:
    """An unexpected JSONB value must not stop the pipeline generating anything."""
    _override_setting("ai_model_writer", "env/writer")
    session = CredentialSession("not-a-dict")

    routing = await load_model_routing(session.as_session())

    assert routing.writer == "env/writer"


async def test_resolve_model_returns_one_role() -> None:
    session = CredentialSession({"writer": "auto/fast"})

    assert await resolve_model(session.as_session(), "writer") == "auto/fast"


# ============================================ MTProto ingestion rules


def _channel(**overrides: Any) -> Channel:
    """Build a broadcast channel entity."""
    values: dict[str, Any] = {
        "id": 1,
        "title": "Новости",
        "photo": None,
        "date": None,
        "broadcast": True,
        "megagroup": False,
    }
    values.update(overrides)
    return Channel(**values)


def test_a_public_channel_is_accepted() -> None:
    assert is_broadcast_channel(_channel(username="tass_agency")) is True


def test_a_private_channel_is_accepted() -> None:
    """Privacy is only the absence of a username.

    A subscribed account reads a private channel exactly like a public one, so
    excluding it would drop a legitimate source.
    """
    assert is_broadcast_channel(_channel(username=None)) is True


@pytest.mark.parametrize(
    ("entity", "expected_label"),
    [
        (_channel(broadcast=False, megagroup=True), "супергруппа"),
        (_channel(broadcast=True, gigagroup=True), "broadcast-группа"),
        (
            Chat(
                id=2,
                title="Чат",
                photo=None,
                participants_count=3,
                date=None,
                version=1,
            ),
            "группа",
        ),
        (User(id=3, first_name="Иван"), "личный чат"),
        (User(id=4, first_name="Бот", bot=True), "бот"),
    ],
)
def test_non_channels_are_rejected(entity: Any, expected_label: str) -> None:
    """Groups and private chats must never be read.

    A gigagroup is the subtle case: Telegram converted it from a megagroup, so it
    reports `broadcast=True` while still carrying conversational traffic. Checking
    `broadcast` alone would let it through.
    """
    assert is_broadcast_channel(entity) is False
    assert describe_entity(entity) == expected_label


async def test_a_marked_group_is_read() -> None:
    """The operator's checkbox is the filter: a marked group is collected."""
    ingestor = MTProtoIngestor(PoolSession([]).as_session())
    client = _FakeTelethonReader(entity=_channel(broadcast=False, megagroup=True))
    object.__setattr__(ingestor, "_client", client)

    result = await ingestor.fetch_new_messages(
        "some_group", last_max_id=None, last_scraped_at=datetime.now(UTC)
    )

    assert [message.id for message in result] == [1]


async def test_a_first_pass_takes_the_configured_window_with_no_offset() -> None:
    """No watermark means exactly `INGEST_MAX_MESSAGES_PER_SOURCE` recent posts.

    `min_id=0` matters: applying the overlap to a non-existent watermark would
    subtract from zero and only shrink an already bounded first window.
    """
    ingestor = MTProtoIngestor(PoolSession([]).as_session())
    client = _FakeTelethonReader(entity=_channel())
    object.__setattr__(ingestor, "_client", client)

    await ingestor.fetch_new_messages("news", last_max_id=None)

    assert client.calls[-1]["min_id"] == 0
    # Freshness scans past the keep-limit so yesterday's posts don't eat the budget.
    assert client.calls[-1]["limit"] == max(
        settings.ingest_max_messages_per_source * 4, 50
    )


async def test_a_later_pass_reads_only_new_posts_from_the_watermark() -> None:
    """`min_id` is exclusive in Telethon, so this returns new posts only.

    The offset below the watermark is deliberate: Telegram allows a post to be
    edited and its media attached late, and re-reading a few messages is the only
    way to catch that. Repeats are safe because uniqueness is enforced by the
    database constraint.
    """
    ingestor = MTProtoIngestor(PoolSession([]).as_session())
    client = _FakeTelethonReader(entity=_channel())
    object.__setattr__(ingestor, "_client", client)

    await ingestor.fetch_new_messages(
        "news", last_max_id=500, last_scraped_at=datetime.now(UTC)
    )

    expected = 500 - settings.ingest_overlap_messages
    assert client.calls[-1]["min_id"] == expected
    assert client.calls[-1]["limit"] == settings.ingest_max_messages_per_source


async def test_the_watermark_offset_never_goes_negative() -> None:
    """A channel with fewer posts than the overlap window must not send min_id<0."""
    ingestor = MTProtoIngestor(PoolSession([]).as_session())
    client = _FakeTelethonReader(entity=_channel())
    object.__setattr__(ingestor, "_client", client)

    await ingestor.fetch_new_messages(
        "news", last_max_id=2, last_scraped_at=datetime.now(UTC)
    )

    assert client.calls[-1]["min_id"] == 0


async def test_messages_are_returned_oldest_first() -> None:
    """Album grouping and the watermark both assume ascending ids."""
    ingestor = MTProtoIngestor(PoolSession([]).as_session())
    client = _FakeTelethonReader(
        entity=_channel(), messages=[_message(12), _message(11), _message(10)]
    )
    object.__setattr__(ingestor, "_client", client)

    result = await ingestor.fetch_new_messages(
        "news", last_max_id=None, last_scraped_at=datetime.now(UTC)
    )

    assert [message.id for message in result] == [10, 11, 12]


async def test_marking_read_uses_send_read_acknowledge() -> None:
    ingestor = MTProtoIngestor(PoolSession([]).as_session())
    client = _FakeTelethonReader(entity=_channel())
    object.__setattr__(ingestor, "_client", client)

    assert await ingestor.mark_read("news", 140) is True
    assert client.acknowledged == [140]


async def test_marking_read_is_skipped_when_disabled() -> None:
    previous = _override_setting("ingest_mark_read", False)
    try:
        ingestor = MTProtoIngestor(PoolSession([]).as_session())
        client = _FakeTelethonReader(entity=_channel())
        object.__setattr__(ingestor, "_client", client)

        assert await ingestor.mark_read("news", 140) is False
        assert client.acknowledged == []
    finally:
        _override_setting("ingest_mark_read", previous)


async def test_a_failed_acknowledgement_is_swallowed() -> None:
    """Cosmetic by contract: the rows are already committed when this runs.

    Raising here would turn a successful pass — whose posts are safely stored —
    into a reported failure.
    """
    ingestor = MTProtoIngestor(PoolSession([]).as_session())
    client = _FakeTelethonReader(
        entity=_channel(), acknowledge_error=RuntimeError("nope")
    )
    object.__setattr__(ingestor, "_client", client)

    assert await ingestor.mark_read("news", 140) is False


def _message(
    message_id: int,
    text: str = "Текст новости",
    *,
    date: datetime | None = None,
) -> Message:
    """Build a real Telethon message.

    A duck-typed stub would be silently skipped: the reader checks
    `isinstance(message, Message)` before converting, so a stand-in would make
    this test assert an empty result and pass for the wrong reason.
    """
    return Message(
        id=message_id,
        peer_id=PeerChannel(channel_id=1),
        message=text,
        date=date or datetime.now(UTC),
        views=10,
        forwards=1,
    )


class _FakeTelethonReader:
    """Telethon client double for the reading path.

    Records the `iter_messages` arguments so a test can assert the window, which
    is the whole point of the first-pass and watermark rules.
    """

    def __init__(
        self,
        *,
        entity: Any,
        messages: list[Any] | None = None,
        acknowledge_error: Exception | None = None,
    ) -> None:
        self._entity = entity
        self._messages = messages if messages is not None else [_message(1)]
        self._acknowledge_error = acknowledge_error
        self.calls: list[dict[str, Any]] = []
        self.acknowledged: list[int] = []

    async def get_entity(self, channel: str) -> Any:
        return self._entity

    def iter_messages(self, entity: Any, *, limit: int, min_id: int) -> Any:
        self.calls.append({"limit": limit, "min_id": min_id})
        messages = self._messages

        class _Iterator:
            def __aiter__(self) -> Any:
                self._index = 0
                return self

            async def __anext__(self) -> Any:
                if self._index >= len(messages):
                    raise StopAsyncIteration
                message = messages[self._index]
                self._index += 1
                return message

        return _Iterator()

    async def send_read_acknowledge(self, entity: Any, *, max_id: int) -> bool:
        if self._acknowledge_error is not None:
            raise self._acknowledge_error
        self.acknowledged.append(max_id)
        return True


def test_yesterday_is_dropped_on_a_first_pass() -> None:
    moscow = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 9, 4, 15, 0, tzinfo=moscow)
    start = moscow_today_start(now)
    yesterday = start - timedelta(seconds=1)

    assert needs_freshness_cutoff(None, now=now) is True
    assert is_on_or_after_moscow_today(yesterday, now=now) is False
    assert is_on_or_after_moscow_today(start, now=now) is True


def test_a_same_day_resume_does_not_reapply_the_date_gate() -> None:
    moscow = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 9, 4, 18, 0, tzinfo=moscow)
    scraped = datetime(2026, 9, 4, 9, 0, tzinfo=moscow)
    assert needs_freshness_cutoff(scraped, now=now) is False
    overnight = datetime(2026, 9, 3, 23, 0, tzinfo=moscow)
    assert needs_freshness_cutoff(overnight, now=now) is True


async def test_a_first_pass_keeps_only_today() -> None:

    moscow = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 9, 4, 12, 0, tzinfo=moscow)
    today = datetime(2026, 9, 4, 10, 0, tzinfo=moscow)
    yesterday = datetime(2026, 9, 3, 23, 0, tzinfo=moscow)
    ingestor = MTProtoIngestor(PoolSession([]).as_session())
    client = _FakeTelethonReader(
        entity=_channel(),
        messages=[
            _message(3, date=today),
            _message(2, date=yesterday),
            _message(1, date=yesterday),
        ],
    )
    object.__setattr__(ingestor, "_client", client)

    result = await ingestor.fetch_new_messages("news", last_max_id=None, now=now)

    assert [message.id for message in result] == [3]


# ==================================================== dialog links and avatars


def test_a_public_channel_links_to_its_t_me_page() -> None:
    """The owner clicks this link to check the channel before enabling it."""
    entity = _channel(username="tass_agency")

    assert peer_invite_link(entity) == "https://t.me/tass_agency"
    assert peer_identifier(entity) == "@tass_agency"


def test_a_private_channel_links_through_the_c_form() -> None:
    """A subscribed account resolves `t.me/c/<id>`, so the row is checkable.

    Returning nothing here is what left fifteen rows with "нет публичной
    ссылки" on the production page: a private channel has no username, but the
    collecting account is already subscribed to it.
    """
    entity = _channel(id=1117628569, username=None)

    assert peer_invite_link(entity) == "https://t.me/c/1117628569"
    assert peer_identifier(entity) == "-1001117628569"


def test_a_user_without_a_username_links_by_id() -> None:
    entity = User(id=777000, first_name="Telegram")

    assert peer_invite_link(entity) == "tg://user?id=777000"


async def test_the_avatar_uses_the_profile_photo_call() -> None:
    """`download_media` returns None for an entity; `download_profile_photo` works.

    This is the bug that produced a page of letter placeholders: the import ran
    without error and stored no avatar at all.
    """
    ingestor = MTProtoIngestor(PoolSession([]).as_session())
    calls: list[dict[str, Any]] = []

    class _AvatarClient:
        async def download_profile_photo(
            self, entity: Any, *, file: Any, download_big: bool
        ) -> bytes:
            calls.append({"download_big": download_big})
            return b"\xff\xd8\xff\xd9"

        async def download_media(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("an entity avatar must not go through download_media")

    object.__setattr__(ingestor, "_client", _AvatarClient())

    avatar = await ingestor._avatar(_channel(photo=object()))

    assert avatar is not None
    assert avatar.startswith("data:image/jpeg;base64,")
    assert calls == [{"download_big": False}]


async def test_a_peer_without_a_photo_needs_no_download() -> None:
    ingestor = MTProtoIngestor(PoolSession([]).as_session())

    class _NoDownloads:
        async def download_profile_photo(self, *args: Any, **kwargs: Any) -> bytes:
            raise AssertionError("nothing to download")

    object.__setattr__(ingestor, "_client", _NoDownloads())

    assert await ingestor._avatar(_channel(photo=None)) is None


# ============================================== duplicate dialogs on one peer


class _Row:
    """The slice of `Source` the import matcher reads."""

    def __init__(
        self,
        *,
        row_id: int,
        identifier: str,
        telegram_id: int | None = None,
        username: str | None = None,
        posts_collected: int = 0,
        is_active: bool = False,
        peer_kind: str | None = "channel",
    ) -> None:
        self.id = row_id
        self.identifier = identifier
        self.telegram_id = telegram_id
        self.username = username
        self.posts_collected = posts_collected
        self.is_active = is_active
        self.peer_kind = peer_kind


def _dialog(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "telegram_id": 1754252633,
        "identifier": "@toporlive",
        "title": "Топор Live",
        "username": "toporlive",
        "kind": TelegramPeerKind.CHANNEL,
        "description": "",
        "invite_link": "https://t.me/toporlive",
        "participants": None,
        "avatar_data_uri": None,
    }
    values.update(overrides)
    return DialogDiscovery(**values)


def test_one_peer_stored_twice_is_matched_as_one() -> None:
    """`@toporlive` and `-1001754252633` are the same channel.

    Production ended up with both because an earlier import wrote the numeric
    form. Matching only on the identifier is what showed the owner two rows.
    """
    rows = [
        _Row(row_id=1, identifier="@toporlive", telegram_id=1754252633),
        _Row(row_id=2, identifier="-1001754252633", telegram_id=1754252633),
    ]

    matches = _matching_sources(cast(Any, rows), _dialog())

    assert {row.id for row in matches} == {1, 2}


def test_a_username_matches_with_and_without_the_at_sign() -> None:
    """`rian_ru` versus `@rian_ru`: one peer, two spellings, no telegram_id."""
    rows = [
        _Row(row_id=1, identifier="rian_ru"),
        _Row(row_id=2, identifier="@rian_ru", username="rian_ru"),
    ]

    matches = _matching_sources(
        cast(Any, rows),
        _dialog(telegram_id=0, identifier="@rian_ru", username="rian_ru"),
    )

    assert {row.id for row in matches} == {1, 2}


def test_a_retired_duplicate_is_not_matched_again() -> None:
    """Once merged, a row must not be reconsidered on the next import."""
    rows = [
        _Row(row_id=1, identifier="@toporlive", telegram_id=1754252633),
        _Row(
            row_id=2,
            identifier="-1001754252633#dup2",
            telegram_id=1754252633,
            peer_kind="merged_duplicate",
        ),
    ]

    matches = _matching_sources(cast(Any, rows), _dialog())

    assert [row.id for row in matches] == [1]


def test_the_row_with_collected_posts_survives_a_merge() -> None:
    """History hangs off `source_id`, so the row holding posts must be kept."""
    empty = _Row(row_id=1, identifier="@toporlive", telegram_id=1754252633)
    with_history = _Row(
        row_id=2,
        identifier="-1001754252633",
        telegram_id=1754252633,
        posts_collected=317,
    )

    assert _preferred_source(cast(Any, [empty, with_history])) is with_history


def test_an_enabled_row_wins_over_an_untouched_one() -> None:
    """With no history either way, the operator's tick is the signal to keep."""
    untouched = _Row(row_id=1, identifier="@toporlive", telegram_id=1754252633)
    enabled = _Row(
        row_id=2,
        identifier="-1001754252633",
        telegram_id=1754252633,
        is_active=True,
    )

    assert _preferred_source(cast(Any, [untouched, enabled])) is enabled


# ============================================ links for rows outside the import


class _LinkRow:
    """The slice of `Source` the row builder reads for its link."""

    def __init__(
        self,
        *,
        identifier: str,
        username: str | None = None,
        invite_link: str | None = None,
        source_type: SourceType = SourceType.TELEGRAM,
    ) -> None:
        self.identifier = identifier
        self.username = username
        self.invite_link = invite_link
        self.source_type = source_type


def test_a_stored_link_is_used_as_is() -> None:
    row = _LinkRow(identifier="@toporlive", invite_link="https://t.me/c/1754252633")

    assert _telegram_link(cast(Any, row)) == "https://t.me/c/1754252633"


def test_a_legacy_row_gets_a_link_from_its_identifier() -> None:
    """`tass_agency` was typed before dialog import existed and has no link.

    The account is not subscribed, so the channel never appears in the dialog
    list and nothing backfills `invite_link` — yet the owner still needs to open
    it to decide whether to resubscribe.
    """
    row = _LinkRow(identifier="tass_agency")

    assert _telegram_link(cast(Any, row)) == "https://t.me/tass_agency"


def test_a_numeric_peer_id_yields_no_invented_link() -> None:
    """`-100…` has no `t.me` form a non-subscriber can open."""
    row = _LinkRow(identifier="-1001117628569")

    assert _telegram_link(cast(Any, row)) == ""


def test_a_website_source_never_gets_a_telegram_link() -> None:
    row = _LinkRow(identifier="https://example.test/news", source_type=SourceType.WEBSITE)

    assert _telegram_link(cast(Any, row)) == ""


# ================================ public channels absent from the dialog list


async def test_a_public_channel_is_described_without_being_joined() -> None:
    """`iter_dialogs` is not the boundary of what an account can read.

    ТАСС and Интерфакс sat in production with no kind, link, avatar or
    description because they never appeared in the dialog list — while
    `get_entity` resolved both and ingestion could read them. Blaming a missing
    subscription was wrong; the importer simply never asked about them.
    """
    ingestor = MTProtoIngestor(PoolSession([]).as_session())
    entity = _channel(id=1052722179, username="tass_agency", title="ТАСС")

    class _ResolvingClient:
        async def get_entity(self, identifier: str) -> Any:
            assert identifier == "tass_agency"
            return entity

        async def download_profile_photo(self, *args: Any, **kwargs: Any) -> None:
            return None

    object.__setattr__(ingestor, "_client", _ResolvingClient())

    dialog = await ingestor.describe_peer("tass_agency")

    assert dialog is not None
    assert dialog.kind is TelegramPeerKind.CHANNEL
    assert dialog.identifier == "@tass_agency"
    assert dialog.invite_link == "https://t.me/tass_agency"
    assert dialog.title == "ТАСС"


async def test_an_unresolvable_peer_is_skipped_not_raised() -> None:
    """One renamed channel must not fail a refresh covering dozens of rows."""
    ingestor = MTProtoIngestor(PoolSession([]).as_session())

    class _FailingClient:
        async def get_entity(self, identifier: str) -> Any:
            raise ValueError("no such peer")

    object.__setattr__(ingestor, "_client", _FailingClient())

    assert await ingestor.describe_peer("gone_channel") is None
