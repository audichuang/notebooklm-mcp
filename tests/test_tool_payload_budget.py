"""Tripwire:`tools/list` 的總量有上限。

這份 payload 會被灌進**每一個**連上這台 server 的 session 的 system prompt,不管那個
session 用不用得到 NotebookLM。v0.9.26 實測(`claude -p --output-format json` 的
`usage`):沒接 MCP 的 session 18,685 tokens,接上之後 33,021 —— 工具清單本身就是
**14,336 tokens**,而且每多一個 turn 就在 `cache_read` 裡再付一次。

所以描述的長度是**跨所有使用者的固定成本**,不是這個 repo 內部的事。沒有上限的話它
只會單向長回去:每次加紅線都加在描述裡最省事,而代價不出現在任何人的畫面上。

紅了要做的**不是**把上限調高,是回答「這段新加的字,少了它呼叫端會不會做錯?」
- 會做錯 → 留在描述裡,把同一支裡的事故敘事/設計理由搬去
  `audi-skill/notebooklm/references/tool-reference.md`,總量換回來。
- 不會 → 它本來就該住在 reference 或 gotchas。

`inputSchema` 也算在內,但那一半**不是靠文字瘦身能動的** —— 它由參數名與型別推導
(全 repo 沒有任何 `Field(description=...)`),要降只能刪參數。所以實務上這條上限
擋的是 docstring。
"""
import json

from notebooklm_mcp import app

# v0.9.26 瘦身後實測 36,144 字元(從 45,654 降下來,-21%)。留約 10% 緩衝:
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
        ((len(json.dumps(t.model_dump(exclude_none=True), ensure_ascii=False)), t.name)
         for t in tools),
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
        if len(json.dumps(t.model_dump(exclude_none=True), ensure_ascii=False)) > MAX_SINGLE_TOOL_CHARS
    }
    assert not oversized, (
        f"這幾支單支就超過 {MAX_SINGLE_TOOL_CHARS} 字元:{oversized}。"
        "通常代表規則被事故敘事淹沒了 —— 敘事搬去 tool-reference,規則留下。"
    )
