"""Notifier base — Protocol + default factory."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from takhziin.models import BackupRecord, Database, NotifierConfig


@runtime_checkable
class Notifier(Protocol):
    """A channel that emits backup lifecycle messages."""

    name: str

    def send_success(
        self,
        record: BackupRecord,
        *,
        db: Database,
        config: NotifierConfig,
    ) -> None: ...

    def send_failure(
        self,
        record: BackupRecord,
        *,
        db: Database,
        config: NotifierConfig,
    ) -> None: ...

    def send_message(
        self,
        text: str,
        *,
        config: NotifierConfig,
    ) -> None: ...


def build_default_notifier() -> Notifier | None:
    """Construct the v1 default notifier (Telegram)."""
    from takhziin.notifiers.telegram import TelegramNotifier

    try:
        return TelegramNotifier()
    except Exception:
        return None
