"""Expectations a stream states after a session, and how each one is checked.

Every check reads what the runner observed after the session: the ledger
projection, the session's own id, the requests the scripted model received
during that session, the raw ledger lines, and `rka-memory metrics` on demand.
`CHECKS` is the one table of check kinds; the stream parser validates against
it, so a typo in a scenario fails when the file loads, not after a long run.
"""

from __future__ import annotations

import json
import operator
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rka_memory import anchors
from rka_memory.config import ARMS
from rka_memory.domain import Event, Kind, Rung
from rka_memory.skills.gate import skill_name
from rka_memory.workload import files
from rka_memory.world import RecordState, World

PACKET_CHANNELS = ("session_start", "prompt")
COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
}


def normalize(text: str) -> str:
    return " ".join(str(text).split())


@dataclass(frozen=True)
class Expect:
    check: str
    value: Any
    record: str | None = None

    def describe(self) -> str:
        subject = f'record "{self.record}" ' if self.record is not None else ""
        return f"{subject}{self.check} {json.dumps(self.value, ensure_ascii=False)}"


@dataclass(frozen=True)
class Observed:
    world: World
    session_id: str | None
    requests: list[dict[str, Any]]
    ledger_lines: list[str]
    metrics: Callable[[], dict[str, Any]]
    project: Path | None = None
    # What rejecting a record would do, from `rka-memory impact`.
    impact: Callable[[str], dict[str, Any]] | None = None
    # The agent user's uid and gid, when the runner confines the agent.
    run_as: tuple[int, int] | None = None

    def deliveries(self, channels: tuple[str, ...] = PACKET_CHANNELS):
        return [d for d in self.world.deliveries if d.event.session_id == self.session_id and d.channel in channels]


def find_record(world: World, content: str) -> RecordState:
    want = normalize(content)
    found = [r for r in world.records.values() if normalize(r.spec.content) == want]
    if len(found) != 1:
        raise LookupError(f"{len(found)} records have this content")
    return found[0]


Outcome = tuple[bool, Any]


def _rung(obs: Observed, rec: RecordState, want: str) -> Outcome:
    got = rec.rung.value if rec.rung else None
    return got == want, got


def _delivered(obs: Observed, rec: RecordState, want: bool) -> Outcome:
    got = any(rec.id in d.record_ids for d in obs.deliveries())
    return got == want, got


def _agent_requests(obs: Observed) -> list[dict[str, Any]]:
    return [r["body"] for r in obs.requests if r.get("kind") == "agent"]


PACKET_HEADERS = ("## Validated memory (rka-memory)",)


def _in_prompt(obs: Observed, rec: RecordState, want: bool) -> Outcome:
    """The record id is in a packet the model received: in a system or
    developer message (the session-start packet), or in a message that
    carries a packet (the first prompt's)."""
    packets = []
    for body in _agent_requests(obs):
        for m in body.get("messages") or ():
            text = json.dumps(m.get("content"), ensure_ascii=False)
            if m.get("role") in ("system", "developer") or any(h in text for h in PACKET_HEADERS):
                packets.append(text)
    got = any(rec.id in text for text in packets)
    return got == want, got


def _first_in_packet(obs: Observed, rec: RecordState, want: bool) -> Outcome:
    """The record opened a packet delivered in this session."""
    got = any(d.record_ids[:1] == (rec.id,) for d in obs.deliveries())
    return got == want, [list(d.record_ids) for d in obs.deliveries() if d.record_ids]


IMPACT_KEYS = {"withdrawn", "withheld", "kept", "files"}


def _is_impact(value: Any) -> None:
    if not isinstance(value, dict) or "record" not in value or not set(value) - {"record"} <= IMPACT_KEYS:
        raise ValueError(f"expected {{record, and any of {sorted(IMPACT_KEYS)}}}, records named by content")
    for k in set(value) - {"record"}:
        if not isinstance(value[k], list) or not all(isinstance(x, str) for x in value[k]):
            raise ValueError(f"{k}: expected a list of strings")


