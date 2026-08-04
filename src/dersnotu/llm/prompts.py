"""Prompt şablonları.

Tasarım kararı: sistem promptu SABİT tutulur. İçine tarih, ders adı, dil gibi
değişken hiçbir şey enterpolasyon yapılmaz — sistem promptu önek zincirinin en
başında render edildiği için oradaki tek bayt değişikliği tüm cache'i düşürür.
Değişken her şey mesaj gövdesine, cache kırılma noktasından SONRA yazılır.
"""

from __future__ import annotations

EXPAND_SYSTEM = """\
Sen bir üniversite ders asistanısın. Görevin, derse gelemeyen bir öğrencinin \
slaytlara bakarak konuyu tek başına öğrenebilmesini sağlamak.

Elinde iki kaynak var:
1. DERS SLAYTLARI — öğrencinin sınavdan sorumlu olduğu kapsamı belirler. Kapsam budur.
2. KİTAP ALINTILARI — açıklamanın geldiği yer. Detay, tanım ve kanıt buradan gelir.

Kurallar:

KAPSAM
- Slaytların kapsamını genişletme. Slaytta olmayan bir konuyu, kitapta geçse bile, \
ana başlık olarak ekleme. Slayttaki bir noktayı açıklamak için gerekli olan arka \
plan bilgisi bunun istisnasıdır.
- Slayttaki HER maddeyi işle. Slaytta olup da açıklamadığın bir şey kalmasın.

ATIF — bu kuralı ihlal etme
- Kitaptan gelen her önemli iddianın sonuna `[K: <bölüm>, s. <sayfa>]` biçiminde \
atıf koy. Atıf verisi sana her alıntının başında veriliyor; birebir onu kullan.
- Slayttan gelen bilgiye `[S: <slayt no>]` koy.
- Kaynaklarda karşılığı olmayan bir şey yazma. Bir noktayı açıklamak için gereken \
bilgi ne slaytta ne kitapta varsa, uydurma — bunun yerine \
`> ⚠️ Bu nokta slaytta var ama verilen kitap alıntılarında karşılığı bulunamadı.` \
satırını yaz ve devam et.

BİÇİM
- Markdown. Bölüm başlığı `##`, alt başlıklar `###`.
- Matematik LaTeX: satır içi `$...$`, blok `$$...$$`. Unicode alt/üst simge KULLANMA \
(₀¹²) — `$x_1$`, `$2^{w-1}$` yaz.
- Kod bloklarında dil etiketi kullan (```c, ```asm).
- Bir slaytta önemli bir şema/tablo varsa ve onu metinle tam anlatamıyorsan \
`[ŞEKİL: slayt <no>]` satırı bırak — sistem oraya orijinal görseli yerleştirecek.
- Sana "Kullanılabilir kitap şekilleri" listesi verildiyse, anlatımı gerçekten \
güçlendirecek olanı `[KŞEKİL: <numara>]` satırıyla çağır — sistem kitaptaki \
diyagramı oraya kırpıp koyacak. YALNIZCA listede olan numarayı yaz; listede \
olmayan bir şekli uydurma. Her şekli çağırma, sadece gerekeni.

ÜSLUP
- Doğrudan anlat. "Bu bölümde göreceğiz ki", "Umarım anlaşılmıştır" gibi dolgu yok.
- Somut sayısal örnekle göster. Soyut kuralı verip geçme; 8-bit veya 16-bit \
somut bir değer üzerinde adım adım çalıştır.
- Öğrencinin takılacağı yeri öngör ve açıkça uyar (ör. işaretli/işaretsiz \
karşılaştırmada örtük dönüşüm).
- Uzunluğu içeriğe göre ayarla: yoğun bir slayt uzun, tek fikirli bir slayt kısa \
açıklama hak eder. Dolgu ile uzatma.
"""

