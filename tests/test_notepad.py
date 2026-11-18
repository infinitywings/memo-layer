"""The `notepad` arm, E2's model-curated baseline (docs/E2.md Section 3)."""

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

from ledger_builder import Ledger
from rka_memory import notepad
from rka_memory.config import NotepadConfig
from rka_memory.domain import RecordKind, State
from rka_memory.ledger import hooks
from rka_memory.miner.backends import BackendError, Completion

UV = dict(project="/p/app", toolchain="uv", task_class="coding", model_id="m1", user_context="default")
NOTES = "- Register every new calc.py function in calc.OPERATIONS.\n- Run the tests with pytest -q."
CHECK_SAYS = "ops CLI check failed: " + "the ops CLI offers only the functions registered in calc.OPERATIONS. " * 6

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("mock_llm_notepad", REPO / "scripts" / "mock_llm.py")
mock_llm = sys.modules["mock_llm_notepad"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock_llm)


class Curator:
    """A backend that answers each call with the next reply and keeps the prompts."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> Completion:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Completion(text=reply, tokens=100, seconds=0.1)


def sessions(memory, *ids: str, output: str = "ops CLI check passed") -> Ledger:
    lg = Ledger()
    for sid in ids:
        lg.start(sid, **UV)
        lg.user_msg(sid, "Add a mean(values) function to calc.py, with tests.")
        lg.outcome_output(sid, 1, output)
        lg.end(sid)
    memory.append(*lg.events)
    return lg


def test_the_curators_reply_is_what_the_next_session_receives(memory):
    sessions(memory, "s1", "s2")
    notepad.after_session(memory, "s1", Curator(NOTES))
    start = memory.deliver("s2", State(**UV), channel="session_start", arm="notepad")
    prompt = memory.deliver("s2", State(**UV), channel="prompt", arm="notepad")
    assert start.render() == f"{notepad.HEADER}\n{NOTES}"
    [logged] = [d.event for d in memory.world().deliveries if d.channel == "session_start"]
    assert logged.payload["tokens"] == len(start.render()) // 4 + 1
    assert (start.record_ids, prompt.render()) == ((), "")
    assert [r["step"] for r in notepad.log(memory.cfg)] == ["curate"]
    assert notepad.curated(memory.cfg, "s1") and not notepad.curated(memory.cfg, "s2")


def test_the_curator_reads_the_previous_notes_and_the_whole_check_output(memory):
    sessions(memory, "s1", "s2", output=CHECK_SAYS)
    curator = Curator(NOTES, NOTES)
    notepad.after_session(memory, "s1", curator)
    notepad.after_session(memory, "s2", curator)
    assert len(CHECK_SAYS) > 400
    assert json.dumps(CHECK_SAYS)[1:-1] in curator.prompts[1]
    assert f"<notepad>\n{NOTES}\n</notepad>" in curator.prompts[1]
    assert "<notepad>\n(empty)\n</notepad>" in curator.prompts[0]


def test_a_reply_over_the_budget_loses_its_last_notes(memory):
    memory.cfg = replace(memory.cfg, budget_tokens=20)
    sessions(memory, "s1")
    lines = [f"- note number {n} about the project" for n in range(10)]
    [step] = notepad.after_session(memory, "s1", Curator("```\n" + "\n".join(lines) + "\n```"))
    assert (step.notes, step.trimmed) == (2, 8)
    assert notepad.current(memory.cfg) == "\n".join(lines[:2]) + "\n"


def test_a_failed_or_empty_reply_keeps_the_notes(memory):
    sessions(memory, "s1", "s2", "s3")
    notepad.after_session(memory, "s1", Curator(NOTES))
    notepad.after_session(memory, "s2", Curator("  \n"))
    notepad.after_session(memory, "s3", Curator(BackendError("miner endpoint answered HTTP 503")))
    assert notepad.current(memory.cfg) == NOTES + "\n"
    assert [(r["session_id"], r["ok"], r["error"]) for r in notepad.log(memory.cfg)] == [
        ("s1", True, ""),
        ("s2", False, "no output"),
        ("s3", False, "miner endpoint answered HTTP 503"),
    ]


def test_consolidation_follows_every_nth_kept_session_and_reads_those_sessions(memory):
    memory.cfg = replace(memory.cfg, notepad=NotepadConfig(consolidate_every=2))
    sessions(memory, "s1", "s2", "s3", "s4")
    curator = Curator(*[NOTES] * 6)
    for sid in ("s1", "s2", "s3", "s4"):
        notepad.after_session(memory, sid, curator)
    steps = [(r["session_id"], r["step"]) for r in notepad.log(memory.cfg)]
    assert steps == [
        ("s1", "curate"), ("s2", "curate"), ("s2", "consolidate"),
        ("s3", "curate"), ("s4", "curate"), ("s4", "consolidate"),
    ]
    last = curator.prompts[-1]
    assert last.startswith(notepad.CONSOLIDATE_PREFIX)
    assert ('<session id="s3">' in last, '<session id="s4">' in last, '<session id="s2">' in last) == (True, True, False)
    assert len(list(notepad.directory(memory.cfg).glob("*-*.md"))) == 6


def test_the_notepad_arm_delivers_no_records_where_gated_does(memory):
    lg = Ledger()
    lg.start("s1", **UV)
    msg = lg.user_msg("s1", "From now on, always run the suite with pytest -q.")
    lg.propose("s1", "r_rule", kind=RecordKind.PREFERENCE, content="run the suite with pytest -q")
    lg.link("r_rule", msg, statement={"quote": "run the suite with pytest -q", "marker": "always"})
    lg.end("s1")
    lg.start("s2", **UV)
    memory.append(*lg.events)
    gated = memory.deliver("s2", State(**UV), channel="session_start", arm="gated")
    kept = memory.deliver("s2", State(**UV), channel="session_start", arm="notepad")
    assert (gated.record_ids, kept.record_ids, kept.render()) == (("r_rule",), (), "")


def test_session_end_in_the_notepad_arm_runs_the_curator(memory, tmp_path, monkeypatch):
    config = tmp_path / "none.yaml"
    config.write_text(json.dumps({"gate": {"validation": "threshold"}, "packet": {"arms": ["notepad"]}, "hooks": {"background": "off"}}))
    curator = Curator(NOTES)
    monkeypatch.setattr(notepad, "backend_for", lambda cfg: curator)
    proj = tmp_path / "proj"
    proj.mkdir()
    base = {"session_id": "sess", "transcript_path": str(tmp_path / "sess.jsonl"), "cwd": str(proj)}
    for event, extra in (("SessionStart", {"source": "startup"}), ("SessionEnd", {"reason": "exit"})):
        assert hooks.main(event, json.dumps({**base, **extra, "hook_event_name": event})) == 0
    assert len(curator.prompts) == 1 and notepad.curated(memory.cfg, "sess")


def test_the_scripted_model_answers_the_curator_with_its_script_or_the_notes_it_holds():
    assert mock_llm.NOTEPAD_PREFIXES == (notepad.CURATE_PREFIX, notepad.CONSOLIDATE_PREFIX)
    prompt = notepad.CURATE.substitute(budget=1200, chars=4800, notepad=NOTES, session_id="s1", events="(no events)")
    empty = notepad.CURATE.substitute(budget=1200, chars=4800, notepad="(empty)", session_id="s1", events="(no events)")
    assert mock_llm.notepad_reply(prompt, None) == NOTES
    assert mock_llm.notepad_reply(prompt, "- a scripted note") == "- a scripted note"
    assert mock_llm.notepad_reply(empty, None) == "- No notes yet."
