# notebooklm-mcp v0.9.6 真實環境驗收 — FINDINGS

- 工作區:`$HOME/research/audiskill/nblm-acceptance-v0.9.6`
- 涵蓋版本:v0.9.4 / v0.9.5 / v0.9.6(判定 **full**)
- 帳號:`doppler -p notebooklm -c stg`(測試帳號 pool,9 槽)
- 開跑:2026-08-10
- 判準:**照著每一個 `safe_next_action` 做,走不走得出去。**

---

## Phase 0 — 本機檢查 ✅ PASS(14/14)

`bash local-checks.sh`(2026-08-10 重跑一次確認,非沿用前次結果)

```
=== 0. 實裝版本 ===
  ✅ notebooklm-mcp 0.9.6
  ✅ notebooklm-py 0.8.0
  ℹ️  mcp 1.29.0
=== 1. 身分不再放 process 全域(v0.9.0 的核心)===
  ✅ _sync_auth_env 已移除、snapshot() 已就位且無 suspend 點
=== 2. dispatch 把「實際送出的帳號與 client」交還給呼叫端 ===
  ✅ _dispatch_audio_with_failover 吃 account/client 並回三元組
  ✅ 沒有殘留的「dispatch 後重讀全域」註解
=== 3. ⭐ 落檔預驗證必須與 SDK strict loader 同語義 ===
  ✅ 空值 PSIDTS 在落檔前就被擋掉(strict 同語義)
  ✅ 憑證檔是 0600
=== 4. 這一版新增的對外契約 ===
  ✅ podcast_attempt_retract 有 abandon_in_flight(預設 False)
  ✅ series 有 notebook_access_denied 停點
=== 5. 分享的權限判準(v0.9.0 的 P0)===
  ✅ VIEWER 不算已分享 / OWNER 算 / email 大小寫不敏感
=== 6. SDK 契約(上游漂移絆線)===
  ✅ add_user 預設仍是 VIEWER、permission 仍可位置傳、SharedUser 有 permission
=== 7. 工具面完整 ===
  ✅ 35 個 MCP 工具
=== 8. v0.9.6 新增行為的斷言(每版必填)===
  ✅ capabilities 單一事實來源:終態不可重送、白名單入口、歷史/output 不教旗標、守門⊆免旗標、指引可執行
  ✅ retract 稽核記下實際生效的 authorization_basis
──────────────────────────────────────────
  通過 14 / 失敗 0
```

實裝版本以「真正會跑 `nblm-mcp` 的那個直譯器」查得:`notebooklm-mcp 0.9.6`。

---

## Phase 1 — 協定層與認證層 ✅ PASS

腳本:`phase1_auth.py`(在 `doppler -c stg` 下用 tool venv 的 python 跑,原始輸出見
`phase1_result.json`)。**沒有讀 `/proc/<pid>/environ`** —— 依 README 警告改用
`doppler secrets --only-names` 與 in-process 斷言。

### 1-A `auth_check` 對 pool 每個槽位都成功(9/9)

用的是 `auth_check` 的同一支實作(`auth_probe.probe_auth` → `notebooks.list()` 真 RPC):

| slot | env | ok | notebooks | account |
|---|---|---|---|---|
| 1 | `NOTEBOOKLM_AUTH_JSON` | true | 3 | pool-account-1@example.invalid |
| 2 | `NOTEBOOKLM_AUTH_JSON_2` | true | 2 | pool-account-2@example.invalid |
| 3 | `NOTEBOOKLM_AUTH_JSON_3` | true | 3 | pool-account-3@example.invalid |
| 4 | `NOTEBOOKLM_AUTH_JSON_4` | true | 4 | pool-account-4@example.invalid |
| 5 | `NOTEBOOKLM_AUTH_JSON_5` | true | 9 | pool-account-8@example.invalid |
| 6 | `NOTEBOOKLM_AUTH_JSON_6` | true | 0 | pool-account-6@example.invalid |
| 7 | `NOTEBOOKLM_AUTH_JSON_7` | true | 2 | pool-account-9@example.invalid |
| 8 | `NOTEBOOKLM_AUTH_JSON_8` | true | 20 | pool-account-7@example.invalid |
| 9 | `NOTEBOOKLM_AUTH_JSON_9` | true | 1 | pool-account-5@example.invalid |

MCP server 本身 `auth_check` → `{"ok": true, "notebooks": 3}`(作用中 = slot 1,數字對得上)。
9 個 label 全是真 email(無 `#N` 退化),`_reject_duplicate_accounts` 亦未觸發 = 無重複帳號。

### 1-B 憑證落檔權限

`_write_credential_file` 實跑落檔:目錄 `0o700`、9 個 `slot-N.json` 全部 `0o600`。

### 1-C lifespan 的 env override 與清理(真的跑一次 `_lifespan`)

先刻意汙染環境(`NOTEBOOKLM_HEADLESS_REAUTH=1`、`NOTEBOOKLM_REFRESH_CMD=/bin/true`):

| 變數 | 進 lifespan 前 | lifespan 內 | 離開後 |
|---|---|---|---|
| `NOTEBOOKLM_HEADLESS_REAUTH` | `"1"` | **`None`(已刪)** | `"1"`(還原) |
| `NOTEBOOKLM_REFRESH_CMD` | `"/bin/true"` | **`None`(已刪)** | `"/bin/true"`(還原) |
| `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` | `"1"` | `"1"` | `"1"` |

pool 內 9 個帳號 label 與上表一致。憑證目錄 `/tmp/notebooklm-mcp-auth-u07snsnj`
在**正常結束(lifespan 展開)後已刪除** → `cred_dir_removed_after_normal_exit = true`。

### 1-D 環境衛生(觀測事實,非 bug)

開跑前掃到 3 個 `/tmp/notebooklm-mcp-auth-*`,依 CLAUDE.md §三 的「ctime 對 lstart」法對照
(**未使用 `fuser`/`lsof`**):

| 目錄 | ctime | 對應 process | 判定 |
|---|---|---|---|
| `43cj6qzy` | 11:42:56 | pid 3762987(`-c prd`,11:42:42 起) | 使用中,**不動** |
| `t0l8x6_6` | 21:45:37 | pid 4117138(`-c stg`,21:45:22 起,本工作區 MCP) | 使用中 |
| `vvz7teps` | 14:41:57 | 無對應活 process | **孤兒**,收工時刪 |


---

## Phase 2 — 讀取面回歸 ✅ PASS

