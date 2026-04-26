"""Document storage router.

Async workflow
--------------
1. ``POST /documents``  — validates metadata, registers a **pending** record,
   enqueues the file-write, and returns immediately with the document ID.
2. ``GET /documents/status?id=<uuid>`` — agent polls this until
   ``status`` reaches ``completed`` or ``failed``.
3. ``PATCH /documents/{doc_id}`` — update metadata and/or replace file content
   (re-queues the write, setting status back to ``pending``).
4. ``DELETE /documents/{doc_id}`` — admin-only; removes the registry record
   and optionally the file on disk.
5. ``GET /documents/find`` — search by path prefix and optional wildcard.
6. ``GET /documents/endpoints`` — self-describing OpenAPI-style endpoint map.

Status values
-------------
pending → processing → completed
                     ↘ failed
"""

from __future__ import annotations

import hmac
import os
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from con_pilot.conductor import ConPilot
from con_pilot.documents import db as _db
from con_pilot.documents.worker import WorkItem, get_worker

router = APIRouter(prefix="/documents", tags=["documents"])

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-. ]+$")
FINAL_STATES = {"completed", "failed"}


# ── Dependencies ───────────────────────────────────────────────────────────────


def _get_pilot() -> ConPilot:
    from con_pilot.app import get_pilot
    return get_pilot()


def _verify_admin_key(
    x_admin_key: str | None = Header(None),
    pilot: ConPilot = Depends(_get_pilot),
) -> ConPilot:
    if not x_admin_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Admin key required. Provide X-Admin-Key header.")
    expected_key = pilot._load_or_generate_key()
    if not hmac.compare_digest(x_admin_key, expected_key):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid admin key")
    return pilot


# ── Models ─────────────────────────────────────────────────────────────────────


class DocumentResponse(BaseModel):
    id: str
    name: str
    file_path: str
    content_type: str
    source: str
    comment: str | None
    created_at: str
    status: str
    error: str | None


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]


class DirectoryResponse(BaseModel):
    path: str
    created: bool


class DirectoryListResponse(BaseModel):
    path: str
    directories: list[str]
    files: list[str]


class DirectoryDeleteResponse(BaseModel):
    path: str
    deleted: bool
    files_removed: int


# ── Helpers ────────────────────────────────────────────────────────────────────


def _resolve_dir(path: str, home: str) -> Path:
    if os.path.isabs(path):
        return Path(path).resolve()
    return (Path(home) / path).resolve()


def _db_path(pilot: ConPilot) -> str:
    return os.path.join(pilot.home, "documents.sqlite3")


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.post("", status_code=status.HTTP_201_CREATED, response_model=DocumentResponse)
async def save_document(
    request: Request,
    name: str,
    document_type: str,
    path: str,
    source: str,
    comment: str | None = None,
    pilot: ConPilot = Depends(_get_pilot),
) -> DocumentResponse:
    """Queue a document write and return immediately with status ``pending``.

    Poll ``GET /documents/status?id=<uuid>`` until status is ``completed``
    or ``failed``.
    """
    if not name or not _SAFE_NAME_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="name must be non-empty and contain only alphanumerics, hyphens, underscores, dots, or spaces",
        )

    target_dir = _resolve_dir(path, pilot.home)
    home = Path(pilot.home).resolve()
    if not str(target_dir).startswith(str(home)) and not os.path.isabs(path):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Relative path escapes CONDUCTOR_HOME")

    file_path = target_dir / name
    content = b"".join([chunk async for chunk in request.stream()])

    db = _db_path(pilot)
    doc_id = _db.register_document(
        db,
        name=name,
        file_path=str(file_path),
        content_type=document_type,
        source=source,
        comment=comment,
        status="pending",
    )
    get_worker().enqueue(WorkItem(doc_id=doc_id, file_path=str(file_path), content=content))

    return DocumentResponse(**_db.get_document(db, doc_id) or {})


