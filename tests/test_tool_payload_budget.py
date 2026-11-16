"""Tripwire:`tools/list` 的總量有上限。

v0.9.26 時這份 payload 整份進每個 session 的 system prompt(實測 +14,336 tokens)。
Claude Code 現在預設延遲載入 MCP 工具:2.1.325 實測接上本 server 每個 session 只多
629 tokens(工具名),描述與 schema 等 ToolSearch 選到那支才載入。預算仍然要守:
不做延遲載入的 host 照付整份,Claude Code 每載入一支就付那一支。沒有上限的話它只會
單向長回去:每次加紅線都加在描述裡最省事,而代價不出現在任何人的畫面上。

紅了要做的**不是**把上限調高,是回答「這段新加的字,少了它呼叫端會不會做錯?」
- 會做錯 → 留在描述裡,把同一支裡的事故敘事/設計理由搬去
  `audi-skill/notebooklm/references/tool-reference.md`,總量換回來。
- 不會 → 它本來就該住在 reference 或 gotchas。

`inputSchema` 也算在內:參數描述來自 `_param_docs.py`,自動產生的 `title` 已拿掉。新增
參數描述一樣佔這份預算,只寫呼叫端看名字猜不到的事。
"""

import json

from notebooklm_mcp import app

# v0.9.26 瘦身後實測 37,165 字元(從 45,654 降下來,-19%)。留約 7% 緩衝:
# 加一兩條真正必要的紅線不會紅,長回一整段敘事會。
MAX_PAYLOAD_CHARS = 40_000

# 單支的上限:v0.9.26 之前 `podcast_attempt_retract` 一支就 4,657 字元(描述 3,981),
# 裡面大半是「P1 修復…同一根因第十一次現形…這句先前寫反了」這類版本考古,而真正的
# 規則(執行 safe_next_action 要用 safe_next_attempt_id)被埋在中間。單支過大本身就是
# 「規則被敘事淹沒」的訊號,所以分開鎖。
MAX_SINGLE_TOOL_CHARS = 3_200


async def test_tool_list_payload_stays_within_budget():
    tools = await app.mcp.list_tools()
    rows = sorted(
        (
            (len(json.dumps(t.model_dump(exclude_none=True), ensure_ascii=False)), t.name)
            for t in tools
        ),
        reverse=True,
    )
    total = sum(size for size, _ in rows)
    worst = "\n".join(f"    {size:>6}  {name}" for size, name in rows[:8])
    assert total <= MAX_PAYLOAD_CHARS, (
        f"tools/list 總量 {total} 字元 > 上限 {MAX_PAYLOAD_CHARS}。最大的幾支:\n{worst}\n"
        "先讀本檔開頭那兩個問題再決定怎麼做,不要直接調高上限。"
    )


async def test_no_single_tool_description_dominates():
    tools = await app.mcp.list_tools()
    oversized = {
        t.name: len(json.dumps(t.model_dump(exclude_none=True), ensure_ascii=False))
        for t in tools
        if len(json.dumps(t.model_dump(exclude_none=True), ensure_ascii=False))
        > MAX_SINGLE_TOOL_CHARS
    }
    assert not oversized, (
        f"這幾支單支就超過 {MAX_SINGLE_TOOL_CHARS} 字元:{oversized}。"
        "通常代表規則被事故敘事淹沒了 —— 敘事搬去 tool-reference,規則留下。"
    )
