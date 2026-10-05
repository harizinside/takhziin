"""MariaDB insufficient-privilege test.

Connects with a user that lacks EVENT and TRIGGER; expects the engine to
refuse clearly with ``InsufficientPrivileges(missing=['EVENT', 'TRIGGER'])``.

Requires the test docker compose stack with mariadb running.
"""

from __future__ import annotations

import os
import time

import mysql.connector
import pytest

from takhziin.backup.mariadb import InsufficientPrivileges, MariadbEngine
from takhziin.models import Database, MariaDatabase


pytestmark = pytest.mark.integration


def _conn_args():
    return dict(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", "3306")),
        user="root",
        password="test",
    )


def _wait_db():
    deadline = time.monotonic() + 30
    last = None
    while time.monotonic() < deadline:
        try:
            c = mysql.connector.connect(connection_timeout=2, **_conn_args())
            c.close()
            return
        except mysql.connector.Error as exc:
            last = exc
            time.sleep(1)
    pytest.skip(f"mariadb unavailable: {last}")


def _bootstrap_test_user(host, port, root_pw):
    """Create a user with SELECT/INSERT/UPDATE/DELETE/SHOW VIEW/LOCK TABLES but
    WITHOUT TRIGGER/EVENT — grant on a fresh schema."""
    conn = mysql.connector.connect(host=host, port=port, user="root", password=root_pw)
    cur = conn.cursor()
    cur.execute("DROP DATABASE IF EXISTS takh_priv_test")
    cur.execute("CREATE DATABASE takh_priv_test")
    cur.execute("DROP USER IF EXISTS 'takh_no_priv'@'%'")
    cur.execute("CREATE USER 'takh_no_priv'@'%' IDENTIFIED BY 'takh_no_priv'")
    cur.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE, SHOW VIEW, LOCK TABLES, EXECUTE "
        "ON takh_priv_test.* TO 'takh_no_priv'@'%'"
    )
    cur.execute("FLUSH PRIVILEGES")
    conn.commit()
    conn.close()


def _cleanup_test_user(host, port, root_pw):
    conn = mysql.connector.connect(host=host, port=port, user="root", password=root_pw)
    cur = conn.cursor()
    cur.execute("DROP DATABASE IF EXISTS takh_priv_test")
    cur.execute("DROP USER IF EXISTS 'takh_no_priv'@'%'")
    conn.commit()
    conn.close()


def test_insufficient_privileges_surfaced():
    _wait_db()
    args = _conn_args()
    _bootstrap_test_user(args["host"], args["port"], args["password"])
    try:
        db = Database(
            name="takh_priv_test",
            kind="mariadb",
            host=args["host"],
            port=args["port"],
            user="takh_no_priv",
            password="takh_no_priv",
            database="takh_priv_test",
            mariadb=MariaDatabase(),
        )
        with pytest.raises(InsufficientPrivileges) as excinfo:
            MariadbEngine().test_connection(db)
        assert "EVENT" in excinfo.value.missing
        assert "TRIGGER" in excinfo.value.missing
        assert "EVENT" in str(excinfo.value)
    finally:
        _cleanup_test_user(args["host"], args["port"], args["password"])


def test_full_grants_pass():
    _wait_db()
    db = Database(
        name="takh_priv_test",
        kind="mariadb",
        host=_conn_args()["host"],
        port=_conn_args()["port"],
        user="root",
        password=_conn_args()["password"],
        database="takh_priv_test",
        mariadb=MariaDatabase(),
    )
    MariadbEngine().test_connection(db)