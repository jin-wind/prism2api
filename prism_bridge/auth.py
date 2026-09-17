"""Prism web-session lifecycle; Codex OAuth credentials are a separate audience.

Uses Prism's observed GET/POST /auth/session and a real CookieJar. Does not send
refresh tokens to a guessed OAuth client or inspect unrelated credential stores.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import time

import httpx
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream

from .protocol import BridgeError

HOST = "prism.openai.com"
CF_COOKIES = ("__cf_bm", "__cflb", "_cfuvid", "cf_clearance")
ORIGIN = "https://" + HOST
TRANSPORT_ERRORS = (httpx.HTTPError, EndOfStream, BrokenResourceError, ClosedResourceError)


def cookie_values(header):
    values = {}
    for part in header.split(";"):
        if "=" in part:
            name, value = part.strip().split("=", 1)
            if name:
                values[name] = value
    return values


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def access_token_from_file(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        token = data.get("access_token") or data.get("accessToken") or (data.get("tokens") or {}).get("access_token")
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
            raise ValueError()
        return token.strip()
    except (OSError, ValueError, AttributeError, TypeError):
        raise BridgeError("Auth file must contain access_token, accessToken, or tokens.access_token.", "invalid_auth_file") from None


def import_access_token_header(existing_header, token):
    # Remove the old identity/recovery cookies so the probe cannot accidentally
    # succeed by recovering the previous browser account instead of this token.
    values = {k: v for k, v in cookie_values(existing_header).items()
              if k != "prism_session_token" and not k.startswith("prism_oai_")}
    values["prism_oai_access_token"] = token
    return "; ".join(k + "=" + v for k, v in values.items())


class PrismAuth:
    def __init__(self, seed_cookie, *, expected_user=None, state_file=None):
        self.seed_cookie = seed_cookie
        self.expected_user = expected_user
        self.state_file = Path(state_file) if state_file else None
        self.lock = asyncio.Lock()
        self.ready = False
        self.refresh_at = 0.0
        self.summary = None
        self.refresh_generation = 0
        self.pinned_cf = {}
        self._seed_cf = {k: v for k, v in cookie_values(seed_cookie).items() if k in CF_COOKIES}
        # Browser-original CF values are the single source of truth for the
        # Server Action handshake. They are pinned, re-asserted after every
        # auth refresh, and never overwritten by persist().
        self.pinned_cf.update(self._seed_cf)
        # Immutable browser-original cookie header (from the first successful
        # state load). Server Action replay uses this; it can never be
        # polluted by persist() writing refresh-rotated values to the file.
        self._original_cookie_header = ""

    def pin_cf_values(self, values):
        """Force-carry browser-origin Cloudflare cookies on every request.

        CookieJar drops expired CF cookies (__cf_bm/__cflb), but Prism's
        Server Action handshake rejects requests without them, while it still
        accepts the original browser values even after nominal expiry.
        """
        for name, value in values.items():
            if name in CF_COOKIES and value:
                self.pinned_cf.setdefault(name, value)

    def file_cookie_header(self):
        """Return the browser-original Cookie header for Server Action replay.

        Server Action IDs are bound to the browser cookie set captured at
        page load (CF fingerprint). auth-refresh-rotated values get rejected
        (500) by the Next.js handshake, so conversation registration replays
        the original cookies captured at first load, never persist()-rotated
        values from the on-disk file.
        """
        if self._original_cookie_header:
            return self._original_cookie_header
        if not self.state_file or not self.state_file.exists():
            return ""
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if data.get("version") != 1 or data.get("expected_user") != self.expected_user:
                return ""
            parts = []
            for item in data.get("cookies", []):
                if item.get("name") and item.get("value"):
                    parts.append(item["name"] + "=" + item["value"])
            return "; ".join(parts)
        except (OSError, ValueError, TypeError):
            return ""

    def cf_pin_header(self, header):
        if not self.pinned_cf:
            return header
        parts = [x for x in header.split("; ") if x and not any(
            x.startswith(name + "=") for name in self.pinned_cf)] if header else []
        for name, value in self.pinned_cf.items():
            parts.append(name + "=" + value)
        return "; ".join(parts)

    def _load(self, client):
        if not self.state_file or not self.state_file.exists():
            return False
        try:
            if self.state_file.stat().st_size > 256 * 1024:
                raise ValueError()
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if data.get("version") != 1 or data.get("expected_user") != self.expected_user:
                raise ValueError()
            cookies = data["cookies"]
            for item in cookies:
                if item["domain"] not in (HOST, "." + HOST):
                    raise ValueError()
                if item["name"] in CF_COOKIES and item["name"] not in self._seed_cf:
                    self.pinned_cf.setdefault(item["name"], item["value"])
                if item.get("expires") is not None and item["expires"] < time.time():
                    continue
                client.cookies.jar.set_cookie(http.cookiejar.Cookie(
                    version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
                    domain=item["domain"], domain_specified=item.get("domain_specified", False),
                    domain_initial_dot=item["domain"].startswith("."), path=item["path"], path_specified=True,
                    secure=item.get("secure", True), expires=item.get("expires"),
                    discard=item.get("expires") is None, comment=None, comment_url=None,
                    rest={"HttpOnly": None}, rfc2109=False))
            if not self._original_cookie_header:
                self._original_cookie_header = "; ".join(
                    f"{item['name']}={item['value']}" for item in data["cookies"] if item.get("name") and item.get("value"))
            return bool(list(client.cookies.jar))
        except (OSError, ValueError, KeyError, TypeError):
            raise BridgeError("Invalid or mismatched local Prism auth-state file.", "invalid_auth_state") from None

    def persist(self, client):
        if not self.state_file:
            return
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                    "secure": c.secure, "expires": c.expires, "domain_specified": c.domain_specified}
                   for c in client.cookies.jar if c.domain in (HOST, "." + HOST)]
        # Never persist refresh-rotated challenge cookies: the Server Action
        # handshake binds to the browser-origin CF values. The seed value is
        # authoritative; pinned is the fallback.
        for _c in cookies:
            if _c["name"] in CF_COOKIES:
                _v = self._seed_cf.get(_c["name"]) or self.pinned_cf.get(_c["name"])
                if _v:
                    _c["value"] = _v
        atomic_json(self.state_file, {"version": 1, "expected_user": self.expected_user,
                                     "saved_at": time.time(), "cookies": cookies})

    async def ensure(self, client, *, force=False, import_only=False):
        generation = self.refresh_generation
        async with self.lock:
            if force and self.ready and self.refresh_generation != generation:
                return self.summary
            if self.ready and not force and time.time() < self.refresh_at:
                return self.summary
            first = not self.ready
            loaded = self._load(client) if first and not import_only else False
            # Use an explicit seed Cookie only on the initial handshake. All
            # later requests use CookieJar, including rotated auth/LB cookies.
            seed = first and not loaded
            headers = {"Cookie": self.seed_cookie} if seed else {}
            if seed:
                self.pin_cf_values(cookie_values(self.seed_cookie))
            # Snapshot browser-origin Cloudflare cookies. The no-headless
            # set-cookie values from the session refresh are rejected by the
            # Server Action handshake, so keep the original CF values while
            # session JWTs may still rotate normally.
            cf_before = {}
            for _c in client.cookies.jar:
                if _c.name in CF_COOKIES:
                    cf_before[_c.name] = (_c.value, _c.domain, _c.path, _c.expires)
            try:
                async with client.stream("POST" if force else "GET", "/auth/session",
                                         headers={"Accept": "application/json", "Cache-Control": "no-store", **headers},
                                         timeout=30) as r:
                    raw = await r.aread()
                    if len(raw) > 256 * 1024:
                        raise BridgeError("Prism auth response exceeded limit.", "auth_protocol_error", 502)
                    if r.status_code != 200:
                        raise BridgeError(f"Prism session refresh returned HTTP {r.status_code}.", "prism_auth_failed", 401)
                    try:
                        data = r.json()
                    except ValueError:
                        raise BridgeError("Prism auth response was not JSON.", "auth_protocol_error", 502) from None
            except TRANSPORT_ERRORS as exc:
                raise BridgeError("Prism session transport failed (" + type(exc).__name__ + ").", "auth_transport_error", 502) from None
            user, policy_user = data.get("user") or {}, (data.get("policy") or {}).get("user") or {}
            if not user or user.get("is_anonymous") is True:
                raise BridgeError("Prism did not accept this credential as a signed-in session.", "prism_auth_failed", 401)
            identities = {user.get("id"), (user.get("app_metadata") or {}).get("user_id"),
                          policy_user.get("id"), policy_user.get("openai_user_id"), policy_user.get("prism_user_id")}
            if self.expected_user and self.expected_user not in identities:
                client.cookies.clear()
                raise BridgeError("Credential account does not match the HAR account; no project requests were sent.", "auth_account_mismatch", 401)
            if seed:
                returned_names = {c.name for c in client.cookies.jar}
                for name, value in cookie_values(self.seed_cookie).items():
                    if name not in returned_names:
                        client.cookies.set(name, value, domain=HOST, path="/")
            # Challenge cookies keep the browser-original value as the single
            # source of truth; session JWTs are allowed to rotate normally.
            _seed_cf_all = dict(self._seed_cf)
            _seed_cf_all.update({k: v for k, v in self.pinned_cf.items() if k not in _seed_cf_all})
            for _name in CF_COOKIES:
                for _c in [c for c in client.cookies.jar if c.name == _name]:
                    client.cookies.jar.clear(_c.domain, _c.path, _name)
                _value = _domain = _path = _expires = None
                if _name in _seed_cf_all:
                    _value, _domain, _path, _expires = _seed_cf_all[_name], HOST, "/", None
                elif _name in cf_before and (cf_before[_name][3] is None or cf_before[_name][3] >= time.time()):
                    _value, _domain, _path, _expires = cf_before[_name]
                if _value is not None:
                    client.cookies.set(_name, _value, domain=_domain or HOST, path=_path or "/")
            # Read the server schedule, bounded to a sensible check interval.
            refresh = data.get("openAiRefreshAt")
            next_at = (refresh / 1000 if refresh > 1e12 else refresh) if isinstance(refresh, (int, float)) else time.time() + 300
            self.refresh_at = max(time.time() + 30, min(next_at, time.time() + 300))
            self.ready = True
            self.refresh_generation += 1
            self.summary = {"signed_in": True, "refreshed": bool((data.get("resolutionDiagnostics") or {}).get("refreshed")),
                            "cookie_names": sorted({c.name for c in client.cookies.jar}),
                            "persistent": self.state_file is not None}
            self.persist(client)
            return self.summary
