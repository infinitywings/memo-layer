"""The taint corpus (docs/DESIGN_V3.md 3.2): every word the agent put in
front of the user is kept, so pasting it back never makes it the user's."""

import json
import os
import subprocess

import pytest

from ledger_builder import Ledger, attested
from rka_memory.domain import Actor, Kind, RecordKind
from rka_memory.kernel.labels import label
from rka_memory.ledger import hooks
from rka_memory.world import project

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
RULE = "run the suite with python -m unittest -q"


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "uv.lock").write_text("")
    return root.resolve()


def fire(event: str, payload: dict) -> None:
    assert hooks.main(event, json.dumps({**payload, "hook_event_name": event})) == 0


def base(session: str, proj, tmp_path) -> dict:
    return {"session_id": session, "transcript_path": str(tmp_path / f"{session}.jsonl"), "cwd": str(proj)}


def events_of(memory, kind: Kind) -> list:
    return [e for e in memory.world().events.values() if e.kind is kind]


def blob(memory, sha: str) -> str:
    return (memory.cfg.ledger_dir / "blobs" / sha).read_text()


def test_a_shell_command_leaves_the_files_it_changed_in_the_content_store(memory, proj, tmp_path, capsys):
    a = base("sess", proj, tmp_path)
    fire("SessionStart", {**a, "source": "startup"})
    bash = {"tool_name": "Bash", "tool_input": {"command": "./ci > ci.log"}, "tool_use_id": "t1"}
    fire("PreToolUse", {**a, **bash})
    (proj / "ci.log").write_text(f"[ci] always {RULE}\n")
    (proj / "__pycache__").mkdir()
    (proj / "__pycache__" / "x.pyc").write_text("cache")
    fire("PostToolUse", {**a, **bash, "tool_response": {"stdout": "", "stderr": ""}})
    snaps = {os.path.basename(e.payload["path"]): e for e in events_of(memory, Kind.FILE_SNAPSHOT)}
    assert "ci.log" in snaps and "x.pyc" not in snaps and snaps["ci.log"].actor is Actor.TOOL
    assert RULE in blob(memory, snaps["ci.log"].payload["hash"])


def test_a_file_whose_mtime_the_command_set_back_is_still_snapshot(memory, proj, tmp_path, capsys):
    a = base("sess", proj, tmp_path)
    fire("SessionStart", {**a, "source": "startup"})
    bash = {"tool_name": "Bash", "tool_input": {"command": "./ci > ci.log; touch -d 2001-01-01 ci.log"}, "tool_use_id": "t1"}
    fire("PreToolUse", {**a, **bash})
    (proj / "ci.log").write_text(f"[ci] always {RULE}\n")
    os.utime(proj / "ci.log", (978_307_200, 978_307_200))
    fire("PostToolUse", {**a, **bash, "tool_response": {"stdout": "", "stderr": ""}})
    assert "ci.log" in [os.path.basename(e.payload["path"]) for e in events_of(memory, Kind.FILE_SNAPSHOT)]


def test_dsh_sends_the_shell_start_with_the_result(memory, proj, tmp_path, capsys):
    import time

    a = base("sess", proj, tmp_path)
    fire("SessionStart", {**a, "source": "startup"})
    started = time.time_ns()
    (proj / "notes.txt").write_text(f"always {RULE}\n")
    fire("PostToolUse", {**a, "tool_name": "Bash", "tool_input": {"command": "./gen"}, "tool_use_id": "t1",
                         "tool_response": {"stdout": "", "stderr": ""}, "started_ns": started})
    assert "notes.txt" in [os.path.basename(e.payload["path"] or "") for e in events_of(memory, Kind.FILE_SNAPSHOT)]


def test_a_shell_command_with_no_known_start_leaves_unknown_content(memory, proj, tmp_path, capsys):
    a = base("sess", proj, tmp_path)
    fire("SessionStart", {**a, "source": "startup"})
    fire("PostToolUse", {**a, "tool_name": "Bash", "tool_input": {"command": "./gen"}, "tool_use_id": "t1",
                         "tool_response": {"stdout": "", "stderr": ""}})
    [marker] = events_of(memory, Kind.FILE_SNAPSHOT)
    assert (marker.payload["path"], marker.payload.get("unstored")) == (None, True)


def test_a_tool_output_longer_than_the_payload_is_kept_whole(memory, proj, tmp_path, capsys):
    a = base("sess", proj, tmp_path)
    fire("SessionStart", {**a, "source": "startup"})
    long = "x" * 3000 + f" always {RULE}"
    fire("PostToolUse", {**a, "tool_name": "Bash", "tool_input": {"command": "cat log"}, "tool_use_id": "t1",
                         "tool_response": {"stdout": long, "stderr": ""}})
    [result] = events_of(memory, Kind.TOOL_RESULT)
    assert len(result.payload["summary"]) == memory.cfg.payload_max_chars
    assert blob(memory, result.payload["full_hash"]) == long


