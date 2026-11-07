import json
import os
import stat

import pytest

from ledger_builder import Ledger
from rka_memory.domain import Actor, Kind, RecordKind, Scope, State, Step
from rka_memory.miner import backends, extract
from rka_memory.miner.backends import BackendError, Completion
from rka_memory.miner.miner import mine_session

STATE = dict(project="/p/app", toolchain="uv", model_id="m1", user_context="default")


class FakeBackend:
    """Answers miner prompts from `replies` in order and rule-extractor
    prompts with `rules` (none stated, and free, by default)."""

    def __init__(self, *replies, rules=None):
        self.replies = list(replies)
        self.rules = rules
        self.prompts = []
        self.rule_prompts = []

    def __call__(self, prompt):
        if prompt.startswith(extract.PREFIX):
            self.rule_prompts.append(prompt)
            if self.rules is None:
                return Completion('{"rules": []}', tokens=0, seconds=0.0)
            reply = self.rules
            return Completion(reply if isinstance(reply, str) else json.dumps(reply), tokens=300, seconds=1.0)
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Completion(reply if isinstance(reply, str) else json.dumps(reply), tokens=1500, seconds=2.0)


def sync(memory, lg):
    start = getattr(lg, "synced", 0)
    memory.append(*lg.events[start:])
    lg.synced = len(lg.events)


def appended(memory, result):
    by_id = {e.id: e for e in memory.ledger.read()}
    return [by_id[i] for i in result.appended]


def session_one(memory, lg):
    lg.start("s1", **STATE)
    lg.user_msg("s1", "run the tests")
    call = lg.tool_call("s1", "Bash", "uv run pytest -q")
    ok = lg.test("s1", True)
    lg.end("s1")
    sync(memory, lg)
    reply = {
        "task_class": "testing",
        "candidates": [
            {
                "content": "Run the tests with uv run pytest",
                "kind": "fact",
                "evidence": [ok.id],
                "broaden": ["model_id"],
            },
            {"content": "The user prefers short commits", "kind": "preference", "evidence": [call.id]},
            {"content": "x", "kind": "opinion", "evidence": [ok.id]},
        ],
        "counter_links": [],
    }
    return call, ok, reply


def test_week2_grounded_candidates_reach_l1_model_cited_ones_l0d_and_duplicates_promote(memory):
    lg = Ledger()
    call, ok1, reply = session_one(memory, lg)
    first = mine_session(memory, "s1", backend=FakeBackend(reply))

    grounded, ungrounded = (p["record_id"] for p in first.proposed)
    world = memory.world()
    assert first.status == "mined"
    assert first.task_class == "testing"
    assert first.ungrounded == [ungrounded]
    assert [d.to_json() for d in first.dropped] == [
        {"item": "candidate 2: x", "reason": "unknown kind 'opinion'"},
        {"item": f"candidate 1 evidence {call.id}", "reason": "model-authored event never grounds"},
    ]
    assert world.records[grounded].rung.value == "L1"
    assert world.records[ungrounded].rung.value == "L0D"
    assert world.records[grounded].scope.describe() == (
        "task_class=testing, project=/p/app, toolchain=uv, user_context=default"
    )
    assert first.moves == [
        {"record_id": grounded, "from": None, "to": "L1", "reason": "proposed with non-model grounding"},
        {"record_id": ungrounded, "from": None, "to": "L0D", "reason": "proposed without non-model grounding"},
    ]
    events = appended(memory, first)
    assert [e.kind for e in events] == [
        Kind.JUDGMENT,
        Kind.RECORD_PROPOSED,
        Kind.EVIDENCE_LINKED,
        Kind.RECORD_PROPOSED,
        Kind.COST,
    ]
    assert {e.session_id for e in events} == {"s1"}
    assert [e.actor for e in events] == [Actor.MODEL] * 4 + [Actor.RUNNER]

    lg.start("s2", **STATE)
    ok2 = lg.test("s2", True)
    lint = lg.exit("s2", 1, "uv run ruff check")
    lg.end("s2")
    sync(memory, lg)
    backend = FakeBackend(
        {
            "task_class": "testing",
            "candidates": [{"content": "Run tests with uv run pytest", "kind": "fact", "evidence": [ok2.id]}],
            "counter_links": [{"record_id": grounded, "event_id": lint.id, "reason": "lint failed"}],
        }
    )
    second = mine_session(memory, "s2", backend=backend)

    assert second.proposed == []
    assert second.merged == [{"record_id": grounded, "content": "Run tests with uv run pytest", "evidence": [ok2.id]}]
    assert [d.to_json() for d in second.dropped] == [
        {
            "item": f"counter_link 0 {grounded}",
            "reason": "record was not delivered in this session or a session it edited",
        }
    ]
    assert second.links == []
    assert second.moves == [
        {"record_id": grounded, "from": "L1", "to": "L2", "reason": "grounded in enough distinct sessions"}
    ]
    assert memory.world().records[grounded].rung.value == "L2"
    kinds = [e.kind for e in appended(memory, second)]
    assert kinds == [Kind.JUDGMENT, Kind.EVIDENCE_LINKED, Kind.COST]
    assert Kind.RUNG_CHANGE not in kinds + [e.kind for e in events]

    prompt = backend.prompts[0]
    assert f"{ok2.id} test_result tool " in prompt
    assert f"- {grounded} [L1 fact] Run the tests with uv run pytest" in prompt
    assert ok1.id not in prompt


