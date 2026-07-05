"""Fail when MCP tool names drift from the notebooklm skill docs."""
from __future__ import annotations

import asyncio
from pathlib import Path

from notebooklm_mcp import app


ROOT = Path(__file__).resolve().parents[1]
CI_SKILL_DIR = ROOT / "audi-skill" / "notebooklm"
LOCAL_SKILL_DIR = Path("/home/user/research/audi-skill/notebooklm")
SKILL_DIR = CI_SKILL_DIR if CI_SKILL_DIR.exists() else LOCAL_SKILL_DIR
SKILL_MD = SKILL_DIR / "SKILL.md"
TOOL_REFERENCE = SKILL_DIR / "references" / "tool-reference.md"


async def _tool_names() -> list[str]:
    tools = await app.mcp.list_tools()
    return sorted(tool.name for tool in tools)


def _missing(tool_names: list[str], path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [name for name in tool_names if f"`{name}`" not in text]


async def main() -> int:
    missing_files = [path for path in (SKILL_MD, TOOL_REFERENCE) if not path.exists()]
    if missing_files:
        for path in missing_files:
            print(f"missing skill file: {path}")
        return 1

    names = await _tool_names()
    failures: list[str] = []
    for path in (SKILL_MD, TOOL_REFERENCE):
        missing = _missing(names, path)
        if missing:
            failures.append(f"{path}: missing {', '.join(missing)}")

    if failures:
        print("\n".join(failures))
        return 1

    print(f"skill docs cover {len(names)} MCP tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
