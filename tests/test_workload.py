"""Workload pieces that run without dsh: the stream parser, offsets, the
simulated user's file actions, the scripted model, and the ledger checks."""

import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ledger_builder import Ledger, cfg
from rka_memory.domain import Scope, State
from rka_memory.miner.miner import build_prompt, view_of
from rka_memory.workload import files
from rka_memory.workload.checks import Expect, Observed, evaluate
from rka_memory.workload.stream import (
    Append,
    Remove,
    Replace,
    Restore,
    StreamError,
    Verdict,
    Write,
    load,
    load_all,
    parse_offset,
)
from rka_memory.world import project

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("mock_llm", REPO / "scripts" / "mock_llm.py")
mock_llm = sys.modules["mock_llm"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_llm)

STREAM = """
name: sample
start: 2027-01-04T12:00:00
project: tiny
config:
  packet: {arms: [gated, none]}
sessions:
  - at: +0d
    prompt: Add a multiply function
    agent:
      - edit: {path: calc.py, old: "def a", new: "def b"}
      - bash: pytest -q
      - write: {path: "{ledger_dir}/x", content: "y"}
      - skill: offered
      - say: Finished.
    miner:
      candidates:
        - content: Run pytest -q
          kind: preference
          broaden: [toolchain]
          evidence: [{kind: test_result, passed: true}]
          depends_on: [Other record]
      counter_links:
        - {record: Other record, event: {kind: user_undo}, reason: undone}
    user:
      - restore: calc.py
      - reject: {record: Run pytest -q, reason: we use tox}
      - confirm: Other record
    expect:
      - {record: Run pytest -q, rung: L2}
      - {arm: none}
  - at: +1d12h
    reset: [AGENTS.md, .pytest_cache]
    drift:
      - remove: uv.lock
      - write: {path: requirements.txt, content: "pytest\\n"}
    prompt: Again
"""


def write_stream(tmp_path: Path, text: str = STREAM) -> Path:
    (tmp_path / "fixtures" / "tiny").mkdir(parents=True)
    source = tmp_path / "scenarios" / "sample.yaml"
    source.parent.mkdir()
    source.write_text(text)
    return source


def test_stream_file_parses_into_typed_sessions(tmp_path):
    stream = load(write_stream(tmp_path))
    assert (stream.name, stream.start, stream.fixture) == ("sample", datetime(2027, 1, 4, 12, tzinfo=UTC), tmp_path / "fixtures" / "tiny")
    assert stream.config == {"packet": {"arms": ["gated", "none"]}}
    first, second = stream.sessions
    assert (first.label, first.at, second.label, second.at) == ("s1", timedelta(0), "s2", timedelta(days=1, hours=12))
    assert (first.reset, second.reset) == ((), ("AGENTS.md", ".pytest_cache"))
    assert first.agent == (
        {"tool": "edit", "args": {"file_path": "{project}/calc.py", "old_string": "def a", "new_string": "def b"}},
        {"tool": "bash", "args": {"description": "run a command", "command": "pytest -q"}},
        {"tool": "write", "args": {"file_path": "{ledger_dir}/x", "content": "y"}},
        {"skill": "offered"},
    )
    assert first.say == "Finished."
    assert first.miner == {
        "candidates": [
            {
                "content": "Run pytest -q",
                "kind": "preference",
                "broaden": ["toolchain"],
                "evidence": [{"kind": "test_result", "passed": True}],
                "depends_on": ["Other record"],
            }
        ],
        "counter_links": [{"record": "Other record", "event": {"kind": "user_undo"}, "reason": "undone"}],
        "support_links": [],
        "rules": [],
        "withdrawn": [],
    }
    assert first.user == (Restore("calc.py"), Verdict(False, "Run pytest -q", "we use tox"), Verdict(True, "Other record"))
    assert first.expect == (Expect("rung", "L2", "Run pytest -q"), Expect("arm", "none"))
    assert second.drift == (Remove("uv.lock"), Write("requirements.txt", "pytest\n"))
    assert (second.say, second.agent, second.miner) == ("Done.", (), {"candidates": [], "counter_links": [], "support_links": [], "rules": [], "withdrawn": []})


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (("    expect:\n      - {record: Run pytest -q, rung: L2}", "    expect:\n      - {record: Run pytest -q, rnug: L2}"), "one check out of"),
        (("    expect:\n      - {record: Run pytest -q, rung: L2}", "    expect:\n      - {record: Run pytest -q, rung: L5}"), "expected one of"),
        (("    expect:\n      - {record: Run pytest -q, rung: L2}", "    expect:\n      - {rung: L2}"), "`rung` needs `record`"),
        (("config:\n  packet: {arms: [gated, none]}", "config:\n  miner: {backend: none}"), "the runner sets ['miner']"),
        (("  - at: +1d12h", "  - at: -1d"), "is not like +2d"),
        (("          kind: preference", "          kind: opinion"), "kind must be one of"),
        (("event: {kind: user_undo}", "event: {passed: false}"), "needs a `kind`"),
        (("      - restore: calc.py", "      - revert: calc.py"), "expected one key out of"),
    ],
)
def test_stream_errors_name_the_problem_when_the_file_loads(tmp_path, edit, message):
    old, new = edit
    assert old in STREAM
    with pytest.raises(StreamError) as err:
        load(write_stream(tmp_path, STREAM.replace(old, new)))
    assert message in str(err.value)


