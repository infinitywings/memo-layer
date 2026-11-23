"""scripts/openai_plan.py against a fake OpenAI: the sign-in checks, the
token refresh, and the proxy's two routes. Nothing here calls OpenAI."""

import base64
import hashlib
import importlib.util
import json
import stat
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("openai_plan", REPO / "scripts" / "openai_plan.py")
plan = sys.modules["openai_plan"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


def jwt(claims: dict) -> str:
    def enc(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


def record(tmp_path: Path, saved_at: float, **extra) -> plan.Paths:
    paths = plan.Paths(tmp_path)
    plan.write_private(paths.record, json.dumps({
        "email": "u@example.com", "subject": "sub-1", "client_id": "oaiapp_1", "ext_agent_host_id": "urn:uuid:h",
        "id_token": "x", "access_token": "old-access", "refresh_token": "refresh-1", "token_type": "Bearer",
        "expires_in": 3600, "scopes": ["chatgpt.tokens.use.direct"], "saved_at": saved_at, **extra}))
    return paths


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def completed(text: str) -> dict:
    return {"type": "response.completed", "response": {
        "id": "resp_1", "model": "m1", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 100, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 40},
                  "output_tokens_details": {"reasoning_tokens": 3}}}}


class Upstream:
    """A fake auth.openai.com and api.openai.com that records what it got."""

    def __init__(self, stream: bytes = b"", fail_first_401: bool = False) -> None:
        self.stream = stream
        self.fail_first_401 = fail_first_401
        self.refreshes: list[dict] = []
        self.bodies: list[dict] = []
        self.auth: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth/token"):
            form = dict(urllib.parse.parse_qsl(req.content.decode()))
            self.refreshes.append(form)
            n = len(self.refreshes)
            return httpx.Response(200, json={"access_token": f"access-{n}", "refresh_token": f"refresh-{n + 1}",
                                             "expires_in": 3600, "scope": "chatgpt.tokens.use.direct offline_access"})
        self.auth.append(req.headers["Authorization"])
        if self.fail_first_401 and len(self.auth) == 1:
            return httpx.Response(401, json={"detail": "expired"})
        self.bodies.append(json.loads(req.content))
        return httpx.Response(200, content=self.stream, headers={"content-type": "text/event-stream"})


def proxy_for(paths: plan.Paths, upstream: Upstream) -> plan.Proxy:
    client = httpx.Client(transport=httpx.MockTransport(upstream))
    return plan.Proxy(paths, plan.Tokens(paths, client), client, upstream="https://api.test/v1")