def _impact(obs: Observed, want: dict[str, Any]) -> Outcome:
    """`rka-memory impact` for the record, compared by content: what rejecting
    it would withdraw, withhold and keep, and the files written while it was
    in force (relative to the project)."""
    if obs.impact is None:
        return False, "no impact preview in this run"
    rec = find_record(obs.world, want["record"])
    report = obs.impact(rec.id)
    content = {rid: normalize(r.spec.content) for rid, r in obs.world.records.items()}
    project = str(obs.project or "")
    got = {
        "withdrawn": sorted(content[r] for r in report["withdrawn"]),
        "withheld": sorted(content[w["record_id"]] for w in report["withheld"]),
        "kept": sorted(content[k["record_id"]] for k in report["kept"]),
        "files": sorted(f.removeprefix(project).lstrip("/") for f in report["files_written_while_in_force"]),
    }
    diff = {k: got[k] for k in IMPACT_KEYS & set(want) if got[k] != sorted(normalize(x) if k != "files" else x for x in want[k])}
    return not diff, diff or got


# The trial parameters the PI registered (DESIGN_V2 5.3), restated here so
# the check does not read them from the code it checks.
TRIAL_M, TRIAL_DELTA = 3, 1 / 3


def _trial_status(world: World, arms: dict[str, str], before: int | None) -> str:
    """A procedure's trial recomputed from the log: sessions with a coin, in
    the order they ended (before position `before` if given), each counted
    as passing if its last test or exit result passed. A session whose
    modification check found its tests changed counts against promotion: a
    failure if treated, a pass if withheld."""
    n = {"treated": [0, 0], "withheld": [0, 0]}
    changed = {e.session_id for e in world.checks if e.payload.get("check") == "modification" and not e.payload.get("passed")}
    ended = sorted(
        (world.position[s.ended.id], s)
        for s in (world.sessions.get(x) for x in arms)
        if s is not None and s.ended is not None and (before is None or world.position[s.ended.id] < before)
    )
    for _, s in ended:
        outs = [e for e in s.events if e.kind in (Kind.TEST_RESULT, Kind.EXIT_CODE)]
        last = outs[-1] if outs else None
        ok = last is not None and (bool(last.payload.get("passed")) if last.kind is Kind.TEST_RESULT else last.payload.get("code") == 0)
        arm = n.get(arms[s.id])
        if arm is None:
            continue
        arm[0] += 1
        arm[1] += (arms[s.id] == "withheld") if s.id in changed else ok
        if n["treated"][0] >= TRIAL_M and n["withheld"][0] >= TRIAL_M:
            gap = n["treated"][1] / n["treated"][0] - n["withheld"][1] / n["withheld"][0]
            return "positive" if gap >= TRIAL_DELTA - 1e-9 else "closed"
    return "open"


def _trial(obs: Observed, content: str) -> Outcome:
    """A procedure in a randomized trial, checked against the log: one coin
    in this session if the trial was open when it began, the procedure
    delivered exactly when that coin treated it, and its rung what the
    logged coins and outcomes say (L2 or above only after a positive
    contrast). Only scenarios whose sessions run in the gated arm use it."""
    world, sid = obs.world, obs.session_id
    rec = find_record(world, content)
    mine = [e for e in world.trials if e.payload.get("record_id") == rec.id]
    arms: dict[str, str] = {}
    for e in mine:
        arms.setdefault(e.session_id, str(e.payload.get("arm")))
    sess = world.sessions.get(sid or "")
    start = world.position[sess.started.id] if sess is not None and sess.started is not None else None
    here = [e for e in mine if e.session_id == sid]
    problems = []
    if _trial_status(world, arms, start) == "open" and len(here) != 1:
        problems.append(f"{len(here)} coins in a session that began with the trial open")
    treated = bool(here) and here[0].payload.get("arm") == "treated"
    delivered = any(rec.id in d.record_ids for d in obs.deliveries())
    if delivered != treated:
        problems.append(f"delivered {delivered}, coin {here[0].payload.get('arm') if here else 'none'}")
    status = _trial_status(world, arms, None)
    rung = rec.rung.value if rec.rung else None
    if (status == "positive") != (rung in ("L2", "L3")):
        problems.append(f"trial {status} but rung {rung}")
    counts = {a: sum(1 for x in arms.values() if x == a) for a in ("treated", "withheld")}
    return not problems, problems or f"trial {status}; coins {counts}; rung {rung}"


