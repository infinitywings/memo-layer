"""One labelling function over the log (DESIGN_V2 Section 5).

`label(world, cfg, now)` gives every record a rung from evidence alone. It
reads the projection's structure (records, links, deliveries, sessions) and
never the rung state v1's RUNG_CHANGE events fold into it, so the same log
and the same `now` always give the same labels.

Records are labelled in log order. Every edge a record declares (depends_on,
supersedes) points to an earlier record, so a record's dependencies are
labelled before it.

Stratum 1 is the withdrawn set W: records the user withdrew by a typed
reject after the last affirmation, plus every record that depends on them.
Only the user may withdraw, and only by naming the record; a model-linked
counter, even one quoting the user's checked words, demotes its target and
never cascades. A dependency edge declared by the miner does not pull in a
record the user affirmed.

Stratum 2 labels every other record:
- support is counted by the evidence rules shared with v1 (non-model
  emitter, polarity, own session, own target), inside the record's scope;
- support from sessions where an invalid dependency was delivered is set
  aside, as v1's propagation does;
- a fact counts toward L2 only support that is independent: nothing in the
  fact's reach (its versions and dependents) was in the influence of the
  evidence (DESIGN_V2 rule 4);
- a record learned under a withdrawn record counts only support whose
  influence held no withdrawn record (rule 5);
- prescriptive records (procedures, preferences, constraints) reach L2 only
  by the user's affirmation, or by an interventional trial (layer 4); a
  user affirmation is exempt from rules 4 and 5;
- counted counters after the last affirmation block L2; a counter whose
  influence held a withdrawn record is set aside.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from rka_memory.config import Config
from rka_memory.domain import Actor, Event, Kind, Polarity, RecordKind, Rung, State, derived_id, parse_ts
from rka_memory.kernel.evidence import Grounding, affirmation, applicable, counted, inside, meets_l2
from rka_memory.kernel import trials
from rka_memory.kernel.influence import Influence, Reach
from rka_memory.skills.gate import OUTCOME_KINDS, _used_skill, final_outcome, follows, normalize_command, outcome_ok
from rka_memory.world import RecordState, Session, World

DESCRIPTIVE = frozenset({RecordKind.FACT})
DELIVERABLE = frozenset({Rung.L2, Rung.L3})
ENGINE_SESSION = "engine"


@dataclass(frozen=True)
class Label:
    record_id: str
    rung: Rung
    reason: str
    # For L2 and L3: the ledger position at which the record's current
    # unbroken stretch at L2 or above began. Views order by it, so a record
    # keeps its place until it falls, and takes a new one when it returns.
    since: int | None = None
    support: tuple[str, ...] = ()
    set_aside: tuple[tuple[str, str], ...] = ()
    counters: tuple[str, ...] = ()
    affirmed: bool = False
    withdrawn: bool = False
    learned_under: tuple[str, ...] = ()
    # Miner restrictions of this record that have evidence but are not yet
    # valid: the record keeps its rung but is not delivered (DESIGN_V2 6.1).
    withheld_by: tuple[str, ...] = ()
    # For a prescriptive record whose support met the threshold: its
    # randomized trial, "open", "positive" or "closed" (DESIGN_V2 5.3).
    trial: str | None = None

    @property
    def valid(self) -> bool:
        """At L2 or L3: what dependencies and independence read."""
        return self.rung in DELIVERABLE

    @property
    def deliverable(self) -> bool:
        """Valid and not withheld: what views read."""
        return self.valid and not self.withheld_by


@dataclass
class Labels:
    by_id: dict[str, Label] = field(default_factory=dict)
    withdrawn: frozenset[str] = frozenset()

    def rung(self, record_id: str) -> Rung | None:
        label = self.by_id.get(record_id)
        return label.rung if label else None

    def deliverable(self) -> list[str]:
        return [rid for rid, label in self.by_id.items() if label.deliverable]


def valid(labels: Labels, rec: RecordState, state: State, world: World) -> bool:
    """valid(r, s) = grounded(r) AND applicable(r, s), as views read it."""
    label = labels.by_id.get(rec.id)
    return label is not None and label.deliverable and applicable(rec, state, world) is True


def _affirmations(rec: RecordState, world: World, cfg: Config) -> list[Grounding]:
    """The user's affirmations of the record inside its scope: typed confirms,
    and standing statements of exactly its words. Both the withdrawn set and
    the label read this one list, so they cannot disagree about whether the
    user affirmed the record after withdrawing it."""
    return [g for g in counted(rec, world, cfg, Polarity.SUPPORT) if affirmation(g) and inside(rec.scope, g)]


RUNNER_OWNED = frozenset({"citation", "falsifier"})


def _withdrawals(rec: RecordState, world: World, cfg: Config) -> tuple[int, int, str]:
    """Positions of the latest affirmation and the latest withdrawal (-1 if
    none), and the withdrawal's reason.

    Only two authorities withdraw (DESIGN_V2 7.2). The user, by a typed
    reject naming the record, written only from paths the user types into;
    a user message, even one whose words the code checked, is not one: the
    miner chose which record those words withdraw, so such a link is a
    counter that demotes and never cascades. And the runner, when a check it
    owns (a citation or a falsifier of the record) fails."""
    last_aff = max((world.position[g.event.id] for g in _affirmations(rec, world, cfg)), default=-1)
    last_wd, why = -1, ""
    for link in rec.links:
        ev = world.events.get(link.event_id)
        if link.polarity is not Polarity.COUNTER or ev is None or ev.payload.get("record_id") != rec.id:
            continue
        if ev.kind is Kind.USER_REJECT and ev.actor is Actor.USER:
            reason = "withdrawn by the user"
        elif ev.kind is Kind.CHECK and ev.actor is Actor.RUNNER and ev.payload.get("check") in RUNNER_OWNED:
            reason = f"a runner-owned check failed: {ev.payload['check']}"
        else:
            continue
        if world.position[ev.id] > last_wd:
            last_wd, why = world.position[ev.id], reason
    return last_aff, last_wd, why


def _withdrawn_set(world: World, cfg: Config, reach: Reach) -> tuple[frozenset[str], dict[str, str]]:
    """The withdrawn set and why each member is in it: its own withdrawal, or
    a dependency on a record the user (or a failed check) withdrew. A record
    the user affirmed is never pulled in by a dependency edge."""
    roots: dict[str, str] = {}
    affirmed = set()
    for rec in world.records.values():
        last_aff, last_wd, why = _withdrawals(rec, world, cfg)
        if last_wd > last_aff:
            roots[rec.id] = why
        elif last_aff >= 0:
            affirmed.add(rec.id)
    reasons = dict(roots)
    by_user = frozenset(r for r, why in roots.items() if why == "withdrawn by the user")
    for root_set, why in ((by_user, "depends on a record the user withdrew"), (frozenset(roots) - by_user, "depends on a record a failed check withdrew")):
        for rid in reach.dependents(root_set) - set(roots) - affirmed:
            reasons.setdefault(rid, why)
    return frozenset(reasons), reasons


def _met(dep: str, world: World, done: Labels, withdrawn: frozenset[str]) -> bool:
    """A dependency is met by the record or by the user's standing
    replacement of it: a correction keeps the claim, so what depends on the
    claim follows the current wording."""
    if done.by_id[dep].valid:
        return True
    return any(a.record_id not in withdrawn for a in world.replaced_by(dep))


def _touches(rec: RecordState, world: World) -> list[tuple[int, datetime]]:
    """When the record was touched: proposed, given support, or delivered on
    a channel the runner drives. A counter link is not a touch, or a claim the
    miner keeps refuting would never retire; nor is an MCP request, or the
    agent could keep a record alive by asking for it."""
    events = [rec.proposed] + [
        world.events[link.via] for link in rec.links if link.polarity is Polarity.SUPPORT and link.via in world.events
    ]
    events += [d.event for d in world.deliveries if rec.id in d.record_ids and d.channel not in ("hints", "mcp")]
    return sorted((world.position[e.id], parse_ts(e.ts)) for e in events)


def _retired(rec: RecordState, world: World, cfg: Config, now: datetime, valid_from: Event | None) -> bool:
    """Untouched for the retirement window before the record first became
    valid (at event `valid_from`), or, if it is not valid, at any time up to
    now. Retirement is terminal: evidence that arrives after it does not
    revive the record; only the user's affirmation does."""
    window = timedelta(days=cfg.gate.retire_after_days)
    cut = world.position[valid_from.id] if valid_from is not None else len(world.position)
    stamps = [t for p, t in _touches(rec, world) if p <= cut]
    stamps.append(parse_ts(valid_from.ts) if valid_from is not None else now)
    return any(b - a > window for a, b in zip(stamps, stamps[1:]))