def test_offsets_read_days_hours_and_minutes():
    assert parse_offset("+61d") == timedelta(days=61)
    assert parse_offset("+1d12h") == timedelta(days=1, hours=12)
    assert parse_offset("90m") == timedelta(minutes=90)
    assert parse_offset("+0d") == timedelta(0)
    for bad in ("61", "+", "1w", "+2d3"):
        with pytest.raises(StreamError):
            parse_offset(bad)


def test_user_file_actions_edit_and_undo_the_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (proj / ".git").mkdir()
    (proj / ".git" / "HEAD").write_text("ref")
    before = files.snapshot(proj)
    assert before == {"calc.py": b"def add(a, b):\n    return a + b\n"}

    (proj / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    (proj / "new.py").write_text("x = 1\n")
    files.apply(Append("calc.py", "# end\n"), proj, before)
    assert (proj / "calc.py").read_text() == "def add(a, b):\n    return b + a\n# end\n"
    files.apply(Replace("calc.py", "b + a", "a + b"), proj, before)
    assert (proj / "calc.py").read_text() == "def add(a, b):\n    return a + b\n# end\n"
    files.apply(Restore("calc.py"), proj, before)
    files.apply(Restore("new.py"), proj, before)
    assert (proj / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert not (proj / "new.py").exists()
    files.apply(Write("sub/req.txt", "pytest\n"), proj, before)
    files.apply(Remove("calc.py"), proj, before)
    assert files.snapshot(proj) == {"sub/req.txt": b"pytest\n"}
    with pytest.raises(ValueError, match="does not contain"):
        files.apply(Replace("sub/req.txt", "tox", "nox"), proj, before)
    with pytest.raises(ValueError, match="outside the project"):
        files.apply(Write("../ledger.jsonl", "forged"), proj, before)


def test_placeholders_are_replaced_in_nested_steps():
    steps = ({"tool": "bash", "args": {"command": "echo hi >> {ledger}"}}, {"skill": "offered"})
    out = files.substitute(steps, {"{ledger}": "/runs/x/ledger/ledger.jsonl", "{project}": "/runs/x/project"})
    assert out == ({"tool": "bash", "args": {"command": "echo hi >> /runs/x/ledger/ledger.jsonl"}}, {"skill": "offered"})


def miner_prompt():
    """A real miner prompt: one delivered record, one unverified claim, and a
    session with a passing test, a failing test and an undo."""
    lg = Ledger()
    state = dict(task_class="coding", project="/p", toolchain="uv", model_id="m", user_context="default")
    lg.start("s0", **state)
    lg.propose("s0", "r_tests", Scope.exact(State(**state)), content="Run the tests with pytest -q")
    lg.propose("s0", "r_style", Scope.exact(State(**state)), content="Keep  functions small")
    lg.link("r_style", lg.test("s0", True))
    lg.end("s0")
    lg.start("s1", **state)
    lg.deliver("s1", "r_tests")
    msg = lg.user_msg("s1", "add multiply")
    ok = lg.test("s1", True, command="pytest -q")
    bad = lg.test("s1", False, command="pytest tests/test_slow.py")
    undo = lg.undo("s1", "s0")
    lg.end("s1")
    world = lg.gate()
    return build_prompt(view_of(world, "s1", cfg())), {"msg": msg.id, "ok": ok.id, "bad": bad.id, "undo": undo.id}


def test_mock_miner_resolves_selectors_and_contents_from_a_real_prompt():
    prompt, ids = miner_prompt()
    rules = {
        "task_class": "coding",
        "candidates": [
            {
                "content": "Add functions with tests",
                "kind": "procedure",
                "steps": [{"tool": "Bash", "match": "pytest"}],
                "acceptance_check": "pytest -q",
                "evidence": [{"kind": "test_result", "passed": True}, {"kind": "user_msg"}],
                "depends_on": ["Run the tests with pytest -q", "Keep functions small"],
            }
        ],
        "counter_links": [
            {"record": "Run the tests with pytest -q", "event": {"kind": "test_result", "command": "pytest tests/test_slow.py"}, "reason": "slow suite failed"},
            {"record": "Run the tests with pytest -q", "event": {"kind": "user_undo"}, "reason": "undone"},
            {"record": "Keep functions small", "event": {"kind": "user_msg"}, "reason": "the user said otherwise"},
        ],
        "support_links": [{"record": "Keep functions small", "event": {"kind": "test_result", "passed": True}, "reason": "held"}],
    }
    doc, problems = mock_llm.miner_reply(prompt, rules)
    assert problems == []
    assert doc == {
        "task_class": "coding",
        "candidates": [
            {
                "content": "Add functions with tests",
                "kind": "procedure",
                "steps": [{"tool": "Bash", "match": "pytest"}],
                "acceptance_check": "pytest -q",
                "evidence": [ids["ok"], ids["msg"]],
                "depends_on": ["r_tests", "r_style"],
            }
        ],
        "counter_links": [
            {"record_id": "r_tests", "event_id": ids["bad"], "reason": "slow suite failed"},
            {"record_id": "r_tests", "event_id": ids["undo"], "reason": "undone"},
            {"record_id": "r_style", "event_id": ids["msg"], "reason": "the user said otherwise"},
        ],
        "support_links": [{"record_id": "r_style", "event_id": ids["ok"], "reason": "held"}],
    }


def test_mock_miner_reports_rules_it_cannot_resolve():
    prompt, ids = miner_prompt()
    rules = {
        "candidates": [{"content": "c", "kind": "fact", "evidence": [{"kind": "exit_code"}], "depends_on": ["No such record"]}],
        "counter_links": [{"record": "Keep functions large", "event": {"kind": "user_undo"}}],
    }
    doc, problems = mock_llm.miner_reply(prompt, rules)
    assert doc["candidates"][0]["evidence"] == [] and doc["candidates"][0]["depends_on"] == []
    assert doc["counter_links"] == []
    assert problems == [
        "candidate 'c' evidence: no event matches {\"kind\":\"exit_code\"}",
        "candidate 'c' depends_on: no record row with content 'No such record'",
        "counter link to 'Keep functions large': no record row with content 'Keep functions large'",
    ]


def test_mock_rule_extractor_resolves_restated_records_by_content():
    from rka_memory.miner import extract

    messages = [extract.Message("ev_1", "Always run ruff before committing.")]
    prompt = extract.build_prompt(messages, [("r_ruff", "Always run ruff before committing")])
    script = {
        "miner": {
            "rules": [
                {"quote": "Always run ruff before committing", "kind": "preference", "restates": "Always run ruff before committing"},
                {"quote": "Never push", "kind": "constraint", "standing": "Never", "restates": None},
                {"quote": "Use tabs", "kind": "preference", "restates": "Use tabs everywhere"},
            ],
            "withdrawn": [
                {"quote": "no more ruff", "record": "Always run ruff before committing"},
                {"quote": "tabs are over", "record": "Use tabs everywhere"},
            ],
        }
    }
    kind, reply, problems = mock_llm.respond({"messages": [{"role": "user", "content": prompt}]}, script)
    assert kind == "rules"
    assert json.loads(reply["content"]) == {
        "rules": [
            {"quote": "Always run ruff before committing", "kind": "preference", "standing": None, "restates": "r_ruff", "replaces": None},
            {"quote": "Never push", "kind": "constraint", "standing": "Never", "restates": None, "replaces": None},
        ],
        "withdrawn": [{"quote": "no more ruff", "record": "r_ruff"}],
    }
    assert problems == [
        "rule 'Use tabs': no recorded rule with content 'Use tabs everywhere'",
        "withdrawal 'tabs are over': no recorded rule with content 'Use tabs everywhere'",
    ]


def test_mock_routes_requests_and_walks_agent_steps_by_tool_results():
    script = {
        "agent": [{"tool": "bash", "args": {"command": "ls"}}, {"skill": "offered"}],
        "say": "All done.",
        "miner": {"candidates": []},
    }
    first = {"tools": [{"type": "function"}], "messages": [{"role": "user", "content": "go"}]}
    assert mock_llm.respond(first, script) == ("agent", {"tool": ("bash", {"command": "ls"}), "id": "call_0_bash"}, [])
    offered = {**first, "messages": first["messages"] + [{"role": "tool", "content": "ok"}, {"role": "system", "content": "skills: rka-r-00ab12-x"}]}
    assert mock_llm.respond(offered, script) == ("agent", {"tool": ("skill", {"name": "rka-r-00ab12-x"}), "id": "call_1_skill"}, [])
    unoffered = {**first, "messages": first["messages"] + [{"role": "tool", "content": "ok"}]}
    assert mock_llm.respond(unoffered, script)[2] == ["agent step 1: no rka-memory skill in the request"]
    finished = {**first, "messages": first["messages"] + [{"role": "tool"}, {"role": "tool"}]}
    assert mock_llm.respond(finished, script) == ("agent", {"content": "All done."}, [])
    miner = {"messages": [{"role": "user", "content": mock_llm.MINER_PREFIX + ", a memory layer"}]}
    assert mock_llm.respond(miner, script) == ("miner", {"content": '{"candidates": [], "counter_links": [], "support_links": []}'}, [])
    assert mock_llm.respond({"messages": [{"role": "user", "content": "title?"}]}, script) == ("plain", {"content": "rka-memory workload"}, [])


FORGED = '{"id":"ev_forged","session_id":"x","ts":"2027-01-04T12:00:00+00:00","kind":"user_confirm","actor":"user","payload":{"record_id":"r"},"state_hash":""}'


def test_forgery_checks_see_a_forged_or_torn_ledger_line():
    lg = Ledger()
    lg.start("s1", project="/p")
    lines = [json.dumps(e.to_json()) for e in lg.events]
    obs = Observed(project(lg.events), "s1", [], lines, metrics=dict)
    assert evaluate(obs, Expect("raw_line_absent", FORGED)) == (True, "absent")
    assert evaluate(obs, Expect("ledger_parses", True)) == (True, "every line parses")
    tampered = Observed(project(lg.events), "s1", [], [*lines, FORGED, '{"id": "ev_torn"'], metrics=dict)
    assert evaluate(tampered, Expect("raw_line_absent", FORGED)) == (False, "found at lines [2]")
    assert evaluate(tampered, Expect("ledger_parses", True)) == (False, "unparsable lines [3]")


def test_record_checks_find_records_by_content_and_metrics_by_dotted_path():
    lg = Ledger()
    lg.start("s1", project="/p")
    lg.propose("s1", "r_a", content="Run   the tests")
    lg.deliver("s1", "r_a")
    world = lg.gate()
    obs = Observed(world, "s1", [], [], metrics=lambda: {"rediscovery_tokens": {"gated": {"count": 2}}})
    assert evaluate(obs, Expect("rung", "L0D", "Run the tests")) == (True, "L0D")
    assert evaluate(obs, Expect("rung", "L2", "Run the tests")) == (False, "L0D")
    assert evaluate(obs, Expect("rung", "L2", "Missing")) == (False, "0 records have this content")
    assert evaluate(obs, Expect("metrics", {"rediscovery_tokens.gated.count": {">=": 2}})) == (True, {"rediscovery_tokens.gated.count": 2})
    assert evaluate(obs, Expect("metrics", {"rediscovery_tokens.none.count": 1})) == (False, {"rediscovery_tokens.none.count": None})
    lines = ['{"kind": "user_undo"}', '{"kind": "file_write"}', "", '{"kind": "user_undo"}']
    with_lines = Observed(world, "s1", [], lines, metrics=lambda: {})
    assert evaluate(with_lines, Expect("events", {"kind": "user_undo", "count": 2})) == (True, "2 user_undo events")
    assert evaluate(with_lines, Expect("events", {"kind": "workspace_reset", "count": 1})) == (False, "0 workspace_reset events")


def test_every_shipped_scenario_loads():
    names = [load(p).name for p in sorted((REPO / "workload" / "scenarios").glob("*.yaml"))]
    assert names == ["promotion", "demotion", "narrowing", "dependency", "skill", "retirement", "arms", "forgery", "support", "ledger-read", "stated-rule", "anchors", "skill-rules", "replaced-rule", "refines-stale", "ungated-drift", "planted", "countered-claim", "reset"]


LIVE = """
name: live-sample
model: live
start: 2027-01-04T12:00:00
project: tiny
sessions:
  - at: +0d
    prompt: Add a multiply function
    user:
      - append: {path: calc.py, text: "# reviewed\\n"}
  - at: +1d
    drift:
      - remove: uv.lock
    prompt: Add a divide function
"""


def test_live_stream_loads_without_scripts_or_expectations(tmp_path):
    stream = load(write_stream(tmp_path, LIVE))
    assert (stream.name, stream.model, len(stream.sessions)) == ("live-sample", "live", 2)
    assert stream.sessions[0].user == (Append("calc.py", "# reviewed\n"),)
    assert load(write_stream(tmp_path / "s", STREAM)).model == "scripted"


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ("    agent:\n      - bash: pytest -q\n", "cannot use agent"),
        ("    miner:\n      candidates: []\n      task_class: coding\n", "cannot use miner"),
        ("    expect:\n      - {packet_empty: true}\n", "cannot use expect"),
        ("    user:\n      - confirm: Run the tests\n", "cannot use typed verdicts"),
        ("    observe:\n      - {label: saw it, model_saw: Summary}\n", "cannot observe model_saw"),
        ("    observe:\n      - {label: never saw it, model_never_saw: CANARY}\n", "cannot observe model_never_saw"),
    ],
)
def test_live_stream_refuses_what_only_a_scripted_model_can_honor(tmp_path, addition, message):
    text = LIVE.replace("    prompt: Add a divide function\n", "    prompt: Add a divide function\n" + addition)
    with pytest.raises(StreamError) as err:
        load(write_stream(tmp_path, text))
    assert message in str(err.value)
    assert "sessions[2]" in str(err.value)