筆記本 **NB-MAIN** `25b4eddf-cfbe-464b-91e3-3a458b965a2a`(`ZZ-TEST v0.9.6 主線 — 排隊與雜湊`)。
`notebook_create` 回傳自動分享給 pool 其餘 8 個帳號(名單與 Phase 1 的 9 槽扣掉建立者一致)。

### 對帳:`source_add_*` 之後 `source_list` 逐字看得到

| 工具 | 回傳 | `source_list` 逐字對帳 |
|---|---|---|
| `source_add_file`(md) | `41c59f33-…fee0`, `char_count=1126` | `A1 排隊與尾端延遲` / `kind=markdown` / `ready=true` ✅ |
| `source_add_file`(md) | `30224c56-…1fb8`, `char_count=993` | `A2 吞吐與負載的取捨` ✅ |
| `source_add_file`(md) | `83f891dd-…6a9e`, `char_count=962` | `B2 生日界與碰撞` ✅ |
| `source_add_text` | `dd272db5-…9b17`(text 入口不回 char_count) | `B1 數位簽章的基本盤` ✅,`source_fulltext` 量到 `char_count=931` |
| `source_add_url` | `465ba2a0-…0496`, `char_count=43858` | `Birthday attack - Wikipedia` / `kind=web_page` ✅ |

- `notebook_get` → `sources_count=5`、`is_owner=false`(**符合** v0.8.1 G-1 的已知上游行為:
  有共享者就恆為 false,不是這一版的迴歸)。
- `notebook_list` → 開跑前 3 本(全是前幾輪驗收留下的 v0.8.x notebook,不屬本輪,未動);
  收工前用它找出本輪標題含 `ZZ-TEST v0.9.6` 的筆記本刪除。
- `artifact_list` → `[]`(空筆記本正確)。
- `feed_info(show_id="zz-test-v096")` → `token=slam737ufsgjznf466qxvybf`,
  feed URL 落在 stg salt 的 URL 空間(與正式節目不互撞,符合 `docs/test-account.md`)。
- `source_fulltext(max_chars=0, contains=[…])` 兩份互查,**領域隔離在來源層先證明**:

  | 來源 | 自家詞 | 對方詞 |
  |---|---|---|
  | A1 | `A1-指紋-01`✓ `63.4`✓ `百分位`✓ `尾端`✓ | `生日`✗ `簽章`✗ |
  | B1 | `B1-指紋-03`✓ `RFC 6979`✓ `簽章`✓ `碰撞`✓ | `排隊`✗ `吞吐`✗ |

- `chat_ask(source_ids=[A1], strip_citations=True, include_references=False)`
  問「負載 0.90→0.99 相對排隊延遲各幾倍」→ 答 `"9, 99"`(與 A1-指紋-02 逐字相符),
  `references=[]`(參數生效)。

---

## Phase 6 ⭐ — v0.9.5 的權限停點新契約 ✅ PASS(**照著做走得出去**)

fixture:`slot_do.py` 用 **slot 5**(`pool-account-8@example.invalid`)的憑證直接建
**NB-DENIED** `6719f969-a403-45a5-b3d7-0651b46a6775`,**刻意不分享**,並以同一槽位加一筆來源。
作用中帳號是 slot 1(`pool-account-1@example.invalid`),看不到它。

### 停點(完整回傳)

```json
{"notebook_id":"6719f969-a403-45a5-b3d7-0651b46a6775","episodes":[],
 "manifest":"…/output/denied/series_manifest.json","complete":false,
 "stopped_at_episode":1,"attempt_id":null,
 "observed_state":"notebook_access_denied",
 "safe_next_action":"notebook_share_with_pool",
 "attempt_count":0,"superseded_attempt_count":0,
 "error":"…這個帳號對 notebook 6719f969-… 沒有存取權。…呼叫 notebook_share_with_pool(notebook_id=…) 補分享…"}
```

- `safe_next_action` = **`notebook_share_with_pool`**(v0.9.5 新契約,舊版回 `podcast_series`)✅
- `attempt_count=0`:守門在建 attempt **之前**就擋下,沒留半成品 ✅

### 照著做:`notebook_share_with_pool(notebook_id=…)`

```json
{"notebook_id":"6719f969-…","shared_by":"pool-account-8@example.invalid",
 "shared_with":["pool-account-1@example.invalid","pool-account-2@example.invalid","pool-account-3@example.invalid",
                "pool-account-4@example.invalid","pool-account-6@example.invalid","pool-account-9@example.invalid",
                "pool-account-7@example.invalid","pool-account-5@example.invalid"],
 "already_shared":[]}
```

- **成功**,而且 `shared_by` 是**真 owner**(slot 5),不是作用中帳號(slot 1)——
  正是 v0.9.0 Phase 9-1 那條「指引自己也 permission denied」的修正 ✅
- **輪替游標不變**:接著那次 series 的第一筆 `dispatch_failover` 記的是
  `from_account: "pool-account-1@example.invalid"`(slot 1)——證明分享用了 slot 5 卻沒挪動游標 ✅

### 再跑一次 series:**往前走了**

```json
{"stopped_at_episode":1,"attempt_id":"24f2a7a7-3f46-4152-8c9e-0f765bc45b3d",
 "observed_state":"pending","safe_next_action":"podcast_series",
 "attempt_count":1,"superseded_attempt_count":0}
```

manifest 的 `dispatch.status="accepted"`、`remote.artifact_id="1c0f8a45-…"` —— 真的送出去了。

---

## 🔴 實跑觀測:配額 failover 真的在動(slot 1/2/3 當日已耗盡)

上面那次 dispatch 的 `attempt.errors[]` 逐筆記下三次真實輪替:

| # | type | from → to |
|---|---|---|
| 1 | `RateLimitError` | `pool-account-1@example.invalid` → `pool-account-2@example.invalid` |
| 2 | `RateLimitError` | `pool-account-2@example.invalid` → `pool-account-3@example.invalid` |
| 3 | `RateLimitError` | `pool-account-3@example.invalid` → `pool-account-4@example.invalid` |

`dispatch.account` 最後是 `pool-account-4@example.invalid`(slot 4)= 實際送出的那一個,記帳正確。
**slot 1–3 今天的配額已經耗盡**,游標停在 slot 4(後續 `notebook_create` 的 `shared_with`
名單不含 slot 4、卻含 slot 5,交叉印證游標位置)。
> 依 CLAUDE.md §一:輪到 slot 8/9(付費兜底,與 prd 共用)時會回報。

---

## Phase 8(部分)— 來源筆數守門

### 8-1 9 筆來源整季照生 ✅ PASS

