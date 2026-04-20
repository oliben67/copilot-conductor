"""
snapshot.py — GitHub directory snapshot management for con-pilot.

Provides tar.gz snapshots of the .github directory with change detection
for automatic backups when monitored files (md, cron, json, yaml) change.
"""

import hashlib
import json
import logging
import os
import tarfile
import threading
import time
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from con_pilot.paths import PathResolver

log = logging.getLogger(__name__)


# File patterns to include in snapshots
SNAPSHOT_PATTERNS = ["*.md", "*.cron", "*.json", "*.yaml", "*.yml"]


class SnapshotMetadata(BaseModel):
    """Metadata about a stored snapshot."""

    filename: str = Field(..., description="Snapshot filename")
    timestamp: datetime = Field(..., description="When this snapshot was created")
    automatic: bool = Field(
        default=False, description="Whether this was an automatic snapshot"
    )
    file_count: int = Field(default=0, description="Number of files in snapshot")
    size_bytes: int = Field(default=0, description="Size of the archive in bytes")
    file_hashes: dict[str, str] = Field(
        default_factory=dict, description="MD5 hashes of included files"
    )


class SnapshotIndex(BaseModel):
    """Index of all stored snapshots."""

    snapshots: list[SnapshotMetadata] = Field(default_factory=list)
    last_hashes: dict[str, str] = Field(
        default_factory=dict, description="Last known file hashes for change detection"
    )


