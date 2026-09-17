"""Bounded credential-free operational diagnostics (never log request bodies)."""
from collections import deque
from datetime import datetime, timezone
import json
import re
import sys
import time


SAFE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
# Split an identifier into words at _ - and lowercase->uppercase boundaries, so
# both project_edit_access_required and sandboxUnavailable read as a few words.
WORDS = re.compile(r"[A-Za-z][a-z0-9]*|[0-9]+")


def safe_reason(value):
    """Pass through an upstream reason code only if it reads as a few short words.

    Credential material fails this: a JWT or opaque id fragments into many runs
    once split on case boundaries, and an API key has one run far longer than a
    word. Nothing here is a substitute for not echoing free-form text at all,
    which is why the upstream message is still dropped entirely.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value):
        return None
    words = WORDS.findall(value)
    if not 1 <= len(words) <= 6 or any(len(w) > 16 for w in words):
        return None
    # Reassembling must account for every character, so separators are all that
    # can be dropped; anything else means the value was not word-shaped.
    return value if len("".join(words)) == len(value.replace("_", "").replace("-", "")) else None


def safe_int(value, limit=100000):
    """Pass through a bounded integer, which cannot carry credential material."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= limit else None


def task_failure_details(wrapper):
    """Project arbitrary upstream text onto a small, credential-free vocabulary.

    The mapped category stays stable for callers, but the raw reason code and the
    response's key names are carried alongside it: without them an unmapped
    failure is indistinguishable from every other unmapped failure, which makes
    a live incident impossible to diagnose.
    """
    payload = wrapper.get("payload") if isinstance(wrapper, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    raw_reason = safe_reason(payload.get("reason"))
    # Key names describe the response shape, never its contents, and are what
    # reveals an upstream schema change. The payload level is included because a
    # failure carrying no "reason" at all is itself the diagnosis.
    def keys(obj, prefix=""):
        return [prefix + k for k in (list(obj) if isinstance(obj, dict) else [])
                if isinstance(k, str) and SAFE_KEY.fullmatch(k)]

    shape = sorted(keys(wrapper) + keys(payload, "payload."))[:20]
    task_status = safe_reason(wrapper.get("status") if isinstance(wrapper, dict) else None)
    root_cause = safe_reason(payload.get("rootCause") or payload.get("root_cause"))
    http_status = safe_int(payload.get("httpStatus") or payload.get("http_status"), limit=599)
    reason = payload.get("reason")
    reason = reason if reason in ("sandbox_reconnecting", "conversation_too_large",
                                  "project_edit_access_required", "unknown") else "unknown"
    message = payload.get("message")
    message = message if isinstance(message, str) else ""
    category = reason
    status = None
    if "legacy conversation" in message.lower() and "read-only" in message.lower():
        category = "legacy_conversation"
    match = re.search(r"Error while processing conversation \((500 Internal Server Error|502 Bad Gateway|503 Service Unavailable|504 Gateway Timeout)\)", message)
    if match:
        status = int(match.group(1)[:3])
        category = "upstream_service_error"
    return {"upstream_reason": reason, "upstream_category": category,
            **({"upstream_status": status} if status is not None else {}),
            **({"upstream_reason_raw": raw_reason} if raw_reason and raw_reason != reason else {}),
            **({"upstream_task_status": task_status} if task_status else {}),
            **({"upstream_root_cause": root_cause} if root_cause else {}),
            **({"upstream_http_status": http_status} if http_status is not None else {}),
            **({"upstream_shape": shape} if shape else {})}


def operation_for(path):
    if path.startswith("/api/projects/") and path.endswith("/sandbox/resources-token"):
        return "project.resources.issue"
    return {
        "/api/backend/1/new": "sandbox.acquire",
        "/api/y": "project.sync.credentials",
        "/s/sandboxes/proxy/resources-token": "sandbox.resources.register",
        "/s/sandboxes/proxy/token": "sandbox.sync.register",
        "/s/sandboxes/proxy/wait-for-sync": "sandbox.sync.wait",
        "/s/sandboxes/proxy/heartbeat": "sandbox.heartbeat",
        "/api/llm/response_with_tools_start": "model.start",
        "/api/llm/response_with_tools_status": "model.poll",
        "/api/llm/response_with_tools_stop": "model.stop",
        "/auth/session": "auth.session",
        "/api/project-access": "project.access",
    }.get(path, "upstream.request")


class Diagnostics:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.events = deque(maxlen=40)
        self.current_stage = "idle"
        self.last_error = None
        self.model_request_sent = False
        self.model_task_accepted = False
        self.completed_turns = 0

    def begin_turn(self):
        self.model_request_sent = False
        self.model_task_accepted = False
        self.last_error = None
        self.emit("turn", "begin")

    def emit(self, stage, phase, *, seconds=None, http_status=None, error_code=None, error_class=None,
             upstream_state=None, upstream_reason=None, upstream_category=None, upstream_status=None,
             upstream_reason_raw=None, upstream_shape=None, upstream_task_status=None,
             upstream_root_cause=None, upstream_http_status=None):
        event = {"at": datetime.now(timezone.utc).isoformat(), "stage": stage, "phase": phase}
        if seconds is not None:
            event["seconds"] = round(seconds, 3)
        if http_status is not None:
            event["http_status"] = http_status
        if error_code is not None:
            event["error_code"] = error_code
        if error_class is not None:
            event["error_class"] = error_class
        for key, value in (("upstream_state", upstream_state), ("upstream_reason", upstream_reason),
                           ("upstream_category", upstream_category), ("upstream_status", upstream_status),
                           ("upstream_reason_raw", upstream_reason_raw), ("upstream_shape", upstream_shape),
                           ("upstream_task_status", upstream_task_status),
                           ("upstream_root_cause", upstream_root_cause),
                           ("upstream_http_status", upstream_http_status)):
            if value is not None:
                event[key] = value
        self.events.append(event)
        if phase == "begin" and stage not in ("model.stop", "sandbox.keepalive"):
            self.current_stage = stage
        if phase == "failed" and stage != "sandbox.keepalive":
            self.last_error = dict(event)
        if self.enabled:
            print("[prism-bridge] " + json.dumps(event), file=sys.stderr, flush=True)

    def fail(self, error, stage, started, http_status=None, error_class=None):
        elapsed = time.monotonic() - started
        error.stage = stage
        error.elapsed_seconds = round(elapsed, 3)
        self.emit(stage, "failed", seconds=elapsed, http_status=http_status,
                  error_code=error.code, error_class=error_class,
                  **getattr(error, "upstream_details", {}))
        return error

    def snapshot(self):
        return {"current_stage": self.current_stage, "model_request_sent": self.model_request_sent,
                "model_task_accepted": self.model_task_accepted, "completed_turns": self.completed_turns,
                "last_error": self.last_error, "events": list(self.events)}