def _skill_offered(obs: Observed, rec: RecordState, want: bool) -> Outcome:
    name = skill_name(rec.id)
    got = any(name in json.dumps(body) for body in _agent_requests(obs))
    return got == want, got


def _arm(obs: Observed, want: str) -> Outcome:
    got = sorted({d.arm for d in obs.deliveries()})
    return got == [want], got


def _packet_empty(obs: Observed, want: bool) -> Outcome:
    delivered = sorted({rid for d in obs.deliveries() for rid in d.record_ids})
    return (not delivered) == want, delivered


def _ledger_parses(obs: Observed, want: bool) -> Outcome:
    bad = []
    for i, line in enumerate(obs.ledger_lines, 1):
        if not line.strip():
            continue
        try:
            Event.from_json(json.loads(line))
        except (ValueError, KeyError, TypeError):
            bad.append(i)
    return (not bad) == want, f"unparsable lines {bad}" if bad else "every line parses"


def _raw_line_absent(obs: Observed, line: str) -> Outcome:
    hits = [i for i, raw in enumerate(obs.ledger_lines, 1) if raw.strip() == line.strip()]
    return not hits, f"found at lines {hits}" if hits else "absent"


def _is_event_count(value: Any) -> None:
    kinds = {k.value for k in Kind}
    if not isinstance(value, dict) or set(value) != {"kind", "count"} or value["kind"] not in kinds or not isinstance(value["count"], int):
        raise ValueError("expected {kind, count} with a ledger event kind")


def _event_count(obs: Observed, value: dict[str, Any]) -> Outcome:
    n = sum(1 for raw in obs.ledger_lines if raw.strip() and json.loads(raw).get("kind") == value["kind"])
    return n == value["count"], f"{n} {value['kind']} events"


def _is_path_text(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"path", "text"} or not all(isinstance(v, str) for v in value.values()):
        raise ValueError("expected {path, text}")


def _file_contains(obs: Observed, want: dict[str, str]) -> Outcome:
    path = Path(want["path"])
    if not path.is_file():
        return False, "no such file"
    found = want["text"] in path.read_text(encoding="utf-8", errors="replace")
    return found, "found" if found else "not found"


