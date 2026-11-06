import asyncio
from dataclasses import replace

import pytest

from ledger_builder import Ledger
from rka_memory.domain import Actor, FieldConstraint, Kind, Scope, State
from rka_memory.mcp import server

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")


@pytest.fixture
def gated(memory):
    memory.cfg = replace(memory.cfg, arms=("gated",))
    lg = Ledger()
    lg.start("s1", **UV)
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_val", scope=Scope(project=FieldConstraint(frozenset({"/p/app"}))), content="Use uv run pytest")
    lg.link("r_val", ok1)
    lg.start("s2", **UV)
    lg.link("r_val", lg.exit("s2", 0))
    ok3 = lg.test("s2", True)
    lg.propose("s2", "r_hint", content="Ruff may be configured")
    lg.link("r_hint", ok3)
    memory.append(*lg.events)
    memory.gate(now=lg.clock)
    return memory


def ledger_kinds(memory):
    return [e.kind for e in memory.ledger.read()]


def test_memory_confirm_logs_a_model_judgment_and_never_a_user_confirm(gated):
    before = gated.world().records["r_hint"].rung

    out = server.request_verdict(gated, "r_hint", "confirm")
    gated.gate()

    judgment = gated.world().events[out["event_id"]]
    assert (judgment.kind, judgment.actor) == (Kind.JUDGMENT, Actor.MODEL)
    assert judgment.payload == {
        "target": "verdict_request",
        "record_id": "r_hint",
        "verdict": "confirm",
        "reason": "",
        "judge": "model",
    }
    assert "`rkm confirm r_hint`" in out["message"]
    assert Kind.USER_CONFIRM not in ledger_kinds(gated)
    assert gated.world().records["r_hint"].rung == before


def test_memory_reject_asks_the_user_and_leaves_the_record_valid(gated):
    out = server.request_verdict(gated, "r_val", "reject", "wrong runner")
    gated.gate()

    assert "`rkm reject r_val wrong runner`" in out["message"]
    assert Kind.USER_REJECT not in ledger_kinds(gated)
    assert gated.world().records["r_val"].rung.value == "L2"
    assert server.request_verdict(gated, "r_nope", "confirm") == {"error": "unknown record r_nope"}


def test_memory_packet_delivers_valid_records_and_logs_the_delivery(gated):
    out = server.packet(gated, state=UV, session_id="s9")

    assert [r["record_id"] for r in out["records"]] == ["r_val"]
    assert (out["arm"], out["omitted_count"]) == ("gated", 0)
    assert "Use uv run pytest" in out["text"]
    delivery = gated.world().deliveries[-1]
    assert (delivery.event.session_id, delivery.channel, delivery.record_ids) == ("s9", "mcp", ("r_val",))


def test_memory_packet_defaults_to_the_detected_state(gated):
    elsewhere = server.packet(gated, detect=lambda m: State(**{**UV, "project": "/p/other"}))
    here = server.packet(gated, detect=lambda m: State(**UV))

    assert (elsewhere["records"], [r["record_id"] for r in here["records"]]) == ([], ["r_val"])
    assert gated.world().deliveries[-1].event.session_id == "mcp"


def test_memory_hints_returns_l1_records_tagged_unvalidated(gated):
    out = server.hints(gated, state=UV, session_id="s9")

    assert [(r["record_id"], r["tag"], r["unvalidated"]) for r in out["records"]] == [("r_hint", "unvalidated", True)]
    assert gated.world().deliveries[-1].channel == "hints"


def test_memory_explain_and_state(gated):
    assert server.explain(gated, "r_val")["rung"] == "L2"
    assert server.explain(gated, "r_nope") == {"error": "unknown record r_nope"}
    report = server.state_report(gated, "s1", detect=lambda m: State(project="/p/here"))
    assert report == {
        "detected": {
            "task_class": None,
            "project": "/p/here",
            "toolchain": None,
            "model_id": None,
            "user_context": None,
        },
        "session": UV,
    }


def test_server_registers_exactly_the_six_memory_tools():
    tools = asyncio.run(server.build_server().list_tools())
    assert sorted(t.name for t in tools) == [
        "memory_confirm",
        "memory_explain",
        "memory_hints",
        "memory_packet",
        "memory_reject",
        "memory_state",
    ]
