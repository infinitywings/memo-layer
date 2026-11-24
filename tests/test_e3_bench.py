"""scripts/e3_bench.py with a scripted agent, judge, miner and curator:
the turn mapping, one ledger per item, what each arm receives, and the
released judge's rules (docs/E3.md)."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from rka_memory import notepad
from rka_memory.domain import Kind
from rka_memory.miner.backends import Completion

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
spec = importlib.util.spec_from_file_location("e3_bench", REPO / "scripts" / "e3_bench.py")
e3 = sys.modules["e3_bench"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e3)
plan_spec = importlib.util.spec_from_file_location("openai_plan", REPO / "scripts" / "openai_plan.py")
openai_plan = sys.modules["openai_plan"] = importlib.util.module_from_spec(plan_spec)
plan_spec.loader.exec_module(openai_plan)

LESSON = "DeepAnalyzer times out on detailed analysis requests; use QuickCheck instead"
T = e3.Turn


def conditioning(task_id="001"):
    learning = (
        T("user", "I need a detailed analysis of our server performance."),
        T("assistant", "I'll run a detailed analysis using DeepAnalyzer..."),
        T("system", "Error: Timeout Error. DeepAnalyzer failed due to computational complexity."),
        T("user", "Can you check system health?"),
        T("assistant", "Health looks fine."),
    )
    ctx = e3.Context(learning, (T("user", "What's the weather?"), T("assistant", "I can't see weather.")),
                     "I need a detailed analysis of our network traffic.")
    raw = {"task_name": "Conditioned API Aversion", "learning_phase": [{"role": t.role.title(), "content": t.content} for t in learning],
           "test_probe": {"role": "User", "content": ctx.probe}}
    return e3.Item("classical_conditioning", "dataset/classical_conditioning/conditioned_api_aversion.json",
                   "Conditioned API Aversion", task_id, {"main": ctx}, expected="avoid DeepAnalyzer", raw=raw)


def scripted_miner(prompt: str) -> Completion:
    """Extractor: no rules. Miner: one constraint citing the tool result."""
    if prompt.startswith("You are the rule extractor"):
        return Completion('{"rules": [], "withdrawn": []}', 10, 0.0)
    if "notepad" in prompt.lower() and "candidates" not in prompt:
        return Completion("- The user wants short answers.\n- " + LESSON, 10, 0.0)
    ids = re.findall(r"^(ev_\S+) tool_result env", prompt, re.MULTILINE)
    doc = {"candidates": [{"content": LESSON, "kind": "constraint", "evidence": ids[:1], "confidence": 0.8}]}
    return Completion(json.dumps(doc), 10, 0.0)


class Agent:
    def __init__(self):
        self.calls = []

    def __call__(self, messages, model, effort):
        self.calls.append((model, effort, messages))
        if model == "judge":
            text = messages[-1]["content"]
            correct = "QuickCheck" in text.split('"model_answer_to_evaluate"')[1][:400] if "model_answer_to_evaluate" in text else False
            return json.dumps({"reasoning": "r", "decision": "Correct" if correct else "Incorrect"}), {"input": 5, "output": 1}
        sees = " ".join(m["content"] for m in messages)
        return ("I'll use QuickCheck." if "QuickCheck" in sees or "Timeout" in sees else "Running DeepAnalyzer."), {"input": 3, "output": 2}


def run(tmp_path, agent):
    return e3.Run(tmp_path, "agent-model", "medium", "judge", "http://unused", agent, mine_backend=scripted_miner)


@pytest.fixture(autouse=True)
def no_host_env(monkeypatch):
    monkeypatch.delenv("RKA_MEMORY_DIR", raising=False)
    monkeypatch.delenv("RKA_MEMORY_MINER_BASE_URL", raising=False)


def test_turns_map_onto_user_tool_and_assistant_events():
    kinds = [(e.kind, e.actor.value) for e in e3.events_for(conditioning().contexts["main"].learning, "s1")]
    assert kinds == [
        (Kind.USER_MSG, "user"), (Kind.TOOL_CALL, "model"), (Kind.TOOL_RESULT, "env"),
        (Kind.USER_MSG, "user"), (Kind.ASSISTANT_MSG, "model"),
    ]


def test_each_arm_gets_what_section_4_says(tmp_path):
    agent = Agent()
    doc = run(tmp_path, agent).run_item(conditioning())
    arms = doc["contexts"]["main"]["arms"]
    rec = doc["contexts"]["main"]["records"]
    assert [(r["content"], r["rung"]) for r in rec] == [(LESSON, "L1")]
    assert (arms["gated"]["packet"], arms["gated"]["reused_none"], arms["gated"]["answer"]) == ("", True, arms["none"]["answer"])
    assert LESSON in arms["sourced"]["packet"] and LESSON in arms["ungated"]["packet"]
    assert arms["sourced"]["packet"] != arms["ungated"]["packet"]
    assert LESSON in arms["notepad"]["packet"]
    assert arms["none"]["answer"] == "Running DeepAnalyzer."
    assert {a: doc["verdicts"][a]["correct"] for a in e3.ARMS} == {
        "in-context": True, "none": False, "gated": False, "sourced": True, "ungated": True, "notepad": True}
    agent_models = {(m, eff) for m, eff, _ in agent.calls if m != "judge"}
    assert agent_models == {("agent-model", "medium")}
    assert sum(m != "judge" for m, _, _ in agent.calls) == 5  # gated reuses none's answer


def test_items_never_share_a_ledger_and_reruns_reuse_results(tmp_path):
    r = run(tmp_path, Agent())
    a, b = conditioning("001"), conditioning("002")
    r.run_item(a)
    r.run_item(b)
    for item in (a, b):
        ledger = tmp_path / "ledgers" / item.family / item.key / "main" / "rka" / "ledger.jsonl"
        sessions = {json.loads(line)["session_id"] for line in ledger.read_text().splitlines()}
        assert all(s.startswith(item.key) or not s.startswith("conditioned") for s in sessions)
        assert not any(s.startswith((b if item is a else a).key) for s in sessions)
    calls = Agent()
    r.complete = calls
    r.run_item(a)
    assert calls.calls == []


def test_no_skill_file_or_core_leaves_the_run(tmp_path):
    cfg = e3.memory_config(tmp_path / "l", model="m", base_url="http://x", notepad_on=True)
    assert (cfg.skills_export_files, cfg.skills_dir, cfg.ledger_dir) == (False, tmp_path / "l" / "skills", tmp_path / "l")
    assert cfg.core_url == "http://127.0.0.1:9" and cfg.notepad.assistant_turns


def test_the_notepad_reads_assistant_turns_only_with_the_flag(tmp_path):
    item = conditioning()
    on = e3.learn(tmp_path / "on", item, "main", model="m", base_url="http://x", notepad_on=True, mine_backend=scripted_miner)
    world = on.memory.world()
    sid = f"{item.key}-main-s1"
    assert "Health looks fine." in notepad.digest(world, sid, on.memory.cfg)
    off = e3.memory_config(tmp_path / "off", model="m", base_url="http://x", notepad_on=False)
    assert "Health looks fine." not in notepad.digest(world, sid, off)


def test_the_released_rules_score_three_procedural_tasks_by_substring():
    item = e3.Item("procedural_memory", "f", "The 'Omega' Operation", "1", {}, expected={"a": "OMEGA(3)"})
    assert e3.verdict(item, "the answer is omega(3).", None, "judge")["correct"] is True
    assert e3.verdict(item, "OMEGA 3", None, "judge")["correct"] is False


def test_a_judge_reply_wrapped_in_prose_still_parses():
    assert e3.json_object('Sure:\n```json\n{"decision": "Correct"}\n```') == {"decision": "Correct"}
    assert e3.json_object("no json") == {}


def test_the_released_data_loads_whole():
    if not (e3.DATA_DIR / "tasks" / "priming.json").exists():
        pytest.skip("run scripts/e3_bench.py fetch")
    items = e3.load()
    count = {f: sum(i.family == f for i in items) for f in e3.FAMILIES}
    assert count == {"procedural_memory": 100, "classical_conditioning": 100, "priming": 100}
    assert {tuple(i.contexts) for i in items if i.family == "priming"} == {("experimental", "control")}
    assert len(e3.pilot(items)) == 33


def test_the_proxy_keeps_a_late_system_turn_in_place_and_passes_reasoning():
    body = {"model": "m", "reasoning_effort": "medium", "messages": [
        {"role": "system", "content": "packet"}, {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"}, {"role": "system", "content": "Error"}, {"role": "user", "content": "p"}]}
    out = openai_plan.chat_to_responses(body)
    assert out["instructions"] == "packet" and out["reasoning"] == {"effort": "medium"}
    assert [i["role"] for i in out["input"]] == ["user", "assistant", "system", "user"]
    assert "reasoning" not in openai_plan.chat_to_responses({"model": "m", "messages": [{"role": "user", "content": "x"}]})


an_spec = importlib.util.spec_from_file_location("e3_analysis", REPO / "scripts" / "e3_analysis.py")
e3a = sys.modules["e3_analysis"] = importlib.util.module_from_spec(an_spec)
an_spec.loader.exec_module(e3a)


def test_the_paired_tests_match_hand_computed_values():
    assert e3a.mcnemar([True] * 6 + [False] * 4, [False] * 6 + [False] * 4) == (6, 0, 0.03125)
    assert e3a.mcnemar([True, False], [True, False]) == (0, 0, 1.0)
    # differences 1, 2, 3, -4: W- = 4; 7 of the 16 sign patterns give a sum of 4 or less
    assert e3a.wilcoxon([1, 2, 3, 0], [0, 0, 0, 4]) == 0.875


def test_the_analysis_reads_a_scripted_run(tmp_path, capsys):
    r = run(tmp_path, Agent())
    for n in ("001", "002"):
        r.run_item(conditioning(n))
    (tmp_path / "run.json").write_text(json.dumps({"model": "agent-model", "reasoning": "medium", "judge": "judge",
                                                   "manifest_commit": "927413bf"}))
    assert e3a.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "conditioning   2        100%          0%          0%        100%        100%        100%" in out
    assert "Q3 sourced and ungated +15 or more over none in conditioning -> holds" in out
    assert "conditioning: 2 sessions mined, 2 with a record; 2 records {'L1': 2}, kinds {'constraint': 2}" in out
    assert "packets not empty: {'gated': 0, 'sourced': 2, 'ungated': 2, 'notepad': 2}" in out
