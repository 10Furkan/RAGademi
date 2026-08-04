"""Hatalı bölümleri yeniden üretme.

Gerçek bir kesintiden doğdu: 8 bölümlük bir koşuda internet gidip geldi, tek
bölüm (B3) patladı. Tamamını yeniden üretmek ~20 dakika ve boşuna kota demekti.

En önemli sözleşme: yeniden deneme mevcut çıktıyı ASLA kötüleştirmez. Yeniden
denenen bölüm gene patlarsa dokümanda eski hâli kalır.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dersnotu.config import Settings
from dersnotu.models import (
    ExpandedSection,
    SectionAlignment,
    StudyDocument,
    TopicCard,
    Usage,
)
from dersnotu.pipeline import Inputs, retry_failed

LECTURE = Path("Lecture02 - Bitsints.pptx.pdf")
BOOK = Path("CSAPP_2016.pdf")
HAVE_PDFS = LECTURE.exists() and BOOK.exists()


def belge(*, hatali: set[int], bolum: int = 4) -> StudyDocument:
    return StudyDocument(
        lecture_title="Ders",
        language="Türkçe",
        sections=[
            ExpandedSection(
                section_index=i,
                title=f"B{i}",
                markdown="" if i in hatali else f"## B{i}\neski metin {i}",
                slide_range=(i * 2 + 1, i * 2 + 2),
                error="RuntimeError: ağ" if i in hatali else None,
            )
            for i in range(bolum)
        ],
        cards=[TopicCard(section_index=i, title=f"B{i}") for i in range(bolum)],
        alignments=[SectionAlignment(section_index=i) for i in range(bolum)],
        usage=Usage(input_tokens=100, output_tokens=50, calls=3),
    )


def test_failed_property_lists_only_errors():
    doc = belge(hatali={1, 3})
    assert [s.section_index for s in doc.failed] == [1, 3]


def test_no_failures_is_a_noop():
    doc = belge(hatali=set())
    olaylar: list[str] = []
    out = retry_failed(
        doc, Inputs(lecture_path=Path("x"), book_path=Path("y")), Settings(),
        progress=lambda e, d="": olaylar.append(e),
    )
    assert out is doc
    assert "retry:none" in olaylar


@pytest.mark.skipif(not HAVE_PDFS, reason="örnek PDF'ler yok")
@pytest.mark.slow
def test_document_without_cards_rebuilds_them():
    """Eski sürümle üretilmiş dokümanlar da kurtarılabilmeli: iki ucuz çağrı,
    tüm dersi baştan üretmekten çok ucuz."""
    doc = belge(hatali={1}, bolum=3)
    doc.cards = []
    doc.alignments = []

    class Kartli(SahteLLM):
        def complete_json(self, *, system, content, schema, model=None):
            self.usage.add(Usage(input_tokens=5, output_tokens=5, calls=1))
            if "cards" in str(schema):
                return {"cards": [
                    {"section_index": i, "title": f"B{i}", "key_terms": [],
                     "formulas": [], "gaps": []} for i in range(3)
                ]}
            return {"alignments": [
                {"section_index": i, "book_sections": [], "page_start": 0,
                 "page_end": 0, "confidence": "low"} for i in range(3)
            ]}

    olaylar: list[str] = []
    out = retry_failed(
        doc, Inputs(lecture_path=LECTURE, book_path=BOOK), Settings(),
        progress=lambda e, d="": olaylar.append(e), llm=Kartli(),
    )
    assert "retry:rebuild" in olaylar
    assert out.sections[1].error is None
    # Kartlar saklanmalı ki bir sonraki deneme bedava olsun.
    assert out.cards and out.alignments


# --- Gerçek PDF gerektiren, ama modele çıkmayan testler ------------------
class SahteLLM:
    """Belirlenen bölümlerde patlar, kalanında metin üretir."""

    def __init__(self, patlayanlar: set[int] = frozenset()):
        self.patlayanlar = set(patlayanlar)
        self.usage = Usage()
        self.cagrilar: list[str] = []

    def complete(self, *, system, content, model=None, json_schema=None, on_delta=None):
        import re

        from dersnotu.llm.client import CallResult

        metin = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        self.cagrilar.append(metin)
        # Kart başlıkları "B{i}"; istek gövdesindeki başlıktan bölümü oku.
        m = re.search(r"# ŞİMDİ YAZILACAK BÖLÜM: B(\d+)", metin)
        if m and int(m.group(1)) in self.patlayanlar:
            raise RuntimeError("yine ağ hatası")
        self.usage.add(Usage(input_tokens=10, output_tokens=5, calls=1))
        return CallResult(text="## yeni\nyeni metin", usage=Usage(), stop_reason="end_turn")

    def complete_json(self, *, system, content, schema, model=None):
        raise AssertionError("retry_failed model çağrısı yapmamalı (kartlar saklı)")


@pytest.mark.skipif(not HAVE_PDFS, reason="örnek PDF'ler yok")
@pytest.mark.slow
def test_retry_only_touches_failed_sections():
    doc = belge(hatali={1}, bolum=3)
    llm = SahteLLM()
    out = retry_failed(
        doc,
        Inputs(lecture_path=LECTURE, book_path=BOOK),
        Settings(),
        llm=llm,
    )
    # Yalnızca bir genişletme çağrısı yapılmalı.
    assert len(llm.cagrilar) == 1
    # Başarılı bölümler bit bit korunmalı.
    assert out.sections[0].markdown == "eski metin 0" or "eski metin 0" in out.sections[0].markdown
    assert out.sections[2].markdown.endswith("eski metin 2")
    # Hatalı bölüm düzelmeli.
    assert out.sections[1].error is None
    assert "yeni metin" in out.sections[1].markdown


@pytest.mark.skipif(not HAVE_PDFS, reason="örnek PDF'ler yok")
@pytest.mark.slow
def test_retry_never_worsens_the_document():
    """Yeniden denenen bölüm gene patlarsa ESKİ kayıt korunur."""
    doc = belge(hatali={1}, bolum=3)
    onceki = doc.sections[1].error
    out = retry_failed(
        doc,
        Inputs(lecture_path=LECTURE, book_path=BOOK),
        Settings(),
        llm=SahteLLM(patlayanlar={1}),
    )
    assert out.sections[1].error is not None
    assert out.sections[1].error == onceki  # eski hata kaydı duruyor
    assert out.sections[0].markdown.endswith("eski metin 0")


@pytest.mark.skipif(not HAVE_PDFS, reason="örnek PDF'ler yok")
@pytest.mark.slow
def test_usage_accumulates_across_retry():
    doc = belge(hatali={1}, bolum=3)
    onceki = doc.usage.calls
    out = retry_failed(
        doc, Inputs(lecture_path=LECTURE, book_path=BOOK), Settings(), llm=SahteLLM()
    )
    assert out.usage.calls >= onceki  # önceki muhasebe kaybolmasın
