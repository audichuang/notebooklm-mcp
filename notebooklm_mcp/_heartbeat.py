"""工具呼叫期間的 progress 心跳:讓「慢但活著」的呼叫不被 client 當成閒置砍掉。

Claude Code 對 stdio MCP 工具呼叫有閒置窗:既沒回應也沒 progress 就 abort
(`sent no response or progress for …; aborting`;stdio 預設 30 分鐘,調低時實測最短約 60s)。長工具
(podcast_series / podcast_episode / research_wait …)本身不送 progress,所以每個
工具都包一層,呼叫期間每 `INTERVAL` 秒送一次 `notifications/progress`。

不變式:心跳失敗、卡住或被取消,**絕不**取消、延遲或改變工具本體與其結果/例外
—— cancel 落在遠端 dispatch 與 manifest 寫入之間會讓 attempt 卡在 acceptance_unknown。
所以:心跳是獨立的 asyncio task(不用 anyio TaskGroup,子任務失敗會反過來取消本體、
把例外包成 ExceptionGroup);本體結束只 `cancel()` 不 `await`(await 會延遲結果);
client 沒帶 progressToken 就什麼都不做。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from mcp.server.lowlevel.server import request_ctx

logger = logging.getLogger(__name__)

# 遠低於閒置窗可調到的最短值(實測約 60s)
INTERVAL = 20.0


async def _beat(session: Any, token: str | int, request_id: Any, started: float) -> None:
    while True:
        await asyncio.sleep(INTERVAL)
        elapsed = time.monotonic() - started  # 不取整:取整會讓相鄰兩拍同值,違反 MCP 的遞增要求
        try:
            await session.send_progress_notification(
                token, elapsed, message=f"仍在執行({elapsed:.0f}s)", related_request_id=request_id
            )
        except Exception:  # 心跳失敗只記錄,下一拍再試,不碰本體
            logger.debug("progress heartbeat send failed", exc_info=True)


def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        rc = request_ctx.get(None)
        token = rc.meta.progressToken if rc is not None and rc.meta is not None else None
        if token is None:
            return await fn(*args, **kwargs)
        task = asyncio.create_task(_beat(rc.session, token, str(rc.request_id), time.monotonic()))
        try:
            return await fn(*args, **kwargs)
        finally:
            task.cancel()

    wrapper.__heartbeat__ = True  # type: ignore[attr-defined]
    return wrapper


def install(tools: Iterable[Any]) -> None:
    """把每個已註冊 FastMCP Tool 的 `fn` 換成心跳包裝(fn_metadata 已由原函式算好,schema 不變)。"""
    for tool in tools:
        tool.fn = wrap(tool.fn)
