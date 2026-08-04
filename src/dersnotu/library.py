"""Kalıcı ders kitaplığı: dersler, materyaller ve üretilmiş dokümanlar.

`JobStore` bilerek bellekte tutuluyor — bir koşunun canlı durumu sunucunun
ömrüyle sınırlı olabilir. Kitaplık tam tersi: kullanıcının "geçen hafta
yüklediğim kitap" dediği şey yeniden başlatmayı atlatmak zorunda. Bu yüzden
SQLite.

**Materyaller içerik adresli saklanır** (`.cache/materials/<sha16>.pdf`). Aynı
ders kitabını iki derse yüklemek diskte ikinci bir kopya açmaz ve — kitap
indeksi de içerik adresli olduğu için — kitabı ikinci kez indekslemez. Bu, 100
MB'lık bir ders kitabı ve 25 sn'lik bir indeksleme için önemsiz bir kazanç
değil.

Bunun bedeli silmede ödenir: bir blob'u silmek ancak ona bakan **son** satır
gidince güvenlidir. `_collect_garbage` bu sayımı yapar; sha hâlâ başka bir
derste geçiyorsa dosya yerinde kalır.

Üretilmiş dokümanlar `out/` altında kalır ve satırları yalnızca yolları taşır.
Dosya elle silinirse satır öksüz kalır — arayüz bunu `available` alanından
görür ve indirme bağlantısını göstermez.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Materyal türleri. Üçü de tamamen farklı muamele görüyor: ders modele bütün
# gider, kitap asla bütün gitmez, sınav kâğıdı ise cache'lenen önekte taşınır.
# Tür bir etiket değil, işleme yolunu seçen şey.
KINDS = ("lecture", "book", "exam")

# Üretilen belge türleri. `note` kitapla genişletilmiş ders notu, `practice`
# geçmiş sınava benzetilerek üretilmiş deneme sınavı. İkisi aynı tabloda
# duruyor çünkü yaşam döngüleri aynı (üret → oku → indir → sil) ve arama
# ikisini birden taramalı; ayrıştıkları yer `sections` alanının anlamı
# (bölüm sayısı / soru sayısı) ve süre kalibrasyonu.
DOC_KINDS = ("note", "practice")

# `PRAGMA user_version` ile takip edilir. Şema değişince ARTTIR ve `_migrate`
# içine adımı yaz — kullanıcının kitaplığı silinebilir bir önbellek değil.
SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    code        TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- `kind` üzerinde CHECK kısıtı YOK ve olmamalı: SQLite'ta CHECK değiştirilemez,
-- yeni bir tür eklemek tabloyu yeniden kurmayı gerektirir. Doğrulama
-- `add_material` içinde, KINDS'e karşı yapılıyor.
CREATE TABLE IF NOT EXISTS materials (
    id          TEXT PRIMARY KEY,
    course_id   TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    sha         TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    pages       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS materials_by_course ON materials(course_id);
CREATE INDEX IF NOT EXISTS materials_by_sha ON materials(sha);

-- Materyal silinince doküman ÖLMEZ, sadece bağı kopar: çıktı kaynaklarından
-- daha değerli. Arayüz kaynağı olmayan dokümanda "yeniden dene"yi gizler.
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    course_id   TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    lecture_id  TEXT REFERENCES materials(id) ON DELETE SET NULL,
    book_id     TEXT REFERENCES materials(id) ON DELETE SET NULL,
    exam_id     TEXT REFERENCES materials(id) ON DELETE SET NULL,
    -- 'note' | 'practice'. Varsayılanı 'note': göç öncesi yazılmış her satır
    -- ders notudur, deneme sınavı bu sütunla birlikte geldi.
    kind        TEXT NOT NULL DEFAULT 'note',
    title       TEXT NOT NULL,
    pdf_path    TEXT,
    md_path     TEXT,
    doc_path    TEXT,
    html_path   TEXT,
    language    TEXT NOT NULL DEFAULT '',
    depth       TEXT NOT NULL DEFAULT '',
    extras      TEXT NOT NULL DEFAULT '[]',
    backend     TEXT NOT NULL DEFAULT '',
    failed      TEXT NOT NULL DEFAULT '[]',
    usage       TEXT NOT NULL DEFAULT '{}',
    -- Süre tahminini kalibre eder: gerçek koşuların medyanı, sabit varsayımı
    -- yener. `sections` olmadan saniye/bölüm hesaplanamaz, ikisi birlikte.
    duration    REAL NOT NULL DEFAULT 0,
    sections    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_by_course ON documents(course_id);

-- Ders notlarında tam metin arama. Harici içerik değil bağımsız tablo:
-- bölümler `documents` satırlarında değil markdown dosyasında duruyor.
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
    document_id UNINDEXED,
    course_id   UNINDEXED,
    section     UNINDEXED,
    heading,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


# `documents` tablosunun şema ötesi sütunları: sürüm sürüm eklendiler ve
# hepsi `ALTER TABLE ADD COLUMN` ile geriye dönük eklenebilir. Liste burada,
# tek yerde: `_SCHEMA` taze veritabanını kurar, bu liste eskisini yetiştirir.
_DOC_COLUMNS = (
    ("exam_id", "TEXT"),                            # v2
    ("html_path", "TEXT"),                          # v2
    ("duration", "REAL NOT NULL DEFAULT 0"),        # v2
    ("sections", "INTEGER NOT NULL DEFAULT 0"),     # v2
    # v3 — deneme sınavları da bu tabloda. Varsayılan 'note' olduğu için göç
    # öncesi yazılmış her satır doğru türü kendiliğinden alır.
    ("kind", "TEXT NOT NULL DEFAULT 'note'"),
)


def _migrate(c: sqlite3.Connection) -> None:
    """Var olan bir kitaplığı güncel şemaya taşır.

    `CREATE TABLE IF NOT EXISTS` yeni sütun EKLEMEZ: taze bir veritabanı
    yukarıdaki şemayı alır, eskisi olduğu yerde kalır ve ilk sorguda
    `no such column` ile patlar. Kitaplık kullanıcının ders listesi — silip
    yeniden kurmak bir seçenek değil, göç yazılır.

    **Damga tek başına yeterli bir kapı DEĞİL.** Eskiden bu fonksiyon
    `user_version >= SCHEMA_VERSION` ise hemen dönüyordu. `SCHEMA_VERSION`
    arttırılıp ona karşılık gelen adım henüz yazılmamışken kitaplık bir kez
    açılırsa — geliştirme sırasında bir dakikalık bir aralık — damga yazılır,
    sütun eklenmez ve o erken dönüş bozuk hâli KALICI kilitler: sonraki her
    açılış "zaten güncel" deyip geçer, her sorgu `no such column` ile patlar.
    Kurtuluş yolu da yoktur.

    Bu yüzden her adım damgaya bakılmadan, her açılışta doğrulanıyor. Hepsi
    idempotent ve "yapılacak bir şey var mı" sorusunu şemanın KENDİSİNE
    soruyor, damgaya değil. Bedeli üç okuma; karşılığı, damganın
    yalanlayamayacağı bir şema.

    Damga yine de yazılıyor — ama artık bir kapı değil, bir kayıt: hangi
    sürümün beklendiğini söyler ve göçün ne zaman koştuğunu okunur kılar.
    Yeni bir adım eklerken `SCHEMA_VERSION`'ı arttır, ama adımı da bu
    fonksiyonun ŞEMAYA BAKAN diline yaz — "sürüm küçükse" diline değil.
    """
    # Ucuz ve idempotent: sütun varsa dokunulmaz.
    for ad, tanim in _DOC_COLUMNS:
        _add_column(c, "documents", ad, tanim)

    # v1'de `kind` üzerinde CHECK vardı; 'exam' onu ihlal ederdi ve SQLite
    # CHECK'i ALTER ile kaldırmaya izin vermiyor. Kendi koşulunu sqlite_master'
    # dan okuyor, damgadan değil.
    _drop_kind_check(c)

    if c.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _add_column(c: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    var = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    if name not in var:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _drop_kind_check(c: sqlite3.Connection) -> None:
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='materials'"
    ).fetchone()
    if row is None or "CHECK" not in (row["sql"] or ""):
        return
    # SQLite'ın önerdiği yol: yeni tabloyu kur, kopyala, takas et.
    c.executescript("""
        PRAGMA foreign_keys = OFF;
        CREATE TABLE materials_new (
            id TEXT PRIMARY KEY,
            course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, name TEXT NOT NULL, sha TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        INSERT INTO materials_new SELECT id, course_id, kind, name, sha, size,
               pages, created_at FROM materials;
        DROP TABLE materials;
        ALTER TABLE materials_new RENAME TO materials;
        CREATE INDEX IF NOT EXISTS materials_by_course ON materials(course_id);
        CREATE INDEX IF NOT EXISTS materials_by_sha ON materials(sha);
        PRAGMA foreign_keys = ON;
    """)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Kayıtlar
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Course:
    id: str
    name: str
    code: str
    note: str
    created_at: str
    lectures: int = 0
    books: int = 0
    documents: int = 0  # toplam üretilmiş belge
    notes: int = 0
    practices: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "note": self.note,
            "created_at": self.created_at,
            "counts": {
                "lectures": self.lectures,
                "books": self.books,
                "documents": self.documents,
                "notes": self.notes,
                "practices": self.practices,
            },
        }


@dataclass(frozen=True)
class Material:
    id: str
    course_id: str
    kind: str
    name: str
    sha: str
    size: int
    pages: int
    created_at: str
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "kind": self.kind,
            "name": self.name,
            "sha16": self.sha[:16],
            "size": self.size,
            "pages": self.pages,
            "created_at": self.created_at,
            "available": bool(self.path and self.path.exists()),
        }


@dataclass(frozen=True)
class Document:
    id: str
    course_id: str
    lecture_id: str | None
    book_id: str | None
    title: str
    pdf_path: Path | None
    md_path: Path | None
    doc_path: Path | None
    language: str
    depth: str
    exam_id: str | None = None
    kind: str = "note"  # note | practice
    html_path: Path | None = None
    extras: list[str] = field(default_factory=list)
    backend: str = ""
    failed: list[int] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    duration: float = 0.0
    sections: int = 0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "lecture_id": self.lecture_id,
            "book_id": self.book_id,
            "exam_id": self.exam_id,
            "kind": self.kind,
            "title": self.title,
            "language": self.language,
            "depth": self.depth,
            "extras": self.extras,
            "backend": self.backend,
            "failed_sections": self.failed,
            "usage": self.usage,
            "duration": round(self.duration),
            # Ders notunda bölüm, deneme sınavında soru sayısı.
            "sections": self.sections,
            "created_at": self.created_at,
            "has_pdf": bool(self.pdf_path and self.pdf_path.exists()),
            "has_md": bool(self.md_path and self.md_path.exists()),
            "has_html": bool(self.html_path and self.html_path.exists()),
            # Yeniden deneme hem hatalı bölüm hem de kaynak dosyalar ister;
            # ikincisini sunucu doğruluyor, burada yalnızca ilki bilinir.
            "can_retry": bool(
                self.failed and self.doc_path and self.doc_path.exists()
            ),
        }


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    document_title: str
    section: int
    heading: str
    snippet: str
    # Belgenin türü sonuçla birlikte taşınıyor: okuyucudaki çıpa adı türe göre
    # değişiyor (`#bolum-N` / `#soru-N`) ve arayüz onu bu alandan seçiyor.
    document_kind: str = "note"

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_title": self.document_title,
            "document_kind": self.document_kind,
            "section": self.section,
            "heading": self.heading,
            "snippet": self.snippet,
        }


class NotFound(LookupError):
    """İstenen kayıt yok. Sunucu bunu 404'e çevirir."""


# ---------------------------------------------------------------------------
# Depo
# ---------------------------------------------------------------------------
class LibraryStore:
    """Dersler / materyaller / dokümanlar için SQLite deposu.

    Her işlem kendi bağlantısını açar. Uzun ömürlü tek bir bağlantı tutmak,
    isteklerin thread havuzuna dağıldığı bir FastAPI uygulamasında
    `ProgrammingError: SQLite objects created in a thread...` demek olurdu; bu
    ölçekte bağlantı açmanın maliyeti o riske değmez.
    """

    def __init__(self, db_path: Path, materials_dir: Path):
        self.db_path = Path(db_path)
        self.materials_dir = Path(materials_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.materials_dir.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            _migrate(c)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Cascade ve SET NULL yalnızca bu pragma açıkken çalışır ve bağlantı
        # başına ayarlanır — şemaya yazmak yetmez.
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def blob_path(self, sha: str) -> Path:
        return self.materials_dir / f"{sha[:16]}.pdf"

    # ----- dersler -------------------------------------------------------
    def create_course(self, name: str, code: str = "", note: str = "") -> Course:
        name = name.strip()
        if not name:
            raise ValueError("Ders adı boş olamaz.")
        course = Course(
            id=_uid(), name=name, code=code.strip(), note=note.strip(),
            created_at=_now(),
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO courses (id, name, code, note, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (course.id, course.name, course.code, course.note, course.created_at),
            )
        return course

    def rename_course(self, course_id: str, *, name=None, code=None, note=None) -> Course:
        cur = self.course(course_id)
        yeni = {
            "name": (name if name is not None else cur.name).strip(),
            "code": (code if code is not None else cur.code).strip(),
            "note": (note if note is not None else cur.note).strip(),
        }
        if not yeni["name"]:
            raise ValueError("Ders adı boş olamaz.")
        with self._conn() as c:
            c.execute(
                "UPDATE courses SET name = ?, code = ?, note = ? WHERE id = ?",
                (yeni["name"], yeni["code"], yeni["note"], course_id),
            )
        return self.course(course_id)

    def course(self, course_id: str) -> Course:
        with self._conn() as c:
            row = c.execute(
                _COURSE_SELECT + " WHERE c.id = ?", (course_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"Ders bulunamadı: {course_id}")
        return _row_to_course(row)

    def courses(self) -> list[Course]:
        with self._conn() as c:
            rows = c.execute(_COURSE_SELECT + " ORDER BY c.created_at DESC").fetchall()
        return [_row_to_course(r) for r in rows]

    def delete_course(self, course_id: str) -> dict[str, int]:
        """Dersi, materyallerini ve ürettiği dokümanları siler.

        Dönen özet arayüzde "ne silindi" demek için kullanılıyor; sessizce
        dosya silen bir uçtan daha iyisi, ne sildiğini söyleyen bir uç.
        """
        self.course(course_id)  # yoksa NotFound
        with self._conn() as c:
            shas = [
                r["sha"]
                for r in c.execute(
                    "SELECT DISTINCT sha FROM materials WHERE course_id = ?",
                    (course_id,),
                )
            ]
            ciktilar = _output_paths(
                c.execute(
                    "SELECT pdf_path, md_path, doc_path, html_path FROM documents"
                    " WHERE course_id = ?",
                    (course_id,),
                ).fetchall()
            )
            # doc_fts sanal tablo: yabancı anahtarı yok, cascade ona işlemez.
            # Elle silinmezse silinmiş dersin bölümleri aramada çıkmaya devam eder.
            c.execute("DELETE FROM doc_fts WHERE course_id = ?", (course_id,))
            c.execute("DELETE FROM courses WHERE id = ?", (course_id,))

        return {
            "materials": self._collect_garbage(shas),
            "documents": _unlink_all(ciktilar),
        }

    # ----- materyaller ---------------------------------------------------
    def add_material(
        self, course_id: str, kind: str, name: str, src: Path, sha: str,
        pages: int = 0,
    ) -> Material:
        """PDF'i içerik adresli depoya alır ve derse bağlar.

        `src` çağıran tarafından yazılmış geçici bir dosya; SHA'sı zaten
        hesaplanmış olarak geliyor (sunucu onu indeks önbelleğini sorgulamak
        için de kullanıyor, iki kez okumanın anlamı yok).
        """
        if kind not in KINDS:
            raise ValueError(f"Geçersiz materyal türü: {kind}")
        self.course(course_id)

        # Aynı dosya aynı derse ikinci kez yüklendi: yeni satır açma, mevcut
        # olanı döndür. Kullanıcı "yükle"ye iki kez basmış olabilir.
        with self._conn() as c:
            var = c.execute(
                "SELECT * FROM materials WHERE course_id = ? AND kind = ? AND sha = ?",
                (course_id, kind, sha),
            ).fetchone()
        if var is not None:
            return self._row_to_material(var)

        hedef = self.blob_path(sha)
        if not hedef.exists():
            hedef.write_bytes(Path(src).read_bytes())

        mat = Material(
            id=_uid(), course_id=course_id, kind=kind, name=name, sha=sha,
            size=hedef.stat().st_size, pages=pages, created_at=_now(),
            path=hedef,
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO materials"
                " (id, course_id, kind, name, sha, size, pages, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mat.id, course_id, kind, mat.name, sha, mat.size, pages,
                 mat.created_at),
            )
        return mat

    def material(self, material_id: str) -> Material:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM materials WHERE id = ?", (material_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"Materyal bulunamadı: {material_id}")
        return self._row_to_material(row)

    def materials(self, course_id: str, kind: str | None = None) -> list[Material]:
        sql = "SELECT * FROM materials WHERE course_id = ?"
        args: list[Any] = [course_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        with self._conn() as c:
            rows = c.execute(sql + " ORDER BY created_at DESC", args).fetchall()
        return [self._row_to_material(r) for r in rows]

    def delete_material(self, material_id: str) -> dict[str, int]:
        mat = self.material(material_id)
        with self._conn() as c:
            c.execute("DELETE FROM materials WHERE id = ?", (material_id,))
        return {"files": self._collect_garbage([mat.sha])}

    def material_usage(self, material_id: str) -> int:
        """Bu materyalden kaç doküman üretilmiş. Silme onayında gösteriliyor."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM documents"
                " WHERE lecture_id = ? OR book_id = ?",
                (material_id, material_id),
            ).fetchone()
        return int(row["n"])

    # ----- dokümanlar ----------------------------------------------------
    def add_document(self, **kw: Any) -> Document:
        kind = kw.get("kind", "note")
        if kind not in DOC_KINDS:
            raise ValueError(f"Geçersiz belge türü: {kind}")
        doc = Document(
            id=kw.get("id") or _uid(),
            course_id=kw["course_id"],
            lecture_id=kw.get("lecture_id"),
            book_id=kw.get("book_id"),
            exam_id=kw.get("exam_id"),
            kind=kind,
            title=kw.get("title", "Ders notu"),
            pdf_path=_as_path(kw.get("pdf_path")),
            md_path=_as_path(kw.get("md_path")),
            doc_path=_as_path(kw.get("doc_path")),
            html_path=_as_path(kw.get("html_path")),
            language=kw.get("language", ""),
            depth=kw.get("depth", ""),
            extras=list(kw.get("extras") or []),
            backend=kw.get("backend", ""),
            failed=list(kw.get("failed") or []),
            usage=dict(kw.get("usage") or {}),
            duration=float(kw.get("duration") or 0.0),
            sections=int(kw.get("sections") or 0),
            created_at=kw.get("created_at") or _now(),
        )
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO documents (id, course_id, lecture_id,"
                " book_id, exam_id, kind, title, pdf_path, md_path, doc_path, html_path,"
                " language, depth, extras, backend, failed, usage, duration,"
                " sections, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.id, doc.course_id, doc.lecture_id, doc.book_id, doc.exam_id,
                    doc.kind,
                    doc.title, _as_str(doc.pdf_path), _as_str(doc.md_path),
                    _as_str(doc.doc_path), _as_str(doc.html_path),
                    doc.language, doc.depth, json.dumps(doc.extras),
                    doc.backend, json.dumps(doc.failed), json.dumps(doc.usage),
                    doc.duration, doc.sections, doc.created_at,
                ),
            )
        return doc

    # ----- arama ---------------------------------------------------------
    def index_document(self, doc_id: str, course_id: str,
                       sections: list[tuple[int, str, str]]) -> None:
        """Bir dokümanın bölümlerini arama tablosuna yazar.

        Yeniden deneme aynı kimlikle tekrar çağırır; önce eskisi silinir,
        yoksa aynı bölüm iki kez eşleşir.
        """
        with self._conn() as c:
            c.execute("DELETE FROM doc_fts WHERE document_id = ?", (doc_id,))
            c.executemany(
                "INSERT INTO doc_fts (document_id, course_id, section, heading, body)"
                " VALUES (?, ?, ?, ?, ?)",
                [(doc_id, course_id, i, h, b) for i, h, b in sections],
            )

    def search(self, course_id: str, query: str, limit: int = 20) -> list[SearchHit]:
        """Dersin tüm ders notlarında tam metin araması.

        Sorgu `_to_fts_query`'den geçmek ZORUNDA: FTS5'te noktalama işleci
        sayılır, `two's complement` ham gönderilirse sözdizimi hatası verir.
        """
        ifade = _to_fts_query(query)
        if not ifade:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT f.document_id, f.section, f.heading,"
                "       snippet(doc_fts, 4, '<mark>', '</mark>', '…', 18) AS snip,"
                "       d.title AS doc_title, d.kind AS doc_kind"
                "  FROM doc_fts f JOIN documents d ON d.id = f.document_id"
                " WHERE f.course_id = ? AND doc_fts MATCH ?"
                " ORDER BY rank LIMIT ?",
                (course_id, ifade, limit),
            ).fetchall()
        return [
            SearchHit(
                document_id=r["document_id"], document_title=r["doc_title"],
                document_kind=r["doc_kind"],
                section=r["section"], heading=r["heading"], snippet=r["snip"],
            )
            for r in rows
        ]

    def section_seconds(
        self, backend: str, limit: int = 10, kind: str = "note"
    ) -> list[float]:
        """Son koşulardan birim başına saniye — süre tahminini kalibre eder.

        `kind` filtresi zorunlu, süs değil: deneme sınavı TEK çağrıda üretilir
        ve `sections` alanı orada SORU sayısını tutar. Filtresiz bir sorgu
        "12 soru / 40 saniye" koşusunu "bölüm başına 3 saniye" diye okur ve
        ders notu tahminini yerle bir eder.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT duration, sections FROM documents"
                " WHERE backend = ? AND kind = ? AND duration > 0 AND sections > 0"
                " ORDER BY created_at DESC LIMIT ?",
                (backend, kind, limit),
            ).fetchall()
        return [r["duration"] / r["sections"] for r in rows]

    def document(self, document_id: str) -> Document:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"Doküman bulunamadı: {document_id}")
        return _row_to_document(row)

    def documents(self, course_id: str) -> list[Document]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM documents WHERE course_id = ? ORDER BY created_at DESC",
                (course_id,),
            ).fetchall()
        return [_row_to_document(r) for r in rows]

    def delete_document(self, document_id: str) -> dict[str, int]:
        doc = self.document(document_id)
        with self._conn() as c:
            c.execute("DELETE FROM doc_fts WHERE document_id = ?", (document_id,))
            c.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        return {
            "files": _unlink_all([
                p for p in (doc.pdf_path, doc.md_path, doc.doc_path, doc.html_path)
                if p
            ])
        }

    # ----- iç yardımcılar ------------------------------------------------
    def _collect_garbage(self, shas: list[str]) -> int:
        """Artık hiçbir satırın işaret etmediği blob'ları siler.

        İçerik adresli depolamanın bedeli tam olarak burası: dosya bir derse
        değil, bir SHA'ya ait. Sayımı atlamak, aynı kitabı kullanan başka bir
        dersin altını oymak demek olurdu.
        """
        silinen = 0
        with self._conn() as c:
            for sha in set(shas):
                kalan = c.execute(
                    "SELECT COUNT(*) AS n FROM materials WHERE sha = ?", (sha,)
                ).fetchone()["n"]
                if kalan:
                    continue
                blob = self.blob_path(sha)
                if blob.exists():
                    blob.unlink()
                    silinen += 1
        return silinen

    def _row_to_material(self, row: sqlite3.Row) -> Material:
        return Material(
            id=row["id"], course_id=row["course_id"], kind=row["kind"],
            name=row["name"], sha=row["sha"], size=row["size"],
            pages=row["pages"], created_at=row["created_at"],
            path=self.blob_path(row["sha"]),
        )


_COURSE_SELECT = """
SELECT c.*,
  (SELECT COUNT(*) FROM materials m
    WHERE m.course_id = c.id AND m.kind = 'lecture') AS lectures,
  (SELECT COUNT(*) FROM materials m
    WHERE m.course_id = c.id AND m.kind = 'book')    AS books,
  (SELECT COUNT(*) FROM documents d
    WHERE d.course_id = c.id)                        AS documents,
  (SELECT COUNT(*) FROM documents d
    WHERE d.course_id = c.id AND d.kind = 'note')     AS notes,
  (SELECT COUNT(*) FROM documents d
    WHERE d.course_id = c.id AND d.kind = 'practice') AS practices
FROM courses c
"""


def _row_to_course(row: sqlite3.Row) -> Course:
    return Course(
        id=row["id"], name=row["name"], code=row["code"], note=row["note"],
        created_at=row["created_at"], lectures=row["lectures"],
        books=row["books"], documents=row["documents"],
        notes=row["notes"], practices=row["practices"],
    )


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"], course_id=row["course_id"], lecture_id=row["lecture_id"],
        book_id=row["book_id"], exam_id=row["exam_id"], kind=row["kind"],
        title=row["title"],
        pdf_path=_as_path(row["pdf_path"]), md_path=_as_path(row["md_path"]),
        doc_path=_as_path(row["doc_path"]), html_path=_as_path(row["html_path"]),
        language=row["language"], depth=row["depth"],
        extras=json.loads(row["extras"]),
        backend=row["backend"], failed=json.loads(row["failed"]),
        usage=json.loads(row["usage"]), duration=row["duration"],
        sections=row["sections"], created_at=row["created_at"],
    )


