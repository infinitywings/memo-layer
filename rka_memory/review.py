"""The file claims awaiting the user's verdict (E2c's R4, docs/E2C.md).

`rka-memory review` lists up to `review.per_session` file claims that have no
verdict: claims never listed come first, oldest first, then those listed
fewest times. A claim the user leaves unanswered is listed again at most
twice; one the user confirmed, rejected or amended never again. A claim
withdrawn only because it depends on a rejected record has no verdict of
its own and stays listable.

What was listed is kept in `review.jsonl` beside the ledger. It is no
evidence for anything, and no part of any packet: the agent never sees it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rka_memory.config import Config
from rka_memory.domain import Actor, Kind, clock_now
from rka_memory.world import RecordState, World

LOG = "review.jsonl"
MAX_LISTINGS = 3
VERDICT_KINDS = (Kind.USER_CONFIRM, Kind.USER_REJECT)
HEADER = "Claims found in project files. The agent does not see them until you confirm one."


@dataclass(frozen=True)
class Line:
    record_id: str
    content: str
    path: str
    quote: str
    listed_before: int

    def render(self) -> str:
        return (
            f"- {self.record_id}: {self.content}\n"
            f"  {self.path} says: \"{self.quote}\"\n"
            f"  rkm confirm {self.record_id}\n"
            f"  rkm reject {self.record_id} <reason>"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "content": self.content,
            "path": self.path,
            "quote": self.quote,
            "listed_before": self.listed_before,
        }


def log_path(cfg: Config) -> Path:
    return cfg.ledger_dir / LOG


def listings(cfg: Config) -> dict[str, int]:
    """How often each record was listed."""
    path = log_path(cfg)
    counts: dict[str, int] = {}
    for raw in path.read_text(encoding="utf-8").splitlines() if path.exists() else ():
        for rid in json.loads(raw)["record_ids"]:
            counts[rid] = counts.get(rid, 0) + 1
    return counts


def answered(world: World) -> frozenset[str]:
    """Records the user gave a verdict on: a typed confirm or reject, or an
    amendment of the record."""
    out = set()
    for ev in world.events.values():
        if ev.actor is not Actor.USER:
            continue
        if ev.kind in VERDICT_KINDS:
            out.add(str(ev.payload.get("record_id")))
        elif ev.kind is Kind.USER_AMEND:
            out.add(str(ev.payload.get("target")))
    return frozenset(out)


def pending(world: World, cfg: Config) -> list[Line]:
    """Every listable file claim, in the order the list takes them."""
    seen, done = listings(cfg), answered(world)

    def eligible(rec: RecordState) -> bool:
        return rec.spec.claimed_by is not None and rec.id not in done and seen.get(rec.id, 0) < MAX_LISTINGS

    recs = sorted(
        (r for r in world.records.values() if eligible(r)),
        key=lambda r: (seen.get(r.id, 0), world.position[r.proposed.id]),
    )
    return [
        Line(r.id, r.spec.content, r.spec.claimed_by.path, r.spec.claimed_by.quote, seen.get(r.id, 0))
        for r in recs
        if r.spec.claimed_by is not None
    ]


def take(world: World, cfg: Config, now: datetime | None = None) -> list[Line]:
    """The next list, logged as shown. Lists nothing while review is off."""
    lines = pending(world, cfg)[: cfg.review_per_session]
    if lines:
        entry = {"at": (now or clock_now()).isoformat(), "record_ids": [line.record_id for line in lines]}
        with log_path(cfg).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return lines


def render(lines: list[Line]) -> str:
    if not lines:
        return "No claims await your verdict."
    return "\n".join([HEADER, *(line.render() for line in lines)])
