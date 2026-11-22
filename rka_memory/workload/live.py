"""Which model a live stream runs on: the agent's dsh route and the miner's.

RKM_LIVE_PROVIDER picks one:

- deepseek (default): dsh's own default route and DeepSeek for the miner,
  key in DEEPSEEK_API_KEY. E2 and the E2b pilot ran here.
- openai: the user's ChatGPT plan through the host's token proxy
  (scripts/openai_plan.py serve). The agent uses dsh's openai-responses
  route and the miner the proxy's chat route, both with the local proxy key
  in RKM_OPENAI_PROXY_KEY and the model in RKM_OPENAI_MODEL.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rka_memory.miner.backends import DEEPSEEK, deepseek_miner

PROXY_URL = "http://host.docker.internal:8790/v1"
CONTEXT_WINDOW = 200_000


@dataclass(frozen=True)
class LiveRoute:
    provider: str
    miner: dict[str, Any]
    dsh: list[dict[str, Any]] | None  # a route overlay; None keeps dsh's default
    route: str  # for the report
    needs: tuple[str, ...]

    @property
    def miner_label(self) -> str:
        return f"{self.miner['model']} at {self.miner['base_url']}"

    def missing(self, env: Mapping[str, str]) -> list[str]:
        return [name for name in self.needs if not env.get(name)]


def openai_route(env: Mapping[str, str]) -> LiveRoute:
    model = env.get("RKM_OPENAI_MODEL", "")
    base = env.get("RKM_OPENAI_BASE_URL") or PROXY_URL
    dsh = [
        {
            "id": "llm-pi-ai",
            "name": "@deepseek-ai/dsh-llm-pi-ai",
            "config": {
                "providers": {
                    "openai-plan": {
                        "displayName": "OpenAI (ChatGPT plan)",
                        "apiKeyEnv": "RKM_OPENAI_PROXY_KEY",
                        "api": "openai-responses",
                        "baseURL": base,
                        "compat": {"supportsMaxOutputTokens": False},
                        "models": [{"id": model, "name": model, "contextWindow": int(env.get("RKM_OPENAI_CONTEXT") or CONTEXT_WINDOW)}],
                    }
                }
            },
        },
        {"id": "agent-default-model", "name": "@deepseek-ai/dsh-agent-default-model", "config": {"provider": "openai-plan", "model": model}},
    ]
    miner = {"backend": "openai_compat", "base_url": base, "model": env.get("RKM_MINER_MODEL") or model, "api_key_env": "RKM_OPENAI_PROXY_KEY"}
    return LiveRoute("openai", miner, dsh, f"OpenAI {model} on the ChatGPT plan via {base}", ("RKM_OPENAI_PROXY_KEY", "RKM_OPENAI_MODEL"))


def live_route(env: Mapping[str, str] | None = None) -> LiveRoute:
    env = os.environ if env is None else env
    provider = env.get("RKM_LIVE_PROVIDER") or "deepseek"
    if provider == "deepseek":
        return LiveRoute("deepseek", deepseek_miner(), None, "dsh's default DeepSeek model", (DEEPSEEK["api_key_env"],))
    if provider == "openai":
        return openai_route(env)
    raise ValueError(f"RKM_LIVE_PROVIDER must be deepseek or openai, not {provider!r}")
