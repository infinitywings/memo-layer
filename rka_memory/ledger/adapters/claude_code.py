"""Claude Code hook payloads and transcripts, parsed into ledger events.

Everything here is pure: the hook shell reads stdin, hashes files and appends;
these functions only turn dicts into `Event`s. Payload shapes vary across
Claude Code versions, so every field is read tolerantly.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from rka_memory.config import Config
from rka_memory.domain import Actor, Event, Kind, RecordKind, RecordSpec, Scope, State, canonical_json, derived_id

WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
# Tools that cannot change a file. After any other tool the runner re-reads
# the tests and test configuration (hooks._step_check).
READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead", "WebFetch", "WebSearch", "TodoWrite", "decide"})
EXIT_CODE_FIELDS = ("exit_code", "exitCode", "returnCode", "return_code")
_EXIT_CODE_TEXT = re.compile(r"Exit code (-?\d+)")


def _clip(text: str, cfg: Config) -> str:
    return text[: cfg.payload_max_chars]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def session_id(payload: Mapping[str, Any]) -> str:
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        raise ValueError("hook payload has no session_id")
    return sid


def written_path(tool: str, tool_input: Mapping[str, Any], cwd: str | None) -> str | None:
    """Absolute path a file-writing tool targets, or None for other tools."""
    if tool not in WRITE_TOOLS:
        return None
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    return str(path)


def input_summary(tool: str, tool_input: Mapping[str, Any], cfg: Config) -> str:
    if tool == "Bash" and isinstance(tool_input.get("command"), str):
        text = tool_input["command"]
    elif isinstance(tool_input.get("file_path") or tool_input.get("notebook_path"), str):
        text = tool_input.get("file_path") or tool_input["notebook_path"]
    elif tool == "Skill" and isinstance(tool_input.get("skill"), str):
        text = tool_input["skill"]
    else:
        text = canonical_json(dict(tool_input))
    return _clip(text, cfg)


def _response_summary(response: Any, cfg: Config) -> str:
    if isinstance(response, str):
        return _clip(response, cfg)
    resp = _mapping(response)
    if "stdout" in resp or "stderr" in resp:
        parts = [str(resp.get("stdout") or ""), str(resp.get("stderr") or "")]
        return _clip("\n".join(p for p in parts if p), cfg)
    if response is None:
        return ""
    return _clip(canonical_json(response), cfg)


RUNNER_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("uv", "run"),
    ("poetry", "run"),
    ("pipenv", "run"),
    ("pdm", "run"),
    ("npx",),
    ("bunx",),
    ("pnpm", "exec"),
    ("python", "-m"),
    ("python3", "-m"),
)
SHELL_META = re.compile(r"[;&|`<>#\n]|\$\(")
ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def command_argv(command: str) -> list[str] | None:
    """The argv of a plain command with runner prefixes stripped, or None for
    anything compound, piped, redirected or prefixed with env assignments
    (`PATH=/tmp/x pytest` could run any binary)."""
    if SHELL_META.search(command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if argv and ENV_ASSIGNMENT.fullmatch(argv[0]):
        return None
    stripped = True
    while stripped:
        stripped = False
        for prefix in RUNNER_PREFIXES:
            if len(argv) > len(prefix) and tuple(argv[: len(prefix)]) == prefix:
                argv, stripped = argv[len(prefix) :], True
    return argv or None


PYTEST_FLAGS = frozenset(
    {"-q", "-qq", "-v", "-vv", "-vvv", "-x", "-s", "-l", "-ra", "-rA", "--quiet", "--verbose", "--exitfirst",
     "--lf", "--last-failed", "--ff", "--failed-first", "--no-header"}
)
JS_FLAGS = frozenset({"--run", "--ci", "--silent"})
# Per runner: flags allowed as-is, and flags allowed only in `--name=value` form.
# Anything else after the runner (listing, plugin, config, rootdir or workspace
# switches) means the command records an exit code but no test result.
TEST_RUNNER_ARGS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "pytest": (PYTEST_FLAGS, frozenset({"--tb", "--maxfail", "--durations", "--color"})),
    "cargo": (frozenset({"-q", "--quiet", "-v", "--verbose", "--release", "--all", "--workspace"}), frozenset()),
    "go": (frozenset({"-v", "-race", "-short"}), frozenset({"-count", "-timeout"})),
    "npm": (JS_FLAGS, frozenset()),
    "pnpm": (JS_FLAGS, frozenset()),
    "yarn": (JS_FLAGS, frozenset()),
    "bun": (JS_FLAGS, frozenset()),
    "vitest": (JS_FLAGS, frozenset()),
    "jest": (JS_FLAGS, frozenset()),
}
NO_ARGS: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())
TESTS_RAN = re.compile(
    r"\b[1-9]\d* (?:passed|tests? passed)\b"
    r"|test result: ok\. [1-9]\d* passed"
    r"|^ok\s+\S+\s+[\d.]+s\s*$"
    r"|Tests:.*\b[1-9]\d* passed"
    r"|\b[1-9]\d* (?:tests?|specs?|examples?) (?:completed|passed|run)\b",
    re.MULTILINE | re.IGNORECASE,
)


NO_TESTS = re.compile(r"\[no test files\]|\[no tests to run\]|no tests ran|\b0 passed\b", re.IGNORECASE)
SUMMARY_LINES = 5


def tests_ran(output: str) -> bool:
    """The runner's closing summary shows at least one test ran and passed.

    Only the last lines count, so text printed earlier in the run cannot pose
    as the summary."""
    tail = "\n".join([line for line in output.splitlines() if line.strip()][-SUMMARY_LINES:])
    return NO_TESTS.search(tail) is None and TESTS_RAN.search(tail) is not None


# Runners whose positional arguments select tests. For the others (make and the
# package-script runners) a positional is a target or a variable override
# such as `make test PYTEST=true`, so none is allowed.
PATH_SELECTING_RUNNERS = frozenset({"pytest", "cargo", "go", "vitest", "jest"})


def _allowed_positional(arg: str, runner: str) -> bool:
    if runner not in PATH_SELECTING_RUNNERS or "=" in arg:
        return False
    # Stay a relative path inside the project: `/tmp/fake_test.py`, `../x` or a
    # pytest `@argsfile` could run other code.
    parts = Path(arg).parts
    inside = not arg.startswith(("@", "~")) and not Path(arg).is_absolute() and ".." not in parts and "://" not in arg
    if runner == "go":
        return inside and (arg in (".", "./...") or arg.startswith("./"))
    return inside


def _allowed_test_arg(arg: str, runner: str) -> bool:
    flags, value_flags = TEST_RUNNER_ARGS.get(runner, NO_ARGS)
    if arg == "--":
        return True
    if arg.startswith("-"):
        name, has_value, _ = arg.partition("=")
        return name in value_flags if has_value else arg in flags
    return _allowed_positional(arg, runner)


def is_test_command(command: str, cfg: Config) -> bool:
    """A test result is strong grounding, so only a plain invocation of a
    configured runner with allowlisted arguments counts; `echo pytest`,
    `pytest; true`, `pytest --collect-only` or `go test -list .` do not."""
    argv = command_argv(command)
    if argv is None:
        return False
    for configured in cfg.test_commands:
        want = command_argv(configured)
        if want and argv[: len(want)] == want:
            return all(_allowed_test_arg(a, want[0]) for a in argv[len(want) :])
    return False


@dataclass(frozen=True)
class ToolUse:
    session_id: str
    tool: str
    tool_input: Mapping[str, Any]
    tool_use_id: str
    response: Any
    error: str | None
    interrupted: bool

    @classmethod
    def parse(cls, payload: Mapping[str, Any], *, failed: bool) -> ToolUse:
        response = payload.get("tool_response")
        return cls(
            session_id=session_id(payload),
            tool=str(payload.get("tool_name") or "unknown"),
            tool_input=_mapping(payload.get("tool_input")),
            tool_use_id=str(payload.get("tool_use_id") or ""),
            response=response,
            error=str(payload.get("error") or "tool failed") if failed else None,
            interrupted=bool(payload.get("is_interrupt") or _mapping(response).get("interrupted")),
        )

    @property
    def command(self) -> str:
        command = self.tool_input.get("command")
        return command if isinstance(command, str) else ""

    @property
    def backgrounded(self) -> bool:
        resp = _mapping(self.response)
        return bool(self.tool_input.get("run_in_background") or resp.get("backgroundTaskId"))

    def event(self, kind: Kind, actor: Actor, payload: Mapping[str, Any], state_hash: str) -> Event:
        ev_id = derived_id("ev", "tool", self.session_id, self.tool_use_id, kind.value) if self.tool_use_id else None
        return Event.new(kind, actor, self.session_id, payload, state_hash=state_hash, id=ev_id)


def _outcome_known(payload: Mapping[str, Any], use: ToolUse) -> bool:
    """Claude Code always sends a transcript and routes failed commands to
    PostToolUseFailure. A caller without a transcript must state the exit code
    itself (the rka-memory dsh plugin does). dsh's generic hooks-claude-code
    bridge sends neither and has no failure event, so its Bash calls record no
    outcome at all."""
    if payload.get("transcript_path") or use.error is not None:
        return True
    resp = _mapping(use.response)
    return any(isinstance(resp.get(name), int) for name in EXIT_CODE_FIELDS)


def _exit_code(use: ToolUse) -> int | None:
    """The command's exit status, or None when it is not known."""
    if use.interrupted or use.backgrounded:
        return None
    if use.error is not None:
        match = _EXIT_CODE_TEXT.search(use.error)
        return int(match.group(1)) if match else 1
    resp = _mapping(use.response)
    for name in EXIT_CODE_FIELDS:
        if isinstance(resp.get(name), int):
            return resp[name]
    return 0


