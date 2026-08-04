"""FastAPI sunucusu: yükleme → iş → SSE ilerleme → PDF indirme."""

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

from ..config import settings
from ..index import BookIndex
from ..llm import (
    BACKENDS,
    ClaudeCodeClient,
    backend_status,
    make_client,
    resolve_backend,
)
from ..llm.client import LLMClient
from ..llm.prompts import DEPTHS, EXTRAS
from ..models import StudyDocument
from ..pdfio import sha256_file
from ..pipeline import Inputs, retry_failed, run, save_debug, to_markdown
from ..render import render_document
from .jobs import Event, Job, JobStatus, JobStore

STATIC = Path(__file__).resolve().parent / "static"
UPLOADS = settings.cache_dir / "uploads"

store = JobStore(max_workers=2)


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
    return Inputs(
        lecture_path=Path(p["paths"]["lecture"]),
        book_path=Path(p["paths"]["book"]),
        language=p["language"],
        depth=p.get("depth", "standart"),
        extras=p.get("extras", []),
        backend=p.get("backend", "auto"),
    )


def _publish(job: Job, doc: StudyDocument, emit) -> None:
    """Dokümanı diske yazar: markdown, PDF ve yeniden deneme için JSON."""
    job.usage = doc.usage.model_dump()
    job.failed_sections = [s.section_index for s in doc.failed]

    out_base = settings.out_dir / f"{job.id}"
    md_path = out_base.with_suffix(".md")
    md_path.write_text(to_markdown(doc), encoding="utf-8")
    job.md_path = md_path

    # Yeniden deneme bu dosyadan besleniyor: konu kartları ve hizalama burada.
    doc_path = out_base.with_suffix(".doc.json")
    save_debug(doc, doc_path)
    job.doc_path = doc_path

    emit(Event("render:start"))
    pdf_path = render_document(
        doc,
        out_base.with_suffix(".pdf"),
        lecture_pdf=job.params["paths"]["lecture"],
        book_pdf=job.params["paths"]["book"],
    )
    job.pdf_path = pdf_path
    emit(Event("render:done", {"size": pdf_path.stat().st_size}))


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
        limit_sections=job.params.get("sections") or None,
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
# Uçlar
# ---------------------------------------------------------------------------
@app.post("/api/jobs")
async def create_job(
    lecture: UploadFile = File(...),
    book: UploadFile = File(...),
    language: str = Form("Türkçe"),
    backend: str = Form("auto"),
    depth: str = Form("standart"),
    # Çoklu seçim: aynı ad birden çok kez gönderilir (analoji, soru, ...).
    extras: list[str] = Form([]),
    sections: int = Form(0),
    demo: bool = Form(False),
) -> dict:
    if not lecture.filename.lower().endswith(".pdf") or not book.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Her iki dosya da PDF olmalı.")
    if backend not in BACKENDS:
        raise HTTPException(400, f"Geçersiz arka uç: {backend}. Seçenekler: {', '.join(BACKENDS)}")
    if depth not in DEPTHS:
        raise HTTPException(400, f"Geçersiz derinlik: {depth}. Seçenekler: {', '.join(DEPTHS)}")
    if bad := [e for e in extras if e not in EXTRAS]:
        raise HTTPException(400, f"Geçersiz açıklama biçimi: {', '.join(bad)}")

    job = store.create(
        {
            "language": language,
            "backend": backend,
            "depth": depth,
            "extras": extras,
            "sections": sections,
            "demo": demo,
            "lecture_name": lecture.filename,
            "book_name": book.filename,
        }
    )

    paths = {}
    for name, upload in (("lecture", lecture), ("book", book)):
        dest = UPLOADS / f"{job.id}-{name}.pdf"
        with dest.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        paths[name] = str(dest)
    job.params["paths"] = paths

    # Kitap zaten indekslenmişse arayüzde hemen belli olsun.
    job.params["book_cached"] = BookIndex.is_built(settings.cache_dir, sha256_file(paths["book"]))

    store.submit(job, _execute)
    return job.to_dict()


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


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    page = STATIC / "index.html"
    if not page.exists():
        return "<h1>dersnotu</h1><p>Arayüz dosyası bulunamadı.</p>"
    return page.read_text(encoding="utf-8")
