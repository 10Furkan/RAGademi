"""Markdown → HTML.

Üç özel işlem var:

1. MATEMATİK. Ham `$...$` markdown'dan geçerse `_` vurgu, `*` italik olarak
   yorumlanıp formül bozulur. `dollarmath` eklentisi matematiği önce yakalar ve
   `\\(...\\)` / `\\[...\\]` sınırlayıcılarına çevirir; asıl render'ı tarayıcıda
   KaTeX yapar.

2. ATIFLAR. Model `[K: bölüm, s. 61]` ve `[S: 12]` işaretçileri bırakır; bunlar
   kenar sütununa yerleşen chip'lere dönüştürülür. Atıf zinciri çıktının
   doğrulanabilirliğini sağlayan şey, o yüzden görünür kalmalı.

3. ŞEKİLLER. `[ŞEKİL: slayt 12]` işaretçisi orijinal slaytın render'ıyla
   değiştirilir — model şemayı yeniden çizemez, ama nereye gerektiğini bilir.
"""

from __future__ import annotations

import base64
import html
import re
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.container import container_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

from ..pdfio.figures import crop_figure
from ..pdfio.render import render_pages

_CITE_BOOK = re.compile(r"\[K:\s*([^\]]+?)\]")
_CITE_SLIDE = re.compile(r"\[S:\s*(\d+(?:\s*,\s*\d+)*)\]")
# `\s*$` yerine `[ \t]*$`: `\s` satır sonunu da yer, ardından gelen boş satır
# kaybolur ve sonraki blok (ör. blockquote) figürün HTML bloğuna yapışıp
# markdown olarak işlenmeden ham basılır.
_FIGURE = re.compile(
    r"^[ \t]*\[ŞEKİL:\s*slayt\s*(\d+)\s*\][ \t]*$", re.IGNORECASE | re.MULTILINE
)
# Kitap şekli. `[ \t]*$` gerekçesi yukarıdakiyle aynı: `\s*$` sonraki boş satırı
# yiyor ve ardından gelen blok figürün HTML'ine yapışıyor.
_BOOK_FIGURE = re.compile(
    r"^[ \t]*\[KŞEKİL:\s*([\d.]+?)\s*\][ \t]*$", re.IGNORECASE | re.MULTILINE
)


def _math_renderer(content: str, is_block: bool = False) -> str:
    """Matematiği KaTeX auto-render'ın tanıyacağı sınırlayıcılarla bırakır."""
    esc = html.escape(content)
    if is_block:
        return f'<div class="math-block">\\[{esc}\\]</div>'
    return f'<span class="math-inline">\\({esc}\\)</span>'


def _highlight_code(code: str, lang: str, _attrs) -> str:
    """Fenced code bloğunu renklendirir.

    Çıktı MUTLAKA `<pre` ile başlamalı: markdown-it, highlight fonksiyonunun
    dönüşü `<pre` ile başlamıyorsa onu kendi `<pre><code>` sarmalayıcısına
    koyar ve iç içe iki kod kutusu oluşur. Bu yüzden Pygments `nowrap=True`
    ile yalnızca span'ları üretir, sarmalamayı burada biz yaparız.
    """
    try:
        lexer = get_lexer_by_name(lang) if lang else guess_lexer(code)
    except ClassNotFound:
        lexer = None
    if lexer is None:
        return f'<pre class="code"><code>{html.escape(code)}</code></pre>'
    inner = highlight(code, lexer, HtmlFormatter(nowrap=True))
    return f'<pre class="code"><code>{inner}</code></pre>'


# `::: soru … :::` blokları. Prompt Türkçe ad kullanıyor (proje sözleşmesi),
# CSS sınıfı ASCII: sınıf adında ö/ü ile uğraşmaya değmez.
_CALLOUTS = {
    "analoji": ("callout-analogy", "Analoji"),
    "soru": ("callout-quiz", "Kendini sına"),
    "sözlük": ("callout-glossary", "Sözlük"),
    "sınav": ("callout-exam", "Geçmiş sınavda"),
}


def _callout_renderer(css_class: str, label: str):
    def render(self, tokens, idx, _options, _env) -> str:
        if tokens[idx].nesting == 1:
            return f'<aside class="callout {css_class}"><p class="callout-label">{label}</p>\n'
        return "</aside>\n"

    return render


def make_parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"highlight": _highlight_code})
    md.enable(["table", "strikethrough"])
    for name, (css_class, label) in _CALLOUTS.items():
        md.use(container_plugin, name=name, render=_callout_renderer(css_class, label))
    md.use(
        dollarmath_plugin,
        # Sözleşme: renderer(content, {"display_mode": bool}). Anahtar adı
        # "display_mode"; "display" yazmak her formülü satır içi yapar.
        renderer=lambda content, opts: _math_renderer(content, opts.get("display_mode", False)),
        allow_space=True,
        double_inline=True,
    )
    return md


def pygments_css() -> str:
    return HtmlFormatter(cssclass="code").get_style_defs(".code")


def _figure_html(slide_no: int, data_uri: str) -> str:
    return (
        '<figure class="slide-figure">'
        f'<img src="{data_uri}" alt="Slayt {slide_no}">'
        f'<figcaption>Slayt {slide_no}</figcaption>'
        "</figure>"
    )


