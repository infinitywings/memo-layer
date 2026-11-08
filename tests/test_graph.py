import json

from ledger_builder import Ledger, cfg
from rka_memory import cli, graph
from rka_memory.domain import RecordKind

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
RULE = "run the tests as `pytest -q -p no:cacheprovider`"


def house_rules(lg: Ledger):
    """s1: the user states a rule and a test passes; a procedure depends on the rule.
    s2: both are delivered, and a newer procedure version supersedes the first."""
    lg.start("s1", **UV)
    msg = lg.user_msg("s1", f"House rule from now on: {RULE}.")
    ok = lg.test("s1", True)
    lg.propose("s1", "r_rule", kind=RecordKind.CONSTRAINT, content=RULE)
    lg.link("r_rule", msg, statement={"quote": RULE, "marker": "from now on"})
    lg.propose("s1", "r_proc", kind=RecordKind.PROCEDURE, content="Add a function, then run the tests", depends_on=("r_rule",))
    lg.link("r_proc", ok)
    lg.end("s1")
    lg.gate()
    lg.start("s2", **UV)
    delivery = lg.deliver("s2", "r_rule", "r_proc")
    lg.propose("s2", "r_proc2", kind=RecordKind.PROCEDURE, content="Add a function and its test, then run the tests", supersedes="r_proc")
    lg.end("s2")
    return msg, ok, delivery


def triples(g: graph.Graph) -> set[tuple[str, str, str]]:
    return {(e.type.value, e.source, e.target) for e in g.edges}


def test_the_graph_holds_evidence_relations_and_deliveries_as_typed_edges():
    lg = Ledger()
    msg, ok, delivery = house_rules(lg)
    g = graph.project(lg.gate(), cfg())

    assert triples(g) == {
        ("evidence", msg.id, "r_rule"),
        ("evidence", ok.id, "r_proc"),
        ("depends_on", "r_proc", "r_rule"),
        ("supersedes", "r_proc2", "r_proc"),
        ("delivered", delivery.id, "r_rule"),
        ("delivered", delivery.id, "r_proc"),
        ("touches", ok.id, "command:pytest"),
    }
    (stated,) = [e for e in g.edges if e.source == msg.id]
    assert {k: stated.attrs[k] for k in ("polarity", "grounds", "weight", "confirms", "statement")} == {
        "polarity": "support",
        "grounds": True,
        "weight": 1.0,
        "confirms": True,
        "statement": {"quote": RULE, "standing": True},
    }
    rule = next(n for n in g.nodes if n.id == "r_rule")
    assert (rule.type.value, rule.attrs["rung"], rule.attrs["kind"]) == ("record", "L2", "constraint")
    assert {n.id for n in g.around("r_rule").nodes} == {"r_rule", msg.id, "r_proc", delivery.id}


def test_every_delivery_traces_to_evidence_linked_before_it_or_is_a_gap():
    lg = Ledger()
    msg, ok, delivery = house_rules(lg)
    lg.start("s3", **UV)
    lg.propose("s3", "r_late", content="Linked only after its delivery")
    late = lg.deliver("s3", "r_late")
    lg.link("r_late", lg.test("s3", True))
    lg.end("s3")
    world = lg.gate()

    assert [(t.delivery_id, t.record_id, t.evidence) for t in graph.traces(world, cfg())] == [
        (delivery.id, "r_rule", (msg.id,)),
        (delivery.id, "r_proc", (ok.id,)),
        (late.id, "r_late", ()),
    ]
    assert [t.record_id for t in graph.traces(world, cfg(), "s2")] == ["r_rule", "r_proc"]


def test_explain_shows_relations_and_the_users_quoted_words(memory):
    lg = Ledger()
    msg, _, delivery = house_rules(lg)
    memory.append(*lg.events)

    proc = memory.explain("r_proc")
    assert proc["relations"] == {
        "depends_on": [{"record_id": "r_rule", "rung": "L2"}],
        "dependents": [],
        "supersedes": None,
        "superseded_by": [{"record_id": "r_proc2", "rung": None}],
        "refines": [],
        "refined_by": [],
    }
    assert [d["event_id"] for d in proc["deliveries"]] == [delivery.id]
    rule = memory.explain("r_rule")
    assert rule["relations"]["dependents"] == [{"record_id": "r_proc", "rung": "L1"}]
    assert [(link["event_id"], link["statement"]) for link in rule["links"]] == [
        (msg.id, {"quote": RULE, "standing": True})
    ]


def test_graph_command_exports_a_neighborhood_and_trace_fails_on_a_gap(memory, capsys):
    lg = Ledger()
    house_rules(lg)
    memory.append(*lg.events)

    assert cli.main(["graph", "--record", "r_proc2"]) == 0
    near = json.loads(capsys.readouterr().out)
    assert {n["id"] for n in near["nodes"]} == {"r_proc2", "r_proc"}
    assert cli.main(["graph", "--trace"]) == 0
    assert json.loads(capsys.readouterr().out) == {"delivered": 2, "traced": 2, "gaps": []}

    lg.start("s3", **UV)
    lg.propose("s3", "r_bare", content="Never linked")
    lg.deliver("s3", "r_bare")
    memory.append(*lg.events[-3:])
    assert cli.main(["graph", "--trace"]) == 1
    assert [gap["record_id"] for gap in json.loads(capsys.readouterr().out)["gaps"]] == ["r_bare"]
