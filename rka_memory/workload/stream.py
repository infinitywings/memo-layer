"""Stream files: YAML in, typed sessions out. The format is in docs/WORKLOAD.md.

Everything a scenario can get wrong is rejected here, when the file loads:
unknown keys, bad offsets, unknown checks, miner rules without content or
selectors, and config keys the runner owns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from rka_memory.domain import SCOPE_FIELDS, RecordKind
from rka_memory.workload.checks import CHECKS, Expect

OFFSET = re.compile(r"\+?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?")
RUNNER_OWNED_CONFIG = frozenset({"ledger", "core", "miner", "hooks", "skills"})
# A live model's output is not known in advance, so a live stream cannot
# script the agent or the miner, name records by content, or assert on them.
MODELS = ("scripted", "live")


class StreamError(ValueError):
    pass


@dataclass(frozen=True)
class Write:
    path: str
    content: str


@dataclass(frozen=True)
class Remove:
    path: str


@dataclass(frozen=True)
class Append:
    path: str
    text: str


@dataclass(frozen=True)
class Replace:
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Restore:
    """Put the file back as it was before the latest agent session; delete it if it did not exist."""

    path: str


@dataclass(frozen=True)
class Verdict:
    """A typed `rkm confirm|reject`; `record` is the record's content, resolved to its id at run time."""

    confirm: bool
    record: str
    reason: str = ""


FileAction = Write | Remove | Append | Replace | Restore
UserAction = FileAction | Verdict


@dataclass(frozen=True)
class Session:
    label: str
    at: timedelta
    prompt: str
    drift: tuple[FileAction, ...]
    agent: tuple[dict[str, Any], ...]
    say: str
    miner: dict[str, Any]
    user: tuple[UserAction, ...]
    expect: tuple[Expect, ...]


@dataclass(frozen=True)
class Stream:
    name: str
    description: str
    model: str
    start: datetime
    config: dict[str, Any]
    fixture: Path
    sessions: tuple[Session, ...]
    source: Path


def parse_offset(text: str) -> timedelta:
    """`+61d`, `+1d12h`, `+90m` -> timedelta."""
    m = OFFSET.fullmatch(str(text).strip())
    if m is None or not any(m.groups()):
        raise StreamError(f"offset {text!r} is not like +2d, +1d12h or +30m")
    days, hours, minutes = (int(g or 0) for g in m.groups())
    return timedelta(days=days, hours=hours, minutes=minutes)