def shell_write_event(
    payload: Mapping[str, Any], path: str, pre_hash: str, post_hash: str, state_hash: str = ""
) -> Event:
    """A file the agent wrote earlier changed during one of its shell commands."""
    use = ToolUse.parse(payload, failed=payload.get("hook_event_name") == "PostToolUseFailure")
    return Event.new(
        Kind.FILE_WRITE,
        Actor.TOOL,
        use.session_id,
        {"tool": use.tool, "tool_use_id": use.tool_use_id, "path": path, "pre_hash": pre_hash, "post_hash": post_hash},
        state_hash=state_hash,
        id=derived_id("ev", "shell-write", use.session_id, use.tool_use_id, path),
    )


def late_write_event(session_id: str, path: str, pre_hash: str, post_hash: str, state_hash: str = "") -> Event:
    """A file the agent wrote changed after its last tool call but before the
    session ended, e.g. by a backgrounded command: still the agent's change."""
    return Event.new(
        Kind.FILE_WRITE,
        Actor.TOOL,
        session_id,
        {"tool": "session_end", "tool_use_id": "", "path": path, "pre_hash": pre_hash, "post_hash": post_hash},
        state_hash=state_hash,
        id=derived_id("ev", "late-write", session_id, path, post_hash),
    )


def snapshot_event(
    payload: Mapping[str, Any], path: str | None, sha: str | None, kept: bool, state_hash: str = ""
) -> Event:
    """A file the agent's shell command left changed, kept for taint; with no
    path, the command changed more than could be kept."""
    use = ToolUse.parse(payload, failed=payload.get("hook_event_name") == "PostToolUseFailure")
    body: dict[str, Any] = {"tool": use.tool, "tool_use_id": use.tool_use_id, "path": path, "hash": sha}
    if not kept:
        body["unstored"] = True
    return Event.new(
        Kind.FILE_SNAPSHOT,
        Actor.TOOL,
        use.session_id,
        body,
        state_hash=state_hash,
        id=derived_id("ev", "snapshot", use.session_id, use.tool_use_id, path or "*", sha or ""),
    )


