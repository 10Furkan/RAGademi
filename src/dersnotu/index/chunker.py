"""Kitap metnini bölüm-farkında parçalara ayırır.

Kurallar:
  * Bir chunk asla bölüm sınırını aşmaz — retrieval'da alakasız konu karışmasın.
  * Her chunk kaynağını taşır (bölüm başlığı + sayfa aralığı) ki çıktıda atıf
    verilebilsin. Atıf zinciri halüsinasyona karşı ana savunma.
  * Token tahmini karakter/3.5 ile yapılır; tokenizer'a bağımlılık istemiyoruz.
"""

from __future__ import annotations

import re

from ..models import Book, BookChunk

CHARS_PER_TOKEN = 3.5
_WS = re.compile(r"[ \t]+")
_MULTI_NL = re.compile(r"\n{3,}")

# Ön/arka matter: içerik taşımaz ama terim yoğunluğu yüksek olduğu için
# BM25 sonuçlarını kirletir (kitabın arka dizini her terimle eşleşir).
_NON_CONTENT = re.compile(
    r"^\s*(contents|index|bibliography|preface|about the authors?|"
    r"copyright|title page|cover|dedication|acknowledg\w*|"
    r"list of (figures|tables)|references)\b",
    re.IGNORECASE,
)

# --- PDF çıkarma artefaktları ---------------------------------------------
# Şekiller sayfa metnine düz yazı gibi karışır. Bunlar chunk ATILARAK
# temizlenemez: aynı chunk'ta gerçek düzyazı da var, üstelik "düzyazı oranı"
# gibi bir ölçüt CSAPP'nin kod listelerini de eler — oysa kod listeleri bu
# kitabın en değerli içeriği. O yüzden yalnızca artefaktın kendisi kesilir.

# ASCII bayt dökümü şekli: "35 105 110 99 108 117 100 101 32 60 115 116 ...".
# Eşik 12: gerçek tablolar (struct alan offsetleri, cache boyutları) en fazla
# 8-9 ardışık sayıya çıkıyor; 12 onları korur, bayt dökümünü keser.
_BYTE_RUN = re.compile(r"(?:\b\d{1,3}\b[ ]+){11,}\b\d{1,3}\b")

# Şekilde boşluğu gösteren SP işaretleri. Serbest duranlar ("SP SP SP")
# doğrudan kesilir; tek bir "SP" meşrudur (stack pointer).
_SP_RUN = re.compile(r"(?:\bSP\b[ ]*){2,}")
# Kelimeye yapışık olanlar (`#includeSP <stdio`, `intSP main`) \b sınırı
# oluşturmadığı için ayrı yakalanır. Tek başına "SP" içeren meşru bir sözcük
# olabileceğinden yalnızca sayfada 3+ kez geçiyorsa uygulanır.
_SP_GLUE = re.compile(r"(?<=[\w,])SP(?=[\s<(\"'])")

# Bölüm başı içindekiler bloğu: "2.1 Information Storage 70 2.2 Integer
# Representations 95 ...". Bölüm başlığı gerçek olduğu için _NON_CONTENT
# bunları yakalayamıyor; ardışık 4 giriş şartı yanlış pozitifi engelliyor.
# Başlık üst sınırı cömert: 58'de "Programs Are Translated by Other Programs
# into Different Forms" (60 karakter) elenip zinciri kırıyordu.
_INLINE_TOC = re.compile(r"(?:\d{1,2}\.\d{1,2}[ ]\S[^\n]{2,90}?[ ]\d{2,4}\s+){4,}")


def is_content_section(title: str) -> bool:
    return not _NON_CONTENT.match(title or "")


def scrub_artifacts(text: str) -> str:
    """Şekil/navigasyon artefaktlarını metinden keser, düzyazıyı bırakır."""
    text = _INLINE_TOC.sub(" ", text)
    text = _BYTE_RUN.sub(" ", text)
    text = _SP_RUN.sub(" ", text)
    if len(_SP_GLUE.findall(text)) >= 3:
        text = _SP_GLUE.sub(" ", text)
    return _WS.sub(" ", text).strip()


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def normalize(text: str) -> str:
    text = text.replace("\r", "")
    text = _WS.sub(" ", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


def _page_section_map(book: Book) -> dict[int, str]:
    """Sayfa numarası -> en spesifik (en derin) bölüm başlığı."""
    mapping: dict[int, str] = {}
    for sec in sorted(book.sections, key=lambda s: s.level):
        for page in range(sec.page_start, sec.page_end + 1):
            mapping[page] = sec.title
    return mapping


def chunk_book(
    book: Book,
    pages: list[str],
    *,
    target_tokens: int = 900,
    overlap_ratio: float = 0.15,
) -> list[BookChunk]:
    """Sayfaları bölüm bazında gruplayıp hedef boyutta chunk'lara böler."""
    page_section = _page_section_map(book)
    target_chars = int(target_tokens * CHARS_PER_TOKEN)
    overlap_chars = int(target_chars * overlap_ratio)

    chunks: list[BookChunk] = []
    buf: list[str] = []
    buf_len = 0
    buf_start = 1
    current_section = page_section.get(1, "")

    def flush(page_end: int) -> None:
        nonlocal buf, buf_len, buf_start
        text = normalize("\n".join(buf))
        if len(text) < 200:  # anlamsız kırıntıları at
            buf, buf_len = [], 0
            return
        cid = f"c{len(chunks):05d}"
        chunks.append(
            BookChunk(
                chunk_id=cid,
                text=text,
                section_title=current_section,
                page_start=buf_start,
                page_end=page_end,
                token_estimate=estimate_tokens(text),
            )
        )
        # Sonraki chunk'a örtüşme payı taşı (bağlam kopmasın).
        tail = text[-overlap_chars:] if overlap_chars else ""
        buf = [tail] if tail else []
        buf_len = len(tail)
        buf_start = page_end

    for page_no, raw in enumerate(pages, start=1):
        text = scrub_artifacts(normalize(raw))
        if not text:
            continue
        section = page_section.get(page_no, current_section)

        # İçindekiler / dizin / kaynakça sayfalarını indeksleme.
        if not is_content_section(section):
            if buf_len > 0:
                flush(page_no - 1)
                buf, buf_len = [], 0
            current_section = section
            continue

        # Bölüm değiştiyse biriken metni kapat.
        if section != current_section and buf_len > 0:
            flush(page_no - 1)
            buf, buf_len = [], 0  # bölüm sınırında örtüşme taşıma
            buf_start = page_no
        current_section = section

        if buf_len == 0:
            buf_start = page_no

        buf.append(text)
        buf_len += len(text)

        if buf_len >= target_chars:
            flush(page_no)

    if buf_len > 0:
        flush(len(pages))

    return chunks
