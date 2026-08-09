# notebooklm-mcp v0.9.0 真實環境驗收 — FINDINGS

- 日期:2026-08-09
- 工作區:`/home/user/research/audiskill/nblm-acceptance-v0.9.0`
- Doppler config:`notebooklm / stg`(9 槽位:`NOTEBOOKLM_AUTH_JSON` + `_2`…`_9`)
- 實裝:`notebooklm-mcp 0.9.0` · `notebooklm-py 0.8.0` · `mcp 1.29.0`
- 命令:`nblm-mcp`(shim → `/home/user/.local/share/uv/tools/notebooklm-mcp/bin/python`)

判定用詞:**PASS** 有可觀測事實支撐 / **FAIL** 與期望不符 /
**INCONCLUSIVE** 判斷不了(絕不寫成 PASS)/ **N/A-刻意不測** 附理由。

---

## 總結

**v0.9.0 的核心改動是對的,而且是用「新舊行為的判別實驗」證明的,不只是「沒壞」。**
35/35 個工具都被碰過。抓到 **2 個 FAIL**(都不在核心機制上,都是契約/指引與行為不符)、
**2 個 INCONCLUSIVE**、**4 個工作區文件缺陷**。

| 面向 | 判定 | 一句話證據 |
|---|---|---|
| Phase 0 本機檢查 | **PASS** | 12/0,硬關卡 §3 綠 |
| Phase 1 `mcp` 1.29.0 協定層 | **PASS** | 35 工具、4 支複雜 schema 零漂移、stdout 非 JSON 行數 **0**、三種錯誤格式完整 |
| Phase 2 憑證檔生命週期 | **PASS** | 9 槽位 = 9 個 `0600` 檔於 `0700` 目錄;正常結束 `exit 0` 且目錄消失;6 種壞憑證全部啟動時 raise 並指名槽位 |
| Phase 3 讀取面 13 支 | **PASS** | 全部回結構正確的結果;`converted_from`、`is_owner=false`、`contains` 正負探針都對上 |
| Phase 4 單集正常路徑 | **PASS** | 44 MB mp3 + sha256 對帳;artifact / 回錄 source / manifest 三處同名 `EP01 佇列延遲`;探針正向 4/6、負向 0/3 |
| Phase 5 五條 dispatch + 三個續跑入口 | **PASS** | 五條都真的走到;failover 鏈式 `(A→B)(B→C)(C→D)`、`attempts=1`;reconcile 的零/唯一/多候選三態全部走到 |
| Phase 6 身分正確性 | **PASS** | 並行時序真的排出來了;**env 汙染實驗**證明身分來自 client 自己的 storage_state |
| Phase 7 sharing | **PASS** | ⭐ **OWNER 前提結案**:`shared_users` 含 owner 且 `permission=OWNER`,並有實跑反例佐證;VIEWER 修正綠燈 |
| Phase 8 附件/研究/救援 11 支 | **PASS,1 支 FAIL** | slides/report/description/research/cover/救援全綠;`artifact_revise_slide` **FAIL** |
| Phase 9 skill 層 | **停點 PASS,指引 FAIL** | `notebook_access_denied` 停點正確,但**照著做解不開** |

### 兩個 FAIL

1. ⛔ **`notebook_access_denied` 的指引在它自己產生的狀態下不可執行**(Phase 9-1)——
   `error` 叫呼叫端跑 `notebook_share_with_pool`,而那支工具用**作用中帳號**執行,
   正是看不到這個 notebook 的那個帳號,於是同樣 permission denied。文件描述的典型情境
   (既有 notebook + pool 已 rotate)**就是這個死路的常態**。
2. ⛔ **`artifact_revise_slide` 的「artifact 不變」是錯的**(Phase 8)——
   實測在遠端 fork 出 `System Latency Realities (2)`,舊的還在;實裝碼 docstring 與
   skill 文件都寫錯,而它放大了文件自己承認的「分不出是哪一集的 deck」風險。

### 兩個 INCONCLUSIVE(絕不寫成 PASS)

1. `artifact_retry_failed` 的**成功路徑** —— 這一輪 11 次生成沒有自然產生 `failed` artifact,
   而遠端刪除得到的是 `removed` 不是 `failed`,無法可靠製造。只驗到負向 fail-loud。
2. `source_delete` 之後那筆 source **對生成端 context 的影響** —— `source_list` 已看不到它,
   但 `source_fulltext` 55 分鐘後仍讀得到全文,所以 retract 流程「清掉 stale 回錄 source
   以免污染後續各集」的效果**未被證明**。

### 使用者明確選擇不做的

`publish_series` 的**成功發布路徑**(uploader 不刪檔,上傳即永久)。已用三層 preflight
負向測試 + `HTTP 404` 證明「preflight 擋在第一個 PUT 之前」。

---

### 閱讀順序說明

**下面的小節是按「實際執行順序」排的,不是 Phase 編號順序。** 因為生成動輒十幾分鐘,
不燒配額的 Phase 7 / 9-1 是刻意塞在 Phase 4 的等待視窗裡跑的 —— 那個交錯本身就是
Phase 6-c 要的並行時序,把它拆開重排會失去時間軸證據。索引:

| Phase | 位置 |
|---|---|
| 0 本機檢查 | 「Phase 0 — 本機檢查」 |
| 1 協定層 | 「Phase 1 — 協定層」 |
| 2 認證層 | 「Phase 2 — 認證層」(含兩個文件缺陷) |
| 3 讀取面 13 支 | 「Phase 3 — 讀取面全工具回歸」 |
| **7 sharing** | 「Phase 7 — sharing」← 在 Phase 4 等待期間跑 |
| **9-1 access_denied** | 「Phase 9-1」← 同上,**FAIL** |
| 4 單集正常路徑 | 「Phase 4 — 單集正常路徑」 |
| 5 五條 dispatch | 「Phase 5 — attempt 狀態機」 |
| 6 身分正確性 | 「Phase 6 — 身分正確性」(6-d 是決定性實驗) |
| 5 續跑三入口 | 「續跑入口 1/3 ~ 3/3」(夾在 Phase 6 之後) |
| 9-2 abandon_in_flight | 「Phase 9-2」 |
| 8 附件/研究/救援 | 「Phase 8」(含 **FAIL**) |
| 9-3 skill 文件 | 「Phase 9-3」 |
| 收工 | 「覆蓋自檢」→「配額帳」→「收工紀錄」→「要寫回程式碼 / 文件的東西」 |

---

## Phase 0 — 本機檢查(不打 RPC)

`bash local-checks.sh` → **通過 12 / 失敗 0**,PASS。逐條實測輸出:

| 節 | 斷言 | 結果 |
|---|---|---|
| 0 | `notebooklm-mcp` 版本 | `0.9.0` ✅ |
| 0 | `notebooklm-py` 版本 | `0.8.0` ✅ |
| 0 | `mcp` 版本(僅資訊) | `1.29.0`(與 lock 一致) |
| 1 | `_sync_auth_env` 已移除、`snapshot()` 就位且 AST 內無 suspend 點 | ✅ |
| 2 | `_dispatch_audio_with_failover` 吃 `account`/`client`、回三元組 | ✅ |
| 2 | 無殘留「dispatch 後重讀全域」註解(計數 = 0) | ✅ |
| 3 ⭐ | 空值 `__Secure-1PSIDTS` 在**落檔前**就被擋掉(與 SDK strict loader 同語義) | ✅ |
| 3 | 憑證檔權限 `0o600` | ✅ |
| 4 | `podcast_attempt_retract` 有 `abandon_in_flight`,預設 `False` | ✅ |
| 4 | `tools_podcast` 原始碼含 `notebook_access_denied` 停點 | ✅ |
| 5 | `_has_sufficient_permission`:VIEWER→False、EDITOR→True、OWNER→True、email 大小寫不敏感 | ✅ |
| 6 | 上游 SDK 未漂移(`add_user` 預設仍 VIEWER、`permission` 仍可位置傳、`SharedUser` 有 `permission`) | ✅ |
| 7 | MCP 工具數 = 35 | ✅ |

**硬關卡 §3 綠燈** → 允許進入真帳號情境。

### 開工前的環境基線(既有殘留,不是這一輪造成的)

```
$ ls -ld /tmp/notebooklm-mcp-auth-*
drwx------ 2 audichuang audichuang 4096 Aug  9 21:19 /tmp/notebooklm-mcp-auth-39et0a_9   ← 本 session 的 live server
```

`pgrep -af` 抓到的 server(開工時刻):

| PID(python) | 啟動 | 已活 | cwd / config | 判定 |
|---|---|---|---|---|
| 2446476 | 09:36:02 | **11h44m** | `nblm-acceptance-v0.7.1`,`-c stg` | **前一輪驗收的孤兒**(CLAUDE.md §三 預言的那種,這次是第二次發生) |
| 1785660 / 1789271 / 2061517 / 2064789 | 更早 | — | `-c dev`,舊命令名 `notebooklm-mcp` | 其他 session 的孤兒(4 個) |
| 3074816 | 21:18:50 | — | `nblm-acceptance-v0.9.0`,`-c stg` | 本 session 的 MCP server |

→ 共 **5 個 server 孤兒**在機器上。收工時處理(見末節)。

---

## Phase 1 — 協定層:mcp 1.29.0 換版

方法:繞過 Claude Code,直接用原始 stdio 跟 `doppler run -p notebooklm -c stg -- nblm-mcp
--transport stdio` 講 JSON-RPC(`scratchpad/stdio_probe.py`),這樣才看得到 stdout/stderr 分離。

**1-1 工具清單 — PASS。** `tools/list` 回 **35** 個工具,名單與 `local-checks.sh §7`
的 35 一致,也與本 session Claude Code client 看到的 35 個 `mcp__notebooklm__*` 逐字一致:

