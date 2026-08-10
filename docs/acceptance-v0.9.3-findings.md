# notebooklm-mcp v0.9.3 真實環境驗收 — FINDINGS

環境:`doppler -p notebooklm -c stg`(測試帳號 pool)。日期 2026-08-10。
判定詞彙:**PASS** / **FAIL** / **INCONCLUSIVE**(判斷不了就標這個,不猜成通過)。

---

## Phase 0 — 本機檢查(不打 RPC)

`bash local-checks.sh` → **通過 14 / 失敗 0**,§8 硬關卡全綠。

可觀測事實:

| 項目 | 實測值 |
|---|---|
| `notebooklm-mcp` | `0.9.3` ✅ |
| `notebooklm-py` | `0.8.0` ✅ |
| `mcp` | `1.29.0`(與 lock 相同) |
| MCP 工具數 | `35` ✅ |
| 憑證落檔權限(離線 `_write_credential_file`) | `0o600` ✅ |
| 空值 PSIDTS | 落檔前 raise ✅(strict loader 同語義) |
| `runtime._sync_auth_env` / `AUTH_JSON_ENV` | 已移除;`snapshot()` 同步且無 suspend 點 ✅ |
| `_dispatch_audio_with_failover` | 吃 `account`/`client`,回三元組 ✅ |
| `podcast_attempt_retract.abandon_in_flight` | 存在,預設 `False` ✅ |
| §8 判別實驗 | 筆數守門(邊界 9 通過 / 10 擋 / 指名 10 筆擋 / `pending_uploads=1` 擋)、權限分類(`rpc_code=7`→`NotebookAccessDenied` 且訊息含 `notebook_share_with_pool`;`rpc_code=5` 原樣冒出)、三入口都有守門、`SAFE_NEXT_ACTIONS` ⊇ {`podcast_episode`,`podcast_attempt_retract`} 且都是已註冊工具名 ✅ |
| §8B | retract 准入判準讀 `dispatch.status`,`never_dispatched` 涵蓋 `prepared` ✅ |

**判定:PASS。** 裝到的確實是 v0.9.3,可以開始花配額。

---

## Phase 1 — 協定層與認證層

### 1-a server 起得來

`echo '' | doppler run -p notebooklm -c stg -- nblm-mcp --transport stdio` → `exit=0`,無 traceback。

stderr 第一行(README 預測的那條,實裝有而 CI 沒跑過)逐字:

```
.../site-packages/pydantic_settings/sources/utils.py:47: IncompleteFieldDefinitionWarning:
Field 'lifespan' has an incomplete definition: its annotation contains an unresolved forward
reference, so settings sources may fail to correctly resolve its value. Call `model_rebuild()`
on the model where the field is defined, once all the referenced types are defined.
```

**PASS** —— 走 stderr,不破壞 stdio(stdout 只有 149 bytes、全是 JSON-RPC)。
另一條 `ERROR Received exception from stream: ... Invalid JSON: EOF while parsing a value`
是我送空行造成的,不是缺陷。

`mcp` 版本 = **1.29.0**。

### 1-b pool 每個槽位

MCP 工具面 `auth_check` 只驗**作用中**槽位(回 `{"ok": true, "notebooks": 3}`),
沒有「切到某槽位」的 API(輪替游標刻意不外露)。所以照 v0.9.1 驗收做法直接驅動
`app._lifespan`,對每槽打一次唯讀 `notebooks.list()`,**RPC 全真**:

| slot | 帳號 label(遮罩) | 結果 | notebooks |
|---|---|---|---|
| 1 | `masked***@example.invalid` | ok | 3 |
| 2 | `masked***@example.invalid` | ok | 2 |
| 3 | `masked***@example.invalid` | ok | 2 |
| 4 | `masked***@example.invalid` | ok | 4 |
| 5 | `masked***@example.invalid` | ok | 9 |
| 6 | `masked***@example.invalid` | ok | 0 |
| 7 | `masked***@example.invalid` | ok | 2 |
| 8 | `masked***@example.invalid` | ok | 20 |
| 9 | `masked***@example.invalid` | ok | 1 |

`account_count=9`,9/9 成功。**輪替游標掃描前後相同**(`cursor_unchanged: true`)。**PASS**

### 1-c 憑證落檔的護欄

在本機 shell 先設 `NOTEBOOKLM_HEADLESS_REAUTH=1` 與 `NOTEBOOKLM_REFRESH_CMD=/bin/true`
(只動本機 env,未碰 Doppler),再進 lifespan:

| 變數 | 進 lifespan 前 | lifespan 內 | 離開後 |
|---|---|---|---|
| `NOTEBOOKLM_HEADLESS_REAUTH` | `"1"` | **`null`** ✅ | `"1"`(還原) |
| `NOTEBOOKLM_REFRESH_CMD` | `"/bin/true"` | **`null`** ✅ | `"/bin/true"`(還原) |
| `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` | `"1"` | `"1"`(強制) | — |

落檔實際路徑 `/tmp/notebooklm-mcp-auth-9r9ot5om/slot-{1..9}.json`;
**目錄 `0o700`、9 個檔全部 `0o600`**。正常結束後
`cred_dir_removed_after_normal_exit: true`。
`master_token.json` 在 `~/.notebooklm/`、`/tmp/`、`$HOME` 4 層內**都不存在**。**PASS**

### 1-d ⚠️ FINDING-1:CLAUDE.md §三 的清理指令偵測不到活著的 server

CLAUDE.md 給的收尾指令宣稱「只刪沒有 process 持有的,不會誤殺正在跑的 server」:

```bash
for d in /tmp/notebooklm-mcp-auth-*; do fuser "$d" >/dev/null 2>&1 || rm -rf "$d"; done
```

**判別實驗**:起一個活著的 server(stdin 掛 fifo 不送 EOF),取得它剛建的
`/tmp/notebooklm-mcp-auth-dueoa55y`(`700`,內含 `slot-1.json` `600`),在 server
**仍活著**時:

- `fuser -v <dir>` → 回傳非零、無輸出
- `lsof +D <dir>` → 無輸出

原因是 server 寫完 slot 檔就關掉 FD(`_write_credential_file` 是 open→write→close),
執行期不持有任何 open FD;`/proc/<pid>/cwd` 也不在該目錄。所以那條指令對**每一個**
活著的 server 都會判成「沒人持有」而 `rm -rf` 掉它的憑證目錄。

同一次實驗確認正常結束(fifo 關閉)後目錄**自動刪除** ✅ —— 清理機制本身是好的,
壞的是那條「安全刪除」指令的前提。

**影響**:收工時照抄那段會打斷同機其他正在跑的 MCP server(本機當下就有 3 個,
其中一個是 `-c prd` 的)。**判定:FAIL(文件缺陷,非程式碼缺陷)**。
可行的替代:先列出活著的 server 起始時間,只刪 mtime 不對應任何活 process 的目錄。

### 1-e 環境現況(非本輪造成,先記錄)

