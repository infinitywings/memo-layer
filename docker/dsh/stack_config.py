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

MINERS = {
    "mock": {"base_url": "http://mock-llm:8000/v1", "model": "mock-model", "api_key_env": "MOCK_LLM_KEY"},
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key_env": "DEEPSEEK_API_KEY"},
}


def main() -> int:
    model = os.environ.get("RKM_MODEL", "mock")
    if model not in MINERS:
        print(f"RKM_MODEL must be one of {sorted(MINERS)}, not {model!r}", file=sys.stderr)
        return 2
    miner = {"backend": "openai_compat", **MINERS[model]}
    if os.environ.get("RKM_MINER_MODEL"):
        miner["model"] = os.environ["RKM_MINER_MODEL"]
    path = user_config_path()
    raw = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    stack = {
        # the memory service is the only syncer; session ends only record
        "core": {"sync_on_session_end": False},
        "miner": miner,
        "skills": {"dir": str(Path(os.environ["RKA_MEMORY_DIR"]) / "skills")},
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
