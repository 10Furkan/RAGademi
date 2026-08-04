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
dersnotu build    <lecture.pdf> <book.pdf> --sections 1   # real run (needs credentials)
dersnotu retry    <doc.json> <lecture.pdf> <book.pdf>     # re-run only failed sections
#   --lang English --depth derin -e analoji -e soru -e sözlük
#   depth: özet | standart | derin · extras also accepted by `preview`
dersnotu serve    [-p 8000] [--reload]          # web UI + API
```

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not slow" -q   # skip Chromium/PDF tests
```

`render` with no argument prints a built-in sample document — that is the way to
exercise the whole Markdown → HTML → KaTeX → PDF chain without an API key.

**Only `build` calls a model.** Everything else runs offline — use `preview` and
`estimate` to validate request construction and cost before spending money.
`--sections N` limits a real run to the first N sections.

### Two auth backends

`build --backend` / the web form pick between them; `llm/factory.py` is the only
place that decides.

| | auth | cost |
|---|---|---|
| `api` | `ANTHROPIC_API_KEY` | per token |
| `cli` | Claude Pro/Max login via the `claude` binary | subscription quota, no per-token charge |
| `demo` | none | free, `FakeLLMClient` |

`auto` resolves api → cli → demo, so the app always starts.

## Architecture

Pipeline: `pipeline.run()` orchestrates six stages, each independently testable.

```
parse_lecture ─┐
               ├─ build_topic_cards (1 cheap call, ALL sections at once)
load_book_index┤
               ├─ align_to_book (1 call) → per-section page range
               └─ per section: retrieve → expand_section (bulk of cost)
                                              ↓
                                        to_markdown
```

### The asymmetry that drives everything

The lecture is small and visual (51 slides, 16K chars, mostly diagrams); the book
is huge and textual (1105 pages, ~600K tokens). So they get opposite treatment:

- **Lecture** goes to the model nearly whole — full text always, plus rendered PNGs
  for slides detected as diagram-bearing.
- **Book never goes whole.** It is chunked and indexed locally (SQLite FTS5); only
  retrieved excerpts reach the model.

### Non-obvious invariants

**Slide titles are not the first line.** Some slides put code or diagram labels
above the title. `_extract_title` picks the largest font size via pdfplumber word
attrs, then joins vertically-adjacent lines at that size.

**`page.images` does not detect diagrams.** PowerPoint shapes are vector drawings,
so image-heavy slides report 0 embedded rasters. Detection uses vector object count
(`rects + lines + curves`); the template baseline is ~6, real diagrams are 40+.
`visual_shape_threshold` (default 20) is the knob.

**Section boundaries come from repeated agenda slides.** CSAPP-style decks re-show
the "Today: …" slide before each topic. `_mark_dividers` flags slides whose body
text repeats ≥2× in the deck, or whose title matches an agenda pattern. Falls back
to fixed-size grouping when no dividers exist.

**Prompt caching barely helps this workload — do not "optimize" it further.**
The only content shared across section calls is the lecture text (~5K tokens).
Slide images are *not* cached because each image is needed by exactly one section;
putting them in the cached prefix roughly doubles their cost. Measured: $0.76/lecture
with caching vs $0.82 without. `test_cached_prefix_is_byte_stable` guards the prefix
against timestamps/counters leaking in and silently killing the cache.

**Image tokens dominate cost** (~57K of ~110K input tokens), and scale with *area*:
`slide_image_max_edge` halved → image cost quartered. `estimate` prints this tradeoff.

**Haiku 4.5 rejects `thinking` and `effort`.** `supports_thinking()` gates them;
sending either to Haiku returns 400. Same for `temperature`/`top_p`/`budget_tokens`
on any current model — never add them.

**Section failures must stay visible.** `expand_section` catches exceptions and
returns `ExpandedSection(error=...)`; `to_markdown` renders those as ⚠️ blocks
rather than silently dropping content. This is what makes a mid-run network
outage survivable — the run continues and only the affected sections are lost.

**`retry_failed` must never worsen an existing document.** It re-expands only
`doc.failed` and merges *only the ones that succeeded* back in; a section that
fails again keeps its previous record. `StudyDocument` therefore carries
`cards`/`alignments` — without them a retry would need two more model calls and
could produce a section framed inconsistently with its siblings. Documents
written before that field existed still work: the cards are rebuilt (2 cheap
calls) and stored, so the *next* retry is free. `tests/test_retry.py` locks the
never-worsen contract.

**Never filter chunks by prose ratio — scrub the artifact instead.** Figures leak
into page text as prose (hello.c's ASCII dump: `#includeSP <stdio. 35 105 110 99`;
cache diagram bit axes: `12 11 10 9 … 0`; chapter-opening TOC blocks). The obvious
fix — drop low-prose chunks — is wrong: CSAPP's *code listings* score lowest on
every prose metric and are the book's most valuable content. `scrub_artifacts()`
cuts only the artifact and leaves the chunk. Thresholds are measured, not guessed:
byte runs need 12+ consecutive numbers (real struct-offset tables reach 9), and
glued `SP` markers are only stripped when a page has 3+ (a lone `SP` is the stack
pointer). Total removed across 1105 pages: 0.28% of text. `tests/test_scrub.py`
locks both directions — what must be cut *and* what must survive.