class SnapshotService:
    """
    Manages snapshots of the .github directory.

    Provides:
    - On-demand tar.gz snapshots filtered to specific file types
    - Automatic snapshots when monitored files change
    - Change detection via file hash comparison
    - Snapshot listing and metadata management
    """

    INSTRUCTIONS_DIR = ".instructions"
    INDEX_FILE = "snapshot-index.json"

    def __init__(self, paths: PathResolver) -> None:
        self._paths = paths
        self._index: SnapshotIndex | None = None
        self._lock = threading.Lock()
        self._watcher_running = False
        self._watcher_thread: threading.Thread | None = None

    @property
    def instructions_dir(self) -> str:
        """Path to CONDUCTOR_HOME/.instructions/"""
        return os.path.join(self._paths.home, self.INSTRUCTIONS_DIR)

    @property
    def index_path(self) -> str:
        """Path to CONDUCTOR_HOME/.instructions/snapshot-index.json"""
        return os.path.join(self.instructions_dir, self.INDEX_FILE)

    @property
    def github_dir(self) -> str:
        """Path to CONDUCTOR_HOME/.github/"""
        return self._paths.github_dir

    def ensure_instructions_dir(self) -> None:
        """Create .instructions directory if it doesn't exist."""
        Path(self.instructions_dir).mkdir(parents=True, exist_ok=True)

    def _load_index(self) -> SnapshotIndex:
        """Load or create the snapshot index."""
        if self._index is not None:
            return self._index

        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, encoding="utf-8") as f:
                    data = json.load(f)
                self._index = SnapshotIndex.model_validate(data)
            except Exception as e:
                log.warning("Failed to load snapshot index: %s", e)
                self._index = SnapshotIndex()
        else:
            self._index = SnapshotIndex()

        return self._index

    def _save_index(self) -> None:
        """Persist the snapshot index to disk."""
        if self._index is None:
            return

        self.ensure_instructions_dir()
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(self._index.model_dump(mode="json"), f, indent=2, default=str)

    def _should_include(self, filename: str) -> bool:
        """Check if a file should be included in the snapshot."""
        return any(fnmatch(filename, pattern) for pattern in SNAPSHOT_PATTERNS)

    def _compute_file_hash(self, filepath: str) -> str:
        """Compute MD5 hash of a file."""
        hasher = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def get_file_hashes(self) -> dict[str, str]:
        """
        Compute hashes for all monitored files in .github directory.

        Returns:
            Dictionary mapping relative file paths to their MD5 hashes.
        """
        hashes: dict[str, str] = {}
        github_dir = self.github_dir

        if not os.path.isdir(github_dir):
            log.warning("GitHub directory does not exist: %s", github_dir)
            return hashes

        for root, _, files in os.walk(github_dir):
            for filename in files:
                if self._should_include(filename):
                    filepath = os.path.join(root, filename)
                    rel_path = os.path.relpath(filepath, github_dir)
                    try:
                        hashes[rel_path] = self._compute_file_hash(filepath)
                    except OSError as e:
                        log.warning("Failed to hash file %s: %s", filepath, e)

        return hashes

    def detect_changes(self) -> tuple[bool, dict[str, str]]:
        """
        Detect if monitored files have changed since last snapshot.

        Returns:
            Tuple of (has_changes, current_hashes).
        """
        index = self._load_index()
        current_hashes = self.get_file_hashes()

        if not index.last_hashes:
            # No previous hashes - consider this as changed if there are files
            return bool(current_hashes), current_hashes

        # Compare hashes
        if current_hashes != index.last_hashes:
            return True, current_hashes

        return False, current_hashes

    def _generate_filename(self, automatic: bool) -> str:
        """Generate snapshot filename with timestamp."""
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        prefix = "automatic-snapshot" if automatic else "snapshot"
        return f"{prefix}-{timestamp}.github.tar.gz"

    def create_snapshot(self, automatic: bool = False) -> SnapshotMetadata:
        """
        Create a tar.gz snapshot of the .github directory.

        Parameters
        ----------
        automatic:
            If True, use automatic-snapshot prefix in filename.

        Returns
        -------
        SnapshotMetadata:
            Metadata about the created snapshot.
        """
        with self._lock:
            self.ensure_instructions_dir()
            github_dir = self.github_dir

            if not os.path.isdir(github_dir):
                raise FileNotFoundError(f".github directory not found: {github_dir}")

            filename = self._generate_filename(automatic)
            filepath = os.path.join(self.instructions_dir, filename)

            file_hashes: dict[str, str] = {}
            file_count = 0

            with tarfile.open(filepath, "w:gz") as tar:
                for root, _, files in os.walk(github_dir):
                    for fname in files:
                        if self._should_include(fname):
                            full_path = os.path.join(root, fname)
                            rel_path = os.path.relpath(full_path, github_dir)
                            arcname = os.path.join(".github", rel_path)

                            tar.add(full_path, arcname=arcname)
                            file_hashes[rel_path] = self._compute_file_hash(full_path)
                            file_count += 1

            size_bytes = os.path.getsize(filepath)

            metadata = SnapshotMetadata(
                filename=filename,
                timestamp=datetime.now(UTC),
                automatic=automatic,
                file_count=file_count,
                size_bytes=size_bytes,
                file_hashes=file_hashes,
            )

            # Update index
            index = self._load_index()
            index.snapshots.append(metadata)
            index.last_hashes = file_hashes
            self._save_index()

            log.info(
                "Created %s snapshot: %s (%d files, %d bytes)",
                "automatic" if automatic else "manual",
                filename,
                file_count,
                size_bytes,
            )

            return metadata

    def list_snapshots(self) -> list[SnapshotMetadata]:
        """List all stored snapshots."""
        return self._load_index().snapshots

    def get_snapshot(self, filename: str) -> SnapshotMetadata | None:
        """Get metadata for a specific snapshot."""
        for snap in self._load_index().snapshots:
            if snap.filename == filename:
                return snap
        return None

    def get_snapshot_path(self, filename: str) -> str | None:
        """Get full path to a snapshot file."""
        path = os.path.join(self.instructions_dir, filename)
        return path if os.path.exists(path) else None

    def delete_snapshot(self, filename: str) -> bool:
        """
        Delete a snapshot file and remove from index.

        Returns True if deleted, False if not found.
        """
        with self._lock:
            index = self._load_index()

            for i, snap in enumerate(index.snapshots):
                if snap.filename == filename:
                    path = os.path.join(self.instructions_dir, filename)
                    if os.path.exists(path):
                        os.remove(path)
                    index.snapshots.pop(i)
                    self._save_index()
                    log.info("Deleted snapshot: %s", filename)
                    return True

            return False

    def check_and_snapshot(self) -> SnapshotMetadata | None:
        """
        Check for changes and create automatic snapshot if needed.

        Returns SnapshotMetadata if a snapshot was created, None otherwise.
        """
        has_changes, _ = self.detect_changes()
        if has_changes:
            return self.create_snapshot(automatic=True)
        return None

    def start_watcher(self, interval: int = 60) -> None:
        """
        Start background watcher for automatic snapshots.

        Parameters
        ----------
        interval:
            Check interval in seconds (default: 60).
        """
        if self._watcher_running:
            log.warning("Snapshot watcher already running")
            return

        self._watcher_running = True

        def _watch_loop() -> None:
            log.info("Snapshot watcher started (interval=%ds)", interval)
            while self._watcher_running:
                try:
                    result = self.check_and_snapshot()
                    if result:
                        log.info("Automatic snapshot created: %s", result.filename)
                except Exception:
                    log.exception("Snapshot watcher check failed")
                time.sleep(interval)
            log.info("Snapshot watcher stopped")

        self._watcher_thread = threading.Thread(target=_watch_loop, daemon=True)
        self._watcher_thread.start()

    def stop_watcher(self) -> None:
        """Stop the background watcher."""
        self._watcher_running = False
        if self._watcher_thread:
            self._watcher_thread.join(timeout=5)
            self._watcher_thread = None

    @property
    def versions(self) -> list[SnapshotMetadata]:
        """Alias for list_snapshots() for API consistency."""
        return self.list_snapshots()
