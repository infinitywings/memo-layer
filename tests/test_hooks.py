import hashlib
import json
import subprocess
import sys

import pytest
import yaml

from conftest import V2
from ledger_builder import Ledger
from rka_memory import config as config_mod
from rka_memory.domain import Actor, FieldConstraint, Kind, Scope
from rka_memory.ledger import hooks
from rka_memory.world import project


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "spawn", lambda commands, log_path: calls.append(commands))
    return calls


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "uv.lock").write_text("")
    return root.resolve()


@pytest.fixture
def user_config(tmp_path):
    """The config file hooks read through Memory.open(); see the memory fixture."""

    def write(**raw):
        (tmp_path / "none.yaml").write_text(yaml.safe_dump(config_mod.merge(V2, raw)))

    return write


def fire(capsys, event: str, payload: dict):
    assert hooks.main(event, json.dumps({**payload, "hook_event_name": event})) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def base(session: str, proj, tmp_path) -> dict:
    return {"session_id": session, "transcript_path": str(tmp_path / f"{session}.jsonl"), "cwd": str(proj)}


def write_transcript(path, records) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def assistant(msg_id: str, usage: tuple[int, int, int, int], *tools: str) -> dict:
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    content = [{"type": "tool_use", "name": t, "id": f"tu_{t}", "input": {}} for t in tools] or [
        {"type": "text", "text": "ok"}
    ]
    return {
        "type": "assistant",
        "message": {"id": msg_id, "model": "claude-opus-5-5", "usage": dict(zip(keys, usage)), "content": content},
    }


def run_session_a(capsys, proj, tmp_path):
    a = base("sess-a", proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    fire(capsys, "UserPromptSubmit", {**a, "prompt_id": "p1", "permission_mode": "default", "prompt": "fix the failing parser test"})

    new_file = proj / "new.py"
    write_new = {"tool_name": "Write", "tool_input": {"file_path": str(new_file), "content": "x = 1\n"}, "tool_use_id": "toolu_w1"}
    fire(capsys, "PreToolUse", {**a, **write_new})
    new_file.write_text("x = 1\n")
    fire(capsys, "PostToolUse", {**a, **write_new, "tool_response": {"type": "create", "filePath": str(new_file)}})

    old_file = proj / "old.py"
    old_file.write_text("a = 1\n")
    edit_old = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(old_file), "old_string": "a = 1\n", "new_string": "a = 1\nb = 2\n"},
        "tool_use_id": "toolu_e1",
    }
    fire(capsys, "PreToolUse", {**a, **edit_old})
    old_file.write_text("a = 1\nb = 2\n")
    fire(capsys, "PostToolUse", {**a, **edit_old, "tool_response": {"filePath": str(old_file)}})

    bash_ok = {
        "tool_name": "Bash",
        "tool_input": {"command": "uv run pytest -q"},
        "tool_use_id": "toolu_b1",
        "tool_response": {"stdout": "3 passed", "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False},
    }
    fire(capsys, "PostToolUse", {**a, **bash_ok})
    fire(
        capsys,
        "PostToolUseFailure",
        {
            **a,
            "tool_name": "Bash",
            "tool_input": {"command": "uv run  pytest tests/test_parser.py"},
            "tool_use_id": "toolu_b2",
            "error": "Exit code 2\nFAILED tests/test_parser.py::test_split",
        },
    )

    write_transcript(
        tmp_path / "sess-a.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "fix the failing parser test"}},
            assistant("msg_1", (10, 0, 0, 5)),
            assistant("msg_2", (20, 1, 2, 3), "Write"),
            assistant("msg_3", (1, 1, 1, 1), "Bash"),
        ],
    )
    fire(capsys, "SessionEnd", {**a, "prompt_id": "p1", "reason": "other"})
    return new_file, old_file


def test_week1_session_replays_from_ledger_and_next_session_sees_edit_and_undo(
    memory, proj, tmp_path, spawned, capsys
):
    new_file, old_file = run_session_a(capsys, proj, tmp_path)

    ledger = memory.ledger.read()
    world = project(ledger)
    replayed = world.sessions["sess-a"].events
    assert [e.id for e in replayed] == [e.id for e in ledger if e.session_id == "sess-a"]
    assert [(e.kind.value, e.actor.value) for e in replayed] == [
        ("session_start", "runner"),
        ("packet_delivered", "runner"),
        ("user_msg", "user"),
        ("judgment", "runner"),
        ("packet_delivered", "runner"),
        ("tool_call", "model"),
        ("tool_result", "tool"),
        ("file_write", "tool"),
        ("tool_call", "model"),
        ("tool_result", "tool"),
        ("file_write", "tool"),
        ("tool_call", "model"),
        ("tool_result", "tool"),
        ("exit_code", "tool"),
        ("test_result", "tool"),
        # No PreToolUse for these commands: what they wrote is unknown.
        ("file_snapshot", "tool"),
        ("tool_call", "model"),
        ("tool_result", "tool"),
        ("exit_code", "tool"),
        ("test_result", "tool"),
        ("file_snapshot", "tool"),
        ("check", "runner"),
        ("session_end", "runner"),
    ]
    (modified,) = [e for e in replayed if e.kind is Kind.CHECK]
    assert modified.payload["check"] == "modification"
    assert world.sessions["sess-a"].state.to_json() == {
        "task_class": "debugging",
        "project": str(proj),
        "toolchain": "uv",
        "model_id": None,
        "user_context": "default",
    }
    writes = [e for e in replayed if e.kind is Kind.FILE_WRITE]
    assert [dict(e.payload) for e in writes] == [
        {"tool": "Write", "tool_use_id": "toolu_w1", "path": str(new_file), "pre_hash": "absent", "post_hash": sha("x = 1\n")},
        {
            "tool": "Edit",
            "tool_use_id": "toolu_e1",
            "path": str(old_file),
            "pre_hash": sha("a = 1\n"),
            "post_hash": sha("a = 1\nb = 2\n"),
        },
    ]
    outcomes = [(e.kind.value, e.payload.get("code", e.payload.get("passed"))) for e in replayed if e.kind in (Kind.EXIT_CODE, Kind.TEST_RESULT)]
    assert outcomes == [("exit_code", 0), ("test_result", True), ("exit_code", 2), ("test_result", False)]
    assert dict(replayed[-1].payload) == {
        "reason": "other",
        "tokens_total": 45,
        "tokens_before_first_write": 15,
        "first_write_seen": True,
        "tokens_input": 31,
        "tokens_cache_read": 3,
        "tokens_cache_write": 2,
        "tokens_output": 9,
    }
    assert spawned == [[[sys.executable, "-I", "-m", "rka_memory.cli", "mine", "sess-a"]]]

    new_file.write_text("x = 1\ny = 2\n")
    old_file.write_text("a = 1\n")
    fire(capsys, "SessionStart", {**base("sess-b", proj, tmp_path), "source": "startup"})

    world = project(memory.ledger.read())
    edits = [e for e in world.events.values() if e.kind is Kind.USER_EDIT]
    undos = [e for e in world.events.values() if e.kind is Kind.USER_UNDO]
    assert [(e.session_id, e.actor.value, dict(e.payload)) for e in edits] == [
        (
            "sess-b",
            "user",
            {"path": str(new_file), "write_event_id": writes[0].id, "write_session_id": "sess-a", "lines_added": 1, "lines_removed": 0},
        )
    ]
    assert [(e.session_id, dict(e.payload)) for e in undos] == [
        ("sess-b", {"path": str(old_file), "write_event_id": writes[1].id, "write_session_id": "sess-a"})
    ]
    assert len(spawned) == 1, "sess-a just ended; its own SessionEnd miner owns it"


