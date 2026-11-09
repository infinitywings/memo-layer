"""What the simulated user does to the project between and after sessions,
and the verdicts the user types. Shared by the stream parser and the code
that applies the actions, so neither imports the other."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Write:
    path: str
    content: str


@dataclass(frozen=True)
class Remove:
    path: str


@dataclass(frozen=True)
class Append:
    path: str
    text: str


@dataclass(frozen=True)
class Replace:
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Clean:
    """Delete the file or directory if it exists, such as a cache the agent left."""

    path: str


@dataclass(frozen=True)
class Commit:
    """Commit every change in the project, as a user does between sessions."""

    message: str


@dataclass(frozen=True)
class Move:
    """Rename a file inside the project, as a user does when restructuring it."""

    path: str
    to: str


@dataclass(frozen=True)
class Restore:
    """Put the file back as it was before the latest agent session; delete it if it did not exist."""

    path: str


@dataclass(frozen=True)
class Verdict:
    """A typed `rkm confirm|reject`; `record` is the record's content, resolved to its id at run time."""

    confirm: bool
    record: str
    reason: str = ""


FileAction = Write | Remove | Append | Replace | Restore | Clean | Commit | Move
UserAction = FileAction | Verdict
