import json

from fastapi.testclient import TestClient

from prism_bridge.app import create_app
from prism_bridge.auth import PrismAuth
from prism_bridge.diagnostics import Diagnostics
from prism_bridge.protocol import PROTOCOL, BridgeError, dumps

KEY = "test-bridge-secret-at-least-16-chars"


class FakeTemplate:
    headers = {"User-Agent": "test", "Cookie": ""}
    metadata = {"userId": "user-TESTUSER123456", "projectId": "proj-TESTPROJECT", "sandbox_token": "tok"}


class FakeAdminBackend:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.template = FakeTemplate()
        self.diagnostics = Diagnostics()
        self.auth = PrismAuth("", expected_user="user-TESTUSER123456")
        self.needs_bootstrap = True
        self.sandbox_provisioned = False
        self.sandbox_was_replaced = False
        self.sandbox_session_id = None
        self.resource_expires_at = 0.0
        self.preflight_calls = []

    async def complete(self, messages, model, effort):
        request = json.loads(messages[1]["content"][0]["text"])["bridge_request"]
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return dumps({"protocol": PROTOCOL, "nonce": request["nonce"],
                      "text": reply.get("text", ""), "calls": reply.get("calls", [])})

    async def preflight(self, *, provision=False, deadline=120):
        self.preflight_calls.append(provision)
        return {"ok": True, "checks": {"signed_in": True, "project_accessible": True},
                "model_request_sent": False, "diagnostics": self.diagnostics.snapshot()}

    async def close(self):
        pass


def client(backend=None, **kwargs):
    backend = backend or FakeAdminBackend()
    app = create_app(backend, KEY, **kwargs)
    return TestClient(app, base_url="http://localhost",
                      headers={"Authorization": "Bearer " + KEY}), backend


def test_max_reasoning_effort_accepted():
    backend = FakeAdminBackend([{"text": "ok"}])
    c, _ = client(backend)
    with c:
        r = c.post("/v1/responses", json={"input": "t", "reasoning": {"effort": "max"}})
        assert r.status_code == 200


UI_KEY = {"X-Bridge-Key": KEY, "Authorization": ""}


def test_ui_page_served_without_key():
    c, _ = client()
    with c:
        r = c.get("/ui", headers={"Authorization": ""})
        assert r.status_code == 200
        assert "Prism Bridge" in r.text
        assert r.headers["cache-control"] == "no-store"


def test_ui_api_requires_bridge_key():
    c, _ = client()
    with c:
        assert c.get("/ui/api/status", headers={"Authorization": ""}).status_code == 401
        assert c.get("/ui/api/status", headers={"X-Bridge-Key": "wrong", "Authorization": ""}).status_code == 401
        assert c.get("/ui/api/status", headers=UI_KEY).status_code == 200


def test_ui_api_allows_browser_origin_with_key():
    # Unlike /v1/*, the UI API must accept browser requests; CSRF is prevented
    # by the custom header requirement, not by Origin rejection.
    c, _ = client()
    with c:
        r = c.get("/ui/api/status", headers={**UI_KEY, "Origin": "http://localhost"})
        assert r.status_code == 200


def test_status_shape_and_masking():
    c, _ = client()
    with c:
        s = c.get("/ui/api/status", headers=UI_KEY).json()
        assert s["bridge"]["version"]
        assert s["prism_auth"]["configured"] is True
        assert s["prism_auth"]["signed_in"] is False
        assert s["sandbox"]["needs_bootstrap"] is True
        # Identifiers are masked, never full values.
        assert s["project"]["user"].endswith("…")
        assert "TESTUSER123456" not in json.dumps(s)


def test_traffic_records_ok_and_failed_turns():
    backend = FakeAdminBackend([
        {"calls": [{"name": "exec_command", "arguments": {"cmd": "echo hi"}}]},
        BridgeError("upstream failed", "test_error", 502),
    ])
    function = {"type": "function", "name": "exec_command", "parameters": {
        "type": "object", "properties": {"cmd": {"type": "string"}},
        "required": ["cmd"], "additionalProperties": False}}
    c, _ = client(backend)
    with c:
        assert c.post("/v1/responses", json={"input": "t", "tools": [function]}).status_code == 200
        assert c.post("/v1/responses", json={"input": "t"}).status_code == 502
        t = c.get("/ui/api/traffic", headers=UI_KEY).json()
        assert t["total"] == 2 and t["failed"] == 1
        newest, oldest = t["turns"][0], t["turns"][1]
        assert newest["status"] == "failed" and newest["error"]["code"] == "test_error"
        assert oldest["status"] == "ok" and oldest["tool_calls"] == ["exec_command"]
        # No prompt or argument content is retained.
        assert "echo hi" not in json.dumps(t)


