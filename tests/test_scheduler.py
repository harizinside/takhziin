"""Scheduler unit tests — fake engine, real APScheduler."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger

from takhziin.config import Settings
from takhziin.models import BackupStatus, Database, Schedule
from takhziin.scheduler import TakhziinScheduler
from takhziin.state import State


def test_scheduler_picks_up_schedule(tmp_path: Path, env_settings: Settings) -> None:
    """A cron with min-hrs is valid; the scheduler adds it without firing."""
    state = State.load(env_settings.state_file)
    db = Database(
        name="d", kind="mongo", host="h", port=27017, user="u", password="p", database="d"
    )
    state.add_database(db)
    state.add_schedule(Schedule(db_id=db.id, cron="0 0 * * *"))

    sched = TakhziinScheduler(state, env_settings)
    sched.start()
    try:
        # job is registered
        jobs = sched._bg.get_jobs()
        assert any(j.id == state.schedules[0].id for j in jobs)
    finally:
        sched.shutdown(wait=False)


def test_invalid_cron_is_ignored(tmp_path: Path, env_settings: Settings) -> None:
    state = State.load(env_settings.state_file)
    db = Database(
        name="d", kind="mongo", host="h", port=27017, user="u", password="p", database="d"
    )
    state.add_database(db)
    state.add_schedule(Schedule(db_id=db.id, cron="not a cron"))

    sched = TakhziinScheduler(state, env_settings)
    sched.start()
    try:
        assert sched._bg.get_jobs() == []
    finally:
        sched.shutdown(wait=False)


def test_reload_removes_orphan_jobs(tmp_path: Path, env_settings: Settings) -> None:
    state = State.load(env_settings.state_file)
    db = Database(
        name="d", kind="mongo", host="h", port=27017, user="u", password="p", database="d"
    )
    state.add_database(db)
    s1 = Schedule(db_id=db.id, cron="0 0 * * *")
    state.add_schedule(s1)
    sched = TakhziinScheduler(state, env_settings)
    sched.start()
    try:
        # remove the schedule from state
        state.schedules.remove(s1)
        sched.reload()
    finally:
        sched.shutdown(wait=False)
    assert sched._bg.get_jobs() == []


def test_cron_trigger_round_trip() -> None:
    """Smoke: a cron expression accepted by APScheduler is what the CLI will store."""
    CronTrigger.from_crontab("*/5 * * * *")