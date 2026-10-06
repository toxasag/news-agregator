"""Tests for the dashboard: auth, HTMX partials and the SSE log stream.

No external services. PostgreSQL is replaced by a session double injected through
FastAPI's dependency override, Redis is `fakeredis`, and the app is driven through
`httpx.ASGITransport` **without** its lifespan — the real lifespan health-checks
PostgreSQL and Redis and would refuse to start, which is correct in production and
useless here.

The security tests are the point of this file. A dashboard guard that can be
bypassed, a cookie without `HttpOnly`, or a login form that redirects wherever a
query parameter says are each a whole-system compromise, so each is asserted
directly rather than assumed from the implementation.
"""

# Fixtures are Russian editorial copy, so Cyrillic letters that look like Latin
# ones are the subject matter.
# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    MutableMapping,
    Sequence,
)
from datetime import UTC, datetime
from typing import Any, Final, cast
from unittest.mock import patch

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fakeredis.aioredis import FakeRedis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import dashboard
from app.api.auth import LOGIN_PATH
from app.api.dashboard import TEMPLATES_DIR
from app.api.logs_sse import (
    EVENTS_CHANNEL,
    LOGS_HISTORY_KEY,
    RedisLogHandler,
    format_sse,
    publish_event,
    stream_events,
)
from app.config import Settings, settings
from app.core.security import (
    SecretBox,
    TokenError,
    authenticate_admin,
    create_session_token,
    decode_session_token,
    hash_password,
    session_subject_is_current,
    verify_password,
)
from app.database import get_db
from app.main import app
from app.models.article import Article
from app.models.enums import ArticleStatus, SourceType
from app.services.admin_credentials import ADMIN_CREDENTIALS_KEY
from app.services.categories import CATEGORY_MAP
from app.services.web_scraper import CrawlPreview, ScraperError
from app.workers.ingestion_worker import IngestionRunReport, SourceIngestReport

ADMIN_PASSWORD: Final[str] = "correct-horse-battery"
COOKIE: Final[str] = settings.session_cookie_name


# ============================================================ session double


class _Scalars:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        return list(self._rows)


def _selects_admin_credentials(statement: Any) -> bool:
    """Whether a statement is the admin-credential lookup.

    Matched on the rendered SQL rather than on call order, so a page test does
    not have to know how many guard queries preceded its own.
    """
    try:
        compiled = getattr(statement, "compile", lambda **_: statement)(
            compile_kwargs={"literal_binds": True}
        )
        return ADMIN_CREDENTIALS_KEY in str(compiled)
    except (SQLAlchemyError, TypeError, ValueError):
        return ADMIN_CREDENTIALS_KEY in str(statement)