```
artifact_download_audio artifact_download_report artifact_download_slides artifact_list
artifact_rename artifact_retry_failed artifact_revise_slide artifact_wait auth_check
chat_ask episode_set_description feed_info generate_audio generate_report generate_slides
notebook_create notebook_get notebook_list notebook_share_with_pool podcast_attempt_adopt
podcast_attempt_retract podcast_episode podcast_episode_reconcile podcast_episode_resume
podcast_series publish_series research_import research_start research_wait source_add_file
source_add_text source_add_url source_delete source_fulltext source_list
```

`initialize` 回 `protocolVersion: 2025-06-18`、capabilities 齊全、`instructions` 完整送達。

**1-2 參數 schema 沒漂 — PASS。** 四支複雜工具的 wire schema 與
`references/tool-reference.md` 的範例逐欄對照:

| 工具 | required | 抽查的 optional 預設值 | 與 doc 一致? |
|---|---|---|---|
| `podcast_episode` | `brief`(nullable 但必帶 key)· `episode_n` · `notebook_id` · `output_dir` · `title` | `audio_format='deep-dive'` · `audio_length='long'` · `wait_timeout=1200.0` · `source_ids=None` | ✅ |
| `generate_report` | `episode_n` · `manifest_path` · `notebook_id` | `report_format='study_guide'` · `wait_timeout=1800.0` | ✅ |
| `research_wait` | `notebook_id` · `task_id` | `timeout=1800.0` · `max_report_chars=0` | ✅ |
| `publish_series` | **只有** `manifest_path` | 其餘 13 欄全 `None`(`require_slides`/`require_report` 也是 `None` 而非 `True`) | ✅ doc 明說「只有 `manifest_path` 必填」 |

**1-3 錯誤回傳格式 — PASS**(三種都完整送達、非空、非截斷):

| 打壞的方式 | 回傳 |
|---|---|
| `notebook_get(notebook_id="")` | `isError: true` + `Error executing tool notebook_get: The server rejected this request (invalid argument).`(上游 RPC 錯誤原文;server log 同時有 `RPC GET_NOTEBOOK failed after 0.366s: RPCError rpc_code=3`) |
| `notebook_get(notebook_id=123)` | pydantic 驗證錯誤全文:`1 validation error for notebook_getArguments / notebook_id / Input should be a valid string [type=string_type, input_value=123, input_type=int]` + 官方連結 |
| `tools/call no_such_tool` | `Unknown tool: no_such_tool`,並在 stderr 留 `WARNING Tool 'no_such_tool' not listed, no validation will be performed` |

> 觀察(非 FAIL):空字串 `notebook_id` **沒有本機前置驗證**,會真的送一趟 RPC 出去。
> 錯誤訊息可讀,所以判 PASS,但這條路每次都燒一次往返。

**1-4 stdout 純淨 — PASS。** 整段 session 的 stdout **非 JSON 行數 = 0**。
1.29.0 多印的那行確實只在 stderr:

```
pydantic_settings/sources/utils.py:47: IncompleteFieldDefinitionWarning:
Field 'lifespan' has an incomplete definition: its annotation contains an
unresolved forward reference, so settings sources may fail to correctly resolve its value.
```

stderr 另有 400 行 SDK 的 HTTP INFO log(9 個帳號各自跑一輪 `notebooklm.google.com → 301
→ notebook.google.com → 302 → accounts.google.com/ServiceLogin → SetOSID → 200`)。
`f.sid=***` 已被遮罩,沒看到 cookie 值外洩。

**1-5 觀察:`serverInfo.version` 報的是 MCP SDK 版本,不是 app 版本 — 低嚴重度缺口。**

```json
"serverInfo": {"name": "notebooklm", "version": "1.29.0"}
```

`1.29.0` 是 `mcp` 套件版本(FastMCP 未指定 version 時的預設),不是 `notebooklm-mcp 0.9.0`。
本 repo 的歷史失敗模式正是「uv git cache 壞掉 → 靜默裝成舊版」(`local-checks.sh §0`
就是為它寫的),而**呼叫端從 MCP handshake 看不出自己接到哪一版**。建議 `FastMCP(...)`
帶 `version=importlib.metadata.version("notebooklm-mcp")`。這是改善建議,不是這一輪的 FAIL。

---

## Phase 2 — 認證層:憑證檔的生命週期

**2-a 全部帳號都建得起來 — PASS。** 本 session 的 live server(pid 3074816,cwd 為本工作區):

```
$ ls -ld /tmp/notebooklm-mcp-auth-*
drwx------ 2 audichuang audichuang 4096 Aug  9 21:19 /tmp/notebooklm-mcp-auth-39et0a_9
$ ls -l /tmp/notebooklm-mcp-auth-39et0a_9/
-rw------- 1 audichuang audichuang 12793 slot-1.json
-rw------- … 12795 slot-2.json   12784 slot-3.json   12771 slot-4.json   13306 slot-5.json
       13666 slot-6.json   12790 slot-7.json   12771 slot-8.json   12762 slot-9.json
```

- slot 檔數 **9**;Doppler `stg` 的槽位 **9**(`NOTEBOOKLM_AUTH_JSON` + `_2`…`_9`)→ 相等,
  沒有槽位建 client 失敗被吞掉
- 目錄 `0700`、九個檔全部 `0600` ✅
- 探針那一輪(獨立 process)同樣拿到 `dir mode 0o700 / slot count 9 / file modes ['0o600']`

**2-b 正常結束後不留檔 — PASS。** 探針關掉 stdin(EOF = 正常結束)→ `exit code: 0` →
`/tmp/notebooklm-mcp-auth-vhxasmwb` **完全消失**(`正常結束後殘留: (無 —— 清乾淨)`)。

**2-c 壞憑證要在啟動時大聲失敗 — 6 個情境全部 raise、exit=1。**
全部只在本機 shell 的 env 層做,**Doppler 未被修改**。

| # | 情境 | 實際訊息 | 判定 |
|---|---|---|---|
| i | `_2` 的 `__Secure-1PSIDTS` 為空字串(**多槽位**) | `RuntimeError: NOTEBOOKLM_AUTH_JSON_2 不是可用的 storage_state:ValueError: 必要 cookie 的值是空的:['__Secure-1PSIDTS']` | **PASS**,且**指名槽位** |
| i-附 | 同上,`RotateCookies` 出現次數 | **0** | PASS(L2 那條路沒被走到) |
| i-附 | 同上,失敗後 auth dir 殘留 | **無新增**(例外路徑也走同一條 `AsyncExitStack`) | PASS |
| ii | 清掉不帶後綴的 `NOTEBOOKLM_AUTH_JSON`(留 `_2`…`_9`) | `RuntimeError: NOTEBOOKLM_AUTH_JSON_2 存在,但編號在 NOTEBOOKLM_AUTH_JSON 就斷了。補上 … 或把編號接連續——靜默跳過會讓那個帳號永遠不進 pool` | PASS |
| iii | `_2` 設成與 base 相同的憑證 | `RuntimeError: pool 裡有重複帳號 ['<masked>@gmail.com']:兩個槽位是同一個帳號,配額沒有變多,而 failover 會寫下一筆謊報的 rotation…` | PASS(email 真的抓到了 → 這道檢查沒退化成 `#N`) |
| iv | 跳號(有 `_2`、無 `_3`、有 `_4`…`_9`) | `RuntimeError: NOTEBOOKLM_AUTH_JSON_4 存在,但編號在 NOTEBOOKLM_AUTH_JSON_3 就斷了…` | PASS,**指名斷點** |
| v | `_2` 設成空字串 | `RuntimeError: NOTEBOOKLM_AUTH_JSON_2 是空的——憑證沒設好,不是「沒有這個帳號」` | PASS |

### ⚠️ 文件缺陷:README §Phase 2-c(i) / acceptance-v0.9.0.md §1-b 的 repro 測不到它想測的東西

兩份文件給的重現方式是**只設一個槽位**:

```bash
NOTEBOOKLM_AUTH_JSON='{"cookies":[…"__Secure-1PSIDTS","value":""…]}' nblm-mcp --transport stdio
```

實跑結果:確實 `exit=1`,但 raise 來自 **SDK strict loader**,不是 `_write_credential_file`
的預驗證,訊息**沒有指名槽位**:

```
app.py:261  client = await stack.enter_async_context(NotebookLMClient.from_storage())
  → notebooklm/_auth/cookies.py:455 _build_httpx_cookies_from_storage_strict
  → ValueError: Missing required cookies: __Secure-1PSIDTS
```

原因在 `app.py:239` —— **`if len(creds) > 1:`**。單帳號刻意不走落檔路徑
(原始碼註解:「單帳號不走這條:沒有輪替就沒有 race」),所以單槽位 repro 根本
**碰不到那段預驗證**,而那段預驗證正是 §0 tripwire 的真帳號對應面。

- 對安全論證無害:單帳號走 env 模式,`_resolve_recovery_path` 在 env 模式回 `None`,
  L2 recovery 天生不可達(所以單槽位空值 PSIDTS 也不會發 RotateCookies)。
- 但**驗收價值是零**:多槽位預驗證若回歸,照文件跑會照樣看到 `exit=1` 而判成通過。
- **正確 repro**(本輪實際採用,已 PASS):留 Doppler 的 9 槽位,只把 `_2` 換成空值憑證。

→ 建議把 README §Phase 2-c 與 `docs/acceptance-v0.9.0.md §1-b` 的指令改成多槽位版本。

### ⚠️ 文件缺陷:CLAUDE.md §三 的收尾清理指令會刪掉**正在跑的** server 的憑證

```bash
for d in /tmp/notebooklm-mcp-auth-*; do fuser "$d" >/dev/null 2>&1 || rm -rf "$d"; done
```

實測(對 live server pid 3074816 持有的 `/tmp/notebooklm-mcp-auth-39et0a_9`):

```
fuser dir : (none)
fuser file: (none)
/proc/<pid>/fd 也沒有任何 fd 指向 auth dir
```

`_write_credential_file` 寫完就關檔、SDK 讀完也關檔,目錄不是任何 process 的 cwd
→ **`fuser` 永遠看不到持有者**,那行的 `||` 於是恆真,會把 live server 的憑證整包刪掉。

