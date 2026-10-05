"""Configuration loading and path resolution for takhziin.

Resolution precedence (highest first):
  1. Environment variables (TAKHZIIN_*).
  2. YAML file at ``$config_dir/config.yaml``.
  3. Built-in defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pyyaml is optional at runtime; we only need it when a config file exists
    import yaml as _yaml
except ImportError:  # pragma: no cover - environment without PyYAML
    _yaml = None


CONFIG_ENV = "TAKHZIIN_CONFIG_DIR"
DATA_ENV = "TAKHZIIN_DATA_DIR"
BIND_HOST_ENV = "TAKHZIIN_BIND_HOST"
BIND_PORT_ENV = "TAKHZIIN_BIND_PORT"
BIN_DIR_ENV = "TAKHZIIN_BIN_DIR"


@dataclass
class Settings:
    """Resolved runtime settings."""

    config_dir: Path
    data_dir: Path
    backup_dir: Path
    bin_dir: Path
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def master_key_file(self) -> Path:
        return self.config_dir / "master.key"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    def ensure_dirs(self) -> None:
        """Create config + data dirs; idempotent."""
        for d in (self.config_dir, self.data_dir, self.backup_dir, self.bin_dir):
            d.mkdir(parents=True, exist_ok=True)


def _coerce_port(raw: str | int) -> int:
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid port value: {raw!r}") from exc


def _default_data_dir() -> Path:
    return Path(os.environ.get(DATA_ENV) or Path.home() / ".local" / "share" / "takhziin")


def _default_config_dir() -> Path:
    if env := os.environ.get(CONFIG_ENV):
        return Path(env)
    return Path.home() / ".config" / "takhziin"


def _default_bin_dir() -> Path:
    if env := os.environ.get(BIN_DIR_ENV):
        return Path(env)
    # Container default — Debian trixie ships tools into /usr/bin via apt.
    if Path("/usr/bin/mongodump").exists():
        return Path("/usr/bin")
    return Path.home() / ".local" / "share" / "takhziin" / "bin"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if _yaml is None:
        raise RuntimeError(
            f"PyYAML is required to read {path}; install 'pyyaml' or remove the file"
        )
    data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must be a mapping, got {type(data).__name__}")
    return data


def load_settings(
    config_dir: Path | None = None,
    data_dir: Path | None = None,
    bin_dir: Path | None = None,
    *,
    bind_host: str | None = None,
    bind_port: int | None = None,
    skip_yaml: bool = False,
) -> Settings:
    """Resolve settings with env > yaml > defaults precedence."""
    cfg_dir = config_dir or _default_config_dir()
    dat_dir = data_dir or _default_data_dir()
    bdir = bin_dir or _default_bin_dir()

    host = bind_host or os.environ.get(BIND_HOST_ENV, "127.0.0.1")
    port_raw = bind_port if bind_port is not None else os.environ.get(BIND_PORT_ENV)
    port = _coerce_port(port_raw) if port_raw is not None else 8765

    if not skip_yaml:
        yaml_data = _load_yaml(cfg_dir / "config.yaml")
        host = yaml_data.get("bind_host", host)
        if "bind_port" in yaml_data:
            port = _coerce_port(yaml_data["bind_port"])
        if "data_dir" in yaml_data:
            dat_dir = Path(yaml_data["data_dir"])
        if "backup_dir" in yaml_data:
            backup_dir = Path(yaml_data["backup_dir"])
        else:
            backup_dir = dat_dir / "backups"
        if "bin_dir" in yaml_data:
            bdir = Path(yaml_data["bin_dir"])
    else:
        backup_dir = dat_dir / "backups"

    return Settings(
        config_dir=cfg_dir,
        data_dir=dat_dir,
        backup_dir=backup_dir,
        bin_dir=bdir,
        bind_host=str(host),
        bind_port=port,
    )


__all__ = [
    "BIND_HOST_ENV",
    "BIND_PORT_ENV",
    "BIN_DIR_ENV",
    "CONFIG_ENV",
    "DATA_ENV",
    "Settings",
    "load_settings",
]