開工時 `/tmp` 已有 **7 個** `notebooklm-mcp-auth-*` 殘留目錄(最舊 07:07),
`pgrep -af nblm-mcp` 有 **3 對**活著的 server,其中
`3762882 doppler run -p notebooklm -c prd -- nblm-mcp`(**正式帳號 pool**)
已存活 3 小時。都是前幾輪留下的孤兒。收工時處理。

---

## Phase 2 — 讀取面回歸(錯誤型別變了之後,好路徑沒被弄壞)

測試筆記本 `c4537556-f5cc-4d39-8edf-864755237a77`「ZZ-TEST v0.9.3 讀取面回歸」。

| 工具 | 實測回傳 | 判定 |
|---|---|---|
| `notebook_create` | `shared_with` 列出 **8** 個 peer(9 槽位 pool),自動分享成立 | PASS |
| `notebook_list` | 4 顆,含剛建的 | PASS |
| `notebook_get` | `sources_count: 3`、`is_owner: false`、`created_at: 2026-08-10T06:51:40+00:00` | PASS(`is_owner=false` 與 docstring 記載的 SDK 行為一致) |
| `source_add_text` | `source_id: 797e24de-…` | PASS |
| `source_add_url` | `https://en.wikipedia.org/wiki/Birthday_attack` → `char_count: 43858` | PASS |
| `source_add_file` | `.md` → `char_count: 410`(無 `converted_from`,副檔名本來就吃得下) | PASS |
| `source_list` | 3 筆,`title` 與傳入**逐字相同**(`ZZ 讀取面 貼上全文 A02` / `ZZ 讀取面 檔案 B01`),`kind` 分別 `markdown`/`markdown`/`web_page`,全部 `ready: true` | PASS |
| `source_fulltext` | `max_chars=0, contains=[鹽田,尾端,百分位,3400,簽章]` → `hits: {鹽田:true, 尾端:true, 百分位:true, 3400:true, 簽章:false}`、`char_count: 234`、`content: ""` | PASS —— **負向對照有效**(不在該來源的「簽章」判 false) |
| `artifact_list` | `{"artifacts": []}` | PASS |
| `chat_ask`(預設) | 答案含引用標記 `[1]`,`references` 有 1 筆帶 `cited_text`;數字全部答對(3400 / 1900 / 1100 / 400 / 62 / 54 倍) | PASS |
| `chat_ask`(`strip_citations=true, include_references=false, source_ids=[…]`) | 標記已清、`references: []` | PASS |
| `feed_info` | `show_id=zz-test-v093-acceptance` → `token: 5krikfv3huic7fzb3wr44tiz`,`feed_url`/`show_page_url` 都算得出來 | PASS(純計算,未觸網) |

**判定:PASS。** `_list_sources` 換掉例外型別之後,讀取面沒有回歸。

### 小觀察(非缺陷)

`strip_citations=True` 把 `[1]` 拿掉後留下一個孤立空白:
`…極端延遲問題 。`(全形句號前多一個半形空格)。做 show notes 時肉眼看得到。

---

## Phase 6 ⭐ — v0.9.2 的 `notebook_share_with_pool`(從未驗收)

MCP 工具面沒有「切到某槽位」的 API(輪替游標刻意不外露),所以驅動 `app._lifespan`
起真 pool、`runtime.rotate_client()` 把作用中帳號轉到 slot 2,**RPC 全真**。
slot 1 = owner。

### 6a 死路重現(v0.9.1 的 FAIL-1 情境)

notebook `8f6d53d7-5ce0-4df1-a5a4-81b719dc1e56`「ZZ-TEST v0.9.3 Phase6 刻意不分享」,
用 slot 1 的 client 直接走 SDK `notebooks.create()` 建出(不經 `notebook_create`,
所以沒有自動分享)。

- 前提成立:作用中帳號(slot 2)`notebooks.get` → `ClientError` **`rpc_code=7`**
- `notebook_share_with_pool` → **沒有 permission denied**,`shared_by` = **slot 1(真 owner)**,
  `shared_with` = 其餘 8 個槽位,`already_shared: []`
- **輪替游標掃描前後相同** ✅
- 事後 `get_status`:**9 列 = `OWNER×1 + EDITOR×8`**,9 個相異 email 全部是 pool 成員,
  OWNER 那列就是 slot 1,pool 成員一個不缺
- 冪等:立刻再跑一次 → `shared_with: []`、`already_shared` 8 筆

**PASS**

### 6b 判別實驗(v0.9.2 真正改的那一條)

這條才會在 v0.9.1 的程式碼上紅:notebook `c4537556-…` 全 pool 都看得到,
**作用中帳號(slot 2)是 EDITOR、owner 是 slot 1**。「挑第一個 `get_status` 成功的帳號」
會選中作用中的 EDITOR;正解是 owner。

- `active_permission: "EDITOR"`,`get_status` 回的 `shared_users` 裡 OWNER 那列 = slot 1
- `notebook_share_with_pool` → `shared_by` = **slot 1**,不是作用中的 slot 2
  (`shared_by_is_owner_not_active: true`)
- `shared_with: []`、`already_shared: 8`(全員已是 EDITOR,零寫入)
- 游標不變 ✅

**PASS** —— owner 定位由**同一趟** `get_status` 的 `shared_users` 完成,沒有逐槽掃描。

### 6c 單帳號模式對不屬於自己的 notebook

把 pool 縮成 1 個(非 owner 的槽位),對 6a 那顆 notebook 呼叫:

- **沒有拋 `NotebookAccessDenied`**,安靜 no-op
- 回傳 keys = `["already_shared", "notebook_id", "shared_by", "shared_with"]`
  —— **`shared_by` 在**(v0.9.2 修的 schema 一致性)
- `get_status` 呼叫次數 = **0**(純本機判斷,沒有用一趟 RPC 換純本機的答案)

**PASS**

### 6d 不存在的 notebook id

- `ClientError`,**`rpc_code=5`**,`is_NotebookAccessDenied: false` —— **原樣重拋**,
  非權限錯誤沒有被吞掉
- `get_status` 呼叫次數 = **1**(只打 1 趟就冒出來,沒有繼續往下掃 9 個槽位)
- 訊息開頭:`The server rejected this request (not found). If you have multiple Google
  accounts signed in, this is commonly an account-routing mismatch — the request default…`

**PASS**

**Phase 6 整體判定:PASS。** v0.9.2 結案。

---

## Phase 3 ⭐ — 來源筆數守門(判別實驗)

筆記本 `85c70db5-1ab5-4ae5-931f-c91928ed17c4`「ZZ-TEST v0.9.3 守門與 series」,
A 域 9 篇合成教材起步,逐步長到 15 筆。

