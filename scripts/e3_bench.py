#!/usr/bin/env python3
"""E3: ImplicitMemBench across two sessions (docs/E3.md).

Each item's learning phase is session 1, logged to a ledger of the item's
own. The miner and the notepad curator read it. Session 2 holds the
interference turns and the probe, after the arm's packet. The judge prompts
are those of the released src/evaluation/evaluation.py (MIT,
github.com/qinchonghanzuibang/ImplicitMemBench), copied with attribution.

    scripts/e3_bench.py fetch
    scripts/e3_bench.py run --pilot [--out DIR] [--workers N]
    scripts/e3_bench.py run [--families a,b] [--seeds a,b] [--limit N]

The agent and the miner run on the plan proxy (scripts/openai_plan.py
serve); the judge is a different model on the same proxy. No ledger, skill
file or Core is shared with anything outside the run directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from rka_memory import config as config_mod
from rka_memory import notepad
from rka_memory.domain import Actor, Event, Kind, State
from rka_memory.miner import backends, miner
from rka_memory.service import Memory

MANIFEST = REPO / "workload" / "e3" / "manifest.json"
DATA_DIR = REPO / "workload-runs" / "e3-data"
PLAN_DIR = Path(os.environ.get("RKM_OPENAI_PLAN_DIR") or Path.home() / ".config" / "rka-memory-dev")
PROXY = os.environ.get("RKM_OPENAI_BASE_URL") or "http://127.0.0.1:8790/v1"

FAMILIES = ("procedural_memory", "classical_conditioning", "priming")
ARMS = ("in-context", "none", "gated", "sourced", "ungated", "notepad")
LEDGER_ARMS = ("gated", "sourced", "ungated")
RULE_BASED_SEEDS = frozenset({"The Ternary Logic System", "The 'Omega' Operation", "The Modified Recurrence Sequence"})
# One project per item and context, fixed for both sessions: a record reaches
# a packet only if its scope covers the state (docs/E3.md Section 3).
TOOLCHAIN = "chat"
TASK_CLASS = "chat"
USER_CONTEXT = "default"

Complete = Callable[[list[dict[str, str]], str, str | None], tuple[str, dict[str, int]]]


# --- data -------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    role: str  # user | assistant | system
    content: str


@dataclass(frozen=True)
class Context:
    learning: tuple[Turn, ...]
    interference: tuple[Turn, ...]
    probe: str


@dataclass(frozen=True)
class Item:
    family: str
    file: str
    task: str
    task_id: str
    contexts: dict[str, Context]  # "main", or "experimental" and "control"
    expected: Any = None
    theme: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> str:
        return f"{Path(self.file).stem}-{self.task_id}"


def manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text())


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(data_dir: Path = DATA_DIR, man: dict[str, Any] | None = None) -> None:
    """Download every pinned file and check it against the manifest."""
    man = man or manifest()
    base = f"https://raw.githubusercontent.com/{man['repository']}/{man['commit']}/"
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for rel, digest in man["files"].items():
            path = data_dir / rel
            if path.exists() and sha256(path.read_bytes()) == digest:
                continue
            resp = client.get(base + rel)
            resp.raise_for_status()
            if sha256(resp.content) != digest:
                raise SystemExit(f"{rel}: hash differs from the manifest")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
    print(f"{len(man['files'])} files at {man['commit'][:8]} in {data_dir}")


def turns(phase: Sequence[dict[str, Any]] | None) -> tuple[Turn, ...]:
    """A phase as turns, roles normalized as the released script does: an
    unknown role is the assistant's; empty turns are dropped."""
    out = []
    for m in phase or ():
        if "role" in m and "content" in m:
            role, text = str(m["role"]).strip().lower(), str(m["content"]).strip()
        elif "speaker" in m and "text" in m:
            role = "user" if str(m["speaker"]).strip().lower() == "user" else "assistant"
            text = str(m["text"]).strip()
        else:
            continue
        if text:
            out.append(Turn(role if role in {"user", "assistant", "system"} else "assistant", text))
    return tuple(out)


def _probe(probe: Any) -> str:
    if isinstance(probe, str):
        return probe.strip()
    if isinstance(probe, dict):
        return str(probe.get("content") or probe.get("text") or probe.get("prompt") or "").strip()
    if isinstance(probe, list):
        users = [m for m in probe if isinstance(m, dict) and (m.get("role") == "user" or m.get("speaker") == "user")]
        return str(users[-1].get("content") or users[-1].get("text") or "").strip() if users else ""
    return ""


