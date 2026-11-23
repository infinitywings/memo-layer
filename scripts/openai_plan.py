"""Run live experiments on the user's ChatGPT plan: OpenAI's Sign in with
ChatGPT for open-source and local apps (Preview), as documented at
https://developers.openai.com/siwc/token-sharing-open-source.

    login    sign in once in the browser; the credential record goes to
             ~/.config/rka-memory-dev/openai-plan.json (mode 0600)
    status   account, granted scopes and token expiry (never the tokens)
    models   the account's model slugs
    serve    the token proxy the workload containers call
    logout   revoke the refresh token and clear the record

The proxy holds the only copy of the tokens, refreshes them one at a time,
and forwards to https://api.openai.com/v1/responses with the fields the
plan route rejects removed and `store: false`, `stream: true` set. Two
routes serve the containers:

    POST /v1/responses          dsh's openai-responses route, streamed through
    POST /v1/chat/completions   the miner and the notepad curator: a plain
                                chat completion built from the streamed response
    GET  /v1/models             passed through

Callers authenticate with the local proxy key (proxy-key in the same
directory), never with an OpenAI token. Every request appends one line of
usage, without content, to openai-usage.jsonl there.

Usage: .venv/bin/python scripts/openai_plan.py login|status|models|logout
       .venv/bin/python scripts/openai_plan.py serve [--host 127.0.0.1] [--port 8790]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

AUTH = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH}/api/accounts/authorize"
TOKEN_URL = f"{AUTH}/api/accounts/oauth/token"
API = "https://api.openai.com/v1"
APP_NAME = "rka-memory"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
CALLBACK_PATH = "/auth/callback"
CALLBACK_PORT = 1455
DEFAULT_DIR = Path.home() / ".config" / "rka-memory-dev"
REFRESH_MARGIN_SECONDS = 300
# The plan route sometimes holds a stream open on keepalives alone; dsh then
# waits about 300 s before it retries. Completed responses took at most
# 10.6 s in the first 187 (2026-11-22), so a minute without progress is a
# stall: the proxy ends the stream unfinished and the caller retries.
STALL_SECONDS = 60.0

# preview-limitations: fields the plan route rejects
UNSUPPORTED = frozenset({
    "background", "conversation", "max_output_tokens", "max_tool_calls", "metadata", "moderation",
    "multi_agent", "prompt", "prompt_cache_retention", "safety_identifier", "temperature",
    "top_logprobs", "top_p", "truncation", "user", "previous_response_id", "service_tier",
})

# errors-and-recovery: structured codes, and the status a chat caller sees
PLAN_ERRORS = {
    "subscription_sharing_usage_limit_exceeded": 429,
    "subscription_sharing_usage_unavailable": 503,
    "subscription_sharing_user_unavailable": 503,
    "subscription_sharing_user_not_eligible": 403,
    "subscription_sharing_unsupported_capability": 400,
    "subscription_sharing_route_not_supported": 403,
    "subscription_sharing_invalid_user": 401,
}


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    dir: Path

    @property
    def record(self) -> Path:
        return self.dir / "openai-plan.json"

    @property
    def proxy_key(self) -> Path:
        return self.dir / "proxy-key"

    @property
    def usage(self) -> Path:
        return self.dir / "openai-usage.jsonl"


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_record(paths: Paths) -> dict[str, Any]:
    if not paths.record.exists():
        raise PlanError(f"not signed in: run `scripts/openai_plan.py login` ({paths.record} is missing)")
    return json.loads(paths.record.read_text())


def proxy_key(paths: Paths) -> str:
    if not paths.proxy_key.exists():
        write_private(paths.proxy_key, secrets.token_urlsafe(32) + "\n")
    return paths.proxy_key.read_text().strip()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


# ---------------------------------------------------------------- sign-in


@dataclass(frozen=True)
class Attempt:
    """One authorization attempt: what the callback and the code exchange
    must match."""

    client_id: str
    host_id: str
    redirect_uri: str
    state: str
    nonce: str
    verifier: str

    @classmethod
    def new(cls, client_id: str, host_id: str, port: int) -> Attempt:
        return cls(client_id, host_id, f"http://127.0.0.1:{port}{CALLBACK_PATH}",
                   secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48))

    @property
    def registering(self) -> bool:
        return self.client_id == "dynamic_agent_client"

    def url(self, id_token_hint: str | None = None) -> str:
        params = {
            "client_id": self.client_id,
            "ext_agent_host_id": self.host_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "resource": API,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge_method": "S256",
            "code_challenge": b64url(hashlib.sha256(self.verifier.encode()).digest()),
        }
        if self.registering:
            params["agent_name_hint"] = APP_NAME
        elif id_token_hint:
            params["id_token_hint"] = id_token_hint
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"

    def callback(self, query: dict[str, str]) -> tuple[str, str]:
        """The code and the client id to exchange it with."""
        if query.get("state") != self.state:
            raise PlanError("the callback's state does not match this sign-in attempt")
        if "error" in query:
            raise PlanError(f"sign-in stopped: {query['error']} {query.get('error_description', '')}".strip())
        if not query.get("code"):
            raise PlanError("the callback carries no code")
        issued = query.get("client_id")
        if self.registering:
            if not issued:
                raise PlanError("registration incomplete: the callback carries no issued client_id")
            return query["code"], issued
        if issued and issued != self.client_id:
            raise PlanError("the callback names a different client_id than this account's registration")
        return query["code"], self.client_id


def validate_id_token(id_token: str, client_id: str, nonce: str, now: float) -> dict[str, Any]:
    # The ID token comes straight from the token endpoint over TLS, which
    # OIDC Core 3.1.3.7 accepts in place of a signature check; the claims are
    # still checked.
    claims = jwt_claims(id_token)
    aud = claims.get("aud")
    if claims.get("iss") != AUTH:
        raise PlanError(f"ID token issuer is {claims.get('iss')!r}")
    if client_id not in (aud if isinstance(aud, list) else [aud]):
        raise PlanError("ID token audience is not this client")
    if claims.get("nonce") != nonce:
        raise PlanError("ID token nonce does not match this sign-in attempt")
    if float(claims.get("exp", 0)) < now:
        raise PlanError("ID token has expired")
    return claims


def record_from(tokens: dict[str, Any], claims: dict[str, Any], client_id: str, host_id: str, now: float) -> dict[str, Any]:
    scopes = sorted(tokens.get("scope", "").split())
    if PLAN_SCOPE not in scopes:
        raise PlanError("signed in, but ChatGPT plan usage was not granted; sign in again and allow plan usage")
    return {
        "email": claims.get("email"),
        "issuer": AUTH,
        "subject": claims["sub"],
        "client_id": client_id,
        "ext_agent_host_id": host_id,
        "id_token": tokens["id_token"],
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": int(tokens["expires_in"]),
        "earliest_refresh_at": tokens.get("earliest_refresh_at"),
        "scopes": scopes,
        "saved_at": now,
    }


def wait_for_callback(port: int) -> dict[str, str]:
    got: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_error(404)
                return
            got.update(dict(urllib.parse.parse_qsl(parsed.query)))
            body = b"<p>rka-memory: sign-in received. You can close this tab.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        while not got:
            server.handle_request()
    return got


def login(paths: Paths, port: int, open_browser: bool) -> int:
    old = json.loads(paths.record.read_text()) if paths.record.exists() else {}
    host_id = old.get("ext_agent_host_id") or f"urn:uuid:{uuid.uuid4()}"
    attempt = Attempt.new(old.get("client_id") or "dynamic_agent_client", host_id, port)
    url = attempt.url(old.get("id_token"))
    print("Open this URL, sign in to ChatGPT and allow plan usage:", flush=True)
    print(url if attempt.registering else url.split("&id_token_hint=")[0] + "&id_token_hint=<redacted>", flush=True)
    if open_browser:
        subprocess.run(["open", url], check=False)
    code, client_id = attempt.callback(wait_for_callback(port))
    resp = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "code_verifier": attempt.verifier, "redirect_uri": attempt.redirect_uri, "resource": API,
    }, timeout=30)
    if resp.status_code != 200:
        raise PlanError(f"code exchange answered HTTP {resp.status_code}: {resp.text[:300]}")
    tokens = resp.json()
    now = time.time()
    claims = validate_id_token(tokens["id_token"], client_id, attempt.nonce, now)
    if old.get("subject") and old.get("subject") != claims["sub"]:
        raise PlanError("this sign-in is a different ChatGPT account than the saved one; run logout first")
    write_private(paths.record, json.dumps(record_from(tokens, claims, client_id, host_id, now), indent=2))
    proxy_key(paths)
    print(f"signed in; plan usage granted; record in {paths.record}")
    return 0


# ---------------------------------------------------------------- tokens


class Tokens:
    """The credential record, refreshed under one lock: the refresh token
    rotates, so two refreshes must never race."""

    def __init__(self, paths: Paths, client: httpx.Client) -> None:
        self.paths = paths
        self.client = client
        self.lock = threading.Lock()
        self.record = read_record(paths)

    def _due(self, now: float, force: bool) -> bool:
        r = self.record
        earliest = float(r.get("earliest_refresh_at") or 0)
        if now < earliest:
            return False
        return force or now >= float(r["saved_at"]) + int(r["expires_in"]) - REFRESH_MARGIN_SECONDS

    def access(self, force: bool = False) -> str:
        with self.lock:
            now = time.time()
            if self._due(now, force):
                self._refresh(now)
            return self.record["access_token"]

    def _refresh(self, now: float) -> None:
        r = self.record
        resp = self.client.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "client_id": r["client_id"],
            "refresh_token": r["refresh_token"], "resource": API,
        })
        if resp.status_code != 200:
            raise PlanError(f"token refresh answered HTTP {resp.status_code}: {resp.text[:300]}; run login again")
        t = resp.json()
        self.record = {
            **r,
            "access_token": t["access_token"],
            "refresh_token": t.get("refresh_token", r["refresh_token"]),
            "expires_in": int(t["expires_in"]),
            "earliest_refresh_at": t.get("earliest_refresh_at"),
            "scopes": sorted(t["scope"].split()) if t.get("scope") else r["scopes"],
            "id_token": t.get("id_token", r["id_token"]),
            "saved_at": now,
        }
        write_private(self.paths.record, json.dumps(self.record, indent=2))


# ---------------------------------------------------------------- requests


def sanitize(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A Responses body the plan route accepts, and the fields dropped."""
    dropped = sorted(k for k in body if k in UNSUPPORTED)
    out = {k: v for k, v in body.items() if k not in UNSUPPORTED}
    out["store"] = False
    out["stream"] = True
    items = out.get("input")
    if isinstance(items, list):
        out["input"] = [{**i, "role": "developer"} if isinstance(i, dict) and i.get("role") == "system" else i for i in items]
    return out, dropped