| 筆數 | 呼叫 | 預期 | 實測 | 判定 |
|---|---|---|---|---|
| 9 | `podcast_series`(不指名) | 通過,正常生成 | **EP01 真的生成完成**:`ep01.mp3` 40,678,305 bytes / 1263.9s(21 分鐘),`artifact_id=4e0e66ae-…`,`feedback_source_id=07e1de66-…` | **PASS** |
| 10 | `podcast_series`(不指名) | 拒絕 `too_many_sources` | 結構化停點(見 Phase 4 情境 A) | **PASS** |
| 10 | `podcast_episode`(不指名) | **raise** | raise,訊息「生成時會有 **10** 筆來源…」 | **PASS** |
| 10 | `generate_audio()` 低階 | 拒絕 | raise,同一段訊息 | **PASS** |
| 15 | `podcast_episode(source_ids=[6 筆])` | 通過 —— 唯一的出路 | **EP03 真的生成完成**:`ep03.mp3` 27,624,279 bytes / 858.3s | **PASS** |
| 15 | `podcast_episode(source_ids=[10 筆])` | 拒絕 | raise,訊息形狀**不同**:「**指名了 10 筆來源**…把清單砍到…」 | **PASS** |
| 15 | `generate_audio()` 低階 | 拒絕 | raise,「生成時會有 **15** 筆來源…」 | **PASS** |
| 9(+prior) | `podcast_episode(prior_mp3_path=…)` | 拒絕 —— 預先計入 | 見下 | **PASS** |

### 判準是筆數不是「有沒有指名」——兩種訊息形狀

不指名時訊息說「notebook … **生成時會有 N 筆來源**,不指名 source_ids 會全部帶進生成」;
指名超標時說「**指名了 10 筆來源**」。後者**沒有筆記本 id**,也就是**沒打對帳 RPC**
——與 CHANGELOG 說的「指名時是純本地的 `len()`,超標連對帳 RPC 都不打」一致。

拒絕訊息逐字(不指名版,10 筆那次):

```
notebook 85c70db5-1ab5-4ae5-931f-c91928ed17c4 生成時會有 10 筆來源,不指名 source_ids
會全部帶進生成 —— 實測 >= 11 筆就會讓模型拿前面集數的內容填空(聽起來很順、但整項是假的,
而音檔/sha/時長全部正常,事後只有逐字稿提問驗得出來);10 這一格沒量過,所以 fail-closed
從它開始拒絕。改用 podcast_episode(..., source_ids=[...]) 指名這一集要聽的來源:本集自己
的來源 + 最近 5 集的音檔回錄(共約 6 筆),集號比本集大的回錄一律排除。用 source_list 拿
真實 id,manifest 每集的 feedback_source_id 是回錄來源的正本。
```

三件要求的事都在:**說出實際筆數**(10 / 15)、**說 `>= 11` 是實測而 10 是 fail-closed
的政策選擇**、**給出可執行的下一步**(工具名 + 參數 + 怎麼拿 id)。

### `prior_mp3_path` 預先計入(Phase 10 第 5 項同時結案)

筆記本 `c4537556-…` 實際 **9** 筆,呼叫
`podcast_episode(..., prior_mp3_path=/…/ep01.mp3)`(不傳 `manifest_path`,走 standalone):

- 拒絕訊息說「生成時會有 **10** 筆來源」 —— 比實際多 1,就是那筆 prior
- 拒絕後 `notebook_get` → **`sources_count: 9`**,prior **沒有**被上傳
  → 守門確實在上傳**之前**

對照組不必另跑:同樣是 9 筆、不帶 prior 的 `podcast_series` 已在上面生成成功。
所以「帶 prior 時實際上限少一筆」**是真的**。**PASS**

### 守門沒有洩進不該擋的地方

同一個 15 筆筆記本,**不指名**:

| 工具 | 結果 |
|---|---|
| `generate_slides(episode_n=1)` | **正常生成**,`ep01-slides.pdf` 17,126,058 bytes,`artifact_id=aa2ed9a4-…`,路徑回寫 manifest |
| `generate_report(episode_n=1, study_guide)` | **正常生成**,`ep01-report.md` 6,958 bytes,`artifact_id=ee7e6427-…`,路徑回寫 manifest |

**PASS** —— 音檔的實測數據沒有被套到簡報/講義上。

> 附帶佐證:那份不指名的講義標題是「**系統效能、容量規劃與密碼學實務**學習指南」,
> 內文把 A 域(排隊/延遲)與 B 域(密碼學)混在一起。這正好反證「不指名 = 全部帶進去」
> 是真的,也就是守門要擋的那件事本身確實存在。

### 內容對照(探針先證死活,順序沒有顛倒)

| 集 | 帶進去的來源 | 自家指紋數字 | 對方領域探針 | 判定 |
|---|---|---|---|---|
| EP01 | 9 筆(全部 A 域) | `3400` ✅ `418` ✅;主題詞 排隊/延遲/尾端/負載 **4 個命中** | 簽章/碰撞/生日 全 `false`(**弱**負向:B 域當時不在筆記本裡) | 探針有效 |
| EP02 | 指名 4 筆 | `73` `99.2` `610` `24` `6.5` **全中** | 未指名的 A 域 `780`/`9.4`/`7.8` 全 `false` | 無錯置 |
| EP03 | 指名 6 筆(筆記本此時 15 筆) | `9.4` `250` `27` `7.8` `5.6` `780` `96` **全中** | **B 域 4 篇就在筆記本裡但沒被指名** → `簽章`/`碰撞`/`生日`/`82`/`340`/`馬公`/`將軍` **全 `false`** | **強負向,無錯置** |

EP03 那一列是這一輪最有價值的對照:守門指的那條出路(指名 6 筆)在一個
**被污染的 15 筆筆記本**上仍然產出乾淨內容。

**Phase 3 整體判定:PASS。**

---

## Phase 4 ⭐ — series 停在結構化停點,而且走得出去

### 造狀態的方法(說明白,免得被當成 fake)

`prepared` / `acceptance_unknown` / `accepted` 三種既有 attempt 沒辦法用 MCP 工具面
造出來(要嘛得真的撞配額,要嘛得精確控制取消時點)。這裡用**可控的 client
cancellation** —— 那本來就是 MCP 的常態失敗形狀(外層 timeout 砍 request)。
做法是驅動 `app._lifespan` 起真 pool,`asyncio.wait_for(podcast_series(...), T)`。
**RPC 全真,沒有任何 fake / monkeypatch 到生成路徑上。**

實測時序(筆記本 `60e1ef40-…`,6 筆來源):

| 取消時點 T | 落到的 `dispatch.status` |
|---|---|
| 3.2s | **`prepared`**(還在打 baseline `artifacts.list`) |
| 3.8s / 4.4s / 7s | **`acceptance_unknown`**(已進 generate RPC) |
| 45s | **`accepted`** + `remote: pending` |

### 情境 A(沒有 attempt)—— 與 Phase 3 的 9 筆格是同一次實驗

筆記本 9 筆 → `podcast_series` 兩集 → EP01 完成、回錄讓筆記本變 10 筆 → EP02 撞守門。

回傳(逐字重點):

