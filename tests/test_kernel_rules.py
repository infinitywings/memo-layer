"""The evidence and ladder rules, as the kernel labels them (DESIGN_V2 5).

Ported from the v1 engine's tests. Where v2 changed the semantics the test
says so: a typed reject withdraws its record and its dependents, a
prescriptive record rises only by the user's affirmation, and nothing narrows
a scope.
"""

import pytest

from ledger_builder import Ledger, cfg
from rka_memory.domain import Actor, Event, FieldConstraint, Kind, Polarity, RecordKind, Scope, State
from rka_memory.kernel.evidence import counted
from rka_memory.kernel.labels import audit_events, label
from rka_memory.world import project

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")


def lab(lg: Ledger, record_id: str, config=None):
    return label(project(lg.events), config or cfg(), lg.clock).by_id[record_id]


def rung_reason(lg: Ledger, record_id: str) -> tuple[str, str]:
    got = lab(lg, record_id)
    return got.rung.value, got.reason


def two_strong_sessions(lg: Ledger, record_id: str = "r_a", scope: Scope | None = None, **spec) -> None:
    lg.start("s1", **UV)
    ok1 = lg.test("s1", True)
    lg.propose("s1", record_id, scope=scope, **spec)
    lg.link(record_id, ok1)
    lg.end("s1")
    lg.start("s2", **UV)
    lg.link(record_id, lg.exit("s2", 0))
    lg.end("s2")


def test_two_strong_events_in_two_sessions_promote_a_fact_to_l2():
    lg = Ledger()
    two_strong_sessions(lg)
    assert rung_reason(lg, "r_a") == ("L2", "independent grounding in enough distinct sessions")


def test_the_same_evidence_puts_a_constraint_into_a_trial_at_l1():
    lg = Ledger()
    two_strong_sessions(lg, kind=RecordKind.CONSTRAINT)
    assert rung_reason(lg, "r_a") == ("L1", "in trial: treated 0, withheld 0 sessions")


def test_strong_evidence_from_one_session_stays_l1():
    lg = Ledger()
    lg.start("s1", **UV)
    a, b = lg.test("s1", True), lg.exit("s1", 0)
    lg.propose("s1", "r_a")
    lg.link("r_a", a)
    lg.link("r_a", b)
    assert rung_reason(lg, "r_a") == ("L1", "independent grounding below threshold")


def test_model_authored_evidence_leaves_record_l0d():
    lg = Ledger()
    lg.start("s1", **UV)
    call = lg.tool_call("s1", "Bash", "uv sync")
    lg.propose("s1", "r_a")
    lg.link("r_a", call)
    assert rung_reason(lg, "r_a") == ("L0D", "no counted non-model evidence")


def test_failing_outcome_cannot_be_cited_as_support():
    lg = Ledger()
    lg.start("s1", **UV)
    bad = lg.exit("s1", 2)
    lg.propose("s1", "r_a")
    lg.link("r_a", bad, "support")
    assert lab(lg, "r_a").rung.value == "L0D"


def test_weak_signals_count_one_third_and_need_three_sessions():
    lg = Ledger()
    lg.start("s1", **UV)
    msgs = [lg.user_msg("s1", f"use uv {i}") for i in range(3)]
    lg.propose("s1", "r_a")
    for m in msgs:
        lg.link("r_a", m)
    lg.start("s2", **UV)
    for m in [lg.user_msg("s2", f"uv again {i}") for i in range(3)]:
        lg.link("r_a", m)
    assert lab(lg, "r_a").rung.value == "L1"
    lg.start("s3", **UV)
    lg.link("r_a", lg.user_msg("s3", "uv, really"))
    assert lab(lg, "r_a").rung.value == "L2"


def test_one_strong_signal_lifts_the_weak_only_session_requirement():
    lg = Ledger()
    lg.start("s1", **UV)
    msg = lg.user_msg("s1", "use uv")
    lg.propose("s1", "r_a")
    lg.link("r_a", msg)
    lg.start("s2", **UV)
    lg.link("r_a", lg.test("s2", True))
    lg.link("r_a", lg.user_msg("s2", "uv please"))
    assert lab(lg, "r_a").rung.value == "L1"
    lg.link("r_a", lg.user_msg("s2", "uv, I said"))
    assert lab(lg, "r_a").rung.value == "L2"


