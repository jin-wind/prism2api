from __future__ import annotations

import asyncio
import copy
import contextlib
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from .protocol import BridgeError, dumps, loads
from .auth import PrismAuth, TRANSPORT_ERRORS
from .diagnostics import Diagnostics, operation_for, task_failure_details

ORIGIN = "https://prism.openai.com"
START = "/api/llm/response_with_tools_start"
STATUS = "/api/llm/response_with_tools_status"
STOP = "/api/llm/response_with_tools_stop"
HEARTBEAT = "/s/sandboxes/proxy/heartbeat"


@dataclass
class SessionTemplate:
    metadata: dict
    headers: dict
    conversation_action: dict | None = None
    editor_context: dict | None = None
    initial_system: dict | None = None

    @classmethod
    def from_har(cls, path, cookie_file=None, *, cookie_header=None, allow_missing_cookie=False):
        try:
            entries = json.loads(Path(path).read_text(encoding="utf-8-sig"))["log"]["entries"]
            matches = [e for e in entries if e["request"]["method"] == "POST"
                       and e["request"]["url"] == ORIGIN + START]
            request = matches[-1]["request"]
            captured = loads(request["postData"]["text"])
            metadata = captured["metadata"]
            if any(not isinstance(metadata.get(k), str) or not metadata[k]
                   for k in ("projectId", "userId", "sandbox_url", "sandbox_token")):
                raise ValueError()
            headers = {h["name"].lower(): h["value"] for h in request.get("headers", [])}
        except (KeyError, ValueError, TypeError, IndexError, OSError):
            raise BridgeError("HAR has no usable Prism start-request metadata.", "invalid_har") from None
        cookie = headers.get("cookie", "")
        if cookie_header is not None:
            cookie = cookie_header
        if cookie_file:
            try:
                cookie = Path(cookie_file).read_text(encoding="utf-8-sig").strip()
            except OSError:
                raise BridgeError("Cannot read PRISM_COOKIE_FILE.", "missing_cookie") from None
            if cookie.lower().startswith("cookie:"):
                cookie = cookie[7:].strip()
        if (not cookie and not allow_missing_cookie) or "\n" in cookie or "\r" in cookie:
            raise BridgeError("A current Prism Cookie is required. Supply a local PRISM_COOKIE_FILE; this HAR has no usable Cookie.", "missing_cookie", 401)
        selected = {"Cookie": cookie, "Origin": ORIGIN, "Accept": "application/json"}
        if headers.get("user-agent"):
            selected["User-Agent"] = headers["user-agent"]
        referer = headers.get("referer", ORIGIN + "/")
        selected["Referer"] = referer if urlsplit(referer).netloc == "prism.openai.com" else ORIGIN + "/"
        action = None
        for entry in reversed(entries):
            req = entry.get("request", {})
            target = urlsplit(req.get("url", ""))
            hs = {h["name"].lower(): h["value"] for h in req.get("headers", [])}
            if req.get("method") != "POST" or target.netloc != "prism.openai.com" or target.path != "/" or not hs.get("next-action"):
                continue
            try:
                args = loads(req.get("postData", {}).get("text", ""))
            except (ValueError, TypeError):
                continue
            reply = entry.get("response", {}).get("content", {}).get("text", "")
            if args == [metadata["projectId"]] and re.search(r'(?m)^\d+:"cdx1_[0-9a-f-]{36}"\s*$', reply):
                action = {"url": req["url"], "headers": {k: v for k, v in hs.items()
                          if k in ("next-action", "next-router-state-tree", "content-type", "accept")}}
                break
        context = None
        for item in captured.get("input", []):
            if item.get("role") != "system":
                continue
            for part in item.get("content", []):
                try:
                    candidate = loads(part.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(candidate, dict) and "openFile" in candidate:
                    context = copy.deepcopy(item)
        initial_system = None
        for entry in entries:
            req = entry.get("request", {})
            if req.get("url") != ORIGIN + START:
                continue
            try:
                prior = loads(req.get("postData", {}).get("text", ""))
            except (ValueError, TypeError):
                continue
            if prior.get("metadata", {}).get("projectId") != metadata["projectId"]:
                continue
            for item in prior.get("input", []):
                if item.get("role") != "system":
                    continue
                text = "".join(p.get("text", "") for p in item.get("content", []))
                if text and not text.lstrip().startswith("{"):
                    initial_system = copy.deepcopy(item)
                    break
            if initial_system:
                break
        return cls(copy.deepcopy(metadata), selected, action, context, initial_system)

    @classmethod
    def from_fixture(cls, path, cookie_file=None, *, allow_missing_cookie=False):
        """Load a serialized template fixture (deployment without a HAR)."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise BridgeError("Cannot read template fixture.", "invalid_fixture") from None
        if data.get("version") != 1 or not isinstance(data.get("metadata"), dict):
            raise BridgeError("Unsupported template fixture version.", "invalid_fixture") from None
        metadata = data["metadata"]
        if any(not isinstance(metadata.get(k), str) or not metadata[k]
               for k in ("projectId", "userId", "sandbox_url", "sandbox_token")):
            raise BridgeError("Template fixture has no usable Prism metadata.", "invalid_fixture") from None
        headers = {str(k).lower(): str(v) for k, v in (data.get("headers") or {}).items()}
        cookie = headers.get("cookie", "")
        if cookie_file:
            try:
                cookie = Path(cookie_file).read_text(encoding="utf-8-sig").strip()
            except OSError:
                raise BridgeError("Cannot read PRISM_COOKIE_FILE.", "missing_cookie") from None
            if cookie.lower().startswith("cookie:"):
                cookie = cookie[7:].strip()
        if (not cookie and not allow_missing_cookie) or "\n" in cookie or "\r" in cookie:
            raise BridgeError("A current Prism Cookie is required; template fixture has none.", "missing_cookie", 401)
        headers = {k: v for k, v in headers.items() if k != "cookie"}
        selected = {"Cookie": cookie, "Origin": ORIGIN, "Accept": "application/json", **headers}
        referer = selected.get("Referer") or ORIGIN + "/"
        from urllib.parse import urlsplit as _split
        selected["Referer"] = referer if _split(referer).netloc == "prism.openai.com" else ORIGIN + "/"
        return cls(copy.deepcopy(metadata), selected,
                   copy.deepcopy(data.get("conversation_action")),
                   copy.deepcopy(data.get("editor_context")),
                   copy.deepcopy(data.get("initial_system")))

    def new_turn(self, messages, model, effort, conversation=None, continuation=None):
        metadata = copy.deepcopy(self.metadata)
        # HAR and live validation: other prefixes are legacy/read-only conversations.
        conversation = conversation or "cdx1_" + str(uuid.uuid4())
        if not re.fullmatch(r"cdx1_[0-9a-f-]{36}", conversation):
            raise BridgeError("Invalid Prism Codex conversation identity.", "prism_protocol_error", 502)
        # proxy_request_debug is a serialized JSON object, NOT a boolean flag.
        metadata.update(model=model, reasoning_effort=effort)
        metadata["sandbox_url"] = metadata["sandbox_url"].rstrip("/") + "/"
        try:
            snapshot = loads(metadata.get("codex_listen_snapshot", "null")) or {}
        except (TypeError, ValueError):
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        snapshot.update(user_id=metadata["userId"], project_id=metadata["projectId"],
                        conversation_id=conversation, sandbox_url=metadata["sandbox_url"],
                        sandbox_token=metadata["sandbox_token"], workspace_session_id=conversation[5:], codex_session_id=None,
                        last_turn_id=None, endpoint_identity=None, last_exec_at=None,
                        transcript_cursor=0, created_at=now, updated_at=now, last_saved_at=None)
        if continuation:
            previous = continuation.get("snapshot")
            if not isinstance(previous, dict) or previous.get("conversation_id") != conversation:
                raise BridgeError("Invalid upstream continuation snapshot.", "prism_protocol_error", 502)
            if previous.get("project_id") != metadata["projectId"] or previous.get("user_id") != metadata["userId"]:
                raise BridgeError("Upstream continuation identity does not match the captured account/project.", "prism_protocol_error", 502)
            snapshot = copy.deepcopy(previous)
            # The server's returned snapshot/cursor is authoritative. Do not reset it.
        # Verified HAR invariant: workspace_session_id == conversation_id[len('cdx1_'):].
        metadata["codex_listen_snapshot"] = dumps(snapshot)
        messages = copy.deepcopy(messages)
        if continuation and messages and messages[0].get("role") == "system":
            messages.pop(0)
        elif self.initial_system and messages and messages[0].get("role") == "system":
            messages[0] = copy.deepcopy(self.initial_system)
        if self.editor_context:
            # The browser sends a separate JSON editor-context system message.
            # Keep its wire shape; the local history in the user payload remains authoritative.
            messages.insert(next((i for i, x in enumerate(messages) if x.get("role") == "user"), len(messages)),
                            copy.deepcopy(self.editor_context))
        body = {"input": messages, "metadata": metadata, "conversationId": conversation}
        if continuation:
            body["previousResponseId"] = continuation["response_id"]
        return body


class BackendText(str):
    """Text with private server-side continuation; never serialized to Codex."""
    def __new__(cls, text, continuation=None):
        obj = super().__new__(cls, text)
        obj.continuation = continuation
        return obj


def observed_remote_tools(data):
    progress = data.get("codex_live_progress") or {}
    if progress.get("toolCalls"):
        return True
    if any(e.get("payload_type") in ("function_call", "custom_tool_call", "web_search_call")
           for e in progress.get("eventPreviews", []) if isinstance(e, dict)):
        return True
    payload = (data.get("response") or {}).get("payload") or {}
    return any(x.get("type") in ("function_call", "custom_tool_call", "web_search_call")
               for x in payload.get("output", []) if isinstance(x, dict))


class PrismBackend:
    def __init__(self, template: SessionTemplate, *, timeout=300.0, poll=1.0, client=None, bootstrap=False, auth_state_file=None,
                 read_timeout=90.0, diagnostics=False):
        self.template, self.timeout, self.poll = template, timeout, poll
        if timeout <= 0 or read_timeout <= 0:
            raise ValueError("Timeouts must be positive.")
        self.read_timeout = read_timeout
        self.diagnostics = Diagnostics(diagnostics)
        normal_headers = {k: v for k, v in template.headers.items() if k.lower() != "cookie"}
        self.client = client or httpx.AsyncClient(base_url=ORIGIN, headers=normal_headers,
                                                 follow_redirects=False, timeout=httpx.Timeout(read_timeout, connect=10, pool=10), http2=True,
                                                 limits=httpx.Limits(max_connections=4))
        self.owns_client = client is None
        self.auth = PrismAuth(template.headers.get("Cookie", ""), expected_user=template.metadata.get("userId"),
                              state_file=auth_state_file) if self.owns_client else None
        if self.auth is not None:
            async def _pin_cf(request):
                cookie = request.headers.get("cookie") or ""
                pinned = self.auth.cf_pin_header(cookie)
                if pinned != cookie:
                    request.headers["cookie"] = pinned
            self.client.event_hooks["request"] = [_pin_cf]
        # One captured sandbox is shared; never run overlapping turns against it.
        self.lock = asyncio.Lock()
        self.needs_bootstrap = bootstrap
        self.sandbox_provisioned = False
        self.resource_expires_at = 0.0
        self.sandbox_session_id = None
        self.sandbox_was_replaced = False

    def _sandbox_binding(self):
        # Private process-local continuation fence; never exposed in model output.
        return hashlib.sha256(self.template.metadata["sandbox_token"].encode()).hexdigest()

    async def close(self):
        if self.owns_client:
            if self.auth and self.auth.ready:
                self.auth.persist(self.client)
            await self.client.aclose()

    async def _request(self, method, path, payload=None, *, headers=None, params=None):
        stage = operation_for(path)
        started = time.monotonic()
        self.diagnostics.emit(stage, "begin")
        if path == START:
            self.diagnostics.model_request_sent = True
        try:
            async with self.client.stream(method, path, json=payload, headers=headers, params=params) as r:
                if r.status_code in (401, 403):
                    error = BridgeError("Prism rejected the web session or sandbox credentials. Refresh the local Cookie/HAR.", "prism_auth_failed", 502)
                    error.http_status = r.status_code
                    raise error
                if not 200 <= r.status_code < 300:
                    error = BridgeError(f"Prism {stage} returned HTTP {r.status_code}; response body is withheld.", "prism_http_error", 502)
                    error.http_status = r.status_code
                    raise error
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 16 * 1024 * 1024:
                        raise BridgeError("Prism response exceeded 16 MiB.", "upstream_too_large", 502)
                try:
                    obj = loads(data)
                except (ValueError, TypeError, RecursionError):
                    raise BridgeError("Prism returned a non-JSON response.", "prism_protocol_error", 502) from None
                if not isinstance(obj, dict):
                    raise BridgeError("Prism returned an invalid envelope.", "prism_protocol_error", 502)
                if path == START and obj.get("status") == "started":
                    self.diagnostics.model_task_accepted = True
                if path in (START, STATUS):
                    raw_state = obj.get("status")
                    upstream_state = raw_state if raw_state in ("started", "pending", "completed", "stopped", "failed") else "unrecognized"
                    wrapper = obj.get("response") or {}
                    rejected = raw_state == "completed" and (not isinstance(wrapper, dict) or wrapper.get("status") != "success")
                    details = task_failure_details(wrapper) if rejected else {}
                    self.diagnostics.emit(stage, "response" if rejected else "ok",
                                          seconds=time.monotonic() - started, http_status=r.status_code,
                                          upstream_state=upstream_state, **details)
                else:
                    self.diagnostics.emit(stage, "ok", seconds=time.monotonic() - started, http_status=r.status_code)
                return obj
        except BridgeError as exc:
            raise self.diagnostics.fail(exc, stage, started, getattr(exc, "http_status", None))
        except TRANSPORT_ERRORS as exc:
            elapsed = time.monotonic() - started
            model_state = ("No model request was sent." if not self.diagnostics.model_request_sent else
                           "A model task was accepted; its result is not confirmed." if self.diagnostics.model_task_accepted else
                           "Model submission outcome is unknown; it was not automatically resubmitted.")
            message = f"Prism {stage} failed ({type(exc).__name__}) after {elapsed:.1f}s. {model_state}"
            error = BridgeError(message, "prism_transport_error", 502)
            raise self.diagnostics.fail(error, stage, started, error_class=type(exc).__name__) from None

    async def _post(self, path, payload, *, headers=None):
        # Polls are observations with an explicit cursor; reissuing the same poll
        # does not submit another model turn. Never retry START here.
        attempts = 3 if path == STATUS else 1
        auth_retried = False
        for attempt in range(attempts):
            try:
                return await self._request("POST", path, payload, headers=headers)
            except BridgeError as exc:
                if getattr(exc, "http_status", None) == 401 and self.auth and not auth_retried:
                    auth_retried = True
                    await self.auth.ensure(self.client, force=True)
                    return await self._request("POST", path, payload, headers=headers)
                if exc.code != "prism_transport_error" or attempt + 1 == attempts:
                    raise
                await asyncio.sleep(1 + attempt)

    async def _create_conversation(self):
        action = self.template.conversation_action
        if action is None:
            # Injected unit-test templates may omit the browser action. The CLI
            # requires it for live operation instead of guessing a registered ID.
            return None
        if self.auth is not None and self.auth.state_file is not None:
            # Server Action IDs are bound to the browser cookie set captured at
            # page load (CF fingerprint). Replay the original cookies verbatim
            # via a dedicated client: the shared client's request hook must not
            # overwrite them with refresh-rotated values.
            replay = self.auth.file_cookie_header()
            if replay:
                return await self._create_conversation_replay(action, replay)
        started = time.monotonic()
        stage = "conversation.create"
        self.diagnostics.emit(stage, "begin")
        try:
            async with self.client.stream("POST", action["url"], headers=action["headers"],
                                          content=dumps([self.template.metadata["projectId"]]).encode()) as r:
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise BridgeError("Conversation action response exceeded limit.", "prism_protocol_error", 502)
                if r.status_code != 200:
                    raise BridgeError("Prism conversation registration failed.", "conversation_registration_failed", 502)
                matches = re.findall(r'(?m)^\d+:"(cdx1_[0-9a-f-]{36})"\s*$', data.decode("utf-8", "replace"))
                if len(matches) != 1:
                    raise BridgeError("Prism action did not return a Codex conversation. A fresh HAR may be needed after a deployment.", "conversation_registration_failed", 502)
                self.diagnostics.emit(stage, "ok", seconds=time.monotonic() - started, http_status=r.status_code)
                return matches[0]
        except BridgeError as exc:
            raise self.diagnostics.fail(exc, stage, started)
        except TRANSPORT_ERRORS as exc:
            error = BridgeError(f"Prism {stage} failed ({type(exc).__name__}) after {time.monotonic()-started:.1f}s. No model request was sent.", "prism_transport_error", 502)
            raise self.diagnostics.fail(error, stage, started, error_class=type(exc).__name__) from None

    async def _create_conversation_replay(self, action, replay):
        started = time.monotonic()
        stage = "conversation.create"
        self.diagnostics.emit(stage, "begin")
        headers = dict(action["headers"])
        headers["Cookie"] = replay
        try:
            # Dedicated client: no auth hook, no cookie jar, exact replay.
            async with httpx.AsyncClient(http2=True, timeout=httpx.Timeout(self.read_timeout, connect=15),
                                         follow_redirects=False) as standalone:
                async with standalone.stream("POST", action["url"], headers=headers,
                                             content=dumps([self.template.metadata["projectId"]]).encode()) as r:
                    data = bytearray()
                    async for chunk in r.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1024 * 1024:
                            raise BridgeError("Conversation action response exceeded limit.", "prism_protocol_error", 502)
                    if r.status_code != 200:
                        raise BridgeError("Prism conversation registration failed.",
                                          "conversation_registration_failed", 502)
                    matches = re.findall(r'(?m)^\d+:"(cdx1_[0-9a-f-]{36})"\s*$', data.decode("utf-8", "replace"))
                    if len(matches) != 1:
                        raise BridgeError("Prism action did not return a Codex conversation. A fresh HAR may be needed after a deployment.",
                                          "conversation_registration_failed", 502)
                    self.diagnostics.emit(stage, "ok", seconds=time.monotonic() - started, http_status=r.status_code)
                    return matches[0]
        except BridgeError as exc:
            raise self.diagnostics.fail(exc, stage, started)
        except TRANSPORT_ERRORS as exc:
            error = BridgeError(f"Prism {stage} failed ({type(exc).__name__}) after {time.monotonic()-started:.1f}s. No model request was sent.",
                                "prism_transport_error", 502)
            raise self.diagnostics.fail(error, stage, started, error_class=type(exc).__name__) from None

    async def _initialize_sandbox(self):
        self.needs_bootstrap = True
        if not self.sandbox_provisioned:
            fresh = await self._post("/api/backend/1/new", None)
            if fresh.get("url", "").rstrip("/") != ORIGIN + "/s/sandboxes/proxy" or not isinstance(fresh.get("token"), str):
                raise BridgeError("Unexpected sandbox bootstrap response.", "prism_protocol_error", 502)
            self.template.metadata.update(sandbox_url=fresh["url"].rstrip("/") + "/", sandbox_token=fresh["token"])
            self.sandbox_provisioned = True
        metadata = self.template.metadata
        pid = metadata["projectId"]
        headers = {"x-crixet-sandbox-token": metadata["sandbox_token"]}
        y = await self._post("/api/y", {"docId": pid})
        resource = await self._post("/api/projects/" + quote(pid, safe="") + "/sandbox/resources-token",
                                    {"sandbox_session_id": self.sandbox_session_id, "sandbox_token": metadata["sandbox_token"]})
        if any(k not in y for k in ("url", "baseUrl", "docId", "token", "authorization")) or any(k not in resource for k in ("access_token", "resources_base_url")):
            raise BridgeError("Missing project synchronization credentials.", "prism_protocol_error", 502)
        expiry = resource.get("expires_at")
        self.resource_expires_at = float(expiry) if isinstance(expiry, (int, float)) else time.time() + 300
        # Credential registration is idempotent; start/conversation creation is not retried.
        for attempt in range(3):
            try:
                await self._post("/s/sandboxes/proxy/resources-token",
                                 {"token": resource["access_token"], "resourceBaseUrl": resource["resources_base_url"].rstrip("/") + "/", "projectId": pid}, headers=headers)
                break
            except BridgeError as exc:
                if attempt == 2 or exc.code != "prism_http_error":
                    raise
                await asyncio.sleep(2)
        await self._post("/s/sandboxes/proxy/token", {k: y[k] for k in ("url", "baseUrl", "docId", "token", "authorization")}, headers=headers)
        while True:  # Bounded by complete()'s absolute deadline.
            ready = await self._request("GET", "/s/sandboxes/proxy/wait-for-sync", headers=headers,
                                        params={"wait_ms": 10000, "prism_cache_bust": str(time.time_ns())})
            if ready.get("status") == "synced":
                self.needs_bootstrap = False
                return
            if ready.get("status") not in ("syncing", "pending", "waiting"):
                raise BridgeError("Sandbox did not report a recognized sync state.", "prism_sync_failed", 502)
            await asyncio.sleep(1)

    async def _stop(self, request_id, state, conversation):
        try:
            async with asyncio.timeout(5):
                await self._post(STOP, {"request_id": request_id, "turn_state": state,
                                        "conversation_id": conversation})
        except (BridgeError, TimeoutError):
            pass

    async def _check_sandbox(self, *, background=False):
        """Observe the sandbox before sending a model request; never execute tools."""
        stage = "sandbox.keepalive" if background else "sandbox.check"
        started = time.monotonic()
        self.diagnostics.emit(stage, "begin")
        try:
            async with self.client.stream("GET", HEARTBEAT,
                                          headers={"x-crixet-sandbox-token": self.template.metadata["sandbox_token"]},
                                          params={"prism_cache_bust": str(time.time_ns())}, timeout=min(20, self.read_timeout)) as r:
                size = 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > 8192:
                        raise BridgeError("Sandbox heartbeat exceeded its response limit; no body was logged.", "sandbox_probe_failed", 502)
                expired = r.headers.get("x-crixet-sandbox-expired", "").strip().lower() == "true"
                if r.status_code in (404, 410) or (r.status_code == 502 and expired):
                    self.diagnostics.emit(stage, "expired", seconds=time.monotonic()-started, http_status=r.status_code)
                    return {"alive": False, "session_id": None}
                if r.status_code != 200:
                    error = BridgeError(f"Sandbox heartbeat returned HTTP {r.status_code}; model submission is blocked until a successful check.", "sandbox_probe_failed", 502)
                    error.http_status = r.status_code
                    raise error
                self.diagnostics.emit(stage, "ok", seconds=time.monotonic()-started, http_status=200)
                session = r.headers.get("x-session-id")
                # Never log the opaque session value.
                return {"alive": True, "session_id": session if session and len(session) <= 256 else None}
        except BridgeError as exc:
            if background:
                self.diagnostics.emit(stage, "unavailable", seconds=time.monotonic()-started,
                                      http_status=getattr(exc, "http_status", None), error_code=exc.code)
                raise
            raise self.diagnostics.fail(exc, stage, started, getattr(exc, "http_status", None))
        except TRANSPORT_ERRORS as exc:
            error = BridgeError(f"Sandbox heartbeat failed ({type(exc).__name__}); model submission is blocked until a successful check.", "sandbox_probe_failed", 502)
            if background:
                self.diagnostics.emit(stage, "unavailable", seconds=time.monotonic()-started, error_class=type(exc).__name__)
                raise error from None
            raise self.diagnostics.fail(error, stage, started, error_class=type(exc).__name__) from None

    async def _prepare_sandbox(self, continuation):
        """Recover only before model submission, once, on positive expiry evidence."""
        if continuation:
            bound_elsewhere = continuation.get("sandbox_binding") not in (None, self._sandbox_binding())
            invalid_lifetime = self.sandbox_was_replaced and (
                not continuation.get("sandbox_binding") or not self.sandbox_provisioned)
            if bound_elsewhere or invalid_lifetime:
                raise BridgeError("This continuation belongs to an expired sandbox lifetime. No model request was sent; begin a fresh turn with full local history.", "sandbox_continuation_expired", 409)
        if self.needs_bootstrap:
            await self._initialize_sandbox()
        if not (self.template.conversation_action or self.sandbox_provisioned):
            return  # Injected protocol-only test backends have no sandbox lifecycle.
        probe = await self._check_sandbox()
        replaced = False
        if not probe["alive"]:
            self.sandbox_was_replaced = True
            self.sandbox_provisioned = False
            self.sandbox_session_id = None
            self.resource_expires_at = 0.0
            self.needs_bootstrap = True
            if continuation:
                raise BridgeError("The sandbox for this tool continuation expired. No model request was sent; begin a fresh turn with full local history.", "sandbox_continuation_expired", 409)
            self.diagnostics.emit("sandbox.recover", "begin")
            await self._initialize_sandbox()
            probe = await self._check_sandbox()
            if not probe["alive"]:
                self.sandbox_provisioned = False
                self.needs_bootstrap = True
                raise BridgeError("Replacement sandbox also failed its lifetime check; no model request was sent.", "sandbox_recovery_failed", 502)
            replaced = True
        session_changed = bool(self.sandbox_session_id and probe["session_id"] and self.sandbox_session_id != probe["session_id"])
        if probe["session_id"]:
            self.sandbox_session_id = probe["session_id"]
        if session_changed:
            self.sandbox_was_replaced = True
            self.needs_bootstrap = True
        if continuation:
            stale = (continuation.get("sandbox_binding") not in (None, self._sandbox_binding())
                     or (continuation.get("sandbox_session_id") and self.sandbox_session_id
                         and continuation["sandbox_session_id"] != self.sandbox_session_id)
                     or (not continuation.get("sandbox_binding") and self.sandbox_was_replaced))
            if stale or session_changed:
                raise BridgeError("This continuation belongs to a different sandbox lifetime. No model request was sent; begin a fresh turn with full local history.", "sandbox_continuation_expired", 409)
        if self.needs_bootstrap or (self.sandbox_provisioned and time.time()+60 >= self.resource_expires_at):
            await self._initialize_sandbox()
        if replaced:
            self.diagnostics.emit("sandbox.recover", "ok")

    async def _heartbeat(self):
        while True:
            # A synchronous check already ran before START. Keepalive must not
            # replace the sandbox or overwrite foreground diagnostic stages.
            await asyncio.sleep(10)
            try:
                await self._check_sandbox(background=True)
            except BridgeError:
                pass  # Observe only: do not reprovision or resubmit an active task.

    async def complete(self, messages, model, effort, *, continuation=None):
        request_id, state, conversation = None, None, None
        finished = False
        acquired = False
        heartbeat = None
        turn_started = time.monotonic()
        # This absolute deadline includes waiting for the shared sandbox lock.
        try:
            async with asyncio.timeout(self.timeout):
                await self.lock.acquire()
                acquired = True
                self.diagnostics.begin_turn()
                if self.auth:
                    await self._ensure_auth()
                await self._prepare_sandbox(continuation)
                if self.template.conversation_action:
                    heartbeat = asyncio.create_task(self._heartbeat())
                registered = continuation["conversation_id"] if continuation else await self._create_conversation()
                body = self.template.new_turn(messages, model, effort, registered, continuation)
                conversation = body["conversationId"]
                current = await self._post(START, body)
                request_id = current.get("request_id")
                state = current.get("turn_state")
                if current.get("status") != "completed" and (not isinstance(request_id, str) or not isinstance(state, dict)):
                    raise BridgeError("Prism start response lacks request_id/turn_state.", "prism_protocol_error", 502)
                while True:
                    if isinstance(current.get("turn_state"), dict):
                        state = current["turn_state"]
                    if observed_remote_tools(current):
                        raise BridgeError("Prism executed a native remote tool instead of the local-tool protocol. No local calls were forwarded.", "remote_tools_observed", 502)
                    status = current.get("status")
                    if status == "completed":
                        finished = True
                        wrapper = current.get("response") or {}
                        if not isinstance(wrapper, dict) or wrapper.get("status") != "success":
                            details = task_failure_details(wrapper)
                            code = details.get("upstream_status")
                            summary = f"upstream HTTP {code}" if code else details["upstream_category"]
                            error = BridgeError(f"Prism returned an application error ({summary}) inside an HTTP 200 response. The model request was not automatically resubmitted.", "prism_task_error", 502)
                            error.upstream_details = details
                            raise error
                        parts = [c["text"] for item in (wrapper.get("payload") or {}).get("output", [])
                                 if item.get("type") == "message" and item.get("role") == "assistant"
                                 for c in item.get("content", [])
                                 if c.get("type") == "output_text" and isinstance(c.get("text"), str)]
                        if not parts:
                            raise BridgeError("Prism completed without assistant text.", "prism_protocol_error", 502)
                        payload = wrapper.get("payload") or {}
                        snapshot = payload.get("codexListenSnapshot")
                        next_state = None
                        if isinstance(snapshot, dict) and isinstance(payload.get("id"), str):
                            next_state = {"conversation_id": conversation, "response_id": payload["id"],
                                          "snapshot": copy.deepcopy(snapshot), "sandbox_binding": self._sandbox_binding(),
                                          "sandbox_session_id": self.sandbox_session_id}
                        self.diagnostics.completed_turns += 1
                        self.diagnostics.emit("turn", "ok", seconds=time.monotonic()-turn_started)
                        self.diagnostics.current_stage = "idle"
                        return BackendText("\n".join(parts), next_state)
                    if status not in ("started", "pending"):
                        finished = True
                        raise BridgeError("Prism task stopped, failed or returned an unknown status.", "prism_task_error", 502)
                    await asyncio.sleep(self.poll)
                    current = await self._post(STATUS, {"request_id": request_id, "turn_state": state})
        except BridgeError as exc:
            if not getattr(exc, "stage", None):
                self.diagnostics.fail(exc, self.diagnostics.current_stage, turn_started)
            raise
        except TimeoutError:
            stage = self.diagnostics.current_stage if acquired else "queue"
            suffix = "No model request was sent." if not self.diagnostics.model_request_sent else "Model submission may have taken place."
            error = BridgeError(f"Prism turn exceeded its absolute deadline at {stage}. {suffix}", "prism_timeout", 504)
            raise self.diagnostics.fail(error, stage, turn_started, error_class="TimeoutError") from None
        finally:
            if acquired:
                try:
                    if request_id and not finished:
                        task = asyncio.create_task(self._stop(request_id, state, conversation))
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            await task
                finally:
                    if heartbeat:
                        heartbeat.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat
                    self.lock.release()

    async def _ensure_auth(self):
        started = time.monotonic()
        self.diagnostics.emit("auth.session", "begin")
        try:
            summary = await self.auth.ensure(self.client)
            self.diagnostics.emit("auth.session", "ok", seconds=time.monotonic()-started)
            return summary
        except BridgeError as exc:
            raise self.diagnostics.fail(exc, "auth.session", started)

    async def preflight(self, *, provision=False, deadline=120):
        """Read-only account/project checks; optional sandbox acquisition, no model."""
        started = time.monotonic()
        self.diagnostics.begin_turn()
        checks = {}
        try:
            async with asyncio.timeout(deadline):
                async with self.lock:
                    if self.auth:
                        auth = await self._ensure_auth()
                        checks["signed_in"] = auth["signed_in"]
                    project = await self._request("GET", "/api/project-access", params={"d": self.template.metadata["projectId"]})
                    checks["project_accessible"] = project.get("accessible") is True
                    if not checks["project_accessible"]:
                        raise BridgeError("Captured project is not accessible to this session.", "project_access_denied", 403)
                    if provision:
                        self.needs_bootstrap = True
                        await self._prepare_sandbox(None)
                        checks["sandbox_synced"] = True
            return {"ok": True, "checks": checks, "model_request_sent": False, "diagnostics": self.diagnostics.snapshot()}
        except TimeoutError:
            stage = self.diagnostics.current_stage
            error = BridgeError(f"Diagnostic deadline reached at {stage}; no model request was sent.", "diagnostic_timeout", 504)
            self.diagnostics.fail(error, stage, started, error_class="TimeoutError")
            return {"ok": False, "checks": checks, "error": error.payload(), "model_request_sent": False,
                    "diagnostics": self.diagnostics.snapshot()}
        except BridgeError as exc:
            return {"ok": False, "checks": checks, "error": exc.payload(), "model_request_sent": False,
                    "diagnostics": self.diagnostics.snapshot()}