def load(data_dir: Path = DATA_DIR, families: Sequence[str] = FAMILIES, man: dict[str, Any] | None = None) -> list[Item]:
    man = man or manifest()
    items = []
    for rel, digest in sorted(man["files"].items()):
        parts = Path(rel).parts
        if parts[0] != "dataset" or parts[1] not in families:
            continue
        raw_bytes = (data_dir / rel).read_bytes()
        if sha256(raw_bytes) != digest:
            raise SystemExit(f"{rel}: hash differs from the manifest; run fetch")
        doc = json.loads(raw_bytes)
        family = parts[1]
        for inst in doc["instances"]:
            if family == "priming":
                contexts = {
                    name: Context(turns(inst[f"{name}_instance"].get("priming_phase")),
                                  turns(inst[f"{name}_instance"].get("interference_phase")),
                                  _probe(inst[f"{name}_instance"].get("test_probe")))
                    for name in ("experimental", "control")
                }
                theme = doc.get("source_theme") or doc.get("selected_source_theme") or inst.get("selected_source_theme") or ""
                items.append(Item(family, rel, theme, str(inst["task_id"]), contexts, theme=theme, raw=inst))
            else:
                ctx = Context(turns(inst.get("learning_phase")), turns(inst.get("interference_phase")), _probe(inst.get("test_probe")))
                items.append(Item(family, rel, doc.get("task_seed", ""), str(inst["task_id"]), {"main": ctx},
                                  expected=inst.get("expected_pattern"), raw=inst))
    return items


def pilot(items: Sequence[Item]) -> list[Item]:
    """docs/E3.md Section 8: the first conditioning and priming files and the
    first two procedural files, by file name."""
    files = {f: sorted({i.file for i in items if i.family == f}) for f in FAMILIES}
    keep = set(files["classical_conditioning"][:1] + files["priming"][:1] + files["procedural_memory"][:2])
    return [i for i in items if i.file in keep]


# --- sessions ----------------------------------------------------------------


def events_for(learning: Sequence[Turn], session_id: str) -> list[Event]:
    """docs/E3.md Section 3: a user turn is user_msg; an assistant turn
    followed by a system turn is a tool call and the system turn its env
    result; any other assistant turn is assistant_msg."""
    out = []
    for n, t in enumerate(learning):
        nxt = learning[n + 1] if n + 1 < len(learning) else None
        if t.role == "user":
            out.append(Event.new(Kind.USER_MSG, Actor.USER, session_id, {"text": t.content}))
        elif t.role == "system":
            out.append(Event.new(Kind.TOOL_RESULT, Actor.ENV, session_id, {"tool": "action", "tool_use_id": f"t{n - 1}", "output": t.content}))
        elif nxt is not None and nxt.role == "system":
            out.append(Event.new(Kind.TOOL_CALL, Actor.MODEL, session_id, {"tool": "action", "tool_use_id": f"t{n}", "input": t.content}))
        else:
            out.append(Event.new(Kind.ASSISTANT_MSG, Actor.MODEL, session_id, {"text": t.content}))
    return out


def state_for(item: Item, context: str, model: str) -> State:
    return State(task_class=TASK_CLASS, project=f"/e3/{item.key}/{context}", toolchain=TOOLCHAIN, model_id=model,
                 user_context=USER_CONTEXT)


def memory_config(ledger_dir: Path, *, model: str, base_url: str, notepad_on: bool) -> config_mod.Config:
    """The default configuration, with this item's ledger, the plan backends,
    no skill files and no Core (docs/E3.md Section 3)."""
    if os.environ.get("RKA_MEMORY_DIR") or os.environ.get("RKA_MEMORY_MINER_BASE_URL"):
        raise SystemExit("unset RKA_MEMORY_DIR and RKA_MEMORY_MINER_BASE_URL: E3 sets its own")
    backend = {"backend": "openai_compat", "base_url": base_url, "model": model, "api_key_env": "RKM_OPENAI_PROXY_KEY"}
    over = {
        "ledger": {"dir": str(ledger_dir)},
        "core": {"url": "http://127.0.0.1:9", "sync_on_session_end": False},
        "skills": {"dir": str(ledger_dir / "skills"), "export_files": False},
        "miner": backend,
        "notepad": {**backend, "assistant_turns": True} if notepad_on else {"backend": "none"},
    }
    return config_mod.load(path=config_mod.DEFAULT_PATH, overrides=over)


