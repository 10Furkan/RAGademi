"""Arka uç seçimi: API anahtarı / Claude Pro aboneliği / demo.

`ClaudeCodeClient`'ın çıktı ayrıştırması gerçek CLI çalıştırmadan sınanır —
alt süreç yerine kaydedilmiş NDJSON verilir. Bu, ölçülerek bulunmuş dört
davranışın (yalın bayraklar, cache_control sıyırma, stream_event deltaları,
rate_limit_event) regresyona uğramasını engeller.
"""

from __future__ import annotations

import json

import pytest

from dersnotu.llm import BACKENDS, ClaudeCodeClient, resolve_backend
from dersnotu.llm.cli_client import _SLIM_FLAGS, _strip_cache_control
from dersnotu.llm.factory import make_client


class Ayarlar:
    model = "claude-sonnet-5"
    cheap_model = "claude-haiku-4-5"
    effort = "high"
    max_tokens = 16000


# --- Seçim ---------------------------------------------------------------
def test_demo_backend_needs_no_credentials():
    client = make_client("demo", Ayarlar())
    assert hasattr(client, "complete") and hasattr(client, "complete_json")


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="Bilinmeyen arka uç"):
        make_client("gpt", Ayarlar())


def test_auto_falls_back_to_demo_when_nothing_available(monkeypatch):
    """Hiçbir kimlik yoksa kullanıcı yine de bir şey görebilmeli."""
    monkeypatch.setattr(
        "dersnotu.llm.factory.LLMClient.credentials_available", staticmethod(lambda: False)
    )
    monkeypatch.setattr(
        "dersnotu.llm.factory.ClaudeCodeClient.available", staticmethod(lambda *_: False)
    )
    assert resolve_backend("auto") == "demo"


def test_auto_prefers_api_then_cli(monkeypatch):
    monkeypatch.setattr(
        "dersnotu.llm.factory.LLMClient.credentials_available", staticmethod(lambda: True)
    )
    monkeypatch.setattr(
        "dersnotu.llm.factory.ClaudeCodeClient.available", staticmethod(lambda *_: True)
    )
    assert resolve_backend("auto") == "api"

    monkeypatch.setattr(
        "dersnotu.llm.factory.LLMClient.credentials_available", staticmethod(lambda: False)
    )
    assert resolve_backend("auto") == "cli"


def test_explicit_backend_is_not_overridden(monkeypatch):
    monkeypatch.setattr(
        "dersnotu.llm.factory.LLMClient.credentials_available", staticmethod(lambda: True)
    )
    assert resolve_backend("demo") == "demo"


def test_backend_names_are_stable():
    assert BACKENDS == ("auto", "api", "cli", "demo")


# --- CLI istemcisinin sözleşmeleri ---------------------------------------
def test_cache_control_is_stripped():
    """CLI kendi 1h TTL'li bloğunu ekliyor; bizimki 400 döndürüyordu."""
    blocks = [
        {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
        {"type": "image", "source": {"type": "base64", "data": "x"}},
    ]
    out = _strip_cache_control(blocks)
    assert all("cache_control" not in b for b in out)
    # Geri kalan alanlar korunmalı.
    assert out[0]["text"] == "a"
    assert out[1]["source"]["data"] == "x"


def test_slim_flags_disable_the_agent_harness():
    """Bunlar olmadan çağrı başına ~41K token ek yük biniyor (ölçüldü)."""
    flags = " ".join(_SLIM_FLAGS)
    for beklenen in ("--tools", "--setting-sources", "--strict-mcp-config",
                     "--disable-slash-commands", "--no-session-persistence"):
        assert beklenen in flags


def _client() -> ClaudeCodeClient:
    c = ClaudeCodeClient.__new__(ClaudeCodeClient)
    c.model, c.cheap_model, c.effort = "m", "c", "high"
    c.max_tokens, c.timeout, c.executable = 100, 10, "claude"
    from dersnotu.models import Usage

    c.usage = Usage()
    c.rate_limit = None
    return c


def _ndjson(*events: dict) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events)


def _delta(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _result(text: str, **kw) -> dict:
    base = {
        "type": "result",
        "is_error": False,
        "result": text,
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3,
        },
    }
    return {**base, **kw}


