"""scripts/e2_analysis.py on a synthetic E2 run: one fixture, one seed, three arms."""

import importlib.util
import json
import sys
from pathlib import Path

from ledger_builder import Ledger
from rka_memory.domain import Actor, Kind

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("e2_analysis", REPO / "scripts" / "e2_analysis.py")
e2 = sys.modules["e2_analysis"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2)


def arm_dir(run: Path, arm: str, passes, follows_f, notes: str = "") -> Path:
    """Twenty sessions: the user's check passes in sessions `passes`, F_A is
    followed in `follows_f`, U always; the agent reads HANDOFF.md in s1."""
    d = run / f"e2-calc-{arm}"
    (d / "ledger").mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"stream": f"e2-calc-{arm}", "config": {"packet": {"arms": [arm]}}}))
    (d / "config.yaml").write_text(f"packet: {{arms: [{arm}]}}\n")
    lg, rows = Ledger(), []
    for n in range(1, 21):
        sid = f"{arm}-{n}"
        lg.start(sid, **UV)
        lg._ev(Kind.PACKET_DELIVERED, Actor.RUNNER, sid, {"record_ids": [], "arm": arm, "channel": "session_start", "tokens": 40 if n > 1 else 0})
        if n == 1:
            lg.tool_call(sid, "read", "HANDOFF.md")
        lg.check(sid, "outcome", n in passes)
        lg._ev(Kind.SESSION_END, Actor.RUNNER, sid, {"tokens_total": 1000, "tokens_input": 300, "tokens_cache_read": 600, "tokens_cache_write": 0, "tokens_output": 100})
        rows.append({"session": f"s{n}", "session_id": sid, "observed": {"U followed": True, "T_X met": n in passes, "T_A met": False, "F_A followed": n in follows_f}})
    (d / "ledger" / "ledger.jsonl").write_text("".join(json.dumps(e.to_json()) + "\n" for e in lg.events))
    (d / "report.json").write_text(json.dumps({"sessions": rows}))
    if notes:
        (d / "ledger" / "notepad").mkdir()
        (d / "ledger" / "notepad" / f"001-curate-{arm}-1.md").write_text(notes)
        (d / "ledger" / "notepad" / "log.jsonl").write_text(json.dumps({"session_id": f"{arm}-1", "step": "curate", "ok": True, "tokens": 700}) + "\n")
    return d


def test_one_seed_of_one_fixture_scores_every_arm_and_prediction(tmp_path):
    run = tmp_path / "20270101T000000Z"
    arm_dir(run, "stated", passes=(), follows_f=())
    arm_dir(run, "gated", passes=range(3, 21), follows_f=(5,))
    arm_dir(run, "notepad", passes=range(2, 5), follows_f=range(5, 21), notes="- Round float results to 2 decimals.\n- Register each function in calc.OPERATIONS.\n")
    runs = e2.load_runs([str(run)])
    assert [(r.fixture, r.seed, r.arm) for r in runs] == [("calc", 1, "stated"), ("calc", 1, "gated"), ("calc", 1, "notepad")]
    stated, gated, notepad = runs
    assert (stated.pass_rate, gated.pass_rate, round(notepad.pass_rate, 3)) == (0.0, 1.0, round(1 / 17, 3))
    assert (gated.follows("F_A"), notepad.follows("F_A")) == (1 / 17, 16 / 17)
    assert (notepad.holds("F_A", 1), notepad.holds("F_A", 2), notepad.holds("T_A", 4)) == (False, True, False)
    assert (notepad.curator_tokens, gated.cached_share(), sum(s.read_handoff for s in gated.sessions)) == (700, 600 / 900, 1)
    lines = e2.predictions(runs)
    assert lines[2] == "P3 gated passes more than stated: 1 of 1 pairs, one-sided sign p = 0.500 (predicted 6 of 6)"
    assert lines[4] == "P5 the notepad held at s4: U in 0 of 1, T_X in 1 of 1, T_A in 0 of 1, F_A in 1 of 1"
    assert lines[5] == "P6 gated harm below notepad's where the notepad kept F_A: 1 of 1 pairs, one-sided sign p = 0.500 (predicted all)"
    assert lines[7] == "P8 the notepad meets T_A more than gated where it kept T_A: 0 of 0 pairs, one-sided sign p = 1.000 (predicted all)"
    assert lines[9] == "P10 cached share, gated minus notepad: mean +0.0% over 1 pairs (no difference: drop advantage 4)"
    text = e2.render(runs, show_notes=True)
    assert "calc 1 gated | 100% | 6% | 100% | 100% | 0% | 1/3 | 40 | 67% | 20000/0/0" in text
    assert "  notes at s2:\n    - Round float results to 2 decimals." in text


def test_a_note_that_negates_an_item_does_not_hold_it(tmp_path):
    run = tmp_path / "20270101T000000Z"
    arm_dir(run, "notepad", passes=(), follows_f=(), notes="- Do not blanket-round float results; round only when asked.\n- Never coerce ints, and apply round(result, 2) to floats.\n")
    [notepad] = e2.load_runs([str(run)])
    notes = notepad.sessions[1].memory.splitlines()
    assert [e2.Run.holds(e2.Run("calc", "notepad", "", [e2.Session(2, "x", {}, memory=line)]), "F_A", 2) for line in notes] == [False, True]


def test_the_sign_test_counts_ties_against_the_prediction():
    assert (e2.sign_p(6, 6), round(e2.sign_p(5, 6), 3), e2.sign_p(0, 0)) == (1 / 64, 0.109, 1.0)
