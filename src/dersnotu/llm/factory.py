"""Select the single LLM backend used by the pipeline.

Backends expose the same `complete` / `complete_json` / `usage` surface:

    api    — Anthropic Messages API, billed per token.
    cli    — Claude Code CLI with a Claude Pro/Max subscription.
    codex  — Codex CLI with ChatGPT subscription access.
    demo   — Offline fake client for orchestration tests.

`auto` prefers the Anthropic API, then Claude Code, then Codex, and finally demo.
"""

from __future__ import annotations

from typing import Any

from .cli_client import ClaudeCodeClient
from .client import LLMClient
from .codex_client import CodexSubscriptionClient

BACKENDS = ("auto", "api", "cli", "codex", "demo")


def resolve_backend(backend: str = "auto") -> str:
    if backend != "auto":
        return backend
    if LLMClient.credentials_available():
        return "api"
    if ClaudeCodeClient.available():
        return "cli"
    if CodexSubscriptionClient.available():
        return "codex"
    return "demo"


def backend_status() -> dict[str, Any]:
    api_ok = LLMClient.credentials_available()
    cli_ok = ClaudeCodeClient.available()
    codex_ok = CodexSubscriptionClient.available()
    return {
        "resolved": resolve_backend("auto"),
        "api": {
            "available": api_ok,
            "label": "API key",
            "help": "Anthropic API through ANTHROPIC_API_KEY. Fast, predictable, and billed per token.",
        },
        "cli": {
            "available": cli_ok,
            "label": "Claude Pro / Max subscription",
            "help": "Uses the Claude Code session with no separate API key or per-token charge; consumes subscription quota and is slower than the API.",
        },
        "codex": {
            "available": codex_ok,
            "label": "Codex subscription",
            "help": "Uses a ChatGPT/Codex subscription session created with `codex login`; no separate API key or per-token charge, but subscription quota is consumed.",
        },
        "demo": {
            "available": True,
            "label": "Demo (no API call)",
            "help": "Makes no model call. Output is composed from real slide titles and retrieved textbook excerpts to test the pipeline.",
        },
    }


def make_client(backend: str, settings) -> Any:
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

    if backend == "codex":
        codex_model = getattr(settings, "codex_model", "")
        return CodexSubscriptionClient(
            model=codex_model,
            cheap_model=codex_model,
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

    raise ValueError(f"Unknown backend: {backend}. Options: {', '.join(BACKENDS)}")
