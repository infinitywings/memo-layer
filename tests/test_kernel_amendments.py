"""Amendments, decisions, retraction authority and the impact preview
(docs/DESIGN_V2.md 6 and 7), written before the code.

The user amends by typing; the miner and the agent only propose. A user's
replacement or correction retires what it replaces, and the old version
returns only if the user retracts the new one. An update or a close is a
state condition on applicability. A pending restriction from the miner
withholds what it restricts, unless the user affirmed it. A decision is in
force in the session that made it and deliverable only once the user
affirms it; retracting it withdraws what depends on or implements it and
withholds what was learned under it.
"""

import random

from ledger_builder import Ledger, cfg
from rka_memory.domain import RecordKind, State
from rka_memory.kernel.impact import impact
from rka_memory.kernel.labels import label
from rka_memory.kernel.views import packet
from rka_memory.world import project
from test_kernel_labels import STATEMENT

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
PIP = {**UV, "toolchain": "pip"}


def labels_of(lg: Ledger):
    return label(project(lg.events), cfg(), lg.clock)


def rr(lg: Ledger, rid: str) -> tuple[str, str]:
    lab = labels_of(lg).by_id[rid]
    return lab.rung.value, lab.reason


def delivered(lg: Ledger, state: dict = UV) -> tuple[str, ...]:
    world = project(lg.events)
    return packet(world, label(world, cfg(), lg.clock), cfg(), State(**state), arm="gated", channel="session_start").record_ids


def validated(lg: Ledger, rid: str, first: str, second: str, state: dict = UV, **spec) -> None:
    lg.start(first, **state)
    lg.propose(first, rid, **spec)
    lg.link(rid, lg.test(first, True))
    lg.end(first)
    lg.start(second, **state)
    lg.link(rid, lg.test(second, True))
    lg.end(second)


def stated_rule(lg: Ledger, rid: str, session: str = "s0") -> None:
    lg.start(session, **UV)
    msg = lg.user_msg(session, "From now on: End every reply with a Summary line.")
    lg.propose(session, rid, kind=RecordKind.PREFERENCE, content="End every reply with a Summary line")
    lg.link(rid, msg, statement=STATEMENT)
    lg.end(session)


# Amendment operations


def test_a_users_qualification_is_valid_at_once_and_trails_its_target():
    lg = Ledger()
    validated(lg, "r_a", "s1", "s2", content="calc.divide raises ValueError")
    lg.amend("cli", "qualify", "r_a", "only when b is zero", new_id="r_q")
    assert rr(lg, "r_q") == ("L2", "written by the user")
    world = project(lg.events)
    view = packet(world, label(world, cfg(), lg.clock), cfg(), State(**UV), arm="gated", channel="session_start")
    assert view.render().endswith("### Amendments to the records above\n- [r_q] (on r_a) only when b is zero")


def test_a_users_replacement_retires_the_old_rule_until_the_user_retracts_the_new_one():
    lg = Ledger()
    stated_rule(lg, "r_old")
    lg.amend("cli", "replace", "r_old", "End every reply with a TL;DR line", new_id="r_new")
    assert rr(lg, "r_old") == ("L4", "replaced by the user with r_new")
    assert delivered(lg) == ("r_new",)
    lg.start("s1", **UV)
    lg.deliver("s1", "r_new")
    lg.link("r_new", lg.user_msg("s1", "that is wrong"), "counter")
    lg.end("s1")
    assert rr(lg, "r_new")[0] == "L1"
    assert rr(lg, "r_old")[0] == "L4"
    lg.reject("cli", "r_new")
    assert rr(lg, "r_old") == ("L2", "the user stated it as a standing rule")
    assert delivered(lg) == ("r_old",)


def test_a_users_correction_retires_the_old_wording_and_its_dependents_follow_the_new_one():
    lg = Ledger()
    validated(lg, "r_a", "s1", "s2", content="calc.py has four functions")
    lg.start("s3", **UV)
    lg.deliver("s3", "r_a")
    lg.propose("s3", "r_dep", depends_on=("r_a",))
    lg.link("r_dep", lg.test("s3", True))
    lg.end("s3")
    lg.start("s4", **UV)
    lg.deliver("s4", "r_a")
    lg.link("r_dep", lg.test("s4", True))
    lg.end("s4")
    assert rr(lg, "r_dep")[0] == "L2"
    lg.amend("cli", "correct", "r_a", "calc.py has five functions", new_id="r_fix")
    assert rr(lg, "r_a") == ("L4", "corrected by the user with r_fix")
    assert rr(lg, "r_dep")[0] == "L2"
    assert delivered(lg) == ("r_dep", "r_fix")


