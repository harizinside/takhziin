"""HTTP routes for the Telegram notifier panel."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from takhziin.models import NotifierConfig
from takhziin.notifiers.telegram import TelegramNotifier, TelegramSendError
from takhziin.secrets import mask_token
from takhziin.state import State

router = APIRouter()


@router.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request) -> HTMLResponse:
    state: State = request.app.state.state
    templates: Jinja2Templates = request.app.state.templates
    cfg = state.notifier
    masked = ""
    if cfg and cfg.bot_token:
        from takhziin.secrets import Secrets

        sec = Secrets(master_key_file=request.app.state.settings.master_key_file)
        try:
            masked = mask_token(sec.decrypt(cfg.bot_token))
        except Exception:
            masked = "****"
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {"title": "Notifications", "cfg": cfg, "masked": masked, "error": None, "ok": None},
    )


@router.post("/notifications/save")
async def notifications_save(request: Request) -> RedirectResponse:
    state: State = request.app.state.state
    secrets = request.app.state.secrets
    form = await request.form()
    bot_token = str(form.get("bot_token", "")).strip()
    chat_id = str(form.get("chat_id", "")).strip()
    proxy_url = str(form.get("proxy_url", "")).strip() or None
    events = []
    if str(form.get("on_success", "")) == "on":
        events.append("success")
    if str(form.get("on_failure", "")) == "on":
        events.append("failure")
    if not bot_token or not chat_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bot_token and chat_id required")
    try:
        TelegramNotifier.test_token(bot_token, proxy_url=proxy_url)
    except TelegramSendError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"token invalid: {exc}") from exc
    state.notifier = NotifierConfig(
        kind="telegram",
        bot_token=secrets.encrypt(bot_token),
        chat_id=chat_id,
        events=events,
        proxy_url=secrets.encrypt_optional(proxy_url),
    )
    state.save()
    return RedirectResponse(url="/notifications", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/notifications/test")
async def notifications_test(request: Request) -> RedirectResponse:
    state: State = request.app.state.state
    secrets = request.app.state.secrets
    if state.notifier is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not configured")
    cfg = state.notifier.model_copy(
        update={
            "bot_token": secrets.decrypt(state.notifier.bot_token),
            "proxy_url": secrets.decrypt_optional(state.notifier.proxy_url),
        }
    )
    try:
        TelegramNotifier().send_message(
            "*\\u2728 Test notification*\nThis is a test message from takhziin\\.",
            config=cfg,
        )
    except TelegramSendError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"send failed: {exc}") from exc
    return RedirectResponse(url="/notifications", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/notifications/clear")
def notifications_clear(request: Request) -> RedirectResponse:
    state: State = request.app.state.state
    state.notifier = None
    state.save()
    return RedirectResponse(url="/notifications", status_code=status.HTTP_303_SEE_OTHER)


__all__ = ["router"]
