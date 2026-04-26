"""Tests for all /documents endpoints.

Covers:
    POST   /documents               — save (queue write)
    GET    /documents               — list
    GET    /documents/status        — poll status
    GET    /documents/find          — find by path + pattern
    GET    /documents/endpoints     — self-descriptor
    GET    /documents/{id}          — detail
    PATCH  /documents/{id}          — update metadata / re-queue write
    DELETE /documents/{id}          — admin delete (record only + delete_file)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from con_pilot.documents import db as _db
from con_pilot.documents.router import FINAL_STATES, router
from con_pilot.documents.worker import DocumentWorker, WorkItem, init_worker

# ── Constants ──────────────────────────────────────────────────────────────────

_ADMIN_KEY = "test-admin-key-abc123"
_BASE = "/documents"


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated CONDUCTOR_HOME with a real documents DB and a stub ConPilot."""
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def db_path(home: Path) -> str:
    path = str(home / "documents.sqlite3")
    _db.init_db(path)
    return path


@pytest.fixture()
def mock_pilot(home: Path, db_path: str) -> MagicMock:
    pilot = MagicMock()
    pilot.home = str(home)
    pilot._load_or_generate_key.return_value = _ADMIN_KEY
    return pilot


@pytest.fixture()
def mock_worker() -> MagicMock:
    worker = MagicMock(spec=DocumentWorker)
    worker.enqueue = MagicMock()
    return worker


@pytest.fixture()
def app(mock_pilot: MagicMock, mock_worker: MagicMock) -> FastAPI:
    from con_pilot.documents import router as router_module

    application = FastAPI()
    application.include_router(router)

    # Override pilot and worker dependencies
    from con_pilot.documents.router import _get_pilot
    application.dependency_overrides[_get_pilot] = lambda: mock_pilot

    # Patch get_worker used in route handlers
    patcher = patch.object(router_module, "get_worker", return_value=mock_worker)
    patcher.start()
    yield application
    patcher.stop()
    application.dependency_overrides.clear()


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _seed_document(db_path: str, *, name: str = "report.md",
                   path: str | None = None, home: str = "/tmp",
                   status: str = "completed", source: str = "conductor",
                   comment: str | None = None) -> str:
    """Insert a document record directly into the DB and return its ID."""
    file_path = path or f"{home}/docs/{name}"
    doc_id = _db.register_document(
        db_path,
        name=name,
        file_path=file_path,
        content_type="text/markdown",
        source=source,
        comment=comment,
        status=status,
    )
    if status != "pending":
        _db.update_document_status(db_path, doc_id, status)
    return doc_id


# ── POST /documents ────────────────────────────────────────────────────────────