def test_text_deltas_are_streamed_and_joined():
    c = _client()
    parcalar: list[str] = []
    out = c._parse(_ndjson(_delta("Mer"), _delta("haba"), _result("yoksayilir")), parcalar.append)
    assert parcalar == ["Mer", "haba"]
    assert out.text == "Merhaba"


def test_thinking_deltas_are_not_treated_as_output():
    """Claude Code'da adaptive thinking açık; düşünme metni çıktıya sızmamalı."""
    c = _client()
    dusunme = {
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": "thinking_delta", "thinking": "gizli"}},
    }
    imza = {
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": "signature_delta", "signature": "abc"}},
    }
    out = c._parse(_ndjson(dusunme, imza, _delta("görünür"), _result("x")), None)
    assert out.text == "görünür"
    assert "gizli" not in out.text


def test_falls_back_to_result_text_when_no_deltas():
    c = _client()
    assert c._parse(_ndjson(_result("yalnız sonuç")), None).text == "yalnız sonuç"


def test_blank_deltas_fall_back_to_result():
    """--json-schema modunda çıktı StructuredOutput aracına gidiyor; metin
    kanalından yalnızca boşluk akıyor. " " truthy olduğu için ham `or` bunu
    geçerli sanıp JSON ayrıştırmasını patlatıyordu."""
    c = _client()
    out = c._parse(_ndjson(_delta(" "), _delta("\n"), _result('{"cards":[]}')), None)
    assert out.text == '{"cards":[]}'
    assert json.loads(out.text) == {"cards": []}


def test_usage_is_accumulated():
    c = _client()
    c._parse(_ndjson(_result("a")), None)
    c._parse(_ndjson(_result("b")), None)
    assert c.usage.calls == 2
    assert c.usage.input_tokens == 20
    assert c.usage.cache_read_tokens == 6


def test_rate_limit_event_is_captured():
    """Abonelik kotası API'de yok; arayüze taşınabilmesi için yakalanmalı."""
    c = _client()
    ev = {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour",
                            "resetsAt": 1785799200},
    }
    c._parse(_ndjson(ev, _result("x")), None)
    assert c.rate_limit["rateLimitType"] == "five_hour"
    assert c.rate_limit["status"] == "allowed"


def test_structured_output_is_preferred_over_text():
    """CLI şemayı StructuredOutput aracıyla uyguluyor; nesne `result` olayının
    `structured_output` alanından gelir, metin kanalı boş kalabilir."""
    c = _client()
    out = c._parse(
        _ndjson(_result("", structured_output={"cards": [{"title": "A"}]})), None
    )
    assert out.structured == {"cards": [{"title": "A"}]}


def test_complete_json_uses_structured_field(monkeypatch):
    from dersnotu.llm.client import CallResult

    c = _client()
    monkeypatch.setattr(
        c, "complete",
        lambda **kw: CallResult(text="", structured={"alignments": [1, 2]}),
    )
    assert c.complete_json(system="s", content=[], schema={}) == {"alignments": [1, 2]}


def test_complete_json_reports_empty_structured_output(monkeypatch):
    """Boş yanıt sessiz bir JSONDecodeError yerine anlaşılır hata vermeli."""
    from dersnotu.llm.client import CallResult

    c = _client()
    monkeypatch.setattr(
        c, "complete", lambda **kw: CallResult(text="  ", stop_reason="max_tokens")
    )
    with pytest.raises(RuntimeError, match="yapılandırılmış çıktı döndürmedi"):
        c.complete_json(system="s", content=[], schema={})


def test_error_result_raises():
    c = _client()
    with pytest.raises(RuntimeError, match="claude CLI"):
        c._parse(_ndjson(_result("API Error: 400", is_error=True)), None)


def test_missing_result_event_raises():
    """Süreç sessizce ölürse bunu sessiz bir boş yanıt sanmayalım."""
    c = _client()
    with pytest.raises(RuntimeError, match="result"):
        c._parse(_ndjson(_delta("yarım")), None)


def test_garbage_lines_are_skipped():
    c = _client()
    bozuk = "bu json degil\n" + _ndjson(_delta("iyi"), _result("x"))
    assert c._parse(bozuk, None).text == "iyi"
