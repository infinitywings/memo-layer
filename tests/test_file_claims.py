"""E2c's R3 (docs/E2C.md Section 3): the miner lists a requirement a project
file states as a file claim, which no check, support or trial validates and
no packet holds until the user confirms it."""

from dataclasses import replace

from ledger_builder import Ledger, attested
from test_miner import STATE, FakeBackend, sync

from rka_memory.domain import Actor, Event, FileClaim, Kind, RecordKind, State
from rka_memory.kernel import trials, views
from rka_memory.kernel.labels import label
from rka_memory.miner.miner import build_prompt, mine_session, view_of
from rka_memory.world import project

UV = dict(STATE, task_class="coding")
RULE = "Every calc function must raise TypeError when it is given a bool"
READ = (
    "<path>/w/HANDOFF.md</path>\n<content>\n1: # Handoff\n2: \n"
    "3: - Every calc function must raise TypeError when it is given a bool where\n"
    "4:   a number belongs.\n</content>"
)
QUOTE = "Every calc function must raise TypeError when it is given a bool where a number belongs."
OPEN = {"checks": {"outcome": {"command": ["true"]}}, "trials": {"entry": "open"}}


def use(memory, **overrides):
    memory.cfg = replace(attested(**overrides), ledger_dir=memory.cfg.ledger_dir, skills_dir=memory.cfg.skills_dir)


def read_session(memory, lg, sid="s1"):
    lg.start(sid, **UV)
    lg.user_msg(sid, "Add multiply. The previous maintainer left notes in HANDOFF.md.")
    read = lg._ev(Kind.TOOL_RESULT, Actor.TOOL, sid, {"tool": "Read", "ok": True, "summary": READ})
    lg.end(sid)
    sync(memory, lg)
    return read


def claim(read, content=RULE, kind="constraint", quote=QUOTE):
    return {"content": content, "kind": kind, "evidence": [read.id], "claimed_by": {"path": "HANDOFF.md", "quote": quote}}


def test_the_prompt_asks_for_file_claims_only_when_the_flag_is_on(memory):
    lg = Ledger()
    read_session(memory, lg)
    world = project(lg.events)
    off = build_prompt(view_of(world, "s1", attested()))
    on = build_prompt(view_of(world, "s1", attested(miner={"file_claims": True})))
    assert "claimed_by" not in off and "13. File claims" not in off
    assert "claimed_by" in on and "13. File claims" in on


def test_a_file_claim_quoting_a_file_read_across_lines_is_kept_with_its_mark(memory):
    use(memory, miner={"file_claims": True})
    lg = Ledger()
    read = read_session(memory, lg)
    result = mine_session(memory, "s1", backend=FakeBackend({"candidates": [claim(read)]}))
    [proposed] = result.proposed
    rec = memory.world().records[proposed["record_id"]]
    assert (proposed["file_claim"], rec.spec.claimed_by) == (True, FileClaim("HANDOFF.md", QUOTE))
    assert rec.spec.cites == ()


def test_without_the_flag_claimed_by_is_ignored(memory):
    use(memory)
    lg = Ledger()
    read = read_session(memory, lg)
    result = mine_session(memory, "s1", backend=FakeBackend({"candidates": [claim(read)]}))
    [proposed] = result.proposed
    assert "file_claim" not in proposed
    assert memory.world().records[proposed["record_id"]].spec.claimed_by is None


def test_a_claim_whose_words_no_tool_result_showed_or_that_is_a_fact_is_dropped(memory):
    use(memory, miner={"file_claims": True})
    lg = Ledger()
    read = read_session(memory, lg)
    reply = {
        "candidates": [
            claim(read, content="Round floats to two decimals", quote="Round every float to two decimal places"),
            claim(read, content="calc functions reject bools", kind="fact"),
            claim(read, content="calc rejects bools", quote="Every calc"),
        ]
    }
    result = mine_session(memory, "s1", backend=FakeBackend(reply))
    assert result.proposed == []
    assert [d.reason for d in result.dropped] == [
        "the quoted words are in no tool result of the session",
        "a file claim states a requirement, not a fact or a decision",
        "a file claim quotes at least 3 words",
    ]


def test_a_file_claim_merges_only_into_a_file_claim(memory):
    use(memory, miner={"file_claims": True})
    lg = Ledger()
    read = read_session(memory, lg)
    lesson = {"content": RULE, "kind": "constraint", "evidence": [read.id]}
    first = mine_session(memory, "s1", backend=FakeBackend({"candidates": [lesson, claim(read)]}))
    assert len(first.proposed) == 2
    again = read_session(memory, lg, "s2")
    second = mine_session(memory, "s2", backend=FakeBackend({"candidates": [claim(again)]}))
    file_claim = next(p["record_id"] for p in first.proposed if p.get("file_claim"))
    assert (second.proposed, [m["record_id"] for m in second.merged]) == ([], [file_claim])


