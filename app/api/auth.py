"""Dashboard authentication: login, logout and the route guard.

This is the replacement for the interim shared-secret bearer guard in
:mod:`app.api.ingestion`, which its own docstring flags as a Package 5 placeholder.

The session is a JWT in a cookie, and each cookie flag is load-bearing:

* ``httponly`` — JavaScript cannot read the token, so an XSS anywhere on the
  dashboard cannot exfiltrate the session. This is the single most valuable flag
  of the four.
* ``secure`` — the cookie is withheld on plain HTTP, so the token cannot be
  captured in transit. Disabled automatically outside production, because a
  developer on ``http://localhost`` would otherwise never receive it and the login
  would appear silently broken.
* ``samesite="lax"`` — the cookie is not sent on cross-site POSTs, which is what
  makes CSRF against the mutating dashboard endpoints impractical. ``strict``
  would be marginally stronger but would drop the session on any inbound link,
  which an operator experiences as being logged out at random.
* ``path="/"`` — one session for the whole dashboard, and one ``delete_cookie``
  that actually clears it.

**Two guards, deliberately different.** :func:`get_current_admin_user` returns 401
for API callers, while :func:`require_admin_page` redirects a browser to the login
page. A dashboard that answered a stale-cookie page request with raw JSON would be
a dead end for the operator; an HTMX partial that answered with an HTML redirect
would silently paste a login form into a table cell, which is why the API guard
keeps its status code.

**Login failures are deliberately vague.** The response never distinguishes an
unknown username from a wrong password, and the timing does not either — see
:func:`~app.core.security.authenticate_admin`.
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response
from fastapi import status as http_status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import (
    SessionToken,
    TokenError,
    create_session_token,
    decode_session_token,
    session_subject_is_current,
)
from app.database import get_db
from app.services.admin_credentials import authenticate_admin_db, load_credentials

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "LOGIN_PATH",
    "AdminUser",
    "get_current_admin_user",
    "require_admin_page",
    "router",
    "set_session_cookie",
]

LOGIN_PATH: Final[str] = "/login"

#: Sent with every 401 so a client knows this is a cookie session rather than a
#: bearer scheme it should retry with a token, and so HTMX redirects to login.
_UNAUTHORIZED_HEADERS: Final[dict[str, str]] = {
    "WWW-Authenticate": "Cookie",
    "HX-Redirect": "/login",
}

#: Alias for the session cookie, resolved once so the long `Annotated` form
#: below stays readable.
_COOKIE: Final[str] = settings.session_cookie_name

router: Final[APIRouter] = APIRouter(prefix="/auth", tags=["auth"])


def set_session_cookie(response: Response, token: SessionToken) -> None:
    """Attach the session cookie to a response.

    ``secure`` follows the environment: enforced in production, relaxed elsewhere
    so a developer on ``http://localhost`` receives the cookie at all. Getting this
    wrong in either direction is a real failure — always-on breaks local
    development, always-off ships a session token over plain HTTP.
    """
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token.token,
        max_age=token.max_age_seconds,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Remove the session cookie.

    The attributes must match those used when setting it, or the browser keeps the
    original cookie and the operator stays logged in after clicking "log out".
    """
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )


class AdminUser:
    """The authenticated administrator.

    A class rather than a bare string so a future permission check has somewhere
    to live, and so route signatures read as ``admin: AdminUser`` instead of an
    anonymous ``str`` that could be confused with a path parameter.
    """

    __slots__ = ("username",)

    def __init__(self, username: str) -> None:
        self.username = username

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AdminUser({self.username!r})"


async def get_current_admin_user(
    session: Annotated[AsyncSession, Depends(get_db)],
    session_cookie: Annotated[str | None, Cookie(alias=_COOKIE)] = None,
) -> AdminUser:
    """Authenticate an API request from the session cookie.

    Raises 401 for a missing, malformed or expired token, and for a token naming
    a username that is no longer current — renaming the account has to end the
    sessions belonging to the old name. The current name is read from the
    database, so the check follows a change made on the profile page.

    Used for JSON endpoints and HTMX partials, where a redirect to an HTML login
    page would be pasted into whatever element triggered the request.
    """
    if not session_cookie:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Sign in at /login.",
            headers=_UNAUTHORIZED_HEADERS,
        )
    try:
        username = decode_session_token(session_cookie)
    except TokenError as exc:
        # The specific reason is logged, not returned: "expired" versus "bad
        # signature" tells an attacker whether they have the signing key.
        logger.info("Rejected a dashboard request: %s", exc)
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Session is invalid or has expired. Sign in again.",
            headers=_UNAUTHORIZED_HEADERS,
        ) from exc

    credentials = await load_credentials(session)
    if not session_subject_is_current(username, expected=credentials.username):
        logger.info("Rejected a session for a stale administrator name")
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Session is invalid or has expired. Sign in again.",
            headers=_UNAUTHORIZED_HEADERS,
        )
    return AdminUser(username)


