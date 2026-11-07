"""Scope coverage, counter-driven narrowing, and read-time applicability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from rka_memory.domain import FUZZY_FIELDS, Event, Kind, Scope, State
from rka_memory.world import RecordState, World

TARGETED_KINDS = (Kind.USER_CONFIRM, Kind.USER_REJECT)


@dataclass(frozen=True)
class Grounding:
    """A link that survived validation: a non-model event with an allowed polarity."""

    event: Event
    weight: float
    state: State
    targeted: bool
    # The user stated, in this event and in so many words, that what the
    # record says is a standing rule: it confirms the record like a verdict.
    affirms: bool = False

    @property
    def session_id(self) -> str:
        return self.event.session_id


def is_targeted(ev: Event, record_id: str) -> bool:
    """User verdicts name the record directly, so they apply wherever it applies."""
    return ev.kind in TARGETED_KINDS and ev.payload.get("record_id") == record_id


def inside(scope: Scope, g: Grounding) -> bool:
    return g.targeted or scope.covers(g.state)


def choose_narrowing(
    scope: Scope,
    counter: Grounding,
    support: Sequence[Grounding],
    keeps_rung: Callable[[list[Grounding]], bool],
    priority: Sequence[str],
) -> tuple[str, str] | None:
    """Pick the field value to exclude so the counter leaves scope.

    Among fields whose exclusion still leaves enough support for the current
    rung, choose the one that loses the least support weight, ties broken by
    `priority`. None means the counter cannot be scoped away.
    """
    if counter.targeted:
        return None
    options = []
    for rank, name in enumerate(priority):
        value = counter.state.get(name)
        if value is None:
            continue
        narrowed = scope.without(name, value)
        remaining = [g for g in support if inside(narrowed, g)]
        if not keeps_rung(remaining):
            continue
        lost = sum(g.weight for g in support) - sum(g.weight for g in remaining)
        options.append((lost, rank, name, value))
    if not options:
        return None
    _, _, name, value = min(options)
    return name, value


def applicable(rec: RecordState, state: State, world: World) -> bool | None:
    """Exact match on structured fields; fuzzy fields fall back to logged judgments.

    Returns None when a fuzzy field is uncovered and no judgment exists yet, so
    the caller can ask a judge and log the answer as a JUDGMENT event.
    """
    for name in rec.scope.uncovered_fields(state):
        value = state.get(name)
        if name not in FUZZY_FIELDS or value is None or value in rec.scope.constraint(name).exclude:
            return False
        cached = world.judgments.get((rec.id, name, value))
        if cached is None:
            return None
        if not cached:
            return False
    return True
