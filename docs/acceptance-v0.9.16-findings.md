# notebooklm-mcp v0.9.16 真實環境驗收 — FINDINGS

驗收日期:**2026-08-17**(19:37 – 21:06 CST / UTC+8)
工作區:`/home/user/research/audiskill/nblm-acceptance-v0.9.16`
帳號 pool:Doppler `notebooklm` / **`-c stg`**(9 槽,全程未動 Doppler)
實裝:`notebooklm-mcp 0.9.16` / `notebooklm-py 0.8.1` / `mcp 1.29.0`
直譯器:`/home/user/.local/share/uv/tools/notebooklm-mcp/bin/python`
驗收 notebook:`66f31068-5997-4c43-9960-0973084269d1` — **已刪除**(21:08)
額外素材 notebook:`bddb2202-0f5b-483a-8d4b-74e9d00e6d02`(權限測試用,刻意不分享)— **已刪除**
兩本刪除後 owner(槽位 1)的 `notebooks.list()` = `[]`,遠端無殘留。

**未執行**:`publish_series`(CLAUDE.md 鐵律 2)。
**未走到 prd 槽位**:槽位 8 `pool-account-7` / 槽位 9 `pool-account-5` 全程沒有被 dispatch 過
(鐵律 3 的停止線沒有被觸及 —— 免費槽位只用掉 3 個)。

---

## 五個問題的答案

### Q1 — `generate_slide_deck` 撞配額的形狀 ✅ 答完

- **例外型別**:同步 `raise notebooklm.exceptions.RateLimitError`
  —— **不是**回 `task_id=""` + `is_failed` 的 status 物件。
  所以走的是 `_failover.REFUSED_WITHOUT_DISPATCH` 那條 except,**不是** `ensure_started`
  之後的「有 id + failed」分支。訊息逐字:
  `API rate limit or quota exceeded. Please wait before retrying. (Upstream: Resource exhausted.)`
- **dispatch → 拒絕的秒數**:**0.63s / 0.75s**(獨立探針,`time.monotonic()` 夾 kickoff 那一個
  await);SDK 自報同一顆 RPC 是 `RPC CREATE_ARTIFACT failed after 0.677s:
  RateLimitError rpc_code=USER_DISPLAYABLE_ERROR`。
  → 與 ADR-0010 在 audio 上量到的 1.35 / 1.43s **同一量級的同步拒絕**(slides 更快一點)。
- **`artifact_list(kind="slide_deck")` 拒絕前後差集**:**三次量測全部為空**
  (探針 2 次:before=4 after=4;單帳號耗盡 1 次:before=5 after=5)。
  → ⭐ **「零副作用拒絕」在 slides 上成立**,重送是冪等的。
- **`attachment_errors` 那一筆**(MCP 真實路徑落地,逐字):
  ```json
  {"phase": "attachment_dispatch_failover", "kind": "slides", "type": "RateLimitError",
   "message": "API rate limit or quota exceeded. Please wait before retrying. (Upstream: Resource exhausted.)",
   "recorded_at": "2026-08-17T12:11:41.326015+00:00",
   "from_account": "pool-account-1@example.invalid", "to_account": "pool-account-2@example.invalid"}
  ```
  `from_account` / `to_account` 與實際 dispatch 的帳號**對得上**(見下方 §Phase 3 逐次表:
  第 4 次呼叫的成品 `slides_account` = `pool-account-2`,正是 `to_account`)。
- **全部耗盡時的終態**(單帳號 pool,本機 env 層造,Doppler 未動):
  **原樣拋 `RateLimitError`**,`attachment_dispatch_refused` 記在**最後那一腿**的帳號
  (`account: "pool-account-1@example.invalid"`),**零 failover 紀錄**(rotate 找不到下一個帳號時
  沒有寫下任何從未發生的換帳號),provenance 欄位保持 `None`,artifact 差集空。
  → `_failover` 紅線⑤(`tried` 擋在稽核寫入之前)與 `on_clean_refusal` 的「補最後一腿」
  都得證。
- **結論**:**零副作用拒絕在 slides 上成立**。ADR-0010 只在 audio 上量過的那個形狀,
  在 slides 上逐項對得上(同步 raise、同一個例外型別、同量級秒數、零 artifact 副作用)。
  failover 掛在正確的分支上。