async def require_admin_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    session_cookie: Annotated[str | None, Cookie(alias=_COOKIE)] = None,
) -> AdminUser:
    """Authenticate a full-page request, redirecting to the login form on failure.

    Raises an :class:`fastapi.HTTPException` carrying a 303 and a ``Location``
    header rather than returning a response, because a dependency cannot short-
    circuit a route any other way. 303 is correct here: it tells the browser to
    follow up with ``GET`` regardless of the original method, so a POST whose
    session expired mid-form does not re-POST to the login page.

    The originally requested path travels in ``?next=`` so the operator lands
    where they were going. Only a site-relative path is preserved — an absolute
    URL would make this an open redirect that could bounce a logged-in operator to
    an attacker's page.
    """
    if session_cookie:
        try:
            subject = decode_session_token(session_cookie)
        except TokenError as exc:
            logger.info("Redirecting a dashboard page request to login: %s", exc)
        else:
            credentials = await load_credentials(session)
            if session_subject_is_current(subject, expected=credentials.username):
                return AdminUser(subject)
            logger.info("Redirecting a stale-name session to login")

    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    location = LOGIN_PATH
    if target and target.startswith("/") and not target.startswith("//"):
        # `//host` is protocol-relative and would leave the site.
        location = f"{LOGIN_PATH}?next={target}"
    raise HTTPException(
        status_code=http_status.HTTP_303_SEE_OTHER,
        detail="Authentication required.",
        headers={"Location": location},
    )


def _safe_next(candidate: str | None) -> str:
    """Validate a post-login redirect target.

    Anything that is not a plain site-relative path falls back to the dashboard
    root. Without this check the login form is an open redirect: an attacker sends
    ``/login?next=https://evil.test``, the operator signs in, and the browser
    hands them to a convincing clone.
    """
    if not candidate:
        return "/"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    # A backslash is normalised to a forward slash by some browsers, so `/\evil`
    # would escape the site.
    if "\\" in candidate:
        return "/"
    return candidate


@router.post("/login", summary="Sign in and receive the session cookie")
async def login(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next_url: Annotated[str | None, Form(alias="next")] = None,
) -> Response:
    """Verify credentials and start a session.

    Credentials come from ``system_settings`` when the profile page has ever
    changed them, and from the environment otherwise — see
    :mod:`app.services.admin_credentials`. Checked per request rather than cached,
    because a cached hash would let a changed password keep working.

    On success the browser is redirected to ``next`` (validated) with the session
    cookie attached. On failure it is returned to the login form with a generic
    error — the form re-renders the message, so the operator sees what happened
    without the response revealing which half of the credential was wrong.
    """
    credentials = await load_credentials(session)
    if not await authenticate_admin_db(session, username, password):
        logger.warning("Failed dashboard login attempt for username %r", username[:64])
        redirect = RedirectResponse(
            url=f"{LOGIN_PATH}?error=1", status_code=http_status.HTTP_303_SEE_OTHER
        )
        # Any stale cookie is cleared, so a failed login cannot leave a previous
        # session half-alive.
        clear_session_cookie(redirect)
        return redirect

    token = create_session_token(credentials.username)
    redirect = RedirectResponse(
        url=_safe_next(next_url), status_code=http_status.HTTP_303_SEE_OTHER
    )
    set_session_cookie(redirect, token)
    logger.info("Dashboard login succeeded for %s", credentials.username)
    return redirect


@router.post("/logout", summary="End the session")
async def logout() -> Response:
    """Clear the session cookie and return to the login form.

    No authentication is required to log *out*: a caller with an expired or
    malformed cookie should still be able to clear it, and demanding a valid
    session first would trap them with a cookie they cannot remove.
    """
    redirect = RedirectResponse(
        url=LOGIN_PATH, status_code=http_status.HTTP_303_SEE_OTHER
    )
    clear_session_cookie(redirect)
    return redirect


@router.get("/me", summary="Who the current session belongs to")
async def whoami(
    admin: Annotated[AdminUser, Depends(get_current_admin_user)],
) -> dict[str, str]:
    """Return the authenticated administrator.

    Used by the dashboard to confirm a session is still live without fetching a
    full page, and by the tests as the smallest possible protected endpoint.
    """
    return {"username": admin.username}
