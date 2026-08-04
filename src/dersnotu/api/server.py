"""FastAPI sunucusu: ders kitaplığı + yükleme → iş → SSE ilerleme → PDF.

İki depo var ve ayrı olmaları kasıtlı: `JobStore` bir koşunun canlı durumunu
bellekte tutar (sunucu ömrüyle sınırlı), `LibraryStore` kullanıcının
kitaplığını diskte tutar (yeniden başlatmayı atlatır). Bir iş bittiğinde
sonucu kitaplığa **yazılır**; böylece sunucu kapansa bile üretilmiş ders notu
ders sayfasında durur ve yeniden denenebilir.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import settings
from ..estimate import project
from ..index import BookIndex
from ..library import KINDS, LibraryStore, Material, NotFound
from ..llm import (
    BACKENDS,
    ClaudeCodeClient,
    backend_status,
    last_rate_limit,
    make_client,
    resolve_backend,
)
from ..llm.client import LLMClient
from ..llm.prompts import DEPTH_HELP, DEPTHS, EXTRA_HELP, EXTRAS
from ..models import StudyDocument
from ..pdfio import sha256_file
from ..pipeline import EXAM_CHAR_LIMIT, Inputs, retry_failed, run, save_debug, to_markdown
from ..render import render_document
from ..render.html import build_nav
from .jobs import Event, Job, JobStatus, JobStore

STATIC = Path(__file__).resolve().parent / "static"
UPLOADS = settings.cache_dir / "uploads"

store = JobStore(max_workers=2)
library = LibraryStore(settings.library_path, settings.materials_dir)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.bind_loop(asyncio.get_running_loop())
    settings.ensure_dirs()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    yield
    store.shutdown()


app = FastAPI(title="dersnotu", lifespan=lifespan)


# ---------------------------------------------------------------------------
# İş yürütücüsü
# ---------------------------------------------------------------------------
def _pick_backend(p: dict, emit) -> Any:
    """Kimlik yolunu seçer ve elde olmayan bir seçim yapıldıysa açıkça söyler."""
    # `demo` onay kutusu arka uç seçimini geçersiz kılar (geriye dönük uyum).
    backend = "demo" if p.get("demo") else resolve_backend(p.get("backend", "auto"))
    if backend == "api" and not LLMClient.credentials_available():
        raise RuntimeError(
            "API anahtarı yok. ANTHROPIC_API_KEY ayarla, Claude Pro aboneliği "
            "için 'Claude Pro' seçeneğini kullan, ya da demo modunu işaretle."
        )
    if backend == "cli" and not ClaudeCodeClient.available():
        raise RuntimeError(
            "`claude` komutu bulunamadı. Claude Code kurulu değil; "
            "API anahtarı veya demo modunu kullan."
        )
    emit(Event("backend", {"detail": backend}))
    return make_client(backend, settings)


def _inputs_for(job: Job) -> Inputs:
    p = job.params
    sinav = p["paths"].get("exam")
    return Inputs(
        lecture_path=Path(p["paths"]["lecture"]),
        book_path=Path(p["paths"]["book"]),
        language=p["language"],
        depth=p.get("depth", "standart"),
        extras=p.get("extras", []),
        backend=p.get("backend", "auto"),
        exam_path=Path(sinav) if sinav else None,
    )


def _publish(job: Job, doc: StudyDocument, emit) -> None:
    """Dokümanı diske yazar: markdown, PDF ve yeniden deneme için JSON.

    Dosya adı iş kimliğinden değil `out_stem`'den geliyor. Yeniden deneme yeni
    bir iş açar ama **aynı dokümanı** üretir; iş kimliği kullanılsaydı her
    denemede ders sayfasına yeni bir satır ve diskte yeni bir PDF birikirdi.
    """
    job.usage = doc.usage.model_dump()
    job.failed_sections = [s.section_index for s in doc.failed]

    stem = job.params.get("out_stem", job.id)
    out_base = settings.out_dir / stem
    md_path = out_base.with_suffix(".md")
    md_path.write_text(to_markdown(doc), encoding="utf-8")
    job.md_path = md_path

    # Yeniden deneme bu dosyadan besleniyor: konu kartları ve hizalama burada.
    doc_path = out_base.with_suffix(".doc.json")
    save_debug(doc, doc_path)
    job.doc_path = doc_path

    emit(Event("render:start"))
    # Okuyucu bu HTML'i sunuyor; tek render iki tüketici besliyor. Çubuk
    # belgenin içinde ama `@media print` ile PDF'te gizli.
    nav = ""
    if cid := job.params.get("course_id"):
        nav = build_nav(f"/ders/{cid}", "Derse dön", f"/api/documents/{stem}/download")
    html_path = out_base.with_suffix(".html")
    pdf_path = render_document(
        doc,
        out_base.with_suffix(".pdf"),
        lecture_pdf=job.params["paths"]["lecture"],
        book_pdf=job.params["paths"]["book"],
        nav_html=nav,
        save_html=html_path,
    )
    job.pdf_path = pdf_path
    job.html_path = html_path
    emit(Event("render:done", {"size": pdf_path.stat().st_size}))

    _record(job, doc)


def _record(job: Job, doc: StudyDocument) -> None:
    """Sonucu kitaplığa yazar. Derse bağlı olmayan işler kitaplığa girmez."""
    p = job.params
    if not p.get("course_id"):
        return
    doc_id = p.get("out_stem", job.id)
    try:
        library.add_document(
            id=doc_id,
            course_id=p["course_id"],
            lecture_id=p.get("lecture_id"),
            book_id=p.get("book_id"),
            exam_id=p.get("exam_id"),
            title=Path(p.get("lecture_name", "Ders notu")).stem,
            pdf_path=job.pdf_path,
            md_path=job.md_path,
            doc_path=job.doc_path,
            html_path=job.html_path,
            language=p.get("language", ""),
            depth=p.get("depth", ""),
            extras=p.get("extras", []),
            backend=p.get("backend", ""),
            failed=job.failed_sections,
            usage=job.usage,
            # Süre tahminini kalibre eder. Demo koşusu da yazılıyor ama
            # `estimate` onu yalnızca demo arka ucu için okuyor.
            duration=job.elapsed,
            sections=len(doc.sections),
            created_at=p.get("document_created_at"),
        )
        library.index_document(
            doc_id,
            p["course_id"],
            [
                (s.section_index, s.title or f"Bölüm {s.section_index + 1}", s.markdown)
                for s in doc.sections
                if not s.error
            ],
        )
    except NotFound:
        pass  # ders bu arada silinmiş; çıktı yine de diskte ve indirilebilir


def _execute(job: Job, emit) -> None:
    def progress(event: str, detail: str = "") -> None:
        emit(Event(event, {"detail": detail}))

    def on_delta(section_index: int, text: str) -> None:
        emit(Event("section:delta", {"index": section_index, "text": text}))

    llm = _pick_backend(job.params, emit)
    doc: StudyDocument = run(
        _inputs_for(job),
        settings,
        progress=progress,
        on_delta=on_delta,
        llm=llm,
    )
    _publish(job, doc, emit)


def _execute_retry(job: Job, emit) -> None:
    """Önceki işin hatalı bölümlerini yeniden üretir."""
    def progress(event: str, detail: str = "") -> None:
        emit(Event(event, {"detail": detail}))

    def on_delta(section_index: int, text: str) -> None:
        emit(Event("section:delta", {"index": section_index, "text": text}))

    src = Path(job.params["source_doc"])
    doc = StudyDocument(**json.loads(src.read_text(encoding="utf-8")))
    llm = _pick_backend(job.params, emit)
    doc = retry_failed(doc, _inputs_for(job), settings, progress=progress,
                       on_delta=on_delta, llm=llm)
    _publish(job, doc, emit)


# ---------------------------------------------------------------------------
# Kitaplık uçları
# ---------------------------------------------------------------------------
class CourseIn(BaseModel):
    name: str
    code: str = ""
    note: str = ""


class CoursePatch(BaseModel):
    name: str | None = None
    code: str | None = None
    note: str | None = None


def _material_dict(m: Material) -> dict:
    d = m.to_dict()
    if m.kind == "book":
        # Kitap zaten indekslenmişse üretim anında 25 sn kazanılıyor; bunu
        # kullanıcı seçim yaparken görmeli.
        d["indexed"] = BookIndex.is_built(settings.cache_dir, m.sha)
    return d


@app.get("/api/courses")
async def list_courses() -> list[dict]:
    return [c.to_dict() for c in library.courses()]


@app.post("/api/courses")
async def create_course(body: CourseIn) -> dict:
    try:
        return library.create_course(body.name, body.code, body.note).to_dict()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/courses/{course_id}")
async def get_course(course_id: str) -> dict:
    course = _course_or_404(course_id)
    mats = library.materials(course_id)
    return {
        **course.to_dict(),
        "lectures": [_material_dict(m) for m in mats if m.kind == "lecture"],
        "books": [_material_dict(m) for m in mats if m.kind == "book"],
        "exams": [_material_dict(m) for m in mats if m.kind == "exam"],
        "documents": [d.to_dict() for d in library.documents(course_id)],
    }


@app.patch("/api/courses/{course_id}")
async def patch_course(course_id: str, body: CoursePatch) -> dict:
    _course_or_404(course_id)
    try:
        return library.rename_course(
            course_id, name=body.name, code=body.code, note=body.note
        ).to_dict()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/courses/{course_id}")
async def delete_course(course_id: str) -> dict:
    _course_or_404(course_id)
    return {"deleted": library.delete_course(course_id)}


@app.post("/api/courses/{course_id}/materials")
async def upload_material(
    course_id: str,
    kind: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    _course_or_404(course_id)
    if kind not in KINDS:
        raise HTTPException(400, f"Geçersiz tür: {kind}. Seçenekler: {', '.join(KINDS)}")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Dosya PDF olmalı.")

    tmp = UPLOADS / f"up-{course_id}-{kind}.pdf"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        mat = library.add_material(
            course_id, kind, file.filename, tmp, sha256_file(tmp),
            pages=_page_count(tmp),
        )
    finally:
        tmp.unlink(missing_ok=True)
    return _material_dict(mat)


@app.delete("/api/materials/{material_id}")
async def delete_material(material_id: str) -> dict:
    try:
        used = library.material_usage(material_id)
        return {"deleted": library.delete_material(material_id), "documents": used}
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/materials/{material_id}/download")
async def download_material(material_id: str) -> FileResponse:
    try:
        mat = library.material(material_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    if not (mat.path and mat.path.exists()):
        raise HTTPException(404, "Dosya diskte yok.")
    return FileResponse(mat.path, filename=mat.name, media_type="application/pdf")


@app.get("/api/documents/{document_id}/download")
async def download_document(document_id: str, fmt: str = "pdf") -> FileResponse:
    try:
        doc = library.document(document_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    path = doc.pdf_path if fmt == "pdf" else doc.md_path
    if not (path and path.exists()):
        raise HTTPException(404, "Çıktı diskte yok.")
    return FileResponse(path, filename=f"{doc.title}-ders-notu.{fmt}")


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str) -> dict:
    try:
        return {"deleted": library.delete_document(document_id)}
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/courses/{course_id}/search")
async def search_course(course_id: str, q: str = "", limit: int = 20) -> dict:
    """Dersin tüm ders notlarında tam metin arama."""
    _course_or_404(course_id)
    hits = library.search(course_id, q, limit=limit) if q.strip() else []
    return {"query": q, "hits": [h.to_dict() for h in hits]}


# Ders PDF'ini ayrıştırmak birkaç saniye sürüyor; kullanıcı seçim değiştirdikçe
# tekrar tekrar ödemeye değmez. Anahtar dosya SHA'sı, yani PDF değişirse
# önbellek kendiliğinden ıskalar.
_lecture_cache: dict[str, Any] = {}


def _parsed_lecture(mat: Material):
    if mat.sha not in _lecture_cache:
        from ..pdfio import parse_lecture

        _lecture_cache[mat.sha] = parse_lecture(
            mat.path,
            shape_threshold=settings.visual_shape_threshold,
            max_section_slides=settings.max_section_slides,
        )
    return _lecture_cache[mat.sha]


@app.get("/api/estimate")
async def estimate_run(
    lecture_id: str,
    book_id: str,
    exam_id: str = "",
    backend: str = "auto",
) -> dict:
    """Üret'e basmadan önce token / maliyet / süre projeksiyonu.

    CLI'daki `dersnotu estimate` ile AYNI fonksiyonu çağırır; iki rakamın
    ayrışmaması için hesap tek yerde (`estimate.py`).
    """
    lec_mat = _material_for("lecture", lecture_id, "")
    _material_for("book", book_id, "")
    book_mat = library.material(book_id)

    cozulen = resolve_backend(backend)
    exam_chars = 0
    if exam_id:
        exam = _material_for("exam", exam_id, "")
        try:
            exam_chars = min(exam.path.stat().st_size // 3, EXAM_CHAR_LIMIT)
        except OSError:
            exam_chars = 0

    try:
        lec = _parsed_lecture(lec_mat)
    except Exception as exc:
        raise HTTPException(400, f"Ders PDF'i okunamadı: {exc}") from exc

    p = project(
        lec,
        lec_mat.path,
        settings,
        book_sha=book_mat.sha,
        backend=cozulen,
        exam_chars=exam_chars,
        history=library.section_seconds(cozulen),
    )
    return {**p.to_dict(), "backend": cozulen, "quota": last_rate_limit()}


@app.get("/api/quota")
async def quota() -> dict:
    """Claude Pro abonelik kotası. API'de karşılığı yok; yalnızca CLI akışında
    bildiriliyor, o yüzden hiç çağrı yapılmadıysa `null`."""
    return {"backend": resolve_backend("auto"), "rate_limit": last_rate_limit()}


@app.get("/ders/{course_id}/not/{document_id}", response_class=HTMLResponse)
async def read_document(course_id: str, document_id: str) -> str:
    """Uygulama içi okuyucu — PDF'le birebir aynı render."""
    try:
        doc = library.document(document_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    if not (doc.html_path and doc.html_path.exists()):
        raise HTTPException(
            404,
            "Bu ders notunun okunabilir sürümü yok (eski bir koşudan kalmış). "
            "PDF'i indirebilir ya da eksikleri tamamlayıp yeniden üretebilirsin.",
        )
    return doc.html_path.read_text(encoding="utf-8")


@app.post("/api/documents/{document_id}/retry")
async def retry_document(document_id: str) -> dict:
    """Kalıcı dokümandan yeniden deneme.

    `/api/jobs/{id}/retry` yalnızca sunucu açık kaldıysa çalışır. Asıl senaryo
    — üretim yarıda kaldı, tarayıcı kapandı — bu uç tarafından karşılanıyor:
    kaynaklar kitaplıkta, konu kartları `.doc.json` içinde.
    """
    try:
        doc = library.document(document_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    if not doc.failed:
        raise HTTPException(400, "Bu dokümanda hatalı bölüm yok.")
    if not (doc.doc_path and doc.doc_path.exists()):
        raise HTTPException(400, "Dokümanın ara dosyası diskte yok; yeniden denenemiyor.")

    paths, ids = {}, {}
    for name, mid in (("lecture", doc.lecture_id), ("book", doc.book_id)):
        if not mid:
            raise HTTPException(400, "Kaynak dosya silinmiş; yeniden denenemiyor.")
        try:
            mat = library.material(mid)
        except NotFound as exc:
            raise HTTPException(400, "Kaynak dosya silinmiş; yeniden denenemiyor.") from exc
        if not (mat.path and mat.path.exists()):
            raise HTTPException(400, f"Kaynak dosya diskte yok: {mat.name}")
        paths[name] = str(mat.path)
        ids[f"{name}_id"] = mid

    # Sınav kâğıdı önekin parçası: atlanırsa yeniden üretilen bölüm
    # kardeşlerinden farklı bir çerçeveden çıkar. Silinmişse sessizce devam
    # edilir — bu bölümün hiç üretilmemesinden iyidir.
    if doc.exam_id:
        try:
            exam = library.material(doc.exam_id)
            if exam.path and exam.path.exists():
                paths["exam"] = str(exam.path)
                ids["exam_id"] = doc.exam_id
        except NotFound:
            pass

    job = store.create({
        "language": doc.language or "Türkçe",
        "backend": doc.backend or "auto",
        "depth": doc.depth or "standart",
        "extras": doc.extras,
        "course_id": doc.course_id,
        "lecture_name": doc.title,
        "paths": paths,
        **ids,
        "source_doc": str(doc.doc_path),
        # Aynı dokümanı güncelliyoruz: dosya adı ve satır kimliği korunur.
        "out_stem": doc.id,
        "document_created_at": doc.created_at,
    })
    store.submit(job, _execute_retry)
    return job.to_dict()


def _course_or_404(course_id: str):
    try:
        return library.course(course_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


def _page_count(path: Path) -> int:
    """Sayfa sayısı bilgi amaçlı; okunamayan PDF yüklemeyi bozmamalı."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# İş uçları
# ---------------------------------------------------------------------------
@app.post("/api/jobs")
async def create_job(
    # İki kaynak yolu: doğrudan yükleme (kitaplıksız hızlı deneme) ya da
    # kitaplıktaki bir materyalin kimliği (ders sayfasından gelen normal yol).
    lecture: UploadFile | None = File(None),
    book: UploadFile | None = File(None),
    lecture_id: str = Form(""),
    book_id: str = Form(""),
    # Geçmiş sınav kâğıdı: isteğe bağlı, yalnızca kitaplıktan seçilir.
    exam_id: str = Form(""),
    course_id: str = Form(""),
    language: str = Form("Türkçe"),
    backend: str = Form("auto"),
    depth: str = Form("standart"),
    # Çoklu seçim: aynı ad birden çok kez gönderilir (analoji, soru, ...).
    extras: list[str] = Form([]),
    demo: bool = Form(False),
) -> dict:
    if backend not in BACKENDS:
        raise HTTPException(400, f"Geçersiz arka uç: {backend}. Seçenekler: {', '.join(BACKENDS)}")
    if depth not in DEPTHS:
        raise HTTPException(400, f"Geçersiz derinlik: {depth}. Seçenekler: {', '.join(DEPTHS)}")
    if bad := [e for e in extras if e not in EXTRAS]:
        raise HTTPException(400, f"Geçersiz açıklama biçimi: {', '.join(bad)}")
    if course_id:
        _course_or_404(course_id)

    job = store.create(
        {
            "language": language,
            "backend": backend,
            "depth": depth,
            "extras": extras,
            "demo": demo,
            "course_id": course_id or None,
        }
    )
    job.params["out_stem"] = job.id

    paths: dict[str, str] = {}
    for name, mid, upload in (
        ("lecture", lecture_id, lecture),
        ("book", book_id, book),
    ):
        if mid:
            mat = _material_for(name, mid, course_id)
            paths[name] = str(mat.path)
            job.params[f"{name}_id"] = mat.id
            job.params[f"{name}_name"] = mat.name
            # Ders belirtilmediyse materyalinkini benimse: ders sayfasından
            # gelen istek zaten tek bir derse ait.
            job.params["course_id"] = job.params["course_id"] or mat.course_id
        elif upload is not None and upload.filename:
            if not upload.filename.lower().endswith(".pdf"):
                raise HTTPException(400, "Her iki dosya da PDF olmalı.")
            dest = UPLOADS / f"{job.id}-{name}.pdf"
            with dest.open("wb") as f:
                shutil.copyfileobj(upload.file, f)
            paths[name] = str(dest)
            job.params[f"{name}_name"] = upload.filename
            # Derse yüklendiyse kitaplığa da girsin, yoksa bir dahaki sefere
            # kullanıcı aynı 100 MB'ı tekrar yüklemek zorunda kalır.
            if job.params.get("course_id"):
                mat = library.add_material(
                    job.params["course_id"], name, upload.filename, dest,
                    sha256_file(dest), pages=_page_count(dest),
                )
                job.params[f"{name}_id"] = mat.id
                paths[name] = str(mat.path)
        else:
            raise HTTPException(400, f"Kaynak eksik: {name}. Dosya yükle veya seç.")

    if exam_id:
        exam = _material_for("exam", exam_id, course_id)
        paths["exam"] = str(exam.path)
        job.params["exam_id"] = exam.id
        job.params["exam_name"] = exam.name

    job.params["paths"] = paths

    # Kitap zaten indekslenmişse arayüzde hemen belli olsun.
    job.params["book_cached"] = BookIndex.is_built(settings.cache_dir, sha256_file(paths["book"]))

    store.submit(job, _execute)
    return job.to_dict()


def _material_for(kind: str, material_id: str, course_id: str) -> Material:
    try:
        mat = library.material(material_id)
    except NotFound as exc:
        raise HTTPException(404, f"Materyal bulunamadı: {material_id}") from exc
    if mat.kind != kind:
        raise HTTPException(400, f"{material_id} bir {kind} değil.")
    if course_id and mat.course_id != course_id:
        raise HTTPException(400, "Materyal bu derse ait değil.")
    if not (mat.path and mat.path.exists()):
        raise HTTPException(400, f"Dosya diskte yok: {mat.name}")
    return mat


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict:
    """Hatalı bölümleri yeniden üretir. Başarılı bölümler yeniden çağrılmaz."""
    src = store.get(job_id)
    if src is None:
        raise HTTPException(404, "İş bulunamadı.")
    if src.status not in (JobStatus.DONE, JobStatus.FAILED):
        raise HTTPException(409, "İş hâlâ çalışıyor; bitmesini bekle.")
    if not src.failed_sections:
        raise HTTPException(400, "Bu işte hatalı bölüm yok.")
    if not (src.doc_path and src.doc_path.exists()):
        raise HTTPException(
            400, "Bu işin dokümanı diskte yok; yeniden deneme yapılamıyor."
        )

    job = store.create({**src.params, "source_doc": str(src.doc_path), "retry_of": src.id})
    store.submit(job, _execute_retry)
    return job.to_dict()


@app.get("/api/jobs")
async def list_jobs() -> list[dict]:
    return [j.to_dict() for j in store.list()[:25]]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "İş bulunamadı.")
    return {**job.to_dict(), "events": [e.to_dict() for e in job.events]}


@app.get("/api/jobs/{job_id}/events")
async def stream_events(job_id: str) -> StreamingResponse:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "İş bulunamadı.")

    queue = store.subscribe(job_id)

    async def gen():
        try:
            # Bağlanmadan önce olmuş olayları önce gönder (yeniden bağlanma için).
            for ev in list(job.events):
                yield _sse(ev.to_dict())
            if job.status.value in ("done", "failed"):
                yield _sse({"type": "stream:end"})
                return
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"  # proxy'ler bağlantıyı kapatmasın
                    continue
                yield _sse(ev.to_dict())
                if ev.type == "stream:end":
                    return
        finally:
            store.unsubscribe(job_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str, fmt: str = "pdf") -> FileResponse:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "İş bulunamadı.")
    path = job.pdf_path if fmt == "pdf" else job.md_path
    if not path or not path.exists():
        raise HTTPException(404, "Çıktı henüz hazır değil.")
    stem = Path(job.params.get("lecture_name", "ders-notu")).stem
    return FileResponse(path, filename=f"{stem}-ders-notu.{fmt}")


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "credentials": LLMClient.credentials_available(),
        "model": settings.model,
        # Arayüz seçenekleri buradan okur; tek kaynak prompts.py.
        "depths": list(DEPTHS),
        "extras": list(EXTRAS),
        # Kullanıcıya gösterilen açıklamalar da aynı yerden — direktif
        # değişip açıklama olduğu yerde kalırsa arayüz yalan söyler.
        "depth_help": DEPTH_HELP,
        "extra_help": EXTRA_HELP,
        # Hangi kimlik yolları kullanılabilir (API anahtarı / Claude Pro / demo).
        "backends": backend_status(),
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Arayüz
# ---------------------------------------------------------------------------
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _page(name: str) -> str:
    page = STATIC / name
    if not page.exists():
        return "<h1>dersnotu</h1><p>Arayüz dosyası bulunamadı.</p>"
    return page.read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _page("index.html")


@app.get("/ders/{course_id}", response_class=HTMLResponse)
async def course_page(course_id: str) -> str:
    return _page("course.html")