```json
{"complete": false, "stopped_at_episode": 2, "attempt_id": null,
 "observed_state": "too_many_sources", "safe_next_action": "podcast_episode",
 "attempt_count": 0, "superseded_attempt_count": 0,
 "episodes": [{"episode": 1, "label": "EP01 尾端與排隊",
               "artifact_id": "4e0e66ae-…", "mp3_path": "…/ep01.mp3",
               "attempt_id": "b6f2e26b-…", "feedback_source_id": "07e1de66-…"}]}
```

- **沒有拋例外** ✅
- `complete: false` / `stopped_at_episode: 2` / `observed_state: "too_many_sources"` /
  `safe_next_action: "podcast_episode"` ✅
- **`episodes` 陣列裡 EP01 的完整結果還在** ✅(裸拋會整份丟掉)
- `attempt_count: 0`、manifest `revision=14` 只有 episode 1 —— **零 manifest 副作用** ✅

**照著 `safe_next_action` 做**:`podcast_episode(episode_n=2, source_ids=[4 筆])`
→ EP02 生成完成(`ep02.mp3` 27,932,175 bytes / 867.9s),manifest 記
`settings.source_ids` 長度 4。**走得出去。PASS**

### 情境 B(已有 attempt)

那顆 `prepared` attempt `a3c66ea3-…`,筆記本加到 **11** 筆後重呼 `podcast_series`:

```json
{"complete": false, "stopped_at_episode": 1, "attempt_id": "a3c66ea3-…",
 "observed_state": "too_many_sources", "safe_next_action": "podcast_attempt_retract",
 "attempt_count": 1, "superseded_attempt_count": 0}
```

- `safe_next_action` = **`podcast_attempt_retract`**,不是 `podcast_episode` ✅
- `attempt_id` 指向那顆既有 attempt ✅
- **`attempt_count` 沒有比上一輪多**(仍是 1)—— 守門在 re-arm/supersede 之前,
  沒留半成品 ✅
- `error` 帶完整兩步指引(筆數段 + 「這一集已經有 active attempt …先
  podcast_attempt_retract 掉它…再用指名版重生」)✅

**先故意照錯的做**:直接 `podcast_episode(..., source_ids=[3 筆])` →

```
episode 1 already has durable active attempt 'a3c66ea3-0954-4a55-aea7-19731af37ba5'
(dispatch='prepared'); reconcile or resume it before creating another attempt.
If it never dispatched, re-call with the identical arguments to resend that same
attempt instead of creating a new one.
```

死路重現 ✅ —— 這就是「為什麼停點不能指 `podcast_episode`」。

**再照對的做**:
1. `podcast_attempt_retract`(**不傳 `abandon_in_flight`**)→ **成功**,
   `stale_source_ids: []`、`observed_state: "retracted"`、`safe_next_action: "podcast_series"`
2. 照它給的 `podcast_series` 再打一次 → 停點翻成
   `attempt_id: null` + `safe_next_action: "podcast_episode"`(**不是死路,只多一趟**)
3. `podcast_episode(..., source_ids=[3 筆])` → 生成完成,
   `attempt_id=dc9bfa07-…`,mp3 落在 `attempts/dc9bfa07-…/ep01.mp3`(30,667,380 bytes)

manifest 事後:`retracted_attempt_ids: ["a3c66ea3-…"]`,舊 attempt 整份留著
(`dispatch=prepared`、`retracted=true`),新 attempt `accepted`→`completed` 並 promote。
`pending_source_cleanup` 不存在(從沒產出,沒有清理義務)。**整條走得通。PASS**

### 情境 C(不該被擋的那條)

那顆 `accepted` + `remote: pending` 的 attempt `0946e9ad-…`,筆記本加到 **11** 筆後
重呼 `podcast_series`:

```json
{"complete": true, "episodes": [{"episode": 1, "artifact_id": "d47b70ed-…",
  "mp3_path": "…/sc-c/ep01.mp3", "feedback_source_id": "93fb437b-…"}]}
```

**沒有被守門攔下,正常 finalize 完成** ✅(`ep01.mp3` 25,511,705 bytes)。
manifest:單一 attempt `dispatch=accepted` / `remote=completed`,已 promote。

內容對照:該集 dispatch 時筆記本只有 6 筆 A 域,finalize 時已有 11 筆(含 B 域)——
回錄逐字稿 `排隊`/`尾端`/`418`/`3400`/`71` 全中,`簽章`/`碰撞`/`生日`/`82` **全無**。
生成輸入在 dispatch 當下就凍住了,後加的來源沒有洩進來。

> 探針註記:`340` 判 true 是**子字串假陽性**(`3400` 含 `340`),不是 B04 的洩漏。
> 下一輪的探針詞不要用另一個探針詞的前綴。

**Phase 4 整體判定:PASS。**

---

## Phase 5 ⭐ — `podcast_attempt_retract` 的准入放寬

### 5-1 免旗標那條

**已在 Phase 4 情境 B 結案**:`dispatch.status = "prepared"` 的 attempt,
`podcast_attempt_retract(..., reason="…")` **不傳 `abandon_in_flight`** → **成功**,
`stale_source_ids: []`,重生走得通。*v0.9.2 在這裡會拒絕。*

⚠️ **`not_accepted` 本身沒有重現到**:它要伺服器同步拒絕(`RateLimitError` /
`ArtifactFeatureUnavailableError`),而 9 槽位 pool 會先 failover,要撞到得把 9 個帳號
當日配額全部打完。`prepared` 與 `not_accepted` 在程式碼裡是**同一個 `never_dispatched`
判準**(local-checks §8B 已鎖住兩者都在該 tuple 裡),所以放寬的行為有覆蓋到;
但「真的撞配額之後長什麼樣」這一格 **INCONCLUSIVE**,不寫成 PASS。

### 5-2 仍需旗標那條(邊界沒有一起放寬)

`dispatch.status = "acceptance_unknown"` 的 attempt `ab20b383-…`:

- **不傳旗標** → **被拒** ✅
- **`abandon_in_flight=true`** → **成功**,`observed_state: "retracted"`

### 5-3 照 troubleshooting 的死結流程走一次

1. `artifact_list(notebook_id, kind="audio")` → 3 顆 `completed: false / status: pending`
   的 audio artifact(取消的 dispatch 在雲端留下的孤兒,與文件說的一致)
2. `abandon_in_flight=true` retract → 成功
3. `podcast_episode(..., source_ids=[2 筆])` 重生 → 見下方結果

**流程可執行 ✅**

### 5-4 ⚠️ FINDING-2:拒絕訊息沒有指向正門

5-2 第一步的拒絕訊息逐字:

```
attempt 'ab20b383-5a39-4b4c-9c2d-a7a28516fe31' is not episode 1's durable output;
only a promoted output attempt can be retracted
```