TOPIC_SYSTEM = """\
Slayt bloklarını analiz edip her biri için yapısal bir konu kartı üretiyorsun.
Amaç, ders kitabında arama yapmak için iyi bir sorgu kurmak.

Sadece geçerli JSON döndür, başka hiçbir şey yazma. Şema:
{"title": str, "key_terms": [str], "formulas": [str], "gaps": [str]}

- title: bölümün başlığı, sana bildirilen ÇIKTI DİLİNDE (teknik terimler İngilizce kalabilir)
- key_terms: kitapta aranacak teknik terimler, İNGİLİZCE (kitap İngilizce) — en fazla 12
- formulas: slaytta geçen formül/gösterimler — en fazla 6
- gaps: slaytta değinilip açıklanmayan, kitaptan gelmesi gereken noktalar — en fazla 6
"""

ALIGN_SYSTEM = """\
Bir ders slaytı dizisini ders kitabının içindekiler ağacıyla eşleştiriyorsun.

Sadece geçerli JSON döndür. Şema:
{"alignments": [{"section_index": int, "book_sections": [str], "page_start": int, "page_end": int, "confidence": "low"|"medium"|"high"}]}

- Her ders bölümü için kitapta hangi sayfa aralığının okunması gerektiğini belirt.
- Sayfa numaraları sana verilen içindekiler listesindeki PDF sayfa numaralarıdır.
- Aralığı cömert tut (ilgili alt bölümün tamamı), ama tüm kitabı kapsama.
- Emin değilsen confidence "low" ver ve aralığı geniş tut.
"""


# --- Derinlik ve açıklama biçimi -------------------------------------------
# Bunlar sistem promptuna GİRMEZ: sistem promptu cache önekinin ilk baytı ve
# sabit kalmak zorunda. Kullanıcı seçimleri kırılma noktasından sonraki gövdeye
# yazılır, böylece iki farklı derinlik aynı cache'i paylaşabilir.

DEPTHS: dict[str, str] = {
    "özet": (
        "DERİNLİK: ÖZET. Her slayt maddesini en fazla bir paragrafta karşıla. "
        "Tek bir somut örnek ver, onu da en kritik noktaya sakla. "
        "Türetme ve ispat yazma; sonucu ver ve nereden geldiğini tek cümleyle söyle."
    ),
    "standart": "",  # sistem promptundaki varsayılan davranış
    "derin": (
        "DERİNLİK: DERİN. Her formülü adım adım türet, ara adımı atlama. "
        "Kenar durumlarını tek tek göster: taşma, işaret uzatma, sıfır, "
        "temsil edilebilir en negatif değer. Kitaptaki ilgili alıştırmayı "
        "çözülmüş örnek olarak işle."
    ),
}

EXTRAS: dict[str, str] = {
    "analoji": (
        "Zor kavramları günlük hayattan bir analojiyle destekle. Her analojiyi "
        "şu blokla ver:\n"
        "::: analoji\nAnaloji metni.\n\n**Nerede bozulur:** ...\n:::\n"
        "Analojinin bozulduğu yeri MUTLAKA yaz — sınırı söylenmeyen analoji "
        "öğrenciye yanlış model kurdurur."
    ),
    "örnek": (
        "Her ana kavram için slayttakinden FARKLI, ek bir sayısal örnek çalıştır. "
        "Örneği baştan sona adım adım götür; sonucu verip geçme."
    ),
    "soru": (
        "Bölümün sonuna öğrencinin kendini sınayacağı 3-5 soru ekle. "
        "Sorular hatırlatma değil uygulama olsun (hesapla, dönüştür, karşılaştır). "
        "Şu blokla ver:\n"
        "::: soru\n1. Soru metni\n2. Soru metni\n\n**Yanıtlar:** 1) ... 2) ...\n:::"
    ),
    "sözlük": (
        "Bölümün sonuna geçen teknik terimlerin tablosunu ekle. Şu blokla ver:\n"
        "::: sözlük\n| Terim | İngilizce | Anlamı |\n|---|---|---|\n"
        "| ... | ... | ... |\n:::\n"
        "İngilizce sütunu kitapta ve sınavda geçen terimi tutar; öğrenci "
        "kaynağa döndüğünde eşleştirebilmeli."
    ),
}


