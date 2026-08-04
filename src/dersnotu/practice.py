"""Deneme sınavı üretimi — geçmiş sınav kâğıdına benzer yeni sorular.

Geçmiş sınav kâğıdının ikinci kullanımı. Ders notunda kâğıt derinliği
kaydırıyordu (`EXAM_RULE`); burada kâğıdın kendisi ŞABLON: soru tipi, uzunluk,
puanlama ve zorluk oradan alınır, sorular yeniden kurulur.

Akış:

    ders parse ──┐
                 ├─ konu kartları (1 ucuz çağrı) → retrieval sorgusu
    kitap index ─┤
    sınav metni ─┴─ TEK çağrı → sorular + çözümler (yapısal JSON)

**Neden bölüm bölüm değil de tek çağrı?** Ders notunda bölüm doğal birimdi:
her bölüm ayrı bir metin ve biri patlarsa kalanı kurtulur. Sınav kâğıdı öyle
değil — bütün olmak zorunda. Bölüm başına çağrı yapmak aynı konudan iki kez
soru sormaya, puanların toplamının tutmamasına ve zorluk dağılımının
kaybolmasına yol açardı. Kâğıdın dengesi ancak tüm sorular aynı anda
görülürken kurulabilir. Yan faydası: sekiz çağrı yerine bir çağrı.

**TOC hizalaması atlanıyor.** Ders notunda hizalama, retrieval'ı doğru sayfa
aralığına kilitlemek için bir model çağrısına değiyordu. Burada bölüm başına
yalnızca birkaç alıntı isteniyor ve sorular zaten slaytın kapsamıyla sınırlı;
ikinci bir çağrının bedeli kazandırdığı kesinliği aşıyor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .index import BookIndex
from .llm import RefusalError, make_client, resolve_backend
from .llm.prompts import PRACTICE_SCHEMA, PRACTICE_SYSTEM, build_practice_request
from .models import BookChunk, PracticeExam, PracticeQuestion, SectionAlignment
from .pipeline import (
    Progress,
    _noop,
    build_topic_cards,
    load_book_index,
    load_exam_text,
    load_lecture,
    retrieve,
)

# Tek çağrıya tüm dersin alıntıları giriyor. Ders notunda bölüm başına 6 alıntı
# makul; burada aynı sayı 8 bölümde 48 alıntı ≈ 35K token eder ve sorunun
# kendisi o kadar bağlam istemiyor — soru kurmak için kavramın tanımı yeterli,
# ders notu yazmak için değil.
CHUNKS_PER_SECTION = 2
# Çok bölümlü bir derste bile istek şişmesin.
MAX_CHUNKS = 24


class PracticeError(RuntimeError):
    """Deneme sınavı üretilemedi. İş bu mesajla düşer."""


@dataclass
class PracticeInputs:
    lecture_path: Path
    book_path: Path
    exam_path: Path
    # Kâğıdın kullanıcıya gösterilecek adı. Web akışında `exam_path` içerik
    # adresli depoyu gösteriyor (`7667c6af….pdf`); kapakta o SHA'yı basmak
    # kullanıcıya hangi kâğıttan üretildiğini söylemez. Boşsa dosya adına
    # düşülür — CLI'da zaten gerçek ad odur.
    exam_name: str = ""
    language: str = "Türkçe"
    # 0 = "geçmiş kâğıtta kaç soru varsa o kadar". Sabit bir sayı dayatmak
    # kâğıdın biçimini taklit etme işine ters düşer.
    count: int = 0
    backend: str = "auto"


def collect_chunks(
    idx: BookIndex, cards, *, per_section: int = CHUNKS_PER_SECTION, cap: int = MAX_CHUNKS
) -> list[BookChunk]:
    """Her bölümden birkaç alıntı topla, tekrarları at.

    Aynı chunk iki bölümün sorgusuna birden düşebilir; `chunk_id` üzerinden
    teklenmezse modele aynı metin iki kez gider ve yer kaplar.
    """
    secilen: dict[str, BookChunk] = {}
    for card in cards:
        for c in retrieve(
            idx, card, SectionAlignment(section_index=card.section_index),
            limit=per_section,
        ):
            secilen.setdefault(c.chunk_id, c)
        if len(secilen) >= cap:
            break
    return list(secilen.values())[:cap]


def _to_questions(data: dict) -> list[PracticeQuestion]:
    """Ham JSON'u modele çevirir; bozuk soruyu atar, kâğıdı düşürmez.

    Tek soru şemadan sapmışsa (ör. `slides` alanına dizge gelmişse) tüm
    sınavı çöpe atmak yerine o soru atlanır — kalan 11 soru hâlâ işe yarar.
    """
    out: list[PracticeQuestion] = []
    for ham in data.get("questions", []):
        try:
            q = PracticeQuestion(**ham)
        except Exception:
            continue
        if q.prompt.strip():
            out.append(q)
    # Model numaralandırmayı atlarsa veya tekrarlarsa kâğıt bozuk görünür.
    for i, q in enumerate(out, start=1):
        q.number = i
    return out


def generate(
    inputs: PracticeInputs,
    settings: Settings,
    *,
    progress: Progress = _noop,
    llm: Any | None = None,
) -> PracticeExam:
    """Uçtan uca deneme sınavı üretir."""
    settings.ensure_dirs()

    exam_text = load_exam_text(inputs.exam_path)
    if not exam_text.strip():
        # Şablon yoksa "benzer soru" diye bir şey de yok. Sessizce kapsamdan
        # soru üretmek kullanıcının istediği şey değil.
        raise PracticeError(
            "Sınav kâğıdının metni okunamadı. Taranmış (görüntü) bir PDF olabilir; "
            "metin katmanı olan bir kopya gerekiyor."
        )
    progress("exam:loaded", f"{len(exam_text):,} karakter sınav metni")

    lecture = load_lecture(_pipeline_inputs(inputs), settings, progress)
    _, idx = load_book_index(_pipeline_inputs(inputs), settings, progress)

    if llm is None:
        chosen = resolve_backend(inputs.backend)
        progress("backend", chosen)
        llm = make_client(chosen, settings)

    cards = build_topic_cards(llm, lecture, progress, language=inputs.language)
    chunks = collect_chunks(idx, cards)
    idx.close()
    progress("retrieve:done", f"{len(chunks)} kitap alıntısı")

    progress(
        "questions:start",
        f"{inputs.count or 'kâğıttaki kadar'} soru, {len(lecture.slides)} slayt kapsamı",
    )
    payload = build_practice_request(
        lecture, exam_text, chunks, inputs.language, count=inputs.count
    )
    try:
        data = llm.complete_json(
            system=PRACTICE_SYSTEM,
            content=[{"type": "text", "text": payload}],
            schema=PRACTICE_SCHEMA,
            # Soru kurmak ucuz modelin işi değil: ders notuyla aynı modeli
            # kullan. `complete_json` aksi belirtilmezse cheap_model'e düşer.
            model=settings.model,
        )
    except RefusalError as exc:
        raise PracticeError(f"Model isteği reddetti: {exc.category}") from exc
    except json.JSONDecodeError as exc:
        raise PracticeError(
            "Model geçerli bir soru kâğıdı döndürmedi (JSON ayrıştırılamadı)."
        ) from exc

    questions = _to_questions(data if isinstance(data, dict) else {})
    if not questions:
        raise PracticeError("Model hiç soru üretmedi; sınav kâğıdı boş kaldı.")

    dayanakli = sum(1 for q in questions if q.modeled_on.strip())
    progress(
        "questions:done",
        f"{len(questions)} soru · {dayanakli} tanesi geçmiş bir soruya dayanıyor",
    )
    return PracticeExam(
        lecture_title=lecture.title,
        language=inputs.language,
        source_exam=inputs.exam_name or Path(inputs.exam_path).name,
        profile=str(data.get("profile", "")).strip(),
        duration_minutes=int(data.get("duration_minutes") or 0),
        questions=questions,
        usage=llm.usage,
    )


def _pipeline_inputs(inputs: PracticeInputs):
    """`load_lecture` / `load_book_index` boru hattının Inputs'unu bekliyor.

    İkisini tek bir tipte birleştirmek yerine burada çeviriyoruz: ders notunun
    Inputs'una derinlik/ek anlatım alanları ait, deneme sınavına değil.
    """
    from .pipeline import Inputs

    return Inputs(
        lecture_path=Path(inputs.lecture_path),
        book_path=Path(inputs.book_path),
        language=inputs.language,
        backend=inputs.backend,
        exam_path=Path(inputs.exam_path),
    )


# ---------------------------------------------------------------------------
# Markdown çıktısı
# ---------------------------------------------------------------------------
_HARF = "ABCDEFGH"


def to_markdown(exam: PracticeExam) -> str:
    """Sorular önce, cevap anahtarı sonra.

    Sıra kasıtlı: çözüm sorunun hemen altında olsaydı öğrenci soruyu çözmeden
    yanıtı görürdü ve kâğıt bir deneme olmaktan çıkıp okuma parçasına dönerdi.
    """
    parts = [f"# {exam.lecture_title} — deneme sınavı", ""]
    if exam.profile:
        parts += [f"> {exam.profile}", ""]
    for q in exam.questions:
        parts += [f"## Soru {q.number}" + (f" ({q.points} puan)" if q.points else ""), ""]
        parts += [q.prompt, ""]
        if q.has_choices:
            for i, c in enumerate(q.choices):
                parts.append(f"{_HARF[i] if i < len(_HARF) else i + 1}) {c}")
            parts.append("")

    parts += ["", "# Cevap anahtarı", ""]
    for q in exam.questions:
        parts += [f"## Soru {q.number}", ""]
        if q.answer:
            parts += [f"**Yanıt:** {q.answer}", ""]
        if q.solution:
            parts += [q.solution, ""]
        if q.citations:
            parts += [" ".join(f"[K: {c}]" for c in q.citations), ""]
        if q.modeled_on.strip():
            parts += [
                "::: sınav",
                f"**Örnek alınan soru:** {q.modeled_on.strip()}",
                ":::",
                "",
            ]
    return "\n".join(parts)


def save_debug(exam: PracticeExam, path: Path) -> None:
    path.write_text(
        json.dumps(exam.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
