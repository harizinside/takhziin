"""PostgreSQL round-trip integration test."""

from __future__ import annotations

import os
import time
import uuid

import psycopg
import pytest

from takhziin.backup import run_backup
from takhziin.config import load_settings
from takhziin.models import BackupStatus, Database
from takhziin.secrets import Secrets
from takhziin.state import State
from takhziin.storage import make_storage

pytestmark = pytest.mark.integration


def _conn_args():
    return dict(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", "test"),
        dbname="postgres",
    )


def _wait_db():
    deadline = time.monotonic() + 30
    last = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(connect_timeout=2, **_conn_args()):
                pass
            return
        except psycopg.Error as exc:
            last = exc
            time.sleep(1)
    pytest.skip(f"postgres unavailable: {last}")


def _seed(name: str) -> None:
    args = _conn_args()
    args["dbname"] = "postgres"
    with psycopg.connect(autocommit=True, **args) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {name}")
            cur.execute(f"CREATE DATABASE {name}")
    args["dbname"] = name
    with psycopg.connect(autocommit=True, **args) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE a (id INT PRIMARY KEY, v TEXT)")
            cur.execute("CREATE TABLE b (id INT PRIMARY KEY, v TEXT)")
            cur.execute("INSERT INTO a VALUES (1, 'x'), (2, 'y')")
            cur.execute("INSERT INTO b VALUES (1, 'z')")
            cur.execute("CREATE VIEW v_ab AS SELECT a.id, a.v AS av, b.v AS bv FROM a JOIN b USING (id)")
            cur.execute(
                """
                CREATE FUNCTION fn_upper(p TEXT) RETURNS TEXT AS $$
                    SELECT upper(p)
                $$ LANGUAGE SQL
                """
            )


def _list_objects(name: str) -> dict:
    args = _conn_args()
    args["dbname"] = name
    out: dict[str, int] = {}
    with psycopg.connect(**args) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind IN ('r','v','S') AND n.nspname='public'"
            )
            out["tables_views"] = len(cur.fetchall())
            cur.execute(
                "SELECT n.nspname || '.' || p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public'"
            )
            out["functions"] = len(cur.fetchall())
    return out


def test_postgres_round_trip(tmp_path):
    _wait_db()
    src = f"takh_src_{uuid.uuid4().hex[:6]}"
    try:
        _seed(src)
        before = _list_objects(src)
        settings = load_settings(
            config_dir=tmp_path / "cfg",
            data_dir=tmp_path / "data",
            bin_dir=tmp_path / "bin",
            skip_yaml=True,
        )
        settings.ensure_dirs()
        secrets = Secrets(master_key_file=settings.master_key_file)
        state = State.load(settings.state_file)
        db = Database(
            name="orders",
            kind="postgres",
            host=_conn_args()["host"],
            port=_conn_args()["port"],
            user=_conn_args()["user"],
            password=secrets.encrypt(_conn_args()["password"]),
            database=src,
        )
        state.add_database(db)
        state.save()
        result = run_backup(
            db,
            state,
            settings,
            storage_factory=lambda d: make_storage(d, settings),
        )
        assert result.record.status == BackupStatus.SUCCESS
        # size should be > 0
        assert result.record.size_bytes > 0
    finally:
        try:
            args = _conn_args()
            args["dbname"] = "postgres"
            with psycopg.connect(autocommit=True, **args) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DROP DATABASE IF EXISTS {src}")
        except psycopg.Error:
            pass