"""SQLite FTS5 tabanlı kitap indeksi.

Neden FTS5: `two's complement`, `0x3F`, `sizeof` gibi tam terimlerde BM25
gömme tabanlı aramadan iyi, ek servis gerektirmiyor, tek dosya. Faz 1'de
üstüne anlamsal arama eklenip RRF ile birleştirilecek.

İndeks kitabın SHA-256'sı ile adlandırılır: aynı kitap sistemde bir kez
indekslenir, sonraki tüm dersler hazır indeksi kullanır.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from ..models import Book, BookChunk, BookFigure

# FTS5 sorgu sözdiziminde özel anlamı olan karakterler.
_FTS_SPECIAL = re.compile(r"[^\w\s]", re.UNICODE)

# İndeks dosyası kitabın SHA'sıyla adlandırılıyor, ama içeriği chunker'ın
# çıktısı. Chunker değişince SHA aynı kalır ve eski indeks sessizce kullanılır.
# Bu sayaç her chunker davranış değişikliğinde artırılmalı.
# 3: şekil tablosu eklendi.
# 4: şekil bbox'ları yanlış CropBox ofsetiyle hesaplanmıştı (bir satır kayık).
INDEX_VERSION = "4"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id      TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    section_title TEXT NOT NULL DEFAULT '',
    page_start    INTEGER NOT NULL,
    page_end      INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS figures (
    number     TEXT PRIMARY KEY,
    caption    TEXT NOT NULL DEFAULT '',
    page       INTEGER NOT NULL,
    x0 REAL NOT NULL, top REAL NOT NULL, x1 REAL NOT NULL, bottom REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS figures_page ON figures(page);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    section_title,
    content='chunks',
    content_rowid='rowid',
    tokenize='unicode61'
);
"""


class BookIndex:
    """Bir kitabın aranabilir indeksi."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ----- yaşam döngüsü ------------------------------------------------
    @classmethod
    def path_for(cls, cache_dir: Path, sha256: str) -> Path:
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"book-{sha256[:16]}.sqlite"

    @classmethod
    def is_built(cls, cache_dir: Path, sha256: str) -> bool:
        p = cls.path_for(cache_dir, sha256)
        if not p.exists():
            return False
        try:
            conn = sqlite3.connect(str(p))
            rows = dict(
                conn.execute(
                    "SELECT key, value FROM meta WHERE key IN ('complete', 'index_version')"
                ).fetchall()
            )
            conn.close()
        except sqlite3.Error:
            return False
        # Sürüm damgası yoksa indeks damgadan eski demektir — yeniden kur.
        return rows.get("complete") == "1" and rows.get("index_version") == INDEX_VERSION

    def close(self) -> None:
        self.conn.close()

    # ----- yazma --------------------------------------------------------
    def build(
        self,
        book: Book,
        chunks: list[BookChunk],
        figures: Sequence[BookFigure] = (),
    ) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM chunks")
        cur.execute("DELETE FROM chunks_fts")
        cur.execute("DELETE FROM figures")
        cur.executemany(
            "INSERT OR REPLACE INTO figures (number, caption, page, x0, top, x1, bottom)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(f.number, f.caption, f.page, *f.bbox) for f in figures],
        )
        cur.executemany(
            "INSERT INTO chunks (chunk_id, text, section_title, page_start, page_end, token_estimate)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (c.chunk_id, c.text, c.section_title, c.page_start, c.page_end, c.token_estimate)
                for c in chunks
            ],
        )
        # content= tablosuyla senkronize et.
        cur.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('book', ?)",
            (book.model_dump_json(),),
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('index_version', ?)",
            (INDEX_VERSION,),
        )
        cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('complete', '1')")
        self.conn.commit()

    # ----- okuma --------------------------------------------------------
    def book(self) -> Book | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key='book'").fetchone()
        return Book(**json.loads(row[0])) if row else None

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def get(self, chunk_id: str) -> BookChunk | None:
        row = self.conn.execute(
            "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        return _row_to_chunk(row) if row else None

    # ----- şekiller ------------------------------------------------------
    def figure_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM figures").fetchone()[0]

    def figure(self, number: str) -> BookFigure | None:
        row = self.conn.execute(
            "SELECT * FROM figures WHERE number = ?", (number,)
        ).fetchone()
        return _row_to_figure(row) if row else None

    def figures_for_pages(self, page_start: int, page_end: int) -> list[BookFigure]:
        """Verilen sayfa aralığındaki şekiller — modele 'şunlar var' demek için."""
        rows = self.conn.execute(
            "SELECT * FROM figures WHERE page BETWEEN ? AND ? ORDER BY page",
            (page_start, page_end),
        ).fetchall()
        return [_row_to_figure(r) for r in rows]

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        page_range: tuple[int, int] | None = None,
    ) -> list[tuple[BookChunk, float]]:
        """BM25 araması. `page_range` verilirse sonuçlar o aralığa kısıtlanır.

        Sayfa aralığı TOC hizalama adımından gelir ve gürültüyü büyük ölçüde
        keser (1105 sayfalık kitapta ~60 sayfaya inmek isabeti çok artırıyor).
        """
        match = _to_fts_query(query)
        if not match:
            return []

        sql = (
            "SELECT c.*, bm25(chunks_fts) AS score "
            "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ?"
        )
        params: list = [match]
        if page_range:
            sql += " AND c.page_end >= ? AND c.page_start <= ?"
            params += [page_range[0], page_range[1]]
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)

        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        # bm25() küçükse daha iyi; pozitif "yüksek daha iyi" skora çevir.
        return [(_row_to_chunk(r), -float(r["score"])) for r in rows]


def _row_to_figure(row: sqlite3.Row) -> BookFigure:
    return BookFigure(
        number=row["number"],
        caption=row["caption"],
        page=row["page"],
        bbox=(row["x0"], row["top"], row["x1"], row["bottom"]),
    )


def _row_to_chunk(row: sqlite3.Row) -> BookChunk:
    return BookChunk(
        chunk_id=row["chunk_id"],
        text=row["text"],
        section_title=row["section_title"],
        page_start=row["page_start"],
        page_end=row["page_end"],
        token_estimate=row["token_estimate"],
    )


def _to_fts_query(query: str, max_terms: int = 24) -> str:
    """Serbest metni güvenli bir FTS5 OR sorgusuna çevirir.

    Noktalama FTS5'te operatör anlamına geldiği için temizlenir; aksi halde
    "two's complement" gibi bir terim sözdizimi hatası verir.
    """
    cleaned = _FTS_SPECIAL.sub(" ", query)
    terms = [t for t in cleaned.split() if len(t) > 1]
    if not terms:
        return ""
    seen: list[str] = []
    for t in terms:
        low = t.lower()
        if low not in seen:
            seen.append(low)
        if len(seen) >= max_terms:
            break
    return " OR ".join(f'"{t}"' for t in seen)
