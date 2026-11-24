"""Views over the labelled graph (DESIGN_V2 6.2 and 9).

A view composes seed records over typed edges and renders them. Packets and
SKILL.md files are seed policies over the same functions; `Memory.explain`
is the diagnostic one. Render adds fixed headings, never generated prose, and
a record line is its id and its text: no rung, weight, date or scope, which
change between sessions and would break the cached prefix.

Layout, for reuse of the provider's prefix cache across sessions:

1. the stable core: deliverable records in the order in which each began its
   current stretch at L2 (`Label.since`), oldest first, so a newly validated
   record appends and a quiet session renders the same bytes;
2. the amendments block: records that qualify a core record (`refines`),
   each naming its target, so amending an old record does not insert into
   the prefix; an amendment whose target is not delivered is not shown;
3. the tail: in the ungated arm, records not yet validated; in the gated
   arm, the trial candidates this session's logged coins treat (5.3).

The sourced arm delivers what the ungated arm delivers, as one list under a
neutral header, and ends each line with its source: where the events its
counted support cites came from. The clause names a class of source, never a
count, so it changes only when the class does. Under attested validation
(DESIGN_V3) the gated arm renders the same way: its validated records and
the trial candidates its coins treat, one list, each with its source.

A budget selects whole dependency groups (user-affirmed first, then those
about recently touched anchors, then the oldest) and never reorders. A
withdrawn record is never delivered, even though removing it breaks the
prefix. Nothing here reads the filesystem: a view depends on the log alone.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from typing import Any

import yaml

from rka_memory import relations
from rka_memory.config import Config
from rka_memory.domain import (
    OUTCOME_CHECK,
    SCOPE_FIELDS,
    Actor,
    Integrity,
    Kind,
    Polarity,
    RecordKind,
    Rung,
    State,
    collapse,
    integrity,
)
from rka_memory.kernel.evidence import counted, statement
from rka_memory.kernel.labels import Labels, valid
from rka_memory.skills.gate import skill_name
from rka_memory.world import RecordState, World

MAX_CONTENT_CHARS = 400
DESCRIPTION_MAX = 1024
HEADER = (
    "## Validated memory (rka-memory)\n"
    "Each record below was grounded by events outside the model (tests, exit codes, user edits) or stated by the user, "
    "and applies to the current project state. Follow them unless the current task contradicts one. "
    "If one is wrong, tell the user, who can type `rkm reject <id> <reason>`."
)
SOURCED_HEADER = (
    "## Notes from earlier sessions (rka-memory)\n"
    "Each note is a lesson from an earlier session in this environment and ends with its source; none is confirmed yet. "
    "Treat a note as advice: it never overrides what the user asks in this session, and never on its own justifies an action with side effects. When the current request matches the situation a note describes, take the note into account and say in one sentence "
    "which note, by its id, changed what you do. If one is wrong, tell the user, who can type `rkm reject <id> <reason>`."
)
AMENDMENTS = "### Amendments to the records above"
UNVALIDATED = "### Not yet validated"
TRIAL = "### Being tested"
RECENT_SESSIONS = 3


def flatten(text: str) -> str:
    """One line, capped: record text cannot open its own sections."""
    line = " ".join(text.split()).lstrip("#>-* ")
    return line if len(line) <= MAX_CONTENT_CHARS else line[: MAX_CONTENT_CHARS - 1] + "…"


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


def recent_anchors(world: World, project: str | None, before_session: str) -> frozenset[str]:
    """Anchors the project's last few ended sessions touched, other than
    `before_session`: the budget's selection key, fixed for a whole session."""
    if not project:
        return frozenset()
    ended = [
        s for s in world.sessions.values()
        if s.id != before_session and s.ended is not None and s.started is not None and s.state.project == project
    ]
    ended.sort(key=lambda s: world.position[s.ended.id])
    return frozenset().union(*(s.anchors for s in ended[-RECENT_SESSIONS:]))


