"""Web UI auth glue — cookie decode + dependency + CSRF.

We deliberately avoid Starlette/FastAPI SessionMiddleware: the session token
is an opaque random string stored in SQLite, the cookie value is the token +
HMAC(secret, token) for tamper detection. Stateless cookie verification on
every request via ``require_session`` keeps the model simple.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request, status

from takhziin.auth import AuthManager, SessionRow

COOKIE_NAME = "takhzi_session"


def get_auth(request: Request) -> AuthManager:
    return request.app.state.auth  # type: ignore[no-any-return]


def current_session(request: Request) -> Optional[SessionRow]:
    """Return the active session for this request, or None.

    Performs the cookie MAC verification before hitting the DB.
    """
    auth = get_auth(request)
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    token = auth.parse_cookie_value(cookie)
    if token is None:
        return None
    return auth.get_session(token)


def require_session(request: Request) -> SessionRow:
    """FastAPI dependency that 302s to the login page when no session is present."""
    session = current_session(request)
    if session is None:
        # We can't `raise HTTPException(redirect)` cleanly here, so return a
        # sentinel: caller checks for None and issues the redirect.
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return session


def require_session_or_none(request: Request) -> Optional[SessionRow]:
    """Soft dependency — returns None instead of redirecting."""
    return current_session(request)


def csrf_token_for(request: Request, session: SessionRow) -> str:
    auth = get_auth(request)
    return auth.make_csrf_token(session.id)


def verify_csrf(request: Request, session: SessionRow, submitted: Optional[str]) -> bool:
    auth = get_auth(request)
    return auth.verify_csrf_token(session.id, submitted or "")


__all__ = [
    "COOKIE_NAME",
    "get_auth",
    "current_session",
    "require_session",
    "require_session_or_none",
    "csrf_token_for",
    "verify_csrf",
]