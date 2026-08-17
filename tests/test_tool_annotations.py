"""Tripwire:鎖住每支 MCP tool 的 ToolAnnotations。

寧可漏標,不可錯標——錯的 readOnlyHint 會讓 client 對 mutating 工具自動放行/
並行執行,錯的 destructiveHint 同理。這裡從真正的 FastMCP registry(`list_tools()`)
讀回**實際**掛的 annotations,而不是重新猜一份,逐一與 AGENTS.md/審查報告核准的
清單做等值比對——新工具或改動忘了加/加錯 annotations 時測試要紅。
"""
from __future__ import annotations

from notebooklm_mcp import app

# 逐一驗證過「不寫本機檔、不打 mutating RPC、不寫 manifest」才標 readOnlyHint=True。
# artifact_wait / research_wait 只輪詢既有 task 狀態,不下載、不寫 manifest,一併納入。
EXPECTED_READ_ONLY = {
    "auth_check",
    "notebook_list",
    "notebook_get",
    "source_list",
    "source_fulltext",
    "artifact_list",
    "artifact_wait",
    "feed_info",
    "research_wait",
}

# 純本機 mutation 但語意是「作廢/刪除」→ destructiveHint=True。
EXPECTED_DESTRUCTIVE = {
    "source_delete",
    "podcast_attempt_retract",
}

# source_delete 會先驗證 id 屬於指定 notebook，查無此 id 時**不發** destructive RPC
# 而回 was_present=False —— 重呼同一個 id 沒有額外後果，符合 MCP 的 idempotentHint。
# (曾經改成 fail-loud，但那會打斷 retract 的「stale_source_ids 逐一 source_delete」
# 清理迴圈；安全性質留在「不打 RPC」，冪等留在「不 raise」。)
# retract 對同一 attempt 重呼仍沿用既有 retraction，不重寫時間戳。
EXPECTED_IDEMPOTENT = {
    "source_delete",
    "podcast_attempt_retract",
    # v0.9.18:同一組 (state, reason) 重呼是真正的 no-op —— 連 revision 都不 +1
    # (`_NoChange` 從 mutator 拋出去 = 零寫入離場)。遺失 response 後重放是預期操作。
    "episode_set_publication_state",
}

# 這份清單是「核准紀錄」而非語意判定:openWorldHint 缺省時 client 本就假設 True,
# 標了只是明文化。新增工具時請一併更新,不要靠直覺猜哪些該標。
# 長跑生成/發布/對外研究工具,低風險可標;openWorldHint 與 readOnlyHint 不互斥
# (research_wait 只輪詢但底層仍是對外部世界的研究任務)。
EXPECTED_OPEN_WORLD = {
    "generate_audio",
    "generate_slides",
    "generate_report",
    "podcast_episode",
    "podcast_series",
    "publish_series",
    "research_start",
    "research_wait",
    "research_import",
}


# **顯式 False 也要被鎖住。** 其他四份清單都只收集 `is True`,所以
# `openWorldHint=False` 被刪掉時沒有任何測試會紅 —— 而它不是預設值:MCP client 在缺省時
# 假設 True,標 False 是在明講「這支不碰外部世界」。純本機 manifest 寫入的工具才准進這裡。
EXPECTED_NOT_OPEN_WORLD = {
    "episode_set_publication_state",
}


async def _annotations_by_name() -> dict[str, object]:
    tools = await app.mcp.list_tools()
    return {t.name: t.annotations for t in tools}


def _names_with(annotations: dict[str, object], field: str) -> set[str]:
    return {
        name for name, ann in annotations.items()
        if ann is not None and getattr(ann, field, None) is True
    }


async def test_read_only_hint_matches_approved_list():
    annotations = await _annotations_by_name()
    assert _names_with(annotations, "readOnlyHint") == EXPECTED_READ_ONLY


async def test_destructive_hint_matches_approved_list():
    annotations = await _annotations_by_name()
    assert _names_with(annotations, "destructiveHint") == EXPECTED_DESTRUCTIVE


async def test_idempotent_hint_matches_approved_list():
    annotations = await _annotations_by_name()
    assert _names_with(annotations, "idempotentHint") == EXPECTED_IDEMPOTENT


async def test_open_world_hint_matches_approved_list():
    annotations = await _annotations_by_name()
    assert _names_with(annotations, "openWorldHint") == EXPECTED_OPEN_WORLD


async def test_explicit_not_open_world_matches_approved_list():
    """顯式 `openWorldHint=False` 的那幾支 —— 刪掉標註要會紅(見清單上方註解)。"""
    annotations = await _annotations_by_name()
    explicit_false = {
        name for name, ann in annotations.items()
        if ann is not None and getattr(ann, "openWorldHint", None) is False
    }
    assert explicit_false == EXPECTED_NOT_OPEN_WORLD


async def test_destructive_and_idempotent_hints_only_on_non_read_only_tools():
    """readOnlyHint=True 時 destructiveHint/idempotentHint 沒有意義(MCP spec);
    確保沒有工具同時被標成兩種互斥語意,避免未來合併 annotations 時標錯。"""
    annotations = await _annotations_by_name()
    read_only = _names_with(annotations, "readOnlyHint")
    destructive_or_idempotent = (
        _names_with(annotations, "destructiveHint")
        | _names_with(annotations, "idempotentHint")
    )
    assert not (read_only & destructive_or_idempotent)
