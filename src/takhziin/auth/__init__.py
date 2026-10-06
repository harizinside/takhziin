"""Auth package — users + sessions + CSRF for the web UI.

The CLI bypasses auth (the operator is already trusted at the shell). The web
UI is the only surface exposed to network; this module backs it.

Storage layout under ``$config_dir``:

    users.db   SQLite database holding users + sessions. chmod 0600.
    session_secret.key  32-byte HMAC secret used to sign session cookies.

Password hashing uses bcrypt with a default cost factor of 12. Session tokens
are random 256-bit values stored in the database; the cookie value is the token
verbatim (HTTPS-enforced in prod) plus a MAC of the same token so tampering is
detectable. CSRF tokens are derived from the session token + session secret so
a leaked cookie secret cannot be used to forge a CSRF token.
"""

from __future__ import annotations

from takhziin.auth.manager import AuthManager, AuthError, SessionRow, UserRow

__all__ = ["AuthManager", "AuthError", "SessionRow", "UserRow"]