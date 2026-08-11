"""Markdown → HTML with math, source citations, and source figures."""

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

_CITE_BOOK = re.compile(r"\[(?:B|K):\s*([^\]]+?)\]")
_CITE_SLIDE = re.compile(r"\[S:\s*(\d+(?:\s*,\s*\d+)*)\]")
# Keep the following blank line; `\s*$` would consume it and merge blocks.
_FIGURE = re.compile(
    r"^[ \t]*\[(?:FIGURE:\s*slide|ŞEKİL:\s*slayt)\s*(\d+)\s*\][ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# Accept legacy Turkish markers in previously generated documents.
_BOOK_FIGURE = re.compile(
    r"^[ \t]*\[(?:BOOKFIGURE|KŞEKİL):\s*([\d.]+?)\s*\][ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _math_renderer(content: str, is_block: bool = False) -> str:
    """Wrap math in delimiters recognized by KaTeX auto-render."""
    esc = html.escape(content)
    if is_block:
        return f'<div class="math-block">\\[{esc}\\]</div>'
    return f'<span class="math-inline">\\({esc}\\)</span>'


def _highlight_code(code: str, lang: str, _attrs) -> str:
    """Highlight a fenced code block without creating nested wrappers."""
    try:
        lexer = get_lexer_by_name(lang) if lang else guess_lexer(code)
    except ClassNotFound:
        lexer = None
    if lexer is None:
        return f'<pre class="code"><code>{html.escape(code)}</code></pre>'
    inner = highlight(code, lexer, HtmlFormatter(nowrap=True))
    return f'<pre class="code"><code>{inner}</code></pre>'


_CALLOUTS = {
    "analogy": ("callout-analogy", "Analogy"),
    "quiz": ("callout-quiz", "Self-check"),
    "glossary": ("callout-glossary", "Glossary"),
    "exam": ("callout-exam", "From a past exam"),
    # Legacy containers remain readable in existing generated Markdown.
    "analoji": ("callout-analogy", "Analogy"),
    "soru": ("callout-quiz", "Self-check"),
    "sözlük": ("callout-glossary", "Glossary"),
    "sınav": ("callout-exam", "From a past exam"),
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
        # The plugin contract names this option `display_mode`.
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
        f'<img src="{data_uri}" alt="Slide {slide_no}">'
        f'<figcaption>Slide {slide_no}</figcaption>'
        "</figure>"
    )


def insert_figures(markdown: str, lecture_pdf: str | Path, *, max_edge: int = 1100) -> str:
    """Replace slide-figure markers with rendered source slides."""
    wanted = [int(m.group(1)) for m in _FIGURE.finditer(markdown)]
    if not wanted:
        return markdown

    rendered = render_pages(lecture_pdf, sorted(set(wanted)), max_edge=max_edge)

    def sub(m: re.Match) -> str:
        n = int(m.group(1))
        page = rendered.get(n)
        if page is None:
            return f"> ⚠️ The image for slide {n} could not be found."
        b64 = base64.standard_b64encode(page.png).decode("ascii")
        # Blank lines keep the HTML as a standalone Markdown block.
        return "\n" + _figure_html(n, f"data:image/png;base64,{b64}") + "\n"

    return _FIGURE.sub(sub, markdown)


def _citations_to_chips(html_text: str) -> str:
    """Convert source markers into visible citation chips."""

    def book(m: re.Match) -> str:
        label = html.escape(m.group(1).strip())
        return f'<span class="cite cite-book" data-src="{label}">{label}</span>'

    def slide(m: re.Match) -> str:
        nums = m.group(1).replace(" ", "")
        return f'<span class="cite cite-slide">Slide {html.escape(nums)}</span>'

    html_text = _CITE_BOOK.sub(book, html_text)
    return _CITE_SLIDE.sub(slide, html_text)


def _book_figure_html(fig, data_uri: str) -> str:
    """Render a textbook figure with a concise source label."""
    label = html.escape(fig.label)
    return (
        '<figure class="book-figure">'
        f'<img src="{data_uri}" alt="{html.escape(fig.caption or label)}">'
        f'<figcaption><span class="src">Textbook</span>'
        f'<span class="ref" lang="en">{label}</span>'
        f'<span class="pg">p. {fig.page}</span></figcaption>'
        "</figure>"
    )


def insert_book_figures(
    markdown: str, book_pdf: str | Path, figures, *, max_edge: int = 900
) -> str:
    """Replace textbook-figure markers with diagrams cropped from the source."""
    by_number = {f.number: f for f in figures}
    if not by_number:
        return strip_book_figure_markers(markdown)

    cache: dict[str, str] = {}

    def sub(m: re.Match) -> str:
        number = m.group(1).strip(".")
        fig = by_number.get(number)
        if fig is None:
            return f"\n<p class=\"figure-missing\">Textbook figure {number} could not be found.</p>\n"
        if number not in cache:
            png = crop_figure(book_pdf, fig, max_edge=max_edge)
            b64 = base64.standard_b64encode(png).decode("ascii")
            cache[number] = f"data:image/png;base64,{b64}"
        return "\n" + _book_figure_html(fig, cache[number]) + "\n"

    return _BOOK_FIGURE.sub(sub, markdown)


def strip_book_figure_markers(markdown: str) -> str:
    """Turn a marker into a readable note when the textbook PDF is unavailable."""

    def sub(m: re.Match) -> str:
        return (
            f'\n<p class="figure-missing">Textbook figure {m.group(1)} — '
            "could not be inserted because the textbook PDF was not provided.</p>\n"
        )

    return _BOOK_FIGURE.sub(sub, markdown)


def strip_figure_markers(markdown: str) -> str:
    """Turn a marker into a readable note when the lecture PDF is unavailable."""

    def sub(m: re.Match) -> str:
        return (
            f'\n<p class="figure-missing">Slide {m.group(1)} image — '
            "could not be inserted because the lecture PDF was not provided.</p>\n"
        )

    return _FIGURE.sub(sub, markdown)


# Model atıf işaretçilerini bazen backtick'e alıyor (`[S: 3]`). O zaman
# işaretçi <code> içine hapsoluyor ve chip stili kod kutusuyla çakışıyor.
# İşaretçiler sistemin iç sözleşmesi; kod olarak gösterilmeleri gerekmiyor.
_TICKED_MARKER = re.compile(
    r"`(\[(?:B|K|S|BOOKFIGURE|KŞEKİL|FIGURE|ŞEKİL):[^\]`]*\])`", re.IGNORECASE
)


def markdown_to_html(
    markdown: str,
    *,
    lecture_pdf: str | Path | None = None,
    book_pdf: str | Path | None = None,
    figures=(),
) -> str:
    """Convert study-note Markdown into body HTML."""
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