較安全的替代(ponytail 版:沒有任何 server 在跑時才清):

```bash
pgrep -f 'bin/nblm-mcp' >/dev/null || rm -rf /tmp/notebooklm-mcp-auth-*
```

(這是工作區文件的缺陷,不是 v0.9.0 產品缺陷。)

---

## Phase 3 — 讀取面全工具回歸(13 支,全部經由 `runtime.get_client()`)

Notebook:`ZZ-TEST v0.9.0 Phase3 scratch` = `4c9bf138-5899-439b-b85c-a8a085e71d9b`

| 工具 | 實際回傳(節錄) | 判定 |
|---|---|---|
| `auth_check` | `{"ok": true, "notebooks": 3}` | PASS |
| `notebook_list` | 3 本(全是前幾輪驗收殘留:`v0.8.1 驗收 D —— 刻意不分享(SDK 直建)` / `v0.8.1 驗收 —— 重送 failover 與自動分享` / `v0.8.0 驗收 —— 配額 failover`) | PASS |
| `notebook_create` | 回 `notebook_id` + `shared_with`(**8 個帳號**,pool 共 9 → 其餘全拿到) | PASS(同時是 **7-d**) |
| `notebook_get` | `{"sources_count":0, "is_owner": false, "created_at":"2026-08-09T13:28:29+00:00"}` | PASS —— `is_owner=false` 符合已知行為 |
| `source_add_file`(`.md`) | `{"source_id":"ec2464d9…","char_count":904}` | PASS |
| `source_add_file`(`.json`) | `{"source_id":"05b6ab31…","converted_from":"probe-config.json","char_count":153}`;`source_list` 標題為 **`probe-config.json.md`** | PASS —— 自動轉存 `.md` + 回 `converted_from` 兩項都對上 |
| `source_add_url` | `https://en.wikipedia.org/wiki/Little%27s_law` → `char_count: 37146`(非 0,無 paywall warning) | PASS |
| `source_add_text` | `{"source_id":"c4b5f45d…"}` | PASS |
| `source_list` | 4 筆,`kind` 分別 `web_page` / `pasted_text` / `markdown` / `markdown`,全部 `ready: true` | PASS |
| `source_fulltext` | `max_chars=0` + `contains=[…]` → `{"char_count":139,"hits":{"ZZPROBE-TEXT-4C71":true,"利用率":true,"這個字串不該出現-XYZ":false},"truncated":true,"content":""}` | PASS —— 正負探針都對 |
| `source_delete` | 刪真 id → `{"deleted":"05b6ab31…"}`;刪 `00000000-…-000000000000` → **也回 `deleted`**(文件已註明 SDK idempotent) | PASS |
| `artifact_list` | 新 notebook → `{"artifacts": []}` | PASS |
| `chat_ask` | 見下 | PASS(附 2 條觀察) |

### 3-1 ⚠️ 文件缺陷:README §Phase 3 說「`chat_ask` 預設清引用標記」是錯的

實測預設呼叫的 `answer` **帶著標記**,`references` 也照回:

```
"answer": "**利用率與等待時間之間並非線性關係**，…呈現非線性的雙曲線爆炸式增長 [1, 2]。…十倍以上的劇烈變化** [2]。"
"references": [{"citation_number":1,…},{"citation_number":2,…}]
```

帶 `strip_citations=true, include_references=false` 才變成:

```
"answer": "利用率與等待時間之間並非線性關係，而是呈現一條…雙曲線 。…非線性爆炸**，…延遲感 。"
"references": []
```

`inputSchema` 的預設是 `strip_citations=false` / `include_references=true`,而
skill `references/tool-reference.md:527` 寫的是「四個選填參數皆可省,**預設維持舊行為
(標記保留、references 照回)**」—— **skill 正確,工作區 README 那一行錯**。
(順帶:清標記後會留下標記原處的空格,如 `雙曲線 。`,純美觀問題。)

### 3-2 觀察:`chat_ask` 不傳 `conversation_id` 時兩次呼叫回**同一個** `conversation_id`

兩次獨立呼叫都回 `da107e69-08a6-4a1e-84c2-1e210272e51f`。看起來 NotebookLM 每本
notebook 只有一串 chat,所以「不傳 conversation_id」不等於「開新對話」。
非 v0.9.0 引入,但對「避免 show notes 被前文污染」的假設有影響 —— 想隔離只能靠
`source_ids`,不能靠開新對話。標記為**觀察**,未判定。

### 3-3 觀察:`source_delete` 之後 `source_fulltext` 仍讀得到該來源全文

刪掉 `05b6ab31…` 後:

- `source_list` → 只剩 3 筆,該 id 不在列表 ✅
- `source_fulltext(source_id="05b6ab31…")` → 仍回 `{"title":"probe-config.json.md","char_count":153,"hits":{"ZZPROBE-JSON-9F3A":true}}`

即「已從筆記本移除」不等於「讀不到內容」。可能是 NotebookLM 端的最終一致性延遲。
標記為**觀察**;收尾時會再探一次確認是否只是延遲。

---

## Phase 7 — sharing(四項全 PASS,其中 7-a 是這一輪要結案的離線證不了的前提)

pool 帳號在本文件一律遮罩成 `前6碼…@gmail.com`。實測推出的槽位順序
(來自 `notebook_create` 的 `shared_with` 排序 + failover 鏈):

| slot | 帳號 | 備註 |
|---|---|---|
| 1 | `pool-account-1…`(base) | 建立所有 ZZ-TEST notebook 的 owner |
| 2 | `pool-account-2…` | |
| 3 | `pool-account-3…` | |
| 4 | `pool-account-4…` | |
| 5 | `pool-account-8…` | |
| 6 | `pool-account-6…` | |
| 7 | `pool-account-9…` | |
| 8 | `pool-account-7…` | CLAUDE.md 說最後兩格是付費兜底、與 `prd` 共用 |
| 9 | `pool-account-5…` | 同上 |

### 7-a ⭐ 結案:`get_status().shared_users` **含 owner 那一列**,現行實作正確 — PASS

方法:以 base 槽位(= notebook owner)身分跑**唯讀** `sharing.get_status()`,不動任何權限。

```
caller (base slot) = pool-account-1…@gmail.com
ShareStatus fields = ['access', 'is_public', 'notebook_id', 'share_url', 'shared_users', 'view_level']
shared_users 共 9 列:
  - pool-account-1…@gmail.com   permission=<SharePermission.OWNER: 1>   ← 就是 caller/owner 本人
  - pool-account-2…@gmail.com   permission=<SharePermission.EDITOR: 2>
  - pool-account-3…@gmail.com   permission=<SharePermission.EDITOR: 2>
  - pool-account-4…@gmail.com   permission=<SharePermission.EDITOR: 2>
  - pool-account-8…@gmail.com   permission=<SharePermission.EDITOR: 2>
  - masked…@example.invalid   permission=<SharePermission.EDITOR: 2>
  - masked…@example.invalid   permission=<SharePermission.EDITOR: 2>
  - masked…@example.invalid   permission=<SharePermission.EDITOR: 2>
  - pool-account-5…@gmail.com   permission=<SharePermission.EDITOR: 2>
```

→ **有 owner 那一列,permission 就是 `OWNER`。** `_has_sufficient_permission` 把 OWNER
算進「已足夠」因此是對的,那條「failover 後 B 對 owner A 打 `add_user(EDITOR)` 靜默降權」
的路徑**不存在**。`tools_basic._has_sufficient_permission` 的 `⚠️ P1,未被離線證明的前提`
註解可以結案(改寫見本文件末節「要寫回程式碼的東西」)。

**額外的真實世界佐證(比唯讀查詢更強)**:7-b 那次 `notebook_share_with_pool` 是在
pool **已經 rotate 到 slot 4** 的狀態下跑的(EP01 撞配額換了三次帳號),所以
`_pool_peers` 真的把 owner `pool-account-1…` 放進了 peers —— 而它落在 `already_shared`,
沒有被打 `add_user(EDITOR)`。**這就是那條降權路徑的實跑反例。**

### 7-b VIEWER 修正(v0.9.0 的 P0)— PASS

前置(模擬「網頁上手動分享」= 預設 VIEWER;網頁預設與 SDK `add_user` 預設都是 VIEWER,
伺服器狀態相同,所以用 SDK 直接把一個 pool 帳號降成 VIEWER):

```
降權後 masked…@example.invalid → permission=<SharePermission.VIEWER: 3>
```

然後跑 MCP `notebook_share_with_pool(4c9bf138-…)`:

```json
{
  "shared_with": ["pool-account-7@example.invalid"],
  "already_shared": ["pool-account-1@…","pool-account-2@…","pool-account-3@…",
                     "pool-account-8@…","pool-account-6@…","pool-account-9@…","pool-account-5@…"]
}
```

→ VIEWER 那個帳號落在 **`shared_with`**(被重送成 EDITOR),**不是** `already_shared`;
其餘 7 個 EDITOR/OWNER 正確跳過。舊版的錯誤行為(算成 `already_shared`、`add_user` 0 次)
沒有出現。**這一項是整支工具的存在理由,綠燈。**

### 7-c 後檢 fail-loud — PASS

用不存在的 email `zz-nonexistent-9f3a-acceptance@example.invalid`:

```
--- 1) 上游 add_user 直接呼叫 ---
上游沒有 raise。回傳 shared_users 筆數 = 9
bogus email 在裡面嗎? False              ← 呼叫成功但沒生效,上游完全不吭聲

--- 2) 走我們的 _share_each(含後檢)---
raise 了: RuntimeError
notebook '4c9bf138-…' 分享給 zz-nonexistent-9f3a-acceptance@example.invalid 呼叫成功但未生效
(get_status 後檢仍不是 EDITOR/OWNER——可能是 workspace 網域政策擋外部分享,或伺服器靜默忽略)。
已分享:[]。請確認 email 正確後改跑 notebook_share_with_pool 重試。
```

