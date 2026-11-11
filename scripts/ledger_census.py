"""Count what the gate did across ledgers, and what it was never asked to do.

For the ledgers given, pooled: how many mined records reached L2 and by which
rule, which events those promotions cited, how much of their support came
from sessions that had received the record, how often a record came back
down, who made the evidence links, and how much the parts of the design that
need variety saw of it (scope narrowing, task classes, hints, user verdicts,
the memory tools). Records the runner planted are left out.

Usage: python scripts/ledger_census.py <ledger.jsonl>...
Prints counts only, never the text of a message or a record.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from rka_memory import config as config_mod
from rka_memory.domain import Event, Kind, Polarity, Rung
from rka_memory.world import RecordState, World, project

THRESHOLD = "grounded in enough distinct sessions"
TEST_COMMANDS = config_mod.load(path=config_mod.DEFAULT_PATH).test_commands
SECTIONS = (
    "totals",
    "records by origin",
    "records by final rung",
    "reached L2, by origin",
    "first promotion to L2, by rule",
    "threshold promotions, by kinds cited",
    "strong events cited by threshold promotions",
    "support links on threshold-promoted records",
    "records that reached L2",
    "rung changes",
    "evidence links by maker",
    "user signals",
    "records that declare",
    "scope narrowings by field",
    "session task classes",
    "deliveries by channel",
    "agent calls to memory tools",
)


def user_stated(rec: RecordState, world: World) -> bool:
    """The rule extractor recorded it from the user's quoted words."""
    makers = (world.events.get(link.via) for link in rec.links)
    return any(m is not None and isinstance(m.payload.get("statement"), dict) for m in makers)


def census(events: list[Event], out: dict[str, Counter]) -> None:
    world = project(events)
    totals = out["totals"]
    totals["ledgers"] += 1
    totals["events"] += len(events)
    totals["sessions"] += sum(1 for s in world.sessions.values() if s.started is not None)

    for e in events:
        if e.kind is Kind.EVIDENCE_LINKED:
            out["evidence links by maker"][e.actor.value] += 1
        elif e.kind in (Kind.USER_CONFIRM, Kind.USER_REJECT, Kind.USER_EDIT, Kind.USER_UNDO, Kind.USER_REPEAT):
            out["user signals"][e.kind.value] += 1
        elif e.kind is Kind.COST and e.payload.get("component") == "miner":
            totals["miner tokens"] += int(e.payload.get("tokens") or 0)
        elif e.kind is Kind.SESSION_END:
            totals["agent tokens"] += int(e.payload.get("tokens_total") or 0)
        elif e.kind is Kind.SCOPE_NARROWED:
            out["scope narrowings by field"][str(e.payload.get("field"))] += 1
        elif e.kind is Kind.RUNG_CHANGE:
            move = f"{e.payload.get('from') or 'new'} -> {e.payload.get('to')}: {e.payload.get('reason')}"
            out["rung changes"][move] += 1
        elif e.kind is Kind.JUDGMENT and e.payload.get("target") == "session" and e.payload.get("field") == "task_class":
            out["session task classes"][str(e.payload.get("value"))] += 1
        elif e.kind is Kind.TOOL_CALL and "memory_" in str(e.payload.get("tool") or ""):
            out["agent calls to memory tools"][str(e.payload["tool"])] += 1

    for d in world.deliveries:
        out["deliveries by channel"][f"{d.channel}, {'with records' if d.record_ids else 'empty'}"] += 1

    for rec in world.records.values():
        if rec.proposed.payload.get("planted"):
            totals["planted records (left out)"] += 1
            continue
        origin = "user-stated rule" if user_stated(rec, world) else "miner lesson"
        totals["mined records"] += 1
        out["records by origin"][origin] += 1
        out["records by final rung"][rec.rung.value if rec.rung else "none"] += 1
        for name in ("depends_on", "refines", "supersedes", "about", "falsifier"):
            out["records that declare"][name] += bool(getattr(rec.spec, name, None))

        promotion = next((m for m in rec.history if m.to_rung is Rung.L2 and m.from_rung in (Rung.L0D, Rung.L1)), None)
        if promotion is None:
            continue
        out["reached L2, by origin"][origin] += 1
        out["first promotion to L2, by rule"][promotion.reason] += 1
        came_down = any(m.from_rung in (Rung.L2, Rung.L3) and m.to_rung not in (Rung.L2, Rung.L3) for m in rec.history)
        out["records that reached L2"]["later demoted or retired" if came_down else "never came down"] += 1
        if promotion.reason != THRESHOLD:
            continue
        cited = [world.events[c] for c in promotion.cause_ids if c in world.events]
        out["threshold promotions, by kinds cited"]["+".join(sorted({e.kind.value for e in cited}))] += 1
        for e in cited:
            if e.kind is Kind.TEST_RESULT:
                out["strong events cited by threshold promotions"]["a test result"] += 1
            elif e.kind is Kind.EXIT_CODE:
                runs_tests = any(t in str(e.payload.get("command") or "") for t in TEST_COMMANDS)
                label = "the exit code of a command that runs the tests" if runs_tests else "the exit code of another command"
                out["strong events cited by threshold promotions"][label] += 1
        for link in rec.links:
            ev = world.events.get(link.event_id)
            if link.polarity is Polarity.SUPPORT and ev is not None:
                received = rec.id in world.delivered_in(ev.session_id)
                where = "from a session that had received the record" if received else "from a session that had not"
                out["support links on threshold-promoted records"][where] += 1


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    out = {name: Counter() for name in SECTIONS}
    for path in paths:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        census([Event.from_json(json.loads(line)) for line in lines if line.strip()], out)
    for name in SECTIONS:
        print(name)
        for key, n in sorted(out[name].items(), key=lambda kv: (-kv[1], kv[0])) or [("(none)", 0)]:
            print(f"  {n:>10,}  {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
