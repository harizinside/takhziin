"""HTTP routes for the takhziin web UI."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

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
from takhziin.ui.telegram_routes import router as telegram_router

router = APIRouter()
router.include_router(telegram_router)


def get_state(request: Request) -> State:
    return request.app.state.state  # type: ignore[no-any-return]


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates  # type: ignore[no-any-return]


def _save(request: Request, state: State) -> None:
    state.save()


# --- dashboard --------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    state: State = request.app.state.state
    templates: Jinja2Templates = request.app.state.templates
    now = dt.datetime.now(dt.UTC)
    last_24h = [
        r
        for r in state.runs
        if _within_hours(r.started_at, 24)
    ]
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
def databases_list(request: Request) -> HTMLResponse:
    state: State = request.app.state.state
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "databases.html",
        {"title": "Databases", "databases": state.databases},
    )


@router.get("/databases/new", response_class=HTMLResponse)
def databases_new(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "database_form.html",
        {"title": "New database", "db": None, "errors": []},
    )


@router.post("/databases/new")
async def databases_create(request: Request) -> RedirectResponse:
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
        # kind-specific options
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
def database_test(request: Request, db_id: str) -> RedirectResponse:
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
def database_delete(request: Request, db_id: str) -> RedirectResponse:
    state: State = request.app.state.state
    if not state.remove_database(db_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown db id {db_id}")
    state.save()
    return RedirectResponse(url="/databases", status_code=status.HTTP_303_SEE_OTHER)


# --- backups ----------------------------------------------------------------


@router.get("/backups", response_class=HTMLResponse)
def backups_list(request: Request) -> HTMLResponse:
    state: State = request.app.state.state
    templates: Jinja2Templates = request.app.state.templates
    runs = sorted(state.runs, key=lambda r: r.started_at, reverse=True)[:100]
    return templates.TemplateResponse(
        request,
        "backups.html",
        {"title": "Backups", "runs": runs, "databases": state.databases},
    )


@router.get("/backups/{run_id}", response_class=HTMLResponse)
def backup_detail(request: Request, run_id: str) -> HTMLResponse:
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
        {"title": f"Run {run_id}", "rec": rec, "db": db, "download_url": download_url},
    )


@router.get("/backups/{run_id}/download")
def backup_download(request: Request, run_id: str):
    state: State = request.app.state.state
    settings = request.app.state.settings
    rec = next((r for r in state.runs if r.id == run_id), None)
    if rec is None or rec.storage_kind != "local":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown local run {run_id}")
    local_path = settings.backup_dir / rec.location
    if not local_path.exists():
        raise HTTPException(status.HTTP_410_GONE, f"file no longer at {local_path}")
    return _file_response(local_path)


from fastapi.responses import FileResponse  # noqa: E402  (placed late to avoid cycle)


def _file_response(path: Path) -> FileResponse:
    return FileResponse(path, filename=path.name, media_type="application/gzip")


@router.post("/backups/run/{db_id}")
def backup_run(request: Request, db_id: str) -> RedirectResponse:
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
def schedules_list(request: Request) -> HTMLResponse:
    state: State = request.app.state.state
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {"title": "Schedules", "schedules": state.schedules, "databases": state.databases},
    )


@router.post("/schedules/new")
async def schedules_create(request: Request) -> RedirectResponse:
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
def schedule_delete(request: Request, sched_id: str) -> RedirectResponse:
    state: State = request.app.state.state
    if not state.remove_schedule(sched_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown schedule {sched_id}")
    state.save()
    return RedirectResponse(url="/schedules", status_code=status.HTTP_303_SEE_OTHER)


__all__ = ["router"]
