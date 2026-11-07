"""Scenario-driven OpenAI-compatible chat-completions server for workload runs.

The workload runner rewrites a session script (JSON) before every dsh run, and
the server reads it on each request, so one process serves a whole stream.
Requests come in three kinds:

- agent: the request offers tools. The reply walks the script's `agent` steps,
  one step per tool result already in the conversation, then sends `say`.
- miner: the prompt starts with MINER_PREFIX. The reply is the miner JSON built
  from the script's `miner` rules. Event selectors such as `{kind: test_result}`
  resolve to event ids from the prompt's event lines; record contents resolve
  to ids from its delivered and existing record rows.
- rules: the prompt starts with EXTRACT_PREFIX. The reply lists the script's
  `miner.rules` as given; a `restates` content resolves to the id of the
  record row with that content.
- plain: anything else, such as session titles, gets short text.

Every request is appended to the log as one JSON line with the script label,
the kind, the reply and every rule that could not be resolved, so the runner
can check what the model was sent and fail a scenario whose script misfired.

Usage: python scripts/mock_llm.py <port, 0 for any> <request-log.jsonl> <session-script.json>
The bound port is printed as the first line on stdout. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MINER_PREFIX = "You are the candidate miner for rka-memory"
EXTRACT_PREFIX = "You are the rule extractor for rka-memory"
USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
SKILL_NAME = re.compile(r"\brka-r-[0-9a-z-]+")
ROW = re.compile(r"^- (\S+) \[(\S+) (\S+)\] (.*)$")
EVENT_LINE = re.compile(r"^(ev_\S+) (\S+) (\S+) (.*)$")
RULE_ROW = re.compile(r"^- (\S+): (.*)$")
SECTIONS = (
    ("Records delivered to the agent", "delivered"),
    ("Existing records that apply", "existing"),
    ("Rules the user stated", "stated"),
    ("Events, oldest first", "events"),
)


def normalize(text: str) -> str:
    return " ".join(str(text).split())


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class EventLine:
    id: str
    kind: str
    actor: str
    payload_text: str

    def matches(self, selector: dict[str, Any]) -> bool:
        if selector.get("kind") != self.kind or selector.get("actor", self.actor) != self.actor:
            return False
        try:
            payload = json.loads(self.payload_text)
        except ValueError:
            payload = None
        for key, want in selector.items():
            if key in ("kind", "actor"):
                continue
            if isinstance(payload, dict):
                if payload.get(key) != want:
                    return False
            elif f'"{key}":{compact(want)}' not in self.payload_text:
                return False
        return True


@dataclass(frozen=True)
class MinerPrompt:
    """What the miner prompt shows: record rows by content, and the session's events."""

    delivered: dict[str, str]
    existing: dict[str, str]
    events: tuple[EventLine, ...]


def parse_miner_prompt(prompt: str) -> MinerPrompt:
    found: dict[str, list[str]] = {"delivered": [], "existing": [], "stated": [], "events": []}
    current: list[str] | None = None
    for line in prompt.splitlines():
        heading = next((name for prefix, name in SECTIONS if line.startswith(prefix)), None)
        if heading is not None:
            current = found[heading]
        elif current is not None and line.strip():
            current.append(line)
    rows = {
        name: {normalize(m.group(4)): m.group(1) for m in map(ROW.match, found[name]) if m}
        for name in ("delivered", "existing")
    }
    events = tuple(EventLine(*m.groups()) for m in map(EVENT_LINE.match, found["events"]) if m)
    return MinerPrompt(rows["delivered"], rows["existing"], events)


@dataclass
class Resolver:
    prompt: MinerPrompt
    problems: list[str] = field(default_factory=list)

    def events(self, selector: dict[str, Any], label: str) -> list[str]:
        ids = [e.id for e in self.prompt.events if e.matches(selector)]
        if not ids:
            self.problems.append(f"{label}: no event matches {compact(selector)}")
        return ids

    def record(self, content: str, rows: dict[str, str], label: str) -> str | None:
        rid = rows.get(normalize(content))
        if rid is None:
            self.problems.append(f"{label}: no record row with content {content!r}")
        return rid


