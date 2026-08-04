"""Deneme sınavı → HTML/PDF.

Ders notuyla aynı render zincirini (markdown-it → KaTeX → Pygments → Chromium)
kullanır; ayrı olan yalnızca belgenin İSKELETİ.

**Cevap anahtarı ayrı ve sonda.** Çözümü sorunun hemen altına koymak teknik
olarak daha kolaydı ama kâğıdı denemelik olmaktan çıkarır: öğrenci soruyu
okurken yanıtı da görür. Anahtar `break-before: page` ile ayrı sayfaya
basılıyor, ekranda da belgenin sonunda duruyor.

**Soru kâğıdından anahtara bağlantı YOK, tersi var.** Her sorunun yanına
"cevabı gör" koymak aynı sorunu geri getirirdi; anahtardaki her kayıt ise
sorusuna geri bağlanıyor, çünkü çözümü okurken soruya dönmek gerçek bir
ihtiyaç.
"""

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
    """Markdown bloğu → HTML (matematik, kod, atıf chip'leri dahil).

    Ders PDF'i verilmiyor: sınav kâğıdında slayt görüntüsü işaretçisi
    beklenmiyor, model yine de bırakırsa `markdown_to_html` onu görünür bir
    nota çevirir — sessizce ham köşeli parantez basmaz.
    """
    return markdown_to_html(md or "")


def _satir(md: str) -> str:
    """Tek satırlık markdown; sarmalayan `<p>` atılır (şık, yanıt gibi)."""
    parcali = _blok(md).strip()
    m = _TEK_P.match(parcali)
    return m.group(1) if m else parcali


def _sik_listesi(q: PracticeQuestion) -> str:
    if not q.has_choices:
        return ""
    ogeler = "".join(f"<li>{_satir(c)}</li>" for c in q.choices)
    return f'<ol class="choices">{ogeler}</ol>'


def _soru_html(q: PracticeQuestion) -> str:
    kunye = [f"Soru {q.number}"]
    if q.kind:
        kunye.append(_html.escape(q.kind))
    sag = f'<span class="pts">{q.points} puan</span>' if q.points else ""
    return (
        f'<section class="exam-q" id="soru-{q.number}">'
        f'<div class="qhead"><span class="qnum">{" · ".join(kunye)}</span>{sag}</div>'
        f"{_blok(q.prompt)}{_sik_listesi(q)}"
        "</section>"
    )


def _cevap_html(q: PracticeQuestion) -> str:
    parcalar = [
        f'<section class="exam-a" id="cevap-{q.number}">',
        f'<div class="qhead"><span class="qnum">Soru {q.number}</span>'
        f'<a class="pts" href="#soru-{q.number}">soruya dön</a></div>',
    ]
    if q.answer.strip():
        parcalar.append(f'<p class="answer"><b>Yanıt:</b> {_satir(q.answer)}</p>')
    if q.solution.strip():
        parcalar.append(_blok(q.solution))
    if q.citations:
        # Atıflar işaretçi biçiminde geçirilir; chip'e çeviren katman ortak.
        parcalar.append(_blok(" ".join(f"[K: {c}]" for c in q.citations)))
    if q.modeled_on.strip():
        # Alıntı YOKSA bu blok hiç basılmaz. "Buna benzer soru çıkmıştı" demek
        # ancak sorunun kendisi gösterilebiliyorsa bir iddiadır.
        parcalar.append(
            '<aside class="callout callout-exam">'
            '<p class="callout-label">Örnek alınan soru</p>'
            f"<blockquote>{_blok(q.modeled_on)}</blockquote></aside>"
        )
    parcalar.append("</section>")
    return "".join(parcalar)


def _kunye(exam: PracticeExam) -> str:
    ne = [f"<strong>{len(exam.questions)}</strong> soru"]
    if exam.total_points:
        ne.append(f"<strong>{exam.total_points}</strong> puan")
    if exam.duration_minutes:
        ne.append(f"<strong>{exam.duration_minutes}</strong> dakika")
    ne.append(_html.escape(exam.language))
    return " · ".join(ne)


def practice_to_html(exam: PracticeExam, *, nav_html: str = "") -> str:
    """PracticeExam → tam HTML sayfası."""
    ust = []
    if exam.profile:
        ust.append(f'<p class="exam-profile">{_html.escape(exam.profile)}</p>')
    if exam.source_exam:
        dayanakli = len(exam.grounded)
        ust.append(
            '<p class="exam-source">Örnek alınan kâğıt: '
            f"<b>{_html.escape(exam.source_exam)}</b> — "
            f"{dayanakli}/{len(exam.questions)} soru oradaki bir soruya "
            "dayanıyor ve alıntısı cevap anahtarında."
        )
    govde = (
        "".join(ust)
        + "".join(_soru_html(q) for q in exam.questions)
        + '<section class="exam-key"><h2>Cevap anahtarı</h2>'
        + "".join(_cevap_html(q) for q in exam.questions)
        + "</section>"
    )
    return build_page(
        title=f"{exam.lecture_title} — deneme sınavı",
        body_html=govde,
        meta=_kunye(exam),
        toc_html="",
        nav_html=nav_html,
        eyebrow="Deneme sınavı · geçmiş kâğıda göre üretildi",
    )


def render_practice(
    exam: PracticeExam,
    out_path: str | Path,
    *,
    nav_html: str = "",
    save_html: str | Path | None = None,
) -> Path:
    """PDF basar; `save_html` verilirse aynı HTML'i diske de yazar.

    Ders notundaki gerekçenin aynısı: okuyucu bu dosyayı sunuyor, iki kez
    render etmenin karşılığı yok.
    """
    if not assets_available():
        raise RenderError("KaTeX varlıkları yok. Çalıştır: python scripts/vendor_katex.py")
    sayfa = practice_to_html(exam, nav_html=nav_html)
    if save_html:
        Path(save_html).write_text(sayfa, encoding="utf-8")
    return html_to_pdf(sayfa, out_path)
