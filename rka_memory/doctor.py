"""`rka-memory doctor`: is the memory working, in counts and times only.

The agent may run this (rka_memory/ledger/guard.py), so it prints nothing a
record says and nothing a session contained: no record text, no prompts, no
tool output, no error messages (a hook error's message can quote its input).
"""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path

from rka_memory.config import Config
from rka_memory.service import Memory

_ERROR_HEAD = re.compile(r"^--- (\S+) (\S+)$")
_EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))\b")


def _hook_errors(log: Path) -> tuple[int, str | None, str | None, str | None]:
    """Count, and the time, hook event and exception type of the latest error."""
    if not log.is_file():
        return 0, None, None, None
    count, when, event, kind = 0, None, None, None
    for line in log.read_text(errors="replace").splitlines():
        head = _ERROR_HEAD.match(line)
        if head:
            count, when, event, kind = count + 1, head.group(1), head.group(2), None
            continue
        found = _EXCEPTION.match(line)
        if found and count:
            kind = found.group(1)
    return count, when, event, kind


def _miner(cfg: Config) -> str:
    if cfg.miner_backend != "auto":
        return cfg.miner_backend
    for cli in ("claude", "codex"):
        if shutil.which(cli):
            return f"auto -> {cli}_cli"
    return "auto -> none (neither the claude nor the codex CLI is on PATH)"


def report(memory: Memory, now: datetime | None = None) -> list[str]:
    cfg = memory.cfg
    now = now or datetime.now(UTC)
    try:
        version = metadata.version("rka-memory")
    except metadata.PackageNotFoundError:
        version = "unknown"
    world = memory.world()
    starts = [s.started.ts for s in world.sessions.values() if s.started is not None]
    week = (now - timedelta(days=7)).isoformat()
    ended = [s for s in world.sessions.values() if s.ended is not None]
    mined = {s.id for s in ended if s.mined_through is not None}
    count, when, event, kind = _hook_errors(cfg.ledger_dir / "hook-errors.log")
    rungs: dict[str, int] = {}
    for rec in world.records.values():
        key = rec.rung.value if rec.rung else "-"
        rungs[key] = rungs.get(key, 0) + 1
    lines = [
        f"rka-memory {version}",
        f"ledger: {cfg.ledger_dir} ({'present' if (cfg.ledger_dir / 'ledger.jsonl').is_file() else 'no ledger yet'})",
        f"events: {len(world.events)}; sessions: {len(world.sessions)}, {sum(t >= week for t in starts)} started in the last 7 days",
        f"last session start: {max(starts) if starts else 'never'}",
        f"ended sessions mined: {len(mined)} of {len(ended)}",
        f"records by rung: {', '.join(f'{k} {v}' for k, v in sorted(rungs.items())) or 'none'}",
        f"miner: {_miner(cfg)}",
        f"hook errors: {count}" + (f"; latest {when} in {event} ({kind or 'unknown type'})" if count else ""),
        f"guard: {'on' if cfg.hooks_guard else 'off'}",
        f"skills: {cfg.skills_dir if cfg.skills_export_files else 'not exported'}"
        + (f" and {cfg.skills_codex_dir}" if cfg.skills_export_files and cfg.skills_codex_dir else ""),
        f"RKA Core sync: {'project ' + cfg.core_project_id if cfg.core_project_id else 'not configured'}",
    ]
    return lines