**NB-GATE** `cc6720e1-6446-43fd-b08e-17049d7083aa`,`notebook_get → sources_count=9`。
`podcast_series` **沒有被擋**,直接 dispatch:
`attempt_id=eb765e2b-b02c-4db4-9847-c8b8413bf929`、`observed_state=pending`、
`remote.artifact_id=be6c81f1-99c7-4574-9a8b-4eb2c1607d0d`。
(邊界擋錯會把 EP01–05 一起關掉,這條是那個迴歸的守衛。)

### 8-3 只等 finalize 的 attempt **不被守門攔** ✅ PASS

在 ep1 的 attempt 已是 `dispatch=accepted` / `remote=pending` 的狀態下,把 NB-GATE 加到
**10 筆**(`61f9252c-…`,`G6 填充來源(第 10 筆)`),再呼一次同一組 `podcast_series`:

```json
{"stopped_at_episode":1,"attempt_id":"eb765e2b-b02c-4db4-9847-c8b8413bf929",
 "observed_state":"pending","safe_next_action":"podcast_series",
 "attempt_count":1,"superseded_attempt_count":0}
```

回的是 `pending` 而**不是** `too_many_sources` → 守門正確跳過「只等 finalize」的 attempt;
`attempt_count` 仍是 1,沒有白長一格。
程式面對得上:守門只掛在 `dispatch_state in ("prepared","not_accepted")` 或
`remote_state in ("failed","removed")` 的共同上游(`tools_podcast.py:3280`)。

### 8-2 10 筆被擋、指名 10 筆也被擋、指名 6 筆放行 ✅ PASS

NB-GATE 完成 EP01 後回錄上傳,筆記本變 **11 筆**。

| 情況 | 呼叫 | 結果 |
|---|---|---|
| 不指名、該集無 attempt | `podcast_series`(ep2) | `too_many_sources` / `safe_next_action=podcast_episode` / `attempt_id=null` / `attempt_count=0` ✅ |
| **指名 10 筆** | `podcast_episode(source_ids=[10 筆])` | **raise**:「指名了 10 筆來源 …fail-closed 從它開始拒絕」,零副作用 ✅ |
| **指名 6 筆** | `podcast_episode(source_ids=[A1,A2,G1,G2,G3,G4])` | **放行並生成完成**:`attempt_id=fd107e4d-…`、`artifact=e973ef83-…`、`mp3=output/gate/ep02.mp3` ✅ |

### 8-4 `generate_slides` / `generate_report` 在超標筆記本上照生 ✅ PASS

10 筆(後為 11 筆)的 NB-GATE 上:

- `generate_report(episode_n=1, study_guide)` → `report_md_path=output/gate/ep01-report.md`、`artifact_id=e3b65f08-…`
- `generate_slides(episode_n=1, detailed)` → `slides_pdf_path=output/gate/ep01-slides.pdf`、`artifact_id=7902446b-…`

音檔的筆數閾值**沒有**套到它們兩個 ✅

---

## Phase 3 ⭐⭐ — `safe_next_action` 全值域「照著做」

工具:`cancel_at.py`(watcher 一路 poll manifest,狀態一到目標就 `task.cancel()`,
砍點是確定的而不是猜秒數)。

| `observed_state` | 造法 | 停點的 `safe_next_action` | 照著做的結果 |
|---|---|---|---|
| `notebook_access_denied` | 未分享 notebook | `notebook_share_with_pool` | ✅ 見 Phase 6,走得出去 |
| `pending` | 短 `wait_timeout` 砍在 wait | `podcast_series` | ✅ 重呼續驗,最後 `complete=true` |
| `acceptance_unknown` | 砍在 `dispatching` | `podcast_episode_reconcile` | ⚠️ **見 FINDING-1**(零候選時指回自己) |
| `continuity_unverified` | 刪掉已完成集的回錄 source | `podcast_attempt_adopt` | ✅ 走得出去(有一處文件落差,見 FINDING-3) |
| `too_many_sources`(無 attempt) | 11 筆 + 新集 | `podcast_episode` | ✅ 指名 6 筆生成成功 |
| `too_many_sources`(有 attempt) | `prepared` attempt + 加到 10 筆 | `podcast_attempt_retract` | ✅ **不傳旗標**成功(見下),但重生指引多繞一格(FINDING-2) |
| `verification_incomplete` | — | — | **inconclusive**(見下) |
| `failed` / `removed` | — | — | **inconclusive**(見下) |
| `not_accepted`(配額) | — | — | **部分**:failover 層實測到(見上面 🔴),但呼叫端看得到的 `not_accepted` 停點沒撞到 |

### 3-A `pending`(砍在 wait 階段)✅

`podcast_series(wait_timeout=90)` → `observed_state=pending` / `safe_next_action=podcast_series` /
`attempt_count=1`。照著重呼(`wait_timeout=600`)→ `complete=true`,EP01 落地
(`mp3_path=output/denied/ep01.mp3`、`feedback_source_id=d206e2fa-…`)。

### 3-B `acceptance_unknown`(client cancellation 砍在 dispatch 之後)

`cancel_at.py … 2 dispatching` → `✅ 砍在 dispatching:attempt=6c3d37fe-…`,
manifest 落成 `dispatch.status='acceptance_unknown'`(不是 `acceptance_unknown` 以外的任何值)。

照著做 `podcast_episode_reconcile`:

```json
{"complete":false,"episode_n":2,"attempt_id":"6c3d37fe-…",
 "observed_state":"acceptance_unknown","candidate_artifact_ids":[],
 "safe_next_action":"podcast_episode_reconcile"}
```

`artifact_list(kind="audio")` 的 ground truth:該筆記本只有 EP01 那顆 artifact,
**EP02 遠端零 artifact** —— 所以「零候選」是正確結論。

series 層的停點也一樣:
```json
{"stopped_at_episode":2,"attempt_id":"6c3d37fe-…","observed_state":"acceptance_unknown",
 "safe_next_action":"podcast_episode_reconcile","attempt_count":1,
 "superseded_attempt_count":0,"candidate_artifact_ids":[]}
```

**照 skill troubleshooting §「`acceptance_unknown` 又重現不了原本的 brief」的三步走得出去**:

1. `artifact_list(kind="audio")` → 零 ✅
2. `podcast_attempt_retract(abandon_in_flight=true)` → **成功**,
   `authorization_basis="abandon_in_flight"`、`dispatch_status_at_retraction="acceptance_unknown"`、
   `remote_status_at_retraction="unknown"` ✅
