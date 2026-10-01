"""API ve iş kuyruğu.

Tam boru hattı testi `-m slow` ile ayrılmıştır: gerçek PDF'leri okur ve
Chromium ile PDF basar (~10 sn).
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dersnotu.api.jobs import Event, JobStatus, JobStore
from dersnotu.api.server import app
from dersnotu.library import LibraryStore

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
def client(tmp_path, monkeypatch):
    """Kitaplığı geçici dizine alır.

    Aksi hâlde testler kullanıcının gerçek `.cache/library.sqlite` dosyasına
    ders yazar — bir test paketi kullanıcının ders listesini kirletmemeli.
    """
    from dersnotu.api import server

    monkeypatch.setattr(
        server, "library",
        LibraryStore(tmp_path / "library.sqlite", tmp_path / "materials"),
    )
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
    assert "/api/courses" in r.text  # ders listesi istemcisi gömülü


def test_course_page_served(client):
    """Ders sayfası tek dosya; kimlik URL'den okunuyor, sunucu şablon basmıyor."""
    r = client.get("/ders/hangisiolursa")
    assert r.status_code == 200
    assert "EventSource" in r.text  # SSE istemcisi burada


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
    assert "standard" in body["depths"]
    assert {"analogy", "quiz", "glossary"} <= set(body["extras"])


def test_health_reports_available_backends(client):
    """Arayüz elde olmayan kimlik yolunu seçtirmemeli."""
    b = client.get("/api/health").json()["backends"]
    assert b["resolved"] in ("api", "cli", "codex", "demo")
    assert b["demo"]["available"] is True  # demo her zaman var
    for key in ("api", "cli", "codex"):
        assert isinstance(b[key]["available"], bool)
        assert b[key]["label"]


def test_invalid_backend_rejected(client):
    r = client.post("/api/jobs", files=_files(), data={"backend": "gpt"})
    assert r.status_code == 400
    assert "backend" in r.json()["detail"].lower()


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
    assert "depth" in r.json()["detail"].lower()


def test_invalid_extra_rejected(client):
    r = client.post("/api/jobs", files=_files(), data={"extras": ["yokböyle"]})
    assert r.status_code == 400


def test_section_limit_is_not_a_web_option(client):
    """Web arayüzünde "ilk N bölüm" yok: kısaltılmış bir ders notu özet değil,
    eksik bir belgedir ve o eksiği 'tamamla' düğmesi de getirmez. Maliyet
    kaygısını tahmin şeridi ve demo modu karşılıyor. Alan sunucudan da
    kaldırıldı; gönderilse bile yok sayılmalı, sessizce koşuyu kırpmamalı."""
    c = _course(client)
    lec = _material(client, c["id"], "lecture", "slayt.pdf")
    book = _material(client, c["id"], "book", "kitap.pdf")
    r = client.post("/api/jobs", data={
        "course_id": c["id"], "lecture_id": lec["id"], "book_id": book["id"],
        "backend": "demo", "sections": "1",
    })
    assert r.status_code == 200
    assert "sections" not in r.json()["params"]


def test_mode_selection_is_recorded_on_the_job(client):
    """Seçim işe yazılmazsa arayüzdeki kontrol hiçbir şey yapmıyor demektir."""
    r = client.post(
        "/api/jobs",
        files=_files(),
        data={"depth": "derin", "extras": ["analoji", "soru"], "demo": "true"},
    )
    assert r.status_code == 200
    params = r.json()["params"]
    assert params["depth"] == "deep"
    assert params["extras"] == ["analogy", "quiz"]


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


# --- ders kitaplığı --------------------------------------------------------
def _course(client, name="Bilgisayar Sistemleri", code="BLG 212"):
    r = client.post("/api/courses", json={"name": name, "code": code})
    assert r.status_code == 200, r.text
    return r.json()


