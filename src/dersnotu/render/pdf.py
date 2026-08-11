"""HTML → PDF through Playwright and Chromium."""

from __future__ import annotations

from pathlib import Path

from ..models import StudyDocument
from .html import assets_available, build_page, build_toc
from .markdown import markdown_to_html


class RenderError(RuntimeError):
    pass


def document_to_html(
    doc: StudyDocument,
    *,
    lecture_pdf: str | Path | None = None,
    book_pdf: str | Path | None = None,
    nav_html: str = "",
) -> str:
    """Convert a StudyDocument into a complete HTML page."""
    parts: list[str] = []
    for sec in doc.sections:
        # Each section has an anchor for direct search-result links.
        anchor = f'<a id="bolum-{sec.section_index}"></a>'
        if sec.error:
            a, b = sec.slide_range
            parts.append(
                f'{anchor}<section class="failed"><strong>{sec.title or "Section"}</strong> '
                f"could not be generated ({sec.error}). Related slides: {a}–{b}. "
                "You can retry this section independently.</section>"
            )
            continue
        parts.append(
            anchor
            + markdown_to_html(
                sec.markdown,
                lecture_pdf=lecture_pdf,
                book_pdf=book_pdf,
                figures=doc.figures,
            )
        )

    ok = [s for s in doc.sections if not s.error]
    slides = [n for s in doc.sections for n in s.slide_range]
    meta = (
        f"<strong>{len(ok)}</strong> sections · "
        f"slides {min(slides) if slides else 0}–{max(slides) if slides else 0} · "
        f"{doc.language}"
    )
    return build_page(
        title=doc.lecture_title,
        body_html="\n".join(parts),
        meta=meta,
        toc_html=build_toc(doc.sections),
        nav_html=nav_html,
    )


def html_to_pdf(html: str, out_path: str | Path, *, timeout_ms: int = 60_000) -> Path:
    """Print HTML to PDF after KaTeX finishes rendering."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RenderError(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from exc

    # file:// URI mutlak yol ister; göreli yol .as_uri() ile patlar.
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Chromium'un yerel font dosyalarını okuyabilmesi için HTML diske yazılır.
    tmp_html = out_path.with_suffix(".render.html")
    tmp_html.write_text(html, encoding="utf-8")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(tmp_html.as_uri(), wait_until="load", timeout=timeout_ms)
                # KaTeX asenkron; bayrağı bekle ki formüller ham TeX olarak basılmasın.
                try:
                    page.wait_for_function(
                        "document.body.dataset.mathReady === '1'", timeout=15_000
                    )
                except Exception:
                    pass  # matematik yoksa veya KaTeX yoksa yine de bas
                page.emulate_media(media="print")
                page.pdf(
                    path=str(out_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "18mm", "bottom": "20mm", "left": "16mm", "right": "16mm"},
                    display_header_footer=True,
                    header_template="<div></div>",
                    footer_template=(
                        '<div style="width:100%;font-family:Consolas,monospace;'
                        'font-size:7pt;color:#5C6370;text-align:center;">'
                        '<span class="pageNumber"></span> / <span class="totalPages"></span>'
                        "</div>"
                    ),
                )
            finally:
                browser.close()
    finally:
        tmp_html.unlink(missing_ok=True)

    return out_path


def render_document(
    doc: StudyDocument,
    out_path: str | Path,
    *,
    lecture_pdf: str | Path | None = None,
    book_pdf: str | Path | None = None,
    nav_html: str = "",
    save_html: str | Path | None = None,
) -> Path:
    """Render a PDF and optionally save the same HTML used by the reader."""
    if not assets_available():
        raise RenderError(
            "KaTeX assets are missing. Run: python scripts/vendor_katex.py"
        )
    html = document_to_html(
        doc, lecture_pdf=lecture_pdf, book_pdf=book_pdf, nav_html=nav_html
    )
    if save_html:
        Path(save_html).write_text(html, encoding="utf-8")
    return html_to_pdf(html, out_path)
