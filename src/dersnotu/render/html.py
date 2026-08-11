"""Sayfa şablonu ve stil.

Tasarım yönü — "Kenar Notu": iyi anotasyonlu bir ders kitabının görsel dili.
Geniş sol kenar rayı kitap atıflarını taşır; bu süs değil, kaynak izlenebilirliği
demek ve öğrencinin "bu bilgi nereden geldi" sorusunu sayfayı terk etmeden
yanıtlar. Vurgu rengi fosforlu kalem — metnin ARKASINDA, düşük opaklıkta;
öğrencinin kitabı işaretleme jestinden geliyor.

Fontlar bilinçli olarak sistem fontlarına dayanıyor: render sırasında ağa
çıkılmıyor ve Georgia/Cambria/Consolas'ın Türkçe glifleri (ş ğ İ ı Ç) tam.
"""

from __future__ import annotations

import base64
import re
from functools import lru_cache
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "katex"

CSS = """
:root {
  --paper:      #F2F3F0;
  --ink:        #161A22;
  --ink-muted:  #5C6370;
  --rule:       #D8DAD3;
  --marker:     #D9F04B;
  --slide-tint: #E8EDF2;
}

/* Sayfa numarasını Playwright'ın footer_template'i basıyor; burada da
   basmak iki ayrı numaralandırmanın üst üste gelmesine yol açıyor. */
@page {
  size: A4;
  margin: 18mm 16mm 20mm 16mm;
}

* { box-sizing: border-box; }

html { font-size: 10.5pt; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: "Literata", Georgia, Cambria, "Times New Roman", serif;
  line-height: 1.58;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}

/* --- Sayfa iskeleti: gövde + kenar rayı ------------------------------ */
.page {
  display: grid;
  grid-template-columns: 1fr 38mm;
  column-gap: 8mm;
  max-width: 190mm;
  margin: 0 auto;
}
.content { grid-column: 1; min-width: 0; }

/* --- Kapak ------------------------------------------------------------ */
.cover {
  grid-column: 1 / -1;
  border-bottom: 2px solid var(--ink);
  padding-bottom: 6mm;
  margin-bottom: 8mm;
}
.cover .eyebrow {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 8pt;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink-muted);
  margin-bottom: 3mm;
}
.cover h1 {
  font-size: 26pt;
  line-height: 1.12;
  margin: 0 0 4mm;
  letter-spacing: -0.015em;
  font-weight: 600;
}
.cover .meta {
  font-size: 9pt;
  color: var(--ink-muted);
}
.cover .meta strong { color: var(--ink); font-weight: 600; }

/* --- Başlıklar -------------------------------------------------------- */
h2 {
  font-size: 15.5pt;
  font-weight: 600;
  letter-spacing: -0.01em;
  margin: 11mm 0 3mm;
  padding-top: 3mm;
  border-top: 1px solid var(--rule);
  break-after: avoid;
}
h2:first-of-type { margin-top: 0; border-top: none; padding-top: 0; }
h3 {
  font-size: 11.5pt;
  font-weight: 600;
  margin: 6mm 0 2mm;
  break-after: avoid;
}
h4 { font-size: 10.5pt; font-weight: 600; margin: 4mm 0 1.5mm; break-after: avoid; }

p { margin: 0 0 3.2mm; orphans: 3; widows: 3; }
ul, ol { margin: 0 0 3.2mm; padding-left: 5mm; }
li { margin-bottom: 1.2mm; }
li > ul, li > ol { margin-top: 1.2mm; }

strong { font-weight: 600; }

/* İmza öğe: fosforlu kalem — metnin arkasında, üstünde değil. */
mark, .hl {
  background: linear-gradient(transparent 58%, color-mix(in srgb, var(--marker) 62%, transparent) 58%);
  padding: 0 0.1em;
}

/* --- Atıf chip'leri: kenar rayına taşan not -------------------------- */
.cite {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 7.4pt;
  line-height: 1.35;
  letter-spacing: 0.01em;
  display: inline-block;
}
.cite-book {
  float: right;
  clear: right;
  width: 38mm;
  margin: 0.4mm -46mm 2mm 0;   /* raya taşır */
  padding-left: 2.5mm;
  border-left: 2px solid var(--marker);
  color: var(--ink-muted);
  break-inside: avoid;   /* sayfa sonunda etiket/metin ayrılmasın */
}
.cite-book::before {
  content: "Kitap";
  display: block;
  font-size: 6.4pt;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ink);
  opacity: 0.55;
}
.cite-slide {
  font-size: 7pt;
  color: var(--ink-muted);
  border-bottom: 1px dotted var(--rule);
  margin-left: 0.15em;
}

/* --- Kod --------------------------------------------------------------
   Pygments `<div class="code"><pre><code>` üretiyor. Çerçeve/zemin YALNIZCA
   dış kapsayıcıya verilir; iç <pre> ve <code> sıfırlanır, aksi halde
   iç içe üç çerçeve çizilir. */
pre.code {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 8.6pt;
  line-height: 1.45;
  background: #FFFFFF;
  border: 1px solid var(--rule);
  border-left: 3px solid var(--ink);
  border-radius: 2px;
  padding: 2.5mm 3mm;
  margin: 0 0 3.2mm;
  break-inside: avoid;
  white-space: pre-wrap;
  word-break: break-word;
  font-variant-numeric: tabular-nums;   /* hex dump hizalaması */
}
.code code {
  background: none;
  border: none;
  border-radius: 0;
  padding: 0;
  margin: 0;
  font-size: inherit;
  font-family: inherit;
}
/* Satır içi `kod` */
:not(pre) > code {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 0.88em;
  background: #FFFFFF;
  border: 1px solid var(--rule);
  border-radius: 2px;
  padding: 0.05em 0.3em;
}

/* --- Tablolar --------------------------------------------------------- */
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 9pt;
  margin: 0 0 3.2mm;
  break-inside: avoid;
  font-variant-numeric: tabular-nums;
}
th, td { padding: 1.4mm 2mm; text-align: left; border-bottom: 1px solid var(--rule); }
th {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 7.6pt;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-muted);
  border-bottom: 1.5px solid var(--ink);
}

/* --- Alıntı / uyarı --------------------------------------------------- */
blockquote {
  margin: 0 0 3.2mm;
  padding: 2mm 3mm;
  background: color-mix(in srgb, var(--marker) 16%, transparent);
  border-left: 3px solid var(--marker);
  font-size: 9.4pt;
}
blockquote p:last-child { margin-bottom: 0; }

/* --- Açıklama blokları ------------------------------------------------
   Üçü de aynı sesin farklı registerleri; rastgele üç renkli kutu değil.
   Analoji kenar notudur (dolgusuz), sözlük referans verisidir (şekillerle
   aynı zemin), "kendini sına" öğrencinin İŞ YAPTIĞI yerdir — en güçlü
   çerçeveyi ve fosforlu kalem etiketini o alır. */
.callout { margin: 4.5mm 0; break-inside: avoid; }
.callout-label {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 6.8pt;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  margin: 0 0 1.8mm;
}
.callout > :last-child { margin-bottom: 0; }

.callout-analogy {
  padding-left: 3mm;
  border-left: 2px solid var(--rule);
  font-size: 9.6pt;
}
.callout-analogy .callout-label { color: var(--ink-muted); }

.callout-quiz {
  background: #FFFFFF;
  border: 1.5px solid var(--ink);
  border-radius: 2px;
  padding: 3mm 3.5mm;
}
.callout-quiz .callout-label {
  display: inline-block;
  background: var(--marker);
  color: var(--ink);
  padding: 0.7mm 1.8mm;
  margin-bottom: 2.2mm;
}
.callout-quiz ol, .callout-quiz ul { padding-left: 4.5mm; }

.callout-glossary {
  background: var(--slide-tint);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 2.5mm 3mm;
}
.callout-glossary .callout-label { color: var(--ink-muted); }
.callout-glossary table { margin-bottom: 0; }
.callout-glossary th { border-bottom-color: var(--ink-muted); }

/* Geçmiş sınav: kâğıttan kesilmiş bir parça. Üst ve alt cetvel dışında
   kutulama yok — "kendini sına" zaten kutuyu ve fosforlu etiketi almış
   durumda, ikisi de kutu olsaydı öğrenci hangisinde İŞ yapacağını
   ayırt edemezdi. Bu blok bilgi verir, iş istemez. */
.callout-exam {
  border-top: 1.5px solid var(--ink);
  border-bottom: 1.5px solid var(--ink);
  padding: 2.8mm 0 2.5mm;
}
.callout-exam .callout-label {
  display: inline-block;
  border: 1px solid var(--ink);
  color: var(--ink);
  padding: 0.6mm 1.8mm;
  margin-bottom: 2.4mm;
}
.callout-exam strong { font-variant: all-small-caps; letter-spacing: 0.04em; }

/* --- Okuyucu çubuğu ---------------------------------------------------
   Kaydedilen HTML iki mecrada kullanılıyor: tarayıcıda okuyucu, Chromium'da
   PDF. Çubuk belgenin İÇİNDE durur ve baskıda gizlenir — HTML'i sonradan
   string ile kesip yapıştırmaktan çok daha az kırılgan. */
.docnav {
  max-width: 190mm;
  margin: 0 auto 6mm;
  padding: 4mm 0 3mm;
  display: flex;
  gap: 4mm;
  align-items: baseline;
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 8pt;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.docnav a { color: var(--ink-muted); text-decoration: none; }
.docnav a:hover { color: var(--ink); }
.docnav .sep { color: var(--rule); }
@media print { .docnav { display: none; } }

/* --- Slayt şekilleri -------------------------------------------------- */
.slide-figure {
  margin: 4mm 0;
  padding: 2.5mm;
  background: var(--slide-tint);
  border: 1px solid var(--rule);
  border-radius: 3px;
  break-inside: avoid;
}
.slide-figure img { width: 100%; height: auto; display: block; border-radius: 2px; }
/* Kitap şekli: slayt şeklinden AYRI görünmeli — biri dersin, öteki kitabın.
   Slayt şekli renkli zeminde (ekrandan geliyor), kitap şekli kağıt zeminde
   ince çerçevede (basılı sayfadan geliyor). */
.book-figure {
  margin: 4mm 0;
  padding: 2.5mm;
  background: #FFFFFF;
  border: 1px solid var(--rule);
  border-left: 3px solid var(--ink-muted);
  border-radius: 3px;
  break-inside: avoid;
}
.book-figure img { width: 100%; height: auto; display: block; }
.book-figure figcaption {
  display: flex;
  gap: 2.5mm;
  align-items: baseline;
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 6.8pt;
  color: var(--ink-muted);
  margin-top: 1.8mm;
}
/* `text-transform: uppercase` YALNIZCA Türkçe etikette. Sayfa lang="tr"
   olduğu için İngilizce "Figure" kelimesi "FİGURE" oluyordu — kitabın kendi
   etiketi bozuluyor. Referans kendi yazımıyla ve lang="en" ile bırakılır. */
.book-figure .src {
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ink);
}
.book-figure .ref { letter-spacing: 0.04em; }
.book-figure .pg { margin-left: auto; white-space: nowrap; }

.slide-figure figcaption {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 7.2pt;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ink-muted);
  margin-top: 1.8mm;
}

.figure-missing {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 7.6pt;
  color: var(--ink-muted);
  border: 1px dashed var(--rule);
  border-radius: 3px;
  padding: 2mm 2.5mm;
  margin: 3mm 0;
}

/* --- Matematik -------------------------------------------------------- */
.math-block { margin: 3mm 0; text-align: center; break-inside: avoid; }
.katex { font-size: 1.03em; }
.katex-display { margin: 0; }

/* --- İçindekiler ------------------------------------------------------ */
.toc { grid-column: 1 / -1; margin-bottom: 10mm; break-after: page; }
.toc h2 { border: none; margin: 0 0 3mm; padding: 0; font-size: 12pt; }
.toc ol { list-style: none; padding: 0; margin: 0; counter-reset: toc; }
.toc li {
  counter-increment: toc;
  display: flex;
  justify-content: space-between;
  gap: 3mm;
  padding: 1.4mm 0;
  border-bottom: 1px dotted var(--rule);
  font-size: 9.6pt;
}
.toc li::before {
  content: counter(toc, decimal-leading-zero);
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 7.6pt;
  color: var(--ink-muted);
  flex: 0 0 7mm;
}
/* Başlık kalan genişliği doldurmalı; yoksa space-between onu ortalıyor. */
.toc .label { flex: 1 1 auto; text-align: left; }
.toc .slides {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 7.6pt;
  color: var(--ink-muted);
  white-space: nowrap;
}

/* --- Deneme sınavı ----------------------------------------------------
   Soru bir İŞ birimidir: üstünde cetvel, künyesi mono, puanı sağda — sınav
   kâğıdı jesti. Kutu YOK; sekiz soruyu üst üste kutulamak sayfayı kafese
   çevirir, oysa burada kutulanacak tek şey "kendini sına" bloğu zaten var. */
.exam-profile {
  font-size: 9.6pt;
  color: var(--ink-muted);
  padding-left: 3mm;
  border-left: 2px solid var(--rule);
  margin: 0 0 3mm;
}
.exam-source {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 7.6pt;
  color: var(--ink-muted);
  margin: 0 0 8mm;
}

.exam-q, .exam-a {
  border-top: 1px solid var(--rule);
  padding-top: 2.5mm;
  margin: 0 0 7mm;
  break-inside: avoid;
}
.qhead {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 4mm;
  margin-bottom: 2.2mm;
}
.qnum {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 7.6pt;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ink);
}
.qhead .pts {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 7.4pt;
  color: var(--ink-muted);
  white-space: nowrap;
  text-decoration: none;
}
.qhead a.pts:hover { color: var(--ink); }

/* Şıklar: A) B) C). Harf `list-style` ile geliyor, metne yazılmıyor —
   model şık harfini kendi yazsaydı numaralandırma iki kez basılırdı. */
.choices {
  list-style: upper-alpha;
  padding-left: 7mm;
  margin: 2mm 0 0;
}
.choices li { margin-bottom: 1.4mm; }
.choices li::marker {
  font-family: "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-size: 8.6pt;
  color: var(--ink-muted);
}

/* Anahtar ayrı sayfada başlar: çözüm soruyla aynı yaprakta olursa kâğıt
   denemelik olmaktan çıkar. */
.exam-key { break-before: page; margin-top: 10mm; }
.exam-key > h2 { margin-top: 0; }
.exam-a { border-top-style: dotted; }
.answer { font-size: 10.5pt; }
.answer b { font-variant: all-small-caps; letter-spacing: 0.04em; }
.exam-a .callout-exam { margin: 3mm 0 0; }
.exam-a .callout-exam blockquote {
  background: none;
  border-left: none;
  padding: 0;
  margin: 0;
  font-size: 9.2pt;
  color: var(--ink-muted);
}

/* --- Hata bloğu ------------------------------------------------------- */
.failed {
  border: 1px dashed #C4574C;
  background: #FBEEEC;
  padding: 2.5mm 3mm;
  margin: 0 0 4mm;
  font-size: 9pt;
  color: #7A2E26;
}

@media print {
  body { background: #FFFFFF; }
  .slide-figure, pre, table, .math-block { break-inside: avoid; }
}
"""

