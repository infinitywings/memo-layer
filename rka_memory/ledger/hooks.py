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

from rka_memory.domain import Kind, State, clock_now, parse_ts
from rka_memory.ledger import signals
from rka_memory.ledger import state as state_mod
from rka_memory.ledger.adapters import claude_code as adapter
from rka_memory.service import Memory
from rka_memory.world import World

DISABLE_ENV = "RKA_MEMORY_DISABLE"
RKM_VERDICT = re.compile(r"^\s*rkm\s+(confirm|reject)\s+(\S+)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
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
    """Mine earlier sessions that missed it, once they have been quiet for a
    miner timeout: a session still running, or still being mined by its own
    SessionEnd, must not be mined twice."""
    quiet_since = clock_now() - timedelta(seconds=memory.cfg.miner_timeout_seconds)
    due = [
        s
        for s in world.sessions.values()
        if s.started is not None
        and not s.mined
        and s.id != session_id
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
    memory.gate(session_id=sid)
    packet = memory.deliver(sid, state, channel="session_start")
    if memory.cfg.hooks_background == "spawn":
        _spawn_catchup(memory, world, sid)
    text = packet.render()
    if text:
        text += f"\n(rka-memory session id: {sid}; pass it as session_id to memory_* tools.)"
    return _context("SessionStart", text)


def _rkm_command(memory: Memory, session_id: str, prompt: str) -> Output:
    match = RKM_VERDICT.match(prompt)
    if match is None:
        return _block("rka-memory: use `rkm confirm <id>` or `rkm reject <id> <reason>`")
    verb, record_id, reason = match.group(1).lower(), match.group(2), (match.group(3) or "").strip()
    try:
        memory.verdict(record_id, confirm=verb == "confirm", session_id=session_id, reason=reason)
    except KeyError:
        return _block(f"rka-memory: unknown record {record_id}; nothing recorded")
    memory.gate(session_id=session_id)
    rung = memory.world().records[record_id].rung
    return _block(f"rka-memory recorded {verb} for {record_id}; now {rung.value if rung else 'unrated'}")


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
        _write_json(stash, {"path": path, "pre_hash": signals.file_hash(path)})
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
    memory.append(*events)
    if tracked:
        _write_json(tracked_path, tracked)
    if stash:
        stash.unlink(missing_ok=True)
    return None


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
    memory.append(*_late_writes(memory, sid), adapter.session_end_event(payload, usage))
    shutil.rmtree(_pending(memory, sid), ignore_errors=True)
    sync = memory.cfg.sync_on_session_end and bool(memory.cfg.core_project_id)
    if memory.cfg.hooks_background == "spawn":
        commands = [_cli("mine", sid)] + ([_cli("sync")] if sync else [])
        spawn(commands, memory.cfg.ledger_dir / "background.log")
    elif memory.cfg.hooks_background == "inline":
        _mine_inline(memory, sid, sync)
    return None


def _mine_inline(memory: Memory, session_id: str, sync: bool) -> None:
    """Mine (which gates and exports) and sync before the hook returns, so a
    workload's ledger order follows its stream exactly."""
    from rka_memory.ledger import core
    from rka_memory.miner import miner

    with contextlib.redirect_stdout(io.StringIO()):
        miner.mine_session(memory, session_id)
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
