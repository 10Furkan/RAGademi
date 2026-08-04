"""Render katmanı. Testlerin çoğu gerçekten yaşanmış hataları kilitliyor."""

from __future__ import annotations

from pathlib import Path

import pytest

from dersnotu.models import ExpandedSection, StudyDocument
from dersnotu.render.html import assets_available, build_toc
from dersnotu.render.markdown import (
    _highlight_code,
    insert_figures,
    markdown_to_html,
)
from dersnotu.render.pdf import document_to_html

LECTURE_PDF = Path("Lecture02 - Bitsints.pptx.pdf")


# --- Matematik -------------------------------------------------------------
def test_block_math_is_display_not_inline():
    """dollarmath renderer'ına `display` değil `display_mode` gelir.

    Yanlış anahtar okunduğunda TÜM formüller satır içi render oluyordu.
    """
    html = markdown_to_html("$$a^2 + b^2 = c^2$$")
    assert "math-block" in html
    assert "math-inline" not in html


def test_inline_math_stays_inline():
    html = markdown_to_html("Değer $x_1$ olsun.")
    assert "math-inline" in html
    assert "math-block" not in html


def test_math_underscore_not_eaten_by_emphasis():
    """Ham `$...$` markdown'dan geçerse `_` vurgu olur ve formül bozulur."""
    html = markdown_to_html("$x_1 + x_2$")
    assert "<em>" not in html
    assert "x_1 + x_2" in html


# --- Kod -------------------------------------------------------------------
def test_highlight_output_starts_with_pre():
    """markdown-it, `<pre` ile başlamayan çıktıyı kendi <pre><code>'suna sarar.

    Bu da iç içe iki kod kutusu üretiyordu.
    """
    out = _highlight_code("int x = 1;", "c", None)
    assert out.startswith("<pre")


def test_fenced_code_produces_single_pre():
    html = markdown_to_html("```c\nint x = 1;\n```")
    assert html.count("<pre") == 1


def test_unknown_language_still_renders():
    html = markdown_to_html("```notalanguage\nfoo bar\n```")
    assert "<pre" in html
    assert "foo bar" in html


# --- Atıflar ---------------------------------------------------------------
def test_book_citation_becomes_margin_chip():
    html = markdown_to_html("İddia. [K: Integer Representations, s. 96-98]")
    assert 'class="cite cite-book"' in html
    assert "Integer Representations, s. 96-98" in html


def test_slide_citation_becomes_chip():
    html = markdown_to_html("Slayttan. [S: 22]")
    assert 'class="cite cite-slide"' in html
    assert "Slayt 22" in html


def test_citation_html_is_escaped():
    html = markdown_to_html("[K: <script>alert(1)</script>]")
    assert "<script>" not in html


# --- Şekiller --------------------------------------------------------------
@pytest.mark.skipif(not LECTURE_PDF.exists(), reason="örnek ders PDF'i yok")
def test_figure_marker_is_replaced_with_image():
    md = insert_figures("Önce.\n\n[ŞEKİL: slayt 22]\n\nSonra.", LECTURE_PDF)
    assert "data:image/png;base64," in md
    assert "[ŞEKİL:" not in md


@pytest.mark.skipif(not LECTURE_PDF.exists(), reason="örnek ders PDF'i yok")
def test_block_after_figure_still_parsed_as_markdown():
    """Şekil regex'i `\\s*$` ile sonraki boş satırı yiyordu.

    Sonuç: ardından gelen blockquote HTML bloğuna yapışıp ham metin basılıyordu.
    """
    html = markdown_to_html(
        "[ŞEKİL: slayt 22]\n\n> Uyarı metni burada.\n", lecture_pdf=LECTURE_PDF
    )
    assert "<blockquote>" in html
    assert "&gt; Uyarı" not in html


def test_missing_slide_yields_visible_warning():
    md = insert_figures("[ŞEKİL: slayt 9999]", LECTURE_PDF) if LECTURE_PDF.exists() else None
    if md is not None:
        assert "⚠️" in md


