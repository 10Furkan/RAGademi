"""Anthropic Messages API sarmalayıcısı.

İki şey burada özellikle önemli:

1. PROMPT CACHING. Ders bağlamı (~40K token) her bölüm çağrısında aynen
   tekrarlanır. Cache kırılma noktası bu sabit önekin sonuna konur; bölüme özel
   içerik ondan SONRA gelir. 8 bölümlük bir derste bu, önek maliyetini ~5 kat
   düşürüyor. Önekte tek bayt değişirse (tarih, sayaç, sıralanmamış JSON) cache
   sessizce düşer — bu yüzden önek tamamen deterministik kurulur.

2. STREAMING. Yüksek `max_tokens` ile streaming olmadan SDK HTTP zaman aşımına
   düşüyor. Tüm üretim çağrıları stream üzerinden yapılır.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from ..models import Usage

# Adaptive thinking + effort destekleyen modeller. Haiku 4.5 desteklemiyor;
# bu parametreleri ona göndermek 400 döndürür.
_THINKING_MODELS = ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
                    "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5", "claude-mythos-5")

# 1M token bağlam ücretlendirmesi ($/MTok). Yalnızca raporlama için.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}


class CredentialsMissing(RuntimeError):
    pass


class RefusalError(RuntimeError):
    def __init__(self, category: str | None, explanation: str | None):
        self.category = category
        self.explanation = explanation
        super().__init__(f"Model isteği reddetti (kategori={category}): {explanation}")


@dataclass
class CallResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None
    # Yapılandırılmış çıktı, sağlayıcı onu ayrı bir kanalda verdiyse. Claude
    # Code CLI şemayı `StructuredOutput` aracıyla uyguluyor ve sonucu
    # `result.structured_output` alanında döndürüyor; metin kanalı boş kalıyor.
    structured: Any = None


def estimate_cost(usage: Usage, model: str) -> float:
    """Yaklaşık USD maliyet. Cache yazma 1.25x, cache okuma 0.1x girdi fiyatı."""
    inp, out = PRICING.get(model, (0.0, 0.0))
    return (
        usage.input_tokens * inp
        + usage.cache_creation_tokens * inp * 1.25
        + usage.cache_read_tokens * inp * 0.10
        + usage.output_tokens * out
    ) / 1_000_000


def supports_thinking(model: str) -> bool:
    return any(model.startswith(m) for m in _THINKING_MODELS)


class LLMClient:
    def __init__(self, model: str, *, cheap_model: str, effort: str = "high",
                 max_tokens: int = 16000):
        self.model = model
        self.cheap_model = cheap_model
        self.effort = effort
        self.max_tokens = max_tokens
        self.usage = Usage()
        try:
            self._client = anthropic.Anthropic()
        except Exception as exc:  # pragma: no cover
            raise CredentialsMissing(str(exc)) from exc

    # ------------------------------------------------------------------
    @staticmethod
    def credentials_available() -> bool:
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True
        # `ant auth login` profili diskte olabilir.
        from pathlib import Path

        cfg = Path(os.environ.get("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic"))
        return (cfg / "credentials").exists()

    # ------------------------------------------------------------------
    def _params(self, model: str, *, json_schema: dict | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"model": model, "max_tokens": self.max_tokens}
        output_config: dict[str, Any] = {}

        if supports_thinking(model):
            params["thinking"] = {"type": "adaptive"}
            output_config["effort"] = self.effort
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            params["output_config"] = output_config
        return params

    def _record(self, raw_usage) -> Usage:
        u = Usage(
            input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
            cache_creation_tokens=getattr(raw_usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(raw_usage, "cache_read_input_tokens", 0) or 0,
            calls=1,
        )
        self.usage.add(u)
        return u

    # ------------------------------------------------------------------
    def complete(
        self,
        *,
        system: str | list[dict],
        content: list[dict],
        model: str | None = None,
        json_schema: dict | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> CallResult:
        """Tek çağrı. `on_delta` verilirse metin parçaları anlık iletilir."""
        model = model or self.model
        params = self._params(model, json_schema=json_schema)
        params["system"] = system
        params["messages"] = [{"role": "user", "content": content}]

        chunks: list[str] = []
        with self._client.messages.stream(**params) as stream:
            for event in stream:
                if (
                    event.type == "content_block_delta"
                    and getattr(event.delta, "type", "") == "text_delta"
                ):
                    chunks.append(event.delta.text)
                    if on_delta:
                        on_delta(event.delta.text)
            message = stream.get_final_message()

        usage = self._record(message.usage)

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise RefusalError(
                getattr(details, "category", None), getattr(details, "explanation", None)
            )

        text = "".join(chunks)
        if not text:  # thinking-only veya boş yanıt güvencesi
            text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        return CallResult(text=text, usage=usage, stop_reason=message.stop_reason)

    # ------------------------------------------------------------------
    def complete_json(
        self, *, system: str, content: list[dict], schema: dict, model: str | None = None
    ) -> Any:
        """Şemaya uygun JSON döndüren çağrı."""
        result = self.complete(
            system=system, content=content, model=model or self.cheap_model, json_schema=schema
        )
        try:
            return json.loads(result.text)
        except json.JSONDecodeError:
            # Structured output'a rağmen sarmalanmış gelirse kurtar.
            text = result.text.strip()
            start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
            end = max(text.rfind("}"), text.rfind("]"))
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise


def cached(block: dict) -> dict:
    """Bir content block'a cache kırılma noktası koyar."""
    return {**block, "cache_control": {"type": "ephemeral"}}
