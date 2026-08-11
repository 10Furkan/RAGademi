from __future__ import annotations

from pathlib import Path

import pytest

from dersnotu.llm.prompts import build_lecture_context, build_section_request
from dersnotu.models import (
    BookChunk,
    ExpandedSection,
    Lecture,
    LectureSection,
    Slide,
    StudyDocument,
    TopicCard,
)
from dersnotu.pipeline import build_cached_prefix, to_markdown

LECTURE_PDF = Path("Lecture02 - Bitsints.pptx.pdf")


def make_lecture() -> Lecture:
    slides = [
        Slide(number=1, title="Giriş", text="a"),
        Slide(number=2, title="Konu", text="b", is_visual=True),
    ]
    return Lecture(
        source_path="x.pdf",
        source_sha256="abc",
        title="Test Dersi",
        slides=slides,
        sections=[LectureSection(index=0, slides=slides)],
    )


def test_cached_prefix_is_byte_stable():
    """Cache önek eşleşmesidir: aynı girdi her seferinde AYNI baytları vermeli.

    Öneğe tarih/sayaç/rastgele kimlik sızarsa cache sessizce düşer ve
    maliyet fark edilmeden artar.
    """
    lec = make_lecture()
    assert build_cached_prefix(lec) == build_cached_prefix(lec)
    assert build_lecture_context(lec) == build_lecture_context(lec)


def test_cached_prefix_has_exactly_one_breakpoint():
    prefix = build_cached_prefix(make_lecture())
    breakpoints = [b for b in prefix if "cache_control" in b]
    assert len(breakpoints) == 1
    assert breakpoints[-1] is prefix[-1]  # kırılma noktası önekin SONUNDA


def test_section_request_carries_citations():
    lec = make_lecture()
    chunk = BookChunk(
        chunk_id="c1",
        text="content",
        section_title="Information Storage",
        page_start=70,
        page_end=72,
        token_estimate=5,
    )
    card = TopicCard(section_index=0, title="Byte Ordering", gaps=["endianness"])
    text = build_section_request(lec.sections[0], card, [chunk], "English")

    assert "[B: Information Storage, p. 70-72]" in text
    assert "endianness" in text
    assert "English" in text
    # Görsel slayt numarası modele bildirilmeli.
    assert "2" in text


def test_section_request_flags_missing_book_context():
    lec = make_lecture()
    card = TopicCard(section_index=0, title="Topic")
    text = build_section_request(lec.sections[0], card, [], "English")
    assert "No matching excerpt was found" in text


def test_failed_section_becomes_visible_warning_not_silent_gap():
    doc = StudyDocument(
        lecture_title="Ders",
        language="Türkçe",
        sections=[
            ExpandedSection(
                section_index=0, title="İyi", markdown="## İyi\nmetin", slide_range=(1, 2)
            ),
            ExpandedSection(
                section_index=1,
                title="Kötü",
                markdown="",
                slide_range=(3, 4),
                error="RateLimitError",
            ),
        ],
    )
    md = to_markdown(doc)
    assert "## İyi" in md
    assert "⚠️" in md and "RateLimitError" in md and "3-4" in md


@pytest.mark.skipif(not LECTURE_PDF.exists(), reason="örnek ders PDF'i yok")
def test_real_lecture_segmentation():
    from dersnotu.pdfio import parse_lecture

    lec = parse_lecture(LECTURE_PDF, shape_threshold=20, max_section_slides=8)
    assert len(lec.slides) == 51
    assert 4 <= len(lec.sections) <= 12
    assert all(1 <= len(s.slides) <= 8 for s in lec.sections)
    # Tekrar eden "Today:" ajanda slaytları ayırıcı olarak bulunmalı.
    assert {2, 14, 21}.issubset({s.number for s in lec.slides if s.is_divider})
    # Şema slaytları görsel işaretlenmeli, düz metin slaytları değil.
    by_num = {s.number: s for s in lec.slides}
    assert by_num[20].is_visual  # Shift Operations — bol diyagram
    assert not by_num[18].is_visual  # Bit-Level Operations in C — düz metin