def insert_figures(markdown: str, lecture_pdf: str | Path, *, max_edge: int = 1100) -> str:
    """`[ŞEKİL: slayt N]` işaretçilerini gerçek slayt görüntüsüyle değiştirir."""
    wanted = [int(m.group(1)) for m in _FIGURE.finditer(markdown)]
    if not wanted:
        return markdown

    rendered = render_pages(lecture_pdf, sorted(set(wanted)), max_edge=max_edge)

    def sub(m: re.Match) -> str:
        n = int(m.group(1))
        page = rendered.get(n)
        if page is None:
            return f"> ⚠️ Slayt {n} görüntüsü bulunamadı."
        b64 = base64.standard_b64encode(page.png).decode("ascii")
        # HTML bloğunun kendi başına durması için boş satırlarla çevrelenir.
        return "\n" + _figure_html(n, f"data:image/png;base64,{b64}") + "\n"

    return _FIGURE.sub(sub, markdown)


def _citations_to_chips(html_text: str) -> str:
    """Atıf işaretçilerini kenar sütunu chip'lerine çevirir."""

    def book(m: re.Match) -> str:
        label = html.escape(m.group(1).strip())
        return f'<span class="cite cite-book" data-src="{label}">{label}</span>'

    def slide(m: re.Match) -> str:
        nums = m.group(1).replace(" ", "")
        return f'<span class="cite cite-slide">Slayt {html.escape(nums)}</span>'

    html_text = _CITE_BOOK.sub(book, html_text)
    return _CITE_SLIDE.sub(slide, html_text)


def _book_figure_html(fig, data_uri: str) -> str:
    """Kitap şekli + kaynak künyesi.

    Künye kasıtlı olarak kısa: kitabın kendi altyazısı çoğu şekilde kırpımın
    İÇİNDE zaten görünüyor, burada tekrarlamak aynı cümleyi iki kez basıyordu.
    `caption` alanı modele "bu şekil neyi gösteriyor" demek için var, okuyucuya
    değil.
    """
    label = html.escape(fig.label)
    return (
        '<figure class="book-figure">'
        f'<img src="{data_uri}" alt="{html.escape(fig.caption or label)}">'
        f'<figcaption><span class="src">Kitap</span>'
        f'<span class="ref" lang="en">{label}</span>'
        f'<span class="pg">s. {fig.page}</span></figcaption>'
        "</figure>"
    )


def insert_book_figures(
    markdown: str, book_pdf: str | Path, figures, *, max_edge: int = 900
) -> str:
    """`[KŞEKİL: N.M]` işaretçilerini kitaptan kırpılmış diyagramla değiştirir."""
    by_number = {f.number: f for f in figures}
    if not by_number:
        return strip_book_figure_markers(markdown)

    cache: dict[str, str] = {}

    def sub(m: re.Match) -> str:
        number = m.group(1).strip(".")
        fig = by_number.get(number)
        if fig is None:
            # Model listede olmayan bir numara uydurmuş olabilir; sessizce
            # yutma — okuyucuya eksik olduğunu söyle.
            return f"\n<p class=\"figure-missing\">Kitap şekli {number} bulunamadı.</p>\n"
        if number not in cache:
            png = crop_figure(book_pdf, fig, max_edge=max_edge)
            b64 = base64.standard_b64encode(png).decode("ascii")
            cache[number] = f"data:image/png;base64,{b64}"
        return "\n" + _book_figure_html(fig, cache[number]) + "\n"

    return _BOOK_FIGURE.sub(sub, markdown)


def strip_book_figure_markers(markdown: str) -> str:
    """Kitap PDF'i yoksa işaretçiyi okunabilir bir nota çevirir."""

    def sub(m: re.Match) -> str:
        return (
            f'\n<p class="figure-missing">Kitap şekli {m.group(1)} — '
            "kitap PDF'i verilmediği için yerleştirilemedi.</p>\n"
        )

    return _BOOK_FIGURE.sub(sub, markdown)


def strip_figure_markers(markdown: str) -> str:
    """Ders PDF'i yokken işaretçiyi okunabilir bir nota çevirir.

    Aksi halde `[ŞEKİL: slayt 22]` köşeli parantezleriyle ham metin olarak
    okuyucunun önüne çıkıyor — sistemin iç işaretçisi çıktıya sızmamalı.
    """

    def sub(m: re.Match) -> str:
        return (
            f'\n<p class="figure-missing">Slayt {m.group(1)} görüntüsü — '
            "ders PDF'i verilmediği için yerleştirilemedi.</p>\n"
        )

    return _FIGURE.sub(sub, markdown)


# Model atıf işaretçilerini bazen backtick'e alıyor (`[S: 3]`). O zaman
# işaretçi <code> içine hapsoluyor ve chip stili kod kutusuyla çakışıyor.
# İşaretçiler sistemin iç sözleşmesi; kod olarak gösterilmeleri gerekmiyor.
_TICKED_MARKER = re.compile(r"`(\[(?:K|S|KŞEKİL|ŞEKİL):[^\]`]*\])`", re.IGNORECASE)


def markdown_to_html(
    markdown: str,
    *,
    lecture_pdf: str | Path | None = None,
    book_pdf: str | Path | None = None,
    figures=(),
) -> str:
    """Ders notu markdown'ını gövde HTML'ine çevirir."""
    markdown = _TICKED_MARKER.sub(r"\1", markdown)
    markdown = (
        insert_figures(markdown, lecture_pdf) if lecture_pdf else strip_figure_markers(markdown)
    )
    markdown = (
        insert_book_figures(markdown, book_pdf, figures)
        if book_pdf
        else strip_book_figure_markers(markdown)
    )
    body = make_parser().render(markdown)
    return _citations_to_chips(body)
