"""Views derived from the graph: skills show their rules by reference, and a
superseded record leaves every view while its newer version is valid."""

from ledger_builder import Ledger, cfg
from rka_memory.domain import RecordKind, State
from rka_memory.miner.miner import Drop, mine_session
from rka_memory.packet import builder
from rka_memory.skills.export import Placement, exportable, render
from test_miner import FakeBackend, sync

APP = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
RULE = "run the tests as `pytest -q -p no:cacheprovider`"


def stated_rule(lg: Ledger, session: str, record_id: str, words: str) -> None:
    lg.start(session, **APP)
    msg = lg.user_msg(session, f"From now on, {words}.")
    lg.propose(session, record_id, kind=RecordKind.CONSTRAINT, content=words)
    lg.link(record_id, msg, statement={"quote": words, "marker": "From now on"})
    lg.end(session)


def confirmed(lg: Ledger, session: str, record_id: str, **spec) -> None:
    lg.start(session, **APP)
    lg.propose(session, record_id, **spec)
    lg.confirm(session, record_id)
    lg.end(session)


def skill_text(world, record_id: str) -> str:
    return render(world.records[record_id], world, cfg(), Placement(cfg().skills_dir, None, ()))


def test_a_skill_shows_its_rules_by_reference_and_drops_one_the_user_withdraws():
    lg = Ledger()
    stated_rule(lg, "s1", "r_rule", RULE)
    confirmed(lg, "s2", "r_proc", kind=RecordKind.PROCEDURE, content="Add the function, then run the tests", depends_on=("r_rule",))
    world = lg.gate()
    day = lg.events[1].ts[:10]
    assert f'## Rules this procedure follows\n\n- The user said on {day}: "{RULE}"\n' in skill_text(world, "r_proc")

    lg.start("s3", **APP)
    stop = lg.user_msg("s3", "You can stop using no:cacheprovider.")
    lg.link("r_rule", stop, "counter", statement={"quote": "stop using no:cacheprovider"})
    lg.end("s3")
    world = lg.gate()
    assert world.records["r_rule"].rung.value == "L1"
    assert world.records["r_proc"].rung.value == "L2"
    assert "Rules this procedure follows" not in skill_text(world, "r_proc")


def test_a_valid_newer_version_hides_the_old_one_from_packets_and_skills():
    lg = Ledger()
    confirmed(lg, "s1", "r_v1", kind=RecordKind.PROCEDURE, content="Run pytest, then commit")
    lg.start("s2", **APP)
    lg.propose("s2", "r_v2", kind=RecordKind.PROCEDURE, content="Run pytest, review the diff, then commit", supersedes="r_v1")
    lg.end("s2")
    world = lg.gate()
    st = State(**APP)
    assert world.records["r_v2"].rung.value == "L0D"
    assert builder.build(world, cfg(), st, arm="gated", channel="prompt").record_ids == ["r_v1"]

    lg.confirm("s3", "r_v2")
    world = lg.gate()
    assert builder.build(world, cfg(), st, arm="gated", channel="prompt").record_ids == ["r_v2"]
    assert [r.id for r in exportable(world, cfg())] == ["r_v2"]


def test_a_users_rule_is_hidden_only_by_a_newer_rule_in_the_users_words():
    lg = Ledger()
    stated_rule(lg, "s1", "r_old", "end every reply with a `Summary:` line")
    confirmed(lg, "s2", "r_model", kind=RecordKind.CONSTRAINT, content="Replies need no summary", supersedes="r_old")
    world = lg.gate()
    st = State(**APP)
    assert set(builder.build(world, cfg(), st, arm="gated", channel="prompt").record_ids) == {"r_old", "r_model"}

    lg.start("s3", **APP)
    msg = lg.user_msg("s3", "From now on, end every reply with a `TL;DR:` line instead.")
    lg.propose("s3", "r_new", kind=RecordKind.CONSTRAINT, content="end every reply with a `TL;DR:` line", supersedes="r_old")
    lg.link("r_new", msg, statement={"quote": "end every reply with a `TL;DR:` line", "marker": "From now on"})
    lg.end("s3")
    world = lg.gate()
    assert set(builder.build(world, cfg(), st, arm="gated", channel="prompt").record_ids) == {"r_model", "r_new"}
    assert world.records["r_old"].rung.value == "L2"


def test_the_miner_versions_procedures_and_only_the_users_words_replace_a_rule(memory):
    lg = Ledger()
    stated_rule(lg, "s1", "r_rule", "end every reply with a `Summary:` line")
    confirmed(lg, "s1", "r_proc", kind=RecordKind.PROCEDURE, content="Run pytest, then commit")
    lg.start("s2", **APP)
    msg = lg.user_msg("s2", "From now on, end every reply with a `TL;DR:` line instead.")
    ok = lg.test("s2", True)
    lg.end("s2")
    sync(memory, lg)
    memory.gate()
    reply = {
        "candidates": [
            {"content": "Run pytest, review the diff, then commit", "kind": "procedure", "evidence": [ok.id], "supersedes": "r_proc"},
            {"content": "Replies need no summary line", "kind": "constraint", "evidence": [ok.id], "supersedes": "r_rule"},
        ]
    }
    rules = {"rules": [{"quote": "end every reply with a `TL;DR:` line", "kind": "constraint", "standing": "From now on", "replaces": "r_rule"}]}
    result = mine_session(memory, "s2", backend=FakeBackend(reply, rules=rules))

    world = memory.world()
    by_content = {world.records[p["record_id"]].spec.content: world.records[p["record_id"]].spec.supersedes for p in result.proposed}
    assert by_content == {
        "end every reply with a `TL;DR:` line": "r_rule",
        "Run pytest, review the diff, then commit": "r_proc",
        "Replies need no summary line": None,
    }
    assert result.dropped == [Drop("candidate 1 supersedes r_rule", "only a procedure supersedes a listed procedure")]
    assert msg.id in {link.event_id for p in result.proposed for link in world.records[p["record_id"]].links}


def test_a_replacement_is_not_merged_into_the_close_wording_it_replaces(memory):
    lg = Ledger()
    stated_rule(lg, "s1", "r_rule", "end every reply with a line that starts with `Summary:`")
    lg.start("s2", **APP)
    lg.user_msg("s2", "From now on, end every reply with a line that starts with `TL;DR:` instead.")
    lg.end("s2")
    sync(memory, lg)
    memory.gate()
    new = "end every reply with a line that starts with `TL;DR:`"
    rules = {"rules": [{"quote": new, "kind": "constraint", "standing": "From now on", "replaces": "r_rule"}]}
    result = mine_session(memory, "s2", backend=FakeBackend({}, rules=rules))

    assert [(p["content"], result.merged) for p in result.proposed] == [(new, [])]
    assert memory.world().records[result.proposed[0]["record_id"]].spec.supersedes == "r_rule"