def test_mined_session_is_skipped_and_forced_remine_adds_no_duplicates(memory):
    lg = Ledger()
    _, _, reply = session_one(memory, lg)
    first = mine_session(memory, "s1", backend=FakeBackend(reply))

    idle = FakeBackend(reply)
    skipped = mine_session(memory, "s1", backend=idle)
    assert (skipped.status, idle.prompts) == ("already_mined", [])

    again = mine_session(memory, "s1", force=True, backend=FakeBackend(reply))
    ids = [p["record_id"] for p in first.proposed]
    assert again.proposed == []
    assert [(m["record_id"], m["evidence"]) for m in again.merged] == [(ids[0], []), (ids[1], [])]
    assert [e.kind for e in appended(memory, again)] == [Kind.COST]
    assert len(memory.world().records) == 2


def test_citations_must_come_from_the_mined_session_and_never_from_user_verdicts(memory):
    lg = Ledger()
    lg.start("s1", **STATE, task_class="coding")
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_known", scope=Scope())
    lg.link("r_known", ok1)
    lg.end("s1")
    lg.start("s2", **STATE, task_class="coding")
    lg.deliver("s2", "r_known")
    verdict = lg.confirm("s2", "r_known")
    ok2 = lg.test("s2", True)
    failed = lg.test("s2", False)
    lg.end("s2")
    sync(memory, lg)
    reply = {
        "task_class": "coding",
        "candidates": [{"content": "Ruff runs in CI", "kind": "fact", "evidence": [ok1.id, verdict.id, ok2.id]}],
        "counter_links": [
            {"record_id": "r_known", "event_id": ok1.id},
            {"record_id": "r_known", "event_id": verdict.id},
            {"record_id": "r_known", "event_id": ok2.id},
            {"record_id": "r_known", "event_id": failed.id, "reason": "tests failed after following it"},
        ],
    }
    result = mine_session(memory, "s2", backend=FakeBackend(reply))

    assert [p["evidence"] for p in result.proposed] == [[ok2.id]]
    assert [d.to_json() for d in result.dropped] == [
        {"item": f"candidate 0 evidence {ok1.id}", "reason": "evidence outside mined session"},
        {"item": f"candidate 0 evidence {verdict.id}", "reason": "user verdicts count only for the record they name"},
        {"item": f"counter_link 0 evidence {ok1.id}", "reason": "evidence outside mined session"},
        {
            "item": f"counter_link 1 evidence {verdict.id}",
            "reason": "user verdicts count only for the record they name",
        },
        {"item": f"counter_link 2 evidence {ok2.id}", "reason": "test_result cannot carry counter"},
    ]
    assert result.links == [
        {
            "record_id": "r_known",
            "event_id": failed.id,
            "polarity": "counter",
            "reason": "tests failed after following it",
        }
    ]
    assert result.task_class is None