class _Result:
    """The slice of `sqlalchemy.Result` the dashboard consumes."""

    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        """Return the single scalar, or ``None`` when nothing was scripted.

        Used by the admin-credential lookup that now runs on every authenticated
        request. Returning ``None`` for an empty result is what makes the tests
        exercise the `.env` fallback path, which is the state a fresh deployment
        is in.
        """
        if not self._rows:
            return None
        first = self._rows[0]
        return first[0] if isinstance(first, tuple) else first

    def scalar_one(self) -> Any:
        """Return the single scalar of a COUNT-style query.

        The articles pager runs `select(count()).select_from(...)` against the
        same predicates as the page query, so the scripted queue holds the count
        row before the page rows.
        """
        if not self._rows:
            return 0
        first = self._rows[0]
        return first[0] if isinstance(first, tuple) else first

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def one(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """In-memory stand-in for `AsyncSession`.

    `execute` answers from a queue so one request can script the several queries a
    dashboard page issues; `scalar` returns counts. Anything unscripted answers
    empty, which is what an untouched deployment looks like.
    """

    def __init__(
        self,
        *,
        results: Sequence[Sequence[Any]] = (),
        scalars: Sequence[int] = (),
        objects: dict[int, Any] | None = None,
    ) -> None:
        self._results = list(results)
        self._scalars = list(scalars)
        self._objects = dict(objects or {})
        self.executed = 0
        self.commits = 0
        self.rollbacks = 0
        #: Answer for the admin-credential lookup; None means "no row",
        #: which makes the code fall back to the environment.
        self._credentials: Any = None
        #: Set to an exception to make `commit` fail, for the error paths.
        self.commit_error: Exception | None = None

    def queue_credentials(self, document: Any) -> None:
        """Make the next credential lookup return ``document``.

        Answered out of band rather than from the `results` queue because the
        lookup now runs on *every* authenticated request: consuming a queue slot
        would shift every scripted result by one and make each page test depend on
        how many guards ran before it.
        """
        self._credentials = document

    async def execute(self, statement: Any) -> _Result:
        self.executed += 1
        if _selects_admin_credentials(statement):
            return _Result([self._credentials] if self._credentials is not None else [])
        rows = self._results.pop(0) if self._results else []
        return _Result(rows)

    async def scalar(self, statement: Any) -> int:
        return self._scalars.pop(0) if self._scalars else 0

    async def get(self, entity: Any, ident: Any) -> Any:
        return self._objects.get(ident)

    def add(self, instance: Any) -> None:
        self._objects[getattr(instance, "id", id(instance))] = instance

    async def refresh(self, instance: Any) -> None:
        return None

    async def commit(self) -> None:
        """Record the commit, or raise what the test scripted.

        Counted rather than ignored so a test can prove a write was actually
        persisted, and `commit_error` lets one script a database failure to check
        the handler rolls back instead of reporting success.
        """
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        return None

    def as_session(self) -> AsyncSession:
        return cast(AsyncSession, self)


def build_article(**overrides: Any) -> Article:
    """Build a transient article. Nothing is persisted."""
    values: dict[str, Any] = {
        "id": 5150,
        "category": "novosti-obshhestva",
        "title": "В Москве открыли станцию метро «Пыхтино»",
        "content_html": "<h2>Что открыли</h2><p>Мэрия открыла станцию.</p>",
        "seo_title": "Станция «Пыхтино» открыта",
        "seo_description": "Мэрия открыла станцию «Пыхтино».",
        "seo_keywords": ["станция пыхтино"],
        "url_slug": "pyhtino-abc123",
        "status": ArticleStatus.MEDIA_READY,
        "platform_statuses": {},
        "video_links": [],
        "retry_count": 0,
    }
    values.update(overrides)
    return Article(**values)


class _Source:
    """The slice of `Source` the sources page renders."""

    def __init__(self, source_type: SourceType, name: str) -> None:
        self.id = 1
        self.source_type = source_type
        self.name = name
        self.identifier = "@channel" if source_type is SourceType.TELEGRAM else "https://x.test"
        self.is_active = True
        self.skip_media = False
        # New in migration 0004; the sources table renders all of these.
        self.fetch_media = True
        self.telegram_id: int | None = 12345
        self.avatar_data_uri: str | None = None
        self.posts_collected = 0
        self.last_scraped_at = None
        self.peer_kind: str | None = "channel"
        self.username: str | None = "channel"
        self.description: str | None = "Новостной канал"
        self.invite_link: str | None = "https://t.me/channel"
        self.crawl_rules: dict[str, Any] = {"title_selector": "h1"}

        class _Strategy:
            value = "ocr_check"

        self.media_strategy = _Strategy()


# ================================================================== fixtures


def _override_setting(name: str, value: object) -> object:
    """Set one field on the frozen settings instance, returning the old value.

    `Settings` is a frozen pydantic model, so plain assignment raises and patching
    the *class* attribute is silently ignored — the instance's own `__dict__` holds
    the resolved value and shadows it. `object.__setattr__` is what actually takes
    effect, which is why this helper exists rather than a bare `monkeypatch.setattr`.
    """
    previous = getattr(settings, name)
    object.__setattr__(settings, name, value)
    return previous


@pytest.fixture(autouse=True)
def _configure_admin() -> Iterator[None]:
    """Install a known administrator credential for the duration of a test."""
    previous_hash = _override_setting(
        "admin_password_hash", hash_password(ADMIN_PASSWORD)
    )
    previous_user = _override_setting("admin_username", "admin")
    try:
        yield
    finally:
        _override_setting("admin_password_hash", previous_hash)
        _override_setting("admin_username", previous_user)


@pytest.fixture
def session_double() -> FakeSession:
    """A session double with plausible counters for the overview."""
    return FakeSession(scalars=[120, 34, 12, 5, 7])


@pytest.fixture
def client(session_double: FakeSession) -> Iterator[httpx.AsyncClient]:
    """An ASGI client with the database dependency overridden.

    The lifespan is deliberately not run: it health-checks PostgreSQL and Redis,
    which do not exist in this suite.
    """

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session_double.as_session()

    app.dependency_overrides[get_db] = _override
    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(
        transport=transport, base_url="http://dashboard.test", follow_redirects=False
    )
    app.dependency_overrides.clear()


async def login(client: httpx.AsyncClient) -> str:
    """Sign in and leave the session cookie on the client.

    The cookie is set on the client jar rather than passed per request: httpx
    deprecates per-request `cookies=`, and this suite turns warnings into errors.
    """
    response = await client.post(
        "/auth/login", data={"username": "admin", "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 303, response.text
    cookie = response.cookies.get(COOKIE)
    assert cookie, "login did not set the session cookie"
    client.cookies.set(COOKIE, cookie)
    return cookie


# ================================================================== security


def test_a_password_verifies_against_its_hash() -> None:
    digest = hash_password(ADMIN_PASSWORD)

    assert verify_password(ADMIN_PASSWORD, digest) is True
    assert verify_password("wrong", digest) is False


def test_a_malformed_hash_rejects_every_password() -> None:
    # An unconfigured deployment must reject logins, not answer them with a 500.
    assert verify_password(ADMIN_PASSWORD, "not-a-bcrypt-hash") is False
    assert verify_password(ADMIN_PASSWORD, "") is False


def test_an_empty_password_is_refused_at_hash_time() -> None:
    # Hashing "" succeeds in bcrypt and would then accept an empty login form.
    with pytest.raises(ValueError, match="empty"):
        hash_password("")


def test_an_overlong_password_is_refused() -> None:
    # bcrypt truncates at 72 bytes; failing loudly beats silently ignoring the tail.
    with pytest.raises(ValueError, match="72"):
        hash_password("x" * 100)


def test_authentication_requires_both_halves() -> None:
    assert authenticate_admin("admin", ADMIN_PASSWORD) is True
    assert authenticate_admin("admin", "wrong") is False
    assert authenticate_admin("intruder", ADMIN_PASSWORD) is False


def test_a_session_token_round_trips() -> None:
    token = create_session_token("admin")

    assert decode_session_token(token.token) == "admin"
    assert token.max_age_seconds > 0


def test_an_expired_token_is_rejected() -> None:
    token = create_session_token("admin", expires_in_minutes=-1)

    with pytest.raises(TokenError, match="expired"):
        decode_session_token(token.token)


def test_a_tampered_token_is_rejected() -> None:
    token = create_session_token("admin")

    with pytest.raises(TokenError):
        decode_session_token(token.token[:-4] + "AAAA")


def test_a_token_for_another_user_is_rejected() -> None:
    """A validly signed token naming a different administrator must not pass.

    The identity check moved out of `decode_session_token` when the username
    became runtime-editable: the signature check stays synchronous, while the
    name is compared against the live value by the request guard. Both halves are
    asserted here so neither can be dropped silently.
    """
    token = create_session_token("someone-else")

    # The signature is valid, so decoding succeeds and returns the subject.
    assert decode_session_token(token.token) == "someone-else"
    # The subject is not the current administrator, which is what the guards
    # check before letting the request through.
    assert session_subject_is_current("someone-else", expected="admin") is False
    assert session_subject_is_current("admin", expected="admin") is True


async def test_a_renamed_admin_invalidates_an_old_session(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    """Renaming the account must end sessions issued under the old name.

    Otherwise a rename would leave every previously issued cookie working, and
    renaming is exactly what an attacker holding a stolen cookie would do.
    """
    async with client:
        await login(client)
        # The dashboard answers while the name still matches.
        assert (await client.get("/api/ui/stats")).status_code == 200

        # Simulate the profile page having stored a different username.
        session_double.queue_credentials(
            {"username": "renamed", "password_hash": "$2b$12$x"}
        )
        assert (await client.get("/api/ui/stats")).status_code == 401


def test_an_empty_token_is_rejected() -> None:
    with pytest.raises(TokenError):
        decode_session_token("")


def test_the_secret_box_round_trips_and_masks() -> None:
    box = SecretBox(Fernet.generate_key().decode("ascii"))
    ciphertext = box.encrypt("or-v1-abcdef123456")

    assert ciphertext != "or-v1-abcdef123456"
    assert box.decrypt(ciphertext) == "or-v1-abcdef123456"
    # The mask shows enough to identify the key, not enough to use it.
    assert box.mask(ciphertext).endswith("3456")
    assert "abcdef" not in box.mask(ciphertext)


def test_a_tampered_ciphertext_decrypts_to_empty() -> None:
    box = SecretBox(Fernet.generate_key().decode("ascii"))

    # Fernet is authenticated, so tampering is detected rather than yielding
    # garbage that would later be sent to an API as a credential.
    assert box.decrypt("gAAAAABtampered") == ""


def test_an_unset_fernet_key_is_refused() -> None:
    with pytest.raises(ValueError, match="FERNET_KEY"):
        SecretBox("")


# ========================================================== reverse proxy


def test_csv_settings_are_split_and_stripped() -> None:
    parsed = Settings(
        allowed_hosts=" svodkanews.example.ru , 127.0.0.1 ,, ",
        cors_allow_origins="https://a.example, https://b.example",
        trusted_proxy_hosts="10.0.0.5, 10.0.0.6",
    )

    assert parsed.allowed_hosts_list == ("svodkanews.example.ru", "127.0.0.1")
    assert parsed.cors_allow_origins_list == (
        "https://a.example",
        "https://b.example",
    )
    assert parsed.trusted_proxy_hosts_list == ["10.0.0.5", "10.0.0.6"]


def test_a_wildcard_proxy_is_preserved_verbatim() -> None:
    # uvicorn expects the bare string for "any source"; a one-element list would
    # be read as a host literally named `*`.
    assert Settings(trusted_proxy_hosts="*").trusted_proxy_hosts_list == "*"


def _production_settings(**overrides: str) -> Settings:
    """Build production settings whose secrets all pass, then apply overrides.

    Every secret guard has to be satisfied for the validator to reach the setting
    under test; without this the reverse-proxy tests would pass for the wrong
    reason, raising on a missing `SECRET_KEY` instead of the value being asserted.
    """
    values: dict[str, str] = {
        "app_env": "production",
        "secret_key": "x" * 40,
        "postgres_password": "a-real-password",
        "admin_password_hash": "$2b$12$" + "x" * 53,
        "fernet_key": Fernet.generate_key().decode("ascii"),
        "allowed_hosts": "svodkanews.example.ru,127.0.0.1,localhost",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_production_refuses_to_trust_every_proxy() -> None:
    """`X-Forwarded-Proto` from anywhere is a spoofable security control.

    Any direct caller could declare its plain-HTTP request to be HTTPS, which is
    enough to make the app emit `Secure` cookies over cleartext and to forge the
    client IP in the audit log.
    """
    with pytest.raises(ValueError, match="TRUSTED_PROXY_HOSTS"):
        _production_settings(trusted_proxy_hosts="*")


def test_production_refuses_wildcard_cors_with_cookie_auth() -> None:
    with pytest.raises(ValueError, match="CORS_ALLOW_ORIGINS"):
        _production_settings(cors_allow_origins="*")


def test_a_valid_production_configuration_is_accepted() -> None:
    """The guards must not be so strict that a correct deployment cannot boot."""
    configured = _production_settings(trusted_proxy_hosts="150.241.98.133,127.0.0.1")

    assert configured.is_production
    assert configured.trusted_proxy_hosts_list == ["150.241.98.133", "127.0.0.1"]


def test_production_requires_a_host_allowlist() -> None:
    """An unset allowlist skips the middleware, leaving `Host` unvalidated.

    `TrustedHostMiddleware` is registered only when `ALLOWED_HOSTS` is non-empty,
    so an unset value is not a permissive configuration but no configuration at
    all — indistinguishable from a working one in a deployment log.
    """
    with pytest.raises(ValueError, match="ALLOWED_HOSTS is unset"):
        _production_settings(allowed_hosts="")


@pytest.mark.parametrize("value", ["*", "127.0.0.1,*", "svodka.example.ru, * "])
def test_production_refuses_a_wildcard_host(value: str) -> None:
    """A `*` entry makes Starlette set `allow_any` and stop checking entirely.

    The dangerous case is the list form: `127.0.0.1,*` satisfies a naive loopback
    substring check, so the control reports itself as configured while accepting
    every forged `Host`.
    """
    with pytest.raises(ValueError, match="ALLOWED_HOSTS contains"):
        _production_settings(allowed_hosts=value)


def test_production_requires_loopback_in_the_host_allowlist() -> None:
    """The container healthcheck probes 127.0.0.1 and must not be rejected.

    Allowlisting only the public name makes `HEALTHCHECK` and the deploy probe
    400, so Docker restarts a container that is serving correctly.
    """
    with pytest.raises(ValueError, match=re.escape("ALLOWED_HOSTS omits 127.0.0.1")):
        _production_settings(allowed_hosts="svodkanews.example.ru")


async def test_the_configured_allowlist_admits_loopback_and_rejects_forgery() -> None:
    """The accepted configuration behaves correctly once handed to Starlette.

    Driven from `Settings` rather than a literal list, so narrowing the
    configuration fails this test instead of leaving it green.
    """
    configured = _production_settings(
        allowed_hosts="svodkanews.example.ru,127.0.0.1,localhost"
    )
    guarded = TrustedHostMiddleware(
        _ok_app, allowed_hosts=list(configured.allowed_hosts_list)
    )

    for host in configured.allowed_hosts_list:
        assert await _status_for_host(guarded, host) == 200, host
    # A forged Host is still rejected: otherwise it is reflected into the
    # post-login redirect, turning it into an open redirect.
    assert await _status_for_host(guarded, "evil.test") == 400


async def _ok_app(
    scope: MutableMapping[str, Any],
    receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
    send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
) -> None:
    """Minimal ASGI app that always answers 200."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _status_for_host(app_: Any, host: str) -> int:
    """Drive one request through ``app_`` and return the response status."""
    statuses: list[int] = []

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            statuses.append(int(message["status"]))

    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", host.encode())],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    await app_(scope, receive, send)
    return statuses[0]


# ================================================================ login flow


async def test_the_login_page_is_public(client: httpx.AsyncClient) -> None:
    async with client:
        response = await client.get(LOGIN_PATH)

    assert response.status_code == 200
    assert "Войти" in response.text


async def test_a_valid_login_sets_a_hardened_cookie(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.post(
            "/auth/login", data={"username": "admin", "password": ADMIN_PASSWORD}
        )

    assert response.status_code == 303
    header = response.headers.get("set-cookie", "").lower()
    # HttpOnly is what stops an XSS anywhere on the dashboard from stealing the
    # session; SameSite=Lax is what makes CSRF against the mutating endpoints
    # impractical.
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header


async def test_a_failed_login_returns_to_the_form(client: httpx.AsyncClient) -> None:
    async with client:
        response = await client.post(
            "/auth/login", data={"username": "admin", "password": "wrong"}
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"{LOGIN_PATH}?error=1"


async def test_a_failed_login_message_does_not_say_which_half_was_wrong(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.get(f"{LOGIN_PATH}?error=1")

    # Distinguishing them would tell an attacker when they had found the account.
    assert "Неверный логин или пароль" in response.text
    assert response.status_code == 401


async def test_the_login_redirect_honours_a_safe_next(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.post(
            "/auth/login",
            data={
                "username": "admin",
                "password": ADMIN_PASSWORD,
                "next": "/articles?status=media_ready",
            },
        )

    assert response.headers["location"] == "/articles?status=media_ready"


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.test/phish",
        "//evil.test/phish",
        "http://evil.test",
        "/\\evil.test",
    ],
)
async def test_the_login_redirect_refuses_an_external_target(
    client: httpx.AsyncClient, hostile: str
) -> None:
    async with client:
        response = await client.post(
            "/auth/login",
            data={
                "username": "admin",
                "password": ADMIN_PASSWORD,
                "next": hostile,
            },
        )

    # Without this check the login form is an open redirect: sign in, get handed to
    # a convincing clone.
    assert response.headers["location"] == "/"


async def test_logout_clears_the_cookie(client: httpx.AsyncClient) -> None:
    async with client:
        await login(client)
        response = await client.post("/auth/logout")

    assert response.status_code == 303
    assert response.headers["location"] == LOGIN_PATH
    header = response.headers.get("set-cookie", "").lower()
    # Cleared by expiry, and with matching attributes — otherwise the browser keeps
    # the original cookie and the operator stays signed in.
    assert 'svodka_session=""' in header or "svodka_session=;" in header


async def test_logout_needs_no_valid_session(client: httpx.AsyncClient) -> None:
    # A caller holding a broken cookie must still be able to clear it.
    async with client:
        client.cookies.set(COOKIE, "garbage.token.value")
        response = await client.post("/auth/logout")

    assert response.status_code == 303


async def test_whoami_reports_the_session_owner(client: httpx.AsyncClient) -> None:
    async with client:
        await login(client)
        response = await client.get("/auth/me")

    assert response.status_code == 200
    assert response.json() == {"username": "admin"}


# ============================================================= route guards


@pytest.mark.parametrize(
    "path", ["/", "/articles", "/sources", "/settings", "/logs"]
)
async def test_a_page_redirects_an_anonymous_visitor(
    client: httpx.AsyncClient, path: str
) -> None:
    async with client:
        response = await client.get(path)

    # 303 so the browser follows up with GET even if the original was a POST.
    assert response.status_code == 303
    assert response.headers["location"].startswith(LOGIN_PATH)


async def test_the_redirect_preserves_the_requested_path(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.get("/articles?status=failed")

    assert response.headers["location"] == f"{LOGIN_PATH}?next=/articles?status=failed"


async def test_an_expired_cookie_redirects_rather_than_erroring(
    client: httpx.AsyncClient,
) -> None:
    stale = create_session_token("admin", expires_in_minutes=-1).token

    async with client:
        client.cookies.set(COOKIE, stale)
        response = await client.get("/")

    assert response.status_code == 303


@pytest.mark.parametrize(
    "path",
    [
        "/api/ui/stats",
        "/api/ui/articles/table",
        "/api/ui/articles/1/edit",
        "/api/ui/articles/1/preview",
        "/api/ui/articles/1/journal",
        "/api/logs/stream",
    ],
)
async def test_a_partial_returns_401_for_an_anonymous_caller(
    client: httpx.AsyncClient, path: str
) -> None:
    async with client:
        response = await client.get(path)

    # A redirect here would paste a login page into a table cell.
    assert response.status_code == 401


async def test_a_partial_rejects_a_tampered_cookie(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        client.cookies.set(COOKIE, "not.a.real.token")
        response = await client.get("/api/ui/stats")

    assert response.status_code == 401


# =============================================================== dashboard pages


async def test_the_overview_renders_its_counters(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await login(client)
        response = await client.get("/")

    assert response.status_code == 200
    assert "Обзор конвейера" in response.text
    # The scripted counters reach the page.
    assert "120" in response.text
    assert "Готовность воркеров" in response.text


async def test_the_overview_is_marked_noindex(client: httpx.AsyncClient) -> None:
    async with client:
        await login(client)
        response = await client.get("/")

    # An internal control panel must never reach a search index.
    assert "noindex" in response.text


async def test_the_articles_page_lists_articles(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    # COUNT first, then the page query.
    session_double._results = [[1], [build_article()]]

    async with client:
        await login(client)
        response = await client.get("/articles")

    assert response.status_code == 200
    assert "Пыхтино" in response.text
    assert "media_ready" in response.text


async def test_the_articles_page_shows_an_empty_state(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await login(client)
        response = await client.get("/articles")

    assert "Материалов нет" in response.text


async def test_the_sources_page_separates_the_two_source_types(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    session_double._results = [
        [_Source(SourceType.TELEGRAM, "Канал"), _Source(SourceType.WEBSITE, "Сайт")]
    ]

    async with client:
        await login(client)
        response = await client.get("/sources")

    assert response.status_code == 200
    assert "Telegram-диалоги" in response.text
    assert "Сбор" in response.text
    assert "Конструктор селекторов" in response.text


async def test_the_settings_page_masks_every_key(client: httpx.AsyncClient) -> None:
    secret = "or-v1-supersecret-key-value"
    previous = _override_setting("openrouter_api_key", secret)

    try:
        async with client:
            await login(client)
            response = await client.get("/settings")
    finally:
        _override_setting("openrouter_api_key", previous)

    assert response.status_code == 200
    assert "Ключи доступа" in response.text
    # The credential must not be recoverable from the page.
    assert secret not in response.text
    assert "supersecret" not in response.text
    # Only the masked tail is rendered.
    assert "•" in response.text


async def test_the_settings_page_renders_the_model_matrix(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await login(client)
        response = await client.get("/settings/ai")

    assert response.status_code == 200
    assert "Матрица назначения моделей" in response.text
    assert "role_writer" in response.text


async def test_the_settings_page_renders_wp_connection_card(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await login(client)
        response = await client.get("/settings")

    assert response.status_code == 200
    assert "Подключение WordPress" in response.text
    assert "wp-conn-container" in response.text
    assert "wp-test-result" in response.text
    assert "wp-sync-result" in response.text


async def test_the_settings_service_grid_and_drawer_panes(
    client: httpx.AsyncClient,
) -> None:
    """The /settings page renders independent service cards and the slide-over drawer."""
    async with client:
        await login(client)
        # Test full settings page
        page_resp = await client.get("/settings")
        assert page_resp.status_code == 200
        assert "settings-service-grid" in page_resp.text
        assert "service-drawer-root" in page_resp.text
        assert "ServiceDrawer" in page_resp.text
        # Every service card is present independently (no bundling)
        assert "Яндекс Метрика" in page_resp.text
        assert "Яндекс Вебмастер" in page_resp.text
        assert "Яндекс Wordstat" in page_resp.text
        assert "Google Analytics 4" in page_resp.text
        assert "Google Search Console" in page_resp.text
        assert "Bing Webmaster" in page_resp.text
        assert "IndexNow" in page_resp.text
        assert "ВКонтакте & Видео" in page_resp.text
        assert "Telegram Канал" in page_resp.text

        # Test partial grid endpoint
        grid_resp = await client.get("/api/ui/settings/grid")
        assert grid_resp.status_code == 200
        assert (
            "data-service-card" in grid_resp.text
            or "card-test-wordpress" in grid_resp.text
        )
        assert "card-test-yandex_metrika" in grid_resp.text
        assert "card-test-telegram" in grid_resp.text


async def test_post_wp_connection_save_updates_config(
    session_double: FakeSession, client: httpx.AsyncClient
) -> None:
    async with client:
        await login(client)
        response = await client.post(
            "/api/ui/settings/wordpress/save",
            data={
                "wp_base_url": "https://svodka.press",
                "wp_app_user": "news_bot",
                "wp_app_password": "super-secret-wp-password",
                "ssh_host": "144.31.141.17",
                "ssh_user": "root",
                "ssh_port": "22",
                "wp_document_root": "/var/www/svodkapress",
                "is_active": "true",
            },
        )

    assert response.status_code == 200
    assert session_double.commits == 1
    assert "Подключение WordPress" in response.text
    assert "https://svodka.press" in response.text
    assert "news_bot" in response.text
    assert "super-secret-wp-password" not in response.text
    assert "••••••••" in response.text


async def test_post_wp_connection_save_unauthenticated(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        response = await client.post(
            "/api/ui/settings/wordpress/save",
            data={"wp_base_url": "https://svodka.press"},
        )

    # Requires admin authentication
    assert response.status_code in (401, 303)


async def test_post_wp_connection_test_diagnostics(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await login(client)
        response = await client.post("/api/ui/settings/wordpress/test")

    assert response.status_code == 200
    assert "REST API" in response.text
    assert "SSH" in response.text


@respx.mock
async def test_post_wp_connection_sync_categories(
    client: httpx.AsyncClient,
) -> None:
    orig_map = dict(CATEGORY_MAP)
    respx.get(url__regex=r".*/wp-json/wp/v2/categories.*").respond(
        status_code=200,
        json=[
            {"id": 1, "name": "Новости", "slug": "news", "count": 10},
        ],
    )
    try:
        async with client:
            await login(client)
            response = await client.post("/api/ui/settings/wordpress/sync_categories")

        assert response.status_code == 200
        assert "Синхронизация каталога рубрик" in response.text
        assert "Новости" in response.text
    finally:
        CATEGORY_MAP.clear()
        CATEGORY_MAP.update(orig_map)


async def test_the_logs_page_renders_the_terminal(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await login(client)
        response = await client.get("/logs")

    assert response.status_code == 200
    assert "/api/logs/stream" in response.text
    assert "EventSource" in response.text


# =============================================================== HTMX partials


async def test_the_stats_partial_is_a_fragment(client: httpx.AsyncClient) -> None:
    async with client:
        await login(client)
        response = await client.get("/api/ui/stats")

    assert response.status_code == 200
    # A fragment, not a document: HTMX swaps it into an existing element.
    assert "<!DOCTYPE html>" not in response.text
    assert "Готовность воркеров" in response.text


async def test_the_articles_table_block_renders_rows(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    session_double._results = [[2], [build_article()]]

    async with client:
        await login(client)
        response = await client.get("/api/ui/articles/table")

    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text
    assert "<tr" in response.text
    assert "Пыхтино" in response.text


async def test_the_articles_table_block_accepts_filters(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    # Two scripted results: the count query runs first, then the page query.
    session_double._results = [[1], [build_article()]]

    async with client:
        await login(client)
        response = await client.get(
            "/api/ui/articles/table?status=media_ready"
            "&category=novosti-moskvy&period=24h&media=yes&search=метро",
        )

    assert response.status_code == 200


async def test_an_unknown_status_filter_is_ignored_not_rejected(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await login(client)
        response = await client.get(
            "/api/ui/articles/table?status=not-a-status"
        )

    # The value comes from an editable query string; a 422 would be a worse
    # experience than showing everything.
    assert response.status_code == 200


async def test_the_edit_partial_renders_the_article(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    session_double._objects = {5150: build_article()}

    async with client:
        await login(client)
        response = await client.get(
            "/api/ui/articles/5150/edit"
        )

    assert response.status_code == 200
    assert "Пыхтино" in response.text
    assert "seo_title" in response.text or "SEO-заголовок" in response.text


async def test_article_journal_renders_a_missing_history_honestly(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    session_double._objects = {5150: build_article()}

    async with client:
        await login(client)
        response = await client.get("/api/ui/articles/5150/journal")

    assert response.status_code == 200
    assert "нет постоянного журнала" in response.text


async def test_the_edit_partial_escapes_the_article_html(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    session_double._objects = {
        5150: build_article(
            content_html="<p>ok</p><script>alert(1)</script>",
            title="<img src=x onerror=alert(1)>",
        )
    }

    async with client:
        await login(client)
        response = await client.get(
            "/api/ui/articles/5150/edit"
        )

    # The body is model output that quoted scraped pages. The dashboard must not be
    # the one place where it finally executes.
    #
    # The property under test is that no *tag* can form, not that the characters
    # disappear: `onerror=alert(1)` legitimately survives as inert text inside a
    # quoted attribute value, and asserting on that substring would fail against
    # correctly escaped output. What must never appear is an unescaped `<`.
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "<img src=x" not in response.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text


async def test_a_missing_article_returns_404(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await login(client)
        response = await client.get(
            "/api/ui/articles/999999/edit"
        )

    assert response.status_code == 404
    assert "не найден" in response.text


async def test_article_preview_renders_inspector_modal(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    session_double._objects = {5150: build_article()}

    async with client:
        await login(client)
        response = await client.get("/api/ui/articles/5150/preview")

    assert response.status_code == 200
    assert "Инспектор материала" in response.text
    assert "Пыхтино" in response.text
    assert "Одобрить и опубликовать" in response.text
    assert "Переписать" in response.text


async def test_article_preview_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await login(client)
        response = await client.get("/api/ui/articles/999999/preview")

    assert response.status_code == 404
    assert "не найден" in response.text


async def test_article_approve_publish_action(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    article = build_article(status="draft")
    session_double._objects = {5150: article}

    async with client:
        await login(client)
        response = await client.post("/api/ui/articles/5150/approve_publish")

    assert response.status_code == 200
    assert session_double.commits >= 1
    assert "одобрен" in response.text.lower()


async def test_article_rewrite_action(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    article = build_article(
        content_html="<p>В современном мире важно отметить событие.</p>"
    )
    session_double._objects = {5150: article}

    async with client:
        await login(client)
        response = await client.post(
            "/api/ui/articles/5150/rewrite",
            data={"style": "analytical", "instruction": "сделай строже"},
        )

    assert response.status_code == 200
    assert session_double.commits >= 1
    assert "Стиль" in response.text


async def test_article_toggle_watermark_action(
    client: httpx.AsyncClient, session_double: FakeSession
) -> None:
    article = build_article()
    session_double._objects = {5150: article}

    async with client:
        await login(client)
        response = await client.post(
            "/api/ui/articles/5150/toggle_watermark",
            data={"allow_watermark": "1"},
        )

    assert response.status_code == 200
    assert session_double.commits >= 1
    assert "водяным знаком" in response.text


# ============================================================= HTMX actions


def test_every_htmx_target_in_a_template_is_a_registered_route() -> None:
    """Every `hx-get`/`hx-post` URL must resolve to a route that accepts it.

    This is the regression that shipped: the articles page posted to
    `/api/ingestion/run` and the sources page used `hx-put` against a router with
    no PUT route at all, so both buttons failed silently in the browser while every
    test passed. Parsing the templates is what makes a wrong URL a test failure
    rather than a support question.
    """
    registered: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            registered.update((method, path) for method in methods)

    pattern = re.compile(r'hx-(get|post|put|patch|delete)="([^"]+)"')
    checked = 0
    for template in (TEMPLATES_DIR / "dashboard").rglob("*.html"):
        for verb, raw_url in pattern.findall(template.read_text(encoding="utf-8")):
            # Drop the query string and turn a Jinja expression into the path
            # parameter it fills, so `/articles/{{ a.id }}/edit` is compared as
            # `/articles/{article_id}/edit`.
            path = re.sub(r"\{\{[^}]+\}\}", "{}", raw_url.split("?")[0])
            candidates = {
                (verb.upper(), re.sub(r"\{[^}]+\}", "{}", route_path))
                for method, route_path in registered
                if method == verb.upper()
            }
            assert (verb.upper(), path) in candidates, (
                f"{template.name} targets {verb.upper()} {raw_url}, "
                "which no route accepts"
            )
            checked += 1
    # Guards against the parse silently matching nothing and the test passing
    # while checking zero URLs.
    assert checked >= 6


async def test_the_ingestion_trigger_reports_its_counters(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The button runs a pass inline and renders the resulting numbers."""
    report = _ingestion_report(inserted=3, fetched=5)
    monkeypatch.setattr(dashboard, "run_ingestion", _returning(report))

    async with client:
        await login(client)
        response = await client.post("/api/ui/ingestion/run")

    assert response.status_code == 200
    assert "получено" in response.text
    assert ">5<" in response.text.replace(" ", "").replace("\n", "")


async def test_a_held_run_lock_is_not_reported_as_zero_counters(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skipped run must say so, not render an empty but successful result.

    An operator shown "0 fetched" would reasonably conclude the sources are empty,
    when in fact no work was attempted.
    """
    monkeypatch.setattr(
        dashboard, "run_ingestion", _returning(_ingestion_report(lock_skipped=True))
    )

    async with client:
        await login(client)
        response = await client.post("/api/ui/ingestion/run")

    assert response.status_code == 200
    assert "уже идёт" in response.text
    assert "получено" not in response.text


async def test_an_unreachable_redis_renders_a_message_not_a_traceback(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fragment replaces a page element, so it must never carry a traceback."""

    async def _boom() -> None:
        raise RedisError("connection refused")

    monkeypatch.setattr(dashboard, "run_ingestion", _boom)

    async with client:
        await login(client)
        response = await client.post("/api/ui/ingestion/run")

    assert response.status_code == 200
    assert "Redis недоступен" in response.text
    assert "Traceback" not in response.text


async def test_the_skip_media_toggle_persists_the_new_value(
    session_double: FakeSession, client: httpx.AsyncClient
) -> None:
    source = _Source(SourceType.TELEGRAM, "Канал")
    session_double._objects[1] = source

    async with client:
        await login(client)
        response = await client.post(
            "/api/ui/sources/1/skip-media", data={"skip_media": "true"}
        )

    assert response.status_code == 200
    assert source.skip_media is True
    assert session_double.commits == 1
    assert "skip_media = true" in response.text


async def test_unticking_the_box_switches_skip_media_off(
    session_double: FakeSession, client: httpx.AsyncClient
) -> None:
    """An unchecked checkbox submits no field at all.

    Without the handler's `False` default the field would simply be absent, and the
    toggle could only ever be switched on — the classic HTML checkbox trap.
    """
    source = _Source(SourceType.TELEGRAM, "Канал")
    source.skip_media = True
    session_double._objects[1] = source

    async with client:
        await login(client)
        response = await client.post("/api/ui/sources/1/skip-media")

    assert response.status_code == 200
    assert source.skip_media is False
    assert "skip_media = false" in response.text


async def test_a_failed_toggle_rolls_back_and_says_so(
    session_double: FakeSession, client: httpx.AsyncClient
) -> None:
    session_double._objects[1] = _Source(SourceType.TELEGRAM, "Канал")
    session_double.commit_error = SQLAlchemyError("db is gone")

    async with client:
        await login(client)
        response = await client.post(
            "/api/ui/sources/1/skip-media", data={"skip_media": "true"}
        )

    assert response.status_code == 503
    assert session_double.rollbacks == 1
    assert "не сохранено" in response.text


async def test_toggling_a_missing_source_returns_404(
    client: httpx.AsyncClient,
) -> None:
    async with client:
        await login(client)
        response = await client.post("/api/ui/sources/999999/skip-media")

    assert response.status_code == 404
    assert "не найден" in response.text


async def test_the_selector_builder_renders_the_preview(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form posts form-encoded fields and gets a rendered fragment back.

    The previous version posted to a JSON endpoint using the `json-enc` extension,
    which `base.html` does not load, so the body never matched what the endpoint
    parsed.
    """
    captured: dict[str, Any] = {}

    async def _preview(url: str, rules: Any = None) -> CrawlPreview:
        captured["url"] = url
        captured["rules"] = rules
        return _crawl_preview()

    monkeypatch.setattr(dashboard, "test_crawl_url", _preview)

    async with client:
        await login(client)
        response = await client.post(
            "/api/ui/sources/website/test",
            data={
                "url": "https://example.test/news/1",
                "title_selector": "h1.title",
                "content_selector": "  ",
            },
        )

    assert response.status_code == 200
    assert captured["url"] == "https://example.test/news/1"
    # A blank selector is dropped rather than sent as an empty string, which would
    # match nothing and be reported as a failed selector.
    assert captured["rules"] == {"title_selector": "h1.title"}
    assert "trafilatura" in response.text


async def test_an_unreadable_page_is_an_error_not_a_traceback(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(url: str, rules: Any = None) -> CrawlPreview:
        raise ScraperError("404 Not Found")

    monkeypatch.setattr(dashboard, "test_crawl_url", _boom)

    async with client:
        await login(client)
        response = await client.post(
            "/api/ui/sources/website/test", data={"url": "https://example.test/gone"}
        )

    assert response.status_code == 200
    assert "не удалось прочитать" in response.text
    assert "Traceback" not in response.text


async def test_a_partial_extraction_is_a_success_carrying_warnings(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selector that matched nothing is the answer, not a failure.

    Reporting it as an error would hide exactly the diagnostic the selector builder
    exists to provide.
    """
    monkeypatch.setattr(
        dashboard,
        "test_crawl_url",
        _returning_preview(
            _crawl_preview(title="", warnings=("title_selector matched nothing",))
        ),
    )

    async with client:
        await login(client)
        response = await client.post(
            "/api/ui/sources/website/test", data={"url": "https://example.test/1"}
        )

    assert response.status_code == 200
    assert "title_selector matched nothing" in response.text
    assert "не найден" in response.text


async def test_the_htmx_actions_reject_an_unauthenticated_caller(
    client: httpx.AsyncClient,
) -> None:
    """Each action is a state change or a paid run, so none may be anonymous."""
    async with client:
        for method, url in (
            ("POST", "/api/ui/ingestion/run"),
            ("POST", "/api/ui/sources/1/skip-media"),
            ("POST", "/api/ui/sources/website/save"),
            ("GET", "/api/ui/sources/telegram/auth/form"),
            # The reset deletes collected posts and the inspection pulls from
            # Telegram; neither may be triggered by an anonymous caller.
            ("POST", "/api/ui/lab/reset"),
            ("POST", "/api/ui/lab/inspect"),
        ):
            response = await client.request(method, url)
            assert response.status_code == 401, url


async def test_a_malformed_cluster_id_is_refused_rather_than_ignored(
    client: httpx.AsyncClient,
) -> None:
    """Falling back to "newest" would spend a writer call on the wrong story.

    The id arrives from the inspection timeline, so a malformed one means the page
    and the runner disagree — and silently writing a different cluster than the one
    chosen is worse than writing nothing.
    """
    async with client:
        await login(client)
        response = await client.post("/api/ui/lab/run?collect=0&cluster=not-a-uuid")

    assert response.status_code == 422
    assert "Некорректный идентификатор" in response.text


def _ingestion_report(
    *, fetched: int = 0, inserted: int = 0, lock_skipped: bool = False
) -> IngestionRunReport:
    """Build a run report with one source, or an empty skipped one."""
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    sources: tuple[SourceIngestReport, ...] = ()
    if not lock_skipped:
        sources = (
            SourceIngestReport(
                source_id=1,
                source_name="Канал",
                fetched=fetched,
                inserted=inserted,
            ),
        )
    return IngestionRunReport(
        sources=sources,
        started_at=now,
        finished_at=now,
        duration_ms=42,
        lock_skipped=lock_skipped,
    )


def _crawl_preview(**overrides: Any) -> CrawlPreview:
    values: dict[str, Any] = {
        "url": "https://example.test/news/1",
        "title": "Заголовок",
        "text_preview": "Начало текста…",
        "text_length": 2400,
        "published_at": None,
        "media_urls": ("https://example.test/a.jpg",),
        "strategy": "trafilatura",
        "warnings": (),
        "duration_ms": 900,
        "content_html_length": 3100,
    }
    values.update(overrides)
    return CrawlPreview(**values)


def _returning(report: IngestionRunReport) -> Callable[[], Awaitable[Any]]:
    async def _run() -> IngestionRunReport:
        return report

    return _run


def _returning_preview(preview: CrawlPreview) -> Any:
    async def _run(url: str, rules: Any = None) -> CrawlPreview:
        return preview

    return _run


# ==================================================================== SSE


def test_an_sse_frame_is_well_formed() -> None:
    frame = format_sse('{"a":1}', event="log", retry_ms=3000)

    assert frame == 'event: log\nretry: 3000\ndata: {"a":1}\n\n'


def test_a_multiline_payload_is_split_across_data_fields() -> None:
    frame = format_sse("line1\nline2", event="log")

    # A bare newline inside a `data:` field terminates the event, so a traceback
    # would otherwise be delivered as several malformed frames.
    assert frame == "event: log\ndata: line1\ndata: line2\n\n"
    assert frame.endswith("\n\n")


async def test_the_stream_delivers_published_events() -> None:
    redis = FakeRedis(decode_responses=True)
    frames: list[str] = []

    async def consume() -> None:
        async for frame in stream_events(redis=redis, heartbeat=0.2, max_events=2):
            frames.append(frame)

    task = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0.1)
        first = json.dumps(
            {"level": "INFO", "message": "первое событие"}, ensure_ascii=False
        )
        await publish_event(first, redis=redis)
        await publish_event(
            json.dumps({"level": "ERROR", "message": "второе"}, ensure_ascii=False),
            redis=redis,
        )
        await asyncio.wait_for(task, timeout=10)
    finally:
        await redis.aclose(close_connection_pool=True)

    joined = "".join(frames)
    # The first frame announces the connection and carries the reconnect hint.
    assert "event: connected" in frames[0]
    assert "retry: 3000" in frames[0]
    assert joined.count("event: log") == 2
    # Cyrillic survives the round trip unescaped.
    assert "первое событие" in joined


async def test_the_stream_emits_a_heartbeat_when_idle() -> None:
    redis = FakeRedis(decode_responses=True)
    frames: list[str] = []

    # The generator is driven directly rather than through a cancelled task: on
    # cancellation the frames collected so far would be discarded along with the
    # coroutine, which is what made the first version of this test assert against an
    # empty list.
    stream = stream_events(redis=redis, heartbeat=0.05)
    try:
        for _ in range(4):
            frames.append(await asyncio.wait_for(anext(stream), timeout=5))
    finally:
        await stream.aclose()  # type: ignore[attr-defined]
        await redis.aclose(close_connection_pool=True)

    # An idle SSE connection is indistinguishable from a dead one to a proxy, so the
    # stream must keep speaking even with nothing to report.
    assert frames[0].startswith("event: connected")
    assert any(frame.startswith(": heartbeat") for frame in frames[1:])


async def test_the_stream_publishes_on_the_documented_channel() -> None:
    redis = FakeRedis(decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.subscribe(EVENTS_CHANNEL)
    try:
        assert await publish_event('{"m":1}', redis=redis) is True

        # `get_message` returns None immediately when the queue is empty, so the
        # delivery is polled rather than read once after a fixed sleep — a single
        # read is a race that fails on a loaded machine.
        message = None
        for _ in range(40):
            message = await pubsub.get_message(ignore_subscribe_messages=True)
            if message is not None:
                break
            await asyncio.sleep(0.02)

        assert message is not None, "the event never arrived on the channel"
        assert message["data"] == '{"m":1}'
    finally:
        await pubsub.aclose()  # type: ignore[no-untyped-call]
        await redis.aclose(close_connection_pool=True)


def test_the_log_handler_never_raises_without_an_event_loop() -> None:
    handler = RedisLogHandler()
    record = logging.LogRecord(
        "app.worker", logging.INFO, __file__, 1, "сообщение %s", ("x",), None
    )

    # A logging call that raised would replace the error being logged.
    handler.emit(record)


def test_the_log_handler_survives_a_broken_format_string() -> None:
    handler = RedisLogHandler()
    record = logging.LogRecord(
        "app.worker", logging.INFO, __file__, 1, "two %s %s", ("only-one",), None
    )

    handler.emit(record)


def test_the_log_handler_mutes_noisy_libraries() -> None:
    handler = RedisLogHandler()
    published: list[str] = []

    class _Recording(RedisLogHandler):
        def emit(self, record: logging.LogRecord) -> None:
            published.append(record.name)
            super().emit(record)

    recording = _Recording()
    for name in ("httpx", "httpcore.http11", "app.services.deduplicator"):
        recording.emit(
            logging.LogRecord(name, logging.INFO, __file__, 1, "m", (), None)
        )

    # The muted ones are still *seen* by emit; the filter is inside it. What
    # matters is that the handler does not raise on any of them.
    assert "app.services.deduplicator" in published
    assert handler.level == logging.INFO


async def test_lab_page_renders_with_honest_status(
    client: httpx.AsyncClient,
) -> None:
    """The /lab page displays honest metrics, news groups and studio containers."""
    token = create_session_token("admin").token
    async with client:
        client.cookies.set(COOKIE, token)
        response = await client.get("/lab")
        assert response.status_code == 200
        assert "Лаборатория конвейера" in response.text
        assert "Состояние конвейера" in response.text
        assert "Шаг 1. Группы новостей и дедупликация" in response.text
        assert "Шаг 2. Пошаговая студия сюжета" in response.text


async def test_lab_cluster_detail_404_when_missing(
    client: httpx.AsyncClient,
) -> None:
    """Requesting an unknown cluster ID returns 404."""
    token = create_session_token("admin").token
    async with client:
        client.cookies.set(COOKIE, token)
        fake_cid = uuid.uuid4()
        response = await client.get(f"/api/ui/lab/cluster/{fake_cid}")
        assert response.status_code == 404


async def test_lab_stepwise_actions_require_authentication(
    client: httpx.AsyncClient,
) -> None:
    """Stepwise lab endpoints return 401 when called without admin cookie."""
    async with client:
        cid = uuid.uuid4()
        for method, url in (
            ("POST", "/api/ui/lab/collect-reset"),
            ("POST", "/api/ui/lab/collect-only"),
            ("GET", f"/api/ui/lab/cluster/{cid}"),
            ("POST", f"/api/ui/lab/cluster/{cid}/research"),
            ("POST", f"/api/ui/lab/cluster/{cid}/media"),
            ("POST", f"/api/ui/lab/cluster/{cid}/generate"),
            ("POST", f"/api/ui/lab/cluster/{cid}/publish"),
        ):
            response = await client.request(method, url)
            assert response.status_code == 401, url


async def test_the_stream_buffers_and_replays_logs_history() -> None:
    redis = FakeRedis(decode_responses=True)
    try:
        # Publish events before anyone connects
        first = json.dumps(
            {"level": "INFO", "message": "историческое событие 1"},
            ensure_ascii=False,
        )
        second = json.dumps(
            {"level": "WARNING", "message": "историческое событие 2"},
            ensure_ascii=False,
        )
        await publish_event(first, redis=redis)
        await publish_event(second, redis=redis)

        # Check Redis buffer list
        buffered = await redis.lrange(LOGS_HISTORY_KEY, 0, -1)
        assert len(buffered) == 2
        assert "историческое событие 1" in buffered[0]

        # Connect new stream consumer: should instantly receive both buffered events
        frames: list[str] = []
        async for frame in stream_events(redis=redis, heartbeat=0.2, max_events=2):
            frames.append(frame)

        joined = "".join(frames)
        assert "event: connected" in frames[0]
        assert "историческое событие 1" in joined
        assert "историческое событие 2" in joined
    finally:
        await redis.aclose(close_connection_pool=True)


async def test_logs_clear_endpoint_clears_history(client: httpx.AsyncClient) -> None:
    token = create_session_token("admin").token
    fake = FakeRedis(decode_responses=True)
    with patch("app.api.logs_sse.get_redis", return_value=fake):
        async with client:
            client.cookies.set(COOKIE, token)
            response = await client.post("/api/logs/clear")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"


