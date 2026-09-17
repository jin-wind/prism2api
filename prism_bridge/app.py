from __future__ import annotations

import asyncio
import contextlib
import hmac
import time
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .protocol import (MAX_BYTES, BridgeError, MemoryStore, ToolContinuationStore, dumps, loads, make_prompt,
                       normalize, output_events, parse_answer, response, uid)
from .oauth import OAuthManager, REDIRECT_DEFAULT
from .obs import TrafficRecorder

# Loopback hosts are always trusted; anything else must be named explicitly by
# the operator through public_hosts, so a default install cannot be reached
# through an attacker-supplied Host header.
LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]", "::1", "testserver"]

# Browser-navigable management pages. They carry no secrets: the page asks the
# browser for the bridge key and sends it on every API call.
OPEN_MANAGEMENT_PATHS = frozenset({"/ui", "/oauth"})


def create_app(backend, api_key: str, *, keepalive=10, oauth: OAuthManager | None = None,
               ui: bool = True, port: int = 8765, public_hosts=()):
    if not api_key or len(api_key) < 16:
        raise ValueError("PRISM_BRIDGE_API_KEY must have at least 16 characters.")
    store = MemoryStore()
    continuations = ToolContinuationStore()
    traffic = TrafficRecorder()

    @asynccontextmanager
    async def lifespan(_):
        yield
        await backend.close()

    app = FastAPI(title="Prism Codex Bridge (experimental)", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    allowed = list(LOOPBACK_HOSTS) + [h for h in public_hosts if h]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)

    if oauth is not None:
        @app.get("/oauth/login")
        async def oauth_login(request: Request):
            mode = request.query_params.get("mode", "auto")
            if mode == "local":
                oauth2 = OAuthManager(oauth.state_file, client_id=oauth.client_id,
                                      redirect_uri=REDIRECT_DEFAULT)
                oauth2.pending = oauth.pending
                url = oauth2.build_authorize_url()
            else:
                url = oauth.build_authorize_url()
            return JSONResponse({"authorization_url": url,
                                 "state": urllib.parse.urlsplit(url).query.split("state=")[1].split("&")[0] if "state=" in url else "",
                                 "mode": mode})

        @app.get("/oauth")
        async def oauth_guide():
            # The console's login tab supersedes this page and, unlike it, sends
            # the bridge key with every action. The old page also minted a
            # pending PKCE session on each GET, which is unbounded growth once
            # the port is reachable from anywhere.
            return RedirectResponse("/ui#login", status_code=307)

        @app.post("/oauth/submit")
        async def oauth_submit(request: Request):
            form = await request.form()
            callback_url = str(form.get("callback_url", ""))
            from urllib.parse import urlsplit, parse_qs
            q = parse_qs(urlsplit(callback_url).query)
            code = (q.get("code") or [None])[0]
            state = (q.get("state") or [None])[0]
            if not code or not state:
                return JSONResponse({"ok": False, "error": "回调 URL 里缺少 code 或 state"}, status_code=400)
            try:
                result = await oauth.exchange_code(code, state)
                oauth.clear_pending(state)
            except BridgeError as exc:
                return JSONResponse({"ok": False, "error": exc.message, "code": exc.code}, status_code=exc.status)
            return JSONResponse({"ok": True, "login_success": True, "email": result.email,
                                 "account_id": result.account_id, "user_id": result.user_id,
                                 "expires_at": result.expires_at})

        @app.get("/oauth/callback")
        async def oauth_callback(request: Request):
            query = request.query_params
            code = query.get("code")
            state = query.get("state")
            error = query.get("error")
            if error:
                return JSONResponse({"ok": False, "error": error}, status_code=400)
            if not code or not state:
                return JSONResponse({"ok": False, "error": "missing code or state"}, status_code=400)
            try:
                result = await oauth.exchange_code(code, state)
                oauth.clear_pending(state)
            except BridgeError as exc:
                return JSONResponse({"ok": False, "error": exc.message, "code": exc.code}, status_code=exc.status)
            return JSONResponse({
                "ok": True,
                "login_success": True,
                "email": result.email,
                "account_id": result.account_id,
                "user_id": result.user_id,
                "expires_at": result.expires_at,
                "message": "Login successful. The bridge is now authenticated; return to the CLI.",
            })

        @app.post("/oauth/exchange")
        async def oauth_exchange(request: Request):
            body = await request.json()
            code = body.get("code")
            state = body.get("state")
            verifier = body.get("verifier")
            if not code or not state:
                raise BridgeError("OAuth exchange needs code and state.", "oauth_invalid_request", 400)
            oauth_state = await oauth.exchange_code(code, state, verifier=verifier)
            oauth.clear_pending(state)
            return {
                "ok": True,
                "email": oauth_state.email,
                "account_id": oauth_state.account_id,
                "user_id": oauth_state.user_id,
                "expires_at": oauth_state.expires_at,
            }

        @app.post("/oauth/refresh")
        async def oauth_refresh():
            oauth_state = await oauth.refresh()
            return {
                "ok": True,
                "email": oauth_state.email,
                "expires_at": oauth_state.expires_at,
            }

        @app.get("/oauth/status")
        async def oauth_status():
            if not oauth.state.valid:
                return {"ok": False, "configured": False}
            return {
                "ok": True,
                "configured": True,
                "email": oauth.state.email,
                "account_id": oauth.state.account_id,
                "user_id": oauth.state.user_id,
                "expired": oauth.state.expired,
                "expires_at": oauth.state.expires_at,
                "updated_at": oauth.state.updated_at,
            }

    @app.exception_handler(BridgeError)
    async def handle_error(_, exc):
        return JSONResponse({"error": exc.payload()}, status_code=exc.status)

    def management_headers(result):
        result.headers["Cache-Control"] = "no-store"
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "no-referrer"
        result.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        return result

    def has_bridge_key(request) -> bool:
        supplied = request.headers.get("x-bridge-key", "")
        if hmac.compare_digest(supplied.encode(), api_key.encode()):
            return True
        expected = "Bearer " + api_key
        return hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode())

    @app.middleware("http")
    async def auth(request, call_next):
        path = request.url.path
        if path in OPEN_MANAGEMENT_PATHS or path.startswith("/ui/") or path.startswith("/oauth/"):
            # /oauth/callback is reached by a browser redirect from
            # auth.openai.com and cannot carry a key; it is instead gated by the
            # PKCE state, which only a key-holder could have minted through
            # /oauth/login. Every other management route requires the key.
            if path not in OPEN_MANAGEMENT_PATHS and path != "/oauth/callback" and not has_bridge_key(request):
                return management_headers(JSONResponse(
                    {"error": {"message": "Management endpoints require the bridge key.",
                               "type": "authentication_error"}}, status_code=401))
            return management_headers(await call_next(request))
        # This is a loopback API for Codex, not a browser endpoint.
        if request.headers.get("origin"):
            return JSONResponse({"error": {"message": "Browser-origin requests are not accepted."}}, status_code=403)
        expected = "Bearer " + api_key
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
            return JSONResponse({"error": {"message": "Invalid bridge API key.", "type": "authentication_error"}}, status_code=401)
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        return result

    @app.get("/health")
    async def health():
        diag = getattr(backend, "diagnostics", None)
        return {"status": "ok", "upstream_verified": False, "tool_mode": "text-emulated",
                "diagnostics": diag.snapshot() if diag else None}

    @app.get("/diagnostics")
    async def diagnostics():
        diag = getattr(backend, "diagnostics", None)
        return diag.snapshot() if diag else {"available": False}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": "gpt-6-astra", "object": "model", "created": 0,
                                            "owned_by": "prism-bridge-unverified-upstream"}]}

    @app.get("/v1/responses/{response_id}")
    async def retrieve(response_id):
        return store.get(response_id)[0]

    @app.post("/v1/responses")
    async def responses(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise BridgeError("Request exceeds 4 MiB.", "request_too_large", 413)
        try:
            body = loads(raw)
        except (ValueError, RecursionError):
            raise BridgeError("Invalid JSON request.") from None
        if not isinstance(body, dict):
            raise BridgeError("Request must be a JSON object.")
        previous_id = body.get("previous_response_id")
        previous = store.get(previous_id)[1] if previous_id else None
        history, tools = normalize(body, previous)
        nonce = uid("nonce_")
        prompt = make_prompt(body, history, tools, nonce)
        model = body.get("model", "gpt-6-astra")
        initial = response(model, status="in_progress")
        record = traffic.begin("/v1/responses", model, stream=bool(body.get("stream", False)))

        async def generate():
            try:
                continuation = continuations.take(history)
                kwargs = {"continuation": continuation} if continuation else {}
                text = await backend.complete(prompt, model, (body.get("reasoning") or {}).get("effort", "medium"), **kwargs)
                output = parse_answer(text, tools, nonce, body)
                continuations.put(output, getattr(text, "continuation", None))
                result = response(model, initial["id"], output)
                result["created_at"] = initial["created_at"]
                result["parallel_tool_calls"] = body.get("parallel_tool_calls", True)
                if body.get("store", True):
                    store.put(result, history)
                traffic.finish(record, output=output)
                return result
            except BridgeError as exc:
                traffic.finish(record, error=exc.payload())
                raise
            except asyncio.CancelledError:
                traffic.finish(record, error={"code": "client_cancelled", "message": "Client disconnected before completion."})
                raise
            except Exception as exc:
                traffic.finish(record, error={"code": "internal_error", "message": type(exc).__name__})
                raise

        if not body.get("stream", False):
            return await generate()

        async def events():
            sequence = 0

            def encode(event):
                nonlocal sequence
                event["sequence_number"] = sequence
                sequence += 1
                return f"event: {event['type']}\ndata: {dumps(event)}\n\n"

            task = asyncio.create_task(generate())
            try:
                yield encode({"type": "response.created", "response": initial})
                yield encode({"type": "response.in_progress", "response": initial})
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=keepalive)
                    if not done:
                        yield ": keepalive; waiting for Prism completion\n\n"
                result = await task
                # Buffered translation, not genuine upstream token streaming.
                for event in output_events(result):
                    yield encode(event)
            except BridgeError as exc:
                failed = response(model, initial["id"], status="failed", error=exc.payload())
                yield encode({"type": "response.failed", "response": failed})
            except Exception:
                failed = response(model, initial["id"], status="failed",
                                  error={"code": "internal_error", "message": "Bridge failed; no completion was emitted."})
                yield encode({"type": "response.failed", "response": failed})
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise BridgeError("Request exceeds 4 MiB.", "request_too_large", 413)
        try:
            body = loads(raw)
        except (ValueError, RecursionError):
            raise BridgeError("Invalid JSON request.") from None
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body["messages"]:
            raise BridgeError("chat.completions requires a non-empty messages list.")
        model = body.get("model", "gpt-6-astra")
        messages = body["messages"]
        for item in messages:
            if not isinstance(item, dict) or item.get("role") not in ("system", "user", "assistant") \
               or not isinstance(item.get("content"), str):
                raise BridgeError("Each message needs role and string content.")
        if body.get("tools"):
            raise BridgeError("Tool definitions are not supported on this chat.completions endpoint; use /v1/responses.", "unsupported_parameter")
        stream = bool(body.get("stream", False))
        history = [{"type": "message", "role": m["role"],
                    "content": [{"type": "input_text", "text": m["content"]}]} for m in messages]
        nonce = uid("nonce_")
        body_for_prompt = {"instructions": "", "tool_choice": "auto", "parallel_tool_calls": True}
        prompt = make_prompt(body_for_prompt, history, {}, nonce)

        async def generate_chat():
            raw = await backend.complete(prompt, model, (body.get("reasoning") or {}).get("effort", "medium"))
            output = parse_answer(raw, {}, nonce, body_for_prompt)
            parts = [item for item in output if item.get("type") == "message"]
            return parts[0]["content"][0]["text"] if parts else ""

        if not stream:
            answer = await generate_chat()
            return {"id": uid("chatcmpl_"), "object": "chat.completion", "created": int(time.time()),
                    "model": model, "choices": [{"index": 0,
                                                 "message": {"role": "assistant", "content": answer},
                                                 "finish_reason": "stop"}],
                    "usage": None}

        async def chat_events():
            task = asyncio.create_task(generate_chat())
            created = int(time.time())
            try:
                yield "data: " + dumps({"id": uid("chatcmpl_"), "object": "chat.completion.chunk",
                                        "created": created, "model": model,
                                        "choices": [{"index": 0, "delta": {"role": "assistant"},
                                                     "finish_reason": None}]}) + "\n\n"
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=keepalive)
                    if not done:
                        yield ": keepalive; waiting for Prism completion\n\n"
                answer = await task
                # Buffered single-delta translation (no genuine upstream token stream).
                yield "data: " + dumps({"id": uid("chatcmpl_"), "object": "chat.completion.chunk",
                                        "created": created, "model": model,
                                        "choices": [{"index": 0, "delta": {"content": answer},
                                                     "finish_reason": None}]}) + "\n\n"
                yield "data: " + dumps({"id": uid("chatcmpl_"), "object": "chat.completion.chunk",
                                        "created": created, "model": model,
                                        "choices": [{"index": 0, "delta": {},
                                                     "finish_reason": "stop"}]}) + "\n\n"
                yield "data: [DONE]\n\n"
            except BridgeError as exc:
                yield "data: " + dumps({"error": exc.payload()}) + "\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(chat_events(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    if ui:
        from .admin import create_admin_router
        app.include_router(create_admin_router(backend, api_key, traffic, oauth, port=port))

    return app