3. 重生入口 = `podcast_series` ✅

### 3-C `continuity_unverified` ✅ 走得出去

刪掉已完成集的回錄 source(`d206e2fa`)後重呼 series:

```json
{"stopped_at_episode":1,"attempt_id":"24f2a7a7-…",
 "observed_state":"continuity_unverified","safe_next_action":"podcast_attempt_adopt",
 "attempt_count":1,"superseded_attempt_count":0}
```

照著做:重新 `source_add_file(ep01.mp3, title="EP01 簽章與碰撞", mime_type="audio/mpeg")`
→ `7e894fab-…`,再 `podcast_attempt_adopt(attempt_id=24f2a7a7-…, feedback_source_id=7e894fab-…)`:

```json
{"complete":true,"episode_n":1,"attempt_id":"24f2a7a7-…",
 "feedback_source_id":"7e894fab-…","observed_state":"continuity_verified",
 "safe_next_action":"source_delete","stale_source_ids":["d206e2fa-…"]}
```

再照 `source_delete` 做,然後重呼 series → **`complete: true`** ✅

### 3-D `too_many_sources`(有 attempt)✅

`cancel_at.py … 2 prepared` 造出零遠端副作用的 `prepared` attempt
(`4023f8d5-…`),再把 NB-MAIN 從 6 筆加到 **10 筆**,重呼 series:

```json
{"stopped_at_episode":2,"attempt_id":"4023f8d5-…","observed_state":"too_many_sources",
 "safe_next_action":"podcast_attempt_retract","attempt_count":1,"superseded_attempt_count":0,
 "error":"…這一集已經有 active attempt '4023f8d5-…',而它的 settings 是「不指名來源」——
  直接改呼 podcast_episode(..., source_ids=[...]) 會被拒絕(already has durable active attempt)。
  先 podcast_attempt_retract 掉它(純本機動作,不需要 abandon_in_flight…)"}
```

**訊息裡的宣稱逐句驗過**:
- 直接改呼 `podcast_episode(source_ids=[…])` → 真的被拒:
  `episode 2 already has durable active attempt '4023f8d5-…' (dispatch='prepared')` ✅
- 照著做、**不傳旗標** retract → **成功**,`authorization_basis="settled"`、
  `abandon_in_flight=false`、`dispatch_status_at_retraction="prepared"` ✅

### 3-E `failed` / `removed` ✅(用 SDK 的 `artifacts.delete` 真的把遠端 artifact 拿掉)

無法靠等配額穩定重現,改用**真實的遠端終態**:`slot_do.py del_artifact`(SDK
`artifacts.delete`,由 notebook owner slot 執行)把已受理的 artifact 從遠端刪掉,
再讓 finalize 去輪詢 —— SDK 回 NOT_FOUND ⇒ `is_removed` ⇒ `ensure_completed` 拋
`TerminalGenerationError`。manifest 落成:

```json
{"attempt_id":"28ade92b-…","dispatch":{"status":"accepted"},
 "remote":{"status":"removed","status_origin":"remote",
           "error":"Generation removed by server(通常是每日配額耗盡);…status='removed'"}}
```

**正是 README 點名的形狀:`dispatch=accepted` 而 `remote=removed`。**

照 skill 說的重呼 `podcast_series` → **自動 supersede**:
`attempt_id` 換成新的 `82cfb60a-…`、`attempt_count 1→2`、`superseded_attempt_count 0→1` ✅

### 3-F 原樣重呼沿用**同一顆**、`attempt_count` 不增 ✅

(`prepared` 與 `not_accepted` 同屬 `_NEVER_DISPATCHED`,走 `_is_resendable_same_request`
同一條分支。)對一顆 active `prepared` attempt 用**參數完全相同**的 `podcast_series` 重呼:

| | 重呼前 | 重呼後 |
|---|---|---|
| `attempt_id` | `41381651-…` | **`41381651-…`(同一顆)** |
| `attempt_count` | 3 | **3(不增)** |
| `superseded_attempt_count` | 1 | 1 |

### 3-G 未涵蓋的兩格(**inconclusive**,不猜成通過)

- **`not_accepted`(呼叫端可見的停點)**:pool 有 9 個帳號且 failover 會一路輪替,
  要讓呼叫端看到 `not_accepted` 必須**同一次呼叫內 9 個槽位全部拒絕**。這一輪最多輪到
  第 3 格(見上面 🔴),沒撞到。`_mark_not_accepted` 的另一個入口(`NotebookAccessDenied`)
  在 Phase 6 走過,但那條在建 attempt 前就停了。
- **`verification_incomplete`**:要在「已完成集的 drift 複驗途中」發生 `TimeoutError`/
  `ConnectionError`(`tools_podcast.py:3169`)。那是傳輸層失敗,本輪沒有可靠且不造假的
  觸發手段(縮 `wait_timeout` 到 0 會落在別的分支)。

---

## Phase 4 ⭐ — retract 的六種准入情況

每一列都用**真實狀態**造出來,retract 後逐欄位對帳 manifest 的 `retraction` 區塊。

| # | 狀態 | 旗標 | 結果 | `authorization_basis` | `dispatch_status_at_retraction` | `remote_status_at_retraction` |
|---|---|---|---|---|---|---|
| 1 | `prepared`(NB-MAIN ep2 `4023f8d5`) | 不傳 | ✅ 成功 | **`settled`** | `prepared` | `unknown` |
| 2 | `remote=removed`(`82cfb60a`) | 不傳 | ✅ 成功 | **`settled`** | `accepted` | **`removed`** |
| 3 | 正式 output(NB-MAIN ep1 `89077fa5`) | 不傳 | ✅ 成功 | **`output_owner`** | `accepted` | `completed` |
| 3′ | 正式 output(pinned `c1218bd4`、bundle `d8599721`、resume-origin `76ac3aa8`) | 不傳 | ✅ 全成功 | `output_owner` | `accepted` | `completed` |
| 4 | 該集已有**別顆** attempt 接手 output | — | **inconclusive** | — | — | — |
| 5 | 已被 supersede 的**歷史** attempt(`28ade92b`) | 不傳 / **傳 true** | ✅ **兩次都被拒、訊息逐字相同** | — | — | — |
| 6 | `accepted`(`78128e23`)/ `acceptance_unknown`(`6c3d37fe`) 且遠端未回終態 | 不傳→拒 / 傳 true→成功 | ✅ | **`abandon_in_flight`** | `accepted` / `acceptance_unknown` | `pending` / `unknown` |

### 第 2 列 —— v0.9.4 的死路確認已修

