"""FastMCP server entrypoint for the NotebookLM MCP server.

Owns one long-lived NotebookLMClient so SDK keepalive state survives long
generation tasks. Auth is expected from NOTEBOOKLM_AUTH_JSON, typically
injected by Doppler.
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
    async with NotebookLMClient.from_storage() as client:
        runtime.set_client(client)
        try:
            yield
        finally:
            runtime.set_client(None)


mcp = FastMCP("notebooklm", lifespan=_lifespan)

# Register tools. Each module imports `mcp` from here and calls @mcp.tool().
from . import tools_basic, tools_podcast  # noqa: E402,F401


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


if __name__ == "__main__":
    main()
