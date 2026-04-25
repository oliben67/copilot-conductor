"""Pending-task dispatcher.

Drains ``pending.log`` entries enqueued by APScheduler (or manual ``run_task``
calls) into the live conductor Copilot session so cron-fired tasks actually get
executed instead of just sitting on disk.

State is tracked via a byte offset stored under
``<cron-dir>/.state/dispatch.offset``. Successful dispatches append to
``<cron-dir>/processed.log``; failures stay queued (with a per-entry retry cap).
"""

from __future__ import annotations

import aiofiles
import asyncio
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from con_pilot.logger import app_logger

if TYPE_CHECKING:
    from con_pilot.conductor import ConPilot
    from con_pilot.core.services.copilot_service import CopilotAgentService

log = app_logger.bind(module=__name__, component="PendingDispatcher")

_HEADER_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] role=(?P<role>\S+) agent=(?P<agent>\S+) "
    r"job=(?P<job>\S+) schedule=(?P<schedule>.+)$"
)

DISPATCH_TIMEOUT_SECONDS = 180.0
MAX_RETRIES_PER_ENTRY = 3


@dataclass
class PendingEntry:
    offset: int  # byte offset just past this entry
    raw: str
    timestamp: str
    role: str
    agent: str
    job: str
    schedule: str
    task: str