```json
{"abandon_in_flight": false, "dispatch_status_at_retraction": "accepted",
 "authorization_basis": "settled", "remote_status_at_retraction": "removed",
 "safe_next_action": "podcast_series", "next_step": "用 podcast_series 重生。"}
```

### 第 5 列 —— 訊息指出真正的 active/output、且**不**教旗標

不傳旗標與傳 `abandon_in_flight=true` 得到**完全相同**的一句話:

```
attempt '28ade92b-…' 已經不是 episode 1 的 active 或 output attempt
(現在 active='82cfb60a-…'、output=None)—— 它是歷史紀錄,retract 動不了它,
`abandon_in_flight` 也只放行 active 那一顆。要作廢的是現在那顆的話,用它的 id 重呼本工具。
```

照它說的「用它的 id 重呼」→ 對 `82cfb60a` 的 retract **成功**(即第 2 列)✅

### 第 6 列 —— 拒絕訊息可執行

不傳旗標被拒的訊息教:`artifact_list(notebook_id, kind="audio")` → 帶
`abandon_in_flight=True` 重呼。**兩個 attempt 各照做一次,都成功**,
`authorization_basis` 正確記成 `abandon_in_flight`(而不是只記「傳了什麼」)。

### 第 4 列為什麼 inconclusive

`is_active ∧ ¬is_output ∧ output_attempt_id≠None`(active/output 分岔)在 v0.9.6 的
**支援路徑上造不出來** —— `_ensure_resume_attempt` 兩條分支都顯式擋掉
(`already has durable output …; retract it … before resuming another artifact`,本輪實測過),
`_create_audio_attempt` 也擋。剩下的來源只有 v0.5.0 前的 legacy manifest 或手改 JSON,
而手改 manifest 是 ADR-0009 明文禁止、也會讓這一格的驗證失去意義。
**結案方式**:留給 `tests/test_attempt_capabilities.py` 的笛卡爾積窮舉(本機檢查 §8 已綠)。

---

## Phase 5 ⭐ — 重生入口的白名單判準

四種形狀各造一次、各 retract 一次:

| attempt 怎麼建的 | 實際 `settings` | 回傳的重生入口 | 判定 |
|---|---|---|---|
| `podcast_series`(不指名) | `{language, audio_format, audio_length}` | **`podcast_series`** | ✅ |
| `podcast_episode(source_ids=[…])` | 多一個 `source_ids` | **`podcast_episode`** + 「必須帶回原本那組 `source_ids`」 | ✅ |
| `podcast_episode(input_bundle_path=…)` | 多 `source_ids`、且 `input_bundle` 非 None | **`podcast_episode`** + 「用一份**新的、尚未綁定**的 frozen bundle」 | ✅ |
| `podcast_episode_resume` 建的 | **`{"origin": "explicit_resume"}`** | **`podcast_episode`** | ✅ 入口對,**但警告句錯 → FINDING-4** |

### 5-A 內容隔離的逐字稿驗證(這一輪的關鍵數據)

同一集 EP01、同一個筆記本,只差「有沒有指名來源」:

| 版本 | 生成入口 | 帶進去的來源 | 自家(A 領域)探針 | 對方(B 領域)探針 |
|---|---|---|---|---|
| 第 1 版 `89077fa5` | `podcast_series`(不指名) | 筆記本全部 5 筆 | 尾端✓ 百分位✓ 負載✓ 延遲✓ | **簽章✓ 碰撞✓ 生日✓(洩漏)** |
| 第 3 版 `3a4ef6e9` | `podcast_episode(source_ids=[A1,A2])` | **只有 2 筆** | 尾端✓ 百分位✓ 吞吐✓ 負載✓ **63.4✓ 200 毫秒✓** | **簽章✗ 碰撞✗ 生日✗ RFC 6979✗ 2^63.1✗ PlayStation✗** |

**自家指紋 6/6 命中(探針有效)、對方 6/6 為 0。** 第二個獨立資料點:NB-GATE EP02
(指名 6 筆)→ 尾端✓ 百分位✓ 63.4✓ / 簽章✗ 碰撞✗ 生日✗ 2^63.1✗ RFC 6979✗
(該集**標題裡就有「碰撞」兩個字**,逐字稿仍然 0 命中)。

⚠️ **探針方法學修正**:`排隊` **不能**當判別詞 —— NB-DENIED EP01 的唯一來源是 B1
(`source_fulltext` 證實 B1 不含「排隊」),回錄逐字稿卻命中「排隊」(主持人拿它當
一般口語用)。可用的判別詞是 `尾端 / 百分位 / 吞吐` 與數字指紋。

### 5-B frozen bundle 的兩句宣稱都驗過

- EP03 用 `bundles/ep03-v1` 生成,manifest 記下
  `runtime_brief_sha256=ca25e2b4…`(= 磁碟上 `runtime-brief.md` 的 sha,**不是** brief 字串的 sha)、
  `attempt_binding_sha256` 也寫了 → provider 輸入確實來自凍結 bytes ✅
  逐字稿對凍結 brief 點名的四項:反壓✓ 5.1✓ 63✓ 99 個節點✓ 拒絕✓,B 領域 0/3 ✅
- retract 後**沿用同一份 bundle** 重呼 → `attempt 'd8599721-…' was retracted (episode 3);
  it is history` —— 在任何生成 RPC 之前擋下,零配額 ✅(警告句的宣稱為真)
- 照著換成**新的** `bundles/ep03-v2` → **dispatch 成功**(`attempt b90f9644`、
  `artifact 430efea0`)✅ 出路可執行

---

## Phase 7 — attempt 狀態機其餘入口 + 稽核

### 7-A tombstone default-deny ✅

對 retracted 的 `6c3d37fe`:

| 工具 | 結果 |
|---|---|
| `podcast_episode_reconcile` | ❌ 拒:`attempt … was retracted (episode 2); it is history — work on the replacement attempt instead` |
| `podcast_attempt_adopt` | ❌ 拒:同一句 |
| `podcast_episode`(沿用該 bundle) | ❌ 拒:同一句(EP03 那顆) |
| `podcast_attempt_retract`(自己) | ✅ **冪等**:回傳與首次逐欄位相同(`retracted_at` / `reason` / `authorization_basis` 都沒被覆寫) |

### 7-B `_ensure_resume_attempt` 的**兩個**分支都走到 ✅

在 ep1 有一顆 active `prepared`(`41381651`)、外加一顆**已 claim 且未 retract 的歷史
attempt**(`28ade92b`,claim 了 `394ec597`)的狀態下:

