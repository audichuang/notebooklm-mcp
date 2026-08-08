"""認證預檢:用「輕量真 RPC」驗 cookie 是否還活著。

jacob-bd/notebooklm-mcp-cli 的教訓(其 issue #250):homepage probe 會
false-positive,必須打真正的 NotebookLM RPC 才算數。notebooks.list() 是
最便宜的真 RPC。用在長跑工具(podcast_episode / podcast_series)開頭
fail-fast:cookie 已死時秒退並給重登指引,而不是燒掉數小時等待後才炸。
"""
from __future__ import annotations

# ⚠️ 這裡**不能**寫 `notebooklm login`:2026-08 起 Google 把未認證的登入流程轉到
# notebook.google.com,而 SDK(含 0.8.0)的偵測仍寫死舊網域,照著跑會卡滿 5 分鐘 timeout。
# 這段訊息出現的時機正是「認證死了、使用者最會照著做」的時候,指錯就是直接浪費五分鐘。
# 替代腳本 scripts/login_notebooklm.py 只改掉那一行偵測,其餘重用 SDK helper。
# 上游修好時 tests/test_contracts.py 的退場 tripwire 會紅,屆時這段也一起改回去。
RELOGIN_HINT = (
    "NotebookLM 認證失效或無法連線。請在有 GUI 的機器重登後同步\n"
    "(在 notebooklm-mcp repo 目錄下執行):\n"
    "  uv run python scripts/login_notebooklm.py   # 主力帳號;測試帳號加 --profile test\n"
    "  bash scripts/sync-auth.sh                   # 測試帳號:--profile test --config stg\n"
    "再重啟 MCP server(Doppler 會注入新的 NOTEBOOKLM_AUTH_JSON)。\n"
    "註:不要用 `notebooklm login` —— Google 已搬登入網域,上游偵測還沒跟上,會卡到 timeout。"
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
