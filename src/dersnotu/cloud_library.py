"""PostgreSQL library metadata and private S3 files for ephemeral app hosts.

Book retrieval still uses disposable SQLite FTS5 indexes locally. Only library
metadata and searchable generated sections move to PostgreSQL.
"""

from __future__ import annotations

import html
import logging
import ssl
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pg8000 import dbapi

from .config import Settings
from .library import (
    _DOC_COLUMNS,
    _SCHEMA,
    Document,
    LibraryStore,
    Material,
    SearchHit,
    _row_to_document,
    _to_fts_query,
)
from .storage import S3Files, StorageError

log = logging.getLogger(__name__)
_PATH_COLUMNS = ("pdf_path", "md_path", "doc_path", "html_path")
_POSTGRES_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS ragademi;
REVOKE ALL ON SCHEMA ragademi FROM PUBLIC;
SET LOCAL search_path TO ragademi;
""" + _SCHEMA.split("CREATE VIRTUAL TABLE", 1)[0] + """
CREATE TABLE IF NOT EXISTS doc_fts (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    section INTEGER NOT NULL,
    heading TEXT NOT NULL,
    body TEXT NOT NULL,
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', heading || ' ' || body)
    ) STORED,
    PRIMARY KEY(document_id, section)
);
CREATE INDEX IF NOT EXISTS doc_fts_search ON doc_fts USING GIN(search_vector);
CREATE INDEX IF NOT EXISTS doc_fts_course ON doc_fts(course_id);
"""


class _Session:
    """Bind the library's parameterized SQL to PostgreSQL's placeholders."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, args=None):
        cursor = self.connection.cursor()
        cursor.execute(sql.replace("?", "%s"), () if args is None else args)
        return _Rows(cursor)

    def executemany(self, sql, rows):
        cursor = self.connection.cursor()
        try:
            cursor.executemany(sql.replace("?", "%s"), rows)
        finally:
            cursor.close()


