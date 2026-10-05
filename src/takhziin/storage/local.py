"""Local-filesystem storage adapter.

Stores archives under ``$backup_dir/<key>`` with permissions 0600. Atomic write
via ``.tmp`` + ``os.replace``. The ``key`` is the same path scheme used by the
S3 adapter (``<db_id>/<UTC_ISO8601>__<uuid>.tar.gz``) so swapping adapters
later doesn't require data migration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from takhziin.config import Settings
from takhziin.models import Database


@dataclass
class LocalStorage:
    """Storage adapter that writes to ``settings.backup_dir``."""

    root: Path
    name: str = "local"

    @classmethod
    def from_database(cls, db: Database, settings: Settings) -> LocalStorage:
        return cls(root=settings.backup_dir)

    def _resolve(self, key: str) -> Path:
        # Disallow path traversal; key must be relative and clean.
        if not key or key.startswith("/") or ".." in Path(key).parts:
            raise ValueError(f"invalid storage key: {key!r}")
        return self.root / key

    def put(self, key: str, source: IO[bytes], metadata: dict | None = None) -> str:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        total = 0
        try:
            with open(tmp, "wb") as out:
                while chunk := source.read(1024 * 1024):
                    out.write(chunk)
                    total += len(chunk)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
            os.chmod(target, 0o600)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return str(target)

    def get(self, key: str) -> IO[bytes]:
        path = self._resolve(key)
        if not path.exists():
            raise FileNotFoundError(f"storage key not found: {path}")
        return open(path, "rb")

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        path.unlink(missing_ok=True)

    def signed_url(self, key: str, ttl_seconds: int) -> str | None:
        """Local storage has no signed URLs; the UI serves files directly."""
        return None


__all__ = ["LocalStorage"]
