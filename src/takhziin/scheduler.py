"""APScheduler wrapper — cron-driven backup jobs, persisted run records.

The scheduler runs as a background thread inside the takhziin process. Jobs are
rebuilt from ``state.schedules`` on startup. Manual ``run_backup`` calls (CLI
``takhziin backup run``) share the same orchestrator and notifier hook as the
cron jobs.

Concurrency: a single :class:`threading.Lock` (also held across CLI + UI) keeps
``state.json`` writes serial; ``run_backup`` itself is one-at-a-time because
the scheduler thread is single-threaded.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from takhziin.backup import BackupResult, run_backup
from takhziin.config import Settings
from takhziin.models import BackupRecord, Database, Schedule
from takhziin.notifiers.base import Notifier, build_default_notifier
from takhziin.state import State
from takhziin.storage import make_storage

log = logging.getLogger(__name__)

# lock serialising state.json reads/writes across processes/handlers
STATE_WRITE_LOCK = threading.Lock()


class TakhziinScheduler:
    """Background scheduler with notifier hook."""

    def __init__(
        self,
        state: State,
        settings: Settings,
        *,
        notifier: Notifier | None = None,
    ) -> None:
        self.state = state
        self.settings = settings
        self._bg = BackgroundScheduler(daemon=True)
        self._lock = STATE_WRITE_LOCK
        self._notifier = notifier

    # --- scheduler lifecycle ------------------------------------------------

    def start(self) -> None:
        self._bg.start()
        for sched in self.state.schedules:
            if sched.enabled:
                self._add_job(sched)

    def shutdown(self, *, wait: bool = False) -> None:
        try:
            self._bg.shutdown(wait=wait)
        except Exception:
            log.exception("scheduler shutdown raised")

    def reload(self) -> None:
        """Rebuild jobs from current ``state.schedules``."""
        for job in list(self._bg.get_jobs()):
            job.remove()
        for sched in self.state.schedules:
            if sched.enabled:
                self._add_job(sched)

    def _add_job(self, sched: Schedule) -> None:
        try:
            trigger = CronTrigger.from_crontab(sched.cron)
        except ValueError as exc:
            log.error("schedule %s has invalid cron %r: %s", sched.id, sched.cron, exc)
            return
        self._bg.add_job(
            self._triggered_run,
            trigger=trigger,
            id=sched.id,
            args=[sched.db_id, "cron"],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # --- run a backup ----------------------------------------------------------

    def run_now(self, db_id: str, triggered_by: str = "manual") -> BackupResult:
        """Execute one backup inline (used by CLI + UI)."""
        return self._do_run(db_id, triggered_by)

    def _triggered_run(self, db_id: str, triggered_by: str) -> None:
        # APScheduler runs this on its own thread; exceptions there only log.
        try:
            self._do_run(db_id, triggered_by)
        except Exception:
            log.exception("scheduled run failed for db_id=%s", db_id)

    def _do_run(self, db_id: str, triggered_by: str) -> BackupResult:
        with self._lock:
            db = self.state.find_database(db_id)
            if db is None:
                raise ValueError(f"database not found: {db_id}")
            notifier = self._notifier_callback()
            return run_backup(
                db,
                self.state,
                self.settings,
                storage_factory=lambda d: make_storage(d, self.settings),
                triggered_by=triggered_by,
                notifier=notifier,
            )

    def _notifier_callback(self) -> Callable[[BackupRecord], None] | None:
        if self._notifier is None or self.state.notifier is None:
            return None
        cfg = self.state.notifier
        notifier = self._notifier

        def _cb(record: BackupRecord) -> None:
            # best-effort thread pool — must never fail the backup
            try:
                if record.error:
                    notifier.send_failure(record, db=record_db(self, record), config=cfg)
                else:
                    notifier.send_success(record, db=record_db(self, record), config=cfg)
            except Exception:
                log.exception("notifier send failed (ignored)")

        return _cb


def record_db(scheduler: TakhziinScheduler, record: BackupRecord) -> Database:
    db = scheduler.state.find_database(record.db_id)
    if db is None:
        raise ValueError(f"unknown db_id on run: {record.db_id}")
    return db


def build_scheduler(
    state: State,
    settings: Settings,
) -> TakhziinScheduler:
    """Construct a scheduler wired up with the default notifier (Telegram)."""
    notifier = build_default_notifier()
    return TakhziinScheduler(state, settings, notifier=notifier)


__all__ = [
    "STATE_WRITE_LOCK",
    "TakhziinScheduler",
    "build_scheduler",
]
