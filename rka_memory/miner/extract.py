"""Quote-constrained rule extraction.

The full-session miner proposes lessons in its own words. In a live run it
missed a rule the user stated, and asking it for stated rules made it invent
one. This pass asks a narrower question of the user's messages alone: which
standing rules did the user state, and which listed rules did the user
withdraw? Each answer must quote one user message word for word, and code
checks the quote against the ledger. A rule therefore reaches the ledger as
the user's own words, cited to the message that holds them, or not at all.

A rule may also quote the words that make it standing, such as "from now on".
Those must come from the same message. The validity engine checks both quotes
again against the logged message and treats such a statement as the user's
confirmation of a record that says exactly what the user said.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from rka_memory.domain import QUOTE_CHARS, Actor, Kind, RecordKind, collapse, quoted, standing
from rka_memory.world import World

PROMPT = Template(Path(__file__).with_name("extract_prompt.md").read_text(encoding="utf-8"))
PREFIX = "You are the rule extractor for rka-memory"
RULE_KINDS = (RecordKind.PREFERENCE, RecordKind.CONSTRAINT)
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
    standing: str | None
    restates: str | None


@dataclass(frozen=True)
class Withdrawal:
    index: int
    quote: str
    record_id: str


@dataclass(frozen=True)
class Located:
    """A rule whose quote was found in a user message. `standing` is set only
    when the standing words were found in the same message."""

    rule: Rule
    message: Message
    standing: str | None

    def statement(self) -> dict[str, str]:
        out = {"quote": self.rule.quote}
        if self.standing:
            out["marker"] = self.standing
        return out


@dataclass(frozen=True)
class Withdrawn:
    withdrawal: Withdrawal
    message: Message


@dataclass(frozen=True)
class Extraction:
    rules: tuple[Located, ...]
    withdrawn: tuple[Withdrawn, ...]
    dropped: tuple[tuple[str, str], ...]


def user_messages(world: World, session_id: str) -> list[Message]:
    sess = world.sessions.get(session_id)
    if sess is None:
        return []
    return [
        Message(e.id, collapse(e.payload.get("text") or ""))
        for e in sess.events
        if e.kind is Kind.USER_MSG and e.actor is Actor.USER and e.payload.get("text")
    ]


def build_prompt(messages: list[Message], records: list[tuple[str, str]]) -> str:
    rows = "\n".join(f"- {rid}: {content}" for rid, content in records) or "(none)"
    lines = "\n".join(f"{m.event_id}: {m.text}" for m in messages)
    return PROMPT.substitute(records=rows, messages=lines)


def _text(value: Any) -> str | None:
    return collapse(value) if isinstance(value, str) and value.strip() else None


def _rule(i: int, raw: Any) -> Rule | tuple[str, str]:
    if not isinstance(raw, dict):
        return (f"rule {i}", "not a JSON object")
    quote = collapse(raw.get("quote") or "")
    label = f"rule {i}: {quote[:60]}"
    try:
        kind = RecordKind(raw.get("kind"))
    except ValueError:
        return (label, f"unknown kind {raw.get('kind')!r}")
    if kind not in RULE_KINDS:
        return (label, f"a stated rule is a preference or a constraint, not a {kind.value}")
    restates = raw.get("restates")
    return Rule(i, quote, kind, _text(raw.get("standing")), str(restates) if restates else None)


def _withdrawal(i: int, raw: Any) -> Withdrawal | tuple[str, str]:
    if not isinstance(raw, dict) or not raw.get("record"):
        return (f"withdrawal {i}", "needs a quote and a record")
    return Withdrawal(i, collapse(raw.get("quote") or ""), str(raw["record"]))


def parse_output(text: str) -> tuple[list[Rule], list[Withdrawal], list[tuple[str, str]]]:
    """The rules and withdrawals a reply proposes, and (item, reason) for each
    one dropped. Raises ValueError when the reply holds no JSON object."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("extractor output holds no JSON object")
    try:
        doc = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"extractor output is not valid JSON: {exc}") from exc
    doc = doc if isinstance(doc, dict) else {}
    rules: list[Rule] = []
    withdrawals: list[Withdrawal] = []
    dropped: list[tuple[str, str]] = []
    for kept, key, parse in ((rules, "rules", _rule), (withdrawals, "withdrawn", _withdrawal)):
        raw_items = doc.get(key)
        for i, raw in enumerate(raw_items if isinstance(raw_items, list) else []):
            parsed = parse(i, raw)
            (dropped if isinstance(parsed, tuple) else kept).append(parsed)
    return rules, withdrawals, dropped


def _source(quote: str, messages: list[Message]) -> Message | str:
    """The message that holds `quote`, or why there is none."""
    low, high = QUOTE_CHARS
    if not low <= len(quote) <= high:
        return f"a quote holds {low} to {high} characters"
    found = next((m for m in messages if quoted(m.text, quote)), None)
    return found if found is not None else "quote is not in any user message of the session"


def locate(
    rules: list[Rule], withdrawals: list[Withdrawal], messages: list[Message], phrases: tuple[str, ...]
) -> Extraction:
    """Keep what quotes a user message. Standing words that fail the engine's
    own check (a configured phrase next to the rule in its message) are
    dropped, and the rule is kept without them."""
    kept: list[Located] = []
    gone: list[Withdrawn] = []
    dropped: list[tuple[str, str]] = []
    for rule in rules:
        label = f"rule {rule.index}: {rule.quote[:60]}"
        source = _source(rule.quote, messages)
        if isinstance(source, str):
            dropped.append((label, source))
            continue
        words = rule.standing
        if words and not standing(source.text, rule.quote, words, phrases):
            dropped.append((f"{label} standing {words[:40]!r}", "no standing phrase next to the rule in its message"))
            words = None
        kept.append(Located(rule, source, words))
    for w in withdrawals:
        source = _source(w.quote, messages)
        if isinstance(source, str):
            dropped.append((f"withdrawal {w.index}: {w.quote[:60]}", source))
            continue
        gone.append(Withdrawn(w, source))
    return Extraction(tuple(kept), tuple(gone), tuple(dropped))
