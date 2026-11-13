"""The v2 labelling function (docs/DESIGN_V2.md Section 5) on hand-built ledgers."""

import random

from ledger_builder import Ledger, cfg
from rka_memory.domain import RecordKind, Scope, State
from rka_memory.kernel.labels import label
from rka_memory.world import project

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
STATEMENT = {"quote": "End every reply with a Summary line", "marker": "From now on"}


def labels_of(lg: Ledger):
    return label(project(lg.events), cfg(), lg.clock)


def rungs(lg: Ledger) -> dict[str, str]:
    return {rid: lab.rung.value for rid, lab in labels_of(lg).by_id.items()}


def supported_in(lg: Ledger, record_id: str, session: str, deliver: tuple[str, ...] = ()) -> None:
    lg.start(session, **UV)
    if deliver:
        lg.deliver(session, *deliver)
    lg.link(record_id, lg.test(session, True))
    lg.end(session)


def test_a_fact_grounded_before_any_delivery_reaches_l2():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_fact", content="the suite runs with pytest -q")
    lg.link("r_fact", lg.test("s1", True))
    lg.end("s1")
    supported_in(lg, "r_fact", "s2")
    assert rungs(lg) == {"r_fact": "L2"}


def test_support_from_sessions_that_received_the_fact_is_not_independent():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_fact")
    lg.link("r_fact", lg.test("s1", True))
    lg.end("s1")
    supported_in(lg, "r_fact", "s2", deliver=("r_fact",))
    supported_in(lg, "r_fact", "s3", deliver=("r_fact",))
    lab = labels_of(lg).by_id["r_fact"]
    assert lab.rung.value == "L1"
    assert [why for _, why in lab.set_aside] == ["not independent: the record or a dependent was in context"] * 2


def test_support_produced_while_a_dependent_or_another_version_was_in_context_is_not_independent():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_base")
    lg.link("r_base", lg.test("s1", True))
    lg.propose("s1", "r_dep", depends_on=("r_base",))
    lg.propose("s1", "r_new", supersedes="r_base")
    lg.end("s1")
    supported_in(lg, "r_base", "s2", deliver=("r_dep",))
    supported_in(lg, "r_new", "s3", deliver=("r_base",))
    supported_in(lg, "r_new", "s4")
    labs = labels_of(lg).by_id
    assert labs["r_base"].rung.value == "L1"
    assert labs["r_new"].rung.value == "L1"


def test_a_prescriptive_record_needs_the_users_affirmation():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_proc", kind=RecordKind.PROCEDURE, steps=(("Bash", "uv sync"),), acceptance_check="uv run pytest")
    lg.link("r_proc", lg.test("s1", True))
    lg.end("s1")
    for s in ("s2", "s3", "s4"):
        supported_in(lg, "r_proc", s)
    assert rungs(lg) == {"r_proc": "L1"}
    lg.confirm("cli", "r_proc")
    assert rungs(lg) == {"r_proc": "L2"}


def test_a_confirmed_procedure_followed_with_its_check_passing_becomes_a_skill():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_proc", kind=RecordKind.PROCEDURE, steps=(("Bash", "uv sync"),), acceptance_check="uv run pytest")
    lg.confirm("cli", "r_proc")
    lg.end("s1")
    for s in ("s2", "s3"):
        lg.start(s, **UV)
        lg.tool_call(s, "Bash", "uv sync")
        lg.test(s, True, "uv run pytest")
        lg.end(s)
    assert rungs(lg) == {"r_proc": "L3"}


def test_a_user_standing_rule_is_affirmed_by_its_quoted_words():
    lg = Ledger()
    lg.start("s1", **UV)
    msg = lg.user_msg("s1", "From now on: End every reply with a Summary line.")
    lg.propose("s1", "r_rule", kind=RecordKind.PREFERENCE, content="End every reply with a Summary line")
    lg.link("r_rule", msg, statement=STATEMENT)
    lg.end("s1")
    lab = labels_of(lg).by_id["r_rule"]
    assert (lab.rung.value, lab.reason, lab.affirmed) == ("L2", "the user stated it as a standing rule", True)


