# 2026-10-22 真實驗收 —— 附件被配額拒絕的那一腿留下的空殼

**驗什麼**:`_dispatch_attachment` 的每腿對帳(ADR-0011 Amendment 2026-10-22)在真帳號上記不記得到,
以及 v0.9.33-rc ④ 那顆 `pending` 空殼的終態。

**環境**:Doppler `notebooklm/stg`,pool 只留兩個免費槽位(pool-account-1、pool-account-2;
`_3`…`_9` 在進 lifespan 前移除,付費兜底 `_8`/`_9` 與 prd 共用帳號,刻意不碰)。直接呼叫 working
tree 的 `tools_artifacts.generate_slides`(`package:` 印的是 repo 內的 `notebooklm_mcp/__init__.py`),
`slide_length="short"`、`wait_timeout=5`(只要 dispatch 與稽核,不等下載)。一本 `ZZ-TEST` notebook +
一筆貼上文字來源,跑完已刪除(`NotebookNotFoundError` 確認)。

## 結果

| call | 起始帳號 | 結果 | 遠端新增 | `attachment_errors` |
|---|---|---|---|---|
| 1 | pool-account-1 | 受理 | 1 顆 `in_progress` | — |
| 2 | pool-account-1 | 受理 | 1 顆 `in_progress` | — |
| 3 | pool-account-1 | `RateLimitError`(`USER_DISPLAYABLE_ERROR`,2.65s)→ 換 pool-account-2 受理 | 1 顆 `in_progress` + **1 顆 `pending`(早 5 秒)** | `attachment_dispatch_failover`,`stray_artifact_ids=[空殼]` ✅ |
| 4 | pool-account-2 | 受理 | 1 顆 `in_progress` | — |
| 5 | pool-account-2 | `RateLimitError`(2.73s),pool 耗盡 → raise | **1 顆 `pending`** | `attachment_dispatch_refused`,`stray_artifact_ids=[空殼]` ✅ |

- 免費帳號的 slides 配額:每帳號第 3 次被拒(兩個帳號一致)。
- **被拒 2/2 都留下空殼**(v0.9.16 量到 0/3,v0.9.33-rc 1/1)—— 不是罕見情況,至少今天不是。
- 輪詢 40 分鐘(每 2 分鐘):4 顆正常的 5–9 分鐘內 `completed`;**兩顆空殼始終 `pending`**。

## 結論

- 空殼不生成:換帳號重送不會產生重複的簡報,ADR-0010/0011 的換帳號決策不必改。
- 分辨法是 status:空殼一出現就是 `pending`、標題是 notebook 名稱;真的在跑的是 `in_progress`。
  舊指引「有那顆就等它的終態」會讓人永遠等下去 —— 已改。
- 沒量到:空殼算不算配額(被拒的帳號本來就被拒,看不出差別)、空殼會不會在更久之後被伺服器清掉。
- MCP 沒有 artifact 刪除工具,空殼會留在 Studio;隨 notebook 刪除即消失。