def test_evidence_outside_scope_does_not_count():
    lg = Ledger()
    scope = Scope(project=FieldConstraint(frozenset({"/p/app"})))
    lg.start("s1", **UV)
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_a", scope=scope)
    lg.link("r_a", ok1)
    lg.start("s2", **{**UV, "project": "/p/other"})
    lg.link("r_a", lg.test("s2", True))
    assert lab(lg, "r_a").rung.value == "L1"


def test_a_counter_event_inside_scope_demotes_and_nothing_narrows():
    lg = Ledger()
    two_strong_sessions(lg)
    lg.start("s3", **{**UV, "toolchain": "pip"})
    lg.deliver("s3", "r_a")
    lg.link("r_a", lg.test("s3", False), "counter")
    world = lg.gate()
    assert world.records["r_a"].narrowings == []
    assert rung_reason(lg, "r_a") == ("L1", "counter-evidence after the last affirmation")


def test_scope_is_frozen_at_project_and_toolchain():
    assert cfg().scope_fields == ("project", "toolchain")


def refuted_claim(lg: Ledger, counter_event) -> None:
    """r_a at L1 from one session, then a counter link in a session of another
    task class that never received it, then two sessions of support."""
    lg.start("s1", **UV)
    lg.propose("s1", "r_a", scope=Scope())
    lg.link("r_a", lg.test("s1", True))
    lg.start("s2", **{**UV, "task_class": "debugging"})
    lg.link("r_a", counter_event(lg, "s2"), "counter")
    for session in ("s3", "s4"):
        lg.start(session, **UV)
        lg.link("r_a", lg.test(session, True))


def test_a_refutation_of_a_claim_the_session_never_received_counts():
    lg = Ledger()
    refuted_claim(lg, lambda lg, s: lg.user_msg(s, "there is no such registry"))
    assert lab(lg, "r_a").rung.value == "L1"


def test_only_what_a_session_observed_refutes_a_claim_it_never_received():
    lg = Ledger()
    refuted_claim(lg, lambda lg, s: lg.test(s, False))
    assert lab(lg, "r_a").rung.value == "L2"


def test_a_counter_blocks_repromotion_until_the_user_affirms():
    lg = Ledger()
    two_strong_sessions(lg)
    lg.start("s3", **UV)
    lg.deliver("s3", "r_a")
    lg.link("r_a", lg.undo("s3", "s2"), "counter")
    lg.end("s3")
    for s in ("s4", "s5"):
        lg.start(s, **UV)
        lg.link("r_a", lg.test(s, True))
        lg.end(s)
    assert lab(lg, "r_a").rung.value == "L1"
    lg.confirm("cli", "r_a")
    assert rung_reason(lg, "r_a") == ("L2", "user confirmed")


def test_a_reject_withdraws_and_a_later_confirm_restores():
    lg = Ledger()
    two_strong_sessions(lg)
    lg.reject("cli", "r_a")
    assert rung_reason(lg, "r_a") == ("L4", "withdrawn by the user")
    lg.confirm("cli", "r_a")
    assert rung_reason(lg, "r_a") == ("L2", "user confirmed")


def test_a_confirm_promotes_a_candidate_with_no_other_evidence():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a", kind=RecordKind.CONSTRAINT)
    lg.confirm("cli", "r_a")
    assert rung_reason(lg, "r_a") == ("L2", "user confirmed")


def based(lg: Ledger) -> None:
    """r_base at L2, then r_dep proposed and supported in two sessions that
    received r_base: valid, since r_base is not in r_dep's reach."""
    two_strong_sessions(lg, "r_base")
    for s in ("s3", "s4"):
        lg.start(s, **UV)
        lg.deliver(s, "r_base")
        ok = lg.test(s, True)
        if s == "s3":
            lg.propose(s, "r_dep", depends_on=("r_base",))
        lg.link("r_dep", ok)
        lg.end(s)


