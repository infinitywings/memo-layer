"""Quote-constrained rule extraction.

The full-session miner proposes lessons in its own words. In a live run it
missed a rule the user stated, and asking it for stated rules made it invent
one. This pass asks a narrower question of the user's messages alone: which
standing rules did the user state? Each answer must quote one user message
word for word, and code checks the quote against the ledger. A rule therefore
reaches the ledger as the user's own words, cited to the message that holds
them, or not at all; a restated rule adds evidence to its existing record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from rka_memory.domain import Actor, Kind, RecordKind
from rka_memory.world import World

PROMPT = Template(Path(__file__).with_name("extract_prompt.md").read_text(encoding="utf-8"))
PREFIX = "You are the rule extractor for rka-memory"
RULE_KINDS = (RecordKind.PREFERENCE, RecordKind.CONSTRAINT)
# Long enough to be a rule, short enough to be one rule.
MIN_QUOTE, MAX_QUOTE = 12, 400
# A rule the user states holds wherever they work in this project and context,
# whatever the task, toolchain or model.
BROADEN = frozenset({"task_class", "toolchain", "model_id"})


@dataclass(frozen=True)
class Message:
    event_id: str
    text: str


@dataclass(frozen=True)
class Rule:
    index: int
    quote: str
    kind: RecordKind
    restates: str | None


@dataclass(frozen=True)
class Located:
    """A rule whose quote was found in a user message."""

    rule: Rule
    message: Message


def normalize(text: str) -> str:
    return " ".join(text.split())


def user_messages(world: World, session_id: str) -> list[Message]:
    sess = world.sessions.get(session_id)
    if sess is None:
        return []
    return [
        Message(e.id, normalize(str(e.payload.get("text") or "")))
        for e in sess.events
        if e.kind is Kind.USER_MSG and e.actor is Actor.USER and e.payload.get("text")
    ]


def build_prompt(messages: list[Message], records: list[tuple[str, str]]) -> str:
    rows = "\n".join(f"- {rid}: {content}" for rid, content in records) or "(none)"
    lines = "\n".join(f"{m.event_id}: {m.text}" for m in messages)
    return PROMPT.substitute(records=rows, messages=lines)


def parse_output(text: str) -> tuple[list[Rule], list[tuple[str, str]]]:
    """The rules a reply proposes, and (item, reason) for each one dropped.
    Raises ValueError when the reply holds no JSON object."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("extractor output holds no JSON object")
    try:
        doc = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"extractor output is not valid JSON: {exc}") from exc
    rules: list[Rule] = []
    dropped: list[tuple[str, str]] = []
    raw_rules: Any = doc.get("rules") if isinstance(doc, dict) else None
    for i, raw in enumerate(raw_rules if isinstance(raw_rules, list) else []):
        label = f"rule {i}"
        if not isinstance(raw, dict):
            dropped.append((label, "not a JSON object"))
            continue
        quote = normalize(str(raw.get("quote") or ""))
        try:
            kind = RecordKind(raw.get("kind"))
        except ValueError:
            dropped.append((f"{label}: {quote[:60]}", f"unknown kind {raw.get('kind')!r}"))
            continue
        if kind not in RULE_KINDS:
            dropped.append((f"{label}: {quote[:60]}", f"a stated rule is a preference or a constraint, not a {kind.value}"))
            continue
        restates = raw.get("restates")
        rules.append(Rule(i, quote, kind, str(restates) if restates else None))
    return rules, dropped


def locate(rules: list[Rule], messages: list[Message]) -> tuple[list[Located], list[tuple[str, str]]]:
    """Keep the rules whose quote is a span of one user message."""
    found: list[Located] = []
    dropped: list[tuple[str, str]] = []
    for rule in rules:
        label = f"rule {rule.index}: {rule.quote[:60]}"
        if not MIN_QUOTE <= len(rule.quote) <= MAX_QUOTE:
            dropped.append((label, f"a quote holds {MIN_QUOTE} to {MAX_QUOTE} characters"))
            continue
        message = next((m for m in messages if rule.quote in m.text), None)
        if message is None:
            dropped.append((label, "quote is not in any user message of the session"))
            continue
        found.append(Located(rule, message))
    return found, dropped
