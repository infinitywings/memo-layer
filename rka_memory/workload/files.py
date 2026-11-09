"""The project a stream works in: fixture copy, snapshots, and file actions.

Drift and the simulated user change files the same way; the hooks credit any
change to a file the agent wrote to the user, as an edit or an undo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from rka_memory.workload.stream import Append, Clean, Commit, FileAction, Move, Remove, Replace, Restore, Write

UNTRACKED = frozenset({".git", ".pytest_cache", "__pycache__", ".claude"})
Snapshot = dict[str, bytes]


GIT = ["git", "-c", "user.name=workload", "-c", "user.email=workload@example.invalid"]
# The agent owns the repository, .git included. Hooks and fsmonitor are off as
# a second guard; the first is that git runs as the agent user (`sandboxed`).
GIT_GUARDED = [*GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
RunAs = tuple[int, int] | None


def sandboxed(cmd: list[str], cwd: Path, run_as: RunAs, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command that executes or reads what the agent wrote (its tests,
    its repository) as the agent user, with an environment that carries no
    API key and no RKA_MEMORY_* setting. As root, such a command would run
    agent code outside the agent's confinement, next to the ledger."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/agent" if run_as else os.environ.get("HOME", "/"),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    owner = {"user": run_as[0], "group": run_as[1], "extra_groups": []} if run_as else {}
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False, **owner)


def create_project(fixture: Path, project: Path) -> None:
    """Copy the fixture into a fresh git repository with one commit."""
    shutil.copytree(fixture, project)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
        subprocess.run([*GIT, *args], cwd=project, check=True, capture_output=True)


def commit(project: Path, message: str, run_as: RunAs = None) -> None:
    """Commit every change, or nothing when the tree is clean."""
    added = sandboxed([*GIT_GUARDED, "add", "-A"], project, run_as)
    if added.returncode != 0:
        raise ValueError(f"git add failed: {added.stderr.strip()[-300:]}")
    staged = sandboxed([*GIT_GUARDED, "diff", "--cached", "--quiet"], project, run_as)
    if staged.returncode == 1:
        done = sandboxed([*GIT_GUARDED, "commit", "-q", "-m", message], project, run_as)
        if done.returncode != 0:
            raise ValueError(f"git commit failed: {done.stderr.strip()[-300:]}")


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


def apply(action: FileAction, project: Path, before: Snapshot, run_as: RunAs = None) -> None:
    """Apply one drift or user file action. `before` is the snapshot taken
    before the latest agent session, which `restore` returns a file to.
    A commit runs as `run_as`, the agent user, when the runner confines it."""
    if isinstance(action, Commit):
        commit(project, action.message, run_as)
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
        case Move(to=to):
            dest = target(project, to)
            dest.parent.mkdir(parents=True, exist_ok=True)
            path.rename(dest)
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
