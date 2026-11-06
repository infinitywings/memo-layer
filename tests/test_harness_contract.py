"""The hook protocol as a harness without transcripts (the dsh plugin) speaks it."""

import json

from ledger_builder import Ledger, cfg
from rka_memory.domain import Kind, RecordKind, Scope, State
from rka_memory.ledger import hooks
from rka_memory.ledger.adapters import claude_code as adapter
from rka_memory.skills.export import session_skills

BASE = {"session_id": "d1", "transcript_path": "", "harness": "dsh", "tool_name": "Bash", "tool_use_id": "c1"}


def kinds(events):
    return [e.kind.value for e in events]


def test_a_harness_that_states_the_exit_code_records_outcomes_without_a_transcript():
    ok = {**BASE, "tool_input": {"command": "pytest"}, "tool_response": {"stdout": "== 2 passed in 0.1s ==", "exitCode": 0}}
    failed = {**BASE, "tool_input": {"command": "pytest"}, "error": "Exit code 1\n1 failed, 2 passed"}
    assert kinds(adapter.tool_events(ok, cfg(), failed=False)) == ["tool_call", "tool_result", "exit_code", "test_result"]
    out = adapter.tool_events(failed, cfg(), failed=True)
    assert [(e.kind.value, e.payload.get("passed", e.payload.get("code"))) for e in out[2:]] == [
        ("exit_code", 1),
        ("test_result", False),
    ]


def test_session_end_takes_reported_usage_when_there_is_no_transcript(memory, capsys, monkeypatch):
    monkeypatch.setattr(hooks, "spawn", lambda commands, log_path: None)
    payload = {
        "session_id": "d1", "transcript_path": "", "cwd": "/tmp", "hook_event_name": "SessionEnd",
        "usage": {"tokens_total": 900, "tokens_before_first_write": 300, "first_write_seen": True},
    }
    assert hooks.main("SessionEnd", json.dumps(payload)) == 0
    end = [e for e in memory.ledger.read() if e.kind is Kind.SESSION_END][0]
    assert {k: end.payload[k] for k in ("tokens_total", "tokens_before_first_write", "first_write_seen")} == {
        "tokens_total": 900,
        "tokens_before_first_write": 300,
        "first_write_seen": True,
    }


def test_session_skills_returns_only_l3_procedures_valid_for_the_state(memory):
    st = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
    lg = Ledger()
    lg.start("s1", **st)
    lg.propose("s1", "r_proc", scope=Scope.exact(State(**st)), kind=RecordKind.PROCEDURE,
               content="Run uv sync before tests", steps=(("Bash", "uv sync"),), acceptance_check="uv run pytest")
    lg.confirm("cli", "r_proc")
    lg.end("s1")
    memory.append(*lg.events)
    memory.gate(now=lg.clock)
    lg.events.clear()
    for s in ("s2", "s3"):
        lg.start(s, **st)
        lg.tool_call(s, "Bash", "uv sync")
        lg.test(s, True, "uv run pytest")
        lg.end(s)
    memory.append(*lg.events)
    memory.gate(now=lg.clock)
    world = memory.world()
    assert world.records["r_proc"].rung.value == "L3"

    here = session_skills(world, memory.cfg, State(**st))
    assert [(s["name"], s["description"]) for s in here] == [("rka-r-proc", "Run uv sync before tests")]
    assert here[0]["body"].startswith("# rka-r-proc\n")
    assert "Before finishing, run `uv run pytest`" in here[0]["body"]
    assert session_skills(world, memory.cfg, State(**{**st, "model_id": "m2"})) == []