@dataclass
class Learned:
    """Session 1 in one ledger, and what the memory made of it."""

    memory: Memory
    state: State
    mined: dict[str, Any]
    curated: list[dict[str, Any]]


def learn(ledger_dir: Path, item: Item, context: str, *, model: str, base_url: str, notepad_on: bool,
          mine_backend: backends.Backend | None = None) -> Learned:
    cfg = memory_config(ledger_dir, model=model, base_url=base_url, notepad_on=notepad_on)
    mem = Memory.open(cfg)
    st = state_for(item, context, model)
    s1 = f"{item.key}-{context}-s1"
    mem.append(Event.new(Kind.SESSION_START, Actor.RUNNER, s1, {"state": st.to_json()}, state_hash=st.hash()))
    mem.append(*events_for(item.contexts[context].learning, s1))
    mem.append(Event.new(Kind.SESSION_END, Actor.RUNNER, s1, {}, state_hash=st.hash()))
    mem.gate(session_id=s1)
    mined: dict[str, Any] = {}
    curated: list[dict[str, Any]] = []
    if notepad_on:
        curated = [step.to_json() for step in notepad.after_session(mem, s1, mine_backend)]
    else:
        mined = miner.mine_session(mem, s1, backend=mine_backend).to_json()
        mem.gate(session_id=s1)
    return Learned(mem, st, mined, curated)


def packet_for(learned: Learned, item: Item, context: str, arm: str) -> tuple[str, list[str]]:
    s2 = f"{item.key}-{context}-s2-{arm}"
    learned.memory.append(Event.new(Kind.SESSION_START, Actor.RUNNER, s2, {"state": learned.state.to_json()},
                                    state_hash=learned.state.hash()))
    view = learned.memory.deliver(s2, learned.state, channel="session_start", arm=arm)
    return view.render(), list(view.record_ids)


def records_of(learned: Learned) -> list[dict[str, Any]]:
    world = learned.memory.world()
    labels = learned.memory.labels(world)
    out = []
    for rec in world.records.values():
        lab = labels.by_id.get(rec.id)
        cited = [world.events[link.event_id].kind.value for link in rec.links
                 if getattr(link, "event_id", None) in world.events]
        out.append({
            "record_id": rec.id,
            "kind": rec.spec.kind.value,
            "content": rec.spec.content,
            "rung": labels.rung(rec.id).value,
            "why": lab.reason if lab else "",
            "affirmed": bool(lab and lab.affirmed),
            "support": list(lab.support) if lab else [],
            "cited_kinds": cited,
        })
    return out


# --- model calls ---------------------------------------------------------------


@dataclass
class Plan:
    """The plan proxy, as an OpenAI-compatible chat endpoint."""

    base_url: str = PROXY
    key: str = ""
    attempts: int = 6

    def complete(self, messages: list[dict[str, str]], model: str, effort: str | None) -> tuple[str, dict[str, int]]:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if effort and effort != "off":
            body["reasoning_effort"] = effort
        delay = 5.0
        for attempt in range(1, self.attempts + 1):
            try:
                with httpx.Client(timeout=300) as client:
                    resp = client.post(self.base_url.rstrip("/") + "/chat/completions", json=body,
                                       headers={"Authorization": f"Bearer {self.key}"})
            except httpx.HTTPError:
                if attempt == self.attempts:
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            # A rate limit passes; a used-up plan (subscription_sharing_*) does not.
            limited = resp.status_code == 429 and "Rate limit exceeded" in resp.text
            if (resp.status_code in (500, 502, 503, 504) or limited) and attempt < self.attempts:
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"plan proxy answered HTTP {resp.status_code}: {resp.text[:300]}")
            doc = resp.json()
            text = doc["choices"][0]["message"].get("content") or ""
            u = doc.get("usage") or {}
            return text.strip(), {"input": int(u.get("prompt_tokens") or 0), "output": int(u.get("completion_tokens") or 0)}
        raise RuntimeError("unreachable")


def plan_key() -> str:
    key = os.environ.get("RKM_OPENAI_PROXY_KEY")
    if key:
        return key
    path = PLAN_DIR / "proxy-key"
    if not path.exists():
        raise SystemExit(f"no proxy key at {path}; start scripts/openai_plan.py serve")
    return path.read_text().strip()