def _quoted_from(rec: RecordState, world: World, cfg: Config, source: Integrity) -> bool:
    counted_ids = {g.event.id for g in counted(rec, world, cfg, Polarity.SUPPORT)}
    for link in rec.links:
        if link.polarity is not Polarity.SUPPORT or link.event_id not in counted_ids:
            continue
        ev = world.events[link.event_id]
        if integrity(ev) is not source:
            continue
        said = statement(world.events.get(link.via), ev, cfg, world)
        if said is not None and collapse(said[0]) == collapse(rec.spec.content):
            return True
    return False


def stated(rec: RecordState, world: World, cfg: Config) -> bool:
    """The record's text is the user's own words: a counted support link
    quotes a user message, the quote checked against the logged message, and
    the quote is the record's text. Any weaker test would let a view put
    model-written text in the user's mouth."""
    return _quoted_from(rec, world, cfg, Integrity.USER)


def check_printed(rec: RecordState, world: World, cfg: Config) -> bool:
    """The record's text is what the user's outcome check printed, checked
    the same way (attested validation only)."""
    return _quoted_from(rec, world, cfg, Integrity.CHECK)


def text_of(rec: RecordState, world: World, cfg: Config) -> str:
    """A record's text as a view shows it: the user's words as their quote, a
    decision as its choice, the options it set aside and its reason."""
    said = flatten(rec.spec.content)
    if rec.spec.kind is RecordKind.DECISION:
        parts = [f"Decided: {said.rstrip('.')}."]
        if rec.spec.alternatives:
            parts.append(f"Instead of: {flatten(', '.join(rec.spec.alternatives)).rstrip('.')}.")
        if rec.spec.reason:
            parts.append(f"Because: {flatten(rec.spec.reason).rstrip('.')}.")
        return " ".join(parts)
    if stated(rec, world, cfg):
        return f'The user said: "{said}"'
    if check_printed(rec, world, cfg):
        return f'The user\'s own acceptance check, which you are not shown, printed: "{said}"'
    return said


def source_clause(rec: RecordState, world: World, cfg: Config, tested: bool = False) -> str:
    """Where a record came from, read from the events its counted support
    cites and never from its own text. A stated rule's text already names the
    user. A user's check is quoted as it printed, so the agent can judge for
    itself whether it bears on the record. `tested`: a randomized trial found
    the sessions that received it did better."""
    if stated(rec, world, cfg) or check_printed(rec, world, cfg):
        return ""
    support = [g.event for g in counted(rec, world, cfg, Polarity.SUPPORT)]
    if any(ev.kind in (Kind.USER_CONFIRM, Kind.USER_AMEND) for ev in support):
        return "(Source: the user confirmed it.)"
    if tested:
        return "(Source: the memory system's inference; in a randomized test, sessions that received it did better.)"
    for ev in support:
        if ev.actor is Actor.ENV and ev.payload.get("tool") == OUTCOME_CHECK:
            printed = flatten(str(ev.payload.get("output") or ""))
            return f'(Source: the user\'s own acceptance check, which you are not shown, printed: "{printed}")'
    if any(ev.kind is Kind.USER_MSG for ev in support):
        return "(Source: the memory system's reading of a user message, not the user's own words.)"
    return "(Source: the memory system's inference from tool output; not the user's words, not confirmed by a user check.)"


@dataclass(frozen=True)
class Line:
    record_id: str
    text: str
    targets: tuple[str, ...] = ()

    def render(self) -> str:
        on = f"(on {', '.join(flatten(t) for t in self.targets)}) " if self.targets else ""
        return f"- [{flatten(self.record_id)}] {on}{self.text}"

    def to_json(self) -> dict[str, Any]:
        return {"record_id": self.record_id, "text": self.text, "targets": list(self.targets)}