def test_an_update_closes_the_old_version_where_the_world_changed_and_opens_the_new_one_there():
    lg = Ledger()
    validated(lg, "r_a", "s1", "s2", content="the suite runs with uv run pytest", scope=None)
    lg.amend("cli", "update", "r_a", "the suite runs with python -m pytest", new_id="r_pip", condition={"toolchain": "pip"})
    assert delivered(lg, UV) == ("r_a",)
    assert delivered(lg, PIP) == ("r_pip",)
    lg.start("s3", **PIP)
    lg.link("r_a", lg.test("s3", False), "counter")
    lg.end("s3")
    assert rr(lg, "r_a")[0] == "L2"


def test_a_close_ends_a_record_where_a_condition_holds_and_keeps_it_elsewhere():
    lg = Ledger()
    validated(lg, "r_a", "s1", "s2", content="the suite runs with uv run pytest", scope=None)
    lg.amend("cli", "close", "r_a", condition={"toolchain": "pip"})
    assert (delivered(lg, UV), delivered(lg, PIP)) == (("r_a",), ())
    assert rr(lg, "r_a")[0] == "L2"


def test_defeating_an_evidence_link_takes_its_support_away():
    lg = Ledger()
    validated(lg, "r_a", "s1", "s2", content="calc.py has four functions")
    link = next(e for e in reversed(lg.events) if e.kind.value == "evidence_linked")
    lg.amend("cli", "defeat", "r_a", link=link.id)
    assert rr(lg, "r_a") == ("L1", "independent grounding below threshold")


# Pending restrictions (DESIGN_V2 6.1)


def restricted(lg: Ledger, target_affirmed: bool = False) -> None:
    validated(lg, "r_a", "s1", "s2", content="calc.divide raises ValueError")
    if target_affirmed:
        lg.confirm("cli", "r_a")
    lg.start("s3", **UV)
    lg.propose("s3", "r_q", content="only when b is zero", refines=("r_a",))
    lg.link("r_q", lg.test("s3", True))
    lg.end("s3")


def test_a_pending_restriction_withholds_its_target_until_it_is_validated_or_withdrawn():
    lg = Ledger()
    restricted(lg)
    lab = labels_of(lg).by_id["r_a"]
    assert (lab.rung.value, lab.withheld_by, lab.deliverable) == ("L2", ("r_q",), False)
    assert delivered(lg) == ()
    lg.reject("cli", "r_q")
    assert delivered(lg) == ("r_a",)


def test_a_validated_restriction_is_delivered_with_its_target():
    lg = Ledger()
    restricted(lg)
    lg.start("s4", **UV)
    lg.link("r_q", lg.test("s4", True))
    lg.end("s4")
    assert delivered(lg) == ("r_a", "r_q")


def test_a_restriction_never_withholds_what_the_user_affirmed_and_one_without_evidence_withholds_nothing():
    lg = Ledger()
    restricted(lg, target_affirmed=True)
    assert delivered(lg) == ("r_a",)
    lg = Ledger()
    validated(lg, "r_a", "s1", "s2", content="calc.divide raises ValueError")
    lg.start("s3", **UV)
    lg.propose("s3", "r_q", content="only when b is zero", refines=("r_a",))
    lg.end("s3")
    assert delivered(lg) == ("r_a",)


# Authority (DESIGN_V2 7.2)


def test_a_miner_proposed_version_never_retires_what_it_supersedes():
    lg = Ledger()
    stated_rule(lg, "r_rule")
    lg.start("s1", **UV)
    lg.propose("s1", "r_model", kind=RecordKind.PREFERENCE, content="No closing line", supersedes="r_rule", mode="revision")
    lg.confirm("s1", "r_model")
    lg.end("s1")
    assert rr(lg, "r_rule") == ("L2", "the user stated it as a standing rule")
    assert set(delivered(lg)) == {"r_rule", "r_model"}


def test_only_the_user_may_author_an_amendment():
    import pytest

    from rka_memory.domain import Actor, Event, Kind

    for actor in (Actor.MODEL, Actor.RUNNER, Actor.TOOL):
        with pytest.raises(ValueError, match="may not author"):
            Event.new(Kind.USER_AMEND, actor, "s1", {"op": "close", "target": "r_a", "condition": {"toolchain": "uv"}})


# Decisions