| 分支 | 呼叫 | 訊息 |
|---|---|---|
| **已 claim 的 sibling** | `resume(artifact_id=394ec597)` | `episode 1 has another active attempt '41381651-…'; 參數完全相同就原樣重呼…要換 brief 或來源就先 podcast_attempt_retract(**不需要** abandon_in_flight),再用 podcast_series 重生。` |
| **新建** | `resume(artifact_id=28f18710)`(該 manifest 內無人 claim) | `episode 1 has active attempt '41381651-…' (dispatch='prepared'); ` **+ 完全相同的那句指引** |

兩條分支的指引由**同一顆** `_attempt_next_step` 產生(v0.9.5 只修一條的問題已收斂),
而且**照著做真的做得到**:「原樣重呼」實測沿用同一顆(見 3-F);「先 retract(不需要旗標)」
實測對 `prepared` 成功(見 Phase 4 第 1 列)✅

### 7-C 其餘

- `podcast_episode_resume`(正常路徑):EP03 timeout 後照錯誤訊息續完,
  **`attempt_id` 不變**(`d8599721`)—— 沒有多建一顆,checkpoint 續跑正確 ✅
- `podcast_episode_reconcile`:唯一候選 / 零候選兩條都走過(見 3-B)✅
- `podcast_attempt_adopt`:`feedback_source_id` 採納成功 → `continuity_verified` ✅
- `artifact_wait`:對 pending 的 task 正常等待(背景完成)✅
- `artifact_rename`:兩次成功;⚠️ 對**還在生成中**的 artifact 改名,完成時會被
  NotebookLM 用自動標題覆寫回去(`0b88eace` 實測),QA 標記要等 `completed` 後再做
- `artifact_retry_failed`:對已被遠端刪除的 artifact → `artifact … 不在 notebook …
  (用 artifact_list 確認)`,fail-closed ✅。**真正 `status="failed"` 的 artifact 本輪
  取得不到(`artifacts.delete` 造出的是 `removed`),retry 的成功路徑 inconclusive。**

---

## Phase 9 — 附件與發布

| 工具 | 結果 |
|---|---|
| `generate_slides` | ✅ `output/gate/ep01-slides.pdf`(PDF 1.4, 17 頁) |
| `generate_report` | ✅ `output/gate/ep01-report.md`(7496 bytes,study_guide) |
| `artifact_download_slides` | ✅ 用 artifact_id 重下載並回寫 manifest |
| `artifact_download_report` | ✅ 同上 |
| `artifact_download_audio` | ✅ 重下載的 sha256 與 finalize 下載**逐 byte 相同**(`d9665aee…`) |
| `artifact_revise_slide` | ✅ 回新 artifact `fae8d394`(標題 `… (2)`)、`superseded_artifact_id=7902446b`,**舊那顆原封還在**、manifest 指向新 PDF —— 與 docstring 逐項相符 |
| `episode_set_description` | ✅ 寫入並回 `stripped=true`,通過 publish 的 preflight(非空、不等於標題) |
| `research_start` / `research_wait` / `research_import` | ✅ fast/web 跑完回 10 個候選;**指名不存在的 URL 直接 raise 並列出來**;指名 2 筆 → 只有那 2 筆進 scratch notebook |
| `source_delete` | ✅ 多次;冪等(刪已不存在的 id 也回 `deleted`) |
| `notebook_create` / `notebook_share_with_pool` / `auth_check` | ✅ 見 Phase 1/2/6 |
| `generate_audio` | ✅ 見 Phase 10-4(撞配額直接拋) |
| `publish_series` | **未執行 —— 依 CLAUDE.md §四 需先問使用者** |

### `published_at` 回溯 ✅(retract → 重生後 RSS 日期不變)

EP01 第 1 版完成時 `published_at="Mon, 10 Aug 2026 22:11:18 +0800"`。
retract(該欄位被收進 `retraction.retracted_output`)→ 指名來源重生 →
第 3 版於 22:29 完成,episode 的 `published_at` **仍是 `22:11:18`** ✅

---

## Phase 10 ⭐ — skill × MCP 搭配(照著做)

| # | 指引 | 結果 |
|---|---|---|
| 1 | `tool-reference.md` 的 `abandon_in_flight` 六列狀態表 | ✅ 4 列實測通過、1 列(第 5)實測被拒且訊息正確、1 列(第 4)inconclusive(見 Phase 4) |
| 2 | `troubleshooting.md`〈`acceptance_unknown` 又重現不了原本的 brief〉三步 | ✅ 三步照做走得出去(見 3-B) |
| 3 | `troubleshooting.md`〈工具說「來源太多」拒絕生成〉兩種 `safe_next_action` | ✅ 兩種都造出來、都照做成功(見 3-D 與 Phase 8-2) |
| 4 | `SKILL.md` §Auth:**低階 `generate_audio` 沒有配額 failover** | ✅ **決定性**,見下 |
| 5 | retract 回傳有 `next_step` 與 `authorization_basis` | ✅ 每一次 retract 回傳都有這兩個欄位 |

### 10-4 判別實驗(`phase10_failover.py`,同一 process、同一格游標)

```
pool = [pool-account-1, pool-account-2, pool-account-3, pool-account-4, pool-account-8,
        pool-account-6, pool-account-9, pool-account-7, pool-account-5]

generate_audio: {"call":1, "active_before":"pool-account-1@example.invalid",
                 "active_after":"pool-account-1@example.invalid",      ← 沒有 rotate
                 "ok":false, "error_type":"RateLimitError",
                 "error":"API rate limit or quota exceeded…"}

作用中帳號在 generate_audio 被拒之後仍是 'pool-account-1@example.invalid'(沒有 rotate = 沒有 failover)

podcast_episode:  受理成功(artifact_id='394ec597-…';只有 30s wait 逾時)
作用中帳號現在 = 'pool-account-6@example.invalid'                    ← podcast 家族 rotate 了
```

**`generate_audio` 撞配額直接拋 `RateLimitError`、游標一格都沒動;
同一個 process 緊接著的 podcast 家族呼叫 rotate 到別的帳號並成功受理。**
獨立第二次確認:透過**真的 MCP server**(不是腳本)呼叫 `generate_audio` 也直接拋同一個錯。

---

# 🔴 抓到的東西

判準是這一輪唯一的那句:**照著每一個 `safe_next_action` 做,走不走得出去。**

---

## FINDING-1 —— `acceptance_unknown` 零候選時,`safe_next_action` 指回它自己,而「原地打轉」偵測抓不到

