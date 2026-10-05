"""Local storage round-trip tests."""

from __future__ import annotations

from pathlib import Path

from takhziin.config import Settings
from takhziin.storage.local import LocalStorage


def test_local_put_get_delete(tmp_path: Path) -> None:
    settings = Settings(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "data" / "backups",
        bin_dir=tmp_path / "bin",
    )
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    storage = LocalStorage.from_database(db=None, settings=settings)  # type: ignore[arg-type]
    key = "abc/20240101T000000Z__run.tar.gz"
    (tmp_path / "in.bin").write_bytes(b"hello world")

    with open(tmp_path / "in.bin", "rb") as src:
        location = storage.put(key, src)
    assert location.endswith(key)
    p = Path(location)
    assert p.exists()
    assert oct(p.stat().st_mode & 0o777) == "0o600"

    with storage.get(key) as fp:
        assert fp.read() == b"hello world"

    storage.delete(key)
    assert not p.exists()


def test_local_storage_rejects_traversal(tmp_path: Path) -> None:
    settings = Settings(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "data" / "backups",
        bin_dir=tmp_path / "bin",
    )
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    storage = LocalStorage.from_database(db=None, settings=settings)  # type: ignore[arg-type]
    with open(tmp_path / "in.bin", "wb") as src:
        src.write(b"x")
    with open(tmp_path / "in.bin", "rb") as src:
        # should refuse any key with ..
        try:
            storage.put("../escape.tar.gz", src)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for path traversal")