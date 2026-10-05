"""MongoDB backup engine.

Connects via pymongo for the connectivity probe + version detection, and uses
``mongodump --archive --gzip`` (subprocess) for the actual dump. We deliberately
avoid pure-Python BSON walking because mongodump streams to a gzipped archive
natively and is much faster on large collections.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote_plus

from pymongo import MongoClient
from pymongo.errors import ConfigurationError, ConnectionFailure, PyMongoError

from takhziin.backup.archive import ArchiveWriter
from takhziin.models import Database
from takhziin.tools import resolve_binary


def _build_uri(db: Database) -> str:
    """Build a mongodb:// URI from a :class:`Database` model."""
    auth = ""
    if db.user:
        user = quote_plus(db.user)
        pwd = quote_plus(db.password or "")
        auth = f"{user}:{pwd}@"
    scheme = "mongodb"
    options = []
    md = db.mongo
    if md and md.auth_source:
        options.append(f"authSource={quote_plus(md.auth_source)}")
    if md and md.replica_set:
        options.append(f"replicaSet={quote_plus(md.replica_set)}")
    if db.ssl:
        options.append("ssl=true")
    opt_str = ("?" + "&".join(options)) if options else ""
    return f"{scheme}://{auth}{db.host}:{db.port}/{opt_str}"


class MongoEngine:
    name = "mongo"

    def test_connection(self, db: Database) -> None:
        uri = _build_uri(db)
        client = MongoClient(uri, serverSelectionTimeoutMS=10_000)
        try:
            client.admin.command("ping")
        except (ConnectionFailure, ConfigurationError, PyMongoError) as exc:
            raise RuntimeError(f"mongo connectivity check failed: {exc}") from exc
        finally:
            client.close()

    def dump(self, db: Database, writer: ArchiveWriter) -> None:
        uri = _build_uri(db)
        binary = resolve_binary("mongodump")
        # mongodump --archive writes to a single file; we stream it through a
        # temp file because the subprocess output is the page boundary.
        # For an archive dump, mongodump requires --archive=<path>. We
        # therefore pipe stdout only when --archive=- is supported (modern
        # versions). mongodump 100+ supports `--archive=-` (stdout) but not
        # when combined with --gzip; with --gzip we get a tar.gz of BSON, so
        # we instead write to a temp file then read it back into the writer.
        with tempfile.TemporaryDirectory() as td:
            archive_path = Path(td) / "dump.archive"
            cmd = [
                str(binary.path),
                f"--uri={uri}",
                "--gzip",
                f"--archive={archive_path}",
                f"--db={db.database}",
            ]
            # Drop unknown args (defensive): mongodump will fail if we send garbage
            proc = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                stderr = proc.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"mongodump failed (exit {proc.returncode}): {stderr}"
                )
            writer.metadata.engine_version = _version_string(binary.path)
            # stream the archive into the writer
            with open(archive_path, "rb") as src:
                while chunk := src.read(1024 * 1024):
                    writer.write(chunk)


def _version_string(binary_path: Path) -> str:
    try:
        proc = subprocess.run(
            [str(binary_path), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if proc.returncode == 0:
            first = (proc.stdout or "").strip().splitlines()[0] if proc.stdout else ""
            return first
    except (OSError, subprocess.TimeoutExpired):
        pass
    return binary_path.name


__all__ = ["MongoEngine"]
