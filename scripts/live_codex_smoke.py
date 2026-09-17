"""Opt-in real Prism + real Codex CLI read-only tool round-trip tests.

Cookie stays in a local file. No credentials or upstream metadata enter reports.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn

from prism_bridge.app import create_app
from prism_bridge.protocol import BridgeError, loads
from prism_bridge.upstream import PrismBackend, SessionTemplate


class ObservedBackend(PrismBackend):
    def __init__(self, *args, mode, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode
        self.rounds = 0
        self.calls = []
        self.tool_result_seen = False
        self.tool_result_verified = False

    async def _post(self, path, payload, **kwargs):
        started = time.monotonic()
        try:
            result = await super()._post(path, payload, **kwargs)
        except BridgeError as exc:
            print(json.dumps({"event": "prism_request_failed", "operation": path.rsplit("/", 1)[-1],
                              "code": exc.code, "seconds": round(time.monotonic() - started, 2)}), flush=True)
            raise
        if path.endswith(("response_with_tools_start", "response_with_tools_status")):
            print(json.dumps({"event": "prism_request_completed", "operation": path.rsplit("/", 1)[-1],
                              "status": result.get("status"), "seconds": round(time.monotonic() - started, 2)}), flush=True)
        if (result.get("response") or {}).get("status") == "error":
            error = result["response"].get("payload") or {}
            message = re.sub(r"eyJ[\w.-]+|https?://[^\s]+|[0-9a-f]{8}-[0-9a-f-]{27,}", "<redacted>", str(error.get("message", "")))
            print(json.dumps({"event": "prism_application_error", "reason": error.get("reason"), "message": message[:600]}), flush=True)
        return result

    async def complete(self, messages, model, effort, *, continuation=None):
        request = loads(messages[-1]["content"][0]["text"])["bridge_request"]
        self.rounds += 1
        result = next((x for x in reversed(request["history"])
                       if x.get("type") in ("function_call_output", "custom_tool_call_output")), None)
        if result:
            self.tool_result_seen = True
            output = json.dumps(result.get("output"), ensure_ascii=False)
            self.tool_result_verified = ("current_time" in output if self.mode == "custom"
                                         else "error" not in output.lower())
        print(json.dumps({"event": "live_round_started", "mode": self.mode, "round": self.rounds,
                          "has_local_tool_result": bool(result), "native_continuation": bool(continuation)}), flush=True)
        started = time.monotonic()
        answer = await super().complete(messages, model, effort, continuation=continuation)
        clean = answer.strip()
        if clean.startswith("```json\n") and clean.endswith("\n```"):
            clean = clean[8:-4]
        try:
            parsed = loads(clean)
        except (ValueError, TypeError):
            raise BridgeError("Live test received a non-protocol answer.", "live_test_protocol_error", 502) from None
        calls = parsed.get("calls", []) if isinstance(parsed, dict) else []
        # Test-only effect boundary: no shell, edits, web searches or sub-agents.
        for call in calls:
            name = call.get("name")
            allowed = self.mode == "function" and name == "clock.sleep" and call.get("arguments") == {"duration_ms": 1}
            if self.mode == "custom" and name == "functions.exec":
                allowed = re.sub(r"\s+", "", call.get("input", "")).rstrip(";") == "text(awaittools.clock__curr_time({}))"
            if not allowed:
                raise BridgeError("Prism requested an action outside this read-only test's exact tool allowlist.", "live_test_tool_mismatch", 502)
            self.calls.append(name)
        print(json.dumps({"event": "live_round_completed", "mode": self.mode, "round": self.rounds,
                          "seconds": round(time.monotonic() - started, 2),
                          "calls": [x.get("name") for x in calls], "text_characters": len(parsed.get("text", ""))}), flush=True)
        return answer


def run(args, mode):
    template = SessionTemplate.from_har(args.har, args.cookie_file,
                                        allow_missing_cookie=bool(args.auth_state and Path(args.auth_state).exists()))
    if not template.conversation_action:
        raise RuntimeError("HAR lacks the registered create-chat action")
    if args.sandbox_state:
        state = json.loads(Path(args.sandbox_state).read_text(encoding="utf-8"))
        template.metadata.update(sandbox_url=state["url"], sandbox_token=state["token"])
    backend = ObservedBackend(template, mode=mode, timeout=240, poll=2, bootstrap=True, auth_state_file=args.auth_state)
    if args.sandbox_state:
        backend.sandbox_provisioned = True  # Reuse sandbox, renew project/Y credentials.
    key = secrets.token_hex(32)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(backend, key), access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Local bridge failed to start")
        time.sleep(.02)
    started = time.monotonic()
    try:
        scratch = Path(__file__).resolve().parents[1] / ".local" / "live-smoke"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=scratch) as temp:
            root = Path(temp).resolve()
            if not root.is_relative_to(scratch.resolve()):
                raise RuntimeError("Unexpected cleanup target")
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            profile = (Path(__file__).resolve().parents[1] / "prism_bridge.config.toml").read_text(encoding="utf-8")
            profile = profile.replace("127.0.0.1:8765", f"127.0.0.1:{port}")
            (home / "prism_bridge.config.toml").write_text(profile, encoding="utf-8")
            prompts = {
                "text": "Reply exactly prism-live-text-ok. Do not call any tools.",
                "function": "Local tool plumbing test. Call clock.sleep with duration_ms exactly 1, exactly once. After receiving its actual tool result, reply exactly prism-live-function-ok. Do not invoke any other tool, inspect files, edit files, run shell commands, search, or create agents.",
                "custom": "Local custom-tool plumbing test. Call functions.exec exactly once with this exact JavaScript: text(await tools.clock__curr_time({})); After receiving its actual tool result, report the returned current_time and the marker prism-live-custom-ok. Do not invoke any other tool, inspect or edit files, run shell commands, search, or create agents.",
            }
            command = [args.codex, "exec", "--strict-config", "--profile", "prism_bridge", "--skip-git-repo-check",
                       "--ephemeral", "--json", "--color", "never", "--sandbox", "read-only", "-C", str(workspace),
                       "-c", 'approval_policy="never"', prompts[mode]]
            env = {**os.environ, "CODEX_HOME": str(home), "PRISM_BRIDGE_API_KEY": key}
            proc = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=510)
            messages, errors = [], []
            for line in proc.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                    messages.append(event["item"].get("text", ""))
                if event.get("type") in ("error", "turn.failed"):
                    errors.append(event.get("message") or event.get("error", {}).get("message", ""))
            expected = f"prism-live-{mode}-ok"
            final_ok = any(expected in msg for msg in messages)
            passed = proc.returncode == 0 and final_ok
            if mode != "text":
                passed = passed and backend.tool_result_seen and backend.tool_result_verified and len(backend.calls) == 1
            report = {"mode": mode, "live_prism": True, "ok": bool(passed), "exit_code": proc.returncode,
                      "rounds": backend.rounds, "calls": backend.calls,
                      "tool_result_seen": backend.tool_result_seen, "tool_result_verified": backend.tool_result_verified,
                      "final_marker_seen": final_ok, "seconds": round(time.monotonic() - started, 2)}
            if errors:
                report["errors"] = errors[:2]
            elif proc.returncode != 0:
                report["cli_diagnostic"] = re.sub(r"eyJ[\w.-]+", "<redacted>", proc.stderr[-1500:])
            # Only the controlled final marker/time is surfaced, never prompts/metadata.
            if mode == "custom":
                report["returned_utc_time_seen"] = any(re.search(r"\d{4}-\d{2}-\d{2}.*\d{2}:\d{2}", msg) for msg in messages)
            print(json.dumps(report), flush=True)
            return report
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", required=True)
    parser.add_argument("--har", required=True)
    parser.add_argument("--cookie-file")
    parser.add_argument("--auth-state")
    parser.add_argument("--sandbox-state", help="Optional private JSON from an already synchronized sandbox (url/token)")
    parser.add_argument("--mode", choices=["text", "function", "custom", "all"], default="all")
    args = parser.parse_args()
    modes = ["text", "function", "custom"] if args.mode == "all" else [args.mode]
    results = [run(args, mode) for mode in modes]
    dest = Path(__file__).resolve().parents[1] / ".local" / ("live-result-" + args.mode + ".json")
    dest.write_text(json.dumps(results, indent=2), encoding="utf-8")
    sys.exit(0 if all(result["ok"] for result in results) else 1)
