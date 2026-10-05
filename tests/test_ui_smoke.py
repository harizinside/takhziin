"""End-to-end smoke for the web UI — no docker required.

Boots the FastAPI app via TestClient, walks the create-database → test → list
flow, and asserts the page renders without 500s. Mongo connectivity will
fail (no daemon), but the route should not crash.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from takhziin.ui.app import create_app


@pytest.fixture
def ui_env(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    dat = tmp_path / "data"
    cfg.mkdir(parents=True, exist_ok=True)
    dat.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TAKHZIIN_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("TAKHZIIN_DATA_DIR", str(dat))
    monkeypatch.setenv("TAKHZIIN_BIN_DIR", str(tmp_path / "bin"))
    return tmp_path


def test_ui_smoke(ui_env: TestClient) -> None:
    app = create_app()
    c = TestClient(app)
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
    r = c.post(
        "/databases/new",
        data={
            "kind": "mongo",
            "name": "orders",
            "host": "localhost",
            "port": "27017",
            "user": "app",
            "password": "secret",
            "database": "orders",
            "storage": "local",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # databases list should now include it
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


def test_ui_form_renders_all_kinds(ui_env: TestClient) -> None:
    app = create_app()
    c = TestClient(app)
    r = c.get("/databases/new")
    assert r.status_code == 200
    assert "MongoDB" in r.text
    assert "PostgreSQL" in r.text
    assert "MariaDB" in r.text