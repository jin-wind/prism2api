"""Sandbox lifetime regressions; no remote credentials or wall-clock sleeps."""
import asyncio
import json
import time

import httpx
import pytest

from prism_bridge.diagnostics import Diagnostics, task_failure_details
from prism_bridge.protocol import BridgeError
from prism_bridge.upstream import HEARTBEAT, ORIGIN, START, STATUS, PrismBackend, SessionTemplate


def template():
    return SessionTemplate({"projectId": "project", "userId": "user",
                            "sandbox_url": ORIGIN + "/s/sandboxes/proxy", "sandbox_token": "old-secret"}, {})


def success():
    return httpx.Response(200, json={"status": "completed", "response": {"status": "success", "payload": {
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}}})


def init_response(request):
    path = request.url.path
    if path == "/api/backend/1/new":
        return httpx.Response(200, json={"url": ORIGIN + "/s/sandboxes/proxy", "token": "new-secret"})
    if path == "/api/y":
        return httpx.Response(200, json={k: "fixture" for k in ("url", "baseUrl", "docId", "token", "authorization")})
    if path == "/api/projects/project/sandbox/resources-token":
        return httpx.Response(200, json={"access_token": "resource-secret", "resources_base_url": ORIGIN,
                                        "expires_at": time.time() + 3600})
    if path.startswith("/s/sandboxes/proxy"):
        return httpx.Response(200, json={"status": "synced" if path.endswith("wait-for-sync") else "success"})
    raise AssertionError("Unexpected request")


def provisioned_backend(http):
    backend = PrismBackend(template(), client=http, poll=0)
    backend.sandbox_provisioned = True
    backend.resource_expires_at = time.time() + 3600
    return backend


def test_idle_expired_sandbox_recreated_before_next_model_submission():
    async def run():
        paths, starts = [], []
        expired = False
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == HEARTBEAT:
                old = request.headers["x-crixet-sandbox-token"] == "old-secret"
                if expired and old:
                    return httpx.Response(502, headers={"x-crixet-sandbox-expired": "true"}, text="private-error-body")
                return httpx.Response(200, text="OK", headers={"x-session-id": "old-session" if old else "new-session"})
            if request.url.path == START:
                starts.append(json.loads(request.content)["metadata"]["sandbox_token"])
                return success()
            return init_response(request)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            assert await backend.complete([], "model", "medium") == "ok"
            # Simulate remote expiry during the idle gap; the resource JWT remains valid.
            expired = True
            paths.clear()
            assert await backend.complete([], "model", "medium") == "ok"
            assert starts == ["old-secret", "new-secret"]
            assert paths[0] == HEARTBEAT
            assert paths.count("/api/backend/1/new") == 1
            assert paths.index("/s/sandboxes/proxy/wait-for-sync") < paths.index(START)
            assert backend.sandbox_session_id == "new-session"
            assert backend.diagnostics.completed_turns == 2
            assert "private-error-body" not in json.dumps(backend.diagnostics.snapshot())
    asyncio.run(run())


@pytest.mark.parametrize("status,headers", [(500, {}), (502, {}), (503, {}), (504, {}), (401, {}),
                                           (502, {"x-crixet-sandbox-502-reprovision-fallback": "disabled"})])
def test_ambiguous_probe_failure_does_not_submit_or_allocate(status, headers):
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(status, text="secret-token-in-body", headers=headers)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            with pytest.raises(BridgeError) as exc:
                await backend.complete([], "model", "medium")
            assert exc.value.code == "sandbox_probe_failed"
            assert exc.value.stage == "sandbox.check"
            assert paths == [HEARTBEAT]
            assert not backend.diagnostics.model_request_sent
            assert backend.sandbox_provisioned
            assert "secret-token-in-body" not in json.dumps(backend.diagnostics.snapshot())
    asyncio.run(run())


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_probe_transport_failure_is_not_treated_as_expiry(error):
    async def run():
        def handler(request):
            assert request.url.path == HEARTBEAT
            raise error("private-url-and-cookie")
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            with pytest.raises(BridgeError) as exc:
                await backend.complete([], "model", "medium")
            assert exc.value.code == "sandbox_probe_failed"
            assert not backend.diagnostics.model_request_sent
            assert "private-url" not in exc.value.message
    asyncio.run(run())