class TestSaveDocument:
    def test_returns_201_and_pending_status(self, client: TestClient, home: Path) -> None:
        target = home / "docs"
        target.mkdir()
        resp = client.post(
            f"{_BASE}?name=hello.md&document_type=text/markdown&path={target}&source=agent",
            content=b"# Hello",
            headers={"Content-Type": "text/markdown"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "pending"
        assert data["name"] == "hello.md"
        assert data["source"] == "agent"
        assert "id" in data

    def test_enqueues_work_item(self, client: TestClient, home: Path, mock_worker: MagicMock) -> None:
        target = home / "docs"
        target.mkdir()
        client.post(
            f"{_BASE}?name=work.md&document_type=text/markdown&path={target}&source=bot",
            content=b"content",
        )
        mock_worker.enqueue.assert_called_once()
        item = mock_worker.enqueue.call_args[0][0]
        assert isinstance(item, WorkItem)
        assert item.content == b"content"
        assert item.file_path.endswith("work.md")

    def test_with_comment(self, client: TestClient, home: Path) -> None:
        target = home / "docs"
        target.mkdir()
        resp = client.post(
            f"{_BASE}?name=noted.md&document_type=text/markdown&path={target}"
            "&source=agent&comment=my+note",
            content=b"body",
        )
        assert resp.status_code == 201
        assert resp.json()["comment"] == "my note"

    def test_empty_body_accepted(self, client: TestClient, home: Path) -> None:
        target = home / "docs"
        target.mkdir()
        resp = client.post(
            f"{_BASE}?name=empty.md&document_type=text/markdown&path={target}&source=agent",
            content=b"",
        )
        assert resp.status_code == 201

    def test_absolute_path_accepted(self, client: TestClient, home: Path) -> None:
        target = home / "abs"
        target.mkdir()
        resp = client.post(
            f"{_BASE}?name=abs.md&document_type=text/markdown&path={target}&source=agent",
            content=b"data",
        )
        assert resp.status_code == 201
        assert resp.json()["file_path"].endswith("abs.md")

    def test_name_with_invalid_chars_returns_422(self, client: TestClient, home: Path) -> None:
        target = home / "docs"
        target.mkdir()
        resp = client.post(
            f"{_BASE}?name=../evil.md&document_type=text/markdown&path={target}&source=agent",
            content=b"x",
        )
        assert resp.status_code == 422

    def test_empty_name_returns_422(self, client: TestClient, home: Path) -> None:
        target = home / "docs"
        target.mkdir()
        resp = client.post(
            f"{_BASE}?name=&document_type=text/markdown&path={target}&source=agent",
            content=b"x",
        )
        assert resp.status_code == 422

    def test_name_with_slash_returns_422(self, client: TestClient, home: Path) -> None:
        target = home / "docs"
        target.mkdir()
        resp = client.post(
            f"{_BASE}?name=sub/dir.md&document_type=text/markdown&path={target}&source=agent",
            content=b"x",
        )
        assert resp.status_code == 422

    def test_missing_required_param_returns_422(self, client: TestClient, home: Path) -> None:
        # missing 'name'
        resp = client.post(
            f"{_BASE}?document_type=text/markdown&path={home}&source=agent",
            content=b"x",
        )
        assert resp.status_code == 422

    def test_record_persisted_in_db(self, client: TestClient, home: Path, db_path: str) -> None:
        target = home / "docs"
        target.mkdir()
        resp = client.post(
            f"{_BASE}?name=persisted.md&document_type=text/markdown&path={target}&source=agent",
            content=b"stored",
        )
        doc_id = resp.json()["id"]
        record = _db.get_document(db_path, doc_id)
        assert record is not None
        assert record["name"] == "persisted.md"

    def test_binary_content_accepted(self, client: TestClient, home: Path) -> None:
        target = home / "bin"
        target.mkdir()
        resp = client.post(
            f"{_BASE}?name=img.png&document_type=image/png&path={target}&source=agent",
            content=bytes(range(256)),
            headers={"Content-Type": "image/png"},
        )
        assert resp.status_code == 201

    def test_response_has_all_fields(self, client: TestClient, home: Path) -> None:
        target = home / "docs"
        target.mkdir()
        resp = client.post(
            f"{_BASE}?name=full.md&document_type=text/markdown&path={target}&source=agent",
            content=b"x",
        )
        data = resp.json()
        for field in ("id", "name", "file_path", "content_type", "source", "comment",
                      "created_at", "status", "error"):
            assert field in data, f"missing field: {field}"


# ── GET /documents ─────────────────────────────────────────────────────────────


class TestListDocuments:
    def test_empty_list(self, client: TestClient) -> None:
        resp = client.get(_BASE)
        assert resp.status_code == 200
        assert resp.json() == {"documents": []}

    def test_returns_all_documents(self, client: TestClient, db_path: str, home: Path) -> None:
        _seed_document(db_path, name="a.md", home=str(home))
        _seed_document(db_path, name="b.md", home=str(home))
        resp = client.get(_BASE)
        assert resp.status_code == 200
        names = {d["name"] for d in resp.json()["documents"]}
        assert names == {"a.md", "b.md"}

    def test_ordered_newest_first(self, client: TestClient, db_path: str, home: Path) -> None:
        id1 = _seed_document(db_path, name="first.md", home=str(home))
        id2 = _seed_document(db_path, name="second.md", home=str(home))
        resp = client.get(_BASE)
        docs = resp.json()["documents"]
        # newest is last inserted (second) due to timestamp ordering
        ids = [d["id"] for d in docs]
        assert ids.index(id2) < ids.index(id1) or ids[0] == id2

    def test_response_shape(self, client: TestClient, db_path: str, home: Path) -> None:
        _seed_document(db_path, home=str(home))
        resp = client.get(_BASE)
        doc = resp.json()["documents"][0]
        for field in ("id", "name", "file_path", "content_type", "source", "created_at", "status"):
            assert field in doc

    def test_documents_key_is_list(self, client: TestClient) -> None:
        resp = client.get(_BASE)
        assert isinstance(resp.json()["documents"], list)


# ── GET /documents/status ──────────────────────────────────────────────────────


class TestGetDocumentStatus:
    def test_returns_pending_status(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, status="pending", home=str(home))
        resp = client.get(f"{_BASE}/status?id={doc_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

    def test_returns_completed_status(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, status="completed", home=str(home))
        resp = client.get(f"{_BASE}/status?id={doc_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    def test_returns_failed_status_with_error(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, status="pending", home=str(home))
        _db.update_document_status(db_path, doc_id, "failed", error="disk full")
        resp = client.get(f"{_BASE}/status?id={doc_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "failed"
        assert data["error"] == "disk full"

    def test_returns_processing_status(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, status="pending", home=str(home))
        _db.update_document_status(db_path, doc_id, "processing")
        resp = client.get(f"{_BASE}/status?id={doc_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "processing"

    def test_unknown_id_returns_404(self, client: TestClient) -> None:
        resp = client.get(f"{_BASE}/status?id=00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    def test_missing_id_param_returns_422(self, client: TestClient) -> None:
        resp = client.get(f"{_BASE}/status")
        assert resp.status_code == 422

    def test_final_states_constant(self) -> None:
        assert "completed" in FINAL_STATES
        assert "failed" in FINAL_STATES
        assert "pending" not in FINAL_STATES
        assert "processing" not in FINAL_STATES


# ── GET /documents/find ────────────────────────────────────────────────────────


class TestFindDocuments:
    def test_finds_documents_under_path(self, client: TestClient, db_path: str, home: Path) -> None:
        subdir = str(home / "reports")
        _seed_document(db_path, name="r1.md", path=f"{subdir}/r1.md", home=str(home))
        _seed_document(db_path, name="r2.md", path=f"{subdir}/r2.md", home=str(home))
        _seed_document(db_path, name="other.md", path=f"{home}/other/other.md", home=str(home))
        resp = client.get(f"{_BASE}/find?path={subdir}")
        assert resp.status_code == 200
        names = {d["name"] for d in resp.json()["documents"]}
        assert names == {"r1.md", "r2.md"}

    def test_find_with_glob_pattern(self, client: TestClient, db_path: str, home: Path) -> None:
        subdir = str(home / "reports")
        _seed_document(db_path, name="report-2026.md", path=f"{subdir}/report-2026.md", home=str(home))
        _seed_document(db_path, name="data.json", path=f"{subdir}/data.json", home=str(home))
        resp = client.get(f"{_BASE}/find?path={subdir}&pattern=*.md")
        assert resp.status_code == 200
        names = {d["name"] for d in resp.json()["documents"]}
        assert "report-2026.md" in names
        assert "data.json" not in names

    def test_find_empty_dir_returns_empty(self, client: TestClient, home: Path) -> None:
        resp = client.get(f"{_BASE}/find?path={home}/nonexistent")
        assert resp.status_code == 200
        assert resp.json()["documents"] == []

    def test_find_without_pattern_returns_all(self, client: TestClient, db_path: str, home: Path) -> None:
        subdir = str(home / "mix")
        _seed_document(db_path, name="a.md", path=f"{subdir}/a.md", home=str(home))
        _seed_document(db_path, name="b.json", path=f"{subdir}/b.json", home=str(home))
        resp = client.get(f"{_BASE}/find?path={subdir}")
        names = {d["name"] for d in resp.json()["documents"]}
        assert names == {"a.md", "b.json"}

    def test_find_pattern_no_match_returns_empty(self, client: TestClient, db_path: str, home: Path) -> None:
        subdir = str(home / "docs2")
        _seed_document(db_path, name="file.txt", path=f"{subdir}/file.txt", home=str(home))
        resp = client.get(f"{_BASE}/find?path={subdir}&pattern=*.md")
        assert resp.json()["documents"] == []

    def test_find_missing_path_param_returns_422(self, client: TestClient) -> None:
        resp = client.get(f"{_BASE}/find")
        assert resp.status_code == 422

    def test_find_relative_path_resolved_against_home(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        subdir = home / "rel-reports"
        subdir.mkdir()
        _seed_document(db_path, name="rel.md", path=str(subdir / "rel.md"), home=str(home))
        resp = client.get(f"{_BASE}/find?path=rel-reports")
        assert resp.status_code == 200
        assert any(d["name"] == "rel.md" for d in resp.json()["documents"])

    def test_find_does_not_cross_into_sibling_dir(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        subdir_a = str(home / "dir-a")
        subdir_ab = str(home / "dir-ab")  # sibling, not sub-directory
        _seed_document(db_path, name="sibling.md", path=f"{subdir_ab}/sibling.md", home=str(home))
        resp = client.get(f"{_BASE}/find?path={subdir_a}")
        assert all(d["name"] != "sibling.md" for d in resp.json()["documents"])


# ── GET /documents/endpoints ───────────────────────────────────────────────────


class TestDescribeEndpoints:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get(f"{_BASE}/endpoints")
        assert resp.status_code == 200

    def test_openapi_version(self, client: TestClient) -> None:
        data = client.get(f"{_BASE}/endpoints").json()
        assert data["openapi"] == "3.1.0"

    def test_has_info_block(self, client: TestClient) -> None:
        data = client.get(f"{_BASE}/endpoints").json()
        assert "title" in data["info"]
        assert "version" in data["info"]

    def test_paths_present(self, client: TestClient) -> None:
        data = client.get(f"{_BASE}/endpoints").json()
        paths = data["paths"]
        assert "/api/v1/documents" in paths
        assert "/api/v1/documents/status" in paths
        assert "/api/v1/documents/find" in paths
        assert "/api/v1/documents/{doc_id}" in paths
        assert "/api/v1/documents/endpoints" in paths

    def test_post_and_get_on_collection_path(self, client: TestClient) -> None:
        data = client.get(f"{_BASE}/endpoints").json()
        collection = data["paths"]["/api/v1/documents"]
        assert "post" in collection
        assert "get" in collection

    def test_detail_path_has_get_patch_delete(self, client: TestClient) -> None:
        data = client.get(f"{_BASE}/endpoints").json()
        detail = data["paths"]["/api/v1/documents/{doc_id}"]
        assert "get" in detail
        assert "patch" in detail
        assert "delete" in detail

    def test_no_auth_required(self, client: TestClient) -> None:
        # No Authorization or X-Admin-Key header needed
        resp = client.get(f"{_BASE}/endpoints")
        assert resp.status_code == 200

    def test_status_enum_includes_all_states(self, client: TestClient) -> None:
        data = client.get(f"{_BASE}/endpoints").json()
        post_resp = data["paths"]["/api/v1/documents"]["post"]["responses"]["201"]
        schema = post_resp["content"]["application/json"]["schema"]
        status_enum = schema["properties"]["status"]["enum"]
        assert set(status_enum) == {"pending", "processing", "completed", "failed"}


# ── GET /documents/{id} ────────────────────────────────────────────────────────


class TestGetDocument:
    def test_returns_document(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, name="detail.md", home=str(home))
        resp = client.get(f"{_BASE}/{doc_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == doc_id
        assert data["name"] == "detail.md"

    def test_unknown_id_returns_404(self, client: TestClient) -> None:
        resp = client.get(f"{_BASE}/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    def test_status_field_present(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, status="completed", home=str(home))
        resp = client.get(f"{_BASE}/{doc_id}")
        assert resp.json()["status"] == "completed"

    def test_comment_null_when_not_set(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        resp = client.get(f"{_BASE}/{doc_id}")
        assert resp.json()["comment"] is None

    def test_comment_returned_when_set(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, comment="my note", home=str(home))
        resp = client.get(f"{_BASE}/{doc_id}")
        assert resp.json()["comment"] == "my note"

    def test_error_null_when_no_error(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, status="completed", home=str(home))
        resp = client.get(f"{_BASE}/{doc_id}")
        assert resp.json()["error"] is None

    def test_error_returned_when_failed(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, status="pending", home=str(home))
        _db.update_document_status(db_path, doc_id, "failed", error="write error")
        resp = client.get(f"{_BASE}/{doc_id}")
        assert resp.json()["error"] == "write error"

    def test_content_type_persisted(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _db.register_document(
            db_path, name="img.png", file_path=str(home / "img.png"),
            content_type="image/png", source="agent", comment=None, status="completed",
        )
        resp = client.get(f"{_BASE}/{doc_id}")
        assert resp.json()["content_type"] == "image/png"


# ── PATCH /documents/{id} ─────────────────────────────────────────────────────


class TestUpdateDocument:
    def test_update_comment_only(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        resp = client.patch(f"{_BASE}/{doc_id}?comment=revised")
        assert resp.status_code == 200
        assert resp.json()["comment"] == "revised"

    def test_update_source(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        resp = client.patch(f"{_BASE}/{doc_id}?source=new-agent")
        assert resp.status_code == 200
        assert resp.json()["source"] == "new-agent"

    def test_update_document_type(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        resp = client.patch(f"{_BASE}/{doc_id}?document_type=application/json")
        assert resp.status_code == 200
        assert resp.json()["content_type"] == "application/json"

    def test_update_with_body_requeues_write(
        self, client: TestClient, db_path: str, home: Path, mock_worker: MagicMock
    ) -> None:
        doc_id = _seed_document(db_path, status="completed", home=str(home))
        resp = client.patch(
            f"{_BASE}/{doc_id}",
            content=b"new content",
            headers={"Content-Type": "text/markdown"},
        )
        assert resp.status_code == 200
        mock_worker.enqueue.assert_called_once()
        item = mock_worker.enqueue.call_args[0][0]
        assert item.content == b"new content"
        assert item.doc_id == doc_id

    def test_update_with_body_sets_pending(
        self, client: TestClient, db_path: str, home: Path, mock_worker: MagicMock
    ) -> None:
        doc_id = _seed_document(db_path, status="completed", home=str(home))
        resp = client.patch(f"{_BASE}/{doc_id}", content=b"new")
        assert resp.status_code == 200
        # status is reset to pending before enqueue
        record = _db.get_document(db_path, doc_id)
        assert record["status"] == "pending"

    def test_update_without_body_does_not_enqueue(
        self, client: TestClient, db_path: str, home: Path, mock_worker: MagicMock
    ) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        client.patch(f"{_BASE}/{doc_id}?comment=no-body")
        mock_worker.enqueue.assert_not_called()

    def test_unknown_id_returns_404(self, client: TestClient) -> None:
        resp = client.patch(f"{_BASE}/00000000-0000-0000-0000-000000000000?comment=x")
        assert resp.status_code == 404

    def test_update_multiple_fields_at_once(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        resp = client.patch(
            f"{_BASE}/{doc_id}?comment=multi&source=new-src&document_type=application/json"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["comment"] == "multi"
        assert data["source"] == "new-src"
        assert data["content_type"] == "application/json"

    def test_empty_patch_returns_document_unchanged(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        doc_id = _seed_document(db_path, name="stable.md", comment="original", home=str(home))
        resp = client.patch(f"{_BASE}/{doc_id}")
        assert resp.status_code == 200
        assert resp.json()["comment"] == "original"


# ── DELETE /documents/{id} ────────────────────────────────────────────────────


class TestDeleteDocument:
    def test_delete_requires_admin_key(self, client: TestClient, db_path: str, home: Path) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        resp = client.delete(f"{_BASE}/{doc_id}")
        assert resp.status_code == 401

    def test_delete_with_wrong_key_returns_403(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        resp = client.delete(f"{_BASE}/{doc_id}", headers={"X-Admin-Key": "wrong-key"})
        assert resp.status_code == 403

    def test_delete_with_correct_key_returns_200(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        resp = client.delete(f"{_BASE}/{doc_id}", headers={"X-Admin-Key": _ADMIN_KEY})
        assert resp.status_code == 200

    def test_delete_returns_deleted_record(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        doc_id = _seed_document(db_path, name="gone.md", home=str(home))
        resp = client.delete(f"{_BASE}/{doc_id}", headers={"X-Admin-Key": _ADMIN_KEY})
        data = resp.json()
        assert data["id"] == doc_id
        assert data["name"] == "gone.md"

    def test_delete_removes_record_from_db(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        client.delete(f"{_BASE}/{doc_id}", headers={"X-Admin-Key": _ADMIN_KEY})
        assert _db.get_document(db_path, doc_id) is None

    def test_delete_unknown_id_returns_404(self, client: TestClient) -> None:
        resp = client.delete(
            f"{_BASE}/00000000-0000-0000-0000-000000000000",
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.status_code == 404

    def test_delete_without_delete_file_keeps_file(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        file = home / "keep.md"
        file.write_text("keep me")
        doc_id = _db.register_document(
            db_path, name="keep.md", file_path=str(file),
            content_type="text/markdown", source="test", comment=None, status="completed",
        )
        client.delete(f"{_BASE}/{doc_id}", headers={"X-Admin-Key": _ADMIN_KEY})
        assert file.exists()

    def test_delete_with_delete_file_removes_file(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        file = home / "remove.md"
        file.write_text("delete me")
        doc_id = _db.register_document(
            db_path, name="remove.md", file_path=str(file),
            content_type="text/markdown", source="test", comment=None, status="completed",
        )
        client.delete(
            f"{_BASE}/{doc_id}?delete_file=true",
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert not file.exists()

    def test_delete_file_missing_on_disk_still_removes_record(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        doc_id = _db.register_document(
            db_path, name="phantom.md", file_path=str(home / "phantom.md"),
            content_type="text/markdown", source="test", comment=None, status="completed",
        )
        resp = client.delete(
            f"{_BASE}/{doc_id}?delete_file=true",
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.status_code == 200
        assert _db.get_document(db_path, doc_id) is None

    def test_second_delete_returns_404(
        self, client: TestClient, db_path: str, home: Path
    ) -> None:
        doc_id = _seed_document(db_path, home=str(home))
        client.delete(f"{_BASE}/{doc_id}", headers={"X-Admin-Key": _ADMIN_KEY})
        resp = client.delete(f"{_BASE}/{doc_id}", headers={"X-Admin-Key": _ADMIN_KEY})
        assert resp.status_code == 404


# ── POST/GET/DELETE /documents/directories ─────────────────────────────────────


class TestDirectories:
    _DIR_BASE = f"{_BASE}/directories"

    # ── POST /documents/directories ───────────────────────────────────────────

    def test_create_directory_returns_201(self, client: TestClient, home: Path) -> None:
        resp = client.post(self._DIR_BASE, params={"path": "newdir"})
        assert resp.status_code == 201

    def test_create_directory_sets_created_true_when_new(
        self, client: TestClient, home: Path
    ) -> None:
        resp = client.post(self._DIR_BASE, params={"path": "brandnew"})
        assert resp.json()["created"] is True

    def test_create_directory_sets_created_false_when_already_exists(
        self, client: TestClient, home: Path
    ) -> None:
        (home / "existing").mkdir()
        resp = client.post(self._DIR_BASE, params={"path": "existing"})
        assert resp.status_code == 201
        assert resp.json()["created"] is False

    def test_create_directory_path_in_response_is_absolute(
        self, client: TestClient, home: Path
    ) -> None:
        resp = client.post(self._DIR_BASE, params={"path": "mydir"})
        assert resp.json()["path"] == str(home / "mydir")

    def test_create_directory_creates_parents(self, client: TestClient, home: Path) -> None:
        resp = client.post(self._DIR_BASE, params={"path": "a/b/c"})
        assert resp.status_code == 201
        assert (home / "a" / "b" / "c").is_dir()

    def test_create_directory_missing_path_param_returns_422(
        self, client: TestClient, home: Path
    ) -> None:
        resp = client.post(self._DIR_BASE)
        assert resp.status_code == 422

    def test_create_directory_absolute_path_within_home(
        self, client: TestClient, home: Path
    ) -> None:
        resp = client.post(self._DIR_BASE, params={"path": str(home / "absdir")})
        assert resp.status_code == 201
        assert (home / "absdir").is_dir()

    def test_create_directory_outside_home_returns_400(
        self, client: TestClient, home: Path
    ) -> None:
        # An absolute path that cannot possibly be inside the tmp CONDUCTOR_HOME
        resp = client.post(self._DIR_BASE, params={"path": "/outside_conductor_home"})
        assert resp.status_code == 400

    # ── GET /documents/directories ────────────────────────────────────────────

    def test_list_directory_returns_200(self, client: TestClient, home: Path) -> None:
        d = home / "listed"
        d.mkdir()
        resp = client.get(self._DIR_BASE, params={"path": "listed"})
        assert resp.status_code == 200

    def test_list_directory_empty(self, client: TestClient, home: Path) -> None:
        (home / "empty").mkdir()
        resp = client.get(self._DIR_BASE, params={"path": "empty"})
        data = resp.json()
        assert data["directories"] == []
        assert data["files"] == []

    def test_list_directory_contains_subdirs(self, client: TestClient, home: Path) -> None:
        parent = home / "parent"
        parent.mkdir()
        (parent / "sub1").mkdir()
        (parent / "sub2").mkdir()
        resp = client.get(self._DIR_BASE, params={"path": "parent"})
        assert resp.json()["directories"] == ["sub1", "sub2"]

    def test_list_directory_contains_files(self, client: TestClient, home: Path) -> None:
        d = home / "withfiles"
        d.mkdir()
        (d / "z.txt").write_text("z")
        (d / "a.txt").write_text("a")
        resp = client.get(self._DIR_BASE, params={"path": "withfiles"})
        assert resp.json()["files"] == ["a.txt", "z.txt"]  # sorted

    def test_list_directory_path_in_response_is_absolute(
        self, client: TestClient, home: Path
    ) -> None:
        (home / "abschk").mkdir()
        resp = client.get(self._DIR_BASE, params={"path": "abschk"})
        assert resp.json()["path"] == str(home / "abschk")

    def test_list_directory_not_found_returns_404(
        self, client: TestClient, home: Path
    ) -> None:
        resp = client.get(self._DIR_BASE, params={"path": "ghost"})
        assert resp.status_code == 404

    def test_list_directory_on_file_returns_400(
        self, client: TestClient, home: Path
    ) -> None:
        (home / "afile.txt").write_text("hello")
        resp = client.get(self._DIR_BASE, params={"path": "afile.txt"})
        assert resp.status_code == 400

    def test_list_directory_missing_path_param_returns_422(
        self, client: TestClient, home: Path
    ) -> None:
        resp = client.get(self._DIR_BASE)
        assert resp.status_code == 422

    # ── DELETE /documents/directories ─────────────────────────────────────────

    def test_delete_directory_requires_admin_key(
        self, client: TestClient, home: Path
    ) -> None:
        (home / "todel").mkdir()
        resp = client.delete(self._DIR_BASE, params={"path": "todel"})
        assert resp.status_code == 401

    def test_delete_directory_wrong_key_returns_403(
        self, client: TestClient, home: Path
    ) -> None:
        (home / "todel2").mkdir()
        resp = client.delete(
            self._DIR_BASE, params={"path": "todel2"},
            headers={"X-Admin-Key": "wrong"},
        )
        assert resp.status_code == 403

    def test_delete_empty_directory_returns_200(
        self, client: TestClient, home: Path
    ) -> None:
        (home / "byebye").mkdir()
        resp = client.delete(
            self._DIR_BASE, params={"path": "byebye"},
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.status_code == 200

    def test_delete_directory_deleted_flag_true(
        self, client: TestClient, home: Path
    ) -> None:
        (home / "flagchk").mkdir()
        resp = client.delete(
            self._DIR_BASE, params={"path": "flagchk"},
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.json()["deleted"] is True

    def test_delete_directory_removes_it_from_filesystem(
        self, client: TestClient, home: Path
    ) -> None:
        target = home / "gone"
        target.mkdir()
        client.delete(
            self._DIR_BASE, params={"path": "gone"},
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert not target.exists()

    def test_delete_nonempty_without_recursive_returns_409(
        self, client: TestClient, home: Path
    ) -> None:
        d = home / "nonempty"
        d.mkdir()
        (d / "file.txt").write_text("data")
        resp = client.delete(
            self._DIR_BASE, params={"path": "nonempty"},
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.status_code == 409

    def test_delete_recursive_removes_all_contents(
        self, client: TestClient, home: Path
    ) -> None:
        d = home / "tree"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "file.txt").write_text("x")
        resp = client.delete(
            self._DIR_BASE, params={"path": "tree", "recursive": "true"},
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.status_code == 200
        assert not d.exists()

    def test_delete_recursive_reports_files_removed(
        self, client: TestClient, home: Path
    ) -> None:
        d = home / "counted"
        (d / "sub").mkdir(parents=True)
        (d / "a.txt").write_text("a")
        (d / "sub" / "b.txt").write_text("b")
        resp = client.delete(
            self._DIR_BASE, params={"path": "counted", "recursive": "true"},
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.json()["files_removed"] == 2

    def test_delete_directory_not_found_returns_404(
        self, client: TestClient, home: Path
    ) -> None:
        resp = client.delete(
            self._DIR_BASE, params={"path": "ghost"},
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.status_code == 404

    def test_delete_home_root_returns_400(
        self, client: TestClient, home: Path
    ) -> None:
        resp = client.delete(
            self._DIR_BASE, params={"path": str(home)},
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.status_code == 400

    def test_delete_on_file_returns_400(
        self, client: TestClient, home: Path
    ) -> None:
        (home / "notadir.txt").write_text("data")
        resp = client.delete(
            self._DIR_BASE, params={"path": "notadir.txt"},
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.status_code == 400

    def test_delete_directory_path_in_response_is_absolute(
        self, client: TestClient, home: Path
    ) -> None:
        (home / "resp_path").mkdir()
        resp = client.delete(
            self._DIR_BASE, params={"path": "resp_path"},
            headers={"X-Admin-Key": _ADMIN_KEY},
        )
        assert resp.json()["path"] == str(home / "resp_path")

    # ── describe_endpoints includes /directories ───────────────────────────────

    def test_endpoints_descriptor_includes_directories(
        self, client: TestClient
    ) -> None:
        resp = client.get(f"{_BASE}/endpoints")
        paths = resp.json()["paths"]
        assert any("directories" in k for k in paths)

    def test_endpoints_descriptor_directories_has_post_get_delete(
        self, client: TestClient
    ) -> None:
        resp = client.get(f"{_BASE}/endpoints")
        paths = resp.json()["paths"]
        dir_key = next(k for k in paths if "directories" in k)
        assert "post" in paths[dir_key]
        assert "get" in paths[dir_key]
        assert "delete" in paths[dir_key]


# ── DB unit tests ──────────────────────────────────────────────────────────────


class TestDocumentDB:
    def test_init_db_creates_table(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        conn = __import__("sqlite3").connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "documents" in tables

    def test_init_db_idempotent(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        _db.init_db(db)  # should not raise

    def test_register_returns_uuid(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        doc_id = _db.register_document(
            db, name="f.md", file_path="/tmp/f.md",
            content_type="text/plain", source="t", comment=None,
        )
        import uuid
        uuid.UUID(doc_id)  # raises if not valid UUID

    def test_list_empty(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        assert _db.list_documents(db) == []

    def test_get_nonexistent_returns_none(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        assert _db.get_document(db, "no-such-id") is None

    def test_update_status_to_completed(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        doc_id = _db.register_document(
            db, name="x.md", file_path="/tmp/x.md",
            content_type="text/plain", source="t", comment=None, status="pending",
        )
        _db.update_document_status(db, doc_id, "completed")
        assert _db.get_document(db, doc_id)["status"] == "completed"

    def test_update_status_with_error(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        doc_id = _db.register_document(
            db, name="y.md", file_path="/tmp/y.md",
            content_type="text/plain", source="t", comment=None, status="pending",
        )
        _db.update_document_status(db, doc_id, "failed", error="oops")
        record = _db.get_document(db, doc_id)
        assert record["status"] == "failed"
        assert record["error"] == "oops"

    def test_update_document_metadata(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        doc_id = _db.register_document(
            db, name="m.md", file_path="/tmp/m.md",
            content_type="text/plain", source="old", comment=None,
        )
        _db.update_document(db, doc_id, comment="new comment", source="new-src")
        record = _db.get_document(db, doc_id)
        assert record["comment"] == "new comment"
        assert record["source"] == "new-src"

    def test_update_document_no_fields_is_noop(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        doc_id = _db.register_document(
            db, name="n.md", file_path="/tmp/n.md",
            content_type="text/plain", source="s", comment=None,
        )
        _db.update_document(db, doc_id)  # no kwargs — should not raise

    def test_delete_returns_record(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        doc_id = _db.register_document(
            db, name="d.md", file_path="/tmp/d.md",
            content_type="text/plain", source="s", comment=None,
        )
        deleted = _db.delete_document(db, doc_id)
        assert deleted is not None
        assert deleted["id"] == doc_id

    def test_delete_returns_none_for_missing(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        assert _db.delete_document(db, "ghost") is None

    def test_find_with_prefix(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        _db.register_document(
            db, name="in.md", file_path="/reports/in.md",
            content_type="text/plain", source="s", comment=None,
        )
        _db.register_document(
            db, name="out.md", file_path="/other/out.md",
            content_type="text/plain", source="s", comment=None,
        )
        found = _db.find_documents(db, "/reports")
        assert len(found) == 1
        assert found[0]["name"] == "in.md"

    def test_find_with_pattern(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.sqlite3")
        _db.init_db(db)
        _db.register_document(
            db, name="rpt.md", file_path="/docs/rpt.md",
            content_type="text/plain", source="s", comment=None,
        )
        _db.register_document(
            db, name="data.json", file_path="/docs/data.json",
            content_type="application/json", source="s", comment=None,
        )
        found = _db.find_documents(db, "/docs", pattern="*.md")
        assert len(found) == 1
        assert found[0]["name"] == "rpt.md"

    def test_migrate_adds_status_and_error_columns(self, tmp_path: Path) -> None:
        """Simulates a pre-existing table without status/error columns."""
        import sqlite3 as _sqlite3
        db = str(tmp_path / "legacy.sqlite3")
        conn = _sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE documents ("
            "id TEXT PRIMARY KEY, name TEXT, file_path TEXT, "
            "content_type TEXT, source TEXT, comment TEXT, created_at TEXT)"
        )
        conn.commit()
        conn.close()
        _db.init_db(db)  # migration should add columns without raising
        record = _db.get_document(db, "nonexistent")
        assert record is None  # just verifying no crash


# ── Worker unit tests ──────────────────────────────────────────────────────────


class TestDocumentWorker:
    def test_enqueue_puts_item_on_queue(self, tmp_path: Path) -> None:
        db = str(tmp_path / "w.sqlite3")
        _db.init_db(db)
        worker = DocumentWorker(db)
        item = WorkItem(doc_id="abc", file_path="/tmp/f", content=b"x")
        worker.enqueue(item)
        assert worker._queue.qsize() == 1

    def test_worker_writes_file_and_sets_completed(self, tmp_path: Path) -> None:
        db = str(tmp_path / "w.sqlite3")
        _db.init_db(db)
        doc_id = _db.register_document(
            db, name="w.md", file_path=str(tmp_path / "w.md"),
            content_type="text/plain", source="test", comment=None, status="pending",
        )

        async def _run() -> None:
            worker = DocumentWorker(db)
            await worker.start()
            worker.enqueue(WorkItem(doc_id=doc_id, file_path=str(tmp_path / "w.md"), content=b"hello"))
            await asyncio.sleep(0.2)
            await worker.stop()

        asyncio.run(_run())
        assert (tmp_path / "w.md").read_bytes() == b"hello"
        assert _db.get_document(db, doc_id)["status"] == "completed"

    def test_worker_sets_failed_on_bad_path(self, tmp_path: Path) -> None:
        db = str(tmp_path / "w2.sqlite3")
        _db.init_db(db)
        doc_id = _db.register_document(
            db, name="x.md", file_path="/proc/sys/kernel/nop/x.md",  # unwritable
            content_type="text/plain", source="test", comment=None, status="pending",
        )

        async def _run() -> None:
            worker = DocumentWorker(db)
            await worker.start()
            worker.enqueue(WorkItem(doc_id=doc_id, file_path="/proc/sys/kernel/nop/x.md", content=b"x"))
            await asyncio.sleep(0.3)
            await worker.stop()

        asyncio.run(_run())
        record = _db.get_document(db, doc_id)
        assert record["status"] == "failed"
        assert record["error"] is not None

    def test_worker_stop_without_start_is_safe(self, tmp_path: Path) -> None:
        db = str(tmp_path / "w3.sqlite3")
        _db.init_db(db)
        worker = DocumentWorker(db)

        async def _run() -> None:
            await worker.stop()  # stop without start — should not raise

        asyncio.run(_run())

    def test_worker_processes_multiple_items(self, tmp_path: Path) -> None:
        db = str(tmp_path / "m.sqlite3")
        _db.init_db(db)
        ids = []
        for i in range(5):
            fp = str(tmp_path / f"doc{i}.md")
            doc_id = _db.register_document(
                db, name=f"doc{i}.md", file_path=fp,
                content_type="text/plain", source="test", comment=None, status="pending",
            )
            ids.append((doc_id, fp))

        async def _run() -> None:
            worker = DocumentWorker(db)
            await worker.start()
            for doc_id, fp in ids:
                worker.enqueue(WorkItem(doc_id=doc_id, file_path=fp, content=b"data"))
            await asyncio.sleep(0.5)
            await worker.stop()

        asyncio.run(_run())
        for doc_id, fp in ids:
            assert Path(fp).exists()
            assert _db.get_document(db, doc_id)["status"] == "completed"

    def test_init_worker_sets_global_and_get_worker_returns_it(self, tmp_path: Path) -> None:
        from con_pilot.documents import worker as _worker_module
        db = str(tmp_path / "s.sqlite3")
        _db.init_db(db)
        w = init_worker(db)
        assert _worker_module.get_worker() is w

    def test_init_worker_replaces_previous_singleton(self, tmp_path: Path) -> None:
        db1 = str(tmp_path / "s1.sqlite3")
        db2 = str(tmp_path / "s2.sqlite3")
        _db.init_db(db1)
        _db.init_db(db2)
        w1 = init_worker(db1)
        w2 = init_worker(db2)
        assert w1 is not w2