def _since(support: list[Grounding], world: World, cfg: Config) -> int | None:
    """The position of the evidence that first met the L2 threshold."""
    for i in range(1, len(support) + 1):
        if meets_l2(support[:i], cfg):
            return world.position[support[i - 1].event.id]
    return None


def _sessions_after(world: World, position: int) -> list[Session]:
    return [
        s for s in world.sessions.values()
        if s.started is not None and s.ended is not None and world.position[s.started.id] > position
    ]


def _skill(rec: RecordState, world: World, cfg: Config, since: int) -> tuple[bool, str]:
    """L3 for an L2 procedure: followed with success in enough later sessions,
    its acceptance check passed among them, and no failure after it was used."""
    check = normalize_command(rec.spec.acceptance_check or "")
    later = _sessions_after(world, since)
    followed = []
    for sess in later:
        if rec.scope.covers(sess.state) and follows(sess, rec.spec.steps):
            fin = final_outcome(sess)
            if fin is not None and outcome_ok(fin):
                followed.append(sess)
    passed = any(
        ev.kind in OUTCOME_KINDS and ev.actor is Actor.TOOL
        and normalize_command(str(ev.payload.get("command", ""))) == check and outcome_ok(ev)
        for sess in followed for ev in sess.events
    )
    if len(followed) < cfg.gate.skill_min_sessions or not passed:
        return False, ""
    using = set()
    for sess in later:
        start = _used_skill(sess, rec)
        if start is None:
            continue
        using.add(sess.id)
        runs = [e for e in sess.events[start:] if e.kind in OUTCOME_KINDS and normalize_command(str(e.payload.get("command", ""))) == check]
        if runs and not outcome_ok(runs[-1]):
            return False, "acceptance check failed after the skill was used"
    for ev in world.events.values():
        if ev.kind is Kind.USER_UNDO and world.position[ev.id] > since and ev.payload.get("write_session_id") in using:
            return False, "user undid a file written while the skill was in use"
    return True, f"followed and succeeded in {len(followed)} later sessions"


