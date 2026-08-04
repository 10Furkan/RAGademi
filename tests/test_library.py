"""Kalıcı ders kitaplığı.

Buradaki testlerin çoğu **silme** üzerine. İçerik adresli depolamanın bedeli
orada ödeniyor: dosya bir derse değil bir SHA'ya ait, dolayısıyla "bu dersi
sil" ile "bu dosyayı sil" aynı şey değil. Sayımı yanlış yapan bir silme, aynı
kitabı kullanan başka bir dersin altını oyar ve bunu sessizce yapar.
"""

from __future__ import annotations

import hashlib

import pytest

from dersnotu.library import KINDS, LibraryStore, NotFound


@pytest.fixture
def lib(tmp_path):
    return LibraryStore(tmp_path / "library.sqlite", tmp_path / "materials")


def pdf(tmp_path, name: str, body: bytes):
    p = tmp_path / name
    p.write_bytes(body)
    return p, hashlib.sha256(body).hexdigest()


# --- dersler ---------------------------------------------------------------
def test_course_roundtrip(lib):
    c = lib.create_course("Bilgisayar Sistemleri", "BLG 212", "güz")
    assert lib.course(c.id).name == "Bilgisayar Sistemleri"
    assert [x.id for x in lib.courses()] == [c.id]
    assert lib.course(c.id).to_dict()["counts"] == {
        "lectures": 0, "books": 0, "documents": 0, "notes": 0, "practices": 0
    }


def test_blank_course_name_rejected(lib):
    with pytest.raises(ValueError):
        lib.create_course("   ")


def test_missing_course_raises_notfound(lib):
    with pytest.raises(NotFound):
        lib.course("yokboyle")


def test_rename_keeps_untouched_fields(lib):
    c = lib.create_course("Eski", "BLG 212", "not")
    y = lib.rename_course(c.id, name="Yeni")
    assert (y.name, y.code, y.note) == ("Yeni", "BLG 212", "not")


# --- materyaller -----------------------------------------------------------
def test_material_counts_appear_on_course(lib, tmp_path):
    c = lib.create_course("Ders")
    src, sha = pdf(tmp_path, "a.pdf", b"%PDF-a")
    lib.add_material(c.id, "lecture", "a.pdf", src, sha, pages=51)
    assert lib.course(c.id).lectures == 1
    assert lib.materials(c.id, "lecture")[0].pages == 51
    assert lib.materials(c.id, "book") == []


def test_same_file_uploaded_twice_does_not_duplicate(lib, tmp_path):
    """Kullanıcı 'yükle'ye iki kez basınca listede iki satır olmamalı."""
    c = lib.create_course("Ders")
    src, sha = pdf(tmp_path, "a.pdf", b"%PDF-a")
    first = lib.add_material(c.id, "lecture", "a.pdf", src, sha)
    again = lib.add_material(c.id, "lecture", "a.pdf", src, sha)
    assert first.id == again.id
    assert len(lib.materials(c.id)) == 1


def test_same_book_in_two_courses_is_stored_once(lib, tmp_path):
    """İçerik adresli depolamanın bütün amacı: 100 MB'lık kitap tek kopya."""
    a, b = lib.create_course("A"), lib.create_course("B")
    src, sha = pdf(tmp_path, "k.pdf", b"%PDF-kitap")
    ma = lib.add_material(a.id, "book", "k.pdf", src, sha)
    mb = lib.add_material(b.id, "book", "k.pdf", src, sha)

    assert ma.id != mb.id                    # iki ayrı kayıt
    assert ma.path == mb.path                # tek dosya
    assert len(list(lib.materials_dir.iterdir())) == 1


def test_deleting_one_course_keeps_the_shared_blob(lib, tmp_path):
    """Paylaşılan kitabı silmek diğer dersi bozardı — sayım bunu engelliyor."""
    a, b = lib.create_course("A"), lib.create_course("B")
    src, sha = pdf(tmp_path, "k.pdf", b"%PDF-kitap")
    lib.add_material(a.id, "book", "k.pdf", src, sha)
    mb = lib.add_material(b.id, "book", "k.pdf", src, sha)

    assert lib.delete_course(a.id)["materials"] == 0   # blob silinmedi
    assert mb.path.exists()
    assert lib.material(mb.id).to_dict()["available"] is True

    assert lib.delete_course(b.id)["materials"] == 1   # son referans gitti
    assert not mb.path.exists()


def test_deleting_material_removes_its_blob_when_last(lib, tmp_path):
    c = lib.create_course("Ders")
    src, sha = pdf(tmp_path, "a.pdf", b"%PDF-a")
    m = lib.add_material(c.id, "lecture", "a.pdf", src, sha)
    assert lib.delete_material(m.id) == {"files": 1}
    assert not m.path.exists()