def test_withdrawing_a_dependency_withdraws_its_dependents():
    lg = Ledger()
    based(lg)
    assert lab(lg, "r_dep").rung.value == "L2"
    lg.reject("cli", "r_base")
    assert rung_reason(lg, "r_base") == ("L4", "withdrawn by the user")
    assert rung_reason(lg, "r_dep") == ("L4", "depends on a record the user withdrew")


def test_a_dependency_demoted_by_a_counter_takes_the_support_it_was_delivered_with():
    lg = Ledger()
    based(lg)
    lg.start("s5", **UV)
    lg.deliver("s5", "r_base", "r_dep")
    lg.link("r_base", lg.test("s5", False), "counter")
    lg.end("s5")
    assert lab(lg, "r_base").rung.value == "L1"
    assert rung_reason(lg, "r_dep") == ("L0D", "all support was observed where an invalid dependency was delivered")


def test_a_dependent_grounded_where_its_dependency_was_not_delivered_keeps_its_rung():
    lg = Ledger()
    two_strong_sessions(lg, "r_base")
    lg.start("s3", **UV)
    lg.propose("s3", "r_dep", depends_on=("r_base",))
    lg.link("r_dep", lg.test("s3", True))
    lg.start("s4", **UV)
    lg.link("r_dep", lg.test("s4", True))
    lg.start("s5", **UV)
    lg.link("r_base", lg.user_msg("s5", "that is not so"), "counter")
    assert lab(lg, "r_base").rung.value == "L1"
    assert lab(lg, "r_dep").rung.value == "L2"


def test_untouched_l1_retires_after_window():
    lg = Ledger()
    lg.start("s1", **UV)
    ok = lg.test("s1", True)
    lg.propose("s1", "r_a")
    lg.link("r_a", ok)
    lg.advance(days=59)
    assert lab(lg, "r_a").rung.value == "L1"
    lg.advance(days=2)
    assert rung_reason(lg, "r_a") == ("L4", "untouched for 60 days")


def test_retirement_is_terminal_until_the_user_affirms():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a")
    lg.link("r_a", lg.test("s1", True))
    lg.end("s1")
    lg.advance(days=61)
    lg.start("s2", **UV)
    lg.link("r_a", lg.test("s2", True))
    lg.end("s2")
    assert rung_reason(lg, "r_a") == ("L4", "untouched for 60 days")
    lg.confirm("cli", "r_a")
    assert rung_reason(lg, "r_a") == ("L2", "user confirmed")


def test_a_valid_record_never_retires_and_a_demoted_one_retires_from_its_last_touch():
    lg = Ledger()
    two_strong_sessions(lg)
    lg.advance(days=90)
    assert lab(lg, "r_a").rung.value == "L2"
    lg.start("s3", **UV)
    lg.deliver("s3", "r_a")
    lg.link("r_a", lg.test("s3", False), "counter")
    lg.end("s3")
    assert lab(lg, "r_a").rung.value == "L1"
    lg.advance(days=61)
    assert rung_reason(lg, "r_a") == ("L4", "untouched for 60 days")


def test_counter_links_do_not_keep_an_l1_record_alive():
    lg = Ledger()
    lg.start("s1", **UV)
    ok = lg.test("s1", True)
    lg.propose("s1", "r_a")
    lg.link("r_a", ok)
    for session in ("s2", "s3"):
        lg.advance(days=20)
        lg.start(session, **UV)
        lg.link("r_a", lg.user_msg(session, "that is not how this project works"), polarity="counter")
    lg.advance(days=19)
    assert lab(lg, "r_a").rung.value == "L1"
    lg.advance(days=2)
    assert rung_reason(lg, "r_a") == ("L4", "untouched for 60 days")


PROC = dict(
    kind=RecordKind.PROCEDURE,
    content="Run uv sync before tests",
    steps=(("Bash", "uv sync"), ("Bash", "uv run pytest")),
    acceptance_check="uv run pytest",
)


