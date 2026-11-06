"""Personal-agent grounding signals computed by the adapter, never by the model:
user_edit and user_undo from file hashes, user_repeat from prompt similarity."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import tempfile
from pathlib import Path

from rka_memory.config import Config
from rka_memory.domain import Actor, Event, Kind, State, derived_id
from rka_memory.world import Session, World

ABSENT = "absent"
BLOB_MAX_BYTES = 1_000_000
MIN_REPEAT_TOKENS = 3
RKM_COMMAND = re.compile(r"\s*rkm\b", re.IGNORECASE)


def blob_dir(cfg: Config) -> Path:
    return cfg.ledger_dir / "blobs"


def file_hash(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        return ABSENT
    digest = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(path: str | Path, cfg: Config) -> str:
    """Hash a written file and keep its content as a blob for later diffs."""
    p = Path(path)
    if not p.is_file():
        return ABSENT
    if p.stat().st_size > BLOB_MAX_BYTES:
        return file_hash(p)
    data = p.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    target = blob_dir(cfg) / sha
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    return sha


def _lines(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines()


def line_delta(cfg: Config, post_hash: str, path: Path) -> tuple[int | None, int | None]:
    """(lines added, lines removed) going from the agent's write to the file now."""
    blob = blob_dir(cfg) / post_hash
    if not blob.is_file():
        return None, None
    before = _lines(blob.read_bytes())
    after = _lines(path.read_bytes()) if path.is_file() else []
    added = removed = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed += i2 - i1
        if op in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def _last_write_session(world: World, session_id: str, project: str | None) -> Session | None:
    candidates = [
        s
        for s in world.sessions.values()
        if s.id != session_id
        and s.started is not None
        and s.state.project == project
        and any(e.kind is Kind.FILE_WRITE for e in s.events)
    ]
    return max(candidates, key=lambda s: world.position[s.started.id], default=None)


GIT_TREE_COMMANDS = frozenset(
    {"checkout", "restore", "reset", "stash", "apply", "revert", "switch", "pull", "merge", "rebase", "cherry-pick", "am", "clean"}
)


def agent_may_have_touched(world: World, path: Path, after_event_id: str, project: str | None) -> bool:
    """True when an agent shell command in this project, after the write, named
    the file or rewrote the git working tree. Then a change on disk may be
    the agent's own, and crediting it to the user would forge a user signal."""
    for ev in world.events.values():
        if ev.kind is not Kind.TOOL_CALL or not world.before(after_event_id, ev.id):
            continue
        if str(ev.payload.get("tool", "")).lower() != "bash" or world.state_of(ev).project != project:
            continue
        text = str(ev.payload.get("input", ""))
        words = set(re.findall(r"[A-Za-z][A-Za-z-]*", text))
        if path.name in text or ("git" in words and words & GIT_TREE_COMMANDS):
            return True
    return False


def detect_file_changes(world: World, session_id: str, state: State, cfg: Config) -> list[Event]:
    """Compare files the previous session in this project wrote with their content now.

    Unchanged since the agent's write is silent adoption, which grounds
    nothing. Back to the content before the session touched it is an undo;
    anything else is an edit. Ids derive from the write, so detecting the same
    change again appends a duplicate the projection ignores.
    """
    prior = _last_write_session(world, session_id, state.project)
    if prior is None:
        return []
    writes: dict[str, list[Event]] = {}
    for ev in prior.events:
        if ev.kind is Kind.FILE_WRITE:
            writes.setdefault(ev.payload["path"], []).append(ev)
    events = []
    for path_str, path_writes in writes.items():
        first, last = path_writes[0], path_writes[-1]
        path = Path(path_str)
        current = file_hash(path)
        if current == last.payload.get("post_hash"):
            continue
        if agent_may_have_touched(world, path, last.id, state.project):
            continue
        base = {"path": path_str, "write_event_id": last.id, "write_session_id": prior.id}
        reverted = {first.payload.get("pre_hash"), last.payload.get("pre_hash")} - {"unknown", None}
        if current in reverted:
            kind, payload = Kind.USER_UNDO, base
        else:
            added, removed = line_delta(cfg, last.payload.get("post_hash", ""), path)
            kind, payload = Kind.USER_EDIT, {**base, "lines_added": added, "lines_removed": removed}
        events.append(
            Event.new(
                kind,
                Actor.USER,
                session_id,
                payload,
                state_hash=state.hash(),
                id=derived_id("ev", "file-change", last.id),
            )
        )
    return events


def _tokens(text: str) -> frozenset[str]:
    if RKM_COMMAND.match(text):
        return frozenset()
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of normalized token sets.

    Stands in for embedding similarity: the threshold in config is meant for
    a semantic score, and this lexical score is its deterministic stand-in.
    """
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) < MIN_REPEAT_TOKENS or len(tb) < MIN_REPEAT_TOKENS:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def detect_repeat(world: World, prompt_event: Event, cfg: Config) -> Event | None:
    """A USER_REPEAT when the prompt restates one from an earlier session."""
    text = str(prompt_event.payload.get("text") or "")
    if not _tokens(text):
        return None
    earlier = [
        [ev for ev in s.events if ev.kind is Kind.USER_MSG]
        for s in world.sessions.values()
        if s.id != prompt_event.session_id
    ]
    earlier = sorted((msgs for msgs in earlier if msgs), key=lambda msgs: world.position[msgs[0].id])
    best: tuple[float, Event] | None = None
    for msgs in earlier[-cfg.repeat_lookback_sessions :]:
        for ev in msgs:
            score = similarity(text, str(ev.payload.get("text") or ""))
            if score >= cfg.repeat_threshold and (best is None or score >= best[0]):
                best = (score, ev)
    if best is None:
        return None
    score, matched = best
    return Event.new(
        Kind.USER_REPEAT,
        Actor.USER,
        prompt_event.session_id,
        {"prompt_event_id": prompt_event.id, "matched_event_id": matched.id, "similarity": round(score, 3)},
        state_hash=prompt_event.state_hash,
        id=derived_id("ev", "repeat", prompt_event.id),
    )
