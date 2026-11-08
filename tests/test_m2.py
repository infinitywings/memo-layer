"""Simulated clock, the openai_compat miner backend, inline background work."""

import json
import os
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ledger_builder import cfg
from rka_memory.domain import Actor, Event, Kind, clock_now
from rka_memory.ledger import hooks
from rka_memory.miner import backends


def test_clock_file_jumps_and_then_runs_at_real_speed(tmp_path, monkeypatch):
    clock = tmp_path / "clock"
    clock.write_text("2027-03-01T09:00:00+00:00")
    monkeypatch.setenv("RKA_MEMORY_CLOCK_FILE", str(clock))
    first = clock_now()
    assert timedelta(0) <= first - datetime(2027, 3, 1, 9, tzinfo=UTC) < timedelta(seconds=5)
    ev = Event.new(Kind.SESSION_END, Actor.RUNNER, "s", {})
    assert ev.ts.startswith("2027-03-01T09:00")
    os.utime(clock, (time.time() - 3600, time.time() - 3600))
    assert clock_now() - first >= timedelta(minutes=59)
    monkeypatch.setenv("RKA_MEMORY_NOW", "2027-05-01T00:00:00")
    assert clock_now() == datetime(2027, 5, 1, tzinfo=UTC)


def test_openai_compat_posts_the_prompt_and_reads_text_and_tokens(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"candidates": []}'}}], "usage": {"total_tokens": 321}})

    monkeypatch.setenv("MINER_KEY", "sk-test")
    c = cfg(miner={"backend": "openai_compat", "model": "deepseek-chat", "base_url": "http://llm:8000/v1/", "api_key_env": "MINER_KEY"})
    out = backends.openai_compat(c, transport=httpx.MockTransport(handler))("PROMPT")
    assert (out.text, out.tokens) == ('{"candidates": []}', 321)
    assert seen["url"] == "http://llm:8000/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert seen["body"]["model"] == "deepseek-chat"


def test_openai_compat_turns_http_errors_into_backend_errors(monkeypatch):
    monkeypatch.setattr(backends, "RETRY_SECONDS", 0)
    c = cfg(miner={"backend": "openai_compat", "base_url": "http://llm/v1"})
    seen = []
    run = backends.openai_compat(c, transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(503, text="busy")))
    with pytest.raises(backends.BackendError, match="HTTP 503"):
        run("x")
    assert len(seen) == backends.ATTEMPTS


def test_openai_compat_retries_a_transient_failure_once(monkeypatch):
    monkeypatch.setattr(backends, "RETRY_SECONDS", 0)
    c = cfg(miner={"backend": "openai_compat", "base_url": "http://llm/v1"})
    replies = [httpx.Response(429, text="slow down"), httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})]
    run = backends.openai_compat(c, transport=httpx.MockTransport(lambda r: replies.pop(0)))
    assert run("x").text == "{}"
    assert replies == []


def test_a_client_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(backends, "RETRY_SECONDS", 0)
    c = cfg(miner={"backend": "openai_compat", "base_url": "http://llm/v1"})
    seen = []
    run = backends.openai_compat(c, transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(401, text="bad key")))
    with pytest.raises(backends.BackendError, match="HTTP 401"):
        run("x")
    assert len(seen) == 1


def test_inline_background_mines_before_session_end_returns(memory, monkeypatch, capsys):
    from dataclasses import replace

    from rka_memory.miner import miner

    calls, spawned = [], []
    monkeypatch.setattr(
        miner, "mine_session", lambda mem, sid, force=False, backend=None: calls.append(sid) or miner.MineResult(sid, miner.MineStatus.MINED)
    )
    monkeypatch.setattr(hooks, "spawn", lambda commands, log_path: spawned.append(commands))
    monkeypatch.setattr(hooks.Memory, "open", classmethod(lambda cls, c=None: replace(memory, cfg=replace(memory.cfg, hooks_background="inline"))))
    payload = {"session_id": "s1", "transcript_path": "", "cwd": "/tmp", "hook_event_name": "SessionEnd"}
    assert hooks.main("SessionEnd", json.dumps(payload)) == 0
    assert (calls, spawned) == (["s1"], [])
    assert capsys.readouterr().out == ""


def test_an_inline_mining_failure_is_logged_and_the_session_stays_unmined(memory, monkeypatch):
    from dataclasses import replace

    from rka_memory.miner import miner

    failed = miner.MineResult("s1", miner.MineStatus.BACKEND_ERROR, "miner endpoint answered HTTP 503: busy")
    monkeypatch.setattr(miner, "mine_session", lambda mem, sid, force=False, backend=None: failed)
    monkeypatch.setattr(hooks.Memory, "open", classmethod(lambda cls, c=None: replace(memory, cfg=replace(memory.cfg, hooks_background="inline"))))
    payload = {"session_id": "s1", "transcript_path": "", "cwd": "/tmp", "hook_event_name": "SessionEnd"}
    assert hooks.main("SessionEnd", json.dumps(payload)) == 0
    assert (memory.cfg.ledger_dir / "miner-errors.log").read_text() == "s1 backend_error: miner endpoint answered HTTP 503: busy\n"
    assert not (memory.cfg.ledger_dir / "hook-errors.log").exists()
