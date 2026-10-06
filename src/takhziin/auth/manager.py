"""AuthManager — bcrypt-hashed users + opaque session tokens, sqlite-stored.

The DB schema lives in a single file (``users.db``) with two tables:

    users    id TEXT PK, username TEXT UNIQUE, password_hash TEXT,
             created_at TEXT, last_login_at TEXT

    sessions id TEXT PK (random token), user_id TEXT FK, created_at TEXT,
             expires_at TEXT, ip TEXT

A ``hash_secret`` HMAC key (32 bytes, chmod 0600) is generated on first use and
used by the FastAPI layer to sign the CSRF token. The DB row holds the session
token; the cookie carries the token, so signing the cookie is just for tamper
detection — we still authoritatively look up the token in the DB.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Optional

import bcrypt


SESSION_TTL = timedelta(hours=12)
BCRYPT_COST = 12  # default; tuned so a single verify < 250 ms on modern CPUs


class AuthError(RuntimeError):
    """Raised for any auth failure with a human-readable message."""


@dataclass
class UserRow:
    id: str
    username: str
    password_hash: str
    created_at: str
    last_login_at: Optional[str]


@dataclass
class SessionRow:
    id: str
    user_id: str
    username: str
    created_at: str
    expires_at: str


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_password(plaintext: str) -> str:
    """bcrypt-hash a password. Returns the encoded prefix we can decode later."""
    salt = bcrypt.gensalt(rounds=BCRYPT_COST)
    return bcrypt.hashpw(plaintext.encode("utf-8"), salt).decode("ascii")


def _verify_password(plaintext: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


class AuthManager:
    """Facade for user + session + CSRF operations.

    Stateless in-process; one DB file per takhziin installation.
    """

    def __init__(self, db_file: Path, secret_file: Path) -> None:
        self.db_file = Path(db_file)
        self.secret_file = Path(secret_file)
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # --- schema --------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_file), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            TEXT PRIMARY KEY,
                    username      TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    last_login_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id         TEXT PRIMARY KEY,
                    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    ip         TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                """
            )
        # tighten perms on the DB file
        if self.db_file.exists():
            os.chmod(self.db_file, 0o600)

    # --- session secret -----------------------------------------------

    @property
    def session_secret(self) -> bytes:
        """Lazily generate / load the 32-byte HMAC secret used for CSRF + cookie MAC."""
        if self.secret_file.exists():
            raw = self.secret_file.read_bytes()
            if len(raw) == 32:
                return raw
            raise AuthError(f"session secret at {self.secret_file} has wrong length")
        self.secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_bytes(32)
        tmp = self.secret_file.with_suffix(self.secret_file.suffix + ".tmp")
        tmp.write_bytes(secret)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.secret_file)
        os.chmod(self.secret_file, 0o600)
        return secret

    # --- user management ----------------------------------------------

    def has_users(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            return row is not None

    def create_user(self, username: str, password: str) -> UserRow:
        if not username or len(username) > 64:
            raise AuthError("username must be 1..64 characters")
        if not password or len(password) < 8:
            raise AuthError("password must be at least 8 characters")
        password_hash = _hash_password(password)
        user_id = secrets.token_hex(8)
        now = _utcnow_iso()
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (user_id, username, password_hash, now),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthError(f"username {username!r} already exists") from exc
        return UserRow(
            id=user_id,
            username=username,
            password_hash=password_hash,
            created_at=now,
            last_login_at=None,
        )

    def get_user(self, username: str) -> Optional[UserRow]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, created_at, last_login_at FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return UserRow(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )

    def set_password(self, username: str, new_password: str) -> None:
        user = self.get_user(username)
        if user is None:
            raise AuthError(f"user {username!r} not found")
        if not new_password or len(new_password) < 8:
            raise AuthError("password must be at least 8 characters")
        new_hash = _hash_password(new_password)
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (new_hash, user.id),
            )
            # invalidate existing sessions on password change
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))

    # --- login / sessions ---------------------------------------------

    def verify_credentials(self, username: str, password: str) -> Optional[UserRow]:
        user = self.get_user(username)
        if user is None:
            # constant-time-ish: run a hash anyway to avoid user-enum timing leaks
            _verify_password(password, _hash_password("dummy"))
            return None
        if not _verify_password(password, user.password_hash):
            return None
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (_utcnow_iso(), user.id),
            )
        return user

    def create_session(self, user_id: str, *, ip: Optional[str] = None) -> SessionRow:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + SESSION_TTL
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions (id, user_id, created_at, expires_at, ip) VALUES (?, ?, ?, ?, ?)",
                (token, user_id, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds"), ip),
            )
        user = self._get_user_by_id(user_id)
        return SessionRow(
            id=token,
            user_id=user_id,
            username=user.username if user else "",
            created_at=now.isoformat(timespec="seconds"),
            expires_at=expires.isoformat(timespec="seconds"),
        )

    def get_session(self, token: str) -> Optional[SessionRow]:
        if not token:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT s.id, s.user_id, u.username, s.created_at, s.expires_at "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.id = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        # expiry check
        try:
            exp = datetime.fromisoformat(row["expires_at"])
        except (ValueError, TypeError):
            return None
        if exp < datetime.now(timezone.utc):
            self.delete_session(token)
            return None
        return SessionRow(
            id=row["id"],
            user_id=row["user_id"],
            username=row["username"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def delete_session(self, token: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (token,))

    def delete_all_sessions(self, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def _get_user_by_id(self, user_id: str) -> Optional[UserRow]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, created_at, last_login_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return UserRow(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )

    # --- cookie + CSRF ------------------------------------------------

    def make_cookie_value(self, token: str) -> str:
        """Token + dot + base64(MAC(token, session_secret))."""
        mac = hmac.new(self.session_secret, token.encode("ascii"), sha256).digest()
        return f"{token}.{base64.urlsafe_b64encode(mac).decode('ascii').rstrip('=')}"

    def parse_cookie_value(self, cookie_value: str) -> Optional[str]:
        """Verify the MAC and return the bare token, or None on tampering."""
        if not cookie_value or "." not in cookie_value:
            return None
        token, mac_b64 = cookie_value.rsplit(".", 1)
        try:
            mac = base64.urlsafe_b64decode(mac_b64 + "=" * (-len(mac_b64) % 4))
        except Exception:
            return None
        expected = hmac.new(self.session_secret, token.encode("ascii"), sha256).digest()
        if not hmac.compare_digest(mac, expected):
            return None
        return token

    def make_csrf_token(self, token: str) -> str:
        """Derive a CSRF token from the session token + secret."""
        mac = hmac.new(self.session_secret, b"csrf:" + token.encode("ascii"), sha256).digest()
        return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")

    def verify_csrf_token(self, token: str, csrf: str) -> bool:
        if not token or not csrf:
            return False
        expected = self.make_csrf_token(token)
        return hmac.compare_digest(expected.encode("ascii"), csrf.encode("ascii"))


__all__ = [
    "AuthManager",
    "AuthError",
    "UserRow",
    "SessionRow",
    "SESSION_TTL",
]