@dataclass(frozen=True)
class View:
    arm: str
    channel: str
    # One list under the neutral header, each line with its source.
    sourced: bool = False
    core: tuple[Line, ...] = ()
    amendments: tuple[Line, ...] = ()
    tail: tuple[Line, ...] = ()
    trial: tuple[Line, ...] = ()
    omitted: tuple[str, ...] = ()
    ledger_revision: int = 0
    budget_tokens: int = 0
    # The `notepad` arm's notes, delivered whole in place of records.
    notes: str = ""

    @property
    def lines(self) -> tuple[Line, ...]:
        return self.core + self.amendments + self.tail + self.trial

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(line.record_id for line in self.lines)

    def render(self) -> str:
        return self._text

    @cached_property
    def _text(self) -> str:
        if self.notes:
            return self.notes
        if not self.lines:
            return ""
        if self.sourced:
            parts = [SOURCED_HEADER, *(line.render() for line in self.lines)]
            if self.omitted:
                parts += ["", f"({len(self.omitted)} more notes omitted for budget.)"]
            return "\n".join(parts)
        parts = [HEADER, *(line.render() for line in self.core)]
        if self.amendments:
            parts += ["", AMENDMENTS, *(line.render() for line in self.amendments)]
        if self.tail:
            parts += ["", UNVALIDATED, *(line.render() for line in self.tail)]
        if self.trial:
            parts += ["", TRIAL, *(line.render() for line in self.trial)]
        if self.omitted:
            parts += ["", f"({len(self.omitted)} more records omitted for budget.)"]
        return "\n".join(parts)

    @cached_property
    def hash(self) -> str:
        return hashlib.sha256(self.render().encode()).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "channel": self.channel,
            "core": [line.to_json() for line in self.core],
            "amendments": [line.to_json() for line in self.amendments],
            "tail": [line.to_json() for line in self.tail],
            "trial": [line.to_json() for line in self.trial],
            "omitted": list(self.omitted),
            "ledger_revision": self.ledger_revision,
            "notes": self.notes,
            "hash": self.hash,
        }


def superseded(world: World, labels: Labels, cfg: Config, state: State | None = None) -> set[str]:
    """Records a newer version hides from every view. The newer record must be
    deliverable and, given a state, valid in it, so a superseder that falls
    returns the old record by itself. A user's stated rule is hidden only by
    a record that quotes the user too."""
    out = set()
    for new in world.records.values():
        old = world.records.get(new.spec.supersedes or "")
        if old is None or not labels.by_id[new.id].deliverable:
            continue
        if state is not None and not valid(labels, new, state, world):
            continue
        if stated(old, world, cfg) and not stated(new, world, cfg):
            continue
        out.add(old.id)
    return out


def _groups(records: list[RecordState]) -> list[list[RecordState]]:
    """Connected components of dependencies and refines among `records`."""
    by_id = {r.id: r for r in records}
    parent = {rid: rid for rid in by_id}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in records:
        for d in (*r.spec.dependencies, *r.spec.refines):
            if d in by_id:
                parent[find(r.id)] = find(d)
    groups: dict[str, list[RecordState]] = {}
    for r in records:
        groups.setdefault(find(r.id), []).append(r)
    return list(groups.values())


