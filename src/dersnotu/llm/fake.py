"""API anahtarı olmadan boru hattını uçtan uca koşturan sahte istemci.

Amaç sahte içerik üretmek değil, ORKESTRASYONU doğrulamak: bölümleme,
retrieval, atıf zinciri, streaming, hata yolu ve render gerçekten çalışıyor mu?
Çıktı metni gerçek slayt başlıkları ve gerçek kitap alıntılarından kurulur, bu
yüzden retrieval bozuksa demo çıktısında da görünür.

`LLMClient` ile aynı yüzeyi sunar; `pipeline` hangisini aldığını bilmez.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from ..models import Usage
from .client import CallResult

_SECTION_RE = re.compile(r"# ŞİMDİ YAZILACAK BÖLÜM: (.*)")
_SLIDES_RE = re.compile(r"Slaytlar (\d+)-(\d+)")
_CITE_RE = re.compile(r"### \[K: ([^\]]+)\]\n(.{0,400})", re.S)
_SLIDE_TITLE_RE = re.compile(r"--- Slayt (\d+): ([^\n-]*) ---")
_BOOK_FIG_RE = re.compile(r"^- `([\d.]+)` — (.*?) \(s\. (\d+)\)$", re.M)


class FakeLLMClient:
    """Ağa çıkmaz, para harcamaz, deterministiktir."""

    def __init__(self, model: str = "fake", *, cheap_model: str = "fake",
                 effort: str = "high", max_tokens: int = 16000, delay: float = 0.012):
        self.model = model
        self.cheap_model = cheap_model
        self.effort = effort
        self.max_tokens = max_tokens
        self.usage = Usage()
        self.delay = delay  # streaming'i gözle görülür kılmak için

    # ------------------------------------------------------------------
    def complete(
        self,
        *,
        system: str | list[dict],
        content: list[dict],
        model: str | None = None,
        json_schema: dict | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> CallResult:
        text_blocks = [b.get("text", "") for b in content if b.get("type") == "text"]
        joined = "\n".join(text_blocks)
        images = sum(1 for b in content if b.get("type") == "image")

        body = self._compose(joined, images)

        if on_delta:
            for chunk in _chunks(body, 90):
                on_delta(chunk)
                if self.delay:
                    time.sleep(self.delay)

        usage = Usage(
            input_tokens=len(joined) // 4 + images * 1960,
            output_tokens=len(body) // 4,
            calls=1,
        )
        self.usage.add(usage)
        return CallResult(text=body, usage=usage, stop_reason="end_turn")

    # ------------------------------------------------------------------
    def complete_json(
        self, *, system: str, content: list[dict], schema: dict, model: str | None = None
    ) -> Any:
        joined = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        self.usage.add(Usage(input_tokens=len(joined) // 4, output_tokens=300, calls=1))

        if "cards" in str(schema):
            return {"cards": self._fake_cards(joined)}
        return {"alignments": self._fake_alignments(joined)}

    # ------------------------------------------------------------------
    def _fake_cards(self, payload: str) -> list[dict]:
        cards = []
        for m in re.finditer(r"## Bölüm (\d+) \(slayt (\d+)-(\d+)\)", payload):
            idx = int(m.group(1))
            # Bölümün ilk slayt başlığını konu adı yap.
            after = payload[m.end() : m.end() + 1200]
            titles = _SLIDE_TITLE_RE.findall(after)
            title = titles[0][1].strip() if titles else f"Bölüm {idx + 1}"
            terms = [t[1].strip() for t in titles[:6] if t[1].strip()]
            cards.append(
                {
                    "section_index": idx,
                    "title": title or f"Bölüm {idx + 1}",
                    "key_terms": terms,
                    "formulas": [],
                    "gaps": [],
                }
            )
        return cards

    def _fake_alignments(self, payload: str) -> list[dict]:
        out = []
        for m in re.finditer(r"Bölüm (\d+):", payload):
            out.append(
                {
                    "section_index": int(m.group(1)),
                    "book_sections": [],
                    "page_start": 0,
                    "page_end": 0,
                    "confidence": "low",  # aralık filtresi devre dışı kalsın
                }
            )
        return out

    # ------------------------------------------------------------------
    def _compose(self, payload: str, images: int) -> str:
        m = _SECTION_RE.search(payload)
        title = m.group(1).strip() if m else "Bölüm"
        sm = _SLIDES_RE.search(payload)
        first, last = (sm.group(1), sm.group(2)) if sm else ("?", "?")

        slide_titles = _SLIDE_TITLE_RE.findall(payload)
        cites = _CITE_RE.findall(payload)

        parts = [
            f"## {title}",
            "",
            "> ℹ️ **Demo çıktısı.** Bu metin gerçek modelden değil, boru hattını "
            "API anahtarı olmadan doğrulayan sahte istemciden geliyor. "
            "Aşağıdaki slayt başlıkları ve kitap atıfları gerçek — retrieval "
            "gerçekten çalıştığı için buradalar.",
            "",
            f"Bu bölüm {first}–{last} arası slaytları kapsıyor"
            + (f" ve {images} slayt görüntüsü modele gönderildi." if images else "."),
            "",
        ]

        if slide_titles:
            parts += ["### Slaytta işlenen konular", ""]
            for num, st in slide_titles:
                st = st.strip()
                if st:
                    parts.append(f"- **{st}** [S: {num}]")
            parts.append("")

        # Kitap şekli önerildiyse ilkini çağır: hem tespit hem kırpma hem de
        # render yolu demo koşusunda uçtan uca sınanmış olur.
        book_figs = _BOOK_FIG_RE.findall(payload)
        if book_figs:
            num, caption, page = book_figs[0]
            parts += [
                "### Kitaptaki diyagram",
                "",
                f"Aşağıdaki şekil bu konuyu özetliyor: {caption}. [K: s. {page}]",
                "",
                f"[KŞEKİL: {num}]",
                "",
            ]

        if cites:
            parts += ["### Kitaptan gelen destek", ""]
            for citation, excerpt in cites[:4]:
                snippet = " ".join(excerpt.split())[:260]
                parts += [f"{snippet}… [K: {citation}]", ""]
        else:
            parts += [
                "> ⚠️ Bu bölüm için kitapta eşleşen parça bulunamadı.",
                "",
            ]

        # Render hattının matematik ve kod yollarını da tetikle.
        parts += [
            "### Biçim denetimi",
            "",
            "Satır içi matematik $x_{w-1}$ ve blok matematik:",
            "",
            "$$B2U_w(\\vec{x}) = \\sum_{i=0}^{w-1} x_i \\cdot 2^i$$",
            "",
            "```c",
            "unsigned char a = 0x69;   /* 01101001 */",
            "```",
            "",
        ]
        # İstenen açıklama bloklarını üret: seçim gerçekten prompta geçtiyse
        # burada görünür, ve render tarafındaki container yolu da sınanmış olur.
        parts += _requested_callouts(payload)
        return "\n".join(parts)


def _requested_callouts(payload: str) -> list[str]:
    """Promptta istenen `::: ...` bloklarını taklit eder.

    Tetikleyici, direktif metninde geçen blok açılışının kendisi (`::: soru`);
    böylece prompt tarafı ile render tarafı ayrı ayrı değil, BİRLİKTE sınanır.
    """
    out: list[str] = []
    if "::: analoji" in payload:
        out += [
            "::: analoji",
            "Bir baytı 8 anahtarlı bir sigorta kutusu gibi düşün: her anahtar "
            "açık ya da kapalı, arası yok.",
            "",
            "**Nerede bozulur:** Sigortaların sırası önemsizdir, bitlerin sırası "
            "değildir — soldaki bit 128, sağdaki 1 eder.",
            ":::",
            "",
        ]
    if "::: soru" in payload:
        out += [
            "::: soru",
            "1. `0xCA` baytını ikiliye çevir.",
            "2. 8 bitlik işaretli gösterimde `0xFF` hangi sayıdır?",
            "",
            "**Yanıtlar:** 1) `11001010` 2) $-1$",
            ":::",
            "",
        ]
    if "::: sözlük" in payload:
        out += [
            "::: sözlük",
            "| Terim | İngilizce | Anlamı |",
            "|---|---|---|",
            "| Bayt | byte | 8 bitlik grup |",
            "| İkiye tümleyen | two's complement | İşaretli tam sayı gösterimi |",
            ":::",
            "",
        ]
    return out


def _chunks(text: str, size: int):
    for i in range(0, len(text), size):
        yield text[i : i + size]
