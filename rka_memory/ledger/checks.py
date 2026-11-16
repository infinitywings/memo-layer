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

import ast
import hashlib
import os
from collections import Counter
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


def kind_of(path: str, cfg: Config) -> str | None:
    """"test" for a test file (by name), "config" for test configuration or
    any other file under a test directory, None otherwise."""
    if _matches(path, cfg.check_test_globs):
        return "test"
    if _matches(path, cfg.check_config_globs):
        return "config"
    return None


def entry(path: Path, cfg: Config) -> dict | None:
    """One file's snapshot: its kind and content hash, and for a test file
    its text, so a later snapshot can tell an added test from a changed one."""
    kind = kind_of(str(path), cfg)
    if kind is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    out: dict = {"kind": kind, "hash": hashlib.sha256(data).hexdigest()}
    if kind == "test" and len(data) <= MAX_BYTES:
        out["text"] = data.decode("utf-8", errors="replace")
    return out


def snapshot(root: Path, cfg: Config) -> dict[str, dict]:
    """Every test file and test configuration in the project."""
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            e = entry(path, cfg)
            if e is not None:
                out[str(path)] = e
    return out


def _plain_marks(node: ast.AST) -> bool:
    """Decorated only with pytest marks (pytest.mark.<name>, called or not)."""
    for dec in getattr(node, "decorator_list", ()):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "mark"
            and isinstance(target.value.value, ast.Name)
            and target.value.value.id == "pytest"
        ):
            return False
    return True


def _docstring(node: ast.AST) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _new_test(node: ast.AST, taken: set[str]) -> bool:
    """A new, uniquely named test function, or a test class holding only test
    methods; no fixture, hook or other decorator."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name.startswith("test") and node.name not in taken and _plain_marks(node)
    if isinstance(node, ast.ClassDef):
        return (
            node.name.startswith("Test")
            and node.name not in taken
            and not node.bases
            and not node.keywords
            and _plain_marks(node)
            and all(
                _docstring(b) or (isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) and b.name.startswith("test") and _plain_marks(b))
                for b in node.body
            )
        )
    return False


def _python_test_changed(old: str | None, new: str) -> bool:
    """Whether a Python test file changed beyond added tests. Fails closed:
    every top-level statement of the old file must still be there, unchanged
    (test bodies and decorators included), and every new one must be a new
    test, an import (not a star import) or a docstring. Anything else, and
    anything that does not parse, is a change."""
    try:
        old_tree = ast.parse(old) if old is not None else ast.Module(body=[], type_ignores=[])
        new_tree = ast.parse(new)
    except (SyntaxError, ValueError):
        return True
    named = [n.name for n in new_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if len(named) != len(set(named)):
        return True
    remaining = Counter(ast.dump(n) for n in old_tree.body)
    added = []
    for node in new_tree.body:
        key = ast.dump(node)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            added.append(node)
    if +remaining:
        return True
    taken = {n.name for n in old_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for node in added:
        if _new_test(node, taken) or _docstring(node):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)) and not any(a.name == "*" for a in node.names):
            continue
        return True
    return False


def changes(before: dict[str, dict], after: dict[str, dict]) -> tuple[list[str], list[str]]:
    """(changed, added) between two snapshots. Test configuration created,
    edited or removed is a change; a test file removed is a change; a Python
    test file that changed beyond added tests (_python_test_changed) is a
    change; outside Python, only a brand-new test file is an addition."""
    changed, added = [], []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old is not None and new is not None and old["hash"] == new["hash"]:
            continue
        if (new or old)["kind"] == "config" or new is None or "text" not in new:
            changed.append(path)
        elif path.endswith(".py"):
            if old is not None and "text" not in old:
                changed.append(path)
            else:
                (changed if _python_test_changed(old["text"] if old else None, new["text"]) else added).append(path)
        else:
            (added if old is None else changed).append(path)
    return changed, added


def write_check(path: str, pre: dict | None, cfg: Config, session_id: str, state: State) -> Event | None:
    """A failed modification check for one write that changed an existing
    test, logged as it happens, so a change reverted before the session ends
    is still on record. None when the write only added tests or is not to a
    test file (writes to test configuration count on their own)."""
    post = entry(Path(path), cfg)
    if (pre or post) is None or (pre or post)["kind"] != "test":
        return None
    changed, _ = changes({path: pre} if pre else {}, {path: post} if post else {})
    if not changed:
        return None
    return Event.new(
        Kind.CHECK,
        Actor.RUNNER,
        session_id,
        {"check": "modification", "passed": False, "paths": changed, "on": "write"},
        state_hash=state.hash(),
    )


def modification_event(before: dict[str, dict], after: dict[str, dict], session_id: str, state: State) -> Event:
    changed, added = changes(before, after)
    return Event.new(
        Kind.CHECK,
        Actor.RUNNER,
        session_id,
        {"check": "modification", "passed": not changed, "paths": changed, "added": added},
        state_hash=state.hash(),
    )