### Q2 — 簡報與音檔是同一份配額嗎 ✅ 答完:**分開的**

兩個獨立證據:

| 帳號 | slides | audio | 時間 |
|---|---|---|---|
| 槽位 1 `pool-account-1` | 已耗盡(連 3 次 `RateLimitError`) | **受理**(4.66s,`task_id=341fc3f0…`,`in_progress`) | 20:21 拒 → 20:36 audio 受理 |
| 槽位 2 `pool-account-2` | **被拒**(12:49:31 UTC failover 走) | **受理**(12:47:23 UTC `accepted`) | 同一 process、相隔 2 分鐘 |

槽位 2 那一列特別硬:**同一個 MCP process、同一個帳號、相隔 2 分鐘**,audio dispatch 被受理
而 slides dispatch 被拒。

- **結論**:slides 與 audio 是**兩份獨立配額**。pool 的容量模型要按 kind 分開算 ——
  「這個帳號今天用完了」不是一個布林值,至少是 per-kind 的。
- 額外發現(見 FINDING-1):**`REVISE_SLIDE` 又是第三份**,而且看不出上限。

### Q3 — `revise_slide` 被拒時遠端會不會 fork ⚠️ **無法觀測,待確認**

原始問題答不了,因為**打不到 revise 的配額拒絕**:

- 在 slide_deck 生成配額**已完全耗盡**的槽位 1 上,連送 **9 次** `revise_slide` kickoff,
  **9 次全部受理**(2.11s – 3.64s),每次 fork 出一顆 `System Performance Physics (2)…(10)`,
  而且事後查 `artifact_list` 這些 fork **都真的 `completed`**(不是假受理)。
- 所以 `artifact_revise_slide` 那條 failover 分支,在 stg 上**沒有已知的觸發條件**。
- **成功時的 fork 行為與 v0.9.0 實測一致**(見 §Phase 5):回傳新 id、`superseded_artifact_id`
  是舊的、遠端多一顆 `(2)`、舊那顆原封不動。
- **結論**:**待確認**。「被拒也會 fork」這個風險**沒有被排除,也沒有被證實**。
  下一輪若要驗,得先找到能讓 `REVISE_SLIDE` 被限流的條件(9 次不夠)。

### Q4 — 兩個稽核面互不污染 ✅ 答完:不污染

做法:`podcast_episode`(EP01)與 `generate_slides`(EP01,**同一集**)在**同一個 MCP process
並行**跑,兩者都撞到配額換帳號。同一個 episode dict 的兩個稽核面:

**附件稽核面(episode 級)**
```
slides_account     = 'pool-account-3@example.invalid'
slides_artifact_id = da3d98e0-0f84-4f1c-86ae-5b0a213938b5
attachment_errors  = [
  {failover RateLimitError pool-account-1@example.invalid -> pool-account-2@example.invalid  12:11:41},
  {failover RateLimitError pool-account-2@example.invalid -> pool-account-3@example.invalid  12:49:31},
]
```

**音檔稽核面(attempt 級)**
```
attempts[0].attempt_id        = f103df6c-dc83-4bc0-858c-38d0f2ef0c82
attempts[0].dispatch.account  = 'pool-account-2@example.invalid'   status=accepted
attempts[0].dispatch.artifact_ids_before = ['341fc3f0-186d-496b-a178-1ba3eb192849']
attempts[0].remote            = b4b4bd36-a1f3-45f0-9597-69e3562b37e5  completed
attempts[0].errors            = []
attempts[0].finalize.*        = artifact_rename / download /
                                feedback_source_upload / feedback_source_rename 皆 completed
```

**交叉污染檢查(全部否)**
| 檢查 | 結果 |
|---|---|
| episode 頂層出現 `errors[]`(音檔欄位跑到附件層) | 否 |
| attempt 裡出現 `attachment_errors` | 否 |
| attempt 裡出現 `slides_*` | 否 |
| 附件的 failover 有沒有寫進 `attempts[].errors[]` | 否(`errors=[]`) |

- 附帶得證:`dispatch.artifact_ids_before` 正確把探針產物 `341fc3f0` 算進基準線
  —— reconcile 的比對基準沒有被並行的附件生成污染。
- **結論**:兩個稽核面各記各的,**零交叉**。共用迴圈用 callback 表達家族差異、迴圈本身不分岔
  這個設計在真實並行下成立。

