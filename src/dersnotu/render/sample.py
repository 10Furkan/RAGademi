"""Render hattını API'siz doğrulamak için örnek doküman.

İçerik kasıtlı olarak zor: satır içi ve blok matematik, C ve assembly kod
blokları, tablo, kitap/slayt atıfları, şekil işaretçisi, uyarı bloğu ve
üretilememiş bir bölüm. Render'da bunlardan biri bozulursa hemen görünür.
"""

from __future__ import annotations

from ..models import ExpandedSection, StudyDocument, Usage

_S1 = """\
## Tamsayıların İkilik Gösterimi

Bir bilgisayarda tamsayı saklamanın iki yolu var: **işaretsiz** (unsigned) ve
**işaretli** (signed). Aradaki fark, aynı bit dizisinin nasıl yorumlandığıdır —
bitler değişmez, yorum değişir. [S: 22]

### İşaretsiz yorum

$w$ bitlik bir vektör $\\vec{x} = [x_{w-1}, x_{w-2}, \\dots, x_0]$ için işaretsiz
değer basitçe taban-2 açılımıdır:

$$B2U_w(\\vec{x}) = \\sum_{i=0}^{w-1} x_i \\cdot 2^i$$

Aralık $0 \\le B2U_w(\\vec{x}) \\le 2^w - 1$. [K: Integer Representations, s. 96-98]

### İkiye tümleyen yorum

İkiye tümleyende en anlamlı bit **negatif** ağırlık taşır — tek fark budur:

$$B2T_w(\\vec{x}) = -x_{w-1} \\cdot 2^{w-1} + \\sum_{i=0}^{w-2} x_i \\cdot 2^i$$

Bu yüzden aralık asimetriktir: $TMin_w = -2^{w-1}$, $TMax_w = 2^{w-1}-1$, yani
$|TMin_w| = TMax_w + 1$. 16 bit için somut olarak: [K: Integer Representations, s. 98-100]

| Gösterim | İkilik | Onaltılık | Ondalık |
|---|---|---|---|
| `short x` | `00111011 01101101` | `3B 6D` | 15213 |
| `short y` | `11000100 10010011` | `C4 93` | −15213 |
| `TMax` | `01111111 11111111` | `7F FF` | 32767 |
| `TMin` | `10000000 00000000` | `80 00` | −32768 |

[ŞEKİL: slayt 22]

> ⚠️ Sık yapılan hata: `-TMin` işlemi taşar ve yine `TMin` verir. Çünkü $+32768$
> 16 bitlik işaretli aralıkta yok.

::: analoji
İkiye tümleyeni bir araba kilometre sayacı gibi düşün: 0'dan geri sardığında
99999'a düşer. En anlamlı basamak "geri sarma" tarafını gösterir.

**Nerede bozulur:** Sayaçta tüm basamaklar aynı yönde çalışır; ikiye tümleyende
yalnızca en anlamlı bit negatif ağırlıklıdır, kalanı normal taban-2'dir.
:::

::: soru
1. 8 bitlik ikiye tümleyende `0xB4` hangi ondalık sayıdır?
2. `TMin` ile `-TMin` neden aynı bit desenine sahip?
3. 16 bitlik `short` için $|TMin| - TMax$ kaçtır?

**Yanıtlar:** 1) $-76$ 2) $+2^{w-1}$ aralıkta olmadığı için toplama taşar 3) $1$
:::

::: sözlük
| Terim | İngilizce | Anlamı |
|---|---|---|
| İkiye tümleyen | two's complement | En anlamlı biti negatif ağırlıklı işaretli gösterim |
| Taşma | overflow | Sonucun temsil aralığının dışına çıkması |
| En anlamlı bit | most significant bit | Vektörün en soldaki, en büyük ağırlıklı biti |
:::
"""