# Retrieval tarafındaki (`index/store.py`) eşdeğeri terimleri OR ile bağlar:
# orada amaç GERİ ÇAĞIRMA, modele bol alıntı ulaşsın. Arama kutusunda amaç
# KESİNLİK — "two's complement" arayan kullanıcı ikisini birden geçen bölümü
# ister — o yüzden burada AND. Noktalama iki yerde de temizlenmek zorunda;
# FTS5'te işleç anlamına gelir ve ham gönderilirse sorgu sözdizimiyle patlar.
_FTS_SPECIAL = re.compile(r"[^\w\s]", re.UNICODE)


def _to_fts_query(query: str, max_terms: int = 12) -> str:
    terms: list[str] = []
    for t in _FTS_SPECIAL.sub(" ", query).split():
        low = t.lower()
        if len(low) > 1 and low not in terms:
            terms.append(low)
        if len(terms) >= max_terms:
            break
    if not terms:
        return ""
    # Son terime önek yıldızı: kullanıcı "compl" yazarken "complement" bulunur.
    *bas, son = (f'"{t}"' for t in terms)
    return " AND ".join([*bas, f"{son}*"])


def _as_path(v: Any) -> Path | None:
    return Path(v) if v else None


def _as_str(p: Path | None) -> str | None:
    return str(p) if p else None


def _output_paths(rows: list[sqlite3.Row]) -> list[Path]:
    alanlar = ("pdf_path", "md_path", "doc_path", "html_path")
    return [Path(r[k]) for r in rows for k in alanlar if r[k]]


def _unlink_all(paths: list[Path]) -> int:
    n = 0
    for p in paths:
        if p.exists():
            p.unlink()
            n += 1
    return n