### Q5 — 換帳號後的下載身分 ✅ 答完:身分跟著換

- `slides_account` vs 實際 dispatch 的帳號:**一致**。兩次 failover 之後
  `slides_account = 'pool-account-3@example.invalid'`,正是 `attachment_errors` 最後一筆的
  `to_account`,不是起始帳號、也不是「此刻游標指到誰」。
- **PDF 下載成功**:16,452,408 bytes、`PDF document, version 1.4, 17 page(s)`,
  `_validate_pdf` 過。沒有出現 v0.9.0 那個「十幾分鐘後爆 401」的症狀。
- 三次 failover 情境(第 4 / 第 7 次 slides、Phase 5 revise)全部下載成功。
- **結論**:身分釘到 dispatch **並且**跟著走到 finalize。

---

## Phase 0 — 本機檢查

`bash local-checks.sh` → **通過 28 / 失敗 0**。

> 📌 小差異:README §階段順序寫「26 條離線斷言」,實際輸出 28 條(第 0 節 4 條 + 1~10 節 24 條)。
> 收工時把 README 的數字改掉即可,不是問題。

## Phase 1 — 槽位狀態盤點(輸入,不是通過條件)

方法沿用 v0.9.14:走**我們自己的 `app._lifespan`**(持有 rotation flock),不是 SDK 直建 client。
`RotateCookies` 出現 **0 次**,`would_trigger_inline_heal` warning **0 次**。

| 槽位 | env key | label | PSIDTS domain / 剩餘 / len | 真 RPC | 拿它測了什麼 |
|---|---|---|---|---|---|
| 1 | `NOTEBOOKLM_AUTH_JSON` | `pool-account-1@example.invalid` | `.google.com` +356.6d 77 | OK nb=0 | slides 打到耗盡 → Q1 / Q2 / Q3 / 耗盡終態 |
| 2 | `_2` | `pool-account-2@example.invalid` | `.google.com` +356.6d 77 | OK nb=0 | 第 1 腿 failover 目標 → 打到耗盡 → Q2 / Q4;權限測試的 outsider |
| 3 | `_3` | `pool-account-3@example.invalid` | `.google.com` +356.6d 78 | OK nb=0 | 第 2 腿 failover 目標 → Q5;權限測試的第二個 outsider |
| 4 | `_4` | `pool-account-4@example.invalid` | `.google.com` +356.9d 77 | OK nb=4 | 未動用 |
| 5 | `_5` | `pool-account-8@example.invalid` | `.google.com` +356.9d 78 | OK nb=9 | 未動用 |
| 6 | `_6` | `pool-account-6@example.invalid` | `.google.com` +356.9d 77 | OK nb=0 | 未動用 |
| 7 | `_7` | `pool-account-9@example.invalid` | `.google.com` +356.9d 78 | OK nb=2 | 未動用 |
| 8 | `_8` | `pool-account-7@example.invalid` | `.google.com` +356.6d 77 | OK nb=20 | **prd 槽,停止線,全程未觸及** |
| 9 | `_9` | `pool-account-5@example.invalid` | `.google.com` +356.6d 77 | OK nb=1 | **prd 槽,停止線,全程未觸及** |

**9 槽全健康、全 routable、真 RPC 9/9 成功。** 所以 README §Phase 1 說的「不健康的槽位才是
免費的判別實驗」**這一輪沒有現成素材**:三種不健康裡

- **配額耗盡** → 自己造出來了(Phase 3 打爆槽位 1、2),✅ 走 rotate。
- **權限不足** → 用本機 env 層造(見 §Phase 6④),✅ 不 rotate。
- **認證失效(cookie 死)** → **沒有測**。stg 天生沒有這種槽位,而造一個要動 env 層的
  cookie 值;v0.9.14 已在 stg 上驗過同一條(`probe_auth` fail-loud、不 rotate),
  這一輪的改動不碰那條路,所以刻意不重做。

## Phase 2 — 素材

| 步驟 | 結果 |
|---|---|
| `notebook_create` | `66f31068-…`,`shared_with` = 其餘 **8 個帳號**(pool 全員 EDITOR) |
| `source_add_file` × 4 | 4 筆全 `ready=true`(496 / 357 / 412 / 425 char) |
| `notebook_share_with_pool` | **冪等確認**:`shared_with=[]`、`already_shared` = 8 個帳號 |
| `chat_ask`(指名 EP01 兩筆) | 回答正確、帶 2 筆 citation,`conversation_id` 正常 |
| `source_list` | 4 筆,`ready=true` |