→ 「上游不 raise、我們 raise」兩邊都實測到了。

### 7-d `notebook_create` 的自動分享 — PASS

三次 `notebook_create` 全部回 `shared_with` 含**其餘 8 個帳號**(pool 9 − active 1),
`sharing.get_status` 後檢確認全是 `EDITOR`。

---

## Phase 9-1 — `notebook_access_denied` 的指引 — 停點 PASS,**但「照著做」解不開 → FAIL**

製造狀態:用 base(slot 1)的憑證**直接走 SDK** 建一個 notebook 並加兩個來源,
刻意**不分享**給 pool(`shared_users` 只有 owner 一列)。
notebook = `3b8003aa-d63c-431d-b7ca-468ac5f09c65`,owner = `pool-account-1…`,
此時作用中帳號已經因為 EP01 撞配額 rotate 到 slot 4(`pool-account-4…`)。

### 停點本身:PASS

`podcast_series(notebook_id="3b8003aa…", episodes=[…])` 回:

```json
{
  "episodes": [], "complete": false, "stopped_at_episode": 1,
  "attempt_id": "31a91751-20d8-43ff-9d68-3593c41c9c08",
  "observed_state": "notebook_access_denied",
  "safe_next_action": "podcast_series",
  "attempt_count": 1, "superseded_attempt_count": 0,
  "error": "The server rejected this request (permission denied). … 帳號
   'pool-account-4@example.invalid' 對這個 notebook 沒有存取權。多帳號 pool 模式要求 notebook
   對 pool 全員可存取 —— 呼叫 notebook_share_with_pool(notebook_id=...) 補分享給其餘帳號
   (EDITOR)後再重試;MCP 自建 notebook(v0.8.1 起)已自動分享,這通常是舊版建立或在
   網頁上手動建立的既有 notebook。"
}
```

`observed_state` 對、`error` **指名了 `notebook_share_with_pool(notebook_id=...)`**、
被拒的帳號有寫出來、`attempt_count` 為 1。舊版「無聲原地打轉」的問題確實修掉了。

### ⛔ FAIL:照著指引做的下一步,自己也 permission denied

```
> notebook_share_with_pool(notebook_id="3b8003aa-d63c-431d-b7ca-468ac5f09c65")
Error executing tool notebook_share_with_pool: The server rejected this request
(permission denied). If you have multiple Google accounts signed in, this is commonly
an account-routing mismatch …
```

根因:`notebook_share_with_pool` 用 `runtime.snapshot()` 拿**作用中**帳號
(`tools_basic.py:234`),然後 `await client.sharing.get_status(notebook_id)`
(`:238`)—— 而作用中帳號**正是那個看不到這個 notebook 的帳號**。看不到就查不了 share
status,更不可能 `add_user`。**只有 owner 分享得動**,而 MCP 沒有任何方式指定「用哪個
槽位執行」。

於是在**產生這個停點的那個狀態下,指引是不可執行的**,呼叫端進死路(換一種死路而已):

| 想走的路 | 結果 |
|---|---|
| `podcast_series` 原樣重呼 | 同一個停點(游標刻意不為權限輪替)—— 文件自己說了 |
| `notebook_share_with_pool` | **permission denied**(本項實測) |
| 重啟 server(游標回到 slot 1) | 只有在 **owner 剛好是 slot 1** 時有效 —— 錯誤訊息完全沒提 |
| 在 NotebookLM 網頁上手動分享 | 可行,但需要人;錯誤訊息也沒提 |

而這**正是文件描述的典型情境**(「舊版建立或在網頁上手動建立的既有 notebook」):
那種 notebook 的 owner 通常就是使用者自己那個主帳號 = slot 1,而 pool 會 rotate 走,
所以「作用中帳號 != owner」是**常態,不是邊角**。

**驗證解法真的有效**:改用 owner(slot 1)的憑證直接走 SDK 對 8 個 peer
`add_user(EDITOR)`,8/8 後檢 `ok=True`;之後同一支 MCP 工具就回得出正常結果:

```json
{"shared_with": [], "already_shared": ["pool-account-1@…", …8 個]}
```

→ 工具本身沒壞,壞的是**「誰來執行」這件事在 access-denied 狀態下無解**。

**建議**(任一即可,由淺到深):
1. 最省:錯誤訊息補上「若作用中帳號看不到這個 notebook,`notebook_share_with_pool`
   也會 permission denied —— 請用 owner 帳號分享(重啟 server 讓游標回到 slot 1,
   或在網頁上手動分享)」。**這一版至少要做這個**,否則指引把呼叫端指進死路。
2. `notebook_share_with_pool` 在 `get_status` 吃到 permission denied 時,自動改用
   pool 裡其他槽位試一輪(誰是 owner 誰就成功),而不是直接把裸例外丟出來。


---

## Phase 4 — 單集正常路徑(`podcast_episode` 全新一集)— PASS

notebook `db96baf4-0fba-4943-a1e5-7a866cb8ef02`(`ZZ-TEST v0.9.0 EP01 佇列延遲`),
來源只有 `ep01-a` + `ep01-b`,`source_ids` 顯式指名兩筆。

工具回傳:

```json
{
  "episode": 1, "title": "佇列延遲", "label": "EP01 佇列延遲",
  "task_id": "4d31f4d2-6d16-401c-8d54-844f2f923e38",
  "artifact_id": "4d31f4d2-6d16-401c-8d54-844f2f923e38",
  "mp3_path": ".../output/ep01-single/ep01.mp3",
  "published_at": "Sun, 09 Aug 2026 21:49:03 +0800",
  "attempt_id": "2179af73-2f7b-48f3-bf73-944d9380c6bc",
  "feedback_source_id": "3a80b7a3-3465-49fe-abe1-92cd54870982"
}
```

要對帳的事實,逐項:

| 事實 | 實測值 | 判定 |
|---|---|---|
| mp3 落地且非空 | `46096306` bytes(44 MB),`sha256=8158b9bd0d3efb917e19956d6fea59494fceee51b0579112a53b27f891d991c9`,manifest `finalize.download.status=completed` 且 `bytes` 對得上磁碟 | PASS |
| `dispatch.account` 有值 | `pool-account-4@example.invalid`(slot 4) | PASS |
| `published_at` 有值 | `Sun, 09 Aug 2026 21:49:03 +0800` | PASS |
| 回錄 source 與 artifact **完全同名** | `artifact_list` → audio artifact title = `EP01 佇列延遲`;`finalize.feedback_source_rename.source_name` = `EP01 佇列延遲`;回錄 source 的 `source_fulltext.title` = `EP01 佇列延遲` | PASS,三處逐字相同 |
| finalize 四個 checkpoint | `artifact_rename` / `download` / `feedback_source_upload` / `feedback_source_rename` 全 `completed` | PASS |
| 回錄 source 轉錄逐字稿長度 | `char_count = 14261` | PASS(真的吃進去了) |

### 探針詞正反對照(**先證探針活著,再看洩漏**)

```json
"hits": {"排隊": true, "延遲": true, "吞吐": false, "尾端": true, "負載": true, "百分位": false,
         "簽章": false, "碰撞": false, "生日": false}
```

1. **正向(先驗)**:自家主題詞命中 **4 / 6**(`排隊`/`延遲`/`尾端`/`負載`)→ ≥2,**探針活著**,
   這一輪的負向檢查有效。(`吞吐`、`百分位` 未命中 —— 加入實測會被口語/ASR 打壞的名單。)
2. **負向**:對方領域三個詞 `簽章`/`碰撞`/`生日` **全部 false** → **沒有跨集洩漏**。

### 附帶觀察(非 FAIL,但發布時要知道)

- **下載到的 `.mp3` 其實是 MP4/M4A 容器**:`file output/ep01-single/ep01.mp3` →
  `ISO Media, MPEG v4 system, Dynamic Adaptive Streaming over HTTP`。副檔名是 `.mp3`
  但位元組不是 MPEG-1 Layer III。這不是 v0.9.0 引入的(NotebookLM 給的就是這個),
  但 `publish_series` 的 RSS `<enclosure type=...>` 若寫死 `audio/mpeg` 就與實際容器不符。
  未在本輪判定(沒跑 publish),**列為 publish 前要確認的一項**。
- **`generate_slides` / `generate_report` 產的 artifact 不套 `EP{n:02d} {title}` 命名**:
  slide_deck 叫 `ZZ-TEST v0.9.0 EP01 佇列延遲`(= notebook 標題)、report 叫
  `佇列與尾端延遲深度解析學習指南`(= 模型自己取的)。只有 audio 走同名紀律。
  文件沒承諾附件同名,所以不算 FAIL,但 notebook 裡多集並存時不好辨識。

### 併發副產物:`generate_report` 與音檔 finalize 同時寫同一份 manifest,沒有互相蓋掉

`generate_report` 在 EP01 的音檔還在 `remote: pending` 時跑完並回寫
(`revision` 7 → 8),之後音檔 finalize 繼續推到 `revision 18`。最終 manifest 裡
**兩邊的欄位都在**(`report_md_path` / `report_format` 與完整的 `attempts[0]`
含三筆 failover),`attempts` 筆數仍是 1。→ `ManifestStore` 的併發寫入在真實時序下成立。

---

## Phase 5 — attempt 狀態機:五條 dispatch 路徑

### 配額的真實形狀(這一輪真的撞到了,而且撞了很多次)

- **拒絕是同步 raise**,不是等待:`RateLimitError: API rate limit or quota exceeded.
  Please wait before retrying. (Upstream: Resource exhausted.)`
- **每次換帳號約 0.7 秒**(13:33:35.129 → 13:33:35.896 → 13:33:36.584),三次 rotate
  合計 1.5 秒就找到可用帳號 —— failover 對呼叫端是「幾乎無感」的。