def test_invalid_kind_rejected(lib, tmp_path):
    c = lib.create_course("Ders")
    src, sha = pdf(tmp_path, "a.pdf", b"%PDF-a")
    with pytest.raises(ValueError):
        lib.add_material(c.id, "notlar", "a.pdf", src, sha)
    assert set(KINDS) == {"lecture", "book", "exam"}


def test_exam_is_a_first_class_material(lib, tmp_path):
    c = lib.create_course("Ders")
    src, sha = pdf(tmp_path, "vize.pdf", b"%PDF-vize")
    m = lib.add_material(c.id, "exam", "2023-vize.pdf", src, sha, pages=2)
    assert [x.id for x in lib.materials(c.id, "exam")] == [m.id]
    # Sınav kâğıdı ders/kitap sayımlarını kirletmemeli.
    assert lib.course(c.id).lectures == 0 and lib.course(c.id).books == 0


# --- dokümanlar ------------------------------------------------------------
def _course_with_material(lib, tmp_path):
    c = lib.create_course("Ders")
    ls, lsha = pdf(tmp_path, "l.pdf", b"%PDF-ders")
    bs, bsha = pdf(tmp_path, "b.pdf", b"%PDF-kitap")
    return (
        c,
        lib.add_material(c.id, "lecture", "l.pdf", ls, lsha),
        lib.add_material(c.id, "book", "b.pdf", bs, bsha),
    )


def test_document_roundtrip(lib, tmp_path):
    c, lec, book = _course_with_material(lib, tmp_path)
    out = tmp_path / "o.pdf"
    out.write_bytes(b"%PDF-")
    d = lib.add_document(
        course_id=c.id, lecture_id=lec.id, book_id=book.id, title="Ders 2",
        pdf_path=out, language="Türkçe", depth="derin",
        extras=["analoji", "soru"], failed=[3], usage={"calls": 9},
    )
    got = lib.document(d.id).to_dict()
    assert got["extras"] == ["analoji", "soru"]
    assert got["failed_sections"] == [3]
    assert got["usage"] == {"calls": 9}
    assert got["has_pdf"] is True and got["has_md"] is False
    assert lib.course(c.id).documents == 1


def test_deleting_material_keeps_the_document(lib, tmp_path):
    """Çıktı kaynaklarından değerli: kaynak gidince doküman ölmez, bağı kopar."""
    c, lec, book = _course_with_material(lib, tmp_path)
    d = lib.add_document(course_id=c.id, lecture_id=lec.id, book_id=book.id,
                         title="Ders 2")
    assert lib.material_usage(lec.id) == 1

    lib.delete_material(lec.id)
    kalan = lib.document(d.id)
    assert kalan.lecture_id is None
    assert kalan.book_id == book.id


def test_retry_needs_the_intermediate_file(lib, tmp_path):
    """Hatalı bölüm var ama .doc.json yoksa yeniden deneme yapılamaz."""
    c, lec, book = _course_with_material(lib, tmp_path)
    ara = tmp_path / "x.doc.json"
    d = lib.add_document(course_id=c.id, title="X", failed=[1], doc_path=ara)
    assert lib.document(d.id).to_dict()["can_retry"] is False
    ara.write_text("{}", encoding="utf-8")
    assert lib.document(d.id).to_dict()["can_retry"] is True

    temiz = lib.add_document(course_id=c.id, title="Y", failed=[], doc_path=ara)
    assert lib.document(temiz.id).to_dict()["can_retry"] is False


def test_deleting_course_removes_generated_files(lib, tmp_path):
    c, lec, book = _course_with_material(lib, tmp_path)
    ciktilar = []
    for ad in ("o.pdf", "o.md", "o.doc.json"):
        p = tmp_path / ad
        p.write_text("x", encoding="utf-8")
        ciktilar.append(p)
    lib.add_document(course_id=c.id, title="X", pdf_path=ciktilar[0],
                     md_path=ciktilar[1], doc_path=ciktilar[2])

    ozet = lib.delete_course(c.id)
    assert ozet == {"materials": 2, "documents": 3}
    assert not any(p.exists() for p in ciktilar)
    with pytest.raises(NotFound):
        lib.course(c.id)


