"""OpenAI OAuth2 (PKCE) login for Prism bridge.

Mirrors the reference CLIProxyAPI codex auth flow (auth.openai.com
OAuth endpoints, PKCE S256), plus refresh-token persistence so the
deployed bridge can authenticate without captured cookies/HARs.

Client/redirect defaults match the reference provider so a user can
browser-login to auth.openai.com and land back on localhost:<port>
via an SSH tunnel; both automatic callback and manual code paste are
supported.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs

import httpx

from .protocol import BridgeError

AUTH_BASE = "https://auth.openai.com"
AUTHORIZE_URL = AUTH_BASE + "/oauth/authorize"
TOKEN_URL = AUTH_BASE + "/oauth/token"

# Reference CLIProxyAPI Codex OAuth client (known to work; offline_access).
CLIENT_ID_DEFAULT = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_DEFAULT = "http://localhost:1455/auth/callback"
SCOPE_DEFAULT = "openid email profile offline_access"

TRANSPORT_ERRORS = (httpx.HTTPError,)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    verifier = b64url(secrets.token_bytes(96))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


@dataclass
class OAuthSession:
    state: str
    verifier: str
    challenge: str
    created_at: float = field(default_factory=time.time)
    code: str | None = None
    error: str | None = None
    tokens: dict | None = None


@dataclass
class OAuthState:
    """Persisted credential store (version 1)."""
    client_id: str
    redirect_uri: str
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    account_id: str = ""
    email: str = ""
    token_type: str = ""
    expires_at: float = 0.0
    updated_at: float = 0.0
    user_id: str = ""  # OpenAI user id (from id_token), used for account gating

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "account_id": self.account_id,
            "email": self.email,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "updated_at": self.updated_at,
            "user_id": self.user_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OAuthState":
        state = cls(
            client_id=data.get("client_id", CLIENT_ID_DEFAULT),
            redirect_uri=data.get("redirect_uri", REDIRECT_DEFAULT),
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            id_token=data.get("id_token", ""),
            account_id=data.get("account_id", ""),
            email=data.get("email", ""),
            token_type=data.get("token_type", ""),
            expires_at=float(data.get("expires_at", 0) or 0),
            updated_at=float(data.get("updated_at", 0) or 0),
            user_id=data.get("user_id", ""),
        )
        return state

    @property
    def valid(self) -> bool:
        return bool(self.access_token and self.refresh_token)

    @property
    def expired(self) -> bool:
        return self.expires_at <= time.time() + 60


def decode_id_token(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


class OAuthManager:
    def __init__(self, state_file: str | Path, *, client_id: str = CLIENT_ID_DEFAULT,
                 redirect_uri: str = REDIRECT_DEFAULT, expected_user: str | None = None,
                 scope: str = SCOPE_DEFAULT):
        self.state_file = Path(state_file)
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.expected_user = expected_user
        self.pending: dict[str, OAuthSession] = {}
        self.lock = asyncio.Lock()
        self.state = self._load()

    def _load(self) -> OAuthState:
        if not self.state_file.exists():
            return OAuthState(self.client_id, self.redirect_uri)
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                return OAuthState(self.client_id, self.redirect_uri)
            return OAuthState.from_dict(data)
        except (OSError, ValueError, TypeError):
            return OAuthState(self.client_id, self.redirect_uri)

    def persist(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_name(self.state_file.name + ".tmp")
        temporary.write_text(json.dumps(self.state.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            import os
            os.replace(temporary, self.state_file)
        finally:
            if temporary.exists():
                temporary.unlink()

    def build_authorize_url(self) -> str:
        state = secrets.token_urlsafe(24)
        verifier, challenge = generate_pkce()
        self.pending[state] = OAuthSession(state=state, verifier=verifier, challenge=challenge)
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        return AUTHORIZE_URL + "?" + urlencode(params)

    async def exchange_code(self, code: str, state: str, *, verifier: str | None = None) -> OAuthState:
        if verifier is None:
            session = self.pending.get(state)
            if session is None or session.verifier is None:
                raise BridgeError("Unknown or expired OAuth state.", "oauth_invalid_state", 400)
            verifier = session.verifier
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }
        tokens = await self._token_request(data)
        return await self._apply_tokens(tokens)

    async def refresh(self) -> OAuthState:
        if not self.state.refresh_token:
            raise BridgeError("No refresh token persisted; run OAuth login first.", "oauth_missing_refresh", 401)
        async with self.lock:
            if self.state.valid and not self.state.expired:
                return self.state
            data = {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.state.refresh_token,
                "scope": self.scope,
            }
            tokens = await self._token_request(data)
            return await self._apply_tokens(tokens)

    async def _token_request(self, data: dict) -> dict:
        # Absolute URL (no trailing slash): base_url + "" resolves to
        # /oauth/token/ which Cloudflare challenges with 403; the canonical
        # path without the trailing slash returns proper OAuth JSON.
        async with httpx.AsyncClient(timeout=30, http2=True) as client:
            try:
                r = await client.post(TOKEN_URL, data=data, headers={"Accept": "application/json"})
            except TRANSPORT_ERRORS as exc:
                raise BridgeError("OAuth token endpoint transport failed.", "oauth_transport_error", 502) from None
            if r.status_code != 200:
                detail = ""
                try:
                    err = r.json()
                    detail = " " + str((err.get("error") or {}).get("code") or err.get("error") or "")[:160]
                except Exception:
                    detail = " " + r.text[:160]
                raise BridgeError(f"OAuth token endpoint returned HTTP {r.status_code}.{detail}", "oauth_token_failed", 401)
            try:
                return r.json()
            except ValueError:
                raise BridgeError("OAuth token response was not JSON.", "oauth_protocol_error", 502) from None

    async def _apply_tokens(self, tokens: dict) -> OAuthState:
        access = tokens.get("access_token", "")
        refresh = tokens.get("refresh_token", "")
        id_token = tokens.get("id_token", "")
        if not access:
            raise BridgeError("OAuth response missing access_token.", "oauth_token_failed", 401)
        claims = decode_id_token(id_token) if id_token else {}
        expires_in = float(tokens.get("expires_in", 0) or 0)
        self.state = OAuthState(
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            access_token=access,
            refresh_token=refresh or self.state.refresh_token,
            id_token=id_token or self.state.id_token,
            account_id=claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
            or self.state.account_id,
            email=claims.get("email") or claims.get("https://api.openai.com/profile", {}).get("email", "")
            or self.state.email,
            token_type=tokens.get("token_type", "Bearer"),
            expires_at=time.time() + expires_in if expires_in > 0 else time.time() + 3600,
            updated_at=time.time(),
            user_id=claims.get("https://api.openai.com/auth", {}).get("user_id", "")
            or claims.get("sub", "") or self.state.user_id,
        )
        self.persist()
        return self.state

    def clear_pending(self, state: str):
        self.pending.pop(state, None)
