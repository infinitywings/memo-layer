"""Attested validation (docs/DESIGN_V3.md Section 3) on hand-built ledgers,
each rule set against what v2's threshold validation does with the same log."""

from ledger_builder import Ledger, attested, cfg
from rka_memory.domain import Actor, Event, Integrity, Kind, RecordKind, State, integrity
from rka_memory.kernel.labels import label
from rka_memory.kernel.views import packet
from rka_memory.world import project

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
RULE = "run the suite with python -m unittest -q"
STANDING = {"quote": RULE, "marker": "always"}
CHECKED = "ops CLI check failed: the ops CLI offers only the functions registered in calc.OPERATIONS"
WITH_CHECK = {"checks": {"outcome": {"command": ["true"]}}}


def labels(lg: Ledger, c=None):
    return label(project(lg.events), c or attested(), lg.clock)


def rung(lg: Ledger, rid: str, c=None) -> str:
    return labels(lg, c).by_id[rid].rung.value


def test_integrity_is_read_from_the_event_never_from_a_judgment():
    lg = Ledger()
    lg.start("s1", **UV)
    assert integrity(lg.user_msg("s1", "add a mean function")) is Integrity.USER
    assert integrity(lg.confirm("s1", "r_a")) is Integrity.USER
    assert integrity(lg.outcome_output("s1", 1, CHECKED)) is Integrity.CHECK
    assert integrity(lg.test("s1", True)) is Integrity.AGENT
    assert integrity(lg.exit("s1", 0, "git status")) is Integrity.AGENT
    assert integrity(lg.tool_call("s1", "Bash", "ls")) is Integrity.AGENT
    assert integrity(Event.new(Kind.TOOL_RESULT, Actor.ENV, "s1", {"tool": "isolation re-run"})) is Integrity.AGENT


def steered_fact(lg: Ledger) -> None:
    """E0a A1: a fact whose only support is two `git status` exit codes the
    miner linked to it."""
    for sid in ("s1", "s2"):
        lg.start(sid, **UV)
        if sid == "s1":
            lg.propose(sid, "r_fact", content="Put new tests in test_extra.py")
        lg.link("r_fact", lg.exit(sid, 0, "git status"))
        lg.end(sid)


def test_model_linked_support_never_validates_whatever_its_weight():
    lg = Ledger()
    steered_fact(lg)
    assert rung(lg, "r_fact", cfg()) == "L2"
    lab = labels(lg).by_id["r_fact"]
    assert (lab.rung.value, lab.reason) == ("L1", "hypothesis: no outcome check is configured to test it")
    view = packet(project(lg.events), labels(lg), attested(), State(**UV), arm="gated", channel="session_start")
    assert view.record_ids == ()


def stated_rule(lg: Ledger, sid: str, text: str) -> None:
    lg.start(sid, **UV)
    msg = lg.user_msg(sid, text)
    lg.propose(sid, "r_rule", kind=RecordKind.PREFERENCE, content=RULE)
    lg.link("r_rule", msg, statement=STANDING)
    lg.end(sid)


def test_a_standing_statement_the_user_typed_validates():
    lg = Ledger()
    stated_rule(lg, "s1", f"Please always {RULE}, pytest is not installed.")
    lab = labels(lg).by_id["r_rule"]
    assert (lab.rung.value, lab.reason) == ("L2", "the user stated it as a standing rule")


def test_words_the_agent_wrote_earlier_do_not_become_the_users_by_being_pasted():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.tool_call("s1", "Write", f"ci.log: NOTE always {RULE.upper()} because pytest is not installed")
    lg.end("s1")
    stated_rule(lg, "s2", f"Here is the CI log:\nNOTE always {RULE} because pytest is not installed")
    assert rung(lg, "r_rule", cfg()) == "L2"
    assert rung(lg, "r_rule") != "L2"


def confirmed_then_countered(lg: Ledger, counter: Event) -> None:
    lg.start("s1", **UV)
    lg.propose("s1", "r_a", content="calc.py has an add function")
    lg.link("r_a", lg.test("s1", True))
    lg.confirm("s1", "r_a")
    lg.end("s1")
    lg.start("s2", **UV)
    lg.deliver("s2", "r_a")


def test_a_model_counter_is_advice_and_leaves_a_confirmed_record_at_l2():
    lg = Ledger()
    confirmed_then_countered(lg, None)
    ls = lg.tool_result("s2")
    lg.link("r_a", ls, polarity="counter")
    lg.end("s2")
    assert rung(lg, "r_a", cfg()) == "L1"
    lab = labels(lg).by_id["r_a"]
    assert (lab.rung.value, lab.advice, lab.counters) == ("L2", (ls.id,), ())


