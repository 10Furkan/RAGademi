"""Command-line interface for local inspection and document generation."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import settings
from .estimate import project
from .index import BookIndex, chunk_book, estimate_tokens
from .llm.client import LLMClient
from .pdfio import parse_book, parse_lecture, read_pages, sha256_file
from .pipeline import Inputs, run, save_debug, to_markdown


def _konsolu_dayanikli_yap() -> None:
    """Yazdırılamayan bir karakter komutu ÖLDÜRMESİN.

    Türkçe Windows'ta konsol kod sayfası cp1254 olabiliyor. Türkçe harfler
    (ş ğ ı İ) orada var — sorun onlar değil; `→` oku ve rich'in tablo çizgileri
    (─ │ ┌) YOK. Varsayılan `errors="strict"` ile bu, `UnicodeEncodeError`
    demek ve komut hiç iş yapmadan çöküyor: `serve` başlangıç satırını
    basamadığı için sunucu HİÇ ayağa kalkmıyordu, `inspect` tabloyu çizerken
    ölüyordu.

    Akışın kendi kodlamasına dokunmuyoruz — cp1254'ü utf-8'e çevirmek Türkçe
    metni okunmaz hâle getirirdi, oku kaybetmekten çok daha kötü. Yalnızca
    hata kipi gevşetiliyor: yazdırılamayan karakter `?` olur, komut yaşar.
    """
    for akis in (sys.stdout, sys.stderr):
        try:
            akis.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # yeniden yönlendirilmiş ya da desteklemeyen akış


_konsolu_dayanikli_yap()

app = typer.Typer(add_completion=False, help="Turn lecture slides into source-grounded study notes.")
console = Console()


# ---------------------------------------------------------------------------
@app.command()
def inspect(
    lecture: Path = typer.Argument(..., exists=True, help="Lecture-slide PDF"),
    show_text: bool = typer.Option(False, "--text", help="Also print slide text"),
) -> None:
    """Parse a lecture PDF and display its section structure (no API required)."""
    lec = parse_lecture(
        lecture,
        shape_threshold=settings.visual_shape_threshold,
        max_section_slides=settings.max_section_slides,
    )
    console.print(f"[bold]{lec.title}[/bold]")
    console.print(
        f"{len(lec.slides)} slides · {len(lec.sections)} sections · "
        f"{sum(1 for s in lec.slides if s.is_visual)} visual slides\n"
    )
    for sec in lec.sections:
        a, b = sec.slide_range
        table = Table(
            title=f"Section {sec.index} — slides {a}-{b}", show_header=True, header_style="dim"
        )
        table.add_column("#", justify="right", width=4)
        table.add_column("Title")
        table.add_column("Visual", width=7)
        table.add_column("Shapes", justify="right", width=6)
        for s in sec.slides:
            table.add_row(str(s.number), s.title, "✔" if s.is_visual else "", str(s.shape_count))
        console.print(table)
        if show_text:
            console.print(sec.raw_text, style="dim")
        console.print()


# ---------------------------------------------------------------------------
@app.command()
def index(
    book: Path = typer.Argument(..., exists=True, help="Textbook PDF"),
    force: bool = typer.Option(False, "--force", help="Ignore the cache and rebuild the index"),
) -> None:
    """Index a textbook locally; identical books are indexed only once."""
    settings.ensure_dirs()
    sha = sha256_file(book)
    if not force and BookIndex.is_built(settings.cache_dir, sha):
        idx = BookIndex(BookIndex.path_for(settings.cache_dir, sha))
        console.print(f"[green]Index already exists[/green] — {idx.count()} chunks")
        idx.close()
        raise typer.Exit()

    with console.status("Sayfalar okunuyor…"):
        pages = read_pages(book)
    bk, pages = parse_book(book, pages)
    console.print(f"{bk.page_count} pages · {len(bk.sections)} TOC sections")

    with console.status("Chunking…"):
        chunks = chunk_book(
            bk,
            pages,
            target_tokens=settings.chunk_target_tokens,
            overlap_ratio=settings.chunk_overlap_ratio,
        )
    # Şekiller de burada taranmalı: aksi halde CLI'nın kurduğu indeks "tam"
    # işaretlenir, boru hattı onu hazır bulur ve şekil ASLA çıkarılmaz.
    figures = []
    if settings.extract_book_figures:
        from .pdfio import scan_figures

        with console.status("Scanning figures…"):
            figures = scan_figures(book)

    idx = BookIndex(BookIndex.path_for(settings.cache_dir, sha))
    idx.build(bk, chunks, figures)
    total = sum(c.token_estimate for c in chunks)
    console.print(
        f"[green]{len(chunks)} chunk indekslendi[/green] · ~{total:,} token · "
        f"{len(figures)} figures · {BookIndex.path_for(settings.cache_dir, sha).name}"
    )
    idx.close()


# ---------------------------------------------------------------------------
@app.command()
def search(
    book: Path = typer.Argument(..., exists=True),
    query: str = typer.Argument(..., help="Arama sorgusu"),
    limit: int = typer.Option(5, "--limit", "-n"),
    show: bool = typer.Option(False, "--show", help="Also print chunk text"),
) -> None:
    """Search an index locally to inspect retrieval quality."""
    sha = sha256_file(book)
    if not BookIndex.is_built(settings.cache_dir, sha):
        console.print("[red]Index not found.[/red] Run: dersnotu index <book.pdf>")
        raise typer.Exit(code=1)
    idx = BookIndex(BookIndex.path_for(settings.cache_dir, sha))
    hits = idx.search(query, limit=limit)
    if not hits:
        console.print("[yellow]No results[/yellow]")
    for c, score in hits:
        console.print(f"[bold]{score:7.2f}[/bold]  {c.citation}")
        if show:
            console.print(c.text[:600] + "…", style="dim")
            console.print()
    idx.close()


# ---------------------------------------------------------------------------
@app.command()
def estimate(
    lecture: Path = typer.Argument(..., exists=True),
    book: Path = typer.Argument(..., exists=True),
    model: str = typer.Option(None, "--model", help="Default: model from settings"),
) -> None:
    """Estimate tokens, cost, and duration without making an API call."""
    model = model or settings.model
    lec = parse_lecture(
        lecture,
        shape_threshold=settings.visual_shape_threshold,
        max_section_slides=settings.max_section_slides,
    )
    sha = sha256_file(book)
    if not BookIndex.is_built(settings.cache_dir, sha):
        console.print("[red]Index the textbook first:[/red] dersnotu index <book.pdf>")
        raise typer.Exit(code=1)

    # Hesabın tamamı `estimate.py`'de; burada yalnızca sunum var. Arayüzdeki
    # şerit de aynı fonksiyonu çağırıyor, iki rakam ayrışamaz.
    p = project(lec, lecture, settings, book_sha=sha, model=model)

    table = Table(title=f"Maliyet tahmini — {model}", show_header=True)
    table.add_column("Kalem")
    table.add_column("Token", justify="right")
    table.add_row("Sections", str(p.sections))
    table.add_row("Cached prefix (1× write)", f"{p.cache_write:,.0f}")
    table.add_row(f"Cache okuma ({p.sections - 1}×)", f"{p.cache_read:,.0f}")
    table.add_row(
        f"Slide images ({p.visual_slides} × {p.image_tokens_each:,.0f})",
        f"{p.image_tokens:,.0f}",
    )
    table.add_row("Textbook excerpts", f"{p.retrieval_tokens:,.0f}")
    table.add_row("Section slide text", f"{p.section_text_tokens:,.0f}")
    table.add_row("Output", f"{p.output_tokens:,.0f}")
    table.add_row(
        "Inexpensive passes (input/output)", f"{p.cheap_input:,.0f} / {p.cheap_output:,.0f}"
    )
    console.print(table)
    console.print(f"\n[bold green]Estimated cost: ${p.cost:.2f}[/bold green] / lecture")
    console.print(f"[dim]Without prompt caching: ${p.no_cache_cost:.2f}[/dim]")
    console.print(f"[dim]Estimated duration: ~{p.seconds / 60:.0f} min ({p.seconds_source})[/dim]")
    if s := p.sample:
        console.print(
            f"[dim]Sample slide render: {s['width']}×{s['height']}px, {s['kb']} KB "
            f"→ ~{s['tokens_each']:,} tokens/slide[/dim]"
        )
        console.print(
            f"[dim]Halving the long edge would change image cost to "
            f"${s['image_cost']:.2f} → ${s['halved_image_cost']:.2f}[/dim]"
        )


# ---------------------------------------------------------------------------
@app.command()
def preview(
    lecture: Path = typer.Argument(..., exists=True),
    book: Path = typer.Argument(..., exists=True),
    section: int = typer.Option(0, "--section", "-s", help="Section index to preview"),
    language: str = typer.Option("English", "--lang", "-l"),
    depth: str = typer.Option("standard", "--depth", "-d", help="summary | standard | deep"),
    extra: list[str] = typer.Option([], "--extra", "-e", help="analogy | example | quiz | glossary"),
    dump: Path = typer.Option(None, "--dump", help="Write the request body to this file"),
) -> None:
    """Build and display one section's model request without making a call."""
    import json

    from .llm.prompts import EXPAND_SYSTEM, build_section_request
    from .models import SectionAlignment, TopicCard
    from .pdfio.render import render_pages, to_image_block
    from .pipeline import build_cached_prefix, retrieve

    lec = parse_lecture(
        lecture,
        shape_threshold=settings.visual_shape_threshold,
        max_section_slides=settings.max_section_slides,
    )
    if not 0 <= section < len(lec.sections):
        console.print(f"[red]Section {section} does not exist.[/red] Use 0-{len(lec.sections) - 1}.")
        raise typer.Exit(code=1)

    sha = sha256_file(book)
    if not BookIndex.is_built(settings.cache_dir, sha):
        console.print("[red]Index the textbook first:[/red] dersnotu index <book.pdf>")
        raise typer.Exit(code=1)
    idx = BookIndex(BookIndex.path_for(settings.cache_dir, sha))

    sec = lec.sections[section]
    card = TopicCard(
        section_index=section,
        title=sec.slides[0].title if sec.slides else f"Section {section}",
        key_terms=[t for t in sec.titles[:8]],
    )
    chunks = retrieve(
        idx, card, SectionAlignment(section_index=section), limit=settings.chunks_per_section
    )

    content: list[dict] = list(build_cached_prefix(lec))
    visual = [s.number for s in sec.slides if s.is_visual]
    rendered = render_pages(lecture, visual, max_edge=settings.slide_image_max_edge)
    for n in visual:
        if n in rendered:
            content.append({"type": "text", "text": f"[Image of slide {n}]"})
            content.append(to_image_block(rendered[n]))
    content.append(
        {
            "type": "text",
            "text": build_section_request(
                sec, card, chunks, language, depth=depth, extras=list(extra)
            ),
        }
    )

    table = Table(title=f"Request body — section {section}", show_header=True)
    table.add_column("#", justify="right", width=3)
    table.add_column("Tip", width=8)
    table.add_column("Cache", width=6)
    table.add_column("~Token", justify="right", width=9)
    table.add_column("Summary")

    total = estimate_tokens(EXPAND_SYSTEM)
    for i, block in enumerate(content):
        is_cached = "◆" if "cache_control" in block else ""
        if block["type"] == "text":
            tok = estimate_tokens(block["text"])
            summary = block["text"][:60].replace("\n", " ")
        else:
            page = rendered[visual[(i - 1) // 2]] if visual else None
            tok = page.token_estimate if page else 0
            summary = f"PNG {page.width}×{page.height}" if page else "PNG"
        total += tok
        table.add_row(str(i), block["type"], is_cached, f"{tok:,}", summary)

    console.print(table)
    console.print(
        f"\nSistem promptu: ~{estimate_tokens(EXPAND_SYSTEM):,} token · "
        f"[bold]Total input: ~{total:,} tokens[/bold]"
    )
    console.print(
        f"Excerpts: {len(chunks)} · Visual slides: {len(visual)} · "
        f"Cache breakpoint: block {[i for i, b in enumerate(content) if 'cache_control' in b]}"
    )

    if dump:
        payload = {
            "model": settings.model,
            "system": EXPAND_SYSTEM,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": settings.effort},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        b
                        if b["type"] == "text"
                        else {**b, "source": {**b["source"], "data": "<base64 omitted>"}}
                        for b in content
                    ],
                }
            ],
        }
        dump.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]Request body written:[/green] {dump}")
    idx.close()


