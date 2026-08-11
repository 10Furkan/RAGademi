"""Maliyet / süre projeksiyonu.

Bu hesap kullanıcının PARA harcayıp harcamamaya karar verdiği yer, o yüzden
yanlış olmasındansa açıkça "bilmiyorum" demesi yeğ. Buradaki testler iki şeyi
koruyor: rakamların yönü (ne neyi büyütür) ve tahminin kendini geçmişle
kalibre etmesi.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dersnotu.config import Settings
from dersnotu.estimate import FALLBACK_SECONDS, project, seconds_per_section
from dersnotu.models import Lecture, LectureSection, Slide

LECTURE = Path("Lecture02 - Bitsints.pptx.pdf")
HAVE_LECTURE = LECTURE.exists()


def make_lecture(n_sections: int = 4, visual: bool = False) -> Lecture:
    slides, sections = [], []
    for i in range(n_sections):
        blok = [
            Slide(number=i * 3 + j, title=f"S{i}.{j}", text="metin " * 60,
                  is_visual=visual)
            for j in range(3)
        ]
        slides += blok
        sections.append(LectureSection(index=i, slides=blok))
    return Lecture(source_path="x.pdf", source_sha256="abc", title="Ders",
                   slides=slides, sections=sections)


@pytest.fixture
def st(tmp_path):
    return Settings(cache_dir=tmp_path / "c", out_dir=tmp_path / "o")


# --- süre kalibrasyonu ----------------------------------------------------
def test_history_beats_the_fallback():
    """Sabit varsayım yalnızca ilk koşu için; sonrası kendi geçmişinden öğrenir."""
    assert seconds_per_section("cli") == (FALLBACK_SECONDS["cli"], "estimate")
    assert seconds_per_section("cli", [100.0, 120.0, 110.0]) == (110.0, "history")


def test_median_not_mean():
    """Tek bir kötü koşu (ağ kesintisi, kota beklemesi) ortalamayı savurur."""
    sure, _ = seconds_per_section("api", [20.0, 22.0, 21.0, 9000.0])
    assert sure < 100


# --- projeksiyon ----------------------------------------------------------
def test_more_sections_cost_more(st):
    az = project(make_lecture(2), Path("yok.pdf"), st)
    cok = project(make_lecture(8), Path("yok.pdf"), st)
    assert cok.cost > az.cost
    assert cok.sections == 8


def test_section_limit_is_respected(st):
    p = project(make_lecture(8), Path("yok.pdf"), st, limit_sections=2)
    assert p.sections == 2
    assert p.cost < project(make_lecture(8), Path("yok.pdf"), st).cost


def test_cache_read_grows_with_sections_write_does_not(st):
    """Prompt caching'in tüm mantığı: önek bir kez yazılır, N-1 kez okunur."""
    p = project(make_lecture(6), Path("yok.pdf"), st)
    assert p.cache_read == pytest.approx(p.cache_write * 5)
    assert p.cost < p.no_cache_cost


def test_exam_text_enlarges_the_cached_prefix(st):
    yalin = project(make_lecture(4), Path("yok.pdf"), st)
    sinavli = project(make_lecture(4), Path("yok.pdf"), st, exam_chars=20_000)
    assert sinavli.cache_write > yalin.cache_write
    assert sinavli.cost > yalin.cost


def test_subscription_backend_has_no_dollar_cost(st):
    """Claude Pro'da token ücreti yok; kota harcanır. Fiyat göstermek yalan olur."""
    assert project(make_lecture(4), Path("yok.pdf"), st, backend="cli").cost == 0
    assert project(make_lecture(4), Path("yok.pdf"), st, backend="codex").cost == 0
    assert project(make_lecture(4), Path("yok.pdf"), st, backend="demo").cost == 0
    assert project(make_lecture(4), Path("yok.pdf"), st, backend="api").cost > 0


def test_unindexed_book_adds_indexing_time_and_says_so(st):
    """İlk koşuda kitap indekslenecek; kullanıcının bekleyeceği süre odur."""
    p = project(make_lecture(4), Path("yok.pdf"), st, book_sha="hicyok")
    assert p.book_indexed is False
    assert p.index_seconds > 0
    assert p.seconds > p.index_seconds


def test_dict_shape_is_stable(st):
    """Arayüz bu anahtarları okuyor; sessizce değişirse şerit boş görünür."""
    d = project(make_lecture(3), Path("yok.pdf"), st).to_dict()
    assert {"model", "sections", "cost", "seconds", "seconds_source",
            "book_indexed", "tokens"} <= set(d)
    assert {"cache_write", "cache_read", "images", "retrieval", "output",
            "total_input"} <= set(d["tokens"])


@pytest.mark.skipif(not HAVE_LECTURE, reason="örnek ders PDF'i yok")
@pytest.mark.slow
def test_image_tokens_come_from_a_real_render(st):
    """Görüntü maliyeti sabit varsayımdan değil gerçek piksel boyutundan gelir —
    projenin en pahalı kalemi bu ve yanlış tahmin doğrudan paraya yazıyor."""
    from dersnotu.pdfio import parse_lecture

    lec = parse_lecture(LECTURE, shape_threshold=st.visual_shape_threshold,
                        max_section_slides=st.max_section_slides)
    p = project(lec, LECTURE, st)
    assert p.visual_slides > 0
    assert p.image_tokens_each > 100
    assert p.sample["width"] > 0
    # Kenarı yarıya indirmek maliyeti dörtte bire düşürür (alanla orantılı).
    assert p.sample["halved_image_cost"] == pytest.approx(
        p.sample["image_cost"] / 4, rel=0.15
    )
