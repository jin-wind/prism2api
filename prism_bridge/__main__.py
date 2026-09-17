import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn
import httpx

from .app import create_app
from .protocol import BridgeError
from .upstream import ORIGIN, START, PrismBackend, SessionTemplate
from .auth import PrismAuth, access_token_from_file, import_access_token_header
from .oauth import OAuthManager, CLIENT_ID_DEFAULT, REDIRECT_DEFAULT


async def auth_command(args, template):
    seed = template.headers.get("Cookie", "")
    importing = args.command == "auth-import"
    if importing:
        if not args.auth_file:
            raise BridgeError("auth-import needs --auth-file (CLIProxyAPI, Codex auth.json or session JSON).")
        seed = import_access_token_header(seed, access_token_from_file(args.auth_file))
    headers = {k: v for k, v in template.headers.items() if k.lower() != "cookie"}
    auth = PrismAuth(seed, expected_user=template.metadata["userId"], state_file=args.auth_state)
    async with httpx.AsyncClient(base_url=ORIGIN, headers=headers, http2=True, follow_redirects=False, timeout=30) as client:
        summary = await auth.ensure(client, force=args.command == "auth-refresh", import_only=importing)
        print(json.dumps({"provider": "prism", "operation": args.command, **summary}, indent=2))


def make_oauth(args) -> OAuthManager:
    redirect_uri = os.environ.get("PRISM_OAUTH_REDIRECT_URI", REDIRECT_DEFAULT)
    return OAuthManager(args.oauth_state, client_id=CLIENT_ID_DEFAULT, redirect_uri=redirect_uri)


