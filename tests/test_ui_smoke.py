"""End-to-end smoke for the web UI — no docker required.

Boots the FastAPI app via TestClient, walks the login → create-database →
list flow, and asserts the page renders without 500s. Mongo connectivity
will fail (no daemon), but the route should not crash.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from takhziin.auth import AuthManager
from takhziin.ui.app import create_app
from takhziin.ui.auth import COOKIE_NAME


@pytest.fixture
def ui_env(tmp_path, monkeypatch):
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
    ).create_user("admin", "admin-password")
    return tmp_path


def _logged_in(app, password: str = "admin-password") -> TestClient:
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"username": "admin", "password": password})
    cookie = r.cookies.get(COOKIE_NAME)
    assert cookie, "login did not return a session cookie"
    return TestClient(app, cookies={COOKIE_NAME: cookie}, follow_redirects=False)


def _csrf(html: str) -> str:
    import re
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m is not None
    return m.group(1)


def test_ui_smoke(ui_env) -> None:
    app = create_app()
    c = _logged_in(app)
    # dashboard
    r = c.get("/")
    assert r.status_code == 200
    assert "takhziin" in r.text
    # databases list
    r = c.get("/databases")
    assert r.status_code == 200
    # new form
    r = c.get("/databases/new")
    assert r.status_code == 200
    assert "MariaDB" in r.text
    # submit a mongo database
    csrf = _csrf(c.get("/databases").text)
    r = c.post(
        "/databases/new",
        data={
            "csrf_token": csrf,
            "kind": "mongo",
            "name": "orders",
            "host": "localhost",
            "port": "27017",
            "user": "app",
            "password": "secret",
            "database": "orders",
            "storage": "local",
        },
    )
    assert r.status_code == 303
    r = c.get("/databases")
    assert r.status_code == 200
    assert "orders" in r.text
    # backup list empty
    r = c.get("/backups")
    assert r.status_code == 200
    # schedules
    r = c.get("/schedules")
    assert r.status_code == 200
    # notifications form
    r = c.get("/notifications")
    assert r.status_code == 200
    assert "Telegram" in r.text or "Bot token" in r.text


def test_ui_form_renders_all_kinds(ui_env) -> None:
    app = create_app()
    c = _logged_in(app)
    r = c.get("/databases/new")
    assert r.status_code == 200
    assert "MongoDB" in r.text
    assert "PostgreSQL" in r.text
    assert "MariaDB" in r.text