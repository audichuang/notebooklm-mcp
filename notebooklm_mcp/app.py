"""Canonical FastMCP app + tool registration.

This lives in its OWN module (never run as ``__main__``) so there is exactly
ONE ``mcp`` instance regardless of how the server is launched. The earlier
design put this in ``server.py`` and registered tools via ``from .server import
mcp`` — but ``python -m notebooklm_mcp.server`` loads server as ``__main__`` AND
re-imports it as ``notebooklm_mcp.server``, creating TWO ``mcp`` objects: tools
registered on one, ``run()`` served the other (zero tools exposed over the MCP
protocol). Importing the app from a dedicated module avoids that duplication.

Owns one long-lived NotebookLMClient for each MCP process. Auth comes from
NOTEBOOKLM_AUTH_JSON (typically Doppler).
"""
from __future__ import annotations

import argparse
import contextlib
import os
from collections.abc import AsyncIterator

from mcp.server.fastmcp import FastMCP
from notebooklm import NotebookLMClient

from . import runtime

_AUTH_JSON_ENV = "NOTEBOOKLM_AUTH_JSON"
_DISABLE_KEEPALIVE_ENV = "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE"
_HEADLESS_REAUTH_ENV = "NOTEBOOKLM_HEADLESS_REAUTH"

# inline auth(Doppler 注入 NOTEBOOKLM_AUTH_JSON)期間強制成這樣;None = 刪掉該變數。
# 共同理由:**任何會在本 process 內重鑄 cookie 的機制,在 inline 模式都是淨損失**——
# 新 cookie 只活在記憶體、寫不回 Doppler,下一個 stdio process 反而拿舊的啟動,
# 而重鑄本身還會把 3 VM 共用的那份作廢。
#   - DISABLE_KEEPALIVE_POKE=1:擋掉 from_storage() 冷啟動的 RotateCookies poke。
#   - HEADLESS_REAUTH 刪掉:0.8.0 新增的 L3「無頭重新認證」(用持久瀏覽器 profile
#     靜默重鑄 cookie)。它預設就是關的,但只要環境裡有人設了 =1 就會在 RPC 中途
#     自動觸發 —— 顯式壓掉,別讓紀律取決於別人的環境。VM 上也根本沒有那個 profile。
# 兩者都只在 inline 模式壓:登入機讀本機 storage_state 時,重鑄後寫得回檔案,是對的行為。
_INLINE_AUTH_ENV_OVERRIDES: dict[str, str | None] = {
    _DISABLE_KEEPALIVE_ENV: "1",
    _HEADLESS_REAUTH_ENV: None,
}


@contextlib.asynccontextmanager
async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
    # notebooklm-py 0.8.x:from_storage() 是同步函式,回傳可直接 async with 的
    # context(0.4.x「coroutine 必須 await」慣用法已走入歷史)。
    # MCP 一律**不傳 keepalive=**,再加上下面這組 env override(理由見上)。
    inline_auth = _AUTH_JSON_ENV in os.environ
    saved = {name: os.environ.get(name) for name in _INLINE_AUTH_ENV_OVERRIDES}
    if inline_auth:
        for name, value in _INLINE_AUTH_ENV_OVERRIDES.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    try:
        async with NotebookLMClient.from_storage() as client:
            runtime.set_client(client)
            try:
                yield
            finally:
                runtime.set_client(None)
    finally:
        if inline_auth:
            for name, old in saved.items():
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old


# Protocol-level server instructions: surfaced to ANY MCP client (even one
# without our external skill). Deliberately a SKELETON — workflow order + env
# vars + the few killer gotchas — with detail delegated to the skill, so it does
# not drift from SKILL.md (see the Cross-Repo Sync Checklist in AGENTS.md).
_INSTRUCTIONS = """\
建在 notebooklm-py 之上的薄 MCP + 確定性 podcast 續集工具。認證由環境變數
NOTEBOOKLM_AUTH_JSON 注入(通常來自 Doppler,唯讀)。

主流程(優先用高階工具,別自己拼低階步驟):
- 整季/一般單集生成 → podcast_series(episodes 放一集即單集)。若 host 已建立
  frozen generation input bundle，改用 podcast_episode(brief=null, manifest_path=...,
  input_bundle_path=...)(路徑與冪等規則見 skill)。有 manifest-backed attempt 時，
  依工具回傳的 safe_next_action 續跑。
- 發布成 Apple RSS → publish_series(讀 series_manifest.json;需 env
  PODCAST_PUBLIC_BASE_URL / PODCAST_TOKEN_SALT / PODCAST_UPLOAD_URL /
  PODCAST_UPLOAD_TOKEN)。
- 低階工具(notebook_* / source_* / artifact_* / chat_*)供救援與組裝,
  一般流程不需逐個手動呼叫。

鐵律:
- 任何長跑前先 auth_check;cookie 死了秒退,別燒掉數小時。
- start=N 只是 execution lower bound/trust boundary,不會重生已完成集。
- 長 MCP request 可能被 client cancellation 終止；可靠性來自 manifest checkpoint
  與重呼,不保證 server 在背景跑完。未傳 manifest_path 的 standalone call 僅 best-effort。
- QA/protected facts 是可選 host workflow,不是 MCP lifecycle；外部 manifest writer
  必須改用 MCP tool 或同一 ManifestStore,不能直接覆寫 JSON。
- streamable-http / sse 模式「無認證」——勿綁非 loopback host。

完整路由與參數細節見 notebooklm skill(audi-skill/notebooklm)。
"""

mcp = FastMCP("notebooklm", instructions=_INSTRUCTIONS, lifespan=_lifespan)

# Register tools. Each module imports `mcp` from here and calls @mcp.tool().
from . import (  # noqa: E402,F401
    tools_artifacts,
    tools_basic,
    tools_podcast,
    tools_publish,
    tools_research,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="NotebookLM MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8484)
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)