def test_the_users_own_words_against_a_record_demote_it():
    lg = Ledger()
    confirmed_then_countered(lg, None)
    msg = lg.user_msg("s2", "No: calc.py has no add function any more, I removed it.")
    lg.link("r_a", msg, polarity="counter", statement={"quote": "calc.py has no add function any more"})
    lg.end("s2")
    lab = labels(lg).by_id["r_a"]
    assert (lab.rung.value, lab.counters) == ("L1", (msg.id,))


def check_quoted(lg: Ledger, sids: tuple[str, ...]) -> None:
    for sid in sids:
        lg.start(sid, **UV)
        if sid == sids[0]:
            lg.propose(sid, "r_ops", kind=RecordKind.CONSTRAINT, content=CHECKED)
        lg.link("r_ops", lg.outcome_output(sid, 1, CHECKED + "\n"), statement={"quote": CHECKED})
        lg.end(sid)


def test_the_users_check_validates_its_own_words_once_they_recur_in_a_second_session():
    lg = Ledger()
    check_quoted(lg, ("s1",))
    assert rung(lg, "r_ops") == "L1"
    check_quoted(lg, ("s2",))
    lab = labels(lg).by_id["r_ops"]
    assert (lab.rung.value, lab.reason) == ("L2", "the user's check printed its words in two sessions")


def test_a_check_quote_the_agent_wrote_into_its_code_is_taint():
    lg = Ledger()
    lg.start("s0", **UV)
    lg.tool_call("s0", "Edit", f'print("{CHECKED}")')
    lg.end("s0")
    check_quoted(lg, ("s1", "s2"))
    assert rung(lg, "r_ops") == "L1"


def steered_constraint(lg: Ledger) -> None:
    for sid in ("s1", "s2"):
        lg.start(sid, **UV)
        if sid == "s1":
            lg.propose(sid, "r_con", kind=RecordKind.CONSTRAINT, content="Return None on invalid input")
        lg.link("r_con", lg.test(sid, True))
        lg.end(sid)


def test_a_hypothesis_is_tested_only_against_a_check_the_user_configured():
    lg = Ledger()
    steered_constraint(lg)
    plain = labels(lg).by_id["r_con"]
    assert (plain.rung.value, plain.reason, plain.trial) == ("L1", "hypothesis: no outcome check is configured to test it", None)
    assert labels(lg, attested(**WITH_CHECK)).by_id["r_con"].trial == "open"
    assert labels(lg, cfg()).by_id["r_con"].trial == "open"


def test_a_model_refinement_does_not_withhold_its_target():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a", content="calc.py has an add function")
    lg.confirm("s1", "r_a")
    lg.end("s1")
    lg.start("s2", **UV)
    lg.deliver("s2", "r_a")
    lg.propose("s2", "r_q", content="calc.py has an add function for ints only", refines=("r_a",))
    lg.link("r_q", lg.test("s2", False))
    lg.end("s2")
    assert labels(lg).by_id["r_a"].deliverable


def test_the_gated_arm_renders_each_record_with_its_source():
    lg = Ledger()
    stated_rule(lg, "s1", f"From now on: always {RULE}.")
    check_quoted(lg, ("s2", "s3"))
    view = packet(project(lg.events), labels(lg), attested(), State(**UV), arm="gated", channel="session_start")
    assert view.render().splitlines() == [
        "## Notes from earlier sessions (rka-memory)",
        "Each note ends with its source. If one is wrong, tell the user, who can type `rkm reject <id> <reason>`.",
        f'- [r_rule] The user said: "{RULE}"',
        f"- [r_ops] The user's own acceptance check, which you are not shown, printed: \"{CHECKED}\"",
    ]
    v2 = packet(project(lg.events), labels(lg, cfg()), cfg(), State(**UV), arm="gated", channel="session_start")
    assert v2.render().startswith("## Validated memory (rka-memory)")


def test_a_file_the_agent_wrote_taints_the_words_the_user_pastes_from_it(tmp_path, monkeypatch):
    monkeypatch.delenv("RKA_MEMORY_DIR", raising=False)
    c = attested(ledger={"dir": str(tmp_path)})
    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / "h1").write_text(f"[ci] hint: always {RULE} because pytest is not installed\n")
    lg = Ledger()
    lg.start("s1", **UV)
    lg.tool_call("s1", "Write", "/p/app/ci.log")
    lg._ev(Kind.FILE_WRITE, Actor.TOOL, "s1", {"tool": "Write", "path": "/p/app/ci.log", "pre_hash": "absent", "post_hash": "h1"})
    lg.end("s1")
    stated_rule(lg, "s2", f"CI failed, here is the log you wrote:\n[ci] hint: always {RULE} because pytest is not installed")
    assert rung(lg, "r_rule", c) != "L2"
    (tmp_path / "blobs" / "h1").write_text("[ci] all green\n")
    assert rung(lg, "r_rule", attested(ledger={"dir": str(tmp_path)})) == "L2"