def test_document_id_can_be_reused_by_retry(lib, tmp_path):
    """Yeniden deneme AYNI dokümanı günceller; ders sayfasına satır eklemez."""
    c, lec, book = _course_with_material(lib, tmp_path)
    d = lib.add_document(id="sabit", course_id=c.id, title="X", failed=[2, 5])
    lib.add_document(id="sabit", course_id=c.id, title="X", failed=[5])
    assert lib.course(c.id).documents == 1
    assert lib.document("sabit").failed == [5]
    assert d.id == "sabit"


# --- şema göçü ------------------------------------------------------------
# v1 şeması: `kind` üzerinde CHECK var, dokümanda yeni sütunlar yok.
# Kullanıcının diskinde bu sürüm duruyor olabilir; göç yanlışsa uygulama
# `no such column` ile açılmaz ya da 'exam' yüklemesi CHECK'e takılır.
_V1 = """
CREATE TABLE courses (id TEXT PRIMARY KEY, name TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL);
CREATE TABLE materials (id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('lecture', 'book')),
    name TEXT NOT NULL, sha TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE documents (id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    lecture_id TEXT, book_id TEXT, title TEXT NOT NULL,
    pdf_path TEXT, md_path TEXT, doc_path TEXT,
    language TEXT NOT NULL DEFAULT '', depth TEXT NOT NULL DEFAULT '',
    extras TEXT NOT NULL DEFAULT '[]', backend TEXT NOT NULL DEFAULT '',
    failed TEXT NOT NULL DEFAULT '[]', usage TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL);
INSERT INTO courses VALUES ('c1', 'Eski Ders', 'BLG 212', '', '2026-01-01');
INSERT INTO materials VALUES ('m1', 'c1', 'book', 'k.pdf', 'abc', 10, 5, '2026-01-01');
INSERT INTO documents (id, course_id, title, created_at)
    VALUES ('d1', 'c1', 'Eski not', '2026-01-01');
"""


def test_v1_library_migrates_without_losing_data(tmp_path):
    import sqlite3

    db = tmp_path / "library.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(_V1)
    conn.commit()
    conn.close()

    lib = LibraryStore(db, tmp_path / "materials")

    # Veri duruyor
    assert lib.course("c1").name == "Eski Ders"
    assert lib.materials("c1")[0].name == "k.pdf"
    assert lib.document("d1").title == "Eski not"
    # Yeni sütunlar okunabiliyor
    assert lib.document("d1").duration == 0.0
    assert lib.document("d1").exam_id is None
    # CHECK kalktı: sınav kâğıdı artık yüklenebiliyor
    src, sha = pdf(tmp_path, "v.pdf", b"%PDF-v")
    assert lib.add_material("c1", "exam", "vize.pdf", src, sha).kind == "exam"
    # v3: göç öncesi yazılmış her satır ders notudur.
    assert lib.document("d1").kind == "note"
    assert lib.course("c1").to_dict()["counts"]["notes"] == 1


def test_migration_is_idempotent(tmp_path):
    db, mats = tmp_path / "library.sqlite", tmp_path / "materials"
    LibraryStore(db, mats).create_course("Ders")
    # İkinci açılış göçü tekrar uygulamamalı (uvicorn --reload, ardışık testler)
    lib = LibraryStore(db, mats)
    assert len(lib.courses()) == 1


# --- arama ----------------------------------------------------------------
def test_search_finds_sections_and_ranks_them(lib, tmp_path):
    c = lib.create_course("Ders")
    d = lib.add_document(course_id=c.id, title="Bits")
    lib.index_document(d.id, c.id, [
        (0, "Sayı gösterimi", "İkinin tümleyeni negatif sayıları kodlar."),
        (1, "Kayan nokta", "IEEE 754 mantis ve üs alanlarından oluşur."),
    ])
    hits = lib.search(c.id, "tümleyeni")
    assert len(hits) == 1
    assert hits[0].heading == "Sayı gösterimi"
    assert hits[0].document_title == "Bits"
    assert "<mark>" in hits[0].snippet


def test_search_survives_punctuation(lib, tmp_path):
    """FTS5'te noktalama işleçtir; ham gönderilen sorgu OperationalError verir."""
    c = lib.create_course("Ders")
    d = lib.add_document(course_id=c.id, title="X")
    lib.index_document(d.id, c.id, [(0, "B", "two's complement arithmetic")])
    assert lib.search(c.id, "two's complement")          # patlamamalı
    assert lib.search(c.id, "((") == []                   # sadece işleç → boş
    assert lib.search(c.id, "") == []


def test_search_requires_all_terms(lib, tmp_path):
    """Arama kutusu kesinlik ister: iki kelime yazan ikisini de geçeni bekler."""
    c = lib.create_course("Ders")
    d = lib.add_document(course_id=c.id, title="X")
    lib.index_document(d.id, c.id, [
        (0, "A", "kayan nokta sayıları"),
        (1, "B", "tamsayı taşması"),
    ])
    assert len(lib.search(c.id, "kayan nokta")) == 1
    assert lib.search(c.id, "kayan taşması") == []


