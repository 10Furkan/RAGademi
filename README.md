# dersnotu

Ders slaytları anlaşılmıyor, kitap ise bin sayfa. Bu araç ikisini birleştirip
derse gelmeyen birinin tek başına öğrenebileceği bir ders notu PDF'i üretiyor.

Slaytın **kapsamını** korur (sınavdan sorumlu olduğun şey odur), açıklamayı
**kitaptan** çeker ve her iddiaya kaynak koyar.

## Ne yapıyor

Bir ders PDF'i ve bir kitap PDF'i yüklüyorsun; çıktı olarak şunları içeren bir
PDF alıyorsun:

- Slayttaki her maddenin genişletilmiş anlatımı
- Kitaptan gelen her bilgiye `[K: bölüm, s. sayfa]` atfı, kenar rayında
- Slaytın kendi şemaları, gerektiği yere gömülü
- **Kitabın diyagramları**, sayfadan kırpılıp yerleştirilmiş
- LaTeX matematik (KaTeX ile), sözdizimi renklendirilmiş kod
- İstenirse: analoji, ek örnek, kendini sınama soruları, terim sözlüğü
- İstenen dilde

Kaynaklarda karşılığı olmayan bir noktayı uydurmaz — açıkça işaretler.

## Kurulum

Windows PowerShell, venv repo kökünde:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts\vendor_katex.py   # KaTeX'i gömer (tek sefer)
```

## Çalıştırma

```powershell
.\.venv\Scripts\python.exe -m dersnotu.cli serve
```

Sonra <http://127.0.0.1:8000>.

Ana sayfada derslerin durur. Bir ders açtığında o derse ait her şey bir arada:
yüklediğin slaytlar, kitaplar, geçmiş sınavlar ve o güne kadar ürettiğin ders
notları. Dosyaları bir kez yükle, her üretimde listeden seç.

Kitaplar içerik adresli saklanır: aynı kitabı iki derse eklemek diskte ikinci
bir kopya açmaz ve kitabı ikinci kez indekslemez.

**Üret'e basmadan ne ödeyeceğini görürsün** — bölüm sayısı, token, ücret ve
süre. Süre tahmini kendi geçmiş koşularından kalibre olur; abonelik yolunda
fiyat yerine kalan kota gösterilir.

**Ders notunu indirmeden okuyabilirsin.** Okuyucu PDF'le birebir aynı render:
kenar rayında atıflar, kitaptan kırpılmış şekiller, KaTeX matematik. Üstteki
arama kutusu dersin **tüm** notlarında arar ve seni doğrudan ilgili bölüme
götürür.

**Geçmiş sınav kâğıdı yükleyebilirsin.** Kapsamı değiştirmez — sınavdan
sorumlu olduğun şey hâlâ slaytlardır. Yaptığı, slaytta zaten olan bir konu
geçmiş sınavda çıkmışsa onu daha derin işlemek ve soruyu birebir alıntılayarak
işaretlemek. Alıntılayamıyorsa iddia da etmez.

### Kimlik: API anahtarı veya Claude Pro

| Arka uç | Kimlik | Ücret |
|---|---|---|
| `api` | `ANTHROPIC_API_KEY` | token başına (~$0.76 / ders) |
| `cli` | Claude Pro/Max oturumu (`claude` komutu) | token ücreti yok, abonelik kotası |
| `demo` | gerekmez | ücretsiz, sahte istemci |

`auto` sırayla dener: api → cli → demo. Yani hiçbir şey ayarlamadan da açılır.

Claude Pro yolu belirgin şekilde yavaştır (bölüm başına ~3 dk, API'de birkaç
saniye), çünkü her çağrı ayrı bir alt süreçtir.

## Komutlar

Yalnızca `build` ve `retry` bir modele çağrı yapar; kalan her şey çevrimdışıdır.

```powershell
dersnotu inspect  <ders.pdf>                     # slayt → bölüm ayrıştırması
dersnotu index    <kitap.pdf>                    # FTS indeksi + şekil taraması
dersnotu search   <kitap.pdf> "sorgu" --show     # retrieval kalitesini sına
dersnotu estimate <ders.pdf> <kitap.pdf>         # token/maliyet tahmini
dersnotu preview  <ders.pdf> <kitap.pdf> -s 1    # gidecek istek gövdesini dök
dersnotu render                                  # örnek dokümanı PDF'e bas
dersnotu build    <ders.pdf> <kitap.pdf> --sections 1 --depth derin -e soru
dersnotu retry    <doc.json> <ders.pdf> <kitap.pdf>   # sadece hatalı bölümler
```

## Nasıl çalışıyor

```
ders ayrıştır ─┐
               ├─ konu kartları (1 ucuz çağrı, tüm bölümler)
kitap indeksi ─┤
               ├─ TOC hizalama (1 çağrı) → bölüm başına sayfa aralığı
               └─ bölüm başına: retrieval → genişletme (asıl maliyet)
                                               ↓
                                         Markdown → HTML → PDF
```

Tasarımı belirleyen asimetri: **ders küçük ve görsel, kitap devasa ve metinsel.**
Ders modele neredeyse bütün gider (tam metin + şema slaytlarının PNG'leri).
Kitap **asla bütün gitmez** — yerel olarak SQLite FTS5 ile indekslenir, modele
yalnızca getirilen alıntılar ulaşır.

Kitap indeksi içerik adreslidir (`.cache/book-<sha>.sqlite`): aynı kitap bir kez
indekslenir, sonraki tüm dersler hazır indeksi kullanır.

Bir bölüm patlarsa doküman ölmez — o bölüm ⚠️ ile işaretlenir, kalanlar devam
eder, sonra yalnızca eksikler yeniden üretilir. Bu, sunucu kapansa bile
geçerli: ders sayfasındaki eksik ders notunun yanında "tamamla" düğmesi durur.

Dersler, materyaller ve üretilmiş dokümanlar `.cache/library.sqlite` içinde;
`.cache` altında olsa da silinebilir bir önbellek değil, ders listen orada.

## Geliştirme

```powershell
.\.venv\Scripts\python.exe -m pytest -q                # ~145 test, ağ gerekmez
.\.venv\Scripts\python.exe -m pytest -m "not slow" -q  # Chromium/PDF'siz
.\.venv\Scripts\python.exe -m ruff check --fix src/ tests/ scripts/
```

Mimari kararlar ve ölçülerek bulunmuş tuzaklar `CLAUDE.md` dosyasında.

## Notlar

Ders ve kitap PDF'leri depoya dahil değildir (telifli materyal). Testler
bunlara ihtiyaç duymaz; varsa çalışır, yoksa atlanır.

Yorumlar, arayüz ve promptlar Türkçe; kod tanımlayıcıları İngilizce.
