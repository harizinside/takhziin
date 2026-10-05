"""Archive writer/reader round-trip test."""

from __future__ import annotations

from pathlib import Path

from takhziin.backup.archive import (
    ArchiveMetadata,
    ArchiveReader,
    ArchiveWriter,
)


def test_archive_round_trip(tmp_path: Path) -> None:
    meta = ArchiveMetadata(
        takhziin_version="0.1.0",
        db_kind="mariadb",
        db_id="abc",
        db_name="orders",
        started_at="2024-01-01T00:00:00+00:00",
        engine_version="mariadb-dump 11.0",
        schema_objects={"views": 1, "routines": 2, "triggers": 2, "events": 1},
        schema_objects_count=7,
        extras={"hostname": "db1.example.com"},
    )
    archive_path = tmp_path / "test.tar.gz"
    dump = b"hello world this is the dump"
    with ArchiveWriter(archive_path, meta) as w:
        w.write(dump)
        w.write(b" and more")

    assert archive_path.exists()
    assert oct(archive_path.stat().st_mode & 0o777) == "0o600"

    with ArchiveReader(archive_path) as r:
        meta2 = r.read_metadata()
        assert meta2.db_kind == "mariadb"
        assert meta2.db_name == "orders"
        assert meta2.extras == {"hostname": "db1.example.com"}
        dump_out = r.read_dump()
        assert dump_out == dump + b" and more"
        # finished_at should be populated by the writer
        assert meta2.finished_at != ""


def test_archive_writer_cleans_tmp_on_exception(tmp_path: Path) -> None:
    meta = ArchiveMetadata(
        takhziin_version="0.1.0",
        db_kind="mongo",
        db_id="x",
        db_name="y",
        started_at="2024-01-01T00:00:00+00:00",
    )
    archive_path = tmp_path / "boom.tar.gz"

    class _Boom(Exception):
        pass

    try:
        with ArchiveWriter(archive_path, meta) as w:
            w.write(b"partial dump")
            raise _Boom("nope")
    except _Boom:
        pass
    # the .tmp file should be cleaned up; the final path must not exist
    assert not archive_path.exists()
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []