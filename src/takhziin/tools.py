"""Locate native dump tool binaries on disk.

Resolution order:
  1. Explicit override via environment variable ``TAKHZIIN_BIN_DIR`` (the
     Settings class already wires this into ``bin_dir``).
  2. The configured bin_dir.
  3. ``PATH`` lookup via :mod:`shutil.which`.

Missing binaries raise :class:`MissingDependencyError` so the operator sees a
clear install hint.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# Maps takhziin's internal names → list of binary filenames we try in PATH.
KNOWN_BINARIES: dict[str, list[str]] = {
    "mongodump": ["mongodump"],
    "mongorestore": ["mongorestore"],
    "pg_dump": ["pg_dump"],
    "pg_restore": ["pg_restore"],
    "mariadb_dump": ["mariadb-dump", "mysqldump"],
    "mariadb": ["mariadb", "mysql"],
    "mongosh": ["mongosh"],
}


@dataclass
class ResolvedBinary:
    name: str
    path: Path


class MissingDependencyError(RuntimeError):
    """Raised when a required dump tool cannot be found."""


def resolve_binary(name: str, bin_dir: Path | None = None) -> ResolvedBinary:
    """Locate a native binary by takhziin name. See module docstring."""
    candidates = KNOWN_BINARIES.get(name)
    if not candidates:
        raise MissingDependencyError(f"unknown tool alias: {name!r}")

    # 1. Bin dir passed explicitly
    if bin_dir is not None:
        for c in candidates:
            p = bin_dir / c
            if p.exists() and os.access(p, os.X_OK):
                return ResolvedBinary(name=name, path=p)

    # 2. PATH lookup
    for c in candidates:
        p = shutil.which(c)
        if p:
            return ResolvedBinary(name=name, path=Path(p))

    install_hint = {
        "mongodump": "brew install mongodb-database-tools   |   apt-get install mongodb-database-tools",
        "mongorestore": "brew install mongodb-database-tools   |   apt-get install mongodb-database-tools",
        "pg_dump": "brew install libpq   |   apt-get install postgresql-client",
        "pg_restore": "brew install libpq   |   apt-get install postgresql-client",
        "mariadb_dump": "brew install mariadb-client   |   apt-get install mariadb-client",
        "mariadb": "brew install mariadb-client   |   apt-get install mariadb-client",
        "mongosh": "brew install mongosh   |   apt-get install mongosh",
    }.get(name, f"install the appropriate package for {name}")

    raise MissingDependencyError(
        f"required binary not found: {candidates[0]!r} (or one of its aliases). "
        f"Hint: {install_hint}"
    )


__all__ = ["KNOWN_BINARIES", "MissingDependencyError", "ResolvedBinary", "resolve_binary"]