**它沒有提到 `abandon_in_flight`**,而且「only a promoted output attempt can be
retracted」這句話**在這個狀態下是假的** —— 傳了旗標就 retract 得掉(下一步實測)。
只看工具回傳的呼叫端會判定「這條路關著」,正是 CHANGELOG 說 v0.9.3 要修掉的那個誤解
(它修的是 **docstring**,沒修 **runtime 訊息**)。

對照組:同一支工具在 `prepared` 狀態下**沒有**產生這個訊息(直接成功),所以問題只在
需要旗標的那三種狀態。

**判定:FAIL(可用性,非正確性)。** 行為正確、准入邊界正確,但指引在它自己產生的
狀態下讀起來是死路 —— 與 v0.9.1 FAIL-1 同型,只是這次有 skill 文件兜底。
建議:那條 `raise` 在 `dispatch.status ∈ {acceptance_unknown, dispatching, accepted}`
時附一句「這顆 dispatch 狀態是 X;確認過雲端沒有對應 artifact 後,用
`abandon_in_flight=True` 作廢它」。

### 5-5 ⚠️ FINDING-3:`retraction` 稽核區塊沒有記下 `abandon_in_flight`

兩次 retract 的 manifest `retraction` 區塊**欄位完全相同**:

```json
{"episode": 1, "attempt_id": "…", "retracted_at": "…", "reason": "…",
 "retracted_output": {}, "stale_artifact_id": null, "stale_source_id": null,
 "stale_source_ids": [], "retracted_mp3_path": null}
```

一次是 `prepared`(純本機、零遠端後果、不需宣告),一次是
`acceptance_unknown` + `abandon_in_flight=true`(呼叫端**顯式宣告**了 manifest 推導不出
的外部知識,而且遠端可能真的有東西在燒)。`reason` 必填是因為「retract 是審計事件」——
但事後從 manifest **分不出來哪一次動用了旗標**,除非去讀 `dispatch.status` 反推。

**判定:FAIL(稽核完整性,低嚴重度)。** 建議 `retraction` 多存一個
`abandon_in_flight`(以及當時的 `dispatch.status`)。

**Phase 5 整體判定:行為 PASS;訊息與稽核各一個 FAIL;`not_accepted` 實況 INCONCLUSIVE。**

### 5-6 完整 retract(有 output 證據)的對帳

對 sc-c 那顆**已 promote** 的 output attempt `0946e9ad-…` 做 QA 拒收(不需旗標,
它就是 `output_attempt_id`):

```json
{"retracted_output": {"output_attempt_id": "0946e9ad-…", "artifact_id": "d47b70ed-…",
   "mp3_path": "…/sc-c/ep01.mp3", "published_at": "Mon, 10 Aug 2026 15:52:08 +0800",
   "feedback_source_id": "93fb437b-…"},
 "stale_artifact_id": "d47b70ed-…", "stale_source_ids": ["93fb437b-…"],
 "observed_state": "retracted", "safe_next_action": "source_delete"}
```

manifest 事後:

| 欄位 | 值 |
|---|---|
| episode 級剩下的 key | `attempts` / `episode` / `label` / `notebook_id` / `pending_source_cleanup` / `previous_feedback_source_ids` / `retracted_attempt_ids` / `title` |
| `artifact_id` / `mp3_path` / `published_at` / `feedback_source_id` | **全部移除**(原值存進 `retraction.retracted_output`) |
| `active_attempt_id` / `output_attempt_id` | `None` / `None` |
| `retracted_attempt_ids` | `["0946e9ad-…"]` |
| `previous_feedback_source_ids` | `["93fb437b-…"]` |
| `pending_source_cleanup` | `["93fb437b-…"]` |
| attempt 的 `finalize` 紀錄 | **整份留著**(`artifact_rename` / `download` / `feedback_source_rename` / `feedback_source_upload`) |

照 `safe_next_action` 做:`source_delete(93fb437b-…)` → `{"deleted": "93fb437b-…"}`,
再依 skill 紀律 `artifact_rename` 成 `✗作廢 EP01 情境C(v0.9.3 驗收 QA 拒收)`。
(`pending_source_cleanup` 刪完後仍留在 manifest —— 依設計它在下一次生成/resume 前
才會實際查 notebook 對帳並結案,不是刪完就消失。)**PASS**

---

## Phase 7 — attempt 狀態機其餘入口

| 工具 | 走到的路徑 | 實測 | 判定 |
|---|---|---|---|
| `podcast_episode_reconcile` | 零候選安全停點 | `{"observed_state": "acceptance_unknown", "candidate_artifact_ids": [], "safe_next_action": "podcast_episode_reconcile"}`,連跑 3 次(兩顆 attempt)都一致;同時 `artifact_list` 確認雲端**真的**零 audio artifact —— 不是猜的 | PASS |
| `podcast_episode_reconcile` | **tombstone** | `attempt '…' was retracted (episode 1); it is history — work on the replacement attempt instead` | PASS |
| `podcast_attempt_adopt` | **tombstone** | 同上逐字訊息 | PASS |
| `podcast_attempt_adopt` | 正向綁定 | **未執行** —— 見下方說明 | **未覆蓋** |
| `podcast_episode_resume` | 正向 finalize | sc-na 的 `accepted`/`pending` attempt `e14f2a11-…` → **不重生**續完:`ep01.mp3` 落地、`feedback_source_id=b830dc09-…`、attempt 被 promote | PASS |
| `artifact_wait` | 正向 | 等到 Phase 9 的重跑完成,回 `{task_id, artifact_id}` 相同值 | PASS |
| `artifact_rename` | 正向 ×2 | 孤兒標記與 QA 拒收標記都成功,回傳新標題 | PASS |
| `artifact_download_audio` | 正向 | `retried.mp3` 45,092,685 bytes / 1401.1s | PASS |

**`podcast_attempt_adopt` 正向路徑為什麼沒做**:它要一顆「呼叫端自己驗證過、尚未被
claim 的遠端 artifact」+ 同一 notebook 裡一顆卡住的 attempt。這一輪造出來的
`acceptance_unknown` attempt **都是雲端零 artifact**(取消得夠早,伺服器還沒建 task),
而 troubleshooting 指定的補救做法是先用低階 `generate_audio` 生一顆 —— 那支在收尾階段
**撞到配額且沒有 failover**(見 FINDING-4)。不硬湊一個把別集的 artifact 綁進來的假通過。
**標記為未覆蓋,不是 PASS。**

---

## Phase 8 — 附件與發布