def test_reindexing_a_document_replaces_its_rows(lib, tmp_path):
    """Yeniden deneme aynı kimlikle indeksler; eskisi silinmezse çift eşleşir."""
    c = lib.create_course("Ders")
    d = lib.add_document(course_id=c.id, title="X")
    lib.index_document(d.id, c.id, [(0, "A", "taşma")])
    lib.index_document(d.id, c.id, [(0, "A", "taşma")])
    assert len(lib.search(c.id, "taşma")) == 1


def test_deleting_removes_rows_from_search(lib, tmp_path):
    """Sanal tabloya cascade işlemez; elle silinmezse silinen ders aramada kalır."""
    c = lib.create_course("Ders")
    d = lib.add_document(course_id=c.id, title="X")
    lib.index_document(d.id, c.id, [(0, "A", "taşma")])

    lib.delete_document(d.id)
    assert lib.search(c.id, "taşma") == []

    d2 = lib.add_document(course_id=c.id, title="Y")
    lib.index_document(d2.id, c.id, [(0, "A", "taşma")])
    lib.delete_course(c.id)
    assert lib.search(c.id, "taşma") == []


# --- süre kalibrasyonu ----------------------------------------------------
def test_section_seconds_come_from_real_runs(lib):
    c = lib.create_course("Ders")
    lib.add_document(course_id=c.id, title="A", backend="cli",
                     duration=800.0, sections=8)   # 100 sn/bölüm
    lib.add_document(course_id=c.id, title="B", backend="cli",
                     duration=600.0, sections=4)   # 150 sn/bölüm
    lib.add_document(course_id=c.id, title="C", backend="api",
                     duration=60.0, sections=6)
    # Süresi olmayan koşu hesaba girmemeli, yoksa medyanı sıfıra çeker.
    lib.add_document(course_id=c.id, title="D", backend="cli")

    assert sorted(lib.section_seconds("cli")) == [100.0, 150.0]
    assert lib.section_seconds("api") == [10.0]
    assert lib.section_seconds("demo") == []


def test_practice_runs_do_not_pollute_the_note_estimate(lib):
    """Deneme sınavı TEK çağrıda üretiliyor ve `sections` orada SORU sayısını
    tutuyor. Tür filtresi olmasaydı "12 soru / 40 sn" koşusu ders notu
    tahmininde "bölüm başına 3 sn" diye okunur ve rakamı yerle bir ederdi."""
    c = lib.create_course("Ders")
    lib.add_document(course_id=c.id, title="Not", backend="api",
                     duration=300.0, sections=6)               # 50 sn/bölüm
    lib.add_document(course_id=c.id, title="Deneme", kind="practice",
                     backend="api", duration=40.0, sections=12)  # 3.3 sn/soru

    assert lib.section_seconds("api") == [50.0]
    assert lib.section_seconds("api", kind="practice") == [pytest.approx(3.333, rel=1e-3)]


def test_unknown_document_kind_is_rejected(lib):
    c = lib.create_course("Ders")
    with pytest.raises(ValueError):
        lib.add_document(course_id=c.id, title="X", kind="sinav")


def test_search_hit_carries_the_document_kind(lib):
    """Okuyucudaki çıpa adı türe göre değişiyor (`#bolum-N` / `#soru-N`);
    arayüz onu bu alandan seçiyor."""
    c = lib.create_course("Ders")
    d = lib.add_document(course_id=c.id, title="Deneme", kind="practice")
    lib.index_document(d.id, c.id, [(3, "Soru 3", "İkinin tümleyeni sorusu")])
    assert lib.search(c.id, "tümleyeni")[0].document_kind == "practice"


def test_course_counts_split_notes_from_practice_papers(lib):
    c = lib.create_course("Ders")
    lib.add_document(course_id=c.id, title="A")
    lib.add_document(course_id=c.id, title="B", kind="practice")
    n = lib.course(c.id).to_dict()["counts"]
    assert (n["documents"], n["notes"], n["practices"]) == (2, 1, 1)


def test_missing_output_file_is_reported_not_hidden(lib, tmp_path):
    """Dosya elle silinirse arayüz indirme bağlantısını göstermemeli."""
    c = lib.create_course("Ders")
    yok = tmp_path / "yok.pdf"
    d = lib.add_document(course_id=c.id, title="X", pdf_path=yok)
    assert lib.document(d.id).to_dict()["has_pdf"] is False
