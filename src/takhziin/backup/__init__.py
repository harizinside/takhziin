"""Backup engine package — Protocol, registry, orchestrator.

Engines are pluggable: each implements :class:`BackupEngine` and registers
itself in :data:`ENGINES`. The orchestrator (:func:`run_backup`) selects the
right engine based on ``Database.kind``, streams the dump into an
:class:`~takhziin.backup.archive.ArchiveWriter`, and uploads to the storage
adapter.

Engines are imported eagerly here so that side-effect registration happens at
package import time.
"""

from __future__ import annotations

import datetime as dt
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from takhziin import __version__
from takhziin.backup.archive import (
    ARCHIVE_SUFFIX,
    ArchiveMetadata,
    ArchiveWriter,
    utcnow_iso,
)
from takhziin.config import Settings
from takhziin.models import BackupRecord, BackupStatus, Database
from takhziin.state import State

if TYPE_CHECKING:
    from takhziin.storage import Storage


log = logging.getLogger(__name__)


# --- Protocol ---------------------------------------------------------------


@runtime_checkable
class BackupEngine(Protocol):
    """Interface every DB-specific engine implements."""

    name: str

    def test_connection(self, db: Database) -> None:
        """Probe the database; raise on failure."""
        ...

    def dump(self, db: Database, writer: ArchiveWriter) -> None:
        """Stream the dump into ``writer``. Engine writes the dump bytes only;
        ArchiveWriter manages metadata.json + tar packaging."""
        ...


@dataclass
class EngineRegistry:
    """Mutable registry mapping ``db.kind`` → BackupEngine."""

    engines: dict[str, BackupEngine]

    def for_kind(self, kind: str) -> BackupEngine:
        try:
            return self.engines[kind]
        except KeyError as exc:
            raise ValueError(
                f"no backup engine registered for kind={kind!r}; "
                f"available: {sorted(self.engines)}"
            ) from exc

    def register(self, kind: str, engine: BackupEngine) -> None:
        self.engines[kind] = engine


ENGINES = EngineRegistry(engines={})


def _register_engines() -> None:
    """Side-effect import to populate :data:`ENGINES`."""
    # import inside the function to avoid circular imports at module load
    from takhziin.backup.mariadb import MariadbEngine
    from takhziin.backup.mongo import MongoEngine
    from takhziin.backup.postgres import PostgresEngine

    ENGINES.register("mongo", MongoEngine())
    ENGINES.register("postgres", PostgresEngine())
    ENGINES.register("mariadb", MariadbEngine())


# --- Orchestrator ------------------------------------------------------------


class BackupError(RuntimeError):
    """Raised when a backup fails; carries structured context for the run record."""

    def __init__(self, message: str, *, engine_version: str = "") -> None:
        super().__init__(message)
        self.engine_version = engine_version


def _storage_key(db: Database, run_id: str) -> str:
    ts = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{db.id}/{ts}__{run_id}{ARCHIVE_SUFFIX}"


def _local_storage_path(settings: Settings, key: str) -> Path:
    return settings.backup_dir / key


@dataclass
class BackupResult:
    record: BackupRecord
    archive_path: Path | None = None  # set for local storage


def run_backup(
    db: Database,
    state: State,
    settings: Settings,
    *,
    storage_factory: Callable[[Database], Storage],
    triggered_by: str = "manual",
    run_id: str | None = None,
    notifier: Callable[[BackupRecord], None] | None = None,
) -> BackupResult:
    """Run one backup end-to-end and persist a :class:`BackupRecord`.

    Returns the persisted record plus, when storage_kind == "local", the
    archive path on disk. Failures are caught and turned into a ``FAILED``
    record (so the UI can show what happened); the original exception is then
    re-raised after persistence so the caller (CLI/scheduler) can decide what
    to do.
    """
    engine = ENGINES.for_kind(db.kind)
    started_at = utcnow_iso()
    record = BackupRecord(
        id=run_id or record_id(),  # type: ignore[arg-type]
        db_id=db.id,
        status=BackupStatus.RUNNING,
        started_at=started_at,
        storage_kind=db.storage_kind,
        location="",
        triggered_by=triggered_by,  # type: ignore[arg-type]
    )
    state.add_run(record)
    state.save()

    storage = storage_factory(db)
    key = _storage_key(db, record.id)
    # For local storage the archive file IS the storage; for S3 we still keep
    # a temp copy on disk so we can both upload and inspect after a crash.
    local_path = settings.backup_dir / key

    metadata = ArchiveMetadata(
        takhziin_version=__version__,
        db_kind=db.kind,
        db_id=db.id,
        db_name=db.name,
        started_at=started_at,
        engine_version="",
        extras={"hostname": socket.gethostname()},
    )

    error: str | None = None
    engine_version = ""
    try:
        engine.test_connection(db)
        with ArchiveWriter(local_path, metadata) as writer:
            engine.dump(db, writer)
        engine_version = writer.metadata.engine_version
        size = local_path.stat().st_size
        metadata_dict = writer.metadata.extras | {
            "schema_objects": writer.metadata.schema_objects,
        }
        # Upload to storage
        with open(local_path, "rb") as fp:
            storage.put(key, fp, metadata_dict)
        record.status = BackupStatus.SUCCESS
        record.finished_at = utcnow_iso()
        record.location = key
        record.size_bytes = size
        record.engine_version = engine_version
        record.schema_objects_count = writer.metadata.schema_objects_count
        record.metadata = metadata_dict
        state.update_run(record)
        state.save()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("backup failed for db=%s", db.name)
        record.status = BackupStatus.FAILED
        record.finished_at = utcnow_iso()
        record.error = error
        record.engine_version = engine_version
        state.update_run(record)
        state.save()
        if notifier is not None:
            try:
                notifier(record)
            except Exception:
                log.exception("notifier callback failed (ignored)")
        raise BackupError(error) from exc

    if notifier is not None:
        try:
            notifier(record)
        except Exception:
            log.exception("notifier callback failed (ignored)")

    return BackupResult(record=record, archive_path=local_path)


def record_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


__all__ = [
    "ENGINES",
    "BackupEngine",
    "BackupError",
    "BackupResult",
    "EngineRegistry",
    "record_id",
    "run_backup",
]


# Side-effect import: register engines on package load.
_register_engines()
