"""Geçmiş sınav kâğıdı.

Sınav kâğıdı üçüncü bir kaynak ama slayt ve kitapla eşit değil: **kapsamı
değiştirmez.** Slayt neyi kapsıyorsa sorumluluk odur; sınav yalnızca o kapsam
içindeki bir konunun ne kadar derin işleneceğini kaydırır. Buradaki testler
tam olarak bu sınırı ve modele verilen kanıt disiplinini koruyor.
"""

from __future__ import annotations

from dersnotu.llm.prompts import EXAM_RULE, EXPAND_SYSTEM, build_lecture_context
from dersnotu.models import Lecture, LectureSection, Slide
from dersnotu.pipeline import EXAM_CHAR_LIMIT, build_cached_prefix, load_exam_text
from dersnotu.render.markdown import markdown_to_html


def make_lecture() -> Lecture:
    slides = [Slide(number=i, title=f"S{i}", text=f"metin {i}") for i in range(1, 4)]
    return Lecture(
        source_path="x.pdf", source_sha256="abc", title="Ders",
        slides=slides,
        sections=[LectureSection(index=0, slides=slides)],
    )


# --- prompt ---------------------------------------------------------------
def test_exam_text_lands_in_the_cached_prefix():
    """Sınav metni bölümden bölüme değişmiyor; önekte durması bir kez yazılıp
    her bölümde ucuza okunması demek."""
    lec = make_lecture()
    onek = build_cached_prefix(lec, "", "SORU 1: İkinin tümleyeni nedir?")[0]["text"]
    assert "SORU 1: İkinin tümleyeni nedir?" in onek
    assert "PAST EXAM QUESTIONS" in onek


def test_prefix_without_exam_is_unchanged():
    """Sınav kâğıdı olmayan koşu eskisiyle BİT BİT aynı önek üretmeli,
    yoksa özelliği eklemek herkesin cache'ini düşürürdü."""
    lec = make_lecture()
    assert build_cached_prefix(lec) == build_cached_prefix(lec, "", "")
    assert "GEÇMİŞ SINAV" not in build_cached_prefix(lec)[0]["text"]


def test_exam_prefix_is_byte_stable():
    lec = make_lecture()
    a = build_cached_prefix(lec, "not", "SORU 1")
    assert a == build_cached_prefix(lec, "not", "SORU 1")


def test_exam_rule_stays_out_of_the_system_prompt():
    """Sistem promptu sabit; sınav kuralı gövdeye, önekin içine gider."""
    assert "GEÇMİŞ SINAV" not in EXPAND_SYSTEM
    assert EXAM_RULE in build_lecture_context(make_lecture(), "", "SORU 1")


def test_exam_rule_forbids_unsupported_claims():
    """Projenin atıf disiplini burada da geçerli: alıntılayamıyorsan iddia yok."""
    assert "quote the question verbatim" in EXAM_RULE
    assert "never to expand scope" in EXAM_RULE
    assert "do not create this block" in EXAM_RULE


# --- metin okuma ----------------------------------------------------------
def test_missing_exam_is_not_an_error():
    """Sınav kâğıdı isteğe bağlı bir zenginleştirme; koşuyu düşürmemeli."""
    assert load_exam_text(None) == ""


def test_unreadable_exam_degrades_quietly(tmp_path):
    bozuk = tmp_path / "bozuk.pdf"
    bozuk.write_bytes(b"bu bir PDF degil")
    assert load_exam_text(bozuk) == ""


def test_exam_text_is_capped(monkeypatch):
    """40 sayfalık bir soru arşivi öneki üçe katlardı; sınır ölçülü."""
    import dersnotu.pipeline as p

    monkeypatch.setattr(p, "read_pages", lambda _: ["x" * 100_000])
    assert len(p.load_exam_text("var.pdf")) == EXAM_CHAR_LIMIT


# --- render ---------------------------------------------------------------
def test_exam_callout_renders():
    html = markdown_to_html(
        "::: sınav\n**Sorulmuş:** 0xCA baytını ikiliye çevirin.\n\nÇözüm.\n:::"
    )
    assert "callout-exam" in html
    assert "From a past exam" in html
    assert "0xCA" in html


def test_exam_callout_is_not_the_quiz_callout():
    """İkisi farklı iş yapıyor: biri bilgi verir, diğeri öğrenciden iş ister."""
    sinav = markdown_to_html("::: sınav\nx\n:::")
    soru = markdown_to_html("::: soru\nx\n:::")
    assert "callout-exam" in sinav and "callout-quiz" not in sinav
    assert "callout-quiz" in soru and "callout-exam" not in soru
