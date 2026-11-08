"""Relations a real miner left out: rules that govern a procedure, derived in
code, and refinements of a rule the user stated in the same session."""

import pytest

from ledger_builder import Ledger, cfg
from rka_memory import relations
from rka_memory.domain import RecordKind
from rka_memory.miner.miner import mine_session
from rka_memory.skills.export import Placement, render
from test_miner import FakeBackend, sync

APP = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
CACHE = "run the tests as `pytest -q -p no:cacheprovider`"


def ledger_with(*rules: str) -> Ledger:
    lg = Ledger()
    lg.start("s1", **APP)
    lg.tool_call("s1", "Bash", "python -m pytest -q")
    for n, content in enumerate(rules):
        lg.propose("s1", f"r_rule{n}", kind=RecordKind.CONSTRAINT, content=content, scope=None)
    return lg


@pytest.mark.parametrize(
    "rule, governs",
    [
        (CACHE, True),
        ("you run pytest -q before you finish", True),
        ("always use pytest when you test", True),
        ("its pytest tests should assert that behavior", False),
        ("every function gets type hints and a one-line docstring", False),
    ],
)
def test_a_rule_governs_a_procedure_when_it_names_a_command_the_procedure_runs(rule, governs):
    lg = ledger_with(rule)
    lg.propose("s1", "r_proc", kind=RecordKind.PROCEDURE, steps=[("Bash", "python -m pytest -q"), ("Edit", "pytest.ini")])
    world = lg.gate()
    assert relations.governing(world.records["r_proc"], world) == (["r_rule0"] if governs else [])


def test_file_steps_and_rules_never_govern_by_file_names():
    lg = ledger_with("keep `calc.py` small")
    lg.propose("s1", "r_proc", kind=RecordKind.PROCEDURE, steps=[("Edit", "calc.py")])
    world = lg.gate()
    assert relations.runs(world.records["r_proc"]) == set()
    assert relations.names(world.records["r_rule0"], world) == set()
    assert relations.governing(world.records["r_proc"], world) == []


def test_a_skill_shows_a_governing_rule_without_a_declared_dependency():
    lg = Ledger()
    lg.start("s1", **APP)
    msg = lg.user_msg("s1", f"From now on, {CACHE}.")
    lg.propose("s1", "r_rule", kind=RecordKind.CONSTRAINT, content=CACHE)
    lg.link("r_rule", msg, statement={"quote": CACHE, "marker": "From now on"})
    lg.propose("s1", "r_proc", kind=RecordKind.PROCEDURE, content="Add the function, then run the tests", steps=[("Bash", "python -m pytest -q")])
    lg.confirm("s1", "r_proc")
    world = lg.gate()
    text = render(world.records["r_proc"], world, cfg(), Placement(cfg().skills_dir, None, ()))
    assert f'## Rules this procedure follows\n\n- The user said on {msg.ts[:10]}: "{CACHE}"\n' in text


def test_the_miner_refines_a_rule_stated_in_the_same_session_by_its_stated_id(memory):
    lg = Ledger()
    lg.start("s1", **APP)
    lg.user_msg("s1", f"House rule from now on: {CACHE}.")
    ok = lg.test("s1", True, "pytest -q -p no:cacheprovider")
    lg.end("s1")
    sync(memory, lg)
    rules = {"rules": [{"quote": CACHE, "kind": "constraint", "standing": "House rule from now on"}]}
    qualification = {
        "content": "pytest -q -p no:cacheprovider still leaves __pycache__ behind",
        "kind": "fact",
        "evidence": [ok.id],
        "refines": ["stated-0"],
    }
    backend = FakeBackend({"candidates": [qualification]}, rules=rules)
    result = mine_session(memory, "s1", backend=backend)

    world = memory.world()
    rule_id, fact_id = (p["record_id"] for p in result.proposed)
    assert f"- stated-0: {CACHE}\n" in backend.prompts[0]
    assert world.records[fact_id].spec.refines == (rule_id,)
    assert result.dropped == []


def test_a_restated_rule_named_both_by_id_and_stated_id_is_kept_once(memory):
    lg = Ledger()
    lg.start("s1", **APP)
    lg.propose("s1", "r_rule", kind=RecordKind.CONSTRAINT, content=CACHE)
    lg.link("r_rule", lg.user_msg("s1", f"From now on, {CACHE}."), statement={"quote": CACHE, "marker": "From now on"})
    lg.end("s1")
    lg.start("s2", **APP)
    lg.user_msg("s2", f"Remember: {CACHE}.")
    ok = lg.test("s2", True, "pytest -q -p no:cacheprovider")
    lg.end("s2")
    sync(memory, lg)
    memory.gate()
    rules = {"rules": [{"quote": CACHE, "kind": "constraint", "standing": "Remember", "restates": "r_rule"}]}
    fact = {"content": "The cache flag leaves __pycache__ behind", "kind": "fact", "evidence": [ok.id], "refines": ["stated-0", "r_rule"]}
    result = mine_session(memory, "s2", backend=FakeBackend({"candidates": [fact]}, rules=rules))

    assert memory.world().records[result.proposed[0]["record_id"]].spec.refines == ("r_rule",)