def follow(lg: Ledger, session: str, passed: bool = True, skill: bool = False) -> None:
    lg.start(session, **UV)
    if skill:
        lg.tool_call(session, "Skill", "rka-r-proc")
    lg.tool_call(session, "Bash", "uv sync")
    lg.exit(session, 0, "uv sync")
    lg.tool_call(session, "Bash", "uv run pytest -q")
    lg.test(session, passed, "uv run pytest")
    lg.end(session)


def confirmed_procedure(lg: Ledger, **spec) -> None:
    lg.start("s1", **UV)
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_proc", **{**PROC, **spec})
    lg.link("r_proc", ok1)
    lg.confirm("cli", "r_proc")
    lg.end("s1")


def test_a_confirmed_procedure_reaches_l3_then_retires_on_a_failed_acceptance_check():
    lg = Ledger()
    confirmed_procedure(lg)
    assert lab(lg, "r_proc").rung.value == "L2"
    follow(lg, "s2")
    assert lab(lg, "r_proc").rung.value == "L2"
    follow(lg, "s3")
    assert rung_reason(lg, "r_proc") == ("L3", "followed and succeeded in 2 later sessions")
    world = lg.gate()
    assert world.records["r_proc"].acceptance_check == "uv run pytest"
    follow(lg, "s4", passed=False, skill=True)
    assert rung_reason(lg, "r_proc") == ("L4", "acceptance check failed after the skill was used")


def test_a_procedure_backed_by_outcomes_alone_stays_l1_in_a_trial():
    lg = Ledger()
    two_strong_sessions(lg, "r_proc", **PROC)
    follow(lg, "s3")
    follow(lg, "s4")
    assert rung_reason(lg, "r_proc") == ("L1", "in trial: treated 0, withheld 0 sessions")


def test_procedure_followed_but_failing_sessions_do_not_promote():
    lg = Ledger()
    confirmed_procedure(lg)
    follow(lg, "s2", passed=False)
    follow(lg, "s3")
    assert lab(lg, "r_proc").rung.value == "L2"


def test_user_undo_of_skill_output_retires_skill():
    lg = Ledger()
    confirmed_procedure(lg)
    follow(lg, "s2")
    follow(lg, "s3")
    assert lab(lg, "r_proc").rung.value == "L3"
    follow(lg, "s4", skill=True)
    lg.start("s5", **UV)
    lg.undo("s5", "s4")
    assert rung_reason(lg, "r_proc") == ("L4", "user undid a file written while the skill was in use")


def test_procedure_never_reaches_l3_until_its_acceptance_check_was_seen_passing():
    lg = Ledger()
    confirmed_procedure(lg, acceptance_check="make check")
    follow(lg, "s2")
    follow(lg, "s3")
    assert lab(lg, "r_proc").rung.value == "L2"
    lg.start("s4", **UV)
    lg.tool_call("s4", "Bash", "uv sync")
    lg.exit("s4", 0, "uv sync")
    lg.tool_call("s4", "Bash", "uv run pytest -q")
    lg.exit("s4", 0, "make check")
    lg.test("s4", True, "uv run pytest")
    lg.end("s4")
    assert lab(lg, "r_proc").rung.value == "L3"


def test_a_dependent_learned_only_from_a_retired_skill_falls_to_l0d():
    lg = Ledger()
    confirmed_procedure(lg)
    follow(lg, "s2")
    follow(lg, "s3")
    lg.start("s4", **UV)
    lg.deliver("s4", "r_proc")
    ok = lg.test("s4", True)
    lg.propose("s4", "r_dep", depends_on=("r_proc",))
    lg.link("r_dep", ok)
    lg.end("s4")
    assert lab(lg, "r_dep").rung.value == "L1"
    follow(lg, "s5", passed=False, skill=True)
    assert lab(lg, "r_proc").rung.value == "L4"
    assert rung_reason(lg, "r_dep") == ("L0D", "all support was observed where an invalid dependency was delivered")