@router.get("", response_model=DocumentListResponse)
def list_documents(pilot: ConPilot = Depends(_get_pilot)) -> DocumentListResponse:
    """Return all registered documents, newest first."""
    rows = _db.list_documents(_db_path(pilot))
    return DocumentListResponse(documents=[DocumentResponse(**r) for r in rows])


@router.get("/status", response_model=DocumentResponse)
def get_document_status(id: str, pilot: ConPilot = Depends(_get_pilot)) -> DocumentResponse:
    """Poll document workflow status.

    Poll until ``status`` is ``completed`` or ``failed``.
    """
    record = _db.get_document(_db_path(pilot), id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Document '{id}' not found")
    return DocumentResponse(**record)


@router.get("/find", response_model=DocumentListResponse)
def find_documents(
    path: str,
    pattern: str | None = None,
    pilot: ConPilot = Depends(_get_pilot),
) -> DocumentListResponse:
    """Find documents under a path with an optional shell-style filename wildcard."""
    resolved = str(_resolve_dir(path, pilot.home))
    rows = _db.find_documents(_db_path(pilot), resolved, pattern=pattern)
    return DocumentListResponse(documents=[DocumentResponse(**r) for r in rows])


@router.get("/endpoints")
def describe_endpoints() -> dict:
    """Return an OpenAPI-compatible descriptor of all /documents endpoints."""
    base = "/api/v1/documents"
    doc_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "name": {"type": "string"},
            "file_path": {"type": "string"},
            "content_type": {"type": "string"},
            "source": {"type": "string"},
            "comment": {"type": "string", "nullable": True},
            "created_at": {"type": "string", "format": "date-time"},
            "status": {
                "type": "string",
                "enum": ["pending", "processing", "completed", "failed"],
                "description": "Workflow state. Poll /status until completed or failed.",
            },
            "error": {"type": "string", "nullable": True,
                      "description": "Error detail when status is 'failed'."},
        },
        "required": ["id", "name", "file_path", "content_type", "source", "created_at", "status"],
    }
    list_schema = {"type": "object", "properties": {"documents": {"type": "array", "items": doc_schema}}}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Documents API",
            "description": (
                "Agent-accessible document storage. "
                "POST queues the write and returns immediately; "
                "poll GET /status?id=<uuid> until status is 'completed' or 'failed'."
            ),
            "version": "2.0.0",
        },
        "paths": {
            base: {
                "post": {
                    "summary": "Queue a document write",
                    "operationId": "save_document",
                    "parameters": [
                        {"name": "name", "in": "query", "required": True, "schema": {"type": "string"}, "description": "File name (no path separators)"},
                        {"name": "document_type", "in": "query", "required": True, "schema": {"type": "string"}, "description": "MIME content-type"},
                        {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Target directory (abs or relative to CONDUCTOR_HOME)"},
                        {"name": "source", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Origin identifier"},
                        {"name": "comment", "in": "query", "required": False, "schema": {"type": "string"}},
                    ],
                    "requestBody": {"required": True, "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}}},
                    "responses": {
                        "201": {"description": "Queued — status is 'pending'", "content": {"application/json": {"schema": doc_schema}}},
                        "422": {"description": "Invalid file name"},
                    },
                },
                "get": {
                    "summary": "List all documents",
                    "operationId": "list_documents",
                    "responses": {"200": {"content": {"application/json": {"schema": list_schema}}}},
                },
            },
            f"{base}/status": {
                "get": {
                    "summary": "Poll document status",
                    "description": "Poll until status is 'completed' or 'failed'.",
                    "operationId": "get_document_status",
                    "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string", "format": "uuid"}}],
                    "responses": {
                        "200": {"content": {"application/json": {"schema": doc_schema}}},
                        "404": {"description": "Not found"},
                    },
                },
            },
            f"{base}/find": {
                "get": {
                    "summary": "Find documents by path and wildcard",
                    "operationId": "find_documents",
                    "parameters": [
                        {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}},
                        {"name": "pattern", "in": "query", "required": False, "schema": {"type": "string"}, "description": "Shell wildcard (e.g. *.md)"},
                    ],
                    "responses": {"200": {"content": {"application/json": {"schema": list_schema}}}},
                },
            },
            f"{base}/{{doc_id}}": {
                "get": {
                    "summary": "Get a single document",
                    "operationId": "get_document",
                    "parameters": [{"name": "doc_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}],
                    "responses": {"200": {"content": {"application/json": {"schema": doc_schema}}}, "404": {}},
                },
                "patch": {
                    "summary": "Update document metadata or content",
                    "description": "Metadata applied immediately. Body re-queues the file write (status → pending).",
                    "operationId": "update_document",
                    "parameters": [
                        {"name": "doc_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}},
                        {"name": "document_type", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "comment", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "source", "in": "query", "required": False, "schema": {"type": "string"}},
                    ],
                    "requestBody": {"required": False, "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}}},
                    "responses": {"200": {"content": {"application/json": {"schema": doc_schema}}}, "404": {}},
                },
                "delete": {
                    "summary": "Delete a document (admin only)",
                    "operationId": "delete_document",
                    "parameters": [
                        {"name": "doc_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}},
                        {"name": "delete_file", "in": "query", "required": False, "schema": {"type": "boolean", "default": False}},
                        {"name": "X-Admin-Key", "in": "header", "required": True, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"content": {"application/json": {"schema": doc_schema}}}, "401": {}, "403": {}, "404": {}},
                },
            },
            f"{base}/endpoints": {
                "get": {
                    "summary": "Describe documents API endpoints",
                    "operationId": "describe_endpoints",
                    "responses": {"200": {"description": "OpenAPI-compatible endpoint descriptor"}},
                },
            },
            f"{base}/directories": {
                "post": {
                    "summary": "Create a directory",
                    "operationId": "create_directory",
                    "parameters": [
                        {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Directory path (abs or relative to CONDUCTOR_HOME). Parents are created automatically."},
                    ],
                    "responses": {
                        "201": {"description": "Directory created or already existed", "content": {"application/json": {"schema": {"type": "object", "properties": {"path": {"type": "string"}, "created": {"type": "boolean"}}}}}},
                        "400": {"description": "Path outside CONDUCTOR_HOME"},
                        "422": {"description": "Missing path parameter"},
                    },
                },
                "get": {
                    "summary": "List directory contents",
                    "operationId": "list_directory",
                    "parameters": [
                        {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Directory to list (abs or relative to CONDUCTOR_HOME)"},
                    ],
                    "responses": {
                        "200": {"description": "Immediate children", "content": {"application/json": {"schema": {"type": "object", "properties": {"path": {"type": "string"}, "directories": {"type": "array", "items": {"type": "string"}}, "files": {"type": "array", "items": {"type": "string"}}}}}}},
                        "400": {"description": "Path is not a directory"},
                        "404": {"description": "Path not found"},
                        "422": {"description": "Missing path parameter"},
                    },
                },
                "delete": {
                    "summary": "Delete a directory (admin only)",
                    "operationId": "delete_directory",
                    "parameters": [
                        {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Directory to delete"},
                        {"name": "recursive", "in": "query", "required": False, "schema": {"type": "boolean", "default": False}, "description": "Remove all contents recursively"},
                        {"name": "X-Admin-Key", "in": "header", "required": True, "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {"description": "Deleted", "content": {"application/json": {"schema": {"type": "object", "properties": {"path": {"type": "string"}, "deleted": {"type": "boolean"}, "files_removed": {"type": "integer"}}}}}},
                        "400": {"description": "Not a directory or trying to delete CONDUCTOR_HOME root"},
                        "401": {"description": "Missing admin key"},
                        "403": {"description": "Invalid admin key"},
                        "404": {"description": "Directory not found"},
                        "409": {"description": "Directory not empty (recursive not set)"},
                    },
                },
            },
        },
    }


@router.post("/directories", status_code=status.HTTP_201_CREATED, response_model=DirectoryResponse)
def create_directory(
    path: str,
    pilot: ConPilot = Depends(_get_pilot),
) -> DirectoryResponse:
    """Create a directory (and all parents) within CONDUCTOR_HOME.

    Idempotent — succeeds even if the directory already exists.
    ``created`` is ``True`` when the directory was newly made.
    """
    target = _resolve_dir(path, pilot.home)
    home = Path(pilot.home).resolve()
    if not str(target).startswith(str(home)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Path must be within CONDUCTOR_HOME")
    already_exists = target.exists()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Could not create directory: {exc}") from exc
    return DirectoryResponse(path=str(target), created=not already_exists)


@router.get("/directories", response_model=DirectoryListResponse)
def list_directory(
    path: str,
    pilot: ConPilot = Depends(_get_pilot),
) -> DirectoryListResponse:
    """List the immediate subdirectories and files inside a directory."""
    target = _resolve_dir(path, pilot.home)
    if not target.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Directory '{path}' not found")
    if not target.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"'{path}' is not a directory")
    entries = list(target.iterdir())
    directories = sorted(p.name for p in entries if p.is_dir())
    files = sorted(p.name for p in entries if p.is_file())
    return DirectoryListResponse(path=str(target), directories=directories, files=files)


@router.delete("/directories", response_model=DirectoryDeleteResponse)
def delete_directory(
    path: str,
    recursive: bool = False,
    pilot: ConPilot = Depends(_verify_admin_key),
) -> DirectoryDeleteResponse:
    """Delete a directory. Requires ``X-Admin-Key`` header.

    Pass ``recursive=true`` to remove all contents; without it the directory
    must be empty or a ``409 Conflict`` is returned.
    Cannot delete the CONDUCTOR_HOME root.
    """
    target = _resolve_dir(path, pilot.home)
    home = Path(pilot.home).resolve()
    if str(target) == str(home):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Cannot delete CONDUCTOR_HOME root")
    if not target.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Directory '{path}' not found")
    if not target.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"'{path}' is not a directory")
    files_removed = 0
    try:
        if recursive:
            files_removed = sum(1 for p in target.rglob("*") if p.is_file())
            shutil.rmtree(str(target))
        else:
            target.rmdir()
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Could not delete directory: {exc}") from exc
    return DirectoryDeleteResponse(path=str(target), deleted=True, files_removed=files_removed)


@router.get("/{doc_id}", response_model=DocumentResponse)
def get_document(doc_id: str, pilot: ConPilot = Depends(_get_pilot)) -> DocumentResponse:
    """Return a single document record by ID."""
    record = _db.get_document(_db_path(pilot), doc_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Document '{doc_id}' not found")
    return DocumentResponse(**record)


@router.patch("/{doc_id}", response_model=DocumentResponse)
async def update_document(
    request: Request,
    doc_id: str,
    document_type: str | None = None,
    comment: str | None = None,
    source: str | None = None,
    pilot: ConPilot = Depends(_get_pilot),
) -> DocumentResponse:
    """Update document metadata and/or replace file content.

    Metadata changes are applied immediately.  If a request body is provided
    the file write is re-queued and status returns to ``pending`` — poll
    ``GET /status?id=<uuid>`` for completion.
    """
    db = _db_path(pilot)
    record = _db.get_document(db, doc_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Document '{doc_id}' not found")

    body = b"".join([chunk async for chunk in request.stream()])

    if body:
        _db.update_document(db, doc_id, content_type=document_type, comment=comment,
                            source=source, status="pending")
        get_worker().enqueue(WorkItem(doc_id=doc_id, file_path=record["file_path"], content=body))
    else:
        _db.update_document(db, doc_id, content_type=document_type, comment=comment, source=source)

    return DocumentResponse(**_db.get_document(db, doc_id))


@router.delete("/{doc_id}", response_model=DocumentResponse)
def delete_document(
    doc_id: str,
    delete_file: bool = False,
    pilot: ConPilot = Depends(_verify_admin_key),
) -> DocumentResponse:
    """Delete a document record. Requires ``X-Admin-Key`` header."""
    db = _db_path(pilot)
    record = _db.delete_document(db, doc_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Document '{doc_id}' not found")
    if delete_file:
        try:
            Path(record["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return DocumentResponse(**record)
