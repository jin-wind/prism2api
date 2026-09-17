"""Bounded credential-free operational diagnostics (never log request bodies)."""
from collections import deque
from datetime import datetime, timezone
import json
import re
import sys
import time


def task_failure_details(wrapper):
    """Project arbitrary upstream text onto a small, credential-free vocabulary."""
    payload = wrapper.get("payload") if isinstance(wrapper, dict) else None
    payload = payload if isinstance(payload, dict) else {}
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
            **({"upstream_status": status} if status is not None else {})}


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
             upstream_state=None, upstream_reason=None, upstream_category=None, upstream_status=None):
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
                           ("upstream_category", upstream_category), ("upstream_status", upstream_status)):
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
