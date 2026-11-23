"""workload/live.py: the provider a live stream runs on."""

import pytest

from rka_memory.workload.live import live_route

OPENAI = {"RKM_LIVE_PROVIDER": "openai", "RKM_OPENAI_MODEL": "gpt-x", "RKM_OPENAI_PROXY_KEY": "k"}


def test_deepseek_stays_the_default_and_keeps_dshs_own_route():
    r = live_route({"DEEPSEEK_API_KEY": "k"})
    assert (r.provider, r.dsh, r.missing({"DEEPSEEK_API_KEY": "k"})) == ("deepseek", None, [])
    assert r.missing({}) == ["DEEPSEEK_API_KEY"]


def test_openai_routes_agent_and_miner_through_the_proxy_with_the_proxy_key():
    r = live_route(OPENAI)
    providers = r.dsh[0]["config"]["providers"]
    assert providers["openai-plan"]["api"] == "openai-responses"
    assert providers["openai-plan"]["baseURL"] == "http://host.docker.internal:8790/v1"
    assert providers["openai-plan"]["apiKeyEnv"] == "RKM_OPENAI_PROXY_KEY"
    assert r.dsh[1]["config"] == {"provider": "openai-plan", "model": "gpt-x", "reasoningEffort": "medium"}
    assert providers["openai-plan"]["models"][0]["reasoningEfforts"]["high"] == "high"
    assert r.miner == {"backend": "openai_compat", "base_url": "http://host.docker.internal:8790/v1", "model": "gpt-x",
                       "api_key_env": "RKM_OPENAI_PROXY_KEY"}
    assert r.missing({"RKM_LIVE_PROVIDER": "openai"}) == ["RKM_OPENAI_PROXY_KEY", "RKM_OPENAI_MODEL"]


def test_the_miner_model_can_differ_from_the_agents():
    assert live_route({**OPENAI, "RKM_MINER_MODEL": "gpt-mini"}).miner["model"] == "gpt-mini"


def test_reasoning_off_declares_no_levels_and_selects_none():
    r = live_route({**OPENAI, "RKM_OPENAI_REASONING": "off"})
    assert "reasoningEfforts" not in r.dsh[0]["config"]["providers"]["openai-plan"]["models"][0]
    assert r.dsh[1]["config"] == {"provider": "openai-plan", "model": "gpt-x"}


def test_a_reasoning_level_the_plan_does_not_offer_is_refused():
    with pytest.raises(ValueError, match="RKM_OPENAI_REASONING"):
        live_route({**OPENAI, "RKM_OPENAI_REASONING": "ultra"})


def test_an_unknown_provider_is_refused():
    with pytest.raises(ValueError, match="deepseek or openai"):
        live_route({"RKM_LIVE_PROVIDER": "anthropic"})