def test_counter_link_reaches_a_record_delivered_in_the_session_whose_file_the_user_undid(memory):
    lg = Ledger()
    lg.start("s1", **STATE, task_class="coding")
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_known", scope=Scope())
    lg.link("r_known", ok1)
    lg.deliver("s1", "r_known")
    lg.end("s1")
    lg.start("s2", **STATE, task_class="coding")
    undo = lg.undo("s2", "s1")
    lg.end("s2")
    sync(memory, lg)
    reply = {"candidates": [], "counter_links": [{"record_id": "r_known", "event_id": undo.id, "reason": "undone"}]}
    result = mine_session(memory, "s2", backend=FakeBackend(reply))

    assert result.links == [{"record_id": "r_known", "event_id": undo.id, "polarity": "counter", "reason": "undone"}]
    assert result.dropped == []


def test_duplicate_of_a_retired_record_merges_into_it_and_stays_retired(memory):
    lg = Ledger()
    coding = dict(STATE, task_class="coding")
    lg.start("s0", **coding)
    ok0 = lg.test("s0", True)
    lg.propose(
        "s0",
        "r_old",
        scope=Scope.exact(State(**coding)),
        kind=RecordKind.FACT,
        content="Install dependencies with pip install",
    )
    lg.link("r_old", ok0)
    lg.end("s0")
    lg.gate()
    lg.advance(days=61)
    lg.gate()
    lg.start("s9", **coding)
    ok9 = lg.test("s9", True)
    lg.end("s9")
    sync(memory, lg)
    assert memory.world().records["r_old"].rung.value == "L4"

    reply = {"candidates": [{"content": "Install dependencies with pip install", "kind": "fact", "evidence": [ok9.id]}]}
    result = mine_session(memory, "s9", backend=FakeBackend(reply))

    assert result.proposed == []
    assert result.merged == [
        {"record_id": "r_old", "content": "Install dependencies with pip install", "evidence": [ok9.id]}
    ]
    assert result.moves == []
    assert list(memory.world().records) == ["r_old"]
    assert memory.world().records["r_old"].rung.value == "L4"


def test_only_procedures_keep_steps_and_checks_and_unknown_dependencies_are_dropped(memory):
    lg = Ledger()
    lg.start("s1", **STATE, task_class="coding")
    ok = lg.test("s1", True)
    lg.propose("s1", "r_known", scope=Scope())
    lg.end("s1")
    sync(memory, lg)
    steps = [{"tool": "Bash", "match": "uv sync"}, {"tool": "Bash", "match": "uv run pytest"}]
    reply = {
        "candidates": [
            {
                "content": "Sync then test",
                "kind": "procedure",
                "evidence": [ok.id],
                "steps": steps,
                "acceptance_check": "uv run pytest",
                "depends_on": ["r_known", "r_missing"],
            },
            {
                "content": "Tests live in tests/",
                "kind": "fact",
                "evidence": [ok.id],
                "steps": steps,
                "acceptance_check": "ls",
            },
        ]
    }
    result = mine_session(memory, "s1", backend=FakeBackend(reply))

    proc, fact = (memory.world().records[p["record_id"]].spec for p in result.proposed)
    assert (proc.steps, proc.acceptance_check, proc.depends_on) == (
        (Step("Bash", "uv sync"), Step("Bash", "uv run pytest")),
        "uv run pytest",
        ("r_known",),
    )
    assert (fact.steps, fact.acceptance_check) == ((), None)
    assert [d.to_json() for d in result.dropped] == [
        {"item": "candidate 0 depends_on r_missing", "reason": "no such record"}
    ]


def test_invalid_json_records_cost_and_proposes_nothing(memory):
    lg = Ledger()
    session_one(memory, lg)
    result = mine_session(memory, "s1", backend=FakeBackend("I could not find anything useful."))

    assert (result.status, result.detail) == ("parse_error", "miner output holds no JSON object")
    assert [(e.kind, e.payload["tokens"], e.payload["seconds"]) for e in appended(memory, result)] == [
        (Kind.COST, 1500, 2.0)
    ]
    assert memory.world().records == {}
    assert memory.world().sessions["s1"].mined is True


