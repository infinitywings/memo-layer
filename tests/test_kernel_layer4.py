"""Layer 4 of docs/DESIGN_V2.md, written before the code: identity classes
with paraphrases (4.4), artifact influence (4.3), check modification (8.1),
runner-owned checks (8.2, 7.2) and randomized trials (5.3) with the
parameters the PI approved (q = 0.5, m = 3, delta = 1/3).
"""

from ledger_builder import Ledger, cfg
from rka_memory.domain import RecordKind, State
from rka_memory.kernel.impact import impact
from rka_memory.kernel.labels import label
from rka_memory.kernel.trials import candidates, coin
from rka_memory.kernel.views import packet
from rka_memory.world import project

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")


def labels_of(lg: Ledger):
    return label(project(lg.events), cfg(), lg.clock)


def rr(lg: Ledger, rid: str) -> tuple[str, str]:
    lab = labels_of(lg).by_id[rid]
    return lab.rung.value, lab.reason


def supported(lg: Ledger, rid: str, sid: str, *, deliver: tuple[str, ...] = (), before=None) -> None:
    lg.start(sid, **UV)
    if deliver:
        lg.deliver(sid, *deliver)
    if before:
        before(sid)
    lg.link(rid, lg.test(sid, True))
    lg.end(sid)


# Identity (4.4)


def test_support_produced_while_a_paraphrase_was_in_context_is_not_independent():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a", content="the calc test suite runs with pytest -q")
    lg.propose("s1", "r_b", content="the calc test suite runs with pytest -q -x")
    lg.link("r_a", lg.test("s1", True))
    lg.end("s1")
    supported(lg, "r_a", "s2", deliver=("r_b",))
    supported(lg, "r_a", "s3", deliver=("r_b",))
    assert rr(lg, "r_a") == ("L1", "independent grounding below threshold")


# Artifact influence (4.3)


def written_while_in_context(lg: Ledger, rid: str, path: str = "/p/app/f.py") -> None:
    """r is delivered in s1, where the agent writes `path`."""
    lg.start("s1", **UV)
    lg.deliver("s1", rid)
    lg.write("s1", path)
    lg.end("s1")


def test_a_result_of_reading_a_file_written_while_the_record_was_in_context_is_not_independent():
    lg = Ledger()
    lg.start("s0", **UV)
    lg.propose("s0", "r_a", content="f.py holds the parser")
    lg.link("r_a", lg.test("s0", True))
    lg.end("s0")
    written_while_in_context(lg, "r_a")
    lg.start("s2", **UV)
    lg.link("r_a", lg.read("s2", "/p/app/f.py"))
    lg.end("s2")
    lab = labels_of(lg).by_id["r_a"]
    assert lab.rung.value == "L1"
    assert ("not independent: the record or a dependent was in context") in [why for _, why in lab.set_aside]


def test_a_test_result_inherits_the_last_writers_of_the_files_the_record_is_about():
    lg = Ledger()
    lg.start("s0", **UV)
    lg.propose("s0", "r_a", content="f.py parses dates", about=("file:f.py",))
    lg.link("r_a", lg.test("s0", True))
    lg.end("s0")
    written_while_in_context(lg, "r_a")
    supported(lg, "r_a", "s2")
    assert labels_of(lg).by_id["r_a"].rung.value == "L1"
    supported(lg, "r_a", "s3")
    assert labels_of(lg).by_id["r_a"].rung.value == "L1"


def laundered(lg: Ledger) -> None:
    """The agent decides in s1 and writes cfg.py there. The decision is never
    delivered. In s2 and s3 the agent reads cfg.py, and a fact learned from
    those reads is supported only by them."""
    lg.start("s1", **UV)
    lg.decide("s1", "d_cfg", "Keep settings in cfg.py", alternatives=("env vars",), reason="one place")
    lg.write("s1", "/p/app/cfg.py")
    lg.end("s1")
    lg.start("s2", **UV)
    read2 = lg.read("s2", "/p/app/cfg.py")
    lg.propose("s2", "r_fact", content="cfg.py defines DEBUG")
    lg.link("r_fact", read2)
    lg.link("r_fact", lg.test("s2", True))
    lg.end("s2")
    lg.start("s3", **UV)
    lg.link("r_fact", lg.read("s3", "/p/app/cfg.py"))
    lg.link("r_fact", lg.test("s3", True))
    lg.end("s3")


