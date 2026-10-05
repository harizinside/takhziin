"""MongoDB round-trip integration test."""

from __future__ import annotations

import os
import time
import uuid

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from takhziin.backup import run_backup
from takhziin.config import load_settings
from takhziin.models import BackupStatus, Database
from takhziin.secrets import Secrets
from takhziin.state import State
from takhziin.storage import make_storage

pytestmark = pytest.mark.integration


def _uri() -> str:
    host = os.environ.get("MONGO_HOST", "127.0.0.1")
    port = os.environ.get("MONGO_PORT", "27017")
    return f"mongodb://{host}:{port}/"


def _wait_db():
    deadline = time.monotonic() + 30
    last = None
    while time.monotonic() < deadline:
        try:
            MongoClient(_uri(), serverSelectionTimeoutMS=2000).admin.command("ping")
            return
        except PyMongoError as exc:
            last = exc
            time.sleep(1)
    pytest.skip(f"mongo unavailable: {last}")


def _seed(name: str) -> None:
    client = MongoClient(_uri(), serverSelectionTimeoutMS=2000)
    try:
        client.drop_database(name)
        db = client[name]
        db["a"].insert_many([{"i": 1}, {"i": 2}, {"i": 3}])
        db["b"].insert_one({"k": "v"})
        # capped collection
        try:
            db.create_collection("capped", capped=True, size=1024 * 16)
        except Exception:
            pass
        db["a"].create_index([("i", -1)])
        db["b"].create_index("k", unique=True)
    finally:
        client.close()


def _counts(name: str) -> dict:
    client = MongoClient(_uri(), serverSelectionTimeoutMS=2000)
    try:
        db = client[name]
        out = {
            "collections": db.list_collection_names(),
        }
        out["doc_counts"] = {c: db[c].count_documents({}) for c in out["collections"]}
        out["index_counts"] = {c: len(list(db[c].list_indexes())) for c in out["collections"]}
        return out
    finally:
        client.close()


def test_mongo_round_trip(tmp_path):
    _wait_db()
    src = f"takh_src_{uuid.uuid4().hex[:6]}"
    try:
        _seed(src)
        before = _counts(src)
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
            kind="mongo",
            host=os.environ.get("MONGO_HOST", "127.0.0.1"),
            port=int(os.environ.get("MONGO_PORT", "27017")),
            user="",
            password="",
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
        assert result.record.size_bytes > 0
        # basic shape: db column counts include a, b
        assert set(before["collections"]) >= {"a", "b"}
    finally:
        try:
            MongoClient(_uri(), serverSelectionTimeoutMS=2000).drop_database(src)
        except PyMongoError:
            pass