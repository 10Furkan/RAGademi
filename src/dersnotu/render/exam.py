"""Practice exam → HTML/PDF using the shared rendering pipeline."""

from __future__ import annotations

import html as _html
import re
from pathlib import Path

from ..models import PracticeExam, PracticeQuestion
from .html import assets_available, build_page
from .markdown import markdown_to_html
from .pdf import RenderError, html_to_pdf

_TEK_P = re.compile(r"^<p>(.*)</p>\s*$", re.S)


def _blok(md: str) -> str:
    """Render a Markdown block, including math, code, and citations."""
    return markdown_to_html(md or "")


def _satir(md: str) -> str:
    """Render one-line Markdown and remove its wrapping paragraph."""
    parcali = _blok(md).strip()
    m = _TEK_P.match(parcali)
    return m.group(1) if m else parcali


def _sik_listesi(q: PracticeQuestion) -> str:
    if not q.has_choices:
        return ""
    ogeler = "".join(f"<li>{_satir(c)}</li>" for c in q.choices)
    return f'<ol class="choices">{ogeler}</ol>'


def _soru_html(q: PracticeQuestion) -> str:
    kunye = [f"Question {q.number}"]
    if q.kind:
        kunye.append(_html.escape(q.kind))
    sag = f'<span class="pts">{q.points} points</span>' if q.points else ""
    return (
        f'<section class="exam-q" id="soru-{q.number}">'
        f'<div class="qhead"><span class="qnum">{" · ".join(kunye)}</span>{sag}</div>'
        f"{_blok(q.prompt)}{_sik_listesi(q)}"
        "</section>"
    )


def _cevap_html(q: PracticeQuestion) -> str:
    parcalar = [
        f'<section class="exam-a" id="cevap-{q.number}">',
        f'<div class="qhead"><span class="qnum">Question {q.number}</span>'
        f'<a class="pts" href="#soru-{q.number}">back to question</a></div>',
    ]
    if q.answer.strip():
        parcalar.append(f'<p class="answer"><b>Answer:</b> {_satir(q.answer)}</p>')
    if q.solution.strip():
        parcalar.append(_blok(q.solution))
    if q.citations:
        parcalar.append(_blok(" ".join(f"[B: {c}]" for c in q.citations)))
    if q.modeled_on.strip():
        parcalar.append(
            '<aside class="callout callout-exam">'
            '<p class="callout-label">Source question</p>'
            f"<blockquote>{_blok(q.modeled_on)}</blockquote></aside>"
        )
    parcalar.append("</section>")
    return "".join(parcalar)


def _kunye(exam: PracticeExam) -> str:
    ne = [f"<strong>{len(exam.questions)}</strong> questions"]
    if exam.total_points:
        ne.append(f"<strong>{exam.total_points}</strong> points")
    if exam.duration_minutes:
        ne.append(f"<strong>{exam.duration_minutes}</strong> minutes")
    ne.append(_html.escape(exam.language))
    return " · ".join(ne)


def practice_to_html(exam: PracticeExam, *, nav_html: str = "") -> str:
    """Convert a PracticeExam into a complete HTML page."""
    ust = []
    if exam.profile:
        ust.append(f'<p class="exam-profile">{_html.escape(exam.profile)}</p>')
    if exam.source_exam:
        dayanakli = len(exam.grounded)
        ust.append(
            '<p class="exam-source">Paper used as a template: '
            f"<b>{_html.escape(exam.source_exam)}</b> — "
            f"{dayanakli}/{len(exam.questions)} questions are grounded in a quoted "
            "source question shown in the answer key."
        )
    govde = (
        "".join(ust)
        + "".join(_soru_html(q) for q in exam.questions)
        + '<section class="exam-key"><h2>Answer key</h2>'
        + "".join(_cevap_html(q) for q in exam.questions)
        + "</section>"
    )
    return build_page(
        title=f"{exam.lecture_title} — practice exam",
        body_html=govde,
        meta=_kunye(exam),
        toc_html="",
        nav_html=nav_html,
        eyebrow="Practice exam · modeled on a past paper",
    )


def render_practice(
    exam: PracticeExam,
    out_path: str | Path,
    *,
    nav_html: str = "",
    save_html: str | Path | None = None,
) -> Path:
    """Render a PDF and optionally save the same HTML used by the reader."""
    if not assets_available():
        raise RenderError("KaTeX assets are missing. Run: python scripts/vendor_katex.py")
    sayfa = practice_to_html(exam, nav_html=nav_html)
    if save_html:
        Path(save_html).write_text(sayfa, encoding="utf-8")
    return html_to_pdf(sayfa, out_path)
