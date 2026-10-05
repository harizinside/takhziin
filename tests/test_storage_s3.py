"""S3 storage round-trip tests using moto-mocked AWS."""

from __future__ import annotations

import io
from pathlib import Path

import boto3
from moto import mock_aws

from takhziin.config import Settings
from takhziin.models import Database, S3StorageOptions
from takhziin.storage.s3 import S3Storage


def _make_db() -> Database:
    return Database(
        name="orders",
        kind="mariadb",
        host="db.example",
        port=3306,
        user="app",
        password="enc",
        database="orders",
        storage_kind="s3",
        s3=S3StorageOptions(
            bucket="takhziin-backups",
            region="us-east-1",
            access_key_id="AKIA-TEST",
            secret_access_key="secret",
            prefix="prod",
        ),
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "data" / "backups",
        bin_dir=tmp_path / "bin",
    )


def test_s3_put_get_signed(tmp_path: Path) -> None:
    with mock_aws():
        # bootstrap the bucket
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="takhziin-backups")

        db = _make_db()
        storage = S3Storage.from_database(db, _settings(tmp_path))

        key = "abc/20240101T000000Z__run.tar.gz"
        location = storage.put(key, io.BytesIO(b"hello s3"), metadata={"schema_objects_count": "5"})
        assert location.startswith("s3://takhziin-backups/prod/abc/")

        # retrieve via the same adapter
        with storage.get(key) as fp:
            assert fp.read() == b"hello s3"

        # signed URL should be generated
        url = storage.signed_url(key, ttl_seconds=60)
        assert url is not None
        assert "X-Amz-Signature" in url

        # delete and verify
        storage.delete(key)
        try:
            storage.get(key)
        except Exception:
            pass
        else:
            raise AssertionError("expected get to fail after delete")


def test_s3_with_custom_endpoint(tmp_path: Path) -> None:
    """Custom endpoint (e.g. R2/MinIO) — boto3 client must carry endpoint_url."""
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="r2-bucket")
        db = _make_db()
        assert db.s3 is not None
        db.s3.endpoint_url = "https://r2.example"
        db.s3.bucket = "r2-bucket"
        storage = S3Storage.from_database(db, _settings(tmp_path))
        # confirm the endpoint URL landed in the boto3 client (without making a
        # network call — moto only intercepts the default AWS endpoint)
        assert storage.client.meta.endpoint_url == "https://r2.example"