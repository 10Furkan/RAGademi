"""Kitap PDF'ini okur: içindekiler ağacı + sayfa metinleri.

Kitap asla LLM'e bütün olarak gitmez (CSAPP = 1105 sayfa, ~600K token).
Burada sadece yerel çıkarma yapılır; seçim retrieval katmanının işi.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from pypdf import PdfReader

from ..models import Book, BookSection
from .lecture import sha256_file

# Metin katmanı bu eşiğin altındaysa sayfa taranmış (OCR gerekli) sayılır.
SCANNED_CHARS_PER_PAGE = 100

_HEADING = re.compile(r"^\s*(\d+(?:\.\d+){0,2})\s+([A-Z][^\n]{3,80})\s*$")


def read_pages(path: str | Path) -> list[str]:
    """Sayfa metinlerini sırayla döndürür (index 0 == sayfa 1)."""
    reader = PdfReader(str(path))
    return [(p.extract_text() or "") for p in reader.pages]


def looks_scanned(pages: list[str], sample: int = 40) -> bool:
    if not pages:
        return True
    step = max(1, len(pages) // sample)
    probe = pages[::step][:sample]
    avg = sum(len(t) for t in probe) / max(1, len(probe))
    return avg < SCANNED_CHARS_PER_PAGE


def _outline_sections(path: str | Path, page_count: int) -> list[BookSection]:
    """PDF bookmark ağacından bölüm listesi çıkarır."""
    reader = PdfReader(str(path))
    entries: list[tuple[str, int, int]] = []  # (title, level, page_index)

    def walk(node, level: int) -> Iterator[tuple[str, int, int]]:
        for item in node:
            if isinstance(item, list):
                yield from walk(item, level + 1)
                continue
            title = str(getattr(item, "title", "") or "").strip()
            if not title:
                continue
            try:
                page_idx = reader.get_destination_page_number(item)
            except Exception:
                continue
            yield title, level, page_idx

    try:
        entries = list(walk(reader.outline, 0))
    except Exception:
        entries = []

    if not entries:
        return []

    entries.sort(key=lambda e: e[2])
    sections: list[BookSection] = []
    for i, (title, level, page_idx) in enumerate(entries):
        end = entries[i + 1][2] if i + 1 < len(entries) else page_count - 1
        sections.append(
            BookSection(
                title=title,
                level=level,
                page_start=page_idx + 1,
                page_end=max(page_idx + 1, end),
            )
        )
    return sections


def _heuristic_sections(pages: list[str]) -> list[BookSection]:
    """Bookmark yoksa: '2.1 Information Storage' kalıbını sayfa metninde ara."""
    found: list[tuple[str, int, int]] = []
    for i, text in enumerate(pages):
        for line in text.splitlines()[:6]:  # başlıklar sayfa başında olur
            m = _HEADING.match(line)
            if m:
                number, name = m.groups()
                level = number.count(".")
                found.append((f"{number} {name.strip()}", level, i))
                break

    sections: list[BookSection] = []
    for i, (title, level, page_idx) in enumerate(found):
        end = found[i + 1][2] if i + 1 < len(found) else len(pages) - 1
        sections.append(
            BookSection(
                title=title, level=level, page_start=page_idx + 1, page_end=max(page_idx + 1, end)
            )
        )
    return sections


def parse_book(path: str | Path, pages: list[str] | None = None) -> tuple[Book, list[str]]:
    """Kitabı ayrıştırır. `pages` verilirse yeniden okumaz.

    Returns: (Book metadata, sayfa metinleri)
    """
    path = Path(path)
    if pages is None:
        pages = read_pages(path)

    sections = _outline_sections(path, len(pages))
    if not sections:
        sections = _heuristic_sections(pages)

    reader = PdfReader(str(path))
    meta_title = ""
    try:
        meta_title = (reader.metadata.title or "").strip() if reader.metadata else ""
    except Exception:
        pass

    book = Book(
        source_path=str(path),
        source_sha256=sha256_file(path),
        title=meta_title or path.stem,
        page_count=len(pages),
        sections=sections,
    )
    return book, pages


def toc_outline(book: Book, max_level: int = 1, limit: int = 400) -> str:
    """LLM'e verilecek kompakt içindekiler metni (TOC hizalama adımı için)."""
    lines = []
    for s in book.sections:
        if s.level > max_level:
            continue
        indent = "  " * s.level
        lines.append(f"{indent}{s.title}  (s. {s.page_start}-{s.page_end})")
        if len(lines) >= limit:
            break
    return "\n".join(lines)