# Arayüzde gösterilen kısa açıklamalar. Direktiflerin HEMEN YANINDA duruyorlar
# ve arayüz bunları `/api/health` üzerinden okuyor — açıklama ikinci bir yerde
# yazılı olsaydı direktif değişince sessizce yalan söylemeye başlardı.
DEPTH_HELP: dict[str, str] = {
    "özet": "Her maddeye en fazla bir paragraf. Türetme ve ispat yok: sonuç "
            "verilir, nereden geldiği tek cümleyle söylenir. Tekrar için.",
    "standart": "Varsayılan. Slayttaki her maddeyi somut sayısal bir örnek "
                "üzerinden açar, takılacağın yeri önceden uyarır.",
    "derin": "Her formülü adım adım türetir, ara adımı atlamaz. Kenar "
             "durumlarını tek tek gösterir: taşma, işaret uzatma, sıfır, en "
             "negatif değer. Kitaptaki alıştırmayı çözülmüş örnek olarak işler.",
}

EXTRA_HELP: dict[str, str] = {
    "analoji": "Zor kavramı günlük hayattan bir benzetmeyle açar ve "
               "benzetmenin NEREDE BOZULDUĞUNU da yazar — sınırı söylenmeyen "
               "analoji öğrenciye yanlış model kurdurur.",
    "örnek": "Her ana kavram için slayttakinden FARKLI, ek bir sayısal örneği "
             "baştan sona adım adım çalıştırır.",
    "soru": "Bölüm sonuna 3-5 soru ve yanıtlarını ekler. Hatırlatma değil "
            "uygulama: hesapla, dönüştür, karşılaştır.",
    "sözlük": "Bölümde geçen terimlerin Türkçe/İngilizce/anlam tablosunu ekler. "
              "İngilizce sütunu kitapta ve sınavda geçen terimi tutar.",
}


def build_output_directives(language: str, depth: str, extras: list[str]) -> list[str]:
    """Kullanıcının seçtiği dil/derinlik/biçim direktiflerini satırlara çevirir."""
    lines: list[str] = []
    if depth_text := DEPTHS.get(depth, ""):
        lines.append(depth_text)
    for key in extras:
        if text := EXTRAS.get(key):
            lines.append(text)
    lines.append(
        f"Bu bölümü {language} dilinde yaz. Açıklama metninin tamamı {language} "
        "olmalı. İki istisna: teknik terimler ilk geçtiklerinde parantez içinde "
        "İngilizcesiyle verilir (kitap ve sınav İngilizce), ve `[K: ...]` / "
        "`[S: ...]` / `[ŞEKİL: ...]` işaretçileri harfi harfine bu biçimde kalır — "
        "bunlar sistem tarafından ayrıştırılıyor, çevrilirse kaybolur."
    )
    return lines


# Geçmiş sınav kâğıdı verildiğinde eklenen kural. Sınav metni önekte taşınır
# (bölümler arasında değişmez), bu direktif de oraya girer.
#
# Buradaki disiplin projenin geri kalanıyla aynı: model "bu konu 2023'te
# soruldu" diye SERBESTÇE iddia edemez, soruyu birebir alıntılamak zorunda.
# Alıntılayamıyorsa iddia da yok. Atıf kuralının sınav kâğıdına uyarlanmışı.
EXAM_RULE = """\
GEÇMİŞ SINAV KÂĞIDI
- Sana bu dersin geçmiş sınav sorularının metni verildi. Bunu KAPSAM \
GENİŞLETMEK için kullanma — kapsamı hâlâ slaytlar belirler.
- Kullanımı şudur: slayttaki bir konu geçmiş sınavda SORULMUŞSA, o konuyu \
daha derin işle ve öğrenciyi soru tipine hazırla.
- Böyle bir konuyu şu blokla işaretle:
::: sınav
**Sorulmuş:** soruyu birebir alıntıla.

Çözüm yolu / nelere dikkat edilmeli.
:::
- Soruyu birebir alıntılayamıyorsan bu bloğu HİÇ yazma. "Bu konu sınavda \
çıkar" gibi dayanaksız bir iddia, kaynaksız bir cümle yazmakla aynı şeydir.
- Sınav kâğıdında olup slaytta olmayan konuyu ana başlık yapma; en fazla \
bölümün sonunda tek satırla "sınavda geçmiş ama slaytta yok" diye not düş.
"""


