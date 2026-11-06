from datetime import date

from ledger_builder import Ledger, cfg
from rka_memory.domain import FieldConstraint, Scope, State
from rka_memory.packet import builder

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")


def promoted(lg: Ledger, record_id: str, content: str, **spec) -> None:
    lg.start(f"{record_id}-a", **UV)
    ok = lg.test(f"{record_id}-a", True)
    lg.propose(f"{record_id}-a", record_id, content=content, **spec)
    lg.link(record_id, ok)
    lg.start(f"{record_id}-b", **UV)
    lg.link(record_id, lg.exit(f"{record_id}-b", 0))


def test_packet_holds_only_valid_records_and_hints_hold_only_l1():
    lg = Ledger()
    promoted(lg, "r_val", "Use uv run pytest")
    lg.start("s9", **UV)
    ok = lg.test("s9", True)
    lg.propose("s9", "r_hint", content="Maybe ruff is configured")
    lg.link("r_hint", ok)
    lg.propose("s9", "r_l0d", content="Model said so")
    world = lg.gate()

    pkt = builder.build(world, cfg(), State(**UV), arm="gated", channel="session_start")
    assert pkt.record_ids == ["r_val"]
    assert "Use uv run pytest" in pkt.render()

    hints = builder.hints(world, cfg(), State(**UV))
    assert hints.record_ids == ["r_hint"]
    assert "unvalidated" in hints.render()


def test_packet_excludes_records_out_of_scope():
    lg = Ledger()
    promoted(lg, "r_val", "Use uv", scope=Scope(project=FieldConstraint(frozenset({"/p/app"}))))
    world = lg.gate()
    other = State(**{**UV, "project": "/p/other"})
    assert builder.build(world, cfg(), other, arm="gated", channel="session_start").record_ids == []


def test_dependency_groups_are_kept_whole_or_dropped_whole():
    lg = Ledger()
    promoted(lg, "r_base", "B" * 400)
    promoted(lg, "r_dep", "D" * 400, depends_on=("r_base",))
    promoted(lg, "r_small", "small fact")
    world = lg.gate()
    roomy = builder.build(world, cfg(), State(**UV), arm="gated", channel="session_start", budget_tokens=2000)
    assert set(roomy.record_ids) == {"r_base", "r_dep", "r_small"}
    assert roomy.record_ids.index("r_base") < roomy.record_ids.index("r_dep")
    tight = builder.build(world, cfg(), State(**UV), arm="gated", channel="session_start", budget_tokens=250)
    assert tight.record_ids == ["r_small"]
    assert tight.omitted_count == 2


def test_arms_rotate_by_day_and_none_arm_is_empty():
    c = cfg(packet={"arms": ["gated", "none"]})
    assert {builder.arm_for(date(2026, 11, 6), c), builder.arm_for(date(2026, 11, 7), c)} == {"gated", "none"}
    lg = Ledger()
    promoted(lg, "r_val", "x")
    world = lg.gate()
    assert builder.build(world, c, State(**UV), arm="none", channel="session_start").record_ids == []


def test_ungated_baseline_delivers_l1_records_too():
    lg = Ledger()
    lg.start("s1", **UV)
    ok = lg.test("s1", True)
    lg.propose("s1", "r_l1")
    lg.link("r_l1", ok)
    world = lg.gate()
    assert builder.build(world, cfg(), State(**UV), arm="ungated", channel="session_start").record_ids == ["r_l1"]
    assert builder.build(world, cfg(), State(**UV), arm="gated", channel="session_start").record_ids == []
