"""scripts/e2b_analysis.py on synthetic runs: an E2b route run, concurrent
controls, and E2's runs rescored under the E2b check."""

import importlib.util
import json
import sys
from pathlib import Path

from ledger_builder import Ledger
from rka_memory.domain import Actor, Kind, RecordKind

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
spec = importlib.util.spec_from_file_location("e2b_analysis", REPO / "scripts" / "e2b_analysis.py")
e2b = sys.modules["e2b_analysis"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2b)
BOOL = "Every calc function must raise TypeError when it is given a bool"


def arm_dir(root: Path, name: str, met_a, follows_f, *, trial: bool = False, omitted_at: int | None = None) -> Path:
    """Twenty sessions of stream `name`. T_X is met in every session; T_A in
    `met_a`; F_A followed in `follows_f`; the check passes when T_A is met
    and F_A is not followed. With `trial`, the miner proposes the T_A record
    in s1 and from s2 its coin treats the even sessions."""
    d = root / name
    (d / "ledger").mkdir(parents=True)
    arm = name.rsplit("-", 1)[1]
    (d / "manifest.json").write_text(json.dumps({"stream": name, "config": {"packet": {"arms": ["gated" if arm == "route" else arm]}}}))
    (d / "config.yaml").write_text("checks: {outcome: {command: ['true']}}\ntrials: {entry: open}\n")
    lg, rows = Ledger(), []
    for n in range(1, 21):
        sid = f"{name}-{n}"
        lg.start(sid, **UV)
        if trial and n == 1:
            lg.propose(sid, "r_bool", kind=RecordKind.CONSTRAINT, content=BOOL)
            lg.link("r_bool", lg.tool_result(sid))
        if trial and n > 1:
            lg._ev(Kind.TRIAL, Actor.RUNNER, sid, {"record_id": "r_bool", "arm": "treated" if n % 2 == 0 else "withheld", "q": 0.5})
        lg._ev(Kind.PACKET_DELIVERED, Actor.RUNNER, sid, {"record_ids": [], "arm": "gated", "channel": "session_start",
                                                          "trial_omitted": ["r_bool"] if n == omitted_at else []})
        lg.check(sid, "outcome", n in met_a and n not in follows_f)
        lg.end(sid)
        rows.append({"session": f"s{n}", "session_id": sid,
                     "observed": {"T_X met": True, "T_A met": n in met_a, "F_A followed": n in follows_f, "U followed": True}})
    (d / "ledger" / "ledger.jsonl").write_text("".join(json.dumps(e.to_json()) + "\n" for e in lg.events))
    (d / "report.json").write_text(json.dumps({"sessions": rows}))
    return d


def runs(tmp_path, gated_now_a=()):
    even = range(2, 21, 2)
    e2b_seed = tmp_path / "e2b" / "seed-1"
    arm_dir(e2b_seed, "e2b-calc-route", met_a=even, follows_f=(5, 7), trial=True, omitted_at=9)
    arm_dir(e2b_seed, "e2b-calc-gated", met_a=gated_now_a, follows_f=())
    arm_dir(e2b_seed, "e2b-calc-notepad", met_a=range(1, 21), follows_f=range(1, 21))
    e2_seed = tmp_path / "e2" / "seed-1"
    arm_dir(e2_seed, "e2-calc-gated", met_a=(), follows_f=())
    arm_dir(e2_seed, "e2-calc-notepad", met_a=range(1, 21), follows_f=range(1, 21))
    return e2b.e2.load_runs([str(e2b_seed)]), e2b.e2.load_runs([str(e2_seed)])


def test_e2_runs_are_rescored_under_the_e2b_check_and_e2b_runs_by_their_check(tmp_path):
    new, old = runs(tmp_path)
    route = next(r for r in new if r.arm == "route")
    assert (route.experiment, route.fixture, route.arm) == ("e2b", "calc", "route")
    old_notepad = next(r for r in old if r.arm == "notepad")
    assert (e2b.e2b_pass(route), e2b.e2b_pass(old_notepad)) == (9 / 17, 0.0)


def test_the_route_runs_trial_and_the_predictions_read_from_it(tmp_path):
    new, old = runs(tmp_path)
    [t] = next(r for r in new if r.arm == "route").trial_info
    assert (t["proposed"], t["first_coin"], t["status"], t["by"]) == (1, 2, "positive", "model")
    lines = e2b.predictions(new, old)
    assert "drift calc gated: pass 0% vs E2 0%, harm 0% vs E2 0%, T_A 0% vs E2 0% -> within 25 points" in lines
    assert "Q1 miner proposed T_A in 1 of 1 route runs, F_A in 0 (predicted at least 5 of 6 each)" in lines
    assert "Q3 route meets T_A more than gated: 1 of 1 pairs, one-sided sign p = 0.500 (predicted 6 of 6)" in lines
    assert any(line.startswith(f"Q4 T_A promoted by s20 in 1 of 1 route runs") for line in lines)
    assert "Q8 treated lines omitted for budget in route runs: 1 (predicted 0)" in lines


def test_a_drifted_control_is_replaced_by_its_concurrent_run(tmp_path):
    new, old = runs(tmp_path, gated_now_a=range(1, 21))
    lines, drifted = e2b.drift(new, old)
    assert drifted == {"gated"} and any(line.endswith("-> DRIFT on pass") for line in lines)
    ctl = e2b.controls(new, old, drifted)
    assert (ctl[("calc", 1, "gated")].experiment, ctl[("calc", 1, "notepad")].experiment) == ("e2b", "e2")


def test_without_e2_runs_the_controls_are_the_concurrent_runs(tmp_path):
    new, _ = runs(tmp_path)
    ctl = e2b.controls(new, [], set())
    assert (ctl[("calc", 1, "gated")].experiment, ctl[("calc", 1, "notepad")].experiment) == ("e2b", "e2b")
