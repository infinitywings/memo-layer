import json

from ledger_builder import Ledger
from rka_memory import jobs


def test_one_round_gates_exports_and_writes_metrics_without_sync(memory, monkeypatch):
    monkeypatch.setenv("RKA_MEMORY_NOW", "2026-11-20T08:00:00+00:00")
    lg = Ledger()
    lg.start("s1", project="/p/app")
    ok = lg.test("s1", True)
    lg.propose("s1", "r_a")
    lg.link("r_a", ok)
    memory.append(*lg.events)

    report = jobs.run_round(memory)

    assert report.errors == {}
    assert report.steps["gate"] == 1
    assert sorted(report.steps) == ["export_skills", "gate", "metrics"]
    written = memory.cfg.ledger_dir / "metrics" / "2026-11-20.json"
    assert report.steps["metrics"] == str(written)
    assert json.loads(written.read_text())["records_by_rung"]["L1"] == 1
    assert memory.world().records["r_a"].rung.value == "L1"


def test_a_failing_step_is_reported_and_the_rest_still_run(memory, monkeypatch):
    from rka_memory.skills import export

    monkeypatch.setattr(export, "export_skills", lambda mem: (_ for _ in ()).throw(OSError("disk full")))
    report = jobs.run_round(memory)
    assert report.errors == {"export_skills": "OSError: disk full"}
    assert "metrics" in report.steps
