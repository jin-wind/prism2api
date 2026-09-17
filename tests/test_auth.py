import asyncio
import json

import httpx
import pytest
from anyio import EndOfStream

from prism_bridge.auth import (HOST, ORIGIN, PrismAuth, access_token_from_file,
                                import_access_token_header)
from prism_bridge.protocol import BridgeError


def signed_in():
    return {"user": {"id": "prism-user", "is_anonymous": False},
            "policy": {"user": {"openai_user_id": "openai-user"}},
            "resolutionDiagnostics": {"refreshed": True}}


def test_cookie_rotation_refresh_and_persistence(tmp_path):
    async def run():
        seen = []
        def handler(request):
            seen.append(request)
            value = "renewed" if request.method == "POST" else "fresh"
            return httpx.Response(200, json=signed_in(), headers={"set-cookie":
                f"prism_session_token={value}; Path=/; Secure; HttpOnly"})
        path = tmp_path / "state.json"
        auth = PrismAuth("prism_session_token=old; prism_oai_access_token=source; __cf_bm=seed",
                         expected_user="openai-user", state_file=path)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as c:
            result = await auth.ensure(c)
            assert result["signed_in"] and path.exists()
            req = c.build_request("POST", "/api/test")
            assert "prism_session_token=fresh" in req.headers["cookie"]
            assert "prism_session_token=old" not in req.headers["cookie"]
            assert "prism_oai_access_token=source" in req.headers["cookie"]
            await auth.ensure(c)
            assert len(seen) == 1
            await auth.ensure(c, force=True)
            assert seen[-1].method == "POST"
            assert "prism_session_token=fresh" in seen[-1].headers["cookie"]
            assert "prism_session_token=renewed" in c.build_request("GET", "/api/test").headers["cookie"]
            assert not list(tmp_path.glob("*.tmp"))
        second = PrismAuth("prism_session_token=stale", expected_user="openai-user", state_file=path)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as c:
            await second.ensure(c)
            assert "prism_session_token=renewed" in seen[-1].headers["cookie"]
            assert "stale" not in seen[-1].headers["cookie"]
    asyncio.run(run())


def test_concurrent_refresh_is_single_flight():
    async def run():
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json=signed_in())
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as c:
            auth = PrismAuth("prism_oai_access_token=source", expected_user="openai-user")
            await asyncio.gather(*[auth.ensure(c) for _ in range(5)])
            assert calls == 1
            await asyncio.gather(*[auth.ensure(c, force=True) for _ in range(5)])
            assert calls == 2
    asyncio.run(run())


def test_import_removes_old_identity_and_does_not_use_refresh_token(tmp_path):
    p = tmp_path / "codex.json"
    p.write_text(json.dumps({"tokens": {"access_token": "new-token", "refresh_token": "never-transmit"}}))
    token = access_token_from_file(p)
    h = import_access_token_header("prism_session_token=old; prism_oai_refresh_token=old-refresh; prism_oai_access_token=old-access; __cf_bm=keep", token)
    assert "old" not in h
    assert "never-transmit" not in h
    assert "__cf_bm=keep" in h
    assert "prism_oai_access_token=new-token" in h


@pytest.mark.parametrize("data", [{"accessToken": "session-token"}, {"access_token": "session-token"}, {"tokens": {"access_token": "session-token"}}])
def test_supported_explicit_token_formats(tmp_path, data):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps(data))
    assert access_token_from_file(p) == "session-token"


def test_account_mismatch_never_persists(tmp_path):
    async def run():
        p = tmp_path / "state.json"
        auth = PrismAuth("prism_oai_access_token=source", expected_user="different-account", state_file=p)
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=signed_in()))) as c:
            with pytest.raises(BridgeError) as exc:
                await auth.ensure(c)
            assert exc.value.code == "auth_account_mismatch"
            assert not p.exists()
    asyncio.run(run())


def test_anonymous_is_not_success(tmp_path):
    async def run():
        auth = PrismAuth("prism_oai_access_token=bad", state_file=tmp_path / "state.json")
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"user": {"is_anonymous": True}}))) as c:
            with pytest.raises(BridgeError) as exc:
                await auth.ensure(c)
            assert exc.value.code == "prism_auth_failed"
    asyncio.run(run())


def test_tls_stream_close_is_sanitized_transport_error():
    async def run():
        def handler(request):
            raise EndOfStream()
        async with httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(handler)) as c:
            with pytest.raises(BridgeError) as exc:
                await PrismAuth("prism_oai_access_token=private").ensure(c)
            assert exc.value.code == "auth_transport_error"
            assert "EndOfStream" in exc.value.message
            assert "private" not in exc.value.message
    asyncio.run(run())
