"""One YAML config, parsed once at the boundary into a typed Config."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import yaml

from rka_memory.domain import QUOTE_CHARS, SCOPE_FIELDS, Kind

DEFAULT_PATH = Path(__file__).resolve().parent / "default.yaml"


def user_config_path() -> Path:
    return Path(os.environ.get("RKA_MEMORY_CONFIG", "~/.rka-memory/config.yaml")).expanduser()


@dataclass(frozen=True)
class GateConfig:
    k: float = 2.0
    min_sessions: int = 2
    weak_only_min_sessions: int = 3
    skill_min_sessions: int = 2
    retire_after_days: float = 60
    standing_phrases: tuple[str, ...] = ()
    # attested (DESIGN_V3): only the user's words, the user's check, a typed
    # verdict or a trial validate. threshold: v2's labelling, kept to compare.
    validation: str = "attested"


@dataclass(frozen=True)
class TrialConfig:
    """Randomized trials for prescriptive records (DESIGN_V2 5.3), registered
    before any run: the chance a candidate is delivered, the sessions each arm
    needs, and the level of the one-sided Fisher test that decides it."""

    q: float = 0.5
    m: int = 3
    alpha: float = 0.05
    # Sequential trials under attested validation (DESIGN_V3 step 3).
    e_threshold: float = 20.0  # promote at this wealth: 1/0.05, at most 5% null promotion at any horizon
    min_per_arm: int = 2  # sessions each arm needs before a promotion
    hold_below: float = 0.1  # on hold while P(treated better) is at most this
    q_hold: float = 0.1  # the chance of delivery on hold
    cluster_similarity: float = 0.5  # token Jaccard at which two records on trial share a coin
    # How a hypothesis enters a trial under attested validation: "threshold"
    # once its model-made support would have met v2's threshold (DESIGN_V3
    # 3.4), "open" as soon as it is proposed, when an outcome check is
    # configured (E2b's R2, docs/E2B.md).
    entry: str = "threshold"


@dataclass(frozen=True)
class IsolationConfig:
    """Rule 2's isolation re-run (RKA jrn_01M7H11MZ72N1FEQ01P1D5JQ2Y). It runs
    project code the agent wrote, so it runs only when both an interpreter
    and a confinement command are set: a command that runs `<grants> --
    <argv>` (dsh's sandbox-runner syntax, `--bind DIR DIR` making DIR
    writable) with no more rights than the agent's own tools, as the stack's
    confine.sh does. Also: the user the run directory is handed to, the
    directory run directories go in (outside anything the agent can write),
    the test framework ("auto": pytest if the interpreter has it, else
    unittest), parallel processes, and the time the whole re-run may take
    before it fails closed."""

    python: str | None = None
    confine: tuple[str, ...] = ()
    user: str | None = None
    dir: str | None = None
    framework: str = "auto"
    workers: int = 4
    budget_seconds: float = 20.0


@dataclass(frozen=True)
class OutcomeConfig:
    """The user's own check of a session's work (DESIGN_V2 5.3): a command
    the runner runs at session end in a copy of the project, through the
    isolation re-run's confinement, within `budget_seconds`. `{python}` in it
    stands for the isolation interpreter. None: no outcome check."""

    command: tuple[str, ...] | None = None
    budget_seconds: float = 60.0
    # The messages the user wrote into the check for the agent to learn from
    # (DESIGN_V3 3.1): only these, whole, may attest a record. The check runs
    # the agent's code, so nothing else it prints is the user's.
    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotepadConfig:
    """The model that keeps the `notepad` arm's notes, E2's model-curated
    baseline (docs/E2.md Section 3). Same keys as `miner`; `none` keeps no
    notepad. The notes are consolidated after every `consolidate_every`
    sessions the curator has kept."""

    backend: str = "none"
    model: str | None = None
    base_url: str = ""
    api_key_env: str | None = None
    timeout_seconds: int = 240
    consolidate_every: int = 5
    # E3 (docs/E3.md Section 4): the curator also reads what the agent said,
    # as outside memory systems do. Off keeps E2's curator.
    assistant_turns: bool = False


@dataclass(frozen=True)
class Config:
    ledger_dir: Path
    core_url: str
    core_project_id: str | None
    sync_on_session_end: bool
    gate: GateConfig
    weights: dict[Kind, float]
    budget_tokens: int
    arms: tuple[str, ...]
    scope_fields: tuple[str, ...]
    task_classes: tuple[str, ...]
    user_context: str
    repeat_threshold: float
    repeat_lookback_sessions: int
    dedup_threshold: float
    test_commands: tuple[str, ...]
    payload_max_chars: int
    miner_backend: str
    miner_model: str | None
    miner_timeout_seconds: int
    miner_events_max_chars: int
    miner_base_url: str
    miner_api_key_env: str | None
    miner_extract_rules: bool
    graph_anchors: bool
    hooks_background: str
    hooks_guard: bool
    skills_dir: Path
    skills_export_files: bool
    skills_codex_dir: Path | None
    trials: TrialConfig = field(default_factory=TrialConfig)
    # Test files: changing or removing existing content in one makes a
    # session's test results weak; adding tests does not (8.1, rule 2).
    check_test_globs: tuple[str, ...] = ()
    # Test configuration: any change makes a session's test results weak.
    check_config_globs: tuple[str, ...] = ()
    isolation: IsolationConfig = field(default_factory=IsolationConfig)
    outcome: OutcomeConfig = field(default_factory=OutcomeConfig)
    notepad: NotepadConfig = field(default_factory=NotepadConfig)
    # Characters of each event's payload a miner prompt line shows (E2b's
    # R1, docs/E2B.md: 2,000 lets the miner read what the agent read).
    miner_payload_chars: int = 240
    # E2c's R3 (docs/E2C.md): the miner also lists the requirements project
    # files state that nobody has confirmed, as file claims.
    miner_file_claims: bool = False
    # E2c's R4: file claims `rka-memory review` lists at a time; 0 is off.
    review_per_session: int = 0
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def weight(self, kind: Kind) -> float:
        return self.weights.get(kind, 0.0)

    @property
    def check_globs(self) -> tuple[str, ...]:
        """Every path whose change the modification check looks at."""
        return (*self.check_config_globs, *self.check_test_globs)

    @property
    def open_scope_fields(self) -> frozenset[str]:
        return frozenset(SCOPE_FIELDS) - frozenset(self.scope_fields)


def merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


# gated: valid records; stated: valid records the user affirmed (a confirm, or
# a standing statement of the record's words); ungated: every L1-L3 record in
# scope; sourced: the ungated records under a neutral header, each line ending
# with where its support came from; none: no memory; notepad: no records, the
# notes a model keeps instead (E2's baseline, docs/E2.md Section 3).
ARMS = frozenset({"gated", "none", "notepad", "sourced", "stated", "ungated"})
BACKGROUND = frozenset({"spawn", "inline", "off"})


def _choice(value: Any, allowed: frozenset[str], key: str) -> str:
    if value not in allowed:
        raise ValueError(f"{key} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _flag(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false, got {value!r}")
    return value


def _number(value: Any) -> float:
    """Accept 1, 0.5 or an exact fraction such as "1/3"."""
    return float(Fraction(str(value)))


def load(path: Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    user_path = path or user_config_path()
    if user_path.exists():
        raw = merge(raw, yaml.safe_load(user_path.read_text()) or {})
    if overrides:
        raw = merge(raw, overrides)
    return from_dict(raw)


def _isolation(raw: dict[str, Any]) -> IsolationConfig:
    framework = str(raw.get("framework", IsolationConfig.framework))
    if framework not in ("auto", "pytest", "unittest"):
        raise ValueError(f"checks.isolation.framework must be auto, pytest or unittest, not {framework!r}")
    python = raw.get("python")
    return IsolationConfig(
        python=str(python) if python else None,
        confine=tuple(str(a) for a in raw.get("confine") or ()),
        user=str(raw["user"]) if raw.get("user") else None,
        dir=str(raw["dir"]) if raw.get("dir") else None,
        framework=framework,
        workers=max(1, int(raw.get("workers", IsolationConfig.workers))),
        budget_seconds=_number(raw.get("budget_seconds", IsolationConfig.budget_seconds)),
    )


def _outcome(raw: dict[str, Any]) -> OutcomeConfig:
    command = raw.get("command")
    if command is not None and (not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command)):
        raise ValueError("checks.outcome.command must be a non-empty list of strings")
    messages = raw.get("messages") or []
    if not isinstance(messages, list) or not all(isinstance(m, str) for m in messages):
        raise ValueError("checks.outcome.messages must be a list of strings")
    for m in messages:
        if not QUOTE_CHARS[0] <= len(" ".join(m.split())) <= QUOTE_CHARS[1]:
            raise ValueError(f"checks.outcome.messages: each must be {QUOTE_CHARS[0]} to {QUOTE_CHARS[1]} characters, got {m!r}")
    return OutcomeConfig(
        command=tuple(command) if command else None,
        budget_seconds=_number(raw.get("budget_seconds", OutcomeConfig.budget_seconds)),
        messages=tuple(messages),
    )


def _notepad(raw: dict[str, Any]) -> NotepadConfig:
    every = int(raw.get("consolidate_every", NotepadConfig.consolidate_every))
    if every < 1:
        raise ValueError("notepad.consolidate_every must be at least 1")
    return NotepadConfig(
        backend=str(raw.get("backend", NotepadConfig.backend)),
        model=raw.get("model") or None,
        base_url=str(raw.get("base_url", "")),
        api_key_env=raw.get("api_key_env"),
        timeout_seconds=int(raw.get("timeout_seconds", NotepadConfig.timeout_seconds)),
        consolidate_every=every,
        assistant_turns=_flag(raw.get("assistant_turns", False), "notepad.assistant_turns"),
    )


def _trials(raw: dict[str, Any]) -> TrialConfig:
    t = TrialConfig(
        q=float(raw.get("q", TrialConfig.q)),
        m=int(raw.get("m", TrialConfig.m)),
        alpha=_number(raw.get("alpha", TrialConfig.alpha)),
        e_threshold=_number(raw.get("e_threshold", TrialConfig.e_threshold)),
        min_per_arm=int(raw.get("min_per_arm", TrialConfig.min_per_arm)),
        hold_below=_number(raw.get("hold_below", TrialConfig.hold_below)),
        q_hold=_number(raw.get("q_hold", TrialConfig.q_hold)),
        cluster_similarity=_number(raw.get("cluster_similarity", TrialConfig.cluster_similarity)),
        entry=str(raw.get("entry", TrialConfig.entry)),
    )
    if t.entry not in ("threshold", "open"):
        raise ValueError(f"trials.entry must be threshold or open, not {t.entry!r}")
    if not (0 < t.q < 1 and 0 < t.q_hold < 1 and t.e_threshold > 1):
        raise ValueError("trials: q and q_hold must lie strictly between 0 and 1, and e_threshold above 1")
    return t


def _per_session(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"review.per_session must be a whole number from 0, got {value!r}")
    return value


VALIDATIONS = frozenset({"attested", "threshold"})


def _validation(value: Any) -> str:
    if value not in VALIDATIONS:
        raise ValueError(f"gate.validation must be one of {sorted(VALIDATIONS)}, got {value!r}")
    return str(value)


def from_dict(raw: dict[str, Any]) -> Config:
    gate_raw = raw.get("gate", {})
    scope_fields = tuple(raw["scope"].get("fields", SCOPE_FIELDS))
    unknown = set(scope_fields) - set(SCOPE_FIELDS)
    if unknown:
        raise ValueError(f"scope.fields has unknown fields: {sorted(unknown)}")
    arms = tuple(raw["packet"]["arms"])
    if not arms or set(arms) - ARMS:
        raise ValueError(f"packet.arms must be a non-empty subset of {sorted(ARMS)}")
    return Config(
        ledger_dir=Path(os.environ.get("RKA_MEMORY_DIR", raw["ledger"]["dir"])).expanduser(),
        core_url=os.environ.get("RKA_MEMORY_CORE_URL", raw["core"]["url"]).rstrip("/"),
        core_project_id=raw["core"].get("project_id"),
        sync_on_session_end=bool(raw["core"].get("sync_on_session_end", True)),
        gate=GateConfig(
            k=float(gate_raw.get("k", 2.0)),
            min_sessions=int(gate_raw.get("min_sessions", 2)),
            weak_only_min_sessions=int(gate_raw.get("weak_only_min_sessions", 3)),
            skill_min_sessions=int(gate_raw.get("skill_min_sessions", 2)),
            retire_after_days=float(gate_raw.get("retire_after_days", 60)),
            standing_phrases=tuple(str(p) for p in gate_raw.get("standing_phrases") or ()),
            validation=_validation(gate_raw.get("validation", "attested")),
        ),
        weights={Kind(k): _number(v) for k, v in raw.get("weights", {}).items()},
        budget_tokens=int(raw["packet"]["budget_tokens"]),
        arms=arms,
        scope_fields=scope_fields,
        task_classes=tuple(raw["scope"]["task_classes"]),
        user_context=os.environ.get("RKA_MEMORY_CONTEXT", raw["scope"].get("user_context", "default")),
        repeat_threshold=float(raw["signals"]["repeat_threshold"]),
        repeat_lookback_sessions=int(raw["signals"].get("repeat_lookback_sessions", 50)),
        dedup_threshold=float(raw["signals"]["dedup_threshold"]),
        test_commands=tuple(raw["signals"]["test_commands"]),
        payload_max_chars=int(raw["signals"].get("payload_max_chars", 2000)),
        miner_backend=raw["miner"]["backend"],
        miner_model=raw["miner"].get("model") or None,
        miner_timeout_seconds=int(raw["miner"].get("timeout_seconds", 240)),
        miner_events_max_chars=int(raw["miner"].get("events_max_chars", 200_000)),
        miner_base_url=os.environ.get("RKA_MEMORY_MINER_BASE_URL", raw["miner"].get("base_url", "")),
        miner_api_key_env=raw["miner"].get("api_key_env"),
        miner_extract_rules=_flag(raw["miner"].get("extract_rules", True), "miner.extract_rules"),
        graph_anchors=_flag(raw.get("graph", {}).get("anchors", True), "graph.anchors"),
        hooks_background=_choice(raw.get("hooks", {}).get("background", "spawn"), BACKGROUND, "hooks.background"),
        hooks_guard=_flag(raw.get("hooks", {}).get("guard", True), "hooks.guard"),
        skills_dir=Path(raw["skills"]["dir"]).expanduser(),
        skills_export_files=_flag(raw["skills"].get("export_files", True), "skills.export_files"),
        skills_codex_dir=Path(raw["skills"]["codex_dir"]).expanduser() if raw["skills"].get("codex_dir") else None,
        trials=_trials(raw.get("trials", {})),
        check_test_globs=tuple(str(g) for g in raw.get("checks", {}).get("test_globs") or ()),
        check_config_globs=tuple(str(g) for g in raw.get("checks", {}).get("config_globs") or ()),
        isolation=_isolation(raw.get("checks", {}).get("isolation") or {}),
        outcome=_outcome(raw.get("checks", {}).get("outcome") or {}),
        notepad=_notepad(raw.get("notepad") or {}),
        miner_payload_chars=int(raw["miner"].get("payload_chars", 240)),
        miner_file_claims=_flag(raw["miner"].get("file_claims", False), "miner.file_claims"),
        review_per_session=_per_session((raw.get("review") or {}).get("per_session", 0)),
        raw=raw,
    )