def reply_event(session: str, key: str, text: str, cfg: Config, store: Callable[[str], str | None], state_hash: str = "") -> Event:
    """What the agent said to the user, kept for taint (DESIGN_V3 3.2)."""
    kept = _clip(text, cfg)
    return Event.new(
        Kind.ASSISTANT_MSG,
        Actor.MODEL,
        session,
        {"text": kept, **_full(kept, text, store)},
        state_hash=state_hash,
        id=derived_id("ev", "reply", session, key),
    )


def transcript_replies(records: Iterable[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """(message id, text) of every assistant message in a Claude Code
    transcript that says something, in order; blocks of one message joined."""
    texts: dict[str, list[str]] = {}
    for i, record in enumerate(records):
        if record.get("type") != "assistant":
            continue
        message = _mapping(record.get("message"))
        key = str(message.get("id") or record.get("uuid") or i)
        content = message.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}] if isinstance(content, str) else []
        said = [str(_mapping(b).get("text") or "") for b in blocks if _mapping(b).get("type") == "text"]
        texts.setdefault(key, []).extend(t for t in said if t)
    return [(key, "".join(parts)) for key, parts in texts.items() if "".join(parts).strip()]


def _raw_output(use: ToolUse) -> str:
    resp = _mapping(use.response)
    return "\n".join(str(resp.get(k) or "") for k in ("stdout", "stderr")) if resp else str(use.response or "")


