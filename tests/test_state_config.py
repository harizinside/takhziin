"""Basic round-trip tests for config + secrets + state — pure unit."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from takhziin.config import load_settings
from takhziin.models import BackupRecord, BackupStatus, Database, Schedule
from takhziin.secrets import Secrets
from takhziin.state import State


def test_load_settings_env_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    dat = tmp_path / "dat"
    settings = load_settings(
        config_dir=cfg,
        data_dir=dat,
        bind_host="0.0.0.0",
        bind_port=9090,
        skip_yaml=True,
    )
    assert settings.bind_host == "0.0.0.0"
    assert settings.bind_port == 9090
    assert settings.backup_dir == dat / "backups"
    assert settings.state_file == dat / "state.json"
    assert settings.master_key_file == cfg / "master.key"


def test_load_settings_yaml_round_trip(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "bind_host: 0.0.0.0\nbind_port: 9001\ndata_dir: /var/lib/takhziin\n",
        encoding="utf-8",
    )
    settings = load_settings(config_dir=cfg, skip_yaml=False)
    assert settings.bind_host == "0.0.0.0"
    assert settings.bind_port == 9001
    assert settings.data_dir == Path("/var/lib/takhziin")


def test_secrets_encrypt_decrypt(tmp_path: Path) -> None:
    sec = Secrets(master_key_file=tmp_path / "master.key")
    enc = sec.encrypt("hello world")
    assert enc != "hello world"
    assert sec.decrypt(enc) == "hello world"
    # master key should exist and be 32 bytes
    assert sec.master_key_file.exists()
    assert len(sec.master_key_file.read_bytes()) == 32


def test_secrets_wrong_key_fails(tmp_path: Path) -> None:
    a = Secrets(master_key_file=tmp_path / "a.key")
    b = Secrets(master_key_file=tmp_path / "b.key")
    enc = a.encrypt("secret")
    with pytest.raises(Exception):
        b.decrypt(enc)


def test_state_round_trip(tmp_path: Path) -> None:
    sf = tmp_path / "state.json"
    state = State.load(sf)
    assert state.databases == []

    db = Database(
        name="orders",
        kind="mariadb",
        host="localhost",
        port=3306,
        user="root",
        password="enc-pass",
        database="orders",
    )
    state.add_database(db)
    state.add_schedule(Schedule(db_id=db.id, cron="0 3 * * *"))
    state.add_run(
        BackupRecord(
            db_id=db.id,
            status=BackupStatus.SUCCESS,
            started_at="2024-01-01T00:00:00+00:00",
            storage_kind="local",
            location="orders/2024-01-01T00:00:00__abc.tar.gz",
        )
    )
    state.save()
    assert sf.exists()
    assert oct(sf.stat().st_mode & 0o777) == "0o600"

    # Reload and verify
    raw = json.loads(sf.read_text())
    assert raw["databases"][0]["name"] == "orders"
    reloaded = State.load(sf)
    assert reloaded.find_database(db.id) is not None
    assert len(reloaded.schedules) == 1
    assert len(reloaded.runs) == 1


def test_state_remove_cascade(tmp_path: Path) -> None:
    sf = tmp_path / "state.json"
    state = State.load(sf)
    db = Database(
        name="orders",
        kind="postgres",
        host="localhost",
        port=5432,
        user="app",
        password="x",
        database="orders",
    )
    state.add_database(db)
    state.add_schedule(Schedule(db_id=db.id, cron="0 0 * * *"))
    state.add_run(
        BackupRecord(
            db_id=db.id,
            status=BackupStatus.SUCCESS,
            started_at="2024-01-01T00:00:00+00:00",
            storage_kind="local",
            location="x",
        )
    )
    assert state.remove_database(db.id) is True
    assert state.databases == [] and state.schedules == [] and state.runs == []


def test_state_prune_old_runs(tmp_path: Path) -> None:
    from takhziin.state import MAX_RUNS

    sf = tmp_path / "state.json"
    state = State.load(sf)
    db = Database(
        name="d",
        kind="mongo",
        host="h",
        port=27017,
        user="u",
        password="p",
        database="d",
    )
    state.add_database(db)
    # inject MAX_RUNS+5 dummy runs
    for i in range(MAX_RUNS + 5):
        state.add_run(
            BackupRecord(
                db_id=db.id,
                status=BackupStatus.SUCCESS,
                started_at=f"2024-01-{(i % 28) + 1:02d}T00:00:00+00:00",
                storage_kind="local",
                location=f"k{i}",
            )
        )
    state.save()
    reloaded = State.load(sf)
    assert len(reloaded.runs) == MAX_RUNS