class _Rows:
    """Present PostgreSQL DB-API rows like the library's SQLite dictionary rows."""

    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def _map(self, row):
        return dict(zip((column[0] for column in self.cursor.description), row, strict=True))

    def fetchone(self):
        row = self.cursor.fetchone()
        return self._map(row) if row is not None else None

    def fetchall(self):
        return [self._map(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


def _database_connection(config: Settings):
    parsed = urlsplit(config.database_url.get_secret_value())
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.username:
        raise ValueError("DERSNOTU_DATABASE_URL must be a PostgreSQL connection URL.")
    return dbapi.connect(
        host=parsed.hostname, port=parsed.port or 5432,
        user=unquote(parsed.username), password=unquote(parsed.password or ""),
        database=unquote(parsed.path.removeprefix("/")) or "postgres",
        ssl_context=ssl.create_default_context(), timeout=20,
        application_name="ragademi",
    )


class CloudLibraryStore(LibraryStore):
    def __init__(self, config: Settings, *, files: S3Files | None = None):
        self.config = config
        self._transactions = threading.local()
        self.db_path = config.library_path  # Compatibility; never used as a metadata database.
        self.files = files or S3Files(config)
        self.materials_dir = self.files.local_path("materials")
        self.materials_dir.mkdir(parents=True, exist_ok=True)
        with self._conn() as session:
            session.execute(_POSTGRES_SCHEMA)
            for name, decl in _DOC_COLUMNS:
                session.execute(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS {name} {decl}")

    @contextmanager
    def _conn(self):
        if current := getattr(self._transactions, "session", None):
            yield current
            return
        connection = None
        try:
            connection = _database_connection(self.config)
            session = _Session(connection)
            session.execute("SET LOCAL search_path TO ragademi")
            self._transactions.session = session
            yield session
            connection.commit()
        except (dbapi.Error, OSError) as exc:
            raise StorageError("Cloud database is unavailable. Check its connection and project status.") from exc
        finally:
            self._transactions.session = None
            if connection is not None:
                try:
                    connection.close()  # Uncommitted transactions roll back on disconnect.
                except (dbapi.Error, OSError):
                    pass  # Preserve the original error if the socket has already closed.

    @contextmanager
    def _mutation_lock(self, key: str):
        with self._conn() as session:
            # Database locks also cover a brief overlap between app deployments.
            session.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (key,))
            yield

    def blob_path(self, sha: str) -> Path:
        return self.files.local_path(f"materials/{sha}.pdf")

    def add_material(self, course_id, kind, name, src, sha, pages=0) -> Material:
        with self._mutation_lock("material:" + sha):
            return super().add_material(course_id, kind, name, src, sha, pages)

    def _store_material(self, src: Path, sha: str) -> Path:
        path = self.blob_path(sha)
        # Content-addressed blobs can be shared by multiple courses.
        with self._conn() as session:
            exists = session.execute("SELECT 1 FROM materials WHERE sha = ? LIMIT 1", (sha,)).fetchone()
        if not exists:
            self.files.put(self.files.key(path), src)
        return self.files.fetch(path)

    def material(self, material_id: str) -> Material:
        material = super().material(material_id)
        self.fetch_file(material.path)
        return material

    def _row_to_material(self, row) -> Material:
        return replace(super()._row_to_material(row), stored_remotely=True)

    def delete_material(self, material_id: str) -> dict[str, int]:
        # Deletion needs metadata, not a download of the source PDF.
        material = super().material(material_id)
        with self._conn() as session:
            session.execute("DELETE FROM materials WHERE id = ?", (material_id,))
        return {"files": self._collect_garbage([material.sha])}

    def _collect_garbage(self, shas: list[str]) -> int:
        deleted = 0
        for sha in sorted(set(shas)):
            with self._mutation_lock("material:" + sha), self._conn() as session:
                if not session.execute("SELECT COUNT(*) AS n FROM materials WHERE sha = ?", (sha,)).fetchone()["n"]:
                    self.files.delete(self.blob_path(sha))
                    deleted += 1
        return deleted

    def add_document(self, **kw) -> Document:
        document_id = kw.get("id") or uuid.uuid4().hex[:12]
        with self._mutation_lock("document:" + document_id):
            document, old_paths = self._replace_document(document_id, kw)
        # Delete old files only after the metadata commit is acknowledged.
        self._cleanup(old_paths)
        return document

    def _replace_document(self, document_id, kw):
        revision = uuid.uuid4().hex
        with self._conn() as session:
            previous = session.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        uploaded: list[Path] = []
        kw = {**kw, "id": document_id}
        try:
            for column in _PATH_COLUMNS:
                if source := kw.get(column):
                    source = Path(source)
                    key = f"documents/{document_id}/{revision}/{source.name}"
                    self.files.put(key, source)
                    uploaded.append(self.files.local_path(key))
                    kw[column] = Path(key)
            # Commit metadata only after every new file is durable. A failed
            # retry leaves the previous document and publication untouched.
        except Exception:
            self._cleanup(uploaded)
            raise
        # A database commit can succeed even if its acknowledgement is lost.
        # Never delete uploaded files on a database error: retain any orphan
        # rather than deleting files that a committed row might reference.
        document = super().add_document(**kw)
        return document, self._output_paths([previous]) if previous else []

    def set_document_public(self, document_id: str, is_public: bool) -> Document:
        with self._mutation_lock("document:" + document_id):
            return super().set_document_public(document_id, is_public)

    def delete_document(self, document_id: str) -> dict[str, int]:
        with self._mutation_lock("document:" + document_id):
            document = self.document(document_id)
            with self._conn() as session:
                session.execute("DELETE FROM doc_fts WHERE document_id = ?", (document_id,))
                session.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        return {"files": self._delete_files([
            path for path in (document.pdf_path, document.md_path, document.doc_path, document.html_path)
            if path
        ])}

    def _cleanup(self, paths: list[Path]) -> None:
        for path in paths:
            try:
                self.files.delete(path)
            except StorageError:
                log.warning("An unreferenced cloud file could not be removed; retry storage cleanup later.")

    def _row_to_document(self, row) -> Document:
        mapped = dict(row)
        formats = []
        for column in _PATH_COLUMNS:
            if mapped.get(column):
                formats.append(column.removesuffix("_path"))
                mapped[column] = self.files.local_path(mapped[column])
        return replace(_row_to_document(mapped), stored_formats=tuple(formats))

    def fetch_file(self, path: Path | None) -> Path | None:
        return self.files.fetch(path)

    def _file_available(self, ref: str | None) -> bool:
        return bool(ref)  # A committed reference is written only after a successful upload.

    def _output_paths(self, rows) -> list[Path]:
        return [self.files.local_path(row[k]) for row in rows for k in _PATH_COLUMNS if row[k]]

    def _delete_files(self, paths: list[Path]) -> int:
        for path in set(paths):
            self.files.delete(path)
        return len(set(paths))

    def search(self, course_id: str, query: str, limit: int = 20) -> list[SearchHit]:
        expression = _to_fts_query(query)
        if not expression:
            return []
        # Same AND/prefix behavior as the local library's search box.
        terms = expression.replace('"', "").split(" AND ")
        expression = " & ".join(t[:-1] + ":*" if t.endswith("*") else t for t in terms)
        with self._conn() as session:
            rows = session.execute(
                "SELECT f.document_id, f.section, f.heading, d.title AS doc_title,"
                " d.kind AS doc_kind, ts_headline('simple', f.body, q.query,"
                " 'StartSel=<mark>, StopSel=</mark>, MaxWords=25, MinWords=10') AS snip"
                " FROM doc_fts f JOIN documents d ON d.id = f.document_id"
                " CROSS JOIN to_tsquery('simple', ?) AS q(query)"
                " WHERE f.course_id = ? AND f.search_vector @@ q.query"
                " ORDER BY ts_rank(f.search_vector, q.query) DESC LIMIT ?",
                (expression, course_id, limit),
            ).fetchall()
        return [SearchHit(
            document_id=r["document_id"], document_title=r["doc_title"],
            document_kind=r["doc_kind"], section=r["section"], heading=r["heading"],
            snippet=html.escape(r["snip"]).replace("&lt;mark&gt;", "<mark>")
            .replace("&lt;/mark&gt;", "</mark>"),
        ) for r in rows]


def make_library(config: Settings) -> LibraryStore:
    if config.library_backend == "sqlite":
        return LibraryStore(config.library_path, config.materials_dir)
    required = ("database_url", "s3_endpoint", "s3_region", "s3_bucket", "s3_access_key", "s3_secret_key")
    missing = [name for name in required if not getattr(config, name)]
    if missing:
        raise ValueError("Cloud library requires environment variables: " + ", ".join(
            "DERSNOTU_" + name.upper() for name in missing
        ))
    return CloudLibraryStore(config)