**`INDEX_VERSION` must be bumped on any chunker or index-schema change.** The
index filename is the book's SHA, but its *contents* are the chunker's output.
Without the version stamp in `meta`, editing the chunker silently reuses a stale
index. There are **two** places that build an index — `pipeline.load_book_index`
and the `index` CLI command. Both must stay in sync: if the CLI builds an index
without figures, it is marked `complete` and the pipeline will never scan them.

### Book figures (`pdfio/figures.py`)

Figures are cropped out of the book and placed in the output; they are **never
sent to the model** (image tokens already dominate cost). The model is given a
list of available figures and emits `[KŞEKİL: 6.5]`; the render layer does the
crop. Same division of labour as `[ŞEKİL: slayt N]`.

**pdfplumber and pypdfium2 do not share a coordinate system, and the offset is
`(cropbox.x0, cropbox.y0)` — measured, not derived.** pdfplumber reports
MediaBox coordinates (612×792 here); pypdfium2 renders the **CropBox**
(470.9×578.9), and content overflowing the bitmap renders as a **black bar**.
The geometrically "obvious" vertical offset `page.height - cropbox.y1` (97.92)
is **wrong** — it shifts every crop down by about one text line, silently
clipping the figure's top row. The correct value is `cropbox.y0` (115.2).

This was settled by measurement, and that method is the thing to reuse: build the
bitmap's per-row ink profile, slide pdfplumber's line boxes over it, and take the
offset with maximum overlap. Three pages agreed on dy≈114-115; the wrong formula
scored ~60% of the right one. `test_crop_offset_is_cropbox_lower_left` and
`test_crop_is_aligned_with_the_figure_not_shifted` lock it. **If crops look
shifted by a line, check `_crop_offset` first.**

`BookFigure.bbox` is stored **CropBox-relative** so the conversion happens once,
at detection — which also means changing it requires an `INDEX_VERSION` bump.
When a crop looks wrong, overlay the boxes on a full-page render.

**`extract_words()` defaults glue words together.** Inter-word gaps in this font
are ~2.57pt, under the 3.0 default `x_tolerance`, producing
`Readingthecontentsofa`. `_X_TOL = 1.5` fixes it.

**CSAPP puts captions in the left margin, level with the figure top** — not
below it. Code that only looks below finds nothing. `_caption_for` handles both,
and the `beside` branch has to filter out the figure's own labels sharing those
lines.

**A caption is required, and that is also the filter.** Tables produce dense
drawing clusters too (horizontal rules); requiring a `Figure N.M` caption rejects
them, and the model can only reference a figure whose number it knows.

**Font size separates a caption from its surroundings.** Caption 9pt, body 10pt,
in-figure labels 6-8pt. Without the size filter, continuation-line collection
swallowed labels (`CPU`) and the following paragraph. Size alone is not enough
for the *crop box*, though: the running header is 8.47pt and passed the filter,
dragging the crop to the top of the page. Hence the `body_top = 120` guard —
measured, headers sit at 103.0-112.5 and the first real content at 126.8+.

**A caption-continuation line is identified by its LEFT edge, not its right.**
Ruling out "lines ending before the caption's right edge" also removed the
figure's own labels — on p.92 the `2³ = 8` label ends inside the caption
column's width and vanished from the crop.

**Include the book's caption in the crop.** Excluding it sounds tidier but the
caption column and the figure's labels interleave, so the box ends up cutting
text mid-word. The rendered `<figcaption>` is therefore deliberately terse
(source + number + page) — `BookFigure.caption` exists to tell the *model* what
the figure shows, not to be reprinted under it.

Measured on CSAPP: 252 unique figures from 1105 pages in ~95s. Most misses are
not diagrams — CSAPP numbers code listings as figures too, and those already
reach the model as text.

**Depth/extras go after the cache breakpoint, never into the system prompt.**
`DEPTHS` / `EXTRAS` live in `llm/prompts.py` and are appended by
`build_output_directives()` into the section request body. That is what lets two
different depth settings share the same cached prefix. `test_modes.py` asserts
both that the selections reach the prompt and that they stay out of the prefix.

### Claude Code CLI backend (`llm/cli_client.py`)

All four of these were measured, none are documented, and three fail silently
or expensively.

**A bare `claude -p` call loads the whole agent harness.** A 2-token prompt cost
**40,915** input tokens — Claude Code's own system prompt, tool definitions,
CLAUDE.md and settings. `_SLIM_FLAGS` (`--tools "" --setting-sources ""
--strict-mcp-config --disable-slash-commands --no-session-persistence`) brings
it to **196**. Over an 8-section lecture that is ~325K tokens of pure waste.