- **實測每個帳號每天 3 次音檔生成**:slot 4(`pool-account-4…`)接了 3 集
  (`ep01-single#EP1`、`access-denied#EP1`、`ep-retry#EP1`)之後第 4 次就 `RateLimitError`,
  游標移到 slot 5。開工時 slot 1/2/3 就已經是耗盡狀態(當天前面幾輪打掉的)。

### 五條路徑逐條對帳

| # | 路徑 | 怎麼真的走到 | 結果 |
|---|---|---|---|
| 1 | `podcast_episode` 全新一集 | Phase 4,`ep01-single#EP1` | **PASS** |
| 2 | `podcast_episode` 原樣重呼 | `ep-retry#EP1`,見下 | **PASS** |
| 3 | `podcast_series` 全新一集 | `access-denied#EP1` 完整跑完 + `#EP2` | **PASS** |
| 4 | `podcast_series` **inline 重送** | `access-denied#EP1` 撞 `notebook_access_denied` → 補分享 → 重呼整季 | **PASS** |
| 5 | `podcast_series` supersede | 見本節末 | 見下 |

**#1 的 failover 鏈式接龍(最重要的那組欄位)** —— `ep01-single` manifest `errors[]`:

```
(1) RateLimitError  from pool-account-1@…          -> pool-account-2@…   13:33:35.129
(2) RateLimitError  from pool-account-2@…  -> pool-account-3@…          13:33:35.896
(3) RateLimitError  from pool-account-3@…         -> pool-account-4@…        13:33:36.584
dispatch.account = pool-account-4@…   (= 最後實際送出的那個)
attempts 筆數 = 1                    (換帳號是原地重送,沒有新建 attempt、沒有 supersede)
```

- **鏈式**:`(A→B)`、`(B→C)`、`(C→D)`,不是每次都從 A 重來 ✅
- **每筆 `from_account` 都是該次實際被拒的帳號** ✅(A 被拒→A;B 被拒→B;C 被拒→C)
- `dispatch.account` = 最後實際送出 ✅

**#2 `podcast_episode` 原樣重呼 —— 三次呼叫、同一個 attempt。**
notebook `5c4188d7`(SDK 直建、刻意不分享),manifest `output/ep-retry/`:

| 呼叫 | 參數 | 結果 | manifest |
|---|---|---|---|
| 1 | 原始 | raise `NotebookAccessDenied` | attempt `5ed76cb5` 建立,`dispatch.status=not_accepted`,`remote.status=failed`,`remote.error_code=NotebookAccessDenied`,`errors` 1 筆 |
| 2 | **逐字相同** | 同樣 raise | **attempts 仍是 1**、`attempt_id` 仍是 `5ed76cb5`、`dispatched_at` 13:51:44 → 13:52:03、`errors` 累積成 2 筆、`supersedes_attempt_id` 為 `None` |
| 3 | 同上但 `wait_timeout=60`(補分享之後) | dispatch **accepted**,artifact `8f29c794` 綁上同一個 attempt,之後 wait 超時 | 仍是同一個 attempt,`dispatch.account=pool-account-4@…` |

→ 「從未成功 dispatch 的 attempt + 參數逐字相同 = 沿用同一個 attempt 重送」實測成立,
**不多燒配額、不新建 attempt**。`wait_timeout` 不算 settings,改它不影響沿用(符合設計)。

**#4 series inline 重送**(v0.8.0 漏補過的那條):`access-denied#EP1` 第一次跑
`notebook_access_denied` 停在 attempt `31a91751`;補分享後**重呼整季**,同一個
`31a91751` 被恢復成 prepared 再送出 → `accepted`,`attempts` 筆數仍是 **1**,
`errors[]` 保留原來那筆 `dispatch / not_accepted`,最終完整跑完(mp3 44 744 450 bytes)。
→ 三態契約(沿用 / fail-loud / supersede)裡的「沿用」這一態成立。

### 超時救援:`podcast_episode_resume` 的錯誤訊息可直接照抄

`wait_timeout=60` 逼出的錯誤本身就是續跑指令:

```
Task 8f29c794-… timed out after 60.0s (last status: pending; status history: pending)
音檔已在雲端生成(artifact_id='8f29c794-…')但後續步驟失敗。既有 attempt_id='5ed76cb5-…';
用 podcast_episode_resume 續完(不會重新生成):
podcast_episode_resume(notebook_id='5c4188d7-…', episode_n=1, title='尾端延遲',
  artifact_id='8f29c794-…', output_dir='…/output/ep-retry',
  manifest_path='…/output/ep-retry/series_manifest.json')
```

→ 含 `attempt_id`、`artifact_id`、以及一整行可貼上的呼叫。**這是很好的可執行性**
(對比 Phase 9-1 那條指引就是反例)。

---

## Phase 6 — 身分正確性

### 6-a 單線 — PASS
Phase 4 的 `ep01-single#EP1` 下載成功(46 096 306 bytes,sha256 已記錄)。

### 6-b failover 之後 — PASS
同一集在 dispatch 階段換過三次帳號,最後由 slot 4 送出;**下載與回錄上傳都成功**,
`dispatch.account` 全程是 `pool-account-4@…`。

### 6-c ⭐ 並行 —— 這一輪真的排出來了(以往從沒測過)

三個生成請求落在**同一個 server process**、時間窗互相重疊。跨 manifest 合併時間軸(UTC):

```
13:53:03.6  DISPATCH  ep-retry#EP1     accepted  pool-account-4@…     ← A 集送出
13:53:09.7  ACCEPTED  ep-retry#EP1               pool-account-4@…     ← A 進入風險視窗
13:54:17.5  REMOTE    access-denied#EP1 completed
13:54:19.0  DISPATCH  access-denied#EP2 accepted pool-account-8@…         ← B 集
13:54:20.7  FAILOVER  access-denied#EP2 RateLimitError
                        from pool-account-4@… -> pool-account-8@…          ← B 把全域游標移離 slot 4
13:54:25.5  ACCEPTED  access-denied#EP2          pool-account-8@…
```

**要對帳的三件事:**

1. **A 的 `dispatch.account` 在 B rotate 之後仍是 `pool-account-4@…`** ✅
   (B 在 13:54:20 把游標推到 slot 5;A 的紀錄沒有被改寫)
2. **A、B 的 `dispatch.account` 不同**(slot 4 vs slot 5),各自指向真的送出它的帳號 ✅
3. **B 的 failover `from_account` = `pool-account-4@…`** —— 這剛好**就是** A 的帳號。
   README 的檢查條目寫「B 的 `from_account` 不是 A 的帳號」,但在本輪的實際時序下
   **B 真的是先試 slot 4 才被拒的**,所以 `from_account=slot 4` 是**誠實的紀錄**,不是
   bug。這一條的判準應改寫成「`from_account` 是這次 dispatch 實際被拒的帳號」——
   本輪符合。**README 該條目在「A、B 同時落在同一個帳號」的時序下不具鑑別力。**

> ⚠️ 誠實標註:manifest **沒有記錄「下載是哪個帳號發出的」**
> (`finalize.download` 只有 path/bytes/sha256/temp_path)。所以上面三條只能證明
> **記帳沒有被串改**,證不了「下載的位元組真的是用 pinned client 取的」。
> 那一半用下面的 6-d 直接測。

### 6-d ⭐⭐ 直接證明「身分跟著 client 走」(quota-free,決定性)

v0.9.0 的核心宣稱在 `_artifact/downloads.py:709`:
`cookies = await asyncio.to_thread(self._cookie_loader, self._storage_path)`
—— 下載當下重讀的是**這個 client 自己的 storage_state 路徑**,而不是 process 全域 env。

實驗:同一個 process、同一個 audio artifact(`4d31f4d2`,EP01),兩個不同 storage_state
檔各建一個 client;中途以 owner 身分把其中一個帳號 `remove_user` 出共享名單:

```
=== 基線:兩個帳號都還有權限 ===
  victim(pool-account-8…) 有權限時   client身分=pool-account-8…  → 下載成功, 46096306 bytes
  ok(pool-account-4…)   有權限時     client身分=pool-account-4…  → 下載成功, 46096306 bytes

=== owner(pool-account-1…) 把 pool-account-8… 移出共享名單(shared_users 9 → 8 列)===

=== 核心對照(同一個 process、同一個 artifact)===
  victim 的 storage_state    client身分=pool-account-8…  → 失敗: ArtifactNotReadyError
  ok 的 storage_state        client身分=pool-account-4…  → 下載成功, 46096306 bytes

=== 反向:process 全域 NOTEBOOKLM_AUTH_JSON 設成 victim 憑證,但用 ok 的 path 建 client ===
  ok path + victim env       client身分=pool-account-4…  → 下載成功, 46096306 bytes

=== 還原:pool-account-8… 回到 SharePermission.EDITOR ===
```

三條全部成立 → **下載的身分確實來自 client 自己的 storage_state 檔,不是 env。**
第三條特別關鍵:把全域 env 換成沒有權限的那份憑證,下載照樣成功 ——
**舊機制(env 當身分)在這裡會失敗**,所以這是新舊行為的判別實驗,不只是「沒壞」。

> 附帶發現(可讀性缺陷,非安全問題):**沒有權限的帳號下載時,錯誤是
> `ArtifactNotReadyError: Audio artifact … is not ready`,不是權限錯誤。**
> 看不到 artifact 被 SDK 解讀成「還沒生好」。呼叫端(含 LLM)很可能因此判斷成
> 「再等一下就好」而無限重試,真正的原因(權限)完全沒出現在訊息裡。
> 上游 SDK 的行為,但 MCP 的 `artifact_download_audio` / resume 路徑會照樣轉發。

### 續跑入口 1/3:`podcast_episode_resume` — PASS

承 Phase 5 #2 那個被 `wait_timeout=60` 打斷的 attempt,照錯誤訊息貼上就跑:

