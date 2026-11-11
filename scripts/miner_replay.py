"""Replay the miner on saved live ledgers and report the relations it proposes.

For every session a ledger shows mined, the ledger is cut where that
session's miner output began, so the world is the one the miner saw. The
current miner code and prompts then mine the session again against a real
backend, in a throwaway ledger. The report lists, per session, the records
the prompt listed, each candidate with the relations it proposed
(depends_on, refines, supersedes, about), what the planner kept, and what
it dropped. Nothing in the source ledgers changes.

With `--chain`, a ledger is replayed as a whole instead: everything the old
miner and gate wrote is dropped, and the sessions are mined again in order on
top of each other, so records accumulate as they do in a live run while the
agent's sessions stay exactly as recorded. `--prompt <file>` swaps the miner
prompt, so two prompts can be compared on the same sessions.

Usage (in the workload image, with this repo mounted at /src):
  python scripts/miner_replay.py <ledger.jsonl>... --out <report.json> [--dry | --chain] [--prompt <file>]
`--dry` makes no model calls and only reports what each prompt listed.
Each ledger's run config (config.yaml two levels up) supplies the gate and
packet settings; the miner is always DeepSeek (`deepseek_miner()`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from rka_memory import config as config_mod
from rka_memory.domain import Actor, Event, Kind, Polarity
from rka_memory.ledger.jsonl import JsonlLedger
from rka_memory.miner import backends, miner
from rka_memory.service import Memory
from rka_memory.world import project
from string import Template

MINER_OUTPUT = frozenset({Kind.RECORD_PROPOSED, Kind.EVIDENCE_LINKED, Kind.JUDGMENT})
RELATIONS = ("depends_on", "refines", "supersedes")


def mined(events: list[Event]) -> list[tuple[str, int]]:
    """(session id, index where its miner output starts) for each mined session."""
    out = []
    for i, ev in enumerate(events):
        if ev.kind is not Kind.COST or ev.payload.get("component") != "miner":
            continue
        sid = ev.payload.get("session_id", ev.session_id)
        j = i
        while j > 0 and events[j - 1].session_id == sid and events[j - 1].actor is Actor.MODEL and events[j - 1].kind in MINER_OUTPUT:
            j -= 1
        out.append((sid, j))
    return out


def config_for(ledger: Path, tmp: Path) -> config_mod.Config:
    run_config = ledger.parent.parent / "config.yaml"
    overrides = {
        "ledger": {"dir": str(tmp)},
        "skills": {"dir": str(tmp / "skills"), "export_files": False},
        "core": {"sync_on_session_end": False},
        "miner": backends.deepseek_miner(),
    }
    return config_mod.load(path=run_config if run_config.is_file() else None, overrides=overrides)


def _relations(raw: dict[str, Any]) -> dict[str, Any]:
    out = {k: raw.get(k) for k in RELATIONS if raw.get(k)}
    if raw.get("about"):
        out["about"] = raw["about"]
    return out


def replay_session(events: list[Event], sid: str, cut: int, ledger: Path, dry: bool) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg = config_for(ledger, tmp)
        before = project(events[:cut])
        view = miner.view_of(before, sid, cfg)
        listed = [{"id": r.id, "kind": r.kind, "rung": r.rung, "content": r.content, "delivered": r in view.delivered} for r in view.listed]
        row: dict[str, Any] = {"listed": listed}
        if dry:
            return row
        memory = Memory.open(cfg)
        memory.append(*events[:cut])
        replies: list[str] = []
        backend = backends.backend_for(cfg)

        def capture(prompt: str) -> backends.Completion:
            completion = backend(prompt)
            if prompt.startswith("You are the candidate miner"):
                replies.append(completion.text)
            return completion

        result = miner.mine_session(memory, sid, backend=capture)
        world = memory.world()
        try:
            doc = json.loads(replies[-1][replies[-1].find("{") : replies[-1].rfind("}") + 1]) if replies else {}
        except (ValueError, IndexError):
            doc = {}
        row["status"] = result.status.value
        row["tokens"] = result.tokens
        row["candidates"] = [
            {"content": c.get("content"), "kind": c.get("kind"), **_relations(c)}
            for c in doc.get("candidates") or []
            if isinstance(c, dict)
        ]
        row["kept"] = [
            {"id": p["record_id"], "content": p["content"], **_relations(world.records[p["record_id"]].spec.to_json())}
            for p in result.proposed
        ]
        row["merged"] = result.merged
        row["links"] = result.links
        row["dropped"] = [d.to_json() for d in result.dropped]
        return row


ENGINE_KINDS = frozenset({Kind.RUNG_CHANGE, Kind.SCOPE_NARROWED})


def recorded_sessions(events: list[Event]) -> list[Event]:
    """The ledger without what a miner or the gate wrote. Records the workload
    runner planted stay, with the links that planted them: they stand for an
    earlier miner's mistakes, which the replayed miner must meet as the live
    one did."""
    planted = {e.payload["record_id"]: e.session_id for e in events if e.kind is Kind.RECORD_PROPOSED and e.payload.get("planted")}

    def planting(e: Event) -> bool:
        if e.kind is Kind.RECORD_PROPOSED:
            return e.payload["record_id"] in planted
        return e.kind is Kind.EVIDENCE_LINKED and planted.get(e.payload.get("record_id")) == e.session_id

    return [
        e
        for e in events
        if planting(e)
        or not (e.actor is Actor.MODEL and e.kind in MINER_OUTPUT)
        and e.kind not in ENGINE_KINDS
        and not (e.kind is Kind.COST and e.payload.get("component") == "miner")
    ]


def chain(ledger: Path) -> dict[str, Any]:
    events = recorded_sessions(JsonlLedger(ledger).read())
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg = config_for(ledger, tmp)
        memory = Memory.open(cfg)
        backend = backends.backend_for(cfg)
        tokens, statuses, counters = 0, [], []
        for ev in events:
            memory.append(ev)
            if ev.kind is Kind.SESSION_END:
                # Rungs for anything appended since the last mining, such as planted records.
                memory.gate(session_id=ev.session_id)
                result = miner.mine_session(memory, ev.session_id, backend=backend)
                tokens += result.tokens
                statuses.append(result.status.value)
                counters += [dict(link, session_id=ev.session_id) for link in result.links if link["polarity"] == "counter"]
                counters += [{"dropped": d.item, "reason": d.reason, "session_id": ev.session_id} for d in result.dropped if d.item.startswith("counter_link")]
        world = memory.world()
        for c in counters:
            if "event_id" in c:
                c["event_kind"] = world.events[c["event_id"]].kind.value
                c["planted"] = bool(world.records[c["record_id"]].proposed.payload.get("planted"))
        records = sorted(world.records.values(), key=lambda r: world.position[r.proposed.id])
        rungs: dict[str, int] = {}
        for r in records:
            rungs[r.rung.value if r.rung else "-"] = rungs.get(r.rung.value if r.rung else "-", 0) + 1
        return {
            "records": [
                {"id": r.id, "kind": r.spec.kind.value, "rung": r.rung.value if r.rung else None, "content": r.spec.content,
                 "planted": bool(r.proposed.payload.get("planted")), **_relations(r.spec.to_json()),
                 "links": {p.value: sum(1 for link in r.links if link.polarity is p) for p in Polarity}}
                for r in records
            ],
            "by_rung": rungs,
            "with_relations": sum(1 for r in records if r.spec.refines or r.spec.depends_on or r.spec.supersedes),
            "miner_tokens": tokens,
            "statuses": statuses,
            "counter_links": counters,
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ledgers", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--chain", action="store_true")
    ap.add_argument("--prompt", type=Path)
    args = ap.parse_args()
    os.environ.pop("RKA_MEMORY_DIR", None)
    os.environ.pop("RKA_MEMORY_CONFIG", None)
    if args.prompt:
        miner.PROMPT = Template(args.prompt.read_text(encoding="utf-8"))
    report: dict[str, Any] = {}
    if args.chain:
        for ledger in args.ledgers:
            name = f"{ledger.parent.parent.parent.name}/{ledger.parent.parent.name}"
            row = chain(ledger)
            report[name] = row
            print(f"{name}: records {len(row['records'])} {row['by_rung']}, with relations {row['with_relations']}, miner tokens {row['miner_tokens']}, statuses {sorted(set(row['statuses']))}", flush=True)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    for ledger in args.ledgers:
        events = JsonlLedger(ledger).read()
        order = [e.session_id for e in events if e.kind is Kind.SESSION_START]
        name = f"{ledger.parent.parent.parent.name}/{ledger.parent.parent.name}"
        for sid, cut in mined(events):
            label = f"{name} s{order.index(sid) + 1}" if sid in order else f"{name} {sid}"
            row = replay_session(events, sid, cut, ledger, args.dry)
            report[label] = row
            proposed = sum(1 for c in row.get("candidates", []) if any(k in c for k in RELATIONS))
            kept = sum(1 for c in row.get("kept", []) if any(k in c for k in RELATIONS))
            print(f"{label}: listed {len(row['listed'])}, candidates {len(row.get('candidates', []))}, with relations {proposed}, kept {kept}", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    rows = list(report.values())
    totals = {
        "sessions": len(rows),
        "sessions_listing_records": sum(1 for r in rows if r["listed"]),
        "candidates": sum(len(r.get("candidates", [])) for r in rows),
        **{f"proposed_{k}": sum(1 for r in rows for c in r.get("candidates", []) if c.get(k)) for k in RELATIONS},
        **{f"kept_{k}": sum(1 for r in rows for c in r.get("kept", []) if c.get(k)) for k in RELATIONS},
    }
    print(json.dumps(totals), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