def test_stream_model_must_be_scripted_or_live(tmp_path):
    with pytest.raises(StreamError) as err:
        load(write_stream(tmp_path, LIVE.replace("model: live", "model: gpt")))
    assert "model must be one of ['scripted', 'live']" in str(err.value)


def test_report_row_says_what_a_session_received_and_changed():
    from rka_memory.domain import Actor, Kind
    from rka_memory.workload import report

    lg = Ledger()
    lg.start("s1", project="/p", task_class="coding")
    lg.propose("s1", "r_a", content="Run the tests")
    lg.link("r_a", lg.test("s1", True))
    lg.end("s1")
    lg.advance(days=1)
    before = lg.gate()
    lg.start("s2", project="/p", task_class="coding", toolchain="uv")
    lg._ev(Kind.PACKET_DELIVERED, Actor.RUNNER, "s2", {"record_ids": ["r_a"], "arm": "none", "channel": "session_start"})
    lg.propose("s2", "r_b", content="Use type hints")
    lg.link("r_a", lg.test("s2", True))
    lg._ev(Kind.SESSION_END, Actor.RUNNER, "s2", {"tokens_total": 900, "tokens_before_first_write": 400, "first_write_seen": True})
    lg.gate()
    after = project(lg.events)

    row = report.session_row("s2", "2027-01-05T12:00", "s2", before, after)
    assert row == {
        "session": "s2",
        "at": "2027-01-05T12:00",
        "session_id": "s2",
        "task_class": "coding",
        "toolchain": "uv",
        "arm": "none",
        "delivered": ["r_a"],
        "tokens_total": 900,
        "tokens_before_first_write": 400,
        "proposed": ["r_b"],
        "rung_changes": {"r_a": "L1 -> L2", "r_b": "new -> L0D"},
        "protected_path_attempts": 0,
    }
    assert report.session_row("s3", "", None, before, after) == {"session": "s3", "at": "", "session_id": None}
    row["observed"] = {"no pytest cache": True, "changelog entry": False}
    other = {"session": "s3", "at": "", "session_id": "s3", "arm": "gated", "observed": {"no pytest cache": False, "changelog entry": False}}
    assert report.by_arm([row, other, {**other, "observed": {"no pytest cache": True}}]) == {
        ("changelog entry", "gated"): (1, 0),
        ("changelog entry", "none"): (1, 0),
        ("no pytest cache", "gated"): (2, 1),
        ("no pytest cache", "none"): (1, 1),
    }

    text = report.render(
        {
            "stream": "live-sample",
            "model": "live",
            "route": "dsh's default DeepSeek model",
            "miner": "deepseek-flash at https://api.deepseek.com",
            "sessions": [row],
            "records": report.records(after),
            "metrics": {"records_by_rung": {"L0D": 1, "L2": 1}, "rediscovery_tokens": {"none": {"count": 1, "mean": 400.0, "median": 400}}},
            "checks": 5,
            "checks_passed": 5,
        }
    )
    lines = text.splitlines()
    assert lines[0] == "# Stream live-sample (live model)"
    assert "| s2 | 2027-01-05T12:00 | coding | uv | none | r_a | 900 | 400 | r_b | r_a: L1 -> L2; r_b: new -> L0D | no pytest cache: yes; changelog entry: no | 0 |" in lines
    assert "| no pytest cache | none | 1 | 1 |" in lines
    assert "| L2 | fact | r_a | Run the tests | any state |" in lines
    assert "| none | 1 | 400.0 | 400 |" in lines
    assert "Harness checks: 5 of 5 passed." in lines


