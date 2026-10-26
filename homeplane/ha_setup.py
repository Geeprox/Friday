"""Explicit, loopback-only HA OAuth setup. No device writer or agent runtime.

Reference: https://developers.home-assistant.io/docs/auth_api/
Run: python -m homeplane.ha_setup --config friday.home.json
"""
import argparse
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from friday.home import Credentials

ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL_PATH = ROOT / "data/credentials/home-assistant.json"
CLIENT_ID = "http://127.0.0.1:8741"
CALLBACK = "/auth/ha/callback"
COOKIE = "home_ha_setup_session"


def create_app(config_path: Path, *, transport=None, monotonic=time.monotonic):
    """Config path must be supplied; never loads default HA addresses or model credentials."""
    config = json.loads(Path(config_path).read_text())
    url = config.get("ha_url")
    parts = urlsplit(url) if isinstance(url, str) else None
    if (parts is None or parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("Explicit HA config must contain a credential-free HTTP(S) ha_url")
    ha_origin = f"{parts.scheme}://{parts.netloc}"
    client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=15)
    credentials = Credentials(SimpleNamespace(ha_url=url, ha_token_env="HOME_HA_SETUP_UNUSED_TOKEN"), client)
    credentials.path = CREDENTIAL_PATH
    pending = {}
    status = {"credential_saved": False, "last_error": None}

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            pending.clear()
            await client.aclose()

    app = FastAPI(title="Home Assistant local authorization", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # Fixed host AND port: no forwarded header or Host-controlled OAuth redirects.
        if request.headers.get("host") != "127.0.0.1:8741":
            return JSONResponse({"code": "invalid_host"}, status_code=400)
        origin = request.headers.get("origin")
        callback = request.method == "GET" and request.url.path == CALLBACK
        if origin is not None and origin != CLIENT_ID:
            return JSONResponse({"code": "invalid_origin"}, status_code=403)
        if request.method == "POST" and origin != CLIENT_ID:
            return JSONResponse({"code": "origin_required"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site" and not callback:
            return JSONResponse({"code": "cross_site"}, status_code=403)
        if len(request.scope.get("query_string", b"")) > 8192:
            return JSONResponse({"code": "request_budget_exceeded"}, status_code=413)
        # No endpoint needs a body. Stream-bound rather than trusting Content-Length.
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1024:
                return JSONResponse({"code": "request_budget_exceeded"}, status_code=413)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache",
                                 "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": f"default-src 'none'; form-action 'self' {ha_origin}; frame-ancestors 'none'"})
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return """<!doctype html><html lang="en"><meta charset="utf-8"><title>Connect Home Assistant</title>
<h1>Connect Home Assistant</h1><p>Authorize this local control plane using your configured Home Assistant.
This setup saves a local credential. It does not control lights, start cameras, or start an agent.</p>
<form action="/auth/ha/start" method="post"><button type="submit">Connect Home Assistant</button></form>
<p>If the button does not open the login page, <a href="/auth/ha/start">open Home Assistant login</a>.</p>
<p>After authorization, return here to continue read-only verification.</p></html>"""

    @app.get("/auth/ha/start")
    @app.post("/auth/ha/start")
    async def start(request: Request):
        if request.method == "GET" and not (
            request.headers.get("sec-fetch-site") == "same-origin"
            or request.headers.get("origin") == CLIENT_ID
        ):
            return JSONResponse({"code": "same_origin_navigation_required"}, status_code=403)
        nonce, session = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        pending.clear()  # one in-flight login, bounded memory; restarting cancels old attempt
        pending[nonce] = (monotonic() + 600, session)
        query = urlencode({"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID + CALLBACK, "state": nonce})
        response = RedirectResponse(url.rstrip("/") + "/auth/authorize?" + query, status_code=303)
        response.set_cookie(COOKIE, session, max_age=600, httponly=True, samesite="lax", path="/")
        return response

    @app.get(CALLBACK)
    async def callback(request: Request, code: str = "", state: str = ""):
        entry = pending.get(state)
        session = request.cookies.get(COOKIE, "")
        if not entry or not secrets.compare_digest(entry[1], session):
            return JSONResponse({"code": "invalid_login_session"}, status_code=400)
        # Consume before network I/O; failed/expired exchanges cannot replay authorization codes.
        pending.pop(state, None)
        if entry[0] <= monotonic() or not code or len(code) > 4096:
            return JSONResponse({"code": "expired_or_invalid_login_session"}, status_code=400)
        try:
            if credentials.path.resolve() != credentials.path.absolute():
                raise ValueError("credential_path_symlink")
            await credentials.exchange(code, CLIENT_ID)
            os.chmod(credentials.path, 0o600)
            status.update(credential_saved=True, last_error=None)
            response = RedirectResponse("/complete", status_code=303)
        except Exception:
            # Never echo upstream error bodies, code, tokens, or private endpoint into logs/UI.
            status.update(credential_saved=False, last_error="authorization_exchange_failed")
            response = RedirectResponse("/failed", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/complete", response_class=HTMLResponse)
    async def complete():
        return "<h1>Authorization saved locally</h1><p>You can close this tab and continue read-only verification.</p>"

    @app.get("/failed", response_class=HTMLResponse)
    async def failed():
        return '<h1>Authorization did not complete</h1><p><a href="/">Start a new login</a>.</p>'

    @app.get("/status")
    async def setup_status():
        return {**status, "device_writes": "disabled", "camera_runtime": "not_started"}

    return app



def install_oauth(app: FastAPI, config_path: str | Path, base_url="http://127.0.0.1:8740", *,
                  transport=None, monotonic=time.monotonic):
    """Mount Settings OAuth on the primary Console without creating network clients.

    Primary middleware must permit cross-site top-level GET /auth/ha/callback;
    callback authorization remains bound to nonce + browser cookie here.
    """
    base = urlsplit(base_url)
    if (base.scheme != "http" or base.hostname != "127.0.0.1" or base.port != 8740
            or base.path not in {"", "/"} or base.query or base.fragment or base.username or base.password):
        raise ValueError("fixed_primary_loopback_origin_required")
    client_id = base_url.rstrip("/")
    cookie_name = "home_ha_console_session"
    pending = {}

    def configured_url():
        try:
            data = json.loads(Path(config_path).read_text())
            data = data.get("config", data)
            url = data["ha_url"]
            parts = urlsplit(url)
            if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
                    or parts.query or parts.fragment):
                raise ValueError()
            return url
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def valid_host(request):
        return request.headers.get("host") == base.netloc

    def sealed(response):
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        return response

    def login_session_failed():
        # A restarted server, expired nonce or different browser cookie must fail
        # closed, but leave a usable recovery page rather than a bare JSON error.
        return sealed(HTMLResponse('''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Home Assistant 登录未完成</title><h1>登录会话已失效</h1>
<p>可能是登录超时、服务重启或浏览器会话不一致。已保存的凭据不会被删除。</p>
<p><a href="/?ha=failed#settings">返回连接设置，检查现有授权或重新登录</a></p></html>''', status_code=400))

    @app.post("/api/settings/ha/connect")
    async def settings_connect(request: Request):
        if (not valid_host(request) or request.headers.get("origin") != client_id
                or request.headers.get("sec-fetch-site") == "cross-site"):
            return sealed(JSONResponse({"code": "unauthorized"}, status_code=403))
        url = configured_url()
        if url is None:
            return sealed(JSONResponse({"code": "ha_url_required"}, status_code=400))
        nonce, session = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        pending.clear()
        pending[nonce] = (monotonic() + 600, session, url)
        query = urlencode({"client_id": client_id, "redirect_uri": client_id + CALLBACK, "state": nonce})
        response = JSONResponse({"authorization_url": url.rstrip("/") + "/auth/authorize?" + query})
        response.set_cookie(cookie_name, session, max_age=600, httponly=True, samesite="lax", path="/")
        return sealed(response)

    @app.get(CALLBACK)
    async def settings_callback(request: Request, code: str = "", state: str = ""):
        if not valid_host(request) or len(request.scope.get("query_string", b"")) > 8192:
            return sealed(JSONResponse({"code": "unauthorized"}, status_code=403))
        entry = pending.get(state)
        if not entry or not secrets.compare_digest(entry[1], request.cookies.get(cookie_name, "")):
            return login_session_failed()
        pending.pop(state, None)
        if entry[0] <= monotonic() or not code or len(code) > 4096:
            return login_session_failed()
        try:
            if CREDENTIAL_PATH.resolve() != CREDENTIAL_PATH.absolute():
                raise ValueError("credential_path_symlink")
            # Bind exchange to endpoint selected at begin; changing Settings never redirects a pending code.
            async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=15) as client:
                credentials = Credentials(SimpleNamespace(ha_url=entry[2], ha_token_env="HOME_HA_SETUP_UNUSED_TOKEN"), client)
                credentials.path = CREDENTIAL_PATH
                await credentials.exchange(code, client_id)
                os.chmod(CREDENTIAL_PATH, 0o600)
            response = RedirectResponse("/?ha=connected#settings", status_code=303)
        except Exception:
            response = RedirectResponse("/?ha=failed#settings", status_code=303)
        response.delete_cookie(cookie_name, path="/")
        return sealed(response)

    @app.get("/api/settings/ha/status")
    async def settings_status(request: Request):
        if not valid_host(request) or request.headers.get("origin") not in {None, client_id}:
            return sealed(JSONResponse({"code": "unauthorized"}, status_code=403))
        url = configured_url()
        available = False
        try:
            saved = json.loads(CREDENTIAL_PATH.read_text())
            available = bool(url and saved.get("ha_url") == url and saved.get("access_token")
                             and saved.get("refresh_token") and saved.get("client_id"))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return sealed(JSONResponse({"configured": url is not None, "credential_available": available,
                                    "source_activation": "explicit_required", "device_writes": "disabled"}))

def main():
    parser = argparse.ArgumentParser(description="Loopback-only Home Assistant OAuth setup")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        app = create_app(args.config)
    except (OSError, ValueError, TypeError):
        raise SystemExit("Invalid explicit HA config; no credential or endpoint details logged.") from None
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8741, access_log=False, proxy_headers=False, log_level="warning")


if __name__ == "__main__":
    main()
