"""Generation-status guards shared by the tools.

The SDK reports a failed / refused generation (rate limit, quota, content
rejection) as GenerationStatus(task_id="", status="failed", error=...) rather
than by raising. Blindly using status.task_id would then wait / download with an
empty id (hang or garbage). These helpers fail fast with a clear message.
"""
from __future__ import annotations


class TerminalGenerationError(RuntimeError):
    """生成以「伺服器端終態」收場(failed / removed,如每日配額耗盡)。

    是 RuntimeError 的子類(既有 `pytest.raises(RuntimeError)` / isinstance 判斷不變),
    但獨立型別讓呼叫端能精準區分「終態、不可續跑」與「生成之後的可續跑失敗(逾時/
    下載中斷)」——podcast 流程據此決定要不要提示 `podcast_episode_resume`。"""


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
    假成功、podcast 流程帶著死 artifact 繼續、最後以誤導性的錯誤爆掉、遮蔽配額真因)。
    故一併擋 is_removed 並點出配額。

    (歷史註:0.7.x 時下游還會多一段「rename 因 return_object=False 靜默 no-op」的
    連鎖。0.8.0 起 rename 兩種模式都會做存在性檢查,那一環改成 raise 了 —— 但這
    只是把症狀往前挪,本檢查仍是唯一會把「配額」講出來的地方。)
    """
    if getattr(status, "is_failed", False):
        detail = getattr(status, "error", None) or getattr(status, "status", None) or "failed"
        raise TerminalGenerationError(f"Generation failed while waiting: {detail}")
    if getattr(status, "is_removed", False):
        raise TerminalGenerationError(
            "Generation removed by server(通常是每日配額耗盡);稍後再試或換帳號。"
            f" status={getattr(status, 'status', 'removed')!r}"
        )
