# takhziin

Lean single-DBA backup CLI + minimal web UI for **MongoDB**, **PostgreSQL**, **MariaDB**.

- **Backup engines**: dumps each database via the native CLI tool (mongodump / pg_dump / mariadb-dump) wrapped in a tar.gz archive with metadata.
- **MariaDB fix**: routines / triggers / events are emitted reliably. Insufficient privileges surface as a clear, named error.
- **Storage**: local filesystem or any S3-compatible endpoint (AWS / R2 / MinIO / B2).
- **Scheduler**: in-process APScheduler, single global bot, JSON state file.
- **Notifier**: single global Telegram bot, MarkdownV2, best-effort (never fails the backup).
- **CLI + Web UI** share the same `state.json`. Single binary = containerised or `uv tool install`.

## Quickstart

```bash
# install
uv tool install takhziin          # or: pipx install takhziin

# bootstrap dirs + master encryption key
takhziin init

# add a database (interactive — prompts for missing fields)
takhziin db add --kind=mariadb --name=orders --interactive

# test connectivity
takhziin db list
takhziin db test <id>

# run a backup now
takhziin backup run <id>

# schedule a cron backup
takhziin schedule add <id> "0 3 * * *"

# launch the web UI
takhziin ui            # http://127.0.0.1:8765
```

## Install the native dump tools

takhziin shells out to `mongodump`, `pg_dump`, `pg_restore`, `mariadb-dump`. Install them via your platform's package manager:

```bash
# macOS (Homebrew)
brew install mongodb-database-tools libpq mariadb-client

# Debian / Ubuntu
apt-get install -y mongodb-database-tools postgresql-client mariadb-client

# or use takhziin's downloader (best-effort, macOS arm64 only):
takhziin tools setup
takhziin tools check    # verify installation
```

If a binary is missing at backup time you'll see `MissingDependencyError` with the install hint.

## Telegram notifier

Single global bot. MarkdownV2 reserved characters in `db.name`, locations and error messages are escaped before message construction.

```bash
takhziin notifier set --interactive
takhziin notifier show          # bot_token is hidden as 123456:****
takhziin notifier test         # sends a synthetic test message
takhziin notifier clear
```

You only get one notifier for the whole instance. The Telegram URL is hard-coded to `https://api.telegram.org/bot<token>/<method>`. To use a proxy, pass `http://` or `socks5h://` to `takhziin notifier set --proxy-url`.

## Configuration paths

| File | Default |
|------|---------|
| Config | `~/.config/takhziin/{config.yaml,master.key}` |
| Data | `~/.local/share/takhziin/{state.json,backups/}` |
| Bin (tools) | auto-resolved from `PATH` or `$TAKHZIIN_BIN_DIR` |

Override via environment: `TAKHZIIN_CONFIG_DIR`, `TAKHZIIN_DATA_DIR`, `TAKHZIIN_BIN_DIR`, `TAKHZIIN_BIND_HOST`, `TAKHIIN_BIND_PORT`. Config-file keys: `bind_host`, `bind_port`, `data_dir`, `backup_dir`, `bin_dir`.

## MariaDB: closing the routines gap

The `mariadb` engine refuses to run when the connecting user lacks the privileges needed for a complete dump. The error names them and tells you the SQL you need:

```
InsufficientPrivileges: mariadb-dump cannot produce a complete backup of
'orders': user is missing privileges: EVENT, TRIGGER. Grant them on the
schema and retry, e.g. `GRANT EVENT, TRIGGER ON orders.* TO 'app'@'%'`.
```

Dump flags used:

```
mariadb-dump --single-transaction --quick --routines --triggers --events
  --add-drop-table --skip-lock-tables --no-tablespaces
  --set-gtid-purged=OFF --default-character-set=utf8mb4
  --hex-blob --max-allowed-packet=1G --databases <db>
```

## Deployment (GHCR + docker-compose)

Image: `ghcr.io/harizinside/takhziin`. Push to `main` builds and publishes `:latest` + a short-SHA tag.

### On a clean repo

```bash
git init
git remote add origin git@github.com:harizinside/takhziin.git
gh repo create harizinside/takhziin --public --source=. --remote=origin --push
```

### First push triggers CI + publish

After the GHCR workflow completes, the image is published (private by default — make it public once):

```bash
gh api --method PATCH /user/packages/container/takhziin -f visibility=public
```

### Production deployment

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps    # (healthy) within ~30s
curl http://localhost:8765/
```

`docker-compose.prod.yml` binds to `127.0.0.1:8765` only — no public exposure by default. For public hosts, front it with nginx + TLS.

## Development

```bash
git clone https://github.com/harizinside/takhziin
cd takhziin
uv venv --python 3.11
uv pip install -e ".[dev]"

# unit tests
pytest tests/ -v

# full integration (requires docker compose)
docker compose -f tests/docker-compose.yml up -d
pytest tests/ -v -m integration
docker compose -f tests/docker-compose.yml down -v

# type / lint
ruff check src/
mypy src/takhziin
```

## Architecture

```
src/takhziin/
  cli.py            Typer entrypoint (init / db / backup / schedule / ui / notifier / tools)
  config.py         YAML + env var settings resolution
  models.py         Pydantic schemas (Database, Schedule, BackupRecord, NotifierConfig)
  state.py          JSON-file state with flock; AES-GCM-encrypted credentials
  backup/__init__.py   BackupEngine protocol + run_backup orchestrator
  backup/archive.py    tar.gz writer / reader; lock-step metadata.json schema
  backup/mongo.py      mongodump subprocess wrapper
  backup/postgres.py   pg_dump subprocess wrapper (psycopg connectivity probe)
  backup/mariadb.py    mariadb-dump subprocess wrapper + privilege check (the gap fix)
  storage/__init__.py  Storage protocol + registry
  storage/local.py     filesystem adapter (atomic .tmp+rename, 0600)
  storage/s3.py        boto3 adapter (any S3-compatible endpoint)
  notifiers/telegram.py  urllib + MarkdownV2 transport; AES-encrypted bot_token at rest
  scheduler.py        BackgroundScheduler wrapper; notifier hook after each record persists
  ui/app.py           FastAPI factory
  ui/routes.py        All HTTP routes
  ui/templates/       Server-rendered Jinja (dark theme)
  ui/static/          Vanilla CSS + tiny app.js
```

## Security

- Master key at `~/.config/takhziin/master.key` is 32 random bytes (chmod 0600). **Loss is unrecoverable.**
- DB passwords, Telegram bot_token and proxy_url are AES-GCM encrypted at rest in `state.json`.
- Local backups are chmod 0600 on disk.
- The web UI binds to `127.0.0.1` by default. No built-in auth — put it behind SSH tunnel / reverse proxy.
- No remote endpoint registration.

## License

MIT.