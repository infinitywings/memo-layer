"""The validity engine: the only code that changes rungs or scopes.

`evaluate` is deterministic. Given a world and `now`, it returns the
RUNG_CHANGE and SCOPE_NARROWED events the ladder rules require, already
folded into the world. Running it again on the result returns nothing.
Event ids derive from the decision, so two gates racing on the same ledger
write the same ids and the projection keeps one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from rka_memory.config import Config
from rka_memory.domain import (
    DELIVERABLE,
    GROUNDING,
    REFUTING_KINDS,
    Actor,
    Event,
    Kind,
    Polarity,
    RecordKind,
    Rung,
    Scope,
    State,
    collapse,
    derived_id,
    parse_ts,
    quoted,
    standing,
)
from rka_memory.skills.gate import check_passed, sessions_following, skill_failure
from rka_memory.validity import propagate
from rka_memory.validity.scope import (
    TARGETED_KINDS,
    Grounding,
    applicable,
    choose_narrowing,
    inside,
    is_targeted,
)
from rka_memory.world import RecordState, World

ENGINE_SESSION = "engine"
UNBACKED = "a preference needs the user's own evidence"


@dataclass(frozen=True)
class Assessment:
    """Everything the rules need to know about one record right now.
    `unbacked` marks a preference grounded only by events the user did not
    author, such as tests: its support counts as empty, since only the user
    shows what the user wants."""

    support: list[Grounding]
    counters: list[Grounding]
    last_confirm: Grounding | None
    unbacked: bool = False


def statement(made_by: Event | None, ev: Event, cfg: Config) -> tuple[str, bool] | None:
    """The user's words a link quotes from the user message it cites, checked
    here against the logged message with the extractor's own check:
    (quote, standing), or None when the link quotes nothing or misquotes.
    Standing means the link also quotes words next to the rule that hold one
    of the configured standing phrases, such as "from now on"."""
    if made_by is None or ev.kind is not Kind.USER_MSG or ev.actor is not Actor.USER:
        return None
    said = made_by.payload.get("statement")
    text = str(ev.payload.get("text") or "")
    if not isinstance(said, dict) or not isinstance(said.get("quote"), str) or not quoted(text, said["quote"]):
        return None
    marker = said.get("marker")
    return said["quote"], isinstance(marker, str) and standing(text, said["quote"], marker, cfg.gate.standing_phrases)


def counted(rec: RecordState, world: World, cfg: Config, polarity: Polarity) -> list[Grounding]:
    """Links whose cited event can ground: not model-authored, a grounding
    kind, a polarity that event can carry, and already in the ledger when the
    link was made. A user verdict counts only for the record it names, and a
    model-made link only cites events from the session it was made in, so the
    miner cannot carry outcomes or verdicts across to unrelated claims."""
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
            # The user withdrew the rule: like a reject, it is not scoped away.
            targeted = True
        model_counter = polarity is Polarity.COUNTER and made_by is not None and made_by.actor is Actor.MODEL
        if model_counter and rec.id not in world.received_in(ev.session_id):
            # The session never received the record, so this is no failure of
            # following it here: it refutes the claim, and only what the
            # session observed can do that. Like a reject, it is not scoped away.
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


def assess(rec: RecordState, world: World, cfg: Config, scope: Scope | None = None) -> Assessment:
    scope = scope or rec.scope
    tainted = propagate.tainted_sessions(rec, world)
    support = [
        g
        for g in counted(rec, world, cfg, Polarity.SUPPORT)
        if inside(scope, g) and (g.targeted or g.session_id not in tainted)
    ]
    unbacked = (
        rec.spec.kind is RecordKind.PREFERENCE and bool(support) and not any(g.event.actor is Actor.USER for g in support)
    )
    if unbacked:
        support = []
    confirms = [g for g in support if affirmation(g)]
    last_confirm = confirms[-1] if confirms else None
    counters = [
        g
        for g in counted(rec, world, cfg, Polarity.COUNTER)
        if inside(scope, g) and (last_confirm is None or world.before(last_confirm.event.id, g.event.id))
    ]
    return Assessment(support, counters, last_confirm, unbacked)


def meets_l2(support: list[Grounding], cfg: Config) -> bool:
    """Weighted grounding of at least k across enough distinct sessions. Weak-only
    grounding (no event of weight >= 1) needs more sessions: weak signals are
    evidence, not labels."""
    sessions = len({g.session_id for g in support})
    needed = cfg.gate.min_sessions
    if not any(g.weight >= 1 for g in support):
        needed = max(needed, cfg.gate.weak_only_min_sessions)
    return sum(g.weight for g in support) >= cfg.gate.k - 1e-9 and sessions >= needed


def sufficient_for_l2(support: list[Grounding], cfg: Config) -> bool:
    """Enough grounding to hold L2: the k/sessions threshold, or a user confirm
    (a standing statement of exactly the record's words counts as one)."""
    if cfg.gate.confirm_overrides and any(affirmation(g) for g in support):
        return True
    return meets_l2(support, cfg)


def _keeps(rung: Rung, cfg: Config):
    if rung in DELIVERABLE:
        return lambda support: sufficient_for_l2(support, cfg)
    return lambda support: bool(support)


def _last_touched(rec: RecordState, world: World) -> datetime:
    # A counter link is not a touch: a claim the miner keeps refuting would
    # otherwise never retire.
    stamps = [rec.proposed.ts] + [
        world.events[link.via].ts for link in rec.links if link.polarity is Polarity.SUPPORT and link.via in world.events
    ]
    # Hints and MCP calls are model-driven; letting them count would let the
    # agent keep an unvalidated record alive just by asking for it.
    stamps += [d.event.ts for d in world.deliveries if rec.id in d.record_ids and d.channel not in ("hints", "mcp")]
    return max(parse_ts(ts) for ts in stamps)


def _rung(rec: RecordState, to: Rung, causes: list[str], reason: str, now: datetime, **extra) -> Event:
    payload = {
        "record_id": rec.id,
        "from": rec.rung.value if rec.rung else None,
        "to": to.value,
        "cause_ids": causes,
        "reason": reason,
        **extra,
    }
    ev_id = derived_id("ev", "rung", rec.id, payload["from"] or "", to.value, *causes, reason)
    return Event.new(Kind.RUNG_CHANGE, Actor.RUNNER, ENGINE_SESSION, payload, id=ev_id, ts=now.isoformat())


def _narrow(rec: RecordState, name: str, value: str, cause: Grounding, now: datetime) -> Event:
    payload = {"record_id": rec.id, "field": name, "value": value, "cause_ids": [cause.event.id]}
    ev_id = derived_id("ev", "narrow", rec.id, name, value, cause.event.id)
    return Event.new(Kind.SCOPE_NARROWED, Actor.RUNNER, ENGINE_SESSION, payload, id=ev_id, ts=now.isoformat())


def _ids(gs: list[Grounding]) -> list[str]:
    return [g.event.id for g in gs]


def step(rec: RecordState, world: World, cfg: Config, now: datetime) -> Event | None:
    """The single next event this record needs, or None if it is settled."""
    rung = rec.rung
    if rung is Rung.L4:
        return None
    a = assess(rec, world, cfg)

    if rung is None:
        if a.support:
            return _rung(rec, Rung.L1, _ids(a.support), "proposed with non-model grounding", now)
        if a.unbacked:
            return _rung(rec, Rung.L0D, [rec.proposed.id], UNBACKED, now)
        return _rung(rec, Rung.L0D, [rec.proposed.id], "proposed without non-model grounding", now)

    if rung in (Rung.L0D, Rung.L1) and cfg.gate.confirm_overrides and a.last_confirm and not a.counters:
        why = "user confirmed" if a.last_confirm.event.kind is Kind.USER_CONFIRM else "the user stated it as a standing rule"
        return _rung(rec, Rung.L2, [a.last_confirm.event.id], why, now)

    if a.unbacked and rung in (Rung.L1, Rung.L2):
        return _rung(rec, rung.one_below(), [rec.proposed.id], UNBACKED, now)

    if a.counters and rung is not Rung.L0D:
        counter = a.counters[0]
        choice = choose_narrowing(rec.scope, counter, a.support, _keeps(rung, cfg), cfg.gate.narrow_priority)
        if choice is not None:
            return _narrow(rec, *choice, counter, now)
        if rung is Rung.L3:
            return _rung(rec, Rung.L4, _ids(a.counters), "counter-event inside scope", now)
        if rung is Rung.L2:
            return _rung(rec, Rung.L1, _ids(a.counters), "counter-event inside scope", now)

    if rung is Rung.L3:
        failure = skill_failure(rec, world)
        if failure is not None:
            ev, reason = failure
            return _rung(rec, Rung.L4, [ev.id], reason, now)
        fresh = propagate.fresh_dependency_causes(rec, world)
        if fresh and not sufficient_for_l2(a.support, cfg):
            return _rung(rec, Rung.L2, fresh, "dependency invalidated", now)
        return None

    if rung is Rung.L2:
        fresh = propagate.fresh_dependency_causes(rec, world)
        if fresh and not sufficient_for_l2(a.support, cfg):
            return _rung(rec, Rung.L1, fresh, "dependency invalidated", now)
        if rec.spec.kind is RecordKind.PROCEDURE and rec.spec.steps and rec.spec.acceptance_check:
            follows = sessions_following(rec, world)
            observed = check_passed([s for s, _ in follows], rec.spec.acceptance_check)
            if len(follows) >= cfg.gate.skill_min_sessions and observed is not None:
                return _rung(
                    rec,
                    Rung.L3,
                    [fin.id for _, fin in follows] + [observed.id],
                    f"procedure followed and succeeded in {len(follows)} later sessions",
                    now,
                    acceptance_check=rec.spec.acceptance_check,
                )
        return None

    if rung is Rung.L1:
        if not a.counters and meets_l2(a.support, cfg):
            return _rung(rec, Rung.L2, _ids(a.support), "grounded in enough distinct sessions", now)
        fresh = propagate.fresh_dependency_causes(rec, world)
        if fresh and not a.support:
            if all(d.rung is Rung.L4 for d in propagate.invalid_dependencies(rec, world)):
                return _rung(rec, Rung.L4, fresh, "evidence only from a retired dependency", now)
            return _rung(rec, Rung.L0D, fresh, "no independent grounding", now)
        if now - _last_touched(rec, world) > timedelta(days=cfg.gate.retire_after_days):
            return _rung(rec, Rung.L4, [], f"untouched for {cfg.gate.retire_after_days:g} days", now)
        return None

    if rung is Rung.L0D and a.support:
        return _rung(rec, Rung.L1, _ids(a.support), "non-model grounding arrived", now)
    return None


def evaluate(world: World, cfg: Config, now: datetime) -> list[Event]:
    """Apply ladder rules until nothing moves. Mutates `world`; returns new events."""
    emitted: list[Event] = []
    limit = 8 * (len(world.records) + 1)
    for _ in range(limit):
        moved = False
        for rec in sorted(world.records.values(), key=lambda r: world.position[r.proposed.id]):
            ev = step(rec, world, cfg, now)
            if ev is not None:
                world.apply(ev)
                emitted.append(ev)
                moved = True
        if not moved:
            return emitted
    raise RuntimeError("validity engine did not converge; a rule is oscillating")


def valid(rec: RecordState, state: State, world: World, cfg: Config) -> bool:
    """valid(r, s) = grounded(r) AND applicable(r, s), as the packet reads it."""
    if rec.rung not in DELIVERABLE:
        return False
    a = assess(rec, world, cfg)
    return bool(a.support) and not a.counters and applicable(rec, state, world) is True


def affirmed(rec: RecordState, world: World, cfg: Config) -> bool:
    """The user confirmed the record, or stated its words as a standing rule,
    and no counter-event inside scope came after."""
    return assess(rec, world, cfg).last_confirm is not None
