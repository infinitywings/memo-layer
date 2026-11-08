"""Phase 4 of docs/GRAPH.md: a qualification travels with the record it
refines, and a record about a file the project no longer has says so."""

from ledger_builder import Ledger, cfg
from rka_memory import graph
from rka_memory.domain import State
from rka_memory.miner.miner import Drop, mine_session
from rka_memory.packet import builder
from test_miner import FakeBackend, sync

APP = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")


def promote(lg: Ledger, record_id: str, content: str, **spec) -> None:
    lg.start(f"{record_id}-a", **APP)
    ok = lg.test(f"{record_id}-a", True)
    lg.propose(f"{record_id}-a", record_id, content=content, **spec)
    lg.link(record_id, ok)
    lg.start(f"{record_id}-b", **APP)
    lg.link(record_id, lg.exit(f"{record_id}-b", 0))


def test_a_qualification_is_delivered_right_after_the_record_it_refines():
    lg = Ledger()
    promote(lg, "r_rule", "Run the tests with pytest -q")
    promote(lg, "r_other", "calc.py holds the arithmetic helpers")
    promote(lg, "r_qual", "pytest -q still leaves __pycache__ behind", refines=("r_rule",))
    world = lg.gate()

    pkt = builder.build(world, cfg(), State(**APP), arm="gated", channel="prompt")
    assert pkt.record_ids == ["r_rule", "r_qual", "r_other"]
    assert ("refines", "r_qual", "r_rule") in {(e.type.value, e.source, e.target) for e in graph.project(world, cfg()).edges}


def test_a_record_about_a_file_the_project_no_longer_has_says_so(tmp_path):
    (tmp_path / "calc.py").write_text("")
    st = dict(APP, project=str(tmp_path))
    lg = Ledger()
    for rid, about in (("r_calc", ("file:calc.py",)), ("r_helpers", ("file:helpers.py", "command:pytest"))):
        lg.start(f"{rid}-a", **st)
        ok = lg.test(f"{rid}-a", True)
        lg.propose(f"{rid}-a", rid, content=f"content of {rid}", about=about)
        lg.link(rid, ok)
        lg.start(f"{rid}-b", **st)
        lg.link(rid, lg.exit(f"{rid}-b", 0))
    world = lg.gate()

    gone = builder.missing_files(world, State(**st))
    pkt = builder.build(world, cfg(), State(**st), arm="gated", channel="prompt", missing=gone)
    lines = {i.record_id: i.line() for i in pkt.items}
    assert gone == frozenset({"file:helpers.py"})
    assert "(helpers.py no longer exists)" in lines["r_helpers"]
    assert "no longer exists" not in lines["r_calc"]


def test_the_miner_may_refine_only_a_listed_record(memory):
    lg = Ledger()
    lg.start("s1", **APP)
    lg.propose("s1", "r_rule", content="Run the tests with pytest -q")
    lg.link("r_rule", lg.test("s1", True))
    lg.end("s1")
    lg.start("s2", **APP)
    ok = lg.test("s2", True)
    lg.end("s2")
    sync(memory, lg)
    memory.gate()
    reply = {
        "candidates": [
            {"content": "pytest -q still leaves __pycache__ behind", "kind": "fact", "evidence": [ok.id], "refines": ["r_rule", "r_nope"]}
        ]
    }
    result = mine_session(memory, "s2", backend=FakeBackend(reply))

    assert memory.world().records[result.proposed[0]["record_id"]].spec.refines == ("r_rule",)
    assert result.dropped == [Drop("candidate 0 refines r_nope", "refines a record that was not listed")]