class PendingDispatcher:
    """Drains pending.log into the conductor session."""

    def __init__(
        self,
        pilot: ConPilot,
        copilot_service: CopilotAgentService,
        *,
        poll_interval: float = 30.0,
    ) -> None:
        """
        Construct a dispatcher bound to the given pilot and Copilot service.

        Example:
            dispatcher = PendingDispatcher(pilot, copilot_service)
            await dispatcher.start()

        Note:
            The dispatcher does not start running until :meth:`start` is awaited.

        :param pilot: the live :class:`ConPilot` instance whose ``cron_dir`` and
            ``pending.log`` will be drained.
        :type pilot: `ConPilot`
        :param copilot_service: the running :class:`CopilotAgentService` used to
            forward each pending task to the conductor session.
        :type copilot_service: `CopilotAgentService`
        :param poll_interval: maximum seconds between drain ticks when no
            wake-up event is received.
        :type poll_interval: `float`
        :return: None
        :rtype: `None`
        """
        self._pilot = pilot
        self._copilot = copilot_service
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wakeup = asyncio.Event()
        self._failure_counts: dict[str, int] = {}
        self._stats = {
            "processed": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
            "last_error": None,
        }

    # ── State paths ────────────────────────────────────────────────────────────

    def _pending_path(self) -> str:
        return os.path.join(self._pilot.cron_dir, "pending.log")

    def _processed_path(self) -> str:
        return os.path.join(self._pilot.cron_dir, "processed.log")

    def _offset_path(self) -> str:
        state_dir = os.path.join(self._pilot.cron_dir, ".state")
        os.makedirs(state_dir, exist_ok=True)
        return os.path.join(state_dir, "dispatch.offset")

    def _read_offset(self) -> int:
        path = self._offset_path()
        if not os.path.exists(path):
            return 0
        try:
            return int(open(path).read().strip() or "0")
        except (OSError, ValueError):
            return 0

    def _write_offset(self, offset: int) -> None:
        with open(self._offset_path(), "w") as f:
            f.write(str(offset))

    # ── Parsing ────────────────────────────────────────────────────────────────

    def _parse_entries(self, content: str, base_offset: int) -> list[PendingEntry]:
        """Parse pending.log content starting at base_offset into entries."""
        entries: list[PendingEntry] = []
        # Each entry: header line, then one or more ``  task: ...`` continuation
        # lines, terminated by a blank line.
        cursor = 0
        while cursor < len(content):
            # Find end of this entry (double newline)
            end = content.find("\n\n", cursor)
            if end == -1:
                # Incomplete entry (still being written) — stop here.
                break
            block = content[cursor:end]
            entry_byte_end = base_offset + end + 2  # past the "\n\n"
            cursor = end + 2

            lines = block.split("\n")
            if not lines:
                continue
            header = lines[0]
            m = _HEADER_RE.match(header)
            if not m:
                log.warning("Skipping malformed pending entry: %r", header[:120])
                continue
            task_lines: list[str] = []
            for ln in lines[1:]:
                stripped = ln.lstrip()
                if stripped.startswith("task:"):
                    task_lines.append(stripped[5:].lstrip())
                elif task_lines:
                    task_lines.append(ln.strip())
            entries.append(
                PendingEntry(
                    offset=entry_byte_end,
                    raw=block,
                    timestamp=m.group("ts"),
                    role=m.group("role"),
                    agent=m.group("agent"),
                    job=m.group("job"),
                    schedule=m.group("schedule").strip("'\""),
                    task=" ".join(task_lines).strip(),
                )
            )
        return entries

    # ── Dispatch ───────────────────────────────────────────────────────────────

    def _build_prompt(self, entry: PendingEntry) -> str:
        return (
            f"A scheduled cron task has fired and is queued for execution.\n\n"
            f"Job:        {entry.job}\n"
            f"Role:       {entry.role}\n"
            f"Agent:      {entry.agent}\n"
            f"Schedule:   {entry.schedule}\n"
            f"Queued at:  {entry.timestamp}\n\n"
            f"Instructions:\n{entry.task}\n\n"
            f"Please execute the task now. If the role is not 'conductor', spawn or "
            f"delegate to the configured agent and supervise execution. When done, "
            f"report a one-line status."
        )

    async def _dispatch_entry(self, entry: PendingEntry) -> bool:
        prompt = self._build_prompt(entry)
        try:
            response = await asyncio.wait_for(
                self._copilot.send_to_conductor(prompt),
                timeout=DISPATCH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.warning(
                "Dispatch timed out for job=%s queued=%s", entry.job, entry.timestamp
            )
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Dispatch raised %s for job=%s queued=%s",
                type(exc).__name__,
                entry.job,
                entry.timestamp,
            )
            self._stats["last_error"] = f"{type(exc).__name__}: {exc}"
            return False

        if response is None:
            log.info("Conductor returned no response for job=%s", entry.job)
        self._append_processed(entry, response or "")
        return True

    def _append_processed(self, entry: PendingEntry, response: str) -> None:
        now = datetime.now(tz=UTC).isoformat()
        snippet = response.strip().splitlines()[0] if response.strip() else ""
        snippet = (snippet[:240] + "…") if len(snippet) > 240 else snippet
        with open(self._processed_path(), "a") as f:
            f.write(
                f"[{now}] queued={entry.timestamp} role={entry.role} "
                f"agent={entry.agent} job={entry.job} status=ok\n"
            )
            if snippet:
                f.write(f"  response: {snippet}\n")
            f.write("\n")

    # ── Loop ───────────────────────────────────────────────────────────────────

    async def drain_once(self) -> dict:
        """
        Process every readable pending entry and return a counts summary.

        Reads ``pending.log`` from the persisted byte offset, parses complete
        entries (incomplete trailing blocks are deferred), and dispatches each
        one to the conductor session. Successful dispatches advance the offset
        and append to ``processed.log``; the first failure stops the drain to
        preserve ordering.

        Example:
            summary = await dispatcher.drain_once()
            # {"processed": 3, "failed": 0, "skipped": 0}

        Note:
            When the conductor session is not yet attached, every parsed entry
            is reported as ``skipped`` and the offset is left untouched.

        :return: a mapping with the keys ``processed``, ``failed`` and
            ``skipped`` describing this drain pass.
        :rtype: `dict[str, int]`
        """
        pending = self._pending_path()
        if not os.path.exists(pending):
            return {"processed": 0, "failed": 0, "skipped": 0}

        offset = self._read_offset()
        try:
            async with aiofiles.open(pending, "rb") as f:
                await f.seek(offset)
                tail_bytes = await f.read()
        except OSError as exc:
            log.warning("Cannot read %s: %s", pending, exc)
            return {"processed": 0, "failed": 0, "skipped": 0}

        if not tail_bytes:
            return {"processed": 0, "failed": 0, "skipped": 0}

        try:
            content = tail_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content = tail_bytes.decode("utf-8", errors="replace")

        entries = self._parse_entries(content, base_offset=offset)
        processed = failed = skipped = 0

        if not self._copilot or not getattr(self._copilot, "_conductor_session", None):
            log.debug(
                "Conductor session unavailable; deferring %d entries", len(entries)
            )
            return {"processed": 0, "failed": 0, "skipped": len(entries)}

        for entry in entries:
            key = f"{entry.timestamp}::{entry.job}"
            if self._failure_counts.get(key, 0) >= MAX_RETRIES_PER_ENTRY:
                log.warning(
                    "Skipping entry after %d failures: job=%s queued=%s",
                    MAX_RETRIES_PER_ENTRY,
                    entry.job,
                    entry.timestamp,
                )
                self._write_offset(entry.offset)
                self._failure_counts.pop(key, None)
                skipped += 1
                continue
            ok = await self._dispatch_entry(entry)
            if ok:
                self._write_offset(entry.offset)
                self._failure_counts.pop(key, None)
                processed += 1
            else:
                self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
                failed += 1
                # Stop on first failure so subsequent entries aren't reordered.
                break

        self._stats["processed"] += processed
        self._stats["failed"] += failed
        self._stats["skipped"] += skipped
        self._stats["last_run"] = datetime.now(tz=UTC).isoformat()
        return {"processed": processed, "failed": failed, "skipped": skipped}

    def status(self) -> dict:
        """
        Return a snapshot of dispatcher state and cumulative counters.

        Example:
            >>> dispatcher.status()
            {'processed': 5, 'failed': 0, 'skipped': 0, 'last_run': '...',
             'last_error': None, 'running': True, 'pending_offset': 2822,
             'pending_size': 2822}

        :return: a mapping containing cumulative ``processed``/``failed``/
            ``skipped`` counters, the ISO-8601 ``last_run`` timestamp, the
            most recent ``last_error`` (if any), a ``running`` flag, and the
            current ``pending_offset`` and ``pending_size`` in bytes.
        :rtype: `dict[str, Any]`
        """
        return {
            **self._stats,
            "running": bool(self._task and not self._task.done()),
            "pending_offset": self._read_offset(),
            "pending_size": (
                os.path.getsize(self._pending_path())
                if os.path.exists(self._pending_path())
                else 0
            ),
        }

    def notify(self) -> None:
        """
        Wake the dispatcher loop early so a freshly enqueued entry is drained
        without waiting for the next poll tick.

        Example:
            pilot._append_pending(entry)
            dispatcher.notify()

        Note:
            Safe to call before :meth:`start` or after :meth:`stop`; raised
            ``RuntimeError`` from a missing event loop is swallowed.

        :return: None
        :rtype: `None`
        """
        try:
            self._wakeup.set()
        except RuntimeError:
            pass

    async def run(self) -> None:
        """
        Run the dispatch loop until :meth:`stop` is called.

        Drains the backlog once on entry, then alternates between awaiting a
        wake-up event (with ``poll_interval`` as the upper bound) and calling
        :meth:`drain_once`. Exceptions raised by a single tick are logged and
        do not terminate the loop.

        Example:
            asyncio.create_task(dispatcher.run())

        :return: None
        :rtype: `None`
        """
        log.info(
            "PendingDispatcher started (cron_dir=%s, poll=%.0fs)",
            self._pilot.cron_dir,
            self._poll_interval,
        )
        # Drain any backlog first.
        try:
            await self.drain_once()
        except Exception:
            log.exception("Initial drain failed")

        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass
            self._wakeup.clear()
            if self._stop.is_set():
                break
            try:
                await self.drain_once()
            except Exception:
                log.exception("Dispatcher tick failed")

        log.info("PendingDispatcher stopped")

    async def start(self) -> None:
        """
        Schedule :meth:`run` on the running event loop as a background task.

        Example:
            await dispatcher.start()

        Note:
            Idempotent: a no-op when an active dispatcher task is already
            running.

        :return: None
        :rtype: `None`
        """
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._wakeup.clear()
        self._task = asyncio.create_task(self.run(), name="pending-dispatcher")

    async def stop(self) -> None:
        """
        Signal the dispatcher to stop and await termination of the background
        task.

        Example:
            await dispatcher.stop()

        Note:
            If the task does not exit within five seconds it is cancelled.

        :return: None
        :rtype: `None`
        """
        self._stop.set()
        self._wakeup.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
            self._task = None
