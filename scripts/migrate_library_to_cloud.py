"""Copy an existing local library to an EMPTY cloud library without deleting it.

Default is a local preflight only. Use --apply after configuring cloud credentials.
Book retrieval indexes are disposable and are rebuilt by the hosted app.
"""

from __future__ import annotations

import argparse
import sqlite3
import uuid
from pathlib import Path

from dersnotu.cloud_library import make_library
from dersnotu.config import Settings

_TABLES = {
    "courses": ("id", "name", "code", "note", "created_at"),
    "materials": ("id", "course_id", "kind", "name", "sha", "size", "pages", "created_at"),
    "documents": (
        "id", "course_id", "lecture_id", "book_id", "exam_id", "kind", "title",
        "pdf_path", "md_path", "doc_path", "html_path", "language", "depth", "extras",
        "backend", "failed", "usage", "duration", "sections", "is_public", "created_at",
    ),
    "doc_fts": ("document_id", "course_id", "section", "heading", "body"),
}
_FILES = ("pdf_path", "md_path", "doc_path", "html_path")


def migrate(library_path: Path, materials_dir: Path, *, apply: bool = False) -> None:
    if not library_path.is_file():
        raise ValueError("Local library database does not exist.")
    with sqlite3.connect(library_path.resolve().as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        rows = {
            table: [dict(row) for row in source.execute(f"SELECT {', '.join(columns)} FROM {table}")]
            for table, columns in _TABLES.items()
        }
    uploads: dict[str, Path] = {}
    for row in rows["materials"]:
        uploads[f"materials/{row['sha']}.pdf"] = materials_dir / f"{row['sha'][:16]}.pdf"
    for row in rows["documents"]:
        revision = uuid.uuid4().hex
        for column in _FILES:
            if row[column]:
                source = Path(row[column])
                key = f"documents/{row['id']}/{revision}/{source.name}"
                uploads[key] = source
                row[column] = key
    config = Settings()
    for path in uploads.values():
        if not path.is_file():
            raise ValueError(f"A referenced local file is missing: {path}")
        if path.stat().st_size > config.s3_max_file_mb * 1024 * 1024:
            raise ValueError(f"File exceeds the {config.s3_max_file_mb} MB cloud limit: {path.name}")
    total = sum(p.stat().st_size for p in uploads.values())
    print(f"Courses: {len(rows['courses'])}; materials: {len(rows['materials'])}; "
          f"documents: {len(rows['documents'])}; unique files: {len(uploads)}; "
          f"file storage: {total / 1024**2:.1f} MB.")
    if not apply:
        print("Preflight passed. No remote writes. Use --apply to copy to an empty cloud library.")
        return
    target = make_library(config.model_copy(update={"library_backend": "postgres"}))
    # Check before file upload and again inside the metadata transaction.
    with target._conn() as session:
        _require_empty(session)
    for key, path in uploads.items():
        target.files.put(key, path)
    with target._conn() as session:
        _require_empty(session)
        for table, columns in _TABLES.items():
            if rows[table]:
                session.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})",
                    [tuple(row[column] for column in columns) for row in rows[table]],
                )
    print("Cloud copy completed. Local database and files were not changed.")


def _require_empty(session):
    for table in _TABLES:
        if session.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]:
            raise ValueError("Target cloud library must be empty. Existing cloud data was not changed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=Path(".cache/library.sqlite"))
    parser.add_argument("--materials", type=Path, default=Path(".cache/materials"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        migrate(args.library, args.materials, apply=args.apply)
    except Exception as exc:
        parser.exit(1, f"Migration stopped: {exc}\n")
