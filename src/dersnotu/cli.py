"""Komut satırı arayüzü.

API anahtarı gerektirmeyen komutlar (inspect / index / search / estimate)
boru hattının LLM dışı tamamını doğrulamayı sağlar.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import settings
from .index import BookIndex, chunk_book, estimate_tokens
from .llm.client import PRICING, LLMClient
from .llm.prompts import build_lecture_context
from .pdfio import parse_book, parse_lecture, read_pages, sha256_file
from .pdfio.render import render_pages
from .pipeline import Inputs, run, save_debug, to_markdown

app = typer.Typer(add_completion=False, help="Ders slaytlarını anlaşılır ders notuna çevirir.")
console = Console()


# ---------------------------------------------------------------------------
@app.command()
def inspect(
    lecture: Path = typer.Argument(..., exists=True, help="Ders slaytı PDF'i"),
    show_text: bool = typer.Option(False, "--text", help="Slayt metinlerini de yaz"),
) -> None:
    """Ders PDF'ini ayrıştır ve bölüm yapısını göster (API gerekmez)."""
    lec = parse_lecture(
        lecture,
        shape_threshold=settings.visual_shape_threshold,
        max_section_slides=settings.max_section_slides,
    )
    console.print(f"[bold]{lec.title}[/bold]")
    console.print(
        f"{len(lec.slides)} slayt · {len(lec.sections)} bölüm · "
        f"{sum(1 for s in lec.slides if s.is_visual)} görsel slayt\n"
    )
    for sec in lec.sections:
        a, b = sec.slide_range
        table = Table(
            title=f"Bölüm {sec.index} — slayt {a}-{b}", show_header=True, header_style="dim"
        )
        table.add_column("#", justify="right", width=4)
        table.add_column("Başlık")
        table.add_column("Görsel", width=7)
        table.add_column("Şekil", justify="right", width=6)
        for s in sec.slides:
            table.add_row(str(s.number), s.title, "✔" if s.is_visual else "", str(s.shape_count))
        console.print(table)
        if show_text:
            console.print(sec.raw_text, style="dim")
        console.print()


# ---------------------------------------------------------------------------
@app.command()
def index(
    book: Path = typer.Argument(..., exists=True, help="Ders kitabı PDF'i"),
    force: bool = typer.Option(False, "--force", help="Önbelleği yok say, yeniden indeksle"),
) -> None:
    """Kitabı indeksle (API gerekmez). Aynı kitap bir kez indekslenir."""
    settings.ensure_dirs()
    sha = sha256_file(book)
    if not force and BookIndex.is_built(settings.cache_dir, sha):
        idx = BookIndex(BookIndex.path_for(settings.cache_dir, sha))
        console.print(f"[green]Indeks zaten var[/green] — {idx.count()} chunk")
        idx.close()
        raise typer.Exit()

    with console.status("Sayfalar okunuyor…"):
        pages = read_pages(book)
    bk, pages = parse_book(book, pages)
    console.print(f"{bk.page_count} sayfa · {len(bk.sections)} TOC bölümü")

    with console.status("Chunk'lanıyor…"):
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

        with console.status("Şekiller taranıyor…"):
            figures = scan_figures(book)

    idx = BookIndex(BookIndex.path_for(settings.cache_dir, sha))
    idx.build(bk, chunks, figures)
    total = sum(c.token_estimate for c in chunks)
    console.print(
        f"[green]{len(chunks)} chunk indekslendi[/green] · ~{total:,} token · "
        f"{len(figures)} şekil · {BookIndex.path_for(settings.cache_dir, sha).name}"
    )
    idx.close()


