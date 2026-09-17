"""Bounded, credential-free bridge traffic recorder for the web UI.

Records one entry per Codex-facing turn (endpoint, model, duration, tool-call
names, error classification). Never stores prompts, tool arguments, outputs,
cookies, or tokens.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from itertools import count


def _tool_names(output) -> list[str]:
    names = []
    for item in output or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("function_call", "custom_tool_call"):
            name = item.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


class TrafficRecorder:
    def __init__(self, max_items: int = 200):
        self.turns = deque(maxlen=max_items)
        self._sequence = count(1)
        self.started_at = time.time()
        self.total = 0
        self.failed = 0

    def begin(self, endpoint: str, model: str, *, stream: bool = False) -> dict:
        record = {
            "seq": next(self._sequence),
            "at": datetime.now(timezone.utc).isoformat(),
            "endpoint": endpoint,
            "model": model,
            "stream": stream,
            "status": "in_progress",
            "_started": time.monotonic(),
        }
        self.turns.append(record)
        self.total += 1
        return record

    def finish(self, record: dict, *, output=None, error=None):
        record["duration_s"] = round(time.monotonic() - record.pop("_started"), 3)
        if error is not None:
            record["status"] = "failed"
            # BridgeError payloads carry a code + human message only.
            record["error"] = {"code": error.get("code"), "message": error.get("message")} \
                if isinstance(error, dict) else {"code": "internal_error", "message": str(error)[:200]}
            self.failed += 1
        else:
            record["status"] = "ok"
            tools = _tool_names(output)
            if tools:
                record["tool_calls"] = tools
            record["output_items"] = len(output or [])

    def snapshot(self) -> dict:
        turns = []
        for record in self.turns:
            item = {k: v for k, v in record.items() if not k.startswith("_")}
            turns.append(item)
        turns.reverse()  # newest first for the UI
        return {"started_at": self.started_at, "total": self.total,
                "failed": self.failed, "turns": turns}