def test_a_fact_laundered_through_a_file_is_withheld_when_the_decision_is_withdrawn():
    lg = Ledger()
    laundered(lg)
    assert labels_of(lg).by_id["r_fact"].rung.value == "L2"
    report = impact(project(lg.events), cfg(), lg.clock, "d_cfg")
    assert report["withheld"] == [{"record_id": "r_fact", "from": "L2", "to": "L1", "learned_under": ["d_cfg"]}]
    assert report["files_written_while_in_force"] == ["/p/app/cfg.py"]
    lg.reject("cli", "d_cfg")
    lab = labels_of(lg).by_id["r_fact"]
    assert (lab.rung.value, lab.learned_under) == ("L1", ("d_cfg",))


# Check modification (8.1)


def test_a_test_result_from_a_session_that_changed_its_tests_is_weak():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a", content="calc.divide raises on zero")
    lg.write("s1", "/p/app/test_calc.py")
    lg.link("r_a", lg.test("s1", True))
    lg.end("s1")
    lg.start("s2", **UV)
    lg.check("s2", "modification", passed=False, paths=["/p/app/tests/conftest.py"])
    lg.link("r_a", lg.test("s2", True))
    lg.end("s2")
    assert rr(lg, "r_a") == ("L1", "independent grounding below threshold")
    supported(lg, "r_a", "s3", before=lambda s: lg.check(s, "modification", passed=True, paths=[]))
    supported(lg, "r_a", "s4")
    assert rr(lg, "r_a")[0] == "L2"


# Runner-owned checks (8.2, 7.2)


def test_a_passing_citation_is_no_evidence_and_a_failing_one_withdraws_the_record():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a", content="helpers.clamp keeps a value between bounds")
    lg.link("r_a", lg.test("s1", True))
    lg.propose("s1", "r_dep", content="calc.py imports helpers.clamp", depends_on=("r_a",))
    lg.link("r_dep", lg.test("s1", True))
    lg.end("s1")
    lg.check("s2", "citation", record_id="r_a", passed=True, paths=["/p/app/helpers.py"])
    assert rr(lg, "r_a") == ("L1", "independent grounding below threshold")
    lg.check("s3", "citation", record_id="r_a", passed=False, paths=["/p/app/helpers.py"])
    assert rr(lg, "r_a") == ("L4", "a runner-owned check failed: citation")
    assert rr(lg, "r_dep") == ("L4", "depends on a record a failed check withdrew")


def test_a_citation_the_model_chose_never_validates_a_claim_however_often_it_passes():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_claim", kind=RecordKind.CONSTRAINT, content="Register every function in OPERATIONS")
    lg.link("r_claim", lg.tool_result("s1"))
    lg.end("s1")
    for i in range(2, 7):
        lg.check(f"s{i}", "citation", record_id="r_claim", passed=True, paths=["/p/app/calc.py"])
    assert rr(lg, "r_claim") == ("L1", "prescriptive: needs the user's affirmation or a trial")
    assert candidates(project(lg.events), labels_of(lg), cfg()) == []


# Randomized trials (5.3)

PROC = dict(kind=RecordKind.PROCEDURE, content="Run uv sync before the tests", steps=(("Bash", "uv sync"),), acceptance_check="uv run pytest")


def proposed_with_support(lg: Ledger, sessions: int = 2, weak: bool = False) -> None:
    for i in range(sessions):
        sid = f"p{i}"
        lg.start(sid, **UV)
        if i == 0:
            lg.propose(sid, "r_proc", **PROC)
        ev = lg.user_msg(sid, "sync first") if weak else lg.test(sid, True)
        lg.link("r_proc", ev)
        lg.end(sid)


def trial_session(lg: Ledger, sid: str, arm: str, passed: bool) -> None:
    lg.start(sid, **UV)
    lg.assign(sid, "r_proc", arm)
    if arm == "treated":
        lg.deliver(sid, "r_proc")
    lg.test(sid, passed, "uv run pytest")
    lg.end(sid)


def test_a_prescriptive_record_enters_a_trial_only_once_its_support_meets_the_threshold():
    lg = Ledger()
    proposed_with_support(lg, sessions=1)
    assert candidates(project(lg.events), labels_of(lg), cfg()) == []
    lg = Ledger()
    proposed_with_support(lg, sessions=3, weak=True)
    assert candidates(project(lg.events), labels_of(lg), cfg()) == []
    lg = Ledger()
    proposed_with_support(lg)
    assert candidates(project(lg.events), labels_of(lg), cfg()) == ["r_proc"]
    assert rr(lg, "r_proc") == ("L1", "in trial: treated 0, withheld 0 sessions")


