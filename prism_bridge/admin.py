"""Admin API + web UI for the bridge.

All state-reading and login endpoints live under /ui/api and require the
bridge key in the X-Bridge-Key header. The HTML page itself (/ui) is served
without the key and contains no secrets; the browser stores the key locally
and sends it with every API call. Cross-site requests cannot forge the
custom header without a CORS preflight, which is never granted.

Responses never include cookies, tokens, prompts, or tool arguments.
"""
from __future__ import annotations

import hmac
import json
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .auth import PrismAuth, import_access_token_header
from .protocol import BridgeError

STATIC_DIR = Path(__file__).parent / "static"


MAX_CREDENTIAL_BYTES = 64 * 1024


async def read_json_body(request: Request, allowed_fields: set[str]) -> dict:
    """Read a small credential payload, rejecting oversize or unexpected fields.

    Unknown keys are refused rather than ignored so a caller cannot smuggle in
    fields (a file path, a state-file override) that a future handler might read.
    """
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_CREDENTIAL_BYTES:
                raise BridgeError("Credential request is too large.", "auth_content_too_large", 413)
        except ValueError:
            raise BridgeError("Invalid Content-Length header.", "invalid_content_length", 400) from None
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_CREDENTIAL_BYTES:
            raw.clear()
            raise BridgeError("Credential request is too large.", "auth_content_too_large", 413)
    try:
        body = json.loads(bytes(raw))
    except (UnicodeDecodeError, ValueError):
        raw.clear()
        raise BridgeError("Request body must be JSON.", "invalid_request", 400) from None
    finally:
        raw.clear()
    if not isinstance(body, dict) or not set(body) or not set(body) <= allowed_fields:
        raise BridgeError(f"Request accepts only these fields: {', '.join(sorted(allowed_fields))}.",
                          "invalid_request", 400)
    return body


def _mask(value, keep: int = 8) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return value[:keep] + "…" if len(value) > keep else value


def _token_from_content(content: str) -> str:
    try:
        data = json.loads(content)
        token = data.get("access_token") or data.get("accessToken") or (data.get("tokens") or {}).get("access_token")
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token.strip()):
            raise ValueError()
        return token.strip()
    except (ValueError, AttributeError, TypeError):
        raise BridgeError("Pasted JSON must contain access_token, accessToken, or tokens.access_token.",
                          "invalid_auth_file") from None


