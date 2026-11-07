"""Budgeted context packets: valid records only, dependency groups kept whole."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from rka_memory.config import Config
from rka_memory.domain import DELIVERABLE, Rung, State, parse_ts
from rka_memory.validity.engine import assess, valid
from rka_memory.world import RecordState, World


MAX_CONTENT_CHARS = 400


def flatten(text: str) -> str:
    """One line, capped: record text cannot open its own sections in the packet."""
    line = " ".join(text.split()).lstrip("#>-* ")
    return line if len(line) <= MAX_CONTENT_CHARS else line[: MAX_CONTENT_CHARS - 1] + "…"


@dataclass(frozen=True)
class PacketItem:
    record_id: str
    rung: str
    kind: str
    content: str
    scope: str
    grounding: float
    sessions: int
    unvalidated: bool = False
    stated: str | None = None

    def line(self) -> str:
        tag = "unvalidated hint" if self.unvalidated else f"{self.rung} {self.kind}"
        # A rule in the user's own words is shown as their quote, not as an
        # order from the memory, so it reads as what the user asked for.
        said = f'The user said on {self.stated}: "{flatten(self.content)}"' if self.stated else flatten(self.content)
        return (
            f"- [{flatten(self.record_id)} | {tag}] {said} "
            f"(scope: {flatten(self.scope)}; grounding {self.grounding:.2f} across {self.sessions} sessions)"
        )

    def to_json(self) -> dict:
        return self.__dict__.copy()


@dataclass(frozen=True)
class Packet:
    arm: str
    channel: str
    items: tuple[PacketItem, ...] = ()
    omitted_count: int = 0
    ledger_revision: int = 0
    budget_tokens: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def record_ids(self) -> list[str]:
        return [i.record_id for i in self.items]

    def render(self) -> str:
        if not self.items:
            return ""
        body = "\n".join(i.line() for i in self.items)
        tail = f"\n({self.omitted_count} more records omitted for budget.)" if self.omitted_count else ""
        return f"{header(self.channel)}\n{body}{tail}"

    def to_json(self) -> dict:
        return {
            "arm": self.arm,
            "channel": self.channel,
            "records": [i.to_json() for i in self.items],
            "omitted_count": self.omitted_count,
            "ledger_revision": self.ledger_revision,
        }


def header(channel: str) -> str:
    if channel == "hints":
        return (
            "## Unvalidated memory hints (rka-memory)\n"
            "These are L1 claims: grounded but not validated. Check before acting on one."
        )
    return (
        "## Validated memory (rka-memory)\n"
        "Each record below was grounded by events outside the model (tests, exit codes, user edits) "
        "and applies to the current project state. Follow them unless the current task contradicts one. "
        "If one is wrong, tell the user, who can type `rkm reject <id> <reason>`."
    )


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def arm_for(day: date, cfg: Config) -> str:
    return cfg.arms[day.toordinal() % len(cfg.arms)]


def session_arm(world: World, session_id: str, cfg: Config, today: date) -> str:
    """The arm a session runs under: the one its session-start packet used, so
    every later channel (prompt packets, skills) follows it; else today's arm."""
    for d in world.deliveries:
        if d.event.session_id == session_id and d.channel == "session_start":
            return d.arm
    return arm_for(today, cfg)


def _item(rec: RecordState, world: World, cfg: Config, unvalidated: bool = False) -> PacketItem:
    support = assess(rec, world, cfg).support
    statements = [g for g in support if g.affirms]
    return PacketItem(
        record_id=rec.id,
        rung=rec.rung.value if rec.rung else "-",
        kind=rec.spec.kind.value,
        content=rec.spec.content,
        scope=rec.scope.describe(),
        grounding=round(sum(g.weight for g in support), 2),
        sessions=len({g.session_id for g in support}),
        unvalidated=unvalidated,
        stated=parse_ts(statements[-1].event.ts).date().isoformat() if statements else None,
    )


def _groups(records: list[RecordState]) -> list[list[RecordState]]:
    """Connected components of depends_on among the selected records."""
    by_id = {r.id: r for r in records}
    parent = {rid: rid for rid in by_id}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in records:
        for d in r.spec.depends_on:
            if d in by_id:
                parent[find(r.id)] = find(d)
    groups: dict[str, list[RecordState]] = {}
    for r in records:
        groups.setdefault(find(r.id), []).append(r)
    return list(groups.values())


def _recency(rec: RecordState, world: World) -> int:
    marks = [world.position[rec.proposed.id]] + [world.position[m.event_id] for m in rec.history]
    return max(marks)


def _select(world: World, cfg: Config, state: State, arm: str, exclude: frozenset[str]) -> list[RecordState]:
    if arm == "none":
        return []
    if arm == "ungated":
        pool = [r for r in world.records.values() if r.rung in (Rung.L1, Rung.L2, Rung.L3)]
        return [r for r in pool if r.id not in exclude and r.scope.covers(state)]
    return [r for r in world.deliverable() if r.id not in exclude and valid(r, state, world, cfg)]


def build(
    world: World,
    cfg: Config,
    state: State,
    *,
    arm: str,
    channel: str,
    budget_tokens: int | None = None,
    exclude: frozenset[str] = frozenset(),
) -> Packet:
    budget = cfg.budget_tokens if budget_tokens is None else budget_tokens
    chosen = _select(world, cfg, state, arm, exclude)
    groups = _groups(chosen)
    groups.sort(
        key=lambda g: (max(r.rung.order for r in g), max(_recency(r, world) for r in g)),
        reverse=True,
    )
    items: list[PacketItem] = []
    omitted = 0
    used = estimate_tokens(header(channel))
    for group in groups:
        ordered = sorted(group, key=lambda r: (len(r.spec.depends_on), world.position[r.proposed.id]))
        lines = [_item(r, world, cfg, unvalidated=r.rung not in DELIVERABLE) for r in ordered]
        cost = sum(estimate_tokens(i.line()) for i in lines)
        if used + cost > budget:
            omitted += len(group)
            continue
        items.extend(lines)
        used += cost
    return Packet(arm, channel, tuple(items), omitted, world.revision, budget)


def hints(world: World, cfg: Config, state: State, *, exclude: frozenset[str] = frozenset()) -> Packet:
    pool = [r for r in world.records.values() if r.rung is Rung.L1 and r.id not in exclude]
    pool = [r for r in pool if r.scope.covers(state) and not assess(r, world, cfg).counters]
    pool.sort(key=lambda r: _recency(r, world), reverse=True)
    items, used, omitted = [], estimate_tokens(header("hints")), 0
    for r in pool:
        item = _item(r, world, cfg, unvalidated=True)
        cost = estimate_tokens(item.line())
        if used + cost > cfg.hints_budget_tokens:
            omitted += 1
            continue
        items.append(item)
        used += cost
    return Packet("gated", "hints", tuple(items), omitted, world.revision, cfg.hints_budget_tokens)