```json
{"episode": 1, "title": "尾端延遲", "label": "EP01 尾端延遲",
 "task_id": "8f29c794-4438-40a8-b807-d4ff6f8a0625",
 "artifact_id": "8f29c794-4438-40a8-b807-d4ff6f8a0625",
 "mp3_path": ".../output/ep-retry/ep01.mp3",
 "published_at": "Sun, 09 Aug 2026 22:05:48 +0800",
 "attempt_id": "5ed76cb5-de9b-4563-ba6c-22dfb49a243a",
 "feedback_source_id": "63e10fc7-a712-42a4-a824-834fb734e110"}
```

| 要對帳的事實 | 實測 |
|---|---|
| 接回**同一個** artifact | `8f29c794…` = 中斷時那一顆 ✅ |
| **不新建 attempt** | `attempts` 筆數 = **1**,`attempt_id` 仍是 `5ed76cb5…` ✅ |
| finalize 正常走完 | `download.bytes = 40650761`(磁碟同值)、`feedback_source_rename = {status: completed, source_name: "EP01 尾端延遲", verified_at: 14:05:48}` ✅ |
| 沒有重新生成 | 遠端只有一顆 audio artifact,`published_at` 是 resume 完成時間 ✅ |

### Phase 5 #5 `podcast_series` supersede — PASS(用真的遠端刪除,不是注入狀態)

製造 `removed`:series 把 EP03 dispatch 出去(attempt `85100419`,artifact `8bef33f8`,slot 5)、
還在 `pending` 時,用 owner 憑證走 SDK `client.artifacts.delete()` **真的把遠端 artifact 刪掉**
(`刪除前 artifact 數: 3 → 刪除後: 2`)。

第一次觀察:in-flight 的那個 series 呼叫安全停止,**不自作主張新建 attempt**:

```json
{"episodes": [], "complete": false, "stopped_at_episode": 3,
 "attempt_id": "85100419-2f71-4f90-a868-85100d1c243b",
 "observed_state": "removed", "safe_next_action": "podcast_series",
 "attempt_count": 1, "superseded_attempt_count": 0}
```

第二次明確重呼 series → 建立取代 attempt 並連回舊的:

```
85100419  accepted  pool-account-8@…  | remote removed   8bef33f8 | supersedes -
6a9fb89f  accepted  pool-account-8@…  | remote completed 1d395f20 | supersedes 85100419   ← 新
```

最終 `EP3 attempts=2`、`output_attempt_id=6a9fb89f`、`ep03.mp3` 落地、
**舊 attempt 的 `remote.status=removed` 與失敗 artifact id 完整保留、沒有被覆寫**。✅

### 續跑入口 2/3:`podcast_episode_reconcile` — 三種結果全部走到 — PASS

`acceptance_unknown` 是用 **MCP 協定層的 `notifications/cancelled`** 逼出來的
(skill 明講的真實失敗模式「長 MCP request 可能被 client cancellation 終止」)。
自寫的 `cancel_probe.py` 起一個獨立 server,送出 `podcast_episode` 之後 N 秒送
cancel notification,再正常關閉。取消一律得到 `{"error": {"code": 0, "message": "Request cancelled"}}`,
server `exit 0`,憑證目錄清乾淨。

| 取消時機 | attempt 落在 | reconcile 結果 |
|---|---|---|
| 2.0s(RPC 還沒送) | `dispatch=prepared` / `remote=unknown` | **fail-loud**:`attempt dispatched_at is required for reconciliation` |
| 3.5s(dispatch 視窗內)EP10 | `dispatch=acceptance_unknown` | `{"observed_state":"acceptance_unknown","candidate_artifact_ids":[],"safe_next_action":"podcast_episode_reconcile"}` ← **零候選** |
| 3.5s EP12 + 事後在視窗內造 2 顆孤兒 artifact | `acceptance_unknown` | `{"observed_state":"reconciliation_ambiguous","candidate_artifact_ids":["8d40b987…","f047d3f0…"],"safe_next_action":"podcast_attempt_adopt"}` ← **多候選** |
| 3.5s EP11(EP12 adopt 掉一顆之後) | `acceptance_unknown` | `{"observed_state":"accepted","artifact_id":"8d40b987…","safe_next_action":"podcast_episode_resume"}` ← **唯一候選補綁** |

兩個額外驗到的不變式:

- **視窗判準有效**:更早的那顆 audio artifact(`653f219e`,14:17:19,早於 EP10 的
  `dispatched_at=14:20:57`)**沒有**被列進候選。
- **artifact claim 唯一性有效**:EP12 adopt 掉 `f047d3f0` 之後,EP11 的 reconcile
  只看到剩下那一顆 → 從「多候選」變成「唯一候選」。`_claimed_artifact_ids` 真的在擋。

### 續跑入口 3/3:`podcast_attempt_adopt` — PASS

```json
{"episode_n": 12, "attempt_id": "dcefabb5…", "artifact_id": "f047d3f0…",
 "observed_state": "accepted", "safe_next_action": "podcast_episode_resume"}
```

明確指名 artifact 後正確綁定,並把下一步指向 resume。

### ⭐ 三支續跑工具對「剛被 retract 的 attempt」都乾脆拒絕(tombstone default-deny)— PASS

對已 retract 的 `6c761f0e`(Phase 9-2 那顆)分別呼叫,**三支給的是同一句可讀訊息**,
不是內部錯誤:

```
podcast_episode_reconcile → attempt '6c761f0e…' was retracted (episode 2);
                            it is history — work on the replacement attempt instead
podcast_attempt_adopt     → (同上)
podcast_episode_resume    → (同上)
```

---

## Phase 9-2 — `abandon_in_flight` 的新出路 — PASS(含反向)

notebook `5c4188d7`、manifest `output/ep-retry/`、episode 2、標題 `對沖請求`。
第一次刻意送一個壞 brief(「請完全用英文,只講烹飪食譜」),`wait_timeout=45` 讓它在
finalize 之前就回來 → attempt `6c761f0e` = `dispatch accepted` / artifact `da64eaac` 還在飛。

| 步驟 | 呼叫 | 結果 |
|---|---|---|
| 反向 | `podcast_attempt_retract(...)` **不帶旗標** | **照舊拒絕**:`attempt '6c761f0e…' is not episode 2's durable output; only a promoted output attempt can be retracted` ✅ |
| 正向 | 同上 + `abandon_in_flight=True` | 成功,不 raise(見下) |

```json
{"episode": 2, "attempt_id": "6c761f0e…",
 "retracted_at": "2026-08-09T14:13:57.947892+00:00",
 "reason": "brief 失真:誤寫成英文烹飪食譜,與這一集主題完全無關",
 "retracted_output": {}, "stale_artifact_id": "da64eaac…",
 "stale_source_id": null, "stale_source_ids": [], "retracted_mp3_path": null,
 "observed_state": "retracted", "safe_next_action": "podcast_series"}
```

`stale_source_ids` 是空的 —— 合理:finalize 從未跑到回錄上傳,沒有要清的 source。

用修正過的 brief 重生同一集之後對帳:

| 事實 | 實測 |
|---|---|
| 被作廢的 attempt 留著 `retraction` 與理由 | `attempts[0].retraction` 完整(含 `reason` 原文);`episode.retracted_attempt_ids = ['6c761f0e…']` ✅ |
| 重生合法產生新 attempt | `f765099d…`,`attempts` 筆數 2 ✅ |
| **標題不變** | `title='對沖請求'`、`label='EP02 對沖請求'` ✅ |
| 取代版 mp3 落在 `attempts/<attempt_id>/` | `output/ep-retry/attempts/f765099d-2a13-4005-836d-cede066b8a76/ep02.mp3` ✅ |
| `pending_source_cleanup` | `None`(沒有未完成的清理義務) ✅ |

> 對照:Phase 5 #5 的 **supersede** 取代版走的是普通路徑(`output/access-denied/ep03.mp3`),
> 不進 `attempts/`。與文件一致 —— `attempts/<id>/` 那條紀律是**retract 專屬**
> (「不覆寫拒收版」),supersede 的舊 attempt 從來沒下載過東西,沒有要保護的檔案。

---

## Phase 8 — 附件 / 研究 / 發布的回歸 + 四支救援工具

### 附件三支

