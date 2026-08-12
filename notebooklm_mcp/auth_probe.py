"""認證預檢:用「輕量真 RPC」驗 cookie 是否還活著。

jacob-bd/notebooklm-mcp-cli 的教訓(其 issue #250):homepage probe 會
false-positive,必須打真正的 NotebookLM RPC 才算數。notebooks.list() 是
最便宜的真 RPC。用在長跑工具(podcast_episode / podcast_series)開頭
fail-fast:cookie 已死時秒退並給重登指引,而不是燒掉數小時等待後才炸。
"""

from __future__ import annotations

try:
    from notebooklm import is_auth_error
except ImportError:  # notebooklm-py 0.8.0; remove after the public helper ships
    from notebooklm._runtime import is_auth_error
from notebooklm.exceptions import RPCError

RELOGIN_HINT = (
    "NotebookLM 認證失效。請在有 GUI 的機器重登後同步\n"
    "(在 notebooklm-mcp repo 目錄下執行):\n"
    "  uv run notebooklm login                     # 測試帳號加 -p test\n"
    "  bash scripts/sync-auth.sh                   # 測試帳號:--profile test --config stg\n"
    "再重啟 MCP server(Doppler 會注入新的 NOTEBOOKLM_AUTH_JSON)。"
)


class _AuthProbeError(RuntimeError):
    """認證探測已確認失效；caller 可安全轉成重登停點。"""


def _is_probe_auth_error(exc: Exception) -> bool:
    """沿用 SDK 分類，兼容 0.8.0 尚未沿 mapper cause 的行為。"""
    if is_auth_error(exc):
        return True
    if type(exc) is not RPCError:
        return False
    if any(
        getattr(exc, name, None) is not None
        for name in ("status_code", "rpc_code", "code", "status")
    ):
        return False
    cause = exc.__cause__
    return isinstance(cause, Exception) and is_auth_error(cause)


async def probe_auth(client) -> dict:
    """輕量真 RPC 探測；只把明確的認證錯誤轉成可操作的重登指引。

    網路、限流與其他暫時性錯誤保留原型別，讓 caller 能正確重試或退避。
    """
    try:
        nbs = await client.notebooks.list()
    except Exception as exc:
        if not _is_probe_auth_error(exc):
            raise
        raise _AuthProbeError(f"{RELOGIN_HINT}\n原始錯誤:{exc!r}") from exc
    return {"ok": True, "notebooks": len(nbs)}