def _full(kept: str, whole: str, store: Callable[[str], str | None] | None) -> dict[str, Any]:
    """Where the whole of a clipped text is kept (DESIGN_V3 3.2): its hash in
    the content store, or `unstored` when it could not be kept."""
    if store is None or whole == kept:
        return {}
    sha = store(whole)
    return {"full_hash": sha} if sha else {"unstored": True}


def _whole_input(tool_input: Mapping[str, Any]) -> str:
    """Every string the model put in a tool call, such as a written file's
    content or an edit's new text, in order."""
    def strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, Mapping):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, (list, tuple)):
            return [s for v in value for s in strings(v)]
        return []

    return "\n".join(strings(dict(tool_input)))


def _whole_response(use: ToolUse) -> str:
    if use.error is not None:
        return use.error
    resp = _mapping(use.response)
    if isinstance(use.response, str):
        return use.response
    if "stdout" in resp or "stderr" in resp:
        return "\n".join(p for p in (str(resp.get("stdout") or ""), str(resp.get("stderr") or "")) if p)
    return "" if use.response is None else canonical_json(use.response)


def tool_events(
    payload: Mapping[str, Any],
    cfg: Config,
    *,
    failed: bool,
    state_hash: str = "",
    pre_hash: str = "unknown",
    post_hash: str | None = None,
    store: Callable[[str], str | None] | None = None,
) -> list[Event]:
    """Events for one PostToolUse (`failed=False`) or PostToolUseFailure payload.

    `post_hash` is the written file's hash after the tool ran, supplied by the
    caller for file-writing tools. With `store`, the whole input and output
    go to the content store when the payload keeps less of them, so taint
    can read every word the agent wrote or saw.
    """
    use = ToolUse.parse(payload, failed=failed)
    ref = {"tool": use.tool, "tool_use_id": use.tool_use_id}
    kept_input = input_summary(use.tool, use.tool_input, cfg)
    kept_output = _clip(use.error, cfg) if failed else _response_summary(use.response, cfg)
    events = [
        use.event(
            Kind.TOOL_CALL,
            Actor.MODEL,
            {**ref, "input": kept_input, **_full(kept_input, _whole_input(use.tool_input), store)},
            state_hash,
        ),
        use.event(
            Kind.TOOL_RESULT,
            Actor.TOOL,
            {**ref, "ok": not failed, "summary": kept_output, **_full(kept_output, _whole_response(use), store)},
            state_hash,
        ),
    ]
    if use.tool == "Bash" and _outcome_known(payload, use):
        code = _exit_code(use)
        if code is not None:
            outcome = {"command": _clip(use.command, cfg), "tool_use_id": use.tool_use_id}
            events.append(use.event(Kind.EXIT_CODE, Actor.TOOL, {**outcome, "code": code}, state_hash))
            if is_test_command(use.command, cfg) and (code != 0 or tests_ran(_raw_output(use))):
                events.append(use.event(Kind.TEST_RESULT, Actor.TOOL, {**outcome, "passed": code == 0}, state_hash))
    path = written_path(use.tool, use.tool_input, payload.get("cwd"))
    if path and not failed and post_hash is not None:
        events.append(
            use.event(
                Kind.FILE_WRITE,
                Actor.TOOL,
                {**ref, "path": path, "pre_hash": pre_hash, "post_hash": post_hash},
                state_hash,
            )
        )
    return events