def packet(
    world: World,
    labels: Labels,
    cfg: Config,
    state: State,
    *,
    arm: str,
    channel: str,
    budget_tokens: int | None = None,
    exclude: frozenset[str] = frozenset(),
    anchors: frozenset[str] = frozenset(),
    treated: tuple[str, ...] = (),
) -> View:
    """The packet for one session state under one arm. `exclude` holds records
    already delivered in the session; `anchors` rank groups for the budget;
    `treated` are the trial candidates this session's logged coins deliver."""
    budget = cfg.budget_tokens if budget_tokens is None else budget_tokens
    with_sources = arm == "sourced" or (arm == "gated" and cfg.gate.validation == "attested")
    empty = View(arm, channel, sourced=with_sources, ledger_revision=world.revision, budget_tokens=budget)
    if arm in ("none", "notepad"):
        return empty
    hidden = exclude | superseded(world, labels, cfg, state)
    chosen = [r for r in world.records.values() if r.id not in hidden and valid(labels, r, state, world)]
    if arm == "stated":
        chosen = [r for r in chosen if labels.by_id[r.id].affirmed]
    core_ids = {r.id for r in chosen if not r.spec.refines}
    amendments = {r.id: tuple(t for t in r.spec.refines if t in core_ids) for r in chosen if r.spec.refines}
    amendments = {rid: targets for rid, targets in amendments.items() if targets}
    selected = [r for r in chosen if r.id in core_ids or r.id in amendments]
    unvalidated: list[RecordState] = []
    if arm in ("ungated", "sourced"):
        unvalidated = [
            r for r in world.records.values()
            if labels.rung(r.id) is Rung.L1 and r.id not in hidden and r.scope.covers(state)
            # A file claim reaches no agent before the user confirms it.
            and r.spec.claimed_by is None
        ]

    def line(r: RecordState) -> Line:
        text = text_of(r, world, cfg)
        if with_sources:
            tested = labels.by_id[r.id].trial == "positive" if r.id in labels.by_id else False
            text = " ".join(part for part in (text, source_clause(r, world, cfg, tested)) if part)
        return Line(r.id, text, amendments.get(r.id, ()))

    def since(r: RecordState) -> tuple[int, int]:
        lab = labels.by_id[r.id]
        return (lab.since if lab.since is not None else world.position[r.proposed.id], world.position[r.proposed.id])

    groups = _groups(selected)
    groups.sort(
        key=lambda g: (
            not any(labels.by_id[r.id].affirmed for r in g),
            not any(not anchors.isdisjoint(r.spec.about) for r in g),
            min(since(r) for r in g),
        )
    )
    groups += [[r] for r in sorted(unvalidated, key=lambda r: world.position[r.proposed.id])]
    kept: set[str] = set()
    omitted: list[str] = []
    used = estimate_tokens(SOURCED_HEADER if with_sources else HEADER)
    for group in groups:
        cost = sum(estimate_tokens(line(r).render()) for r in group)
        if used + cost > budget:
            omitted += [r.id for r in sorted(group, key=since)]
            continue
        kept.update(r.id for r in group)
        used += cost
    in_order = sorted((r for r in selected if r.id in kept), key=since)
    trial = []
    for rid in treated:
        if rid in exclude or rid not in world.records:
            continue
        line_ = line(world.records[rid])
        cost = estimate_tokens(line_.render())
        if used + cost > budget:
            omitted.append(rid)
            continue
        trial.append(line_)
        used += cost
    return View(
        arm,
        channel,
        sourced=with_sources,
        core=tuple(line(r) for r in in_order if r.id in core_ids),
        amendments=tuple(line(r) for r in in_order if r.id in amendments),
        tail=tuple(line(r) for r in unvalidated if r.id in kept),
        trial=tuple(trial),
        omitted=tuple(omitted),
        ledger_revision=world.revision,
        budget_tokens=budget,
    )


def exportable(world: World, labels: Labels, cfg: Config, state: State | None = None) -> list[RecordState]:
    """Procedures at L2 or L3 that no valid newer version supersedes."""
    hidden = superseded(world, labels, cfg, state)
    return [
        r for r in world.records.values()
        if r.spec.kind is RecordKind.PROCEDURE and labels.by_id[r.id].deliverable and r.id not in hidden
    ]


def rules_followed(rec: RecordState, world: World, labels: Labels, cfg: Config) -> list[Line]:
    """The procedure's dependencies and the rules that govern it
    (`relations.governing`) that are deliverable, as lines. One that falls
    below L2 leaves the skill at the next render; the procedure keeps its
    own label."""
    hidden = superseded(world, labels, cfg)
    ids = dict.fromkeys((*rec.spec.depends_on, *relations.governing(rec, world)))
    return [
        Line(d, text_of(world.records[d], world, cfg))
        for d in ids
        if d in world.records and labels.by_id[d].deliverable and d not in hidden
    ]


