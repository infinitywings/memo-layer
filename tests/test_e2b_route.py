"""E2b's route for true agent-visible text (docs/E2B.md Section 3): R1, the
miner reads each event up to `miner.payload_chars`; R2, `trials.entry: open`
puts a hypothesis on trial as soon as it is proposed."""

from dataclasses import replace

import pytest

from ledger_builder import Ledger, attested
from rka_memory.domain import Actor, Event, Kind, RecordKind, State
from rka_memory.kernel import trials
from rka_memory.kernel.labels import label
from rka_memory.miner.miner import build_prompt, view_of, windows
from rka_memory.world import project

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
CHECK = {"checks": {"outcome": {"command": ["true"]}}}
OPEN = {**CHECK, "trials": {"entry": "open"}}
HANDOFF = "# Handoff\n\n" + "- filler about the repository.\n" * 12 + "- Every calc function must raise TypeError for a bool argument."
RULE = "Every calc function must raise TypeError for a bool argument"


def read_handoff(lg: Ledger, sid: str) -> Event:
    return lg._ev(Kind.TOOL_RESULT, Actor.TOOL, sid, {"tool": "read", "summary": HANDOFF})


def test_the_miner_sees_a_long_file_read_whole_only_with_r1():
    lg = Ledger()
    lg.start("s1", **UV)
    read_handoff(lg, "s1")
    lg.end("s1")
    world = project(lg.events)
    assert len(HANDOFF) > 240
    assert RULE not in build_prompt(view_of(world, "s1", attested()))
    assert RULE in build_prompt(view_of(world, "s1", attested(miner={"payload_chars": 2000})))


def test_longer_prompt_lines_fill_a_window_sooner():
    lg = Ledger()
    lg.start("s1", **UV)
    events = [read_handoff(lg, "s1") for _ in range(3)]
    assert [len(w) for w in windows(events, 700)] == [2, 1]
    assert [len(w) for w in windows(events, 700, 2000)] == [1, 1, 1]


def hypothesis() -> Ledger:
    """A constraint the miner drew from one file read in one session: weak,
    model-made support that never meets v2's threshold."""
    lg = Ledger()
    lg.start("s1", **UV)
    lg.propose("s1", "r_bool", kind=RecordKind.CONSTRAINT, content=RULE)
    lg.link("r_bool", read_handoff(lg, "s1"))
    lg.end("s1")
    return lg


def why_and_trial(lg: Ledger, **cfg) -> tuple[str, str | None]:
    lab = label(project(lg.events), attested(**cfg), lg.clock).by_id["r_bool"]
    return lab.reason, lab.trial


def test_open_entry_puts_a_hypothesis_on_trial_at_once_but_only_with_a_check():
    lg = hypothesis()
    assert why_and_trial(lg, **CHECK) == ("hypothesis: model-made support only", None)
    assert why_and_trial(lg, trials={"entry": "open"}) == ("hypothesis: no outcome check is configured to test it", None)
    assert why_and_trial(lg, **OPEN)[1] == "open"


def test_an_open_entry_trial_that_hurts_the_check_goes_on_hold():
    lg = hypothesis()
    for i in range(6):
        sid = f"t{i}"
        lg.start(sid, **UV)
        lg._ev(Kind.TRIAL, Actor.RUNNER, sid, {"record_id": "r_bool", "arm": "treated" if i % 2 == 0 else "withheld", "q": 0.5})
        lg.check(sid, "outcome", i % 2 == 1)
        lg.end(sid)
    world = project(lg.events)
    t = trials.evaluate_sequential(world.records["r_bool"], world, attested(**OPEN))
    assert (t.status, t.treated, t.withheld) == ("hold", (3, 0), (3, 3))


def test_entry_must_be_threshold_or_open():
    with pytest.raises(ValueError, match="trials.entry"):
        attested(trials={"entry": "always"})


def test_a_treated_line_the_budget_leaves_out_is_logged(memory):
    memory.cfg = replace(attested(**OPEN), ledger_dir=memory.cfg.ledger_dir, skills_dir=memory.cfg.skills_dir, budget_tokens=20)
    memory.append(*hypothesis().events)
    sid = next(f"s{n}" for n in range(2, 200) if trials.coin(f"s{n}", "r_bool") < memory.cfg.trials.q)
    memory.append(Event.new(Kind.SESSION_START, Actor.RUNNER, sid, {"state": State(**UV).to_json()}))
    view = memory.deliver(sid, State(**UV), channel="session_start", arm="gated")
    [logged] = [d.event for d in memory.world().deliveries if d.event.session_id == sid]
    assert (view.record_ids, view.omitted, logged.payload["trial_omitted"]) == ((), ("r_bool",), ["r_bool"])