def miner_reply(prompt: str, rules: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The miner JSON the rules describe, with selectors and contents resolved to ids."""
    r = Resolver(parse_miner_prompt(prompt))
    known = {**r.prompt.existing, **r.prompt.delivered}
    candidates = []
    for c in rules.get("candidates") or ():
        label = f"candidate {c['content']!r}"
        out = {k: v for k, v in c.items() if k not in ("evidence", "depends_on")}
        out["evidence"] = [eid for sel in c.get("evidence") or () for eid in r.events(sel, f"{label} evidence")]
        deps = [r.record(dep, known, f"{label} depends_on") for dep in c.get("depends_on") or ()]
        out["depends_on"] = [d for d in deps if d]
        candidates.append(out)
    doc: dict[str, Any] = {"candidates": candidates}
    # a counter link names a delivered record; a support link any listed one
    for key, rows in (("counter_links", r.prompt.delivered), ("support_links", known)):
        links = []
        for link in rules.get(key) or ():
            label = f"{key[:-6]} link to {link['record']!r}"
            rid = r.record(link["record"], rows, label)
            for eid in r.events(link["event"], label):
                if rid:
                    links.append({"record_id": rid, "event_id": eid, "reason": link.get("reason", "")})
        doc[key] = links
    if rules.get("task_class"):
        doc["task_class"] = rules["task_class"]
    return doc, r.problems


def rules_reply(prompt: str, rules: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """The extractor JSON: the scripted rules, with `restates` resolved to a record id."""
    _, _, section = prompt.partition("Rules already recorded:")
    section, _, _ = section.partition("User messages")
    rows = {normalize(m.group(2)): m.group(1) for m in map(RULE_ROW.match, section.splitlines()) if m}
    problems, out = [], []
    for rule in rules:
        rid = None
        if rule.get("restates"):
            rid = rows.get(normalize(rule["restates"]))
            if rid is None:
                problems.append(f"rule {rule['quote']!r}: no recorded rule with content {rule['restates']!r}")
                continue
        out.append({"quote": rule["quote"], "kind": rule["kind"], "restates": rid})
    return {"rules": out}, problems


def agent_reply(body: dict[str, Any], script: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The next scripted tool call, or the final text once every step has a result."""
    done = sum(1 for m in body.get("messages") or () if m.get("role") == "tool")
    steps = script.get("agent") or []
    if done >= len(steps):
        return {"content": script.get("say") or "Done."}, []
    step = steps[done]
    if "skill" in step:
        offered = SKILL_NAME.findall(json.dumps(body))
        if not offered:
            return {"content": "No rka-memory skill was offered."}, [f"agent step {done}: no rka-memory skill in the request"]
        return {"tool": ("skill", {"name": offered[0]}), "id": f"call_{done}_skill"}, []
    return {"tool": (step["tool"], step["args"]), "id": f"call_{done}_{step['tool']}"}, []


def first_user_text(body: dict[str, Any]) -> str:
    for m in body.get("messages") or ():
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                return "".join(p.get("text", "") for p in content if isinstance(p, dict))
            return str(content or "")
    return ""


def respond(body: dict[str, Any], script: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
    """(kind, reply, problems) for one request."""
    if body.get("tools"):
        return ("agent", *agent_reply(body, script))
    prompt = first_user_text(body)
    if prompt.startswith(MINER_PREFIX):
        doc, problems = miner_reply(prompt, script.get("miner") or {})
        return "miner", {"content": json.dumps(doc)}, problems
    if prompt.startswith(EXTRACT_PREFIX):
        doc, problems = rules_reply(prompt, (script.get("miner") or {}).get("rules") or [])
        return "rules", {"content": json.dumps(doc)}, problems
    return "plain", {"content": "rka-memory workload"}, []


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "mock-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _message(reply: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if "tool" in reply:
        name, args = reply["tool"]
        call = {"index": 0, "id": reply["id"], "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        return {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
    return {"role": "assistant", "content": reply["content"]}, "stop"


class Handler(BaseHTTPRequestHandler):
    log_path: Path
    script_path: Path

    def log_message(self, *args: Any) -> None:
        pass

    def _send_json(self, doc: dict[str, Any]) -> None:
        out = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self) -> None:
        self._send_json({"object": "list", "data": [{"id": "mock-model", "object": "model"}]})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        script = json.loads(self.script_path.read_text())
        kind, reply, problems = respond(body, script)
        entry = {"label": script.get("label"), "kind": kind, "path": self.path, "body": body, "reply": reply, "problems": problems}
        with open(self.log_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        message, finish = _message(reply)
        if not body.get("stream"):
            self._send_json(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "mock-model",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": USAGE,
                }
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        delta = {"role": "assistant", **({"tool_calls": message["tool_calls"]} if "tool_calls" in message else {"content": message["content"]})}
        self.wfile.write(_chunk(delta))
        self.wfile.write(_chunk({}, finish))
        usage = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock-model", "choices": [], "usage": USAGE}
        self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main(argv: list[str]) -> None:
    port, log, script = int(argv[0]), Path(argv[1]), Path(argv[2])
    Handler.log_path, Handler.script_path = log, script
    server = ThreadingHTTPServer((os.environ.get("MOCK_BIND", "127.0.0.1"), port), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
