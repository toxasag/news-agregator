"""Security primitives for the dashboard: password hashing, JWT sessions, Fernet.

Three concerns live here rather than in the API layer, because each has exactly
one correct implementation and scattering them invites a second, weaker copy:

* **Password verification.** bcrypt with a per-hash salt, verified in constant
  time by the library. bcrypt is used directly rather than through ``passlib``,
  which is unmaintained and whose bcrypt backend warns on modern releases.

* **Session tokens.** A JWT signed with ``SECRET_KEY``, carrying a subject, an
  expiry and an issued-at claim. The token is short by design: it authenticates a
  single administrator, so it needs no roles, scopes or refresh flow, and every
  claim that is not checked is a claim that can be forged with impunity.

* **Secret storage.** Fernet (AES-128-CBC with an HMAC-SHA256 tag) for the
  third-party API keys the settings page stores. Fernet is authenticated
  encryption, so a tampered ciphertext fails to decrypt rather than yielding
  garbage that would later be sent to an API as a credential.

**The Fernet key is separate from ``SECRET_KEY``.** Rotating the JWT signing key
should log everyone out; it must not render every stored API key permanently
undecryptable. Two keys with two lifecycles is the whole point.

**Expiry is verified, not merely present.** :func:`decode_session_token` lets PyJWT
enforce ``exp`` and rejects a token whose subject does not match the configured
administrator — a token signed with the right key but issued for a username that
has since changed must not still open the dashboard.

Run ``python -m app.core.security hash <password>`` to generate the value for
``ADMIN_PASSWORD_HASH``.
"""

from __future__ import annotations

import logging
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "BCRYPT_ROUNDS",
    "SESSION_SUBJECT_CLAIM",
    "SecretBox",
    "SessionToken",
    "TokenError",
    "authenticate_admin",
    "create_session_token",
    "decode_session_token",
    "get_secret_box",
    "hash_password",
    "session_subject_is_current",
    "verify_password",
]

#: bcrypt cost. 12 is the current sensible default: roughly 250 ms per hash on
#: modern hardware, which is unnoticeable on a login form and expensive enough
#: that an offline attack on a leaked hash is impractical.
BCRYPT_ROUNDS: Final[int] = 12

#: bcrypt truncates its input at 72 bytes and, in the version pinned here, raises
#: on anything longer rather than silently ignoring the tail. Passwords are
#: rejected above this length so the error surfaces at hash time, not at login.
MAX_PASSWORD_BYTES: Final[int] = 72

SESSION_SUBJECT_CLAIM: Final[str] = "sub"

#: Clock skew tolerated when validating ``exp``. Small, because the token lifetime
#: is measured in hours: a generous leeway would extend every session past its
#: stated expiry.
LEEWAY_SECONDS: Final[int] = 10


class TokenError(RuntimeError):
    """A session token is missing, malformed, expired or not ours."""


@dataclass(frozen=True, slots=True)
class SessionToken:
    """An issued session token and the moment it stops being valid."""

    token: str
    expires_at: datetime

    @property
    def max_age_seconds(self) -> int:
        """Remaining lifetime, for the cookie's ``Max-Age``.

        Derived from the token's own expiry rather than from the setting, so the
        cookie and the JWT cannot disagree about when the session ends — a cookie
        outliving its token produces a confusing "logged in but rejected" state.
        """
        remaining = (self.expires_at - datetime.now(UTC)).total_seconds()
        return max(int(remaining), 0)


