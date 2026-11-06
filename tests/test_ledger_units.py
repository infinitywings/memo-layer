import subprocess

from ledger_builder import cfg
from rka_memory.domain import Actor, Event, Kind, State
from rka_memory.ledger import signals
from rka_memory.ledger import state as state_mod
from rka_memory.ledger.adapters import claude_code as adapter
from rka_memory.world import project


def kinds_and_payloads(events):
    return [(e.kind.value, dict(e.payload)) for e in events]


def test_detect_uses_git_root_and_the_first_toolchain_marker(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg" / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "package.json").write_text("{}")
    (root / "pnpm-lock.yaml").write_text("")
    state = state_mod.detect(root / "pkg" / "sub", cfg(), model_id="claude-opus-5-5")
    assert state == State(
        task_class=None, project=str(root.resolve()), toolchain="pnpm", model_id="claude-opus-5-5", user_context="default"
    )

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "requirements.txt").write_text("")
    assert (state_mod.detect(plain, cfg()).project, state_mod.detect(plain, cfg()).toolchain) == (str(plain.resolve()), "pip")
    (plain / "requirements.txt").unlink()
    assert state_mod.detect(plain, cfg()).toolchain == "none"


def test_classify_task_by_keywords():
    c = cfg()
    assert state_mod.classify_task("Fix the crash when parsing empty files", c) == "debugging"
    assert state_mod.classify_task("Add a /health endpoint to the API", c) == "coding"
    assert state_mod.classify_task("Refactor the config loader and rename helpers", c) == "refactoring"
    assert state_mod.classify_task("Write the README section on installation docs", c) == "writing"
    assert state_mod.classify_task("hello there", c) == "other"
    assert state_mod.classify_task("Fix the crash", cfg(scope={"task_classes": ["coding", "other"]})) == "other"


def test_bash_exit_codes_come_from_the_payload_and_unknown_outcomes_ground_nothing():
    c = cfg()
    common = {"session_id": "s", "transcript_path": "/t/s.jsonl", "tool_name": "Bash", "tool_use_id": "t1"}
    explicit = adapter.tool_events(
        {**common, "tool_input": {"command": "make build"}, "tool_response": {"stdout": "", "stderr": "boom", "exitCode": 3}},
        c,
        failed=False,
    )
    assert kinds_and_payloads(explicit)[2:] == [("exit_code", {"command": "make build", "tool_use_id": "t1", "code": 3})]

    background = adapter.tool_events(
        {**common, "tool_input": {"command": "pytest", "run_in_background": True}, "tool_response": {"backgroundTaskId": "b1"}},
        c,
        failed=False,
    )
    assert [e.kind.value for e in background] == ["tool_call", "tool_result"]

    missing = adapter.tool_events({**common, "tool_input": {"command": "cargo test"}, "error": "command not found"}, c, failed=True)
    assert kinds_and_payloads(missing)[1:] == [
        ("tool_result", {"tool": "Bash", "tool_use_id": "t1", "ok": False, "summary": "command not found"}),
        ("exit_code", {"command": "cargo test", "tool_use_id": "t1", "code": 1}),
        ("test_result", {"command": "cargo test", "tool_use_id": "t1", "passed": False}),
    ]

    interrupted = adapter.tool_events(
        {**common, "tool_input": {"command": "pytest"}, "error": "Exit code 130", "is_interrupt": True}, c, failed=True
    )
    assert [e.kind.value for e in interrupted] == ["tool_call", "tool_result"]


def test_tool_call_inputs_are_one_line_summaries_truncated_to_the_payload_limit():
    c = cfg(signals={"payload_max_chars": 12})

    def summary(tool, tool_input):
        events = adapter.tool_events({"session_id": "s", "tool_name": tool, "tool_input": tool_input, "tool_use_id": "t"}, c, failed=False)
        return events[0].payload["input"]

    assert summary("Skill", {"skill": "pdf", "args": "x"}) == "pdf"
    assert summary("Read", {"file_path": "/a/b.py"}) == "/a/b.py"
    assert summary("Grep", {"pattern": "x"}) == '{"pattern":"'
    assert summary("Bash", {"command": "echo 0123456789abcdef"}) == "echo 0123456"


