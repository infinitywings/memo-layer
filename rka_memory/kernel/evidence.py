"""Which links count as evidence (DESIGN_V2 5.1, rules 1 and 2), and where a record applies.

`counted` keeps a link only if its cited event can ground: not model-authored,
a grounding kind, a polarity that event can carry, already in the ledger when
the link was made, and, for a model-made link, from the session the link was
made in and, unless it carries the user's checked words, not from one of the
record's own trial sessions (5.3). A user verdict counts only for the record
it names. Independence,
cleanliness and the withdrawn set are the labelling function's business.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from rka_memory.config import Config
from rka_memory.domain import (
    FUZZY_FIELDS,
    GROUNDING,
    REFUTING_KINDS,
    Actor,
    Event,
    Integrity,
    Kind,
    Polarity,
    Scope,
    State,
    collapse,
    integrity,
    quoted,
    standing,
)
from rka_memory.world import RecordState, World

# Events that name the one record they bear on: a miner cannot cite them for another.
TARGETED_KINDS = (Kind.USER_CONFIRM, Kind.USER_REJECT, Kind.USER_AMEND, Kind.CHECK)
OUTCOMES = (Kind.TEST_RESULT, Kind.EXIT_CODE)


def modified_sessions(world: World, cfg: Config) -> frozenset[str]:
    """Sessions whose test and exit results are weak (DESIGN_V2 5.1 rule 2,
    8.1, as the PI refined it): the runner's modification check found
    existing test content or test configuration changed. A session with no
    modification check logged (a ledger from before the check existed) is
    judged by the strict rule: any agent write to a test path counts. A
    write to test configuration counts in any session, even if the file was
    put back before the end: results produced while it differed prove
    nothing. So does a session owing an isolation re-run that never logged
    its result."""
    cache = world.__dict__.setdefault("_modified", {})
    if world.revision not in cache:
        checked = {ev.session_id for ev in world.checks if ev.payload.get("check") == "modification"}
        out = {
            ev.session_id for ev in world.checks
            if ev.payload.get("check") == "modification" and not ev.payload.get("passed")
        }
        # An isolation re-run that was due and never logged its result.
        due = {ev.session_id for ev in world.checks if ev.payload.get("isolation") == "due"}
        out |= due - {ev.session_id for ev in world.checks if ev.payload.get("on") == "isolation"}
        for ev in world.events.values():
            if ev.kind is not Kind.FILE_WRITE:
                continue
            path = str(ev.payload.get("path") or "")
            config = _matches(path, cfg.check_config_globs) and not _matches(path, cfg.check_test_globs)
            if config or (ev.session_id not in checked and _matches(path, cfg.check_globs)):
                out.add(ev.session_id)
        cache.clear()
        cache[world.revision] = frozenset(out)
    return cache[world.revision]


def _matches(path: str, globs: tuple[str, ...]) -> bool:
    """A glob `dir/**` matches anything under a directory of that name; any
    other glob matches the path's tail or its file name."""
    pure = PurePosixPath(path)
    for g in globs:
        if g.endswith("/**") and "/" not in g[:-3]:
            if g[:-3] in pure.parts[:-1]:
                return True
        elif pure.match(g.removeprefix("**/")) or fnmatch(pure.name, g):
            return True
    return False


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
    # The words the link quotes from a user message or the user's check
    # output, checked against the logged event and, under attested
    # validation, not taint.
    quote: str | None = None

    @property
    def session_id(self) -> str:
        return self.event.session_id


def is_targeted(ev: Event, record_id: str) -> bool:
    """User verdicts name the record directly, so they apply wherever it applies."""
    return ev.kind in TARGETED_KINDS and ev.payload.get("record_id") == record_id


def inside(scope: Scope, g: Grounding) -> bool:
    return g.targeted or scope.covers(g.state)


def statement(made_by: Event | None, ev: Event, cfg: Config, world: World | None = None) -> tuple[str, bool] | None:
    """The words a link quotes from the user message, or (under attested
    validation) the output of the user's check, that it cites, checked here
    against the logged event: (quote, standing), or None when the link quotes
    nothing, misquotes, or quotes taint. Standing means the link also quotes
    words next to a user's rule that hold one of the configured standing
    phrases, such as "from now on"; a check's output is never standing."""
    if made_by is None:
        return None
    attested = cfg.gate.validation == "attested"
    user = ev.kind is Kind.USER_MSG and ev.actor is Actor.USER
    if user:
        text = str(ev.payload.get("text") or "")
    elif attested and integrity(ev) is Integrity.CHECK:
        text = str(ev.payload.get("output") or "")
    else:
        return None
    said = made_by.payload.get("statement")
    if not isinstance(said, dict) or not isinstance(said.get("quote"), str) or not quoted(text, said["quote"]):
        return None
    if not user and declared_message(said["quote"], cfg) is None:
        # The check runs the agent's code, which can print anything; only a
        # message the user declared for the check is the user's.
        return None
    if user and attested and world is not None and taint(world, said["quote"], ev, cfg.ledger_dir / "blobs"):
        return None
    marker = said.get("marker")
    is_rule = ev.kind is Kind.USER_MSG and isinstance(marker, str)
    return said["quote"], is_rule and standing(text, said["quote"], marker, cfg.gate.standing_phrases)