DECIDE_TOOLS = frozenset({"decide"})


def decision_event(payload: Mapping[str, Any], state: State, cfg: Config, state_hash: str = "") -> Event | None:
    """The decision a successful `decide` call recorded, proposed by the model
    and in force in its session from now on (DESIGN_V2 7.1). None when the
    call is not a well-formed decide call."""
    use = ToolUse.parse(payload, failed=False)
    args = use.tool_input
    if use.tool.lower() not in DECIDE_TOOLS or not isinstance(args, Mapping):
        return None
    choice, alternatives, reason = args.get("choice"), args.get("alternatives"), args.get("reason")
    if not isinstance(choice, str) or not choice.strip() or not isinstance(reason, str):
        return None
    if not isinstance(alternatives, list) or not all(isinstance(a, str) for a in alternatives):
        return None
    spec = RecordSpec(
        derived_id("d", use.session_id, use.tool_use_id, choice),
        RecordKind.DECISION,
        _clip(" ".join(choice.split()), cfg),
        Scope.exact(state, broaden=cfg.open_scope_fields),
        alternatives=tuple(_clip(" ".join(a.split()), cfg) for a in alternatives if a.strip()),
        reason=_clip(" ".join(reason.split()), cfg),
    )
    return Event.new(
        Kind.RECORD_PROPOSED,
        Actor.MODEL,
        use.session_id,
        {**spec.to_json(), "origin": "decide", "tool_use_id": use.tool_use_id},
        state_hash=state_hash,
        id=derived_id("ev", "decide", use.session_id, use.tool_use_id),
    )


def session_start_event(payload: Mapping[str, Any], state: State, model: str | None) -> Event:
    sid = session_id(payload)
    return Event.new(
        Kind.SESSION_START,
        Actor.RUNNER,
        sid,
        {
            "state": state.to_json(),
            "source": payload.get("source"),
            "cwd": payload.get("cwd"),
            "transcript_path": payload.get("transcript_path"),
            "model": model,
            "harness": payload.get("harness") or "claude_code",
            "parent_session": payload.get("parent_session"),
        },
        state_hash=state.hash(),
        id=derived_id("ev", "session_start", sid),
    )


def prompt_text(payload: Mapping[str, Any]) -> str:
    prompt = payload.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def user_msg_event(payload: Mapping[str, Any], cfg: Config, state_hash: str = "") -> Event:
    sid = session_id(payload)
    text = prompt_text(payload)
    prompt_id = payload.get("prompt_id")
    return Event.new(
        Kind.USER_MSG,
        Actor.USER,
        sid,
        {
            "text": _clip(text, cfg),
            "sha": hashlib.sha256(text.encode()).hexdigest(),
            "prompt_id": prompt_id,
        },
        state_hash=state_hash,
        id=derived_id("ev", "user_msg", sid, prompt_id) if isinstance(prompt_id, str) and prompt_id else None,
    )