def test_session_start_refire_counts_once_and_does_not_redetect(memory, proj, tmp_path, spawned, capsys):
    run_session_a(capsys, proj, tmp_path)
    b = {**base("sess-b", proj, tmp_path), "source": "startup"}
    fire(capsys, "SessionStart", b)
    (proj / "new.py").write_text("changed after sess-b started\n")
    fire(capsys, "SessionStart", {**b, "source": "compact"})

    ledger = memory.ledger.read()
    starts_written = [e for e in ledger if e.kind is Kind.SESSION_START and e.session_id == "sess-b"]
    counted = [e for e in project(ledger).events.values() if e.kind is Kind.SESSION_START and e.session_id == "sess-b"]
    assert len(starts_written) == 2
    assert [e.payload["source"] for e in counted] == ["startup"]
    assert [e.kind for e in ledger if e.kind in (Kind.USER_EDIT, Kind.USER_UNDO)] == []


def test_catchup_mines_quiet_unmined_sessions_but_not_the_current_one(memory, proj, tmp_path, spawned, capsys, user_config):
    user_config(miner={"timeout_seconds": 0})
    fire(capsys, "SessionStart", {**base("old", proj, tmp_path), "source": "startup"})
    fire(capsys, "UserPromptSubmit", {**base("old", proj, tmp_path), "prompt_id": "p1", "prompt": "fix the parser"})
    fire(capsys, "SessionStart", {**base("now", proj, tmp_path), "source": "startup"})
    assert spawned == [[[sys.executable, "-I", "-m", "rka_memory.cli", "mine", "old"]]]


def test_catchup_mines_what_came_after_the_last_mine_and_skips_sessions_with_nothing_new(
    memory, proj, tmp_path, spawned, capsys, user_config
):
    user_config(miner={"timeout_seconds": 0})
    lg = Ledger()

    def mined(session, through):
        lg._ev(Kind.COST, Actor.RUNNER, session, {"component": "miner", "session_id": session, "through": through.id})

    lg.start("bare", project=str(proj))
    lg.start("done", project=str(proj))
    mined("done", lg.user_msg("done", "fix the parser"))
    lg.start("done", project=str(proj))
    lg.end("done")
    lg.start("resumed", project=str(proj))
    mined("resumed", lg.user_msg("resumed", "fix the lexer"))
    lg.start("resumed", project=str(proj))
    lg.user_msg("resumed", "and its tests")
    memory.append(*lg.events)

    fire(capsys, "SessionStart", {**base("now", proj, tmp_path), "source": "startup"})
    assert spawned == [[[sys.executable, "-I", "-m", "rka_memory.cli", "mine", "resumed"]]]


def test_session_end_also_syncs_when_a_core_project_is_configured(memory, proj, tmp_path, spawned, capsys, user_config):
    user_config(core={"project_id": "prj_x"})
    fire(capsys, "SessionEnd", {**base("s", proj, tmp_path), "reason": "logout"})
    cli = [sys.executable, "-I", "-m", "rka_memory.cli"]
    assert spawned == [[[*cli, "mine", "s"], [*cli, "sync"]]]
    end = [e for e in memory.ledger.read() if e.kind is Kind.SESSION_END]
    assert [dict(e.payload) for e in end] == [
        {
            "reason": "logout", "tokens_total": 0, "tokens_before_first_write": 0, "first_write_seen": False,
            "tokens_input": 0, "tokens_cache_read": 0, "tokens_cache_write": 0, "tokens_output": 0,
        }
    ]


