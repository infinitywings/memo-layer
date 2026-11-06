"""Duplicate detection: a candidate that restates an existing record merges into it."""

from __future__ import annotations

import re

from rka_memory.domain import RecordKind, State
from rka_memory.world import RecordState, World

_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(text.lower()))


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_duplicate(content: str, kind: RecordKind, world: World, state: State, threshold: float) -> RecordState | None:
    """The most similar same-kind record whose scope covers `state`, at any rung.

    Retired (L4) records are included on purpose: merging refuted content into
    its retired record is what keeps it from re-entering the ladder at L1.
    """
    best: tuple[float, int, RecordState] | None = None
    for rec in world.records.values():
        if rec.spec.kind is not kind or not rec.scope.covers(state):
            continue
        score = similarity(content, rec.spec.content)
        if score < threshold:
            continue
        key = (score, -world.position[rec.proposed.id], rec)
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best else None
