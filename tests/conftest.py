import sys
from pathlib import Path

import pytest
import yaml

V2 = {"gate": {"validation": "threshold"}}

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def memory(tmp_path, monkeypatch):
    """A Memory over an empty ledger in a temp dir, with skills exported to tmp."""
    from rka_memory import config as config_mod
    from rka_memory.service import Memory

    monkeypatch.setenv("RKA_MEMORY_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("RKA_MEMORY_CONFIG", str(tmp_path / "none.yaml"))
    # The tests written for DESIGN_V2 run under its threshold validation,
    # also for every Memory.open() a hook makes; v3 tests ask for attested.
    (tmp_path / "none.yaml").write_text(yaml.safe_dump(V2))
    cfg = config_mod.load(overrides={"skills": {"dir": str(tmp_path / "skills")}, "miner": {"backend": "none"}})
    return Memory.open(cfg)
