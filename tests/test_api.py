"""API ve iş kuyruğu.

Tam boru hattı testi `-m slow` ile ayrılmıştır: gerçek PDF'leri okur ve
Chromium ile PDF basar (~10 sn).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dersnotu.api.jobs import Event, JobStatus, JobStore
from dersnotu.api.server import app

LECTURE = Path("Lecture02 - Bitsints.pptx.pdf")
BOOK = Path("CSAPP_2016.pdf")
HAVE_PDFS = LECTURE.exists() and BOOK.exists()


# --- JobStore (ağ/PDF gerektirmez) ----------------------------------------
def test_job_lifecycle_success():
    store = JobStore(max_workers=1)
    job = store.create({"x": 1})
    assert job.status is JobStatus.QUEUED

    store.submit(job, lambda j, emit: emit(Event("custom:step", {"detail": "ok"})))
    _wait(lambda: job.status is JobStatus.DONE)

    types = [e.type for e in job.events]
    assert types == ["job:running", "custom:step", "job:done", "stream:end"]
    store.shutdown()


def test_failing_job_is_captured_not_raised():
    store = JobStore(max_workers=1)
    job = store.create({})

    def boom(j, emit):
        raise ValueError("patladı")

    store.submit(job, boom)
    _wait(lambda: job.status is JobStatus.FAILED)

    assert "ValueError: patladı" in job.error
    # Hata bile olsa akış düzgün kapanmalı, yoksa istemci asılı kalır.
    assert job.events[-1].type == "stream:end"
    store.shutdown()


def test_delta_events_are_not_stored_in_history():
    """Delta'lar saniyede yüzlerce; geçmişte tutmak belleği şişirir."""
    store = JobStore(max_workers=1)
    job = store.create({})
    for _ in range(500):
        store.publish(job.id, Event("section:delta", {"text": "x"}))
    store.publish(job.id, Event("section:done"))
    assert [e.type for e in job.events] == ["section:done"]
    store.shutdown()


def test_publish_to_unknown_job_is_noop():
    store = JobStore(max_workers=1)
    store.publish("yok", Event("x"))  # patlamamalı
    store.shutdown()


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("zaman aşımı")


# --- HTTP uçları -----------------------------------------------------------
@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health_reports_credential_state(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert isinstance(body["credentials"], bool)
    assert body["model"]


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "dersnotu" in r.text
    assert "EventSource" in r.text  # SSE istemcisi gömülü


def test_unknown_job_returns_404(client):
    assert client.get("/api/jobs/yokboyle").status_code == 404
    assert client.get("/api/jobs/yokboyle/download").status_code == 404
    assert client.get("/api/jobs/yokboyle/events").status_code == 404


def test_non_pdf_upload_rejected(client):
    r = client.post(
        "/api/jobs",
        files={
            "lecture": ("a.txt", b"x", "text/plain"),
            "book": ("b.pdf", b"x", "application/pdf"),
        },
    )
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_health_exposes_mode_options(client):
    """Arayüz seçenekleri tek kaynaktan (prompts.py) okumalı."""
    body = client.get("/api/health").json()
    assert "standart" in body["depths"]
    assert {"analoji", "soru", "sözlük"} <= set(body["extras"])


def test_health_reports_available_backends(client):
    """Arayüz elde olmayan kimlik yolunu seçtirmemeli."""
    b = client.get("/api/health").json()["backends"]
    assert b["resolved"] in ("api", "cli", "demo")
    assert b["demo"]["available"] is True  # demo her zaman var
    for key in ("api", "cli"):
        assert isinstance(b[key]["available"], bool)
        assert b[key]["label"]


def test_invalid_backend_rejected(client):
    r = client.post("/api/jobs", files=_files(), data={"backend": "gpt"})
    assert r.status_code == 400
    assert "arka uç" in r.json()["detail"].lower()


def test_backend_choice_is_recorded_on_the_job(client):
    r = client.post("/api/jobs", files=_files(), data={"backend": "demo"})
    assert r.status_code == 200
    assert r.json()["params"]["backend"] == "demo"


def _files():
    return {
        "lecture": ("a.pdf", b"%PDF-", "application/pdf"),
        "book": ("b.pdf", b"%PDF-", "application/pdf"),
    }


def test_invalid_depth_rejected(client):
    r = client.post("/api/jobs", files=_files(), data={"depth": "çok-derin"})
    assert r.status_code == 400
    assert "derinlik" in r.json()["detail"].lower()


def test_invalid_extra_rejected(client):
    r = client.post("/api/jobs", files=_files(), data={"extras": ["yokböyle"]})
    assert r.status_code == 400


def test_mode_selection_is_recorded_on_the_job(client):
    """Seçim işe yazılmazsa arayüzdeki kontrol hiçbir şey yapmıyor demektir."""
    r = client.post(
        "/api/jobs",
        files=_files(),
        data={"depth": "derin", "extras": ["analoji", "soru"], "demo": "true"},
    )
    assert r.status_code == 200
    params = r.json()["params"]
    assert params["depth"] == "derin"
    assert params["extras"] == ["analoji", "soru"]


def test_retry_unknown_job_is_404(client):
    assert client.post("/api/jobs/yokboyle/retry").status_code == 404


def test_retry_rejected_while_running(client):
    """Çalışan bir işi yeniden denemek iki işin aynı dosyaya yazması demek."""
    r = client.post("/api/jobs", files=_files(), data={"backend": "demo"})
    jid = r.json()["id"]
    resp = client.post(f"/api/jobs/{jid}/retry")
    # Ya hâlâ çalışıyor (409) ya da bitmiş ve hatalı bölüm yok (400).
    assert resp.status_code in (400, 409)


def test_job_dict_reports_retry_capability():
    """Arayüz düğmeyi bu iki alana bakarak gösteriyor."""
    from dersnotu.api.jobs import Job

    job = Job(id="x", params={})
    d = job.to_dict()
    assert d["failed_sections"] == []
    assert d["can_retry"] is False

    job.failed_sections = [3]
    assert job.to_dict()["can_retry"] is False  # doküman diskte yok
    job.doc_path = Path(__file__)  # var olan bir dosya
    assert job.to_dict()["can_retry"] is True


@pytest.mark.skipif(not HAVE_PDFS, reason="örnek PDF'ler yok")
@pytest.mark.slow
def test_full_demo_run_produces_downloadable_pdf(client):
    with LECTURE.open("rb") as lf, BOOK.open("rb") as bf:
        r = client.post(
            "/api/jobs",
            files={
                "lecture": (LECTURE.name, lf, "application/pdf"),
                "book": (BOOK.name, bf, "application/pdf"),
            },
            data={"language": "Türkçe", "sections": "1", "demo": "true"},
        )
    assert r.status_code == 200
    jid = r.json()["id"]

    def done():
        return client.get(f"/api/jobs/{jid}").json()["status"] in ("done", "failed")

    _wait(done, timeout=180)

    state = client.get(f"/api/jobs/{jid}").json()
    assert state["status"] == "done", state["error"]
    assert state["has_pdf"]

    types = [e["type"] for e in state["events"]]
    for expected in ("lecture:done", "topics:done", "section:done", "render:done"):
        assert expected in types, types

    pdf = client.get(f"/api/jobs/{jid}/download")
    assert pdf.status_code == 200
    assert pdf.content[:5] == b"%PDF-"
    assert len(pdf.content) > 20_000

    md = client.get(f"/api/jobs/{jid}/download?fmt=md")
    assert md.status_code == 200
    # Retrieval gerçekten çalıştıysa çıktıda kitap atıfı olmalı.
    assert "[K:" in md.text
