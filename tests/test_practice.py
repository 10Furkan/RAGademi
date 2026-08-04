"""Deneme sınavı üretimi.

Geçmiş sınav kâğıdının İKİNCİ kullanımı: `test_exam.py` kâğıdın ders notunda
derinliği kaydırmasını koruyor, buradaki testler kâğıdın ŞABLON olarak
kullanılmasını koruyor.

Korunan sözleşmeler:
  - üç kaynağın işi ayrı (kâğıt biçim, slayt kapsam, kitap doğruluk)
  - soru KOPYALANMAZ; örnek alınan soru birebir alıntılanır ya da hiç iddia
    edilmez (`modeled_on` boş)
  - cevap anahtarı soru kâğıdının içinde değil, sonunda
  - tek bozuk soru tüm kâğıdı düşürmez
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dersnotu.llm.fake import FakeLLMClient
from dersnotu.llm.prompts import (
    PRACTICE_SCHEMA,
    PRACTICE_SYSTEM,
    build_practice_request,
)
from dersnotu.models import (
    BookChunk,
    Lecture,
    LectureSection,
    PracticeExam,
    PracticeQuestion,
    Slide,
)
from dersnotu.practice import _to_questions, to_markdown
from dersnotu.render.exam import practice_to_html


def make_lecture() -> Lecture:
    slides = [
        Slide(number=1, title="Bit Sıralaması", text="little-endian"),
        Slide(number=2, title="Bugün:", text="ajanda", is_divider=True),
        Slide(number=3, title="İkiye Tümleyen", text="işaretli gösterim"),
    ]
    return Lecture(
        source_path="x.pdf", source_sha256="abc", title="Bits",
        slides=slides,
        sections=[LectureSection(index=0, slides=slides)],
    )


def make_chunk() -> BookChunk:
    return BookChunk(
        chunk_id="c1", text="two's complement tanımı", section_title="Integer Representations",
        page_start=91, page_end=93, token_estimate=8,
    )


def make_exam(n: int = 2) -> PracticeExam:
    return PracticeExam(
        lecture_title="Bits", language="Türkçe", source_exam="vize.pdf",
        profile="6 soru, hesaplama ağırlıklı.", duration_minutes=90,
        questions=[
            PracticeQuestion(
                number=i, kind="çoktan seçmeli", points=10, topic="İkiye tümleyen",
                slides=[3], prompt=f"Soru metni {i}", choices=["A şıkkı", "B şıkkı"],
                answer="A", solution="Adım adım çözüm.",
                citations=["Integer Representations, s. 91-93"],
                modeled_on="What is the value of 0x9C?" if i == 1 else "",
            )
            for i in range(1, n + 1)
        ],
    )


# --- prompt: üç kaynağın işi ayrı -----------------------------------------
def test_the_three_sources_have_three_separate_jobs():
    """Kâğıt biçimi, slayt kapsamı, kitap doğruluğu verir.

    Bu ayrım bozulursa model kâğıttaki konudan soru sormaya başlar ve
    öğrenciye sorumlu OLMADIĞI şeyi çalıştırır.
    """
    assert "BİÇİMİ verir" in PRACTICE_SYSTEM
    assert "KAPSAMI verir" in PRACTICE_SYSTEM
    assert "DOĞRULUĞU verir" in PRACTICE_SYSTEM
    assert "Kapsamı BELİRLEMEZ" in PRACTICE_SYSTEM


def test_copying_the_past_question_is_forbidden():
    """Kullanıcının istediği şey 'benzer soru'; kopya zaten elinde."""
    assert "KOPYALAMA YASAK" in PRACTICE_SYSTEM
    assert "olduğu gibi sorma" in PRACTICE_SYSTEM


def test_similarity_must_be_quotable_or_left_empty():
    """Atıf disiplininin sınav kâğıdına uygulanmışı: alıntılayamıyorsan iddia yok."""
    assert "BİREBİR yaz" in PRACTICE_SYSTEM
    assert "BOŞ bırak" in PRACTICE_SYSTEM
    assert "uydurmak değildir" in PRACTICE_SYSTEM


def test_distractors_come_from_typical_mistakes():
    """Rastgele çeldirici öğrenciye hiçbir şey öğretmez."""
    assert "TİPİK BİR HATADAN" in PRACTICE_SYSTEM


def test_request_carries_scope_format_and_truth():
    lec = make_lecture()
    metin = build_practice_request(lec, "SORU 1: 0x9C nedir?", [make_chunk()], "Türkçe")
    assert "Slayt 1: Bit Sıralaması" in metin  # kapsam
    assert "SORU 1: 0x9C nedir?" in metin  # biçim
    assert "[K: Integer Representations, s. 91-93]" in metin  # doğruluk
    assert "Türkçe" in metin
    # Ajanda slaytı kapsam metnine girmez: içeriği yok, yalnızca başlık listesi.
    assert "ajanda" not in metin


def test_zero_count_means_as_many_as_the_paper_has():
    """Sabit soru sayısı dayatmak biçimi taklit etme işine ters düşer:
    4 soruluk bir finalin denemesi 20 soruyla yapılmaz."""
    lec = make_lecture()
    serbest = build_practice_request(lec, "SORU 1", [], "Türkçe", count=0)
    sabit = build_practice_request(lec, "SORU 1", [], "Türkçe", count=12)
    assert "kaç soru varsa o kadar" in serbest
    assert "Tam olarak 12 soru" in sabit


def test_schema_requires_the_grounding_fields():
    """Şema `modeled_on` ve `slides` alanlarını zorunlu tutmalı; isteğe bağlı
    olsalardı model onları hiç doldurmadan geçerli yanıt verebilirdi."""
    zorunlu = PRACTICE_SCHEMA["properties"]["questions"]["items"]["required"]
    assert "modeled_on" in zorunlu
    assert "slides" in zorunlu
    assert "citations" in zorunlu


# --- bozuk yanıta dayanıklılık --------------------------------------------
def test_one_broken_question_does_not_sink_the_paper():
    sorular = _to_questions({
        "questions": [
            {"number": 1, "prompt": "iyi soru"},
            {"number": 2, "prompt": "bozuk", "slides": "üç"},  # tip hatası
            {"number": 9, "prompt": "diğer iyi soru"},
        ]
    })
    assert [q.prompt for q in sorular] == ["iyi soru", "diğer iyi soru"]


def test_numbering_is_rewritten_so_the_paper_reads_straight():
    """Model 1, 5, 6 diye numaralarsa kâğıt bozuk görünür."""
    sorular = _to_questions({
        "questions": [{"number": 4, "prompt": "a"}, {"number": 4, "prompt": "b"}]
    })
    assert [q.number for q in sorular] == [1, 2]


def test_empty_prompts_are_dropped():
    assert _to_questions({"questions": [{"number": 1, "prompt": "   "}]}) == []


# --- markdown: anahtar sonda ----------------------------------------------
def test_answer_key_comes_after_all_questions():
    """Çözüm sorunun altında olsaydı kâğıt denemelik olmaktan çıkardı."""
    md = to_markdown(make_exam(2))
    anahtar = md.index("# Cevap anahtarı")
    assert md.index("Soru metni 2") < anahtar
    assert md.index("Adım adım çözüm.") > anahtar


def test_choices_are_lettered_by_the_system():
    md = to_markdown(make_exam(1))
    assert "A) A şıkkı" in md and "B) B şıkkı" in md


def test_unquotable_question_gets_no_similarity_claim():
    """`modeled_on` boşsa 'örnek alınan soru' bloğu HİÇ basılmamalı."""
    md = to_markdown(make_exam(2))
    assert md.count("::: sınav") == 1  # yalnızca alıntısı olan soru


# --- render ----------------------------------------------------------------
def test_html_separates_the_paper_from_the_key():
    html = practice_to_html(make_exam(2))
    assert html.index('id="soru-2"') < html.index('class="exam-key"')
    assert 'id="cevap-1"' in html


def test_html_renders_math_and_citations():
    exam = make_exam(1)
    exam.questions[0].prompt = "Değer $2^{w-1}$ nedir?"
    html = practice_to_html(exam)
    assert "math-inline" in html
    # Kitap künyesi kenar rayı chip'ine dönüşmeli, ham işaretçi kalmamalı.
    assert "cite-book" in html
    assert "[K:" not in html


def test_html_omits_the_source_block_when_nothing_can_be_quoted():
    exam = make_exam(1)
    exam.questions[0].modeled_on = ""
    assert "Örnek alınan soru" not in practice_to_html(exam)


def test_grounded_counts_only_quotable_questions():
    assert len(make_exam(2).grounded) == 1


def test_the_cover_names_the_paper_not_its_storage_hash():
    """Web akışında kâğıt içerik adresli depodan geliyor (`7667c6af….pdf`).
    Kapakta o SHA'yı basmak kullanıcıya hangi kâğıttan üretildiğini söylemez."""
    from dersnotu.practice import PracticeInputs

    girdi = PracticeInputs(
        lecture_path=Path("a.pdf"), book_path=Path("b.pdf"),
        exam_path=Path(".cache/materials/7667c6af73585114.pdf"),
        exam_name="bbm341-vize-2023.pdf",
    )
    assert girdi.exam_name == "bbm341-vize-2023.pdf"
    exam = make_exam(1)
    exam.source_exam = girdi.exam_name or girdi.exam_path.name
    assert "bbm341-vize-2023.pdf" in practice_to_html(exam)
    assert "7667c6af" not in practice_to_html(exam)


