"""SQLite-backed document registry.

Stores metadata for every document saved via the /documents endpoint.
The database file lives at ``$CONDUCTOR_HOME/documents.sqlite3``.

Schema
------
documents
    id           TEXT PRIMARY KEY  — UUIDv4, used as the document's unique key
    name         TEXT NOT NULL     — file name (no directory part)
    file_path    TEXT NOT NULL     — absolute path where the file is stored
    content_type TEXT NOT NULL     — MIME type string (e.g. "text/markdown")
    source       TEXT NOT NULL     — free-form origin identifier (agent name, URL, …)
    comment      TEXT              — optional free-form notes
    created_at   TEXT NOT NULL     — ISO-8601 UTC timestamp
    status       TEXT NOT NULL     — workflow state: pending | processing | completed | failed
    error        TEXT              — error message when status is 'failed'
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path


# ── Public helpers ─────────────────────────────────────────────────────────────

def init_db(db_path: str) -> None:
    """Create the documents table if it does not exist.

    Parameters
    ----------
    db_path:
        Absolute path to the SQLite database file.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                file_path    TEXT NOT NULL,
                content_type TEXT NOT NULL,
                source       TEXT NOT NULL,
                comment      TEXT,
                created_at   TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'completed',
                error        TEXT
            )
        """)
        # Migrate pre-existing tables that lack the new columns.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        if "status" not in existing:
            conn.execute("ALTER TABLE documents ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
        if "error" not in existing:
            conn.execute("ALTER TABLE documents ADD COLUMN error TEXT")


def register_document(
    db_path: str,
    *,
    name: str,
    file_path: str,
    content_type: str,
    source: str,
    comment: str | None,
    status: str = "pending",
) -> str:
    """Insert a document record and return its generated UUID.

    Parameters
    ----------
    db_path:
        Absolute path to the SQLite database file.
    name:
        File name (no directory).
    file_path:
        Absolute path where the file is stored on disk.
    content_type:
        MIME type string (e.g. ``"text/markdown"``).
    source:
        Free-form origin string (agent name, URL, …).
    comment:
        Optional notes.

    Returns
    -------
    str
        The UUIDv4 assigned as the document's primary key.
    """
    doc_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO documents
                (id, name, file_path, content_type, source, comment, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (doc_id, name, file_path, content_type, source, comment, created_at, status),
        )
    return doc_id


def list_documents(db_path: str) -> list[dict]:
    """Return all document records ordered by creation date (newest first).

    Parameters
    ----------
    db_path:
        Absolute path to the SQLite database file.

    Returns
    -------
    list[dict]
        Each dict has keys: id, name, file_path, content_type, source, comment, created_at.
    """
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, name, file_path, content_type, source, comment, created_at, status, error "
            "FROM documents ORDER BY created_at DESC"
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def get_document(db_path: str, doc_id: str) -> dict | None:
    """Return a single document record by ID, or ``None`` if not found.

    Parameters
    ----------
    db_path:
        Absolute path to the SQLite database file.
    doc_id:
        UUID of the document.

    Returns
    -------
    dict | None
    """
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, name, file_path, content_type, source, comment, created_at, status, error "
            "FROM documents WHERE id = ?",
            (doc_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def find_documents(
    db_path: str,
    path_prefix: str,
    pattern: str | None = None,
) -> list[dict]:
    """Return documents whose ``file_path`` starts with ``path_prefix``.

    Parameters
    ----------
    db_path:
        Absolute path to the SQLite database file.
    path_prefix:
        Directory prefix to filter on (e.g. ``/home/user/.conductor/reports``).
        A trailing slash is added automatically so sub-directories are not
        confused with sibling directories that share a common name prefix.
    pattern:
        Optional shell-style wildcard matched against the document **name**
        (not the full path).  Uses ``fnmatch`` semantics — e.g. ``*.md``,
        ``report-*``.  When omitted all files under the path are returned.

    Returns
    -------
    list[dict]
        Matching records ordered by creation date (newest first).
    """
    import fnmatch

    prefix = path_prefix.rstrip("/") + "/"
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, name, file_path, content_type, source, comment, created_at, status, error "
            "FROM documents WHERE file_path LIKE ? ORDER BY created_at DESC",
            (prefix + "%",),
        )
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]

    if pattern:
        rows = [r for r in rows if fnmatch.fnmatch(r["name"], pattern)]

    return rows


def delete_document(db_path: str, doc_id: str) -> dict | None:
    """Delete a document record from the database and return it.

    The file on disk is **not** removed by this function; callers are
    responsible for deleting it when required.

    Parameters
    ----------
    db_path:
        Absolute path to the SQLite database file.
    doc_id:
        UUID of the document to delete.

    Returns
    -------
    dict | None
        The deleted record, or ``None`` if no record with that ID existed.
    """
    record = get_document(db_path, doc_id)
    if record is None:
        return None
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return record


def update_document_status(db_path: str, doc_id: str, new_status: str, *, error: str | None = None) -> None:
    """Update the workflow status (and optional error message) of a document."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE documents SET status = ?, error = ? WHERE id = ?",
            (new_status, error, doc_id),
        )


def update_document(
    db_path: str,
    doc_id: str,
    *,
    content_type: str | None = None,
    comment: str | None = None,
    source: str | None = None,
    status: str | None = None,
) -> None:
    """Update mutable metadata fields on a document record.

    Only non-``None`` arguments are written; omitted fields are unchanged.
    """
    fields: list[str] = []
    values: list[object] = []
    if content_type is not None:
        fields.append("content_type = ?")
        values.append(content_type)
    if comment is not None:
        fields.append("comment = ?")
        values.append(comment)
    if source is not None:
        fields.append("source = ?")
        values.append(source)
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if not fields:
        return
    values.append(doc_id)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE documents SET {', '.join(fields)} WHERE id = ?", values)  # noqa: S608


# ── Internal ───────────────────────────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