def create_admin_router(backend, api_key: str, traffic, oauth=None, *, port: int = 8765) -> APIRouter:
    router = APIRouter()
    started_at = time.time()

    def check_key(request: Request):
        supplied = request.headers.get("x-bridge-key", "")
        if not hmac.compare_digest(supplied.encode(), api_key.encode()):
            raise BridgeError("Invalid bridge key.", "authentication_error", 401)

    @router.get("/ui", include_in_schema=False)
    async def ui_page():
        page = STATIC_DIR / "ui.html"
        if not page.exists():
            return HTMLResponse("<h1>UI assets missing</h1><p>static/ui.html was not packaged.</p>", status_code=500)
        return HTMLResponse(page.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @router.get("/ui/api/status")
    async def status(request: Request):
        check_key(request)
        auth = backend.auth
        diag = backend.diagnostics
        template = backend.template
        prism = {"configured": auth is not None}
        if auth is not None:
            prism.update({
                "ready": auth.ready,
                "signed_in": bool((auth.summary or {}).get("signed_in")),
                "persistent": auth.state_file is not None,
                "state_file": auth.state_file.name if auth.state_file else None,
                "next_refresh_in_s": max(0, round(auth.refresh_at - time.time())) if auth.ready else None,
                "expected_user": _mask(auth.expected_user),
            })
        oauth_info = {"configured": False}
        if oauth is not None and oauth.state.valid:
            oauth_info = {"configured": True, "email": oauth.state.email,
                          "expired": oauth.state.expired,
                          "expires_in_s": max(0, round(oauth.state.expires_at - time.time()))}
        return {
            "bridge": {"version": __version__, "port": port,
                       "uptime_s": round(time.time() - started_at),
                       "endpoint": f"http://127.0.0.1:{port}/v1"},
            "traffic": {"total": traffic.total, "failed": traffic.failed},
            "prism_auth": prism,
            "oauth": oauth_info,
            "sandbox": {
                "provisioned": backend.sandbox_provisioned,
                "needs_bootstrap": backend.needs_bootstrap,
                "replaced": backend.sandbox_was_replaced,
                "session_id": _mask(backend.sandbox_session_id or ""),
                "resource_expires_in_s": max(0, round(backend.resource_expires_at - time.time()))
                if backend.resource_expires_at else 0,
            },
            "project": {"id": _mask(template.metadata.get("projectId", "")),
                        "user": _mask(template.metadata.get("userId", ""))},
            "diagnostics": {"current_stage": diag.current_stage,
                            "completed_turns": diag.completed_turns,
                            "model_request_sent": diag.model_request_sent,
                            "last_error": diag.last_error},
        }

    @router.get("/ui/api/traffic")
    async def traffic_view(request: Request):
        check_key(request)
        return traffic.snapshot()

    @router.get("/ui/api/logs")
    async def logs(request: Request):
        check_key(request)
        return backend.diagnostics.snapshot()

    @router.post("/ui/api/preflight")
    async def preflight(request: Request):
        check_key(request)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        provision = bool(isinstance(body, dict) and body.get("provision"))
        return await backend.preflight(provision=provision)

    # ---- Prism auth (cookie / token / oauth-bind) -------------------------

    async def _probe_and_adopt(seed: str, *, import_only: bool) -> dict:
        """Verify a credential against Prism on an isolated client, persist the
        auth state on success, then swap the live backend onto the new state."""
        if backend.auth is None:
            raise BridgeError("This backend does not own Prism auth (shared client mode).",
                              "auth_not_managed", 409)
        expected_user = backend.template.metadata.get("userId")
        state_file = backend.auth.state_file
        headers = {k: v for k, v in backend.template.headers.items() if k.lower() != "cookie"}
        candidate = PrismAuth(seed, expected_user=expected_user, state_file=state_file)
        from .upstream import ORIGIN
        async with httpx.AsyncClient(base_url=ORIGIN, headers=headers, http2=True,
                                     follow_redirects=False, timeout=30) as client:
            summary = await candidate.ensure(client, import_only=import_only)
        # Success: persisted to state_file. Reload the live backend from disk so
        # in-flight state cannot mix stale cookies with the new session.
        backend.client.cookies.clear()
        backend.auth = PrismAuth(seed, expected_user=expected_user, state_file=state_file)
        return summary

    @router.get("/ui/api/auth/status")
    async def auth_status(request: Request):
        check_key(request)
        auth = backend.auth
        result = {"prism": None, "oauth": None}
        if auth is not None:
            result["prism"] = {"ready": auth.ready,
                               "signed_in": bool((auth.summary or {}).get("signed_in")),
                               "state_file_exists": bool(auth.state_file and auth.state_file.exists()),
                               "next_refresh_in_s": max(0, round(auth.refresh_at - time.time())) if auth.ready else None}
        if oauth is not None:
            result["oauth"] = {"configured": oauth.state.valid, "email": oauth.state.email,
                               "expired": oauth.state.expired if oauth.state.valid else None}
        return result

    @router.post("/ui/api/auth/refresh")
    async def auth_refresh(request: Request):
        check_key(request)
        if backend.auth is None:
            raise BridgeError("This backend does not own Prism auth.", "auth_not_managed", 409)
        summary = await backend.auth.ensure(backend.client, force=True)
        return {"ok": True, **summary}

    @router.post("/ui/api/auth/cookie")
    async def auth_cookie(request: Request):
        check_key(request)
        body = await read_json_body(request, {"cookie"})
        cookie = body.get("cookie", "")
        if not isinstance(cookie, str) or "=" not in cookie:
            raise BridgeError("Paste the full Cookie header value from a signed-in Prism request.",
                              "invalid_request", 400)
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        summary = await _probe_and_adopt(cookie.strip(), import_only=False)
        return {"ok": True, "method": "cookie", **summary}

    @router.post("/ui/api/auth/import")
    async def auth_import(request: Request):
        check_key(request)
        body = await read_json_body(request, {"token", "content"})
        token = body.get("token") or ""
        content = body.get("content") or ""
        if not token and content:
            token = _token_from_content(content)
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token.strip()):
            raise BridgeError("Provide an access token or paste the auth JSON file content.",
                              "invalid_request", 400)
        seed = import_access_token_header("", token.strip())
        summary = await _probe_and_adopt(seed, import_only=True)
        return {"ok": True, "method": "access_token", **summary}

    @router.post("/ui/api/auth/oauth-bind")
    async def oauth_bind(request: Request):
        check_key(request)
        if oauth is None:
            raise BridgeError("OAuth is not configured on this server.", "oauth_not_configured", 409)
        state = await oauth.refresh() if oauth.state.expired else oauth.state
        if not state.access_token:
            raise BridgeError("No OAuth access token; complete the browser login first.",
                              "oauth_missing_access", 401)
        seed = import_access_token_header("", state.access_token)
        summary = await _probe_and_adopt(seed, import_only=True)
        return {"ok": True, "method": "oauth_bind", "email": state.email, **summary}

    @router.get("/ui/api/config")
    async def config_view(request: Request):
        """Read-only effective runtime configuration. These are boot-time
        settings (env vars / CLI flags); changing them requires a restart,
        so the UI shows rather than edits them."""
        check_key(request)
        auth = backend.auth
        return {
            "port": port,
            "turn_timeout_s": getattr(backend, "timeout", None),
            "read_timeout_s": getattr(backend, "read_timeout", None),
            "poll_interval_s": getattr(backend, "poll", None),
            "auth_state_file": auth.state_file.name if auth and auth.state_file else None,
            "sources": {
                "PRISM_TURN_TIMEOUT": "单轮总超时（秒）/ total per-turn timeout",
                "PRISM_HTTP_READ_TIMEOUT": "上游单次读取超时（秒）/ upstream read timeout",
                "PRISM_AUTH_STATE": "认证状态文件路径 / auth state path",
                "PRISM_HAR_PATH / --har": "模板来源，改动需重启 / template source, restart to change",
            },
        }

    # ---- Codex setup helper ----------------------------------------------

    @router.get("/ui/api/setup")
    async def setup(request: Request):
        check_key(request)
        model_id = (getattr(getattr(backend, "template", None), "metadata", {}) or {}).get("model") or "gpt-5.6-sol"
        toml_text = f"""model = "{model_id}"
model_provider = "prism_bridge"
model_reasoning_effort = "medium"
model_reasoning_summary = "none"
web_search = "disabled"

[model_providers.prism_bridge]
name = "Prism bridge (experimental)"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
env_key = "PRISM_BRIDGE_API_KEY"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 360000
"""
        return {"config_toml": toml_text,
                "config_path": "$CODEX_HOME/prism_bridge.config.toml",
                "env_key": "PRISM_BRIDGE_API_KEY",
                "powershell": "& 'D:\\Code\\prism-codex-bridge\\Use-Codex.ps1'",
                "note": "The bridge key you entered in this UI is the PRISM_BRIDGE_API_KEY value."}

    return router