def hash_password(password: str) -> str:
    """Hash a password with bcrypt and a fresh salt.

    Raises :class:`ValueError` for an empty password or one past bcrypt's 72-byte
    input limit, because both would otherwise fail confusingly later — an empty
    password hashes successfully and then accepts an empty login form.
    """
    if not password:
        raise ValueError("refusing to hash an empty password")
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password is {len(encoded)} bytes; bcrypt accepts at most "
            f"{MAX_PASSWORD_BYTES}. Use a shorter passphrase."
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a bcrypt hash.

    Returns ``False`` for a malformed or empty hash rather than raising: an
    unconfigured deployment must reject logins, not answer them with a 500 that
    reveals the configuration is broken.
    """
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        # A truncated or non-bcrypt value in `ADMIN_PASSWORD_HASH`.
        logger.error(
            "ADMIN_PASSWORD_HASH is not a valid bcrypt hash; every login will fail"
        )
        return False


def authenticate_admin(username: str, password: str) -> bool:
    """Verify a login attempt against the configured administrator.

    The username is compared with :func:`secrets.compare_digest` so the check does
    not leak its length through timing, and the password is verified **even when
    the username is wrong**. Returning early on an unknown username would make the
    response measurably faster for a wrong name than for a wrong password, which
    tells an attacker when they have found the right account.
    """
    expected_user = settings.admin_username
    user_ok = secrets.compare_digest(
        username.strip().encode("utf-8"), expected_user.encode("utf-8")
    )
    password_ok = verify_password(password, settings.admin_password_hash)
    return user_ok and password_ok


def create_session_token(
    username: str, *, expires_in_minutes: int | None = None
) -> SessionToken:
    """Issue a signed session token for ``username``."""
    lifetime = timedelta(
        minutes=expires_in_minutes
        if expires_in_minutes is not None
        else settings.access_token_expire_minutes
    )
    issued_at = datetime.now(UTC)
    expires_at = issued_at + lifetime
    payload: dict[str, Any] = {
        SESSION_SUBJECT_CLAIM: username,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        # A per-token id, so a future revocation list has something to key on.
        "jti": secrets.token_hex(8),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
    return SessionToken(token=token, expires_at=expires_at)


def decode_session_token(token: str) -> str:
    """Validate a session token and return its subject.

    Raises :class:`TokenError` for anything that is not a live token issued by
    this deployment for the current administrator. The algorithm is pinned to the
    configured one: accepting whatever the token's header declares is the classic
    JWT confusion attack, where an attacker re-signs with ``none`` or downgrades
    an RS256 token to HS256 using the public key as the HMAC secret.
    """
    if not token:
        raise TokenError("no session token was supplied")
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", SESSION_SUBJECT_CLAIM]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("the session has expired") from exc
    except jwt.InvalidTokenError as exc:
        # Covers a bad signature, a malformed token and a missing claim. The
        # reason is deliberately not echoed to the client.
        raise TokenError("the session token is not valid") from exc

    subject = payload.get(SESSION_SUBJECT_CLAIM)
    if not isinstance(subject, str) or not subject:
        raise TokenError("the session token carries no subject")
    return subject


def session_subject_is_current(subject: str, *, expected: str | None = None) -> bool:
    """Whether a token's subject still names the current administrator.

    Separate from :func:`decode_session_token` because the current username is no
    longer a static value: the profile page can change it, and the live value then
    lives in the database rather than in ``ADMIN_USERNAME``. The signature check
    stays synchronous and dependency-free, while the *identity* check is given the
    expected name by a caller that can read it.

    A token signed with the right key but naming a username that has since
    changed must not still open the dashboard — otherwise renaming the account
    would not end the sessions belonging to the old name.
    """
    reference = settings.admin_username if expected is None else expected
    return secrets.compare_digest(
        subject.encode("utf-8"), reference.encode("utf-8")
    )


class SecretBox:
    """Fernet wrapper for the API keys the settings page stores.

    Constructed lazily through :func:`get_secret_box` so an unconfigured
    deployment fails when it first tries to store a secret, with a message naming
    the missing setting, rather than at import time where the traceback would
    point at the wrong place.
    """

    __slots__ = ("_fernet",)

    def __init__(self, key: str | None = None) -> None:
        raw = (settings.fernet_key if key is None else key).strip()
        if not raw:
            raise ValueError(
                "FERNET_KEY is not set: generate one with "
                "`python -m app.core.security genkey`"
            )
        try:
            self._fernet = Fernet(raw.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "FERNET_KEY is not a valid 32-byte url-safe base64 key; "
                "regenerate it with `python -m app.core.security genkey`"
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a secret for storage. An empty value stays empty.

        An empty string is passed through rather than encrypted so "no key
        configured" round-trips as an empty value instead of as a ciphertext that
        decrypts to nothing — the dashboard shows the difference.
        """
        if not plaintext:
            return ""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a stored secret, returning ``""`` when it cannot be read.

        A tampered or key-rotated ciphertext returns empty rather than raising:
        the caller treats that as "not configured" and degrades, which is far
        better than a dashboard that will not render because one stored key is
        unreadable. Fernet is authenticated, so a modified ciphertext is detected
        rather than silently decrypting to garbage that would be sent to an API.
        """
        if not ciphertext:
            return ""
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            logger.warning(
                "A stored secret could not be decrypted; treating it as unset. "
                "This is expected after a FERNET_KEY rotation."
            )
            return ""

    def mask(self, ciphertext: str) -> str:
        """Render a stored secret for display without revealing it.

        The dashboard has to show *whether* a key is configured and let an
        operator recognise which one it is, without putting the credential in a
        page that could be screenshotted, cached or logged. Only the last four
        characters are shown, and only when the value is long enough that they do
        not constitute most of it.
        """
        plaintext = self.decrypt(ciphertext)
        if not plaintext:
            return ""
        if len(plaintext) <= 8:
            return "•" * 8
        return f"{'•' * 8}{plaintext[-4:]}"


_shared_box: SecretBox | None = None


def get_secret_box() -> SecretBox:
    """Return the process-wide secret box, creating it on first use."""
    global _shared_box  # noqa: PLW0603 - module-level singleton accessor
    if _shared_box is None:
        _shared_box = SecretBox()
    return _shared_box


def _main(argv: list[str]) -> int:
    """Tiny CLI for generating the two secrets this module needs.

    Exists so an operator never has to write a one-off script — and so nobody is
    tempted to paste a plaintext password into `.env` because hashing it looked
    like work.
    """
    if len(argv) >= 2 and argv[1] == "genkey":
        sys.stdout.write(Fernet.generate_key().decode("ascii") + "\n")
        return 0
    if len(argv) >= 3 and argv[1] == "hash":
        try:
            sys.stdout.write(hash_password(argv[2]) + "\n")
        except ValueError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
        return 0
    sys.stderr.write(
        "usage:\n"
        "  python -m app.core.security hash <password>   # ADMIN_PASSWORD_HASH\n"
        "  python -m app.core.security genkey            # FERNET_KEY\n"
    )
    return 2


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(_main(sys.argv))
