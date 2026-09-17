import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from prism_bridge.app import create_app
from prism_bridge.protocol import (PROTOCOL, BridgeError, MemoryStore, ToolContinuationStore, dumps, normalize, make_prompt,
                                   output_events, parse_answer, response)
from prism_bridge.upstream import (ORIGIN, START, STATUS, STOP, PrismBackend, BackendText,
                                   SessionTemplate)

KEY = "test-bridge-secret-at-least-16-chars"
FUNCTION = {"type": "function", "name": "exec_command", "parameters": {
    "type": "object", "properties": {"cmd": {"type": "string"}},
    "required": ["cmd"], "additionalProperties": False}}
CUSTOM = {"type": "custom", "name": "apply_patch", "format": {"type": "text"}}


def envelope(calls=None, text="", nonce="n"):
    return dumps({"protocol": PROTOCOL, "nonce": nonce, "text": text, "calls": calls or []})


class FakeBackend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def complete(self, messages, model, effort):
        request = json.loads(messages[1]["content"][0]["text"])["bridge_request"]
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return envelope(nonce=request["nonce"], **reply)

    async def close(self):
        pass


def client(backend, **kwargs):
    return TestClient(create_app(backend, KEY, **kwargs), base_url="http://localhost",
                      headers={"Authorization": "Bearer " + KEY})


def test_full_function_round_trip():
    backend = FakeBackend([
        {"calls": [{"name": "exec_command", "arguments": {"cmd": "echo hello"}}]},
        {"text": "hello"},
    ])
    with client(backend) as c:
        first = c.post("/v1/responses", json={"model": "gpt-6-astra", "input": "test", "tools": [FUNCTION]}).json()
        call = first["output"][0]
        assert call["type"] == "function_call"
        assert json.loads(call["arguments"]) == {"cmd": "echo hello"}
        second = c.post("/v1/responses", json={
            "previous_response_id": first["id"], "tools": [FUNCTION],
            "input": [{"type": "function_call_output", "call_id": call["call_id"], "output": "hello"}]
        })
        assert second.status_code == 200
        assert second.json()["output"][0]["content"][0]["text"] == "hello"
        assert backend.requests[1]["history"][-1]["output"] == "hello"
        assert c.get("/v1/responses/" + first["id"]).status_code == 200


def test_custom_tool_sse_and_result_round_trip():
    patch = "*** Begin Patch\n*** Add File: hi.txt\n+hello\n*** End Patch"
    backend = FakeBackend([{"calls": [{"name": "apply_patch", "input": patch}]}, {"text": "done"}])
    with client(backend) as c:
        r = c.post("/v1/responses", json={"input": "patch", "tools": [CUSTOM], "stream": True})
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        assert [e["sequence_number"] for e in events] == list(range(len(events)))
        assert events[0]["type"] == "response.created"
        assert any(e["type"] == "response.custom_tool_call_input.delta" and e["delta"] == patch for e in events)
        final = events[-1]
        assert final["type"] == "response.completed"
        result = final["response"]
        item = result["output"][0]
        assert item["input"] == patch
        r = c.post("/v1/responses", json={"previous_response_id": result["id"], "tools": [CUSTOM],
                   "input": [{"type": "custom_tool_call_output", "call_id": item["call_id"], "output": "applied"}]})
        assert r.status_code == 200
        assert backend.requests[1]["history"][-1]["type"] == "custom_tool_call_output"


def test_stream_errors_are_failed_not_completed():
    with client(FakeBackend([BridgeError("upstream failed", "test_error", 502)])) as c:
        r = c.post("/v1/responses", json={"input": "test", "stream": True})
        assert "response.failed" in r.text
        assert "response.completed" not in r.text
        assert "test_error" in r.text


def test_http_upstream_error():
    with client(FakeBackend([BridgeError("upstream failed", "test_error", 502)])) as c:
        assert c.post("/v1/responses", json={"input": "test"}).status_code == 502


def test_auth_origin_and_store_false():
    with client(FakeBackend([{"text": "ok"}])) as c:
        assert c.get("/health", headers={"Authorization": "wrong"}).status_code == 401
        assert c.get("/health", headers={"Origin": "https://example.com"}).status_code == 403
        assert c.get("/health").json()["upstream_verified"] is False
        result = c.post("/v1/responses", json={"input": "test", "store": False}).json()
        assert c.get("/v1/responses/" + result["id"]).status_code == 404


