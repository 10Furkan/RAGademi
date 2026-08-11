"""Boru hattı orkestrasyonu.

Akış:
    ders parse ──┐
                 ├─ konu kartları (ucuz model, tek çağrı)
    kitap index ─┤
                 ├─ TOC hizalama (tek çağrı)  → bölüm başına sayfa aralığı
                 └─ bölüm başına: retrieval → genişletme (asıl maliyet)
                                                    ↓
                                              Markdown birleştirme

Her adım ayrı; ara çıktılar diske yazılabilir. Bir bölüm patlarsa doküman
ölmez — o bölüm hata notuyla işaretlenir ve kalanlar devam eder.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .index import BookIndex, chunk_book
from .llm import LLMClient, RefusalError, cached, make_client, resolve_backend
from .llm.prompts import (
    ALIGN_SYSTEM,
    EXPAND_SYSTEM,
    TOPIC_SYSTEM,
    build_lecture_context,
    build_section_request,
)
from .models import (
    Book,
    BookChunk,
    BookFigure,
    ExpandedSection,
    Lecture,
    LectureSection,
    SectionAlignment,
    StudyDocument,
    TopicCard,
    Usage,
)
from .pdfio import (
    parse_book,
    parse_lecture,
    read_pages,
    scan_figures,
    sha256_file,
    toc_outline,
)
from .pdfio.render import render_pages, to_image_block

Progress = Callable[[str, str], None]  # (event, detail)


def _noop(event: str, detail: str = "") -> None:
    pass


TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section_index": {"type": "integer"},
                    "title": {"type": "string"},
                    "key_terms": {"type": "array", "items": {"type": "string"}},
                    "formulas": {"type": "array", "items": {"type": "string"}},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["section_index", "title", "key_terms", "formulas", "gaps"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}

ALIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "alignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section_index": {"type": "integer"},
                    "book_sections": {"type": "array", "items": {"type": "string"}},
                    "page_start": {"type": "integer"},
                    "page_end": {"type": "integer"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": [
                    "section_index",
                    "book_sections",
                    "page_start",
                    "page_end",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["alignments"],
    "additionalProperties": False,
}


@dataclass
class Inputs:
    lecture_path: Path
    book_path: Path
    language: str = "English"
    depth: str = "standard"  # summary | standard | deep
    # analogy | example | quiz | glossary — multiple selections allowed
    extras: list[str] = field(default_factory=list)
    # auto | api | cli (Claude Pro/Max) | codex (ChatGPT/Codex) | demo
    backend: str = "auto"
    # Geçmiş sınav kâğıdı (isteğe bağlı). Kapsamı DEĞİŞTİRMEZ; slaytta zaten
    # olan bir konunun ne kadar derin işleneceğini kaydırır.
    exam_path: Path | None = None


# ---------------------------------------------------------------------------
# 1-2. Girdi hazırlama
# ---------------------------------------------------------------------------
def load_lecture(inputs: Inputs, settings: Settings, progress: Progress = _noop) -> Lecture:
    progress("lecture:start", str(inputs.lecture_path))
    lec = parse_lecture(
        inputs.lecture_path,
        shape_threshold=settings.visual_shape_threshold,
        max_section_slides=settings.max_section_slides,
    )
    progress(
        "lecture:done",
        f"{len(lec.slides)} slides, {len(lec.sections)} sections, "
        f"{sum(1 for s in lec.slides if s.is_visual)} visual",
    )
    return lec


def load_book_index(
    inputs: Inputs, settings: Settings, progress: Progress = _noop
) -> tuple[Book, BookIndex]:
    """Kitabı indeksler. Aynı kitap daha önce indekslendiyse doğrudan kullanır."""
    sha = sha256_file(inputs.book_path)
    idx_path = BookIndex.path_for(settings.cache_dir, sha)

    if BookIndex.is_built(settings.cache_dir, sha):
        idx = BookIndex(idx_path)
        book = idx.book()
        if book is not None:
            progress("book:cached", f"{idx.count()} chunks (reused existing index)")
            return book, idx
        idx.close()

    progress("book:parse", str(inputs.book_path))
    pages = read_pages(inputs.book_path)
    book, pages = parse_book(inputs.book_path, pages)
    progress("book:chunk", f"{book.page_count} pages, {len(book.sections)} TOC sections")

    chunks = chunk_book(
        book,
        pages,
        target_tokens=settings.chunk_target_tokens,
        overlap_ratio=settings.chunk_overlap_ratio,
    )

    # Şekil taraması pahalı (1105 sayfa ≈ 95 sn), ama indeksle birlikte
    # önbelleğe girdiği için kitap başına bir kez ödenir.
    figures = []
    if settings.extract_book_figures:
        progress("book:figures", "scanning figures")
        figures = scan_figures(inputs.book_path)
        progress("book:figures_done", f"found {len(figures)} figures")

    idx = BookIndex(idx_path)
    idx.build(book, chunks, figures)
    progress("book:done", f"indexed {len(chunks)} chunks and {len(figures)} figures")
    return book, idx


# ---------------------------------------------------------------------------
# 3. Konu kartları — tek ucuz çağrı, tüm bölümler birden
# ---------------------------------------------------------------------------
def build_topic_cards(
    llm: LLMClient,
    lecture: Lecture,
    progress: Progress = _noop,
    *,
    language: str = "English",
) -> list[TopicCard]:
    progress("topics:start", f"{len(lecture.sections)} sections")
    blocks = []
    for sec in lecture.sections:
        a, b = sec.slide_range
        blocks.append(
            f"## Section {sec.index} (slides {a}-{b})\n"
            + (f"Agenda context: {sec.agenda_context}\n" if sec.agenda_context else "")
            + sec.raw_text
        )
    payload = (
        f"Course: {lecture.title}\nOUTPUT LANGUAGE: {language}\n\n"
        + "\n\n".join(blocks)
        + f"\n\nProduce one card per section, exactly {len(lecture.sections)} cards."
        + f"\nWrite titles in {language}; keep key_terms in English for textbook search."
    )

    data = llm.complete_json(
        system=TOPIC_SYSTEM,
        content=[{"type": "text", "text": payload}],
        schema=TOPIC_SCHEMA,
    )
    cards = [TopicCard(**c) for c in data.get("cards", [])]
    by_index = {c.section_index: c for c in cards}
    # Model bir bölümü atlarsa boş kartla doldur — akış kırılmasın.
    out = [
        by_index.get(sec.index, TopicCard(section_index=sec.index, title=f"Section {sec.index + 1}"))
        for sec in lecture.sections
    ]
    progress("topics:done", f"{len(out)} kart")
    return out


# ---------------------------------------------------------------------------
# 4. TOC hizalama — aramayı sayfa aralığına kilitler
# ---------------------------------------------------------------------------
def align_to_book(
    llm: LLMClient,
    lecture: Lecture,
    book: Book,
    cards: list[TopicCard],
    progress: Progress = _noop,
) -> list[SectionAlignment]:
    progress("align:start", book.title)
    outline = toc_outline(book, max_level=1)
    section_list = "\n".join(
        f"Section {c.section_index}: {c.title} — key terms: {', '.join(c.key_terms[:8])}"
        for c in cards
    )
    payload = (
        f"# Lecture sections\n{section_list}\n\n"
        f"# Textbook table of contents ({book.page_count} pages)\n{outline}\n\n"
        "Return the PDF page range to search for every lecture section."
    )
    data = llm.complete_json(
        system=ALIGN_SYSTEM,
        content=[{"type": "text", "text": payload}],
        schema=ALIGN_SCHEMA,
    )
    aligns = [SectionAlignment(**a) for a in data.get("alignments", [])]
    by_index = {a.section_index: a for a in aligns}
    out = [
        by_index.get(sec.index, SectionAlignment(section_index=sec.index, confidence="low"))
        for sec in lecture.sections
    ]
    progress(
        "align:done",
        "; ".join(
            f"B{a.section_index}→s.{a.page_start}-{a.page_end}"
            for a in out
            if a.page_start
        ),
    )
    return out


# ---------------------------------------------------------------------------
# 5. Retrieval + genişletme
# ---------------------------------------------------------------------------
def retrieve(
    idx: BookIndex, card: TopicCard, align: SectionAlignment, *, limit: int
) -> list[BookChunk]:
    page_range = (
        (align.page_start, align.page_end)
        if align.page_start and align.page_end and align.confidence != "low"
        else None
    )
    hits = idx.search(card.query, limit=limit * 3, page_range=page_range)
    if not hits and page_range:
        # Aralık fazla dar kaldıysa kitabın tamamına geri düş.
        hits = idx.search(card.query, limit=limit * 3)
    return [c for c, _ in hits[:limit]]


def retrieve_figures(
    idx: BookIndex, chunks: list[BookChunk], *, limit: int = 6
) -> list[BookFigure]:
    """Getirilen alıntıların sayfalarında duran şekiller.

    Ayrı bir arama yapmıyoruz: metin zaten o sayfalardan geldiyse, oradaki
    diyagram da konuyla ilgilidir. Büyük olanlar önce — küçük çizimler genelde
    süs (ok, çerçeve), büyük olanlar asıl anlatım.
    """
    seen: dict[str, BookFigure] = {}
    for c in chunks:
        for f in idx.figures_for_pages(c.page_start, c.page_end):
            seen.setdefault(f.number, f)
    ranked = sorted(seen.values(), key=lambda f: f.area, reverse=True)
    return ranked[:limit]


def expand_section(
    llm: LLMClient,
    *,
    lecture: Lecture,
    section: LectureSection,
    card: TopicCard,
    chunks: list[BookChunk],
    cached_prefix: list[dict],
    language: str,
    depth: str = "standart",
    extras: list[str] | None = None,
    figures: list[BookFigure] | None = None,
    image_max_edge: int = 1400,
    on_delta: Callable[[str], None] | None = None,
) -> ExpandedSection:
    """Bir bölümü genişletir. Görüntüler ve alıntılar cache noktasından SONRA gelir."""
    a, b = section.slide_range
    content: list[dict] = list(cached_prefix)

    # Bu bölümün görsel slaytları. Cache'lenmezler: her görüntü yalnızca tek
    # bir bölümde gerekiyor, cache ise ancak tekrar kullanımda kazandırır.
    visual = [s.number for s in section.slides if s.is_visual]
    if visual:
        rendered = render_pages(lecture.source_path, visual, max_edge=image_max_edge)
        for n in visual:
            if n in rendered:
                content.append({"type": "text", "text": f"[Image of slide {n}]"})
                content.append(to_image_block(rendered[n]))

    content.append(
        {
            "type": "text",
            "text": build_section_request(
                section,
                card,
                chunks,
                language,
                depth=depth,
                extras=extras,
                figures=figures,
            ),
        }
    )

    try:
        result = llm.complete(system=EXPAND_SYSTEM, content=content, on_delta=on_delta)
    except RefusalError as exc:
        return ExpandedSection(
            section_index=section.index,
            title=card.title,
            markdown="",
            slide_range=(a, b),
            error=f"reddedildi: {exc.category}",
        )
    except Exception as exc:  # ağ/limit hataları bölümü öldürsün, dokümanı değil
        return ExpandedSection(
            section_index=section.index,
            title=card.title,
            markdown="",
            slide_range=(a, b),
            error=f"{type(exc).__name__}: {exc}",
        )

    return ExpandedSection(
        section_index=section.index,
        title=card.title,
        markdown=result.text.strip(),
        citations=[c.citation for c in chunks],
        slide_range=(a, b),
    )


# Sınav kâğıdı önekte taşınıyor (bölümden bölüme değişmez), ama sınırsız
# değil: 20K karakter ≈ 5K token, bir kez yazılıp her bölümde ucuza okunur.
# Sınırsız bırakılsaydı 40 sayfalık bir soru arşivi öneki üçe katlardı.
EXAM_CHAR_LIMIT = 20_000


def load_exam_text(path: Path | None) -> str:
    """Sınav kâğıdının metnini okur. Okunamıyorsa sessizce boş döner —
    sınav kâğıdı isteğe bağlı bir zenginleştirme, koşuyu düşürmemeli."""
    if not path:
        return ""
    try:
        pages = read_pages(Path(path))
    except Exception:
        return ""
    metin = "\n\n".join(p.strip() for p in pages if p and p.strip())
    return metin[:EXAM_CHAR_LIMIT]


def build_cached_prefix(
    lecture: Lecture, alignment_note: str = "", exam_text: str = ""
) -> list[dict]:
    """Tüm bölüm çağrılarında bit-bit aynı kalan önek."""
    return [
        cached({
            "type": "text",
            "text": build_lecture_context(lecture, alignment_note, exam_text),
        })
    ]


def _expand_all(
    llm: Any,
    *,
    lecture: Lecture,
    sections: list[LectureSection],
    cards: list[TopicCard],
    aligns: list[SectionAlignment],
    idx: BookIndex,
    inputs: Inputs,
    settings: Settings,
    prefix: list[dict],
    progress: Progress,
    on_delta: Callable[[int, str], None] | None,
) -> list[ExpandedSection]:
    """Verilen bölümleri sırayla genişletir. `run` ve `retry_failed` ortak yolu."""
    out: list[ExpandedSection] = []
    for sec in sections:
        card = cards[sec.index]
        chunks = retrieve(idx, card, aligns[sec.index], limit=settings.chunks_per_section)
        figures = (
            retrieve_figures(idx, chunks, limit=settings.figures_per_section)
            if settings.extract_book_figures
            else []
        )
        progress(
            "section:start",
            f"S{sec.index} «{card.title}» slides {sec.slide_range[0]}-{sec.slide_range[1]}, "
            f"{len(chunks)} excerpts, {len(figures)} figures",
        )
        delta_cb = (lambda t, i=sec.index: on_delta(i, t)) if on_delta else None
        result = expand_section(
            llm,
            lecture=lecture,
            section=sec,
            card=card,
            chunks=chunks,
            cached_prefix=prefix,
            language=inputs.language,
            depth=inputs.depth,
            extras=inputs.extras,
            figures=figures,
            image_max_edge=settings.slide_image_max_edge,
            on_delta=delta_cb,
        )
        out.append(result)
        progress(
            "section:done",
            f"B{sec.index} "
            + (f"ERROR: {result.error}" if result.error else f"{len(result.markdown)} characters"),
        )
    return out


def _alignment_note(aligns: list[SectionAlignment]) -> str:
    return "\n".join(
        f"- Section {a.section_index}: {', '.join(a.book_sections)} (p. {a.page_start}-{a.page_end})"
        for a in aligns
        if a.page_start
    )


# ---------------------------------------------------------------------------
# Uçtan uca
# ---------------------------------------------------------------------------
def run(
    inputs: Inputs,
    settings: Settings,
    *,
    progress: Progress = _noop,
    limit_sections: int | None = None,
    on_delta: Callable[[int, str], None] | None = None,
    llm: Any | None = None,
) -> StudyDocument:
    """`llm` verilirse o kullanılır (demo/test için sahte istemci enjekte edilir)."""
    settings.ensure_dirs()
    lecture = load_lecture(inputs, settings, progress)
    book, idx = load_book_index(inputs, settings, progress)

    if llm is None:
        chosen = resolve_backend(inputs.backend)
        progress("backend", chosen)
        llm = make_client(chosen, settings)

    cards = build_topic_cards(llm, lecture, progress, language=inputs.language)
    aligns = align_to_book(llm, lecture, book, cards, progress)

    exam_text = load_exam_text(inputs.exam_path)
    if exam_text:
        progress("exam:loaded", f"{len(exam_text):,} characters of exam text")
    prefix = build_cached_prefix(lecture, _alignment_note(aligns), exam_text)

    sections = lecture.sections[:limit_sections] if limit_sections else lecture.sections
    expanded = _expand_all(
        llm,
        lecture=lecture,
        sections=sections,
        cards=cards,
        aligns=aligns,
        idx=idx,
        inputs=inputs,
        settings=settings,
        prefix=prefix,
        progress=progress,
        on_delta=on_delta,
    )

    used = resolve_referenced_figures(idx, expanded)
    if used:
        progress("figures:used", ", ".join(f.number for f in used))
    idx.close()
    return StudyDocument(
        lecture_title=lecture.title,
        language=inputs.language,
        sections=expanded,
        usage=llm.usage,
        figures=used,
        cards=cards,
        alignments=aligns,
        depth=inputs.depth,
        extras=inputs.extras,
    )


def retry_failed(
    doc: StudyDocument,
    inputs: Inputs,
    settings: Settings,
    *,
    progress: Progress = _noop,
    on_delta: Callable[[int, str], None] | None = None,
    llm: Any | None = None,
) -> StudyDocument:
    """Yalnızca hata almış bölümleri yeniden üretir, başarılı olanlara dokunmaz.

    Ağ kesintisi ya da geçici bir limit yüzünden tek bölüm patladığında tüm
    dersi baştan üretmek hem zaman hem kota israfı. Konu kartları ve kitap
    hizalaması dokümanda saklandığı için burada model çağrısı yapılmaz —
    yalnızca eksik bölümler için genişletme çağrısı yapılır.

    Başarılı bölüm yine de hata alırsa dokümanda ESKİ hâli kalır; yeniden
    deneme mevcut çıktıyı asla kötüleştirmez.
    """
    hatalilar = doc.failed
    if not hatalilar:
        progress("retry:none", "no sections to retry")
        return doc

    settings.ensure_dirs()
    lecture = load_lecture(inputs, settings, progress)
    book, idx = load_book_index(inputs, settings, progress)

    if llm is None:
        chosen = resolve_backend(inputs.backend)
        progress("backend", chosen)
        llm = make_client(chosen, settings)

    cards, aligns = doc.cards, doc.alignments
    if not cards or not aligns:
        # Eski sürümle üretilmiş doküman. İki ucuz çağrıyla kartları yeniden
        # kur — yine de tüm dersi baştan üretmekten çok ucuz. Kartlar
        # deterministik olmadığı için yeni bölüm kardeşleriyle birebir aynı
        # çerçeveden gelmeyebilir; bu yüzden açıkça uyarıyoruz.
        progress(
            "retry:rebuild",
            "the document has no saved topic cards; rebuilding them "
            "(section titles may differ slightly)",
        )
        cards = build_topic_cards(llm, lecture, progress, language=inputs.language)
        aligns = align_to_book(llm, lecture, book, cards, progress)

    hedefler = {s.section_index for s in hatalilar}
    progress("retry:start", f"{len(hedefler)} sections: {', '.join(f'S{i}' for i in sorted(hedefler))}")

    by_index = {s.index: s for s in lecture.sections}
    eksik = sorted(hedefler - set(by_index))
    if eksik:
        # Ders PDF'i değişmişse bölüm indeksleri tutmaz; sessizce yanlış bölüm
        # üretmektense açıkça söyle.
        raise ValueError(
            f"The lecture PDF does not match the document: section {eksik} is missing. "
            "Make sure you supplied the same lecture file."
        )

    # Önek ilk koşudakiyle aynı kurulmalı: sınav kâğıdı atlanırsa yeniden
    # üretilen bölüm kardeşlerinden farklı bir çerçeveden çıkar.
    prefix = build_cached_prefix(
        lecture, _alignment_note(aligns), load_exam_text(inputs.exam_path)
    )
    yeniden = _expand_all(
        llm,
        lecture=lecture,
        sections=[by_index[i] for i in sorted(hedefler)],
        cards=cards,
        aligns=aligns,
        idx=idx,
        inputs=inputs,
        settings=settings,
        prefix=prefix,
        progress=progress,
        on_delta=on_delta,
    )

    # Yalnızca DÜZELENLERİ yaz; hâlâ hatalıysa eski kaydı bozma.
    duzelen = {s.section_index: s for s in yeniden if not s.error}
    birlesik = [duzelen.get(s.section_index, s) for s in doc.sections]

    used = resolve_referenced_figures(idx, birlesik)
    idx.close()

    toplam = Usage(**doc.usage.model_dump())
    toplam.add(llm.usage)
    progress(
        "retry:done",
        f"recovered {len(duzelen)}/{len(hedefler)} sections"
        + (f", {len(hedefler) - len(duzelen)} still failed" if len(duzelen) < len(hedefler) else ""),
    )
    return doc.model_copy(
        update={
            "sections": birlesik,
            "figures": used,
            "usage": toplam,
            # Yeniden kurulmuşlarsa sakla: bir sonraki deneme bedava olsun.
            "cards": cards,
            "alignments": aligns,
        }
    )


_FIGURE_REF = re.compile(r"\[(?:BOOKFIGURE|KŞEKİL):\s*([\d.]+?)\s*\]", re.IGNORECASE)


def resolve_referenced_figures(
    idx: BookIndex, sections: list[ExpandedSection]
) -> list[BookFigure]:
    """Metinde çağrılan `[KŞEKİL: N.M]` işaretçilerini gerçek şekillere bağlar.

    Model listede olmayan bir numara uydurursa burada eşleşme bulunamaz ve
    işaretçi render katmanında görünür bir nota dönüşür — sessizce kaybolmaz.
    """
    out: dict[str, BookFigure] = {}
    for sec in sections:
        for m in _FIGURE_REF.finditer(sec.markdown or ""):
            number = m.group(1).strip(".")
            if number in out:
                continue
            fig = idx.figure(number)
            if fig is not None:
                out[number] = fig
    return list(out.values())


def to_markdown(doc: StudyDocument) -> str:
    parts = [f"# {doc.lecture_title}", ""]
    for sec in doc.sections:
        if sec.error:
            parts += [
                f"## {sec.title}",
                f"> ⚠️ This section could not be generated ({sec.error}). Slides {sec.slide_range[0]}-{sec.slide_range[1]}.",
                "",
            ]
            continue
        parts += [sec.markdown, ""]
    return "\n".join(parts)


def save_debug(doc: StudyDocument, path: Path) -> None:
    path.write_text(json.dumps(doc.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