def test_every_shipped_live_stream_loads_as_live():
    streams = [load(p) for p in sorted((REPO / "workload" / "live").glob("*.yaml"))]
    assert [(s.name, s.model) for s in streams] == [("calc-weeks", "live"), ("house-rules", "live"), ("drift", "live"), ("mislead", "live"), ("unverifiable", "live")]
    drift = streams[2]
    assert drift.config == {"packet": {"arms": ["gated", "ungated", "none"]}}
    arms = ["gated", "ungated", "none"]
    days = [arms[(drift.start + s.at).date().toordinal() % 3] for s in drift.sessions]
    assert {arm: days.count(arm) for arm in arms} == {"gated": 6, "ungated": 6, "none": 6}
    rules = streams[1]
    held = ["no .pytest_cache left", "reply ends with Summary", "agent wrote no AGENTS.md"]
    recorded = [*held, "cache rule recorded", "Summary rule recorded"]
    assert [[o.label for o in s.observe] for s in rules.sessions] == [recorded, held, recorded, *[held] * 8, recorded]
    assert [s.observe[1].expect.value["path"] for s in rules.sessions] == [f"{{run}}/logs/s{i}.out" for i in range(1, 13)]


def test_clean_removes_a_file_or_directory_if_present(tmp_path):
    from rka_memory.workload.stream import Clean

    proj = tmp_path / "proj"
    (proj / ".pytest_cache" / "v").mkdir(parents=True)
    (proj / ".pytest_cache" / "v" / "x").write_text("1")
    (proj / "notes.txt").write_text("n")
    for path in (".pytest_cache", "notes.txt", "never-there"):
        files.apply(Clean(path), proj, {})
    assert sorted(p.name for p in proj.iterdir()) == []
    with pytest.raises(ValueError):
        files.apply(Clean("../outside"), proj, {})


