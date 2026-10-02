"""Dry-run MCP server:serves the **real** notebooklm-mcp tool schemas, executes nothing.

用途:量 routing 正確性與 tool-list 的 token 成本,而不碰真實帳號/配額。
每次 tools/call 記進 $NBLM_EVAL_LOG(JSONL),回一句 DRY RUN —— 受測 agent 看到的
schema 與正式 server 逐字相同(直接從 notebooklm_mcp.app.mcp 讀回)。
"""

from __future__ import annotations

import json
import os
import sys

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

sys.path.insert(0, os.environ["NBLM_REPO"])
from notebooklm_mcp.app import mcp as real_mcp

LOG = os.environ.get("NBLM_EVAL_LOG", "/dev/null")
server = Server("notebooklm")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return await real_mcp.list_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool": name, "args": arguments}, ensure_ascii=False) + "\n")
    return [
        types.TextContent(
            type="text",
            text=f"DRY RUN —— 這是規劃用的離線環境,{name} 沒有真的執行。"
            f"已記錄參數:{json.dumps(arguments, ensure_ascii=False)[:400]}",
        )
    ]


async def main() -> None:
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    import anyio

    anyio.run(main)