# --- demo: sahte istemci gerçek kaynaklardan kuruyor -----------------------
def test_fake_client_builds_questions_from_the_real_sources():
    """Demo çıktısı gerçek slayt başlıklarını, gerçek kitap künyelerini ve
    kâğıttan GERÇEKTEN alıntılanmış bir satırı taşımalı — retrieval ya da
    sınav metni okuma bozulursa demo koşusunda da görünsün."""
    lec = make_lecture()
    kagit = "1. What is the decimal value of the bit pattern 0x9C in 8 bits?"
    payload = build_practice_request(lec, kagit, [make_chunk()], "Türkçe", count=2)
    veri = FakeLLMClient().complete_json(
        system=PRACTICE_SYSTEM,
        content=[{"type": "text", "text": payload}],
        schema=PRACTICE_SCHEMA,
    )
    sorular = veri["questions"]
    assert len(sorular) == 2
    assert sorular[0]["topic"] == "Bit Sıralaması"
    assert sorular[0]["citations"] == ["Integer Representations, s. 91-93"]
    assert "0x9C" in sorular[0]["modeled_on"]


def test_fake_client_invents_no_quote_when_the_paper_has_none():
    """Kural her iki istemcide de aynı: alıntılanacak soru yoksa iddia da yok."""
    payload = build_practice_request(
        make_lecture(), "Bu kâğıtta soru cümlesi yok.", [], "Türkçe", count=1
    )
    veri = FakeLLMClient().complete_json(
        system=PRACTICE_SYSTEM, content=[{"type": "text", "text": payload}],
        schema=PRACTICE_SCHEMA,
    )
    assert veri["questions"][0]["modeled_on"] == ""