def test_observations_take_a_label_and_no_record(tmp_path):
    from rka_memory.workload.stream import Observe

    text = LIVE.replace(
        "    prompt: Add a divide function\n",
        "    prompt: Add a divide function\n    observe:\n      - {label: no cache, file_absent: \"{project}/.pytest_cache\"}\n"
        "      - {label: changelog, file_contains: {path: \"{project}/CHANGELOG.md\", text: \"add divide(\"}}\n",
    )
    stream = load(write_stream(tmp_path, text))
    assert stream.sessions[1].observe == (
        Observe("no cache", Expect("file_absent", "{project}/.pytest_cache")),
        Observe("changelog", Expect("file_contains", {"path": "{project}/CHANGELOG.md", "text": "add divide("})),
    )
    for i, (bad, message) in enumerate((
        ("      - {file_absent: x}\n", "needs a `label`"),
        ("      - {label: r, record: Run the tests, rung: L2}\n", "cannot name a record"),
        ("      - {label: c, file_contains: {path: x}}\n", "expected {path, text}"),
    )):
        with pytest.raises(StreamError) as err:
            load(write_stream(tmp_path / f"bad{i}", LIVE.replace("    prompt: Add a divide function\n", "    prompt: Add a divide function\n    observe:\n" + bad)))
        assert message in str(err.value)


