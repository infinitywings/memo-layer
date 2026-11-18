"""Claude Code hook entrypoint: `rka-memory hook <Event>` with the payload on stdin.

A hook must never break Claude Code, so `main` swallows every exception into
`hook-errors.log` and always returns 0.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from rka_memory import commands
from rka_memory.commands import CommandError
from rka_memory.domain import (
    QUOTE_CHARS,
    Actor,
    Event,
    FieldConstraint,
    Integrity,
    Kind,
    RecordKind,
    RecordSpec,
    Scope,
    State,
    clock_now,
    collapse,
    derived_id,
    integrity,
    parse_ts,
)
from rka_memory.kernel.evidence import in_check_source
from rka_memory.ledger import checks as runner_checks
from rka_memory.ledger import signals
from rka_memory.ledger import state as state_mod
from rka_memory.ledger.adapters import claude_code as adapter
from rka_memory.miner.miner import could_apply
from rka_memory.service import Memory
from rka_memory.world import World

DISABLE_ENV = "RKA_MEMORY_DISABLE"
CATCHUP_MINES = 3

Output = dict[str, Any] | None


def _cli(*args: str) -> list[str]:
    # -I: hooks run in the project directory, and plain `-m` would import a
    # `rka_memory/` package planted there instead of the installed one.
    return [sys.executable, "-I", "-m", "rka_memory.cli", *args]


def spawn(commands: list[list[str]], log_path: Path) -> None:
    """Run `commands` one after another in a detached process."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log:
        subprocess.Popen(
            ["/bin/sh", "-c", "; ".join(shlex.join(c) for c in commands)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "_"


def _pending(memory: Memory, session_id: str) -> Path:
    return memory.cfg.ledger_dir / "pending" / _safe(session_id)


def _state_file(memory: Memory, session_id: str) -> Path:
    return _pending(memory, session_id) / "_state.json"


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _remember_state(memory: Memory, session_id: str, state: State) -> None:
    """Cache the session state so tool hooks can stamp state_hash without replaying the ledger."""
    _write_json(_state_file(memory, session_id), state.to_json())


def _state_hash(memory: Memory, session_id: str) -> str:
    cached = _read_json(_state_file(memory, session_id))
    return State.from_json(cached).hash() if isinstance(cached, dict) else ""


def _context(event: str, text: str) -> Output:
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _block(reason: str) -> Output:
    return {"decision": "block", "reason": reason}


def _payload_model(payload: Mapping[str, Any]) -> str | None:
    model = payload.get("model")
    if isinstance(model, Mapping):
        model = model.get("id")
    return model if isinstance(model, str) and model else None


def _spawn_catchup(memory: Memory, world: World, session_id: str) -> None:
    """Mine earlier sessions that hold events no miner has covered (one that
    missed its SessionEnd mine, or was resumed after it), once they have been
    quiet for a miner timeout: a session still running, or still being mined
    by its own SessionEnd, must not be mined twice."""
    quiet_since = clock_now() - timedelta(seconds=memory.cfg.miner_timeout_seconds)
    due = [
        s
        for s in world.sessions.values()
        if s.started is not None
        and s.id != session_id
        and world.to_mine(s.id)
        and parse_ts(s.events[-1].ts) <= quiet_since
    ]
    due.sort(key=lambda s: world.position[s.started.id], reverse=True)
    if due:
        spawn([_cli("mine", s.id) for s in due[:CATCHUP_MINES]], memory.cfg.ledger_dir / "background.log")


def on_session_start(memory: Memory, payload: Mapping[str, Any]) -> Output:
    sid = adapter.session_id(payload)
    world = memory.world()
    known = world.sessions.get(sid)
    if known is not None and known.started is not None:
        state, changes = known.state, []
    else:
        model = _payload_model(payload) or adapter.transcript_model(
            adapter.read_transcript(payload.get("transcript_path"))
        )
        state = state_mod.detect(Path(payload.get("cwd") or os.getcwd()), memory.cfg, model)
        changes = signals.detect_file_changes(world, sid, state, memory.cfg)
    memory.append(adapter.session_start_event(payload, state, state.model_id), *changes)
    _remember_state(memory, sid, state)
    _start_checks(memory, sid, state)
    memory.gate(session_id=sid)
    packet = memory.deliver(sid, state, channel="session_start")
    if memory.cfg.hooks_background == "spawn":
        _spawn_catchup(memory, world, sid)
    text = packet.render()
    if text:
        text += f"\n(rka-memory session id: {sid}; pass it as session_id to memory_* tools.)"
    return _context("SessionStart", text)


def _snapshot_file(memory: Memory, session_id: str) -> Path:
    return _pending(memory, session_id) / "_checks.json"


def _seen_file(memory: Memory, session_id: str) -> Path:
    return _pending(memory, session_id) / "_seen.json"


def _start_checks(memory: Memory, session_id: str, state: State) -> None:
    """Before the session's packet is built: hash the files whose change makes
    its test results weak, and check the citations of every record that could
    apply here, so the packet reads only logged checks (DESIGN_V2 8)."""
    if not state.project:
        return
    snap = runner_checks.snapshot(Path(state.project), memory.cfg)
    _write_json(_snapshot_file(memory, session_id), snap)
    _write_json(_seen_file(memory, session_id), snap)
    labels = memory.labels()
    cited = [
        r for r in memory.world().records.values()
        if r.spec.cites and not labels.by_id[r.id].withdrawn and could_apply(r.scope, state)
    ]
    memory.append(*runner_checks.citation_events(cited, state, session_id))


def _last_test_passed(memory: Memory, session_id: str) -> bool:
    results = [e for e in memory.world().session(session_id).events if e.kind is Kind.TEST_RESULT]
    return bool(results) and results[-1].payload.get("passed") is True


def _end_check(memory: Memory, session_id: str) -> Event | None:
    """The session's tests against how they stood at its start, marked as
    owing an isolation re-run if it only added tests and its last test run
    passed (rule 2): a new test can make another pass by changing shared
    state."""
    before = _read_json(_snapshot_file(memory, session_id))
    state = memory.session_state(session_id)
    if not isinstance(before, dict) or not state.project:
        return None
    after = runner_checks.snapshot(Path(state.project), memory.cfg)
    changed, added = runner_checks.changes(before, after)
    due = not changed and bool(added) and _last_test_passed(memory, session_id)
    return runner_checks.modification_event(before, after, session_id, state, isolation_due=due)


def _isolation_check(memory: Memory, session_id: str, end: Event) -> Event:
    state = memory.session_state(session_id)
    return runner_checks.isolation_event(
        Path(state.project or "."), memory.cfg, session_id, state, end.payload["added"], derived_id("ev", "isolation", session_id)
    )


def _outcome_check(memory: Memory, session_id: str) -> list[Event]:
    """The user's outcome check (DESIGN_V2 5.3), after the agent's last
    action and before the session ends, so the miner sees what it printed.
    Its due mark goes into the log first: a hook killed during the check
    leaves the session owing a result, which counts against promotion."""
    state = memory.session_state(session_id)
    if not memory.cfg.outcome.command or not state.project:
        return []
    memory.append(Event.new(Kind.CHECK, Actor.RUNNER, session_id, {"check": "outcome", "status": "due"}, state_hash=state.hash()))
    return runner_checks.outcome_events(Path(state.project), memory.cfg, session_id, state)


def _check_words(output: str, cfg: Any) -> str | None:
    """What a failing check printed that the user wrote into the check: the
    whole output if it fits a quote, else its first line that does."""
    whole = collapse(output)
    lines = [collapse(line) for line in output.splitlines()]
    for words in (whole, *lines):
        if QUOTE_CHARS[0] <= len(words) <= QUOTE_CHARS[1] and in_check_source(words, cfg):
            return words
    return None


def _check_record(memory: Memory, session_id: str, outcome: list[Event]) -> list[Event]:
    """DESIGN_V3 3.3: when the user's check fails, the runner, not the miner,
    proposes what it printed as a constraint, and links the output to it with
    the quote. One record per project and wording, so it reaches L2 when the
    same words fail a second session. Attested validation only."""
    cfg = memory.cfg
    printed = next((e for e in outcome if integrity(e) is Integrity.CHECK), None)
    if cfg.gate.validation != "attested" or printed is None or printed.payload.get("exit_code") in (0, None):
        return []
    project = memory.session_state(session_id).project
    words = _check_words(str(printed.payload.get("output") or ""), cfg)
    if not project or words is None:
        return []
    rid = derived_id("r", "check", project, words)
    new = []
    if rid not in memory.world().records:
        spec = RecordSpec(rid, RecordKind.CONSTRAINT, words, Scope(project=FieldConstraint(frozenset({project}))))
        new.append(Event.new(Kind.RECORD_PROPOSED, Actor.RUNNER, session_id, spec.to_json()))
    link = {"record_id": rid, "event_id": printed.id, "polarity": "support", "reason": "the user's check printed it",
            "statement": {"quote": words}}
    return [*new, Event.new(Kind.EVIDENCE_LINKED, Actor.RUNNER, session_id, link)]


def _rkm_command(memory: Memory, session_id: str, prompt: str) -> Output:
    try:
        return _block(commands.run(memory, prompt, session_id))
    except CommandError as err:
        return _block(str(err))


def on_user_prompt_submit(memory: Memory, payload: Mapping[str, Any]) -> Output:
    sid = adapter.session_id(payload)
    prompt = adapter.prompt_text(payload)
    if signals.RKM_COMMAND.match(prompt):
        return _rkm_command(memory, sid, prompt)
    world = memory.world()
    session = world.session(sid)
    first = not any(ev.kind is Kind.USER_MSG for ev in session.events)
    msg = adapter.user_msg_event(payload, memory.cfg, session.state.hash())
    new = [msg]
    repeat = signals.detect_repeat(world, msg, memory.cfg)
    if repeat is not None:
        new.append(repeat)
    if first:
        task = state_mod.classify_task(prompt, memory.cfg)
        new.append(adapter.task_class_judgment(sid, task, session.state.hash()))
    memory.append(*new)
    if not first:
        return None
    for ev in new:
        world.apply(ev)
    state = world.session(sid).state
    _remember_state(memory, sid, state)
    packet = memory.deliver(
        sid, state, channel="prompt", exclude=frozenset(world.delivered_in(sid)), world=world
    )
    return _context("UserPromptSubmit", packet.render())


def on_pre_tool_use(memory: Memory, payload: Mapping[str, Any]) -> Output:
    use = adapter.ToolUse.parse(payload, failed=False)
    path = adapter.written_path(use.tool, use.tool_input, payload.get("cwd"))
    if path is not None and use.tool_use_id:
        stash = _pending(memory, use.session_id) / f"{_safe(use.tool_use_id)}.json"
        project = memory.session_state(use.session_id).project
        key = _snapshot_key(path, project) if project else None
        # Taken from the file the write reaches, so a write through a link is
        # compared as that file (_step_check reads the same key afterwards).
        pre = runner_checks.entry(Path(key), memory.cfg) if key else None
        _write_json(stash, {"path": path, "pre_hash": signals.file_hash(path), "key": key, "pre": pre})
    return None


def _tracked_file(memory: Memory, session_id: str) -> Path:
    return _pending(memory, session_id) / "_tracked.json"


def _on_tool(memory: Memory, payload: Mapping[str, Any], *, failed: bool) -> Output:
    use = adapter.ToolUse.parse(payload, failed=failed)
    stash = _pending(memory, use.session_id) / f"{_safe(use.tool_use_id)}.json" if use.tool_use_id else None
    pre = _read_json(stash) if stash else None
    path = adapter.written_path(use.tool, use.tool_input, payload.get("cwd"))
    state_hash = _state_hash(memory, use.session_id)
    post_hash = signals.snapshot(path, memory.cfg) if path and not failed else None
    events = adapter.tool_events(
        payload,
        memory.cfg,
        failed=failed,
        state_hash=state_hash,
        pre_hash=pre.get("pre_hash", "unknown") if isinstance(pre, dict) else "unknown",
        post_hash=post_hash,
    )
    tracked_path = _tracked_file(memory, use.session_id)
    tracked = _read_json(tracked_path)
    tracked = tracked if isinstance(tracked, dict) else {}
    if use.tool == "Bash":
        # Re-hash what the agent wrote so far: a change made by its own shell
        # command is the agent's, and must not later be credited to the user.
        for written, last_hash in sorted(tracked.items()):
            now = signals.snapshot(written, memory.cfg)
            if now != last_hash:
                events.append(adapter.shell_write_event(payload, written, last_hash, now, state_hash))
                tracked[written] = now
    if path and post_hash is not None:
        tracked[path] = post_hash
    if not failed:
        decision = adapter.decision_event(payload, memory.session_state(use.session_id), memory.cfg, state_hash)
        if decision is not None:
            events.append(decision)
    check = _step_check(memory, use, path, pre.get("pre") if isinstance(pre, dict) and "key" in pre else _NO_ENTRY)
    if check is not None:
        events.append(check)
    memory.append(*events)
    if tracked:
        _write_json(tracked_path, tracked)
    if stash:
        stash.unlink(missing_ok=True)
    return None


_NO_ENTRY = object()


def _snapshot_key(path: str, project: str) -> str | None:
    """`path` spelled as runner_checks.snapshot spells it, or None outside the
    project (the tool payload's cwd and git's toplevel may differ by links)."""
    try:
        rel = Path(path).resolve().relative_to(Path(project).resolve())
    except (OSError, ValueError):
        return None
    return str(Path(project) / rel)


def _step_check(memory: Memory, use: adapter.ToolUse, path: str | None, pre: Any) -> Event | None:
    """Compare what the call changed in the tests and test configuration with
    how they stood at the session's start, so each change is on record as it
    happens and a revert hides nothing (rule 2). A single-file write is seen
    from the entry taken just before it, so a change made since the previous
    call counts too; any other tool not known to be read-only (a shell
    command, a patch, an unknown tool) re-reads the whole project. Not
    caught: one command that edits a test, runs it and reverts the edit
    before it returns, and a backgrounded edit that a later write
    overwrites."""
    state = memory.session_state(use.session_id)
    start = _read_json(_snapshot_file(memory, use.session_id))
    seen_path = _seen_file(memory, use.session_id)
    seen = _read_json(seen_path)
    if not state.project or not isinstance(start, dict) or not isinstance(seen, dict):
        return None
    key = _snapshot_key(path, state.project) if path else None
    if path and pre is not _NO_ENTRY:
        if key is None:
            return None
        before = {k: v for k, v in seen.items() if k != key} | ({key: pre} if pre else {})
        post = runner_checks.entry(Path(key), memory.cfg)
        after = {k: v for k, v in before.items() if k != key} | ({key: post} if post else {})
        states, on = [seen, before, after], "write"
    elif use.tool in adapter.READ_ONLY_TOOLS:
        return None
    else:
        after = runner_checks.snapshot(Path(state.project), memory.cfg)
        states, on = [seen, after], "shell"
    if after != seen:
        _write_json(seen_path, after)
    event_id = derived_id("ev", "modification", use.session_id, use.tool_use_id) if use.tool_use_id else None
    return runner_checks.step_check(start, states, use.session_id, state, on, event_id)


def _late_writes(memory: Memory, session_id: str) -> list:
    tracked = _read_json(_tracked_file(memory, session_id))
    if not isinstance(tracked, dict):
        return []
    state_hash = _state_hash(memory, session_id)
    out = []
    for written, last_hash in sorted(tracked.items()):
        now = signals.snapshot(written, memory.cfg)
        if now != last_hash:
            out.append(adapter.late_write_event(session_id, written, last_hash, now, state_hash))
    return out


def on_session_end(memory: Memory, payload: Mapping[str, Any]) -> Output:
    sid = adapter.session_id(payload)
    usage = adapter.reported_usage(payload) or adapter.transcript_usage(
        adapter.read_transcript(payload.get("transcript_path"))
    )
    end = _end_check(memory, sid)
    # The end check goes into the log before the re-run starts, so a hook
    # killed during the re-run leaves the session marked as owing one.
    memory.append(*_late_writes(memory, sid), *([end] if end else []))
    rerun = [_isolation_check(memory, sid, end)] if end and end.payload.get("isolation") == "due" else []
    memory.append(*rerun)
    outcome = _outcome_check(memory, sid)
    memory.append(*outcome, *_check_record(memory, sid, outcome), adapter.session_end_event(payload, usage))
    shutil.rmtree(_pending(memory, sid), ignore_errors=True)
    sync = memory.cfg.sync_on_session_end and bool(memory.cfg.core_project_id)
    if memory.cfg.hooks_background == "spawn":
        commands = [_cli("mine", sid)] + ([_cli("sync")] if sync else [])
        spawn(commands, memory.cfg.ledger_dir / "background.log")
    elif memory.cfg.hooks_background == "inline":
        _mine_inline(memory, sid, sync)
    return None


MINER_ERRORS = "miner-errors.log"


def _mine_inline(memory: Memory, session_id: str, sync: bool) -> None:
    """Mine (which gates and exports) and sync before the hook returns, so a
    workload's ledger order follows its stream exactly."""
    from rka_memory.ledger import core
    from rka_memory.miner import miner

    with contextlib.redirect_stdout(io.StringIO()):
        result = miner.mine_session(memory, session_id)
        if result.status in (miner.MineStatus.BACKEND_ERROR, miner.MineStatus.NO_OUTPUT):
            # What the failed prompt held stays unmined; say why, where a run can find it.
            with open(memory.cfg.ledger_dir / MINER_ERRORS, "a") as fh:
                fh.write(f"{session_id} {result.status.value}: {result.detail}\n")
        if sync:
            core.sync(memory)


HANDLERS: dict[str, Callable[[Memory, Mapping[str, Any]], Output]] = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt_submit,
    "PreToolUse": on_pre_tool_use,
    "PostToolUse": lambda memory, payload: _on_tool(memory, payload, failed=False),
    "PostToolUseFailure": lambda memory, payload: _on_tool(memory, payload, failed=True),
    "SessionEnd": on_session_end,
}


def _log_error(log_dir: Path | None, event_name: str, raw_stdin: str) -> None:
    try:
        log_dir = log_dir or Path(os.environ.get("RKA_MEMORY_DIR", "~/.rka-memory")).expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "hook-errors.log", "a") as fh:
            fh.write(f"--- {datetime.now(UTC).isoformat()} {event_name}\n")
            fh.write(traceback.format_exc())
            fh.write(f"stdin: {raw_stdin[:1000]!r}\n")
    except OSError:
        pass


def main(event_name: str, raw_stdin: str) -> int:
    if os.environ.get(DISABLE_ENV) == "1":
        return 0
    log_dir = None
    try:
        memory = Memory.open()
        log_dir = memory.cfg.ledger_dir
        payload = json.loads(raw_stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not a JSON object")
        handler = HANDLERS.get(event_name)
        output = handler(memory, payload) if handler else None
        if output:
            print(json.dumps(output, ensure_ascii=False), flush=True)
    except Exception:
        _log_error(log_dir, event_name, raw_stdin)
    return 0
