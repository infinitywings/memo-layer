"""C6 counters, computed from the ledger projection alone."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rka_memory.config import ARMS as ARM_NAMES
from rka_memory.config import Config
from rka_memory.domain import Kind, Polarity, Rung, Scope
from rka_memory.skills.gate import final_outcome, outcome_ok
from rka_memory.world import RecordState, RungMove, Session, World

PACKET_CHANNELS = frozenset({"session_start", "prompt", "mcp"})
ARMS = tuple(sorted(ARM_NAMES))
RUNG_KEYS = ("L0D", "L1", "L2", "L3", "L4", "pending")
COST_KEYS = ("miner_tokens", "miner_seconds", "gate_seconds")


def _demotions(rec: RecordState) -> list[RungMove]:
    return [m for m in rec.history if m.from_rung is not None and m.to_rung.order < m.from_rung.order]


def _promotions(rec: RecordState) -> list[RungMove]:
    return [
        m
        for m in rec.history
        if m.to_rung in (Rung.L2, Rung.L3) and (m.from_rung is None or m.from_rung.order < m.to_rung.order)
    ]


def _rate(misapplied: int, sessions: int) -> dict[str, Any]:
    return {
        "sessions": sessions,
        "misapplied": misapplied,
        "rate": round(misapplied / sessions, 4) if sessions else None,
    }


def misapplication(world: World) -> dict[str, Any]:
    """Sessions given a non-empty packet in which a delivered record was later
    demoted, over sessions given a non-empty packet; overall and per arm."""
    received: dict[str, set[str]] = {}
    misapplied: dict[str, set[str]] = {}
    for d in world.deliveries:
        if d.channel not in PACKET_CHANNELS or not d.record_ids:
            continue
        sid = d.event.session_id
        received.setdefault(d.arm, set()).add(sid)
        delivered_at = world.position[d.event.id]
        for rid in d.record_ids:
            rec = world.records.get(rid)
            if rec and any(world.position[m.event_id] > delivered_at for m in _demotions(rec)):
                misapplied.setdefault(d.arm, set()).add(sid)
    all_received = set().union(*received.values())
    all_misapplied = set().union(*misapplied.values())
    return {
        "overall": _rate(len(all_misapplied), len(all_received)),
        "by_arm": {
            arm: _rate(len(misapplied.get(arm, ())), len(received.get(arm, ())))
            for arm in sorted(set(ARMS) | set(received))
        },
    }


def contradiction(world: World, cfg: Config) -> dict[str, Any]:
    """Sessions given a non-empty packet in which a delivered record was later
    shown wrong, over sessions given a non-empty packet; overall and per arm.
    Wrong means demoted, narrowed, or contradicted by a counted counter-event
    observed after the delivery. The spec's misapplication rate counts only
    demotions, and a counter-event never demotes an L1 record, so a wrong
    unvalidated record delivered by the ungated arm would never count there."""
    from rka_memory.validity.engine import counted

    received: dict[str, set[str]] = {}
    wrong: dict[str, set[str]] = {}
    counters = {rid: counted(rec, world, cfg, Polarity.COUNTER) for rid, rec in world.records.items()}
    for d in world.deliveries:
        if d.channel not in PACKET_CHANNELS or not d.record_ids:
            continue
        sid = d.event.session_id
        received.setdefault(d.arm, set()).add(sid)
        at = world.position[d.event.id]
        for rid in d.record_ids:
            rec = world.records.get(rid)
            if rec is None:
                continue
            later = [world.position[m.event_id] for m in _demotions(rec)]
            later += [world.position[n.event_id] for n in rec.narrowings]
            later += [world.position[g.event.id] for g in counters[rid]]
            if any(p > at for p in later):
                wrong.setdefault(d.arm, set()).add(sid)
    return {
        "overall": _rate(len(set().union(*wrong.values())), len(set().union(*received.values()))),
        "by_arm": {
            arm: _rate(len(wrong.get(arm, ())), len(received.get(arm, ())))
            for arm in sorted(set(ARMS) | set(received))
        },
    }


def _scope_at(rec: RecordState, world: World, position: int) -> Scope:
    scope = rec.spec.scope
    for n in rec.narrowings:
        if world.position[n.event_id] < position:
            scope = scope.without(n.field, n.value)
    return scope


@dataclass(frozen=True)
class _Ended:
    session: Session
    started: int
    ended: int


def regressions(world: World) -> dict[str, Any]:
    """Promotions to L2/L3 after which a (project, task_class) that last ended
    in success next ended in failure."""
    ended = [
        _Ended(s, world.position[s.started.id], world.position[s.ended.id])
        for s in world.sessions.values()
        if s.started is not None and s.ended is not None
    ]
    by_pair: dict[tuple[str | None, str | None], list[_Ended]] = {}
    for e in ended:
        by_pair.setdefault((e.session.state.project, e.session.state.task_class), []).append(e)

    details = []
    for rec in sorted(world.records.values(), key=lambda r: world.position[r.proposed.id]):
        for move in _promotions(rec):
            at = world.position[move.event_id]
            scope = _scope_at(rec, world, at)
            for (project, task_class), sessions in sorted(by_pair.items(), key=lambda kv: str(kv[0])):
                if not (scope.project.covers(project) and scope.task_class.covers(task_class)):
                    continue
                before = [e for e in sessions if e.ended < at]
                after = [e for e in sessions if e.started > at]
                if not before or not after:
                    continue
                last = max(before, key=lambda e: e.ended).session
                first = min(after, key=lambda e: e.ended).session
                was, now = final_outcome(last), final_outcome(first)
                if was is None or now is None or not outcome_ok(was) or outcome_ok(now):
                    continue
                details.append(
                    {
                        "record_id": rec.id,
                        "promotion_event_id": move.event_id,
                        "to": move.to_rung.value,
                        "project": project,
                        "task_class": task_class,
                        "succeeded_session": last.id,
                        "failed_session": first.id,
                        "failed_event_id": now.id,
                    }
                )
    return {"count": len(details), "details": details}


def _stats(values: list[int]) -> dict[str, Any]:
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 2) if values else None,
        "median": statistics.median(values) if values else None,
    }


def rediscovery(world: World) -> dict[str, Any]:
    """Tokens spent before the first file write, by the arm of the session-start packet."""
    arm_of: dict[str, str] = {}
    for d in world.deliveries:
        if d.channel == "session_start":
            arm_of.setdefault(d.event.session_id, d.arm)
    tokens: dict[str, list[int]] = {}
    for sess in world.sessions.values():
        end = sess.ended
        if end is None or not end.payload.get("first_write_seen"):
            continue
        arm = arm_of.get(sess.id, "unknown")
        tokens.setdefault(arm, []).append(int(end.payload.get("tokens_before_first_write") or 0))
    return {arm: _stats(tokens.get(arm, [])) for arm in sorted(set(ARMS) | set(tokens))}


def _rounded(row: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 4) for k, v in row.items()}


def validation_cost(world: World) -> dict[str, Any]:
    """Miner tokens and seconds and gate seconds, per mined or gated session and in total."""
    per: dict[str, dict[str, float]] = {}
    for ev in world.events.values():
        component = ev.payload.get("component") if ev.kind is Kind.COST else None
        if component not in ("miner", "gate"):
            continue
        row = per.setdefault(ev.payload.get("session_id", ev.session_id), dict.fromkeys(COST_KEYS, 0))
        if component == "miner":
            row["miner_tokens"] += int(ev.payload.get("tokens") or 0)
            row["miner_seconds"] += float(ev.payload.get("seconds") or 0)
        else:
            row["gate_seconds"] += float(ev.payload.get("seconds") or 0)
    totals = {k: sum(row[k] for row in per.values()) for k in COST_KEYS}
    return {"sessions": {sid: _rounded(row) for sid, row in sorted(per.items())}, "totals": _rounded(totals)}


def records_by_rung(world: World) -> dict[str, int]:
    counts = dict.fromkeys(RUNG_KEYS, 0)
    for rec in world.records.values():
        counts[rec.rung.value if rec.rung else "pending"] += 1
    return counts


def compute(world: World, cfg: Config, now: datetime) -> dict[str, Any]:
    return {
        "generated_at": now.isoformat(),
        "ledger_revision": world.revision,
        "misapplication_rate": misapplication(world),
        "contradiction_rate": contradiction(world, cfg),
        "regression_count": regressions(world),
        "rediscovery_tokens": rediscovery(world),
        "validation_cost": validation_cost(world),
        "records_by_rung": records_by_rung(world),
    }


def write_daily(report: dict[str, Any], cfg: Config) -> Path:
    day = datetime.fromisoformat(report["generated_at"]).date().isoformat()
    path = cfg.ledger_dir / "metrics" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