# ---------------------------------------------------------------------------
@app.command()
def render(
    doc_json: Path = typer.Argument(None, help="build output .json (uses sample when omitted)"),
    lecture: Path = typer.Option(None, "--lecture", help="Lecture PDF used to insert slide figures"),
    book: Path = typer.Option(None, "--book", help="Textbook PDF used to crop textbook figures"),
    out: Path = typer.Option(None, "--out", "-o"),
    html_only: bool = typer.Option(False, "--html", help="Write HTML instead of PDF"),
) -> None:
    """Render a Markdown document to PDF locally."""
    import json as _json

    from .models import StudyDocument
    from .render import RenderError, document_to_html, html_to_pdf
    from .render.sample import sample_document

    if doc_json:
        doc = StudyDocument(**_json.loads(doc_json.read_text(encoding="utf-8")))
        default_name = doc_json.stem
    else:
        doc = sample_document()
        default_name = "sample-study-notes"
        console.print("[dim]Using the built-in sample because doc_json was omitted.[/dim]")

    settings.ensure_dirs()
    out = out or settings.out_dir / f"{default_name}.{'html' if html_only else 'pdf'}"
    out.parent.mkdir(parents=True, exist_ok=True)

    html = document_to_html(doc, lecture_pdf=lecture, book_pdf=book)
    if html_only:
        out.write_text(html, encoding="utf-8")
        console.print(f"[green]HTML written:[/green] {out} ({len(html) / 1024:.0f} KB)")
        raise typer.Exit()

    try:
        with console.status("Rendering with Chromium…"):
            html_to_pdf(html, out)
    except RenderError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    size = out.stat().st_size / 1024
    console.print(f"[green]PDF written:[/green] {out} ({size:.0f} KB)")


