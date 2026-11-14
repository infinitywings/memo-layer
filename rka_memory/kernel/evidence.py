"""Which links count as evidence (DESIGN_V2 5.1, rules 1 and 2), and where a record applies.

`counted` keeps a link only if its cited event can ground: not model-authored,
a grounding kind, a polarity that event can carry, already in the ledger when
the link was made, and, for a model-made link, from the session the link was
made in. A user verdict counts only for the record it names. Independence,
cleanliness and the withdrawn set are the labelling function's business.
"""

from __future__ import annotations

from dataclasses import dataclass

from rka_memory.config import Config
from rka_memory.domain import (
    FUZZY_FIELDS,
    GROUNDING,
    REFUTING_KINDS,
    Actor,
    Event,
    Kind,
    Polarity,
    Scope,
    State,
    collapse,
    quoted,
    standing,
)
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


def statement(made_by: Event | None, ev: Event, cfg: Config) -> tuple[str, bool] | None:
    """The user's words a link quotes from the user message it cites, checked
    here against the logged message: (quote, standing), or None when the link
    quotes nothing or misquotes. Standing means the link also quotes words
    next to the rule that hold one of the configured standing phrases, such as
    "from now on"."""
    if made_by is None or ev.kind is not Kind.USER_MSG or ev.actor is not Actor.USER:
        return None
    said = made_by.payload.get("statement")
    text = str(ev.payload.get("text") or "")
    if not isinstance(said, dict) or not isinstance(said.get("quote"), str) or not quoted(text, said["quote"]):
        return None
    marker = said.get("marker")
    return said["quote"], isinstance(marker, str) and standing(text, said["quote"], marker, cfg.gate.standing_phrases)


def counted(rec: RecordState, world: World, cfg: Config, polarity: Polarity) -> list[Grounding]:
    """The links of one polarity that count, one per cited event, in log order."""
    out = []
    for link in rec.links:
        if link.polarity is not polarity:
            continue
        ev = world.events.get(link.event_id)
        if ev is None or ev.actor is Actor.MODEL:
            continue
        if ev.kind in TARGETED_KINDS and not is_targeted(ev, rec.id):
            continue
        made_by = world.events.get(link.via)
        if made_by is not None and made_by.actor is Actor.MODEL and made_by.session_id != ev.session_id:
            continue
        allowed = GROUNDING.get(ev.kind)
        if allowed is None or polarity not in allowed(ev.payload):
            continue
        if ev.id != link.via and not world.before(ev.id, link.via):
            continue
        weight, targeted, affirms = cfg.weight(ev.kind), is_targeted(ev, rec.id), False
        said = statement(made_by, ev, cfg)
        if said is not None and polarity is Polarity.SUPPORT and said[1]:
            # A standing rule in the user's words weighs like a confirm, and
            # confirms the record outright when the record is those words.
            weight = cfg.weight(Kind.USER_CONFIRM)
            affirms = collapse(said[0]) == collapse(rec.spec.content)
        if said is not None and polarity is Polarity.COUNTER:
            # The user's own words against the rule: not scoped away.
            targeted = True
        model_counter = polarity is Polarity.COUNTER and made_by is not None and made_by.actor is Actor.MODEL
        if model_counter and rec.id not in world.received_in(ev.session_id):
            # The session never received the record, so this is no failure of
            # following it: it refutes the claim, and only what the session
            # observed can do that (the second counter class of 5.1).
            if ev.kind not in REFUTING_KINDS:
                continue
            targeted = True
        if polarity is Polarity.SUPPORT and weight <= 0:
            continue
        out.append(Grounding(ev, weight, world.state_of(ev), targeted, affirms))
    unique: dict[str, Grounding] = {}
    for g in out:
        held = unique.get(g.event.id)
        if held is None or (g.affirms, g.weight, g.targeted) > (held.affirms, held.weight, held.targeted):
            unique[g.event.id] = g
    return sorted(unique.values(), key=lambda g: world.position[g.event.id])


def affirmation(g: Grounding) -> bool:
    """A user confirm, or a standing statement of exactly what the record says."""
    return g.event.kind is Kind.USER_CONFIRM or g.affirms


def meets_l2(support: list[Grounding], cfg: Config) -> bool:
    """Weighted grounding of at least k across enough distinct sessions. Weak-only
    grounding (no event of weight >= 1) needs more sessions."""
    sessions = len({g.session_id for g in support})
    needed = cfg.gate.min_sessions
    if not any(g.weight >= 1 for g in support):
        needed = max(needed, cfg.gate.weak_only_min_sessions)
    return sum(g.weight for g in support) >= cfg.gate.k - 1e-9 and sessions >= needed


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
