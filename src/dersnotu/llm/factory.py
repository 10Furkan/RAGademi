"""Hangi arka uçla konuşulacağına karar veren TEK yer.

Üç seçenek var ve üçü de aynı yüzeyi sunuyor:

    api   — Anthropic Messages API. Kredili API anahtarı ister. Token başına
            ödenir; prompt caching bizim elimizde, maliyet öngörülebilir.
    cli   — Claude Code CLI. Claude Pro/Max ABONELİĞİYLE çalışır, ayrı API
            anahtarı gerekmez. Token başına ödeme yok; bunun yerine abonelik
            kotası (5 saatlik pencere) harcanır.
    demo  — Sahte istemci. Ağa çıkmaz, kimlik istemez; orkestrasyonu doğrular.

`auto` sırayla bakar: API anahtarı varsa `api`, yoksa `claude` komutu varsa
`cli`, o da yoksa `demo`. Böylece kullanıcı hiçbir şey ayarlamadan da bir
şeyler görebiliyor.
"""

from __future__ import annotations

from typing import Any

from .cli_client import ClaudeCodeClient
from .client import LLMClient

BACKENDS = ("auto", "api", "cli", "demo")


def resolve_backend(backend: str = "auto") -> str:
    """`auto` seçimini somut bir arka uca indirger."""
    if backend != "auto":
        return backend
    if LLMClient.credentials_available():
        return "api"
    if ClaudeCodeClient.available():
        return "cli"
    return "demo"


def backend_status() -> dict[str, Any]:
    """Arayüzün hangi seçeneklerin kullanılabilir olduğunu göstermesi için."""
    api_ok = LLMClient.credentials_available()
    cli_ok = ClaudeCodeClient.available()
    return {
        "resolved": resolve_backend("auto"),
        "api": {"available": api_ok, "label": "API anahtarı"},
        "cli": {
            "available": cli_ok,
            "label": "Claude Pro / Max aboneliği",
            # auth_mode() bir alt süreç başlatıyor; sadece istendiğinde çağrılır.
        },
        "demo": {"available": True, "label": "Demo (API çağrısı yok)"},
    }


def make_client(backend: str, settings) -> Any:
    """Seçilen arka ucun istemcisini kurar.

    Dönen nesne her durumda `complete` / `complete_json` / `usage` sunar;
    `pipeline` hangisini aldığını bilmez.
    """
    backend = resolve_backend(backend)

    if backend == "demo":
        from .fake import FakeLLMClient

        return FakeLLMClient(
            model=settings.model,
            cheap_model=settings.cheap_model,
            effort=settings.effort,
            max_tokens=settings.max_tokens,
        )

    if backend == "cli":
        return ClaudeCodeClient(
            model=settings.model,
            cheap_model=settings.cheap_model,
            effort=settings.effort,
            max_tokens=settings.max_tokens,
        )

    if backend == "api":
        return LLMClient(
            model=settings.model,
            cheap_model=settings.cheap_model,
            effort=settings.effort,
            max_tokens=settings.max_tokens,
        )

    raise ValueError(f"Bilinmeyen arka uç: {backend}. Seçenekler: {', '.join(BACKENDS)}")