# --- uçtan uca (gerçek PDF'ler varsa) --------------------------------------
LECTURE_PDF = Path("Lecture02 - Bitsints.pptx.pdf")
BOOK_PDF = Path("CSAPP_2016.pdf")


@pytest.mark.slow
@pytest.mark.skipif(
    not (LECTURE_PDF.exists() and BOOK_PDF.exists()), reason="PDF'ler yok"
)
def test_scanned_exam_without_text_is_refused_loudly(tmp_path):
    """Metin katmanı olmayan bir kâğıttan 'benzer soru' üretilemez.

    Sessizce kapsamdan soru üretmek kullanıcının istediği şey değildi; hata
    açık olmalı, çıktı sahte olmamalı.
    """
    from dersnotu.config import settings
    from dersnotu.practice import PracticeError, PracticeInputs
    from dersnotu.practice import generate as uret

    bos = tmp_path / "bos.pdf"
    bos.write_bytes(b"%PDF-1.4\n%%EOF\n")  # okunamayan kâğıt
    with pytest.raises(PracticeError, match="metni okunamadı"):
        uret(
            PracticeInputs(
                lecture_path=LECTURE_PDF, book_path=BOOK_PDF, exam_path=bos,
                backend="demo",
            ),
            settings,
            llm=FakeLLMClient(),
        )