## Phase 3 ⭐ — 連續生簡報直到撞配額

**逐次紀錄**(兩個出口都貼:工具回傳值 + manifest 落地):

| # | 集 | 工具回傳 `artifact_id` | manifest `slides_account` | 新增稽核 | dispatch(UTC) |
|---|---|---|---|---|---|
| 1 | EP01 | `ce57056c` | `pool-account-1`(槽 1) | — | 11:43:26 |
| 2 | EP02 | `40cef2ae` | `pool-account-1`(槽 1) | — | 11:52:53 |
| 3 | EP01 | `89b7ed39` | `pool-account-1`(槽 1) | — | 12:02:23 |
| **4** | EP01 | `4642131c` | **`pool-account-2`(槽 2)** | **failover 槽1→槽2** | 12:11:43 |
| 5 | EP01 | `37a4cd3e` | `pool-account-2`(槽 2) | —(游標已在槽 2) | 12:22:13 |
| 6 | EP01 | `fea6c82d` | `pool-account-2`(槽 2) | — | 12:32:49 |
| **7** | EP01 | `da3d98e0` | **`pool-account-3`(槽 3)** | **failover 槽2→槽3** | 12:49:31 |

**免費帳號的 slide_deck 每日配額 = 3 次**,兩個槽位完全一致(槽 1:第 1/2/3 次成功,第 4 次被拒;
槽 2:第 4/5/6 次成功,第 7 次被拒)。每次生成 wall clock ≈ 9–10 分鐘。

**拒絕不消耗配額**:槽位 1 耗盡後又被送了 12 次 kickoff(探針 2 次 + 單帳號 1 次 + revise 9 次),
配額計數沒有錯亂,`artifact_list` 差集全空。

`attachment_errors` 最終落地(EP01,append-only、兩筆都留著):
```json
[{"phase":"attachment_dispatch_failover","kind":"slides","type":"RateLimitError",
  "from_account":"pool-account-1@example.invalid","to_account":"pool-account-2@example.invalid",
  "recorded_at":"2026-08-17T12:11:41.326015+00:00", "message":"API rate limit or quota exceeded. …"},
 {"phase":"attachment_dispatch_failover","kind":"slides","type":"RateLimitError",
  "from_account":"pool-account-2@example.invalid","to_account":"pool-account-3@example.invalid",
  "recorded_at":"2026-08-17T12:49:31.905070+00:00", "message":"API rate limit or quota exceeded. …"}]
```

**三個 phase 只驗到 2 個**:`attachment_dispatch_failover` ✅、`attachment_dispatch_refused` ✅
(耗盡 + 權限兩種情境各一次)、**`attachment_acceptance_unknown` 全程沒有觸發過**
—— 沒有任何一次 dispatch 落進「受理不明」。如實記錄,不是通過也不是失敗。

## Phase 4 — 音檔(與 Phase 3 並行,見 Q2 / Q4)

`podcast_episode(episode_n=1, audio_length="short")` 全綠:
`mp3_path=output/ep01.mp3`(12,294,079 bytes)、`artifact_id=b4b4bd36`、
`feedback_source_id=065d3f26`、`published_at="Mon, 17 Aug 2026 20:54:56 +0800"`、
`finalize` 四步全 `completed`、`attempts[0].errors=[]`。
音檔用槽位 2 dispatch 並**一次受理**(當時槽位 2 的 slides 已經在限流邊緣,2 分鐘後就被拒)。

## Phase 5 — `revise_slide`(成功路徑)

`artifact_revise_slide(episode_n=1, artifact_id=da3d98e0, slide_index=0, prompt="把第一頁改短")`

| 出口 | 內容 |
|---|---|
| 工具回傳 | `artifact_id=cb9cb010`(**新的**)、`superseded_artifact_id=da3d98e0`(舊的)、`slide_index=0` |
| manifest | `slides_artifact_id` 回寫成 `cb9cb010`、`slides_account='pool-account-3'`(revise 這次實際送出的帳號)、`attachment_errors` **沒有新增** |
| 遠端 | `artifact_list` 多一顆 `<原標題> (2)`,舊那顆原封不動 —— **與 v0.9.0 實測一致** |
| 檔案 | `ep01-slides.pdf` 重新下載,17 頁 |