# ---------------------------------------------------------------------------
@app.command()
def search(
    book: Path = typer.Argument(..., exists=True),
    query: str = typer.Argument(..., help="Arama sorgusu"),
    limit: int = typer.Option(5, "--limit", "-n"),
    show: bool = typer.Option(False, "--show", help="Chunk metnini de yaz"),
) -> None:
    """İndekste arama yap (API gerekmez) — retrieval kalitesini test etmek için."""
    sha = sha256_file(book)
    if not BookIndex.is_built(settings.cache_dir, sha):
        console.print("[red]İndeks yok.[/red] Önce: dersnotu index <kitap.pdf>")
        raise typer.Exit(code=1)
    idx = BookIndex(BookIndex.path_for(settings.cache_dir, sha))
    hits = idx.search(query, limit=limit)
    if not hits:
        console.print("[yellow]Sonuç yok[/yellow]")
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
    model: str = typer.Option(None, "--model", help="Varsayılan: ayarlardaki model"),
) -> None:
    """Token ve maliyet tahmini yap — hiç API çağrısı yapmadan."""
    model = model or settings.model
    lec = parse_lecture(
        lecture,
        shape_threshold=settings.visual_shape_threshold,
        max_section_slides=settings.max_section_slides,
    )
    sha = sha256_file(book)
    if not BookIndex.is_built(settings.cache_dir, sha):
        console.print("[red]Önce kitabı indeksle:[/red] dersnotu index <kitap.pdf>")
        raise typer.Exit(code=1)
    idx = BookIndex(BookIndex.path_for(settings.cache_dir, sha))

    n_sections = len(lec.sections)
    prefix_tokens = estimate_tokens(build_lecture_context(lec)) + 400  # +sistem promptu

    # Görüntü tokenları: sabit varsayım yerine gerçekten render edilmiş
    # piksel boyutundan hesaplanır (token ≈ genişlik×yükseklik/750).
    visual = [s.number for s in lec.slides if s.is_visual]
    sample = visual[:3]
    px = (
        render_pages(lecture, sample, max_edge=settings.slide_image_max_edge)
        if sample
        else {}
    )
    img_tokens_each = (
        sum(p.token_estimate for p in px.values()) / len(px) if px else 0
    )
    image_tokens = len(visual) * img_tokens_each

    # Bölüm başına değişken metin: slayt metni + alıntılar
    avg_chunk = idx.conn.execute(
        "SELECT AVG(token_estimate) FROM chunks"
    ).fetchone()[0] or 900
    per_section_text = sum(
        estimate_tokens(s.raw_text) for s in lec.sections
    ) / max(1, n_sections)
    retrieval_tokens = settings.chunks_per_section * float(avg_chunk) * n_sections
    section_text_tokens = per_section_text * n_sections

    cache_write = prefix_tokens
    cache_read = prefix_tokens * max(0, n_sections - 1)
    plain_input = image_tokens + retrieval_tokens + section_text_tokens
    output_tokens = n_sections * 3000  # bölüm başına ~2000 kelime

    # Ucuz geçişler (konu kartları + hizalama)
    cheap_input = estimate_tokens(build_lecture_context(lec)) + 8000
    cheap_output = 4000

    inp_price, out_price = PRICING.get(model, (3.0, 15.0))
    cheap_in, cheap_out = PRICING.get(settings.cheap_model, (1.0, 5.0))

    cost = (
        cache_write * inp_price * 1.25
        + cache_read * inp_price * 0.10
        + plain_input * inp_price
        + output_tokens * out_price
        + cheap_input * cheap_in
        + cheap_output * cheap_out
    ) / 1_000_000

    no_cache_cost = (
        (prefix_tokens * n_sections + plain_input) * inp_price + output_tokens * out_price
    ) / 1_000_000

    table = Table(title=f"Maliyet tahmini — {model}", show_header=True)
    table.add_column("Kalem")
    table.add_column("Token", justify="right")
    table.add_row("Bölüm sayısı", str(n_sections))
    table.add_row("Cache'lenen önek (1× yazma)", f"{cache_write:,.0f}")
    table.add_row(f"Cache okuma ({n_sections - 1}×)", f"{cache_read:,.0f}")
    table.add_row(
        f"Slayt görüntüleri ({len(visual)} adet × {img_tokens_each:,.0f})",
        f"{image_tokens:,.0f}",
    )
    table.add_row("Kitap alıntıları", f"{retrieval_tokens:,.0f}")
    table.add_row("Bölüm slayt metinleri", f"{section_text_tokens:,.0f}")
    table.add_row("Çıktı", f"{output_tokens:,.0f}")
    table.add_row("Ucuz geçişler (giriş/çıkış)", f"{cheap_input:,.0f} / {cheap_output:,.0f}")
    console.print(table)
    console.print(f"\n[bold green]Tahmini maliyet: ${cost:.2f}[/bold green] / ders")
    console.print(f"[dim]Prompt caching olmasaydı: ${no_cache_cost:.2f}[/dim]")
    if px:
        s = next(iter(px.values()))
        avg_kb = sum(len(p.png) for p in px.values()) / len(px) / 1024
        console.print(
            f"[dim]Örnek slayt render: {s.width}×{s.height}px, {avg_kb:.0f} KB "
            f"→ ~{img_tokens_each:,.0f} token/slayt[/dim]"
        )
        halved = (
            len(visual) * (s.width // 2) * (s.height // 2) / 750 * inp_price / 1_000_000
        )
        current = image_tokens * inp_price / 1_000_000
        console.print(
            f"[dim]Uzun kenar 700px'e inseydi görüntü maliyeti "
            f"${current:.2f} → ${halved:.2f}[/dim]"
        )
    idx.close()


# ---------------------------------------------------------------------------
@app.command()
def preview(
    lecture: Path = typer.Argument(..., exists=True),
    book: Path = typer.Argument(..., exists=True),
    section: int = typer.Option(0, "--section", "-s", help="Önizlenecek bölüm indeksi"),
    language: str = typer.Option("Türkçe", "--lang", "-l"),
    depth: str = typer.Option("standart", "--depth", "-d", help="özet | standart | derin"),
    extra: list[str] = typer.Option([], "--extra", "-e", help="analoji | örnek | soru | sözlük"),
    dump: Path = typer.Option(None, "--dump", help="İstek gövdesini bu dosyaya yaz"),
) -> None:
    """Bir bölümün API istek gövdesini kur ve göster — çağrı yapmadan.

    Cache kırılma noktasının yerini, blok sırasını ve token dağılımını
    para harcamadan denetlemeyi sağlar.
    """
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
        console.print(f"[red]Bölüm {section} yok.[/red] 0-{len(lec.sections) - 1} arası ver.")
        raise typer.Exit(code=1)

    sha = sha256_file(book)
    if not BookIndex.is_built(settings.cache_dir, sha):
        console.print("[red]Önce kitabı indeksle:[/red] dersnotu index <kitap.pdf>")
        raise typer.Exit(code=1)
    idx = BookIndex(BookIndex.path_for(settings.cache_dir, sha))

    sec = lec.sections[section]
    card = TopicCard(
        section_index=section,
        title=sec.slides[0].title if sec.slides else f"Bölüm {section}",
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
            content.append({"type": "text", "text": f"[Slayt {n} görüntüsü]"})
            content.append(to_image_block(rendered[n]))
    content.append(
        {
            "type": "text",
            "text": build_section_request(
                sec, card, chunks, language, depth=depth, extras=list(extra)
            ),
        }
    )

    table = Table(title=f"İstek gövdesi — bölüm {section}", show_header=True)
    table.add_column("#", justify="right", width=3)
    table.add_column("Tip", width=8)
    table.add_column("Cache", width=6)
    table.add_column("~Token", justify="right", width=9)
    table.add_column("Özet")

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
        f"[bold]Toplam giriş: ~{total:,} token[/bold]"
    )
    console.print(
        f"Alıntı: {len(chunks)} · Görsel slayt: {len(visual)} · "
        f"Cache kırılma noktası: blok {[i for i, b in enumerate(content) if 'cache_control' in b]}"
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
                        else {**b, "source": {**b["source"], "data": "<base64 kısaltıldı>"}}
                        for b in content
                    ],
                }
            ],
        }
        dump.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]İstek gövdesi yazıldı:[/green] {dump}")
    idx.close()


