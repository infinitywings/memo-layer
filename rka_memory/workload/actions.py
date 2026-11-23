"""What the simulated user does to the project between and after sessions,
and the verdicts the user types. Shared by the stream parser and the code
that applies the actions, so neither imports the other."""

from __future__ import annotations

import re
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


@dataclass(frozen=True)
class Typed:
    """A typed `rkm <op>` amendment (rka_memory/commands.py). `record` is the
    record's content; `from_session` names the session whose evidence links a
    `defeat` withdraws."""

    op: str
    record: str
    text: str = ""
    when: tuple[str, str] | None = None
    from_session: str | None = None


REVIEW_POLICIES = ("oracle", "stamp")
# Words that make a claim a suggestion to look into, not a requirement; the
# oracle leaves such a line unanswered (docs/E2C.md Section 4).
DOUBT = (
    "verify", "before adding", "before relying", "check whether", "confirm whether", "find out whether",
    "unverified", "unconfirmed", "not sure", "unclear", "reportedly",
)


@dataclass(frozen=True)
class Review:
    """The user reads `rka-memory review` after the session is mined and types
    a verdict through `rka-memory rkm` on each line the policy decides
    (docs/E2C.md Section 4). The oracle knows the fixture's requirements: it
    confirms a claim matching a `confirm` pattern, rejects one matching a
    `reject` pattern with `reason`, and leaves a claim matching both,
    neither, or words of doubt. The stamp user confirms every line unread."""

    policy: str
    confirm: tuple[str, ...] = ()
    reject: tuple[str, ...] = ()
    reason: str = "not a requirement"

    def decide(self, claim: str) -> str:
        """confirm, reject or leave, for a claim as its line states it."""
        if self.policy == "stamp":
            return "confirm"
        text = " ".join(claim.split())
        if any(d in text.lower() for d in DOUBT):
            return "leave"
        true = any(re.search(p, text, re.IGNORECASE) for p in self.confirm)
        false = any(re.search(p, text, re.IGNORECASE) for p in self.reject)
        if true != false:
            return "confirm" if true else "reject"
        return "leave"


FileAction = Write | Remove | Append | Replace | Restore | Clean | Commit | Move
UserAction = FileAction | Verdict | Typed | Review
