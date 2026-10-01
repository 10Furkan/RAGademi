"""Cloud file durability and library behavior without live account credentials.

Library SQL executes against a real SQLite test database; only the persistent
metadata connection is substituted. The production file storage and library
code run unchanged against an in-memory S3 service.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import ssl
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from dersnotu.cloud_library import (
    CloudLibraryStore,
    _database_connection,
    _Rows,
    _Session,
    make_library,
)
from dersnotu.config import Settings
from dersnotu.library import LibraryStore
from dersnotu.storage import S3Files, StorageError


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.downloads = []
        self.fail_suffix = None
        self.truncate = False

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        if self.fail_suffix and Key.endswith(self.fail_suffix):
            raise ClientError({"Error": {"Code": "QuotaExceeded"}}, "PutObject")
        self.objects[Key] = Body.read()

    def get_object(self, *, Bucket, Key):
        self.downloads.append(Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        content = self.objects[Key]
        return {"Body": io.BytesIO(content[:-1] if self.truncate else content),
                "ContentLength": len(content)}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)


class CloudWithLocalMetadata(CloudLibraryStore):
    _conn = LibraryStore._conn
    search = LibraryStore.search

    @contextmanager
    def _mutation_lock(self, key):
        yield  # PostgreSQL advisory locks are not available in the SQLite test database.

    def __init__(self, db, config, files):
        self.config, self.files = config, files
        LibraryStore.__init__(self, db, files.local_path("materials"))


@pytest.fixture
def cloud(tmp_path):
    config = Settings(_env_file=None, cache_dir=tmp_path / "ephemeral", out_dir=tmp_path / "out")
    client = MemoryS3()
    files = S3Files(config, client=client)
    store = CloudWithLocalMetadata(tmp_path / "persistent-metadata.sqlite", config, files)
    return store, client


def source_pdf(tmp_path):
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-test source")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def lose_cache(store, tmp_path):
    assert store.files.root.is_relative_to(tmp_path.resolve())
    shutil.rmtree(store.files.root)


def document(store, tmp_path, *, id="study", body=b"%PDF-old", **options):
    pdf = tmp_path / "study.pdf"
    pdf.write_bytes(body)
    state = tmp_path / "study.doc.json"
    state.write_text('{"sections": []}', encoding="utf-8")
    return store.add_document(id=id, course_id=store.courses()[0].id,
                              pdf_path=pdf, doc_path=state, **options)


def test_cloud_metadata_listing_survives_loss_of_all_local_files(cloud, tmp_path):
    store, client = cloud
    course = store.create_course("Study")
    src, sha = source_pdf(tmp_path)
    material = store.add_material(course.id, "book", src.name, src, sha)
    doc = document(store, tmp_path, failed=[1])
    store.set_document_public(doc.id, True)
    lose_cache(store, tmp_path)
    restarted = CloudWithLocalMetadata(store.db_path, store.config, store.files)
    assert restarted.materials(course.id)[0].to_dict()["available"]
    listed = restarted.documents(course.id)[0].to_dict()
    assert listed["has_pdf"] and listed["can_retry"]
    assert restarted.public_documents()[0]["id"] == doc.id
    assert client.downloads == []  # Lists are metadata-only, even after a restart.
    assert restarted.material(material.id).path.read_bytes() == src.read_bytes()
    restored_doc = restarted.document(doc.id)
    assert restarted.fetch_file(restored_doc.pdf_path).read_bytes() == b"%PDF-old"
    assert len(client.downloads) == 2  # Source and selected PDF; no JSON download.


def test_shared_cloud_material_removed_only_after_last_reference(cloud, tmp_path):
    store, client = cloud
    a, b = store.create_course("A"), store.create_course("B")
    src, sha = source_pdf(tmp_path)
    store.add_material(a.id, "book", "book.pdf", src, sha)
    store.add_material(b.id, "book", "book.pdf", src, sha)
    lose_cache(store, tmp_path)
    assert store.delete_course(a.id)["materials"] == 0
    assert len(client.objects) == 1
    assert store.delete_course(b.id)["materials"] == 1
    assert not client.objects
    assert not client.downloads


def test_failed_revision_upload_keeps_previous_published_document(cloud, tmp_path):
    store, client = cloud
    store.create_course("Study")
    original = document(store, tmp_path)
    store.set_document_public(original.id, True)
    original_keys = set(client.objects)
    client.fail_suffix = ".json"  # PDF uploads, state upload fails before metadata commit.
    with pytest.raises(StorageError):
        document(store, tmp_path, body=b"%PDF-new")
    saved = store.document(original.id)
    assert saved.is_public and saved.pdf_path == original.pdf_path
    assert store.fetch_file(saved.pdf_path).read_bytes() == b"%PDF-old"
    assert set(client.objects) == original_keys


def test_successful_revision_preserves_publication_and_cleans_old_objects(cloud, tmp_path):
    store, client = cloud
    store.create_course("Study")
    old = document(store, tmp_path)
    store.set_document_public(old.id, True)
    old_keys = set(client.objects)
    updated = document(store, tmp_path, body=b"%PDF-new")
    assert updated.is_public and updated.pdf_path != old.pdf_path
    assert store.fetch_file(updated.pdf_path).read_bytes() == b"%PDF-new"
    assert set(client.objects).isdisjoint(old_keys)


def test_uncertain_database_acknowledgement_does_not_delete_committed_files(cloud, tmp_path, monkeypatch):
    store, client = cloud
    store.create_course("Study")
    real_add = LibraryStore.add_document

    def lost_ack(self, **kwargs):
        real_add(self, **kwargs)
        raise StorageError("Connection lost after commit.")

    monkeypatch.setattr(LibraryStore, "add_document", lost_ack)
    with pytest.raises(StorageError):
        document(store, tmp_path)
    saved = store.document("study")
    assert store.files.key(saved.pdf_path) in client.objects
    assert store.files.key(saved.doc_path) in client.objects


def test_delete_document_cleans_cloud_outputs_without_downloading(cloud, tmp_path):
    store, client = cloud
    store.create_course("Study")
    doc = document(store, tmp_path)
    lose_cache(store, tmp_path)
    assert store.delete_document(doc.id)["files"] == 2
    assert not client.objects and not client.downloads


@pytest.mark.parametrize("key", ["../outside.pdf", "/absolute.pdf", "documents/../../outside", "C:/outside.pdf"])
def test_storage_rejects_paths_outside_cache(cloud, key):
    store, _ = cloud
    with pytest.raises(ValueError):
        store.files.local_path(key)


def test_partial_download_is_never_marked_cached(cloud, tmp_path):
    store, client = cloud
    client.objects["materials/test.pdf"] = b"%PDF-content"
    client.truncate = True
    path = store.files.local_path("materials/test.pdf")
    with pytest.raises(StorageError, match="incomplete"):
        store.fetch_file(path)
    assert not path.exists()
    assert not list(path.parent.glob("*.tmp"))
    client.truncate = False
    assert store.fetch_file(path).read_bytes() == b"%PDF-content"


def test_object_size_limit_enforced_before_upload(cloud, tmp_path):
    store, client = cloud
    store.files.max_bytes = 3
    path, _ = source_pdf(tmp_path)
    with pytest.raises(StorageError, match="limit"):
        store.files.put("materials/test.pdf", path)
    assert not client.objects


def test_public_access_restores_only_published_pdf_and_revocation_blocks_fetch(cloud, tmp_path, monkeypatch):
    from dersnotu.api import server

    store, client = cloud
    store.create_course("Study")
    doc = document(store, tmp_path)
    monkeypatch.setattr(server, "library", store)
    monkeypatch.setattr(server.settings, "admin_password", "owner-test-password")
    lose_cache(store, tmp_path)
    with TestClient(server.app) as http:
        assert http.get("/healthz").status_code == 200
        assert http.get("/api/courses").status_code == 401
        assert http.get(f"/api/public/documents/{doc.id}/pdf").status_code == 404
        assert client.downloads == []
        auth = ("admin", "owner-test-password")
        assert http.patch(f"/api/documents/{doc.id}/publication", json={"is_public": True}, auth=auth).status_code == 200
        assert http.get("/api/public/documents").json()[0]["id"] == doc.id
        assert http.get(f"/api/public/documents/{doc.id}/pdf").content == b"%PDF-old"
        assert len(client.downloads) == 1 and client.downloads[0].endswith(".pdf")
        http.patch(f"/api/documents/{doc.id}/publication", json={"is_public": False}, auth=auth)
        assert http.get(f"/api/public/documents/{doc.id}/pdf").status_code == 404
        assert len(client.downloads) == 1


def test_cloud_backend_requires_credentials_instead_of_using_local_metadata():
    config = Settings(_env_file=None, library_backend="postgres")
    with pytest.raises(ValueError, match="DERSNOTU_DATABASE_URL"):
        make_library(config)


def test_cloud_upload_limit_never_exceeds_storage_limit():
    assert Settings(_env_file=None, library_backend="postgres", max_upload_mb=200).max_upload_mb == 50
    assert Settings(_env_file=None, library_backend="sqlite", max_upload_mb=200).max_upload_mb == 200


def test_postgres_session_binds_values_and_never_interpolates_query_text():
    calls = []
    cursor = SimpleNamespace(execute=lambda sql, args: calls.append((sql, args)))
    connection = SimpleNamespace(cursor=lambda: cursor)
    _Session(connection).execute("SELECT * FROM courses WHERE name = ?", ("x'; DROP TABLE courses;--",))
    assert calls == [("SELECT * FROM courses WHERE name = %s", ("x'; DROP TABLE courses;--",))]


def test_postgres_search_escapes_query_punctuation_and_snippet_html(cloud):
    store, _ = cloud
    captured = []

    @contextmanager
    def session():
        def execute(sql, args):
            captured.append((sql, args))
            return SimpleNamespace(fetchall=lambda: [{"document_id": "d", "section": 1,
                "heading": "Bits", "doc_title": "Study", "doc_kind": "note",
                "snip": "<mark>complement</mark><script>bad</script>"}])
        yield SimpleNamespace(execute=execute)

    store._conn = session
    hits = CloudLibraryStore.search(store, "course", "two's complement")
    assert captured[0][1] == ("two & complement:*", "course", 20)
    assert hits[0].snippet == "<mark>complement</mark>&lt;script&gt;bad&lt;/script&gt;"


def test_postgres_url_decodes_credentials_and_requires_verified_tls(monkeypatch):
    from dersnotu import cloud_library

    captured = []
    monkeypatch.setattr(cloud_library.dbapi, "connect", lambda **kwargs: captured.append(kwargs))
    _database_connection(Settings(_env_file=None,
        database_url="postgresql://postgres.project:p%40ss%3Aword@pooler.example.com:5432/postgres"))
    assert captured[0]["user"] == "postgres.project"
    assert captured[0]["password"] == "p@ss:word"
    assert captured[0]["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert captured[0]["ssl_context"].check_hostname


def test_postgres_connection_commits_only_once_for_nested_library_calls(monkeypatch):
    from dersnotu import cloud_library

    calls = []
    cursor = SimpleNamespace(execute=lambda *args: None)
    connection = SimpleNamespace(cursor=lambda: cursor,
        commit=lambda: calls.append("commit"), close=lambda: calls.append("close"))
    monkeypatch.setattr(cloud_library, "_database_connection", lambda config: connection)
    store = CloudLibraryStore.__new__(CloudLibraryStore)
    store.config = Settings(_env_file=None)
    store._transactions = threading.local()
    with store._conn() as outer, store._conn() as inner:
        assert outer is inner
    assert calls == ["commit", "close"]
    calls.clear()
    with pytest.raises(ValueError), store._conn():
        raise ValueError("rollback")
    assert calls == ["close"]


def test_postgres_dictionary_rows_preserve_column_types_and_rowcount():
    cursor = SimpleNamespace(description=[("id",), ("sections",)], rowcount=2,
        fetchone=lambda: ("d", 3), fetchall=lambda: [("d", 3), ("e", 5)])
    rows = _Rows(cursor)
    assert rows.fetchone() == {"id": "d", "sections": 3}
    assert list(rows) == [{"id": "d", "sections": 3}, {"id": "e", "sections": 5}]
    assert rows.rowcount == 2


def test_migration_preflight_is_read_only_and_apply_preserves_ids_and_publication(cloud, tmp_path, monkeypatch):
    from scripts import migrate_library_to_cloud as migration

    target, client = cloud
    source = LibraryStore(tmp_path / "local.sqlite", tmp_path / "local-materials")
    course = source.create_course("Course")
    pdf, sha = source_pdf(tmp_path)
    material = source.add_material(course.id, "book", "book.pdf", pdf, sha)
    doc = source.add_document(id="keep-id", course_id=course.id, book_id=material.id, pdf_path=pdf)
    source.set_document_public(doc.id, True)
    source.index_document(doc.id, course.id, [(0, "Bits", "Two complement examples")])
    original = source.db_path.read_bytes()
    monkeypatch.setattr(migration, "make_library", lambda config: target)
    migration.migrate(source.db_path, source.materials_dir)
    assert not target.courses() and not client.objects
    migration.migrate(source.db_path, source.materials_dir, apply=True)
    assert source.db_path.read_bytes() == original
    assert target.course(course.id).name == course.name
    assert target.material(material.id).path.read_bytes() == pdf.read_bytes()
    migrated = target.document(doc.id)
    assert migrated.is_public and migrated.book_id == material.id
    assert target.fetch_file(migrated.pdf_path).read_bytes() == pdf.read_bytes()
    assert target.search(course.id, "complement")[0].document_id == doc.id


def test_migration_refuses_nonempty_cloud_library_before_upload(cloud, tmp_path, monkeypatch):
    from scripts import migrate_library_to_cloud as migration

    target, client = cloud
    target.create_course("Already there")
    source = LibraryStore(tmp_path / "local.sqlite", tmp_path / "local-materials")
    source.create_course("Source")
    monkeypatch.setattr(migration, "make_library", lambda config: target)
    with pytest.raises(ValueError, match="empty"):
        migration.migrate(source.db_path, source.materials_dir, apply=True)
    assert not client.objects and len(target.courses()) == 1


def test_cloud_job_rejects_transient_upload_without_a_course(cloud, monkeypatch):
    from dersnotu.api import server

    store, _ = cloud
    monkeypatch.setattr(server, "library", store)
    monkeypatch.setattr(server.settings, "library_backend", "postgres")
    with TestClient(server.app) as http:
        response = http.post("/api/jobs", files={
            "lecture": ("slides.pdf", b"%PDF-slides", "application/pdf"),
            "book": ("book.pdf", b"%PDF-book", "application/pdf"),
        })
        assert response.status_code == 400
        assert "course" in response.json()["detail"]
