"""Personal-agent grounding signals computed by the adapter, never by the model:
user_edit and user_undo from file hashes, user_repeat from prompt similarity."""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
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


def _keep(data: bytes, cfg: Config) -> str:
    sha = hashlib.sha256(data).hexdigest()
    target = blob_dir(cfg) / sha
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    return sha


def snapshot(path: str | Path, cfg: Config) -> str:
    """Hash a written file and keep its content as a blob for later diffs."""
    p = Path(path)
    if not p.is_file():
        return ABSENT
    if p.stat().st_size > BLOB_MAX_BYTES:
        return file_hash(p)
    return _keep(p.read_bytes(), cfg)


def stored(sha: str, cfg: Config) -> bool:
    """The content store holds what `sha` names."""
    return sha != ABSENT and (blob_dir(cfg) / sha).is_file()


def store_text(text: str, cfg: Config) -> str | None:
    """Keep `text` in the content store: its hash, or None when it is too
    large to keep."""
    data = text.encode("utf-8", errors="replace")
    return None if len(data) > BLOB_MAX_BYTES else _keep(data, cfg)


# Directories a shell command's own churn fills, not the agent's words.
SNAPSHOT_SKIP = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".venv", "venv", "node_modules"})
SNAPSHOT_MAX_FILES = 200
WALK_MAX_ENTRIES = 20_000
# File systems with coarse timestamps: a file changed by the command can
# carry an mtime slightly before it started.
MTIME_SLACK_NS = 2_000_000_000


def changed_since(root: Path, since_ns: int) -> tuple[list[Path], bool]:
    """Regular files under `root` changed since `since_ns`, and whether the
    walk saw everything (False past the caps, which callers must treat as
    unknown content). A change is read from the later of mtime and ctime:
    `touch` can set mtime back, but setting it moves ctime to now."""
    out: list[Path] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SNAPSHOT_SKIP)
        for name in sorted(filenames):
            seen += 1
            if seen > WALK_MAX_ENTRIES:
                return out, False
            path = Path(dirpath) / name
            try:
                st = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and max(st.st_mtime_ns, st.st_ctime_ns) >= since_ns - MTIME_SLACK_NS:
                out.append(path)
                if len(out) > SNAPSHOT_MAX_FILES:
                    return out, False
    return out, True


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


def _reset_since(path: str, after: int, resets: list[tuple[str, int]]) -> bool:
    """A harness removed `path`, or a directory holding it, after position `after`."""
    return any(at > after and (path == root or path.startswith(root.rstrip("/") + "/")) for root, at in resets)


def detect_file_changes(world: World, session_id: str, state: State, cfg: Config) -> list[Event]:
    """Compare files the previous session in this project wrote with their content now.

    The last FILE_WRITE per path includes changes the agent's own shell
    commands made (the hooks re-hash written files after every Bash call), so
    a difference from it happened outside the agent's tool calls.

    Unchanged since the agent's write is silent adoption, which grounds
    nothing. Back to the content before the session touched it is an undo;
    anything else is an edit. A path a test harness reset after the write
    (`workspace_reset`) is skipped: the harness, not the user, changed it.
    Ids derive from the write, so detecting the same change again appends a
    duplicate the projection ignores.
    """
    prior = _last_write_session(world, session_id, state.project)
    if prior is None:
        return []
    writes: dict[str, list[Event]] = {}
    for ev in prior.events:
        if ev.kind is Kind.FILE_WRITE:
            writes.setdefault(ev.payload["path"], []).append(ev)
    resets = [(ev.payload.get("path") or "", world.position[ev.id]) for ev in world.events.values() if ev.kind is Kind.WORKSPACE_RESET]
    events = []
    for path_str, path_writes in writes.items():
        first, last = path_writes[0], path_writes[-1]
        if _reset_since(path_str, world.position[last.id], resets):
            continue
        path = Path(path_str)
        current = file_hash(path)
        if current == last.payload.get("post_hash"):
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


# Words a restatement may add or drop without asking for something else.
FILLER = frozenset(
    "a an the and or but so then to of in on at for with by from as is are was were be been it its this that "
    "these those i me my we us our you your please kindly thanks thank again still now just also once more "
    "can could would will should do does did ok okay s t".split()
)


def substituted(a: str, b: str) -> bool:
    """Each prompt has a content word the other lacks: the user changed what
    they ask for, as two tasks from one template naming different functions
    do, however similar the rest."""
    ta, tb = _tokens(a), _tokens(b)
    return bool((ta - tb) - FILLER) and bool((tb - ta) - FILLER)


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
    """A USER_REPEAT when the prompt restates one from an earlier session:
    similar enough, and asking for the same thing (`substituted`)."""
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
            other = str(ev.payload.get("text") or "")
            score = similarity(text, other)
            if score >= cfg.repeat_threshold and not substituted(text, other) and (best is None or score >= best[0]):
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
