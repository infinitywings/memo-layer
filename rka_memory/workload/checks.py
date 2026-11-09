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

from rka_memory.config import ARMS
from rka_memory.domain import SCOPE_FIELDS, Event, Rung
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


PACKET_HEADERS = ("## Validated memory (rka-memory)", "## Unvalidated memory hints (rka-memory)")


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


def _narrowed(obs: Observed, rec: RecordState, want: dict[str, str]) -> Outcome:
    got = {(n.field, n.value) for n in rec.narrowings}
    return set(want.items()) <= got, sorted(got)


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


def _is_path_text(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"path", "text"} or not all(isinstance(v, str) for v in value.values()):
        raise ValueError("expected {path, text}")


def _file_contains(obs: Observed, want: dict[str, str]) -> Outcome:
    path = Path(want["path"])
    if not path.is_file():
        return False, "no such file"
    found = want["text"] in path.read_text(encoding="utf-8", errors="replace")
    return found, "found" if found else "not found"


def _model_never_saw(obs: Observed, text: str) -> Outcome:
    """No request the model received in this session carried the text: not in
    a prompt, not in a tool result. The canaries the runner plants in protected
    directories make this a read-leak check."""
    hits = [i for i, body in enumerate(_agent_requests(obs)) if text in json.dumps(body, ensure_ascii=False)]
    return not hits, f"seen in requests {hits}" if hits else "never seen"


def _model_saw(obs: Observed, text: str) -> Outcome:
    """Some request the model received in this session carried the text."""
    hits = [i for i, body in enumerate(_agent_requests(obs)) if text in json.dumps(body, ensure_ascii=False)]
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


def _is_scope_values(value: Any) -> None:
    if not isinstance(value, dict) or not value or any(k not in SCOPE_FIELDS or not isinstance(v, str) for k, v in value.items()):
        raise ValueError(f"expected a mapping of scope fields {list(SCOPE_FIELDS)} to values")


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
    "narrowed": Check(_is_scope_values, _narrowed, True),
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
    "command_passes": Check(_is_str, _command_passes, False),
    "metrics": Check(_is_metric_conditions, _metrics, False),
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
