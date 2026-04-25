"""Snapshot management endpoints for .github directory backups."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from con_pilot.conductor.snapshot_service import SnapshotMetadata

from con_pilot.conductor import ConPilot

router = APIRouter(prefix="/snapshot", tags=["snapshot"])


def get_pilot() -> ConPilot:
    """Dependency to get the ConPilot instance."""
    from con_pilot.app import get_pilot as _get_pilot

    return _get_pilot()


# ── Request/Response Models ────────────────────────────────────────────────────


class SnapshotListResponse(BaseModel):
    """Response for listing all snapshots."""

    snapshots: list[SnapshotMetadata] = Field(
        ..., description="List of stored snapshots"
    )
    instructions_dir: str = Field(
        ..., description="Path to the .instructions directory"
    )
    watcher_running: bool = Field(
        ..., description="Whether the change watcher is running"
    )


class SnapshotCreateRequest(BaseModel):
    """Request body for creating a snapshot."""

    automatic: bool = Field(default=False, description="Mark as automatic snapshot")


class SnapshotCreateResponse(BaseModel):
    """Response for successful snapshot creation."""

    message: str
    snapshot: SnapshotMetadata


class SnapshotDeleteResponse(BaseModel):
    """Response for successful snapshot deletion."""

    message: str
    filename: str


class ChangeDetectionResponse(BaseModel):
    """Response for change detection check."""

    has_changes: bool = Field(..., description="Whether changes were detected")
    file_count: int = Field(..., description="Number of monitored files")
    current_hashes: dict[str, str] = Field(..., description="Current file hashes")


class WatcherStatusResponse(BaseModel):
    """Response for watcher status operations."""

    running: bool
    message: str


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("", response_model=SnapshotListResponse)
def list_snapshots(pilot: ConPilot = Depends(get_pilot)) -> SnapshotListResponse:
    """
    List all stored .github snapshots.

    Returns metadata for each snapshot including timestamps and file counts.
    """
    pilot = pilot or get_pilot()
    service = pilot.snapshot_service
    return SnapshotListResponse(
        snapshots=service.list_snapshots(),
        instructions_dir=service.instructions_dir,
        watcher_running=service._watcher_running,
    )


@router.post(
    "", response_model=SnapshotCreateResponse, status_code=status.HTTP_201_CREATED
)
def create_snapshot(
    request: SnapshotCreateRequest | None = None,
    pilot: ConPilot = Depends(get_pilot),
) -> SnapshotCreateResponse:
    """
    Create a new snapshot of the .github directory.

    Creates a tar.gz archive containing all .md, .cron, .json, .yaml, and .yml
    files from the .github directory.
    """
    pilot = pilot or get_pilot()
    service = pilot.snapshot_service
    automatic = request.automatic if request else False

    try:
        metadata = service.create_snapshot(automatic=automatic)
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e

    return SnapshotCreateResponse(
        message=f"Snapshot created: {metadata.filename}",
        snapshot=metadata,
    )


@router.get("/changes", response_model=ChangeDetectionResponse)
def check_changes(pilot: ConPilot = Depends(get_pilot)) -> ChangeDetectionResponse:
    """
    Check if monitored files have changed since last snapshot.

    Compares current file hashes with the last recorded hashes.
    """
    pilot = pilot or get_pilot()
    service = pilot.snapshot_service
    has_changes, current_hashes = service.detect_changes()

    return ChangeDetectionResponse(
        has_changes=has_changes,
        file_count=len(current_hashes),
        current_hashes=current_hashes,
    )


@router.post("/check-and-create", response_model=SnapshotCreateResponse | None)
def check_and_create_snapshot(
    pilot: ConPilot = Depends(get_pilot),
) -> SnapshotCreateResponse | dict:
    """
    Check for changes and create automatic snapshot if needed.

    Returns the snapshot metadata if created, or a message if no changes detected.
    """
    pilot = pilot or get_pilot()
    service = pilot.snapshot_service
    result = service.check_and_snapshot()

    if result:
        return SnapshotCreateResponse(
            message=f"Automatic snapshot created: {result.filename}",
            snapshot=result,
        )

    return {"message": "No changes detected, snapshot not created"}


@router.get("/watcher", response_model=WatcherStatusResponse)
def get_watcher_status(pilot: ConPilot = Depends(get_pilot)) -> WatcherStatusResponse:
    """Get the current status of the change watcher."""
    pilot = pilot or get_pilot()
    service = pilot.snapshot_service
    return WatcherStatusResponse(
        running=service._watcher_running,
        message="Watcher is running"
        if service._watcher_running
        else "Watcher is stopped",
    )


@router.post("/watcher/start", response_model=WatcherStatusResponse)
def start_watcher(
    interval: int = 60,
    pilot: ConPilot = Depends(get_pilot),
) -> WatcherStatusResponse:
    """
    Start the automatic snapshot watcher.

    Parameters
    ----------
    interval:
        Check interval in seconds (default: 60).
    """
    pilot = pilot or get_pilot()
    service = pilot.snapshot_service
    if service._watcher_running:
        return WatcherStatusResponse(
            running=True,
            message="Watcher is already running",
        )

    service.start_watcher(interval=interval)
    return WatcherStatusResponse(
        running=True,
        message=f"Watcher started with {interval}s interval",
    )


@router.post("/watcher/stop", response_model=WatcherStatusResponse)
def stop_watcher(pilot: ConPilot = Depends(get_pilot)) -> WatcherStatusResponse:
    """Stop the automatic snapshot watcher."""
    pilot = pilot or get_pilot()
    service = pilot.snapshot_service
    if not service._watcher_running:
        return WatcherStatusResponse(
            running=False,
            message="Watcher is not running",
        )

    service.stop_watcher()
    return WatcherStatusResponse(
        running=False,
        message="Watcher stopped",
    )


@router.get("/{filename}", response_model=SnapshotMetadata)
def get_snapshot(
    filename: str,
    pilot: ConPilot = Depends(get_pilot),
) -> SnapshotMetadata:
    """Get metadata for a specific snapshot."""
    pilot = pilot or get_pilot()
    service = pilot.snapshot_service
    metadata = service.get_snapshot(filename)

    if not metadata:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot not found: {filename}",
        )

    return metadata


@router.get("/{filename}/download")
def download_snapshot(
    filename: str,
    pilot: ConPilot = Depends(get_pilot),
) -> FileResponse:
    """Download a snapshot file."""
    pilot = pilot or get_pilot()
    service = pilot.snapshot_service
    path = service.get_snapshot_path(filename)

    if not path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot file not found: {filename}",
        )

    return FileResponse(
        path=path,
        filename=filename,
        media_type="application/gzip",
    )


@router.delete("/{filename}", response_model=SnapshotDeleteResponse)
def delete_snapshot(
    filename: str,
    pilot: ConPilot = Depends(get_pilot),
) -> SnapshotDeleteResponse:
    """Delete a snapshot."""
    pilot = pilot or get_pilot()
    service = pilot.snapshot_service
    deleted = service.delete_snapshot(filename)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Snapshot not found: {filename}",
        )

    return SnapshotDeleteResponse(
        message=f"Snapshot deleted: {filename}",
        filename=filename,
    )