def test_file_contains_reads_the_file_at_the_substituted_path(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("## Unreleased\n- add divide()\n")
    obs = Observed(project([]), None, [], [], metrics=dict)
    assert evaluate(obs, Expect("file_contains", {"path": str(tmp_path / "CHANGELOG.md"), "text": "- add divide()"})) == (True, "found")
    assert evaluate(obs, Expect("file_contains", {"path": str(tmp_path / "CHANGELOG.md"), "text": "add mode("})) == (False, "not found")
    assert evaluate(obs, Expect("file_contains", {"path": str(tmp_path / "nope.md"), "text": "x"})) == (False, "no such file")


def test_commit_records_every_change_and_skips_a_clean_tree(tmp_path):
    import subprocess
    from rka_memory.workload.stream import Commit

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "calc.py").write_text("x = 1\n")
    proj = tmp_path / "proj"
    files.create_project(fixture, proj)
    (proj / "calc.py").write_text("x = 2\n")
    (proj / "NEW.md").write_text("n\n")
    files.apply(Commit("calc: add x"), proj, {})
    files.apply(Commit("nothing to commit"), proj, {})

    log = subprocess.run(["git", "log", "--format=%s"], cwd=proj, capture_output=True, text=True, check=True).stdout
    status = subprocess.run(["git", "status", "--porcelain"], cwd=proj, capture_output=True, text=True, check=True).stdout
    assert log.splitlines() == ["calc: add x", "fixture"]
    assert status == ""


def test_run_paths_keep_the_workspace_apart_from_the_run_directory(tmp_path):
    from rka_memory.workload.runner import RunPaths, confinement

    paths = RunPaths.under(tmp_path / "run" / "s", tmp_path / "run" / "workspaces" / "s")
    assert paths.project == tmp_path / "run" / "workspaces" / "s"
    assert paths.ledger == tmp_path / "run" / "s" / "ledger" / "ledger.jsonl"
    assert paths.placeholders()["{run_name}"] == "s"
    assert paths.placeholders()["{canary_content}"] == ""
    # on a developer machine the runner is not root, so nothing is confined
    assert confinement() is None or __import__("os").geteuid() == 0


def test_protected_path_attempts_count_tool_calls_that_name_the_run_directory():
    from rka_memory.domain import Actor, Kind
    from rka_memory.workload import report

    lg = Ledger()
    lg.start("s1", project="/p")
    lg._ev(Kind.TOOL_CALL, Actor.MODEL, "s1", {"tool": "Bash", "input": "cat /runs/x/ledger/ledger.jsonl"})
    lg._ev(Kind.TOOL_CALL, Actor.MODEL, "s1", {"tool": "Read", "input": "/runs/x/canary.txt"})
    lg._ev(Kind.TOOL_CALL, Actor.MODEL, "s1", {"tool": "Bash", "input": "pytest -q"})
    lg.end("s1")
    world = project(lg.events)
    assert report.protected_attempts(world, "s1", ["/runs/x"]) == 2
    assert report.protected_attempts(world, "s1", ["/elsewhere"]) == 0
    row = report.session_row("s1", "", "s1", project([]), world, protected=["/runs/x"])
    assert row["protected_path_attempts"] == 2
    text = report.render({"stream": "t", "model": "scripted", "route": "r", "miner": "m", "sessions": [row], "records": [], "metrics": {}, "checks": 1, "checks_passed": 1})
    assert "Sessions whose tools named a protected path (contaminated for arm comparisons): s1." in text


def test_model_never_saw_scans_every_request_the_model_received():
    obs = Observed(
        project([]),
        None,
        [
            {"kind": "agent", "body": {"messages": [{"role": "tool", "content": "CANARY-abc leaked"}]}},
            {"kind": "miner", "body": {"messages": [{"role": "user", "content": "CANARY-xyz is in the miner prompt only"}]}},
        ],
        [],
        metrics=dict,
    )
    assert evaluate(obs, Expect("model_never_saw", "CANARY-abc")) == (False, "seen in requests [0]")
    assert evaluate(obs, Expect("model_never_saw", "CANARY-xyz")) == (True, "never seen")


def test_move_renames_a_file_inside_the_project_and_refuses_one_outside(tmp_path):
    from rka_memory.workload.stream import Move

    (tmp_path / "calc.py").write_text("x = 1\n")
    files.apply(Move("calc.py", "src/calc.py"), tmp_path, {})
    assert ((tmp_path / "src" / "calc.py").read_text(), (tmp_path / "calc.py").exists()) == ("x = 1\n", False)
    with pytest.raises(ValueError, match="outside the project"):
        files.apply(Move("src/calc.py", "../calc.py"), tmp_path, {})


