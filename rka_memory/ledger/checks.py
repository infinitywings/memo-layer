"""The runner's checks, run where the project lives and logged before any
label reads them (DESIGN_V2 4.1, 8).

- Citations and falsifiers: a record names a file and words in it; the
  check reads the file and logs whether the words are there (a citation) or
  absent (a falsifier). The model chose the words, so a passing check is no
  evidence; a failing one withdraws the record (its cited basis is gone).
  The paths come from the model, so a path that is not a relative path
  inside the project is never read; its check is logged as failed.
- Modification: the test files and test configuration, taken at session
  start and compared at session end. A session that changed existing test
  content, or any test configuration, makes its own test and exit results
  weak; one that only added tests does not (rule 2 as the PI refined it).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
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


# An added line that changes how existing tests run without touching them.
HOOK = re.compile(r"autouse|^\s*def\s+pytest_|^\s*pytest_plugins\b")


def snapshot(root: Path, cfg: Config) -> dict[str, dict]:
    """The project files matching the check globs: for test configuration a
    content hash, for a test file also its lines, so the end of the session
    can tell an added test from a changed one."""
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            config = _matches(str(path), cfg.check_config_globs)
            if not config and not _matches(str(path), cfg.check_test_globs):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            entry: dict = {"kind": "config" if config else "test", "hash": hashlib.sha256(data).hexdigest()}
            if not config and len(data) <= MAX_BYTES:
                entry["lines"] = data.decode("utf-8", errors="replace").splitlines()
            out[str(path)] = entry
    return out


def changes(before: dict[str, dict], after: dict[str, dict]) -> tuple[list[str], list[str]]:
    """(changed, added) between two snapshots. Changed: test configuration
    created, edited or removed; a test file removed, or with an existing line
    replaced or deleted, or with an added autouse fixture or pytest hook.
    Added: a new test file, or new lines in one that leave every existing
    line in place. Only a change can make a failing check pass."""
    changed, added = [], []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old is not None and new is not None and old["hash"] == new["hash"]:
            continue
        kind = (new or old)["kind"]
        if kind == "config" or new is None or "lines" not in new:
            changed.append(path)
            continue
        if old is None:
            inserted = new["lines"]
        elif "lines" not in old:
            changed.append(path)
            continue
        else:
            ops = difflib.SequenceMatcher(None, old["lines"], new["lines"], autojunk=False).get_opcodes()
            if any(tag in ("replace", "delete") for tag, *_ in ops):
                changed.append(path)
                continue
            inserted = [line for tag, _, _, j1, j2 in ops if tag == "insert" for line in new["lines"][j1:j2]]
        (changed if any(HOOK.search(line) for line in inserted) else added).append(path)
    return changed, added


def modification_event(before: dict[str, dict], after: dict[str, dict], session_id: str, state: State) -> Event:
    changed, added = changes(before, after)
    return Event.new(
        Kind.CHECK,
        Actor.RUNNER,
        session_id,
        {"check": "modification", "passed": not changed, "paths": changed, "added": added},
        state_hash=state.hash(),
    )