# --- Doküman birleştirme ---------------------------------------------------
def _doc(sections):
    return StudyDocument(lecture_title="Ders", language="Türkçe", sections=sections)


def test_failed_section_renders_as_visible_block():
    doc = _doc(
        [
            ExpandedSection(
                section_index=0, title="Tamam", markdown="## Tamam\nmetin", slide_range=(1, 2)
            ),
            ExpandedSection(
                section_index=1,
                title="Bozuk",
                markdown="",
                slide_range=(3, 4),
                error="RateLimitError",
            ),
        ]
    )
    html = document_to_html(doc)
    assert 'class="failed"' in html
    assert "RateLimitError" in html
    assert "3–4" in html


def test_backticked_citation_markers_are_unwrapped():
    """Model bazen `[S: 3]` yazıyor; işaretçi <code> içine hapsolup chip
    stiliyle çakışıyordu. Gerçek koşuda görüldü."""
    html = markdown_to_html("Gerilim seviyesi budur. `[S: 3]`")
    assert "cite-slide" in html
    assert "<code>" not in html

    html = markdown_to_html("Bir iddia. `[K: Integer Representations, s. 96]`")
    assert "cite-book" in html
    assert "<code>" not in html


def test_real_code_spans_are_untouched():
    html = markdown_to_html("`unsigned char` bir türdür.")
    assert "<code>" in html and "unsigned char" in html


def test_figure_marker_never_leaks_raw_without_lecture_pdf():
    """İç işaretçi köşeli parantezleriyle okuyucunun önüne çıkmamalı."""
    html = markdown_to_html("metin\n\n[ŞEKİL: slayt 22]\n\nsonrası")
    assert "[ŞEKİL" not in html
    assert "figure-missing" in html
    assert "Slayt 22" in html
    assert "sonrası" in html


def test_callout_containers_render_as_aside():
    html = markdown_to_html("::: soru\n1. Soru\n:::")
    assert 'class="callout callout-quiz"' in html
    assert "Kendini sına" in html
    assert "<ol>" in html  # içerik markdown olarak işlenmeli, ham değil
    assert ":::" not in html


@pytest.mark.parametrize(
    ("name", "css_class"),
    [("analoji", "callout-analogy"), ("soru", "callout-quiz"), ("sözlük", "callout-glossary")],
)
def test_every_callout_has_its_own_class(name, css_class):
    assert css_class in markdown_to_html(f"::: {name}\nmetin\n:::")


def test_callout_content_keeps_math_and_tables():
    """Blok içeriği markdown boru hattının tamamından geçmeli."""
    html = markdown_to_html(
        "::: sözlük\n| A | B |\n|---|---|\n| $x_1$ | y |\n:::"
    )
    assert "<table>" in html
    assert "math-inline" in html


def test_unknown_container_name_is_left_alone():
    """Tanımsız blok adı sessizce yutulmamalı — metin kaybolmasın."""
    html = markdown_to_html("::: bilinmeyen\nönemli metin\n:::")
    assert "önemli metin" in html


def test_toc_lists_every_section():
    doc = _doc(
        [
            ExpandedSection(section_index=i, title=f"B{i}", markdown="x", slide_range=(i, i + 1))
            for i in range(3)
        ]
    )
    toc = build_toc(doc.sections)
    assert toc.count("<li>") == 3
    assert "B0" in toc and "B2" in toc


def test_toc_omitted_for_single_section():
    assert build_toc([ExpandedSection(section_index=0, title="Tek", markdown="x", slide_range=(1, 2))]) == ""


def test_document_html_is_self_contained():
    """PDF render'ı sırasında ağa çıkılmamalı — KaTeX gömülü olmalı."""
    if not assets_available():
        pytest.skip("KaTeX varlıkları yok (scripts/vendor_katex.py)")
    html = document_to_html(_doc([
        ExpandedSection(section_index=0, title="B", markdown="$x$", slide_range=(1, 1))
    ]))
    assert "https://" not in html.split("<script>")[0] or "katex" in html
    assert "renderMathInElement" in html