_S2 = """\
## İşaretli ↔ İşaretsiz Dönüşüm

C'de iki tür arasındaki cast **bit desenini korur, yorumu değiştirir**.
[K: Integer Representations, s. 102-104]

```c
short int  v  = -12345;
unsigned short uv = (unsigned short) v;
printf("v = %d, uv = %u\\n", v, uv);   /* v = -12345, uv = 53191 */
```

$-12345 + 2^{16} = 53191$ — yani negatif değere $2^w$ eklenmiş oluyor:

$$T2U_w(x) = \\begin{cases} x + 2^w & x < 0 \\\\ x & x \\ge 0 \\end{cases}$$

### Asıl tuzak: örtük dönüşüm

Bir ifadede işaretli ve işaretsiz karışırsa, **işaretli olan işaretsize
dönüştürülür**. Karşılaştırma operatörleri de buna dahil: [K: Integer Representations, s. 104-106]

```c
if (-1 < 0u) { /* ÇALIŞMAZ: -1 önce 4294967295u olur */ }
```

Derleyicinin ürettiği koda bakınca fark daha net görünüyor:

```asm
    cmpl    $0, %eax        # işaretli karşılaştırma
    jl      .L2
    cmpl    $0, %eax        # işaretsiz: aynı komut, farklı atlama
    jb      .L2
```

Kural: `sizeof` işaretsiz döndürür, o yüzden `if (i < sizeof(a) - 1)` ifadesi
`a` boşken sonsuz döngüye girebilir. [S: 27, 28]
"""

_S3 = """\
## Bit Düzeyinde İşlemler

C'de `&`, `|`, `~`, `^` operatörleri bit vektörleri üzerinde eleman bazında
çalışır ve herhangi bir tamsayı türüne uygulanabilir. [S: 18]

Küme gösterimi için doğal bir kullanım var: $w$ bitlik vektör
$\\{0, \\dots, w-1\\}$ kümesinin alt kümelerini temsil eder, $a_j = 1 \\iff j \\in A$.
Kesişim `&`, birleşim `|`, simetrik fark `^`, tümleyen `~` olur.
[K: Information Storage, s. 85-87]

```c
unsigned char a = 0x69;   /* 01101001 -> {0,3,5,6} */
unsigned char b = 0x55;   /* 01010101 -> {0,2,4,6} */
a & b;   /* 0x41  01000001 -> {0,6}       kesişim */
a | b;   /* 0x7D  01111101 -> {0,2,..,6}  birleşim */
a ^ b;   /* 0x3C  00111100 -> {2,3,4,5}   simetrik fark */
```

`&&` `||` `!` ile karıştırma: mantıksal operatörler 0'ı yanlış, sıfırdan
farklı her şeyi doğru sayar ve **daima 0 veya 1** döndürür; ayrıca kısa devre
yaparlar. `!!x` deyimi bu yüzden "x'i 0/1'e indirge" anlamına gelir.
"""


def sample_document() -> StudyDocument:
    return StudyDocument(
        lecture_title="Bits, Bytes, and Integers",
        language="Türkçe",
        sections=[
            ExpandedSection(
                section_index=0,
                title="Tamsayıların İkilik Gösterimi",
                markdown=_S1,
                slide_range=(22, 24),
                citations=["Integer Representations, s. 96-98"],
            ),
            ExpandedSection(
                section_index=1,
                title="İşaretli ↔ İşaretsiz Dönüşüm",
                markdown=_S2,
                slide_range=(26, 29),
                citations=["Integer Representations, s. 102-104"],
            ),
            ExpandedSection(
                section_index=2,
                title="Bit Düzeyinde İşlemler",
                markdown=_S3,
                slide_range=(15, 20),
                citations=["Information Storage, s. 85-87"],
            ),
            ExpandedSection(
                section_index=3,
                title="Çarpma ve Bölme",
                markdown="",
                slide_range=(40, 50),
                error="RateLimitError (örnek)",
            ),
        ],
        usage=Usage(input_tokens=112_000, output_tokens=24_000, calls=6),
    )
