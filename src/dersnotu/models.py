"""Boru hattı boyunca taşınan alan modelleri.

Her aşama bir öncekinin çıktısını girdi alır; hepsi JSON'a serileştirilebilir
olduğu için ara çıktılar diske yazılıp yeniden kullanılabilir (idempotency).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Slide(BaseModel):
    """Ders PDF'inin tek bir sayfası."""

    number: int  # 1 tabanlı
    title: str
    text: str
    is_divider: bool = False  # "Today:" / ajanda / bölüm ayırıcı slaytı
    is_visual: bool = False  # şema/tablo içerir, görüntü olarak gönderilmeli
    shape_count: int = 0
    image_count: int = 0

    @property
    def label(self) -> str:
        return f"Slide {self.number}: {self.title}" if self.title else f"Slide {self.number}"


class LectureSection(BaseModel):
    """Ajanda slaytlarıyla sınırlanmış slayt bloğu — genişletmenin birimi."""

    index: int  # 0 tabanlı
    slides: list[Slide]
    agenda_context: str = ""  # bölümü açan ajanda slaytının metni

    @property
    def slide_range(self) -> tuple[int, int]:
        return self.slides[0].number, self.slides[-1].number

    @property
    def raw_text(self) -> str:
        return "\n\n".join(f"--- {s.label} ---\n{s.text}" for s in self.slides)

    @property
    def titles(self) -> list[str]:
        return [s.title for s in self.slides if s.title]


class Lecture(BaseModel):
    source_path: str
    source_sha256: str
    title: str
    slides: list[Slide]
    sections: list[LectureSection]


class BookSection(BaseModel):
    """Kitabın içindekiler ağacından gelen bir düğüm."""

    title: str
    level: int
    page_start: int  # 1 tabanlı
    page_end: int


class BookChunk(BaseModel):
    chunk_id: str
    text: str
    section_title: str
    page_start: int
    page_end: int
    token_estimate: int

    @property
    def citation(self) -> str:
        pages = (
            f"p. {self.page_start}"
            if self.page_start == self.page_end
            else f"p. {self.page_start}-{self.page_end}"
        )
        return f"{self.section_title}, {pages}" if self.section_title else pages


class BookFigure(BaseModel):
    """Kitapta bulunmuş, kırpılabilir bir şekil.

    `bbox` CropBox-GÖRELİ puntodur (x0, top, x1, bottom) — MediaBox değil.
    Dönüşüm tespit anında bir kez yapılır; ayrıntı için `pdfio/figures.py`.
    """

    number: str  # "6.5"
    caption: str = ""
    page: int  # 1 tabanlı PDF sayfası
    bbox: tuple[float, float, float, float]

    @property
    def label(self) -> str:
        return f"Figure {self.number}"

    @property
    def area(self) -> float:
        return (self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1])


class Book(BaseModel):
    source_path: str
    source_sha256: str
    title: str
    page_count: int
    sections: list[BookSection]


class TopicCard(BaseModel):
    """Bir bölümün ucuz modelle çıkarılmış özeti — retrieval sorgusunu besler."""

    section_index: int
    title: str
    key_terms: list[str] = Field(default_factory=list)
    formulas: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)  # slaytta eksik, kitaptan gelmeli

    @property
    def query(self) -> str:
        return " ".join([self.title, *self.key_terms, *self.gaps])


class SectionAlignment(BaseModel):
    """Bir bölümün kitaptaki karşılığı (sayfa aralığı filtresi)."""

    section_index: int
    book_sections: list[str] = Field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    confidence: str = "medium"  # low | medium | high


class ExpandedSection(BaseModel):
    section_index: int
    title: str
    markdown: str
    citations: list[str] = Field(default_factory=list)
    slide_range: tuple[int, int]
    error: str | None = None


class Usage(BaseModel):
    """Token/maliyet muhasebesi. Her LLM çağrısından sonra toplanır."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.calls += other.calls


class PracticeQuestion(BaseModel):
    """Geçmiş sınava BAKILARAK üretilmiş yeni bir soru.

    `modeled_on` bu modelin can damarı: örnek alınan geçmiş soru BİREBİR
    buraya yazılır. Boşsa "bu tarz sınavda çıkmıştı" iddiası da yoktur —
    kitap atıfı kuralının sınav kâğıdına uygulanmış hâli. Render katmanı
    boş alanı sessizce atlar, uydurma bir gerekçe basmaz.
    """

    number: int
    kind: str = ""  # multiple choice | calculation | true/false | open-ended | code reading
    points: int = 0
    topic: str = ""
    # Sorunun dayandığı slaytlar — kapsam kanıtı. Boş liste, soruyu kapsam
    # dışı yapmaz ama gözden geçirilmesi gerektiğini söyler.
    slides: list[int] = Field(default_factory=list)
    prompt: str = ""
    choices: list[str] = Field(default_factory=list)  # çoktan seçmeli değilse boş
    answer: str = ""
    solution: str = ""
    citations: list[str] = Field(default_factory=list)
    modeled_on: str = ""  # geçmiş sınavdan birebir alıntı

    @property
    def has_choices(self) -> bool:
        return len(self.choices) >= 2


class PracticeExam(BaseModel):
    """Üretilmiş deneme sınavı.

    Ders notundan farklı olarak tek parça: bir bölüm patlayınca kalanı
    kurtarılabilen bir yapı değil, tek çağrının ürünü. O yüzden `failed`
    benzeri bir alan yok — çağrı ya bir kâğıt üretir ya da iş hata verir.
    """

    lecture_title: str
    language: str
    source_exam: str = ""  # örnek alınan sınav kâğıdının dosya adı
    # Modelin geçmiş kâğıtta GÖZLEMLEDİĞİ biçim, kendi cümleleriyle. Kapakta
    # basılıyor: neye benzetildiğini görmeden "benzer" iddiası denetlenemez.
    profile: str = ""
    duration_minutes: int = 0
    questions: list[PracticeQuestion] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)

    @property
    def total_points(self) -> int:
        return sum(q.points for q in self.questions)

    @property
    def grounded(self) -> list[PracticeQuestion]:
        """Geçmiş bir soruyu birebir alıntılayabilen sorular."""
        return [q for q in self.questions if q.modeled_on.strip()]


class StudyDocument(BaseModel):
    """Boru hattının nihai çıktısı."""

    lecture_title: str
    language: str
    sections: list[ExpandedSection]
    usage: Usage = Field(default_factory=Usage)
    # Metinde `[KŞEKİL: N.M]` ile atıf yapılan kitap şekilleri. Render katmanı
    # işaretçiyi bunlarla eşleştirip gerçek kırpımı yerleştirir.
    figures: list[BookFigure] = Field(default_factory=list)

    # --- Yeniden deneme için taşınan durum -----------------------------
    # Bir bölümü tek başına yeniden üretmek konu kartını ve kitap hizalamasını
    # gerektiriyor. Saklanmasalardı her yeniden denemede iki model çağrısı daha
    # yapılırdı; ayrıca ikisi de deterministik değil, yani yeniden hesaplamak
    # başarılı bölümlerle tutarsız bir kart üretebilirdi.
    cards: list[TopicCard] = Field(default_factory=list)
    alignments: list[SectionAlignment] = Field(default_factory=list)
    depth: str = "standard"
    extras: list[str] = Field(default_factory=list)

    @property
    def failed(self) -> list[ExpandedSection]:
        return [s for s in self.sections if s.error]
