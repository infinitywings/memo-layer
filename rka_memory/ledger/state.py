"""Session state detection: project, toolchain, and the keyword task classifier."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from rka_memory.config import Config
from rka_memory.domain import State

# First marker present at the project root wins.
TOOLCHAIN_MARKERS: tuple[tuple[str, str], ...] = (
    ("uv.lock", "uv"),
    ("poetry.lock", "poetry"),
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("package-lock.json", "npm"),
    ("package.json", "npm"),
    ("Cargo.toml", "cargo"),
    ("go.mod", "go"),
    ("Gemfile", "bundler"),
    ("pyproject.toml", "pip"),
    ("requirements.txt", "pip"),
)

# Keywords match at the start of a word, so "test" also hits "tests" and
# "testing". The class with the most hits wins; ties go to the earlier row.
TASK_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("debugging", ("debug", "bug", "fix", "error", "traceback", "exception", "crash", "fail", "broken", "not working")),
    ("testing", ("test", "pytest", "coverage", "assert", "flaky")),
    ("refactoring", ("refactor", "rename", "extract", "clean up", "cleanup", "simplify", "restructure", "dedupe", "deduplicate")),
    ("writing", ("write up", "readme", "docs", "documentation", "draft", "paper", "blog", "essay", "prose", "email", "summarize")),
    ("research", ("research", "investigate", "explore", "compare", "literature", "survey", "look into", "how does", "explain")),
    ("ops", ("deploy", "docker", "kubernetes", "pipeline", "install", "configure", "server", "infra", "release", "cron")),
    ("data", ("dataset", "csv", "sql", "query", "pandas", "plot", "chart", "analyze", "analysis", "dataframe")),
    ("coding", ("implement", "add", "build", "create", "feature", "function", "endpoint", "code", "write a")),
)

_KEYWORD_PATTERNS = tuple(
    (task, tuple(re.compile(r"\b" + re.escape(kw)) for kw in keywords)) for task, keywords in TASK_KEYWORDS
)


def project_root(cwd: Path) -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd.resolve()
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else cwd.resolve()


def toolchain(root: Path) -> str:
    for marker, name in TOOLCHAIN_MARKERS:
        if (root / marker).exists():
            return name
    return "none"


def detect(cwd: Path, cfg: Config, model_id: str | None = None) -> State:
    root = project_root(cwd)
    return State(
        task_class=None,
        project=str(root),
        toolchain=toolchain(root),
        model_id=model_id,
        user_context=cfg.user_context,
    )


def classify_task(prompt: str, cfg: Config) -> str:
    text = prompt.lower()
    best, best_hits = "other", 0
    for task, patterns in _KEYWORD_PATTERNS:
        if task not in cfg.task_classes:
            continue
        hits = sum(1 for p in patterns if p.search(text))
        if hits > best_hits:
            best, best_hits = task, hits
    return best
