"""Typer CLI for takhziin — every subcommand lives here.

Layout::

    takhziin init                          create config + data dirs
    takhziin version                       print version
    takhziin healthcheck                   probe the bind socket
    takhziin db add [--interactive]        register a database
    takhziin db list                       show registered databases
    takhziin db test <id>                  run a connection check
    takhziin db rm <id>                    remove database + cascades
    takhziin backup run <db_id>            run a backup now
    takhziin backup list                   show last N runs
    takhziin backup show <run_id>          metadata for one run
    takhziin schedule add <db_id> <cron>   add a cron schedule
    takhziin schedule ls                   list schedules
    takhziin schedule rm <id>              remove a schedule
    takhziin setup-tools                   download mongodump/pg_dump/mariadb-dump
    takhziin notifier set                  configure Telegram bot
    takhziin notifier show                 show config (masked)
    takhziin notifier test                 send test message
    takhziin notifier clear                unset
    takhziin ui [--host] [--port]          start the web UI
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from takhziin import __version__
from takhziin.backup import BackupError, run_backup
from takhziin.config import (
    BIN_DIR_ENV,
    CONFIG_ENV,
    DATA_ENV,
    Settings,
    load_settings,
)
from takhziin.models import (
    Database,
    MariaDatabase,
    MongoDatabase,
    NotifierConfig,
    PostgresDatabase,
    S3StorageOptions,
    Schedule,
)
from takhziin.notifiers.telegram import TelegramNotifier, TelegramSendError
from takhziin.secrets import Secrets
from takhziin.state import State
from takhziin.storage import make_storage

console = Console()

# Three subcommand groups under one root Typer app.
app = typer.Typer(
    name="takhziin",
    help="Lean backup CLI + web UI for MongoDB, PostgreSQL, MariaDB.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Manage registered databases.")
backup_app = typer.Typer(help="Trigger and inspect backups.")
schedule_app = typer.Typer(help="Manage cron schedules.")
notifier_app = typer.Typer(help="Configure Telegram notifier.")
tools_app = typer.Typer(help="Manage native dump binaries.")

app.add_typer(db_app, name="db")
app.add_typer(backup_app, name="backup")
app.add_typer(schedule_app, name="schedule")
app.add_typer(notifier_app, name="notifier")
app.add_typer(tools_app, name="tools")


# --- helpers ----------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"takhziin {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show takhziin version and exit.",
    ),
    config_dir: Path | None = typer.Option(
        None,
        "--config-dir",
        envvar=CONFIG_ENV,
        help="Config dir (overrides env TAKHZIIN_CONFIG_DIR)",
        show_default=False,
    ),
    data_dir: Path | None = typer.Option(
        None,
        "--data-dir",
        envvar=DATA_ENV,
        help="Data dir (overrides env TAKHZIIN_DATA_DIR)",
    ),
    bin_dir: Path | None = typer.Option(
        None,
        "--bin-dir",
        envvar=BIN_DIR_ENV,
        help="Native tool binaries dir (overrides env TAKHZIIN_BIN_DIR)",
    ),
) -> None:
    """takhziin — back up your databases, schedule them, sleep at night."""
    # Surface the overrides as env vars so subcommands + Settings pick them up
    # via the standard load_settings() path.
    if config_dir is not None:
        os.environ[CONFIG_ENV] = str(config_dir)
    if data_dir is not None:
        os.environ[DATA_ENV] = str(data_dir)
    if bin_dir is not None:
        os.environ[BIN_DIR_ENV] = str(bin_dir)


def _resolve_state(settings: Settings) -> tuple[State, Secrets]:
    """Load state + secrets; lazily create master key + state.json."""
    settings.ensure_dirs()
    secrets = Secrets(master_key_file=settings.master_key_file)
    state = State.load(settings.state_file)
    return state, secrets


def _encrypt_password(secrets: Secrets, plaintext: str) -> str:
    return secrets.encrypt(plaintext)


def _decrypt_password(secrets: Secrets, token: str) -> str:
    return secrets.decrypt(token)


# --- init -------------------------------------------------------------------


@app.command()
def init(
    config_dir: Path = typer.Option(
        None,
        "--config-dir",
        help=f"Config dir (env {CONFIG_ENV}; default ~/.config/takhziin)",
    ),
    data_dir: Path = typer.Option(
        None,
        "--data-dir",
        help=f"Data dir (env {DATA_ENV}; default ~/.local/share/takhziin)",
    ),
    bin_dir: Path = typer.Option(
        None,
        "--bin-dir",
        help=f"Native tool binaries dir (env {BIN_DIR_ENV})",
    ),
) -> None:
    """Initialize takhziin directories and master encryption key."""
    settings = load_settings(
        config_dir=config_dir,
        data_dir=data_dir,
        bin_dir=bin_dir,
        skip_yaml=True,
    )
    settings.ensure_dirs()
    secrets = Secrets(master_key_file=settings.master_key_file)
    # touch the key file so init guarantees its existence (Secrets lazy-creates on first use)
    _ = secrets.key
    state = State.load(settings.state_file)
    state.save()
    console.print("[green]✓ initialized[/green]")
    console.print(f"  config: {settings.config_dir}")
    console.print(f"  data:   {settings.data_dir}")
    console.print(f"  bin:    {settings.bin_dir}")
    console.print()
    console.print("Next step: [bold]takhziin db add[/bold] to register a database.")


# --- version + healthcheck ---------------------------------------------------


@app.command()
def version() -> None:
    """Print the takhziin version."""
    console.print(f"takhziin {__version__}")


@app.command()
def healthcheck(
    host: str = typer.Option(None, "--host", help="Bind host (default 127.0.0.1)"),
    port: int = typer.Option(None, "--port", help="Bind port (default 8765)"),
    timeout: float = typer.Option(2.0, "--timeout", help="Connection timeout"),
) -> None:
    """Probe the bind socket — exit 0 if reachable, 1 otherwise."""
    settings = load_settings(skip_yaml=True)
    target_host = host or settings.bind_host
    target_port = port or settings.bind_port
    try:
        with socket.create_connection((target_host, target_port), timeout=timeout):
            sys.exit(0)
    except OSError as exc:
        console.print(f"healthcheck failed: {exc}", style="red")
        sys.exit(1)


# --- db ----------------------------------------------------------------------


@db_app.command("add")
def db_add(
    kind: str = typer.Option(..., "--kind", help="mongo | postgres | mariadb"),
    name: str = typer.Option(..., "--name", help="Human-readable label"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(0, "--port"),
    password: str = typer.Option("", "--password", help="DB password (will be encrypted)"),
    database: str = typer.Option("", "--database", help="Database/schema name"),
    user: str = typer.Option("", "--user"),
    ssl: bool = typer.Option(False, "--ssl/--no-ssl"),
    storage: str = typer.Option("local", "--storage", help="local | s3"),
    full_server_dump: bool = typer.Option(False, "--full-server-dump", help="MariaDB only"),
    interactive_mode: bool = typer.Option(False, "--interactive", "-i"),
) -> None:
    """Register a database. Prompts interactively if --interactive or args missing."""
    if kind not in ("mongo", "postgres", "mariadb"):
        raise typer.BadParameter(f"unsupported kind: {kind}; choose mongo|postgres|mariadb")
    settings = load_settings(skip_yaml=True)
    state, secrets = _resolve_state(settings)

    defaults_by_kind = {"mongo": 27017, "postgres": 5432, "mariadb": 3306}

    if interactive_mode or not port:
        port = typer.prompt("Port", default=port or defaults_by_kind[kind], type=int)
    if interactive_mode or not user:
        user = typer.prompt("User", default=user)
    if interactive_mode or not password:
        password = typer.prompt("Password", hide_input=True, default=password or "")
    if interactive_mode or not database:
        database = typer.prompt("Database name", default=database or name)

    encrypted = _encrypt_password(secrets, password) if password else ""

    # Per-kind options
    mongo_opts = None
    pg_opts = None
    mariadb_opts = None
    if kind == "mongo":
        if interactive_mode:
            auth_source = typer.prompt("authSource (blank for none)", default="")
            rs = typer.prompt("replicaSet (blank for none)", default="")
            if auth_source: mongo_opts = MongoDatabase(auth_source=auth_source)
            if rs: mongo_opts = (mongo_opts or MongoDatabase()).model_copy(update={"replica_set": rs})
    elif kind == "postgres":
        if interactive_mode:
            schema = typer.prompt("default schema (blank for public)", default="")
            pg_opts = PostgresDatabase(default_schema=schema or None)
        else:
            pg_opts = PostgresDatabase()
    elif kind == "mariadb":
        mariadb_opts = MariaDatabase(full_server_dump=full_server_dump)

    s3_opts = None
    if storage == "s3":
        bucket = typer.prompt("S3 bucket")
        region = typer.prompt("Region", default="us-east-1")
        endpoint = typer.prompt("Endpoint URL (blank for AWS)", default="")
        ak = typer.prompt("Access key ID")
        sk = typer.prompt("Secret access key", hide_input=True)
        sse = typer.prompt("SSE (AES256|aws:kms|blank)", default="AES256")
        s3_opts = S3StorageOptions(
            bucket=bucket,
            region=region,
            endpoint_url=endpoint or None,
            access_key_id=secrets.encrypt(ak),
            secret_access_key=secrets.encrypt(sk),
            sse=sse or None,
        )

    db = Database(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        host=host,
        port=port,
        user=user,
        password=encrypted,
        database=database,
        ssl=ssl,
        storage_kind=storage,  # type: ignore[arg-type]
        s3=s3_opts,
        mongo=mongo_opts,
        postgres=pg_opts,
        mariadb=mariadb_opts,
    )
    state.add_database(db)
    state.save()
    console.print(f"[green]✓ added {db.name}[/green] (id={db.id})")


@db_app.command("list")
def db_list() -> None:
    """List registered databases."""
    settings = load_settings(skip_yaml=True)
    state, _ = _resolve_state(settings)
    if not state.databases:
        console.print("[yellow]no databases registered[/yellow]")
        raise typer.Exit()
    table = Table(title="Databases", show_lines=False)
    table.add_column("id")
    table.add_column("name")
    table.add_column("kind")
    table.add_column("host:port")
    table.add_column("database")
    table.add_column("storage")
    for db in state.databases:
        table.add_row(db.id, db.name, db.kind, f"{db.host}:{db.port}", db.database, db.storage_kind)
    console.print(table)


@db_app.command("test")
def db_test(db_id: str) -> None:
    """Test the connection to a registered database."""
    settings = load_settings(skip_yaml=True)
    state, secrets = _resolve_state(settings)
    db = state.find_database(db_id)
    if db is None:
        raise typer.BadParameter(f"unknown database id: {db_id}")
    if db.password:
        # decrypt for the engine call
        object.__setattr__(db, "password", _decrypt_password(secrets, db.password))
    from takhziin.backup import ENGINES

    try:
        ENGINES.for_kind(db.kind).test_connection(db)
    except Exception as exc:
        console.print(f"[red]✗ {db.name} ({db.kind}): {exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]✓ {db.name} ({db.kind}) reachable[/green]")


@db_app.command("rm")
def db_rm(db_id: str, force: bool = typer.Option(False, "--force", "-f")) -> None:
    """Remove a database and its schedules + run records."""
    settings = load_settings(skip_yaml=True)
    state, _ = _resolve_state(settings)
    db = state.find_database(db_id)
    if db is None:
        raise typer.BadParameter(f"unknown database id: {db_id}")
    if not force:
        confirmed = typer.confirm(
            f"Remove {db.name!r} (id={db.id}) and cascade-delete its schedules + runs?",
            default=False
        )
        if not confirmed:
            raise typer.Abort()
    if not state.remove_database(db.id):
        raise typer.BadParameter(f"unknown database id: {db_id}")
    state.save()
    console.print("[green]✓ removed[/green]")


# --- backup ------------------------------------------------------------------


@backup_app.command("run")
def backup_run(db_id: str) -> None:
    """Run a backup right now for the given database."""
    settings = load_settings(skip_yaml=True)
    state, secrets = _resolve_state(settings)
    db = state.find_database(db_id)
    if db is None:
        raise typer.BadParameter(f"unknown database id: {db_id}")
    if db.password:
        object.__setattr__(db, "password", _decrypt_password(secrets, db.password))

    notifier_obj = TelegramNotifier() if state.notifier else None
    cfg = state.notifier

    def _notifier(record: Any) -> None:
        if not notifier_obj or cfg is None:
            return
        try:
            if record.error:
                notifier_obj.send_failure(record, db=db, config=cfg)
            else:
                notifier_obj.send_success(record, db=db, config=cfg)
        except TelegramSendError as exc:
            console.print(f"[yellow]telegram send failed: {exc}[/yellow]")

    try:
        result = run_backup(
            db,
            state,
            settings,
            storage_factory=lambda d: make_storage(d, settings),
            notifier=_notifier,
        )
    except BackupError as exc:
        console.print(f"[red]✗ backup failed[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]✓ backup ok[/green] → {result.record.location} "
        f"({result.record.size_bytes} bytes)"
    )


@backup_app.command("list")
def backup_list(
    limit: int = typer.Option(20, "--limit", "-n"),
    db_id: str | None = typer.Option(None, "--db"),
) -> None:
    """Show recent backup runs."""
    settings = load_settings(skip_yaml=True)
    state, _ = _resolve_state(settings)
    runs = sorted(state.runs, key=lambda r: r.started_at, reverse=True)
    if db_id:
        runs = [r for r in runs if r.db_id == db_id]
    runs = runs[:limit]
    if not runs:
        console.print("[yellow]no runs yet[/yellow]")
        raise typer.Exit()
    table = Table(title=f"Recent backups (last {len(runs)})")
    table.add_column("id")
    table.add_column("db")
    table.add_column("status")
    table.add_column("started")
    table.add_column("size")
    table.add_column("location")
    for r in runs:
        sz = f"{r.size_bytes // 1024} KB" if r.size_bytes else "-"
        table.add_row(r.id, r.db_id, r.status, r.started_at, sz, r.location)
    console.print(table)


@backup_app.command("show")
def backup_show(run_id: str) -> None:
    """Show one backup run's detail."""
    settings = load_settings(skip_yaml=True)
    state, _ = _resolve_state(settings)
    rec = next((r for r in state.runs if r.id == run_id), None)
    if rec is None:
        raise typer.BadParameter(f"unknown run id: {run_id}")
    db = state.find_database(rec.db_id)
    table = Table(show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    rows = [
        ("id", rec.id),
        ("database", db.name if db else rec.db_id),
        ("status", rec.status),
        ("started", rec.started_at),
        ("finished", rec.finished_at or "-"),
        ("size", f"{rec.size_bytes} bytes"),
        ("storage", rec.storage_kind),
        ("location", rec.location),
        ("engine", rec.engine_version),
        ("schema_objects", str(rec.schema_objects_count)),
        ("triggered_by", rec.triggered_by),
    ]
    if rec.error:
        rows.append(("error", rec.error))
    for k, v in rows:
        table.add_row(k, str(v))
    console.print(table)


# --- schedule ----------------------------------------------------------------


@schedule_app.command("add")
def schedule_add(
    db_id: str,
    cron: str = typer.Argument(..., help="5-field cron expression, e.g. '0 3 * * *'"),
) -> None:
    """Add a cron schedule for a database."""
    settings = load_settings(skip_yaml=True)
    state, _ = _resolve_state(settings)
    if state.find_database(db_id) is None:
        raise typer.BadParameter(f"unknown database id: {db_id}")
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(cron)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid cron expression: {exc}") from exc
    sched = Schedule(db_id=db_id, cron=cron)
    state.add_schedule(sched)
    state.save()
    console.print(f"[green]✓ scheduled {db_id}[/green] cron={cron} id={sched.id}")


@schedule_app.command("ls")
def schedule_ls() -> None:
    """List schedules."""
    settings = load_settings(skip_yaml=True)
    state, _ = _resolve_state(settings)
    if not state.schedules:
        console.print("[yellow]no schedules[/yellow]")
        raise typer.Exit()
    table = Table(title="Schedules")
    table.add_column("id")
    table.add_column("db_id")
    table.add_column("cron")
    table.add_column("enabled")
    for s in state.schedules:
        table.add_row(s.id, s.db_id, s.cron, "yes" if s.enabled else "no")
    console.print(table)


@schedule_app.command("rm")
def schedule_rm(sched_id: str) -> None:
    """Remove a schedule."""
    settings = load_settings(skip_yaml=True)
    state, _ = _resolve_state(settings)
    if not state.remove_schedule(sched_id):
        raise typer.BadParameter(f"unknown schedule id: {sched_id}")
    state.save()
    console.print("[green]✓ removed[/green]")


# --- notifier ----------------------------------------------------------------


@notifier_app.command("set")
def notifier_set(
    bot_token: str | None = typer.Option(None, "--bot-token", help="Telegram bot token"),
    chat_id: str | None = typer.Option(None, "--chat-id", help="Chat id (negative for groups)"),
    proxy_url: str | None = typer.Option(None, "--proxy-url", help="HTTP(S) proxy URL"),
    on_success: bool = typer.Option(True, "--on-success/--no-on-success"),
    on_failure: bool = typer.Option(True, "--on-failure/--no-on-failure"),
    interactive: bool = typer.Option(False, "--interactive", "-i"),
) -> None:
    """Configure the Telegram notifier."""
    settings = load_settings(skip_yaml=True)
    state, secrets = _resolve_state(settings)
    if interactive or not bot_token:
        bot_token = typer.prompt("Bot token", hide_input=True, default=bot_token or "")
    if interactive or not chat_id:
        chat_id = typer.prompt("Chat id", default=chat_id or "")
    if not bot_token or not chat_id:
        raise typer.BadParameter("bot_token and chat_id are required")
    # validate the token via getMe BEFORE persisting
    try:
        TelegramNotifier.test_token(bot_token, proxy_url=proxy_url)
    except TelegramSendError as exc:
        raise typer.BadParameter(f"token validation failed: {exc}") from exc
    events: list[str] = []
    if on_success:
        events.append("success")
    if on_failure:
        events.append("failure")
    state.notifier = NotifierConfig(
        kind="telegram",
        bot_token=secrets.encrypt(bot_token),
        chat_id=chat_id,
        events=events,
        proxy_url=secrets.encrypt_optional(proxy_url),
    )
    state.save()
    console.print("[green]✓ notifier configured[/green]")


@notifier_app.command("show")
def notifier_show() -> None:
    """Show the current notifier config (token masked)."""
    settings = load_settings(skip_yaml=True)
    state, secrets = _resolve_state(settings)
    if state.notifier is None:
        console.print("[yellow]notifier not configured[/yellow]")
        raise typer.Exit()
    from takhziin.secrets import mask_token as _mask_token

    cfg = state.notifier
    if cfg.bot_token:
        try:
            masked = _mask_token(secrets.decrypt(cfg.bot_token))
        except Exception:
            masked = "****"
    else:
        masked = "<empty>"
    table = Table(show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("kind", cfg.kind)
    table.add_row("bot_token", masked)
    table.add_row("chat_id", cfg.chat_id)
    table.add_row("events", ",".join(cfg.events))
    table.add_row("proxy_url", "<set>" if cfg.proxy_url else "<none>")
    console.print(table)


@notifier_app.command("test")
def notifier_test() -> None:
    """Send a synthetic test message to the configured chat."""
    settings = load_settings(skip_yaml=True)
    state, secrets = _resolve_state(settings)
    if state.notifier is None:
        raise typer.BadParameter("notifier not configured")
    cfg = state.notifier.model_copy(
        update={
            "bot_token": secrets.decrypt(state.notifier.bot_token),
            "proxy_url": secrets.decrypt_optional(state.notifier.proxy_url),
        }
    )
    TelegramNotifier().send_message(
        "*\\u2728 Test notification*\nThis is a test message from takhziin\\.",
        config=cfg,
    )
    console.print("[green]✓ telegram accepted the message[/green]")


@notifier_app.command("clear")
def notifier_clear(force: bool = typer.Option(False, "--force", "-f")) -> None:
    """Remove the notifier config."""
    settings = load_settings(skip_yaml=True)
    state, _ = _resolve_state(settings)
    if state.notifier is None:
        console.print("[yellow]notifier not configured[/yellow]")
        raise typer.Exit()
    if not force:
        if not typer.confirm("Clear the Telegram notifier configuration?", default=False):
            raise typer.Abort()
    state.notifier = None
    state.save()
    console.print("[green]✓ notifier cleared[/green]")


# --- tools -------------------------------------------------------------------


@tools_app.command("setup")
def tools_setup(force: bool = typer.Option(False, "--force", help="Re-download even if present"))-> None:
    """Download mongodump / pg_dump / mariadb-dump into the configured bin_dir."""
    import platform
    import tarfile
    import urllib.request

    settings = load_settings(skip_yaml=True)
    settings.bin_dir.mkdir(parents=True, exist_ok=True)
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64" if machine in ("x86_64", "amd64") else None
    if system != "darwin" or arch is None:
        console.print(
            f"[yellow]automatic setup-tools is best-effort on {system}/{machine}.[/yellow]\n"
            "Please install via your platform package manager and re-run with --bin-dir set."
        )
        raise typer.Exit(code=1)
    from takhziin.tools import KNOWN_BINARIES, resolve_binary

    # MongoDB Database Tools for Apple Silicon
    mongo_url = "https://fastdl.mongodb.org/tools/db/mongodb-database-tools-macos-arm64-100.9.0.tgz"
    # Postgres client via brew taps is awkward to download; rely on the user.
    # We'll just check pg_dump and mariadb-dump; if missing, exit with hint.
    targets = ["mongodump", "pg_dump", "mariadb_dump"]
    if force:
        for t in targets:
            binary = KNOWN_BINARIES[t][0]
            for p in [settings.bin_dir / binary] + [Path("/usr/local/bin") / binary, Path("/opt/homebrew/bin") / binary]:
                if p.exists():
                    p.unlink()
    for t in targets:
        try:
            resolve_binary(t, settings.bin_dir)
            console.print(f"[green]✓ {t} already present[/green]")
            continue
        except Exception:
            pass
    # mongodump download
    mongo_tgz = settings.bin_dir / "mongo-tools.tgz"
    if not any((settings.bin_dir / n).exists() for n in KNOWN_BINARIES["mongodump"]):
        console.print(f"downloading mongodump → {mongo_tgz}")
        try:
            urllib.request.urlretrieve(mongo_url, mongo_tgz)
            with tarfile.open(mongo_tgz) as tar:
                for member in tar.getmembers():
                    if member.name.endswith("mongodump") or member.name.endswith("mongorestore"):
                        member.name = Path(member.name).name
                        tar.extract(member, str(settings.bin_dir))
                        (settings.bin_dir / member.name).chmod(0o755)
        except Exception as exc:
            console.print(f"[red]mongodump download failed: {exc}[/red]")
        finally:
            mongo_tgz.unlink(missing_ok=True)
    console.print("[green]done. verify with `takhziin tools check`.[/green]")


@tools_app.command("check")
def tools_check() -> None:
    """Verify the native dump binaries are present and on PATH."""
    from takhziin.tools import KNOWN_BINARIES, resolve_binary

    for t in KNOWN_BINARIES:
        try:
            r = resolve_binary(t)
            console.print(f"[green]✓[/green] {t}: {r.path}")
        except Exception as exc:
            console.print(f"[red]✗[/red] {t}: {exc}")


# --- ui ----------------------------------------------------------------------


@app.command()
def ui(
    host: str | None = typer.Option(None, "--host", help="Bind host"),
    port: int | None = typer.Option(None, "--port", help="Bind port"),
) -> None:
    """Start the takhziin web UI."""
    import uvicorn

    settings = load_settings(skip_yaml=True)
    target_host = host or settings.bind_host
    target_port = port or settings.bind_port
    # Configure uvicorn programmatically
    config = uvicorn.Config(
        "takhziin.ui.app:create_app",
        factory=True,
        host=target_host,
        port=target_port,
        log_level="info",
        reload=False,
    )
    server = uvicorn.Server(config)
    console.print(f"[green]takhziin UI starting on http://{target_host}:{target_port}[/green]")
    server.run()


if __name__ == "__main__":
    app()
