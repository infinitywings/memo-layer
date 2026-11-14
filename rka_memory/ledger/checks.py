"""The runner's checks, run where the project lives and logged before any
label reads them (DESIGN_V2 4.1, 8).

- Citations and falsifiers: a record names a file and words in it; the
  check reads the file and logs whether the words are there (a citation) or
  absent (a falsifier). The model chose the words, so a passing check is no
  evidence; a failing one withdraws the record (its cited basis is gone).
  The paths come from the model, so a path that is not a relative path
  inside the project is never read; its check is logged as failed.
- Modification: the hashes of the files matching `checks.globs`, taken at
  session start and compared at session end. A session that changed any
  makes its own test and exit results weak.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Iterable

from rka_memory import anchors
from rka_memory.config import Config
from rka_memory.domain import Actor, Event, Kind, State
from rka_memory.kernel.evidence import _matches
from rka_memory.world import RecordState

MAX_BYTES = 1_000_000
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def _inside(root: Path, rel: str) -> Path | None:
    if not rel or PurePosixPath(rel).is_absolute():
        return None
    target = (root / rel).resolve()
    return target if target.is_relative_to(root.resolve()) else None


def _read(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            return fh.read(MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def citation_events(records: Iterable[RecordState], state: State, session_id: str) -> list[Event]:
    """One check per citation of each record, run against the project of
    `state`."""
    root = Path(state.project) if state.project else None
    if root is None or not root.is_dir():
        return []
    out = []
    for rec in records:
        for cite in rec.spec.cites:
            target = _inside(root, cite.path)
            text = _read(target)
            found = text is not None and " ".join(cite.quote.split()) in " ".join(text.split())
            passed = text is not None and (not found if cite.absent else found)
            out.append(
                Event.new(
                    Kind.CHECK,
                    Actor.RUNNER,
                    session_id,
                    {
                        "check": cite.check,
                        "record_id": rec.id,
                        "passed": passed,
                        "paths": [str(target)] if target is not None else [],
                        "anchors": [a for a in [anchors.file_anchor(str(root / cite.path), state.project)] if a] if target is not None else [],
                        "path": cite.path,
                        "quote": cite.quote,
                    },
                    state_hash=state.hash(),
                )
            )
    return out


def snapshot(root: Path, cfg: Config) -> dict[str, str]:
    """Content hashes of the project files matching `checks.globs`."""
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if not _matches(str(path), cfg.check_globs):
                continue
            try:
                out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
    return out


def modification_event(before: dict[str, str], after: dict[str, str], session_id: str, state: State) -> Event:
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    return Event.new(
        Kind.CHECK,
        Actor.RUNNER,
        session_id,
        {"check": "modification", "passed": not changed, "paths": changed},
        state_hash=state.hash(),
    )
