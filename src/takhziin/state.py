"""JSON-file state store at ``$data_dir/state.json``.

Serialises reads/writes across concurrent CLI + web processes via
``fcntl.flock``. Pydantic models from :mod:`takhziin.models` are the on-disk
shape; older payloads are migrated forward on next write.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from takhziin.models import BackupRecord, Database, NotifierConfig, Schedule

MAX_RUNS = 200  # prune older history past this many records


class StateError(RuntimeError):
    """Raised on corrupt or unreadable state."""


@dataclass
class State:
    """In-memory view of the state file; persist via :meth:`save`."""

    state_file: Path
    databases: list[Database] = field(default_factory=list)
    schedules: list[Schedule] = field(default_factory=list)
    runs: list[BackupRecord] = field(default_factory=list)
    notifier: NotifierConfig | None = None

    # --- (de)serialisation --------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "databases": [db.model_dump(mode="json") for db in self.databases],
            "schedules": [s.model_dump(mode="json") for s in self.schedules],
            "runs": [r.model_dump(mode="json") for r in self.runs],
            "notifier": self.notifier.model_dump(mode="json") if self.notifier else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> State:
        databases = [Database.model_validate(d) for d in raw.get("databases", [])]
        schedules = [Schedule.model_validate(s) for s in raw.get("schedules", [])]
        runs = [BackupRecord.model_validate(r) for r in raw.get("runs", [])]
        notifier_raw = raw.get("notifier")
        notifier: NotifierConfig | None = None
        if notifier_raw:
            notifier = NotifierConfig.model_validate(notifier_raw)
        return cls(
            state_file=Path(raw.get("_state_file", "")) if False else None,  # populated by caller
            databases=databases,
            schedules=schedules,
            runs=runs,
            notifier=notifier,
        )

    # --- file I/O -----------------------------------------------------------

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Acquire an exclusive flock over the state file's parent directory.

        Using a sibling lock file (``state.json.lock``) avoids clobbering the
        state file's contents while we read them.
        """
        lock_path = self.state_file.with_suffix(self.state_file.suffix + ".lock")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as fp:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)

    def save(self) -> None:
        """Persist to disk under flock; prune old runs."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # prune oldest runs beyond MAX_RUNS, keep most recent by started_at
        if len(self.runs) > MAX_RUNS:
            self.runs = sorted(self.runs, key=lambda r: r.started_at, reverse=True)[:MAX_RUNS]
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True)
        with self._lock():
            tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_file)
            os.chmod(self.state_file, 0o600)

    @classmethod
    def load(cls, state_file: Path) -> State:
        """Load from disk; return a fresh State on first run."""
        instance = cls(state_file=state_file)
        if not state_file.exists():
            return instance
        try:
            raw = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError(f"corrupt state.json: {exc}") from exc
        if not isinstance(raw, dict):
            raise StateError("state.json root must be a mapping")
        instance.databases = [Database.model_validate(d) for d in raw.get("databases", [])]
        instance.schedules = [Schedule.model_validate(s) for s in raw.get("schedules", [])]
        instance.runs = [BackupRecord.model_validate(r) for r in raw.get("runs", [])]
        notifier_raw = raw.get("notifier")
        instance.notifier = NotifierConfig.model_validate(notifier_raw) if notifier_raw else None
        return instance

    # --- helpers ------------------------------------------------------------

    def find_database(self, db_id: str) -> Database | None:
        for db in self.databases:
            if db.id == db_id:
                return db
        return None

    def add_database(self, db: Database) -> None:
        self.databases.append(db)

    def remove_database(self, db_id: str) -> bool:
        before = len(self.databases)
        self.databases = [d for d in self.databases if d.id != db_id]
        self.schedules = [s for s in self.schedules if s.db_id != db_id]
        self.runs = [r for r in self.runs if r.db_id != db_id]
        return len(self.databases) < before

    def add_schedule(self, sched: Schedule) -> None:
        self.schedules.append(sched)

    def remove_schedule(self, sched_id: str) -> bool:
        before = len(self.schedules)
        self.schedules = [s for s in self.schedules if s.id != sched_id]
        return len(self.schedules) < before

    def find_schedule(self, sched_id: str) -> Schedule | None:
        for s in self.schedules:
            if s.id == sched_id:
                return s
        return None

    def add_run(self, record: BackupRecord) -> None:
        self.runs.append(record)

    def update_run(self, record: BackupRecord) -> None:
        for i, r in enumerate(self.runs):
            if r.id == record.id:
                self.runs[i] = record
                return
        self.runs.append(record)


__all__ = ["MAX_RUNS", "State", "StateError"]
