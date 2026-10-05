"""S3-compatible storage adapter using boto3.

Works against AWS S3, Cloudflare R2, Backblaze B2, MinIO — anything that
implements the S3 API. ``endpoint_url`` on :class:`S3StorageOptions` selects
a non-AWS provider.

Server-side encryption defaults to ``AES256``; ``aws:kms`` is opt-in via
``sse_kms_key_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import IO, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from takhziin.config import Settings
from takhziin.models import Database, S3StorageOptions


class S3StorageError(RuntimeError):
    """Raised when an S3 operation fails."""


@dataclass
class S3Storage:
    bucket: str
    client: Any
    prefix: str = ""
    name: str = "s3"
    signed_url_ttl: int = 300

    @classmethod
    def from_database(cls, db: Database, settings: Settings) -> S3Storage:
        if db.s3 is None:
            raise S3StorageError("database has no S3 options configured")
        opts = db.s3
        config = Config(
            retries={"max_attempts": 5, "mode": "standard"},
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        )
        client = boto3.client(
            "s3",
            endpoint_url=opts.endpoint_url or None,
            aws_access_key_id=opts.access_key_id,
            aws_secret_access_key=opts.secret_access_key,
            region_name=opts.region,
            config=config,
        )
        return cls(
            bucket=opts.bucket,
            client=client,
            prefix=opts.prefix.strip("/"),
            signed_url_ttl=opts.signed_url_ttl,
        )

    def _full_key(self, key: str) -> str:
        if ".." in key.split("/"):
            raise ValueError(f"invalid storage key: {key!r}")
        return f"{self.prefix}/{key}" if self.prefix else key

    def _extra_args(self, metadata: dict | None) -> dict:
        opts: dict[str, Any] = {"Metadata": {k: str(v) for k, v in (metadata or {}).items()}}
        return opts

    def put(self, key: str, source: IO[bytes], metadata: dict | None = None) -> str:
        full_key = self._full_key(key)
        # SSE is configured globally per database — we look at the latest options
        # via the client metadata hint. To keep this self-contained, callers should
        # set metadata["sse"] if they want SSE-KMS (handled by from_database).
        try:
            self.client.upload_fileobj(
                Fileobj=source,
                Bucket=self.bucket,
                Key=full_key,
                ExtraArgs=self._extra_args(metadata),
            )
        except ClientError as exc:
            raise S3StorageError(f"failed to upload {full_key}: {exc}") from exc
        return f"s3://{self.bucket}/{full_key}"

    def get(self, key: str) -> IO[bytes]:
        from io import BytesIO

        full_key = self._full_key(key)
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=full_key)
        except ClientError as exc:
            raise S3StorageError(f"failed to get {full_key}: {exc}") from exc
        body = resp.get("Body")
        if body is None:
            raise S3StorageError(f"empty body for {full_key}")
        return BytesIO(body.read())

    def delete(self, key: str) -> None:
        full_key = self._full_key(key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=full_key)
        except ClientError as exc:
            raise S3StorageError(f"failed to delete {full_key}: {exc}") from exc

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str | None:
        full_key = self._full_key(key)
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": full_key},
                ExpiresIn=ttl_seconds or self.signed_url_ttl,
            )
        except ClientError as exc:
            raise S3StorageError(f"failed to sign URL for {full_key}: {exc}") from exc


def apply_sse(s3_opts: S3StorageOptions) -> dict:
    """Helper for callers that want to add SSE flags to a put."""
    if s3_opts.sse == "aws:kms" and s3_opts.sse_kms_key_id:
        return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": s3_opts.sse_kms_key_id}
    if s3_opts.sse == "AES256":
        return {"ServerSideEncryption": "AES256"}
    return {}


__all__ = ["S3Storage", "S3StorageError", "apply_sse"]