def test_transcript_tokens_count_each_message_once_and_stop_before_the_first_write():
    usage = {"input_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 50, "output_tokens": 10}
    records = [
        {"type": "assistant", "message": {"id": "m1", "model": "<synthetic>", "usage": usage, "content": [{"type": "thinking"}]}},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-x", "usage": usage, "content": [{"type": "text"}]}},
        {"type": "user", "message": {"content": "ok"}},
        {"type": "assistant", "message": {"id": "m2", "model": "claude-x", "usage": usage, "content": [{"type": "thinking"}]}},
        {"type": "assistant", "message": {"id": "m2", "model": "claude-x", "usage": usage, "content": [{"type": "tool_use", "name": "Edit"}]}},
        {"type": "assistant", "message": {"id": "m3", "model": "claude-x", "usage": usage, "content": [{"type": "text"}]}},
    ]
    assert adapter.transcript_usage(records) == adapter.TranscriptUsage(480, 160, True)
    assert adapter.transcript_model(records) == "claude-x"


def write_event(session: str, path, pre: str, post: str) -> Event:
    return Event.new(Kind.FILE_WRITE, Actor.TOOL, session, {"path": str(path), "pre_hash": pre, "post_hash": post, "tool": "Write", "tool_use_id": "t"})


def start(session: str, project_dir) -> Event:
    return Event.new(Kind.SESSION_START, Actor.RUNNER, session, {"state": State(project=str(project_dir)).to_json()})


def test_file_changes_compare_the_previous_sessions_writes_with_disk(tmp_path, memory):
    c = memory.cfg
    created, adopted, rewritten = tmp_path / "created.py", tmp_path / "adopted.py", tmp_path / "rewritten.py"
    adopted.write_text("kept\n")
    rewritten.write_text("v2\n")
    events = [
        start("a", tmp_path),
        write_event("a", created, "absent", "h-created"),
        write_event("a", adopted, "absent", signals.snapshot(adopted, c)),
        write_event("a", rewritten, signals.file_hash(tmp_path / "missing"), "h1"),
        write_event("a", rewritten, "h1", signals.snapshot(rewritten, c)),
        start("other-project", tmp_path / "elsewhere"),
        write_event("other-project", adopted, "absent", "zzz"),
    ]
    rewritten.unlink()
    world = project(events)
    changes = signals.detect_file_changes(world, "b", State(project=str(tmp_path)), c)
    assert kinds_and_payloads(changes) == [
        ("user_undo", {"path": str(created), "write_event_id": events[1].id, "write_session_id": "a"}),
        ("user_undo", {"path": str(rewritten), "write_event_id": events[4].id, "write_session_id": "a"}),
    ]
    again = signals.detect_file_changes(world, "b", State(project=str(tmp_path)), c)
    assert [e.id for e in again] == [e.id for e in changes]


def test_similarity_ignores_case_punctuation_and_rkm_commands():
    assert signals.similarity("Run the tests, then commit!", "run the TESTS then commit") == 1.0
    assert signals.similarity("run the tests then commit", "run the tests then push") == 4 / 6
    assert signals.similarity("rkm reject r_1 run the tests", "rkm reject r_1 run the tests") == 0.0
    assert signals.similarity("continue", "continue") == 0.0


def test_only_plain_runner_invocations_count_as_tests():
    c = cfg()
    plain = [
        "pytest", "uv run pytest -q tests/", "npm test", "python -m pytest -x", "cargo test --all",
        "go test -v ./...", "pytest --tb=short tests/test_app.py", "npm test -- --run",
    ]
    assert [adapter.is_test_command(cmd, c) for cmd in plain] == [True] * 8
    forged = [
        "echo pytest", "pytest; true", "pytest || true", "pytest | tee out", "cd x && pytest", "pytest > log",
        "true # pytest", "PATH=/tmp/x pytest", "pytest --collect-only", "uv run pytest --version",
        "pytest -p myplugin", "pytest -o addopts=--co", "pytest --collect-only-ish",
        "pytest -pmyplugin", "pytest -oaddopts=--co", "pytest --rootdir=/tmp/x", "pytest --confcutdir=/",
        "pytest -qp myplugin", "go test -list .", "go test -c", "go test -exec=true ./...",
        "pytest /tmp/fake_test.py", "pytest ../elsewhere", "pytest @args.txt", "pytest -q=x",
        "npm test --workspace=evil", "pytest --tb",
        "make test PYTEST=true", "make test other-target", "go test example.com/other/pkg", "npm test -- evil.js",
        "go test ./../other/...",
    ]
    assert [adapter.is_test_command(cmd, c) for cmd in forged] == [False] * 32


def test_a_passing_test_result_needs_the_runner_summary_to_show_tests_ran():
    c = cfg()
    base = {"session_id": "a", "transcript_path": "/t/a.jsonl", "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "pytest"}}
    ran = adapter.tool_events({**base, "tool_use_id": "t1", "tool_response": {"stdout": "== 3 passed in 0.1s =="}}, c, failed=False)
    empty = adapter.tool_events({**base, "tool_use_id": "t2", "tool_response": {"stdout": "no tests ran"}}, c, failed=False)
    assert [e.kind.value for e in ran] == ["tool_call", "tool_result", "exit_code", "test_result"]
    assert [e.kind.value for e in empty] == ["tool_call", "tool_result", "exit_code"]


def test_only_the_closing_summary_of_a_run_that_ran_tests_counts():
    assert adapter.tests_ran("collected 3 items\n...\n== 3 passed in 0.12s ==")
    assert adapter.tests_ran("ok  \texample.com/pkg\t0.012s")
    assert not adapter.tests_ran("ok  \texample.com/pkg\t0.012s [no tests to run]")
    assert not adapter.tests_ran("?   \texample.com/pkg\t[no test files]")
    assert not adapter.tests_ran("3 passed\n" + "\n".join(f"line {i}" for i in range(6)) + "\nno tests ran")


def test_a_caller_without_a_transcript_records_no_command_outcomes():
    bridged = {
        "session_id": "a", "transcript_path": "", "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_use_id": "t1", "tool_input": {"command": "pytest"}, "tool_response": "1 failed, 2 passed",
    }
    assert [e.kind.value for e in adapter.tool_events(bridged, cfg(), failed=False)] == ["tool_call", "tool_result"]
