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
    故一併擋 is_removed 並點出配額。0.8.3 起 web 輪詢不再自己回 removed,由
    `wait_for_artifact` 在「整窗缺席」時合成(見該函式)。

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


#: 整個等待窗口都查無此 artifact 時,窗口至少要這麼長才判定「被伺服器下架」。
#: ponytail: 固定門檻;真的遇到「清單漏列超過 5 分鐘」的假 removed 再改成按次數累計。
_REMOVED_MIN_ABSENT_WINDOW = 300.0


async def wait_for_artifact(artifacts, notebook_id: str, artifact_id: str, *, timeout: float):
    """`wait_for_completion` 的薄包:把「整窗缺席」翻回 `removed` 終態。

    notebooklm-py 0.8.3(#2432)不再合成 `removed`:0.8.2 缺席 10 秒就判下架,上游認為
    清單暫時漏列會誤判,改成一路等到 deadline 才 raise `ArtifactTimeoutError`。對我們的
    後果是配額下架**永遠到不了終態**——續跑每次等滿逾時又回 resume,supersede 路徑變死碼,
    配額這個原因也從訊息裡消失。

    判準比 0.8.2 嚴格上百倍:這一整窗(>= 5 分鐘)**從頭到尾**只看過 `not_found`、一次都
    沒看到它。看過 pending 才消失的不算(配額下架的第一窗就是這形狀 → 照舊回 resume;
    續跑那一窗全缺席才判 removed)。回傳合成的 `GenerationStatus`,後面照走
    `ensure_completed` 與既有的 removed 狀態機,不另開新狀態。
    """
    from notebooklm.exceptions import ArtifactTimeoutError
    from notebooklm.types import GenerationStatus

    try:
        return await artifacts.wait_for_completion(notebook_id, artifact_id, timeout=timeout)
    except ArtifactTimeoutError as exc:
        history = tuple(getattr(exc, "status_history", ()) or ())
        window = getattr(exc, "timeout", 0) or 0
        if history and set(history) == {"not_found"} and window >= _REMOVED_MIN_ABSENT_WINDOW:
            return GenerationStatus(task_id=artifact_id, status="removed")
        raise
