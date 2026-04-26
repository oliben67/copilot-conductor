"""Async document write queue.

The worker runs as a long-lived asyncio task for the lifetime of the
application.  The ``POST /documents`` and ``PATCH /documents/{id}`` endpoints
enqueue :class:`WorkItem` instances; the worker dequeues them, writes the
file to disk, and updates the document's status in the SQLite registry.

Lifecycle
---------
``init_worker(db_path)`` is called once during app startup and returns the
singleton :class:`DocumentWorker`.  ``get_worker()`` can then be called from
anywhere to obtain the same instance.  Call ``await worker.stop()`` on
shutdown.

Status progression
------------------
pending → processing → completed
                     ↘ failed (with error message)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import aiofiles

from con_pilot.documents import db as _db
from con_pilot.logger import app_logger

log = app_logger.bind(module=__name__)

# Sentinel used to signal the worker loop to exit cleanly.
_STOP = object()


@dataclass
class WorkItem:
    """A single file-write operation to be processed by the worker."""

    doc_id: str
    file_path: str
    content: bytes


class DocumentWorker:
    """Async queue-backed worker that writes document files to disk."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._queue: asyncio.Queue[WorkItem | object] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    # ── Public API ─────────────────────────────────────────────────────────

    def enqueue(self, item: WorkItem) -> None:
        """Put a work item on the queue (non-blocking, thread-safe)."""
        self._queue.put_nowait(item)

    async def start(self) -> None:
        """Start the background processing loop."""
        self._task = asyncio.create_task(self._run(), name="documents-worker")
        log.info("DocumentWorker started")

    async def stop(self) -> None:
        """Signal the worker to finish the current item and exit."""
        if self._task is None:
            return
        self._queue.put_nowait(_STOP)
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None
        log.info("DocumentWorker stopped")

    # ── Internal loop ──────────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                break
            assert isinstance(item, WorkItem)
            await self._process(item)
            self._queue.task_done()

    async def _process(self, item: WorkItem) -> None:
        _db.update_document_status(self._db_path, item.doc_id, "processing")
        try:
            from pathlib import Path

            Path(item.file_path).parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(item.file_path, "wb") as f:
                await f.write(item.content)
            _db.update_document_status(self._db_path, item.doc_id, "completed")
            log.debug("Document written", doc_id=item.doc_id, path=item.file_path)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            log.error("Document write failed", doc_id=item.doc_id, error=msg)
            _db.update_document_status(self._db_path, item.doc_id, "failed", error=msg)


# ── Module-level singleton ─────────────────────────────────────────────────────

_instance: DocumentWorker | None = None


def init_worker(db_path: str) -> DocumentWorker:
    """Create the module-level singleton.  Call once at app startup."""
    global _instance
    _instance = DocumentWorker(db_path)
    return _instance


def get_worker() -> DocumentWorker:
    """Return the singleton.  Raises ``RuntimeError`` if not yet initialised."""
    if _instance is None:
        raise RuntimeError("DocumentWorker not initialised — call init_worker() first")
    return _instance
