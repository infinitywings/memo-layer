"""LLM backends for the miner, selected by `miner.backend` in config."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from rka_memory.config import Config


@dataclass(frozen=True)
class Completion:
    text: str
    tokens: int
    seconds: float


class BackendError(RuntimeError):
    """The model call did not complete; nothing was spent that the ledger should record."""


Backend = Callable[[str], Completion]

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def parse_cli_output(stdout: str, elapsed: float) -> Completion:
    """Parse `claude -p --output-format json` into a Completion."""
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BackendError(f"claude CLI printed non-JSON output: {stdout[:200]!r}") from exc
    if doc.get("is_error"):
        raise BackendError(f"claude CLI reported an error: {str(doc.get('result', ''))[:300]}")
    usage = doc.get("usage") or {}
    tokens = sum(int(usage.get(k) or 0) for k in USAGE_KEYS)
    duration_ms = doc.get("duration_ms")
    seconds = duration_ms / 1000 if isinstance(duration_ms, (int, float)) else elapsed
    return Completion(text=str(doc.get("result") or ""), tokens=tokens, seconds=round(seconds, 3))


def claude_cli(cfg: Config) -> Backend:
    def run(prompt: str) -> Completion:
        # The miner's own Claude session must not fire rka-memory hooks, or
        # mining one session would log (and later mine) another.
        env = {**os.environ, "RKA_MEMORY_DISABLE": "1"}
        cmd = ["claude", "-p", "--model", cfg.miner_model, "--output-format", "json"]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                env=env,
                timeout=cfg.miner_timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise BackendError("claude CLI not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"claude CLI timed out after {cfg.miner_timeout_seconds}s") from exc
        elapsed = time.monotonic() - started
        if proc.returncode != 0 and not proc.stdout.strip():
            raise BackendError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        return parse_cli_output(proc.stdout, elapsed)

    return run


def none(prompt: str) -> Completion:
    return Completion(text="", tokens=0, seconds=0.0)


BACKENDS: dict[str, Callable[[Config], Backend]] = {
    "claude_cli": claude_cli,
    "none": lambda cfg: none,
}


def backend_for(cfg: Config) -> Backend:
    try:
        return BACKENDS[cfg.miner_backend](cfg)
    except KeyError:
        raise ValueError(f"unknown miner backend {cfg.miner_backend!r}; choose one of {sorted(BACKENDS)}") from None
