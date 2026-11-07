"""Write the compose stack's rka-memory config into the ledger volume.

Run at every start of the `memory` service. It keeps `core.project_id` (set
once by `rka-memory init`) and rewrites only what the stack owns: the miner
route for RKM_MODEL, a single syncer, and the skills directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

from rka_memory.config import load, merge, user_config_path
from rka_memory.miner.backends import deepseek_miner

MOCK_MINER = {"backend": "openai_compat", "base_url": "http://mock-llm:8000/v1", "model": "mock-model", "api_key_env": "MOCK_LLM_KEY"}


def main() -> int:
    model = os.environ.get("RKM_MODEL", "mock")
    if model not in ("mock", "deepseek"):
        print(f"RKM_MODEL must be mock or deepseek, not {model!r}", file=sys.stderr)
        return 2
    path = user_config_path()
    raw = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    stack = {
        # the memory service is the only syncer; session ends only record
        "core": {"sync_on_session_end": False},
        "miner": MOCK_MINER if model == "mock" else deepseek_miner(),
        # dsh gets skills through the plugin, which follows the packet arm
        "skills": {"dir": str(Path(os.environ["RKA_MEMORY_DIR"]) / "skills"), "export_files": False},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(merge(raw, stack), sort_keys=False))
    os.replace(tmp, path)
    cfg = load()
    print(f"config {path}: miner {cfg.miner_model} at {cfg.miner_base_url}, core project {cfg.core_project_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
