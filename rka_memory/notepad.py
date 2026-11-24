"""The `notepad` arm: model-curated memory, E2's baseline (docs/E2.md Section 3).

It is not part of rka-memory's design. A model rewrites one notepad after
every session, consolidates it against the recent sessions' events every few
sessions, and the agent receives it whole at session start. The kernel never
reads it, and no record, label or trial depends on it.

The notepad lives beside the ledger, out of the agent's reach:
`notepad/current.md`, every version as `notepad/NNN-<step>-<session>.md`, and
one line per curator call in `notepad/log.jsonl`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any

from rka_memory.config import Config
from rka_memory.domain import SHOWN_KINDS, Kind, canonical_json
from rka_memory.kernel.views import estimate_tokens
from rka_memory.miner import backends

if TYPE_CHECKING:
    from rka_memory.service import Memory
    from rka_memory.world import World

HEADER = "## Notes from earlier sessions (rka-memory)\nIf one is wrong, tell the user."
# A session's events in one prompt, and each of the sessions a consolidation
# reads. Longer ones keep their start (the task) and their end (the check).
DIGEST_MAX_CHARS = 60_000
CONSOLIDATION_DIGEST_CHARS = 32_000

CURATE_PREFIX = "You keep the notepad of a coding agent's memory."
CONSOLIDATE_PREFIX = "You consolidate the notepad of a coding agent's memory."

_PURPOSE = """\
The agent works on one user's project, one task per session, and starts \
every session knowing nothing but this notepad. Notes are worth keeping when \
they will help the agent do later tasks the way this user wants: the user's \
rules and preferences, requirements and conventions of the project and of \
the systems around it, and lessons from what failed or passed. Leave out \
what the repository itself plainly shows, details of one finished task, and \
anything later events contradict.

Write the whole notepad: keep, edit, merge or delete earlier notes and add \
new ones. One note per line, each starting with "- ". At most $budget tokens \
(about $chars characters) in all. Reply with the notes only."""

CURATE = Template(
    CURATE_PREFIX
    + "\n\n"
    + _PURPOSE
    + """

The events of the session that just ended follow the notepad. They include \
the result of the user's own acceptance check, run after the session ended.

<notepad>
$notepad
</notepad>

<session id="$session_id">
$events
</session>
"""
)

CONSOLIDATE = Template(
    CONSOLIDATE_PREFIX
    + "\n\n"
    + _PURPOSE
    + """

Check every note against the events of the last sessions below, which \
include the results of the user's own acceptance check. Fix a note they \
contradict, delete one they show to be wrong or no longer useful, merge \
notes that say the same thing, and add what the notes missed.

<notepad>
$notepad
</notepad>

$sessions
"""
)


@dataclass(frozen=True)
class Step:
    """One curator call: `curate` after a session, or `consolidate`."""

    session_id: str
    step: str
    ok: bool
    tokens: int = 0
    seconds: float = 0.0
    notes: int = 0
    trimmed: int = 0
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "step": self.step,
            "ok": self.ok,
            "tokens": self.tokens,
            "seconds": self.seconds,
            "notes": self.notes,
            "trimmed": self.trimmed,
            "error": self.error,
        }


def directory(cfg: Config) -> Path:
    return cfg.ledger_dir / "notepad"


def current(cfg: Config) -> str:
    path = directory(cfg) / "current.md"
    return path.read_text() if path.exists() else ""


def render(cfg: Config) -> str:
    """What the agent receives at session start; empty before any note."""
    notes = current(cfg).strip()
    return f"{HEADER}\n{notes}" if notes else ""


def log(cfg: Config) -> list[dict[str, Any]]:
    path = directory(cfg) / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def curated(cfg: Config, session_id: str) -> bool:
    return any(r["session_id"] == session_id and r["step"] == "curate" and r["ok"] for r in log(cfg))


def backend_for(cfg: Config) -> backends.Backend:
    n = cfg.notepad
    return backends.backend_for(
        replace(
            cfg,
            miner_backend=n.backend,
            miner_model=n.model,
            miner_base_url=n.base_url,
            miner_api_key_env=n.api_key_env,
            miner_timeout_seconds=n.timeout_seconds,
        )
    )


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n(... {len(text) - limit} characters of this session left out ...)\n"
    head = (limit - len(marker)) // 3
    return text[:head] + marker + text[len(text) - (limit - len(marker) - head) :]


def digest(world: World, session_id: str, cfg: Config, limit: int = DIGEST_MAX_CHARS) -> str:
    """The session's events as the curator reads them: each event's payload
    up to `payload_max_chars`, ten times what a miner prompt line holds, so
    a file the agent read or a message the check printed is there whole."""
    sess = world.sessions.get(session_id)
    shown = SHOWN_KINDS | {Kind.ASSISTANT_MSG} if cfg.notepad.assistant_turns else SHOWN_KINDS
    events = [e for e in sess.events if e.kind in shown] if sess else []
    lines = []
    for e in events:
        payload = canonical_json(dict(e.payload))
        if len(payload) > cfg.payload_max_chars:
            payload = payload[: cfg.payload_max_chars] + "..."
        lines.append(f"[{e.kind.value} by {e.actor.value}] {payload}")
    return _clip("\n".join(lines) or "(no events)", limit)


def _notes(text: str, budget_tokens: int) -> tuple[str, int, int]:
    """The reply as notes, cut to the budget from the end. Returns the notes,
    how many were kept and how many cut."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    lines = [line if line.startswith("- ") else f"- {line.lstrip('-* ').strip()}" for line in lines]
    kept: list[str] = []
    for line in lines:
        if estimate_tokens("\n".join([*kept, line])) > budget_tokens:
            break
        kept.append(line)
    return "\n".join(kept), len(kept), len(lines) - len(kept)


