from __future__ import annotations

import pytest

from dersnotu.index.store import BookIndex, _to_fts_query
from dersnotu.models import Book, BookChunk


@pytest.fixture
def index(tmp_path):
    book = Book(
        source_path="x.pdf", source_sha256="abc", title="Test", page_count=100, sections=[]
    )
    chunks = [
        BookChunk(
            chunk_id="c1",
            text="Two's complement is the standard signed integer representation.",
            section_title="Integer Representations",
            page_start=96,
            page_end=98,
            token_estimate=20,
        ),
        BookChunk(
            chunk_id="c2",
            text="Little endian stores the least significant byte at the lowest address.",
            section_title="Information Storage",
            page_start=70,
            page_end=72,
            token_estimate=20,
        ),
        BookChunk(
            chunk_id="c3",
            text="Floating point uses IEEE 754 encoding with sign, exponent and fraction.",
            section_title="Floating Point",
            page_start=140,
            page_end=142,
            token_estimate=20,
        ),
    ]
    idx = BookIndex(tmp_path / "t.sqlite")
    idx.build(book, chunks)
    yield idx
    idx.close()


def test_apostrophe_query_does_not_raise():
    """`two's complement` ham haliyle FTS5'te sözdizimi hatası verir."""
    q = _to_fts_query("two's complement signed")
    assert "'" not in q
    assert '"two"' in q


def test_empty_query_returns_empty_string():
    assert _to_fts_query("!!! ???") == ""
    assert _to_fts_query("") == ""


def test_search_finds_relevant_chunk(index):
    hits = index.search("two's complement signed integer")
    assert hits
    assert hits[0][0].chunk_id == "c1"


def test_search_respects_page_range(index):
    hits = index.search("byte address endian", page_range=(60, 80))
    assert [c.chunk_id for c, _ in hits] == ["c2"]

    # Aralık dışında sonuç dönmemeli.
    assert index.search("byte address endian", page_range=(200, 300)) == []


def test_search_empty_query_returns_empty(index):
    assert index.search("") == []


def test_index_roundtrip(index):
    assert index.count() == 3
    assert index.get("c2").section_title == "Information Storage"
    assert index.get("yok") is None
    assert index.book().title == "Test"


def test_is_built_marks_completion(tmp_path):
    assert not BookIndex.is_built(tmp_path, "sha-yok")
    book = Book(
        source_path="x.pdf", source_sha256="s", title="T", page_count=1, sections=[]
    )
    idx = BookIndex(BookIndex.path_for(tmp_path, "s"))
    idx.build(book, [])
    idx.close()
    assert BookIndex.is_built(tmp_path, "s")
