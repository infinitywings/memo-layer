"""Sequential trials under attested validation (docs/DESIGN_V3.md step 3,
RKA jrn_01M7R3MVT74RMM4P8CFH8VR7QQ)."""

import importlib.util
import math
from dataclasses import replace
from pathlib import Path

from ledger_builder import Ledger, attested
from rka_memory.config import OutcomeConfig
from rka_memory.domain import Actor, Event, Kind, RecordKind, State
from rka_memory.kernel import trials
from rka_memory.kernel.labels import label
from rka_memory.world import project

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
CHECK = {"checks": {"outcome": {"command": ["true"]}}}
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "e0d_sequential_rule.py"


def e0d():
    spec = importlib.util.spec_from_file_location("e0d", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def coin(lg: Ledger, sid: str, rid: str, arm: str, q: float = 0.5) -> None:
    lg._ev(Kind.TRIAL, Actor.RUNNER, sid, {"record_id": rid, "arm": arm, "q": q})


def on_trial(arms: list[tuple[str, bool, float]]) -> tuple[Ledger, str]:
    """A constraint whose support puts it on trial, then one session per
    (arm, the user's check passed, q)."""
    lg = Ledger()
    for sid in ("p1", "p2"):
        lg.start(sid, **UV)
        if sid == "p1":
            lg.propose(sid, "r_con", kind=RecordKind.CONSTRAINT, content="Register each new function in calc.OPERATIONS")
        lg.link("r_con", lg.test(sid, True))
        lg.end(sid)
    for i, (arm, passed, q) in enumerate(arms):
        sid = f"t{i}"
        lg.start(sid, **UV)
        coin(lg, sid, "r_con", arm, q)
        lg.check(sid, "outcome", passed)
        lg.end(sid)
    return lg, "r_con"


def trial_of(lg: Ledger, rid: str):
    return trials.evaluate_sequential(project(lg.events).records[rid], project(lg.events), attested(**CHECK))


def test_the_wealth_is_the_e0d_rule_on_the_same_sessions():
    """P1: what E0d simulated is what the kernel computes."""
    hold = 0.1 / 0.9
    lg, rid = on_trial([("treated", True, 0.5), ("withheld", True, 0.5), ("withheld", False, 0.1), ("treated", False, 0.5),
                        ("withheld", False, 0.5), ("withheld", True, 0.1)])
    # treated-good and withheld-bad are concordant; at q = 0.1 a withheld session's ratio is q / (1 - q)
    expected = e0d().ehold(p1=2, m1=2, p9=1, m9=1, r=hold)
    assert math.isclose(trial_of(lg, rid).wealth, expected, rel_tol=1e-12)


def test_seven_concordant_sessions_promote_and_six_do_not():
    six = [("treated", True, 0.5), ("withheld", False, 0.5)] * 3
    lg, rid = on_trial(six)
    assert (trial_of(lg, rid).status, round(trial_of(lg, rid).wealth, 1)) == ("open", 17.4)
    lg, rid = on_trial([*six, ("treated", True, 0.5)])
    t = trial_of(lg, rid)
    assert (t.status, round(t.wealth, 1), t.treated, t.withheld) == ("positive", 30.3, (4, 4), (3, 0))
    assert label(project(lg.events), attested(**CHECK), lg.clock).by_id[rid].rung.value == "L2"


def test_a_record_the_evidence_runs_against_goes_on_hold_and_never_closes():
    lg, rid = on_trial([("treated", False, 0.5), ("withheld", True, 0.5)] * 6)
    t = trial_of(lg, rid)
    assert (t.status, t.treated, t.withheld) == ("hold", (6, 0), (6, 6))
    assert t.wealth < 0.1
    assert label(project(lg.events), attested(**CHECK), lg.clock).by_id[rid].trial == "hold"


def test_no_number_of_sessions_without_an_effect_closes_a_trial():
    lg, rid = on_trial([("treated", True, 0.5), ("withheld", True, 0.5)] * 20)
    assert trial_of(lg, rid).status == "open"


def test_chinese_records_compare_as_english_ones_do():
    assert trials.tokens("新函数要注册") == frozenset({"新函", "函数", "数要", "要注", "注册"})
    assert trials.similarity("每个新函数都要在 calc.OPERATIONS 里注册", "每个新函数都要注册在 calc.OPERATIONS 里") >= 0.5
    assert trials.similarity("每个新函数都要在 calc.OPERATIONS 里注册", "测试用 python -m unittest 运行") < 0.5


def candidates_world():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a", kind=RecordKind.CONSTRAINT, content="Register each new calc.py function in calc.OPERATIONS")
    lg.propose("s1", "r_b", kind=RecordKind.FACT, content="Each new calc.py function must be registered in calc.OPERATIONS")
    lg.propose("s1", "r_c", kind=RecordKind.PROCEDURE, content="Run the tests with python -m unittest -q")
    lg.propose("s1", "r_d", kind=RecordKind.CONSTRAINT, content="Return None on invalid input", refines=("r_c",))
    lg.end("s1")
    return project(lg.events)


def test_alike_or_linked_records_share_a_cluster_and_others_do_not():
    keys = trials.clusters(candidates_world(), ["r_a", "r_b", "r_c", "r_d"], attested())
    assert keys == {"r_a": "r_a", "r_b": "r_a", "r_c": "r_c", "r_d": "r_c"}


def test_a_cluster_shares_its_coin_and_takes_its_lowest_q(memory):
    """P4, through the service: alike records get one arm per session, and a
    cluster with a member on hold is delivered at the lower q."""
    memory.cfg = replace(attested(**CHECK), ledger_dir=memory.cfg.ledger_dir, skills_dir=memory.cfg.skills_dir)
    lg = Ledger()
    for sid in ("p1", "p2"):
        lg.start(sid, **UV)
        if sid == "p1":
            lg.propose(sid, "r_a", kind=RecordKind.CONSTRAINT, content="Register each new calc.py function in calc.OPERATIONS")
            lg.propose(sid, "r_b", kind=RecordKind.FACT, content="Each new calc.py function must be registered in calc.OPERATIONS")
        for rid in ("r_a", "r_b"):
            lg.link(rid, lg.test(sid, True))
        lg.end(sid)
    for i in range(6):
        sid = f"h{i}"
        lg.start(sid, **UV)
        coin(lg, sid, "r_b", "treated" if i % 2 == 0 else "withheld")
        lg.check(sid, "outcome", i % 2 == 1)
        lg.end(sid)
    memory.append(*lg.events)
    world = memory.world()
    labels = label(world, memory.cfg, lg.clock)
    assert (labels.by_id["r_a"].trial, labels.by_id["r_b"].trial) == ("open", "hold")
    for n in range(12):
        sid = f"s{n}"
        memory.append(Event.new(Kind.SESSION_START, Actor.RUNNER, sid, {"state": State(**UV).to_json()}))
        memory._trial_arms(memory.world(), labels, State(**UV), sid)
    coins = [e for e in memory.world().trials if e.session_id.startswith("s")]
    by_session = {}
    for e in coins:
        by_session.setdefault(e.session_id, set()).add((e.payload["arm"], e.payload["q"]))
    assert all(len(v) == 1 for v in by_session.values())
    assert {q for v in by_session.values() for _, q in v} == {0.1}
    assert {e.payload.get("cluster") for e in coins if e.payload["record_id"] == "r_b"} == {"r_a"}
