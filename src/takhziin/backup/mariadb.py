"""MariaDB backup engine — the headline feature.

This module solves the silent-privilege-loss bug: ``mariadb-dump`` quietly
emits routines/triggers/events only when the connecting user has the
appropriate GRANTs. We check ``SHOW GRANTS`` at dump time and refuse with an
``InsufficientPrivileges`` exception listing the missing privileges so the
operator can grant them.

Dump flags follow the MariaDB reference (section 6.5.4):
  --single-transaction       # InnoDB consistency, no FTWRL
  --quick                    # avoid buffering
  --routines                 # stored procedures + functions
  --triggers                 # table-level triggers
  --events                   # scheduled events
  --add-drop-table           # safe restore over existing data
  --skip-lock-tables         # --single-transaction already provides a snapshot
  --no-tablespaces           # avoid PROCESS privilege requirement
  --set-gtid-purged=OFF      # safer on replicas
  --default-character-set=utf8mb4
  --hex-blob                 # binary-safe
  --max-allowed-packet=1G
  --databases <db>           # or --all-databases when full_server_dump

Credentials are passed via ``--defaults-file=<temp .my.cnf>`` (chmod 0600) so
the password is never visible in ``/proc`` (a property the MYSQL_PWD env var
does NOT provide).
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

import mysql.connector

from takhziin.backup.archive import ArchiveWriter
from takhziin.models import Database
from takhziin.tools import resolve_binary

# Privileges we require at the schema level for a complete dump. Each maps to
# the keyword mariadb/mysql print in SHOW GRANTS lines for schema-level privs.
REQUIRED_PRIVS = {
    "SELECT": "SELECT",
    "SHOW_VIEW": "SHOW VIEW",
    "TRIGGER": "TRIGGER",
    "EVENT": "EVENT",
    "LOCK_TABLES": "LOCK TABLES",
    # EXECUTE is required to dump routines; the GRANT line typically reads
    # "GRANT EXECUTE ON PROCEDURE ..." or includes it in the schema-level
    # grant list.
    "EXECUTE": "EXECUTE",
    # ROUTINE-level SELECT is sometimes granted separately for FUNCTION bodies;
    # SELECT is already in there but we keep this explicit for readability.
}


class InsufficientPrivileges(RuntimeError):
    """Raised when the connecting user lacks privileges needed for a complete dump.

    Attributes:
      db_name: logical database name from the Database model
      missing: list of human-readable privilege names (e.g. ['EVENT', 'TRIGGER'])
    """

    def __init__(self, db_name: str, missing: list[str]):
        self.db_name = db_name
        self.missing = missing
        msg = (
            f"mariadb-dump cannot produce a complete backup of {db_name!r}: "
            f"user is missing privileges: {', '.join(missing)}. "
            f"Grant them on the schema and retry, e.g. "
            f"`GRANT {', '.join(missing)} ON {db_name}.* TO '<user>'@'<host>'`."
        )
        super().__init__(msg)


class MariadbDumpError(RuntimeError):
    """Raised when ``mariadb-dump`` exits non-zero. Carries captured stderr."""


def _strip_backticks(s: str) -> str:
    return s.replace("`", "").strip()


def _parse_grant_line(line: str) -> tuple[set[str], str | None, bool]:
    """Parse one line of ``SHOW GRANTS`` output.

    Returns (priv_keywords, db_filter_or_None, is_global).

    Examples:
      GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, EXECUTE
        ON `orders`.* TO `app`@`%`
      → ({SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, EXECUTE}, 'orders', False)

      GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION
      → ({all}, None, True)
    """
    line = line.strip()
    if not line.startswith("GRANT"):
        return set(), None, False
    body = line[len("GRANT") :].strip()
    # strip "ON ... TO ..." suffix to isolate the privilege keywords
    on_match = re.match(r"^(.+?)\s+ON\s+(.+?)\s+TO\s+", body, re.IGNORECASE)
    if not on_match:
        return set(), None, False
    privs_blob = on_match.group(1)
    on_part = on_match.group(2)
    # detect global
    if re.match(r"^\*\.\*|`\*`\.\*`?$", on_part):
        return {"ALL"}, None, True
    # extract schema from `db`.*
    schema_match = re.match(r"^`?([^`]+)`?\.\*", on_part)
    schema = _strip_backticks(schema_match.group(1)) if schema_match else None
    privs = {
        p.strip().upper()
        for p in re.split(r",\s*", privs_blob)
        if p.strip()
    }
    # ALL includes everything — represent as the literal ALL keyword
    if "ALL" in privs or "ALL PRIVILEGES" in privs:
        return {"ALL"}, schema, schema is None
    return privs, schema, False


def _gather_grants(db: Database) -> list[str]:
    """Run ``SHOW GRANTS`` for the current user; return raw lines."""
    conn = mysql.connector.connect(
        host=db.host,
        port=db.port,
        user=db.user,
        password=db.password,
        database=db.database,
        ssl_disabled=bool(db.mariadb and db.mariadb.ssl_disabled),
        connection_timeout=10,
    )
    try:
        cur = conn.cursor()
        cur.execute("SHOW GRANTS")
        rows = [r[0] for r in cur.fetchall() if r and r[0]]
        return rows
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _check_privileges(db: Database) -> None:
    """Verify required GRANTs; raise :class:`InsufficientPrivileges` on gap.

    Acceptable sources for the privileges:
      - GRANT ... ON <db>.*  with each privilege keyword
      - GRANT ALL ON *.*      (global)
      - GRANT ALL ON <db>.*   (schema-level blanket)
    """
    raw = _gather_grants(db)
    schema_grants: set[str] = set()
    global_grants: set[str] = set()
    has_global_all = False
    has_schema_all = False
    target_db = db.database

    for line in raw:
        privs, schema, is_global = _parse_grant_line(line)
        if "ALL" in privs:
            if is_global:
                has_global_all = True
            elif schema == target_db:
                has_schema_all = True
        if is_global:
            global_grants |= privs
        elif schema == target_db:
            schema_grants |= privs

    effective: set[str] = schema_grants | global_grants
    if has_global_all or has_schema_all:
        return

    missing: list[str] = []
    for key, label in REQUIRED_PRIVS.items():
        if label not in effective and label not in ({"ALL"} & effective):
            missing.append(label)
    if missing:
        raise InsufficientPrivileges(db.name, missing)


def _build_defaults_file(db: Database, path: Path) -> None:
    """Write a temporary ``.my.cnf`` with credentials. chmod 0600."""
    lines = [
        "[client]",
        f"host = {db.host}",
        f"port = {db.port}",
        f"user = {db.user}",
    ]
    if db.password:
        # my.cnf allows quoting; double quotes are interpreted literally
        lines.append(f'password = "{db.password}"')
    if db.mariadb and db.mariadb.ssl_disabled:
        lines.append("ssl = 0")
    elif db.ssl:
        lines.append("ssl = 1")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _build_dump_args(db: Database, defaults_file: Path) -> list[str]:
    binary = resolve_binary("mariadb_dump")
    full = bool(db.mariadb and db.mariadb.full_server_dump)
    args = [
        str(binary.path),
        f"--defaults-file={defaults_file}",
        "--single-transaction",
        "--quick",
        "--routines",
        "--triggers",
        "--events",
        "--add-drop-table",
        "--skip-lock-tables",
        "--no-tablespaces",
        "--set-gtid-purged=OFF",
        "--default-character-set=utf8mb4",
        "--hex-blob",
        "--max-allowed-packet=1G",
    ]
    if full:
        args.append("--all-databases")
    else:
        args.append("--databases")
        args.append(db.database)
    return args


def _query_schema_object_counts(db: Database) -> dict[str, int]:
    """Count views, routines, triggers, events for the target schema."""
    counts = {"views": 0, "routines": 0, "triggers": 0, "events": 0}
    conn = mysql.connector.connect(
        host=db.host,
        port=db.port,
        user=db.user,
        password=db.password,
        database=db.database,
        ssl_disabled=bool(db.mariadb and db.mariadb.ssl_disabled),
        connection_timeout=10,
    )
    queries = {
        "views": (
            "SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s"
        ),
        "routines": (
            "SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s"
        ),
        "triggers": (
            "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s"
        ),
        "events": (
            "SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s"
        ),
    }
    try:
        cur = conn.cursor()
        for key, q in queries.items():
            try:
                cur.execute(q, (db.database,))
                row = cur.fetchone()
                counts[key] = int(row[0]) if row and row[0] is not None else 0
            except mysql.connector.Error:
                # If the user lacks SHOW VIEW / EVENT etc., the count is 0 — this is
                # expected when InsufficientPrivileges is raised and won't be hit
                # because we check privileges before this query.
                counts[key] = 0
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    return counts


def _version_string(binary_path: Path) -> str:
    try:
        proc = subprocess.run(
            [str(binary_path), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if proc.returncode == 0:
            first = (proc.stdout or "").strip().splitlines()[0] if proc.stdout else ""
            return first
    except (OSError, subprocess.TimeoutExpired):
        pass
    return binary_path.name


class MariadbEngine:
    name = "mariadb"

    def test_connection(self, db: Database) -> None:
        _check_privileges(db)

    def dump(self, db: Database, writer: ArchiveWriter) -> None:
        # Privilege audit FIRST — refuse clearly if the dump will be incomplete.
        _check_privileges(db)
        # Query schema-object counts BEFORE the dump so we can verify completeness.
        counts = _query_schema_object_counts(db)
        binary = resolve_binary("mariadb_dump")
        with tempfile.TemporaryDirectory() as td:
            defaults_file = Path(td) / "my.cnf"
            _build_defaults_file(db, defaults_file)
            args = _build_dump_args(db, defaults_file)
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdout is not None
            total = 0
            try:
                while chunk := proc.stdout.read(1024 * 1024):
                    writer.write(chunk)
                    total += len(chunk)
                stderr_bytes = proc.stderr.read() if proc.stderr else b""
                proc.wait(timeout=600)
                if proc.returncode != 0:
                    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
                    raise MariadbDumpError(
                        f"mariadb-dump failed (exit {proc.returncode}): {stderr}"
                    )
            finally:
                if proc.poll() is None:
                    with contextlib.suppress(Exception):
                        proc.kill()
            writer.metadata.engine_version = _version_string(binary.path)
            writer.metadata.schema_objects = counts
            writer.metadata.schema_objects_count = sum(counts.values())


__all__ = [
    "REQUIRED_PRIVS",
    "InsufficientPrivileges",
    "MariadbDumpError",
    "MariadbEngine",
    "_parse_grant_line",
]