# --- judge (the released evaluation.py's prompts) ------------------------------


def judge_prompt(item: Item, answer: str) -> str:
    learning = "\n".join(f"{t['role']}: {t['content']}" for t in item.raw.get("learning_phase", []))
    probe = item.raw.get("test_probe", {})
    probe_str = f"{probe.get('role', 'user')}: {probe.get('content', '')}" if isinstance(probe, dict) else f"user: {probe}"
    if item.family == "classical_conditioning":
        role = (
            "You are an expert in cognitive science and AI evaluation. Your mission is to strictly determine if the agent's "
            "behavior in the `TEST_PROBE` demonstrates a learned 'avoidance' or 'adaptive' response, based on the "
            "cause-and-effect relationship established during the `LEARNING_HISTORY`."
        )
        steps = [
            {"step": 1, "action": "Identify the Conditioned Association.",
             "instruction": "Based *only* on the LEARNING_HISTORY, identify the specific association the agent was taught. "
                            "Clearly state: What is the Neutral Cue (CS) and what is the consistent Negative Outcome (US)? "
                            "Example: 'CS = using the keyword 'detailed analysis'; US = Timeout Error from analyzer_v1.'"},
            {"step": 2, "action": "Analyze the Test Situation.",
             "instruction": "Examine the TEST_PROBE. Does the user's final prompt contain the exact Neutral Cue (CS) identified in Step 1? "
                            "Also, what alternative, safer actions or tools were made available to the agent in the test context?"},
            {"step": 3, "action": "Evaluate the Agent's Behavior.",
             "instruction": "Critically analyze the MODEL_ANSWER. Did the agent repeat the action that previously led to the Negative Outcome (US)? "
                            "Or did it exhibit adaptive behavior? Adaptive behavior includes: (1) Choosing the alternative safe action. "
                            "(2) Warning the user about the potential negative outcome. (3) Asking for clarification to avoid the negative outcome."},
            {"step": 4, "action": "Final Verdict.",
             "instruction": "Return 'Correct' ONLY if the agent demonstrated clear adaptive or avoidance behavior as described in Step 3. "
                            "If the agent mechanically repeated the error-prone action, return 'Incorrect'. There is no middle ground."},
        ]
    else:
        role = (
            "You are an impartial and meticulous AI Benchmark Evaluator. Your mission is to strictly determine if the `MODEL_ANSWER` "
            "correctly applies the skill demonstrated in the `LEARNING_HISTORY` to solve the `TEST_PROBE`."
        )
        steps = [
            {"step": 1, "action": "Identify the Skill.", "instruction": "Based only on the LEARNING_HISTORY, extract the single specific non-intuitive procedure or rule taught."},
            {"step": 2, "action": "Analyze the Test Demand.", "instruction": "What exactly does TEST_PROBE ask to perform?"},
            {"step": 3, "action": "Evaluate the Model's Answer.", "instruction": "Verify the answer matches/contains expected_pattern if provided and that the learned procedure was applied exactly."},
            {"step": 4, "action": "Final Verdict.", "instruction": "Return 'Correct' only if the learned skill was perfectly applied; else 'Incorrect'."},
        ]
    data = {
        "evaluation_task": {
            "role": role,
            "output_format": "You MUST respond with a JSON object: `{\"reasoning\": \"<Your step-by-step logical deduction>\", \"decision\": \"<'Correct' or 'Incorrect'>\"}`. Your reasoning must be concise and directly justify your decision.",
        },
        "evaluation_context": {
            "task_name": item.raw.get("task_name", "N/A"),
            "task_category": item.family,
            "learning_history": learning,
            "test_probe": probe_str,
            "model_answer_to_evaluate": answer,
            "expected_pattern": item.expected if item.expected is not None else "N/A",
        },
        "thinking_process_guideline": {"title": "STRICT EVALUATION PROTOCOL - FOLLOW THESE STEPS:", "steps": steps},
    }
    return json.dumps(data, indent=2)


def _flatten(expected: Any) -> list[str]:
    if expected is None:
        return []
    if isinstance(expected, str):
        return [expected]
    if isinstance(expected, dict):
        return [s for v in expected.values() for s in _flatten(v)]
    if isinstance(expected, list):
        return [s for v in expected for s in _flatten(v)]
    return [str(expected)]


def contains_expected(expected: Any, answer: str) -> bool:
    out = (answer or "").strip().lower()
    return any(c and c in out for c in ((str(x) or "").strip().strip('"').lower() for x in _flatten(expected)))


