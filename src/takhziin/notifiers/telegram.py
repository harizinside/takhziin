"""Telegram notifier — MarkdownV2 messages via stdlib ``urllib``.

Single global bot config (one ``bot_token`` + ``chat_id``) sends on
``success`` / ``failure`` events only (per v1 product decision; no start
event). User-controlled strings are escaped per the Telegram MarkdownV2
spec before being inserted into a fixed-template message body.

Network transport: stdlib urllib + an optional ``ProxyHandler`` when
``proxy_url`` is set. No extra deps.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from takhziin.models import BackupRecord, Database, NotifierConfig

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_BOT_TIMEOUT = 10  # seconds
MESSAGE_MAX_LEN = 4000
ERROR_EXCERPT_MAX = 1500

# Characters that MUST be escaped in MarkdownV2 outside of code blocks.
MARKDOWN_V2_RESERVED = set("_*~`>#+-=|{}.!\\")


def escape_markdown_v2(text: str) -> str:
    """Escape every MarkdownV2 reserved char in ``text`` with a leading backslash.

    Per the Telegram Bot API docs: "In all other places characters '_', '*', '[',
    ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!',
    '\\' must be escaped with the preceding '\\' character."
    """
    if text is None:
        return ""
    out = []
    for ch in text:
        if ch in MARKDOWN_V2_RESERVED:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(num_bytes)
    for u in units:
        if f < 1024:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} PB"


def _human_duration(started: str, finished: str | None) -> str:
    """Compute a friendly duration; fall back to raw ISO if parsing fails."""
    if not finished:
        return "-"
    try:
        from datetime import datetime

        s = datetime.fromisoformat(started)
        f = datetime.fromisoformat(finished)
        delta = f - s
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            m, s = divmod(secs, 60)
            return f"{m}m{s}s"
        h, rem = divmod(secs, 3600)
        m = rem // 60
        return f"{h}h{m}m"
    except (ValueError, TypeError):
        return f"{started} → {finished}"


class TelegramSendError(RuntimeError):
    """Raised when Telegram returns a non-200 status code."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Telegram send error {status}: {body}")
        self.status = status
        self.body = body


class TelegramNotifier:
    """Sends MarkdownV2 messages to a single global chat."""

    name = "telegram"

    def __init__(self) -> None:
        # No config here — every call carries the live NotifierConfig from state.
        pass

    # --- formatting ---------------------------------------------------------

    def _format_success(
        self,
        record: BackupRecord,
        db: Database,
        config: NotifierConfig,
    ) -> str:
        duration = _human_duration(record.started_at, record.finished_at)
        size = _human_size(record.size_bytes)
        location = escape_markdown_v2(record.location)
        db_name = escape_markdown_v2(db.name)
        return (
            f"*\\u2705 Backup OK* — `{db_name}`\n"
            f"Duration: `{escape_markdown_v2(duration)}`\n"
            f"Size: `{size}`\n"
            f"Storage: `{db.storage_kind}`\n"
            f"Location: `{location}`"
        )

    def _format_failure(
        self,
        record: BackupRecord,
        db: Database,
        config: NotifierConfig,
    ) -> str:
        duration = _human_duration(record.started_at, record.finished_at)
        error_excerpt = _truncate(
            escape_markdown_v2(record.error or "unknown error"),
            ERROR_EXCERPT_MAX,
        )
        db_name = escape_markdown_v2(db.name)
        return (
            f"*\\u274C Backup FAILED* — `{db_name}`\n"
            f"Duration: `{escape_markdown_v2(duration)}`\n"
            f"Error: `{error_excerpt}`\n"
            f"Run ID: `{record.id}`"
        )

    # --- public API ---------------------------------------------------------

    def send_success(
        self,
        record: BackupRecord,
        *,
        db: Database,
        config: NotifierConfig,
    ) -> None:
        if "success" not in config.events:
            return
        body = self._format_success(record, db, config)
        self._send(body, config)

    def send_failure(
        self,
        record: BackupRecord,
        *,
        db: Database,
        config: NotifierConfig,
    ) -> None:
        if "failure" not in config.events:
            return
        body = self._format_failure(record, db, config)
        self._send(body, config)

    def send_message(self, text: str, *, config: NotifierConfig) -> None:
        self._send(text, config)

    # --- transport ----------------------------------------------------------

    def _send(self, text: str, config: NotifierConfig) -> None:
        if not config.bot_token:
            raise TelegramSendError(401, "bot_token is empty")
        url = f"{TELEGRAM_API}/bot{config.bot_token}/sendMessage"
        payload = {
            "chat_id": config.chat_id,
            "text": _truncate(text, MESSAGE_MAX_LEN),
            "parse_mode": "MarkdownV2",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        opener = self._build_opener(config)
        try:
            with opener.open(req, timeout=TELEGRAM_BOT_TIMEOUT) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise TelegramSendError(exc.code, body) from exc
        except urllib.error.URLError as exc:
            raise TelegramSendError(0, f"network error: {exc}") from exc
        if status != 200:
            raise TelegramSendError(status, body)
        # validate response shape; Telegram returns {ok: false, ...} on API errors
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return
        if parsed.get("ok") is False:
            raise TelegramSendError(200, body)

    def _build_opener(self, config: NotifierConfig) -> urllib.request.OpenerDirector:
        if config.proxy_url:
            proxy_handler = urllib.request.ProxyHandler(
                {"http": config.proxy_url, "https": config.proxy_url}
            )
            return urllib.request.build_opener(proxy_handler)
        return urllib.request.build_opener()

    # --- validation ---------------------------------------------------------

    @staticmethod
    def validate_config(config: NotifierConfig) -> None:
        if not config.bot_token:
            raise ValueError("bot_token is required")
        if not config.chat_id:
            raise ValueError("chat_id is required")

    @staticmethod
    def test_token(bot_token: str, proxy_url: str | None = None) -> dict:
        """Call ``getMe`` and return the JSON body. Raises TelegramSendError on failure."""
        url = f"{TELEGRAM_API}/bot{bot_token}/getMe"
        req = urllib.request.Request(url, method="GET")
        handlers = []
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(req, timeout=TELEGRAM_BOT_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise TelegramSendError(exc.code, exc.read().decode("utf-8", errors="replace")) from exc
        return json.loads(body)


__all__ = [
    "ERROR_EXCERPT_MAX",
    "MARKDOWN_V2_RESERVED",
    "MESSAGE_MAX_LEN",
    "TelegramNotifier",
    "TelegramSendError",
    "escape_markdown_v2",
]