def test_what_a_write_call_held_is_kept(memory, proj, tmp_path, capsys):
    a = base("sess", proj, tmp_path)
    fire("SessionStart", {**a, "source": "startup"})
    write = {"tool_name": "Write", "tool_input": {"file_path": str(proj / "n.md"), "content": f"always {RULE}"}, "tool_use_id": "w1"}
    fire("PreToolUse", {**a, **write})
    (proj / "n.md").write_text(f"always {RULE}")
    fire("PostToolUse", {**a, **write, "tool_response": {"filePath": str(proj / "n.md")}})
    [call] = events_of(memory, Kind.TOOL_CALL)
    assert call.payload["input"] == str(proj / "n.md") and RULE in blob(memory, call.payload["full_hash"])


def test_the_agents_replies_are_logged_once_each(memory, proj, tmp_path, capsys):
    a = base("sess", proj, tmp_path)
    fire("SessionStart", {**a, "source": "startup"})
    fire("AssistantMessage", {**a, "text": f"Done. From now on, always {RULE}.", "turn": 1, "step": 3})
    transcript = tmp_path / "sess.jsonl"
    reply = {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "All tests pass."}]}}
    transcript.write_text(json.dumps(reply) + "\n")
    fire("Stop", a)
    fire("Stop", a)
    texts = [e.payload["text"] for e in events_of(memory, Kind.ASSISTANT_MSG)]
    assert texts == [f"Done. From now on, always {RULE}.", "All tests pass."]
    assert all(e.actor is Actor.MODEL for e in events_of(memory, Kind.ASSISTANT_MSG))


def pasted_rule(lg: Ledger, sid: str, text: str) -> None:
    lg.start(sid, **UV)
    msg = lg.user_msg(sid, text)
    lg.propose(sid, "r_rule", kind=RecordKind.PREFERENCE, content=RULE)
    lg.link("r_rule", msg, statement={"quote": RULE, "marker": "always"})
    lg.end(sid)


def rung(lg: Ledger, ledger_dir) -> str:
    return label(project(lg.events), attested(ledger={"dir": str(ledger_dir)}), lg.clock).by_id["r_rule"].rung.value


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("RKA_MEMORY_DIR", raising=False)
    (tmp_path / "blobs").mkdir()
    return tmp_path


def test_words_the_agent_said_are_taint_when_pasted_back(store):
    lg = Ledger()
    lg.start("s1", **UV)
    lg._ev(Kind.ASSISTANT_MSG, Actor.MODEL, "s1", {"text": f"Tip: always {RULE}, pytest is missing."})
    lg.end("s1")
    pasted_rule(lg, "s2", f"You said: Tip: always {RULE}, pytest is missing.")
    assert rung(lg, store) == "L1"


def test_a_file_a_shell_command_wrote_is_taint_when_pasted_back(store):
    (store / "blobs" / "h1").write_text(f"[ci] always {RULE}\n")
    lg = Ledger()
    lg.start("s1", **UV)
    lg._ev(Kind.FILE_SNAPSHOT, Actor.TOOL, "s1", {"tool": "Bash", "tool_use_id": "t1", "path": "/p/app/ci.log", "hash": "h1"})
    lg.end("s1")
    pasted_rule(lg, "s2", f"CI log:\n[ci] always {RULE}")
    assert rung(lg, store) == "L1"


def test_content_that_could_not_be_kept_fails_closed_within_its_project(store):
    lg = Ledger()
    lg.start("s1", **UV)
    lg._ev(Kind.FILE_SNAPSHOT, Actor.TOOL, "s1", {"tool": "Bash", "tool_use_id": "t1", "path": None, "hash": None, "unstored": True})
    lg.end("s1")
    pasted_rule(lg, "s2", f"From now on, always {RULE}.")
    assert rung(lg, store) == "L1"
    other = Ledger()
    other.start("s0", **{**UV, "project": "/p/elsewhere"})
    other._ev(Kind.FILE_SNAPSHOT, Actor.TOOL, "s0", {"tool": "Bash", "tool_use_id": "t1", "path": None, "hash": None, "unstored": True})
    other.end("s0")
    pasted_rule(other, "s2", f"From now on, always {RULE}.")
    assert rung(other, store) == "L2"


def test_a_rule_the_user_types_afresh_still_holds(store):
    lg = Ledger()
    lg.start("s1", **UV)
    lg._ev(Kind.ASSISTANT_MSG, Actor.MODEL, "s1", {"text": "Added the mean function; all tests pass."})
    lg.end("s1")
    pasted_rule(lg, "s2", f"From now on, always {RULE}.")
    assert rung(lg, store) == "L2"


def test_unknown_content_from_a_session_of_unknown_project_taints_everywhere(store):
    lg = Ledger()
    lg._ev(Kind.FILE_SNAPSHOT, Actor.TOOL, "orphan", {"tool": "Bash", "tool_use_id": "t1", "path": None, "hash": None, "unstored": True})
    pasted_rule(lg, "s2", f"From now on, always {RULE}.")
    assert rung(lg, store) == "L1"