def test_failed_backend_call_leaves_session_unmined(memory):
    lg = Ledger()
    session_one(memory, lg)
    before = len(memory.ledger.read())
    result = mine_session(memory, "s1", backend=FakeBackend(BackendError("claude CLI exited 1: OAuth session expired")))

    assert (result.status, result.detail) == ("backend_error", "claude CLI exited 1: OAuth session expired")
    assert len(memory.ledger.read()) == before
    assert memory.world().sessions["s1"].mined is False


def test_none_backend_mines_nothing_and_unknown_sessions_are_reported(memory):
    lg = Ledger()
    session_one(memory, lg)
    before = len(memory.ledger.read())
    assert mine_session(memory, "s1").status == "no_output"
    assert mine_session(memory, "s404").status == "unknown_session"
    assert len(memory.ledger.read()) == before


CLI_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 8421,
    "duration_api_ms": 8013,
    "num_turns": 1,
    "result": '{"task_class": "coding", "candidates": [], "counter_links": []}',
    "session_id": "0b6c3f6e-1d7a-4c55-9a43-2f0f3a1f9d10",
    "total_cost_usd": 0.0042,
    "usage": {
        "input_tokens": 9,
        "cache_creation_input_tokens": 5120,
        "cache_read_input_tokens": 13042,
        "output_tokens": 311,
        "server_tool_use": {"web_search_requests": 0},
        "service_tier": "standard",
    },
}


def test_cli_output_parses_into_text_summed_tokens_and_seconds():
    assert backends.parse_cli_output(json.dumps(CLI_RESULT), elapsed=99.0) == Completion(
        text='{"task_class": "coding", "candidates": [], "counter_links": []}', tokens=18482, seconds=8.421
    )
    with pytest.raises(BackendError, match="OAuth session expired"):
        backends.parse_cli_output(json.dumps({**CLI_RESULT, "is_error": True, "result": "OAuth session expired"}), 1.0)


def _fake_claude(tmp_path, monkeypatch, script_tail):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/argv"\n'
        f'printf "%s" "$RKA_MEMORY_DISABLE" > "{tmp_path}/env"\n'
        f'cat > "{tmp_path}/stdin"\n' + script_tail
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_claude_cli_backend_runs_claude_with_prompt_on_stdin_and_hooks_disabled(memory, tmp_path, monkeypatch):
    (tmp_path / "response.json").write_text(json.dumps(CLI_RESULT))
    _fake_claude(tmp_path, monkeypatch, f'cat "{tmp_path}/response.json"\n')

    completion = backends.claude_cli(memory.cfg)("PROMPT TEXT")

    assert completion.tokens == 18482
    assert (tmp_path / "argv").read_text().splitlines() == [
        "-p", "--model", "haiku", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
    ]
    assert (tmp_path / "env").read_text() == "1"
    assert (tmp_path / "stdin").read_text() == "PROMPT TEXT"


def test_claude_cli_failure_raises_backend_error(memory, tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, 'echo "OAuth session expired" >&2\nexit 1\n')
    with pytest.raises(BackendError, match="exited 1: OAuth session expired"):
        backends.claude_cli(memory.cfg)("PROMPT TEXT")


def test_support_link_adds_a_later_sessions_evidence_to_an_existing_record_and_promotes_it(memory, monkeypatch):
    monkeypatch.setenv("RKA_MEMORY_NOW", "2026-11-02T09:00:00+00:00")
    lg = Ledger()
    coding = dict(STATE, task_class="coding")
    lg.start("s1", **coding)
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_tests", scope=Scope.exact(State(**coding)), content="Run pytest -q before finishing")
    lg.link("r_tests", ok1)
    lg.end("s1")
    lg.start("s2", **coding)
    ok2 = lg.test("s2", True)
    lg.end("s2")
    sync(memory, lg)
    memory.gate()
    assert memory.world().records["r_tests"].rung.value == "L1"

    reply = {"candidates": [], "support_links": [{"record_id": "r_tests", "event_id": ok2.id, "reason": "tests passed again"}]}
    backend = FakeBackend(reply)
    result = mine_session(memory, "s2", backend=backend)

    assert "- r_tests [L1 preference] Run pytest -q before finishing" in backend.prompts[0]
    assert result.links == [{"record_id": "r_tests", "event_id": ok2.id, "polarity": "support", "reason": "tests passed again"}]
    assert result.dropped == []
    assert [(m["record_id"], m["from"], m["to"]) for m in result.moves] == [("r_tests", "L1", "L2")]


