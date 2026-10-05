"""Unit tests for the MariaDB grant-line parser — no live DB needed."""

from __future__ import annotations

from takhziin.backup.mariadb import _parse_grant_line


def test_parse_full_schema_grant() -> None:
    line = (
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, RELOAD, SHUTDOWN, "
        "PROCESS, FILE, REFERENCES, INDEX, ALTER, SHOW DATABASES, SUPER, "
        "CREATE TEMPORARY TABLES, LOCK TABLES, EXECUTE, REPLICATION SLAVE, "
        "REPLICATION CLIENT, CREATE VIEW, SHOW VIEW, CREATE ROUTINE, ALTER ROUTINE, "
        "CREATE USER, EVENT, TRIGGER, CREATE TABLESPACE ON `orders`.* TO `app`@`%`"
    )
    privs, schema, is_global = _parse_grant_line(line)
    assert is_global is False
    assert schema == "orders"
    assert "SELECT" in privs
    assert "SHOW VIEW" in privs
    assert "TRIGGER" in privs
    assert "EVENT" in privs
    assert "LOCK TABLES" in privs
    assert "EXECUTE" in privs


def test_parse_global_all() -> None:
    line = "GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION"
    privs, schema, is_global = _parse_grant_line(line)
    assert is_global is True
    assert schema is None
    assert "ALL" in privs


def test_parse_schema_all() -> None:
    line = "GRANT ALL ON `orders`.* TO `app`@`%`"
    privs, schema, is_global = _parse_grant_line(line)
    assert is_global is False
    assert schema == "orders"
    assert "ALL" in privs


def test_parse_partial_schema_grant() -> None:
    line = "GRANT SELECT, INSERT ON `orders`.* TO `app`@`%`"
    privs, schema, is_global = _parse_grant_line(line)
    assert is_global is False
    assert schema == "orders"
    assert privs == {"SELECT", "INSERT"}


def test_parse_unbackticked_schema() -> None:
    line = "GRANT SELECT ON orders.* TO `app`@`%`"
    privs, schema, is_global = _parse_grant_line(line)
    assert schema == "orders"
    assert "SELECT" in privs