def test_a_positive_contrast_promotes_and_records_when_it_completed():
    lg = Ledger()
    proposed_with_support(lg)
    for i, (arm, ok) in enumerate([("treated", True), ("withheld", False), ("treated", True), ("withheld", True), ("treated", True)]):
        trial_session(lg, f"t{i}", arm, ok)
    assert rr(lg, "r_proc") == ("L1", "in trial: treated 3, withheld 2 sessions")
    trial_session(lg, "t5", "withheld", False)
    lab = labels_of(lg).by_id["r_proc"]
    assert (lab.rung.value, lab.reason) == ("L2", "positive interventional contrast: treated 3/3, withheld 1/3")
    assert lab.since == max(i for i, e in enumerate(lg.events) if e.kind.value == "session_end")
    assert candidates(project(lg.events), labels_of(lg), cfg()) == []


def test_a_trial_without_a_positive_contrast_closes_and_never_reopens():
    lg = Ledger()
    proposed_with_support(lg)
    for i, arm in enumerate(["treated", "withheld"] * 3):
        trial_session(lg, f"t{i}", arm, True)
    assert rr(lg, "r_proc") == ("L1", "trial closed without a positive contrast: treated 3/3, withheld 3/3")
    assert candidates(project(lg.events), labels_of(lg), cfg()) == []


def test_a_session_that_changed_its_tests_counts_in_neither_arm():
    lg = Ledger()
    proposed_with_support(lg)
    for i, arm in enumerate(["treated", "withheld"] * 3):
        sid = f"t{i}"
        lg.start(sid, **UV)
        lg.assign(sid, "r_proc", arm)
        if sid == "t0":
            lg.write(sid, "/p/app/test_calc.py")
        lg.test(sid, arm == "treated", "uv run pytest")
        lg.end(sid)
    assert rr(lg, "r_proc") == ("L1", "in trial: treated 2, withheld 3 sessions")


def test_a_rules_outcome_is_whether_the_user_corrected_the_session():
    lg = Ledger()
    for i in range(2):
        lg.start(f"p{i}", **UV)
        if i == 0:
            lg.propose("p0", "r_rule", kind=RecordKind.CONSTRAINT, content="Keep functions short")
        lg.link("r_rule", lg.test(f"p{i}", True))
        lg.end(f"p{i}")
    for i, arm in enumerate(["treated", "withheld"] * 3):
        sid = f"t{i}"
        lg.start(sid, **UV)
        lg.assign(sid, "r_rule", arm)
        lg.end(sid)
        if arm == "withheld":
            lg.start(f"{sid}u", **UV)
            lg.undo(f"{sid}u", sid)
            lg.end(f"{sid}u")
    lab = labels_of(lg).by_id["r_rule"]
    assert (lab.rung.value, lab.reason) == ("L2", "positive interventional contrast: treated 3/3, withheld 0/3")


def test_the_coin_is_logged_and_only_treated_candidates_reach_the_packet_tail(memory):
    lg = Ledger()
    proposed_with_support(lg)
    memory.append(*lg.events)
    arms = {}
    for i in range(8):
        sid = f"live{i}"
        view = memory.deliver(sid, State(**UV), channel="session_start", arm="gated", now=lg.clock)
        (assigned,) = [e for e in memory.ledger.read() if e.kind.value == "trial" and e.session_id == sid]
        arms[sid] = assigned.payload["arm"]
        assert assigned.payload == {"record_id": "r_proc", "arm": arms[sid], "q": 0.5, "coin": coin(sid, "r_proc")}
        assert ("r_proc" in view.record_ids) == (arms[sid] == "treated")
        if arms[sid] == "treated":
            assert view.render().splitlines()[-2:] == ["### Being tested", "- [r_proc] Run uv sync before the tests"]
    assert set(arms.values()) == {"treated", "withheld"}


def test_a_planted_claim_citing_one_weak_event_never_enters_a_trial(memory):
    lg = Ledger()
    proposed_with_support(lg, sessions=1, weak=True)
    memory.append(*lg.events)
    for i in range(6):
        view = memory.deliver(f"live{i}", State(**UV), channel="session_start", arm="gated", now=lg.clock)
        assert view.record_ids == ()
    assert [e for e in memory.ledger.read() if e.kind.value == "trial"] == []