def label(world: World, cfg: Config, now: datetime) -> Labels:
    influence, reach = Influence(world), Reach(world, cfg)
    withdrawn, roots = _withdrawn_set(world, cfg, reach)
    out = Labels(withdrawn=withdrawn)
    held = influence.sessions_holding
    ordered = list(world.events.values())
    for rec in sorted(world.records.values(), key=lambda r: world.position[r.proposed.id]):
        out.by_id[rec.id] = _label_one(rec, world, cfg, now, influence, reach, withdrawn, roots, held, out, ordered)
    _withhold_pending_restrictions(world, out)
    return out


def _withhold_pending_restrictions(world: World, labels: Labels) -> None:
    """A model-proposed qualification with evidence of its own (L1) may be
    true, and delivering its target alone would deliver the unqualified claim,
    so the target is withheld until the qualification is valid or withdrawn.
    A target the user affirmed is never withheld, and a proposal with no
    counted evidence withholds nothing: a model proposal can withhold only at
    the cost of evidence, and never over the user's word."""
    pending: dict[str, list[str]] = {}
    for rec in world.records.values():
        lab = labels.by_id[rec.id]
        if rec.proposed.kind is not Kind.RECORD_PROPOSED or lab.rung is not Rung.L1:
            continue
        for target in rec.spec.refines:
            held = labels.by_id.get(target)
            if held is not None and held.valid and not held.affirmed:
                pending.setdefault(target, []).append(rec.id)
    for target, by in pending.items():
        labels.by_id[target] = replace(labels.by_id[target], withheld_by=tuple(sorted(by)))


