"""Sync and pull against a throwaway RKA Core server, never the user's live one."""

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

from rka_memory.domain import Actor, Event, Kind
from rka_memory.ledger import core
from rka_memory.service import Memory

RKA = Path.home() / ".local" / "bin" / "rka"
pytestmark = pytest.mark.skipif(not RKA.exists(), reason="RKA Core CLI not installed at ~/.local/bin/rka")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def core_url(tmp_path_factory):
    data = tmp_path_factory.mktemp("core")
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("RKA_")}
    env.update(RKA_DATA_DIR=str(data), RKA_DB_PATH=str(data / "rka.db"), RKA_PROJECT_DIR=str(data))
    with open(data / "serve.log", "w") as log:
        proc = subprocess.Popen(
            [str(RKA), "serve", "--port", str(port)], cwd=data, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while True:
        try:
            if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            pytest.fail(f"isolated RKA Core did not start:\n{(data / 'serve.log').read_text()[-2000:]}")
        time.sleep(0.2)
    yield url
    proc.terminate()
    proc.wait(timeout=15)


def ev(n: int, kind: Kind, actor: Actor, session: str, payload: dict) -> Event:
    return Event.new(kind, actor, session, payload, id=f"ev_{n:013x}{n:08x}", ts=f"2026-11-0{1 + n % 3}T09:00:00+00:00")


EVENTS = [
    ev(1, Kind.SESSION_START, Actor.RUNNER, "s1", {"state": {"project": "/p"}, "source": "startup"}),
    ev(2, Kind.USER_MSG, Actor.USER, "s1", {"text": "fix the parser", "sha": "abc", "prompt_id": "p1"}),
    ev(3, Kind.TOOL_CALL, Actor.MODEL, "s1", {"tool": "Bash", "input": "uv run pytest", "tool_use_id": "t1"}),
    ev(4, Kind.EXIT_CODE, Actor.TOOL, "s1", {"code": 0, "command": "uv run pytest", "tool_use_id": "t1"}),
    ev(5, Kind.TEST_RESULT, Actor.TOOL, "s1", {"passed": True, "command": "uv run pytest", "tool_use_id": "t1"}),
    ev(6, Kind.COST, Actor.RUNNER, "engine", {"component": "gate", "session_id": "s1", "tokens": 0, "seconds": 0.01}),
    ev(7, Kind.SESSION_END, Actor.RUNNER, "s1", {"reason": "other", "tokens_total": 9}),
]


def notes_in_core(url: str, project_id: str) -> list[dict]:
    resp = httpx.get(
        f"{url}/api/notes", params={"tags": ["rka-memory"], "limit": 200}, headers={"X-RKA-Project": project_id}
    )
    return resp.json()


def test_sync_is_idempotent_and_pull_restores_ledger_order(core_url, tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("RKA_MEMORY_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("RKA_MEMORY_CONFIG", str(config_path))
    config_path.write_text(yaml.safe_dump({"core": {"url": core_url}}))

    memory = Memory.open()
    memory.append(*EVENTS[:3], EVENTS[0], *EVENTS[3:])
    with pytest.raises(core.CoreError, match="run `rka-memory init` first"):
        core.sync(memory)

    project = core.init_project(memory, name="rka-memory test ledger")
    assert yaml.safe_load(config_path.read_text()) == {"core": {"url": core_url, "project_id": project["id"]}}
    memory = Memory.open()

    assert core.sync(memory).to_json() == {"project_id": project["id"], "sent": 7, "duplicates_skipped": 1, "cursor": 8}
    assert core.sync(memory).to_json() == {"project_id": project["id"], "sent": 0, "duplicates_skipped": 0, "cursor": 8}
    (tmp_path / "ledger" / "core_cursor.json").unlink()
    assert core.sync(memory).to_json() == {"project_id": project["id"], "sent": 7, "duplicates_skipped": 1, "cursor": 8}

    notes = notes_in_core(core_url, project["id"])
    assert len(notes) == 7
    exit_note = next(n for n in notes if json.loads(n["content"])["id"] == EVENTS[3].id)
    assert (exit_note["type"], exit_note["source"], exit_note["summary"]) == ("log", "executor", "exit_code by tool")
    assert sorted(exit_note["tags"]) == ["rka-memory", "rkm-kind:exit_code", "rkm-seq:000000000004", "rkm-session:s1"]
    assert json.loads(exit_note["content"]) == EVENTS[3].to_json()

    pulled = core.pull(memory, page_size=3)
    assert (pulled.pulled, pulled.skipped) == (7, 0)
    restored = [Event.from_json(json.loads(line)) for line in Path(pulled.path).read_text().splitlines()]
    assert restored == EVENTS
    assert memory.ledger.read() == [*EVENTS[:3], EVENTS[0], *EVENTS[3:]]

    shutil.copy(pulled.path, memory.ledger.path)
    (tmp_path / "ledger" / "core_cursor.json").unlink()
    assert core.sync(memory).to_json() == {"project_id": project["id"], "sent": 7, "duplicates_skipped": 0, "cursor": 7}
    assert len(notes_in_core(core_url, project["id"])) == 7