def _keys(d: Any, where: str, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise StreamError(f"{where}: expected a mapping, got {type(d).__name__}")
    missing = required - set(d)
    unknown = set(d) - required - set(optional)
    if missing or unknown:
        raise StreamError(f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    return d


def _single(d: Any, where: str, allowed: set[str]) -> tuple[str, Any]:
    if not isinstance(d, dict) or len(d) != 1 or next(iter(d)) not in allowed:
        raise StreamError(f"{where}: expected one key out of {sorted(allowed)}, got {d!r}")
    return next(iter(d.items()))


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise StreamError(f"{where}: expected a string, got {value!r}")
    return value


def _list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise StreamError(f"{where}: expected a list")
    return value


def _file_action(key: str, value: Any, where: str) -> FileAction:
    if key == "write":
        d = _keys(value, where, {"path", "content"})
        return Write(_str(d["path"], where), _str(d["content"], where))
    if key == "remove":
        return Remove(_str(value, where))
    if key == "append":
        d = _keys(value, where, {"path", "text"})
        return Append(_str(d["path"], where), _str(d["text"], where))
    if key == "replace":
        d = _keys(value, where, {"path", "old", "new"})
        return Replace(_str(d["path"], where), _str(d["old"], where), _str(d["new"], where))
    return Restore(_str(value, where))


FILE_ACTIONS = {"write", "remove", "append", "replace", "restore"}


def _user_action(raw: Any, where: str) -> UserAction:
    key, value = _single(raw, where, FILE_ACTIONS | {"confirm", "reject"})
    if key == "confirm":
        return Verdict(True, _str(value, where))
    if key == "reject":
        d = _keys(value, where, {"record", "reason"})
        return Verdict(False, _str(d["record"], where), _str(d["reason"], where))
    return _file_action(key, value, where)


def _in_project(path: str) -> str:
    return path if path.startswith(("/", "{")) else "{project}/" + path


def _agent_step(raw: Any, where: str) -> dict[str, Any]:
    """One scripted tool call, in the shape the mock model sends."""
    key, value = _single(raw, where, {"bash", "write", "edit", "skill", "call"})
    if key == "bash":
        args = {"command": value} if isinstance(value, str) else dict(_keys(value, where, {"command"}, {"sandbox_permissions", "justification"}))
        return {"tool": "bash", "args": {"description": "run a command", **args}}
    if key == "write":
        d = _keys(value, where, {"path", "content"})
        return {"tool": "write", "args": {"file_path": _in_project(_str(d["path"], where)), "content": _str(d["content"], where)}}
    if key == "edit":
        d = _keys(value, where, {"path", "old", "new"})
        args = {"file_path": _in_project(_str(d["path"], where)), "old_string": d["old"], "new_string": d["new"]}
        return {"tool": "edit", "args": args}
    if key == "skill":
        if value != "offered":
            raise StreamError(f"{where}: `skill: offered` loads the rka-memory skill the request offers")
        return {"skill": "offered"}
    d = _keys(value, where, {"tool", "args"})
    return {"tool": _str(d["tool"], where), "args": dict(d["args"])}


def _selector(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str):
        raise StreamError(f"{where}: an event selector needs a `kind`, e.g. {{kind: test_result}}")
    return dict(raw)


LINK_KEYS = ("counter_links", "support_links")
CANDIDATE_KEYS = {"broaden", "evidence", "falsifier", "confidence", "steps", "acceptance_check", "depends_on"}


def _miner(raw: Any, where: str) -> dict[str, Any]:
    d = _keys(raw or {}, where, set(), {"task_class", "candidates", *LINK_KEYS})
    candidates = []
    for i, c in enumerate(_list(d.get("candidates"), f"{where}.candidates")):
        cw = f"{where}.candidates[{i}]"
        c = _keys(c, cw, {"content", "kind"}, CANDIDATE_KEYS)
        if c["kind"] not in {k.value for k in RecordKind}:
            raise StreamError(f"{cw}: kind must be one of {[k.value for k in RecordKind]}")
        if set(_list(c.get("broaden"), cw)) - set(SCOPE_FIELDS):
            raise StreamError(f"{cw}: broaden names scope fields {list(SCOPE_FIELDS)}")
        evidence = [_selector(s, f"{cw}.evidence") for s in _list(c.get("evidence"), cw)]
        depends_on = [_str(x, f"{cw}.depends_on") for x in _list(c.get("depends_on"), cw)]
        candidates.append({**c, "evidence": evidence, "depends_on": depends_on})
    out: dict[str, Any] = {"candidates": candidates}
    for key in LINK_KEYS:
        links = []
        for i, link in enumerate(_list(d.get(key), f"{where}.{key}")):
            lw = f"{where}.{key}[{i}]"
            link = _keys(link, lw, {"record", "event"}, {"reason"})
            links.append({"record": _str(link["record"], lw), "event": _selector(link["event"], lw), "reason": link.get("reason", "")})
        out[key] = links
    if d.get("task_class"):
        out["task_class"] = _str(d["task_class"], where)
    return out


def _expect(raw: Any, where: str) -> Expect:
    if not isinstance(raw, dict):
        raise StreamError(f"{where}: expected a mapping")
    record = raw.get("record")
    kinds = set(raw) - {"record"}
    if len(kinds) != 1 or next(iter(kinds)) not in CHECKS:
        raise StreamError(f"{where}: expected `record` plus one check out of {sorted(CHECKS)}, got {sorted(raw)}")
    name = next(iter(kinds))
    check = CHECKS[name]
    if check.needs_record != (record is not None):
        raise StreamError(f"{where}: `{name}` {'needs' if check.needs_record else 'takes no'} `record`")
    try:
        check.validate(raw[name])
    except ValueError as exc:
        raise StreamError(f"{where}.{name}: {exc}") from None
    return Expect(name, raw[name], _str(record, where) if record is not None else None)


SESSION_KEYS = {"drift", "agent", "miner", "user", "expect"}


def _session(raw: Any, index: int, where: str) -> Session:
    d = _keys(raw, where, {"at", "prompt"}, SESSION_KEYS)
    steps = _list(d.get("agent"), f"{where}.agent")
    say = "Done."
    if steps and isinstance(steps[-1], dict) and set(steps[-1]) == {"say"}:
        say = _str(steps.pop()["say"], f"{where}.agent")
    drift = []
    for i, raw_action in enumerate(_list(d.get("drift"), where)):
        key, value = _single(raw_action, f"{where}.drift[{i}]", FILE_ACTIONS)
        drift.append(_file_action(key, value, f"{where}.drift[{i}]"))
    return Session(
        label=f"s{index}",
        at=parse_offset(d["at"]),
        prompt=_str(d["prompt"], f"{where}.prompt"),
        drift=tuple(drift),
        agent=tuple(_agent_step(s, f"{where}.agent[{i}]") for i, s in enumerate(steps)),
        say=say,
        miner=_miner(d.get("miner"), f"{where}.miner"),
        user=tuple(_user_action(a, f"{where}.user[{i}]") for i, a in enumerate(_list(d.get("user"), where))),
        expect=tuple(_expect(e, f"{where}.expect[{i}]") for i, e in enumerate(_list(d.get("expect"), where))),
    )


def _start(value: Any, where: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise StreamError(f"{where}: start is not an ISO 8601 instant") from None
    if not isinstance(value, datetime):
        raise StreamError(f"{where}: start is not an ISO 8601 instant")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def find_fixture(source: Path, name: str) -> Path:
    """`fixtures/<name>` in the nearest directory above the stream file that has one."""
    for parent in source.resolve().parents:
        candidate = parent / "fixtures" / name
        if candidate.is_dir():
            return candidate
    raise StreamError(f"{source}: no fixtures/{name} directory above the stream file")


def parse(doc: Any, source: Path) -> Stream:
    where = str(source)
    d = _keys(doc, where, {"name", "start", "project", "sessions"}, {"description", "config", "model"})
    model = d.get("model", "scripted")
    if model not in MODELS:
        raise StreamError(f"{where}: model must be one of {list(MODELS)}, got {model!r}")
    config = _keys(d.get("config") or {}, f"{where}.config", set(), set(d.get("config") or {}))
    if RUNNER_OWNED_CONFIG & set(config):
        raise StreamError(f"{where}.config: the runner sets {sorted(RUNNER_OWNED_CONFIG & set(config))}")
    sessions = tuple(_session(s, i, f"{where}.sessions[{i - 1}]") for i, s in enumerate(_list(d["sessions"], where), 1))
    if not sessions:
        raise StreamError(f"{where}: a stream needs at least one session")
    if any(b.at < a.at for a, b in zip(sessions, sessions[1:])):
        raise StreamError(f"{where}: session offsets must not go back in time")
    for s in sessions if model == "live" else ():
        scripted = {
            "agent": s.agent,
            "miner": any(s.miner[k] for k in ("candidates", *LINK_KEYS)) or "task_class" in s.miner,
            "expect": s.expect,
            "typed verdicts": any(isinstance(a, Verdict) for a in s.user),
        }
        used = [k for k, v in scripted.items() if v]
        if used:
            raise StreamError(f"{where}.sessions[{s.label[1:]}]: a live stream cannot use {', '.join(used)}")
    return Stream(
        name=_str(d["name"], where),
        description=str(d.get("description") or ""),
        model=model,
        start=_start(d["start"], where),
        config=dict(config),
        fixture=find_fixture(source, _str(d["project"], where)),
        sessions=sessions,
        source=source,
    )


def load(path: Path) -> Stream:
    return parse(yaml.safe_load(path.read_text(encoding="utf-8")), path)


def load_all(path: Path) -> list[Stream]:
    """One stream file, or every *.yaml in a directory, sorted by file name."""
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    if not files:
        raise StreamError(f"{path}: no stream files")
    streams = [load(f) for f in files]
    names = [s.name for s in streams]
    if len(set(names)) != len(names):
        raise StreamError(f"{path}: stream names must be unique, got {names}")
    return streams
