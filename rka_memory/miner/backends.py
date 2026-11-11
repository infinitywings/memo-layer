"""LLM backends for the miner, selected by `miner.backend` in config."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

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


def miner_env(*prefixes: str) -> dict[str, str]:
    """Only what the CLI needs to find itself and its login: the basics plus
    the variables with one of its own `prefixes`. RKA_MEMORY_DISABLE stops
    rka-memory's own hooks, or mining one session would log another."""
    env = {k: v for k, v in os.environ.items() if k in ENV_PASSTHROUGH or k.startswith(prefixes)}
    env["RKA_MEMORY_DISABLE"] = "1"
    return env


def run_cli(
    name: str, command: list[str], prompt: str, env: dict[str, str], timeout: int
) -> tuple[subprocess.CompletedProcess[str], float]:
    """Run a model CLI on the prompt in an empty directory, so it has no
    project to read. Returns the finished process and the seconds it took."""
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="rkm-miner-") as cwd:
            proc = subprocess.run(command, input=prompt, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)
    except FileNotFoundError as exc:
        raise BackendError(f"{name} CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"{name} CLI timed out after {timeout}s") from exc
    return proc, time.monotonic() - started


CLAUDE_MODEL = "haiku"


def claude_command(cfg: Config) -> list[str]:
    # The prompt quotes tool output the agent saw, so it may carry injected
    # instructions: the miner gets no tools, no MCP servers and no project.
    return [
        "claude", "-p",
        "--model", cfg.miner_model or CLAUDE_MODEL,
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]


def claude_cli(cfg: Config) -> Backend:
    def run(prompt: str) -> Completion:
        env = miner_env("ANTHROPIC_", "CLAUDE_")
        proc, elapsed = run_cli("claude", claude_command(cfg), prompt, env, cfg.miner_timeout_seconds)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise BackendError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        return parse_cli_output(proc.stdout, elapsed)

    return run


# What lets the Codex agent act instead of answer: running commands, the
# user's connected apps and plugins, the browser and the desktop, other
# agents, and its own memories and hooks.
CODEX_OFF = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "browser_use",
    "computer_use",
    "multi_agent",
    "memories",
    "hooks",
)
CODEX_ACTIONS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})


def codex_command(cfg: Config) -> list[str]:
    # As for claude above: no tools, no user config (so no MCP servers), no
    # project, and no session left behind. Codex runs under its own login, so
    # a ChatGPT subscription can pay for mining. Without `miner.model` it
    # uses its own default model.
    command = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config"]
    command += ["--sandbox", "read-only", "--color", "never"]
    for feature in CODEX_OFF:
        command += ["--disable", feature]
    if cfg.miner_model:
        command += ["--model", cfg.miner_model]
    return [*command, "-"]


def _codex_events(stdout: str) -> Iterator[dict[str, Any]]:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _codex_message(event: dict[str, Any]) -> str:
    error = event.get("error")
    return str(event.get("message") or (error.get("message") if isinstance(error, dict) else "") or "")


def parse_codex_output(stdout: str, elapsed: float) -> Completion:
    """Parse `codex exec --json`, one event per line, into a Completion: the
    last agent message, and the input and output tokens of every turn."""
    text, tokens = "", 0
    for event in _codex_events(stdout):
        kind = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") in CODEX_ACTIONS:
            # A switch in CODEX_OFF did not hold, so the answer may be the
            # work of instructions in the prompt, not a reading of it.
            raise BackendError(f"codex CLI used a tool while mining: {item['type']}")
        if kind == "turn.failed":
            raise BackendError(f"codex CLI reported an error: {_codex_message(event)[:300]}")
        if kind == "item.completed" and item.get("type") == "agent_message":
            text = str(item.get("text") or "")
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    return Completion(text=text, tokens=tokens, seconds=round(elapsed, 3))


def codex_cli(cfg: Config) -> Backend:
    def run(prompt: str) -> Completion:
        proc, elapsed = run_cli("codex", codex_command(cfg), prompt, miner_env("CODEX_"), cfg.miner_timeout_seconds)
        if proc.returncode != 0:
            said = [_codex_message(e) for e in _codex_events(proc.stdout) if e.get("type") in ("error", "turn.failed")]
            detail = said[-1] if said else proc.stderr.strip()
            raise BackendError(f"codex CLI exited {proc.returncode}: {detail[-300:]}")
        return parse_codex_output(proc.stdout, elapsed)

    return run


# The miner settings for DeepSeek's OpenAI-compatible endpoint. dsh's own
# default model is deepseek-flash too; RKM_MINER_MODEL overrides the model.
DEEPSEEK = {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key_env": "DEEPSEEK_API_KEY"}


def deepseek_miner() -> dict[str, str]:
    return {"backend": "openai_compat", **DEEPSEEK, "model": os.environ.get("RKM_MINER_MODEL") or DEEPSEEK["model"]}


# A dropped connection, a rate limit or a server error is retried once; a
# session whose miner call fails stays unmined until something mines it again.
ATTEMPTS = 2
RETRY_SECONDS = 5.0
TRANSIENT = frozenset({429, 500, 502, 503, 504})


def openai_compat(cfg: Config, transport: httpx.BaseTransport | None = None) -> Backend:
    """Any OpenAI-compatible chat endpoint: the stack's mock model in tests,
    DeepSeek or another provider in live runs."""
    if not cfg.miner_base_url:
        raise ValueError("miner.backend openai_compat needs miner.base_url")
    if not cfg.miner_model:
        raise ValueError("miner.backend openai_compat needs miner.model")
    url = cfg.miner_base_url.rstrip("/") + "/chat/completions"

    def run(prompt: str) -> Completion:
        key = os.environ.get(cfg.miner_api_key_env) if cfg.miner_api_key_env else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {"model": cfg.miner_model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
        started = time.monotonic()
        for attempt in range(1, ATTEMPTS + 1):
            try:
                with httpx.Client(transport=transport, timeout=cfg.miner_timeout_seconds) as client:
                    resp = client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < ATTEMPTS:
                    time.sleep(RETRY_SECONDS)
                    continue
                raise BackendError(f"miner endpoint unreachable: {exc}") from exc
            if resp.status_code in TRANSIENT and attempt < ATTEMPTS:
                time.sleep(RETRY_SECONDS)
                continue
            break
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
    "codex_cli": codex_cli,
    "openai_compat": openai_compat,
    "none": lambda cfg: none,
}


def backend_for(cfg: Config) -> Backend:
    try:
        return BACKENDS[cfg.miner_backend](cfg)
    except KeyError:
        raise ValueError(f"unknown miner backend {cfg.miner_backend!r}; choose one of {sorted(BACKENDS)}") from None
