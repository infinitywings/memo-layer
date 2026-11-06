"""One YAML config, parsed once at the boundary into a typed Config."""

from __future__ import annotations

import os
from fractions import Fraction
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from rka_memory.domain import SCOPE_FIELDS, Kind

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
    confirm_overrides: bool = True
    narrow_priority: tuple[str, ...] = ("user_context", "task_class", "toolchain", "model_id", "project")


@dataclass(frozen=True)
class Config:
    ledger_dir: Path
    core_url: str
    core_project_id: str | None
    sync_on_session_end: bool
    gate: GateConfig
    weights: dict[Kind, float]
    budget_tokens: int
    hints_budget_tokens: int
    arms: tuple[str, ...]
    task_classes: tuple[str, ...]
    user_context: str
    repeat_threshold: float
    repeat_lookback_sessions: int
    dedup_threshold: float
    test_commands: tuple[str, ...]
    payload_max_chars: int
    miner_backend: str
    miner_model: str
    miner_timeout_seconds: int
    miner_base_url: str
    miner_api_key_env: str | None
    hooks_background: str
    skills_dir: Path
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def weight(self, kind: Kind) -> float:
        return self.weights.get(kind, 0.0)


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


ARMS = frozenset({"gated", "none", "ungated"})
BACKGROUND = frozenset({"spawn", "inline", "off"})


def _choice(value: Any, allowed: frozenset[str], key: str) -> str:
    if value not in allowed:
        raise ValueError(f"{key} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _number(value: Any) -> float:
    """Accept 1, 0.5 or an exact fraction such as "1/3"."""
    return float(Fraction(str(value)))


def load(path: Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    user_path = path or user_config_path()
    if user_path.exists():
        raw = _merge(raw, yaml.safe_load(user_path.read_text()) or {})
    if overrides:
        raw = _merge(raw, overrides)
    return from_dict(raw)


def from_dict(raw: dict[str, Any]) -> Config:
    gate_raw = raw.get("gate", {})
    narrow = tuple(gate_raw.get("narrow_priority", GateConfig.narrow_priority))
    unknown = set(narrow) - set(SCOPE_FIELDS)
    if unknown:
        raise ValueError(f"gate.narrow_priority has unknown fields: {sorted(unknown)}")
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
            confirm_overrides=bool(gate_raw.get("confirm_overrides", True)),
            narrow_priority=narrow,
        ),
        weights={Kind(k): _number(v) for k, v in raw.get("weights", {}).items()},
        budget_tokens=int(raw["packet"]["budget_tokens"]),
        hints_budget_tokens=int(raw["packet"].get("hints_budget_tokens", 600)),
        arms=arms,
        task_classes=tuple(raw["scope"]["task_classes"]),
        user_context=os.environ.get("RKA_MEMORY_CONTEXT", raw["scope"].get("user_context", "default")),
        repeat_threshold=float(raw["signals"]["repeat_threshold"]),
        repeat_lookback_sessions=int(raw["signals"].get("repeat_lookback_sessions", 50)),
        dedup_threshold=float(raw["signals"]["dedup_threshold"]),
        test_commands=tuple(raw["signals"]["test_commands"]),
        payload_max_chars=int(raw["signals"].get("payload_max_chars", 2000)),
        miner_backend=raw["miner"]["backend"],
        miner_model=raw["miner"]["model"],
        miner_timeout_seconds=int(raw["miner"].get("timeout_seconds", 240)),
        miner_base_url=os.environ.get("RKA_MEMORY_MINER_BASE_URL", raw["miner"].get("base_url", "")),
        miner_api_key_env=raw["miner"].get("api_key_env"),
        hooks_background=_choice(raw.get("hooks", {}).get("background", "spawn"), BACKGROUND, "hooks.background"),
        skills_dir=Path(raw["skills"]["dir"]).expanduser(),
        raw=raw,
    )
