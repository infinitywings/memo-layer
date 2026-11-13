import json
from datetime import UTC, datetime

from ledger_builder import Ledger, cfg
from rka_memory.audit import metrics
from rka_memory.domain import Actor, FieldConstraint, Kind, Scope, State
from rka_memory.world import project

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
OTHER = dict(UV, project="/p/other")
NOTES = dict(UV, project="/p/notes")


def packet(lg, session, arm, *record_ids, channel="session_start"):
    payload = {"record_ids": list(record_ids), "arm": arm, "channel": channel}
    return lg._ev(Kind.PACKET_DELIVERED, Actor.RUNNER, session, payload)


def end(lg, session, tokens_before_first_write, first_write_seen=True):
    payload = {"tokens_before_first_write": tokens_before_first_write, "first_write_seen": first_write_seen}
    return lg._ev(Kind.SESSION_END, Actor.RUNNER, session, payload)


def cost(lg, component, session, tokens, seconds):
    payload = {"component": component, "session_id": session, "tokens": tokens, "seconds": seconds}
    return lg._ev(Kind.COST, Actor.RUNNER, session, payload)


def fixture_ledger():
    lg = Ledger()
    lg.start("s1", **UV)
    ok1 = lg.test("s1", True)
    lg.propose("s1", "r_a", scope=Scope.exact(State(**UV)))
    lg.link("r_a", ok1)
    lg.end("s1")
    cost(lg, "miner", "s1", 1200, 3.5)
    lg.start("s2", **UV)
    lg.link("r_a", lg.exit("s2", 0))
    lg.end("s2")
    cost(lg, "miner", "s2", 800, 2.5)
    cost(lg, "gate", "s2", 0, 0.25)
    lg.gate()

    lg.propose("s2", "r_b", scope=Scope(project=FieldConstraint(frozenset({"/p/other"}))))
    lg.confirm("cli", "r_b")
    lg.gate()

    lg.start("s5", **NOTES)
    packet(lg, "s5", "none")
    packet(lg, "s5", "none", "r_a", channel="hints")
    end(lg, "s5", 3000)

    lg.start("s3", **UV)
    packet(lg, "s3", "gated", "r_a")
    failed = lg.test("s3", False)
    end(lg, "s3", 1000)
    lg.link("r_a", failed, "counter")
    lg.gate()

    lg.start("s4", **OTHER)
    packet(lg, "s4", "gated", "r_b")
    lg.test("s4", True)
    end(lg, "s4", 2000)

    lg.start("s6", **NOTES)
    packet(lg, "s6", "none")
    end(lg, "s6", 5000)

    lg.start("s7", **UV)
    packet(lg, "s7", "gated")
    end(lg, "s7", 9999, first_write_seen=False)
    return lg, failed


def test_metrics_from_a_fixture_ledger():
    lg, failed = fixture_ledger()
    world = project(lg.events)
    report = metrics.compute(world, cfg(), datetime(2026, 11, 6, 23, 0, tzinfo=UTC))

    assert [m.to_rung.value for m in world.records["r_a"].history] == ["L1", "L2", "L1"]
    assert report["misapplication_rate"] == {
        "overall": {"sessions": 2, "misapplied": 1, "rate": 0.5},
        "by_arm": {
            "gated": {"sessions": 2, "misapplied": 1, "rate": 0.5},
            "none": {"sessions": 0, "misapplied": 0, "rate": None},
            "stated": {"sessions": 0, "misapplied": 0, "rate": None},
            "ungated": {"sessions": 0, "misapplied": 0, "rate": None},
        },
    }
    promotion = world.records["r_a"].history[1].event_id
    assert report["regression_count"] == {
        "count": 1,
        "details": [
            {
                "record_id": "r_a",
                "promotion_event_id": promotion,
                "to": "L2",
                "project": "/p/app",
                "task_class": "coding",
                "succeeded_session": "s2",
                "failed_session": "s3",
                "failed_event_id": failed.id,
            }
        ],
    }
    assert report["rediscovery_tokens"] == {
        "gated": {"count": 2, "mean": 1500.0, "median": 1500.0},
        "none": {"count": 2, "mean": 4000.0, "median": 4000.0},
        "stated": {"count": 0, "mean": None, "median": None},
        "ungated": {"count": 0, "mean": None, "median": None},
    }
    assert report["validation_cost"] == {
        "sessions": {
            "s1": {"miner_tokens": 1200, "miner_seconds": 3.5, "gate_seconds": 0},
            "s2": {"miner_tokens": 800, "miner_seconds": 2.5, "gate_seconds": 0.25},
        },
        "totals": {"miner_tokens": 2000, "miner_seconds": 6.0, "gate_seconds": 0.25},
    }
    assert report["records_by_rung"] == {"L0D": 0, "L1": 1, "L2": 1, "L3": 0, "L4": 0, "pending": 0}
    assert report["ledger_revision"] == len(lg.events)


def test_daily_report_is_written_under_the_ledger_dir(memory):
    world = project(fixture_ledger()[0].events)
    report = metrics.compute(world, memory.cfg, datetime(2026, 11, 6, 23, 0, tzinfo=UTC))

    path = metrics.write_daily(report, memory.cfg)

    assert path == memory.cfg.ledger_dir / "metrics" / "2026-11-06.json"
    assert json.loads(path.read_text()) == report


def test_a_contradicted_l1_record_counts_as_shown_wrong_though_it_is_never_demoted():
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_l1", content="Keep calc.py at the project root")
    lg.link("r_l1", lg.test("s1", True))
    lg.end("s1")
    lg.gate()
    lg.start("s2", **UV)
    packet(lg, "s2", "ungated", "r_l1")
    lg.link("r_l1", lg.test("s2", False), "counter")
    lg.end("s2")
    lg.start("s3", **UV)
    packet(lg, "s3", "ungated", "r_l1")
    lg.end("s3")
    world = lg.gate()

    assert world.records["r_l1"].rung.value == "L1"
    assert metrics.misapplication(world)["by_arm"]["ungated"] == {"sessions": 2, "misapplied": 0, "rate": 0.0}
    assert metrics.contradiction(world, cfg())["by_arm"]["ungated"] == {"sessions": 2, "misapplied": 1, "rate": 0.5}
