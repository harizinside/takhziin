"""Storage adapters — Protocol, registry, factory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import IO, Protocol, runtime_checkable

from takhziin.config import Settings
from takhziin.models import Database


@runtime_checkable
class Storage(Protocol):
    """A place to put finished backup archives."""

    name: str

    def put(self, key: str, source: IO[bytes], metadata: dict | None = None) -> str:
        """Persist the bytes from ``source`` under ``key``. Returns the storage location."""
        ...

    def get(self, key: str) -> IO[bytes]:
        """Open ``key`` for reading. Caller closes."""
        ...

    def delete(self, key: str) -> None:
        ...

    def signed_url(self, key: str, ttl_seconds: int) -> str | None:
        """Return a URL valid for ``ttl_seconds`` (S3 only). Local returns None."""
        ...


@dataclass
class StorageRegistry:
    engines: dict[str, type[Storage]]

    def for_kind(self, kind: str) -> type[Storage]:
        try:
            return self.engines[kind]
        except KeyError as exc:
            raise ValueError(
                f"no storage adapter registered for kind={kind!r}; "
                f"available: {sorted(self.engines)}"
            ) from exc

    def register(self, kind: str, cls: type[Storage]) -> None:
        self.engines[kind] = cls


STORAGE = StorageRegistry(engines={})


def _register() -> None:
    # side-effect imports to populate the registry
    from takhziin.storage.local import LocalStorage
    from takhziin.storage.s3 import S3Storage

    STORAGE.register("local", LocalStorage)
    STORAGE.register("s3", S3Storage)


def make_storage(db: Database, settings: Settings) -> Storage:
    """Instantiate the storage adapter for a given database."""
    cls = STORAGE.for_kind(db.storage_kind)
    return cls.from_database(db, settings)


__all__ = [
    "STORAGE",
    "Storage",
    "StorageRegistry",
    "make_storage",
]


_register()