`_PROVENANCE_FIELDS["revise_slide"]` 指向 slides 那一組欄位,落地行為與離線斷言(§3)一致。

## Phase 6 — 救援與錯誤路徑(0 配額)

### ① provenance 語意:救援下載不准聲稱知道誰生的 ✅
`artifact_download_slides(artifact_id=89b7ed39)` →
`slides_account` 變成 **`None`**、`slides_artifact_id` 變成 `89b7ed39`、PDF 換成那顆的內容
(11 頁),`attachment_errors` 不變。

### ② 本地錯誤不准留假的遠端事件(這一版修掉的 F4)✅
`language="zh-TW"` → `ValueError`(訊息列出全部合法 code)。
`revision` 25→25、`attachment_errors` 2→2 —— **一筆假的 `acceptance_unknown` 都沒有**。

**RPC 計數硬驗**(數 lifespan 之後的 httpx POST):

| 呼叫形狀 | 結果 | POST | revision | attachment_errors |
|---|---|---|---|---|
| `language` 錯 + **不傳** `source_ids` | `ValueError` | **0** | 25→25 | 2→2 |
| `language` 錯 + **傳** `source_ids` | `ValueError` | **1** | 25→25 | 2→2 |
| `episode_n=99` | `ValueError` | **0** | 25→25 | 2→2 |

> 📌 那 1 次 POST 是 `assert_sources_exist` 的**唯讀** `rLM1Ne`(source 存在性 preflight),
> 排在 `resolve_language` 之前。README §Phase6② 寫「零 RPC」——**傳 `source_ids` 時嚴格說
> 不是零 RPC,是零生成 RPC**。行為正確(那次唯讀 preflight 是刻意的設計),只是文件用詞
> 可以精確一點。

### ③ 打錯集號要在燒配額前 raise ✅
`episode_n=99` → `ValueError: episode 99 not found in manifest …`,**POST=0**,manifest 不動。

### ④ 紅線③:權限被拒**不 rotate** ✅(額外補的,0 配額)
素材:槽位 1 用 SDK 直建一本**不分享**的 notebook `bddb2202-…`;pool 在**本機 env 層**
改成 `[槽位2, 槽位3]`(Doppler 一個字沒動),兩者都看不到那本。

| 斷言 | 結果 |
|---|---|
| 例外型別 | `notebooklm_mcp._errors.NotebookAccessDenied`(不是上游原始的 permission denied) |
| 訊息 | 帶著 `notebook_share_with_pool(notebook_id=...)` 指引 + 指名帳號與 notebook_id |
| `attachment_errors` | **只有 1 筆**,`phase="attachment_dispatch_refused"`、`type="NotebookAccessDenied"`、`account="pool-account-2@example.invalid"` |
| 有沒有偷偷換帳號 | **沒有** —— 零 `attachment_dispatch_failover`(pool 還有第 2 個帳號可換,它沒換) |
| 走哪個終態 | **乾淨終態**(refused),**不是** `acceptance_unknown` |
| artifact 差集 | 空 |

---

## FINDINGS

### FINDING-1 — `REVISE_SLIDE` 與 `CREATE_ARTIFACT` 是**不同配額桶**,而 ADR-0011 把 revise 當「generate 家族的第三支」

- **症狀**:在 slide_deck 生成配額已完全耗盡(連 3 次 `RateLimitError`)的槽位 1 上,
  `revise_slide` 連 **9 次** kickoff **全部受理**且 fork 都真的完成。
- **重現**:對任一已 slides 耗盡的槽位,直接 `client.artifacts.revise_slide(...)` 連打。
- **根因**:兩支走**不同 RPC method** —— `generation.py` 的
  `RPCMethod.REVISE_SLIDE` vs 生成用的 `CREATE_ARTIFACT`(SDK log 印的就是
  `RPC CREATE_ARTIFACT failed …`)。伺服器端顯然按 RPC 分桶。
- **判斷**:**不是 bug**,是**風險模型偏差**。`tools_artifacts.py:440` 的註解寫
  「**改版也燒配額,也會被同步拒絕** —— 它是 generate 家族的第三支」;前半句成立
  (它確實建 artifact),**後半句在 stg 上打不到**。後果是 `artifact_revise_slide`
  那條 failover 分支**沒有已知觸發條件**,也就是 Q3 永遠問不出答案。