def test_support_link_names_only_listed_records_and_cites_by_the_evidence_rules(memory, monkeypatch):
    from rka_memory.miner.miner import Drop

    monkeypatch.setenv("RKA_MEMORY_NOW", "2026-11-02T09:00:00+00:00")
    lg = Ledger()
    coding = dict(STATE, task_class="coding")
    lg.start("s1", **coding)
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_tests", scope=Scope.exact(State(**coding)))
    lg.link("r_tests", ok1)
    lg.propose("s1", "r_bare", scope=Scope.exact(State(**coding)))
    lg.propose("s1", "r_elsewhere", scope=Scope.exact(State(**dict(coding, project="/p/other"))))
    lg.link("r_elsewhere", lg.test("s1", True))
    lg.end("s1")
    lg.start("s2", **coding)
    ok2 = lg.test("s2", True)
    bad = lg.test("s2", False)
    call = lg.tool_call("s2", "Bash", "pytest -q")
    lg.end("s2")
    sync(memory, lg)
    memory.gate()

    links = [("r_unknown", ok2), ("r_bare", ok2), ("r_elsewhere", ok2), ("r_tests", ok1), ("r_tests", call), ("r_tests", bad)]
    reply = {"candidates": [], "support_links": [{"record_id": r, "event_id": e.id, "reason": "r"} for r, e in links]}
    result = mine_session(memory, "s2", backend=FakeBackend(reply))

    assert result.links == []
    assert result.dropped == [
        Drop("support_link 0 r_unknown", "record was not listed in the prompt"),
        Drop("support_link 1 r_bare", "record was not listed in the prompt"),
        Drop("support_link 2 r_elsewhere", "record was not listed in the prompt"),
        Drop(f"support_link 3 evidence {ok1.id}", "evidence outside mined session"),
        Drop(f"support_link 4 evidence {call.id}", "model-authored event never grounds"),
        Drop(f"support_link 5 evidence {bad.id}", "test_result cannot carry support"),
    ]


SUMMARY_RULE = "end every reply with a final line that starts with `Summary:`"


def stated_session(memory, lg, session, text):
    lg.start(session, **STATE)
    msg = lg.user_msg(session, text)
    lg.end(session)
    sync(memory, lg)
    return msg


def test_a_stated_rule_becomes_a_record_in_the_users_own_words_cited_to_the_message(memory):
    from rka_memory.miner.miner import Drop

    lg = Ledger()
    msg = stated_session(
        memory, lg, "s1", "Add multiply. House rule:\n  end every reply with a final line\nthat starts with `Summary:`."
    )
    rules = {
        "rules": [
            {"quote": SUMMARY_RULE, "kind": "preference", "restates": None},
            {"quote": "Always write docstrings in French", "kind": "preference"},
            {"quote": "House rule:", "kind": "constraint"},
            {"quote": "end every reply", "kind": "fact"},
        ]
    }
    miner_reply = {"candidates": [{"content": SUMMARY_RULE, "kind": "preference", "evidence": [msg.id]}]}
    backend = FakeBackend(miner_reply, rules=rules)
    result = mine_session(memory, "s1", backend=backend)

    (proposed,) = result.proposed
    rid = proposed["record_id"]
    assert proposed == {"record_id": rid, "kind": "preference", "content": SUMMARY_RULE, "evidence": [msg.id]}
    assert result.merged == [{"record_id": rid, "content": SUMMARY_RULE, "evidence": []}]
    assert result.dropped == [
        Drop("rule 3: end every reply", "a stated rule is a preference or a constraint, not a fact"),
        Drop("rule 1: Always write docstrings in French", "quote is not in any user message of the session"),
        Drop("rule 2: House rule:", "a quote holds 12 to 400 characters"),
    ]
    rec = memory.world().records[rid]
    assert rec.rung.value == "L1"
    assert rec.scope.describe() == "project=/p/app, user_context=default"
    assert f"{msg.id}: Add multiply. House rule: {SUMMARY_RULE}." in backend.rule_prompts[0]
    assert f"- {SUMMARY_RULE}\n" in backend.prompts[0]
    assert (result.tokens, result.seconds) == (1800, 3.0)
    assert [(e.kind, e.payload.get("tokens")) for e in appended(memory, result)][-1] == (Kind.COST, 1800)


