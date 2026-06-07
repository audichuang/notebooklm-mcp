"""Generation-status guards shared by the tools.

The SDK reports a failed / refused generation (rate limit, quota, content
rejection) as GenerationStatus(task_id="", status="failed", error=...) rather
than by raising. Blindly using status.task_id would then wait / download with an
empty id (hang or garbage). These helpers fail fast with a clear message.
"""
from __future__ import annotations


def ensure_started(status: object) -> str:
    """Return the task_id, raising RuntimeError if generation did not start."""
    task_id = getattr(status, "task_id", "") or ""
    if getattr(status, "is_failed", False) or not task_id:
        detail = (
            getattr(status, "error", None)
            or getattr(status, "error_code", None)
            or getattr(status, "status", None)
            or "no task_id returned"
        )
        raise RuntimeError(f"Audio generation failed: {detail}")
    return task_id


def ensure_completed(status: object) -> None:
    """Raise RuntimeError if a generation finished in a failed state."""
    if getattr(status, "is_failed", False):
        detail = getattr(status, "error", None) or getattr(status, "status", None) or "failed"
        raise RuntimeError(f"Audio generation failed while waiting: {detail}")