def test_first_sign_in_registers_with_pkce_on_a_loopback_callback():
    a = plan.Attempt.new("dynamic_agent_client", "urn:uuid:h", 1455)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(a.url()).query))
    assert q["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert (q["agent_name_hint"], q["resource"], q["code_challenge_method"]) == ("rka-memory", "https://api.openai.com/v1", "S256")
    assert q["code_challenge"] == base64.urlsafe_b64encode(hashlib.sha256(a.verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["scope"].split() == ["openid", "profile", "email", "offline_access", "resource.invoke", "chatgpt.tokens.use.direct"]
    assert "id_token_hint" not in q


def test_the_callback_must_match_the_attempt_and_name_the_issued_client():
    a = plan.Attempt.new("dynamic_agent_client", "urn:uuid:h", 1455)
    assert a.callback({"state": a.state, "code": "c", "client_id": "oaiapp_9"}) == ("c", "oaiapp_9")
    with pytest.raises(plan.PlanError, match="state"):
        a.callback({"state": "other", "code": "c", "client_id": "oaiapp_9"})
    with pytest.raises(plan.PlanError, match="registration incomplete"):
        a.callback({"state": a.state, "code": "c"})
    with pytest.raises(plan.PlanError, match="access_denied"):
        a.callback({"state": a.state, "error": "access_denied"})
    again = plan.Attempt.new("oaiapp_9", "urn:uuid:h", 1455)
    with pytest.raises(plan.PlanError, match="different client_id"):
        again.callback({"state": again.state, "code": "c", "client_id": "oaiapp_other"})


def test_the_id_token_and_the_plan_scope_are_checked_before_anything_is_saved():
    now = time.time()
    good = {"iss": "https://auth.openai.com", "aud": "oaiapp_9", "nonce": "n1", "exp": now + 60, "sub": "s", "email": "u@x"}
    assert plan.validate_id_token(jwt(good), "oaiapp_9", "n1", now)["sub"] == "s"
    with pytest.raises(plan.PlanError, match="nonce"):
        plan.validate_id_token(jwt(good), "oaiapp_9", "n2", now)
    with pytest.raises(plan.PlanError, match="audience"):
        plan.validate_id_token(jwt(good), "oaiapp_8", "n1", now)
    tokens = {"id_token": "i", "access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "openid email"}
    with pytest.raises(plan.PlanError, match="plan usage was not granted"):
        plan.record_from(tokens, good, "oaiapp_9", "urn:uuid:h", now)


def test_a_responses_body_loses_the_fields_the_plan_route_rejects():
    body = {"model": "m", "input": [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}],
            "temperature": 0, "max_output_tokens": 99, "previous_response_id": "r", "store": True, "tools": []}
    out, dropped = plan.sanitize(body)
    assert dropped == ["max_output_tokens", "previous_response_id", "temperature"]
    assert (out["store"], out["stream"], out["input"][0]["role"], out["tools"]) == (False, True, "developer", [])


def test_an_expired_token_is_refreshed_once_under_concurrency_and_saved_with_mode_600(tmp_path):
    paths = record(tmp_path, saved_at=time.time() - 3600)
    up = Upstream()
    tokens = plan.Tokens(paths, httpx.Client(transport=httpx.MockTransport(up)))
    got = []
    threads = [threading.Thread(target=lambda: got.append(tokens.access())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got == ["access-1"] * 8 and len(up.refreshes) == 1
    assert up.refreshes[0] == {"grant_type": "refresh_token", "client_id": "oaiapp_1", "refresh_token": "refresh-1",
                               "resource": "https://api.openai.com/v1"}
    saved = json.loads(paths.record.read_text())
    assert (saved["refresh_token"], saved["access_token"]) == ("refresh-2", "access-1")
    assert stat.S_IMODE(paths.record.stat().st_mode) == 0o600


def test_a_fresh_token_is_not_refreshed_before_earliest_refresh_at(tmp_path):
    paths = record(tmp_path, saved_at=time.time(), earliest_refresh_at=time.time() + 600)
    up = Upstream()
    tokens = plan.Tokens(paths, httpx.Client(transport=httpx.MockTransport(up)))
    assert tokens.access(force=True) == "old-access" and up.refreshes == []


def test_the_chat_route_builds_a_completion_from_the_stream(tmp_path):
    up = Upstream(sse({"type": "response.output_text.delta", "delta": "[]"}, completed("[]")))
    proxy = proxy_for(record(tmp_path, saved_at=time.time()), up)
    status, doc = proxy.chat({"model": "m1", "temperature": 0,
                              "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "mine this"}]})
    assert status == 200
    assert doc["choices"][0]["message"]["content"] == "[]"
    assert doc["usage"] == {"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107}
    assert up.bodies == [{"model": "m1", "input": [{"role": "user", "content": "mine this"}], "instructions": "rules",
                          "store": False, "stream": True}]
    [row] = [json.loads(line) for line in proxy.paths.usage.read_text().splitlines()]
    assert (row["route"], row["terminal"], row["input"], row["cached"], row["output"]) == ("chat", "response.completed", 100, 40, 7)


def test_a_plan_limit_reaches_the_miner_as_429(tmp_path):
    failed = {"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}
    proxy = proxy_for(record(tmp_path, saved_at=time.time()), Upstream(sse(failed)))
    status, doc = proxy.chat({"model": "m1", "messages": [{"role": "user", "content": "x"}]})
    assert (status, doc["error"]["code"]) == (429, "subscription_sharing_usage_limit_exceeded")


def test_a_401_forces_one_refresh_and_a_retry(tmp_path):
    up = Upstream(sse(completed("ok")), fail_first_401=True)
    proxy = proxy_for(record(tmp_path, saved_at=time.time()), up)
    assert proxy.chat({"model": "m1", "messages": [{"role": "user", "content": "x"}]})[0] == 200
    assert up.auth == ["Bearer old-access", "Bearer access-1"] and len(up.refreshes) == 1


def test_the_responses_route_streams_through_and_wants_the_proxy_key(tmp_path):
    stream = sse({"type": "response.output_text.delta", "delta": "he"}, completed("hello"))
    proxy = proxy_for(record(tmp_path, saved_at=time.time()), Upstream(stream))
    server = ThreadingHTTPServer(("127.0.0.1", 0), plan.handler_for(proxy))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        assert httpx.post(f"{base}/responses", json={"model": "m1"}, headers={"Authorization": "Bearer nope"}).status_code == 401
        with httpx.stream("POST", f"{base}/responses", json={"model": "m1", "temperature": 1, "input": []},
                          headers={"Authorization": f"Bearer {proxy.key}"}) as resp:
            events = list(plan.sse_events(resp.iter_lines()))
        assert [e["type"] for e in events] == ["response.output_text.delta", "response.completed"]
    finally:
        server.shutdown()
    [row] = [json.loads(line) for line in proxy.paths.usage.read_text().splitlines()]
    assert (row["route"], row["dropped"], row["terminal"], row["output"]) == ("responses", ["temperature"], "response.completed", 7)


def test_a_stream_held_open_on_keepalives_is_ended_unfinished(tmp_path):
    stalled = sse({"type": "response.created"}, {"type": "keepalive"}, {"type": "keepalive"}, completed("late"))
    proxy = proxy_for(record(tmp_path, saved_at=time.time()), Upstream(stalled))
    proxy.stall_seconds = -1.0
    status, doc = proxy.chat({"model": "m1", "messages": [{"role": "user", "content": "x"}]})
    assert (status, doc["error"]["message"]) == (502, "response ended with stalled")
    server = ThreadingHTTPServer(("127.0.0.1", 0), plan.handler_for(proxy))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.stream("POST", f"http://127.0.0.1:{server.server_port}/v1/responses", json={"model": "m1", "input": []},
                          headers={"Authorization": f"Bearer {proxy.key}"}) as resp:
            types = [e["type"] for e in plan.sse_events(resp.iter_lines())]
    finally:
        server.shutdown()
    assert "response.completed" not in types
    rows = [json.loads(line) for line in proxy.paths.usage.read_text().splitlines()]
    assert [r["terminal"] for r in rows] == ["stalled", "stalled"]


def post_responses(proxy: plan.Proxy) -> httpx.Response:
    server = ThreadingHTTPServer(("127.0.0.1", 0), plan.handler_for(proxy))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return httpx.post(f"http://127.0.0.1:{server.server_port}/v1/responses", json={"model": "m1", "input": []},
                          headers={"Authorization": f"Bearer {proxy.key}"})
    finally:
        server.shutdown()


def overloaded() -> dict:
    return {"type": "response.failed", "response": {"error": {"code": "server_is_overloaded", "message": "try later"}}}


def test_an_overload_before_any_output_is_answered_with_a_503_the_caller_retries(tmp_path):
    proxy = proxy_for(record(tmp_path, saved_at=time.time()), Upstream(sse({"type": "response.created"}, overloaded())))
    resp = post_responses(proxy)
    assert (resp.status_code, resp.json()["error"]["code"]) == (503, "server_is_overloaded")
    [row] = [json.loads(line) for line in proxy.paths.usage.read_text().splitlines()]
    assert (row["status"], row["terminal"], row["code"]) == (503, "response.failed", "server_is_overloaded")


def test_a_failure_after_output_has_started_streams_through(tmp_path):
    stream = sse({"type": "response.created"}, {"type": "response.output_text.delta", "delta": "he"}, overloaded())
    resp = post_responses(proxy_for(record(tmp_path, saved_at=time.time()), Upstream(stream)))
    types = [e["type"] for e in plan.sse_events(iter(resp.text.splitlines()))]
    assert (resp.status_code, types) == (200, ["response.created", "response.output_text.delta", "response.failed"])