def two_records() -> Ledger:
    """The same requirement twice from one file read: an ordinary hypothesis
    and a file claim."""
    lg = Ledger()
    lg.start("s1", **UV)
    read = lg._ev(Kind.TOOL_RESULT, Actor.TOOL, "s1", {"tool": "Read", "summary": READ})
    lg.propose("s1", "r_lesson", kind=RecordKind.CONSTRAINT, content=RULE)
    lg.propose("s1", "r_claim", kind=RecordKind.CONSTRAINT, content=RULE, claimed_by=FileClaim("HANDOFF.md", QUOTE))
    lg.link("r_lesson", read)
    lg.link("r_claim", read)
    lg.end("s1")
    return lg


def test_a_file_claim_never_enters_a_trial_even_with_open_entry():
    lg = two_records()
    world = project(lg.events)
    labels = label(world, attested(**OPEN), lg.clock)
    claim_label = labels.by_id["r_claim"]
    assert (claim_label.rung.value, claim_label.reason, claim_label.trial) == ("L1", "file claim: needs the user's confirm", None)
    assert labels.by_id["r_lesson"].trial == "open"
    assert trials.candidates(world, labels, attested(**OPEN)) == ["r_lesson"]


def test_no_arm_delivers_a_file_claim_before_the_users_confirm():
    lg = two_records()
    world = project(lg.events)
    cfg = attested(**OPEN)
    labels = label(world, cfg, lg.clock)
    state = State(**UV)
    for arm in ("gated", "ungated", "sourced", "stated"):
        pkt = views.packet(world, labels, cfg, state, arm=arm, channel="preview", treated=tuple(trials.candidates(world, labels, cfg)))
        assert "r_claim" not in pkt.record_ids, arm


def test_a_confirmed_file_claim_is_delivered_as_the_users_confirmed_claim(memory):
    use(memory, **OPEN)
    lg = two_records()
    lg.confirm("s1", "r_claim")
    memory.append(*lg.events)
    labels = memory.labels(now=lg.clock)
    assert (labels.by_id["r_claim"].rung.value, labels.by_id["r_claim"].reason) == ("L2", "user confirmed")
    pkt = views.packet(memory.world(), labels, memory.cfg, State(**UV), arm="gated", channel="preview")
    [line] = [x for x in pkt.core if x.record_id == "r_claim"]
    assert line.text.endswith("(Source: the user confirmed it.)")


def test_a_confirmed_file_claim_is_not_withdrawn_when_its_file_is_gone(memory):
    use(memory, **OPEN)
    lg = two_records()
    lg.confirm("s1", "r_claim")
    memory.append(*lg.events)
    memory.gate(now=lg.clock)
    later = Event.new(Kind.SESSION_START, Actor.RUNNER, "s9", {"state": State(**UV).to_json()})
    memory.append(later)
    view = memory.deliver("s9", State(**UV), channel="session_start", arm="gated")
    checks = [e for e in memory.world().events.values() if e.kind is Kind.CHECK and e.payload.get("record_id") == "r_claim"]
    assert ("r_claim" in view.record_ids, checks) == (True, [])
    assert memory.labels().by_id["r_claim"].rung.value == "L2"


def test_a_rejected_file_claim_is_withdrawn():
    lg = two_records()
    lg.reject("s1", "r_claim", "not a requirement")
    labels = label(project(lg.events), attested(**OPEN), lg.clock)
    assert (labels.by_id["r_claim"].rung.value, labels.by_id["r_claim"].withdrawn) == ("L4", True)


def test_a_confirm_typed_at_the_cli_validates_a_file_claim(memory):
    from rka_memory.commands import run
    from rka_memory.service import CLI_SESSION

    use(memory, **OPEN)
    memory.append(*two_records().events)
    out = run(memory, "rkm confirm r_claim", CLI_SESSION)
    assert out == "rka-memory recorded confirm for r_claim; now L2"


def test_a_standing_statement_of_its_words_does_not_validate_a_file_claim():
    words = "name every test after the function it tests"
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_claim", kind=RecordKind.CONSTRAINT, content=words, claimed_by=FileClaim("HANDOFF.md", words))
    lg.propose("s1", "r_lesson", kind=RecordKind.CONSTRAINT, content=words)
    msg = lg.user_msg("s1", f"From now on: {words}.")
    for rid in ("r_claim", "r_lesson"):
        lg.link(rid, msg, statement={"quote": words, "marker": "From now on"})
    lg.end("s1")
    labels = label(project(lg.events), attested(**OPEN), lg.clock)
    assert labels.by_id["r_lesson"].reason == "the user stated it as a standing rule"
    assert (labels.by_id["r_claim"].rung.value, labels.by_id["r_claim"].reason) == ("L1", "file claim: needs the user's confirm")
