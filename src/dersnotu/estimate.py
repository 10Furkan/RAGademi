"""Token / maliyet / süre projeksiyonu — hiç model çağrısı yapmadan.

Bu hesap iki yerden okunuyor: `dersnotu estimate` komutu ve ders sayfasındaki
"üret'e basmadan önce" şeridi. Tek kaynak olması şart, çünkü ikisi ayrı
yazılsaydı biri güncellenip diğeri unutulur ve kullanıcı arayüzde bir rakam,
terminalde başka bir rakam görürdü.

**Görüntü tokenları sabit varsayımla değil, gerçekten render edilmiş piksel
boyutundan hesaplanır** (token ≈ genişlik×yükseklik/750) ve toplam girdinin
yarısından fazlasını tutar. Maliyeti düşürmenin en etkili kolu bu yüzden
`slide_image_max_edge`: kenarı yarıya indirmek görüntü maliyetini DÖRTTE BİRE
düşürür (alanla orantılı). `Projection.halved_image_cost` bu takası gösterir.

**Süre tahmini ölçümle kalibre olur.** Sabit bir "bölüm başına X saniye"
yazmak yanıltıcı olurdu: API birkaç saniye, Claude Pro CLI'ı ölçülen 189
saniye, ve ikisi de makineye/ağa göre kayar. `seconds_per_section` geçmiş
koşuların medyanını alır (kitaplıkta saklanıyor), geçmiş yoksa aşağıdaki kaba
tahminlere düşer.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .index import BookIndex, estimate_tokens
from .index.chunker import CHARS_PER_TOKEN
from .llm.client import PRICING
from .llm.prompts import EXPAND_SYSTEM, build_lecture_context
from .models import Lecture
from .pdfio.render import render_pages

# Geçmiş koşu yoksa kullanılan kaba tahminler (saniye/bölüm). `cli` ölçüldü
# (tek gerçek bölüm: 189 sn); `api` akış hızından kabaca; `demo` anlıktır.
# Bunlar yalnızca ilk koşu için geçerli — sonrası kendi geçmişinden öğrenir.
FALLBACK_SECONDS = {"api": 30.0, "cli": 190.0, "demo": 1.0}

# Bölüm başına beklenen çıktı (~2000 kelime).
_OUTPUT_PER_SECTION = 3000
# Ucuz geçişlerin (konu kartları + hizalama) sabit payı.
_CHEAP_INPUT_PAD = 8000
_CHEAP_OUTPUT = 4000
# Sınav kâğıdı önekte taşınır; uzunluğu bu sınırla kesiliyor (pipeline ile aynı).
EXAM_CHAR_LIMIT = 20_000


@dataclass
class Projection:
    """Bir üretimin token/maliyet/süre projeksiyonu."""

    model: str
    sections: int
    visual_slides: int
    image_tokens_each: float
    cache_write: float
    cache_read: float
    image_tokens: float
    retrieval_tokens: float
    section_text_tokens: float
    output_tokens: float
    cheap_input: float
    cheap_output: float
    cost: float
    no_cache_cost: float
    seconds: float
    seconds_source: str  # "geçmiş" | "tahmin"
    book_indexed: bool
    index_seconds: float = 0.0
    sample: dict = field(default_factory=dict)

    @property
    def total_input(self) -> float:
        return (
            self.cache_write + self.cache_read + self.image_tokens
            + self.retrieval_tokens + self.section_text_tokens
        )

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "sections": self.sections,
            "visual_slides": self.visual_slides,
            "tokens": {
                "cache_write": round(self.cache_write),
                "cache_read": round(self.cache_read),
                "images": round(self.image_tokens),
                "retrieval": round(self.retrieval_tokens),
                "section_text": round(self.section_text_tokens),
                "output": round(self.output_tokens),
                "cheap_input": round(self.cheap_input),
                "cheap_output": round(self.cheap_output),
                "total_input": round(self.total_input),
            },
            "cost": round(self.cost, 3),
            "no_cache_cost": round(self.no_cache_cost, 3),
            "seconds": round(self.seconds),
            "seconds_source": self.seconds_source,
            "book_indexed": self.book_indexed,
            "index_seconds": round(self.index_seconds),
            "sample": self.sample,
        }


def seconds_per_section(backend: str, history: list[float] | None = None) -> tuple[float, str]:
    """Bölüm başına saniye: varsa geçmişin medyanı, yoksa kaba tahmin.

    Medyan tercih edildi çünkü tek bir kötü koşu (ağ kesintisi, kota beklemesi)
    ortalamayı savurur ama medyanı kıpırdatmaz.
    """
    if history:
        return statistics.median(history), "geçmiş"
    return FALLBACK_SECONDS.get(backend, FALLBACK_SECONDS["api"]), "tahmin"


def project(
    lecture: Lecture,
    lecture_path: Path,
    settings: Settings,
    *,
    book_sha: str | None = None,
    model: str | None = None,
    backend: str = "api",
    limit_sections: int | None = None,
    exam_chars: int = 0,
    history: list[float] | None = None,
) -> Projection:
    """Ayrıştırılmış bir dersten projeksiyon üretir.

    Kitap indeksi varsa alıntı boyutu oradan okunur; yoksa makul bir varsayıma
    düşer ve `book_indexed=False` ile bunu söyler — ilk koşuda indeksleme
    süresi de eklenir, çünkü kullanıcının bekleyeceği süre odur.
    """
    model = model or settings.model
    n_sections = len(lecture.sections)
    if limit_sections:
        n_sections = min(n_sections, limit_sections)
    n_sections = max(1, n_sections)

    prefix_text = build_lecture_context(lecture)
    prefix_tokens = (
        estimate_tokens(prefix_text)
        + estimate_tokens(EXPAND_SYSTEM)
        + min(exam_chars, EXAM_CHAR_LIMIT) // CHARS_PER_TOKEN
    )

    # Görüntü tokenları: gerçek render'dan örnekle. Üç slayt yeterli — hepsi
    # aynı şablondan çıktığı için boyutları birkaç piksel içinde aynı.
    visual = [
        slide.number
        for sec in lecture.sections[:n_sections]
        for slide in sec.slides
        if slide.is_visual
    ]
    sample_nos = visual[:3]
    px = (
        render_pages(lecture_path, sample_nos, max_edge=settings.slide_image_max_edge)
        if sample_nos
        else {}
    )
    img_each = sum(p.token_estimate for p in px.values()) / len(px) if px else 0.0
    image_tokens = len(visual) * img_each

    # Alıntı boyutu: indeks varsa gerçek ortalama, yoksa hedef chunk boyutu.
    book_indexed = False
    avg_chunk = float(settings.chunk_target_tokens)
    index_seconds = 0.0
    if book_sha and BookIndex.is_built(settings.cache_dir, book_sha):
        idx = BookIndex(BookIndex.path_for(settings.cache_dir, book_sha))
        try:
            avg_chunk = float(
                idx.conn.execute("SELECT AVG(token_estimate) FROM chunks").fetchone()[0]
                or settings.chunk_target_tokens
            )
            book_indexed = True
        finally:
            idx.close()
    else:
        # İlk koşuda kitap indekslenecek: ~1100 sayfa için ölçülen ~25 sn
        # chunk'lama + ~95 sn şekil taraması. Kullanıcı bunu bekleyecek.
        index_seconds = 25.0 + (95.0 if settings.extract_book_figures else 0.0)

    per_section_text = sum(
        estimate_tokens(s.raw_text) for s in lecture.sections[:n_sections]
    ) / n_sections

    cache_write = float(prefix_tokens)
    cache_read = prefix_tokens * max(0, n_sections - 1)
    retrieval_tokens = settings.chunks_per_section * avg_chunk * n_sections
    section_text_tokens = per_section_text * n_sections
    output_tokens = float(n_sections * _OUTPUT_PER_SECTION)
    cheap_input = float(estimate_tokens(prefix_text) + _CHEAP_INPUT_PAD)
    cheap_output = float(_CHEAP_OUTPUT)

    inp_price, out_price = PRICING.get(model, (3.0, 15.0))
    cheap_in, cheap_out = PRICING.get(settings.cheap_model, (1.0, 5.0))
    plain_input = image_tokens + retrieval_tokens + section_text_tokens

    cost = (
        cache_write * inp_price * 1.25
        + cache_read * inp_price * 0.10
        + plain_input * inp_price
        + output_tokens * out_price
        + cheap_input * cheap_in
        + cheap_output * cheap_out
    ) / 1_000_000
    no_cache_cost = (
        (prefix_tokens * n_sections + plain_input) * inp_price
        + output_tokens * out_price
    ) / 1_000_000

    # Abonelik yolunda token ücreti yok: kota harcanır, para değil.
    if backend == "cli":
        cost = 0.0
    elif backend == "demo":
        cost = no_cache_cost = 0.0

    per_sec, source = seconds_per_section(backend, history)

    sample = {}
    if px:
        s = next(iter(px.values()))
        halved = len(visual) * (s.width // 2) * (s.height // 2) / 750
        sample = {
            "width": s.width,
            "height": s.height,
            "kb": round(sum(len(p.png) for p in px.values()) / len(px) / 1024),
            "tokens_each": round(img_each),
            "halved_image_cost": round(halved * inp_price / 1_000_000, 3),
            "image_cost": round(image_tokens * inp_price / 1_000_000, 3),
        }

    return Projection(
        model=model,
        sections=n_sections,
        visual_slides=len(visual),
        image_tokens_each=img_each,
        cache_write=cache_write,
        cache_read=cache_read,
        image_tokens=image_tokens,
        retrieval_tokens=retrieval_tokens,
        section_text_tokens=section_text_tokens,
        output_tokens=output_tokens,
        cheap_input=cheap_input,
        cheap_output=cheap_output,
        cost=cost,
        no_cache_cost=no_cache_cost,
        seconds=per_sec * n_sections + index_seconds,
        seconds_source=source,
        book_indexed=book_indexed,
        index_seconds=index_seconds,
        sample=sample,
    )
