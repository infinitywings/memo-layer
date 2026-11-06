import pytest

from ledger_builder import Ledger, cfg, moves, rung
from rka_memory.domain import Actor, Event, FieldConstraint, Kind, RecordKind, Scope
from rka_memory.validity.engine import evaluate, valid
from rka_memory.domain import State
from rka_memory.world import project

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")


def two_strong_sessions(lg: Ledger, record_id: str = "r_a", scope: Scope | None = None) -> None:
    lg.start("s1", **UV)
    ok1 = lg.test("s1", True)
    lg.propose("s1", record_id, scope=scope)
    lg.link(record_id, ok1)
    lg.end("s1")
    lg.gate()
    lg.start("s2", **UV)
    ok2 = lg.exit("s2", 0)
    lg.link(record_id, ok2)
    lg.end("s2")


def test_two_strong_events_in_two_sessions_promote_to_l2():
    lg = Ledger()
    two_strong_sessions(lg)
    world = lg.gate()
    assert moves(world, "r_a") == [
        (None, "L1", "proposed with non-model grounding"),
        ("L1", "L2", "grounded in enough distinct sessions"),
    ]


def test_strong_evidence_from_one_session_stays_l1():
    lg = Ledger()
    lg.start("s1", **UV)
    a, b = lg.test("s1", True), lg.exit("s1", 0)
    lg.propose("s1", "r_a")
    lg.link("r_a", a)
    lg.link("r_a", b)
    assert rung(lg.gate(), "r_a") == "L1"


def test_model_authored_evidence_leaves_record_l0d():
    lg = Ledger()
    lg.start("s1", **UV)
    call = lg.tool_call("s1", "Bash", "uv sync")
    lg.propose("s1", "r_a")
    lg.link("r_a", call)
    assert moves(lg.gate(), "r_a") == [(None, "L0D", "proposed without non-model grounding")]


def test_failing_outcome_cannot_be_cited_as_support():
    lg = Ledger()
    lg.start("s1", **UV)
    bad = lg.exit("s1", 2)
    lg.propose("s1", "r_a")
    lg.link("r_a", bad, "support")
    assert rung(lg.gate(), "r_a") == "L0D"


def test_weak_signals_count_one_third():
    lg = Ledger()
    lg.start("s1", **UV)
    msgs = [lg.user_msg("s1", f"use uv {i}") for i in range(3)]
    lg.propose("s1", "r_a")
    for m in msgs:
        lg.link("r_a", m)
    lg.start("s2", **UV)
    two = [lg.user_msg("s2", f"uv again {i}") for i in range(2)]
    for m in two:
        lg.link("r_a", m)
    assert rung(lg.gate(), "r_a") == "L1"
    lg.link("r_a", lg.user_msg("s2", "uv, really"))
    assert rung(lg.gate(), "r_a") == "L2"


def test_evidence_outside_scope_does_not_count():
    lg = Ledger()
    scope = Scope(project=FieldConstraint(frozenset({"/p/app"})))
    lg.start("s1", **UV)
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_a", scope=scope)
    lg.link("r_a", ok1)
    lg.start("s2", **{**UV, "project": "/p/other"})
    lg.link("r_a", lg.test("s2", True))
    assert rung(lg.gate(), "r_a") == "L1"


def test_counter_event_narrows_scope_when_evidence_survives():
    lg = Ledger()
    two_strong_sessions(lg)
    assert rung(lg.gate(), "r_a") == "L2"
    lg.start("s3", **{**UV, "toolchain": "pip"})
    lg.link("r_a", lg.test("s3", False), "counter")
    world = lg.gate()
    rec = world.records["r_a"]
    assert rec.rung.value == "L2"
    assert [(n.field, n.value) for n in rec.narrowings] == [("toolchain", "pip")]
    assert valid(rec, State(**UV), world, cfg())
    assert not valid(rec, State(**{**UV, "toolchain": "pip"}), world, cfg())