def declared_message(words: str, cfg: Config) -> str | None:
    """The message, among those the user declared for their outcome check
    (`checks.outcome.messages`), that `words` are, whole and whitespace
    aside; None when they are none of them (DESIGN_V3 3.1). Fail-closed: a
    check with no declared messages attests nothing."""
    said = collapse(words)
    return next((collapse(m) for m in cfg.outcome.messages if collapse(m) == said), None)


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


# What the agent's actions put in the log. The memory system's own events
# (proposals, links, deliveries) quote records by design and are not here.
AGENT_CONTENT = frozenset({
    Kind.TOOL_CALL, Kind.TOOL_RESULT, Kind.FILE_WRITE, Kind.EXIT_CODE, Kind.TEST_RESULT, Kind.ASSISTANT_MSG, Kind.FILE_SNAPSHOT,
})
# Payload fields naming text kept whole in the content store: a written
# file, a file a shell command changed, a clipped tool input, output or reply.
STORED_TEXT = ("post_hash", "hash", "full_hash")


def _stored(ev: Event, blobs: Path | None) -> str:
    """Every text the event keeps in the content store, by the hashes the log
    names. Content-addressed, so a label stays a function of the log and the
    store it points to."""
    if blobs is None:
        return ""
    out = []
    for field_ in STORED_TEXT:
        digest = ev.payload.get(field_)
        if isinstance(digest, str) and digest:
            try:
                out.append((blobs / digest).read_text(errors="replace"))
            except (OSError, ValueError):
                pass
    return " ".join(out)


def _project(world: World, ev: Event) -> str | None:
    session = world.sessions.get(ev.session_id)
    return session.state.project if session is not None else None


def taint(world: World, quote: str, before: Event, blobs: Path | None = None) -> bool:
    """The quoted words reached the user from the agent (DESIGN_V3 3.2): they
    occur, before `before` and in the same project, in what the agent's
    actions put in the log or the content store, including its replies and
    the files its commands changed. Fail-closed: an earlier agent event whose
    content could not be kept makes every later quote in its project taint,
    or every later quote at all when its project is unknown, which the hooks
    never leave it (a session's project falls back to its directory). The
    user's typed confirm still validates there."""
    cache = world.__dict__.setdefault("_taint", {})
    if cache.get("revision") != (world.revision, blobs):
        cache.clear()
        cache["revision"] = (world.revision, blobs)
        cache["texts"] = [
            (
                world.position[ev.id],
                _project(world, ev),
                bool(ev.payload.get("unstored")),
                collapse(" ".join([*_strings(ev.payload), _stored(ev, blobs)])).lower(),
            )
            for ev in world.events.values()
            if ev.kind in AGENT_CONTENT and integrity(ev) is Integrity.AGENT
        ]
    key = (collapse(quote).lower(), before.id)
    if key not in cache:
        at, here = world.position[before.id], _project(world, before)
        cache[key] = bool(key[0]) and any(
            pos < at
            and (project is None or here is None or project == here)
            and (unknown or key[0] in text)
            for pos, project, unknown, text in cache["texts"]
        )
    return cache[key]


def counted(rec: RecordState, world: World, cfg: Config, polarity: Polarity) -> list[Grounding]:
    """The links of one polarity that count, one per cited event, in log order."""
    out = []
    trial = world.trial_sessions(rec.id)
    for link in rec.links:
        if link.polarity is not polarity or link.via in world.defeated:
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
        if allowed is None or polarity not in allowed(ev):
            continue
        if ev.id != link.via and not world.before(ev.id, link.via):
            continue
        weight, targeted, affirms = cfg.weight(ev.kind), is_targeted(ev, rec.id), False
        if ev.kind in OUTCOMES and ev.session_id in modified_sessions(world, cfg):
            # The session changed its own tests: its results are not strong.
            weight = min(weight, cfg.weight(Kind.TOOL_RESULT))
        said = statement(made_by, ev, cfg, world)
        by_model = made_by is not None and made_by.actor is Actor.MODEL
        if by_model and said is None and ev.session_id in trial:
            # The trial judges its own sessions by their outcomes: the miner's
            # reading of one neither supports nor counts against the record.
            continue
        if said is not None and polarity is Polarity.SUPPORT and said[1]:
            # A standing rule in the user's words weighs like a confirm, and
            # confirms the record outright when the record is those words.
            weight = cfg.weight(Kind.USER_CONFIRM)
            affirms = collapse(said[0]) == collapse(rec.spec.content)
        if said is not None and polarity is Polarity.COUNTER:
            # The user's own words against the rule: not scoped away.
            targeted = True
        if by_model and polarity is Polarity.COUNTER and rec.id not in world.received_in(ev.session_id):
            # The session never received the record, so this is no failure of
            # following it: it refutes the claim, and only what the session
            # observed can do that (the second counter class of 5.1).
            if ev.kind not in REFUTING_KINDS:
                continue
            targeted = True
        if polarity is Polarity.SUPPORT and weight <= 0:
            continue
        out.append(Grounding(ev, weight, world.state_of(ev), targeted, affirms, said[0] if said else None))
    unique: dict[str, Grounding] = {}
    for g in out:
        held = unique.get(g.event.id)
        if held is None or (g.affirms, g.weight, g.targeted) > (held.affirms, held.weight, held.targeted):
            unique[g.event.id] = g
    return sorted(unique.values(), key=lambda g: world.position[g.event.id])


def affirmation(g: Grounding) -> bool:
    """A user confirm, a standing statement of exactly what the record says,
    or the user's typed amendment that wrote the record."""
    return g.event.kind in (Kind.USER_CONFIRM, Kind.USER_AMEND) or g.affirms


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
