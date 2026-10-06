"""Web UI auth tests — login flow, redirect, logout, CSRF."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from takhziin.auth import AuthManager
from takhziin.ui.app import create_app
from takhziin.ui.auth import COOKIE_NAME


@pytest.fixture
def ui_env(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "cfg"
    dat = tmp_path / "data"
    cfg.mkdir(parents=True, exist_ok=True)
    dat.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TAKHZIIN_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("TAKHZIIN_DATA_DIR", str(dat))
    monkeypatch.setenv("TAKHZIIN_BIN_DIR", str(tmp_path / "bin"))
    AuthManager(
        db_file=cfg / "users.db",
        secret_file=cfg / "session_secret.key",
    ).create_user("admin", "correct-password")
    return tmp_path, "correct-password"


def _login(client: TestClient, password: str) -> str:
    r = client.post("/login", data={"username": "admin", "password": password}, follow_redirects=False)
    if r.status_code != 303:
        raise AssertionError(f"login failed: {r.status_code}")
    cookie = r.cookies.get("takhzi_session")
    assert cookie
    return cookie


def _authenticated_client(app, cookie_value: str) -> TestClient:
    """TestClient whose cookies carry the session.

    httpx2's TestClient jar does not always auto-send cookies returned from
    a 303 redirect, so we pass cookies explicitly at client construction.
    ``follow_redirects=False`` is critical because the dashboard route 200s
    after auth — without this, /login -> / -> / chain trips the test.
    """
    return TestClient(app, cookies={COOKIE_NAME: cookie_value}, follow_redirects=False)


def _extract_csrf(html: str) -> str:
    import re
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m is not None, "no csrf_token found"
    return m.group(1)


# --- public ---------------------------------------------------------------


def test_unauthenticated_request_redirects_to_login(ui_env) -> None:
    app = create_app()
    c = TestClient(app)
    for path in ["/", "/databases", "/databases/new", "/backups", "/schedules", "/notifications"]:
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303, f"GET {path} expected 303, got {r.status_code}"
        assert r.headers["location"] == "/login"


def test_login_get_returns_form_when_logged_out(ui_env) -> None:
    app = create_app()
    c = TestClient(app)
    r = c.get("/login")
    assert r.status_code == 200
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text


def test_login_get_redirects_when_already_logged_in(ui_env) -> None:
    app = create_app()
    c = TestClient(app)
    pw = ui_env[1]
    cookie = _login(c, pw)
    c2 = _authenticated_client(app, cookie)
    r = c2.get("/login")
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_success_sets_cookie(ui_env) -> None:
    app = create_app()
    c = TestClient(app)
    r = c.post("/login", data={"username": "admin", "password": ui_env[1]}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "takhzi_session" in r.cookies


def test_login_failure_returns_401(ui_env) -> None:
    app = create_app()
    c = TestClient(app)
    r = c.post("/login", data={"username": "admin", "password": "WRONG"})
    assert r.status_code == 401
    assert "invalid credentials" in r.text.lower()


def test_authenticated_user_can_view_pages(ui_env) -> None:
    app = create_app()
    c = TestClient(app)
    cookie = _login(c, ui_env[1])
    c = _authenticated_client(app, cookie)
    for path in ["/", "/databases", "/databases/new", "/backups", "/schedules", "/notifications"]:
        r = c.get(path)
        assert r.status_code == 200, f"GET {path} expected 200, got {r.status_code}"


def test_logout_clears_session(ui_env) -> None:
    app = create_app()
    c = TestClient(app)
    cookie = _login(c, ui_env[1])
    # get csrf token
    r = c.get("/schedules", cookies={COOKIE_NAME: cookie})
    csrf = _extract_csrf(r.text)
    # logout
    r = c.post("/logout", data={"csrf_token": csrf}, cookies={COOKIE_NAME: cookie}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # session is deleted server-side; even passing the cookie back fails
    c2 = TestClient(app, cookies={COOKIE_NAME: cookie})
    r = c2.get("/", follow_redirects=False)
    assert r.status_code == 303


def test_post_without_csrf_returns_403(ui_env) -> None:
    app = create_app()
    c = TestClient(app)
    cookie = _login(c, ui_env[1])
    c = _authenticated_client(app, cookie)
    r = c.post(
        "/databases/some-id/delete",
        data={},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_post_with_wrong_csrf_returns_403(ui_env) -> None:
    app = create_app()
    c = TestClient(app)
    cookie = _login(c, ui_env[1])
    c = _authenticated_client(app, cookie)
    r = c.post(
        "/databases/some-id/delete",
        data={"csrf_token": "totally-bogus"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_csrf_token_changes_per_session(ui_env) -> None:
    app = create_app()
    c = TestClient(app)
    cookie1 = _login(c, ui_env[1])
    c1 = _authenticated_client(app, cookie1)
    csrf1 = _extract_csrf(c1.get("/").text)

    # second session
    cookie2 = _login(c, ui_env[1])
    c2 = _authenticated_client(app, cookie2)
    csrf2 = _extract_csrf(c2.get("/").text)
    assert csrf1 != csrf2


def test_protected_renders_404_for_nonexistent_db(ui_env) -> None:
    """Auth must precede entity existence checks so we don't leak IDs."""
    app = create_app()
    c = TestClient(app)
    cookie = _login(c, ui_env[1])
    c = _authenticated_client(app, cookie)
    r = c.get("/databases/some-bad-id")
    assert r.status_code == 404


def test_db_add_then_list_strips_session(ui_env) -> None:
    """End-to-end smoke: add a database, see it on the list."""
    app = create_app()
    c = TestClient(app)
    cookie = _login(c, ui_env[1])
    c = _authenticated_client(app, cookie)

    csrf = _extract_csrf(c.get("/databases").text)
    r = c.post(
        "/databases/new",
        data={
            "csrf_token": csrf,
            "kind": "mongo",
            "name": "orders",
            "host": "127.0.0.1",
            "port": "27017",
            "user": "app",
            "password": "",
            "database": "orders",
            "storage": "local",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = c.get("/databases")
    assert "orders" in r.text