- **建議**:①把這個實測寫進 ADR-0011 / docstring(「revise 不吃 slide_deck 的桶,
  上限未知」),別讓下一輪再花一次真實配額發現同一件事;②Q3 保持「待確認」,
  不要因為「這一輪沒出事」就把 revise 的重送當成已證明冪等。

### FINDING-2 — 配額不是一個布林值,是 per-kind(而且 slides 的每日額度 = 3)

- **觀察**:同一帳號同一時刻,slides 被拒 / audio 受理 / revise 受理(三份獨立);
  免費帳號 slide_deck 每日 **3 次**,兩個槽位完全一致。
- **判斷**:**預期行為**,但**沒有被寫在任何地方**。ADR-0010 的容量模型讀起來像
  「一個帳號用完就換下一個」。
- **建議**:寫進 `docs/gotchas-pool.md` —— 「9 槽 × 3 = 每日 27 份簡報」這種算法才成立,
  而 audio 與 revise 各自另計。這直接影響整季規劃該怎麼排。

### FINDING-3 — `attachment_acceptance_unknown` 這一輪完全沒有觸發過

- **觀察**:三個 phase 只驗到 2 個。「受理不明」需要「非 `REFUSED_WITHOUT_DISPATCH`
  的例外」或「有 id + failed」,兩者都沒有自然發生。
- **判斷**:**待確認**(不是 bug)。這條路只有離線測試守著,真實形狀從未被觀測。
- **建議**:不值得為它燒真配額;但 `_failover` 那條分支的離線測試要維持突變驗證,
  因為它沒有任何真實驗收背書。

### FINDING-4 — README 兩處文件精確度(小)

1. §階段順序寫「26 條離線斷言」,`local-checks.sh` 實際輸出 **28** 條。
2. §Phase 6② 寫 `language` 錯誤「應該 ValueError、**零 RPC**」——傳 `source_ids` 時實測
   **POST=1**(`assert_sources_exist` 的唯讀 preflight,排在 `resolve_language` 之前)。
   應寫成「零**生成** RPC / 零稽核寫入」。
- **判斷**:文件用詞,非程式問題。行為本身正確。

### 非 FINDING(確認符合設計,列出以免下一輪重測)

- 零副作用拒絕在 slides 上成立(Q1),failover 掛在 `REFUSED_WITHOUT_DISPATCH`。
- 耗盡終態:原樣拋 + `refused` 記最後一腿 + 零假 failover 紀錄。
- 權限被拒不 rotate、走乾淨終態、指引訊息落地。
- 兩個稽核面零交叉污染(真實並行下)。
- 身分跟著 client 走到 finalize(下載用換過去那個帳號)。
- 救援下載寫 `account=None`。
- F4(本地轉換擋在 closure 外)在真實路徑上成立。
- pool 啟動零 `RotateCookies`、零 heal warning。

---

## 有哪幾條寫得成離線測試

> ⚠️ **本節第一版的評估過度樂觀,已更正。** 逐條比對 `tests/test_attachment_failover.py`
> 與 `tests/test_tools_artifacts.py` 之後,原本列的四條候選裡**只有一條有真缺口** ——
> 其餘都已經被既有測試完整涵蓋。誠實記下來,免得下一輪又「補」一次已經存在的鎖。

| 候選 | 狀態 | 憑據 |
|---|---|---|
| 1. per-kind 配額(FINDING-1/2) | **寫不成離線測試** | 它是伺服器行為。該做的是改 docstring / ADR 文字,見 FINDING-1 建議 |
| 2. 耗盡終態的不變量 | **已被涵蓋** | `test_single_account_pool_still_records_the_refusal`(單帳號:恰好一筆 `refused` + 帳號,list 相等式順帶鎖住「零 failover 紀錄」)、`test_all_accounts_exhausted_raises_after_trying_each_once`(多帳號:兩筆 failover + 最後一腿 `refused`) |
| 3. 權限被拒的不變量 | **①②④ 已被涵蓋,③ 是真缺口 → 已補** | 見下方 |
| 4. 本地錯誤零稽核 + 零 RPC | **已被涵蓋(而且比我想的更全)** | `test_local_argument_errors_are_not_recorded_as_remote_events` 已 parametrize `language` / `slide_format` / `report_format` × slides / report,並斷言 `artifacts.calls == []` 與 `"attachment_errors" not in episode` |
| (額外查)`episode_n` 打錯零 RPC | **已被涵蓋** | `test_generate_slides_and_report_check_the_episode_before_generating`(slides + report)、`test_revise_slide_rejects_an_unknown_episode_before_any_rpc`(連 `get_or_none` 都還沒打) |

