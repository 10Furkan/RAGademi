"""Ders slaytı PDF'ini okur, başlıkları çıkarır ve bölümlere ayırır.

Tasarım notları (gerçek CSAPP deck'i üzerinde doğrulandı):

* Başlık ilk satır DEĞİL. Bazı slaytlarda kod bloğu veya şema metni başlıktan
  önce gelir. Bunun yerine en büyük font boyutuna sahip satır başlık kabul edilir.
* Şekil tespiti `page.images` ile yapılamaz. PowerPoint şemaları vektör çizimdir;
  diyagram dolu slaytlarda bile gömülü raster sayısı 0 çıkar. Bunun yerine
  vektör nesne (rect/line/curve) sayısı kullanılır.
* Bölüm sınırları tekrar eden ajanda slaytlarından bulunur ("Today: ...").
  Ajanda bulunamazsa sabit boyutlu gruplamaya düşülür.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

import pdfplumber

from ..models import Lecture, LectureSection, Slide

# Slayt şablonundan gelen, içerik taşımayan satırlar.
_BOILERPLATE = re.compile(
    r"^(carnegie mellon|bryant and o.?hallaron|computer systems:.*|\d{1,3})$",
    re.IGNORECASE,
)
_AGENDA_TITLE = re.compile(r"^\s*(today|outline|agenda|roadmap|plan|bugün|i̇çerik)\b[:\s]", re.IGNORECASE)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _clean_lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _BOILERPLATE.match(line):
            continue
        out.append(line)
    return out


def _extract_title(page: pdfplumber.page.Page) -> str:
    """Başlık = en büyük font boyutundaki satır (boilerplate hariç).

    Eşit boyutta birden çok satır varsa sayfada en üstte olan seçilir.
    """
    try:
        words = page.extract_words(extra_attrs=["size"], use_text_flow=False)
    except Exception:
        return ""
    if not words:
        return ""

    # Kelimeleri satırlara grupla (dikey konuma göre, 2pt tolerans).
    lines: list[dict] = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        size = round(float(w.get("size") or 0), 1)
        if lines and abs(lines[-1]["top"] - w["top"]) <= 2.5:
            lines[-1]["words"].append(w["text"])
            lines[-1]["size"] = max(lines[-1]["size"], size)
        else:
            lines.append({"top": w["top"], "size": size, "words": [w["text"]]})

    candidates = []
    for ln in lines:
        text = " ".join(ln["words"]).strip()
        if not text or _BOILERPLATE.match(text):
            continue
        candidates.append((ln["size"], ln["top"], text))
    if not candidates:
        return ""

    max_size = max(c[0] for c in candidates)
    top_candidates = sorted([c for c in candidates if c[0] >= max_size - 0.6], key=lambda c: c[1])

    # Başlık birden çok satıra sarmış olabilir ("Summary:" / "Expanding,
    # Truncating: Basic Rules"). Dikey olarak bitişik olanları birleştir.
    parts = [top_candidates[0][2]]
    prev_top = top_candidates[0][1]
    for _size, top, text in top_candidates[1:]:
        if top - prev_top > max_size * 2.0:
            break
        parts.append(text)
        prev_top = top

    title = " ".join(parts).strip()
    # Çok uzun "başlık" muhtemelen gövde metnidir; kırp.
    return title[:120].strip()


def _count_shapes(page: pdfplumber.page.Page) -> int:
    """Vektör çizim nesnesi sayısı — şema yoğunluğunun göstergesi."""
    n = 0
    for attr in ("rects", "lines", "curves"):
        try:
            n += len(getattr(page, attr) or [])
        except Exception:
            pass
    return n


def _count_images(page: pdfplumber.page.Page) -> int:
    try:
        return len(page.images or [])
    except Exception:
        return 0


def _fragment_ratio(lines: list[str]) -> float:
    """Kısa metin parçacıklarının oranı.

    Şema slaytları çok sayıda 1-5 karakterlik etiket üretir ("0000", "Addr", "=").
    Düz madde-işaretli slaytlarda bu oran düşüktür.
    """
    if not lines:
        return 0.0
    short = sum(1 for ln in lines if len(ln) <= 6)
    return short / len(lines)


def parse_lecture(
    path: str | Path, *, shape_threshold: int = 20, max_section_slides: int = 8
) -> Lecture:
    path = Path(path)
    slides: list[Slide] = []

    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            raw = page.extract_text() or ""
            lines = _clean_lines(raw)
            title = _extract_title(page)
            # Başlığı gövdeden çıkar ki iki kez geçmesin.
            body = [ln for ln in lines if ln != title]

            shapes = _count_shapes(page)
            images = _count_images(page)
            frag = _fragment_ratio(body)

            # Şablon zemini bu deck'te 6 nesne; gerçek şemalar 40+ üretiyor.
            # Parçacık kuralı yalnızca şekil sinyali de destekliyorsa devreye
            # girer — tek başına düz metin özet slaytlarını yanlış işaretliyordu.
            is_visual = (
                images > 0
                or shapes >= shape_threshold
                or (frag >= 0.55 and len(body) >= 15 and shapes >= 10)
            )

            slides.append(
                Slide(
                    number=i,
                    title=title,
                    text="\n".join(body),
                    is_visual=is_visual,
                    shape_count=shapes,
                    image_count=images,
                )
            )

    _mark_dividers(slides)
    sections = _build_sections(slides)
    sections = _split_large(sections, max_section_slides)
    lecture_title = slides[0].title if slides and slides[0].title else path.stem

    return Lecture(
        source_path=str(path),
        source_sha256=sha256_file(path),
        title=lecture_title,
        slides=slides,
        sections=sections,
    )


def _mark_dividers(slides: list[Slide]) -> None:
    """Ajanda / bölüm ayırıcı slaytlarını işaretle.

    İki sinyal:
      1. Başlık "Today:" / "Outline" gibi bir kalıpla başlıyor.
      2. Aynı gövde metni deck içinde >=2 kez tekrar ediyor (ajanda slaytı
         her bölüm başında yeniden gösterilir).
    """
    bodies = Counter(s.text.strip() for s in slides if s.text.strip())
    for s in slides:
        repeated = bodies[s.text.strip()] >= 2 and len(s.text.strip()) > 40
        if _AGENDA_TITLE.match(s.title) or repeated:
            s.is_divider = True


def _build_sections(slides: list[Slide]) -> list[LectureSection]:
    """Ayırıcı slaytlar arasındaki kesintisiz blokları bölüm yap."""
    sections: list[LectureSection] = []
    current: list[Slide] = []
    agenda = ""

    for s in slides:
        if s.is_divider:
            if current:
                sections.append(
                    LectureSection(index=len(sections), slides=current, agenda_context=agenda)
                )
                current = []
            agenda = s.text
            continue
        current.append(s)

    if current:
        sections.append(LectureSection(index=len(sections), slides=current, agenda_context=agenda))

    # Ajanda bulunamadıysa (tek dev bölüm) sabit boyutlu gruplamaya düş.
    if len(sections) <= 1 and len(slides) > 10:
        sections = _fallback_sections(slides)

    # Başlık slaytı gibi 1 slaytlık artıkları bir sonrakiyle birleştir.
    return _merge_tiny(sections)


def _fallback_sections(slides: list[Slide], size: int = 6) -> list[LectureSection]:
    content = [s for s in slides if not s.is_divider]
    out: list[LectureSection] = []
    for i in range(0, len(content), size):
        block = content[i : i + size]
        if block:
            out.append(LectureSection(index=len(out), slides=block))
    return out


def _split_large(sections: list[LectureSection], max_slides: int) -> list[LectureSection]:
    """Çok büyük bölümleri böler — tek genişletme çağrısı dengesiz kalmasın.

    Bölme noktası mümkünse "Summary"/"Özet" gibi doğal duraklarda değil,
    eşit parçalarda seçilir; her parça ajanda bağlamını miras alır.
    """
    if max_slides <= 0:
        return sections
    out: list[LectureSection] = []
    for sec in sections:
        n = len(sec.slides)
        if n <= max_slides:
            out.append(sec)
            continue
        parts = -(-n // max_slides)  # tavan bölme
        size = -(-n // parts)  # parçaları dengele
        for i in range(0, n, size):
            block = sec.slides[i : i + size]
            if block:
                out.append(
                    LectureSection(
                        index=len(out), slides=block, agenda_context=sec.agenda_context
                    )
                )
    for i, sec in enumerate(out):
        sec.index = i
    return out


def _merge_tiny(sections: list[LectureSection], min_slides: int = 2) -> list[LectureSection]:
    if len(sections) <= 1:
        return sections
    merged: list[LectureSection] = []
    for sec in sections:
        if merged and len(sec.slides) < min_slides:
            merged[-1].slides.extend(sec.slides)
        elif merged and len(merged[-1].slides) < min_slides:
            sec.slides = merged[-1].slides + sec.slides
            sec.agenda_context = merged[-1].agenda_context or sec.agenda_context
            merged[-1] = sec
        else:
            merged.append(sec)
    for i, sec in enumerate(merged):
        sec.index = i
    return merged