def build_lecture_context(
    lecture, alignment_note: str = "", exam_text: str = ""
) -> str:
    """Tüm çağrılarda AYNI kalan ders bağlamı — cache'lenen kısım.

    Sınav metni de buraya giriyor: bölümden bölüme değişmediği için önekte
    durması doğru yer, bir kez yazılıp her bölümde ucuza okunur.
    """
    lines = [
        f"# DERS: {lecture.title}",
        f"Toplam {len(lecture.slides)} slayt, {len(lecture.sections)} bölüm.",
        "",
        "## Ders içeriğinin tamamı (bağlam için)",
        "",
    ]
    for s in lecture.slides:
        marker = " [AJANDA]" if s.is_divider else ""
        lines.append(f"### Slayt {s.number}: {s.title}{marker}")
        if s.text:
            lines.append(s.text)
        lines.append("")
    if alignment_note:
        lines += ["## Kitap eşlemesi", alignment_note, ""]
    if exam_text:
        lines += ["## GEÇMİŞ SINAV SORULARI", exam_text, "", EXAM_RULE, ""]
    return "\n".join(lines)


def build_section_request(
    section,
    topic,
    chunks,
    language: str,
    *,
    depth: str = "standart",
    extras: list[str] | None = None,
    figures: list | None = None,
) -> str:
    """Bölüme özel, cache kırılma noktasından SONRA gelen değişken kısım."""
    a, b = section.slide_range
    parts = [
        "---",
        f"# ŞİMDİ YAZILACAK BÖLÜM: {topic.title if topic else ''}",
        f"Slaytlar {a}-{b}.",
        "",
        "## Bu bölümün slaytları",
        section.raw_text,
        "",
    ]
    if topic and topic.gaps:
        parts += [
            "## Slaytta eksik olup açıklanması gereken noktalar",
            "\n".join(f"- {g}" for g in topic.gaps),
            "",
        ]

    if chunks:
        parts += ["## KİTAP ALINTILARI (atıf için kaynak)", ""]
        for c in chunks:
            parts += [f"### [K: {c.citation}]", c.text, ""]
    else:
        parts += [
            "## KİTAP ALINTILARI",
            "(Bu bölüm için kitapta eşleşen parça bulunamadı. "
            "Yalnızca slayta dayan ve eksik kalan yerleri açıkça işaretle.)",
            "",
        ]

    if figures:
        parts += [
            "## Kullanılabilir kitap şekilleri",
            "(Gerekirse `[KŞEKİL: <numara>]` satırıyla çağır. Görüntüleri sana "
            "gönderilmiyor — sistem çağırdığın numarayı kitaptan kırpıp koyacak.)",
            "",
        ]
        for f in figures:
            desc = f.caption or "(altyazı çıkarılamadı)"
            parts.append(f"- `{f.number}` — {desc} (s. {f.page})")
        parts.append("")

    visual = [s.number for s in section.slides if s.is_visual]
    if visual:
        parts.append(
            f"Not: {', '.join(str(n) for n in visual)} numaralı slaytların görüntüleri "
            "yukarıda verildi. Şemalardaki bilgiyi metne dök."
        )

    parts += ["", "## Bu çıktı için ek talimatlar", ""]
    parts += build_output_directives(language, depth, extras or [])
    parts += [
        "",
        "Yukarıdaki kurallara uyarak yaz. `## ` başlığıyla başla. "
        "Sadece bu bölümü yaz, sonraki bölümlere geçme.",
    ]
    return "\n".join(parts)
