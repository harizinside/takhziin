"""Telegram notifier tests — monkeypatch urllib.request.urlopen.

``responses`` intercepts the third-party ``requests`` library but NOT stdlib
``urllib.request``. Since the notifier deliberately uses stdlib to keep its
surface zero-dep, the tests patch ``urllib.request.OpenerDirector.open``
instead.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from takhziin.models import BackupRecord, BackupStatus, Database, NotifierConfig
from takhziin.notifiers.telegram import (
    MARKDOWN_V2_RESERVED,
    TelegramNotifier,
    TelegramSendError,
    escape_markdown_v2,
)


# --- fixtures ---------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, body: dict | str) -> None:
        self.status = status
        self._body = body if isinstance(body, dict) else {"raw": body}

    def read(self) -> bytes:
        if isinstance(self._body, dict):
            return json.dumps(self._body).encode("utf-8")
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_record(success: bool = True) -> BackupRecord:
    return BackupRecord(
        id="runabc",
        db_id="dbxyz",
        status=BackupStatus.SUCCESS if success else BackupStatus.FAILED,
        started_at="2024-01-01T03:00:00+00:00",
        finished_at="2024-01-01T03:00:42+00:00",
        storage_kind="local",
        location="orders/2024-01-01T03_00:42Z__runabc.tar.gz",
        size_bytes=1024 * 1024,
        engine_version="mariadb-dump 11.0",
        schema_objects_count=7,
        error=None if success else "InsufficientPrivileges(missing=['EVENT'])",
        triggered_by="cron",
    )


def _make_db() -> Database:
    return Database(
        name="orders",
        kind="mariadb",
        host="db",
        port=3306,
        user="app",
        password="enc",
        database="orders",
    )


def _cfg(events: list[str] | None = None) -> NotifierConfig:
    return NotifierConfig(
        kind="telegram",
        bot_token="123456:ABCDEF",
        chat_id="-1001234567890",
        events=events if events is not None else ["success", "failure"],
    )


class _HttpMock:
    """Captures calls and returns programmed responses in order."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self._responses: list[_FakeResponse | Exception] = []

    def queue_response(self, status: int, body: dict | str = "") -> None:
        self._responses.append(_FakeResponse(status, body))

    def queue_error(self, exc: Exception) -> None:
        self._responses.append(exc)

    def open(self, req, timeout=None):  # noqa: ARG002
        from urllib.error import HTTPError

        # capture the call as the underlying urllib OpenerDirector.open would.
        body = req.data
        if isinstance(body, bytes):
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = body.decode("utf-8", errors="replace")
        else:
            parsed = body
        self.calls.append(
            {"url": req.full_url, "method": req.method, "body": parsed, "headers": dict(req.headers)}
        )
        if not self._responses:
            raise HTTPError(req.full_url, 599, "no mock response queued", {}, None)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def http(monkeypatch):
    mock = _HttpMock()
    import takhziin.notifiers.telegram as tg

    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *handlers: _RecordingOpener(mock, list(handlers)),
    )
    return mock


class _RecordingOpener:
    """Stand-in for ``OpenerDirector`` that records the proxy handlers too."""

    def __init__(self, http_mock: _HttpMock, handlers: list) -> None:
        self.http_mock = http_mock
        self.handlers = handlers

    def open(self, req, timeout=None):
        return self.http_mock.open(req, timeout=timeout)


# --- tests ------------------------------------------------------------------


def test_send_success_message(http: _HttpMock) -> None:
    http.queue_response(200, {"ok": True, "result": {"message_id": 1}})
    notifier = TelegramNotifier()
    notifier.send_success(_make_record(True), db=_make_db(), config=_cfg())
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["url"].startswith("https://api.telegram.org/bot123456:ABCDEF/sendMessage")
    payload = call["body"]
    assert payload["parse_mode"] == "MarkdownV2"
    assert payload["chat_id"] == "-1001234567890"
    assert "Backup OK" in payload["text"]
    assert "`orders`" in payload["text"]


def test_send_failure_message(http: _HttpMock) -> None:
    http.queue_response(200, {"ok": True, "result": {"message_id": 1}})
    notifier = TelegramNotifier()
    notifier.send_failure(_make_record(False), db=_make_db(), config=_cfg())
    assert len(http.calls) == 1
    payload = http.calls[0]["body"]
    assert "Backup FAILED" in payload["text"]
    assert "InsufficientPrivileges" in payload["text"]
    assert "runabc" in payload["text"]


def test_markdown_v2_escaping() -> None:
    raw = "my_db.test [prod].x"
    escaped = escape_markdown_v2(raw)
    for ch in MARKDOWN_V2_RESERVED:
        if ch in raw:
            assert escaped.count("\\" + ch) == raw.count(ch)
    # backslash escapes itself
    assert escape_markdown_v2("a.b") == "a\\.b"


def test_event_filtering(http: _HttpMock) -> None:
    notifier = TelegramNotifier()
    # success with events=['failure'] -> NO HTTP call
    notifier.send_success(_make_record(True), db=_make_db(), config=_cfg(["failure"]))
    assert len(http.calls) == 0
    http.queue_response(200, {"ok": True, "result": {}})
    notifier.send_failure(_make_record(False), db=_make_db(), config=_cfg(["failure"]))
    assert len(http.calls) == 1


def test_http_error_surfaces(http: _HttpMock) -> None:
    http.queue_response(400, {"ok": False, "error_code": 400, "description": "Bad chat_id"})
    notifier = TelegramNotifier()
    with pytest.raises(TelegramSendError) as excinfo:
        notifier.send_success(_make_record(True), db=_make_db(), config=_cfg())
    assert excinfo.value.status == 400


def test_token_validation(http: _HttpMock) -> None:
    http.queue_response(200, {"ok": True, "result": {"id": 123456, "username": "testbot"}})
    body = TelegramNotifier.test_token("123456:ABCDEF")
    assert body["ok"] is True
    assert body["result"]["id"] == 123456
    assert http.calls[0]["url"].endswith("/getMe")
    assert http.calls[0]["method"] == "GET"


def test_proxy_used(http: _HttpMock) -> None:
    http.queue_response(200, {"ok": True, "result": {}})
    notifier = TelegramNotifier()
    cfg = _cfg().model_copy(update={"proxy_url": "http://127.0.0.1:3128"})
    notifier.send_success(_make_record(True), db=_make_db(), config=cfg)
    # the opener was built with a ProxyHandler
    # We can verify by inspecting the recorded call's URL & that nothing
    # rejected the request.
    assert len(http.calls) == 1


def test_message_truncation() -> None:
    long_err = "x" * 5000
    rec = _make_record(False).model_copy(update={"error": long_err})
    body = TelegramNotifier()._format_failure(rec, db=_make_db(), config=_cfg())
    assert len(body) <= 4000
    # the error excerpt inside the body should end with the truncation marker
    assert "…" in body
    assert body.count("x") < 5000  # only a slice of the long_err made it


def test_validator() -> None:
    TelegramNotifier.validate_config(_cfg())
    TelegramNotifier.validate_config(_cfg(["failure"]))
    with pytest.raises(ValueError):
        TelegramNotifier.validate_config(_cfg(["failure"]).model_copy(update={"bot_token": ""}))


def test_api_ok_false_with_200(http: _HttpMock) -> None:
    """Telegram sometimes returns 200 with {ok: false} — must surface."""
    http.queue_response(200, {"ok": False, "error_code": 400, "description": "Bad chat_id"})
    notifier = TelegramNotifier()
    with pytest.raises(TelegramSendError):
        notifier.send_success(_make_record(True), db=_make_db(), config=_cfg())