def test_a_users_reject_withdraws_the_record_and_its_dependents_but_not_an_affirmed_one():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_root")
    lg.link("r_root", lg.test("s1", True))
    lg.propose("s1", "r_dep", depends_on=("r_root",))
    lg.link("r_dep", lg.test("s1", True))
    lg.propose("s1", "r_kept", depends_on=("r_root",))
    lg.link("r_kept", lg.test("s1", True))
    lg.end("s1")
    lg.confirm("cli", "r_kept")
    lg.reject("cli", "r_root")
    labs = labels_of(lg)
    assert labs.withdrawn == {"r_root", "r_dep"}
    assert [labs.by_id[r].rung.value for r in ("r_root", "r_dep", "r_kept")] == ["L4", "L4", "L2"]
    lg.confirm("cli", "r_root")
    assert labels_of(lg).withdrawn == frozenset()


def test_a_model_linked_counter_demotes_its_target_and_never_cascades():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_root")
    lg.link("r_root", lg.test("s1", True))
    lg.propose("s1", "r_dep", depends_on=("r_root",))
    lg.link("r_dep", lg.test("s1", True))
    lg.end("s1")
    supported_in(lg, "r_root", "s2")
    supported_in(lg, "r_dep", "s2")
    lg.start("s3", **UV)
    lg.link("r_root", lg.user_msg("s3", "there is no such thing"), "counter")
    lg.end("s3")
    labs = labels_of(lg)
    assert labs.withdrawn == frozenset()
    assert labs.by_id["r_root"].rung.value == "L1"
    # r_dep was grounded where r_root was never delivered, so it keeps L2.
    assert labs.by_id["r_dep"].rung.value == "L2"


def test_a_miner_linked_standing_statement_demotes_but_never_withdraws():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_root")
    lg.link("r_root", lg.test("s1", True))
    lg.propose("s1", "r_dep", depends_on=("r_root",))
    lg.link("r_dep", lg.test("s1", True))
    lg.end("s1")
    supported_in(lg, "r_root", "s2")
    supported_in(lg, "r_dep", "s2")
    lg.start("s3", **UV)
    msg = lg.user_msg("s3", "From now on: never use the old suite.")
    lg.link("r_root", msg, "counter", statement={"quote": "never use the old suite", "marker": "From now on"})
    lg.end("s3")
    labs = labels_of(lg)
    assert labs.withdrawn == frozenset()
    assert [labs.by_id[r].rung.value for r in ("r_root", "r_dep")] == ["L1", "L2"]


def test_a_record_learned_under_a_withdrawn_one_needs_support_from_outside_its_influence():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_root")
    lg.link("r_root", lg.test("s1", True))
    lg.end("s1")
    lg.start("s2", **UV)
    lg.deliver("s2", "r_root")
    lg.propose("s2", "r_learned")
    lg.link("r_learned", lg.test("s2", True))
    lg.end("s2")
    supported_in(lg, "r_learned", "s3", deliver=("r_root",))
    lg.reject("cli", "r_root")
    lab = labels_of(lg).by_id["r_learned"]
    assert lab.learned_under == ("r_root",)
    assert lab.rung.value == "L1"
    supported_in(lg, "r_learned", "s4")
    supported_in(lg, "r_learned", "s5")
    assert labels_of(lg).by_id["r_learned"].rung.value == "L2"


def test_labels_ignore_v1_rung_events_and_are_deterministic():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_fact")
    lg.link("r_fact", lg.test("s1", True))
    lg.end("s1")
    supported_in(lg, "r_fact", "s2")
    before = labels_of(lg)
    lg.gate()
    after = labels_of(lg)
    assert before.by_id == after.by_id == labels_of(lg).by_id


