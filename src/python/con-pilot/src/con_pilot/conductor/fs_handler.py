"""Concrete SessionFsHandler for Copilot agent sessions.

Implements the ``SessionFsHandler`` protocol (read, write, append, stat, mkdir,
readdir, rm, rename) backed by the real filesystem so that agent sessions can
create and modify files through the standard Copilot SDK tool infrastructure.
"""

from __future__ import annotations

import os
import stat as stat_module
from datetime import UTC, datetime
from pathlib import Path

import aiofiles
import aiofiles.os

from copilot.generated.rpc import (
    Entry,
    EntryType,
    SessionFSAppendFileParams,
    SessionFSExistsParams,
    SessionFSExistsResult,
    SessionFSMkdirParams,
    SessionFSReadFileParams,
    SessionFSReadFileResult,
    SessionFSReaddirParams,
    SessionFSReaddirResult,
    SessionFSReaddirWithTypesParams,
    SessionFSReaddirWithTypesResult,
    SessionFSRenameParams,
    SessionFSRmParams,
    SessionFSStatParams,
    SessionFSStatResult,
    SessionFSWriteFileParams,
)

import structlog

log = structlog.get_logger(__name__)


def _ts(t: float) -> str:
    """Format a POSIX timestamp as ISO-8601 UTC string."""
    return datetime.fromtimestamp(t, tz=UTC).isoformat()


class ConductorFsHandler:
    """Filesystem handler that backs Copilot SDK file-system tool calls.

    All paths received from the SDK are resolved relative to *working_dir*
    when they are not already absolute.  No path traversal outside the
    filesystem root is permitted.
    """

    def __init__(self, working_dir: str) -> None:
        self._cwd = Path(working_dir).resolve()

    # ── Internal helpers ────────────────────────────────────────────────────

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        if not p.is_absolute():
            p = self._cwd / p
        return p.resolve()

    # ── SessionFsHandler protocol ───────────────────────────────────────────

    async def read_file(self, params: SessionFSReadFileParams) -> SessionFSReadFileResult:
        path = self._resolve(params.path)
        log.debug("fs read_file: %s", path)
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            content = await f.read()
        return SessionFSReadFileResult(content=content)

    async def write_file(self, params: SessionFSWriteFileParams) -> None:
        path = self._resolve(params.path)
        log.debug("fs write_file: %s", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(params.content)
        if params.mode is not None:
            os.chmod(path, int(params.mode))

    async def append_file(self, params: SessionFSAppendFileParams) -> None:
        path = self._resolve(params.path)
        log.debug("fs append_file: %s", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "a", encoding="utf-8") as f:
            await f.write(params.content)

    async def exists(self, params: SessionFSExistsParams) -> SessionFSExistsResult:
        path = self._resolve(params.path)
        return SessionFSExistsResult(exists=path.exists())

    async def stat(self, params: SessionFSStatParams) -> SessionFSStatResult:
        path = self._resolve(params.path)
        st = await aiofiles.os.stat(path)
        return SessionFSStatResult(
            is_file=stat_module.S_ISREG(st.st_mode),
            is_directory=stat_module.S_ISDIR(st.st_mode),
            size=float(st.st_size),
            mtime=_ts(st.st_mtime),
            birthtime=_ts(getattr(st, "st_birthtime", st.st_ctime)),
        )

    async def mkdir(self, params: SessionFSMkdirParams) -> None:
        path = self._resolve(params.path)
        log.debug("fs mkdir: %s (recursive=%s)", path, params.recursive)
        path.mkdir(
            parents=bool(params.recursive),
            exist_ok=bool(params.recursive),
            mode=int(params.mode) if params.mode is not None else 0o755,
        )

    async def readdir(self, params: SessionFSReaddirParams) -> SessionFSReaddirResult:
        path = self._resolve(params.path)
        entries = [e.name for e in sorted(path.iterdir(), key=lambda x: x.name)]
        return SessionFSReaddirResult(entries=entries)

    async def readdir_with_types(
        self, params: SessionFSReaddirWithTypesParams
    ) -> SessionFSReaddirWithTypesResult:
        path = self._resolve(params.path)
        entries: list[Entry] = []
        for entry in sorted(path.iterdir(), key=lambda x: x.name):
            etype = EntryType.DIRECTORY if entry.is_dir() else EntryType.FILE
            entries.append(Entry(name=entry.name, type=etype))
        return SessionFSReaddirWithTypesResult(entries=entries)

    async def rm(self, params: SessionFSRmParams) -> None:
        path = self._resolve(params.path)
        log.debug("fs rm: %s (recursive=%s)", path, params.recursive)
        if path.is_dir():
            if params.recursive:
                import shutil
                shutil.rmtree(path)
            else:
                path.rmdir()
        elif path.exists():
            path.unlink()
        elif not params.force:
            raise FileNotFoundError(f"No such file or directory: {path}")

    async def rename(self, params: SessionFSRenameParams) -> None:
        src = self._resolve(params.src)
        dest = self._resolve(params.dest)
        log.debug("fs rename: %s → %s", src, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)


def make_fs_handler(working_dir: str):
    """Return a ``CreateSessionFsHandler`` factory bound to *working_dir*.

    Usage::

        await client.create_session(
            ...
            create_session_fs_handler=make_fs_handler(pilot.home),
        )
    """
    def _factory(_) -> ConductorFsHandler:
        return ConductorFsHandler(working_dir)

    return _factory