def test_counter_event_in_the_evidence_state_demotes_and_blocks_repromotion():
    lg = Ledger()
    two_strong_sessions(lg)
    lg.gate()
    lg.start("s3", **UV)
    lg.link("r_a", lg.undo("s3", "s2"), "counter")
    world = lg.gate()
    assert moves(world, "r_a")[-1] == ("L2", "L1", "counter-event inside scope")
    lg.start("s4", **UV)
    lg.link("r_a", lg.test("s4", True))
    lg.start("s5", **UV)
    lg.link("r_a", lg.test("s5", True))
    assert rung(lg.gate(), "r_a") == "L1"


def test_user_reject_demotes_and_later_confirm_overrides():
    lg = Ledger()
    two_strong_sessions(lg)
    lg.gate()
    lg.reject("cli", "r_a")
    assert rung(lg.gate(), "r_a") == "L1"
    lg.confirm("cli", "r_a")
    world = lg.gate()
    assert moves(world, "r_a")[-1] == ("L1", "L2", "user confirmed")
    assert evaluate(world, cfg(), lg.clock) == []


def test_confirm_promotes_ungrounded_candidate_but_not_when_override_disabled():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a")
    lg.gate()
    lg.confirm("cli", "r_a")
    no_override = cfg(gate={"confirm_overrides": False})
    assert rung(_gate_copy(lg, no_override), "r_a") == "L1"
    assert rung(lg.gate(), "r_a") == "L2"


def _gate_copy(lg: Ledger, config):
    world = project(lg.events)
    evaluate(world, config, lg.clock)
    return world


def test_demoted_dependency_drops_dependents_without_independent_grounding():
    lg = Ledger()
    two_strong_sessions(lg, "r_base")
    lg.gate()
    for s in ("s3", "s4"):
        lg.start(s, **UV)
        lg.deliver(s, "r_base")
        ok = lg.test(s, True)
        if s == "s3":
            lg.propose(s, "r_dep", depends_on=("r_base",))
        lg.link("r_dep", ok)
        lg.end(s)
    assert rung(lg.gate(), "r_dep") == "L2"
    lg.reject("cli", "r_base")
    world = lg.gate()
    assert rung(world, "r_base") == "L1"
    assert moves(world, "r_dep")[-1][1:] == ("L1", "dependency invalidated")
    dep_move = world.records["r_dep"].history[-1]
    assert dep_move.cause_ids == (world.records["r_base"].history[-1].event_id,)


def test_dependent_with_independent_grounding_keeps_its_rung():
    lg = Ledger()
    two_strong_sessions(lg, "r_base")
    lg.gate()
    lg.start("s3", **UV)
    ok3 = lg.test("s3", True)
    lg.propose("s3", "r_dep", depends_on=("r_base",))
    lg.link("r_dep", ok3)
    lg.start("s4", **UV)
    lg.link("r_dep", lg.test("s4", True))
    assert rung(lg.gate(), "r_dep") == "L2"
    lg.reject("cli", "r_base")
    assert rung(lg.gate(), "r_dep") == "L2"


def test_untouched_l1_retires_after_window():
    lg = Ledger()
    lg.start("s1", **UV)
    ok = lg.test("s1", True)
    lg.propose("s1", "r_a")
    lg.link("r_a", ok)
    lg.gate()
    lg.advance(days=59)
    assert rung(lg.gate(), "r_a") == "L1"
    lg.advance(days=2)
    assert moves(lg.gate(), "r_a")[-1] == ("L1", "L4", "untouched for 60 days")


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


def test_procedure_reaches_l3_then_retires_on_failed_acceptance_check():
    lg = Ledger()
    lg.start("s1", **UV)
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_proc", **PROC)
    lg.link("r_proc", ok1)
    lg.end("s1")
    lg.start("s2", **UV)
    lg.link("r_proc", lg.test("s2", True))
    lg.end("s2")
    assert rung(lg.gate(), "r_proc") == "L2"
    follow(lg, "s3")
    assert rung(lg.gate(), "r_proc") == "L2"
    follow(lg, "s4")
    world = lg.gate()
    assert rung(world, "r_proc") == "L3"
    assert world.records["r_proc"].acceptance_check == "uv run pytest"
    follow(lg, "s5", passed=False, skill=True)
    world = lg.gate()
    assert moves(world, "r_proc")[-1] == ("L3", "L4", "acceptance check failed after the skill was used")