| 工具 | 實測 | 判定 |
|---|---|---|
| `generate_slides` | 15 筆筆記本、不指名 → `ep01-slides.pdf` 17,126,058 bytes,`artifact_id=aa2ed9a4-…` | PASS |
| `generate_report` | `study_guide` → `ep01-report.md` 6,958 bytes,`artifact_id=ee7e6427-…` | PASS |
| `artifact_download_slides` | 用 id 重下載,冪等,路徑回寫不變 | PASS |
| `artifact_download_report` | 同上 | PASS |
| `artifact_revise_slide` | `slide_index=1` → 回 **新** `artifact_id=f96750ec-…` + `superseded_artifact_id=aa2ed9a4-…`;`artifact_list(kind="slide_deck")` 實測**兩顆並存**:`Tail Latency Physics (2)`(新)與 `Tail Latency Physics`(舊,原封不動) | PASS —— 與 v0.9.1 修正後的 docstring **逐字相符** |
| `episode_set_description` | 回 `{"stripped": true}`,description 寫進 manifest | PASS |
| `research_start` | `mode=fast` 立即回 `task_id=bd79ef89-…` | PASS |
| `research_wait` | `status=completed`,10 筆 candidates(含 `cited` 欄位),`report_chars=0`(預設不回本文)、`report_importable=false`(fast 沒報告) | PASS |
| `research_import` | 指名 1 個 URL → 匯入 `The Tail at Scale`,回 `imported`/`requested`/`note` | PASS |
| `source_delete` | 刪 research 匯入那筆、刪 retract 留下的 stale 回錄,各一次 | PASS |
| `notebook_create` | Phase 2 已驗(自動分享 8 個 peer) | PASS |
| `publish_series` | 見下 | PASS(preflight) |

> `research_*` 這一輪跑在測試筆記本而不是拋棄式 scratch notebook(skill §Research 的紀律)。
> 這是刻意的:整個工作區就是拋棄式,而且匯入後立刻 `source_delete` 掉。正式流程仍應照
> skill 走 scratch notebook。

### `publish_series` —— 依使用者指示**只跑 fail-closed preflight,零 NAS 寫入**

三道守門依序擋下,每一次都在**任何上傳之前**:

| 呼叫 | 錯誤 |
|---|---|
| 不傳 `artwork_path` | `missing show fields: artwork_path — 首次發布請顯式傳齊(成功後自動存進 manifest['show'],之後只傳 manifest_path 即沿用)` |
| 補 `artwork_path`(`notebooklm-cover --show` 產,3000×3000 JPEG,自帶 Apple 驗證器) | `episode 1: cover_path is required (每集必做,不再 fallback 節目封面)` |
| 補三集封面(`notebooklm-cover --manifest` 產 EP01/02/03,3000×3000,並回寫 manifest) | `episode 2: description is required (真 show notes,不可空白)` |

**驗證真的沒寫**(不只信工具回傳):

```
GET https://podcast.example.com/feeds/FEED_TOKEN/feed.xml   → HTTP 404
GET https://podcast.example.com/feeds/FEED_TOKEN/index.html → HTTP 404
```

**PASS。** 刻意**沒有**把最後一道(EP02/EP03 的 description 與 slides/report)補滿,
以免觸發真正的上傳。

---

## Phase 9 ⭐ — `artifact_retry_failed` 的未驗證邊界

**這一輪拿到結論了,不是 inconclusive。**

### 實驗設計

用 v0.8.0 驗收筆記本 `bd73e28b-…` 裡一顆**真實的 failed AUDIO**
`c9bb1cd2-…`(`status: "failed"`,建於 2026-08-09T03:59:56,當時筆記本約 4–6 筆來源)。

失敗**之後**才加入 4 篇**全新領域**(咖啡/烘焙,與該筆記本原有的排隊/密碼學零重疊)
的合成教材,每篇帶 4 個獨一無二的四位數指紋,共 16 個。筆記本因此變成 **12 筆(超標)**。

`artifact_retry_failed(bd73e28b-…, c9bb1cd2-…)` → 回
`{"task_id": "c9bb1cd2-…", "artifact_id": "c9bb1cd2-…"}`(**artifact_id 不變**,原地重跑)
→ `artifact_wait` → 完成,`status: "completed"`,標題變成「國中數學搬空金庫」
→ `artifact_download_audio`(45,092,685 bytes / 1401.1s)→ 上傳回錄 → `source_fulltext`。

### 結果

| 探針 | 重跑後的逐字稿 | 正向對照 |
|---|---|---|
| 16 個四位數指紋(9271 / 6483 / 1157 / 3062 / 8425 / 2739 / 5106 / 7318 / 4917 / 2648 / 1394 / 6205 / 3581 / 7960 / 4273 / 8016) | **0 / 16 命中** | **16 / 16 命中** |
| 主題詞 咖啡 / 烘焙 / 杯測 / 大甲 | **0 / 4** | **4 / 4** |

**正向對照**是同一批 4 筆來源、同一個筆記本,用
`podcast_episode(..., source_ids=[那 4 筆])` 生一集(12,896 → 13,903 字逐字稿)。
探針全部會亮 —— 所以上面那個 0/16 **不是探針死掉**,是內容真的不在。

### 結論

**`artifact_retry_failed` 沒有重抓筆記本當下的來源;伺服器沿用了該 artifact 原本的
來源集合。** docstring 裡「伺服器**應該**沿用…但那是推測」這句,這一輪從推測變成
**一次實測**。

**判定:PASS(限定條件)。** 限定條件要跟著寫進程式碼:

- n=1。單一 artifact、單一失敗成因、單一筆記本。
- 只測了「失敗**之後**新增來源」。**沒測**「失敗之後**刪掉**原來源」會怎樣
  (原集合裡的 id 失效時伺服器的行為未知),也沒測跨帳號重跑。
- 所以**不建議**因為這次結果就宣告這支工具安全無虞;建議的下一步是把 docstring 的
  「推測」改成「2026-08-10 實測一次:新增來源不會洩入;刪除來源未測」,守門仍不加
  (加了會廢掉救援路)。

---

## Phase 10 ⭐ — skill × MCP 搭配(照著做)

| # | skill 指引 | 怎麼驗的 | 判定 |
|---|---|---|---|
| 1 | `SKILL.md` §Episodic 步驟 3:「5 集以內用 series、EP06 起改單集」,理由是逐集加原文時 EP_n 開始前有 `2n-1` 筆 | 見下 | **PASS(構造性,未跑滿 6 集)** |
| 2 | `troubleshooting.md`〈工具說「來源太多」拒絕生成〉那張表,兩種 `safe_next_action` | 兩種**各走一次完整流程**:`podcast_episode`(Phase 4 情境 A)與 `podcast_attempt_retract`(Phase 4 情境 B),都跑到重生成功 | PASS |
| 3 | `troubleshooting.md`〈`acceptance_unknown` 又重現不了原本的 brief〉三步 | `artifact_list` → `abandon_in_flight=true` retract → `podcast_episode(source_ids)` 重生,三步照跑,解得開(Phase 5-3) | PASS |
| 4 | `tool-reference.md` 的 `abandon_in_flight` 狀態表,每一列各驗一次 | 見下 | **部分** |
| 5 | `tool-reference.md`:`prior_mp3_path` 預先計入,帶 prior 時實際上限少一筆 | 9 筆筆記本 + prior → 拒絕訊息說「會有 **10** 筆」,且拒絕後 `sources_count` 仍是 **9**(沒上傳) | PASS |