**`cache_control` must be stripped from content blocks.** The CLI injects its
own 1-hour-TTL cache block; ours (5 min) then violates the ordering rule and the
API returns `400 ... cache_control.ttl`. Caching is Claude Code's business here.

**`--input-format stream-json` forces `--output-format stream-json --verbose`.**
That combination is also the *only* way to send images: base64 blocks go in as
NDJSON on stdin. Verified against a real slide — the model read its title back.

**Structured output arrives out-of-band.** `--json-schema` is implemented as a
`StructuredOutput` tool, so the text channel emits only whitespace and the
parsed object lands in `result.structured_output`. Reading the text channel with
a bare `or` treated `" "` as valid and blew up in `json.loads`. `CallResult.
structured` carries it; `complete_json` prefers it.

**Subscription quota surfaces as `rate_limit_event`** (`five_hour` window,
`resetsAt`). There is no API equivalent — read it from the stream.

Expect it to be **slower than the API** (one real section: 189s vs a few
seconds), because each call is a fresh subprocess with its own model turn.

### Render layer traps (all cost real debugging time — tests lock them in)

**`dollarmath`'s renderer key is `display_mode`, not `display`.** Reading the
wrong key silently makes every equation inline.

**markdown-it wraps highlight output that doesn't start with `<pre`.** Pygments'
default `<div class="code"><pre>` therefore became `<pre><code><div…><pre>` —
two nested code boxes. `_highlight_code` uses `nowrap=True` and emits its own
`<pre class="code">`.

**Regex `\s*$` eats the following blank line.** The `[ŞEKİL: slayt N]` pattern
used it, so the next block glued onto the figure's HTML block and rendered as
raw text. Use `[ \t]*$` and pad the replacement with newlines.

**Page numbers come from Playwright's `footer_template`, not CSS `@page`.**
Having both prints two numbering systems on top of each other.

**`::: soru` / `::: analoji` / `::: sözlük` are markdown-it containers**
(`_CALLOUTS` in `render/markdown.py`). Container names are Turkish to match the
prompts; CSS classes are ASCII. An unregistered name falls through as plain text
rather than being swallowed — `test_unknown_container_name_is_left_alone` guards
that, because silently eating a block would lose content.

**`text-transform: uppercase` is locale-sensitive.** The page is `lang="tr"`, so
uppercasing the English word "Figure" yields "FİGURE" (dotted capital I) and
mangles the book's own label. Anything quoted from an English source needs
`lang="en"`, or no transform at all.

**Internal markers must never reach the reader.** `[ŞEKİL: slayt N]` is replaced
by the slide image when a lecture PDF is available; without one,
`strip_figure_markers` turns it into a styled note. Leaving the raw bracket form
in the output was a real bug found by rasterizing the PDF and looking at it.

### Web layer traps

**The highlighter inverts in dark mode.** `linear-gradient(transparent 58%,
--marker 58%)` puts a bright yellow band under text that has *flipped to light*
under `prefers-color-scheme: dark` — light-on-yellow is unreadable. The dark
override converts the band to a full-height translucent tint plus a marker
underline. It must come *after* the base rules; equal specificity means source
order decides.

**Verify UI changes by screenshotting both color schemes** (`page.emulate_media(
color_scheme=...)`). Also note Playwright's `check()` fails on visually-hidden
inputs — click the wrapping `<label>`, which is the real user path anyway.

### Web layer

`api/server.py` (FastAPI) + `api/jobs.py` (`JobStore`) + a single static page.
The pipeline is blocking, so jobs run in a `ThreadPoolExecutor` and events cross
into asyncio via `loop.call_soon_threadsafe`. Progress reaches the browser over
SSE at `/api/jobs/{id}/events`.

`JobStore` is deliberately four methods (`create/get/publish/subscribe`) so
swapping in Redis + ARQ means rewriting that file, not its callers.

**`store` is a module-level singleton and lifespan can run more than once**
(`uvicorn --reload`, consecutive `TestClient`s). The pool is therefore created
lazily and `shutdown()` sets it back to `None` — otherwise the second lifespan
hits "cannot schedule new futures after shutdown".

`section:delta` events are streamed but **not** appended to job history; at
hundreds per second they would balloon memory.

**`FakeLLMClient` (`llm/fake.py`) runs the whole pipeline with no API key** —
demo checkbox in the UI, `demo=true` on the API. It composes its output from
*real* slide titles and *real* retrieved book chunks, so a retrieval regression
shows up in demo output too. Use it for any orchestration work.

## Conventions

- Comments, docstrings, CLI output and prompts are Turkish; code identifiers English.
- Book indexes are content-addressed (`.cache/book-<sha16>.sqlite`) so the same
  textbook is indexed once and shared across lectures.
- Prompts live in `llm/prompts.py`. The system prompt is **constant** — never
  interpolate variables into it (breaks the cache prefix); variable content goes
  into the message body after the breakpoint.
- FTS5 queries must go through `_to_fts_query`: raw punctuation is operator syntax
  there, so `two's complement` raises without escaping.
