"""Local PKCE login. Credentials and authorization codes never enter responses or traces."""
import base64
import hashlib
import logging
import re
import secrets
import ssl
from threading import Lock
from time import monotonic
from urllib.parse import parse_qs, urlencode

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

CALLBACK = "/oauth/openrouter/callback"
COOKIE = "cbs_openrouter_state"


class CallbackLogFilter(logging.Filter):
    """Also cover HTTP clients/access loggers that format the original URL."""
    def filter(self, record):
        message = record.getMessage()
        if CALLBACK + "?" in message:
            record.msg = re.sub(r"(/oauth/openrouter/callback)\?[^\s\"']*", r"\1?[REDACTED]", message)
            record.args = ()
        return True


async def exchange_key(code, verifier, tls_strict):
    context = ssl.create_default_context()
    if not tls_strict:
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    async with httpx.AsyncClient(verify=context, timeout=30, follow_redirects=False) as client:
        response = await client.post("https://openrouter.ai/api/v1/auth/keys", json={
            "code": code, "code_verifier": verifier, "code_challenge_method": "S256"})
        response.raise_for_status()
        key = response.json()["key"]
        if not isinstance(key, str) or not key:
            raise ValueError()
        return key


def install_openrouter_oauth(app, pipeline, save_key):
    pending, lock = {}, Lock()
    completed = 0
    for name in ("uvicorn.access", "httpx", "httpx2"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, CallbackLogFilter) for f in logger.filters):
            logger.addFilter(CallbackLogFilter())
    app.state.openrouter_exchange = exchange_key

    @app.middleware("http")
    async def hide_callback_query(request, call_next):
        if request.scope["path"] == CALLBACK:
            # Uvicorn formats its access log from this scope after the response.
            request.scope.setdefault("state", {})["oauth_query"] = parse_qs(request.scope.get("query_string", b"").decode("utf-8", errors="replace"))
            request.scope["query_string"] = b""
        return await call_next(request)

    @app.get("/oauth/openrouter/start")
    def start(request: Request):
        port = request.url.port or 80
        origin = f"http://127.0.0.1:{port}"
        if request.url.hostname != "127.0.0.1":
            return RedirectResponse(origin + "/oauth/openrouter/start", status_code=303)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        with lock:
            for old in [s for s, (_, expiry) in pending.items() if expiry <= monotonic()]:
                pending.pop(old)
            if len(pending) >= 32:
                pending.pop(next(iter(pending)))
            pending[state] = (verifier, monotonic() + 600)
        auth_url = "https://openrouter.ai/auth?" + urlencode({
            "callback_url": origin + CALLBACK, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": state, "key_label": "Awalia"})
        response = RedirectResponse("https://openrouter.ai/sign-in?" + urlencode({"redirect_url": auth_url}), status_code=303)
        response.set_cookie(COOKIE, state, max_age=600, httponly=True, samesite="lax", path="/oauth/openrouter")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/openrouter/status")
    def login_status():
        return JSONResponse({"completed": completed}, headers={"Cache-Control": "no-store"})

    @app.get(CALLBACK)
    async def callback(request: Request):
        nonlocal completed
        query = request.scope["state"].pop("oauth_query", {})
        state = query.get("state", [""])[0]
        cookie = request.cookies.get(COOKIE, "")
        status = "invalid"
        entry = None
        if state and cookie and secrets.compare_digest(state, cookie):
            with lock:
                entry = pending.pop(state, None)
        if entry:
            verifier, expiry = entry
            if expiry <= monotonic():
                status = "expired"
            elif query.get("error"):
                status = "denied"
            elif len(query.get("code", [])) == 1 and query["code"][0]:
                try:
                    key = await app.state.openrouter_exchange(query["code"][0], verifier, pipeline.config.llm.tls_strict)
                    save_key("openrouter", key)
                    completed += 1
                    status = "connected"
                except Exception:
                    status = "failed"
        message = ("連携できました。アプリに戻ってください（このタブは閉じてかまいません）。" if status == "connected"
                   else "連携が拒否・キャンセルされました。アプリからもう一度試してください。" if status == "denied"
                   else "連携できませんでした。アプリからもう一度試してください。")
        response = HTMLResponse('<!doctype html><meta charset="utf-8"><title>OpenRouter の接続</title>'
            '<script>history.replaceState(null,"","' + CALLBACK + '");</script><p>' + message + '</p>')
        response.delete_cookie(COOKIE, path="/oauth/openrouter")
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})
        return response