def test_preflight_passthrough():
    c, backend = client()
    with c:
        r = c.post("/ui/api/preflight", headers=UI_KEY, json={"provision": True})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert backend.preflight_calls == [True]


def test_setup_returns_profile_config():
    c, _ = client(port=9999)
    with c:
        s = c.get("/ui/api/setup", headers=UI_KEY).json()
        assert 'base_url = "http://127.0.0.1:9999/v1"' in s["config_toml"]
        assert s["env_key"] == "PRISM_BRIDGE_API_KEY"


def test_config_view_is_read_only_snapshot():
    c, _ = client(port=8123)
    with c:
        cfg = c.get("/ui/api/config", headers=UI_KEY).json()
        assert cfg["port"] == 8123
        assert "sources" in cfg


def test_auth_cookie_rejects_garbage():
    c, _ = client()
    with c:
        r = c.post("/ui/api/auth/cookie", headers=UI_KEY, json={"cookie": "not-a-cookie"})
        assert r.status_code == 400
        r = c.post("/ui/api/auth/import", headers=UI_KEY, json={"content": "{\"nope\": 1}"})
        assert r.status_code == 400


def test_untrusted_host_rejected_unless_declared_public():
    c, _ = client()
    with c:
        assert c.get("/health", headers={"Host": "144.79.170.102:8765"}).status_code == 400
    c, _ = client(public_hosts=["144.79.170.102"])
    with c:
        assert c.get("/health", headers={"Host": "144.79.170.102:8765"}).status_code == 200
        # Declaring one public host must not re-open the wildcard.
        assert c.get("/health", headers={"Host": "attacker.example"}).status_code == 400


def test_oauth_endpoints_require_the_bridge_key(tmp_path):
    from prism_bridge.oauth import OAuthManager
    backend = FakeAdminBackend()
    app = create_app(backend, KEY, oauth=OAuthManager(tmp_path / "oauth.json"))
    with TestClient(app, base_url="http://localhost") as c:
        # Previously these were reachable with no credential at all, which let
        # anyone bind their own OpenAI account to someone else's bridge.
        for path in ("/oauth/status", "/oauth/login"):
            assert c.get(path).status_code == 401
            assert c.get(path, headers={"X-Bridge-Key": KEY}).status_code == 200
        assert c.post("/oauth/submit", data={"callback_url": "x"}).status_code == 401
        assert c.post("/oauth/refresh").status_code == 401
        assert c.post("/oauth/exchange", json={"code": "c", "state": "s"}).status_code == 401


def test_oauth_callback_stays_open_but_is_state_gated(tmp_path):
    from prism_bridge.oauth import OAuthManager
    app = create_app(FakeAdminBackend(), KEY, oauth=OAuthManager(tmp_path / "oauth.json"))
    with TestClient(app, base_url="http://localhost") as c:
        # The browser redirect from auth.openai.com carries no key; an unknown
        # PKCE state must still be refused.
        r = c.get("/oauth/callback", params={"code": "abc", "state": "never-issued"})
        assert r.status_code == 400
        assert r.json()["code"] == "oauth_invalid_state"


def test_oauth_page_redirects_to_console(tmp_path):
    from prism_bridge.oauth import OAuthManager
    oauth = OAuthManager(tmp_path / "oauth.json")
    app = create_app(FakeAdminBackend(), KEY, oauth=oauth)
    with TestClient(app, base_url="http://localhost") as c:
        r = c.get("/oauth", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/ui#login"
        # The old page minted a PKCE session on every GET; the redirect must not.
        assert oauth.pending == {}


def test_management_responses_carry_security_headers():
    c, _ = client()
    with c:
        r = c.get("/ui", headers={"Authorization": ""})
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_credential_import_rejects_oversize_and_unknown_fields():
    c, _ = client()
    with c:
        big = c.post("/ui/api/auth/import", headers=UI_KEY, content=b"x" * (64 * 1024 + 1))
        assert big.status_code == 413
        assert big.json()["error"]["code"] == "auth_content_too_large"
        # An unexpected field must be refused, not silently ignored.
        sneaky = c.post("/ui/api/auth/import", headers=UI_KEY,
                        json={"content": "{}", "path": "C:\\secret.json"})
        assert sneaky.status_code == 400
        assert c.post("/ui/api/auth/cookie", headers=UI_KEY,
                      json={"cookie": "a=b", "state_file": "/etc/passwd"}).status_code == 400


def test_ui_disabled_flag():
    c, _ = client(ui=False)
    with c:
        assert c.get("/ui", headers={"Authorization": ""}).status_code in (401, 403, 404)
