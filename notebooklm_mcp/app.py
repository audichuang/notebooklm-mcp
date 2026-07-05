"""Canonical FastMCP app + tool registration.

This lives in its OWN module (never run as ``__main__``) so there is exactly
ONE ``mcp`` instance regardless of how the server is launched. The earlier
design put this in ``server.py`` and registered tools via ``from .server import
mcp`` — but ``python -m notebooklm_mcp.server`` loads server as ``__main__`` AND
re-imports it as ``notebooklm_mcp.server``, creating TWO ``mcp`` objects: tools
registered on one, ``run()`` served the other (zero tools exposed over the MCP
protocol). Importing the app from a dedicated module avoids that duplication.

Owns one long-lived NotebookLMClient so SDK keepalive state survives long
generation tasks. Auth comes from NOTEBOOKLM_AUTH_JSON (typically Doppler).
"""
from __future__ import annotations

import argparse
import contextlib
from collections.abc import AsyncIterator

from mcp.server.fastmcp import FastMCP
from notebooklm import NotebookLMClient

from . import runtime


@contextlib.asynccontextmanager
async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
    # notebooklm-py 0.7.x:from_storage() 是同步函式,回傳可直接 async with 的
    # context(0.4.x「coroutine 必須 await」慣用法已走入歷史)。
    # keepalive=600 開啟 session 內背景 RotateCookies task(Google 自宣告的輪替
    # 週期即 600s):process-scoped、隨 server 生滅,讓跨小時長生成不因
    # __Secure-1PSIDTS 過期中途死。env-var 唯讀模式下只轉記憶體、不落盤,
    # 跨 session 的 cookie 老化仍靠 GUI 機重登 + sync-auth.sh。
    async with NotebookLMClient.from_storage(keepalive=600) as client:
        runtime.set_client(client)
        try:
            yield
        finally:
            runtime.set_client(None)


mcp = FastMCP("notebooklm", lifespan=_lifespan)

# Register tools. Each module imports `mcp` from here and calls @mcp.tool().
from . import tools_artifacts, tools_basic, tools_podcast, tools_publish  # noqa: E402,F401


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
