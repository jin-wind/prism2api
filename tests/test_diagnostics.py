import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from prism_bridge.app import create_app
from prism_bridge.diagnostics import Diagnostics, operation_for
from prism_bridge.protocol import BridgeError
from prism_bridge.upstream import ORIGIN, START, PrismBackend, SessionTemplate
from anyio import EndOfStream


def template():
    return SessionTemplate({"projectId": "secret-project", "userId": "secret-user",
                            "sandbox_url": ORIGIN + "/s/sandboxes/proxy", "sandbox_token": "private-token"}, {})


@pytest.mark.parametrize("path,stage", [
    ("/api/backend/1/new", "sandbox.acquire"),
    ("/s/sandboxes/proxy/resources-token", "sandbox.resources.register"),
    ("/api/projects/secret-project/sandbox/resources-token", "project.resources.issue"),
    (START, "model.start"),
])
def test_readtimeout_reports_precise_stage_without_leaking_payload(path, stage):
    async def run():
        def handler(req):
            raise httpx.ReadTimeout("sensitive URL and private-token")
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(template(), client=http)
            with pytest.raises(BridgeError) as error:
                await backend._post(path, {"token": "private-token", "prompt": "private-prompt"})
            assert error.value.stage == stage
            assert stage in error.value.message
            serialized = json.dumps([error.value.payload(), backend.diagnostics.snapshot()])
            for secret in ("secret-project", "secret-user", "private-token", "private-prompt", "sensitive URL"):
                assert secret not in serialized
            if path == START:
                assert backend.diagnostics.model_request_sent is True
                assert "unknown" in error.value.message
            else:
                assert backend.diagnostics.model_request_sent is False
                assert "No model request was sent" in error.value.message
                assert "start request" not in error.value.message
    asyncio.run(run())


def test_preflight_never_starts_model_and_reports_bootstrap_timeout():
    async def run():
        paths = []
        def handler(req):
            paths.append(req.url.path)
            if req.url.path == "/api/project-access":
                return httpx.Response(200, json={"accessible": True})
            if req.url.path == "/api/backend/1/new":
                raise httpx.ReadTimeout("private")
            raise AssertionError("Unexpected request")
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(template(), client=http)
            report = await backend.preflight(provision=True)
            assert not report["ok"]
            assert report["error"]["stage"] == "sandbox.acquire"
            assert report["checks"]["project_accessible"] is True
            assert START not in paths
    asyncio.run(run())


def test_diagnostic_absolute_deadline_preserves_current_operation():
    async def run():
        async def handler(req):
            if req.url.path == "/api/project-access":
                return httpx.Response(200, json={"accessible": True})
            await asyncio.Event().wait()
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(template(), client=http)
            report = await backend.preflight(provision=True, deadline=.02)
            assert report["error"]["code"] == "diagnostic_timeout"
            assert report["error"]["stage"] == "sandbox.acquire"
            assert report["model_request_sent"] is False
    asyncio.run(run())


def test_diagnostics_endpoint_requires_key_and_returns_no_secrets():
    key = "test-diagnostics-key-minimum-length"
    backend = PrismBackend(template(), client=httpx.AsyncClient())
    backend.diagnostics.emit("sandbox.acquire", "failed", error_class="ReadTimeout", error_code="prism_transport_error")
    with TestClient(create_app(backend, key), base_url="http://localhost") as c:
        assert c.get("/diagnostics").status_code == 401
        r = c.get("/diagnostics", headers={"Authorization": "Bearer " + key})
        assert r.status_code == 200
        assert r.json()["last_error"]["stage"] == "sandbox.acquire"
        assert "private-token" not in r.text
    asyncio.run(backend.client.aclose())


def test_events_are_bounded_and_dynamic_paths_redacted():
    diag = Diagnostics()
    for _ in range(100):
        diag.emit(operation_for("/api/projects/private/sandbox/resources-token"), "ok", seconds=.1)
    assert len(diag.snapshot()["events"]) == 40
    assert "private" not in json.dumps(diag.snapshot())


def test_raw_end_of_stream_is_reported_without_traceback():
    async def run():
        def handler(request):
            raise EndOfStream()
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as http:
            backend = PrismBackend(template(), client=http)
            with pytest.raises(BridgeError) as exc:
                await backend._post("/api/backend/1/new", None)
            assert exc.value.stage == "sandbox.acquire"
            assert "EndOfStream" in exc.value.message
    asyncio.run(run())
