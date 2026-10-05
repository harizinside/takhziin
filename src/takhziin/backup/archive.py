"""Archive format: gzip-wrapped tar containing ``metadata.json`` + ``dump.bin``.

The metadata.json schema is the wire contract between engines and consumers:
restore tooling parses it to verify completeness without unpacking the dump.

Fields (ArchiveMetadata dataclass):
  takhziin_version: str
  db_kind: "mongo" | "postgres" | "mariadb"
  db_id: str
  db_name: str (user-visible name)
  started_at: ISO 8601 UTC
  finished_at: ISO 8601 UTC
  schema_objects_count: int
  engine_version: str (e.g. "mongodump 100.9.5", "pg_dump 16.1", "mariadb-dump 11.0")
  schema_objects (optional, mariadb only): {views, routines, triggers, events}
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

METADATA_FILENAME = "metadata.json"
DUMP_FILENAME = "dump.bin"
ARCHIVE_SUFFIX = ".tar.gz"


def utcnow_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


@dataclass
class ArchiveMetadata:
    """Schema for the ``metadata.json`` file inside each archive."""

    takhziin_version: str
    db_kind: str
    db_id: str
    db_name: str
    started_at: str
    finished_at: str = ""
    schema_objects_count: int = 0
    engine_version: str = ""
    schema_objects: dict[str, int] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        body = {
            "takhziin_version": self.takhziin_version,
            "db_kind": self.db_kind,
            "db_id": self.db_id,
            "db_name": self.db_name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "schema_objects_count": self.schema_objects_count,
            "engine_version": self.engine_version,
            "schema_objects": self.schema_objects,
        }
        body.update(self.extras)
        return json.dumps(body, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> ArchiveMetadata:
        d = json.loads(raw)
        known = set(cls.__dataclass_fields__) - {"extras"}
        extras = {k: v for k, v in d.items() if k not in known}
        clean = {k: v for k, v in d.items() if k in known}
        return cls(extras=extras, **clean)


class ArchiveWriter:
    """Streaming-friendly archive writer.

    Engines call ``write(...)`` to push raw dump bytes; the writer buffers
    them and on context-exit bundles ``metadata.json`` + ``dump.bin`` into a
    gzip-compressed tar archive at ``self.path``. Atomicity: writes go to a
    sibling .tmp and are renamed on success.
    """

    def __init__(
        self,
        path: Path,
        metadata: ArchiveMetadata,
        *,
        compresslevel: int = 5,
    ) -> None:
        self.path = Path(path)
        self.metadata = metadata
        self.compresslevel = compresslevel
        self._dump = io.BytesIO()
        self._opened = False

    def __enter__(self) -> ArchiveWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._opened = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._opened:
            return
        self._opened = False
        if exc is not None:
            # caller signaled an error — leave no archive behind
            try:
                self._dump.close()
            finally:
                return

        self.metadata.finished_at = utcnow_iso()
        dump_bytes = self._dump.getvalue()
        if not self.metadata.schema_objects_count:
            self.metadata.schema_objects_count = len(dump_bytes)
        meta_bytes = self.metadata.to_json().encode("utf-8")
        now = int(dt.datetime.now(dt.UTC).timestamp())

        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with gzip.GzipFile(
                fileobj=open(tmp_path, "wb"),
                mode="wb",
                compresslevel=self.compresslevel,
            ) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
                for name, data, mode in (
                    (DUMP_FILENAME, dump_bytes, 0o600),
                    (METADATA_FILENAME, meta_bytes, 0o600),
                ):
                    info = tarfile.TarInfo(name=name)
                    info.size = len(data)
                    info.mtime = now
                    info.mode = mode
                    tar.addfile(info, io.BytesIO(data))
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            self._dump.close()

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if not self._opened:
            raise RuntimeError("ArchiveWriter must be used as a context manager")
        if not data:
            return 0
        self._dump.write(bytes(data))
        return len(data)

    @property
    def bytes_written(self) -> int:
        return self._dump.tell()


class ArchiveReader:
    """Read metadata + dump bytes back out of an archive."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._tar: tarfile.TarFile | None = None

    def __enter__(self) -> ArchiveReader:
        self._tar = tarfile.open(self.path, mode="r:gz")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tar is not None:
            self._tar.close()
            self._tar = None

    def read_metadata(self) -> ArchiveMetadata:
        if self._tar is None:
            raise RuntimeError("ArchiveReader must be used as a context manager")
        try:
            member = self._tar.getmember(METADATA_FILENAME)
        except KeyError as exc:
            raise ArchiveError(f"archive {self.path} missing metadata.json") from exc
        fp = self._tar.extractfile(member)
        if fp is None:
            raise ArchiveError(f"archive {self.path} has unreadable metadata.json")
        return ArchiveMetadata.from_json(fp.read().decode("utf-8"))

    def read_dump(self) -> bytes:
        if self._tar is None:
            raise RuntimeError("ArchiveReader must be used as a context manager")
        try:
            member = self._tar.getmember(DUMP_FILENAME)
        except KeyError as exc:
            raise ArchiveError(f"archive {self.path} missing dump.bin") from exc
        fp = self._tar.extractfile(member)
        if fp is None:
            raise ArchiveError(f"archive {self.path} has unreadable dump.bin")
        return fp.read()

    def dump_to(self, dest: Path | BinaryIO) -> int:
        if self._tar is None:
            raise RuntimeError("ArchiveReader must be used as a context manager")
        member = self._tar.getmember(DUMP_FILENAME)
        src = self._tar.extractfile(member)
        if src is None:
            raise ArchiveError(f"archive {self.path} has unreadable dump.bin")
        total = 0
        if isinstance(dest, Path):
            with open(dest, "wb") as out:
                while chunk := src.read(1024 * 1024):
                    out.write(chunk)
                    total += len(chunk)
        else:
            while chunk := src.read(1024 * 1024):
                dest.write(chunk)
                total += len(chunk)
        return total


class ArchiveError(RuntimeError):
    """Raised when an archive cannot be parsed."""


__all__ = [
    "ARCHIVE_SUFFIX",
    "DUMP_FILENAME",
    "METADATA_FILENAME",
    "ArchiveError",
    "ArchiveMetadata",
    "ArchiveReader",
    "ArchiveWriter",
    "utcnow_iso",
]