def _strings(value: Any):
    """Every string in a request, keys included, as the model reads it:
    decoded, so a quote in the text is a quote here too, not JSON's escape."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _requests_holding(obs: Observed, text: str) -> list[int]:
    return [i for i, body in enumerate(_agent_requests(obs)) if any(text in s for s in _strings(body))]


def _model_never_saw(obs: Observed, text: str) -> Outcome:
    """No request the model received in this session carried the text: not in
    a prompt, not in a tool result. The canaries the runner plants in protected
    directories make this a read-leak check."""
    hits = _requests_holding(obs, text)
    return not hits, f"seen in requests {hits}" if hits else "never seen"


def _model_saw(obs: Observed, text: str) -> Outcome:
    """Some request the model received in this session carried the text."""
    hits = _requests_holding(obs, text)
    return bool(hits), f"seen in requests {hits}" if hits else "never seen"


def _record_contains(obs: Observed, text: str) -> Outcome:
    """Some record's content holds `text`, case aside: the miner kept a rule
    whose wording is not known in advance. Reports each match with its rung."""
    want = normalize(text).lower()
    found = sorted(
        f"{r.id} {r.rung.value if r.rung else '-'}"
        for r in obs.world.records.values()
        if want in normalize(r.spec.content).lower()
    )
    return bool(found), found or "no record"


COMMAND_TIMEOUT = 300


def _command_passes(obs: Observed, command: str) -> Outcome:
    """The shell command exits 0 when run in the project after the session,
    such as the test suite. Its last output lines are the detail."""
    if obs.project is None:
        return False, "no project"
    try:
        # The command runs code the agent wrote, so it runs as the agent.
        proc = files.sandboxed(["bash", "-c", command], obs.project, obs.run_as, timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {COMMAND_TIMEOUT}s"
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    return proc.returncode == 0, f"exit {proc.returncode}: {' | '.join(tail)}"


def _ran(obs: Observed, text: str) -> Outcome:
    """A shell command the agent ran in this session holds `text`. It reads
    the session's tool calls in the ledger, not the model's requests, so a
    live stream can observe it."""
    sess = obs.world.sessions.get(obs.session_id or "")
    commands = [
        str(e.payload.get("input") or "")
        for e in (sess.events if sess else ())
        if e.kind is Kind.TOOL_CALL and e.payload.get("tool") in anchors.SHELL_TOOLS
    ]
    hits = [c for c in commands if text in c]
    return bool(hits), [" ".join(c.split())[:120] for c in hits[:3]] or f"none of {len(commands)} commands"


def _wrote(obs: Observed, name: str) -> Outcome:
    """The agent wrote a file at this project-relative path in this session,
    by a file tool or a shell command (the ledger's file writes)."""
    sess = obs.world.sessions.get(obs.session_id or "")
    paths = {str(e.payload.get("path") or "") for e in (sess.events if sess else ()) if e.kind is Kind.FILE_WRITE}
    hits = sorted(x for x in paths if x == name or x.endswith("/" + name))
    return bool(hits), hits or f"none of {len(paths)} written files"


def _file_absent(obs: Observed, path: str) -> Outcome:
    return not Path(path).exists(), "exists" if Path(path).exists() else "absent"


def _dig(doc: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        doc = doc.get(key) if isinstance(doc, dict) else None
    return doc


def _metrics(obs: Observed, want: dict[str, Any]) -> Outcome:
    report = obs.metrics()
    got, ok = {}, True
    for path, condition in want.items():
        value = _dig(report, path)
        got[path] = value
        tests = condition if isinstance(condition, dict) else {"==": condition}
        for op, expected in tests.items():
            ok = ok and value is not None and COMPARE[op](value, expected)
    return ok, got


def _is_bool(value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError("expected true or false")


def _is_str(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("expected a non-empty string")


def _is_rung(value: Any) -> None:
    if value not in {r.value for r in Rung}:
        raise ValueError(f"expected one of {[r.value for r in Rung]}")


def _is_arm(value: Any) -> None:
    if value not in ARMS:
        raise ValueError(f"expected one of {sorted(ARMS)}")


def _is_metric_conditions(value: Any) -> None:
    if not isinstance(value, dict) or not value:
        raise ValueError("expected a mapping of dotted metric paths to values or {op: value}")
    for condition in value.values():
        if isinstance(condition, dict) and (not condition or set(condition) - set(COMPARE)):
            raise ValueError(f"comparison operators are {sorted(COMPARE)}")


@dataclass(frozen=True)
class Check:
    validate: Callable[[Any], None]
    run: Callable[..., Outcome]
    needs_record: bool


CHECKS: dict[str, Check] = {
    "rung": Check(_is_rung, _rung, True),
    "delivered": Check(_is_bool, _delivered, True),
    "in_prompt": Check(_is_bool, _in_prompt, True),
    "first_in_packet": Check(_is_bool, _first_in_packet, True),
    "skill_offered": Check(_is_bool, _skill_offered, True),
    "arm": Check(_is_arm, _arm, False),
    "packet_empty": Check(_is_bool, _packet_empty, False),
    "ledger_parses": Check(_is_bool, _ledger_parses, False),
    "raw_line_absent": Check(_is_str, _raw_line_absent, False),
    "file_absent": Check(_is_str, _file_absent, False),
    "file_contains": Check(_is_path_text, _file_contains, False),
    "model_never_saw": Check(_is_str, _model_never_saw, False),
    "record_contains": Check(_is_str, _record_contains, False),
    "model_saw": Check(_is_str, _model_saw, False),
    "impact": Check(_is_impact, _impact, False),
    "trial": Check(_is_str, _trial, False),
    "command_passes": Check(_is_str, _command_passes, False),
    "ran": Check(_is_str, _ran, False),
    "wrote": Check(_is_str, _wrote, False),
    "metrics": Check(_is_metric_conditions, _metrics, False),
    "events": Check(_is_event_count, _event_count, False),
}


def evaluate(obs: Observed, expect: Expect) -> Outcome:
    check = CHECKS[expect.check]
    if not check.needs_record:
        return check.run(obs, expect.value)
    try:
        rec = find_record(obs.world, expect.record or "")
    except LookupError as exc:
        return False, str(exc)
    return check.run(obs, rec, expect.value)
