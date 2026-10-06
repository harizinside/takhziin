# takhziin

> **تخزين** — _takhziin_ (Arabic: to stash, to keep in reserve).
> A single-DBA backup tool for **MongoDB**, **PostgreSQL**, and **MariaDB**.

takhziin ships a CLI and a minimal server-rendered web UI that share one
JSON state file. It shells out to the native dump tools (`mongodump`,
`pg_dump`, `mariadb-dump`) and stores each run as a tar.gz archive with a
companion `metadata.json`. Schedules run in-process via APScheduler;
notifications go to a single global Telegram bot; storage is local FS or
any S3-compatible endpoint.

This project exists because popular alternatives either don't capture
MariaDB routines/triggers/events reliably or carry a lot of features a
single-DBA box doesn't need. The headline behaviour is that
`mariadb-dump` privileges are checked up-front and the exact missing
grant is named — see [The MariaDB gap](#the-mariadb-routines-events-triggers-gap).

---

## Table of contents

- [Features](#features)
- [Quickstart](#quickstart)
- [Concepts](#concepts)
- [CLI reference](#cli-reference)
  - [`takhziin init`](#takhziin-init)
  - [`takhziin auth *`](#takhziin-auth-)
  - [`takhziin db *`](#takhziin-db-)
  - [`takhziin backup *`](#takhziin-backup-)
  - [`takhziin schedule *`](#takhziin-schedule-)
  - [`takhziin notifier *`](#takhziin-notifier-)
  - [`takhziin tools *`](#takhziin-tools-)
  - [`takhziin ui`](#takhziin-ui)
  - [`takhziin healthcheck`, `version`](#takhziin-healthcheck-version)
- [Web UI](#web-ui)
- [The MariaDB routines/events/triggers gap](#the-mariadb-routines-events-triggers-gap)
- [Telegram notifier](#telegram-notifier)
- [Configuration paths & env vars](#configuration-paths--env-vars)
- [Install the native dump tools](#install-the-native-dump-tools)
- [Deployment (GHCR + docker-compose)](#deployment-ghcr--docker-compose)
- [Architecture](#architecture)
- [Security model](#security-model)
- [Operational runbooks](#operational-runbooks)
- [Development](#development)
- [License](#license)

---

## Features

| Area | Behaviour |
|------|-----------|
| Engines | `mongo`, `postgres`, `mariadb` (v1). Each emits a tar.gz dump with `dump.bin` + `metadata.json`. |
| MariaDB privilege check | Refuses clearly when the user lacks `SELECT / SHOW VIEW / TRIGGER / EVENT / LOCK TABLES / EXECUTE`. Error names every missing privilege + the SQL to grant. |
| Storage | Local filesystem (atomic `.tmp`+rename, 0600) and any S3-compatible endpoint (AWS S3 / Cloudflare R2 / MinIO / B2). Path scheme identical → swap without migration. |
| Scheduler | APScheduler `BackgroundScheduler` running in-process; cron expressions; manual + scheduled runs share the same orchestrator. |
| Notifier | Single global Telegram bot. MarkdownV2 reserved characters are escaped before message construction. Best-effort delivery — never fails the backup. |
| State | Single JSON file (`state.json`, 0600) with `fcntl.flock` to keep CLI/web writes coherent. |
| Credentials | All DB passwords, bot tokens and proxy URLs AES-GCM-encrypted at rest with a key file (also 0600). |
| Auth (web UI) | Single-admin bcrypt login, HMAC-signed session cookies, CSRF on every POST. CLI bypasses auth (operators on the shell are already trusted). |
| Distribution | `uv tool install` / `pipx install` for dev hosts; multi-arch (amd64+arm64) GHCR image for production hosts. |

---

## Quickstart

```bash
# 1. install (uv or pipx both fine; uv shown here)
uv tool install takhziin

# 2. bootstrap dirs + master encryption key + admin password
takhziin init
# -> prompts for an admin password (min 8 chars), creates master.key,
#    users.db, session_secret.key, state.json.

# 3. add a database interactively
takhziin db add --kind=mariadb --name=orders --interactive
# (or pass all fields: --host 10.0.0.5 --port 3306 --user backup --password ...)

# 4. install the native dump tools the engine will shell out to
brew install mongodb-database-tools libpq mariadb-client   # macOS
# apt-get install -y mongodb-database-tools postgresql-client mariadb-client  # Debian/Ubuntu
takhziin tools check   # verify

# 5. test connectivity
takhziin db list
takhziin db test <id>

# 6. take a backup now
takhziin backup run <id>

# 7. schedule it
takhziin schedule add <id> "0 3 * * *"

# 8. (optional) wire a Telegram bot for success/failure alerts
takhziin notifier set --interactive

# 9. launch the web UI
takhziin ui           # default: http://127.0.0.1:8765
# log in with user=admin and the password you set in step 2.
```

The dashboard shows last-24h OK/failed counts, recent runs, the Telegram
status pill, and empty states with next-step CTAs.

---

## Concepts

takhziin treats four things distinctly:

1. **Databases** — connection metadata. Encrypted at rest. Each carries a
   per-database storage adapter (local FS or S3).
2. **Schedules** — cron expressions bound to a database id.
3. **Runs** — one execution of a backup. `running` / `success` / `failed`,
   with `started_at`, `finished_at`, `size_bytes`, `location`, schema-object
   counts (MariaDB), engine version, and optional error.
4. **Notifiers** — single global Telegram bot config (bot_token encrypted at
   rest, chat_id + enabled events + optional proxy URL).

All four live in the same `state.json`. The web UI and CLI both read/write
that file under a `fcntl.flock`.

---

## CLI reference

Every command supports `--help`. All paths default to
`~/.config/takhziin/` and `~/.local/share/takhziin/`; override via
`TAKHZIIN_CONFIG_DIR`, `TAKHZIIN_DATA_DIR`, `TAKHZIIN_BIN_DIR`.

### `takhziin init`

Create `config_dir`, `data_dir`, `bin_dir`, `master.key`, `users.db`,
`session_secret.key`, and an initial `state.json`. Prompts for an admin
password on first run; skips that prompt if a user already exists.

```bash
takhziin init [--config-dir PATH] [--data-dir PATH] [--bin-dir PATH]
```

### `takhziin auth *`

| Subcommand | Purpose |
|-----------|---------|
| `auth status` | Show admin user, created_at, last_login_at |
| `auth passwd -u admin` | Rotate the admin password; revokes all sessions |
| `auth login -u admin` | Verify credentials, print `Cookie: takhzi_session=…` for curl scripts |

### `takhziin db *`

| Subcommand | Purpose |
|-----------|---------|
| `db add` | Register a database. `--kind {mongo,postgres,mariadb}`. Interactive prompts if a field is missing. MariaDB: `--full-server-dump` toggles `--all-databases`. S3: `--storage s3` plus `--s3-bucket`, `--s3-region`, `--s3-endpoint`, `--s3-ak`, `--s3-sk`, `--s3-sse`. |
| `db list` | Tabular view of all registered databases. |
| `db test <id>` | Run a connectivity probe + privilege audit (MariaDB refuses clearly on missing GRANTs). |
| `db rm <id> [-f]` | Remove a database; cascades its schedules and run records. |

### `takhziin backup *`

| Subcommand | Purpose |
|-----------|---------|
| `backup run <db_id>` | Run a backup immediately; live progress via Rich; on completion writes to `data/backups/` and uploads to the database's storage. |
| `backup list [-n 20] [--db ID]` | Show the most recent runs. |
| `backup show <run_id>` | Detail view: status, duration, size, location, engine version, schema_objects (MariaDB), error. |

### `takhziin schedule *`

| Subcommand | Purpose |
|-----------|---------|
| `schedule add <db_id> "0 3 * * *"` | Add a cron schedule (5-field crontab). |
| `schedule ls` | List all schedules. |
| `schedule rm <id>` | Remove a schedule. |

The scheduler reads `state.schedules` on startup and adds jobs to APScheduler.
`CronTrigger.from_crontab` rejects invalid expressions.

### `takhziin notifier *`

| Subcommand | Purpose |
|-----------|---------|
| `notifier set --interactive` | Configure the global Telegram bot. Bot token validated via `getMe` before persisting. |
| `notifier show` | Print config with `bot_token` masked (`123456:****`). |
| `notifier test` | Send a synthetic message; exit 0 if Telegram returns 200, else exit 2. |
| `notifier clear [-f]` | Remove notifier config. |

### `takhziin tools *`

| Subcommand | Purpose |
|-----------|---------|
| `tools setup [--force]` | Best-effort download of `mongodump`/`mongorestore` into `$TAKHZIIN_BIN_DIR` (macOS arm64). Other tools rely on package manager. |
| `tools check` | Verify `mongodump`, `mongorestore`, `pg_dump`, `pg_restore`, `mariadb-dump`, `mariadb`, `mongosh` resolve on `PATH` or in `bin_dir`. |

### `takhziin ui`

Start the FastAPI/uvicorn server. Default `127.0.0.1:8765`; override with
`TAKHZIIN_BIND_HOST`/`TAKHZIIN_BIND_PORT` env vars or `--host`/`--port`.

### `takhziin healthcheck`, `version`

- `healthcheck [--host H] [--port P] [--timeout 2]`: probe the bind socket —
  exit 0 on success, 1 on failure. Used by the image's `HEALTHCHECK`.
- `version`: print the takhziin version and exit.

---

## Web UI

The web UI is server-rendered Jinja over FastAPI on top of the same
`state.json` the CLI uses. Dark theme (Minimalism / Swiss), accessible
focus rings, status pills with `role="status" aria-atomic="true"`,
empty-state CTAs, responsive grid (1-col <640px, 2-col <1024px, 4-col ≥1024px).

Routes:

| Path | Purpose |
|------|---------|
| `GET /` | Dashboard: stats + recent runs + Telegram status |
| `GET /databases`, `GET /databases/new` | List + form |
| `POST /databases/new` | Create (CSRF + auth) |
| `POST /databases/{id}/test` | Connectivity probe (CSRF + auth) |
| `POST /databases/{id}/delete` | Cascade-remove (CSRF + auth) |
| `GET /backups`, `GET /backups/{run_id}` | List + detail |
| `GET /backups/{run_id}/download` | Local-storage archive download |
| `POST /backups/run/{db_id}` | Ad-hoc run (CSRF + auth) |
| `GET /schedules` | List + inline create form |
| `POST /schedules/new` | Add schedule (CSRF + auth) |
| `POST /schedules/{id}/delete` | Remove (CSRF + auth) |
| `GET /notifications`, `POST /notifications/save|test|clear` | Telegram bot config |
| `GET /login`, `POST /login`, `POST /logout` | Auth |

All routes except `/login` and `/static/*` require an authenticated
session. Every POST carries a CSRF token in a hidden form field.

---

## The MariaDB routines/events/triggers gap

`mariadb-dump <db>` only emits routines/triggers/events when the
connecting role has the appropriate GRANTs. Tools that don't check will
silently produce a partial dump — a restore looks fine until the
application calls a missing stored procedure or finds its triggers
didn't fire.

takhziin refuses the dump up-front and prints the exact missing GRANTs:

```
mariadb-dump cannot produce a complete backup of 'orders': user is
missing privileges: EVENT, TRIGGER. Grant them on the schema and retry,
e.g. `GRANT EVENT, TRIGGER ON orders.* TO 'app'@'%'`.
```

Required privileges (all schema-scoped): `SELECT`, `SHOW VIEW`, `TRIGGER`,
`EVENT`, `LOCK TABLES`, `EXECUTE`. `GRANT ALL ON db.*` is treated as
sufficient.

Dump flags (built into the engine, not configurable in v1):

```
mariadb-dump --single-transaction --quick --routines --triggers --events
  --add-drop-table --skip-lock-tables --no-tablespaces
  --set-gtid-purged=OFF --default-character-set=utf8mb4
  --hex-blob --max-allowed-packet=1G --databases <dbname>
```

Credentials are passed via `--defaults-file=<temp .my.cnf>` (chmod 0600,
per-call `tempfile.TemporaryDirectory`) instead of `MYSQL_PWD` so the
password doesn't leak via `/proc/*/environ`. After the dump, the engine
counts `views / routines / triggers / events` from `information_schema`
and stores them in `metadata.json.schema_objects` for completeness audits.

Per-database opt-in `--full-server-dump` flips `--databases <db>` to
`--all-databases` — useful when one role has visibility across schemas.

---

## Telegram notifier

Single global bot, single global chat. MarkdownV2 with strict character
escaping on every user-controlled field (`db.name`, location, error).

Setup with the bot:

```bash
# 1. talk to @BotFather on Telegram, get a token
# 2. talk to your bot, send any message
# 3. get the chat_id via https://api.telegram.org/bot<token>/getUpdates
takhziin notifier set --interactive
# enter bot token, chat id, optional proxy url, success/failure checkboxes
# -> calls getMe to validate the token BEFORE persisting

# 4. verify
takhziin notifier test         # synthetic "Test notification" message

# 5. inspect (bot_token is masked)
takhziin notifier show
```

Both `bot_token` and `proxy_url` are AES-GCM encrypted at rest in
`state.json`; only `chat_id` and `events` are plaintext. The default
events are `["success", "failure"]`.

Optional `proxy_url` accepts `http://` or `socks5h://` URLs. When set,
the notifier builds an opener with `ProxyHandler` using stdlib `urllib`
(no extra deps). SOCKS proxies need `PySocks` installed manually.

---

## Configuration paths & env vars

| What | Default |
|------|---------|
| Config dir | `~/.config/takhziin/` |
| Master key | `$config_dir/master.key` (32 random bytes, chmod 0600) |
| Auth DB | `$config_dir/users.db` (chmod 0600) |
| Session HMAC key | `$config_dir/session_secret.key` (chmod 0600) |
| Config YAML | `$config_dir/config.yaml` (optional) |
| Data dir | `~/.local/share/takhziin/` |
| State file | `$data_dir/state.json` (chmod 0600) |
| Backups | `$data_dir/backups/<db_id>/<UTC_ISO8601>__<uuid>.tar.gz` (chmod 0600) |
| Bin dir | auto: `/usr/bin` (image) or `~/.local/share/takhziin/bin` (host) |
| Bind | `127.0.0.1:8765` |

Env vars override everything:

| Variable | Overrides |
|----------|-----------|
| `TAKHZIIN_CONFIG_DIR` | `Settings.config_dir` |
| `TAKHZIIN_DATA_DIR` | `Settings.data_dir` |
| `TAKHZIIN_BIN_DIR` | `Settings.bin_dir` |
| `TAKHZIIN_BIND_HOST` | `Settings.bind_host` |
| `TAKHZIIN_BIND_PORT` | `Settings.bind_port` |

`config.yaml` overrides everything else (only `bind_host`, `bind_port`,
`data_dir`, `backup_dir`, `bin_dir` keys recognised).

---

## Install the native dump tools

takhziin shells out to the upstream dump tools — they're not vendored.
Install via your package manager.

```bash
# macOS (Homebrew)
brew install mongodb-database-tools libpq mariadb-client

# Debian / Ubuntu (apt)
apt-get install -y mongodb-database-tools postgresql-client mariadb-client

# or use takhziin's downloader (best-effort, macOS arm64 only)
takhziin tools setup
takhziin tools check   # verify each binary resolves on PATH or bin_dir
```

If a binary is missing at backup time you'll see
`MissingDependencyError(tool='pg_dump')` and the install command for
your platform.

---

## Deployment (GHCR + docker-compose)

The Docker image is multi-arch (`linux/amd64`, `linux/arm64`), built and
published to `ghcr.io/harizinside/takhziin` on every push to `main` via
`.github/workflows/build.yml`.

### On a fresh repo

```bash
git init
git remote add origin git@github.com:harizinside/takhziin.git
gh repo create harizinside/takhziin --public --source=. --remote=origin --push
```

`gh` here uses your local `gho_*` token with `repo` scope — sufficient
for `repo create` and `git push`. The CI workflow uses the workflow's
ephemeral `GITHUB_TOKEN` with `packages: write` to push to GHCR.

### First push triggers CI + publish

After the workflow completes:

```bash
gh api --method PATCH /user/packages/container/takhziin -f visibility=public
```

(`packages: write` lets the workflow do this automatically, but user-level
package visibility is sometimes restricted — the documented fallback is
this one-time `gh api` call.)

### Production deployment

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps     # (healthy) within ~30s
curl http://localhost:8765/
```

`docker-compose.prod.yml` binds to `127.0.0.1:8765` only. For remote
exposure put nginx + TLS in front (the file is commented to make the
intended host config obvious).

Pin a specific version by overriding the image:

```bash
TAKHZIIN_IMAGE_TAG=v0.1.0 docker compose -f docker-compose.prod.yml pull
TAKHZIIN_IMAGE_TAG=v0.1.0 docker compose -f docker-compose.prod.yml up -d
```

### Inside the image

| Path | Purpose |
|------|---------|
| `/usr/bin/mongodump`, `/usr/bin/pg_dump`, `/usr/bin/mariadb-dump` | From `mongodb-database-tools`, `postgresql-client`, `mariadb-client` apt packages |
| `/usr/local/...` | Python deps + takhziin |
| `/app/src/` | Source tree (read-only) |
| `/data` | Volume target — `master.key`, `users.db`, `state.json`, `backups/` |

Container runs as unprivileged `takhziin` (uid 1000) with `tini` as PID 1.

---

## Architecture

```
src/takhziin/
  cli.py            Typer entrypoint (init / db / backup / schedule / ui / notifier / tools / auth)
  config.py         YAML + env var settings resolution
  models.py         Pydantic schemas (Database, Schedule, BackupRecord, NotifierConfig)
  state.py          JSON-file state with flock; AES-GCM-encrypted credentials
  auth/             bcrypt + sqlite-backed auth (users.db), session cookies, CSRF
    __init__.py     re-exports
    manager.py      AuthManager class — users + sessions + cookie MAC + CSRF tokens
  secrets.py        AES-GCM wrapper for DB passwords / bot tokens / proxy URLs
  tools.py          Native binary resolver (mongodump / pg_dump / mariadb-dump / mongosh)
  backup/
    __init__.py     BackupEngine Protocol + run_backup orchestrator + registry
    archive.py      tar.gz writer / reader + metadata.json schema
    mongo.py        mongodump subprocess wrapper
    postgres.py     pg_dump subprocess wrapper + psycopg connectivity probe
    mariadb.py      mariadb-dump subprocess wrapper + privilege check (the gap fix)
  storage/
    __init__.py     Storage Protocol + registry
    local.py        filesystem adapter (atomic .tmp+rename, 0600)
    s3.py            boto3 adapter (any S3-compatible endpoint)
  notifiers/
    __init__.py     Notifier Protocol + default factory
    base.py          Protocol definitions
    telegram.py     urllib + MarkdownV2 transport; AES-encrypted bot_token at rest
  scheduler.py      APScheduler BackgroundScheduler wrapper; notifier hook after each record persists
  ui/
    app.py          FastAPI factory (mounts AuthManager on app.state.auth)
    auth.py         require_session dependency + CSRF helpers
    routes.py       All HTTP routes (gated; CSRF on POST)
    telegram_routes.py   Telegram notifier panel
    templates/      Server-rendered Jinja (dark theme)
    static/         Vanilla CSS + tiny app.js
```

### Wire format (archive on disk)

Each backup is a gzip-wrapped tar containing exactly two files:

- `dump.bin` — the upstream tool's own output (BSON for mongodump, custom
  format for pg_dump, SQL for mariadb-dump).
- `metadata.json` — operator-readable audit record:

  ```json
  {
    "takhziin_version": "0.1.0",
    "db_kind": "mariadb",
    "db_id": "abc123",
    "db_name": "orders",
    "started_at": "2024-01-01T03:00:00+00:00",
    "finished_at": "2024-01-01T03:00:42+00:00",
    "schema_objects_count": 7,
    "engine_version": "mariadb-dump Ver 11.0 Distrib 11.0.0",
    "schema_objects": { "views": 1, "routines": 2, "triggers": 2, "events": 1 }
  }
  ```

Restore tooling should compare `schema_objects` counts against a fresh
`information_schema` query to detect drift.

---

## Security model

| Surface | Encrypted? | Noted |
|---------|----------------|--------|
| DB passwords in `state.json` | AES-GCM | per-load `master.key` |
| Telegram bot_token, proxy_url | AES-GCM | per-load `master.key` |
| Admin password | bcrypt (cost 12) | not reversible |
| Session token (DB) | random 32 bytes, opaque | revoked on `passwd` |
| Cookie value | `<token>.<HMAC(token, secret)>` | tamper-evident |
| CSRF token | HMAC(session_id, b"csrf:", secret) | derived, not stored |
| `master.key` (32 bytes) | chmod 0600 | **loss is unrecoverable** |
| `users.db` | chmod 0600 | bcrypt hashes + sessions |
| `session_secret.key` (32 bytes) | chmod 0600 | rotates every installed session; invalidate by deleting the file (will rotate on next startup) |
| Backup files | chmod 0600 on disk | optional `SSE` on S3 (`AES256` default, `aws:kms` opt-in) |
| Web UI bind | `127.0.0.1` default | `Secure` cookie attribute auto-applied on HTTPS or non-loopback |

The web UI **does not** ship with an external auth provider — single admin
is intentional. For multi-tenant use, integrate your reverse proxy's
auth or replace `AuthManager` with OIDC.

CLI commands (`takhziin db add`, `takhziin backup run`, …) **do not**
require login; operators on the shell are already trusted. If you SSH
into the box and your shell history leaks the password, the operator did
something worse than a backup tool can fix.

---

## Operational runbooks

### Reset admin password

```bash
takhziin auth passwd -u admin    # prompts twice; revokes all sessions
```

### Force-logout all sessions (suspected cookie leak)

```bash
# delete the session DB rows; takhziin writes no zombies back.
sqlite3 "$TAKHZIIN_DATA_DIR/../config/users.db" "DELETE FROM sessions;"
# or rotate the HMAC secret so all cookies fail MAC check (also logs
# everyone out, even if sessions table is not purged):
rm "$TAKHZIIN_CONFIG_DIR/session_secret.key"
# next request to /login regenerates it; existing cookies are invalid.
```

### Recover a lost `master.key`

You cannot. `master.key` encrypts every credential in `state.json`. If
it's gone, `takhziin` can read no password. Wipe `state.json`, re-add
databases, re-set the notifier.

### First-run mistakes

I ran into each of these while writing the README. The CLI tells you:

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: takhziin` | forgot `source .venv/bin/activate` | activate the venv |
| `externally-managed-environment` on Debian 12 / macOS 15+ | PEP 668 | use venv, uv, pipx, or `--break-system-packages` |
| `MissingDependencyError(tool='pg_dump')` | dump tool missing | install via package manager (see [Install](#install-the-native-dump-tools)) |
| `mariadb-dump cannot produce a complete backup` | missing GRANTs | run the suggested `GRANT` (see [MariaDB gap](#the-mariadb-routinesevents-triggers-gap)) |
| Telegram nothing arrives | `takhziin notifier test` failed | run `notifier test` first; check chat_id, firewall outbound to `api.telegram.org` |
| `takhziin healthcheck` returns 1 | port in use | `TAKHZIIN_BIND_PORT=9876 takhziin ui` |
| `bootstrap admin password` is empty / short | min length 8 | enter ≥ 8 chars |

---

## Development

```bash
git clone https://github.com/harizinside/takhziin
cd takhziin
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"

# unit tests (no docker required)
pytest tests/ -v \
  --ignore=tests/test_mariadb_dump.py \
  --ignore=tests/test_mariadb_privileges.py \
  --ignore=tests/test_postgres_dump.py \
  --ignore=tests/test_mongo_dump.py

# integration tests (docker compose must be running)
docker compose -f tests/docker-compose.yml up -d
pytest tests/ -v -m integration
docker compose -f tests/docker-compose.yml down -v

# lint / type
ruff check src tests
mypy src/takhziin
```

The design system is checked into the repo under `design-system/takhziin/`
(Master + page overrides) — generated by `ui-ux-pro-max` against the
shipping product surface. Update via `--force` only when intentionally
discarding prior decisions.

---

## License

MIT. See [`LICENSE`](LICENSE) (or your fork's equivalent).

---

## Acknowledgements

takhziin stands on the upstream `mongodump`, `pg_dump`, and
`mariadb-dump` — those projects do the hard work. The auth design
follows the standard "opaque session token + signed cookie + CSRF
derivation" pattern (OWASP ASVS V3/V4). The MariaDB privilege-check
story is the same one captured (and frequently missed) by
[databasus/databasus](https://github.com/databasus/databasus) — which is
what this tool replaces for the author.