def decided(lg: Ledger) -> None:
    """The agent decides to use SQLite in s1, where it also learns a fact
    without declaring an edge. Two procedures implement the decision; the
    user confirms one of them. A neighbour fact is supported where the
    decision was never in force."""
    lg.start("s1", **UV)
    lg.decide("s1", "d_db", "Store results in SQLite", alternatives=("Postgres",), reason="no server to run")
    lg.propose("s1", "r_learned", content="results live in results.db")
    lg.link("r_learned", lg.test("s1", True))
    lg.propose("s1", "r_proc", kind=RecordKind.PROCEDURE, content="Run migrations with sqlite3", implements=("d_db",))
    lg.link("r_proc", lg.test("s1", True))
    lg.propose("s1", "r_kept", kind=RecordKind.PROCEDURE, content="Back up results.db before a migration", implements=("d_db",))
    lg.write("s1", "results.db")
    lg.end("s1")
    lg.confirm("cli", "d_db")
    lg.confirm("cli", "r_kept")
    lg.start("s2", **UV)
    lg.deliver("s2", "d_db", "r_kept")
    lg.link("r_learned", lg.test("s2", True))
    lg.end("s2")
    validated(lg, "r_nbr", "s3", "s4", content="calc.py has four functions")


def test_a_decision_is_deliverable_only_once_the_user_affirms_it():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.decide("s1", "d_db", "Store results in SQLite", alternatives=("Postgres",), reason="no server to run")
    lg.end("s1")
    assert rr(lg, "d_db") == ("L0D", "no counted non-model evidence")
    lg.confirm("cli", "d_db")
    assert rr(lg, "d_db") == ("L2", "user confirmed")
    world = project(lg.events)
    view = packet(world, label(world, cfg(), lg.clock), cfg(), State(**UV), arm="gated", channel="session_start")
    assert view.render().splitlines()[2:] == ["- [d_db] Decided: Store results in SQLite. Instead of: Postgres. Because: no server to run."]


def test_retracting_a_decision_withdraws_what_implements_it_and_withholds_what_was_learned_under_it():
    lg = Ledger()
    decided(lg)
    assert [rr(lg, r)[0] for r in ("d_db", "r_proc", "r_kept", "r_learned", "r_nbr")] == ["L2", "L1", "L2", "L2", "L2"]
    lg.reject("cli", "d_db")
    labs = labels_of(lg)
    assert labs.by_id["d_db"].reason == "withdrawn by the user"
    assert (labs.by_id["r_proc"].rung.value, labs.by_id["r_proc"].reason) == ("L4", "depends on a record the user withdrew")
    assert labs.by_id["r_kept"].rung.value == "L2"
    assert (labs.by_id["r_learned"].rung.value, labs.by_id["r_learned"].learned_under) == ("L1", ("d_db",))
    assert labs.by_id["r_nbr"].rung.value == "L2"


def test_the_impact_preview_names_what_a_retraction_would_withdraw_withhold_and_keep():
    lg = Ledger()
    decided(lg)
    report = impact(project(lg.events), cfg(), lg.clock, "d_db")
    assert report == {
        "record_id": "d_db",
        "withdrawn": ["d_db", "r_proc"],
        "withheld": [{"record_id": "r_learned", "from": "L2", "to": "L1", "learned_under": ["d_db"]}],
        "kept": [{"record_id": "r_kept", "reason": "the user affirmed it"}],
        "files_written_while_in_force": ["results.db"],
        "skills": [],
    }
    assert labels_of(lg).by_id["d_db"].rung.value == "L2"


def test_model_proposals_never_enlarge_the_deliverable_set_with_restrictions_and_versions():
    changed = 0
    for seed in range(200):
        rng = random.Random(seed)
        lg = Ledger()
        base: list[str] = []
        model: set[str] = set()
        model_events: set[str] = set()
        for s in range(8):
            sid = f"s{s}"
            lg.start(sid, **UV)
            if base and rng.random() < 0.5:
                lg.deliver(sid, *rng.sample(base, k=rng.randint(1, len(base))))
            if len(base) < 4 and rng.random() < 0.7:
                rid = f"r{len(base)}"
                lg.propose(sid, rid)
                base.append(rid)
            for rid in rng.sample(base, k=min(len(base), 2)):
                lg.link(rid, lg.test(sid, True))
            if base and rng.random() < 0.5:
                mid = f"m{len(model)}"
                edge = {"refines": (rng.choice(base),)} if rng.random() < 0.5 else {"supersedes": rng.choice(base), "mode": "revision"}
                model_events.add(lg.propose(sid, mid, **edge).id)
                model_events.add(lg.link(mid, lg.test(sid, True)).id)
                model.add(mid)
            lg.end(sid)
        full = label(project(lg.events), cfg(), lg.clock)
        ablated_events = [e for e in lg.events if e.id not in model_events]
        ablated = label(project(ablated_events), cfg(), lg.clock)
        with_model = set(full.deliverable()) - model
        without = set(ablated.deliverable())
        assert with_model <= without, (seed, with_model - without)
        changed += with_model != without
    assert changed > 0
