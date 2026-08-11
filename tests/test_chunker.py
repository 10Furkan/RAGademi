from __future__ import annotations

from dersnotu.index.chunker import chunk_book, estimate_tokens, is_content_section
from dersnotu.models import Book, BookSection


def make_book(sections: list[BookSection], page_count: int) -> Book:
    return Book(
        source_path="x.pdf",
        source_sha256="deadbeef",
        title="Test",
        page_count=page_count,
        sections=sections,
    )


def test_chunks_never_cross_section_boundary():
    sections = [
        BookSection(title="Bölüm A", level=1, page_start=1, page_end=3),
        BookSection(title="Bölüm B", level=1, page_start=4, page_end=6),
    ]
    pages = ["A içeriği " * 200] * 3 + ["B içeriği " * 200] * 3
    chunks = chunk_book(make_book(sections, 6), pages, target_tokens=200)

    assert chunks
    for c in chunks:
        assert c.section_title in {"Bölüm A", "Bölüm B"}
        # Bir chunk'ın sayfa aralığı kendi bölümünün dışına taşmamalı.
        expected = sections[0] if c.section_title == "Bölüm A" else sections[1]
        assert c.page_start >= expected.page_start
        assert c.page_end <= expected.page_end


def test_front_and_back_matter_excluded():
    sections = [
        BookSection(title="Contents", level=0, page_start=1, page_end=2),
        BookSection(title="Information Storage", level=1, page_start=3, page_end=4),
        BookSection(title="Index", level=0, page_start=5, page_end=6),
    ]
    pages = ["içindekiler " * 200] * 2 + ["gerçek içerik " * 200] * 2 + ["dizin " * 200] * 2
    chunks = chunk_book(make_book(sections, 6), pages, target_tokens=200)

    titles = {c.section_title for c in chunks}
    assert titles == {"Information Storage"}


def test_is_content_section():
    assert is_content_section("Integer Representations")
    assert not is_content_section("Index")
    assert not is_content_section("Bibliography")
    assert not is_content_section("  preface ")


def test_citation_formats_single_and_range():
    sections = [BookSection(title="Bölüm", level=1, page_start=1, page_end=4)]
    pages = ["metin " * 400] * 4
    chunks = chunk_book(make_book(sections, 4), pages, target_tokens=150)
    assert chunks
    for c in chunks:
        assert "p." in c.citation
        assert c.section_title in c.citation


def test_token_estimate_scales_with_length():
    assert estimate_tokens("a" * 350) == 100
    assert estimate_tokens("") == 0