_TEMPLATE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{katex_css}</style>
<style>{pygments_css}</style>
<style>{css}</style>
</head>
<body>
{nav}
<div class="page">
  <header class="cover">
    <div class="eyebrow">{eyebrow}</div>
    <h1>{title}</h1>
    <div class="meta">{meta}</div>
  </header>
  {toc}
  <main class="content">
{body}
  </main>
</div>
<script>{katex_js}</script>
<script>{autorender_js}</script>
<script>
  document.addEventListener("DOMContentLoaded", function () {{
    renderMathInElement(document.body, {{
      delimiters: [
        {{left: "\\\\[", right: "\\\\]", display: true}},
        {{left: "\\\\(", right: "\\\\)", display: false}}
      ],
      throwOnError: false,
      errorColor: "#C4574C"
    }});
    document.body.dataset.mathReady = "1";
  }});
</script>
</body>
</html>
"""


@lru_cache(maxsize=1)
def _katex_css() -> str:
    """KaTeX CSS'ini fontlar GÖMÜLÜ olacak şekilde okur.

    Font yolları eskiden mutlak `file://` yapılıyordu. Bu, Chromium HTML'i
    diskten açtığı sürece (PDF basımı) çalışıyor; ama aynı HTML uygulama içi
    okuyucuda **http://** üzerinden sunuluyor ve o zaman tarayıcı her fontu
    "Not allowed to load local resource" diyerek reddediyor — matematik yedek
    fontla, yanlış görünüyor.

    Çözüm data: URI: tek bir HTML iki mecrada da doğru çalışır. Yalnızca
    woff2 gömülür (~296 KB → ~395 KB base64); woff/ttf yedekleri
    ayıklanır, çünkü hedef tarayıcı zaten Chromium ve üç kopya gömmek
    dosyayı gereksiz üçe katlar.
    """
    css_path = ASSETS / "katex.min.css"
    if not css_path.exists():
        return ""
    css = css_path.read_text(encoding="utf-8")

    def gom(m: re.Match) -> str:
        f = ASSETS / "fonts" / m.group(1)
        if not f.exists():
            return m.group(0)
        veri = base64.b64encode(f.read_bytes()).decode("ascii")
        return f'url(data:font/woff2;base64,{veri}) format("woff2")'

    # KaTeX'in dağıttığı CSS `format("woff2")` yazıyor — tırnak türü sürüme
    # göre değişebildiği için ikisi de kabul ediliyor. Eşleşmezse fontlar
    # sessizce gömülmez ve hata ancak tarayıcıda görülür.
    q = r"[\"']"
    css = re.sub(rf",\s*url\(fonts/[^)]+\)\s*format\({q}(?:woff|truetype){q}\)", "", css)
    css, n = re.subn(rf"url\(fonts/([^)]+\.woff2)\)\s*format\({q}woff2{q}\)", gom, css)
    if not n:  # pragma: no cover — sürüm değişirse burada yakalanır
        raise RuntimeError(
            "KaTeX CSS'inde gömülecek woff2 bulunamadı; font sözdizimi değişmiş "
            "olabilir. Gömülmezse okuyucuda matematik yedek fontla çıkar."
        )
    return css


def _read(name: str) -> str:
    p = ASSETS / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def assets_available() -> bool:
    return (ASSETS / "katex.min.css").exists() and (ASSETS / "katex.min.js").exists()


def build_toc(sections) -> str:
    if len(sections) < 2:
        return ""
    items = []
    for sec in sections:
        a, b = sec.slide_range
        title = sec.title or f"Section {sec.section_index + 1}"
        items.append(
            f'<li><span class="label">{title}</span>'
            f'<span class="slides">slayt {a}–{b}</span></li>'
        )
    return '<nav class="toc"><h2>Table of contents</h2><ol>' + "".join(items) + "</ol></nav>"


def build_nav(course_href: str, course_name: str, pdf_href: str = "") -> str:
    """Okuyucu çubuğu. Baskıda gizli; PDF yolu boş string geçer."""
    parts = [f'<a href="{course_href}">← {course_name}</a>']
    if pdf_href:
        parts += ['<span class="sep">·</span>', f'<a href="{pdf_href}">PDF indir</a>']
    return f'<nav class="docnav">{"".join(parts)}</nav>'


def build_page(*, title: str, body_html: str, meta: str, toc_html: str,
               lang: str = "tr", nav_html: str = "",
               eyebrow: str = "Study notes · expanded with textbook evidence") -> str:
    from .markdown import pygments_css

    return _TEMPLATE.format(
        lang=lang,
        title=title,
        meta=meta,
        eyebrow=eyebrow,
        toc=toc_html,
        body=body_html,
        nav=nav_html,
        css=CSS,
        pygments_css=pygments_css(),
        katex_css=_katex_css(),
        katex_js=_read("katex.min.js"),
        autorender_js=_read("auto-render.min.js"),
    )
