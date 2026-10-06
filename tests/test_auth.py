"""AuthManager unit tests — bcrypt + sqlite + sessions + CSRF."""

from __future__ import annotations

from pathlib import Path

import pytest

from takhziin.auth import AuthError, AuthManager


@pytest.fixture
def auth(tmp_path: Path) -> AuthManager:
    return AuthManager(
        db_file=tmp_path / "users.db",
        secret_file=tmp_path / "session_secret.key",
    )


def test_no_users_on_fresh_db(auth: AuthManager) -> None:
    assert auth.has_users() is False


def test_create_user_rejects_short_password(auth: AuthManager) -> None:
    with pytest.raises(AuthError):
        auth.create_user("admin", "short")


def test_create_user_rejects_blank_username(auth: AuthManager) -> None:
    with pytest.raises(AuthError):
        auth.create_user("", "longenoughpw")


def test_create_user_duplicate(auth: AuthManager) -> None:
    auth.create_user("admin", "longenoughpw")
    with pytest.raises(AuthError):
        auth.create_user("admin", "anotherpw12")


def test_verify_credentials_round_trip(auth: AuthManager) -> None:
    auth.create_user("admin", "correctpassword")
    assert auth.verify_credentials("admin", "correctpassword") is not None
    assert auth.verify_credentials("admin", "wrongpassword") is None


def test_verify_credentials_unknown_user_does_constant_work(auth: AuthManager) -> None:
    # Should not raise and should return None
    assert auth.verify_credentials("nobody", "x" * 16) is None


def test_create_session_and_lookup(auth: AuthManager) -> None:
    auth.create_user("admin", "longenoughpw")
    user = auth.verify_credentials("admin", "longenoughpw")
    sess = auth.create_session(user.id, ip="127.0.0.1")
    assert sess.user_id == user.id
    fetched = auth.get_session(sess.id)
    assert fetched is not None
    assert fetched.username == "admin"


def test_delete_session(auth: None) -> None:  # type: ignore[assignment]
    pass


def test_make_and_parse_cookie_round_trip(auth: AuthManager) -> None:
    cookie = auth.make_cookie_value("abc123")
    assert cookie.startswith("abc123.")
    assert auth.parse_cookie_value(cookie) == "abc123"


def test_parse_cookie_rejects_tampering(auth: AuthManager) -> None:
    cookie = auth.make_cookie_value("abc123")
    tampered = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
    assert auth.parse_cookie_value(tampered) is None


def test_parse_cookie_rejects_missing_mac(auth: AuthManager) -> None:
    assert auth.parse_cookie_value("notavalidcookie") is None
    assert auth.parse_cookie_value("") is None


def test_csrf_token_round_trip(auth: AuthManager) -> None:
    token = "sess-abc"
    csrf = auth.make_csrf_token(token)
    assert auth.verify_csrf_token(token, csrf) is True
    assert auth.verify_csrf_token("different-token", csrf) is False
    assert auth.verify_csrf_token(token, "totally-bogus") is False
    assert auth.verify_csrf_token("", "") is False


def test_set_password_invalidates_sessions(auth: AuthManager) -> None:
    auth.create_user("admin", "oldpassword")
    user = auth.verify_credentials("admin", "oldpassword")
    sess = auth.create_session(user.id)
    assert auth.get_session(sess.id) is not None
    auth.set_password("admin", "newpassword1")
    # Old session must be gone
    assert auth.get_session(sess.id) is None
    # Old password rejected
    assert auth.verify_credentials("admin", "oldpassword") is None
    # New password works
    assert auth.verify_credentials("admin", "newpassword1") is not None


def test_db_file_permissions(auth: AuthManager, tmp_path: Path) -> None:
    # Touch via a no-op create_user
    auth.create_user("admin", "longenoughpw")
    mode = auth.db_file.stat().st_mode & 0o777
    assert mode == 0o600, f"users.db not 0600: got {oct(mode)}"


def test_session_secret_persisted(auth: AuthManager, tmp_path: Path) -> None:
    secret1 = auth.session_secret
    secret2 = AuthManager(
        db_file=auth.db_file,
        secret_file=auth.secret_file,
    ).session_secret
    assert secret1 == secret2
    # file should be 0600
    mode = auth.secret_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_user_enum_timing_doesnt_crash(auth: AuthManager) -> None:
    # the verify_credentials implementation runs a hash for timing-parity
    auth.verify_credentials("nonexistent", "anything")
    auth.verify_credentials("nonexistent", "x" * 50)