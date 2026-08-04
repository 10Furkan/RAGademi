"""Slayt segmentasyon mantığı — PDF'e ihtiyaç duymadan saf fonksiyonlar."""

from __future__ import annotations

from dersnotu.models import LectureSection, Slide
from dersnotu.pdfio.lecture import _build_sections, _mark_dividers, _split_large

AGENDA = "⬛ Bits\n⬛ Bit-level manipulations\n⬛ Integers\n⬛ Summary ve biraz daha metin"


def slide(n: int, title: str = "", text: str = "") -> Slide:
    return Slide(number=n, title=title, text=text)


def test_repeated_body_marks_divider():
    slides = [
        slide(1, "Ders Başlığı"),
        slide(2, "Today: X", AGENDA),
        slide(3, "İçerik A", "bir şeyler"),
        slide(4, "Today: X", AGENDA),
        slide(5, "İçerik B", "başka şeyler"),
    ]
    _mark_dividers(slides)
    assert [s.number for s in slides if s.is_divider] == [2, 4]


def test_agenda_title_marks_divider_even_when_unique():
    slides = [slide(1, "Outline: bugün", "tek seferlik ajanda metni")]
    _mark_dividers(slides)
    assert slides[0].is_divider


def test_sections_split_at_dividers():
    slides = [
        slide(1, "Başlık"),
        slide(2, "Today", AGENDA),
        slide(3, "A", "x"),
        slide(4, "B", "y"),
        slide(5, "Today", AGENDA),
        slide(6, "C", "z"),
        slide(7, "D", "w"),
    ]
    _mark_dividers(slides)
    sections = _build_sections(slides)
    ranges = [s.slide_range for s in sections]
    # Slayt 1 tek başına kaldığı için sonraki blokla birleşmeli.
    assert ranges == [(1, 4), (6, 7)]
    assert sections[1].agenda_context == AGENDA


def test_large_section_is_split_into_balanced_parts():
    section = LectureSection(index=0, slides=[slide(i) for i in range(1, 17)])
    parts = _split_large([section], max_slides=8)
    assert [len(p.slides) for p in parts] == [8, 8]
    assert [p.index for p in parts] == [0, 1]


def test_split_preserves_agenda_context():
    section = LectureSection(
        index=0, slides=[slide(i) for i in range(1, 11)], agenda_context=AGENDA
    )
    parts = _split_large([section], max_slides=4)
    assert len(parts) == 3
    assert all(p.agenda_context == AGENDA for p in parts)


def test_no_split_when_under_limit():
    section = LectureSection(index=0, slides=[slide(i) for i in range(1, 5)])
    assert _split_large([section], max_slides=8) == [section]
