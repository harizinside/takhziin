"""MariaDB round-trip integration test.

Requires a live MariaDB reachable at MARIADB_HOST (default 127.0.0.1:3306,
root/test). Run via ``docker compose -f tests/docker-compose.yml up -d mariadb``.

Seeds a view, two routines (procedure + function), two triggers, and one event;
runs ``run_backup``; restores into a fresh MariaDB and asserts object counts
match via ``information_schema``.

Marked with ``@pytest.mark.integration`` so it can be skipped via
``-m 'not integration'`` on developer machines without docker.
"""

from __future__ import annotations

import os
import uuid

import mysql.connector
import pytest

from takhziin.backup import run_backup
from takhziin.config import load_settings
from takhziin.models import (
    BackupStatus,
    Database,
    MariaDatabase,
)
from takhziin.secrets import Secrets
from takhziin.state import State
from takhziin.storage.local import LocalStorage


pytestmark = pytest.mark.integration


def _conn_args() -> dict:
    return dict(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", "3306")),
        user=os.environ.get("MARIADB_USER", "root"),
        password=os.environ.get("MARIADB_PASSWORD", "test"),
    )


def _wait_db() -> None:
    import time

    deadline = time.monotonic() + 30
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            c = mysql.connector.connect(connection_timeout=2, **_conn_args())
            c.close()
            return
        except mysql.connector.Error as exc:
            last = exc
            time.sleep(1)
    pytest.skip(f"mariadb unavailable: {last}")


def _seed_database(name: str) -> None:
    conn = mysql.connector.connect(database="takhziin", **_conn_args())
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
    cur.execute(f"CREATE DATABASE `{name}`")
    conn.commit()
    conn.database = name
    cur = conn.cursor()
    # table
    cur.execute(
        """
        CREATE TABLE orders (
            id INT PRIMARY KEY AUTO_INCREMENT,
            total DECIMAL(10, 2) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # view
    cur.execute(
        """
        CREATE VIEW v_orders AS
        SELECT id, total, created_at FROM orders WHERE total > 0
        """
    )
    # procedure
    cur.execute(
        """
        CREATE PROCEDURE sp_recalc(IN p_id INT)
        BEGIN
            UPDATE orders SET total = total * 1.0 WHERE id = p_id;
        END
        """
    )
    # function
    cur.execute(
        """
        CREATE FUNCTION fn_discount(p_total DECIMAL(10, 2)) RETURNS DECIMAL(10, 2)
        DETERMINISTIC
        RETURN p_total * 0.9
        """
    )
    # triggers
    cur.execute(
        """
        CREATE TRIGGER trg_orders_ai AFTER INSERT ON orders FOR EACH ROW
        BEGIN
            UPDATE orders SET new_total = NEW.total WHERE id = NEW.id;
        END
        """
    )
    cur.execute(
        """
        CREATE TRIGGER trg_orders_au AFTER UPDATE ON orders FOR EACH ROW
        BEGIN
            UPDATE orders SET new_total = NEW.total WHERE id = NEW.id;
        END
        """
    )
    # event
    cur.execute(
        """
        CREATE EVENT evt_daily
         ON SCHEDULE AT CURRENT_TIMESTAMP + INTERVAL 1 DAY
         DO UPDATE orders SET total = total
        """
    )
    conn.commit()
    conn.close()


def _object_counts(name: str) -> dict[str, int]:
    conn = mysql.connector.connect(database=name, **_conn_args())
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA=%s", (name,))
        views = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=%s", (name,))
        routines = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s", (name,))
        triggers = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=%s", (name,))
        events = cur.fetchone()[0]
    finally:
        conn.close()
    return {"views": views, "routines": routines, "triggers": triggers, "events": events}


def _restore(archive_path, target_name: str) -> None:
    """Restore a takhziin archive into a fresh MariaDB database."""
    import subprocess
    import tempfile

    from takhziin.backup.archive import ArchiveReader

    # extract dump.bin from the tar.gz to a temp file, then pipe to mariadb
    with ArchiveReader(archive_path) as r:
        with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tf:
            r.dump_to(tf.name)
            tmp_path = tf.name
    try:
        # create target database first
        conn = mysql.connector.connect(database="takhziin", **_conn_args())
        cur = conn.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS `{target_name}`")
        cur.execute(f"CREATE DATABASE `{target_name}`")
        conn.commit()
        conn.close()
        # stream dump.sql into mariadb on the target
        with open(tmp_path, "rb") as src:
            subprocess.run(  # noqa: S603
                [
                    "mariadb",
                    "-h",
                    _conn_args()["host"],
                    "-P",
                    str(_conn_args()["port"]),
                    "-u",
                    _conn_args()["user"],
                    f"-p{_conn_args()['password']}",
                    target_name,
                ],
                stdin=src,
                check=False,
            )
    finally:
        os.unlink(tmp_path)


def test_mariadb_dump_round_trip(tmp_path) -> None:
    _wait_db()
    src_db = f"takh_src_{uuid.uuid4().hex[:6]}"
    dst_db = f"takh_dst_{uuid.uuid4().hex[:6]}"
    try:
        _seed_database(src_db)
        before = _object_counts(src_db)
        assert before["views"] == 1
        assert before["routines"] == 2
        assert before["triggers"] == 2
        assert before["events"] == 1

        # Configure settings + secrets
        settings = load_settings(
            config_dir=tmp_path / "cfg",
            data_dir=tmp_path / "data",
            bin_dir=tmp_path / "bin",
            skip_yaml=True,
        )
        settings.ensure_dirs()
        secrets = Secrets(master_key_file=settings.master_key_file)
        state = State.load(settings.state_file)

        db_model = Database(
            name="orders",
            kind="mariadb",
            host=_conn_args()["host"],
            port=_conn_args()["port"],
            user=_conn_args()["user"],
            password=secrets.encrypt(_conn_args()["password"]),
            database=src_db,
            mariadb=MariaDatabase(),
        )
        state.add_database(db_model)
        state.save()

        # Run the backup
        from takhziin.storage import make_storage
        result = run_backup(
            db_model,
            state,
            settings,
            storage_factory=lambda d: make_storage(d, settings),
        )
        assert result.record.status == BackupStatus.SUCCESS
        assert result.archive_path is not None

        # Restore into a fresh database
        _restore(result.archive_path, dst_db)

        after = _object_counts(dst_db)
        # the restored database should have the same object counts
        assert after == before, f"object counts diverged: {after} vs {before}"

        # metadata should reflect them too
        from takhziin.backup.archive import ArchiveReader
        with ArchiveReader(result.archive_path) as r:
            meta = r.read_metadata()
        assert meta.schema_objects == before
    finally:
        # cleanup
        try:
            conn = mysql.connector.connect(database="takhziin", **_conn_args())
            cur = conn.cursor()
            cur.execute(f"DROP DATABASE IF EXISTS `{src_db}`")
            cur.execute(f"DROP DATABASE IF EXISTS `{dst_db}`")
            conn.commit()
            conn.close()
        except mysql.connector.Error:
            pass