def random_ledger(rng: random.Random, sessions: int = 8, records: int = 6) -> Ledger:
    lg = Ledger()
    proposed: list[str] = []
    kinds = [RecordKind.FACT, RecordKind.FACT, RecordKind.CONSTRAINT, RecordKind.PROCEDURE]
    for s in range(sessions):
        sid = f"s{s}"
        lg.start(sid, **UV)
        if proposed and rng.random() < 0.6:
            lg.deliver(sid, *rng.sample(proposed, k=rng.randint(1, len(proposed))))
        if len(proposed) < records and rng.random() < 0.7:
            rid = f"r{len(proposed)}"
            lg.propose(sid, rid, kind=rng.choice(kinds))
            proposed.append(rid)
        for rid in rng.sample(proposed, k=min(len(proposed), rng.randint(0, 3))):
            lg.link(rid, lg.test(sid, rng.random() < 0.85), "support" if rng.random() < 0.85 else "counter")
        if proposed and rng.random() < 0.15:
            lg.confirm(sid, rng.choice(proposed))
        lg.end(sid)
    return lg


def with_model_proposals(rng: random.Random, sessions: int = 10):
    """A ledger in which a miner also proposed records that depend on earlier
    ones and linked counters, and the same ledger without those proposals.
    Deliveries of a proposed record are the runner's; they stay in the
    ablated ledger with that record's id removed."""
    lg = Ledger()
    model_ids: set[str] = set()
    model_events: set[str] = set()
    proposed: list[str] = []
    kinds = [RecordKind.FACT, RecordKind.FACT, RecordKind.CONSTRAINT]
    for s in range(sessions):
        sid = f"s{s}"
        lg.start(sid, **UV)
        if proposed and rng.random() < 0.6:
            lg.deliver(sid, *rng.sample(proposed, k=rng.randint(1, len(proposed))))
        if len(proposed) < 6 and rng.random() < 0.6:
            rid = f"r{len(proposed)}"
            lg.propose(sid, rid, kind=rng.choice(kinds))
            proposed.append(rid)
        base = [r for r in proposed if r not in model_ids]
        if base and rng.random() < 0.4:
            rid = f"m{len(model_ids)}"
            ev = lg.propose(sid, rid, depends_on=tuple(rng.sample(base, k=min(2, len(base)))))
            model_ids.add(rid)
            model_events.add(ev.id)
            proposed.append(rid)
        for rid in rng.sample(proposed, k=min(len(proposed), rng.randint(0, 3))):
            ev = lg.link(rid, lg.test(sid, True))
            if rid in model_ids:
                model_events.add(ev.id)
        if base and rng.random() < 0.3:
            model_events.add(lg.link(rng.choice(base), lg.test(sid, False), "counter").id)
        lg.end(sid)
    ablated = []
    for ev in lg.events:
        if ev.id in model_events:
            continue
        if ev.kind.value == "packet_delivered":
            kept = [r for r in ev.payload["record_ids"] if r not in model_ids]
            ev = type(ev)(**{**ev.__dict__, "payload": {**ev.payload, "record_ids": kept}})
        ablated.append(ev)
    return lg.events, ablated, model_ids, lg.clock


def test_model_proposals_never_enlarge_the_deliverable_set():
    changed = 0
    for seed in range(200):
        full, ablated, model_ids, now = with_model_proposals(random.Random(seed))
        with_proposals = set(label(project(full), cfg(), now).deliverable()) - model_ids
        without = set(label(project(ablated), cfg(), now).deliverable())
        assert with_proposals <= without, (seed, with_proposals - without)
        changed += without != with_proposals
    assert changed > 0, "the proposals never changed a label; the test would pass vacuously"


def test_no_withdrawn_record_is_deliverable():
    for seed in range(60):
        rng = random.Random(seed)
        lg = random_ledger(rng)
        world = project(lg.events)
        if world.records:
            lg.reject("cli", sorted(world.records)[0])
        labs = labels_of(lg)
        assert not (labs.withdrawn & set(labs.deliverable())), seed