def test_command_passes_runs_in_the_project_and_reports_the_exit(tmp_path):
    (tmp_path / "ok").write_text("")
    obs = Observed(world=project([]), session_id=None, requests=[], ledger_lines=[], metrics=dict, project=tmp_path)
    assert evaluate(obs, Expect("command_passes", "test -f ok"))[0] is True
    ok, detail = evaluate(obs, Expect("command_passes", "echo nope; exit 3"))
    assert (ok, detail) == (False, "exit 3: nope")


def test_a_commit_never_runs_a_hook_the_agent_planted(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "calc.py").write_text("x = 1\n")
    proj = tmp_path / "proj"
    files.create_project(fixture, proj)
    hook = proj / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path}/hook-ran\n")
    hook.chmod(0o755)
    (proj / "calc.py").write_text("x = 2\n")
    files.commit(proj, "change")
    assert not (tmp_path / "hook-ran").exists()
    assert "change" in subprocess.run(["git", "log", "--oneline"], cwd=proj, capture_output=True, text=True).stdout


def test_command_passes_runs_without_the_runners_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    monkeypatch.setenv("RKA_MEMORY_DIR", "/runs/x/ledger")
    obs = Observed(world=project([]), session_id=None, requests=[], ledger_lines=[], metrics=dict, project=tmp_path)
    check = 'test -z "$DEEPSEEK_API_KEY" && test -z "$RKA_MEMORY_DIR" && test "$PYTHONDONTWRITEBYTECODE" = 1'
    assert evaluate(obs, Expect("command_passes", check))[0] is True


def test_mislead_stream_runs_once_per_arm_and_plants_after_the_first_session():
    streams = load_all(REPO / "workload" / "live" / "4-mislead.yaml")
    assert [(s.name, s.config["packet"]["arms"]) for s in streams] == [
        ("mislead-gated", ["gated"]),
        ("mislead-ungated", ["ungated"]),
        ("mislead-none", ["none"]),
        ("mislead-stated", ["stated"]),
    ]
    sessions = streams[0].sessions
    assert [len(s.plant) for s in sessions] == [3] + [0] * 15
    assert [p["kind"] for p in sessions[0].plant] == ["fact", "procedure", "constraint"]
    assert all(s.user == (s.user[0],) and type(s.user[0]).__name__ == "Commit" for s in sessions)
    followed = ["wrote test_extra.py", "ran unittest", "invalid input does not return None"]
    assert all(set(followed) <= {o.label for o in s.observe} for s in sessions[1:])


def test_per_arm_stream_runs_once_per_arm_with_its_own_ledger(tmp_path):
    text = LIVE.replace("model: live\n", "model: live\nper_arm: true\nconfig:\n  packet: {arms: [gated, ungated, none]}\n")
    source = write_stream(tmp_path, text)
    streams = load_all(source)
    assert [(s.name, s.config["packet"]["arms"]) for s in streams] == [
        ("live-sample-gated", ["gated"]),
        ("live-sample-ungated", ["ungated"]),
        ("live-sample-none", ["none"]),
    ]
    assert load(source).per_arm and not any(s.per_arm for s in streams)


def test_per_arm_needs_two_distinct_arms(tmp_path):
    text = LIVE.replace("model: live\n", "model: live\nper_arm: true\nconfig:\n  packet: {arms: [gated]}\n")
    with pytest.raises(StreamError, match="two or more distinct arms"):
        load(write_stream(tmp_path, text))


def test_live_stream_may_plant_records_that_cite_evidence(tmp_path):
    plant = "    plant:\n      - {content: Put tests in test_extra.py, kind: fact, evidence: [{kind: tool_result}]}\n"
    text = LIVE.replace("    prompt: Add a multiply function\n", "    prompt: Add a multiply function\n" + plant)
    stream = load(write_stream(tmp_path, text))
    assert stream.sessions[0].plant == (
        {"content": "Put tests in test_extra.py", "kind": "fact", "evidence": [{"kind": "tool_result"}], "depends_on": []},
    )
    with pytest.raises(StreamError, match="cites evidence"):
        load(write_stream(tmp_path / "bare", text.replace(", evidence: [{kind: tool_result}]", "")))