def task_class_judgment(session: str, task_class: str, state_hash: str = "") -> Event:
    return Event.new(
        Kind.JUDGMENT,
        Actor.RUNNER,
        session,
        {"target": "session", "session_id": session, "field": "task_class", "value": task_class, "judge": "keywords"},
        state_hash=state_hash,
        id=derived_id("ev", "judgment", session, "task_class"),
    )


@dataclass(frozen=True)
class TranscriptUsage:
    tokens_total: int = 0
    tokens_before_first_write: int = 0
    first_write_seen: bool = False
    # The four parts of the total: prompt tokens served fresh, from the
    # provider's cache, or written to it, and output (E2 measures cache reuse).
    tokens_input: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    tokens_output: int = 0


def read_transcript(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _assistant_messages(records: Iterable[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], bool]]:
    """(usage, has_write) per assistant message, in transcript order.

    Claude Code writes one record per content block, each repeating the
    message's usage, so records sharing a message id are one message.
    """
    messages: dict[Any, tuple[Mapping[str, Any], bool]] = {}
    for i, record in enumerate(records):
        if record.get("type") != "assistant":
            continue
        message = _mapping(record.get("message"))
        key = message.get("id") or ("record", i)
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        has_write = any(
            _mapping(b).get("type") == "tool_use" and _mapping(b).get("name") in WRITE_TOOLS for b in blocks
        )
        _, seen_write = messages.get(key, ({}, False))
        messages[key] = (_mapping(message.get("usage")), seen_write or has_write)
    return list(messages.values())


def _tokens(usage: Mapping[str, Any]) -> int:
    fields = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return sum(usage[f] for f in fields if isinstance(usage.get(f), int))


PARTS = {
    "tokens_input": "input_tokens",
    "tokens_cache_read": "cache_read_input_tokens",
    "tokens_cache_write": "cache_creation_input_tokens",
    "tokens_output": "output_tokens",
}


def transcript_usage(records: Iterable[Mapping[str, Any]]) -> TranscriptUsage:
    total, before, seen = 0, 0, False
    parts = dict.fromkeys(PARTS, 0)
    for usage, has_write in _assistant_messages(records):
        if has_write:
            seen = True
        tokens = _tokens(usage)
        total += tokens
        if not seen:
            before += tokens
        for name, field_name in PARTS.items():
            value = usage.get(field_name)
            parts[name] += value if isinstance(value, int) else 0
    return TranscriptUsage(total, before, seen, **parts)


def transcript_model(records: Iterable[Mapping[str, Any]]) -> str | None:
    for record in records:
        if record.get("type") != "assistant":
            continue
        model = _mapping(record.get("message")).get("model")
        if isinstance(model, str) and model and model != "<synthetic>":
            return model
    return None


def reported_usage(payload: Mapping[str, Any]) -> TranscriptUsage | None:
    """Token usage a harness without transcripts reports in the payload."""
    usage = payload.get("usage")
    if not isinstance(usage, Mapping) or not isinstance(usage.get("tokens_total"), int):
        return None
    return TranscriptUsage(
        int(usage["tokens_total"]),
        int(usage.get("tokens_before_first_write") or 0),
        bool(usage.get("first_write_seen")),
        **{name: int(usage.get(name) or 0) for name in PARTS},
    )


def session_end_event(payload: Mapping[str, Any], usage: TranscriptUsage) -> Event:
    return Event.new(
        Kind.SESSION_END,
        Actor.RUNNER,
        session_id(payload),
        {
            "reason": payload.get("reason"),
            "tokens_total": usage.tokens_total,
            "tokens_before_first_write": usage.tokens_before_first_write,
            "first_write_seen": usage.first_write_seen,
            **{name: getattr(usage, name) for name in PARTS},
        },
    )