### 已補的那一條 —— 權限被拒的**稽核 phase** 完全沒有斷言

新增 `tests/test_attachment_failover.py::test_permission_denied_is_a_clean_refusal_not_acceptance_unknown`

**為什麼是真缺口**:既有的 `test_permission_denied_never_rotates_and_names_the_fix` 只斷言
`_failovers(...) == []` 與例外訊息 —— 而 `_failovers` 為空在**兩種終態下都成立**。也就是說
「權限被拒走 `refused` 還是 `acceptance_unknown`」這件事,在這一輪之前**沒有任何測試鎖著**,
而它決定呼叫端會不會被導去一趟註定撈不到東西的對帳。斷言的形狀直接取自這一輪 stg 上量到的
那一筆(§Phase 6④)。

**突變驗證(兩個突變點,都在 `_failover.py:221`)**

| 突變 | 新測試 | 既有那條 |
|---|---|---|
| `PHASE_REFUSED` → `PHASE_ACCEPTANCE_UNKNOWN` | **紅**(`attachment_acceptance_unknown != attachment_dispatch_refused`) | **綠** ← 缺口的直接證據 |
| 稽核改拿原始 `exc`(而非 `access_denied_error` 產出的 `denied`) | **紅**(`ClientError != NotebookAccessDenied`,且指引不在 message) | 綠 |

還原後全套:**12967 passed / 597 skipped**(`_failover.py` 與突變前逐字一致)。
第二個突變點順帶鎖住了「稽核要寫在 `access_denied_error` **之後**」那個順序 ——
呼叫端很可能只看得到紀錄(`podcast_series` 的結構化 partial 只回訊息、不回原始例外),
指引一旦寫成上游那句 `permission denied` 就整條蒸發。

---

## 主迴圈複審(收錄前)

獨立重跑了兩個突變點,結果與上表一致:`PHESE_REFUSED → PHASE_ACCEPTANCE_UNKNOWN` 時
新測試紅、`test_permission_denied_never_rotates_and_names_the_fix` **確實照樣綠**
(缺口是真的);改拿原始 `exc` 也紅。還原後 `_failover.py` 逐字一致、全套 12967 passed。

**三處措辭收緊**(結論不變,只是把推論與觀測分開,免得下一輪把它們當成量過的常數):

1. **「revise 的 failover 分支沒有已知觸發條件」→ 限定在「對限流沒有」。**
   `REFUSED_WITHOUT_DISPATCH` 還有第二個成員 `ArtifactFeatureUnavailableError`(SDK 解不出
   artifact id 時自己拋),那條**沒有被排除**。所以那個分支不是死碼。
2. **「免費帳號 slide_deck 每日 3 次」→ 標成 n=2、同一天的實測。** 兩槽一致是好訊號,但
   樣本小,而且**沒有驗過**它是 UTC 日界重置還是滑動視窗 —— 要拿它排整季之前先自己再量。
3. **「拒絕不消耗配額」→ 標成推論。** 觀測到的是「耗盡後 12 次 kickoff 仍是拒絕、artifact
   差集全空」,那與「拒絕不計數」一致,但計數器觀測不到,分不出「沒被計」與「計了但早就
   在上限」。

**已落地的文件**:ADR-0011 的「這一版沒有做真實驗收」整節換成量測結果 + revise 的風險模型
更正 + 「三個 phase 只驗到兩個」;`tools_artifacts` 那句註解改成「也建 artifact,但『也會被
同步拒絕』沒有實測支持」;`docs/gotchas-pool.md` 新增 §三之〇(per-kind 配額表 + 上面兩個
誠實邊界);CHANGELOG 的 v0.9.16 節把「真實驗收未跑」換成驗收結果。

**FINDING-4 的兩條(README 數字 26→28、「零 RPC」應為「零生成 RPC」)沒有回填工作區** ——
那個目錄是拋棄式的、不在版控裡,收在這份 FINDINGS 就是它的正本。
