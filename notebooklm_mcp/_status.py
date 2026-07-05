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
        raise RuntimeError(f"Generation failed: {detail}")
    return task_id


def ensure_completed(status: object) -> None:
    """Raise RuntimeError if a generation finished in a failed OR removed state.

    0.6.0 起 SDK 對「被伺服器下架」的 artifact(NOT_FOUND 輪詢耗盡,SDK 文件說
    這通常是每日配額拒絕)改回報 status="removed" 且 is_failed=False —— 不再像
    0.4.x 合成成 "failed"。只檢查 is_failed 會把配額下架當成功放行(artifact_wait
    假成功、podcast 流程帶著死 artifact 繼續、rename 因 return_object=False 靜默
    no-op、最後 download 以誤導性的 "not ready" 爆掉、遮蔽配額真因)。故一併擋
    is_removed 並點出配額。
    """
    if getattr(status, "is_failed", False):
        detail = getattr(status, "error", None) or getattr(status, "status", None) or "failed"
        raise RuntimeError(f"Generation failed while waiting: {detail}")
    if getattr(status, "is_removed", False):
        raise RuntimeError(
            "Generation removed by server(通常是每日配額耗盡);稍後再試或換帳號。"
            f" status={getattr(status, 'status', 'removed')!r}"
        )