@pytest.mark.parametrize("status", [404, 410, 502])
def test_positive_expiry_has_only_one_replacement_attempt(status):
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == HEARTBEAT:
                return httpx.Response(status, headers={"x-crixet-sandbox-expired": "true"})
            return init_response(request)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            with pytest.raises(BridgeError) as exc:
                await backend.complete([], "model", "medium")
            assert exc.value.code == "sandbox_recovery_failed"
            assert paths.count("/api/backend/1/new") == 1
            assert START not in paths
            assert backend.needs_bootstrap and not backend.sandbox_provisioned
    asyncio.run(run())


def test_expired_tool_continuation_is_not_retargeted_or_replayed():
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(410)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            with pytest.raises(BridgeError) as exc:
                await backend.complete([], "model", "medium", continuation={"conversation_id": "old-conversation"})
            assert exc.value.code == "sandbox_continuation_expired"
            assert paths == [HEARTBEAT]
            assert backend.needs_bootstrap and not backend.sandbox_provisioned
    asyncio.run(run())


def test_changed_remote_session_resyncs_without_allocating_new_sandbox():
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == HEARTBEAT:
                return httpx.Response(200, text="OK", headers={"x-session-id": "new-session"})
            if request.url.path == START:
                return success()
            if request.url.path.endswith("/sandbox/resources-token"):
                assert json.loads(request.content)["sandbox_session_id"] == "new-session"
            return init_response(request)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            backend.sandbox_session_id = "old-session"
            assert await backend.complete([], "model", "medium") == "ok"
            assert "/api/backend/1/new" not in paths
            assert paths.index("/s/sandboxes/proxy/wait-for-sync") < paths.index(START)
    asyncio.run(run())


def test_healthy_sandbox_continuation_with_old_binding_is_rejected():
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, text="OK")
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            with pytest.raises(BridgeError) as exc:
                await backend.complete([], "model", "medium", continuation={"sandbox_binding": "stale-private-binding"})
            assert exc.value.code == "sandbox_continuation_expired"
            assert paths == []
            assert not backend.diagnostics.model_request_sent
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["immediate", "poll"])
def test_http200_application_error_is_classified_not_logged_as_success(phase):
    async def run():
        paths = []
        secret = "Cookie=session-secret https://internal.test/private user-private"
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == HEARTBEAT:
                return httpx.Response(200, text="OK")
            if phase == "poll" and request.url.path == START:
                return httpx.Response(200, json={"status": "started", "request_id": "r", "turn_state": {}})
            return httpx.Response(200, json={"status": "completed", "response": {"status": "error", "payload": {
                "reason": secret, "message": "Error while processing conversation (502 Bad Gateway). " + secret}}})
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            with pytest.raises(BridgeError) as exc:
                await backend.complete([], "model", "medium")
            assert exc.value.code == "prism_task_error"
            assert exc.value.payload()["upstream_status"] == 502
            assert exc.value.payload()["upstream_reason"] == "unknown"
            assert paths.count(START) == 1
            stage = "model.start" if phase == "immediate" else "model.poll"
            assert exc.value.stage == stage
            assert not any(e["phase"] == "ok" and e["stage"] == stage and e.get("upstream_state") == "completed"
                           for e in backend.diagnostics.events)
            serialized = json.dumps([backend.diagnostics.snapshot(), exc.value.payload()])
            assert secret not in serialized
            assert "session-secret" not in serialized
            assert backend.diagnostics.last_error["upstream_status"] == 502
    asyncio.run(run())


def test_task_failure_projection_does_not_return_arbitrary_text():
    # The free-form message is never echoed, whatever it contains.
    details = task_failure_details({"payload": {"reason": "secret", "message": "tok-abc.def ghi"}})
    assert details["upstream_reason"] == "unknown"
    assert details["upstream_category"] == "unknown"
    assert "tok-abc.def ghi" not in str(details)
    assert all("message" != k for k in details)
    assert task_failure_details({"payload": {"reason": "sandbox_reconnecting"}})["upstream_category"] == "sandbox_reconnecting"


