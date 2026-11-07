"""The project a stream works in: fixture copy, snapshots, and file actions.

Drift and the simulated user change files the same way; the hooks credit any
change to a file the agent wrote to the user, as an edit or an undo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from rka_memory.workload.stream import Append, Clean, Commit, FileAction, Remove, Replace, Restore, Write

UNTRACKED = frozenset({".git", ".pytest_cache", "__pycache__", ".claude"})
Snapshot = dict[str, bytes]


GIT = ["git", "-c", "user.name=workload", "-c", "user.email=workload@example.invalid"]


def create_project(fixture: Path, project: Path) -> None:
    """Copy the fixture into a fresh git repository with one commit."""
    shutil.copytree(fixture, project)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
        subprocess.run([*GIT, *args], cwd=project, check=True, capture_output=True)


def commit(project: Path, message: str) -> None:
    """Commit every change, or nothing when the tree is clean."""
    subprocess.run([*GIT, "add", "-A"], cwd=project, check=True, capture_output=True)
    staged = subprocess.run([*GIT, "diff", "--cached", "--quiet"], cwd=project, capture_output=True)
    if staged.returncode == 1:
        subprocess.run([*GIT, "commit", "-q", "-m", message], cwd=project, check=True, capture_output=True)


def snapshot(project: Path) -> Snapshot:
    """Every file the agent or the user could have touched, by project-relative path."""
    out: Snapshot = {}
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if path.is_file() and not UNTRACKED.intersection(rel.parts):
            out[rel.as_posix()] = path.read_bytes()
    return out


def target(project: Path, path: str) -> Path:
    full = (project / path).resolve()
    if full != project.resolve() and project.resolve() not in full.parents:
        raise ValueError(f"{path} is outside the project")
    return full


def apply(action: FileAction, project: Path, before: Snapshot) -> None:
    """Apply one drift or user file action. `before` is the snapshot taken
    before the latest agent session, which `restore` returns a file to."""
    if isinstance(action, Commit):
        commit(project, action.message)
        return
    path = target(project, action.path)
    match action:
        case Write(content=content):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        case Remove():
            path.unlink()
        case Clean():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        case Append(text=text):
            path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")
        case Replace(old=old, new=new):
            text = path.read_text(encoding="utf-8")
            if old not in text:
                raise ValueError(f"replace: {action.path} does not contain {old!r}")
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
        case Restore():
            rel = path.relative_to(project.resolve()).as_posix()
            if rel in before:
                path.write_bytes(before[rel])
            else:
                path.unlink(missing_ok=True)


def substitute(value: Any, placeholders: dict[str, str]) -> Any:
    """Replace `{project}`-style tokens in every string inside `value`."""
    if isinstance(value, str):
        for token, real in placeholders.items():
            value = value.replace(token, real)
        return value
    if isinstance(value, list | tuple):
        return type(value)(substitute(v, placeholders) for v in value)
    if isinstance(value, dict):
        return {k: substitute(v, placeholders) for k, v in value.items()}
    return value
