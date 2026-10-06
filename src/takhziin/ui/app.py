"""FastAPI factory + shared helpers for the takhziin web UI.

The web UI is server-rendered Jinja on top of the same ``State`` JSON file the
CLI reads/writes. Routes are gated by ``require_session`` (cookie-based auth
administered via ``AuthManager``); see ``takhziin/ui/auth.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from takhziin.auth import AuthManager
from takhziin.config import load_settings
from takhziin.secrets import Secrets
from takhziin.state import State
from takhziin.ui.routes import router as ui_router

UI_DIR = Path(__file__).resolve().parent


def _utc_to_local(dt_str: str | None) -> str:
    if not dt_str:
        return "—"
    try:
        parsed = datetime.fromisoformat(dt_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        local = parsed.astimezone()
        return local.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return dt_str


def _human_size(n: int) -> str:
    if not n:
        return "0 B"
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} PB"


def create_app() -> FastAPI:
    settings = load_settings(skip_yaml=True)
    settings.ensure_dirs()
    state = State.load(settings.state_file)
    secrets = Secrets(master_key_file=settings.master_key_file)
    auth = AuthManager(
        db_file=settings.config_dir / "users.db",
        secret_file=settings.config_dir / "session_secret.key",
    )

    app = FastAPI(title="takhziin", version="0.1.0")
    templates = Jinja2Templates(directory=str(UI_DIR / "templates"))
    templates.env.filters["format_dt"] = _utc_to_local
    templates.env.filters["human_size"] = _human_size
    app.state.templates = templates
    app.state.settings = settings
    app.state.state = state
    app.state.secrets = secrets
    app.state.auth = auth

    # Mount static (CSS / JS)
    app.mount(
        "/static",
        StaticFiles(directory=str(UI_DIR / "static")),
        name="static",
    )
    app.include_router(ui_router)

    return app


__all__ = ["create_app"]