@pytest.mark.parametrize("body", [
    {"tools": [{"type": "web_search"}]},
    {"tools": [FUNCTION, FUNCTION]},
    {"tool_choice": "required"},
    {"input": [{"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]},
    {"input": [{"type": "function_call_output", "call_id": "missing", "output": "x"}]},
    {"input": [{"type": "reasoning", "encrypted_content": "opaque"}]},
    {"background": True},
    {"stream": "true"},
    {"reasoning": {"effort": "invented"}},
    {"tool_choice": {"type": "function", "name": "missing"}},
    {"input": [{"type": "compaction", "encrypted_content": "opaque"}]},
])
def test_unsupported_inputs_fail_before_upstream(body):
    backend = FakeBackend([])
    with client(backend) as c:
        assert c.post("/v1/responses", json=body).status_code == 400
        assert not backend.requests


@pytest.mark.parametrize("answer,body", [
    (envelope([{"name": "missing", "arguments": {}}]), {}),
    (envelope([{"name": "exec_command", "arguments": {"cmd": 42}}]), {}),
    (envelope([{"name": "exec_command", "arguments": {"cmd": "a", "surprise": True}}]), {}),
    (envelope([{"name": "exec_command", "arguments": {"cmd": "a"}}]), {"tool_choice": "none"}),
    (envelope(text="finished"), {"tool_choice": "required"}),
    (envelope(text="finished", nonce="wrong"), {}),
    ('{"protocol":"a","protocol":"b"}', {}),
    ("not JSON", {}),
    (envelope([{"name": "exec_command", "arguments": {"cmd": "a"}}] * 2), {"parallel_tool_calls": False}),
])
def test_bad_tool_protocol_is_not_forwarded(answer, body):
    with pytest.raises(BridgeError) as exc:
        parse_answer(answer, {FUNCTION["name"]: FUNCTION}, "n", body)
    assert exc.value.status == 502


def test_function_sse_arguments_and_final_message():
    items = parse_answer(envelope([{"name": "exec_command", "arguments": {"cmd": "x"}}], text="checking"),
                         {"exec_command": FUNCTION}, "n", {})
    events = list(output_events(response("gpt-6-astra", output=items)))
    assert any(e["type"] == "response.function_call_arguments.done" for e in events)
    assert items[0]["phase"] == "commentary"
    assert events[-1]["response"]["usage"] is None


def test_store_eviction_and_expiry():
    store = MemoryStore(max_items=1)
    first, second = response("model"), response("model")
    store.put(first, [])
    store.put(second, [])
    with pytest.raises(BridgeError):
        store.get(first["id"])
    assert store.get(second["id"])[0]["id"] == second["id"]
    store.ttl = -1
    with pytest.raises(BridgeError):
        store.get(second["id"])


def template():
    return SessionTemplate({"projectId": "test-project", "userId": "test-user",
                            "sandbox_url": ORIGIN + "/s/sandboxes/proxy", "sandbox_token": "FAKE",
                            "codex_listen_snapshot": dumps({"workspace_session_id": "test-workspace"})}, {})


def test_upstream_poll_updates_state_and_never_reuses_conversation():
    async def run():
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append((request.url.path, body))
            if request.url.path == START:
                return httpx.Response(200, json={"status": "started", "request_id": "r", "turn_state": {"v": 1}})
            if len(requests) == 2:
                assert body["turn_state"] == {"v": 1}
                return httpx.Response(200, json={"status": "pending", "turn_state": {"v": 2}})
            assert body["turn_state"] == {"v": 2}
            return httpx.Response(200, json={"status": "completed", "response": {"status": "success", "payload": {
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}}})
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(template(), client=http, poll=0)
            assert await backend.complete([], "gpt-6-astra", "medium") == "ok"
            assert [x[0] for x in requests] == [START, STATUS, STATUS]
            first = requests[0][1]
            second = template().new_turn([], "gpt-6-astra", "medium")
            assert first["conversationId"] != second["conversationId"]
            snapshot = json.loads(first["metadata"]["codex_listen_snapshot"])
            assert snapshot["codex_session_id"] is None
            assert first["conversationId"].startswith("cdx1_")
            assert snapshot["workspace_session_id"] == first["conversationId"][5:]
    asyncio.run(run())


@pytest.mark.parametrize("mode,code", [("timeout", "prism_timeout"), ("native", "remote_tools_observed"),
                                     ("auth", "prism_auth_failed"), ("transport", "prism_transport_error")])
def test_upstream_stop_and_no_start_retry(mode, code):
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == STOP:
                return httpx.Response(200, json={"status": "stopped"})
            if mode == "auth":
                return httpx.Response(403, text="private-cookie-value")
            if mode == "transport":
                raise httpx.ReadError("private-url-and-token")
            value = {"status": "pending", "request_id": "r", "turn_state": {"v": 1}}
            if mode == "native":
                value["codex_live_progress"] = {"toolCalls": [{"name": "exec_command"}]}
            return httpx.Response(200, json=value)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(template(), client=http, timeout=.02, poll=.05)
            with pytest.raises(BridgeError) as exc:
                await backend.complete([], "model", "medium")
            assert exc.value.code == code
            assert "private" not in str(exc.value)
            assert paths.count(START) == 1
            if mode in ("timeout", "native"):
                assert paths[-1] == STOP
            assert not backend.lock.locked()
    asyncio.run(run())


def test_cancellation_requests_remote_stop():
    async def run():
        started, stopped = asyncio.Event(), asyncio.Event()
        def handler(request):
            if request.url.path == START:
                started.set()
                return httpx.Response(200, json={"status": "started", "request_id": "r", "turn_state": {}})
            stopped.set()
            return httpx.Response(200, json={"status": "stopped"})
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(template(), client=http, poll=30)
            task = asyncio.create_task(backend.complete([], "model", "medium"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stopped.is_set()
            assert not backend.lock.locked()
    asyncio.run(run())


def test_har_credentials_local_only(tmp_path):
    har = tmp_path / "test.har"
    har.write_text(dumps({"log": {"entries": [{"request": {
        "url": ORIGIN + START, "method": "POST", "headers": [],
        "postData": {"text": dumps({"metadata": template().metadata})}}}]}}), encoding="utf-8")
    with pytest.raises(BridgeError) as exc:
        SessionTemplate.from_har(har)
    assert exc.value.code == "missing_cookie"
    cookie = tmp_path / "cookie.txt"
    cookie.write_text("Cookie: fake_session=not-a-real-credential", encoding="utf-8")
    loaded = SessionTemplate.from_har(har, cookie)
    assert loaded.headers["Cookie"] == "fake_session=not-a-real-credential"
    assert loaded.headers["Origin"] == ORIGIN


def test_unknown_response_id_does_not_call_upstream():
    backend = FakeBackend([])
    with client(backend) as c:
        r = c.post("/v1/responses", json={"previous_response_id": "missing", "input": "x"})
        assert r.status_code == 404
        assert backend.requests == []


def test_additional_tools_namespaces_and_output_wire_identity():
    body = {"input": [{"type": "additional_tools", "tools": [
        {"type": "namespace", "name": "functions", "tools": [CUSTOM, FUNCTION]}]},
        {"role": "user", "content": "test"}]}
    history, tools = normalize(body)
    assert set(tools) == {"functions.apply_patch", "functions.exec_command"}
    out = parse_answer(envelope([{"name": "functions.apply_patch", "input": "raw"}]), tools, "n", body)
    assert out[0]["name"] == "apply_patch"
    assert out[0]["namespace"] == "functions"
    out = parse_answer(envelope([{"name": "functions.exec_command", "arguments": {"cmd": "x"}}]), tools, "n", body)
    assert out[0]["namespace"] == "functions"
    assert out[0]["name"] == "exec_command"
    user_payload = json.loads(make_prompt(body, history, tools, "n")[1]["content"][0]["text"])
    assert "bridge_protocol_instructions" in user_payload
    assert "_wire_name" not in user_payload["bridge_request"]["tools"][0]


def test_additional_tool_updates_and_continuation():
    new = copy.deepcopy(FUNCTION)
    new["description"] = "updated"
    body = {"input": [{"type": "additional_tools", "tools": [FUNCTION]},
                       {"type": "additional_tools", "tools": [new]}]}
    history, tools = normalize(body)
    assert tools["exec_command"]["description"] == "updated"
    _, next_tools = normalize({"input": "next"}, history)
    assert next_tools == tools


@pytest.mark.parametrize("body", [
    {"text": "invalid"},
    {"text": {"format": "invalid"}},
    {"input": [{"type": "function_call_output", "call_id": [], "output": "x"}]},
    {"tools": [{"type": "function", "name": "f", "parameters": {"$ref": "https://example.com/schema"}}]},
    {"tools": [{"type": "namespace", "name": "n", "tools": [{"type": "namespace", "name": "nested", "tools": []}]}]},
    {"tool_choice": {"type": "function", "name": []}},
])
def test_malformed_structures_fail_cleanly(body):
    with client(FakeBackend([])) as c:
        assert c.post("/v1/responses", json=body).status_code == 400


def test_forced_namespaced_tool_choice():
    body = {"tools": [{"type": "namespace", "name": "f", "tools": [FUNCTION]}],
            "tool_choice": {"type": "function", "namespace": "f", "name": "exec_command"}}
    _, tools = normalize(body)
    items = parse_answer(envelope([{"name": "f.exec_command", "arguments": {"cmd": "x"}}]), tools, "n", body)
    assert items[0]["namespace"] == "f"


@pytest.mark.parametrize("success", [True, False])
def test_immediate_completed_start_without_turn_state(success):
    async def run():
        def handler(request):
            payload = {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}
            return httpx.Response(200, json={"status": "completed", "response": {"status": "success" if success else "error", "payload": payload}})
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(template(), client=http)
            if success:
                assert await backend.complete([], "model", "medium") == "ok"
            else:
                with pytest.raises(BridgeError) as exc:
                    await backend.complete([], "model", "medium")
                assert exc.value.code == "prism_task_error"
    asyncio.run(run())


def test_registered_conversation_and_browser_wire_context():
    async def run():
        t = template()
        t.conversation_action = {"url": ORIGIN + "/", "headers": {"next-action": "test-action"}}
        t.initial_system = {"role": "system", "content": [{"type": "input_text", "text": "browser-system"}]}
        t.editor_context = {"role": "system", "content": [{"type": "input_text", "text": '{"openFile":{}}'}]}
        cid = "cdx1_11111111-2222-3333-4444-555555555555"
        def handler(request):
            if request.url.path == "/":
                assert json.loads(request.content) == ["test-project"]
                assert request.headers["next-action"] == "test-action"
                return httpx.Response(200, text='0:{"a":"$@1"}\n1:"' + cid + '"\n')
            if request.url.path.endswith("heartbeat"):
                return httpx.Response(200, text="OK")
            body = json.loads(request.content)
            assert body["conversationId"] == cid
            assert json.loads(body["metadata"]["codex_listen_snapshot"])["workspace_session_id"] == cid[5:]
            assert body["input"][0]["content"][0]["text"] == "browser-system"
            assert body["input"][1] == t.editor_context
            return httpx.Response(200, json={"status": "completed", "response": {"status": "success", "payload": {
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}}})
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(t, client=http)
            messages = [{"role": "system", "content": []}, {"role": "user", "content": []}]
            assert await backend.complete(messages, "model", "medium") == "ok"
    asyncio.run(run())


def test_sandbox_bootstrap_mounts_project_before_start():
    async def run():
        paths = []
        def handler(request):
            path = request.url.path
            paths.append(path)
            if path == "/api/backend/1/new":
                return httpx.Response(200, json={"url": ORIGIN + "/s/sandboxes/proxy", "token": "fresh-fake-token"})
            if path == "/api/y":
                return httpx.Response(200, json={k: "fake" for k in ("url", "baseUrl", "docId", "token", "authorization")})
            if path == "/api/projects/test-project/sandbox/resources-token":
                return httpx.Response(200, json={"access_token": "fake-resource-token", "resources_base_url": ORIGIN})
            if path.startswith("/s/sandboxes/proxy"):
                assert request.headers["x-crixet-sandbox-token"] == "fresh-fake-token"
                return httpx.Response(200, json={"status": "synced" if path.endswith("wait-for-sync") else "success"})
            assert path == START
            assert json.loads(request.content)["metadata"]["sandbox_token"] == "fresh-fake-token"
            return httpx.Response(200, json={"status": "completed", "response": {"status": "success", "payload": {
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}}})
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(template(), client=http, bootstrap=True)
            assert await backend.complete([], "model", "medium") == "ok"
            assert paths.index("/s/sandboxes/proxy/wait-for-sync") < paths.index(START)
            assert backend.needs_bootstrap is False
    asyncio.run(run())


def test_only_status_transport_is_retried():
    async def run():
        count = {START: 0, STATUS: 0}
        def handler(request):
            count[request.url.path] += 1
            if request.url.path == START:
                return httpx.Response(200, json={"status": "started", "request_id": "r", "turn_state": {}})
            if count[STATUS] == 1:
                raise httpx.ReadError("temporary")
            return httpx.Response(200, json={"status": "completed", "response": {"status": "success", "payload": {
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}}})
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(template(), client=http, poll=0)
            assert await backend.complete([], "model", "medium") == "ok"
            assert count == {START: 1, STATUS: 2}
    asyncio.run(run())


def test_tool_continuation_is_one_shot_and_only_for_latest_batch():
    store = ToolContinuationStore()
    output = [{"type": "function_call", "call_id": "a"}, {"type": "custom_tool_call", "call_id": "b"}]
    state = {"conversation_id": "private", "response_id": "r", "snapshot": {"transcript_cursor": 7}}
    results = [{"type": "function_call_output", "call_id": "a", "output": "ok"},
               {"type": "custom_tool_call_output", "call_id": "b", "output": "ok"}]
    store.put(output, state)
    assert store.take(results[:1]) is None
    assert store.take(results + [{"role": "user", "content": "new task"}]) is None
    assert store.take(output + results) == state
    assert store.take(output + results) is None


def test_store_false_tool_loop_preserves_private_remote_continuation():
    continuation = {"conversation_id": "private", "response_id": "upstream", "snapshot": {"secret_marker": "not-for-client"}}
    class StatefulFake(FakeBackend):
        async def complete(self, messages, model, effort, *, continuation=None):
            self.last_continuation = continuation
            answer = await super().complete(messages, model, effort)
            return BackendText(answer, globals_state)
    globals_state = continuation
    backend = StatefulFake([{"calls": [{"name": "exec_command", "arguments": {"cmd": "echo x"}}]}, {"text": "done"}])
    with client(backend) as c:
        first = c.post("/v1/responses", json={"input": "test", "tools": [FUNCTION], "store": False}).json()
        assert "not-for-client" not in dumps(first)
        assert backend.last_continuation is None
        call = first["output"][0]
        second = c.post("/v1/responses", json={"tools": [FUNCTION], "store": False, "input": [
            {"role": "user", "content": "test"}, call,
            {"type": "function_call_output", "call_id": call["call_id"], "output": "x"}]})
        assert second.status_code == 200
        assert backend.last_continuation == continuation
        assert "not-for-client" not in second.text


def test_upstream_continuation_reuses_returned_snapshot_and_response_id():
    async def run():
        t = template()
        cid = "cdx1_11111111-2222-3333-4444-555555555555"
        snapshot = {"conversation_id": cid, "workspace_session_id": cid[5:], "project_id": "test-project",
                    "user_id": "test-user", "codex_session_id": "session", "transcript_cursor": 99}
        continuation = {"conversation_id": cid, "response_id": "upstream-response", "snapshot": snapshot}
        def handler(request):
            assert request.url.path == START
            body = json.loads(request.content)
            assert body["conversationId"] == cid
            assert body["previousResponseId"] == "upstream-response"
            assert json.loads(body["metadata"]["codex_listen_snapshot"]) == snapshot
            assert len(body["input"]) == 1
            return httpx.Response(200, json={"status": "completed", "response": {"status": "success", "payload": {
                "id": "next-response", "codexListenSnapshot": snapshot,
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}}})
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(t, client=http)
            result = await backend.complete([{"role": "system", "content": []}, {"role": "user", "content": []}],
                                            "model", "medium", continuation=continuation)
            assert result == "ok"
            assert result.continuation["response_id"] == "next-response"
    asyncio.run(run())