def _material(client, cid, kind, name="a.pdf"):
    r = client.post(
        f"/api/courses/{cid}/materials",
        data={"kind": kind},
        files={"file": (name, b"%PDF-govde", "application/pdf")},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_batch_upload_accepts_multiple_pdfs_and_reports_bad_files(client):
    c = _course(client)
    r = client.post(
        f"/api/courses/{c['id']}/materials/batch",
        data={"kind": "lecture"},
        files=[
            ("files", ("first.pdf", b"%PDF-first", "application/pdf")),
            ("files", ("second.pdf", b"%PDF-second", "application/pdf")),
            ("files", ("notes.txt", b"not pdf", "text/plain")),
        ],
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["uploaded"]) == 2
    assert [e["name"] for e in r.json()["errors"]] == ["notes.txt"]
    assert len(client.get(f"/api/courses/{c['id']}").json()["lectures"]) == 2


def test_publication_is_opt_in_and_reversible(client, tmp_path):
    from dersnotu.api import server

    c = _course(client)
    pdf = tmp_path / "study.pdf"
    pdf.write_bytes(b"%PDF-public-note")
    doc = server.library.add_document(course_id=c["id"], title="Study", pdf_path=pdf)
    url = f"/api/public/documents/{doc.id}/pdf"
    assert client.get("/api/public/documents").json() == []
    assert client.get(url).status_code == 404

    published = client.patch(
        f"/api/documents/{doc.id}/publication", json={"is_public": True}
    )
    assert published.status_code == 200
    assert published.json()["is_public"] is True
    assert client.get("/api/public/documents").json()[0]["title"] == "Study"
    assert client.get(url).content == b"%PDF-public-note"

    # Replacing a failed document during retry keeps the owner's choice.
    server.library.add_document(id=doc.id, course_id=c["id"], title="Study", pdf_path=pdf)
    assert server.library.document(doc.id).is_public is True
    client.patch(f"/api/documents/{doc.id}/publication", json={"is_public": False})
    assert client.get("/api/public/documents").json() == []
    assert client.get(url).status_code == 404


def test_admin_password_protects_private_routes_but_not_public_gallery(
    client, monkeypatch
):
    from dersnotu.api import server

    monkeypatch.setattr(server.settings, "admin_password", "secret-password")
    assert client.get("/").status_code == 401
    assert client.get("/api/courses").status_code == 401
    assert client.get("/public").status_code == 200
    assert client.get("/api/public/documents").status_code == 200
    token = base64.b64encode(b"admin:secret-password").decode()
    headers = {"Authorization": f"Basic {token}"}
    assert client.get("/api/courses", headers=headers).status_code == 200
    assert client.post("/api/courses", json={"name": "X"}, headers={
        **headers, "Origin": "https://other.example"
    }).status_code == 403


def test_course_create_list_delete(client):
    c = _course(client)
    assert c["code"] == "BLG 212"
    assert [x["id"] for x in client.get("/api/courses").json()] == [c["id"]]

    assert client.delete(f"/api/courses/{c['id']}").status_code == 200
    assert client.get("/api/courses").json() == []
    assert client.get(f"/api/courses/{c['id']}").status_code == 404


def test_blank_course_name_rejected(client):
    assert client.post("/api/courses", json={"name": "  "}).status_code == 400


def test_unknown_course_is_404(client):
    assert client.get("/api/courses/yok").status_code == 404
    assert client.delete("/api/courses/yok").status_code == 404
    assert client.patch("/api/courses/yok", json={"name": "x"}).status_code == 404


def test_course_detail_splits_materials_by_kind(client):
    c = _course(client)
    lec = _material(client, c["id"], "lecture", "slayt.pdf")
    book = _material(client, c["id"], "book", "kitap.pdf")

    d = client.get(f"/api/courses/{c['id']}").json()
    assert [m["id"] for m in d["lectures"]] == [lec["id"]]
    assert [m["id"] for m in d["books"]] == [book["id"]]
    assert d["counts"] == {
        "lectures": 1, "books": 1, "documents": 0, "notes": 0, "practices": 0
    }
    # Kitap indeks durumu seçim ekranında gösteriliyor; alan hep bulunmalı.
    assert d["books"][0]["indexed"] is False
    assert "indexed" not in d["lectures"][0]


def test_unreadable_pdf_still_uploads(client):
    """Sayfa sayısı bilgi amaçlı; okunamayan PDF yüklemeyi bozmamalı."""
    c = _course(client)
    m = _material(client, c["id"], "book")
    assert m["pages"] == 0
    assert m["available"] is True


def test_non_pdf_material_rejected(client):
    c = _course(client)
    r = client.post(
        f"/api/courses/{c['id']}/materials",
        data={"kind": "lecture"},
        files={"file": ("a.txt", b"x", "text/plain")},
    )
    assert r.status_code == 400


def test_invalid_material_kind_rejected(client):
    c = _course(client)
    r = client.post(
        f"/api/courses/{c['id']}/materials",
        data={"kind": "notlar"},
        files={"file": ("a.pdf", b"%PDF-", "application/pdf")},
    )
    assert r.status_code == 400


def test_material_download_returns_the_original(client):
    c = _course(client)
    m = _material(client, c["id"], "book", "kitap.pdf")
    r = client.get(f"/api/materials/{m['id']}/download")
    assert r.status_code == 200
    assert r.content == b"%PDF-govde"


def test_course_rename(client):
    c = _course(client)
    r = client.patch(f"/api/courses/{c['id']}", json={"name": "Yeni ad"})
    assert r.json()["name"] == "Yeni ad"
    assert r.json()["code"] == "BLG 212"  # dokunulmayan alan korunur


def test_job_from_library_materials(client):
    """Ders sayfasının normal yolu: dosya değil, materyal kimliği gönderilir."""
    c = _course(client)
    lec = _material(client, c["id"], "lecture", "slayt.pdf")
    book = _material(client, c["id"], "book", "kitap.pdf")

    r = client.post("/api/jobs", data={
        "course_id": c["id"], "lecture_id": lec["id"], "book_id": book["id"],
        "backend": "demo",
    })
    assert r.status_code == 200, r.text
    p = r.json()["params"]
    assert p["course_id"] == c["id"]
    assert p["lecture_id"] == lec["id"] and p["book_id"] == book["id"]
    assert p["lecture_name"] == "slayt.pdf"


def test_job_rejects_material_of_the_wrong_kind(client):
    """Kitabı slayt diye göndermek sessizce saçma bir çıktı üretirdi."""
    c = _course(client)
    book = _material(client, c["id"], "book", "kitap.pdf")
    r = client.post("/api/jobs", data={
        "lecture_id": book["id"], "book_id": book["id"], "backend": "demo",
    })
    assert r.status_code == 400


def test_job_with_unknown_material_is_404(client):
    r = client.post("/api/jobs", data={
        "lecture_id": "yok", "book_id": "yok", "backend": "demo",
    })
    assert r.status_code == 404


def test_job_without_any_source_is_rejected(client):
    r = client.post("/api/jobs", data={"backend": "demo"})
    assert r.status_code == 400
    assert "missing" in r.json()["detail"].lower()


def test_upload_through_job_lands_in_the_library(client):
    """Derse yüklenen dosya kitaplığa girmezse kullanıcı her seferinde
    aynı 100 MB'ı tekrar yükler."""
    c = _course(client)
    r = client.post("/api/jobs", files=_files(),
                    data={"course_id": c["id"], "backend": "demo"})
    assert r.status_code == 200

    d = client.get(f"/api/courses/{c['id']}").json()
    assert d["counts"]["lectures"] == 1 and d["counts"]["books"] == 1


def test_document_retry_requires_failed_sections(client):
    c = _course(client)
    from dersnotu.api import server

    doc = server.library.add_document(course_id=c["id"], title="X")
    assert client.post(f"/api/documents/{doc.id}/retry").status_code == 400
    assert client.post("/api/documents/yok/retry").status_code == 404


def test_document_retry_refuses_when_source_is_gone(client, tmp_path):
    """Kaynak silinmişse net bir hata; sessizce yanlış dosyayla koşmak değil."""
    from dersnotu.api import server

    c = _course(client)
    ara = tmp_path / "x.doc.json"
    ara.write_text("{}", encoding="utf-8")
    doc = server.library.add_document(
        course_id=c["id"], title="X", failed=[1], doc_path=ara
    )
    r = client.post(f"/api/documents/{doc.id}/retry")
    assert r.status_code == 400
    assert "deleted" in r.json()["detail"]


def test_document_download_404_when_file_missing(client):
    from dersnotu.api import server

    c = _course(client)
    doc = server.library.add_document(course_id=c["id"], title="X")
    assert client.get(f"/api/documents/{doc.id}/download").status_code == 404
    assert client.delete(f"/api/documents/{doc.id}").status_code == 200


# --- sınav materyali -------------------------------------------------------
def test_exam_material_is_separate_from_lectures_and_books(client):
    c = _course(client)
    _material(client, c["id"], "lecture", "slayt.pdf")
    exam = _material(client, c["id"], "exam", "2023-vize.pdf")

    d = client.get(f"/api/courses/{c['id']}").json()
    assert [m["id"] for m in d["exams"]] == [exam["id"]]
    assert d["counts"]["lectures"] == 1  # sınav slayt sayımına girmemeli


def test_exam_reaches_the_job(client):
    c = _course(client)
    lec = _material(client, c["id"], "lecture", "slayt.pdf")
    book = _material(client, c["id"], "book", "kitap.pdf")
    exam = _material(client, c["id"], "exam", "vize.pdf")

    r = client.post("/api/jobs", data={
        "course_id": c["id"], "lecture_id": lec["id"], "book_id": book["id"],
        "exam_id": exam["id"], "backend": "demo",
    })
    assert r.json()["params"]["exam_id"] == exam["id"]


def test_exam_id_must_be_an_exam(client):
    """Kitabı sınav diye göndermek 600 sayfayı öneke tıkardı."""
    c = _course(client)
    lec = _material(client, c["id"], "lecture", "slayt.pdf")
    book = _material(client, c["id"], "book", "kitap.pdf")
    r = client.post("/api/jobs", data={
        "course_id": c["id"], "lecture_id": lec["id"], "book_id": book["id"],
        "exam_id": book["id"], "backend": "demo",
    })
    assert r.status_code == 400


# --- arama -----------------------------------------------------------------
def test_course_search(client):
    from dersnotu.api import server

    c = _course(client)
    doc = server.library.add_document(course_id=c["id"], title="Bits")
    server.library.index_document(doc.id, c["id"], [
        (0, "Sayı gösterimi", "İkinin tümleyeni negatif sayıları kodlar."),
    ])

    body = client.get(f"/api/courses/{c['id']}/search", params={"q": "tümleyeni"}).json()
    assert len(body["hits"]) == 1
    assert body["hits"][0]["document_id"] == doc.id
    assert body["hits"][0]["section"] == 0

    # Boş sorgu tüm dokümanları dökmemeli.
    assert client.get(f"/api/courses/{c['id']}/search").json()["hits"] == []
    assert client.get("/api/courses/yok/search?q=x").status_code == 404


# --- okuyucu ---------------------------------------------------------------
def test_reader_404_when_html_missing(client):
    """Eski koşularda HTML yok; PDF hâlâ inilebilir olmalı, okuyucu net demeli."""
    from dersnotu.api import server

    c = _course(client)
    doc = server.library.add_document(course_id=c["id"], title="X")
    r = client.get(f"/ders/{c['id']}/not/{doc.id}")
    assert r.status_code == 404
    assert "readable" in r.json()["detail"]


def test_reader_serves_saved_html(client, tmp_path):
    from dersnotu.api import server

    c = _course(client)
    page = tmp_path / "x.html"
    page.write_text("<html><body>ders notu</body></html>", encoding="utf-8")
    doc = server.library.add_document(course_id=c["id"], title="X", html_path=page)
    r = client.get(f"/ders/{c['id']}/not/{doc.id}")
    assert r.status_code == 200
    assert "ders notu" in r.text


# --- tahmin / kota ---------------------------------------------------------
def test_estimate_rejects_unreadable_lecture(client):
    """6 baytlık sahte PDF ayrıştırılamaz; net hata, sessiz sıfır değil."""
    c = _course(client)
    lec = _material(client, c["id"], "lecture", "slayt.pdf")
    book = _material(client, c["id"], "book", "kitap.pdf")
    r = client.get("/api/estimate",
                   params={"lecture_id": lec["id"], "book_id": book["id"]})
    assert r.status_code == 400
    assert "could not be read" in r.json()["detail"]


def test_estimate_needs_real_materials(client):
    assert client.get("/api/estimate",
                      params={"lecture_id": "yok", "book_id": "yok"}).status_code == 404


def test_quota_endpoint_is_honest_when_unknown(client):
    """Kota yalnızca CLI akışında bildiriliyor; hiç çağrı yoksa null döner."""
    body = client.get("/api/quota").json()
    assert body["backend"] in ("api", "cli", "codex", "demo")
    assert "rate_limit" in body


@pytest.mark.skipif(not HAVE_PDFS, reason="örnek PDF'ler yok")
@pytest.mark.slow
def test_estimate_on_real_pdfs(client):
    """Gerçek ders + indekslenmiş kitapla projeksiyon."""
    c = _course(client)
    with LECTURE.open("rb") as f:
        lec = client.post(f"/api/courses/{c['id']}/materials", data={"kind": "lecture"},
                          files={"file": (LECTURE.name, f, "application/pdf")}).json()
    with BOOK.open("rb") as f:
        book = client.post(f"/api/courses/{c['id']}/materials", data={"kind": "book"},
                           files={"file": (BOOK.name, f, "application/pdf")}).json()

    t = client.get("/api/estimate", params={
        "lecture_id": lec["id"], "book_id": book["id"], "backend": "api",
    }).json()
    assert t["sections"] > 1
    assert t["tokens"]["images"] > 0           # görsel slaytlar sayıldı
    assert t["tokens"]["total_input"] > 10_000
    assert 0 < t["cost"] < 20                  # makul aralık
    assert t["seconds"] > 0
    # Görüntü tokenları girdinin en büyük kalemi olmalı — projenin ana bulgusu.
    assert t["tokens"]["images"] > t["tokens"]["retrieval"]


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
            data={"language": "Türkçe", "demo": "true"},
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
    assert "[B:" in md.text


# --- deneme sınavı ---------------------------------------------------------
def test_practice_requires_a_past_exam(client):
    """Ders notunda kâğıt isteğe bağlıydı; burada ŞABLON o, zorunlu."""
    c = _course(client)
    lec = _material(client, c["id"], "lecture", "slayt.pdf")
    book = _material(client, c["id"], "book", "kitap.pdf")
    r = client.post("/api/practice", data={
        "course_id": c["id"], "lecture_id": lec["id"], "book_id": book["id"],
    })
    assert r.status_code == 422  # exam_id eksik


def test_practice_rejects_a_material_of_the_wrong_kind(client):
    c = _course(client)
    lec = _material(client, c["id"], "lecture", "slayt.pdf")
    book = _material(client, c["id"], "book", "kitap.pdf")
    r = client.post("/api/practice", data={
        "course_id": c["id"], "lecture_id": lec["id"], "book_id": book["id"],
        "exam_id": lec["id"],  # slayt, sınav değil
    })
    assert r.status_code == 400
    assert "exam" in r.json()["detail"]


def test_practice_rejects_an_absurd_question_count(client):
    c = _course(client)
    lec = _material(client, c["id"], "lecture", "slayt.pdf")
    book = _material(client, c["id"], "book", "kitap.pdf")
    exam = _material(client, c["id"], "exam", "vize.pdf")
    r = client.post("/api/practice", data={
        "course_id": c["id"], "lecture_id": lec["id"], "book_id": book["id"],
        "exam_id": exam["id"], "count": 500,
    })
    assert r.status_code == 400
    assert "Question count" in r.json()["detail"]


@pytest.mark.skipif(not HAVE_PDFS, reason="örnek PDF'ler yok")
@pytest.mark.slow
def test_full_demo_practice_run_produces_a_paper_and_a_key(client, tmp_path):
    """Uçtan uca: kâğıt oku → kapsam çıkar → alıntı topla → soru üret → bas.

    Demo modunda koşuyor ama içerik GERÇEK kaynaklardan kuruluyor; bu yüzden
    retrieval ya da sınav metni okuma bozulursa burada yakalanır.
    """
    from dersnotu.render.pdf import html_to_pdf

    kagit = tmp_path / "vize.pdf"
    html_to_pdf(
        "<!doctype html><html lang='en'><body>"
        "<p>1. What is the decimal value of the bit pattern 0x9C in 8 bits?</p>"
        "<p>2. Which byte lies at address 0x102 on a little-endian machine?</p>"
        "</body></html>",
        kagit,
    )

    c = _course(client)
    ids = {}
    for kind, path in (("lecture", LECTURE), ("book", BOOK), ("exam", kagit)):
        with path.open("rb") as f:
            ids[kind] = client.post(
                f"/api/courses/{c['id']}/materials", data={"kind": kind},
                files={"file": (path.name, f, "application/pdf")},
            ).json()["id"]

    r = client.post("/api/practice", data={
        "course_id": c["id"], "lecture_id": ids["lecture"], "book_id": ids["book"],
        "exam_id": ids["exam"], "count": 3, "demo": "true",
    })
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    _wait(lambda: client.get(f"/api/jobs/{jid}").json()["status"] in ("done", "failed"),
          timeout=240)

    state = client.get(f"/api/jobs/{jid}").json()
    assert state["status"] == "done", state["error"]
    types = [e["type"] for e in state["events"]]
    for beklenen in ("exam:loaded", "topics:done", "retrieve:done",
                     "questions:done", "render:done"):
        assert beklenen in types, types

    # Kitaplığa deneme olarak yazılmış olmalı — ders notu sayımına karışmamalı.
    ders = client.get(f"/api/courses/{c['id']}").json()
    assert ders["counts"] == {"lectures": 1, "books": 1, "documents": 1,
                              "notes": 0, "practices": 1}
    belge = ders["documents"][0]
    assert belge["kind"] == "practice"
    assert belge["sections"] == 3

    md = client.get(f"/api/documents/{belge['id']}/download?fmt=md").text
    # Anahtar SONDA: çözüm soruların altında olsaydı kâğıt denemelik olmazdı.
    assert md.index("Question 3") < md.index("# Answer key")
    assert "[B:" in md
    assert "0x9C" in md  # geçmiş kâğıttan birebir alıntı

    # Sorular aramaya girmiş olmalı, çıpası soru numarası.
    hits = client.get(f"/api/courses/{c['id']}/search", params={"q": "tümleyen"}).json()
    assert all(h["document_kind"] == "practice" for h in hits["hits"])