# ---------------------------------------------------------------------------
@app.command()
def render(
    doc_json: Path = typer.Argument(None, help="build çıktısı .json (yoksa örnek kullanılır)"),
    lecture: Path = typer.Option(None, "--lecture", help="Şekilleri çekmek için ders PDF'i"),
    book: Path = typer.Option(None, "--book", help="Kitap şekillerini kırpmak için kitap PDF'i"),
    out: Path = typer.Option(None, "--out", "-o"),
    html_only: bool = typer.Option(False, "--html", help="PDF yerine HTML yaz"),
) -> None:
    """Markdown dokümanını PDF'e bas (API gerekmez).

    `doc_json` verilmezse yerleşik örnek doküman kullanılır — render hattını
    API anahtarı olmadan uçtan uca doğrulamak için.
    """
    import json as _json

    from .models import StudyDocument
    from .render import RenderError, document_to_html, html_to_pdf
    from .render.sample import sample_document

    if doc_json:
        doc = StudyDocument(**_json.loads(doc_json.read_text(encoding="utf-8")))
        default_name = doc_json.stem
    else:
        doc = sample_document()
        default_name = "ornek-ders-notu"
        console.print("[dim]Örnek doküman kullanılıyor (doc_json verilmedi).[/dim]")

    settings.ensure_dirs()
    out = out or settings.out_dir / f"{default_name}.{'html' if html_only else 'pdf'}"
    out.parent.mkdir(parents=True, exist_ok=True)

    html = document_to_html(doc, lecture_pdf=lecture, book_pdf=book)
    if html_only:
        out.write_text(html, encoding="utf-8")
        console.print(f"[green]HTML yazıldı:[/green] {out} ({len(html) / 1024:.0f} KB)")
        raise typer.Exit()

    try:
        with console.status("Chromium ile basılıyor…"):
            html_to_pdf(html, out)
    except RenderError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    size = out.stat().st_size / 1024
    console.print(f"[green]PDF yazıldı:[/green] {out} ({size:.0f} KB)")


# ---------------------------------------------------------------------------
@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Web arayüzünü ve API'yi başlat."""
    import uvicorn

    console.print(f"[bold]dersnotu[/bold] → http://{host}:{port}")
    if not LLMClient.credentials_available():
        console.print("[yellow]API anahtarı yok — arayüz demo moduna geçecek.[/yellow]")
    uvicorn.run("dersnotu.api.server:app", host=host, port=port, reload=reload)


