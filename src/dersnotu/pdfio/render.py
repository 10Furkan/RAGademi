"""Slayt sayfalarını PNG'ye render eder (pypdfium2 — BSD/Apache lisanslı).

PyMuPDF daha zengin ama AGPL; ticari kullanımı engellememek için pypdfium2
tercih edildi.

Çözünürlük doğrudan maliyet demek: bir slayt görüntüsü 1.5-4.8K token
arasında faturalanıyor. `max_edge` bunun freni.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium

# Claude görüntü token yaklaşımı: (genişlik × yükseklik) / 750.
IMAGE_TOKEN_DIVISOR = 750


@dataclass
class RenderedPage:
    png: bytes
    width: int
    height: int

    @property
    def token_estimate(self) -> int:
        return int(self.width * self.height / IMAGE_TOKEN_DIVISOR)


def render_pages(
    path: str | Path, page_numbers: list[int], *, max_edge: int = 1400
) -> dict[int, RenderedPage]:
    """Verilen 1-tabanlı sayfaları render eder.

    `max_edge` doğrudan maliyet kolu: token sayısı piksel alanıyla orantılı,
    yani kenarı yarıya indirmek görüntü maliyetini dörtte bire düşürür.
    """
    if not page_numbers:
        return {}

    out: dict[int, RenderedPage] = {}
    pdf = pdfium.PdfDocument(str(path))
    try:
        for n in page_numbers:
            idx = n - 1
            if idx < 0 or idx >= len(pdf):
                continue
            page = pdf[idx]
            w, h = page.get_size()
            scale = min(max_edge / max(w, h), 3.0)  # aşırı büyütme yok
            pil = page.render(scale=scale).to_pil()
            buf = io.BytesIO()
            pil.save(buf, format="PNG", optimize=True)
            out[n] = RenderedPage(png=buf.getvalue(), width=pil.width, height=pil.height)
    finally:
        pdf.close()
    return out


def to_image_block(page: RenderedPage) -> dict:
    """Messages API için image content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(page.png).decode("ascii"),
        },
    }
