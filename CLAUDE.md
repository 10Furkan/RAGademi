# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Windows PowerShell; the venv is `.venv` at the repo root.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"   # setup
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts\vendor_katex.py      # one-time: embed KaTeX
.\.venv\Scripts\python.exe -m pytest -q                 # all tests (~6s, no network)
.\.venv\Scripts\python.exe -m pytest tests/test_store.py::test_search_respects_page_range
.\.venv\Scripts\python.exe -m ruff check --fix src/ tests/ scripts/
```

CLI (`dersnotu` once installed, or `python -m dersnotu.cli`):

```powershell
dersnotu inspect  <lecture.pdf> [--text]        # slide → section segmentation
dersnotu index    <book.pdf> [--force]          # build FTS index (slow: ~25s for 1100pp)
dersnotu search   <book.pdf> "query" -n 5 --show
dersnotu estimate <lecture.pdf> <book.pdf>      # token/cost projection
dersnotu preview  <lecture.pdf> <book.pdf> -s 2 # dump the exact API request body
dersnotu render   [doc.json] --lecture <lecture.pdf> [-o out.pdf] [--html]
dersnotu build    <lecture.pdf> <book.pdf> --sections 1  # real run; add --backend codex for Codex
dersnotu retry    <doc.json> <lecture.pdf> <book.pdf>     # re-run only failed sections
dersnotu practice <lecture.pdf> <book.pdf> <exam.pdf> [-n 10] [--pdf]  # practice exam
#   depth: summary | standard | deep
#   extras: analogy | example | quiz | glossary (also accepted by `preview`)
dersnotu serve    [-p 8000] [--reload]          # web UI + API
```

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not slow" -q   # skip Chromium/PDF tests
```

`render` with no argument prints a built-in sample document — that is the way to
exercise the whole Markdown → HTML → KaTeX → PDF chain without an API key.

**`build`, `practice`, and `retry` may call a model.** Everything else runs offline — use `preview` and
`estimate` to validate request construction and cost before spending money.
`--sections N` limits a real run to the first N sections — **CLI only, and
deliberately not exposed on the web.** A truncated document is not a summary:
the missing sections are simply absent, and the "tamamla" button does not bring
them back (it only re-runs sections that *errored*). On the web the same
worry — spending before you know the settings are right — is answered by the
pre-flight estimate strip and demo mode, neither of which produces a document
the user might mistake for complete.

### Authentication backends

`build --backend` / the web form pick between them; `llm/factory.py` is the only
place that decides.

| | auth | cost |
|---|---|---|
| `api` | `ANTHROPIC_API_KEY` | per token |
| `cli` | Claude Pro/Max login via the `claude` binary | subscription quota, no per-token charge |
| `codex` | ChatGPT subscription via `codex login` | subscription quota, no per-token charge |
| `demo` | none | free, `FakeLLMClient` |

`auto` resolves api → cli → codex → demo, so the app always starts.

On Windows, the Codex adapter resolves its executable from
`RAGADEMI_CODEX_PATH`, `PATH`, or the newest OpenAI VS Code/VS Code Insiders
extension bundle. Do not assume that a bare `codex` command is available in the
PowerShell session that starts the web server.

## Architecture

Detailed architecture and measured decisions live in [docs/architecture.md](docs/architecture.md). Read it before changing retrieval, prompts, caching, figures, persistence, rendering, exams or retry behavior.

## Conventions

- Comments, docstrings, CLI output, prompts, and user-facing text are English.
- Book indexes are content-addressed (`.cache/book-<sha16>.sqlite`) so the same
  textbook is indexed once and shared across lectures.
- Prompts live in `llm/prompts.py`. The system prompt is **constant** — never
  interpolate variables into it (breaks the cache prefix); variable content goes
  into the message body after the breakpoint.
- FTS5 queries must go through `_to_fts_query`: raw punctuation is operator syntax
  there, so `two's complement` raises without escaping.