# ---------------------------------------------------------------------------
@app.command()
def retry(
    doc_json: Path = typer.Argument(..., exists=True, help="build çıktısı .json"),
    lecture: Path = typer.Argument(..., exists=True),
    book: Path = typer.Argument(..., exists=True),
    backend: str = typer.Option("auto", "--backend", "-b"),
    out: Path = typer.Option(None, "--out", "-o", help="Çıktı .md yolu"),
) -> None:
    """Yalnızca hata almış bölümleri yeniden üret (başarılılara dokunmaz)."""
    import json as _json

    from .llm import BACKENDS, resolve_backend
    from .models import StudyDocument
    from .pipeline import retry_failed

    if backend not in BACKENDS:
        console.print(f"[red]Geçersiz --backend:[/red] {backend} · {', '.join(BACKENDS)}")
        raise typer.Exit(code=1)

    doc = StudyDocument(**_json.loads(doc_json.read_text(encoding="utf-8")))
    hatalilar = doc.failed
    if not hatalilar:
        console.print("[green]Bu dokümanda hatalı bölüm yok.[/green]")
        raise typer.Exit()

    console.print(
        f"[yellow]{len(hatalilar)} hatalı bölüm:[/yellow] "
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
        f"\n[bold green]Yazıldı:[/bold green] {out}\n"
        + (f"[yellow]{kalan} bölüm hâlâ hatalı.[/yellow]" if kalan else "[green]Tüm bölümler tamam.[/green]")
    )


# ---------------------------------------------------------------------------
@app.command()
def build(
    lecture: Path = typer.Argument(..., exists=True),
    book: Path = typer.Argument(..., exists=True),
    out: Path = typer.Option(None, "--out", "-o", help="Çıktı .md yolu"),
    language: str = typer.Option("Türkçe", "--lang", "-l"),
    depth: str = typer.Option("standart", "--depth", "-d", help="özet | standart | derin"),
    extra: list[str] = typer.Option(
        [], "--extra", "-e", help="analoji | örnek | soru | sözlük (birden çok verilebilir)"
    ),
    backend: str = typer.Option(
        "auto", "--backend", "-b",
        help="auto | api (anahtar) | cli (Claude Pro/Max aboneliği) | demo",
    ),
    model: str = typer.Option(None, "--model"),
    sections: int = typer.Option(None, "--sections", help="İlk N bölümü işle (test için)"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Üretimi canlı yaz"),
) -> None:
    """Ders notunu üret. Kimlik: API anahtarı veya Claude Pro aboneliği."""
    from .llm import BACKENDS, resolve_backend
    from .llm.prompts import DEPTHS, EXTRAS

    if backend not in BACKENDS:
        console.print(f"[red]Geçersiz --backend:[/red] {backend} · {', '.join(BACKENDS)}")
        raise typer.Exit(code=1)
    if depth not in DEPTHS:
        console.print(f"[red]Geçersiz derinlik:[/red] {depth} · seçenekler: {', '.join(DEPTHS)}")
        raise typer.Exit(code=1)
    if bad := [e for e in extra if e not in EXTRAS]:
        console.print(
            f"[red]Geçersiz --extra:[/red] {', '.join(bad)} · seçenekler: {', '.join(EXTRAS)}"
        )
        raise typer.Exit(code=1)

    chosen = resolve_backend(backend)
    if chosen == "demo" and backend == "auto":
        console.print(
            "[red]Hiçbir kimlik bulunamadı.[/red]\n"
            "  API için:        ANTHROPIC_API_KEY ortam değişkenini ayarla\n"
            "  Claude Pro için: Claude Code kur ve `claude` ile giriş yap "
            "(sonra --backend cli)\n"
            "  Denemek için:    --backend demo\n\n"
            "Kimliksiz çalışanlar: [bold]inspect / index / search / estimate / preview[/bold]"
        )
        raise typer.Exit(code=1)
    console.print(f"[dim]arka uç: [bold]{chosen}[/bold][/dim]")

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
        f"\n[bold green]Yazıldı:[/bold green] {out}\n"
        f"Çağrı: {u.calls} · giriş {u.input_tokens:,} · çıkış {u.output_tokens:,} · "
        f"cache yazma {u.cache_creation_tokens:,} · cache okuma {u.cache_read_tokens:,}\n"
        f"Tahmini maliyet: [bold]${estimate_cost(u, settings.model):.3f}[/bold]"
    )
    failed = [s for s in doc.sections if s.error]
    if failed:
        console.print(f"[yellow]{len(failed)} bölüm üretilemedi[/yellow]")


if __name__ == "__main__":
    app()
