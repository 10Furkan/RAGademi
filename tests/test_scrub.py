"""PDF çıkarma artefaktlarının temizlenmesi.

Bu testlerin hepsi CSAPP'den ALINAN gerçek metinler üzerine kurulu; regex'i
kağıt üstünde değil kitabın gerçekten ürettiği çıktıya karşı kilitliyorlar.

En kritik olan: kod listeleri ASLA elenmemeli. İlk yaklaşım "düzyazı oranı"
filtresiydi ve kod listelerini de eliyordu — oysa CSAPP'de en değerli içerik
onlar. O yüzden chunk atılmıyor, yalnızca artefakt kesiliyor.
"""

from __future__ import annotations

from dersnotu.index.chunker import scrub_artifacts

# --- Kesilmesi gerekenler ---------------------------------------------------

def test_ascii_byte_dump_is_cut():
    """hello.c'nin ASCII gösterimi şekli, sayfa metnine düzyazı gibi karışıyor."""
    raw = (
        "Section 1.1 Information Is Bits + Context 39 #include <stdio. "
        "35 105 110 99 108 117 100 101 32 60 115 116 100 105 111 46 "
        "h>\\ n \\ n int main()\\ n { Our hello program begins life as a source program."
    )
    out = scrub_artifacts(raw)
    assert "35 105 110 99" not in out
    assert "115 116 100 105 111" not in out
    # Düzyazı ve kod iskeleti korunmalı.
    assert "Our hello program begins life" in out
    assert "#include <stdio." in out


def test_glued_sp_markers_are_cut_when_repeated():
    raw = '#includeSP <stdio. intSP main() printfSP ("helloSP world") returnSP 0;'
    out = scrub_artifacts(raw)
    assert "SP" not in out
    assert "#include <stdio." in out
    assert "int main()" in out


def test_freestanding_sp_run_is_cut():
    out = scrub_artifacts("printf(\"hel SP SP SP SP lo, world\");")
    assert "SP" not in out


def test_inline_chapter_toc_is_cut():
    """Bölüm başı içindekiler: başlık gerçek olduğu için _NON_CONTENT yakalayamaz."""
    raw = (
        "2 Representing and Manipulating Information "
        "2.1 Information Storage 70 "
        "2.2 Integer Representations 95 "
        "2.3 Integer Arithmetic 120 "
        "2.4 Floating Point 144 "
        "2.5 Summary 162 "
        "Modern computers store and process information represented as two-valued signals."
    )
    out = scrub_artifacts(raw)
    assert "2.1 Information Storage 70" not in out
    assert "2.4 Floating Point 144" not in out
    assert "Modern computers store and process information" in out


def test_long_toc_title_does_not_break_the_chain():
    """58 karakterlik üst sınır "Programs Are Translated by Other Programs into
    Different Forms" (60 karakter) girişini eleyip zinciri kırıyor, ilk iki
    girdi metinde kalıyordu."""
    raw = (
        "1.1 Information Is Bits + Context 39 "
        "1.2 Programs Are Translated by Other Programs into Different Forms 40 "
        "1.3 It Pays to Understand How Compilation Systems Work 42 "
        "1.4 Processors Read and Interpret Instructions Stored in Memory 43 "
        "1.5 Caches Matter 47 "
        "A computer system consists of hardware and systems software."
    )
    out = scrub_artifacts(raw)
    assert "1.1 Information Is Bits + Context 39" not in out
    assert "1.2 Programs Are Translated" not in out
    assert "A computer system consists of hardware" in out


# --- Korunması gerekenler ---------------------------------------------------

def test_code_listing_survives_untouched():
    """Regresyon freni: kod listeleri kitabın en değerli içeriği."""
    code = (
        "1 void swap(); 2 3 int buf[2] = {1, 2}; 4 5 int main() 6 { "
        "7 swap(); 8 return 0; 9 }"
    )
    assert scrub_artifacts(code) == code


def test_numeric_table_survives():
    """struct alan offsetleri: 8-9 ardışık sayı: bayt dökümü eşiğinin altında."""
    raw = "Field a b c d e f g h Size 8 4 1 2 8 8 4 8 Offset 0 8 12 16 24 32 40 48"
    out = scrub_artifacts(raw)
    assert "0 8 12 16 24 32 40 48" in out


def test_single_sp_survives():
    """SP tek başına meşrudur — yığın göstericisi."""
    raw = "The stack pointer SP is stored in register %rsp."
    assert scrub_artifacts(raw) == raw


def test_lone_section_reference_survives():
    """Tek bir "3.4 ... 215" göndermesi içindekiler değil, çapraz atıftır."""
    raw = "As we saw in Section 3.4 Accessing Information 215 the operand forms vary."
    assert scrub_artifacts(raw) == raw


def test_prose_is_untouched():
    raw = (
        "Our hello program begins life as a source program that the programmer "
        "creates with an editor and saves in a text file called hello.c."
    )
    assert scrub_artifacts(raw) == raw
