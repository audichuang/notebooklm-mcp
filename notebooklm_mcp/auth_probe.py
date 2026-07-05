"""認證預檢:用「輕量真 RPC」驗 cookie 是否還活著。

jacob-bd/notebooklm-mcp-cli 的教訓(其 issue #250):homepage probe 會
false-positive,必須打真正的 NotebookLM RPC 才算數。notebooks.list() 是
最便宜的真 RPC。用在長跑工具(podcast_episode / podcast_series)開頭
fail-fast:cookie 已死時秒退並給重登指引,而不是燒掉數小時等待後才炸。
"""
from __future__ import annotations

RELOGIN_HINT = (
    "NotebookLM 認證失效或無法連線。請在有 GUI 的機器重登後同步:\n"
    "  notebooklm login && bash scripts/sync-auth.sh\n"
    "再重啟 MCP server(Doppler 會注入新的 NOTEBOOKLM_AUTH_JSON)。"
)


async def probe_auth(client) -> dict:
    """輕量真 RPC 探測。活著回 {'ok': True, 'notebooks': N};死了 raise RuntimeError。

    刻意攔一切例外:對呼叫端而言,無論 AuthError、RPC 錯誤還是網路斷線,
    可操作的下一步都一樣(檢查認證/連線再重跑),原始錯誤附在訊息尾供診斷。
    """
    try:
        nbs = await client.notebooks.list()
    except Exception as exc:
        raise RuntimeError(f"{RELOGIN_HINT}\n原始錯誤:{exc!r}") from exc
    return {"ok": True, "notebooks": len(nbs)}