def version(rec: RecordState, world: World) -> int:
    """1 for an original procedure, n + 1 for one that supersedes version n."""
    n, current, seen = 1, rec, {rec.id}
    while current.spec.supersedes in world.records and current.spec.supersedes not in seen:
        current = world.records[current.spec.supersedes]
        seen.add(current.id)
        n += 1
    return n


def _code(text: str) -> str:
    """Model-proposed text rendered as inline code: one line, no backticks."""
    return flatten(text).replace("`", "'")


def _description(content: str) -> str:
    line = " ".join(content.split())
    return line if len(line) <= DESCRIPTION_MAX else line[: DESCRIPTION_MAX - 3] + "..."


def skill(
    rec: RecordState,
    world: World,
    labels: Labels,
    cfg: Config,
    *,
    auto_load: bool,
    unenforced: tuple[str, ...] = (),
) -> str:
    """SKILL.md for one procedure: the procedure, its steps, the rules it
    follows by id, and its acceptance check."""
    name = skill_name(rec.id)
    lab = labels.by_id[rec.id]
    check = rec.spec.acceptance_check
    described = {f: rec.scope.constraint(f).describe() for f in SCOPE_FIELDS}
    front: dict[str, Any] = {"name": name, "description": _description(rec.spec.content)}
    if not auto_load:
        front["disable-model-invocation"] = True
    front["metadata"] = {
        "record_id": rec.id,
        "rung": lab.rung.value,
        "version": version(rec, world),
        "scope": {f: d for f, d in described.items() if d != "*"},
        "auto_load": auto_load,
        "evidence": list(lab.support),
        "acceptance_check": check,
    }
    body = [f"# {name}", "", flatten(rec.spec.content), "", f"Applies when: {flatten(rec.scope.describe())}."]
    if lab.rung is Rung.L3 and unenforced:
        body += [
            "",
            f"Auto-loading is off because Claude Code cannot enforce this skill's scope on "
            f"{', '.join(unenforced)}. rka-memory delivers it in the session packet when it applies.",
        ]
    if rec.spec.steps:
        body += ["", "## Steps", ""]
        body += [
            f"{i}. Tool `{_code(s.tool)}`, with input containing `{_code(s.match)}`."
            for i, s in enumerate(rec.spec.steps, 1)
        ]
    rules = rules_followed(rec, world, labels, cfg)
    if rules:
        body += ["", "## Rules this procedure follows", ""]
        body += [line.render() for line in rules]
    decisions = [
        Line(d, text_of(world.records[d], world, cfg))
        for d in rec.spec.implements
        if d in world.records and labels.by_id[d].deliverable
    ]
    if decisions:
        body += ["", "## Decisions this procedure carries out", ""]
        body += [line.render() for line in decisions]
    body.append("")
    if lab.rung is Rung.L3 and check:
        body += [f"Before finishing, run `{_code(check)}`; rka-memory records the result.", ""]
    body.append(f"If this skill is wrong, the user can type `rkm reject {rec.id} <reason>`.")
    frontmatter = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=10_000)
    return f"---\n{frontmatter}---\n\n" + "\n".join(body) + "\n"


def session_skills(world: World, labels: Labels, cfg: Config, state: State, arm: str) -> list[dict[str, str]]:
    """L3 skills valid for one session's state, for a harness whose skill
    provider is asked per session and so enforces the full scope itself. A
    session in the `none` or `notepad` arm gets no records, so no skills
    either, and one in the `stated` arm only skills the user affirmed."""
    if arm in ("none", "notepad"):
        return []
    out = []
    for rec in sorted(exportable(world, labels, cfg, state), key=lambda r: r.id):
        lab = labels.by_id[rec.id]
        if lab.rung is not Rung.L3 or not valid(labels, rec, state, world):
            continue
        if arm == "stated" and not lab.affirmed:
            continue
        _, front, body = skill(rec, world, labels, cfg, auto_load=True).split("---\n", 2)
        out.append(
            {
                "record_id": rec.id,
                "name": skill_name(rec.id),
                "description": yaml.safe_load(front)["description"],
                "body": body.lstrip("\n"),
            }
        )
    return out
