"""LLM backends for the miner, selected by `miner.backend` in config."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

import httpx

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


ENV_PASSTHROUGH = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "SHELL")


def miner_env() -> dict[str, str]:
    """Only what the CLI needs to find itself and its login. RKA_MEMORY_DISABLE
    stops rka-memory's own hooks, or mining one session would log another."""
    env = {k: v for k, v in os.environ.items() if k in ENV_PASSTHROUGH or k.startswith(("ANTHROPIC_", "CLAUDE_"))}
    env["RKA_MEMORY_DISABLE"] = "1"
    return env


def claude_command(cfg: Config) -> list[str]:
    # The prompt quotes tool output the agent saw, so it may carry injected
    # instructions: the miner gets no tools, no MCP servers and no project.
    return [
        "claude", "-p",
        "--model", cfg.miner_model,
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]


def claude_cli(cfg: Config) -> Backend:
    def run(prompt: str) -> Completion:
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="rkm-miner-") as cwd:
                proc = subprocess.run(
                    claude_command(cfg),
                    input=prompt,
                    capture_output=True,
                    text=True,
                    env=miner_env(),
                    cwd=cwd,
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


def openai_compat(cfg: Config, transport: httpx.BaseTransport | None = None) -> Backend:
    """Any OpenAI-compatible chat endpoint: the stack's mock model in tests,
    DeepSeek or another provider in live runs."""
    if not cfg.miner_base_url:
        raise ValueError("miner.backend openai_compat needs miner.base_url")
    url = cfg.miner_base_url.rstrip("/") + "/chat/completions"

    def run(prompt: str) -> Completion:
        key = os.environ.get(cfg.miner_api_key_env) if cfg.miner_api_key_env else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {"model": cfg.miner_model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
        started = time.monotonic()
        try:
            with httpx.Client(transport=transport, timeout=cfg.miner_timeout_seconds) as client:
                resp = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise BackendError(f"miner endpoint unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise BackendError(f"miner endpoint answered HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            doc = resp.json()
            text = doc["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise BackendError(f"miner endpoint returned an unexpected body: {resp.text[:300]}") from exc
        usage = doc.get("usage") or {}
        tokens = int(usage.get("total_tokens") or int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0))
        return Completion(text=text, tokens=tokens, seconds=round(time.monotonic() - started, 3))

    return run


def none(prompt: str) -> Completion:
    return Completion(text="", tokens=0, seconds=0.0)


BACKENDS: dict[str, Callable[[Config], Backend]] = {
    "claude_cli": claude_cli,
    "openai_compat": openai_compat,
    "none": lambda cfg: none,
}


def backend_for(cfg: Config) -> Backend:
    try:
        return BACKENDS[cfg.miner_backend](cfg)
    except KeyError:
        raise ValueError(f"unknown miner backend {cfg.miner_backend!r}; choose one of {sorted(BACKENDS)}") from None