async def oauth_command(args):
    oauth = make_oauth(args)
    if args.command == "oauth-login" and args.oauth_code:
        state_code = args.oauth_state_code or ""
        verifier = None
        session = oauth.pending.get(state_code)
        if session is not None:
            verifier = session.verifier
        result = await oauth.exchange_code(args.oauth_code, state_code, verifier=verifier)
        print(json.dumps({"provider": "openai-oauth", "operation": "login", "ok": True,
                          "email": result.email, "account_id": result.account_id, "user_id": result.user_id,
                          "expires_at": result.expires_at}, indent=2))
        return
    if args.command == "oauth-login":
        url = oauth.build_authorize_url()
        print(json.dumps({"provider": "openai-oauth", "operation": "login",
                          "authorization_url": url,
                          "instructions": "Open the URL in a browser, log in, paste the ?code= value back with --oauth-code."}, indent=2))
        return
    if args.command == "oauth-refresh":
        result = await oauth.refresh()
        print(json.dumps({"provider": "openai-oauth", "operation": "refresh", "ok": True,
                          "email": result.email, "expires_at": result.expires_at}, indent=2))
        return
    if args.command == "oauth-bind":
        # OAuth access token becomes the Prism access-token cookie, then
        # verify the Prism session and persist a fresh auth-state.
        from .auth import PrismAuth, import_access_token_header
        result = await oauth.refresh()
        if not result.access_token:
            raise BridgeError("OAuth session has no access token; run oauth-login first.", "oauth_missing_access", 401)
        seed = import_access_token_header("", result.access_token)
        if args.template_file:
            template = SessionTemplate.from_fixture(args.template_file, allow_missing_cookie=True)
        else:
            template = SessionTemplate.from_har(args.har, allow_missing_cookie=True)
        headers = {k: v for k, v in template.headers.items() if k.lower() != "cookie"}
        auth = PrismAuth(seed, expected_user=template.metadata["userId"], state_file=args.auth_state)
        async with httpx.AsyncClient(base_url=ORIGIN, headers=headers, http2=True,
                                     follow_redirects=False, timeout=30) as client:
            summary = await auth.ensure(client, force=True)
        print(json.dumps({"provider": "prism", "operation": "oauth-bind", **summary,
                          "oauth_email": result.email}, indent=2))
        return
    print(json.dumps({"provider": "openai-oauth", "operation": "status",
                      "configured": oauth.state.valid,
                      "email": oauth.state.email,
                      "account_id": oauth.state.account_id,
                      "user_id": oauth.state.user_id,
                      "expired": oauth.state.expired,
                      "expires_at": oauth.state.expires_at}, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Experimental Prism to Codex Responses bridge")
    parser.add_argument("command", choices=["inspect", "serve", "doctor", "auth-status", "auth-refresh", "auth-import",
                                              "oauth-login", "oauth-status", "oauth-refresh", "oauth-bind"])
    parser.add_argument("--har", default=os.environ.get("PRISM_HAR_PATH"))
    parser.add_argument("--template-file", default=os.environ.get("PRISM_TEMPLATE_FILE"))
    parser.add_argument("--cookie-file", default=os.environ.get("PRISM_COOKIE_FILE"))
    parser.add_argument("--host", default=os.environ.get("PRISM_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reuse-har-sandbox", action="store_true", help="Skip provisioning; only for a still-ready captured sandbox.")
    parser.add_argument("--auth-state", default=os.environ.get("PRISM_AUTH_STATE", ".local/prism-auth.json"))
    parser.add_argument("--auth-file", help="Explicit token JSON path for auth-import; never discovered automatically.")
    parser.add_argument("--oauth-state", default=os.environ.get("PRISM_OAUTH_STATE", ".local/oauth-state.json"))
    parser.add_argument("--oauth-code", help="Manual authorization code for oauth-login (paste from browser).")
    parser.add_argument("--oauth-state-code", help="OAuth state value paired with --oauth-code.")
    parser.add_argument("--provision", action="store_true", help="doctor only: acquire/sync a sandbox without submitting any model prompt.")
    parser.add_argument("--deadline", type=float, default=120, help="doctor absolute time budget in seconds.")
    args = parser.parse_args()
    if not args.har and not args.template_file:
        parser.error("Supply --har/PRISM_HAR_PATH or --template-file/PRISM_TEMPLATE_FILE.")
    if args.command == "inspect":
        entries = json.loads(Path(args.har).read_text(encoding="utf-8-sig"))["log"]["entries"]
        starts = [e for e in entries if e["request"]["url"] == ORIGIN + START]
        print(json.dumps({"entries": len(entries), "prism_starts": len(starts),
                          "has_start_cookie": any(h["name"].lower() == "cookie" and bool(h["value"])
                                                  for e in starts for h in e["request"].get("headers", [])),
                          "native_tool_schema_observed": any("tools" in json.loads(e["request"]["postData"]["text"])
                                                             for e in starts)}, indent=2))
        return
    try:
        seed = None
        if args.command == "auth-import" and not args.cookie_file:
            if not args.auth_file:
                raise BridgeError("auth-import needs --auth-file.")
            seed = import_access_token_header("", access_token_from_file(args.auth_file))
        if args.template_file:
            template = SessionTemplate.from_fixture(args.template_file, args.cookie_file,
                                                    allow_missing_cookie=Path(args.auth_state).exists())
        else:
            template = SessionTemplate.from_har(args.har, args.cookie_file, cookie_header=seed,
                                               allow_missing_cookie=Path(args.auth_state).exists())
        if args.command.startswith("oauth-"):
            asyncio.run(oauth_command(args))
            return
        if args.command.startswith("auth-"):
            asyncio.run(auth_command(args, template))
            return
        if args.command == "doctor":
            if args.deadline <= 0:
                raise BridgeError("--deadline must be positive.")
            async def doctor():
                backend = PrismBackend(template, auth_state_file=args.auth_state,
                                       read_timeout=float(os.environ.get("PRISM_HTTP_READ_TIMEOUT", "90")), diagnostics=True)
                try:
                    result = await backend.preflight(provision=args.provision, deadline=args.deadline)
                    print(json.dumps(result, indent=2))
                    return result["ok"]
                finally:
                    await backend.close()
            if not asyncio.run(doctor()):
                raise SystemExit(1)
            return
        if not template.conversation_action:
            raise BridgeError("HAR must include the successful create-chat Next.js action for this project.", "missing_conversation_action")
        backend = PrismBackend(template, timeout=float(os.environ.get("PRISM_TURN_TIMEOUT", "300")),
                               bootstrap=not args.reuse_har_sandbox, auth_state_file=args.auth_state,
                               read_timeout=float(os.environ.get("PRISM_HTTP_READ_TIMEOUT", "90")), diagnostics=True)
        app = create_app(backend, os.environ.get("PRISM_BRIDGE_API_KEY", ""),
                         oauth=make_oauth(args) if args.command == "serve" else None)
    except (BridgeError, ValueError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
