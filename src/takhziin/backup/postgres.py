"""PostgreSQL backup engine.

Subprocess ``pg_dump --format=custom`` streams the dump into the archive.
Custom format is chosen so restore is a single ``pg_restore`` invocation.

The connectivity probe uses psycopg directly so we surface connection
problems with the same error class as Mongo/MariaDB engines.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import psycopg

from takhziin.backup.archive import ArchiveWriter
from takhziin.models import Database
from takhziin.tools import resolve_binary


def _build_conninfo(db: Database) -> str:
    parts = [
        f"host={db.host}",
        f"port={db.port}",
        f"user={db.user}",
        f"dbname={db.database}",
    ]
    if db.password:
        parts.append(f"password={db.password}")
    if db.ssl:
        parts.append("sslmode=require")
    return " ".join(shlex.quote(p) for p in parts)


def _build_pg_dump_args(db: Database, binary_path: Path) -> list[str]:
    args = [
        str(binary_path),
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--jobs=2",
        "--dbname",
        db.database,
        f"--host={db.host}",
        f"--port={db.port}",
        f"--username={db.user}",
    ]
    if db.ssl:
        args.append("--sslmode=require")
    return args


class PostgresEngine:
    name = "postgres"

    def test_connection(self, db: Database) -> None:
        conninfo = _build_conninfo(db)
        try:
            with psycopg.connect(conninfo, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
        except psycopg.Error as exc:
            raise RuntimeError(f"postgres connectivity check failed: {exc}") from exc

    def dump(self, db: Database, writer: ArchiveWriter) -> None:
        binary = resolve_binary("pg_dump")
        args = _build_pg_dump_args(db, binary.path)
        # pg_dump writes to stdout by default — stream straight into writer
        env = os.environ.copy()
        if db.password:
            env["PGPASSWORD"] = db.password
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            assert proc.stdout is not None
            while chunk := proc.stdout.read(1024 * 1024):
                writer.write(chunk)
            stderr_bytes = proc.stderr.read() if proc.stderr else b""
            proc.wait(timeout=60)
            if proc.returncode != 0:
                stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"pg_dump failed (exit {proc.returncode}): {stderr}"
                )
            writer.metadata.engine_version = _version_string(binary.path)
        finally:
            env.pop("PGPASSWORD", None)


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


__all__ = ["PostgresEngine"]