def test_a_restated_rule_adds_evidence_to_the_record_it_names_and_nothing_else(memory):
    from rka_memory.miner.miner import Drop

    lg = Ledger()
    stated_session(memory, lg, "s1", f"Please {SUMMARY_RULE} from now on.")
    first = mine_session(memory, "s1", backend=FakeBackend({}, rules={"rules": [{"quote": SUMMARY_RULE, "kind": "preference"}]}))
    rid = first.proposed[0]["record_id"]

    again = stated_session(memory, lg, "s2", f"Remember: {SUMMARY_RULE}.")
    restated = [
        {"quote": SUMMARY_RULE, "kind": "preference", "restates": rid},
        {"quote": f"Remember: {SUMMARY_RULE}", "kind": "preference", "restates": "r_nope"},
    ]
    backend = FakeBackend({}, rules={"rules": restated})
    second = mine_session(memory, "s2", backend=backend)

    assert f"- {rid}: {SUMMARY_RULE}" in backend.rule_prompts[0]
    assert second.proposed == []
    assert second.links == [
        {"record_id": rid, "event_id": again.id, "polarity": "support", "reason": "the user stated this rule again"}
    ]
    assert second.dropped == [Drop("rule 1 r_nope", "restates a record that was not listed")]
    assert len(memory.world().records[rid].links) == 2


def test_rules_survive_a_miner_reply_that_does_not_parse(memory):
    lg = Ledger()
    stated_session(memory, lg, "s1", f"House rule: {SUMMARY_RULE}.")
    rules = {"rules": [{"quote": SUMMARY_RULE, "kind": "preference"}]}
    result = mine_session(memory, "s1", backend=FakeBackend("no JSON here", rules=rules))

    assert (result.status, result.detail) == ("parse_error", "miner output holds no JSON object")
    assert [p["content"] for p in result.proposed] == [SUMMARY_RULE]
    assert [r.rung.value for r in memory.world().records.values()] == ["L1"]


def test_an_extractor_reply_that_does_not_parse_leaves_the_miner_to_run(memory):
    from rka_memory.miner.miner import Drop

    lg = Ledger()
    stated_session(memory, lg, "s1", f"House rule: {SUMMARY_RULE}.")
    reply = {"candidates": [{"content": "Reply with a Summary line", "kind": "preference"}]}
    result = mine_session(memory, "s1", backend=FakeBackend(reply, rules="none to report"))

    assert result.status == "mined"
    assert result.dropped == [Drop("rule extractor", "extractor output holds no JSON object")]
    assert [p["content"] for p in result.proposed] == ["Reply with a Summary line"]


def test_the_extractor_is_not_asked_without_user_messages_or_when_switched_off(memory):
    from dataclasses import replace

    from rka_memory.service import Memory

    lg = Ledger()
    lg.start("s1", **STATE)
    lg.test("s1", True)
    lg.end("s1")
    stated_session(memory, lg, "s2", f"House rule: {SUMMARY_RULE}.")
    silent = FakeBackend({})
    mine_session(memory, "s1", backend=silent)
    off = FakeBackend({}, rules={"rules": [{"quote": SUMMARY_RULE, "kind": "preference"}]})
    result = mine_session(Memory.open(replace(memory.cfg, miner_extract_rules=False)), "s2", backend=off)

    assert (silent.rule_prompts, off.rule_prompts) == ([], [])
    assert (len(silent.prompts), len(off.prompts)) == (1, 1)
    assert result.proposed == []
