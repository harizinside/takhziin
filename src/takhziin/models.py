"""Pydantic models for takhziin configuration data.

These classes are the wire format for ``state.json`` and the database. They are
deliberately permissive about extra keys (ignored) so older files can be read.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_KIND_LITERAL = Literal["mongo", "postgres", "mariadb"]
_STORAGE_LITERAL = Literal["local", "s3"]


class DbKind(StrEnum):
    MONGO = "mongo"
    POSTGRES = "postgres"
    MARIADB = "mariadb"


class StorageKind(StrEnum):
    LOCAL = "local"
    S3 = "s3"


class ScheduleKind(StrEnum):
    CRON = "cron"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MongoDatabase(BaseModel):
    """MongoDB-specific connection options."""

    auth_source: str | None = None
    replica_set: str | None = None


class PostgresDatabase(BaseModel):
    """Postgres-specific connection options."""

    model_config = ConfigDict(protected_namespaces=())

    default_schema: str | None = None  # defaults to 'public'


class MariaDatabase(BaseModel):
    """MariaDB-specific connection options."""

    full_server_dump: bool = False  # if True: --all-databases
    ssl_disabled: bool = True  # explicit opt-out; production should set False


class S3StorageOptions(BaseModel):
    """S3-compatible storage adapter options."""

    endpoint_url: str | None = None  # set for R2/MinIO/Backblaze
    bucket: str
    region: str = "us-east-1"
    prefix: str = ""
    access_key_id: str  # encrypted at rest
    secret_access_key: str  # encrypted at rest
    sse: Literal["AES256", "aws:kms"] | None = "AES256"
    sse_kms_key_id: str | None = None
    signed_url_ttl: int = 300  # seconds; default 5 min


class LocalStorageOptions(BaseModel):
    """Local storage adapter — settings are inherited from ``Settings.backup_dir``."""

    pass


class _Base(BaseModel):
    """Common base — forbids unknown keys for safety, immutable after creation."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class Database(_Base):
    """A single database connection that takhziin knows how to back up."""

    id: str = Field(default_factory=_new_id)
    name: str
    kind: _KIND_LITERAL
    host: str = "127.0.0.1"
    port: int
    user: str
    password: str = ""  # encrypted at rest
    database: str  # logical name; for postgres this is also the dump target, mongo db name, etc.
    ssl: bool = False
    storage_kind: _STORAGE_LITERAL = "local"
    s3: S3StorageOptions | None = None
    mongo: MongoDatabase | None = None
    postgres: PostgresDatabase | None = None
    mariadb: MariaDatabase | None = None
    created_at: str = Field(default_factory=_utcnow_iso)

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in ("mongo", "postgres", "mariadb"):
            raise ValueError(f"unsupported kind: {v}")
        return v

    @field_validator("port")
    @classmethod
    def _check_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError(f"port out of range: {v}")
        return v

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v or not re.match(r"^[A-Za-z0-9_.@:\- ]+$", v):
            raise ValueError(
                "name may only contain letters, digits, dot, dash, underscore, colon, "
                "at sign, and spaces"
            )
        return v


class Schedule(_Base):
    """A cron schedule bound to one database."""

    id: str = Field(default_factory=_new_id)
    db_id: str
    cron: str  # 5-field crontab expression
    enabled: bool = True
    created_at: str = Field(default_factory=_utcnow_iso)


class BackupStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class BackupRecord(_Base):
    """One execution of a backup (manual or scheduled)."""

    id: str = Field(default_factory=_new_id)
    db_id: str
    status: BackupStatus
    started_at: str
    finished_at: str | None = None
    storage_kind: _STORAGE_LITERAL
    location: str  # key for storage adapter, or absolute local path
    size_bytes: int = 0
    schema_objects_count: int = 0
    engine_version: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    triggered_by: Literal["manual", "cron"] = "manual"


# --- Telegram notifier -------------------------------------------------------


_NOTIFIER_KIND_LITERAL = Literal["telegram"]
_NOTIFICATION_KIND = Literal["success", "failure", "start"]


class TelegramConfig(_Base):
    """Telegram notifier settings. Single global bot."""

    kind: _NOTIFIER_KIND_LITERAL = "telegram"
    bot_token: str  # encrypted at rest
    chat_id: str  # can be negative for groups
    events: list[Literal["success", "failure"]] = Field(default_factory=lambda: ["success", "failure"])
    proxy_url: str | None = None  # encrypted at rest

    @field_validator("chat_id")
    @classmethod
    def _check_chat_id(cls, v: str) -> str:
        if not re.match(r"^-?\d+$", v):
            raise ValueError("chat_id must be an integer (or negative for groups)")
        return v

    @field_validator("events")
    @classmethod
    def _check_events(cls, v: list[str]) -> list[str]:
        allowed = {"success", "failure"}
        bad = [e for e in v if e not in allowed]
        if bad:
            raise ValueError(f"unsupported events: {bad}")
        return list(v)


NotifierConfig = TelegramConfig  # alias until a second notifier is added


def telegram_default_message() -> str:
    """Default test notification body (MarkdownV2-safe)."""
    return "*🔔 Test notification*\nThis is a test message from takhziin\\."


__all__ = [
    "BackupRecord",
    "BackupStatus",
    "Database",
    "DbKind",
    "LocalStorageOptions",
    "MariaDatabase",
    "MongoDatabase",
    "NotifierConfig",
    "PostgresDatabase",
    "S3StorageOptions",
    "Schedule",
    "ScheduleKind",
    "StorageKind",
    "TelegramConfig",
    "telegram_default_message",
]