### 第 1 項:`2n-1` 時序

模型的兩個組成部分**各自實測過**:

- **每集恰好 +1 筆回錄**:`podcast_series` 跑完 EP01 後,筆記本從 9 → 10
  (`feedback_source_id=07e1de66-…`);`podcast_episode` 每次 finalize 也各 +1。
- **守門邊界**:9 通過 / 10 拒絕 / 11 拒絕 / 15 拒絕(Phase 3 逐格實測)。

交叉點是這兩者的算術結論:逐集加 1 篇原文時 EP_n 開始前有 `n + (n-1) = 2n-1` 筆,
`2n-1 >= 10` → `n >= 5.5` → **n=6**,與 skill 寫的「EP06 起改用單集入口」相符。

**沒有真的跑滿 6 集**(那是 5 次生成,而收尾時配額已經吃緊)。這一輪的筆記本從 9 篇
原文起步,所以同一個機制在 **EP02** 就撞到 —— 把實際起始筆數代進模型算出來的位置,
與實測停點一致。**判定 PASS,但這是構造性驗證,不是逐集跑到 EP06。**

### 第 4 項:`abandon_in_flight` 狀態表逐列

| `dispatch.status` | 表說 | 實測 |
|---|---|---|
| `prepared` | 不用旗標 | ✅ **實測**:不傳旗標 retract 成功(Phase 4 情境 B) |
| `not_accepted` | 不用旗標 | ⚠️ **INCONCLUSIVE** —— 造不出來(見 Phase 5-1) |
| `acceptance_unknown` | 要旗標 | ✅ **實測**:不傳被拒、傳了成功(Phase 5-2) |
| `dispatching` | 要旗標 | ⚠️ **未覆蓋** —— 那是 dispatch RPC 進行中的瞬時狀態,取消時序落不進去 |
| `accepted` | 要旗標 | ⚠️ **未覆蓋(未 promote 的那種)** —— 這一輪的 `accepted` attempt 都被 resume/finalize 促成 output 了;已 promote 的 output attempt 走的是另一列(不需旗標),那一列**有**實測(Phase 5-6) |

**2/5 直接實測、1/5 走到另一列、2/5 未覆蓋。**

---

## ⚠️ FINDING-4:低階 `generate_audio` 沒有配額 failover,而文件說「呼叫端不會看到失敗」

驗收尾聲,`generate_audio`(低階)連兩次直接爆:

```
API rate limit or quota exceeded. Please wait before retrying. (Upstream: Resource exhausted.)
```

**同一時間 podcast 路徑跑得動** —— 因為 `_rotate_for_quota` **只存在於 `tools_podcast`**:

```
tools_podcast.py:610  def _rotate_for_quota(...)
tools_podcast.py:688  rotated = _rotate_for_quota(...)   # _REFUSED_WITHOUT_DISPATCH 分支
tools_podcast.py:745  rotated = _rotate_for_quota(...)   # ensure_started 失敗分支
```

而 `tools_basic.generate_audio` 是 `client = runtime.get_client()` 之後直接送,
撞到 `RateLimitError` 就原樣冒出來。用**全新 process**(游標從 slot 1 起算)重跑
確認不是游標位置問題:`{"account": "pool-account-1@example.invalid", "type": "RateLimitError"}`。

**為什麼這是問題**:`SKILL.md` §Auth 寫

> `prd` 注入的是**多帳號 pool**…某帳號當日配額耗盡時 **server 會自動換下一個帳號重送,
> 呼叫端不會看到失敗**。

這句話沒有限定範圍,讀起來像整個 server 的性質。而 `troubleshooting.md` 對
`acceptance_unknown` 死結明寫的**備援出路**正是
「低階 `generate_audio` → `podcast_attempt_adopt` → `podcast_episode_resume`」——
**那條路在配額吃緊時會直接失敗,而配額吃緊正是最常需要它的時候**。這一輪就因此
沒能執行 `podcast_attempt_adopt` 的正向路徑(Phase 7)。

**判定:FAIL(文件與行為不符,中嚴重度)。** 兩個修法擇一:
(a) 把 failover 抽出來讓 `generate_audio` 也用;
(b) 在 SKILL.md §Auth 與 `generate_audio` 的 docstring 明寫「failover 只涵蓋
podcast 家族的 dispatch,低階入口撞到配額會直接 raise」。
考慮到低階入口的定位是「呼叫端自己負責」,(b) 成本低且誠實;但 troubleshooting
那條備援指引要一併加註「這條路本身會吃配額,而且沒有 failover」。

---

## 覆蓋自檢(35 / 35 支工具都真的被呼叫過)

`for t in $(list_tools); do grep -q "$t" FINDINGS.md; done` → **未記錄 0 支**。
但 grep 只證明「有寫到」,下表是**實際呼叫過**的對帳,兩支覆蓋不完整的明寫出來:

| 工具 | 覆蓋 |
|---|---|
| `auth_check` `notebook_list` `notebook_create` `notebook_get` `notebook_share_with_pool` | 完整 |
| `source_add_text` `source_add_url` `source_add_file` `source_list` `source_delete` `source_fulltext` | 完整 |
| `chat_ask` `feed_info` | 完整 |
| `artifact_list` `artifact_wait` `artifact_rename` `artifact_download_audio` `artifact_download_slides` `artifact_download_report` `artifact_revise_slide` `artifact_retry_failed` | 完整 |
| `generate_audio` `generate_slides` `generate_report` | 完整(`generate_audio` 正向被配額擋掉,但守門拒絕路徑實測 3 次) |
| `podcast_series` `podcast_episode` `podcast_episode_resume` `podcast_attempt_retract` `podcast_episode_reconcile` | 完整 |
| `episode_set_description` `research_start` `research_wait` `research_import` | 完整 |
| **`podcast_attempt_adopt`** | **僅拒絕路徑(tombstone)**;正向綁定未執行 —— 理由見 Phase 7 |
| **`publish_series`** | **僅 fail-closed preflight(3 道守門)**;真正上傳依使用者指示不做 |

## 配額狀態(回報用)

這一輪實際生成 **13 次**(含被取消的 dispatch,那些一樣佔配額):
EP01/EP02/EP03(series-gate)、sc-b T=7、sc-try 3.2/3.8/4.4、sc-c T=45、
sc-try-3.2 重生、sc-b 重生、sc-na T=25、Phase 9 retry、Phase 9 正向對照。
另有簡報 ×1、講義 ×1、改頁 ×1、research fast ×1。

收尾時 **slot 1(`pool-account-1@example.invalid`)已耗盡**,低階入口因此直接爆。
podcast 路徑仍能 failover 跑動,表示後面槽位還有餘額 —— 但**沒有**探測到底走到第幾格
(探測本身會燒掉還有餘額的槽位)。依 CLAUDE.md 的約定回報一聲,未自行決定停不停。

---

## 收工

### 1. 收回離線測試(含突變驗證)

