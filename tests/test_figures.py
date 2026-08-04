"""Kitap şekillerinin tespiti, kırpılması ve çıktıya yerleşmesi.

Koordinat testleri gerçek kitap üzerinde koşar (`-m slow`): bu katmandaki
hataların hepsi ancak gerçek bir PDF'te ortaya çıkıyor — MediaBox/CropBox
kayması, yapışık kelimeler, sol kenardaki altyazı.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dersnotu.index.store import BookIndex
from dersnotu.models import Book, BookChunk, BookFigure, ExpandedSection, StudyDocument
from dersnotu.pipeline import resolve_referenced_figures, retrieve_figures
from dersnotu.render.markdown import markdown_to_html

BOOK_PDF = Path("CSAPP_2016.pdf")


def make_index(tmp_path: Path, figures: list[BookFigure]) -> BookIndex:
    idx = BookIndex(tmp_path / "t.sqlite")
    book = Book(
        source_path="b.pdf", source_sha256="x", title="T", page_count=100, sections=[]
    )
    chunks = [
        BookChunk(
            chunk_id="c1", text="metin", section_title="S",
            page_start=40, page_end=45, token_estimate=10,
        )
    ]
    idx.build(book, chunks, figures)
    return idx


def fig(number: str, page: int, *, w: float = 200, h: float = 100) -> BookFigure:
    return BookFigure(number=number, caption=f"altyazı {number}", page=page,
                      bbox=(10.0, 10.0, 10.0 + w, 10.0 + h))


# --- İndeks --------------------------------------------------------------
def test_figures_round_trip_through_index(tmp_path):
    idx = make_index(tmp_path, [fig("1.4", 42), fig("6.5", 613)])
    assert idx.figure_count() == 2
    got = idx.figure("6.5")
    assert got is not None and got.page == 613
    assert got.caption == "altyazı 6.5"
    assert idx.figure("yok") is None
    idx.close()


def test_figures_for_pages_filters_by_range(tmp_path):
    idx = make_index(tmp_path, [fig("1.4", 42), fig("6.5", 613)])
    assert [f.number for f in idx.figures_for_pages(40, 50)] == ["1.4"]
    assert idx.figures_for_pages(700, 800) == []
    idx.close()


def test_index_version_invalidates_stale_cache(tmp_path, monkeypatch):
    """Chunker/şema değişince eski indeks sessizce kullanılmamalı."""
    from dersnotu.index import store

    idx = make_index(tmp_path, [])
    idx.close()
    sha = "0" * 64
    (tmp_path / f"book-{sha[:16]}.sqlite").write_bytes((tmp_path / "t.sqlite").read_bytes())
    assert BookIndex.is_built(tmp_path, sha)
    monkeypatch.setattr(store, "INDEX_VERSION", "999")
    assert not BookIndex.is_built(tmp_path, sha)


# --- Seçim ---------------------------------------------------------------
def test_retrieve_figures_prefers_larger_ones(tmp_path):
    """Küçük çizimler genelde süs; büyük olanlar asıl anlatım."""
    small, big = fig("1.1", 42, w=60, h=40), fig("1.2", 43, w=400, h=300)
    idx = make_index(tmp_path, [small, big])
    chunks = [
        BookChunk(chunk_id="c", text="t", section_title="S",
                  page_start=40, page_end=45, token_estimate=1)
    ]
    assert [f.number for f in retrieve_figures(idx, chunks, limit=2)] == ["1.2", "1.1"]
    idx.close()


def test_retrieve_figures_respects_limit(tmp_path):
    idx = make_index(tmp_path, [fig(f"1.{i}", 42) for i in range(10)])
    chunks = [
        BookChunk(chunk_id="c", text="t", section_title="S",
                  page_start=42, page_end=42, token_estimate=1)
    ]
    assert len(retrieve_figures(idx, chunks, limit=3)) == 3
    idx.close()


def test_only_referenced_figures_are_collected(tmp_path):
    idx = make_index(tmp_path, [fig("1.4", 42), fig("6.5", 613)])
    sections = [
        ExpandedSection(section_index=0, title="A", slide_range=(1, 2),
                        markdown="metin\n\n[KŞEKİL: 6.5]\n"),
        ExpandedSection(section_index=1, title="B", slide_range=(3, 4),
                        markdown="şekil çağırmayan bölüm"),
    ]
    assert [f.number for f in resolve_referenced_figures(idx, sections)] == ["6.5"]
    idx.close()


def test_hallucinated_figure_number_is_not_resolved(tmp_path):
    """Model listede olmayan numara uydurursa sessizce eşleşmemeli."""
    idx = make_index(tmp_path, [fig("1.4", 42)])
    sections = [
        ExpandedSection(section_index=0, title="A", slide_range=(1, 2),
                        markdown="[KŞEKİL: 99.9]")
    ]
    assert resolve_referenced_figures(idx, sections) == []
    idx.close()


# --- Render --------------------------------------------------------------
def test_book_figure_marker_never_leaks_raw():
    html = markdown_to_html("önce\n\n[KŞEKİL: 6.5]\n\nsonra")
    assert "[KŞEKİL" not in html
    assert "figure-missing" in html
    assert "sonra" in html


def test_unknown_figure_becomes_visible_note():
    """Kırpılamayan şekil sessizce kaybolmamalı."""
    html = markdown_to_html(
        "[KŞEKİL: 9.9]", book_pdf="yok.pdf", figures=[fig("1.4", 42)]
    )
    assert "9.9" in html and "figure-missing" in html


def test_block_after_book_figure_still_parsed_as_markdown():
    """`\\s*$` sonraki boş satırı yer ve blok figüre yapışırdı."""
    html = markdown_to_html("[KŞEKİL: 1.4]\n\n> uyarı metni")
    assert "<blockquote>" in html


# --- Gerçek kitap --------------------------------------------------------
@pytest.mark.skipif(not BOOK_PDF.exists(), reason="kitap PDF'i yok")
@pytest.mark.slow
def test_real_figure_detection_and_crop():
    import pdfplumber

    from dersnotu.pdfio.figures import crop_figure, find_figures

    with pdfplumber.open(BOOK_PDF) as pdf:
        figs = find_figures(pdf.pages[612])  # s.613 — Figure 6.5

    assert len(figs) == 1, [f.number for f in figs]
    f = figs[0]
    assert f.number == "6.5"
    # Yapışık kelime hatası: x_tolerance=3.0 "Readingthecontentsofa" veriyordu.
    assert "Reading the contents" in f.caption
    # CropBox-göreli kutu sayfa sınırları içinde kalmalı (MediaBox olsaydı taşardı).
    assert 0 <= f.bbox[0] < f.bbox[2] <= 471
    assert 0 <= f.bbox[1] < f.bbox[3] <= 579

    png = crop_figure(BOOK_PDF, f, max_edge=600)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 5_000  # boş/siyah kırpma bu boyuta ulaşmaz


@pytest.mark.skipif(not BOOK_PDF.exists(), reason="kitap PDF'i yok")
@pytest.mark.slow
def test_crop_offset_is_cropbox_lower_left():
    """Kayma ÖLÇÜLDÜ: CropBox'ın sol-alt köşesi.

    "Doğal" görünen `page.height - cropbox.y1` kırpımı tam bir satır aşağı
    kaydırıyor ve şeklin üst satırı kesiliyordu. Bu testin düşmesi, kırpımların
    kaymaya başladığı anlamına gelir.
    """
    import pdfplumber

    from dersnotu.pdfio.figures import _crop_offset

    with pdfplumber.open(BOOK_PDF) as pdf:
        page = pdf.pages[91]
        cb = page.cropbox
        assert _crop_offset(page) == (cb[0], cb[1])
        # Yanlış formülden gerçekten farklı olmalı, yoksa test hiçbir şey demiyor.
        assert cb[1] != page.height - cb[3]


@pytest.mark.skipif(not BOOK_PDF.exists(), reason="kitap PDF'i yok")
@pytest.mark.slow
def test_crop_is_aligned_with_the_figure_not_shifted():
    """Kırpım gerçekten şeklin üstüne mi oturuyor?

    Doğru kırpım mürekkep yoğun; bir satır kaymış kırpım sayfa boşluğuna
    taşar ve belirgin biçimde beyazlaşır. Bunu ölçerek kilitliyoruz.
    """
    import pdfplumber
    from PIL import Image

    from dersnotu.pdfio.figures import crop_figure, find_figures

    with pdfplumber.open(BOOK_PDF) as pdf:
        fig = find_figures(pdf.pages[91])[0]  # s.92 — Figure 2.12

    import io

    img = Image.open(io.BytesIO(crop_figure(BOOK_PDF, fig, max_edge=600))).convert("L")
    px = img.load()
    dark = sum(
        1
        for y in range(0, img.height, 2)
        for x in range(0, img.width, 2)
        if px[x, y] < 200
    )
    total = (img.height // 2 + 1) * (img.width // 2 + 1)
    assert dark / total > 0.05, f"kırpım fazla boş: {dark / total:.3f}"


@pytest.mark.skipif(not BOOK_PDF.exists(), reason="kitap PDF'i yok")
@pytest.mark.slow
def test_table_without_caption_is_not_a_figure():
    """Tablolar da yoğun çizim üretir; altyazı şartı onları eler."""
    import pdfplumber

    from dersnotu.pdfio.figures import find_figures

    with pdfplumber.open(BOOK_PDF) as pdf:
        page = pdf.pages[612]
        objs = len(page.rects) + len(page.lines) + len(page.curves)
        figs = find_figures(page)

    assert objs > 100  # sayfada iki çizim kümesi var (şekil + tablo)
    assert len(figs) == 1  # ama yalnızca altyazılı olan şekil sayılmalı


@pytest.mark.skipif(not BOOK_PDF.exists(), reason="kitap PDF'i yok")
@pytest.mark.slow
def test_document_renders_real_book_figure():
    import pdfplumber

    from dersnotu.pdfio.figures import find_figures
    from dersnotu.render.pdf import document_to_html

    with pdfplumber.open(BOOK_PDF) as pdf:
        found = find_figures(pdf.pages[612])[0]

    doc = StudyDocument(
        lecture_title="T",
        language="Türkçe",
        sections=[
            ExpandedSection(section_index=0, title="A", slide_range=(1, 2),
                            markdown="## A\n\n[KŞEKİL: 6.5]\n")
        ],
        figures=[found],
    )
    html = document_to_html(doc, book_pdf=BOOK_PDF)
    assert "book-figure" in html
    assert "data:image/png;base64," in html
    assert "Figure 6.5" in html