| 工具 | 實測事實 | 判定 |
|---|---|---|
| `generate_slides` | `ep01-slides.pdf` = **12 958 926 bytes / PDF 1.4 / 11 頁**;manifest 回寫 `slides_pdf_path` | PASS |
| `artifact_download_slides` | 拿既有 `439ae359…` 重下載,回同一路徑並回寫 manifest(不重生) | PASS |
| `generate_report` | `ep01-report.md` = **5 609 bytes**,內容確實只講佇列/尾端延遲(利用率 50%→90%→95% 的雙曲線、Little's Law、批次權衡);manifest 回寫 `report_md_path` + `report_format="study_guide"` | PASS |
| `artifact_download_report` | 拿既有 `32ba8770…` 重下載,回同一路徑 + `report_format` | PASS |
| `episode_set_description` | 回 `{"stripped": true}`,manifest 的 `description` 有值、非空、不等於標題;引用標記已清 | PASS |

`generate_report` 的 fail-loud 也順手驗到:對**不存在該集**的 manifest 呼叫 →
`episode 1 not found in manifest …/output/phase8-scratch/series_manifest.json`(不打 RPC)。

### research 三支 — PASS

```
research_start(mode="fast") → {"task_id": "ad863d32-…", "report_id": null, "mode": "fast", "source": "web"}
research_wait               → status=completed, 10 個 candidates, cited_url_count=0,
                              report_chars=0, report_importable=false(fast 模式沒有報告,符合文件)
research_import(urls=[2 個]) → imported 2 筆(The Tail at Scale / Fanouts and Percentiles),
                              requested=2;source_list 從 3 筆變 5 筆 —— **只匯入指名的那兩個**
research_import(不在候選清單的 URL) → raise:
  這些 URL 不在 task ad863d32-… 的候選清單裡:['https://example.com/not-a-candidate-zz9f3a']
  (候選共 10 個 identity;請用 research_wait 回的 url 原樣傳)
```

`task_id` 可續(`research_wait` 對已完成的 task 立刻回,不重等)。

### 封面 CLI(`notebooklm-cover`)— PASS

```
--show    → output/covers/show.jpg   {'width': 3000, 'height': 3000, 'format': 'JPEG'}  526 709 bytes
--manifest→ output/covers/EP01.jpg   3000×3000 JPEG  424 864 bytes + "Manifest updated: …"
            (access-denied 那份一次批出 EP01/EP02 兩張)
```

manifest 回寫 `cover_path` ✅(Apple 3000×3000 合規尺寸,CLI 自帶驗證器)。

### `publish_series` — 只做 preflight 負向測試(使用者明確選擇不上傳)

對 `output/access-denied/series_manifest.json` 逐步逼出**三層 fail-closed 閘門**,
每一層都在**第一個上傳之前**就 raise:

```
1) episode 1: cover_path is required (每集必做,不再 fallback 節目封面)
2) (補上 cover 後) episode 1: description is required (真 show notes,不可空白)
3) (補上 description 後,require_slides=True)
   episode 1: slides_pdf_path 未回寫(簡報可能還在生成中)。等 generate_slides/
   generate_report 回寫後再發布;使用者明講整季不做這項才傳 require_slides=False
```

**證明真的沒有上傳**(read-only GET 到 feed host):

```
$ curl -sI https://podcast.example.com/feeds/FEED_TOKEN/feed.xml
feed.xml   HTTP 404
index.html HTTP 404
```

→ preflight 擋在第一個 PUT 之前,NAS 上沒有任何 blob 產生。**PASS(負向)**。

**未測(使用者明確選擇)**:成功路徑的 feed.xml / index.html 內容、enclosure URL 的
內容雜湊穩定性、滾動加集。→ 見「刻意不測」一節。

### 四支救援 / 遠端 mutation 工具

| 工具 | 實測 | 判定 |
|---|---|---|
| `generate_audio` | ① 成功路徑:回 `{"task_id":"653f219e…","artifact_id":"653f219e…"}`(無 manifest/attempt,符合薄包設計)② **配額路徑**:`Error executing tool generate_audio: API rate limit or quota exceeded. (Upstream: Resource exhausted.)` —— **直接 raise,沒有 failover、沒有換帳號**,與文件「刻意不接 failover」一致 | PASS(含負向) |
| `artifact_wait` | 對已完成的 `4d31f4d2…` 立刻回 `{"task_id":…,"artifact_id":…}` | PASS |
| `artifact_download_audio` | 下到 `output/rescue/ep01-rescued.mp3`,**sha256 與 Phase 4 manifest 記錄的 `8158b9bd…` 逐位元相同**(46 096 306 bytes) | PASS |
| `artifact_rename` | report artifact `32ba8770…` → `EP01 佇列延遲 講義`,回傳新標題;`artifact_list` 確認生效 | PASS |
| `artifact_retry_failed` | 沒有現成的 failed artifact,改跑負向:對 completed 的 audio artifact 呼叫 → `artifact 4d31f4d2… 不是 failed 狀態(kind='audio' status='completed');retry 只用於失敗的 artifact,要重生請用對應的 generate_* 工具` | PASS(僅負向,見「刻意不測」) |
| `artifact_revise_slide` | 見下 —— **FAIL(行為與文件不符)** | **FAIL** |

### ⛔ FAIL:`artifact_revise_slide` 不是「就地改版」,它在遠端**新增了一顆 artifact**

呼叫(`slide_index=0`,改第一頁標題):

```
輸入 artifact_id = 439ae359-e48c-46e9-8939-cae126d6e738
回傳 artifact_id = 2be9219d-30dc-42b8-95c6-9c89fe062c35     ← 不一樣!
```

`artifact_list(kind="slide_deck")` 之後:

```
2be9219d-…  "System Latency Realities (2)"   completed  created 14:17:33   ← 新增的
439ae359-…  "System Latency Realities"       completed  created 13:47:04   ← 原本那顆還在
```

本機 PDF 確實換新了(12 958 926 → 12 993 745 bytes,仍是 11 頁),`slides_pdf_path` 回寫正常。

**但文件兩處都明確承諾 artifact 不變:**

- MCP 自己的 docstring(v0.9.0 實裝碼,**不是過期快照**):
  「省配額用:一頁改一句話不必整份重生(那還會連帶改動其他頁)。**artifact 不變**、其餘頁面不動」
- skill `references/tool-reference.md:1143`:
  「這裡走 SDK 的就地改版,**artifact 不變**、其餘頁面不動」

實際行為是伺服器 **fork 出一份 `… (2)`**。已確認不是 skill 快照過期(實裝碼的 docstring
同樣這樣寫),所以判 **FAIL:契約與行為不符**。

**為什麼這件事有後果**(不只是文字錯):同一節目的 notebook 裡於是有兩顆標題幾乎一樣的
slide_deck,而 `artifact_download_slides` 的救援路徑靠人挑 artifact_id,文件自己也寫
「**不確定是哪一筆別猜** —— 重生比綁錯便宜」、「artifact ↔ episode 的 binding 仍未驗證
(manifest 沒存 `slides_artifact_id`)」。`artifact_revise_slide` 每呼叫一次就多製造一顆
難以分辨的兄弟,**正好把那條已知風險放大**。

**建議**:① 訂正兩處文件為「會產生新 artifact(伺服器行為),舊的不會被刪」;
② 順手把 `slides_artifact_id` 寫回 manifest —— 這支工具已經知道新 id 了,寫下去
救援路徑就不用猜。

### 併發副產物(額外的 6-c 佐證)

`generate_report`(21:46)在 EP01 音檔 `remote: pending` 期間跑完並回寫;
`artifact_revise_slide`(22:17)在 `ep-retry#EP02` 生成中跑完並回寫;
`episode_set_description`(22:01)、`notebooklm-cover --manifest`(22:01)也都在別的
生成在飛時寫同一批 manifest。**最終每一份 manifest 的欄位都完整、attempts 筆數都正確、
沒有任何一次覆寫遺失。**

---

## Phase 9-3 — skill 文件是否足以讓 agent 正確使用新契約 — PASS

逐條檢查 v0.9.0 的三個新契約有沒有寫在該出現的地方
(`.claude/skills/notebooklm/` 是上游 skill 的逐字快照):

| 契約 | 位置 | 內容是否夠用 |
|---|---|---|
| `abandon_in_flight` | `references/tool-reference.md:791`(params 範例)+ `:805` 起整段說明 | ✅ **「省不了配額」寫得很明確**:「它省不了配額(生成已經在燒,retract 是純本機、取消不了遠端),省的是整整一輪 finalize…以及那筆回錄 source 對後續各集 context 的污染」<br>✅ **「只放行 active 那一顆」也寫了**:「旗標只放行 `active_attempt_id` 那一顆,不是萬用的 manifest 改寫鍵」<br>✅ 還寫了為什麼必須顯式宣告(「manifest 推導不出來——那個 attempt 與『第一次 dispatch、還在飛』逐欄位相同」)、以及不給旗標時該狀態留給 reconcile/resume |
| VIEWER 警告 | `references/tool-reference.md:64-66`,就在 `notebook_share_with_pool` 那節 | ✅ 位置正確,且說明了舊行為錯在哪、症狀為何延遲爆發 |
| `notebook_access_denied` | `references/tool-reference.md:978` 起 | ✅ 有「不要原樣重呼 series」「游標刻意不為它輪替」「`attempt_count` 永遠是 1,看起來像在原地打轉」<br>⛔ **但少了 Phase 9-1 抓到的那一句**:沒說「作用中帳號看不到 notebook 時,`notebook_share_with_pool` 自己也會 permission denied」。文件把呼叫端指向一條走不通的路 → 見 Phase 9-1 的 FAIL |

MCP server 的 `_INSTRUCTIONS`(協定層 `instructions`,連沒裝 skill 的 client 也看得到)
也完整送達,內容與 skill 的骨架一致(工作流順序 + env 變數 + 幾條鐵律)。

---

## 追加確認:Phase 3-3 的「刪掉的 source 還讀得到」不是最終一致性延遲

首次觀察 21:31,再探 22:26(相隔 **55 分鐘**),結果完全相同:

```
source_list                  → 該 id 不在列表(3 筆 → 後來 5 筆,都沒有它)
source_fulltext(該 id)       → {"title":"probe-config.json.md","char_count":153,
                                "hits":{"ZZPROBE-JSON-9F3A":true}}
```

→ **確認**:`source_delete` 之後,該 source 已不屬於 notebook,但用 id 仍讀得到全文。
不是延遲。這是上游 NotebookLM / SDK 的行為,`source_delete` 的 docstring 只承諾
「呼叫後該 id 已不在筆記本」——嚴格說沒有違約,但「刪掉」在直覺上會被讀成「讀不到了」。
Phase 9-2 的 retract 流程要求把 stale 回錄 source `source_delete` 掉以避免污染後續 context,
**若 NotebookLM 的生成端也像 `source_fulltext` 這樣還讀得到,那條清理義務的效果是待驗的**。
判定:**INCONCLUSIVE(對生成端的影響未驗證)**,對 `source_list` 的契約則是 PASS。

---

## 覆蓋自檢:35 個工具全部被碰過

```bash
for t in $(… app.mcp.list_tools() …); do grep -q "$t" FINDINGS.md || echo "  ⚠️ 未記錄: $t"; done
# → 沒有任何輸出
```

35/35 都有實測紀錄。**唯一「只做負向、沒做成功路徑」的兩支,理由如下(不是靜默跳過):**

| 工具 | 做了什麼 | 沒做什麼 + 理由 |
|---|---|---|
| `publish_series` | 三層 preflight fail-closed 全部逼出來,並用 HTTP 404 證明沒有任何 blob 上傳 | **成功發布路徑未測** —— 使用者在本輪明確選擇「只跑 preflight 負向測試(不上傳)」,因為 uploader 不刪檔、上傳即永久。未驗:feed.xml/index.html 內容、enclosure URL 的內容雜湊穩定性、滾動加集、report 的 HTML 渲染允許清單 |
| `artifact_retry_failed` | 對 completed artifact 呼叫 → fail-loud 訊息正確 | **真正的 retry 成功路徑未測** —— 這一輪 NotebookLM 沒有自然產生任何 `failed` artifact(11 次生成全部成功或被配額同步拒絕),而**沒有辦法可靠地製造一顆 failed audio artifact**(刪除得到的是 `removed`,不是 `failed`)。標 **INCONCLUSIVE**,不是 PASS |

另外 `notebook_list` 之外**沒有 notebook 刪除工具**(35 支裡沒有 `notebook_delete`),
所以 CLAUDE.md §七 要求的收尾刪除只能走 SDK —— 這是既有設計,不是這一輪的缺口,
但值得記著:**驗收要清資料就一定得繞出 MCP。**

---

## 這一輪的 pool 配額帳(9 槽位,實測每帳號每天 3 次音檔生成)

| slot | 帳號 | 開工時 | 這一輪用掉 | 結束時 |
|---|---|---|---|---|
| 1 | `pool-account-1…` | 已耗盡 | 0(每次都秒拒) | 耗盡 |
| 2 | `pool-account-2…` | 已耗盡 | 0 | 耗盡 |
| 3 | `pool-account-3…` | 已耗盡 | 0 | 耗盡 |
| 4 | `pool-account-4…` | 有額度 | **3**(ep01-single#1 / access-denied#1 / ep-retry#1) | 耗盡 |
| 5 | `pool-account-8…` | 有額度 | **3**(access-denied#2 / #3 attempt1 / #3 attempt2) | 耗盡 |
| 6 | `pool-account-6…` | 有額度 | **3**(ep-retry#2 作廢版 / ep-retry#2 取代版 / standalone `generate_audio`) | 耗盡,**收工時游標停在這裡** |
| 7 | `pool-account-9…` | 有額度 | **2**(reconcile 用的兩顆孤兒 artifact) | 剩 1 |
| 8 | `pool-account-7…` | 付費兜底 | **0** | 未動用 |
| 9 | `pool-account-5…` | 付費兜底 | **0** | 未動用 |

**付費兜底的兩格完全沒被動到**(游標從未輪到 slot 8/9)—— 不會吃掉隔天 `prd` 的份。
收工時的作用中帳號 = slot 6(`pool-account-6…`),推導方式:對全員已分享的 notebook 跑
`notebook_share_with_pool`,回傳的 8 個帳號裡少掉的那一個就是作用中帳號。

---

## 收工紀錄

### 測試資料清除

MCP 沒有 notebook 刪除工具,走 SDK(`client.notebooks.delete`),只刪標題含 `ZZ-TEST v0.9.0` 的:

```
[已刪] 4c9bf138…  ZZ-TEST v0.9.0 Phase3 scratch
[已刪] 5c4188d7…  ZZ-TEST v0.9.0 episode-retry
[已刪] 3b8003aa…  ZZ-TEST v0.9.0 access-denied
[已刪] db96baf4…  ZZ-TEST v0.9.0 EP01 佇列延遲
[已刪] 93ec89c1…  ZZ-TEST v0.9.0 EP02 雜湊碰撞
刪除後剩 3 本(全部是**前幾輪**的殘留,不是這一輪建的,刻意不動):
   v0.8.1 驗收 D —— 刻意不分享(SDK 直建)
   v0.8.1 驗收 —— 重送 failover 與自動分享
   v0.8.0 驗收 —— 配額 failover
```

> 這 3 本要不要刪由使用者決定 —— 它們是 v0.8.0/v0.8.1 驗收的殘留,
> 標題不含 `ZZ-TEST v0.9.0`,不在本輪的清除範圍內。

本機產物留在 `output/`(整個目錄可刪):3 份 manifest + 6 個 mp3(約 250 MB)+
1 份 report + 1 份 slides PDF + 4 張封面 + 1 個救援下載。

### 憑證殘留 / server 孤兒(⚠️ 需要使用者收尾)

收工時 `/tmp` 只有**一個**憑證目錄,而它屬於**還活著的本 session server**:

```
drwx------ /tmp/notebooklm-mcp-auth-39et0a_9    ← pid 3074816(本 session 的 MCP server)
```

**不能現在刪**(會拔掉正在用的憑證),也**不能用 CLAUDE.md §三 那行 `fuser` 指令刪**
(見 Phase 2 的文件缺陷:`fuser` 看不到持有者,那行會誤刪 live server 的憑證)。
關掉這個 session 之後跑:

```bash
pgrep -f 'bin/nblm-mcp' >/dev/null || rm -rf /tmp/notebooklm-mcp-auth-*
```

**server 孤兒:這台機器上有 5 個,最久的活了 1 天 16 小時** —— 比 CLAUDE.md 記錄的
「11.5 小時」還久,而且不只 acceptance 工作區會留:

| pid | 已活 | cwd | config |
|---|---|---|---|
| 1785660 | **1d 16h 38m** | `audiskill/notebooklm-mcp` | `-c dev`,舊命令名 `notebooklm-mcp` |
| 1789271 | **1d 16h 28m** | `research/AWS` | `-c dev` |
| 2061517 | **1d 05h 46m** | `audiskill/notebooklm-mcp` | `-c dev` |
| 2064789 | **1d 05h 41m** | `audiskill/notebooklm-mcp` | `-c dev` |
| 2446476 | **12h 52m** | `nblm-acceptance-v0.7.1` | `-c stg` |
| 3074816 | 1h 09m | `nblm-acceptance-v0.9.0` | `-c stg` ← 本 session,**不要殺** |

**刻意沒有替使用者殺掉前 5 個**:那 4 個 `-c dev` 的 cwd 顯示它們屬於別的 Claude Code
session,有可能還開著;殺掉會把別人的 MCP 連線打斷。建議自行確認後執行:

```bash
pkill -f 'nblm-acceptance-v0.7.1'          # 或逐一 kill 上表前 5 個 pid
```

（它們沒有留下憑證目錄 —— 都是 v0.9.0 之前啟動的 process,還沒有落檔行為。）

---

## 要寫回程式碼 / 文件的東西

### ✅ 已做:`_has_sufficient_permission` 的 `⚠️ 未被證明` 標記已結案

`../notebooklm-mcp/notebooklm_mcp/tools_basic.py` 的 P1 段落已改寫成實測結論
(owner 那一列存在、`permission=OWNER`,並附上「pool rotate 到 slot 4 之後 owner 落進
peers 卻被正確跳過」的實跑反例)。**未 commit** —— 提交要另外授權。

### 待辦(依嚴重度)

| # | 對象 | 內容 |
|---|---|---|
| 1 ⛔ | `tools_podcast` 的 `notebook_access_denied` 訊息 + skill `tool-reference.md:978` | 補一句「作用中帳號看不到 notebook 時,`notebook_share_with_pool` 也會 permission denied —— 要用 owner 帳號分享(重啟 server 讓游標回 slot 1,或在網頁上手動分享)」。進階做法:`notebook_share_with_pool` 吃到 permission denied 時自動換槽位試一輪 |
| 2 ⛔ | `tools_artifacts.artifact_revise_slide` docstring + skill `tool-reference.md:1143` | 「artifact 不變」是錯的,實測會 fork 出 `… (2)`。順手把 `slides_artifact_id` 回寫 manifest(工具已經知道新 id) |
| 3 ⚠️ | 本工作區 `CLAUDE.md §三` | `fuser` 那行清理指令會誤刪 live server 的憑證,換成 `pgrep -f 'bin/nblm-mcp' >/dev/null \|\| rm -rf …` |
| 4 ⚠️ | 本工作區 `README.md §Phase 2-c`、`docs/acceptance-v0.9.0.md §1-b` | 空值 PSIDTS 的 repro 要改成**多槽位**版本,單槽位走不到 `_write_credential_file` 的預驗證(`app.py:239` 的 `len(creds) > 1`) |
| 5 ⚠️ | 本工作區 `README.md §Phase 3` | 「`chat_ask` 預設清引用標記」寫錯了,預設是**保留**(skill 文件是對的) |
| 6 ⚠️ | 本工作區 `README.md §Phase 6-c` 第 3 條 | 「B 的 `from_account` 不是 A 的帳號」在 A、B 同帳號的時序下不具鑑別力,應改寫成「`from_account` 是這次 dispatch 實際被拒的帳號」 |
| 7 ℹ️ | `app.py` 的 `FastMCP(...)` | 帶 `version=importlib.metadata.version("notebooklm-mcp")`,否則 handshake 回的是 mcp SDK 版本(`1.29.0`),呼叫端看不出自己接到哪一版 app |

### 建議收回離線測試(AGENTS.md 的紀律;寫之前先做突變驗證)

| 測什麼 | 為什麼寫得成離線測試 |
|---|---|
| `_has_sufficient_permission` 對「owner 在 shared_users 且 permission=OWNER」的 fake 必須帶真實形狀 | 前提已結案,把真實回應形狀固定進 fake(9 列、第一列 OWNER) |
| `notebook_share_with_pool` 在 `get_status` raise permission denied 時的行為 | 純 fake 就能測「錯誤訊息有沒有告訴呼叫端要換 owner」 |
| `artifact_revise_slide` 回傳的 artifact_id 與輸入不同時,manifest 應記 `slides_artifact_id` | fake 回一個不同 id 即可;現在沒有任何測試會紅 |
| `_write_credential_file` 的多槽位 vs 單槽位分岔(`len(creds) > 1`) | 直接斷言「單槽位不建立 temp dir、多槽位才建」,順便釘住 §1-b repro 的正確形狀 |
| reconcile 的三種 observed_state(零 / 唯一 / 多候選)+ 視窗判準 + claim 唯一性 | 這一輪四種都真的走到了,fake 完全表達得出來 |
| 三支續跑工具對 retracted attempt 的 tombstone default-deny | 純 manifest 狀態,無需 RPC |