**嚴重度:中。** 不是死路(skill 散文裡有出路),但**工具回傳裡沒有出路**,
而 skill 教呼叫端「拿不準就直接照 `safe_next_action` 做」。

**可觀測事實**(完整回傳見 §3-B):

```json
podcast_episode_reconcile → {"observed_state":"acceptance_unknown",
  "candidate_artifact_ids":[], "safe_next_action":"podcast_episode_reconcile"}
podcast_series           → {"observed_state":"acceptance_unknown",
  "safe_next_action":"podcast_episode_reconcile",
  "attempt_count":1, "superseded_attempt_count":0}
```

連呼兩次 `reconcile`,回傳**逐欄位相同**。ground truth 是 `artifact_list` 證實遠端零
artifact,所以這個迴圈**永遠**不會自己結束。

**為什麼「原地打轉」偵測救不了**:skill 與 `partial()` 的註解都說唯一依據是
「`attempt_count` / `superseded_attempt_count` 持續增加」。這條路上兩者**恆為 1 / 0** ——
偵測條件從不成立。

**與 v0.9.6 立論的關係**:`_attempt_capabilities` 是「所有訊息與 action 的單一事實來源」,
但這個回傳(`tools_podcast.py:2030-2037`)是**手寫**的,沒有經過它、也沒有 `next_step` 欄位。
同一顆 attempt 餵進 `_attempt_capabilities` 會得到 `needs_abandon_flag=True`,
`_attempt_next_step` 會產出正確的那句(「artifact_list 查過雲端之後帶 abandon_in_flight=true」)
—— 資訊在,只是沒接上。

**建議**:零候選分支改由 `_attempt_capabilities` / `_attempt_next_step` 產生 `next_step`
(值域不必改,`safe_next_action` 可維持 `podcast_episode_reconcile`),把
`abandon_in_flight` 那條出路寫進回傳。離線測試:對零候選分支斷言回傳含 `next_step`
且該字串提到 `abandon_in_flight`。

---

## FINDING-2 —— 守門停點叫你 retract,retract 完卻叫你回去撞同一道牆

**嚴重度:低(會終止,但多繞一格)。**

`too_many_sources`(有 attempt)→ `safe_next_action=podcast_attempt_retract` ✅ →
照做成功 → 但 **retract 的回傳是**:

```json
{"safe_next_action":"podcast_series","next_step":"用 podcast_series 重生。"}
```

而這個筆記本正是**因為 `podcast_series` 生不出指名 settings 才停下來的**。照著做:

```
podcast_series → {"observed_state":"too_many_sources","safe_next_action":"podcast_episode",
                  "attempt_id":null,"attempt_count":1}   ← 又被擋一次,這次才指對
```

實測**會**終止(第二次守門因為該集已無 attempt,改指 `podcast_episode`),所以不是死鎖。
但只讀 `safe_next_action` 的自動化會白跑一趟守門。同一件事在**擋下它的那個工具**的
`error` 文字裡已經講對了(「再用**指名版**重生」)—— 兩個欄位又一次不同調。

根因:`podcast_attempt_retract` 的 `_regeneration_entry_point` 只看那顆 attempt 的
`settings`,不知道筆記本此刻已經超標。

**建議**:retract 不打 RPC(設計如此),所以不必讓它自己去數來源;比較便宜的修法是
守門停點的 `error` 已經有的那句話,一併寫進 retract 之後的 `next_step`
(例如由呼叫端把「筆記本超標」帶進來),或至少在 `too_many_sources` 的 `error` 裡
明說「retract 之後**不要**再呼 series」。

---

## FINDING-3 —— skill 的 `continuity_unverified` 一行指引缺了必填參數

**嚴重度:低(文件)。**

`troubleshooting.md:128` 寫:

> `continuity_unverified` | 已完成集的 feedback source 在遠端不見了 |
> `source_list` 找出正確 id 後 `podcast_attempt_adopt(feedback_source_id=...)`

**逐字照做會失敗**:

```
podcast_attempt_adopt(manifest_path, episode_n=1, feedback_source_id=…)
→ Error: attempt_id may be omitted only for legacy durable output
```

補上 `attempt_id`(series 停點的回傳裡就有)才成功。工具的錯誤訊息**沒有**直接說
「請補 `attempt_id`」,只說了它什麼時候可以省略。

**建議**:skill 那一行補成 `podcast_attempt_adopt(attempt_id=…, feedback_source_id=…)`;
順手把工具那句改成「非 legacy 的 attempt 必須傳 `attempt_id`(見停點回傳的 `attempt_id`)」。

---

## FINDING-4 —— retract 對 `origin="explicit_resume"` 的 attempt,給出一句**事實錯誤且不可執行**的重生警告

**嚴重度:中。這是這一輪唯一一個「指引在它自己產生的狀態下不可執行」的新實例。**

`podcast_episode_resume` 建的 attempt,`settings` 逐字是 `{"origin": "explicit_resume"}`
(`brief_sha256` 是 `None`)—— **它沒有任何 `source_ids`**。retract 它:

```json
{"attempt_id":"76ac3aa8-…","authorization_basis":"output_owner",
 "safe_next_action":"source_delete",
 "next_step":"先把 stale_source_ids 全部 source_delete,再用 podcast_episode 重生。
   **重生時必須帶回原本那組 `source_ids`** —— 這一集的生成輸入指名了來源,
   改用 podcast_series 會靜默改成讀整本筆記本。"}
```

重生入口 `podcast_episode` **是對的**(白名單:認不出是 series 建的就要求明示)。
**錯的是那句警告**:它斷言「這一集的生成輸入指名了來源」,而 manifest 裡逐字寫著
`{"origin":"explicit_resume"}`。呼叫端照著去找「原本那組 `source_ids`」——
**manifest 裡不存在那組東西**,找不到就只能猜,而猜錯正是這條警告本來要防的事故。

**根因**(`tools_podcast.py:2858-2872`):

```python
if entry_point != ACTION_EPISODE:      pinned_warning = ""
elif retracted_had_bundle:             pinned_warning = "…新的、尚未綁定的 bundle…"
else:                                  pinned_warning = "…必須帶回原本那組 source_ids…"
```

`_regeneration_entry_point` 對**兩種不同**的形狀都回 `ACTION_EPISODE`:
(a) 真的指名了 `source_ids`、(b) **認不出來**的形狀(resume-origin、
`source_ids=[]`/`input_bundle={}` 這類 falsy-but-present 的舊 manifest)。
`else` 分支把 (b) 當成 (a) —— 正是那顆函式自己的 docstring 點名要 fail-safe 的兩種漏網形狀。

