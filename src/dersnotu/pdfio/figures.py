"""Kitap sayfalarındaki şekilleri bulur ve kırpar.

Neden gerekli: retrieval yalnızca metin döndürüyor, oysa CSAPP'nin anlatımının
önemli bir kısmı diyagramlarda. Model diyagramı yeniden çizemez.

Neden modele GÖNDERMİYORUZ: görüntü token'ları zaten maliyetin yarısından
fazlası. Slayt şekillerinde kurulan yol burada da geçerli — model bir işaretçi
bırakır (`[KŞEKİL: 6.5]`), render katmanı gerçek kırpılmış görüntüyü koyar.
Maliyet sıfır, diyagram gerçek.

Üç ölçülmüş tuzak var; üçü de gerçek hata olarak yaşandı:

1. KOORDİNAT SİSTEMİ. pdfplumber MediaBox uzayında (612×792) çalışır, ama
   pypdfium2 CropBox'ı render eder (CSAPP'de 470.9×578.9). Dönüşüm yapılmazsa
   her kırpma (70.56, 97.92) punto kayar ve bitmap dışına taşan kısım siyah
   çıkar. `_crop_offset` bunu bir kez, tespit anında uygular; `BookFigure.bbox`
   ARTIK CropBox-göreli tutulur.

2. YAPIŞIK KELİMELER. `extract_words()` varsayılan `x_tolerance=3.0` ile
   "Readingthecontentsofa" üretir; bu fontta kelime arası ~2.57 punto.
   `_X_TOL = 1.5` doğru ayırıyor.

3. ALTYAZI ŞEKLİN SOLUNDA. CSAPP altyazıyı sol kenar boşluğuna, şeklin ÜST
   hizasına koyar — altına değil. Sadece "altta ara" varsayımı hiçbir şey
   bulamıyor.

Altyazı zorunludur ve bu bir filtre görevi de görür: tablolar da yoğun çizim
nesnesi üretir (yatay cetveller), ama altyazıları yoktur. Ayrıca model bir
şekle ancak numarasını biliyorsa atıf yapabilir.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium

from ..models import BookFigure

_CAPTION = re.compile(r"^Figure\s*(\d+\.\d+)\b[.\s]*(.*)$")
# Varsayılan 3.0 kelimeleri yapıştırıyor (modül başlığındaki 2. tuzak).
_X_TOL = 1.5
# Altyazı satırlarının sol kenarda aynı sütunda kabul edilme payı (punto).
_CAPTION_COLUMN_TOL = 12.0


def _cluster(objs: list[dict], *, gap: float, min_objects: int) -> list[dict]:
    """Çizim nesnelerini dikey boşluğa göre kümeler."""
    if not objs:
        return []
    boxes = sorted((o["top"], o["bottom"], o["x0"], o["x1"]) for o in objs)
    groups: list[list[tuple]] = [[boxes[0]]]
    for b in boxes[1:]:
        if b[0] - max(x[1] for x in groups[-1]) > gap:
            groups.append([b])
        else:
            groups[-1].append(b)
    return [
        {
            "top": min(x[0] for x in g),
            "bottom": max(x[1] for x in g),
            "x0": min(x[2] for x in g),
            "x1": max(x[3] for x in g),
            "n": len(g),
        }
        for g in groups
        if len(g) >= min_objects
    ]


def _lines(page) -> list[dict]:
    """Kelimeleri satırlara toplar. Yapışmayı önlemek için dar tolerans."""
    words = page.extract_words(
        x_tolerance=_X_TOL, keep_blank_chars=False, extra_attrs=["size"]
    )
    rows: dict[int, list[dict]] = {}
    for w in words:
        rows.setdefault(round(w["top"] / 3), []).append(w)
    out = []
    for group in rows.values():
        group.sort(key=lambda w: w["x0"])
        out.append(
            {
                "text": " ".join(w["text"] for w in group),
                "words": group,
                "top": min(w["top"] for w in group),
                "bottom": max(w["bottom"] for w in group),
                "x0": min(w["x0"] for w in group),
                "x1": max(w["x1"] for w in group),
            }
        )
    return sorted(out, key=lambda r: r["top"])


def _caption_for(cluster: dict, lines: list[dict]) -> tuple[str, str, dict] | None:
    """Kümeye ait altyazıyı bulur: numara, metin, altyazının kutusu.

    İki yerleşim var ve ikisi farklı ele alınmalı:

    * YANDA (CSAPP'nin normali) — altyazı sol kenar boşluğunda, şeklin üst
      hizasında. Bu durumda altyazı satırları şeklin KENDİ etiketleriyle aynı
      y'yi paylaşır, o yüzden yalnızca şeklin soluna düşen kelimeler alınır.
    * ALTTA/ÜSTTE (klasik) — altyazı tam genişlikte. Burada aynı yatay filtreyi
      uygulamak metnin tamamını eler ve altyazı BOŞ çıkardı.
    """
    window_top = cluster["top"] - 70
    window_bottom = cluster["bottom"] + 70
    for line in lines:
        if not (window_top <= line["top"] <= window_bottom):
            continue
        # Altyazı şeklin kendi etiketleriyle aynı satıra düşebilir; yalnızca
        # satırın başındaki kelimeleri sınarız.
        head = " ".join(w["text"] for w in line["words"][:2])
        m = _CAPTION.match(head)
        if not m:
            continue
        number = m.group(1)
        first = line["words"][0]
        beside = line["top"] < cluster["bottom"] and line["bottom"] > cluster["top"]

        parts: list[str] = []
        box = {
            "x0": first["x0"],
            "x1": line["words"][-1]["x1"],
            "top": line["top"],
            "bottom": line["bottom"],
        }
        # Altyazının punto'su devam satırlarını gövde metninden ve şekil
        # etiketlerinden ayıran gerçek sinyal: CSAPP'de altyazı 9pt, gövde
        # 10pt, şekil etiketleri 6-8pt. Punto filtresi olmadan altyazıya
        # "CPU" gibi etiketler ve arkasından gelen paragraf yapışıyordu.
        cap_size = first.get("size") or 0.0

        for cand in lines:
            if not (line["top"] - 2 <= cand["top"] <= line["bottom"] + 60):
                continue
            if abs(cand["x0"] - first["x0"]) > _CAPTION_COLUMN_TOL:
                continue
            words = cand["words"]
            if beside:
                words = [w for w in words if w["x0"] < cluster["x0"] - 2]
            if cap_size:
                words = [w for w in words if abs((w.get("size") or 0.0) - cap_size) < 0.4]
            if not words:
                continue
            parts.append(" ".join(w["text"] for w in words))
            box["x1"] = max(box["x1"], max(w["x1"] for w in words))
            box["bottom"] = max(box["bottom"], cand["bottom"])
        text = " ".join(parts)
        text = _CAPTION.sub(lambda mm: mm.group(2), text, count=1).strip(" .")
        return number, text, box
    return None


def _merge_same_number(figures: list[BookFigure]) -> list[BookFigure]:
    """Aynı numaraya bağlanmış çizim kümelerini tek şekle indirger.

    Bir şekil birden çok çizim kümesine bölünebiliyor (aralarındaki boşluk
    kümeleme eşiğini aşınca), sonuçta aynı numara iki kez çıkıyordu. Yakınsalar
    kutuları birleştirilir; uzaksalar aynı numaranın iki ayrı parçası değil,
    yanlış eşleşme olma ihtimali yüksektir — büyük olan tutulur.
    """
    by_number: dict[str, BookFigure] = {}
    for fig in figures:
        prev = by_number.get(fig.number)
        if prev is None:
            by_number[fig.number] = fig
            continue
        gap = max(prev.bbox[1], fig.bbox[1]) - min(prev.bbox[3], fig.bbox[3])
        if gap <= 120:
            by_number[fig.number] = BookFigure(
                number=fig.number,
                caption=prev.caption or fig.caption,
                page=prev.page,
                bbox=(
                    min(prev.bbox[0], fig.bbox[0]),
                    min(prev.bbox[1], fig.bbox[1]),
                    max(prev.bbox[2], fig.bbox[2]),
                    max(prev.bbox[3], fig.bbox[3]),
                ),
            )
        elif _area(fig.bbox) > _area(prev.bbox):
            by_number[fig.number] = fig
    return list(by_number.values())


def _area(bbox: tuple[float, float, float, float]) -> float:
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def _crop_offset(page) -> tuple[float, float]:
    """pdfplumber koordinatı → pypdfium2 bitmap koordinatı kayması.

    Kayma, CropBox'ın SOL-ALT köşesidir: `(cropbox.x0, cropbox.y0)`.

    Bu ÖLÇÜLDÜ, türetilmedi. "Doğal" görünen `page.height - cropbox.y1`
    (=97.92) yanlış sonuç veriyor: kırpım tam bir satır aşağı kayıyor ve
    şeklin üst satırı kesiliyor. Ölçüm yöntemi — bitmap'in satır bazlı
    mürekkep profilini pdfplumber'ın satır kutularıyla çakıştırıp en yüksek
    örtüşmeyi veren kaymayı aramak. Üç ayrı sayfada da sonuç aynı çıktı:
    dy=114-115 (cropbox.y0=115.2), dx=70 (cropbox.x0=70.56); yanlış formülün
    skoru doğrusunun %60'ı kadar kalıyor.

    Kırpım bir satır kaymış görünüyorsa önce burayı sına.
    """
    cb = page.cropbox
    return cb[0], cb[1]


def find_figures(page, *, min_objects: int = 8, gap: float = 30.0) -> list[BookFigure]:
    """Bir pdfplumber sayfasındaki altyazılı şekilleri döndürür."""
    objs = list(page.rects) + list(page.lines) + list(page.curves)
    clusters = _cluster(objs, gap=gap, min_objects=min_objects)
    if not clusters:
        return []

    lines = _lines(page)
    dx, dy = _crop_offset(page)
    cw = page.cropbox[2] - page.cropbox[0]
    ch = page.cropbox[3] - page.cropbox[1]
    # Sayfa üst/alt bilgisi de küçük punto ("50 Chapter 1 A Tour of Computer
    # Systems" 8.47pt) — punto filtresini geçip kırpımı sayfanın tepesine kadar
    # çekiyordu. Gövde şeridinin dışındaki satırlar hiçbir şeyi genişletemez.
    # Sınır ölçüldü: üst bilgi her sayfada MediaBox 103.0-112.5, ilk gerçek
    # içerik en erken 126.8. 120 ikisinin tam ortasında.
    body_top = 120.0
    body_bottom = dy + ch - 18

    figures: list[BookFigure] = []
    for cl in clusters:
        found = _caption_for(cl, lines)
        if found is None:
            continue  # altyazısız çizim kümesi = tablo/cetvel, şekil değil
        number, caption, cap_box = found

        # Altyazı kırpıma DAHİL. Dışarıda bırakmayı denemek, altyazı sütunuyla
        # şekil etiketleri iç içe geçtiği için kutuyu kelimenin ortasından
        # kesiyordu. Kitabın altyazısı zaten şeklin görsel bileşeninin parçası;
        # render katmanının kendi künyesi kısa tutularak tekrar önleniyor.
        x0 = min(cl["x0"], cap_box["x0"])
        x1 = max(cl["x1"], cap_box["x1"])
        top = min(cl["top"], cap_box["top"])
        bottom = max(cl["bottom"], cap_box["bottom"])
        for line in lines:
            inside = line["top"] >= cl["top"] - 4 and line["bottom"] <= cl["bottom"] + 4
            if inside:
                x0, x1 = min(x0, line["x0"]), max(x1, line["x1"])
                continue

            # Şeklin en üst/alt etiketleri çizim kümesinin dışına taşabiliyor
            # ("2³ = 8" satırı kesiliyordu). Dikey genişletmeyi serbest bırakmak
            # alttaki paragrafı içeri alır, o yüzden iki şart birden aranır:
            # etiket punto'su gövdeden küçük OLACAK ve şeklin yatay aralığıyla
            # örtüşecek. Gövde paragrafı sol kenardan başlar ve 10pt'dir.
            size = min((w.get("size") or 99.0) for w in line["words"])
            overlaps = line["x1"] > cl["x0"] and line["x0"] < cl["x1"]
            in_body = line["top"] >= body_top and line["bottom"] <= body_bottom
            if size >= 9.5 or not overlaps or not in_body:
                continue
            if cl["top"] - 20 <= line["bottom"] <= cl["top"]:
                top = min(top, line["top"])
                x0, x1 = min(x0, line["x0"]), max(x1, line["x1"])
            elif cl["bottom"] <= line["top"] <= cl["bottom"] + 20:
                bottom = max(bottom, line["bottom"])
                x0, x1 = min(x0, line["x0"]), max(x1, line["x1"])

        pad = 10.0
        bbox = (
            max(0.0, x0 - dx - pad),
            max(0.0, top - dy - pad),
            min(cw, x1 - dx + pad),
            min(ch, bottom - dy + pad),
        )
        if bbox[2] - bbox[0] < 40 or bbox[3] - bbox[1] < 30:
            continue  # anlamsız küçük kırpma
        figures.append(
            BookFigure(number=number, caption=caption, page=page.page_number, bbox=bbox)
        )
    return _merge_same_number(figures)


def scan_figures(path: str | Path, pages: list[int] | None = None) -> list[BookFigure]:
    """Kitabın tamamını (veya verilen sayfaları) tarar."""
    out: list[BookFigure] = []
    with pdfplumber.open(str(path)) as pdf:
        targets = pages or range(1, len(pdf.pages) + 1)
        for pno in targets:
            if 1 <= pno <= len(pdf.pages):
                out.extend(find_figures(pdf.pages[pno - 1]))
    return out


def crop_figure(path: str | Path, fig: BookFigure, *, max_edge: int = 900) -> bytes:
    """Şekli PNG olarak kırpar. `bbox` zaten CropBox-göreli, doğrudan ölçeklenir."""
    w = fig.bbox[2] - fig.bbox[0]
    h = fig.bbox[3] - fig.bbox[1]
    scale = min(4.0, max(1.0, max_edge / max(w, h)))

    doc = pdfium.PdfDocument(str(path))
    try:
        pil = doc[fig.page - 1].render(scale=scale).to_pil().convert("RGB")
        box = tuple(int(round(v * scale)) for v in fig.bbox)
        # Yuvarlama bitmap sınırını bir piksel aşabilir; taşan kırpma siyah dolgu verir.
        box = (
            max(0, box[0]),
            max(0, box[1]),
            min(pil.width, box[2]),
            min(pil.height, box[3]),
        )
        crop = pil.crop(box)
    finally:
        doc.close()

    buf = io.BytesIO()
    crop.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