def test_unmapped_reason_is_carried_only_when_it_looks_like_an_enum():
    # Without this an unmapped failure is indistinguishable from any other, which
    # is what made live incidents undiagnosable.
    details = task_failure_details({"status": "error", "payload": {"reason": "sandbox_expired"}})
    assert details["upstream_reason"] == "unknown"
    assert details["upstream_reason_raw"] == "sandbox_expired"
    assert details["upstream_shape"] == ["payload", "payload.reason", "status"]
    assert details["upstream_task_status"] == "error"
    # Anything not enum-shaped (spaces, length, a JWT-ish blob) is dropped.
    for hostile in ["a b", "x" * 80, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0", "", None, 5,
                    "Bearer sk-abc", "user@example.com",
                    "eyJhbGciOiJIUzI1NiJ9",          # one long run, no separator
                    "sk-proj-abcdefghijklmnopqrst",  # long unseparated segment
                    "user-MgDLlr5H0msuiYS0khbdpW3O",
                    "sandboxExpired"]:               # mixed case is not an enum code here
        assert "upstream_reason_raw" not in task_failure_details({"payload": {"reason": hostile}})
    # A mapped reason is not duplicated into the raw field.
    assert "upstream_reason_raw" not in task_failure_details({"payload": {"reason": "sandbox_reconnecting"}})


def test_background_keepalive_does_not_hide_model_poll_stage():
    async def run():
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(lambda r: httpx.Response(503))) as http:
            backend = provisioned_backend(http)
            backend.diagnostics.emit("model.poll", "begin")
            with pytest.raises(BridgeError):
                await backend._check_sandbox(background=True)
            assert backend.diagnostics.current_stage == "model.poll"
            assert backend.diagnostics.last_error is None
    asyncio.run(run())


def test_pending_model_transport_failure_never_restarts_model():
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == HEARTBEAT:
                return httpx.Response(200, text="OK")
            assert request.url.path == START
            raise httpx.ReadTimeout("outcome unknown")
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            with pytest.raises(BridgeError) as exc:
                await backend.complete([], "model", "medium")
            assert exc.value.code == "prism_transport_error"
            assert paths == [HEARTBEAT, START]
            assert backend.diagnostics.model_request_sent
            assert not backend.diagnostics.model_task_accepted
    asyncio.run(run())


def test_expired_continuation_cannot_allocate_on_second_attempt():
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            assert request.url.path == HEARTBEAT
            return httpx.Response(410)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            continuation = {"conversation_id": "old", "sandbox_binding": backend._sandbox_binding()}
            for _ in range(2):
                with pytest.raises(BridgeError) as exc:
                    await backend.complete([], "model", "medium", continuation=continuation)
                assert exc.value.code == "sandbox_continuation_expired"
            assert paths == [HEARTBEAT]
    asyncio.run(run())


def test_probe_deadline_fails_before_model_submission():
    async def run():
        paths = []
        async def handler(request):
            paths.append(request.url.path)
            await asyncio.Event().wait()
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            backend.timeout = .02
            with pytest.raises(BridgeError) as exc:
                await backend.complete([], "model", "medium")
            assert exc.value.code == "prism_timeout"
            assert exc.value.stage == "sandbox.check"
            assert paths == [HEARTBEAT]
            assert not backend.lock.locked()
    asyncio.run(run())


def test_resources_refresh_only_after_healthy_probe():
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == HEARTBEAT:
                return httpx.Response(200, text="OK", headers={"x-session-id": "existing-session"})
            if request.url.path == START:
                return success()
            return init_response(request)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = provisioned_backend(http)
            backend.resource_expires_at = 1
            assert await backend.complete([], "model", "medium") == "ok"
            assert paths[0] == HEARTBEAT
            assert "/api/backend/1/new" not in paths
            assert paths.index("/s/sandboxes/proxy/wait-for-sync") < paths.index(START)
    asyncio.run(run())