def test_gating_twice_writes_nothing_the_second_time_and_replay_agrees():
    lg = Ledger()
    two_strong_sessions(lg)
    lg.start("s3", **{**UV, "toolchain": "pip"})
    lg.link("r_a", lg.test("s3", False), "counter")
    world = lg.gate()
    assert audit_events(world, label(world, cfg(), lg.clock), lg.clock) == []
    labels = label(project(lg.events), cfg(), lg.clock)
    assert {rid: r.rung for rid, r in world.records.items()} == {rid: labels.rung(rid) for rid in world.records}


def test_the_audit_trail_records_each_move_with_its_reason():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a")
    lg.link("r_a", lg.test("s1", True))
    lg.end("s1")
    lg.gate()
    lg.start("s2", **UV)
    lg.link("r_a", lg.exit("s2", 0))
    lg.end("s2")
    world = lg.gate()
    assert [(m.from_rung and m.from_rung.value, m.to_rung.value, m.reason) for m in world.records["r_a"].history] == [
        (None, "L1", "independent grounding below threshold"),
        ("L1", "L2", "independent grounding in enough distinct sessions"),
    ]


def test_adapter_cannot_attribute_user_signals_to_the_model():
    with pytest.raises(ValueError, match="may not author"):
        Event.new(Kind.USER_EDIT, Actor.MODEL, "s1", {})
    with pytest.raises(ValueError, match="may not author"):
        Event.new(Kind.RUNG_CHANGE, Actor.MODEL, "s1", {})


def test_a_confirm_for_one_record_cannot_be_linked_to_promote_another():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a")
    lg.propose("s1", "r_b")
    ok = lg.confirm("s1", "r_a")
    lg.link("r_b", ok)
    assert lab(lg, "r_a").rung.value == "L2"
    assert lab(lg, "r_b").rung.value == "L0D"


def test_model_links_cannot_cite_events_from_another_session():
    lg = Ledger()
    lg.start("s1", **UV)
    old = lg.test("s1", True)
    lg.start("s2", **UV)
    lg.propose("s2", "r_a")
    lg.events.append(
        Event.new(Kind.EVIDENCE_LINKED, Actor.MODEL, "s2", {"record_id": "r_a", "event_id": old.id, "polarity": "support"}, id="ev_x")
    )
    assert lab(lg, "r_a").rung.value == "L0D"


def test_model_judgments_cannot_move_sessions_between_scopes():
    lg = Ledger()
    lg.start("s1", **{**UV, "task_class": None})
    lg._ev(Kind.JUDGMENT, Actor.RUNNER, "s1", {"target": "session", "field": "task_class", "value": "coding"})
    lg._ev(Kind.JUDGMENT, Actor.MODEL, "s1", {"target": "session", "field": "task_class", "value": "ops"})
    lg._ev(Kind.JUDGMENT, Actor.MODEL, "s1", {"target": "session", "field": "project", "value": "/p/other"})
    lg._ev(Kind.JUDGMENT, Actor.MODEL, "s1", {"target": "session", "session_id": "s9", "field": "user_context", "value": "x"})
    world = project(lg.events)
    assert world.sessions["s1"].state == State(**{**UV, "task_class": "coding"})
    assert "s9" not in world.sessions


def test_only_the_runner_can_log_record_applicability_judgments():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a")
    lg._ev(Kind.JUDGMENT, Actor.MODEL, "s1", {"target": "record", "record_id": "r_a", "field": "task_class", "value": "ops", "applies": True})
    lg._ev(Kind.JUDGMENT, Actor.RUNNER, "s1", {"target": "record", "record_id": "r_a", "field": "task_class", "value": "data", "applies": True})
    assert project(lg.events).judgments == {("r_a", "task_class", "data"): True}


SUMMARY = "end every reply with a final line that starts with `Summary:`"


def stated(lg: Ledger, session: str, text: str, record_id: str, polarity: str = "support", **said) -> Event:
    lg.start(session, **UV)
    msg = lg.user_msg(session, text)
    lg.link(record_id, msg, polarity, statement=said)
    lg.end(session)
    return msg


def summary_rule(lg: Ledger, content: str = SUMMARY, **spec) -> None:
    lg.start("s0", **UV)
    lg.propose("s0", "r_sum", kind=RecordKind.PREFERENCE, content=content, **spec)
    lg.end("s0")


