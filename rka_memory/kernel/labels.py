"""One labelling function over the log (DESIGN_V2 Section 5).

`label(world, cfg, now)` gives every record a rung from evidence alone. It
reads the projection's structure (records, links, deliveries, sessions) and
never the rung state v1's RUNG_CHANGE events fold into it, so the same log
and the same `now` always give the same labels.

Records are labelled in log order. Every edge a record declares (depends_on,
supersedes) points to an earlier record, so a record's dependencies are
labelled before it.

Stratum 1 is the withdrawn set W: records the user withdrew (a typed reject,
or a standing statement withdrawing the rule, after the last affirmation),
plus every record that depends on them. Only the user may withdraw; a
model-linked counter demotes its target and never cascades. A dependency
edge declared by the miner does not pull in a record the user affirmed.

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

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rka_memory.config import Config
from rka_memory.domain import Actor, Kind, Polarity, RecordKind, Rung, State, parse_ts
from rka_memory.kernel.influence import Influence, Reach
from rka_memory.skills.gate import OUTCOME_KINDS, _used_skill, final_outcome, follows, normalize_command, outcome_ok
from rka_memory.validity.engine import affirmation, counted, meets_l2, statement
from rka_memory.validity.scope import Grounding, applicable, inside
from rka_memory.world import RecordState, Session, World

DESCRIPTIVE = frozenset({RecordKind.FACT})
DELIVERABLE = frozenset({Rung.L2, Rung.L3})


@dataclass(frozen=True)
class Label:
    record_id: str
    rung: Rung
    reason: str
    # Ledger position at which the evidence for the current L2 or L3 was complete.
    since: int | None = None
    support: tuple[str, ...] = ()
    set_aside: tuple[tuple[str, str], ...] = ()
    counters: tuple[str, ...] = ()
    affirmed: bool = False
    withdrawn: bool = False
    learned_under: tuple[str, ...] = ()

    @property
    def deliverable(self) -> bool:
        return self.rung in DELIVERABLE


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


def _withdrawals(rec: RecordState, world: World, cfg: Config) -> tuple[int, int]:
    """Positions of the latest affirmation and the latest withdrawal by the user (-1 if none).

    A withdrawal is the user's own act: a typed reject naming the record, or
    a user message whose quoted words the code checked when the link was
    counted. A model link to a user message that merely contradicts the
    record is a counter, not a withdrawal, and never cascades."""
    last_aff = max((world.position[g.event.id] for g in counted(rec, world, cfg, Polarity.SUPPORT) if affirmation(g)), default=-1)
    last_wd = -1
    for link in rec.links:
        if link.polarity is not Polarity.COUNTER:
            continue
        ev, made_by = world.events.get(link.event_id), world.events.get(link.via)
        if ev is None or ev.actor is not Actor.USER:
            continue
        if ev.kind is Kind.USER_REJECT and ev.payload.get("record_id") == rec.id:
            last_wd = max(last_wd, world.position[ev.id])
        elif ev.kind is Kind.USER_MSG and made_by is not None and statement(made_by, ev, cfg) is not None:
            last_wd = max(last_wd, world.position[ev.id])
    return last_aff, last_wd


def _withdrawn_set(world: World, cfg: Config, reach: Reach) -> tuple[frozenset[str], frozenset[str]]:
    roots, affirmed = set(), set()
    for rec in world.records.values():
        last_aff, last_wd = _withdrawals(rec, world, cfg)
        if last_wd > last_aff:
            roots.add(rec.id)
        elif last_aff >= 0:
            affirmed.add(rec.id)
    closure = reach.dependents(frozenset(roots))
    return frozenset(roots | {rid for rid in closure if rid not in affirmed}), frozenset(roots)


def _last_touched(rec: RecordState, world: World) -> datetime:
    stamps = [rec.proposed.ts] + [
        world.events[link.via].ts for link in rec.links if link.polarity is Polarity.SUPPORT and link.via in world.events
    ]
    stamps += [d.event.ts for d in world.deliveries if rec.id in d.record_ids and d.channel not in ("hints", "mcp")]
    return max(parse_ts(ts) for ts in stamps)


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
    influence, reach = Influence(world), Reach(world)
    withdrawn, roots = _withdrawn_set(world, cfg, reach)
    out = Labels(withdrawn=withdrawn)
    held = influence.sessions_holding
    for rec in sorted(world.records.values(), key=lambda r: world.position[r.proposed.id]):
        out.by_id[rec.id] = _label_one(rec, world, cfg, now, influence, reach, withdrawn, roots, held, out)
    return out


def _label_one(
    rec: RecordState,
    world: World,
    cfg: Config,
    now: datetime,
    influence: Influence,
    reach: Reach,
    withdrawn: frozenset[str],
    roots: frozenset[str],
    held: dict[str, frozenset[str]],
    done: Labels,
) -> Label:
    rid = rec.id
    if rid in withdrawn:
        why = "withdrawn by the user" if rid in roots else "depends on a record the user withdrew"
        return Label(rid, Rung.L4, why, withdrawn=True)

    set_aside: list[tuple[str, str]] = []
    support = [g for g in counted(rec, world, cfg, Polarity.SUPPORT) if inside(rec.scope, g)]

    bad_deps = [d for d in rec.spec.depends_on if d in done.by_id and not done.by_id[d].deliverable]
    tainted = set().union(*(held.get(d, frozenset()) for d in bad_deps)) if bad_deps else set()
    kept = []
    for g in support:
        if g.session_id in tainted and not g.targeted:
            set_aside.append((g.event.id, "observed where an invalid dependency was delivered"))
        else:
            kept.append(g)
    support = kept

    if rec.spec.kind is RecordKind.PREFERENCE and support and not any(g.event.actor is Actor.USER for g in support):
        return Label(rid, Rung.L0D, "a preference needs the user's own evidence", set_aside=tuple(set_aside))
    if not support:
        why = "all support was observed where an invalid dependency was delivered" if set_aside else "no counted non-model evidence"
        return Label(rid, Rung.L0D, why, set_aside=tuple(set_aside))

    affirmations = [g for g in support if affirmation(g)]
    last_aff = max((world.position[g.event.id] for g in affirmations), default=-1)

    learned_under = tuple(sorted(w for w in withdrawn if w in influence.in_session(rec.proposed.session_id)))
    counters = []
    for g in counted(rec, world, cfg, Polarity.COUNTER):
        if not inside(rec.scope, g) or world.position[g.event.id] < last_aff:
            continue
        if influence.of(g.event) & withdrawn:
            set_aside.append((g.event.id, "counter observed under a withdrawn record"))
            continue
        counters.append(g.event.id)

    usable = []
    own_reach = reach.of(rid)
    for g in support:
        if affirmation(g):
            usable.append(g)
            continue
        seen = influence.of(g.event)
        if learned_under and seen & withdrawn:
            set_aside.append((g.event.id, "observed under a withdrawn record it was learned under"))
            continue
        if rec.spec.kind in DESCRIPTIVE and seen & own_reach:
            set_aside.append((g.event.id, "not independent: the record or a dependent was in context"))
            continue
        usable.append(g)

    common = dict(support=tuple(g.event.id for g in usable), set_aside=tuple(set_aside), counters=tuple(counters),
                  affirmed=bool(affirmations), learned_under=learned_under)
    l2, since, why = False, None, ""
    if counters:
        why = "counter-evidence after the last affirmation"
    elif affirmations:
        l2, since = True, last_aff
        why = "user confirmed" if any(g.event.kind is Kind.USER_CONFIRM for g in affirmations) else "the user stated it as a standing rule"
    elif rec.spec.kind in DESCRIPTIVE:
        independent = [g for g in usable if not affirmation(g)]
        since = _since(independent, world, cfg)
        if since is not None:
            l2, why = True, "independent grounding in enough distinct sessions"
        else:
            why = "independent grounding below threshold"
    else:
        why = "prescriptive: needs the user's affirmation or a trial"

    if l2:
        if rec.spec.kind is RecordKind.PROCEDURE and rec.spec.steps and rec.spec.acceptance_check:
            ok, skill_why = _skill(rec, world, cfg, since)
            if ok:
                return Label(rid, Rung.L3, skill_why, since=since, **common)
            if skill_why:
                return Label(rid, Rung.L4, skill_why, since=since, **common)
        return Label(rid, Rung.L2, why, since=since, **common)

    if now - _last_touched(rec, world) > timedelta(days=cfg.gate.retire_after_days):
        return Label(rid, Rung.L4, f"untouched for {cfg.gate.retire_after_days:g} days", **common)
    return Label(rid, Rung.L1, why, **common)
