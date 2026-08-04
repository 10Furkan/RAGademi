"""Çalışma zamanı ayarları. Ortam değişkenleri veya .env dosyasından okunur."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="DERSNOTU_", extra="ignore"
    )

    # --- Modeller -------------------------------------------------------
    # Genişletme (asıl maliyet kalemi). Sonnet 5 varsayılan; Opus 5 premium.
    model: str = "claude-sonnet-5"
    # Konu kartı / sınıflandırma gibi ucuz geçişler.
    cheap_model: str = "claude-haiku-4-5"
    # low | medium | high | xhigh | max
    effort: str = "high"
    max_tokens: int = 16000

    # --- Dizinler -------------------------------------------------------
    cache_dir: Path = Path(".cache")
    out_dir: Path = Path("out")

    # --- Retrieval ------------------------------------------------------
    chunk_target_tokens: int = 900
    chunk_overlap_ratio: float = 0.15
    chunks_per_section: int = 6
    # Kitaptaki diyagramları çıkarıp çıktıya yerleştir. İlk indeksleme sırasında
    # ~95 sn ekler (1105 sayfa), sonrasında önbellekten gelir.
    extract_book_figures: bool = True
    # Bir bölümde modele önerilecek azami kitap şekli — liste uzarsa model
    # ilgisiz şekil çağırmaya başlıyor.
    figures_per_section: int = 6

    # --- Ders PDF'i -----------------------------------------------------
    # Bir slaytı "görsel" saymak için gereken vektör nesne sayısı eşiği.
    # Slayt şablonunun kendi çerçevesi ~6 nesne üretir; gerçek şemalar 40+.
    visual_shape_threshold: int = 20
    # Tek genişletme çağrısına giren azami slayt sayısı.
    max_section_slides: int = 8
    # Claude'a kaç slayt görüntüsü gönderilebileceğinin üst sınırı (maliyet freni).
    max_slide_images: int = 30
    # Slayt görüntüsünün uzun kenarı (piksel). Token maliyeti ALANLA orantılı:
    # kenarı yarıya indirmek görüntü maliyetini dörtte bire düşürür.
    slide_image_max_edge: int = 1400

    def ensure_dirs(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
