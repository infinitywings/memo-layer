import hashlib
import json
import subprocess
import sys

import pytest
import yaml

from ledger_builder import Ledger
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
        (tmp_path / "none.yaml").write_text(yaml.safe_dump(raw))

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
        ("tool_call", "model"),
        ("tool_result", "tool"),
        ("exit_code", "tool"),
        ("test_result", "tool"),
        ("session_end", "runner"),
    ]
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
        {"reason": "logout", "tokens_total": 0, "tokens_before_first_write": 0, "first_write_seen": False}
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
    assert "[r_any | L2 fact] Run uv run pytest -x first" in started["hookSpecificOutput"]["additionalContext"]
    assert "r_dbg" not in started["hookSpecificOutput"]["additionalContext"]

    prompted = fire(capsys, "UserPromptSubmit", {**s, "prompt_id": "p1", "prompt": "fix the crash in the parser"})
    assert prompted["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "[r_dbg | L2 fact] Reproduce with a failing test before fixing" in prompted["hookSpecificOutput"]["additionalContext"]
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
    assert out == {"decision": "block", "reason": "rka-memory recorded reject for r_uv; now L1"}
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
