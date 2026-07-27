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


#: Contract term coverage(**不是**完整的 schema↔docs sync)。工具名沒變的改動——新參數、
#: 新回傳欄位——原本完全逃得過本 checker:v0.3.3 加了 `converted_from` /
#: `require_slides` / `require_report`,忘記寫文件 CI 照樣綠。這裡只硬性要求
#: tool-reference 提到這幾個字,不做 signature 解析。加新參數時把它加進來。
REQUIRED_CONTRACT_TERMS = (
    "converted_from",
    "require_slides",
    "require_report",
    "custom_prompt",     # generate_report 的 custom 格式;工具名沒變,只有新參數
    "include_report",    # research_import:報告 entry 沒有 URL,只能靠這個旗標指名
)


def _missing(tool_names: list[str], path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [name for name in tool_names if f"`{name}`" not in text]


def _missing_terms(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [term for term in REQUIRED_CONTRACT_TERMS if f"`{term}`" not in text]


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

    missing_terms = _missing_terms(TOOL_REFERENCE)
    if missing_terms:
        failures.append(
            f"{TOOL_REFERENCE}: missing contract term(s) {', '.join(missing_terms)}"
        )

    if failures:
        print("\n".join(failures))
        return 1

    print(
        f"skill docs cover {len(names)} MCP tools "
        f"+ {len(REQUIRED_CONTRACT_TERMS)} contract terms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