判準與結論分家:`_regeneration_entry_point` 已經把「為什麼回 episode」算出來了,
但只回傳一個字串,警告句只好回去用 `if/else` 猜,**猜錯的正好是白名單特意涵蓋的那一類**。

**建議**:讓 `_attempt_capabilities` 多回一個 `regeneration_reason`
(`pinned_sources` / `frozen_bundle` / `unrecognised`),三句警告各對一個值;
`unrecognised` 那句要說實話 ——「這顆 attempt 沒有留下來源 provenance,重生時必須
自己指名 `source_ids`(本集來源 + 最近 5 集回錄),不可改用 `podcast_series`」。
離線測試:`tests/test_attempt_capabilities.py` 的維度常數加上
`settings ∈ {series-shaped, pinned, resume-origin, empty-pinned, empty-bundle}`,
對每一格斷言「警告句提到的東西在該 attempt 上真的存在」。

---

## 觀測到但**不是** bug 的三件事(避免下一輪重查)

1. **憑證落檔到 `/tmp/notebooklm-mcp-auth-*`** —— v0.9.0 的核心設計。權限 0700/0600 已驗、
   正常結束會刪乾淨已驗(Phase 1-C)。
2. **`notebook_get.is_owner` 恆為 `false`** —— 上游 SDK 對 share status 的解讀,
   v0.8.1 G-1 已結案。
3. **`RateLimitError` ≠ 當日配額耗盡** —— 13:56 有三個槽位連續拒絕、游標推到第 4 格;
   同樣的 slot 1 在 14:22 的另一個 process 裡又被受理。`rotate_client()` 的 docstring
   已經把「游標按被拒次數前進、不按帳號狀態」列為已知取捨 —— 這一輪實測到它的代價:
   **一次瞬時限流會讓那個帳號在該 process 餘生都不再被試**。同一天內三個帳號因此被提早退場。

### `publish_series` — **未執行(依使用者決定)**

CLAUDE.md §四 要求動它之前先問。使用者先批准「只發 EP01 一集(只上傳一個 blob)」,
但實測發現**做不到**:preflight 掃**整份 manifest**,而 NB-MAIN 的 EP02/EP03 正是驗收
過程中被 retract 掉的集數。要發就得先把它們補完 → 會變成永久上傳 3 個 blob,超出批准範圍。
回報後使用者決定不發。

**零 PUT 的情況下實測到的 fail-closed preflight 鏈**(每一關都在任何上傳之前):

1. 只傳 `manifest_path` → `missing show fields: show_id, show_title, show_description,
   author, owner_name, owner_email, artwork_path — 首次發布請顯式傳齊…`
2. 補齊 show 七欄(封面用 `notebooklm-cover --show`,3000×3000 JPEG,過 Apple 驗證)
   → `episode 1: cover_path is required (每集必做,不再 fallback 節目封面)`
3. 補齊單集封面(`notebooklm-cover --manifest`,三集各 3000×3000 JPEG 並回寫 manifest)
   → `episode 2: description is required (真 show notes,不可空白)`

三關都**逐集**、都在 `TemporaryDirectory` 與任何 PUT 之前,沒有任何 orphan media 落到 NAS ✅

**沒驗到的**:RSS 輸出本身(`feed.xml` 的 `pubDate`)與公網讀回驗證。
**已驗到的替代證據**:`published_at` 在 retract→重生後於 manifest 層**逐字不變**
(`Mon, 10 Aug 2026 22:11:18 +0800`,見 Phase 9 §`published_at` 回溯),而 publisher
讀的就是這個欄位。`feed_info` 也確認測試 feed 落在 stg salt 的獨立 URL 空間。

---

# 收工

## 覆蓋自檢 ✅

README 的腳本跑過:**35 個 MCP 工具全部在本檔有紀錄**,無 `⚠️ 未記錄`。

## 清理 ✅

- **notebook**:本輪建的 4 本(標題含 `ZZ-TEST v0.9.6`)全部以**各自的 owner 槽位**刪除 ——
  `25b4eddf`(slot 1)、`6719f969`(slot 5)、`cc6720e1`(slot 4)、`475375f3`(slot 6)。
  刪後 `notebook_list` 只剩使用者自己的兩本(未動)。
- **憑證殘留**:依 CLAUDE.md §三 用 **ctime ↔ lstart 對照**(**未使用 `fuser`/`lsof`**)判定:

  | 目錄 | ctime | 對應活 process | 處置 |
  |---|---|---|---|
  | `43cj6qzy` | 11:42:56 | pid 3762987(`-c prd`,11:42:42) | 保留 |
  | `t0l8x6_6` | 21:45:37 | pid 4117138(`-c stg`,21:45:22) | 保留 |
  | `vvz7teps` | 14:41:57 | 無 | **已刪** |

- **本輪腳本(`phase1_auth.py` / `slot_do.py` / `cancel_at.py` / `phase3_cancel.py` /
  `phase10_failover.py`)零殘留** —— 它們都走 lifespan 或 `try/finally`,正常結束自刪。
  這也是 Phase 1-C「正常結束會刪乾淨」的第二組實測樣本。

## 未結案的前提(要記進程式碼或 AGENTS.md)

1. **FINDING-1**:`podcast_episode_reconcile` 零候選分支不經 `_attempt_capabilities`,
   `safe_next_action` 自我指向且無 `next_step`。→ 修法與離線測試見該節。
2. **FINDING-2**:retract 之後的 `next_step` 不知道筆記本已超標。→ 見該節。
3. **FINDING-3**:skill `troubleshooting.md:128` 的 `podcast_attempt_adopt` 少了 `attempt_id`。
4. **FINDING-4**:`origin="explicit_resume"` 的重生警告句是假的。
   → `tests/test_attempt_capabilities.py` 的維度常數要加 `settings` 這一維
   (`series-shaped / pinned / resume-origin / empty-pinned / empty-bundle`),
   **不要另外挑情境寫**(README 明講:挑情境正是前幾輪漏掉的原因)。
5. **Phase 4 第 4 列**(active/output 分岔)在支援路徑上造不出來 →
   結案方式:交給 `test_attempt_capabilities.py` 的笛卡爾積。
6. **`verification_incomplete`** 與 **`artifact_retry_failed` 的成功路徑** 本輪無可靠觸發手段 → inconclusive。
7. **`not_accepted`(呼叫端可見)**:9 槽位同時拒絕才看得到,本輪未達 → inconclusive。