def _label_one(
    rec: RecordState,
    world: World,
    cfg: Config,
    now: datetime,
    influence: Influence,
    reach: Reach,
    withdrawn: frozenset[str],
    roots: dict[str, str],
    held: dict[str, frozenset[str]],
    done: Labels,
    ordered: list[Event],
) -> Label:
    rid = rec.id
    if rid in withdrawn:
        return Label(rid, Rung.L4, roots[rid], withdrawn=True)
    standing = [a for a in world.replaced_by(rid) if a.record_id not in withdrawn]
    if standing:
        # The user's replacement stands; it reopens the old version only if
        # the user withdraws it, not if it merely loses support.
        a = standing[-1]
        return Label(rid, Rung.L4, f"{'corrected' if a.op == 'correct' else 'replaced'} by the user with {a.record_id}")

    set_aside: list[tuple[str, str]] = []
    support = [g for g in counted(rec, world, cfg, Polarity.SUPPORT) if inside(rec.scope, g)]

    bad_deps = [d for d in rec.spec.dependencies if d in done.by_id and not _met(d, world, done, withdrawn)]
    tainted = set().union(*(held.get(d, frozenset()) for d in bad_deps)) if bad_deps else set()
    kept = []
    for g in support:
        if g.session_id in tainted and not g.targeted and not affirmation(g):
            set_aside.append((g.event.id, "observed where an invalid dependency was delivered"))
        else:
            kept.append(g)
    support = kept

    if rec.spec.kind is RecordKind.PREFERENCE and support and not any(g.event.actor is Actor.USER for g in support):
        return Label(rid, Rung.L0D, "a preference needs the user's own evidence", set_aside=tuple(set_aside))
    if not support:
        why = "all support was observed where an invalid dependency was delivered" if set_aside else "no counted non-model evidence"
        return Label(rid, Rung.L0D, why, set_aside=tuple(set_aside))

    affirmations = _affirmations(rec, world, cfg)
    last_aff = max((world.position[g.event.id] for g in affirmations), default=-1)

    learned_under = tuple(sorted(w for w in withdrawn if w in influence.session(rec.proposed.session_id)))
    counters, earlier = [], []
    for g in counted(rec, world, cfg, Polarity.COUNTER):
        if not inside(rec.scope, g):
            continue
        if influence.of(g.event, rec) & withdrawn:
            if world.position[g.event.id] > last_aff:
                set_aside.append((g.event.id, "counter observed under a withdrawn record"))
            continue
        (earlier if world.position[g.event.id] < last_aff else counters).append(g.event.id)

    usable = []
    own_reach = reach.of(rid)
    for g in support:
        if affirmation(g):
            usable.append(g)
            continue
        seen = influence.of(g.event, rec)
        if learned_under and seen & withdrawn:
            set_aside.append((g.event.id, "observed under a withdrawn record it was learned under"))
            continue
        if rec.spec.kind in DESCRIPTIVE and seen & own_reach:
            set_aside.append((g.event.id, "not independent: the record or a dependent was in context"))
            continue
        usable.append(g)

    common = dict(support=tuple(g.event.id for g in usable), set_aside=tuple(set_aside), counters=tuple(counters),
                  affirmed=bool(affirmations), learned_under=learned_under)
    threshold = _since([g for g in usable if not affirmation(g)], world, cfg) if rec.spec.kind in DESCRIPTIVE else None
    l2, since, why = False, None, ""
    if counters:
        why = "counter-evidence after the last affirmation"
    elif affirmations:
        l2 = True
        kinds = {g.event.kind for g in affirmations}
        why = (
            "user confirmed" if Kind.USER_CONFIRM in kinds
            else "written by the user" if Kind.USER_AMEND in kinds
            else "the user stated it as a standing rule"
        )
        # The stretch at L2 began at the first affirmation after the record
        # last fell (a counter, or the user's withdrawal), or, if it never
        # fell, when it first met the threshold, if that came earlier.
        fall = max([world.position[e] for e in earlier] + [_withdrawals(rec, world, cfg)[1]], default=-1)
        since = min(world.position[g.event.id] for g in affirmations if world.position[g.event.id] > fall)
        if fall < 0 and threshold is not None:
            since = min(since, threshold)
    elif threshold is not None:
        l2, since, why = True, threshold, "independent grounding in enough distinct sessions"
    elif rec.spec.kind in DESCRIPTIVE:
        why = "independent grounding below threshold"
    elif meets_l2(support, cfg):
        # Its own evidence would validate a fact, so it may be tested: a
        # randomized trial decides (DESIGN_V2 5.3).
        tr = trials.evaluate(rec, world, cfg)
        common["trial"] = tr.status
        why = tr.reason()
        if tr.status == "positive":
            l2, since = True, tr.completed_at
    else:
        why = "prescriptive: needs the user's affirmation or a trial"

    retire = f"untouched for {cfg.gate.retire_after_days:g} days"
    valid_from = ordered[threshold] if threshold is not None else None
    if l2 and not affirmations and _retired(rec, world, cfg, now, valid_from):
        return Label(rid, Rung.L4, retire, **common)
    if l2:
        if rec.spec.kind is RecordKind.PROCEDURE and rec.spec.steps and rec.spec.acceptance_check:
            ok, skill_why = _skill(rec, world, cfg, since)
            if ok:
                return Label(rid, Rung.L3, skill_why, since=since, **common)
            if skill_why:
                return Label(rid, Rung.L4, skill_why, since=since, **common)
        return Label(rid, Rung.L2, why, since=since, **common)

    if affirmations or threshold is not None:
        # Once valid, it retires only from its last touch on; a gap before it
        # became valid counts unless the user has affirmed it since.
        last = _touches(rec, world)[-1][1]
        retired = now - last > timedelta(days=cfg.gate.retire_after_days)
        retired = retired or (valid_from is not None and not affirmations and _retired(rec, world, cfg, now, valid_from))
    else:
        retired = _retired(rec, world, cfg, now, None)
    if retired:
        return Label(rid, Rung.L4, retire, **common)
    return Label(rid, Rung.L1, why, **common)


