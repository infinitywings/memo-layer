"""Relations code derives from record content, with no model call.

A rule (a preference or a constraint) governs a procedure when the rule
names a command the procedure runs. The cache rule "run the tests as
`pytest -q -p no:cacheprovider`" governs every procedure that runs pytest,
whether or not its steps already carry the flag. The relation only shapes
views: a skill shows the rules that govern its procedure. It never changes
a rung, so a rule that names a command in passing costs one extra line in
a skill, not a wrong promotion.

The live miners almost never declared which rule a procedure follows, so
deriving the commonest case in code, where both sides name the same
command, is more reliable than asking for it.
"""

from __future__ import annotations

import re

from rka_memory import anchors
from rka_memory.domain import RecordKind
from rka_memory.world import RecordState, World

RULE_KINDS = (RecordKind.PREFERENCE, RecordKind.CONSTRAINT)
BACKTICKED = re.compile(r"`([^`]+)`")


def runs(rec: RecordState) -> set[str]:
    """Command anchors a procedure runs: its shell steps and its acceptance check."""
    texts = [s.match for s in rec.spec.steps if s.tool in anchors.SHELL_TOOLS]
    if rec.spec.acceptance_check:
        texts.append(rec.spec.acceptance_check)
    return set().union(*(anchors.command_anchors(t) for t in texts)) if texts else set()


def _known(rec: RecordState, world: World) -> set[str]:
    include = rec.scope.project.include
    projects = include if include is not None else world.known.keys()
    return {a for p in projects for a in world.known.get(p, ()) if a.startswith("command:")}


def _used(name: str, text: str) -> bool:
    """`name` appears as a command, not just a word: after "run", "use" or
    "with", or followed by an option ("pytest -q"). "Its pytest tests" is
    not a use of pytest."""
    n = re.escape(name)
    return bool(re.search(rf"(?i)\b(?:run|runs|running|use|using|with)\s+{n}(?![\w-])|(?<![\w-]){n}\s+-", text))


def names(rec: RecordState, world: World) -> set[str]:
    """Command anchors a rule names: its `about` commands, its backticked
    commands, and commands its project has run that its text uses."""
    text = rec.spec.content
    out = {a for a in rec.spec.about if a.startswith("command:")}
    for span in BACKTICKED.findall(text):
        if not anchors.PATH_TOKEN.fullmatch(span.strip()):
            out |= anchors.command_anchors(span)
    out |= {a for a in _known(rec, world) if _used(a.removeprefix("command:"), text)}
    return out


def governing(rec: RecordState, world: World) -> list[str]:
    """Ids of the rules that govern procedure `rec`, oldest first."""
    if rec.spec.kind is not RecordKind.PROCEDURE:
        return []
    ran = runs(rec)
    if not ran:
        return []
    rules = sorted(
        (r for r in world.records.values() if r.spec.kind in RULE_KINDS and r.id != rec.id),
        key=lambda r: world.position[r.proposed.id],
    )
    return [r.id for r in rules if names(r, world) & ran]