def _save(cfg: Config, step: Step, notes: str | None) -> Step:
    d = directory(cfg)
    d.mkdir(parents=True, exist_ok=True)
    if notes is not None:
        n = sum(1 for r in log(cfg) if r["ok"]) + 1
        (d / f"{n:03d}-{step.step}-{step.session_id}.md").write_text(notes + "\n" if notes else "")
        (d / "current.md").write_text(notes + "\n" if notes else "")
    with open(d / "log.jsonl", "a") as fh:
        fh.write(json.dumps(step.to_json()) + "\n")
    return step


def _call(cfg: Config, session_id: str, step: str, prompt: str, backend: backends.Backend) -> Step:
    started = time.monotonic()
    try:
        completion = backend(prompt)
    except backends.BackendError as exc:
        return _save(cfg, Step(session_id, step, False, seconds=round(time.monotonic() - started, 3), error=str(exc)[:300]), None)
    if not completion.text.strip():
        # The notepad is never emptied by a call that returned nothing.
        return _save(cfg, Step(session_id, step, False, completion.tokens, completion.seconds, error="no output"), None)
    notes, kept, cut = _notes(completion.text, cfg.budget_tokens)
    return _save(cfg, Step(session_id, step, True, completion.tokens, completion.seconds, kept, cut), notes)


def _fill(cfg: Config) -> dict[str, Any]:
    return {"budget": cfg.budget_tokens, "chars": cfg.budget_tokens * 4, "notepad": current(cfg).strip() or "(empty)"}


def curate(memory: Memory, session_id: str, backend: backends.Backend | None = None) -> Step:
    cfg = memory.cfg
    prompt = CURATE.substitute(**_fill(cfg), session_id=session_id, events=digest(memory.world(), session_id, cfg))
    return _call(cfg, session_id, "curate", prompt, backend or backend_for(cfg))


def consolidate(memory: Memory, session_ids: list[str], backend: backends.Backend | None = None) -> Step:
    cfg = memory.cfg
    world = memory.world()
    sessions = "\n\n".join(
        f'<session id="{sid}">\n{digest(world, sid, cfg, CONSOLIDATION_DIGEST_CHARS)}\n</session>' for sid in session_ids
    )
    prompt = CONSOLIDATE.substitute(**_fill(cfg), sessions=sessions)
    return _call(cfg, session_ids[-1], "consolidate", prompt, backend or backend_for(cfg))


def after_session(memory: Memory, session_id: str, backend: backends.Backend | None = None) -> list[Step]:
    """Curate after a session; consolidate after every `consolidate_every`
    sessions the curator kept, over those sessions."""
    backend = backend or backend_for(memory.cfg)
    steps = [curate(memory, session_id, backend)]
    if not steps[0].ok:
        return steps
    kept = list(dict.fromkeys(r["session_id"] for r in log(memory.cfg) if r["step"] == "curate" and r["ok"]))
    every = memory.cfg.notepad.consolidate_every
    if len(kept) % every == 0:
        steps.append(consolidate(memory, kept[-every:], backend))
    return steps
