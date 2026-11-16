"""Layer 4 of docs/DESIGN_V2.md, written before the code: identity classes
with paraphrases (4.4), artifact influence (4.3), check modification (8.1),
runner-owned checks (8.2, 7.2) and randomized trials (5.3) with the
parameters the PI approved (q = 0.5, m = 3; a one-sided Fisher test at
alpha = 0.05 replaced the margin delta = 1/3 after the trial-ceiling study).
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


def test_a_session_whose_check_found_only_added_tests_keeps_strong_results():
    """The check decides when it was run: writing to a test file is an
    addition unless the check found existing test content changed. Without a
    check (ledgers from before it existed) a write to a test file counts."""
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a", content="calc.divide raises on zero")
    lg.write("s1", "/p/app/test_calc.py")
    lg.check("s1", "modification", passed=True, paths=[])
    lg.link("r_a", lg.test("s1", True))
    lg.end("s1")
    lg.start("s2", **UV)
    lg.write("s2", "/p/app/test_calc.py")
    lg.check("s2", "modification", passed=True, paths=[])
    lg.link("r_a", lg.test("s2", True))
    lg.end("s2")
    assert rr(lg, "r_a") == ("L2", "independent grounding in enough distinct sessions")


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


def trial_session(lg: Ledger, sid: str, arm: str, passed: bool, *, own_tests: bool = True, due_only: bool = False) -> None:
    """One session of r_proc's trial. Its outcome is the user's outcome check
    (`passed`); the session's own test run (`own_tests`) is logged too, and
    decides nothing."""
    lg.start(sid, **UV)
    lg.assign(sid, "r_proc", arm)
    if arm == "treated":
        lg.deliver(sid, "r_proc")
    lg.test(sid, own_tests, "uv run pytest")
    lg.outcome_due(sid)
    if not due_only:
        lg.check(sid, "outcome", passed)
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


def test_a_complete_split_promotes_and_records_when_it_completed():
    lg = Ledger()
    proposed_with_support(lg)
    for i, (arm, ok) in enumerate([("treated", True), ("withheld", False), ("treated", True), ("withheld", False), ("treated", True)]):
        trial_session(lg, f"t{i}", arm, ok)
    assert rr(lg, "r_proc") == ("L1", "in trial: treated 3, withheld 2 sessions")
    trial_session(lg, "t5", "withheld", False)
    lab = labels_of(lg).by_id["r_proc"]
    assert (lab.rung.value, lab.reason) == ("L2", "positive interventional contrast: treated 3/3, withheld 0/3, p = 0.050")
    assert lab.since == max(i for i, e in enumerate(lg.events) if e.kind.value == "session_end")
    assert candidates(project(lg.events), labels_of(lg), cfg()) == []


def test_a_margin_the_old_rule_promoted_is_not_significant():
    """Treated 3/3 against withheld 1/3 beat the old margin of 1/3; one-sided
    Fisher p is 0.2."""
    lg = Ledger()
    proposed_with_support(lg)
    for i, (arm, ok) in enumerate([("treated", True), ("withheld", True), ("treated", True), ("withheld", False), ("treated", True), ("withheld", False)]):
        trial_session(lg, f"t{i}", arm, ok)
    assert rr(lg, "r_proc") == ("L1", "trial closed without a positive contrast: treated 3/3, withheld 1/3, p = 0.200")


def test_a_trial_without_a_positive_contrast_closes_and_never_reopens():
    lg = Ledger()
    proposed_with_support(lg)
    for i, arm in enumerate(["treated", "withheld"] * 3):
        trial_session(lg, f"t{i}", arm, True)
    assert rr(lg, "r_proc") == ("L1", "trial closed without a positive contrast: treated 3/3, withheld 3/3, p = 1.000")
    assert candidates(project(lg.events), labels_of(lg), cfg()) == []


def test_the_sessions_own_tests_do_not_decide_a_trial():
    """Treated sessions failed their own tests and passed the user's check;
    withheld ones passed their tests and failed the check. The old outcome
    (the last test result) would have scored this the other way round."""
    lg = Ledger()
    proposed_with_support(lg)
    for i, arm in enumerate(["treated", "withheld"] * 3):
        treated = arm == "treated"
        trial_session(lg, f"t{i}", arm, treated, own_tests=not treated)
    assert rr(lg, "r_proc") == ("L2", "positive interventional contrast: treated 3/3, withheld 0/3, p = 0.050")


def test_a_session_that_changed_its_tests_counts_against_promotion_in_either_arm():
    """t0 (treated) edited the tests: its passing check counts as a failure.
    Counted as observed, the trial would be positive (treated 3/3, withheld
    0/3)."""
    lg = Ledger()
    proposed_with_support(lg)
    for i, arm in enumerate(["treated", "withheld"] * 3):
        sid = f"t{i}"
        lg.start(sid, **UV)
        lg.assign(sid, "r_proc", arm)
        if sid == "t0":
            lg.write(sid, "/p/app/test_calc.py")
        lg.check(sid, "outcome", arm == "treated")
        lg.end(sid)
    assert rr(lg, "r_proc") == ("L1", "trial closed without a positive contrast: treated 2/3, withheld 0/3, p = 0.200")


def test_a_session_whose_outcome_check_never_logged_a_result_counts_against_promotion():
    lg = Ledger()
    proposed_with_support(lg)
    for i, arm in enumerate(["treated", "withheld"] * 3):
        trial_session(lg, f"t{i}", arm, arm == "treated", due_only=i == 0)
    assert rr(lg, "r_proc") == ("L1", "trial closed without a positive contrast: treated 2/3, withheld 0/3, p = 0.200")


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
    assert (lab.rung.value, lab.reason) == ("L2", "positive interventional contrast: treated 3/3, withheld 0/3, p = 0.050")


def test_a_failing_outcome_check_is_a_bad_outcome_for_a_rule_too():
    """Uncorrected sessions whose outcome check failed are bad outcomes for
    every prescriptive kind, not only for procedures."""
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
        lg.check(sid, "outcome", arm == "treated")
        lg.end(sid)
    lab = labels_of(lg).by_id["r_rule"]
    assert (lab.rung.value, lab.reason) == ("L2", "positive interventional contrast: treated 3/3, withheld 0/3, p = 0.050")


# Counters and the outcome check (5.1, 5.3)

OPS = dict(kind=RecordKind.CONSTRAINT, content="calc.py must keep the OPERATIONS table in step with its functions")
CHECK_SAYS = "AssertionError: OPERATIONS is missing 'power': every function needs an OPERATIONS entry"


def fact_at_l2(lg: Ledger) -> None:
    lg.start("s0", **UV)
    lg.propose("s0", "r_fact", content="pytest -q runs the tests")
    lg.end("s0")
    supported(lg, "r_fact", "s1")
    supported(lg, "r_fact", "s2")


def test_a_passing_outcome_check_never_counts_against_a_record():
    """Its output shows that the user's requirement held. A failing check's
    output still counts against a record that was followed."""
    for code, expected in ((0, ("L2", "independent grounding in enough distinct sessions")),
                           (1, ("L1", "counter-evidence after the last affirmation"))):
        lg = Ledger()
        fact_at_l2(lg)
        lg.start("s3", **UV)
        lg.deliver("s3", "r_fact")
        lg.link("r_fact", lg.outcome_output("s3", code, "1 passed" if code == 0 else "1 failed"), "counter")
        lg.end("s3")
        assert rr(lg, "r_fact") == expected


def test_a_failing_outcome_check_that_states_the_requirement_supports_it():
    """The unseen-requirement run: the user's check printed the requirement
    the repository does not show, and that output is the rule's support. A
    tool result weighs a third, so it takes six sessions, as in that run."""
    lg = Ledger()
    for sid in [f"p{i}" for i in range(6)]:
        lg.start(sid, **UV)
        if sid == "p0":
            lg.propose(sid, "r_ops", **OPS)
        lg.link("r_ops", lg.outcome_output(sid, 1, CHECK_SAYS))
        lg.end(sid)
    assert rr(lg, "r_ops") == ("L1", "in trial: treated 0, withheld 0 sessions")


def test_the_miners_reading_of_a_trial_session_does_not_decide_the_record():
    """A treated session followed the rule and passed the user's check, then
    the miner cited the agent's empty search as a counter. The trial judges
    its own sessions by their outcomes; the same counter from a session
    outside the trial still blocks the record."""
    lg = Ledger()
    proposed_with_support(lg)
    lg.start("t0", **UV)
    lg.assign("t0", "r_proc", "treated")
    lg.deliver("t0", "r_proc")
    lg.link("r_proc", lg.tool_result("t0"), "counter")
    lg.check("t0", "outcome", True)
    lg.end("t0")
    assert rr(lg, "r_proc") == ("L1", "in trial: treated 1, withheld 0 sessions")
    lg.start("o0", **UV)
    lg.link("r_proc", lg.tool_result("o0"), "counter")
    lg.end("o0")
    assert rr(lg, "r_proc") == ("L1", "counter-evidence after the last affirmation")


def test_miner_support_from_a_trial_session_adds_nothing():
    lg = Ledger()
    proposed_with_support(lg)
    before = labels_of(lg).by_id["r_proc"].support
    lg.start("t0", **UV)
    lg.assign("t0", "r_proc", "treated")
    lg.deliver("t0", "r_proc")
    lg.link("r_proc", lg.test("t0", True))
    lg.end("t0")
    assert len(before) == 2
    assert labels_of(lg).by_id["r_proc"].support == before


def test_the_users_checked_words_in_a_trial_session_still_count():
    """The user's words are the user's act, not the miner's reading of the
    session: said against the rule they block it, stated as a standing rule
    they affirm it."""
    for text, quote, polarity, expected in (
        ("Do not run uv sync before the tests", {"quote": "Do not run uv sync before the tests"}, "counter",
         ("L1", "counter-evidence after the last affirmation")),
        ("Run uv sync before the tests, from now on", {"quote": "Run uv sync before the tests", "marker": "from now on"},
         "support", ("L2", "the user stated it as a standing rule")),
    ):
        lg = Ledger()
        proposed_with_support(lg)
        lg.start("t0", **UV)
        lg.assign("t0", "r_proc", "treated")
        lg.deliver("t0", "r_proc")
        lg.link("r_proc", lg.user_msg("t0", text), polarity, statement=quote)
        lg.end("t0")
        assert rr(lg, "r_proc") == expected


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


def test_a_write_to_test_configuration_makes_the_session_weak_even_if_reverted():
    """The end-of-session check saw no change, but the agent wrote
    conftest.py during the session: its results while the file was
    different cannot be trusted."""
    lg = Ledger()
    for sid in ("s1", "s2"):
        lg.start(sid, **UV)
        if sid == "s1":
            lg.propose(sid, "r_a", content="calc.divide raises on zero")
        lg.write(sid, "/p/app/conftest.py")
        lg.check(sid, "modification", passed=True, paths=[])
        lg.link("r_a", lg.test(sid, True))
        lg.end(sid)
    assert rr(lg, "r_a") == ("L1", "independent grounding below threshold")
