"""Run the real Codex CLI against a synthetic, loopback-only model backend.

No Prism calls or account credentials. Uses a temporary CODEX_HOME/workspace.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn

from prism_bridge.app import create_app
from prism_bridge.protocol import PROTOCOL, dumps


class SyntheticBackend:
    def __init__(self, mode):
        self.mode = mode
        self.seen = []
        self.result_seen = False
        self.result_verified = False
        self.result_preview = None
        self.selected = None

    async def close(self):
        pass

    async def complete(self, messages, model, effort):
        req = json.loads(messages[1]["content"][0]["text"])["bridge_request"]
        self.seen.append({"tools": [(t["type"], t["name"]) for t in req["tools"]],
                          "input_types": [i.get("type", "message") for i in req["history"]]})
        result = next((i for i in reversed(req["history"])
                       if i.get("type") in ("function_call_output", "custom_tool_call_output")), None)
        self.result_seen = result is not None
        if result:
            rendered = dumps(result.get("output"))
            self.result_preview = rendered[:1000]
            self.result_verified = ("current_time" in rendered if self.mode == "custom" and self.selected == "functions.exec"
                                    else "error" not in rendered.lower())
        calls = []
        text = "bridge-e2e-ok"
        if self.mode != "text" and not self.result_seen:
            text = ""
            if self.mode == "custom":
                patch = "*** Begin Patch\n*** Add File: bridge-marker.txt\n+bridge-e2e-ok\n*** End Patch"
                tool = next(t for t in req["tools"] if t["type"] == "custom" and t["name"] in ("apply_patch", "functions.exec"))
                self.selected = tool["name"]
                raw = "text(await tools.clock__curr_time({}));" if tool["name"] == "functions.exec" else patch
                calls = [{"name": tool["name"], "input": raw}]
            else:
                tool = next(t for t in req["tools"] if t["type"] == "function" and t["name"] in ("update_plan", "clock.sleep"))
                self.selected = tool["name"]
                arguments = {"duration_ms": 1} if tool["name"] == "clock.sleep" else {"plan": [{"step": "Verify local tool dispatch", "status": "completed"}]}
                calls = [{"name": tool["name"], "arguments": arguments}]
        return dumps({"protocol": PROTOCOL, "nonce": req["nonce"], "text": text, "calls": calls})


def run(executable, mode):
    key = "synthetic-codex-smoke-key-no-real-credentials"
    backend = SyntheticBackend(mode)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(backend, key)
    captured = []
    @app.middleware("http")
    async def capture_test_request(request, call_next):
        if request.method == "POST":
            try:
                value = json.loads(await request.body())
                captured.append({"keys": list(value), "input_types": [i.get("type", "message")
                                                                       for i in value.get("input", []) if isinstance(i, dict)]})
            except (ValueError, TypeError):
                pass
        return await call_next(request)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise TimeoutError("Local test server did not start")
        time.sleep(.02)
    try:
        scratch = Path(__file__).resolve().parents[1] / ".local" / "smoke"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="prism-codex-smoke-", dir=scratch) as temp:
            # Verify the owned cleanup target before TemporaryDirectory removes it.
            if not Path(temp).resolve().is_relative_to(scratch.resolve()):
                raise RuntimeError("Unexpected temporary workspace path")
            home = Path(temp) / "home"
            workspace = Path(temp) / "workspace"
            home.mkdir()
            workspace.mkdir()
            (home / "config.toml").write_text(f'''model = "gpt-6-astra"
model_provider = "prism_bridge"
web_search = "disabled"
model_supports_reasoning_summaries = false
[model_providers.prism_bridge]
name = "Synthetic loopback test"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
env_key = "PRISM_BRIDGE_API_KEY"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
''', encoding="utf-8")
            env = {**os.environ, "CODEX_HOME": str(home), "PRISM_BRIDGE_API_KEY": key}
            command = [executable, "exec", "--skip-git-repo-check", "--ephemeral", "--json", "--color", "never",
                       "--sandbox", "workspace-write", "-C", str(workspace),
                       "-c", 'approval_policy="never"', "-c", "features.multi_agent=false",
                       "Protocol smoke test: execute only the synthetic tool request then finish."]
            proc = subprocess.run(command, env=env, capture_output=True, stdin=subprocess.DEVNULL, text=True,
                                  encoding="utf-8", errors="replace", timeout=90)
            marker = workspace / "bridge-marker.txt"
            ok = proc.returncode == 0 and "bridge-e2e-ok" in proc.stdout
            if mode != "text":
                ok = ok and backend.result_seen and backend.result_verified
            if mode == "custom" and backend.selected == "apply_patch":
                ok = ok and marker.exists() and marker.read_text().strip() == "bridge-e2e-ok"
            report = {"mode": mode, "ok": ok, "exit_code": proc.returncode,
                      "request_count": len(backend.seen), "tool": backend.selected,
                      "tool_result_seen": backend.result_seen, "marker_written": marker.exists()}
            report["tool_result_verified"] = backend.result_verified
            print(json.dumps(report))
            if not ok:
                print("RAW_SHAPES", json.dumps(captured)[:16000])
                print("REQUEST_SHAPES", json.dumps(backend.seen))
                print("RESULT_PREVIEW", backend.result_preview)
                print("STDOUT", proc.stdout[-6000:])
                print("STDERR", proc.stderr[-4000:])
            return ok
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", required=True)
    parser.add_argument("--mode", choices=["text", "function", "custom", "all"], default="all")
    args = parser.parse_args()
    modes = ["text", "function", "custom"] if args.mode == "all" else [args.mode]
    sys.exit(0 if all([run(args.codex, mode) for mode in modes]) else 1)