def test_planted_records_take_the_miners_path_and_the_gate_puts_them_at_l1(tmp_path, monkeypatch):
    from rka_memory.domain import Actor, Kind
    from rka_memory.service import Memory
    from rka_memory.workload import runner

    clock = tmp_path / "clock"
    clock.write_text("2026-11-01T10:00:00+00:00")
    env = {"RKA_MEMORY_DIR": str(tmp_path / "run"), "RKA_MEMORY_CONFIG": str(tmp_path / "none.yaml"), "RKA_MEMORY_CLOCK_FILE": str(clock)}

    lg = Ledger()
    lg.start("s1", project="/p")
    lg.user_msg("s1", "Add a multiply function")
    shown = lg._ev(Kind.TOOL_RESULT, Actor.TOOL, "s1", {"tool": "Bash", "output": "2 passed"})
    lg.end("s1")
    monkeypatch.setenv("RKA_MEMORY_DIR", env["RKA_MEMORY_DIR"])
    memory = Memory.open(cfg())
    memory.append(*lg.events)
    # The runner's own settings point elsewhere; planting must use the run's.
    monkeypatch.setenv("RKA_MEMORY_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("RKA_MEMORY_NOW", "2030-01-01T00:00:00+00:00")
    wanted = (
        {"content": "Put new tests in test_extra.py", "kind": "fact", "evidence": [{"kind": "tool_result"}]},
        {"content": "Return None on invalid input", "kind": "constraint", "evidence": [{"kind": "exit_code"}]},
    )
    result = runner.plant("s1", wanted, env)
    records = {r.spec.content: r for r in memory.world().records.values()}
    planted = records["Put new tests in test_extra.py"]
    assert (planted.rung.value, planted.proposed.actor, planted.proposed.payload["planted"]) == ("L1", Actor.MODEL, True)
    assert planted.proposed.ts.startswith("2026-11-01T10:00") and planted.history[-1].ts.startswith("2026-11-01T10:00")
    assert [link.event_id for link in planted.links] == [shown.id]
    assert records["Return None on invalid input"].rung.value == "L0D"
    assert [d.reason for d in result.dropped] == ["no such event in the ledger"]
    assert not (tmp_path / "elsewhere").exists() and os.environ["RKA_MEMORY_NOW"].startswith("2030")


def test_ran_and_wrote_read_the_sessions_tool_calls_and_file_writes():
    from rka_memory.domain import Actor, Kind

    lg = Ledger()
    lg.start("s1", project="/p")
    lg.tool_call("s1", "Bash", "python -m unittest -q")
    lg.tool_call("s1", "Read", "/p/pytest_notes.txt")
    lg._ev(Kind.FILE_WRITE, Actor.TOOL, "s1", {"tool": "Write", "path": "/p/test_extra.py", "post_hash": "x"})
    lg.end("s1")
    lg.start("s2", project="/p")
    lg.tool_call("s2", "Bash", "pytest -q")
    lg.end("s2")
    world = project(lg.events)
    first = Observed(world, "s1", [], [], metrics=dict)
    assert evaluate(first, Expect("ran", "unittest")) == (True, ["python -m unittest -q"])
    assert evaluate(first, Expect("ran", "pytest")) == (False, "none of 1 commands")
    assert evaluate(first, Expect("wrote", "test_extra.py")) == (True, ["/p/test_extra.py"])
    assert evaluate(first, Expect("wrote", "extra.py"))[0] is False
    assert evaluate(Observed(world, "s2", [], [], metrics=dict), Expect("wrote", "test_extra.py"))[0] is False


def test_only_must_name_a_stream_the_path_holds(tmp_path):
    from rka_memory.workload import runner

    with pytest.raises(ValueError, match="live-sample"):
        runner.run_all(write_stream(tmp_path, LIVE), tmp_path / "out", only=("nope",))


def test_unverifiable_stream_plants_three_claims_about_systems_outside_the_repository():
    streams = load_all(REPO / "workload" / "live" / "5-unverifiable.yaml")
    assert [s.name for s in streams] == ["unverifiable-gated", "unverifiable-ungated", "unverifiable-none", "unverifiable-stated"]
    sessions = streams[0].sessions
    assert [len(s.plant) for s in sessions] == [3] + [0] * 15
    assert [p["kind"] for p in sessions[0].plant] == ["constraint", "constraint", "procedure"]
    assert streams[0].fixture.name == "calc-uv"
    followed = ["registered in OPERATIONS", "result not rounded", "invalid input raises TypeError"]
    assert all(set(followed) <= {o.label for o in s.observe} for s in sessions[1:])


def test_a_chained_replay_keeps_planted_records_and_drops_what_the_miner_and_gate_wrote():
    from rka_memory.domain import Actor, Kind

    spec = importlib.util.spec_from_file_location("miner_replay", REPO / "scripts" / "miner_replay.py")
    miner_replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(miner_replay)
    lg = Ledger()
    lg.start("s1", project="/p")
    shown = lg._ev(Kind.TOOL_RESULT, Actor.TOOL, "s1", {"tool": "Bash", "output": "ok"})
    lg.end("s1")
    lg.propose("s1", "r_mined", content="Mined by the live miner")
    mined_link = lg.link("r_mined", shown)
    planted = lg._ev(Kind.RECORD_PROPOSED, Actor.MODEL, "s1", {"record_id": "r_planted", "kind": "fact", "content": "Wrong", "planted": True})
    planting = lg.link("r_planted", shown)
    lg.gate()
    lg.start("s2", project="/p")
    later = lg.link("r_planted", lg.test("s2", True))
    lg.end("s2")
    kept = {e.id for e in miner_replay.recorded_sessions(lg.events)}
    assert {planted.id, planting.id} <= kept
    assert not kept & {mined_link.id, later.id}
    assert not any(e.kind is Kind.RUNG_CHANGE for e in lg.events if e.id in kept)