def chat_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    """A text-only chat completion request as a Responses request."""
    system = [m["content"] for m in body.get("messages", []) if m.get("role") == "system"]
    items = [{"role": m["role"], "content": m["content"]} for m in body.get("messages", []) if m.get("role") != "system"]
    out: dict[str, Any] = {"model": body["model"], "input": items}
    if system:
        out["instructions"] = "\n\n".join(system)
    return out


def sse_events(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    data: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line.strip() and data:
            text = "\n".join(data)
            data = []
            if text != "[DONE]":
                yield json.loads(text)
    if data and data != ["[DONE]"]:
        yield json.loads("\n".join(data))


def output_text(response: dict[str, Any]) -> str:
    return "".join(
        part.get("text", "")
        for item in response.get("output", []) if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") == "output_text"
    )


def usage_of(response: dict[str, Any]) -> dict[str, int]:
    u = response.get("usage") or {}
    return {
        "input": int(u.get("input_tokens") or 0),
        "cached": int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0),
        "output": int(u.get("output_tokens") or 0),
        "reasoning": int((u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0),
    }


@dataclass
class Outcome:
    """What one upstream response came to."""

    status: int
    terminal: str | None = None
    response: dict[str, Any] | None = None
    code: str | None = None
    started: float = 0.0
    events: int = 0
    first_event: float | None = None
    last_event: float | None = None
    last_type: str | None = None
    last_progress: float = 0.0

    def scan(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        now = round(time.monotonic() - self.started, 2)
        self.events += 1
        self.first_event = now if self.first_event is None else self.first_event
        self.last_event, self.last_type = now, kind
        if kind != "keepalive":
            self.last_progress = now
        if kind in ("response.completed", "response.failed", "response.incomplete"):
            self.terminal = kind
            self.response = event.get("response") or {}
            self.code = ((self.response.get("error") or {}).get("code")) or self.code
        elif kind == "error":
            self.terminal = "error"
            self.code = event.get("code") or (event.get("error") or {}).get("code")

    def stalled(self, limit: float) -> bool:
        return self.terminal is None and time.monotonic() - self.started - self.last_progress > limit

    def timing(self) -> dict[str, Any]:
        """When events arrived, for streams that stall: seconds from the request."""
        return {"events": self.events, "first_event": self.first_event, "last_event": self.last_event, "last_type": self.last_type}


class Proxy:
    def __init__(self, paths: Paths, tokens: Tokens, client: httpx.Client, upstream: str = API) -> None:
        self.paths = paths
        self.tokens = tokens
        self.client = client
        self.upstream = upstream
        self.key = proxy_key(paths)
        self.stall_seconds = STALL_SECONDS
        self.log_lock = threading.Lock()

    def log(self, **row: Any) -> None:
        line = json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"), **row})
        with self.log_lock, open(self.paths.usage, "a") as fh:
            fh.write(line + "\n")

    def authorized(self, header: str | None) -> bool:
        return secrets.compare_digest(header or "", f"Bearer {self.key}")

    def open_stream(self, body: dict[str, Any]) -> httpx.Response:
        """POST to /responses; one forced refresh and retry on a 401."""
        for force in (False, True):
            req = self.client.build_request("POST", f"{self.upstream}/responses", json=body, headers={
                "Authorization": f"Bearer {self.tokens.access(force=force)}", "Accept": "text/event-stream"})
            resp = self.client.send(req, stream=True)
            if resp.status_code != 401 or force:
                return resp
            resp.close()
        raise AssertionError("unreachable")

    def models(self) -> httpx.Response:
        return self.client.get(f"{self.upstream}/models", headers={"Authorization": f"Bearer {self.tokens.access()}"})

    def chat(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """A chat completion from the streamed response, or an error body."""
        request, _ = sanitize(chat_to_responses(body))
        started = time.monotonic()
        resp = self.open_stream(request)
        outcome = Outcome(resp.status_code, started=started)
        try:
            if resp.status_code != 200:
                text = resp.read().decode(errors="replace")
                self.log(route="chat", model=body.get("model"), status=resp.status_code, detail=text[:200])
                return resp.status_code, {"error": {"message": text[:500], "code": "upstream_http"}}
            for event in sse_events(resp.iter_lines()):
                outcome.scan(event)
                if outcome.stalled(self.stall_seconds):
                    outcome.terminal = "stalled"
                    break
        finally:
            resp.close()
        response = outcome.response or {}
        usage = usage_of(response)
        self.log(route="chat", model=body.get("model"), status=resp.status_code, terminal=outcome.terminal,
                 code=outcome.code, seconds=round(time.monotonic() - started, 2), **usage)
        if outcome.terminal != "response.completed":
            status = PLAN_ERRORS.get(outcome.code or "", 502)
            return status, {"error": {"message": f"response ended with {outcome.terminal}", "code": outcome.code}}
        return 200, {
            "id": response.get("id", "chatcmpl-plan"),
            "object": "chat.completion",
            "model": response.get("model", body.get("model")),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": output_text(response)}}],
            "usage": {"prompt_tokens": usage["input"], "completion_tokens": usage["output"],
                      "total_tokens": usage["input"] + usage["output"]},
        }


def handler_for(proxy: Proxy) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _json(self, status: int, doc: dict[str, Any]) -> None:
            raw = json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict[str, Any]:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

        def _guard(self) -> bool:
            if proxy.authorized(self.headers.get("Authorization")):
                return True
            self._json(401, {"error": {"message": "wrong or missing proxy key", "code": "proxy_key"}})
            return False

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._json(200, {"ok": True})
                return
            if not self._guard():
                return
            if self.path.rstrip("/") == "/v1/models":
                try:
                    resp = proxy.models()
                except PlanError as exc:
                    self._json(503, {"error": {"message": str(exc), "code": "proxy_tokens"}})
                    return
                self._json(resp.status_code, resp.json())
                return
            self._json(404, {"error": {"message": f"no route {self.path}"}})

        def do_POST(self) -> None:
            if not self._guard():
                return
            try:
                body = self._body()
                if self.path == "/v1/chat/completions":
                    self._json(*proxy.chat(body))
                elif self.path == "/v1/responses":
                    self._responses(body)
                else:
                    self._json(404, {"error": {"message": f"no route {self.path}"}})
            except PlanError as exc:
                self._json(503, {"error": {"message": str(exc), "code": "proxy_tokens"}})
            except httpx.HTTPError as exc:
                self._json(502, {"error": {"message": f"upstream unreachable: {exc}", "code": "upstream_unreachable"}})

        def _responses(self, body: dict[str, Any]) -> None:
            request, dropped = sanitize(body)
            started = time.monotonic()
            resp = proxy.open_stream(request)
            outcome = Outcome(resp.status_code, started=started)
            try:
                self.send_response(resp.status_code)
                self.send_header("Content-Type", resp.headers.get("content-type", "text/event-stream"))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                pending: list[str] = []
                for line in resp.iter_lines():
                    self.wfile.write((line + "\n").encode())
                    self.wfile.flush()
                    if line.strip():
                        pending.append(line)
                    elif pending:
                        for event in sse_events(iter([*pending, ""])):
                            outcome.scan(event)
                        pending = []
                        if outcome.stalled(proxy.stall_seconds):
                            # ending the stream unfinished makes the caller retry
                            outcome.terminal = "stalled"
                            break
                for event in sse_events(iter(pending)):
                    outcome.scan(event)
            except (BrokenPipeError, ConnectionResetError):
                outcome.terminal = outcome.terminal or "client_gone"
            finally:
                resp.close()
                self.close_connection = True
            proxy.log(route="responses", model=body.get("model"), status=resp.status_code, terminal=outcome.terminal,
                      code=outcome.code, dropped=dropped, keys=sorted(request), tools=len(request.get("tools") or []),
                      seconds=round(time.monotonic() - started, 2), **outcome.timing(), **usage_of(outcome.response or {}))

        def log_message(self, *_: Any) -> None:
            pass

    return Handler


# ---------------------------------------------------------------- commands


def status(paths: Paths) -> int:
    r = read_record(paths)
    expires = datetime.fromtimestamp(float(r["saved_at"]) + int(r["expires_in"]), UTC)
    print(f"signed in; client {r['client_id'][:12]}...; scopes {' '.join(r['scopes'])}")
    print(f"access token expires {expires.isoformat(timespec='seconds')}; record {paths.record}")
    return 0


def models(paths: Paths) -> int:
    with httpx.Client(timeout=30) as client:
        tokens = Tokens(paths, client)
        resp = client.get(f"{API}/models", headers={"Authorization": f"Bearer {tokens.access()}"})
    if resp.status_code != 200:
        raise PlanError(f"/models answered HTTP {resp.status_code}: {resp.text[:300]}")
    for m in resp.json().get("models", []):
        if m.get("visibility") == "list":
            print(f"{m.get('slug')}\t{m.get('display_name')}")
    return 0


def logout(paths: Paths) -> int:
    r = read_record(paths)
    with httpx.Client(timeout=30) as client:
        endpoint = client.get(f"{AUTH}/.well-known/openid-configuration").json()["revocation_endpoint"]
        resp = client.post(endpoint, data={"token": r["refresh_token"], "token_type_hint": "refresh_token", "client_id": r["client_id"]})
    kept = {k: r[k] for k in ("client_id", "ext_agent_host_id", "email", "subject") if k in r}
    write_private(paths.record.with_name("openai-plan.registration.json"), json.dumps(kept, indent=2))
    paths.record.unlink()
    print("revoked" if resp.status_code == 200 else f"revocation answered HTTP {resp.status_code}; tokens cleared locally anyway")
    return 0


def serve(paths: Paths, host: str, port: int) -> int:
    client = httpx.Client(timeout=httpx.Timeout(600, connect=30))
    proxy = Proxy(paths, Tokens(paths, client), client)
    proxy.tokens.access()
    server = ThreadingHTTPServer((host, port), handler_for(proxy))
    server.daemon_threads = True
    print(f"plan proxy on http://{host}:{port}/v1; usage log {paths.usage}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["login", "status", "models", "serve", "logout"])
    ap.add_argument("--dir", type=Path, default=Path(os.environ.get("RKM_OPENAI_PLAN_DIR") or DEFAULT_DIR))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--callback-port", type=int, default=CALLBACK_PORT)
    ap.add_argument("--no-browser", action="store_true", help="print the sign-in URL without opening it")
    args = ap.parse_args(argv)
    paths = Paths(args.dir)
    try:
        if args.command == "login":
            return login(paths, args.callback_port, not args.no_browser)
        if args.command == "serve":
            return serve(paths, args.host, args.port)
        return {"status": status, "models": models, "logout": logout}[args.command](paths)
    except PlanError as exc:
        print(f"openai_plan: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