def test_first_prompt_delivers_the_records_its_task_class_unlocks(memory, proj, tmp_path, spawned, capsys, user_config):
    user_config(packet={"arms": ["gated"]})
    lg = Ledger()
    state = dict(project=str(proj), toolchain="uv", task_class="debugging", user_context="default")
    debugging_only = Scope(task_class=FieldConstraint(frozenset({"debugging"})))
    for n in (1, 2):
        lg.start(f"g{n}", **state)
        ok = lg.test(f"g{n}", True)
        if n == 1:
            lg.propose("g1", "r_any", content="Run uv run pytest -x first")
            lg.propose("g1", "r_dbg", content="Reproduce with a failing test before fixing", scope=debugging_only)
        lg.link("r_any", ok)
        lg.link("r_dbg", ok)
    memory.append(*lg.events)
    memory.gate()

    s = base("s", proj, tmp_path)
    started = fire(capsys, "SessionStart", {**s, "source": "startup"})
    assert "- [r_any] Run uv run pytest -x first" in started["hookSpecificOutput"]["additionalContext"]
    assert "r_dbg" not in started["hookSpecificOutput"]["additionalContext"]

    prompted = fire(capsys, "UserPromptSubmit", {**s, "prompt_id": "p1", "prompt": "fix the crash in the parser"})
    assert prompted["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "- [r_dbg] Reproduce with a failing test before fixing" in prompted["hookSpecificOutput"]["additionalContext"]
    assert "r_any" not in prompted["hookSpecificOutput"]["additionalContext"]

    assert fire(capsys, "UserPromptSubmit", {**s, "prompt_id": "p2", "prompt": "now fix the next crash"}) is None
    deliveries = [(e.payload["channel"], e.payload["record_ids"]) for e in memory.ledger.read() if e.kind is Kind.PACKET_DELIVERED and e.session_id == "s"]
    assert deliveries == [("session_start", ["r_any"]), ("prompt", ["r_dbg"])]


def test_rkm_reject_records_a_user_verdict_and_never_reaches_the_model(memory, proj, tmp_path, spawned, capsys):
    lg = Ledger()
    lg.start("g1", project=str(proj), toolchain="uv", user_context="default")
    lg.propose("g1", "r_uv", content="Use uv")
    lg.link("r_uv", lg.test("g1", True))
    memory.append(*lg.events)
    memory.gate()
    s = base("s", proj, tmp_path)
    fire(capsys, "SessionStart", {**s, "source": "startup"})

    out = fire(capsys, "UserPromptSubmit", {**s, "prompt_id": "p1", "prompt": "rkm reject r_uv wrong"})
    assert out == {"decision": "block", "reason": "rka-memory recorded reject for r_uv; now L4"}
    unknown = fire(capsys, "UserPromptSubmit", {**s, "prompt_id": "p2", "prompt": "rkm confirm r_nope"})
    assert unknown == {"decision": "block", "reason": "rka-memory: unknown record r_nope; nothing recorded"}

    ledger = memory.ledger.read()
    assert [(e.session_id, e.actor.value, dict(e.payload)) for e in ledger if e.kind is Kind.USER_REJECT] == [
        ("s", "user", {"record_id": "r_uv", "reason": "wrong"})
    ]
    assert [e for e in ledger if e.kind is Kind.USER_MSG] == []


def test_repeated_instruction_in_a_later_session_is_a_user_repeat(memory, proj, tmp_path, spawned, capsys):
    a, b = base("a", proj, tmp_path), base("b", proj, tmp_path)
    fire(capsys, "UserPromptSubmit", {**a, "prompt_id": "pa", "prompt": "Always run the parser tests with uv before committing"})
    fire(capsys, "UserPromptSubmit", {**a, "prompt_id": "pa2", "prompt": "continue"})
    fire(capsys, "UserPromptSubmit", {**b, "prompt_id": "pb", "prompt": "always run the parser tests with uv before committing!"})
    fire(capsys, "UserPromptSubmit", {**b, "prompt_id": "pb2", "prompt": "continue"})

    ledger = memory.ledger.read()
    msgs = {e.payload["prompt_id"]: e.id for e in ledger if e.kind is Kind.USER_MSG}
    repeats = [dict(e.payload) for e in ledger if e.kind is Kind.USER_REPEAT]
    assert repeats == [{"prompt_event_id": msgs["pb"], "matched_event_id": msgs["pa"], "similarity": 1.0}]


def test_disabled_hooks_write_nothing(memory, proj, tmp_path, spawned, capsys, monkeypatch):
    monkeypatch.setenv("RKA_MEMORY_DISABLE", "1")
    s = base("s", proj, tmp_path)
    for event, payload in [
        ("SessionStart", {**s, "source": "startup"}),
        ("UserPromptSubmit", {**s, "prompt": "fix it"}),
        ("PreToolUse", {**s, "tool_name": "Write", "tool_input": {"file_path": str(proj / "f")}, "tool_use_id": "t"}),
        ("PostToolUse", {**s, "tool_name": "Bash", "tool_input": {"command": "pytest"}, "tool_use_id": "t", "tool_response": {}}),
        ("PostToolUseFailure", {**s, "tool_name": "Bash", "tool_input": {"command": "pytest"}, "tool_use_id": "t", "error": "Exit code 1"}),
        ("SessionEnd", {**s, "reason": "other"}),
    ]:
        assert fire(capsys, event, payload) is None
    assert memory.ledger.read() == []
    assert spawned == []

    monkeypatch.delenv("RKA_MEMORY_DISABLE")
    fire(capsys, "SessionStart", {**s, "source": "startup"})
    assert [e.kind for e in memory.ledger.read() if e.session_id == "s"] == [Kind.SESSION_START, Kind.PACKET_DELIVERED]


def test_malformed_payloads_return_zero_and_are_logged(memory, capsys):
    assert hooks.main("PostToolUse", "{not json") == 0
    assert hooks.main("SessionStart", "{}") == 0
    assert hooks.main("UserPromptSubmit", "[1, 2]") == 0
    assert capsys.readouterr().out == ""
    log = (memory.cfg.ledger_dir / "hook-errors.log").read_text()
    assert log.count("--- ") == 3
    assert "JSONDecodeError" in log
    assert "ValueError: hook payload has no session_id" in log
    assert "ValueError: hook payload is not a JSON object" in log
    assert memory.ledger.read() == []


def test_changes_made_by_the_agents_own_shell_commands_are_never_credited_to_the_user(
    memory, proj, tmp_path, spawned, capsys
):
    a = base("sess-a", proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    reverted, formatted, user_owned = proj / "reverted.py", proj / "formatted.py", proj / "user.py"
    for i, f in enumerate((reverted, formatted, user_owned)):
        write = {"tool_name": "Write", "tool_input": {"file_path": str(f), "content": "x=1\n"}, "tool_use_id": f"w{i}"}
        fire(capsys, "PreToolUse", {**a, **write})
        f.write_text("x=1\n")
        fire(capsys, "PostToolUse", {**a, **write, "tool_response": {"filePath": str(f)}})
    reverted.unlink()
    formatted.write_text("x = 1\n")
    fire(capsys, "PostToolUse", {**a, "tool_name": "Bash", "tool_input": {"command": "make tidy"}, "tool_use_id": "b1", "tool_response": {"stdout": ""}})
    fire(capsys, "SessionEnd", {**a, "reason": "other"})

    user_owned.write_text("x = 1  # user\n")
    fire(capsys, "SessionStart", {**base("sess-b", proj, tmp_path), "source": "startup"})

    events = memory.ledger.read()
    shell_writes = [e.payload["path"] for e in events if e.kind is Kind.FILE_WRITE and e.payload["tool"] == "Bash"]
    assert shell_writes == [str(formatted), str(reverted)]
    user_signals = [(e.kind.value, e.payload["path"]) for e in events if e.kind in (Kind.USER_EDIT, Kind.USER_UNDO)]
    assert user_signals == [("user_edit", str(user_owned))]


def test_a_change_after_the_agents_last_tool_call_is_still_the_agents(memory, proj, tmp_path, spawned, capsys):
    a = base("sess-a", proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    f = proj / "bg.py"
    write = {"tool_name": "Write", "tool_input": {"file_path": str(f), "content": "x=1\n"}, "tool_use_id": "w1"}
    fire(capsys, "PreToolUse", {**a, **write})
    f.write_text("x=1\n")
    fire(capsys, "PostToolUse", {**a, **write, "tool_response": {"filePath": str(f)}})
    f.write_text("x=1  # written by a backgrounded command\n")
    fire(capsys, "SessionEnd", {**a, "reason": "other"})
    fire(capsys, "SessionStart", {**base("sess-b", proj, tmp_path), "source": "startup"})
    events = memory.ledger.read()
    assert [e.payload["tool"] for e in events if e.kind is Kind.FILE_WRITE] == ["Write", "session_end"]
    assert [e for e in events if e.kind in (Kind.USER_EDIT, Kind.USER_UNDO)] == []


def test_background_commands_never_import_from_the_project_directory(tmp_path):
    planted = tmp_path / "rka_memory"
    planted.mkdir()
    (planted / "__init__.py").write_text("raise SystemExit('planted package imported')\n")
    (planted / "cli.py").write_text("raise SystemExit('planted package imported')\n")
    cmd = hooks._cli("--help")
    out = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "planted" not in out.stdout + out.stderr


def test_a_decide_call_becomes_a_decision_in_force_and_rkm_commands_amend(memory, proj, tmp_path, spawned, capsys):
    from rka_memory.domain import Kind as K, RecordKind

    sid = "dec-1"
    fire(capsys, "SessionStart", {**base(sid, proj, tmp_path), "source": "startup"})
    use = {
        **base(sid, proj, tmp_path),
        "tool_name": "decide",
        "tool_use_id": "call_dec1",
        "tool_input": {"choice": "Store results in SQLite", "alternatives": ["Postgres"], "reason": "no server"},
        "tool_response": {"recorded": True},
    }
    fire(capsys, "PostToolUse", use)
    fire(capsys, "PostToolUse", {**use, "tool_use_id": "call_dec2", "tool_input": {"choice": "x"}})
    world = memory.world()
    (dec,) = [r for r in world.records.values() if r.spec.kind is RecordKind.DECISION]
    assert (dec.spec.content, dec.spec.alternatives, dec.spec.reason, dec.proposed.session_id) == (
        "Store results in SQLite", ("Postgres",), "no server", sid
    )
    assert memory.labels().by_id[dec.id].rung.value == "L0D"

    def typed(text):
        return fire(capsys, "UserPromptSubmit", {**base(sid, proj, tmp_path), "prompt": text})

    assert typed(f"rkm confirm {dec.id}") == {"decision": "block", "reason": f"rka-memory recorded confirm for {dec.id}; now L2"}
    assert typed(f"rkm impact {dec.id}")["reason"] == f"rka-memory: rejecting {dec.id} would withdraw {dec.id}. Nothing was recorded."
    assert typed(f"rkm close {dec.id} toolchain") == {
        "decision": "block",
        "reason": "rka-memory: `rkm close` needs <field>=<value> with a field of ['task_class', 'project', 'toolchain', 'model_id', 'user_context']",
    }
    reply = typed(f"rkm qualify {dec.id} only for the results store")["reason"]
    (amend,) = [e for e in memory.ledger.read() if e.kind is K.USER_AMEND]
    new = amend.payload["record_id"]
    assert reply == f"rka-memory recorded qualify of {dec.id}; {dec.id} now L2; {new} now L2"
    assert [e for e in memory.ledger.read() if e.session_id == sid and e.kind is K.USER_MSG] == []


def test_each_write_that_changes_an_existing_test_is_logged_as_a_failed_check(memory, proj, tmp_path, spawned, capsys):
    from rka_memory.domain import Kind as K

    sid = "mod-1"
    test_file = proj / "test_calc.py"
    test_file.write_text("import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n")
    fire(capsys, "SessionStart", {**base(sid, proj, tmp_path), "source": "startup"})

    def write(text, use_id):
        call = {**base(sid, proj, tmp_path), "tool_name": "Write", "tool_use_id": use_id, "tool_input": {"file_path": str(test_file), "content": text}}
        fire(capsys, "PreToolUse", call)
        test_file.write_text(text)
        fire(capsys, "PostToolUse", {**call, "tool_response": {"type": "update"}})

    write(test_file.read_text() + "\n\ndef test_more():\n    assert True\n", "w1")
    write(test_file.read_text().replace("def test_add():\n", "def test_add():\n    return\n"), "w2")
    checks = [e.payload for e in memory.ledger.read() if e.kind is K.CHECK and e.session_id == sid]
    assert [(c["check"], c["passed"], c["paths"]) for c in checks] == [("modification", False, [str(test_file)])]


# Added after the same review: a write by a shell command (or any tool not
# known to be read-only) to an existing test was caught only if it lasted
# until the session ended. Residual risk, not covered: one command that edits
# a test, runs it and reverts the edit before it returns.


@pytest.mark.parametrize("tool", ["Bash", "ApplyPatch", "pwsh"])
def test_a_tool_call_that_changes_an_existing_test_is_on_record_even_if_reverted(memory, proj, tmp_path, spawned, capsys, tool):
    from rka_memory.domain import Kind as K
    from rka_memory.kernel.evidence import modified_sessions

    original = "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n"
    test_file = proj / "test_calc.py"
    test_file.write_text(original)
    sid = "shell-1"
    a = base(sid, proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})

    def call(use_id):
        fire(capsys, "PostToolUse", {**a, "tool_name": tool, "tool_use_id": use_id, "tool_input": {"command": "..."}, "tool_response": {"stdout": "", "exit_code": 0}})

    test_file.write_text(original + "\n\ndef test_more():\n    assert True\n")
    call("t1")
    assert [e for e in memory.ledger.read() if e.kind is K.CHECK and e.session_id == sid and not e.payload["passed"]] == []
    test_file.write_text(original.replace("== 5", "== 5 or True"))
    call("t2")
    test_file.write_text(original)
    call("t3")
    fire(capsys, "SessionEnd", {**a, "reason": "other"})
    checks = [e.payload for e in memory.ledger.read() if e.kind is K.CHECK and e.session_id == sid]
    first_failed = next(c for c in checks if not c["passed"])
    assert (first_failed["on"], first_failed["paths"]) == ("shell", [str(test_file)])
    assert checks[-1]["passed"] is True
    assert sid in modified_sessions(memory.world(), memory.cfg)


def test_a_new_test_file_may_import_what_existing_tests_import(memory, proj, tmp_path, spawned, capsys):
    from rka_memory.domain import Kind as K

    (proj / "test_calc.py").write_text("import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n")
    sid = "new-1"
    fire(capsys, "SessionStart", {**base(sid, proj, tmp_path), "source": "startup"})
    new = proj / "test_more.py"
    text = "import calc\n\n\ndef test_double():\n    assert calc.add(1, 1) == 2\n"
    call = {**base(sid, proj, tmp_path), "tool_name": "Write", "tool_use_id": "w1", "tool_input": {"file_path": str(new), "content": text}}
    fire(capsys, "PreToolUse", call)
    new.write_text(text)
    fire(capsys, "PostToolUse", {**call, "tool_response": {"type": "create"}})
    fire(capsys, "PostToolUse", {**base(sid, proj, tmp_path), "tool_name": "Bash", "tool_use_id": "b1", "tool_input": {"command": "python -m pytest"}, "tool_response": {"stdout": "", "exit_code": 0}})
    fire(capsys, "SessionEnd", {**base(sid, proj, tmp_path), "reason": "other"})
    checks = [e.payload for e in memory.ledger.read() if e.kind is K.CHECK and e.session_id == sid]
    assert [(c["passed"], c["paths"], c.get("added")) for c in checks] == [(True, [], [str(new)])]


# The isolation re-run at session end (RKA jrn_01M7H11MZ72N1FEQ01P1D5JQ2Y),
# and a write through a link (third review of ebd2cc7). Written before the code.


def _session_adding_a_test(capsys, proj, tmp_path, sid, added, exit_code):
    (proj / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (proj / "test_calc.py").write_text("import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n")
    a = base(sid, proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    new = proj / "test_00_more.py"
    call = {**a, "tool_name": "Write", "tool_use_id": "w1", "tool_input": {"file_path": str(new), "content": added}}
    fire(capsys, "PreToolUse", call)
    new.write_text(added)
    fire(capsys, "PostToolUse", {**call, "tool_response": {"type": "create"}})
    run = {**a, "tool_name": "Bash", "tool_use_id": "b1", "tool_input": {"command": "python -m pytest -q"}}
    if exit_code:
        fire(capsys, "PostToolUseFailure", {**run, "error": f"Exit code {exit_code}"})
    else:
        fire(capsys, "PostToolUse", {**run, "tool_response": {"stdout": "2 passed", "exit_code": 0}})
    fire(capsys, "SessionEnd", {**a, "reason": "other"})


PATCHING_TEST = "import calc\n\n\ndef test_setup():\n    calc.add = lambda a, b: a + b\n"


def _isolation_config(tmp_path):
    script = tmp_path / "confine.sh"
    script.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    script.chmod(0o755)
    return {"checks": {"isolation": {"python": sys.executable, "confine": [str(script)]}}}


def test_a_session_that_added_tests_and_passed_is_rerun_alone_at_session_end(memory, proj, tmp_path, spawned, capsys, user_config):
    from rka_memory.domain import Kind as K
    from rka_memory.kernel.evidence import modified_sessions

    user_config(**_isolation_config(tmp_path))
    _session_adding_a_test(capsys, proj, tmp_path, "iso-1", PATCHING_TEST, 0)
    checks = [e.payload for e in memory.ledger.read() if e.kind is K.CHECK and e.session_id == "iso-1"]
    assert [(c.get("on"), c["passed"]) for c in checks] == [(None, True), ("isolation", False)]
    assert checks[-1]["failed"] == ["test_calc.py::test_add"]
    assert "iso-1" in modified_sessions(memory.world(), memory.cfg)


def test_without_the_rerun_a_session_that_added_tests_and_passed_counts_as_changed(memory, proj, tmp_path, spawned, capsys):
    from rka_memory.domain import Kind as K
    from rka_memory.kernel.evidence import modified_sessions

    _session_adding_a_test(capsys, proj, tmp_path, "iso-2", PATCHING_TEST, 0)
    checks = [e.payload for e in memory.ledger.read() if e.kind is K.CHECK and e.session_id == "iso-2"]
    assert [(c.get("on"), c["passed"], c.get("error")) for c in checks] == [(None, True, None), ("isolation", False, "not configured")]
    assert "iso-2" in modified_sessions(memory.world(), memory.cfg)


def test_a_session_whose_last_test_run_failed_is_not_rerun(memory, proj, tmp_path, spawned, capsys, user_config):
    from rka_memory.domain import Kind as K

    user_config(**_isolation_config(tmp_path))
    _session_adding_a_test(capsys, proj, tmp_path, "iso-3", PATCHING_TEST, 1)
    checks = [e.payload for e in memory.ledger.read() if e.kind is K.CHECK and e.session_id == "iso-3"]
    assert [(c.get("on"), c["passed"]) for c in checks] == [(None, True)]


def test_a_write_through_a_link_to_a_test_is_compared_as_that_test(memory, proj, tmp_path, spawned, capsys):
    from rka_memory.domain import Kind as K

    test_file = proj / "test_floor.py"
    test_file.write_text("import math\n\n\ndef test_floor():\n    assert math.floor(2.5) == 2\n")
    link = proj / "notes.py"
    link.symlink_to(test_file)
    sid = "link-1"
    a = base(sid, proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    text = test_file.read_text().replace("== 2", "== 2 or True")
    call = {**a, "tool_name": "Write", "tool_use_id": "w1", "tool_input": {"file_path": str(link), "content": text}}
    fire(capsys, "PreToolUse", call)
    link.write_text(text)
    fire(capsys, "PostToolUse", {**call, "tool_response": {"type": "update"}})
    failed = [e.payload for e in memory.ledger.read() if e.kind is K.CHECK and e.session_id == sid and not e.payload["passed"]]
    assert [(c["on"], c["paths"]) for c in failed] == [("write", [str(test_file)])]


def test_a_rerun_that_never_logs_its_result_counts_as_a_change(memory, proj, tmp_path, spawned, capsys, user_config, monkeypatch):
    # The end-of-session check is logged, marked as owing a re-run, before the
    # re-run starts; a hook killed or failing during it leaves the mark.
    from rka_memory.domain import Kind as K
    from rka_memory.kernel.evidence import modified_sessions
    from rka_memory.ledger import checks as runner_checks

    def killed(*args, **kwargs):
        raise RuntimeError("hook killed")

    user_config(**_isolation_config(tmp_path))
    monkeypatch.setattr(runner_checks, "isolation_event", killed)
    _session_adding_a_test(capsys, proj, tmp_path, "iso-4", "import calc\n\n\ndef test_zero():\n    assert True\n", 0)
    checks = [e.payload for e in memory.ledger.read() if e.kind is K.CHECK and e.session_id == "iso-4"]
    assert [(c.get("on"), c["passed"], c.get("isolation")) for c in checks] == [(None, True, "due")]
    assert "iso-4" in modified_sessions(memory.world(), memory.cfg)


# Per-call checks against the session start, not the previous call (live run
# 20261116T022449Z, mislead-gated s6): the agent added a test using math.e,
# then `import math` in a second write; against the state the first write
# left, `math` was no longer a fresh name. Written before the code.

ORIGINAL = "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n"


def _write(capsys, a, path, text, use_id):
    call = {**a, "tool_name": "Write", "tool_use_id": use_id, "tool_input": {"file_path": str(path), "content": text}}
    fire(capsys, "PreToolUse", call)
    kind = "update" if path.exists() else "create"
    path.write_text(text)
    fire(capsys, "PostToolUse", {**call, "tool_response": {"type": kind}})


def _failed(memory, sid):
    from rka_memory.domain import Kind as K

    return [(e.payload["on"], e.payload["paths"]) for e in memory.ledger.read() if e.kind is K.CHECK and e.session_id == sid and not e.payload["passed"]]


def test_a_session_may_import_a_name_its_own_new_test_already_uses(memory, proj, tmp_path, spawned, capsys):
    test_file = proj / "test_calc.py"
    test_file.write_text(ORIGINAL)
    a = base("own-1", proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    _write(capsys, a, test_file, ORIGINAL + "\n\ndef test_e():\n    assert 2 < math.e < 3\n", "w1")
    _write(capsys, a, test_file, "import math\n" + test_file.read_text(), "w2")
    assert _failed(memory, "own-1") == []


@pytest.mark.parametrize("name", ["test_calc.py", "test_more.py"])
def test_a_session_may_edit_a_test_it_added(memory, proj, tmp_path, spawned, capsys, name):
    (proj / "test_calc.py").write_text(ORIGINAL)
    path = proj / name
    a = base("own-2", proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    head = path.read_text() if path.exists() else "import calc\n"
    _write(capsys, a, path, head + "\n\ndef test_more():\n    assert calc.add(1, 1) == 2\n", "w1")
    _write(capsys, a, path, path.read_text().replace("add(1, 1) == 2", "add(2, 2) == 4"), "w2")
    assert _failed(memory, "own-2") == []


def test_a_changed_test_is_logged_by_the_call_that_changed_it_only(memory, proj, tmp_path, spawned, capsys):
    test_file = proj / "test_calc.py"
    test_file.write_text(ORIGINAL)
    a = base("once-1", proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})

    def shell(use_id):
        fire(capsys, "PostToolUse", {**a, "tool_name": "Bash", "tool_use_id": use_id, "tool_input": {"command": "..."}, "tool_response": {"stdout": "", "exit_code": 0}})

    test_file.write_text(ORIGINAL.replace("== 5", "== 5 or True"))
    shell("t1")
    _write(capsys, a, proj / "test_more.py", "import calc\n\n\ndef test_more():\n    assert calc.add(1, 1) == 2\n", "w2")
    shell("t3")
    assert _failed(memory, "once-1") == [("shell", [str(test_file)])]


def test_a_change_made_between_calls_is_on_record_when_a_write_reverts_it(memory, proj, tmp_path, spawned, capsys):
    # Between calls: a process the agent left running, say.
    test_file = proj / "test_calc.py"
    test_file.write_text(ORIGINAL)
    a = base("between-1", proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    test_file.write_text(ORIGINAL.replace("== 5", "== 5 or True"))
    _write(capsys, a, test_file, ORIGINAL, "w1")
    assert _failed(memory, "between-1") == [("write", [str(test_file)])]


# The user's outcome check at session end (DESIGN_V2 5.3, revised after the
# trial-ceiling study, RKA jrn_01M7HF59ZKEG5X2EGG2BWX4B8Y), and repeats that
# ask for the same thing. Written before the code.

OPS_CHECK = "import sys, calc\nok = 'mean' in getattr(calc, 'OPERATIONS', {})\nprint('ops CLI: mean ' + ('found' if ok else 'missing; register it in calc.OPERATIONS'))\nsys.exit(0 if ok else 1)\n"


def test_the_outcome_check_runs_at_session_end_before_the_session_ends(memory, proj, tmp_path, spawned, capsys, user_config):
    from rka_memory.domain import Actor as A
    from rka_memory.domain import Kind as K

    settings = _isolation_config(tmp_path)
    settings["checks"]["outcome"] = {"command": ["{python}", "-c", OPS_CHECK]}
    user_config(**settings)
    (proj / "calc.py").write_text("def mean(values):\n    return sum(values) / len(values)\n")
    a = base("out-1", proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    fire(capsys, "SessionEnd", {**a, "reason": "other"})
    tail = [e for e in memory.ledger.read() if e.session_id == "out-1"][-4:]
    assert [(e.kind, e.actor) for e in tail] == [(K.CHECK, A.RUNNER), (K.CHECK, A.RUNNER), (K.TOOL_RESULT, A.ENV), (K.SESSION_END, A.RUNNER)]
    due, verdict, output, _ = tail
    assert due.payload == {"check": "outcome", "status": "due"}
    assert (verdict.payload["check"], verdict.payload["passed"]) == ("outcome", False)
    assert "register it in calc.OPERATIONS" in output.payload["output"]


def test_without_an_outcome_command_no_outcome_check_is_logged(memory, proj, tmp_path, spawned, capsys, user_config):
    from rka_memory.domain import Kind as K

    user_config(**_isolation_config(tmp_path))
    a = base("out-2", proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    fire(capsys, "SessionEnd", {**a, "reason": "other"})
    assert [e for e in memory.ledger.read() if e.kind is K.CHECK and e.payload.get("check") == "outcome"] == []


def test_prompts_that_ask_for_different_things_are_not_repeats(memory, proj, tmp_path, spawned, capsys):
    a, b = base("a", proj, tmp_path), base("b", proj, tmp_path)
    fire(capsys, "UserPromptSubmit", {**a, "prompt_id": "pa", "prompt": "Add a geometric_mean(values) function to calc.py, with tests."})
    fire(capsys, "UserPromptSubmit", {**b, "prompt_id": "pb", "prompt": "Add a harmonic_mean(values) function to calc.py, with tests."})
    assert [e for e in memory.ledger.read() if e.kind is Kind.USER_REPEAT] == []


def test_a_restated_request_with_a_courtesy_word_is_still_a_repeat(memory, proj, tmp_path, spawned, capsys):
    a, b = base("a", proj, tmp_path), base("b", proj, tmp_path)
    fire(capsys, "UserPromptSubmit", {**a, "prompt_id": "pa", "prompt": "Always run the parser tests with uv before committing"})
    fire(capsys, "UserPromptSubmit", {**b, "prompt_id": "pb", "prompt": "Always run the parser tests with uv before committing, please"})
    assert len([e for e in memory.ledger.read() if e.kind is Kind.USER_REPEAT]) == 1


def fire_codex(capsys, event: str, payload: dict):
    assert hooks.main(event, json.dumps({**payload, "hook_event_name": event}), agent="codex") == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def test_a_codex_session_tracks_patched_files_and_never_logs_an_unknown_outcome_as_a_pass(
    memory, proj, tmp_path, spawned, capsys
):
    a = {"session_id": "cx-a", "transcript_path": str(tmp_path / "rollout.jsonl"), "cwd": str(proj), "model": "gpt-6.2-sol"}
    fire_codex(capsys, "SessionStart", {**a, "source": "startup"})
    fire_codex(capsys, "UserPromptSubmit", {**a, "turn_id": "t1", "prompt": "add a parser module"})
    patch = "*** Begin Patch\n*** Add File: parser.py\n+x = 1\n*** End Patch\n"
    call = {"turn_id": "t1", "tool_name": "apply_patch", "tool_use_id": "c1", "tool_input": {"command": patch}}
    fire_codex(capsys, "PreToolUse", {**a, **call})
    (proj / "parser.py").write_text("x = 1\n")
    fire_codex(capsys, "PostToolUse", {**a, **call, "tool_response": "Success. Updated the following files:\nA parser.py"})
    unknown = {"turn_id": "t1", "tool_name": "Bash", "tool_use_id": "c2", "tool_input": {"command": ["make", "lint"]},
               "tool_response": "lint: 3 errors"}
    fire_codex(capsys, "PostToolUse", {**a, **unknown})
    stated = {"turn_id": "t1", "tool_name": "Bash", "tool_use_id": "c3", "tool_input": {"command": "make build"},
              "tool_response": {"exit_code": 1, "stdout": "", "stderr": "boom"}}
    fire_codex(capsys, "PostToolUse", {**a, **stated})
    fire_codex(capsys, "Stop", {**a, "turn_id": "t1", "stop_hook_active": False, "last_assistant_message": "Added parser.py."})
    fire_codex(capsys, "SessionEnd", {**a, "reason": "other"})

    (proj / "parser.py").write_text("x = 2  # the user's fix\n")
    fire_codex(capsys, "SessionStart", {**a, "session_id": "cx-b", "source": "startup"})

    events = memory.ledger.read()
    writes = [(e.payload["tool"], e.payload["path"]) for e in events if e.kind is Kind.FILE_WRITE and e.session_id == "cx-a"]
    assert writes[0] == ("Edit", str(proj / "parser.py"))
    calls = [e.payload.get("input") for e in events if e.kind is Kind.TOOL_CALL]
    assert "make lint" in calls[1] and "make build" in calls[2]
    assert [e.payload["code"] for e in events if e.kind is Kind.EXIT_CODE] == [1]
    assert [e.payload["text"] for e in events if e.kind is Kind.ASSISTANT_MSG] == ["Added parser.py."]
    assert [(e.kind.value, e.payload["path"]) for e in events if e.kind in (Kind.USER_EDIT, Kind.USER_UNDO)] == [
        ("user_edit", str(proj / "parser.py"))
    ]


def denial(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"] if out else None


def test_the_guard_keeps_the_agents_tools_out_of_the_ledger_and_off_the_cli(memory, proj, tmp_path, spawned, capsys):
    a = base("sess-g", proj, tmp_path)
    fire(capsys, "SessionStart", {**a, "source": "startup"})
    led = memory.cfg.ledger_dir

    def pre(tool, **tool_input):
        return denial(fire(capsys, "PreToolUse", {**a, "tool_name": tool, "tool_use_id": "g", "tool_input": tool_input}))

    ledger = [
        pre("Bash", command=f"tail -5 {led}/ledger.jsonl"),
        pre("Bash", command=f"echo '{{}}' >> {led}/ledger.jsonl"),
        pre("Read", file_path=f"{led}/ledger.jsonl"),
        pre("Grep", pattern="record_proposed", path=str(led)),
        pre("Write", file_path=f"{led}/config.yaml", content="hooks: {guard: false}"),
        pre("Bash", command="cat ../../.rka-memory/ledger.jsonl"),
        pre("Bash", command="cat ~/.rka-memory/config.yaml"),
    ]
    cli = [
        pre("Bash", command="rka-memory confirm r_1"),
        pre("Bash", command='rka-memory "confirm" r_1'),
        pre("Bash", command="rka-memory $(printf confirm) r_1"),
        pre("Bash", command="cd /tmp && ~/.local/share/rka-memory/memo-layer/bin/rka-memory status"),
        pre("Bash", command="uv run rka-memory rkm reject r_1 wrong"),
        pre("Bash", command="bash -c 'rka-memory review'"),
        pre("Bash", command="python -m rka_memory.cli review"),
        pre("Bash", command='python3 -c "import rka_memory"'),
        pre("Bash", command="B=$(command -v rka-memory); $B confirm r_1"),
        pre("Bash", command="script -q /dev/null rka-memory confirm r_1"),
        pre("Bash", command="pip install rka-memory"),
        pre("Bash", command="RKA_MEMORY_DIR=/tmp/x rka-memory doctor"),
    ]
    assert all("directory or config" in r for r in ledger), ledger
    assert all(r and "only `rka-memory doctor`" in r for r in cli), cli
    allowed = [
        pre("Bash", command="rka-memory doctor"),
        pre("Bash", command="~/.local/share/rka-memory/memo-layer/bin/rka-memory doctor 2>/dev/null || true"),
        pre("Bash", command="ls ~/.local/share/rka-memory/memo-layer"),
        pre("Bash", command="rka-memory --help"),
        pre("Read", file_path=f"{led}-backup/notes.md"),
        pre("Bash", command="uv run pytest -q"),
    ]
    assert allowed == [None] * 6


def test_cli_verdicts_refuse_to_run_without_a_terminal(memory, capsys):
    from rka_memory import cli

    assert cli.main(["confirm", "r_1"]) == 2
    assert cli.main(["rkm", "reject", "r_1", "wrong"]) == 2
    assert "verdicts are typed by the user in a terminal" in capsys.readouterr().err
    assert memory.ledger.read() == []


def test_with_the_guard_off_the_agent_may_run_the_cli(memory, proj, tmp_path, spawned, capsys):
    import dataclasses

    memory.cfg = dataclasses.replace(memory.cfg, hooks_guard=False)
    from rka_memory.ledger import guard

    assert guard.blocked("Bash", {"command": "rka-memory status"}, memory.cfg) is None


def test_the_guard_denies_a_codex_patch_into_the_ledger_with_one_reply(memory, proj, tmp_path, spawned, capsys):
    a = {"session_id": "cx-g", "cwd": str(proj)}
    fire_codex(capsys, "SessionStart", {**a, "source": "startup"})
    patch = f"*** Begin Patch\n*** Add File: ok.py\n+x\n*** Update File: {memory.cfg.ledger_dir}/ledger.jsonl\n+y\n*** End Patch\n"
    out = fire_codex(capsys, "PreToolUse", {**a, "turn_id": "t", "tool_name": "apply_patch", "tool_use_id": "p",
                                            "tool_input": {"command": patch}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
