"""LLM istemcisinin ağ çağrısı yapmayan kısımları."""

from __future__ import annotations

from dersnotu.llm.client import LLMClient, cached, estimate_cost, supports_thinking
from dersnotu.models import Usage


def make_client() -> LLMClient:
    c = LLMClient.__new__(LLMClient)  # __init__ bir Anthropic istemcisi kurar
    c.model = "claude-sonnet-5"
    c.cheap_model = "claude-haiku-4-5"
    c.effort = "high"
    c.max_tokens = 16000
    c.usage = Usage()
    return c


def test_thinking_and_effort_only_for_supporting_models():
    assert supports_thinking("claude-sonnet-5")
    assert supports_thinking("claude-opus-5")
    # Haiku 4.5 adaptive thinking / effort desteklemiyor; göndermek 400 döndürür.
    assert not supports_thinking("claude-haiku-4-5")


def test_params_omit_thinking_for_haiku():
    c = make_client()
    params = c._params("claude-haiku-4-5")
    assert "thinking" not in params
    assert "output_config" not in params


def test_params_include_thinking_and_effort_for_sonnet():
    c = make_client()
    params = c._params("claude-sonnet-5")
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"]["effort"] == "high"
    # Kaldırılmış parametreler gönderilmemeli (400 sebebi).
    assert "temperature" not in params
    assert "top_p" not in params
    assert "budget_tokens" not in str(params)


def test_json_schema_goes_into_output_config():
    c = make_client()
    schema = {"type": "object"}
    params = c._params("claude-haiku-4-5", json_schema=schema)
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": schema}


def test_cached_adds_breakpoint_without_mutating_original():
    block = {"type": "text", "text": "abc"}
    out = cached(block)
    assert out["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in block


def test_cost_uses_cache_multipliers():
    # Sonnet 5: $3 giriş / $15 çıkış per MTok
    u = Usage(input_tokens=1_000_000)
    assert estimate_cost(u, "claude-sonnet-5") == 3.0

    # Cache yazma 1.25x, okuma 0.10x
    assert estimate_cost(Usage(cache_creation_tokens=1_000_000), "claude-sonnet-5") == 3.75
    assert abs(estimate_cost(Usage(cache_read_tokens=1_000_000), "claude-sonnet-5") - 0.30) < 1e-9
    assert estimate_cost(Usage(output_tokens=1_000_000), "claude-sonnet-5") == 15.0


def test_usage_accumulates():
    total = Usage()
    total.add(Usage(input_tokens=10, output_tokens=5, calls=1))
    total.add(Usage(input_tokens=20, cache_read_tokens=7, calls=1))
    assert (total.input_tokens, total.output_tokens, total.cache_read_tokens, total.calls) == (
        30,
        5,
        7,
        2,
    )