def test_procedure_followed_but_failing_sessions_do_not_promote():
    lg = Ledger()
    lg.start("s1", **UV)
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_proc", **PROC)
    lg.link("r_proc", ok1)
    lg.confirm("cli", "r_proc")
    lg.end("s1")
    lg.gate()
    follow(lg, "s2", passed=False)
    follow(lg, "s3")
    assert rung(lg.gate(), "r_proc") == "L2"


def test_user_undo_of_skill_output_retires_skill():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_proc", **PROC)
    lg.confirm("cli", "r_proc")
    lg.end("s1")
    lg.gate()
    follow(lg, "s2")
    follow(lg, "s3")
    assert rung(lg.gate(), "r_proc") == "L3"
    follow(lg, "s4", skill=True)
    lg.start("s5", **UV)
    lg.undo("s5", "s4")
    assert moves(lg.gate(), "r_proc")[-1][2] == "user undid a file written while the skill was in use"


def test_replay_reproduces_rungs_and_engine_is_idempotent():
    lg = Ledger()
    two_strong_sessions(lg)
    lg.gate()
    lg.start("s3", **{**UV, "toolchain": "pip"})
    lg.link("r_a", lg.test("s3", False), "counter")
    live = lg.gate()
    replayed = project(lg.events)
    assert {k: (r.rung, r.scope) for k, r in replayed.records.items()} == {
        k: (r.rung, r.scope) for k, r in live.records.items()
    }
    assert evaluate(replayed, cfg(), lg.clock) == []


def test_adapter_cannot_attribute_user_signals_to_the_model():
    with pytest.raises(ValueError, match="may not author"):
        Event.new(Kind.USER_EDIT, Actor.MODEL, "s1", {})
    with pytest.raises(ValueError, match="may not author"):
        Event.new(Kind.RUNG_CHANGE, Actor.MODEL, "s1", {})


def test_l1_with_evidence_only_from_a_retired_dependency_retires():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_proc", **PROC)
    lg.confirm("cli", "r_proc")
    lg.end("s1")
    lg.gate()
    follow(lg, "s2")
    follow(lg, "s3")
    assert rung(lg.gate(), "r_proc") == "L3"
    lg.start("s4", **UV)
    lg.deliver("s4", "r_proc")
    ok = lg.test("s4", True)
    lg.propose("s4", "r_dep", depends_on=("r_proc",))
    lg.link("r_dep", ok)
    lg.end("s4")
    assert rung(lg.gate(), "r_dep") == "L1"
    follow(lg, "s5", passed=False, skill=True)
    world = lg.gate()
    assert rung(world, "r_proc") == "L4"
    assert moves(world, "r_dep")[-1][1:] == ("L4", "evidence only from a retired dependency")


def test_a_confirm_for_one_record_cannot_be_linked_to_promote_another():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_a")
    lg.propose("s1", "r_b")
    ok = lg.confirm("s1", "r_a")
    lg.link("r_b", ok)
    world = lg.gate()
    assert rung(world, "r_a") == "L2"
    assert rung(world, "r_b") == "L0D"


def test_model_links_cannot_cite_events_from_another_session():
    lg = Ledger()
    lg.start("s1", **UV)
    old = lg.test("s1", True)
    lg.start("s2", **UV)
    lg.propose("s2", "r_a")
    lg.events.append(
        Event.new(Kind.EVIDENCE_LINKED, Actor.MODEL, "s2", {"record_id": "r_a", "event_id": old.id, "polarity": "support"}, id="ev_x")
    )
    assert rung(lg.gate(), "r_a") == "L0D"


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