def test_a_standing_statement_of_the_records_own_words_confirms_it_at_once():
    lg = Ledger()
    summary_rule(lg)
    stated(lg, "s1", f"From now on, {SUMMARY}.", "r_sum", quote=SUMMARY, marker="From now on")
    assert rung_reason(lg, "r_sum") == ("L2", "the user stated it as a standing rule")


@pytest.mark.parametrize(
    "said",
    [
        {"quote": SUMMARY},
        {"quote": SUMMARY, "marker": "every time"},
        {"quote": "end every reply with a Summary line", "marker": "from now on"},
        {"quote": SUMMARY, "marker": "Please note"},
        {"quote": SUMMARY, "marker": "always"},
    ],
    ids=[
        "no standing words",
        "standing words not in the message",
        "quote not in the message",
        "words without a standing phrase",
        "standing phrase far from the rule",
    ],
)
def test_a_statement_the_message_does_not_bear_out_weighs_as_a_plain_message(said):
    lg = Ledger()
    summary_rule(lg)
    far = " ".join(["The deploy notes are long."] * 10)
    stated(lg, "s1", f"Please note: from now on, {SUMMARY}. {far} I always check them.", "r_sum", **said)
    world = project(lg.events)
    assert lab(lg, "r_sum").rung.value == "L1"
    assert [round(g.weight, 2) for g in counted(world.records["r_sum"], world, cfg(), Polarity.SUPPORT)] == [0.33]


def test_a_standing_restatement_in_other_words_weighs_like_a_confirm_but_does_not_affirm():
    lg = Ledger()
    summary_rule(lg, content="Finish every reply with a Summary line")
    stated(lg, "s1", f"From now on, {SUMMARY}.", "r_sum", quote=SUMMARY, marker="From now on")
    stated(lg, "s2", f"Remember: {SUMMARY}.", "r_sum", quote=SUMMARY, marker="Remember")
    world = project(lg.events)
    assert [g.weight for g in counted(world.records["r_sum"], world, cfg(), Polarity.SUPPORT)] == [1.0, 1.0]
    assert rung_reason(lg, "r_sum") == ("L1", "in trial: treated 0, withheld 0 sessions")


def test_a_withdrawal_in_the_users_words_demotes_wherever_the_rule_applies_until_restated():
    lg = Ledger()
    summary_rule(lg, scope=Scope.exact(State(**UV), broaden=frozenset({"task_class", "toolchain", "model_id"})))
    stated(lg, "s1", f"From now on, {SUMMARY}.", "r_sum", quote=SUMMARY, marker="From now on")
    assert lab(lg, "r_sum").rung.value == "L2"
    lg.start("s2", **dict(UV, task_class="debugging", toolchain="pip"))
    stop = lg.user_msg("s2", "You can stop adding Summary lines.")
    lg.link("r_sum", stop, "counter", statement={"quote": "stop adding Summary lines"})
    lg.end("s2")
    got = lab(lg, "r_sum")
    assert (got.rung.value, got.reason, got.withdrawn) == ("L1", "counter-evidence after the last affirmation", False)
    stated(lg, "s3", f"Actually, from now on {SUMMARY} again.", "r_sum", quote=SUMMARY, marker="from now on")
    assert rung_reason(lg, "r_sum") == ("L2", "the user stated it as a standing rule")


def test_a_preference_grounded_only_by_outcomes_stays_l0d_until_the_user_backs_it():
    lg = Ledger()
    lg.start("s1", **UV)
    ok = lg.test("s1", True)
    lg.propose("s1", "r_new", kind=RecordKind.PREFERENCE, content="Prefer small commits")
    lg.link("r_new", ok)
    lg.end("s1")
    assert rung_reason(lg, "r_new") == ("L0D", "a preference needs the user's own evidence")
    lg.start("s2", **UV)
    lg.link("r_new", lg.user_msg("s2", "Small commits, please."))
    lg.end("s2")
    assert lab(lg, "r_new").rung.value == "L1"