def json_object(text: str) -> dict[str, Any]:
    """The JSON object in a judge's reply; the plan route ignores
    response_format, so the reply may wrap it in prose or a code fence."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        doc = json.loads(m.group(0))
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def verdict(item: Item, answer: str, complete: Complete, judge_model: str) -> dict[str, Any]:
    if item.family == "procedural_memory" and item.task in RULE_BASED_SEEDS:
        ok = bool(item.expected) and contains_expected(item.expected, answer)
        return {"correct": ok, "by": "substring", "reason": "contains expected pattern" if ok else "does not contain expected pattern"}
    text, usage = complete([{"role": "user", "content": judge_prompt(item, answer)}], judge_model, None)
    doc = json_object(text)
    decision = str(doc.get("decision", "Incorrect")).strip().lower()
    return {"correct": decision == "correct", "by": "judge", "reason": str(doc.get("reasoning", ""))[:600], "usage": usage,
            "parsed": bool(doc)}


def theme_map(data_dir: Path = DATA_DIR) -> dict[str, dict[str, Any]]:
    doc = json.loads((data_dir / "tasks" / "priming.json").read_text())
    out = {}
    for src in doc.get("sources", []):
        c = src.get("core_concepts_and_imagery", {})
        out[src.get("theme_name", "")] = {"name": src.get("theme_name", ""), "setting": c.get("setting", ""),
                                          "motifs": c.get("motifs", ""), "dynamics": c.get("dynamics", ""),
                                          "affect": c.get("affect", "")}
    return out


def priming_judge(theme: dict[str, Any], probe: str, exp_out: str, base_out: str, exp_text: str, base_text: str,
                  complete: Complete, judge_model: str) -> dict[str, Any]:
    system = "You are PrimingJudge v5. Output STRICT JSON with fields 'priming_influence_score' (0–100) and 'reasoning' (<=50 words)."
    user = (
        f"[THEME_NAME]\n{theme.get('name','')}\n\n"
        f"[CORE_CONCEPTS_AND_IMAGERY]\n"
        f"setting: {theme.get('setting','')}\n"
        f"motifs: {theme.get('motifs','')}\n"
        f"dynamics: {theme.get('dynamics','')}\n"
        f"affect: {theme.get('affect','')}\n\n"
        f"[EXPERIMENTAL_PRIMING_CONTEXT]\n{exp_text}\n\n"
        f"[BASELINE_PRIMING_CONTEXT]\n{base_text}\n\n"
        f"[PROBE_PROMPT]\n{probe}\n\n"
        f"[OUTPUTS TO COMPARE]\n"
        f"experimental_output (first response verbatim):\n{exp_out}\n\n"
        f"baseline_output (first response verbatim):\n{base_out}\n\n"
        f"[TASK]\n"
        f"Estimate how much EXPERIMENTAL_PRIMING_CONTEXT influenced the experimental_output RELATIVE to baseline_output (0–100).\n"
        f"Score magnitude of priming influence only. Use a conservative baseline: assume 0 unless strong, attributable evidence exists.\n\n"
        f"[EVIDENCE REQUIREMENTS]\n"
        f"- Identify 1–2 specific elements from EXPERIMENTAL_PRIMING_CONTEXT (lexicon, imagery, affect, dynamics, structural choices) that appear in experimental_output but NOT in baseline_output.\n"
        f"- Prefer hard lexical echoes or close paraphrases (e.g., 'sonar pings' -> 'echo', 'bioluminescent pulses' -> 'glow in darkness').\n"
        f"- Generic metaphors (transformation/growth/craft/adaptation: chrysalis, garden, tapestry, sculptor, alchemy, chameleon, agility) are LOW EVIDENCE unless clearly anchored to theme specifics.\n"
        f"- If similar cues appear in BASELINE_PRIMING_CONTEXT or baseline_output, discount or remove that evidence.\n\n"
        f"[STRICT SCORING POLICY]\n"
        f"HARD CAPS:\n"
        f"- If NO lexical echo/close paraphrase traceable to EXPERIMENTAL_PRIMING_CONTEXT: cap at 20.\n"
        f"- If < 2 distinct theme axes matched (from setting/motifs/dynamics/affect): cap at 40.\n"
        f"BANDS (use MIDPOINT by default; if uncertain, choose lower bound):\n"
        f"- 0–5 None: indistinguishable; no attributable influence.\n"
        f"- 6–12 Trace: single weak cue; tenuous.\n"
        f"- 13–20 Weak: minor differences; mostly generic or ambiguous.\n"
        f"- 25–40 Moderate: ≥2 cues plausibly sourced from experimental priming with partial uniqueness.\n"
        f"- 45–60 Strong: clear multi-facet influence (≥2 distinct axes) unique to experimental.\n"
        f"- 61–80 Very Strong: rich, cohesive influence across imagery/affect/dynamics with clear uniqueness.\n"
        f"- 81–95 Exceptional: unmistakable, multi-axial, specific alignment strongly and uniquely traced to priming. Rare.\n"
        f"PENALTIES:\n"
        f"- If any cited cue overlaps with baseline_output or BASELINE_PRIMING_CONTEXT, demote by 5–10 points.\n"
        f"- If all cues are generic (unanchored), set to 8–15.\n"
        f"FINALIZE: Round DOWN to nearest 5; clamp [0,100]. Favor lower scores unless evidence is compelling.\n\n"
        f"[OUTPUT FORMAT]\nReturn STRICT JSON only with keys: priming_influence_score (int), reasoning (string). Reasoning <= 50 words citing the decisive evidence succinctly."
    )
    text, usage = complete([{"role": "system", "content": system}, {"role": "user", "content": user}], judge_model, None)
    doc = json_object(text)
    try:
        score = max(0, min(100, int(doc.get("priming_influence_score", 0))))
    except (TypeError, ValueError):
        score = 0
    return {"score": score, "reason": str(doc.get("reasoning", ""))[:400], "usage": usage, "parsed": bool(doc)}


# --- one item ----------------------------------------------------------------------


def _messages(turns_: Sequence[Turn]) -> list[dict[str, str]]:
    return [{"role": t.role, "content": t.content} for t in turns_]


def session2_messages(ctx: Context, packet: str) -> list[dict[str, str]]:
    head = [{"role": "system", "content": packet}] if packet else []
    return head + _messages(ctx.interference) + [{"role": "user", "content": ctx.probe}]


def in_context_messages(ctx: Context) -> list[dict[str, str]]:
    return _messages(ctx.learning) + _messages(ctx.interference) + [{"role": "user", "content": ctx.probe}]


@dataclass
class Run:
    out: Path
    agent_model: str
    effort: str
    judge_model: str
    base_url: str
    complete: Complete
    mine_backend: backends.Backend | None = None  # tests: a scripted miner and curator
    themes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def item_path(self, item: Item) -> Path:
        return self.out / "items" / item.family / f"{item.key}.json"

    def answers(self, item: Item, context: str) -> dict[str, Any]:
        """Every arm's packet and answer for one context. An empty packet
        leaves the input equal to the none arm's, so that answer is reused
        (docs/E3.md Section 11)."""
        ctx = item.contexts[context]
        base = self.out / "ledgers" / item.family / item.key / context
        # An item without a result file never finished: a ledger it left is
        # partial, and appending session 1 again would repeat its events.
        shutil.rmtree(base, ignore_errors=True)
        usage: dict[str, Any] = {}
        arms: dict[str, Any] = {}

        def ask(messages: list[dict[str, str]], arm: str) -> str:
            text, u = self.complete(messages, self.agent_model, self.effort)
            usage[f"agent:{arm}"] = u
            return text

        arms["in-context"] = {"packet": None, "answer": ask(in_context_messages(ctx), "in-context")}
        none_answer = ask(session2_messages(ctx, ""), "none")
        arms["none"] = {"packet": "", "answer": none_answer}
        shared = learn(base / "rka", item, context, model=self.agent_model, base_url=self.base_url, notepad_on=False,
                       mine_backend=self.mine_backend)
        pad = learn(base / "notepad", item, context, model=self.agent_model, base_url=self.base_url, notepad_on=True,
                    mine_backend=self.mine_backend)
        for arm in (*LEDGER_ARMS, "notepad"):
            learned = pad if arm == "notepad" else shared
            text, ids = packet_for(learned, item, context, arm)
            answer = ask(session2_messages(ctx, text), arm) if text else none_answer
            arms[arm] = {"packet": text, "record_ids": ids, "answer": answer, "reused_none": not text}
        return {"arms": arms, "records": records_of(shared), "mined": shared.mined, "curated": pad.curated,
                "notes": notepad.render(pad.memory.cfg), "usage": usage}

    def run_item(self, item: Item) -> dict[str, Any]:
        path = self.item_path(item)
        if path.exists():
            return json.loads(path.read_text())
        doc: dict[str, Any] = {"family": item.family, "file": item.file, "task": item.task, "task_id": item.task_id,
                               "key": item.key, "agent_model": self.agent_model, "effort": self.effort,
                               "judge_model": self.judge_model}
        if item.family == "priming":
            exp, ctl = self.answers(item, "experimental"), self.answers(item, "control")
            theme = self.themes.get(item.theme, {"name": item.theme})
            theme = {**theme, "name": theme.get("name") or item.theme}
            exp_text = "\n".join(f"{t.role}: {t.content}" for t in item.contexts["experimental"].learning)
            ctl_text = "\n".join(f"{t.role}: {t.content}" for t in item.contexts["control"].learning)
            scores = {}
            for arm in ARMS:
                scores[arm] = priming_judge(theme, item.contexts["experimental"].probe, exp["arms"][arm]["answer"],
                                            ctl["arms"][arm]["answer"], exp_text, ctl_text, self.complete, self.judge_model)
            doc.update({"contexts": {"experimental": exp, "control": ctl}, "scores": scores})
        else:
            got = self.answers(item, "main")
            doc.update({"contexts": {"main": got}, "verdicts": {arm: verdict(item, got["arms"][arm]["answer"], self.complete, self.judge_model) for arm in ARMS}})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
        return doc


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["fetch", "run", "list"])
    ap.add_argument("--data", type=Path, default=DATA_DIR)
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--families", default=",".join(FAMILIES))
    ap.add_argument("--seeds", default="", help="only these task seeds, as in item keys (a,b)")
    ap.add_argument("--limit", type=int, default=0, help="at most N items (after --pilot)")
    ap.add_argument("--model", default=os.environ.get("RKM_OPENAI_MODEL") or "gpt-6.2-sol")
    ap.add_argument("--reasoning", default=os.environ.get("RKM_OPENAI_REASONING") or "medium")
    ap.add_argument("--judge", default="gpt-6.2-astra")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)
    if args.command == "fetch":
        fetch(args.data)
        return 0
    families = [f for f in args.families.split(",") if f]
    unknown = set(families) - set(FAMILIES)
    if unknown:
        ap.error(f"unknown families {sorted(unknown)}")
    items = load(args.data, families)
    if args.pilot:
        items = pilot(items)
    if args.seeds:
        seeds = set(args.seeds.split(","))
        items = [i for i in items if i.key.rsplit("-", 1)[0] in seeds]
    if args.limit:
        items = items[: args.limit]
    if args.command == "list":
        for i in items:
            print(i.family, i.key, i.task)
        print(f"{len(items)} items")
        return 0
    out = args.out or REPO / "workload-runs" / f"e3-{args.model}-{args.reasoning}-judge-{args.judge}" / ("pilot" if args.pilot else "full")
    out.mkdir(parents=True, exist_ok=True)
    meta = {"model": args.model, "reasoning": args.reasoning, "judge": args.judge, "manifest_commit": manifest()["commit"]}
    meta_path = out / "run.json"
    if meta_path.exists() and json.loads(meta_path.read_text()) != meta:
        raise SystemExit(f"{out} holds a run with other settings: {meta_path.read_text()}")
    meta_path.write_text(json.dumps(meta, indent=1))
    os.environ["RKM_OPENAI_PROXY_KEY"] = plan_key()
    plan = Plan(key=os.environ["RKM_OPENAI_PROXY_KEY"])
    run = Run(out, args.model, args.reasoning, args.judge, PROXY, plan.complete, themes=theme_map(args.data))
    done = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run.run_item, i): i for i in items}
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                fut.result()
                done += 1
            except Exception as exc:  # noqa: BLE001 - one item's failure must not stop the others
                failed += 1
                (out / "errors").mkdir(exist_ok=True)
                (out / "errors" / f"{item.key}.txt").write_text("".join(traceback.format_exception(exc)))
                print(f"failed {item.family} {item.key}: {exc}", file=sys.stderr, flush=True)
            if (done + failed) % 10 == 0:
                print(f"{done + failed}/{len(items)} items ({failed} failed)", flush=True)
    print(f"{done} of {len(items)} items done, {failed} failed; results in {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
