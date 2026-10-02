"""scripts/check_skill_sync.py 208 行零測試——這裡補兩支:fail-closed 缺檔,
與少一個 required contract term 時訊息真的點名該 term。

`scripts/` 不是套件(沒有 __init__.py),用 importlib 直接從檔案路徑載入,不透過
sys.path[0]=scripts/ 那條路(那條路才需要 PYTHONPATH=$(pwd) 這個 gotcha——見
AGENTS.md 與這支腳本自己的呼叫慣例)。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_skill_sync.py"


def _load_check_skill_sync():
    spec = importlib.util.spec_from_file_location("check_skill_sync", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _full_doc(names, terms) -> str:
    """一份把每個工具名與每個 term 都用反引號包住的假文件——除了測試刻意拿掉的
    那一個,其餘全部滿足,才不會被別的缺項蓋過真正要驗的那一條斷言。"""
    lines = [f"- `{name}`" for name in names] + [f"- `{term}`" for term in terms]
    return "\n".join(lines) + "\n"


async def test_missing_skill_files_is_fail_closed(tmp_path, capsys):
    """缺檔(SKILL.md 或 tool-reference.md 任一份不存在)必須回 1,不能因為後面的
    工具名/contract term 檢查都跳過而誤判成過。"""
    module = _load_check_skill_sync()
    skill_md = tmp_path / "SKILL.md"
    tool_reference = tmp_path / "references" / "tool-reference.md"

    rc = await module.main(skill_md=skill_md, tool_reference=tool_reference)

    out = capsys.readouterr().out
    assert rc == 1
    assert f"missing skill file: {skill_md}" in out
    assert f"missing skill file: {tool_reference}" in out


async def test_missing_contract_term_fails_and_names_it(tmp_path, capsys):
    """tool-reference.md 少了一個 REQUIRED_CONTRACT_TERMS 裡的字,main() 要回 1
    且訊息裡點名那個 term——不是只講「有東西不見了」。"""
    module = _load_check_skill_sync()
    names = await module._tool_names()

    skill_dir = tmp_path / "skill"
    (skill_dir / "references").mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    tool_reference = skill_dir / "references" / "tool-reference.md"

    missing_term = "converted_from"
    assert missing_term in module.REQUIRED_CONTRACT_TERMS
    remaining_terms = [t for t in module.REQUIRED_CONTRACT_TERMS if t != missing_term]
    tool_reference.write_text(_full_doc(names, remaining_terms), encoding="utf-8")
    skill_md.write_text(_full_doc(names, module.SKILL_MD_REQUIRED_TERMS), encoding="utf-8")

    rc = await module.main(skill_md=skill_md, tool_reference=tool_reference)

    out = capsys.readouterr().out
    assert rc == 1
    assert missing_term in out


async def test_fully_covered_docs_pass(tmp_path, capsys):
    """對照組:兩份文件把每個工具名與每個 required term 都覆蓋到時必須回 0——
    確保上面兩支紅的是真的因為少了東西,不是 fixture 本身就造不出綠燈。"""
    module = _load_check_skill_sync()
    names = await module._tool_names()

    skill_dir = tmp_path / "skill"
    (skill_dir / "references").mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    tool_reference = skill_dir / "references" / "tool-reference.md"

    tool_reference.write_text(_full_doc(names, module.REQUIRED_CONTRACT_TERMS), encoding="utf-8")
    skill_md.write_text(_full_doc(names, module.SKILL_MD_REQUIRED_TERMS), encoding="utf-8")

    rc = await module.main(skill_md=skill_md, tool_reference=tool_reference)

    assert rc == 0