`../notebooklm-mcp/tests/test_source_selection.py` 新增
**`test_the_guard_does_not_block_an_attempt_that_only_needs_finalizing`**。

為什麼是這一條:既有 26 條守門測試涵蓋了「該擋的有沒有擋住」(`not_accepted` 重送、
指名超標、低階入口、pending upload、權限分類、9 筆放行),**唯獨沒有**
「**不該擋的有沒有放過**」—— 也就是 Phase 4 情境 C 量的那個不變式。守門條件是

```python
if dispatch_state in ("prepared", "not_accepted") or remote_state in ("failed", "removed"):
```

少寫那個條件就會把「已 dispatch、只等 finalize」的一集永久卡死,而**沒有任何測試會紅**。

**突變驗證**:把該行改成 `if True:`(無條件擋)

| | 結果 |
|---|---|
| 突變後 | `FAILED … AssertionError: 只等 finalize 的 attempt 不該被筆數守門攔下` — 1 failed, 25 passed |
| 還原後 | 26 passed |
| 全套 | **604 passed in 107.78s** |

原始碼已還原(`git diff` 對 `tools_podcast.py` 為空)。

### 2. 未結案的前提寫回程式碼

`notebooklm_mcp/tools_basic.py` 的 `artifact_retry_failed` docstring:
「⚠️ **未驗證的邊界** … 那是推測」改成「⚠️ **部分驗證的邊界** … v0.9.3 真實驗收
2026-08-10 實測過一次(0/16 對 16/16)」,並**明寫剩下沒測的兩件事**
(失敗後刪掉原來源、跨帳號重跑)與 n=1 的限制。守門維持不加。

> **兩份變更都沒有 commit**(需要你明示才提交):
> `M notebooklm_mcp/tools_basic.py` / `M tests/test_source_selection.py`,共 +62 −5。

### 3. 清理

| 項目 | 結果 |
|---|---|
| ZZ-TEST notebook | 4 顆全刪(`讀取面回歸` / `守門與 series` / `attempt 狀態機` / `Phase6 刻意不分享`)。**MCP 沒有 `notebook_delete` 工具**,走 SDK `notebooks.delete()` |
| 借用的舊筆記本 `bd73e28b`(v0.8.0 驗收) | 這一輪加的 6 筆來源(P9-01~04 + 兩份回錄)**全部 `source_delete` 掉**,回到原本 8 筆 |
| 該筆記本被 retry 的 artifact | `c9bb1cd2-…` 從 `failed` 變成 `completed` —— **這個改變無法還原**。已 `artifact_rename` 成 `v0.8.0 驗收 —— 配額 failover(原 failed,2026-08-10 v0.9.3 驗收 Phase 9 retry 成功)`,讓下一輪看得懂 |
| 憑證殘留 | 開工時 7 個目錄 → 刪掉 **5 個孤兒(共 37 份憑證)**,保留 2 個活著的 server 持有的 |
| server 孤兒 | **沒有孤兒**。`3919189` 是本 session 的;`3762987`(`-c prd`)的祖先鏈是 `claude → fish → Orca IDE`,**屬於另一個活著的 session**,所以沒有動它 |
| 本機產物 | `output/` 下的 mp3 / pdf / md / 封面 / manifest 全部留著(整個工作區可刪) |

⚠️ 清理**沒有**用 CLAUDE.md §三 那條 `fuser` 指令(見 FINDING-1,它會誤刪活著的
server 的憑證目錄)。改用「活 process 起始時間 vs 目錄 ctime 對照」。

### 4. 這一輪自己造成的一個事故(誠實記錄)

Phase 1 排查時我用 `tr '\0' '\n' < /proc/<pid>/environ | sed 's/=.*/=<value>/'` 想遮罩環境
變數,但 `NOTEBOOKLM_AUTH_JSON` 的值**本身含換行**,`tr` 把它拆成多行、`sed` 的
行首錨點失效,於是 **stg 測試帳號的真實 cookie 值被印進 session transcript**。
不是工具洩漏,是我的操作失誤。stg 是拋棄式測試池,但若要保險,重跑
`scripts/login_notebooklm.py --profile test` + `sync-auth.sh` 換掉即可。
教訓:**不要用 `/proc/*/environ` 取含 JSON 的環境變數**;要看有沒有設某個變數,
用 `doppler secrets --only-names`,或在 process 內部讀 `os.environ.get()` 只印
`is None` / 布林。

---

## 總結

| Phase | 判定 |
|---|---|
| 0 本機檢查 | **PASS**(14/14,§8 硬關卡全綠) |
| 1 協定/認證 | **PASS**;另有 **FINDING-1**(CLAUDE.md 清理指令) |
| 2 讀取面回歸 | **PASS** |
| 3 ⭐ 筆數守門 | **PASS**(8 格全中,含 9 筆真的生得出來、15 筆指名 6 筆真的走得通、簡報/講義沒被誤擋) |
| 4 ⭐ series 停點 | **PASS**(三種情境,含「照著 `safe_next_action` 走真的走得出去」) |
| 5 ⭐ retract 准入 | 行為 **PASS**;**FINDING-2**(拒絕訊息沒指向正門)、**FINDING-3**(稽核沒記旗標);`not_accepted` 實況 **INCONCLUSIVE** |
| 6 ⭐ share_with_pool | **PASS** —— v0.9.2 結案 |
| 7 attempt 其餘入口 | **PASS**;`podcast_attempt_adopt` 正向路徑**未覆蓋**(被 FINDING-4 擋住) |
| 8 附件與發布 | **PASS**;`publish_series` 依指示只跑 preflight(feed URL 實測 404) |
| 9 ⭐ retry_failed | **PASS(限定條件)** —— 從「推測」變成一次實測,限制已寫回 docstring |
| 10 ⭐ skill × MCP | 5 項中 **3 項 PASS、1 項構造性 PASS、1 項部分**(狀態表 2/5 直接實測) |

**驗收目標的三問**:

1. **守門真的擋得住嗎** —— 是。三個公開入口 + 判準是筆數不是指名 + prior 預先計入,
   八個邊界格全部符合預期,而且拒絕訊息把「實測 ≥11 / 政策 10」講清楚了。
2. **擋下來之後照著指引做真的走得出去嗎** —— 是。兩種 `safe_next_action` 各走完整一輪
   到重生成功,包含刻意先照錯的做一次撞出 `already has durable active attempt`。
   唯一的例外是 FINDING-2:`acceptance_unknown` 的拒絕訊息自己沒指路(要靠 skill 文件)。
3. **有沒有把原本能跑的路一起關掉** —— 沒有。9 筆整季流程照跑、只等 finalize 的 attempt
   不被攔、簡報/講義不受影響。這一條現在有離線測試鎖著了。

**四個 FINDING 都不在核心機制上**,兩個是文件/訊息、一個是稽核欄位、一個是低階入口的
failover 範圍。核心的守門、停點、准入判準、權限分類全部如設計運作。