# ---------------------------------------------------------------------------
@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Start the web interface and API."""
    import uvicorn

    console.print(f"[bold]dersnotu[/bold] → http://{host}:{port}")
    if not LLMClient.credentials_available():
        console.print("[yellow]No API key found — the interface will use demo mode.[/yellow]")
    uvicorn.run("dersnotu.api.server:app", host=host, port=port, reload=reload)


# ---------------------------------------------------------------------------
@app.command()
def retry(
    doc_json: Path = typer.Argument(..., exists=True, help="build output .json"),
    lecture: Path = typer.Argument(..., exists=True),
    book: Path = typer.Argument(..., exists=True),
    backend: str = typer.Option("auto", "--backend", "-b"),
    out: Path = typer.Option(None, "--out", "-o", help="Output .md path"),
) -> None:
    """Regenerate only failed sections without changing successful ones."""
    import json as _json

    from .llm import BACKENDS, resolve_backend
    from .models import StudyDocument
    from .pipeline import retry_failed

    if backend not in BACKENDS:
        console.print(f"[red]Invalid --backend:[/red] {backend} · {', '.join(BACKENDS)}")
        raise typer.Exit(code=1)

    doc = StudyDocument(**_json.loads(doc_json.read_text(encoding="utf-8")))
    hatalilar = doc.failed
    if not hatalilar:
        console.print("[green]This document has no failed sections.[/green]")
        raise typer.Exit()

    console.print(
        f"[yellow]{len(hatalilar)} failed sections:[/yellow] "
        + ", ".join(f"B{s.section_index} «{s.title}»" for s in hatalilar)
    )

    def progress(event: str, detail: str = "") -> None:
        color = {"section:start": "cyan", "section:done": "green"}.get(event, "dim")
        console.print(f"[{color}]{event}[/{color}] {detail}")

    def on_delta(_i: int, text: str) -> None:
        console.file.write(text)
        console.file.flush()

    yeni = retry_failed(
        doc,
        Inputs(
            lecture_path=lecture,
            book_path=book,
            language=doc.language,
            depth=doc.depth,
            extras=doc.extras,
            backend=resolve_backend(backend),
        ),
        settings,
        progress=progress,
        on_delta=on_delta,
    )

    out = out or doc_json.with_suffix(".md")
    out.write_text(to_markdown(yeni), encoding="utf-8")
    save_debug(yeni, doc_json)  # dokümanı yerinde güncelle
    kalan = len(yeni.failed)
    console.print(
        f"\n[bold green]Written:[/bold green] {out}\n"
        + (f"[yellow]{kalan} sections still failed.[/yellow]" if kalan else "[green]All sections are complete.[/green]")
    )


# ---------------------------------------------------------------------------
@app.command()
def practice(
    lecture: Path = typer.Argument(..., exists=True, help="Lecture-slide PDF"),
    book: Path = typer.Argument(..., exists=True, help="Textbook PDF"),
    exam: Path = typer.Argument(..., exists=True, help="Past-exam PDF"),
    out: Path = typer.Option(None, "--out", "-o", help="Output .md path"),
    language: str = typer.Option("English", "--lang", "-l"),
    count: int = typer.Option(
        0, "--count", "-n", help="Question count (0 matches the past paper)"
    ),
    backend: str = typer.Option("auto", "--backend", "-b"),
    pdf: bool = typer.Option(False, "--pdf", help="Render a PDF alongside Markdown"),
) -> None:
    """Generate a new practice exam modeled on a past paper."""
    from .llm import BACKENDS, resolve_backend
    from .practice import PracticeError, PracticeInputs
    from .practice import generate as uret
    from .practice import to_markdown as sinav_markdown

    if backend not in BACKENDS:
        console.print(f"[red]Invalid --backend:[/red] {backend} · {', '.join(BACKENDS)}")
        raise typer.Exit(code=1)

    chosen = resolve_backend(backend)
    if chosen == "demo" and backend == "auto":
        console.print(
            "[red]No model credentials were found.[/red] Try --backend demo."
        )
        raise typer.Exit(code=1)
    console.print(f"[dim]backend: [bold]{chosen}[/bold][/dim]")

    def progress(event: str, detail: str = "") -> None:
        color = {"questions:start": "cyan", "questions:done": "green"}.get(event, "dim")
        console.print(f"[{color}]{event}[/{color}] {detail}")

    try:
        sinav = uret(
            PracticeInputs(
                lecture_path=lecture, book_path=book, exam_path=exam,
                language=language, count=count, backend=chosen,
            ),
            settings,
            progress=progress,
        )
    except PracticeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    out = out or settings.out_dir / f"{lecture.stem}-deneme.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(sinav_markdown(sinav), encoding="utf-8")

    from .practice import save_debug as sinav_debug

    sinav_debug(sinav, out.with_suffix(".json"))

    dayanakli = len(sinav.grounded)
    console.print(
        f"\n[bold green]Written:[/bold green] {out}\n"
        f"{len(sinav.questions)} questions · {sinav.total_points} points · "
        f"{sinav.duration_minutes or '?'} dakika\n"
        f"[dim]{dayanakli}/{len(sinav.questions)} questions are grounded in a "
        "quoted question from the past paper.[/dim]"
    )
    # Kâğıda dayanmayan soru bir hata değil ama kullanıcının bilmesi gereken
    # bir şey: o sorular kapsamdan üretilmiş, "benzer" iddiası taşımıyor.
    if dayanakli < len(sinav.questions):
        console.print(
            f"[yellow]{len(sinav.questions) - dayanakli} questions could not be tied "
            "to a past-paper question[/yellow] — they were generated from slide scope."
        )

    if pdf:
        from .render import RenderError, render_practice

        hedef = out.with_suffix(".pdf")
        try:
            with console.status("Rendering with Chromium…"):
                render_practice(sinav, hedef)
        except RenderError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]PDF written:[/green] {hedef}")


# ---------------------------------------------------------------------------
@app.command()
def build(
    lecture: Path = typer.Argument(..., exists=True),
    book: Path = typer.Argument(..., exists=True),
    out: Path = typer.Option(None, "--out", "-o", help="Output .md path"),
    language: str = typer.Option("English", "--lang", "-l"),
    depth: str = typer.Option("standard", "--depth", "-d", help="summary | standard | deep"),
    extra: list[str] = typer.Option(
        [], "--extra", "-e", help="analogy | example | quiz | glossary (repeatable)"
    ),
    backend: str = typer.Option(
        "auto", "--backend", "-b",
        help="auto | api | cli (Claude Pro/Max) | codex (ChatGPT/Codex) | demo",
    ),
    model: str = typer.Option(None, "--model"),
    sections: int = typer.Option(None, "--sections", help="Process only the first N sections (testing)"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream generated text live"),
) -> None:
    """Generate study notes through an API, Claude subscription, or Codex subscription."""
    from .llm import BACKENDS, resolve_backend
    from .llm.prompts import (
        DEPTHS,
        EXTRAS,
        LEGACY_DEPTHS,
        LEGACY_EXTRAS,
        normalize_depth,
        normalize_extras,
    )

    if backend not in BACKENDS:
        console.print(f"[red]Invalid --backend:[/red] {backend} · {', '.join(BACKENDS)}")
        raise typer.Exit(code=1)
    if depth not in DEPTHS and depth not in LEGACY_DEPTHS:
        console.print(f"[red]Invalid depth:[/red] {depth} · options: {', '.join(DEPTHS)}")
        raise typer.Exit(code=1)
    if bad := [e for e in extra if e not in EXTRAS and e not in LEGACY_EXTRAS]:
        console.print(
            f"[red]Invalid --extra:[/red] {', '.join(bad)} · options: {', '.join(EXTRAS)}"
        )
        raise typer.Exit(code=1)

    depth = normalize_depth(depth)
    extra = normalize_extras(extra)
    chosen = resolve_backend(backend)
    if chosen == "demo" and backend == "auto":
        console.print(
            "[red]No model credentials were found.[/red]\n"
            "  API:        set ANTHROPIC_API_KEY\n"
            "  Claude Pro: install Claude Code, sign in with `claude`, then use --backend cli\n"
            "  Codex:      run `codex login`, then use --backend codex\n"
            "  Try it:     --backend demo\n\n"
            "Credential-free commands: [bold]inspect / index / search / estimate / preview[/bold]"
        )
        raise typer.Exit(code=1)
    console.print(f"[dim]backend: [bold]{chosen}[/bold][/dim]")

    if model:
        settings.model = model
    settings.ensure_dirs()

    def progress(event: str, detail: str = "") -> None:
        color = {"section:start": "cyan", "section:done": "green"}.get(event, "dim")
        console.print(f"[{color}]{event}[/{color}] {detail}")

    on_delta = None
    if stream:
        def on_delta(_idx: int, text: str) -> None:  # noqa: F811
            console.file.write(text)
            console.file.flush()

    doc = run(
        Inputs(
            lecture_path=lecture,
            book_path=book,
            language=language,
            depth=depth,
            extras=list(extra),
            backend=chosen,
        ),
        settings,
        progress=progress,
        limit_sections=sections,
        on_delta=on_delta,
    )

    out = out or settings.out_dir / f"{lecture.stem}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_markdown(doc), encoding="utf-8")
    save_debug(doc, out.with_suffix(".json"))

    from .llm.client import estimate_cost

    u = doc.usage
    console.print(
        f"\n[bold green]Written:[/bold green] {out}\n"
        f"Calls: {u.calls} · input {u.input_tokens:,} · output {u.output_tokens:,} · "
        f"cache writes {u.cache_creation_tokens:,} · cache reads {u.cache_read_tokens:,}\n"
        f"Estimated cost: [bold]${estimate_cost(u, settings.model):.3f}[/bold]"
    )
    failed = [s for s in doc.sections if s.error]
    if failed:
        console.print(f"[yellow]{len(failed)} sections could not be generated[/yellow]")


if __name__ == "__main__":
    app()