def audit_events(world: World, labels: Labels, now: datetime) -> list[Event]:
    """RUNG_CHANGE events that bring the projection's cached rungs to the
    labels. They are a derived audit trail for reports and history, never an
    input: the kernel does not read them, and replaying the log recomputes
    the labels they must agree with. Ids derive from the move and its
    evidence, so two gates racing on one ledger write the same events."""
    out = []
    for rec in sorted(world.records.values(), key=lambda r: world.position[r.proposed.id]):
        lab = labels.by_id[rec.id]
        if rec.rung is lab.rung:
            continue
        before = rec.rung.value if rec.rung else None
        payload: dict = {
            "record_id": rec.id,
            "from": before,
            "to": lab.rung.value,
            "cause_ids": [*lab.support, *lab.counters],
            "reason": lab.reason,
            "since": lab.since,
            "derived": "kernel",
        }
        if lab.rung is Rung.L3:
            payload["acceptance_check"] = rec.spec.acceptance_check
        ev_id = derived_id("ev", "rung", rec.id, before or "", lab.rung.value, lab.reason, *payload["cause_ids"])
        out.append(Event.new(Kind.RUNG_CHANGE, Actor.RUNNER, ENGINE_SESSION, payload, id=ev_id, ts=now.isoformat()))
    return out
