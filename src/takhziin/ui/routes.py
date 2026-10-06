"""HTTP routes for the takhziin web UI.

Every route except ``/login`` and the static file mount requires a valid
session cookie. CSRF is enforced on every POST via a form ``csrf_token`` field
verified against ``AuthManager.make_csrf_token`` (HMAC of the session token).
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from takhziin.auth import SessionRow
from takhziin.backup import BackupError, run_backup
from takhziin.models import (
    BackupStatus,
    Database,
    MariaDatabase,
    MongoDatabase,
    PostgresDatabase,
    S3StorageOptions,
    Schedule,
)
from takhziin.state import State
from takhziin.storage import make_storage
from takhziin.ui.auth import (
    COOKIE_NAME,
    csrf_token_for,
    current_session,
    get_auth,
    require_session,
    verify_csrf,
)
from takhziin.ui.telegram_routes import router as telegram_router

router = APIRouter()
router.include_router(telegram_router)


def _is_secure(request: Request) -> bool:
    """Cookie should be Secure except on plain localhost dev.

    Looks at scheme AND the X-Forwarded-Proto header (if behind a TLS-terminating
    reverse proxy). Test client uses scheme=http + host=testserver, so we treat
    that as not-secure too.
    """
    if request.url.scheme == "https":
        return True
    fwd = request.headers.get("x-forwarded-proto", "").lower()
    if fwd.startswith("https"):
        return True
    host = (request.url.hostname or "").lower()
    return host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "testserver", "testclient")


def _set_session_cookie(response: RedirectResponse, request: Request, session_id: str) -> None:
    auth = get_auth(request)
    value = auth.make_cookie_value(session_id)
    response.set_cookie(
        key=COOKIE_NAME,
        value=value,
        max_age=12 * 3600,
        httponly=True,
        secure=_is_secure(request),
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: RedirectResponse, request: Request) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


async def _check_csrf(request: Request, session: SessionRow) -> None:
    """Read the csrf_token form field and verify it. 403 on mismatch."""
    # only POST routes mount this; the form body must already be parsed
    form = await request.form()
    submitted = str(form.get("csrf_token", "")) if form else ""
    if not verify_csrf(request, session, submitted):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid csrf token")


# --- login / logout ---------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request) -> HTMLResponse:
    # If already logged in, bounce to dashboard.
    if current_session(request) is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"title": "Login", "error": None, "username": ""},
    )


@router.post("/login", response_model=None)
async def login_post(request: Request):
    templates: Jinja2Templates = request.app.state.templates
    auth = get_auth(request)
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    if not username or not password:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"title": "Login", "error": "username and password required", "username": username},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    user = auth.verify_credentials(username, password)
    if user is None:
        # Constant-ish delay would be nice; bcrypt already provides one.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"title": "Login", "error": "invalid credentials", "username": username},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    client_ip = request.client.host if request.client else None
    session = auth.create_session(user.id, ip=client_ip)
    redirect = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(redirect, request, session.id)
    return redirect


@router.post("/logout")
def logout(request: Request, _: SessionRow = Depends(require_session)) -> RedirectResponse:
    auth = get_auth(request)
    sess = current_session(request)
    if sess is not None:
        auth.delete_session(sess.id)
    redirect = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _clear_session_cookie(redirect, request)
    return redirect


# --- dashboard --------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: SessionRow = Depends(require_session),
) -> HTMLResponse:
    state: State = request.app.state.state
    templates: Jinja2Templates = request.app.state.templates
    last_24h = [r for r in state.runs if _within_hours(r.started_at, 24)]
    ok = sum(1 for r in last_24h if r.status == BackupStatus.SUCCESS)
    failed = sum(1 for r in last_24h if r.status == BackupStatus.FAILED)
    last_10 = sorted(state.runs, key=lambda r: r.started_at, reverse=True)[:10]
    telegram_status = _telegram_status(state)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "title": "takhziin",
            "db_count": len(state.databases),
            "schedule_count": len(state.schedules),
            "ok_24h": ok,
            "failed_24h": failed,
            "last_10": last_10,
            "databases": state.databases,
            "telegram_status": telegram_status,
            "session": session,
            "csrf": csrf_token_for(request, session),
        },
    )


def _within_hours(iso: str, hours: int) -> bool:
    try:
        d = dt.datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.UTC)
        return (dt.datetime.now(dt.UTC) - d).total_seconds() < hours * 3600
    except (ValueError, TypeError):
        return False


def _telegram_status(state: State) -> dict[str, Any]:
    if state.notifier is None:
        return {"configured": False, "label": "not configured"}
    return {
        "configured": True,
        "label": f"configured ({', '.join(state.notifier.events)})",
    }


# --- databases --------------------------------------------------------------


@router.get("/databases", response_class=HTMLResponse)
def databases_list(
    request: Request,
    session: SessionRow = Depends(require_session),
) -> HTMLResponse:
    state: State = request.app.state.state
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "databases.html",
        {
            "title": "Databases",
            "databases": state.databases,
            "session": session,
            "csrf": csrf_token_for(request, session),
        },
    )


@router.get("/databases/new", response_class=HTMLResponse)
def databases_new(
    request: Request,
    session: SessionRow = Depends(require_session),
) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "database_form.html",
        {
            "title": "New database",
            "db": None,
            "errors": [],
            "session": session,
            "csrf": csrf_token_for(request, session),
        },
    )


@router.post("/databases/new")
async def databases_create(
    request: Request,
    session: SessionRow = Depends(require_session),
) -> RedirectResponse:
    await _check_csrf(request, session)
    state: State = request.app.state.state
    secrets = request.app.state.secrets
    form = await request.form()
    kind = str(form.get("kind", ""))
    if kind not in ("mongo", "postgres", "mariadb"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsupported kind: {kind}")
    try:
        password = str(form.get("password", ""))
        s3_block: dict[str, Any] = {}
        if form.get("storage") == "s3":
            bucket = str(form.get("s3_bucket", "")).strip()
            ak = str(form.get("s3_ak", "")).strip()
            sk = str(form.get("s3_sk", "")).strip()
            if not (bucket and ak and sk):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "S3 requires bucket + ak + sk")
            s3_block = dict(
                bucket=bucket,
                region=str(form.get("s3_region", "us-east-1")),
                endpoint_url=str(form.get("s3_endpoint", "")).strip() or None,
                access_key_id=secrets.encrypt(ak),
                secret_access_key=secrets.encrypt(sk),
                sse=str(form.get("s3_sse", "AES256")) or None,
            )
        s3_opts = S3StorageOptions(**s3_block) if s3_block else None
        mongo = None
        pg = None
        mariadb = None
        if kind == "mongo":
            mongo = MongoDatabase(
                auth_source=str(form.get("mongo_auth_source", "")).strip() or None,
                replica_set=str(form.get("mongo_rs", "")).strip() or None,
            )
        elif kind == "postgres":
            pg = PostgresDatabase(default_schema=str(form.get("pg_schema", "")).strip() or None)
        elif kind == "mariadb":
            mariadb = MariaDatabase(
                full_server_dump=str(form.get("mariadb_full_dump", "")) == "on",
                ssl_disabled=str(form.get("mariadb_ssl_disabled", "on")) == "on",
            )
        db = Database(
            name=str(form.get("name", "")).strip() or f"{kind}-db",
            kind=kind,  # type: ignore[arg-type]
            host=str(form.get("host", "127.0.0.1")),
            port=int(str(form.get("port", 0)) or 0),
            user=str(form.get("user", "")),
            password=secrets.encrypt(password) if password else "",
            database=str(form.get("database", "")),
            ssl=str(form.get("ssl", "")) == "on",
            storage_kind=str(form.get("storage", "local")),  # type: ignore[arg-type]
            s3=s3_opts,
            mongo=mongo,
            postgres=pg,
            mariadb=mariadb,
        )
        state.add_database(db)
        state.save()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"validation: {exc}") from exc
    return RedirectResponse(url="/databases", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/databases/{db_id}/test")
async def database_test(
    request: Request,
    db_id: str,
    session: SessionRow = Depends(require_session),
) -> RedirectResponse:
    await _check_csrf(request, session)
    state: State = request.app.state.state
    secrets = request.app.state.secrets
    db = state.find_database(db_id)
    if db is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown db id {db_id}")
    pw = secrets.decrypt(db.password) if db.password else ""
    db = db.model_copy(update={"password": pw})
    from takhziin.backup import ENGINES

    try:
        ENGINES.for_kind(db.kind).test_connection(db)
    except Exception:
        # don't fail the request — just redirect
        pass
    return RedirectResponse(url="/databases", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/databases/{db_id}/delete")
async def database_delete(
    request: Request,
    db_id: str,
    session: SessionRow = Depends(require_session),
) -> RedirectResponse:
    await _check_csrf(request, session)
    state: State = request.app.state.state
    if not state.remove_database(db_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown db id {db_id}")
    state.save()
    return RedirectResponse(url="/databases", status_code=status.HTTP_303_SEE_OTHER)


# --- backups ----------------------------------------------------------------


@router.get("/backups", response_class=HTMLResponse)
def backups_list(
    request: Request,
    session: SessionRow = Depends(require_session),
) -> HTMLResponse:
    state: State = request.app.state.state
    templates: Jinja2Templates = request.app.state.templates
    runs = sorted(state.runs, key=lambda r: r.started_at, reverse=True)[:100]
    return templates.TemplateResponse(
        request,
        "backups.html",
        {
            "title": "Backups",
            "runs": runs,
            "databases": state.databases,
            "session": session,
            "csrf": csrf_token_for(request, session),
        },
    )


@router.get("/backups/{run_id}", response_class=HTMLResponse)
def backup_detail(
    request: Request,
    run_id: str,
    session: SessionRow = Depends(require_session),
) -> HTMLResponse:
    state: State = request.app.state.state
    templates: Jinja2Templates = request.app.state.templates
    settings = request.app.state.settings
    rec = next((r for r in state.runs if r.id == run_id), None)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown run {run_id}")
    db = state.find_database(rec.db_id)
    download_url = None
    if rec.storage_kind == "local":
        local_path = settings.backup_dir / rec.location
        if local_path.exists():
            download_url = f"/backups/{run_id}/download"
    else:
        try:
            storage = make_storage(db, settings) if db else None  # type: ignore[arg-type]
            if storage is not None:
                download_url = storage.signed_url(rec.location, ttl_seconds=300)
        except Exception:
            download_url = None
    return templates.TemplateResponse(
        request,
        "backup_detail.html",
        {
            "title": f"Run {run_id}",
            "rec": rec,
            "db": db,
            "download_url": download_url,
            "session": session,
            "csrf": csrf_token_for(request, session),
        },
    )


@router.get("/backups/{run_id}/download")
def backup_download(
    request: Request,
    run_id: str,
    _: SessionRow = Depends(require_session),
) -> FileResponse:
    state: State = request.app.state.state
    settings = request.app.state.settings
    rec = next((r for r in state.runs if r.id == run_id), None)
    if rec is None or rec.storage_kind != "local":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown local run {run_id}")
    local_path = settings.backup_dir / rec.location
    if not local_path.exists():
        raise HTTPException(status.HTTP_410_GONE, f"file no longer at {local_path}")
    return FileResponse(local_path, filename=local_path.name, media_type="application/gzip")


@router.post("/backups/run/{db_id}")
async def backup_run(
    request: Request,
    db_id: str,
    session: SessionRow = Depends(require_session),
) -> RedirectResponse:
    await _check_csrf(request, session)
    state: State = request.app.state.state
    settings = request.app.state.settings
    secrets = request.app.state.secrets
    db = state.find_database(db_id)
    if db is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown db {db_id}")
    pw = secrets.decrypt(db.password) if db.password else ""
    db = db.model_copy(update={"password": pw})
    try:
        run_backup(
            db,
            state,
            settings,
            storage_factory=lambda d: make_storage(d, settings),
        )
    except BackupError:
        pass
    return RedirectResponse(url=f"/backups/{state.runs[-1].id}", status_code=status.HTTP_303_SEE_OTHER)


# --- schedules --------------------------------------------------------------


@router.get("/schedules", response_class=HTMLResponse)
def schedules_list(
    request: Request,
    session: SessionRow = Depends(require_session),
) -> HTMLResponse:
    state: State = request.app.state.state
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "title": "Schedules",
            "schedules": state.schedules,
            "databases": state.databases,
            "session": session,
            "csrf": csrf_token_for(request, session),
        },
    )


@router.post("/schedules/new")
async def schedules_create(
    request: Request,
    session: SessionRow = Depends(require_session),
) -> RedirectResponse:
    await _check_csrf(request, session)
    state: State = request.app.state.state
    form = await request.form()
    db_id = str(form.get("db_id", ""))
    cron = str(form.get("cron", ""))
    if not db_id or not cron:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "db_id and cron required")
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(cron)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid cron: {exc}") from exc
    state.add_schedule(Schedule(db_id=db_id, cron=cron))
    state.save()
    return RedirectResponse(url="/schedules", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/schedules/{sched_id}/delete")
async def schedule_delete(
    request: Request,
    sched_id: str,
    session: SessionRow = Depends(require_session),
) -> RedirectResponse:
    await _check_csrf(request, session)
    state: State = request.app.state.state
    if not state.remove_schedule(sched_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown schedule {sched_id}")
    state.save()
    return RedirectResponse(url="/schedules", status_code=status.HTTP_303_SEE_OTHER)


__all__ = ["router"]