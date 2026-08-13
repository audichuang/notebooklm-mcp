# notebooklm-mcp v0.9.13-rc 真實環境驗收 — FINDINGS

- 待驗對象:`codex/mcp-reliability-hardening` @ `40ee6e7`(v0.9.12 之後 9 個 commit,版本號未 bump)
- 實裝位置:`./.tool/bin/nblm-mcp`(工作區專屬;全機 `~/.local/bin/nblm-mcp` 刻意留在已發版 v0.9.12)
- 帳號:`doppler -p notebooklm -c stg`(測試帳號 pool)
- 日期:2026-08-13

記錄原則:寫**可觀測的事實**(欄位值、權限、逐字訊息、回傳 JSON)。判斷不了的標 `inconclusive`。

---

## Phase 0 — 本機檢查(不打 RPC)

`bash local-checks.sh` → **通過 17 / 失敗 0**。

| 節 | 結果 |
|---|---|
| §0 版本與身分 | `notebooklm-mcp 0.9.12`(未 bump,預期)、`notebooklm-py 0.8.0`、`mcp 1.29.0`;`_series_will_redispatch` / `_series_handoff_caps` / `_series_owns_attempt` 三個符號都在 |
| §1 身分不放 process 全域 | `_sync_auth_env` 已移除、`snapshot()` 就位且無 suspend 點 |
| §2 dispatch 交還帳號與 client | `_dispatch_audio_with_failover` 吃 `account`/`client`、回三元組;無殘留「dispatch 後重讀全域」 |
| §3 🔴 落檔預驗證 = strict loader 語義 | 空值 PSIDTS 落檔前被擋;憑證檔 `0600` — **綠**(硬關卡通過) |
| §4 本版新契約 | `podcast_attempt_retract(abandon_in_flight=False)`、`notebook_access_denied` 停點都在 |
| §5 分享權限判準 | VIEWER 不算已分享 / OWNER 算 / email 大小寫不敏感 |
| §6 SDK 漂移絆線 | `add_user` 預設仍 VIEWER、`permission` 仍可位置傳、`SharedUser.permission` 在 |
| §7 工具面 | 35 個 MCP 工具 |
| §8 本版判別實驗 4 條 | 8-1 交棒且帶 `source_ids` / 8-2 frozen bundle 不被誤判 / 8-3 已完成不交棒 / 8-4 retract 回傳自足 — 全綠 |

### 自行補驗:§8 真的在分辨新舊(不是在證明函式存在)

README 宣稱這組在 v0.9.12 上會紅。拿全機那支(已發版 v0.9.12)實跑對照:

```
全機安裝版本: 0.9.12          ← 與待驗版**字串完全相同**
_series_will_redispatch: False
_series_handoff_caps  : False
_series_owns_attempt  : False
```

→ 版本號分不出新舊,符號分得出來。§0 的符號檢查是有效 identity gate。**確認**。

---

## Phase 1 — 協定層:三種 transport

| # | 事實 | 結果 |
|---|---|---|
| 1 | `--transport stdio` | ✅ exit 0。送 `initialize` + `tools/list`,**stdout 只有 2 行合法 JSON-RPC**,`tools=35` |
| 2 | `--transport streamable-http --host 127.0.0.1 --port 8484` | ✅ 起得來,`POST /mcp` 回 `HTTP 200` + 合法 initialize(SSE `event: message`) |
| 3 | `--host 0.0.0.0` 無旗標 | ✅ **被拒**,`exit=1` |
| 4 | `0.0.0.0 --allow-insecure-remote` | ✅ 起得來,`POST /mcp` 回 `HTTP 200`(逃生門還在) |
| 5 | stderr 有沒有漏到 stdout | ✅ 沒有。`IncompleteFieldDefinitionWarning` 與全部 SDK log 都在 stderr |

**3 的逐字訊息**(stdout 全空,以下只在 stderr):

```
HTTP/SSE transport 沒有認證;非 loopback host 必須明確傳入 --allow-insecure-remote
```

守門確實在 lifespan 之前 —— 被拒那次 `/tmp/notebooklm-mcp-auth-*` 目錄數 14 不變,**一份憑證都沒落檔**。

### 順帶對帳:憑證落檔的權限與清理(CLAUDE.md §三)

- 執行期實測:目錄 `0700`、`slot-N.json` `0600`。✅
- **stdio 正常結束(stdin EOF)**:14 → 14,**刪乾淨**。✅
- 憑證檔內容形狀:36 個 cookie,含 `SID`/`APISID`/`HSID`/`LSID`/`OSID`/`NID` —— 是真憑證。

### FINDING-1(低)streamable-http + SIGINT:清理跑完後目錄會被**重建**成 `0775`

判別實驗(同一段程式碼,只換 SIGINT 送給誰):

| SIGINT 對象 | 執行期 | 正常結束後 |
|---|---|---|
| 只給 python 子 process(`doppler` 不動) | 目錄 `0700` / 檔 `0600` | 目錄**殘留**,`0775`、**內容為空** |
| 整個 process group | 同上 | 乾淨,無殘留 |

機制(已定位到行):

1. uvicorn `capture_signals` 離開時 `signal.raise_signal` → asyncio `_on_sigint` 丟 `KeyboardInterrupt`,
   打斷 `AsyncExitStack` 的展開。stderr 實錄:`Task was destroyed but it is pending!
   task: <Task ... coro=<ClientLifecycle.close() running at notebooklm/_runtime/lifecycle.py:509>`。
2. `shutil.rmtree` 已經把憑證檔刪掉,但**遲到的 cookie 回寫**接著跑,
   `filelock/_util.py:72` 的 `Path(filename).parent.mkdir(parents=True, exist_ok=True)`
   把目錄重建 —— `mkdir` 走 umask(本機 `0002`)所以是 `0775`,不是 `mkdtemp` 的 `0700`。

**嚴重性低,不是憑證外洩**:`notebooklm/_atomic_io.py:258` 的 `os.fchmod(temp_file.fileno(), 0o600)`
保證憑證檔一律 `0600`,與目錄權限無關。落在 `0775` 目錄裡別人也讀不到內容。

但兩件事要修文件:

- `app.py` lifespan 註解說殘留的是「$TMPDIR 下一個 **0700** 目錄」—— 實際可能是 `0775`。
- CLAUDE.md §三 說「正常結束(lifespan 展開)會自己刪乾淨」。對 **stdio 成立**(實測 14→14),
  對 **streamable-http + Ctrl-C 不完全成立** —— 會留一個空目錄。

另有一次觀察到更壞的交錯(初次 t2 run):`rmtree` 刪到一半被打斷,目錄 `0700` 但
**留下 `slot-2.json` / `slot-3.json` 兩份真憑證**(各 36 cookie)。不可穩定重現,
但足以說明「Ctrl-C 停 HTTP server」不能當成保證清乾淨的路徑。

### 順帶記一個事實:streamable-http 的 lifespan 是 **per-session**,不是 process 啟動時

實測:啟動後不送 `initialize`,stderr 只有 uvicorn 的 startup log,**完全沒有 NotebookLM 的
HTTP 請求、也沒有任何憑證目錄**;送了 `initialize` 才建 pool、才落檔。

(這條是我第一版探針的 bug 逼出來的 —— 探針沒送 initialize,誤判成「沒落檔」。)
含意:遠端綁定守門是網路對端與「驅動帳號」之間**唯一**的東西,而憑證要等有人連上才落地。

---

## Phase 2 — 認證層:三種輸入要給出三種結果

| 情境 | 製造方式 | 結果 |
|---|---|---|
| cookie 有效 | 正常 | ✅ `auth_check` → `{"ok": true, "notebooks": 3}` |
| cookie 死了 | shell 層把每個 `NOTEBOOKLM_AUTH_JSON*` 的 cookie value 全改成 `DEAD-<name>`(**沒動 Doppler**) | ⚠️ 形狀與預期不同 —— 見下 |
| 網路抖動 | 死 proxy(啟動期)+ 真例外物件過分類器 | ✅ **不**被當成 cookie 死掉,原型別保留 |

### 2-b 的實際形狀:憑證全死時 **server 起不來**,`auth_check` 沒機會回

`exit=1`,爆在 lifespan 建 client 的地方,不是在 probe:

```
app.py:279 _lifespan → client.py:888 _build → tokens.py:193 AuthTokens.from_storage
  → _auth/refresh.py:750
ValueError: Authentication expired or invalid. Redirected to: https://accounts.google.com/<redacted>
Run 'notebooklm login' to re-authenticate.
```

- ✅ **不含** `scripts/login_notebooklm.py`。該腳本確認已從 repo 刪除
  (`scripts/` 現存:`backfill_published_at.py` `check_skill_sync.py` `setup-test-config.sh` `sync-auth.sh`)。
- ⚠️ 也**不含** `sync-auth.sh` 與 `uv run`。MCP 自己那份正確的指引在
  `auth_probe.py:17-23`(`uv run notebooklm login` + `bash scripts/sync-auth.sh`,內容正確),
  但**啟動期這條路走不到它** —— 呼叫端(Claude Code)只會看到「MCP server 啟動失敗」。
  非本輪引入的迴歸,但 README 預期的「秒退並指向 sync-auth.sh」在**重啟後**這個形狀下不成立。

### 2-c 網路抖動:兩條獨立證據都成立

**(1) 端到端(啟動期)**:proxy 指向 `127.0.0.1:1`,server `exit=1`,錯誤逐字是

```
httpcore.ConnectError: All connection attempts failed
httpx.ConnectError: All connection attempts failed
```

—— 網路錯誤**原型別拋出**,沒有被加工成重登指引。

**(2) 真例外物件過 `_is_probe_auth_error()`**(實際去連死 port / 不存在的 DNS / 真 timeout 取得的物件):

| 情境 | 型別 | `is_auth_error` | `_is_probe_auth_error` | 判定 |
|---|---|---|---|---|
| 連線被拒(真) | `httpx.ConnectError` | False | **False** | ✅ 保留原型別 |
| DNS 失敗(真) | `httpx.ConnectError` | False | **False** | ✅ |
| timeout(真) | `httpx.ConnectTimeout` | False | **False** | ✅ |
| `AuthError` | `AuthError` | True | **True** | ✅ 轉重登 |
| `RPCError`(非 auth) | `RPCError` | False | **False** | ✅ |

分類**不過寬**:三種真實網路故障都不會中斷長跑。

### 2-c 的執行期版本:**inconclusive**(方法失效,不是行為失敗)

原設計是自架 CONNECT proxy → server 從它啟動成功 → 殺掉 proxy → 再打 `auth_check`。
實跑:`auth_check #1` 與 `#2` **都**回 `ok:true`,server log 顯示兩次都真的送出
`batchexecute?rpcids=wXbhsf` 且都 `HTTP/1.1 200 OK`。

亦即殺 proxy 對 RPC 路徑無效 —— **啟動期的 httpx GET 走 proxy(死 proxy 會擋掉啟動,已證),
但 batchexecute POST 沒走**。判成 inconclusive,不判成通過。
(附帶查明:`_curl_cffi_transport.py` 完全沒有 proxy 處理,而 `curl_cffi` 在這個 tool venv
**根本沒安裝** —— 所以它不是解釋,真正的 RPC 出口還沒定位。)

要真的做這格需要 root(iptables / netns),本輪沒做。

### 追過但**結案為非缺陷**:`is_auth_error` 認不得的那兩顆,實際到不了 probe

實驗中發現兩顆「名字看起來是 auth、卻被判 False」的例外,追完整條鏈路後確認**碰不到 probe**:

| 例外 | `is_auth_error` | 為什麼到不了 `probe_auth` |
|---|---|---|
| SDK 死憑證的 `ValueError`(`_auth/refresh.py:750`) | **False** | 只在 `AuthTokens.from_storage` → **client build** 時拋。那是 lifespan,沒有 probe(Phase 2-b 實測 server 直接起不來) |
| `TransportAuthExpired`(`_transport_errors.py:68`) | **False** | **從不外逸**:`_rpc_executor.py:264-266` 接住後 `raise exc.original from exc.__cause__`,而 `original` 是 `_middleware/auth_refresh.py:272` 塞進去的**原始 `httpx.HTTPStatusError`** |

所以 probe 路徑上真正會拿到的是那顆 `HTTPStatusError`,實測分類正確:

```
HTTPStatusError(400)  _is_probe_auth_error=True     ← Google 用 400 表示 CSRF 過期
HTTPStatusError(401)  _is_probe_auth_error=True
HTTPStatusError(403)  _is_probe_auth_error=True
HTTPStatusError(429)  _is_probe_auth_error=False    ← 限流保留原型別
HTTPStatusError(500)  _is_probe_auth_error=False    ← server 錯誤保留原型別
```

**`1da5f15` 的分類在 probe 路徑上兩邊都對:該轉的轉、該留的留。**

殘留的一格(低,不在 probe 半徑內):`_chat/transport.py:108` 把 `TransportAuthExpired`
映成 `ChatError("… authentication expired and refresh did not recover")`,而 `ChatError`
不被任何 auth 分類器認得。`chat_ask` 不是長跑工具、也沒有 probe,所以影響僅止於
「cookie 死掉時 `chat_ask` 的錯誤訊息不會導向重登指引」。

---

## Phase 3 — 讀取面回歸:被波及的那 23 支(不燒配額)

pool 實測 **9 個槽位**,slot 1 = `pool-account-1@example.invalid`(owner),其餘 8 個為 failover 帳號。

本輪建立的 notebook(收尾要刪):

| notebook_id | title | 用途 |
|---|---|---|
| `a3673021-2814-4d52-97b4-b00f77f431ed` | ZZ-TEST v0.9.13-rc 排隊延遲 | 領域 A |
| `8f9c3f60-1e09-4e00-9b0e-929fcf63c133` | ZZ-TEST v0.9.13-rc 碰撞簽章 | 領域 B |
| `cf28419f-5347-441c-88f6-9e5d450ce6f0` | ZZ-TEST v0.9.13-rc 未分享 | share_with_pool 真實路徑 |
| `92786692-ef15-4b73-8231-6cc2aa8a1243` | ZZ-TEST v0.9.13-rc 未分享2 | 游標觀測 |

### 逐支結果

| 工具 | 結果 |
|---|---|
| `notebook_list` | ✅ 回 3 本(前幾輪 v0.8.0/v0.8.1 的殘留),不屬本輪清理範圍 |
| `notebook_create` | ✅ 回 `notebook_id` + `shared_with` 8 個帳號(自動分享) |
| `notebook_get` | ✅ `sources_count: 3`;`is_owner: false` —— 與工具 docstring 記載的已知行為一致(有共享者時恆 False) |
| `source_add_file` | ✅ `.md` 上傳,`char_count: 713` / `698` |
| `source_add_text` | ✅ 回 `source_id`(此路徑不回 char_count) |
| `source_add_url` | ✅ Wikipedia `Queueing theory`,`char_count: 73311`,無 paywall warning |
| `source_list` | ✅ 三筆 `ready: true`,`kind` 分別是 `markdown` / `pasted_text` / `web_page` |
| `source_fulltext` | ✅ 見下(CJK) |
| `chat_ask` | ✅ 見下(引用標記) |
| `artifact_list` | ✅ 空 notebook 回 `{"artifacts": []}` |
| `notebook_share_with_pool` | ✅ 見下(兩條路徑都跑) |

### `source_fulltext` 的 CJK:**沒有被插空格**

回傳逐字(節錄):

```
百分位不能平均。一個常見的錯誤是把每台機器的第 99 百分位延遲再平均起來,當成整個服務的第 99 百分位。
```

唯一的空格是我自己輸入就有的(`第 99 百分位`)。`char_count: 575`、`truncated: true`(`max_chars=300`)。

### `chat_ask` 的引用標記:預設保留、`strip_citations=True` 清乾淨

- **預設**(`strip_citations=False`):答案含 `[1]` `[2]`,以及多號形式 `[1, 3]`;
  `references` 三筆,`citation_number` 1/2/3 各自對得回 `source_id`。✅ 行為與 docstring 一致。
- **`strip_citations=True, include_references=False`**:答案中 **`[N]` 全數消失**、`references: []`。✅

#### FINDING-3(極低,外觀)`strip_citations` 會留下標記位置的空格

清掉標記後,原本標記前面的空格留著,於是標點前多一格:

```
…而是來自**等待被處理的時間** 。
…會以 \(1 / (1 - 利用率)\) 的形式放大 。
```

`strip_citations` 的用途就是產 show notes(公開文案),這個空格是使用者看得到的。
一行 regex 的事(把 `\s+(?=[。,、;:!?」』])` 一起收掉)。

### `notebook_share_with_pool`:兩條路徑

- **冪等路徑**(已自動分享的 notebook):`shared_with: []`、`already_shared` 8 個、
  `shared_by: pool-account-1@example.invalid`。✅ 重跑安全。
- **真實分享路徑**(用 SDK 直建、**沒分享給任何人**的 notebook):
  `shared_with` 8 個全新分享、`already_shared: []`、`shared_by: pool-account-1@example.invalid`。✅

  > 刻意**不去動**前一輪那本 `v0.8.1 驗收 D —— 刻意不分享(SDK 直建)` fixture
  > (試過一次,被 Claude Code 的權限分類器擋下;改成自己建一本一次性的,更乾淨)。

- **輪替游標有沒有被動到**:MCP 對外沒有這個觀測面,改用同 process 直接讀
  `runtime.active_account()`:

  ```
  呼叫前 active_account = pool-account-1@example.invalid
  shared_by             = pool-account-1@example.invalid
  新分享數               = 8    已分享數 = 0
  呼叫後 active_account = pool-account-1@example.invalid   ← ✅ 沒動
  ```

  符合 docstring 宣稱的「掃描只打唯讀 `get_status`,不動輪替游標」。

  ⚠️ 這次的作用中帳號**正好看得見**該 notebook,所以沒有觸發 `_resolve_share_executor`
  的「換一個帳號來執行」分支。那條(v0.9.0 Phase 9-1 的形狀)要先把游標推到看不見的
  帳號才測得到,本輪**未覆蓋**。

### 尚未觸及(移到後續 Phase)

`source_delete`(Phase 6-3 的清理義務會用到)、`source_fulltext` 的
`max_chars=0 + contains=[…]` 省 token 模式。

## Phase 8(部分)— skill × MCP 文件對帳

先做不燒配額的**文件比對**;「照著跑」的可執行斷言隨 Phase 5/6 一起做,結果補在下面。

**結論:README 懷疑的過期指引都不存在。這份快照在四條上與新程式碼一致。**

| # | 檢查 | 結果 |
|---|---|---|
| 1 | 認證失效怎麼辦 | ✅ `grep -rn login_notebooklm .` 在整個 skill 快照 **零命中**。`troubleshooting.md:55-56,73-74,81-82` 教的是 `uv run notebooklm login` + `bash scripts/sync-auth.sh`,與 `auth_probe.py:17-23` 的 `RELOGIN_HINT` **一致** |
| 2 | `not_accepted` 的指示 | ✅ **已經是新行為**。`troubleshooting.md:107` 逐字:「**由建立它的那支工具原樣重呼**:整季建的用 `podcast_series`,`podcast_episode` 建的(帶 `source_ids` 的重生)用**參數完全相同的** `podcast_episode`」 |
| 3 | `source_ids` 用法 | ✅ `SKILL.md:152-163` 給了可執行步驟(`source_list` 取真實 id → manifest 的 `feedback_source_id` 為正本 → 排除集號 > N → 傳進 `podcast_episode`)。**待 Phase 6-3 實跑驗證** |
| 4 | 命名鐵律 | ✅ `_episode_label()`(`tools_podcast.py:833`)= `f"EP{episode_n:02d} {title.strip()}"`,與 SKILL.md 的 `EP{n:02d} 標題` 逐字相符。實跑 manifest 也回寫 `"label": "EP01 尾端延遲"` |

補充對到的兩條(README 沒點名但同一族):

- `troubleshooting.md:110` 對 `auth_expired` 寫「**不可固定假設 `podcast_series`**」——
  與這一輪 `_series_handoff_caps()` 的交棒行為一致(Phase 6-4 要實跑驗)。
- `troubleshooting.md:120-122` 寫「帶 `source_ids` 的 attempt 只有 `podcast_episode` 能續 ⋯
  `podcast_series` 會 fail-loud(**訊息會指名該用 `podcast_episode`**)」——
  這正是 6-2 的預期形狀,文件與守門講的是同一件事。
- `troubleshooting.md:253-257` 寫兩種停在 retract 的狀態(`prepared`/`not_accepted` 與
  `failed`/`removed`)**都不需要旗標**,且 retract 之後會告訴你重生用哪支工具。與 6-3 一致。

### 待同步(不是 bug,是發版 checklist)

`SKILL.md` §Auth 的安裝指令 pin 在 `@v0.9.12`;發 v0.9.13 tag 後要更新。
(§Auth 寫 `-c prd` 是**正確的正式流程**,本工作區用 `-c stg` 覆蓋是 CLAUDE.md 的加法,不是 skill 的錯。)

### 環境前提:`MCP_TOOL_TIMEOUT` 實測**預設沒設**

`SKILL.md:109-112` 警告「開跑前先把 host 的工具逾時調長,否則 request 每次都會被砍掉」。
本 session 實測 `env | grep MCP_TOOL_TIMEOUT` 為空、`~/.claude/settings.json` 也沒有
→ **這條警告是必要的,不是多寫的**。

本輪的作法:生成類長呼叫改走自建的 stdio harness(同一支 `./.tool/bin/nblm-mcp`、
同一個 `doppler -c stg`,逾時自己控),短呼叫仍走 host 的 MCP tool。

## Phase 4 — 主流程(進行中)

工作區:`output/A/series_manifest.json`,notebook A(`a3673021…`)。

### EP01 — `podcast_episode` + **指名 source_ids**

呼叫回傳:

```json
{"episode": 1, "title": "尾端延遲", "label": "EP01 尾端延遲",
 "task_id": "34198b69-b5eb-45fe-8c1e-8866b1d22f69",
 "artifact_id": "34198b69-b5eb-45fe-8c1e-8866b1d22f69",
 "mp3_path": ".../output/A/ep01.mp3",
 "published_at": "Thu, 13 Aug 2026 07:15:56 +0800",
 "attempt_id": "d7e35dbe-18e1-44bd-94d7-03a6638f5530",
 "feedback_source_id": "c9a060cb-dd17-4fa1-9d7e-66b056263368"}
```

manifest 終態:

| 欄位 | 值 |
|---|---|
| `dispatch.status` | `accepted` |
| **`dispatch.account`** | **`pool-account-1@example.invalid`**(slot 1,本集沒發生 failover) |
| `remote.status` | `completed`(`status_origin: remote`,不是 sdk_heuristic) |
| `settings.source_ids` | 3 筆,與我傳進去的逐字相同 |
| `finalize` 四步 | `artifact_rename` / `download` / `feedback_source_upload` / `feedback_source_rename` **全部 completed** |
| `download.bytes` / `sha256` | `43848402` / `03f2fd9b95589852…` |
| `errors` | `[]` |

生成耗時約 11 分鐘(07:04 dispatch → 07:15 完成)。

### 命名鐵律:**完全同名**,兩邊都對上

| 位置 | 名稱 |
|---|---|
| Studio artifact(`artifact_list`) | `EP01 尾端延遲` |
| 回錄 source(`source_fulltext.title`) | `EP01 尾端延遲` |
| manifest `label` | `EP01 尾端延遲` |

與 `_episode_label()` = `f"EP{n:02d} {title.strip()}"` 一致。✅

### 探針詞:**先證存活,再看洩漏**(順序沒顛倒)

對 EP01 回錄的 ASR 逐字稿(`char_count: 13532`)做正反對照,
用 `source_fulltext(max_chars=0, contains=[…])` 的省 token 模式(這支路徑同時被覆蓋):

```json
"hits": {"排隊": true, "延遲": true, "尾端": true,
         "吞吐": true, "負載": true, "百分位": true,
         "簽章": false, "碰撞": false, "生日": false}
```

- 正向 **6/6 命中** → 探針有效(不是「全死所以負向自動成立」的那個陷阱)。
- 反向 **3/3 未命中** → 領域 B 的內容沒有洩進 EP01。✅

### 待補(EP02 生成中)

`podcast_series` 生 EP02 —— 刻意傳**完整 episodes 清單 + `start=1`**,
同時驗「已完成的 EP01(帶 `source_ids`)會被跳過,而不是被接手守門誤擋」
—— 這是 local-checks §8-3 的真實環境版。

### EP02 — `podcast_series`(完整 episodes 清單 + `start=1`)

#### 先撞到一條:已完成集的 brief 有 **sha256 守門**

第一次呼叫時我給 EP01 塞了佔位 brief(`"(已完成,不應重生)"`),回:

```
Error executing tool podcast_series: episode 1 brief differs from completed attempt
```

**行為正確**(manifest 存的是 `brief_sha256`,防的是「續製時偷改已完成集的輸入」)。
但這是一條**文件 × 行為的真實互動,值得補進 skill**:

- `SKILL.md:93-94` 只說「續製仍傳完整 `episodes`,`start=N` 只是下界,工具跳過已完成集」,
  沒說**已完成集的 brief 必須逐字相同**。
- 而 `SKILL.md:87-88` 同時建議「brief 用腳本從來源檔產生,不要手寫」——
  產生器只要不是逐位元確定性的(換模型、改模板、改換行),續製就會撞這道守門。
- `series_example.md:51-63` 的 resume 範例把 brief 寫成 `"EP01 ..."` 省略形式;
  那顯然是文件省略而非字面值,但照抄會直接撞牆。

改用**逐字相同**的 EP01 brief 之後通過。

#### ✅ 已完成的 EP01 **被正確跳過**,沒有被接手守門誤擋

EP01 是 `podcast_episode` + `source_ids` 建的、已完成。`podcast_series` 重新進來時
**沒有**把它判成「需要交棒」,直接跳過去生 EP02。
這是 local-checks §8-3(「已完成的一集不交棒」)的**真實環境版**,通過。

#### 中斷狀態(真實手段:背景任務被中止)

EP02 的音檔生完並下載完成,但**回錄 source 的 upload 停在 `acceptance_unknown`**:

| 步驟 | 狀態 |
|---|---|
| `dispatch` | `accepted`,`account: pool-account-1@example.invalid` |
| `remote` | `completed`,`artifact_id: 7be06c54-6aa8-47c7-9acb-eba9b91b3656` |
| `finalize.artifact_rename` | `completed` |
| `finalize.download` | `completed` |
| **`finalize.feedback_source_upload`** | **`acceptance_unknown`** |
| `finalize.feedback_source_rename` | `not_started` |
| `episode.output_attempt_id` | `None`(還沒 promote) |

**回傳自足性 ✅ ——這是本輪紅線,實測通過。** 錯誤訊息逐字給出可直接執行的完整呼叫,
`artifact_id` 就在裡面,不必去翻 manifest:

```
音檔已在雲端生成(artifact_id='7be06c54-6aa8-47c7-9acb-eba9b91b3656')但後續步驟失敗。
既有 attempt_id='6bd0ff52-3e94-47bf-8c7d-d82507eaaf62'。續完(不會重新生成)的完整呼叫:
podcast_episode_resume(notebook_id='a3673021-…', episode_n=2, title='負載控制',
  artifact_id='7be06c54-…', output_dir='…/output/A', manifest_path='…/series_manifest.json')
**只有下面這句仍指向 resume 時才執行上面那個呼叫**:回錄 source 已經送出但 source_id
還沒落盤(upload 停在 'acceptance_unknown'):原呼叫中斷的話用 podcast_episode_resume
接續,它會把那筆 source 的身分對回來。確定那次生成要作廢的話,artifact_list 查過雲端
之後帶 abandon_in_flight=true 呼叫 podcast_attempt_retract。
```

值得單獨指出的兩點:

1. 它**沒有只報欄位名**,而是把 `artifact_id` / `attempt_id` 逐字列出來 —— 符合
   `docs/gotchas-attempt.md` 的自足性紅線。
2. 它附了**條件句**(「只有下面這句仍指向 resume 時才執行」)並同時給出另一條出路
   (`abandon_in_flight=true` retract),不是盲目叫人重跑。

接著**只用這份回傳裡的值**執行 `podcast_episode_resume`(全程沒有讀 manifest 取值)。

#### `podcast_episode_resume` 續不下去:回錄 source 卡在 NotebookLM 端 ingest

照回傳執行 `podcast_episode_resume`(參數逐字取自上面那則錯誤訊息,**沒有讀 manifest**):

```
Error executing tool podcast_episode_resume: feedback source acceptance remains unknown;
wait and resume the same attempt
```

`source_list` 的 ground truth —— 那筆 source **在遠端,而且標題正確**:

| source_id | title | kind | ready |
|---|---|---|---|
| `c9a060cb-…` | `EP01 尾端延遲` | `media` | **true** ← 正常的長相 |
| `59b7bc6b-…` | `ep02.mp3` | **`unknown`** | **false** ← 卡住的那筆 |

**逾 13 小時仍是 `ready: false`**(upload dispatch 於 `07:44`,最後一次確認 `20:38`)。

根因定位到行:`audio_finalize.py:297 unresolved_upload_candidates()` 的五個條件是
baseline / claimed / **kind** / title / 時間窗,而

```python
or _kind_value(getattr(source, "kind", None)) != "media"      # audio_finalize.py:327
```

這筆孤兒 **4/5 條都符合**(不在 baseline、未被認領、`title == expected_title == "ep02.mp3"`、
建立時間落在 dispatch 窗內),只有 `kind` 卡在 `unknown` 過不了 →
零候選 → `acceptance_unknown` → 「wait and resume」。

`podcast_attempt_adopt(feedback_source_id=…)` 也走不通:`tools_podcast.py:3714` 的
`candidate_pending_rename` 要求 `upload.status == "reconciliation_ambiguous"` 且 id 在
`candidate_source_ids` 裡;都不成立時 `allowed_titles` 只剩 `{label}` = `EP02 負載控制`,
而遠端那筆還叫 `ep02.mp3` → 標題不符被拒。

**工具與 skill 在這格是自洽的、也給得出出路** —— `SKILL.md:129-130` 第 3 條就寫了
「`reconcile` 零候選時打轉偵測恆不成立,出路寫在 `next_step` 裡,通常是查過雲端後把
`abandon_in_flight` 傳成 true 再呼叫 `podcast_attempt_retract`」,實跑的 `next_step`
也逐字給了這條。**不是缺陷。**

`podcast_episode_reconcile` 的回傳(自足 ✅,帶 `attempt_id` + `artifact_id`):

```json
{"complete": false, "episode_n": 2, "attempt_id": "6bd0ff52-…",
 "observed_state": "accepted", "artifact_id": "7be06c54-…",
 "safe_next_action": "podcast_episode_resume",
 "next_step": "回錄 source 已經送出但 source_id 還沒落盤(upload 停在 'acceptance_unknown')…
              確定那次生成要作廢的話,artifact_list 查過雲端之後帶 abandon_in_flight=true
              呼叫 podcast_attempt_retract。"}
```

### FINDING-4(中)`kind: unknown` 的孤兒對**清理義務對帳**也隱形 —— 而那正是它要防的事

照 `next_step` 執行 `podcast_attempt_retract(abandon_in_flight=true)`,回傳:

```json
{"episode": 2, "attempt_id": "6bd0ff52-…",
 "stale_artifact_id": "7be06c54-…", "stale_source_id": null, "stale_source_ids": [],
 "retracted_mp3_path": ".../output/A/ep02.mp3",
 "abandon_in_flight": true, "source_cleanup_unresolved": true,
 "dispatch_status_at_retraction": "accepted", "remote_status_at_retraction": "completed",
 "authorization_basis": "abandon_in_flight", "observed_state": "retracted",
 "safe_next_action": null, "source_cleanup_obligations": [],
 "safe_next_attempt_id": null, "safe_next_artifact_id": null,
 "regeneration_source_ids": null,
 "next_step": "…清理義務已經記進 tombstone,不會消失:等候選窗關上後直接重呼 podcast_series,
              生成前的 gate 會自己去 notebook 對帳——撈到候選就把 id 排進這一集的
              pending_source_cleanup 擋下來要你 source_delete…確認零候選才放行。"}
```

正確的部分:

- `source_cleanup_unresolved: true` —— 義務有記下來。✅
- **`regeneration_source_ids: null`** —— EP02 是 `podcast_series` 建的、settings 沒有
  `source_ids`,所以正確地沒有東西要帶回(這一格的重生入口本來就是 series)。✅
- `authorization_basis: "abandon_in_flight"` 誠實標示授權依據。✅

**問題在下一步。** `next_step` 承諾「gate 會自己去 notebook 對帳,撈到候選就擋下來」,
而那個 gate 用的是**同一份** `unresolved_upload_candidates()`
(`audio_finalize.py:302-303` 明寫「這是唯一一份候選判準」,finalize 對帳與
`_assert_source_cleanup_done` 共用)。

於是同一個 `kind != "media"` 條件在這裡再擋一次:**孤兒對清理義務也隱形**
→ gate 判「零候選」→ **放行** → `ep02.mp3` 這筆 `ready:false` 的 media 永遠留在
notebook 裡,污染之後每一集的生成 context,而工具全程回報成功。

這正是整套 `source_cleanup_unresolved` 機制要防的那個結果。

#### 判別實驗結果:**gate 放行,義務被當成「零候選」結案,孤兒留在 notebook 裡**

retract 之後重呼同一組 `podcast_series`:

```json
{"episodes": [{"episode": 2, "label": "EP02 負載控制",
   "artifact_id": "4df05d4a-…", "attempt_id": "fb986f75-…",
   "mp3_path": ".../output/A/attempts/fb986f75-…/ep02.mp3",
   "feedback_source_id": "4682a588-…"}],
 "complete": true}
```

**沒有擋、沒有提到孤兒、`complete: true`。** 三項對照證據:

| 觀測點 | retract 當下 | `podcast_series` 重呼之後 |
|---|---|---|
| 工具回傳 `source_cleanup_unresolved` | **`true`** | — |
| manifest `attempts[6bd0ff52].retraction.source_cleanup_unresolved` | `true` | **`null`** ← 被結案 |
| `episode.pending_source_cleanup` | (未建立) | **key 不存在** |
| notebook 裡的 `59b7bc6b` (`ep02.mp3`, `kind:unknown`) | 在 | **還在** |

`source_list` 現況 —— 同一集有**兩筆**回錄,一筆是孤兒:

```
4682a588  EP02 負載控制   kind=media    ready=true    ← 新的正式回錄
59b7bc6b  ep02.mp3        kind=unknown  ready=false   ← 孤兒,沒有人記得它了
```

義務被清掉、孤兒留著。之後這本 notebook 的每一次 `podcast_series` 都會把它算進來源筆數,
內容也會進生成 context。

(順帶更正欄位名以利查對:tombstone 存在 `attempt.retraction`,
episode 層是 `retracted_attempt_ids: ["6bd0ff52-…"]`,不是 `tombstone` / `pending_source_cleanup`。)

**新 mp3 落在 `output/A/attempts/<attempt_id>/ep02.mp3`**(per-attempt 子目錄),
不覆蓋被 retract 那顆的 `output/A/ep02.mp3`。這個設計是對的。✅

**建議**(不擋發版):候選判準對 `kind` 放寬成「`media` 或**尚未分類**(`unknown` 且
`ready: false`)」,或至少在零候選但存在「title 相符、窗內、未認領、只差 kind」的 source 時
回一個**不同的** `observed_state`(例如 `upload_ingesting`)並把 id 露出來,
不要與「真的什麼都沒有」共用同一格。現況下這兩種情形對呼叫端**長得一模一樣**。

### EP02 重生後的完整產物 + 附帶工具

重呼 `podcast_series` 生出的正式 EP02(`attempt_id: fb986f75-…`、`artifact_id: 4df05d4a-…`、
`feedback_source_id: 4682a588-…`),接著逐支跑附帶產物工具:

| 工具 | 結果 |
|---|---|
| `generate_slides` | ✅ `ep02-slides.pdf`(24.8 MB),`artifact_id: fb725485-…` |
| `generate_report` | ✅ `ep02-report.md`(6.7 KB),`report_format: study_guide`,`artifact_id: d0638dac-…` |
| `episode_set_description` | ✅ 回寫成功,`stripped: true` |
| `artifact_download_slides` | ✅ 用既有 artifact_id 重新下載並回寫,不重生 |
| `artifact_download_report` | ✅ 同上 |
| `artifact_download_audio` | ✅ 另存 `ep01-redownload.mp3` |
| `artifact_wait` | ✅ 對已完成的 task_id 立即回 `{task_id, artifact_id}` |
| `artifact_rename` | ✅ 見下 |
| `artifact_revise_slide` | ✅ 見下 |
| `artifact_retry_failed` | ⏳ 需要一顆 **failed** artifact,見 Phase 6 |

#### `artifact_rename`:照 SKILL.md:139 標記被作廢的那顆

被 retract 的 attempt 其 artifact(`7be06c54-…`)標題仍是 `EP02 負載控制`,與新版**同名**
躺在 Studio 裡。SKILL.md:139-141 明講要標記(MCP 沒有 artifact 刪除工具)。照做:

```json
{"artifact_id": "7be06c54-…", "title": "✗作廢 EP02 負載控制 第1版"}
```

#### `artifact_revise_slide`:docstring 的**更正版**行為逐字成立

```json
{"episode": 2, "slides_pdf_path": ".../ep02-slides.pdf",
 "artifact_id": "cf104f59-efe3-4ece-9ddd-2630c3296cfb",
 "slide_index": 0,
 "superseded_artifact_id": "fb725485-15b4-4f75-9d99-6fa348e6c7e1"}
```

`artifact_list` 佐證 —— **不是就地修改**,遠端多一顆 `(2)`、舊的原封不動:

```
cf104f59  The Efficiency Paradox (2)   slide_deck  completed   ← 新的,manifest 指向它
fb725485  The Efficiency Paradox       slide_deck  completed   ← 舊的,還在
```

docstring 自己更正過的那段(「原本寫『artifact 不變』,是錯的」)**實測正確**。✅

順帶佐證 docstring 提到的既有風險:slide deck 與 report 的標題是 **NotebookLM 自己取的**
(`The Efficiency Paradox`、`排隊理論與尾端延遲:系統效能管理學習指南`),**不帶 `EP{NN}`** ——
所以 `artifact_list` 確實分不出這是哪一集的 deck,只有 manifest 知道。

#### EP02 探針詞

`source_fulltext(4682a588, max_chars=0, contains=[…])`,`char_count: 9932`:

```json
"hits": {"排隊": true, "延遲": true, "尾端": true, "吞吐": false,
         "負載": true, "百分位": true,
         "簽章": false, "碰撞": false, "生日": false}
```

正向 **5/6**(≥2,探針有效;`吞吐` 主持人沒講到),反向 **3/3 未命中**。✅

---

## Phase 6 ⭐ — 接手守門與復原路徑(本版核心)

### 狀態怎麼製造的(全部真實手段,沒有手改 manifest)

拿那筆卡在 `ready:false` 的孤兒 `59b7bc6b` 當**唯一**來源生一集 →
NotebookLM 對「唯一來源不可用」的請求直接拒絕。這給出一顆
`dispatch=not_accepted` + `remote=failed` + **指名來源**的 attempt,正是 6-2 要的形狀。

### 順帶完成 Phase 4 的帳號對帳:**失敗時 `dispatch.account` 記的是換過去的那個**

```
最終 dispatch.account = pool-account-5@example.invalid     ← 不是入口抓的 pool-account-1
dispatch.status       = not_accepted
remote.status         = failed | error = Audio generation is unavailable
```

`errors` 完整記下 9 段轉移(每段都有 `from_account` / `to_account` / `recorded_at`):

```
1. pool-account-1        → pool-account-2   13:15:22
2. pool-account-2→ pool-account-3          13:15:23
3. pool-account-3       → pool-account-4        13:15:24
4. pool-account-4     → pool-account-8            13:15:26
5. pool-account-8         → pool-account-6         13:15:27
6. pool-account-6      → pool-account-9           13:15:28
7. pool-account-9        → pool-account-7      13:15:29
8. pool-account-7   → pool-account-5         13:15:30
9. dispatch not_accepted                    13:15:30
```

✅ **記帳與送出同源**:manifest 記的是最後真正嘗試的那個帳號,不是入口固定的那個。
稽核鏈完整到可以逐段回溯。

> ⚠️ 依 CLAUDE.md §一 回報:輪替掃到最後一個槽位 `pool-account-5`。
> 但**沒有消耗任何配額** —— 9 個帳號都在 1 秒內回同一個
> `ArtifactFeatureUnavailableError`,是請求本身被拒(來源不可用),不是配額耗盡。
> 佐證:同一本 notebook 含同一筆壞來源、但**來源不只它一筆**的 `podcast_series`
> 在 12:42 生成成功。

### 6-2 內容錯置不會回來(守門不能過窄)—— ✅ **通過**

對上述狀態重呼 `podcast_series`(episodes 清單含 EP03,brief 逐字相同):

```
Error executing tool podcast_series: episode 3 attempt '9334e247-…' settings do not match
this podcast_series call. 參數完全相同就原樣重呼建立它的那支工具,沿用同一顆重送(不多燒配額);
要換 brief 或來源就先 podcast_attempt_retract(**不需要** abandon_in_flight),再用
podcast_episode 重生。**若只更換 brief,必須保留原 `source_ids`(逐字是
['59b7bc6b-63ef-4223-8071-194f1c477150'])**;若刻意更換來源,podcast_attempt_retract
(**不需要** abandon_in_flight)後帶新的 `source_ids` 明確重生 —— 兩種情況都不會靜默
改成讀整本筆記本。
```

README 要求的四點逐條對:

| 要求 | 結果 |
|---|---|
| **拒絕**(不是靜默 supersede) | ✅ `isError: True`,沒有建立第二顆 attempt |
| 訊息講得出 `podcast_attempt_retract` | ✅ |
| 訊息講得出 `podcast_episode` | ✅ |
| **逐字列出**原本那組 `source_ids` | ✅ `['59b7bc6b-63ef-4223-8071-194f1c477150']` |

額外加分:明講 `abandon_in_flight` **不需要**(與 `troubleshooting.md:253-257` 一致),
並直接點名被防止的失效模式(「不會靜默改成讀整本筆記本」)。

舊版在這裡會靜默 supersede、第二次 dispatch 不指名,並回 `complete=True`。

### 6-3 復原路徑照著做真的走得出去 ⭐⭐ —— ✅ **通過**

**全程只用公開回傳裡的值,沒有讀 manifest 取 id。**

第 1 步,照守門訊息跑 `podcast_attempt_retract`(未傳 `abandon_in_flight`):

```json
{"episode": 3, "attempt_id": "9334e247-…",
 "abandon_in_flight": false, "source_cleanup_unresolved": false,
 "dispatch_status_at_retraction": "not_accepted", "remote_status_at_retraction": "failed",
 "authorization_basis": "settled",
 "observed_state": "retracted",
 "safe_next_action": "podcast_episode",
 "source_cleanup_obligations": [],
 "regeneration_source_ids": ["59b7bc6b-63ef-4223-8071-194f1c477150"],
 "next_step": "用 podcast_episode 重生。**重生時必須帶回原本那組 `source_ids`(逐字是
              ['59b7bc6b-…'])** —— 這一集的生成輸入指名了來源,改用 podcast_series
              會靜默改成讀整本筆記本。"}
```

- `authorization_basis: "settled"` —— `not_accepted` 不需要旗標,實測我沒傳也過。✅
- `source_cleanup_obligations: []` —— 這顆沒上傳過回錄,確實沒有義務。✅
- ⭐ **`regeneration_source_ids` 有值** —— 這是本輪新增的欄位。少了它,呼叫端只能省略
  `source_ids`,`podcast_episode` 預設 `None` 就靜默讀整本筆記本。

第 2 步(`source_cleanup_obligations` 為空,無 `source_delete` 可跑)。

第 3 步,用**那個欄位的值**呼 `podcast_episode` 重生。對帳:

```
9334e247 (已 retract)  settings.source_ids = ['59b7bc6b-63ef-4223-8071-194f1c477150']
5ee75bba (重生)        settings.source_ids = ['59b7bc6b-63ef-4223-8071-194f1c477150']
🔴 6-3 判準:重生收到的 source_ids == 原本那組 ?  ✅ 相等
```

(重生同樣被拒是預期的 —— 那筆來源本身就是壞的,這是本測試設計的產物,不是缺陷。
本格驗的是 id 有沒有正確傳遞到重生那次的生成輸入。)

### 6-1 正向路徑沒被擋(守門不能過寬)—— ✅ **核心斷言通過**(supersede 分支未覆蓋)

用一本**沒有任何來源**的 notebook(`cf28419f`)讓 `podcast_series` 自己建的 attempt
被伺服器拒絕,得到一顆 **series-owned**(`source_ids: None`)的 `not_accepted`。

第 1 次呼叫 —— 回**結構化安全停點**,不是例外:

```json
{"episodes": [], "complete": false, "stopped_at_episode": 1,
 "attempt_id": "4b607e5a-…", "observed_state": "not_accepted",
 "safe_next_action": "podcast_series",
 "attempt_count": 1, "superseded_attempt_count": 0}
```

`safe_next_action` 是 **`podcast_series`** —— 與 6-2 那顆(指名來源 → 被拒 → 改指
`podcast_attempt_retract`/`podcast_episode`)形成乾淨對比。**守門在分辨歸屬,不是一律擋。**

第 2 次照 `safe_next_action` 重呼:**沒有被擋**,`attempt_count` 仍是 1、
`superseded_attempt_count` 仍是 0,manifest 裡也仍只有一顆 attempt
—— 原地重送同一顆,與 `troubleshooting.md:107` 對 `not_accepted` 的描述逐字相符。

**順帶現場示範了 `troubleshooting.md:115-118` 那條**:這顆同時是
`dispatch.status="not_accepted"`(→ 原樣重呼)與 `remote.status="failed"`(→ 表裡是 supersede),
兩者映到**相反**的動作。工具跟的是 `dispatch`,所以重送而非 supersede。✅

**未覆蓋的部分**:README 6-1 描述的「`attempts` 長成兩顆、第二顆 `supersedes_attempt_id`
指向第一顆」屬於 `dispatch=accepted` **之後** remote 回 `failed`/`removed` 的分支。
那需要 NotebookLM 先受理再失敗,本輪**無法用真實手段製造**(不接受手改 manifest)。
離線側由 `local-checks.sh §8-1` 涵蓋。標 **partial**。

### 6-4 認證失效的交棒 —— ❌ **未覆蓋**(前提做不出來)

設計是「在 6-2 的狀態下把 shell 憑證換成死的,重呼 `podcast_series`」。
但 Phase 2-b 已實測:**憑證全死時 server 在 lifespan 就起不來**,連 probe 都到不了,
更不會有 `observed_state="auth_expired"` 的回傳。

而 `_lifespan` 對 pool 的**每個**槽位都建 client,任一槽位死掉就整個啟動失敗,
所以也做不出「啟動時活著、probe 時死掉」。要真的做出來得讓共用測試帳號的 cookie
在 session 中途失效 —— 那會影響 pool 上其他正在跑的東西。

離線側由 `local-checks.sh §8-1`(交棒且帶 `source_ids`)與 §8-3(已完成不交棒)涵蓋;
`troubleshooting.md:110` 的文件面也對得上(「不可固定假設 `podcast_series`」)。
真實環境這一格標 **未覆蓋**,不標通過。

---

## Phase 7 — research 三支

跑在拋棄式 scratch notebook `6aa48a46-aed1-4f66-a6e3-5fc7b8d15eb8`
(`ZZ-TEST v0.9.13-rc research-scratch`),核可來源不進正片 notebook(ADR-0008)。

### `research_start`:兩種 mode 的 handle 形狀**確實不同**

```json
fast: {"task_id": "f5e7d3e6-…", "report_id": null,          "mode": "fast"}
deep: {"task_id": "363da716-…", "report_id": "363da716-…",  "mode": "deep"}
```

⭐ **`29fc8bd` 的核心**:deep 的對外 `task_id` **就是 `report_id`**(兩欄位相同值),
不是不可輪詢的 sessionId;fast 仍是 SDK 的 `task_id` 且 `report_id: null`。✅

### 可輪詢性:拿它**真的再 wait 一次**(不重新 start)

deep 的 `research_wait` 連續呼叫兩次,同一個 `task_id`:

| 次數 | 結果 |
|---|---|
| 第 1 次(`max_report_chars=0`) | `status: completed`、`report_chars: 44237`、`report_importable: true`、`cited_url_count: 51`、候選 64 筆 |
| 第 2 次(`max_report_chars=300`) | **同樣立即回 completed**,並回報告前 300 字。沒有重等、沒有重新生成 |

✅ 可重入,模擬 client timeout 後續跑走得通。`candidates` 每筆帶 `cited` 布林,
51/64 被引用 —— deep 的引用地圖是實的。

### `research_import`:負向會 raise 且**列出是哪一筆**

傳一個不在候選集裡的 URL(混在一個真候選裡):

```
Error executing tool research_import: 這些 URL 不在 task f5e7d3e6-… 的候選清單裡:
['https://example.com/not-a-candidate-at-all'](候選共 10 個 identity;
請用 research_wait 回的 url 原樣傳)
```

✅ **不是靜默少匯入**,而且點名了是哪一個 URL。

正向(兩個真候選):

```json
{"imported": [{"source_id": "21a8dba1-…", "title": "The Tail at Scale - Google Research"},
              {"source_id": "80908653-…", "title": "Request Hedging - gRPC"}],
 "requested": 2,
 "note": "回傳筆數可能少於實際匯入;用 source_list 對帳並確認 ready"}
```

### FINDING-5(中)research 的輪詢 handle **綁帳號**,pool 輪替後就輪詢不到

判別實驗(同一個 `task_id`、同一個 notebook,只換執行的 server):

| 執行者 | 作用中帳號 | 結果 |
|---|---|---|
| **做 `research_start` 的那個 server** | `pool-account-5@example.invalid`(前一輪 failover 把游標推到最後一槽) | ✅ 立即 `status: completed` + 10 筆候選 |
| 另起的 server(全新 pool,游標在 slot 1) | `pool-account-1@example.invalid` | ❌ 輪詢 **900 秒**後 `timed out (last status: no_research)` |

`no_research` = 那個帳號看不到這個 research task。**notebook 本身有分享給全 pool**,
所以不是 notebook 存取問題,是 research session 綁在發起它的帳號上。

為什麼這一輪值得記:`1cdb0b0` 把 client 固定在**單次呼叫**的入口,但
`research_start` 與 `research_wait` 是**兩次獨立呼叫**,各自在入口重新
`runtime.get_client()`。中間只要發生一次配額 failover(而那正是 pool 存在的理由),
游標就換人,`research_wait` 便輪詢不到 —— 然後 deep research 的
「斷線救援用這支,不要重新 `research_start`」那條指引**永遠走不通**,
使用者只能重跑,而 CHANGELOG 自己說「不建議可能重複消耗配額的 retry」。

**證據強度**:跨 process 那格是**直接觀測**到的。「同一個 server 內游標移動後也會如此」
是從程式路徑推得(兩支工具都在入口 `get_client()`),**未直接觸發**(要製造需要一次
真實配額 failover 剛好落在兩次呼叫之間)。標明區分。

**建議**:`research_start` 的回傳把發起帳號一起帶出來(或把 handle 做成
`account+task_id` 的複合值),讓 `research_wait` 能釘回同一個帳號;
至少 `no_research` 要能與「真的還沒開始」分辨開來,不要輪詢滿 timeout 才失敗。

---

## 覆蓋率自檢(收工 §0)

`for t in $(list_tools); do grep -q "$t" FINDINGS.md || echo 未記錄; done`
→ 35 支中 **32 支已記錄**,3 支處理如下:

| 工具 | 處理 |
|---|---|
| `generate_audio` | ✅ 已補測(見下) |
| `feed_info` | ✅ 已補測(見下) |
| `publish_series` | ⛔ **刻意不測 —— 需使用者授權**。它會把 blob **永久**推上 NAS(uploader 不刪檔),CLAUDE.md §四 明訂動它之前要先問 |

### `generate_audio`:確認它**沒有** failover(與 podcast 家族的關鍵差異)

零配額成本的判別實驗 —— 對**同一筆**壞來源 `59b7bc6b`,兩支工具的行為:

| 工具 | 行為 |
|---|---|
| `podcast_episode(source_ids=[壞來源])` | 掃過 **9 個帳號**、寫進 manifest 的 `errors`、回結構化 `not_accepted` + 續跑指引 |
| `generate_audio(source_ids=[壞來源])` | **裸 raise** `Audio generation is unavailable` —— 無 failover、無停點、無 attempt 記錄 |

✅ 與 docstring 宣稱的「這支沒有配額 failover…撞到配額就直接 raise」逐字相符。
SKILL.md §Auth 那段警告(救援路徑建在沒有 failover 的那一支上)是成立的。

### `feed_info`:純計算,不打 RPC、不寫 NAS

```json
{"show_id": "zz-test-v0913rc",
 "token": "o4hjjef4iy4nnrfizw7uvmit",
 "feed_url": "https://podcast.example.com/feeds/FEED_TOKEN/feed.xml",
 "show_page_url": "https://podcast.example.com/feeds/FEED_TOKEN/index.html"}
```

token 由 `PODCAST_TOKEN_SALT` 導出,而 stg 的 salt 與 prd 不同 —— 落在與正式節目
完全不同的 URL 空間(CLAUDE.md §四 的隔離前提,實測成立)。

---

## 總結

### 這一輪 9 個 commit 的驗收結論

| commit | 主題 | 結論 |
|---|---|---|
| `23e46c6` | 未認證遠端綁定守門 | ✅ 三種 transport 全通過,守門在 lifespan 之前,逐字訊息點名旗標 |
| `1cdb0b0` `a99f2d4` | 多步驟工具入口固定 client | ✅ 23 支讀取面回歸正常;帳號記帳與送出同源(9 段 failover 鏈可回溯) |
| `d41d2ff` `bbb5881` | 認證與 dispatch 身分對齊 | ✅ `dispatch.account` 記的是換過去的那個,不是入口的 |
| `1da5f15` | auth probe 用 SDK `is_auth_error` | ✅ 不過寬(三種真實網路錯誤保留原型別)、不過窄(400/401/403 都轉) |
| `1363157` | account label 失敗只警告 | ⚪ 未直接觸發(label 讀取全程正常) |
| `29fc8bd` | deep research 可輪詢 handle | ✅ `task_id == report_id`,可重入、不需重新 start |
| ⭐ `40ee6e7` | attempt 接手守門 + 交棒 + `regeneration_source_ids` | ✅ 6-1 核心 / 6-2 / 6-3 全通過;6-4 前提做不出來 |

### FINDING 一覽(**沒有任何一條擋發版**)

| # | 嚴重度 | 摘要 |
|---|---|---|
| 1 | 低 | streamable-http + Ctrl-C:清理後目錄被 `filelock` 用 umask 重建成 `0775`(空目錄;憑證檔一律 0600,非外洩)。文件說法要修 |
| 2 | — | (追查後**結案為非缺陷**)`is_auth_error` 認不得的兩顆例外實際到不了 probe |
| 3 | 極低 | `strip_citations` 清掉標記後留下空格(`…的時間 。`),而它的用途正是產公開 show notes |
| **4** | **中** | `kind: unknown` 的孤兒回錄對**清理義務對帳**隱形 → 義務被當成零候選結案、孤兒永留 notebook。**這正是該機制要防的事** |
| **5** | **中** | research 輪詢 handle **綁帳號**,pool 輪替後 `research_wait` 得到 `no_research` 並輪詢到 timeout;而 deep research 的官方救援指引正是「不要重新 start」 |

### 未覆蓋 / inconclusive 一覽(**刻意列出,不當成通過**)

| 項目 | 狀態 | 理由 |
|---|---|---|
| Phase 2-c 執行期網路抖動 | **inconclusive** | 自架 proxy 對 RPC 路徑無效(啟動期 httpx 走 proxy、batchexecute POST 沒走)。要真做需 root(iptables / netns) |
| Phase 6-1 的 supersede 分支 | **partial** | 「兩顆 attempt + `supersedes_attempt_id`」需要 `dispatch=accepted` **之後** remote 回 `failed`/`removed`。無法用真實手段製造(不接受手改 manifest)。離線側由 §8-1 涵蓋 |
| Phase 6-4 認證交棒 | **未覆蓋** | 憑證全死時 server 在 lifespan 就起不來(Phase 2-b 實測),連 probe 都到不了;而 pool 任一槽位死掉就整體啟動失敗,做不出「啟動時活著、probe 時死掉」 |
| `artifact_retry_failed` | **未覆蓋** | 需要一顆 **failed 的 artifact**。本輪所有失敗都停在 dispatch(`not_accepted`),伺服器沒建出 artifact,無對象可 retry |
| `notebook_share_with_pool` 的換帳號執行分支 | **未覆蓋** | 要先把游標推到「看不見該 notebook」的帳號(v0.9.0 Phase 9-1 形狀) |
| `publish_series` | **待授權** | 見上 |
| `1363157` account label 失敗 | **未觸發** | 全程 label 讀取正常,沒有機會觀察到降級行為 |

---

## 收工紀錄

### 測試資料清除(收工 §3)

5 本 `ZZ-TEST v0.9.13-rc` notebook **全數刪除**(MCP 沒有刪除工具,走 SDK;
用「白名單 id + 標題前綴」雙重閘門):

```
已刪 slot1  cf28419f  'ZZ-TEST v0.9.13-rc 未分享'
已刪 slot1  a3673021  'ZZ-TEST v0.9.13-rc 排隊延遲'
已刪 slot1  92786692  'ZZ-TEST v0.9.13-rc 未分享2'
已刪 slot1  8f9c3f60  'ZZ-TEST v0.9.13-rc 碰撞簽章'
已刪 slot9  6aa48a46  'ZZ-TEST v0.9.13-rc research-scratch'
── 共刪除 5/5
```

FINDING-4 的孤兒 `59b7bc6b`(`ep02.mp3`)隨 notebook A 一併消失。

> ⚠️ **清理時發現的一件事,值得寫進紀律**:`notebook_list` 只列**當下作用中帳號**
> 看得到的東西。清理前呼叫時游標已輪到最後一槽,列出來的 4 本裡有 3 本不是我建的,
> 而我建的有 2 本沒列出來。**掃全 pool 才數得齊**。
>
> 更要緊的是:最後那幾個槽位看得到**大量正式 notebook**(`SAP 題庫特訓`、
> `System Design Primer Day 07–30`、`7 天讀懂 JVM Day 1–5`、AWS 系列,以及一本
> 國泰的),實證了 CLAUDE.md「stg 最後兩個槽位與 prd 共用同一組帳號」。
> **清理腳本一定要用白名單,不能用「標題不像我的就刪」或「刪光光」。**

### 憑證與 process 殘留(收工 §3 / CLAUDE.md §三)

照 CLAUDE.md 的對照表做法(**沒有**用 `fuser`/`lsof`):

| 活著的 server | 起始時間 | 對應憑證目錄 |
|---|---|---|
| 2316096 / 2316217(本 session 的 `-c stg`) | 2026-08-13 20:36:35 | `431nm6yo`(ctime 20:36:47)→ **保留** |

其餘 14 個目錄無任何活 process 對應(ctime 從 08-10 到 08-13 06:31,含前幾輪
與今日稍早已結束的 3 個 `-c prd` server 留下的)→ **全部刪除**。

```
已刪除 14 個孤兒目錄
剩餘:drwx------ /tmp/notebooklm-mcp-auth-431nm6yo   ← 使用中
```

server 孤兒:**無**(只剩本 session 的那一個;今早那 3 個 `-c prd` 已自行結束)。

### `publish_series`:依使用者決定**刻意不測**

CLAUDE.md §四 要求動它之前先問。已詢問,使用者選擇不測 ——
NAS 上不留任何測試 blob。`feed_info` 已單獨覆蓋(純計算,不寫 NAS)。

### 尚未完成的收工項目(需要先有決定 / 先有 tag)

- **收工 §1(把發現收回離線測試)**:FINDING-4 與 FINDING-5 **還沒有修正**,
  而 §1 明訂「收之前先做突變驗證(把修正改回舊行為,確認測試真的會紅)」——
  沒有修正就沒有可突變的對象,現在寫只能寫成描述現況的 characterization test,
  那正好是 §1 要避免的東西。**等修法定案後再補。**
- **收工 §5(發 tag 後重跑 Phase 0 與 Phase 1)**:tag 尚未存在。
  發 `v0.9.13` 之後要用 `uv tool install "git+…@v0.9.13"` 重裝再跑一次
  (`uv tool install` 不讀 `uv.lock`,消費端每次自由解析相依;且 `nblm-mcp`
  與上游撞名,`uv tool list` 看不出來)。

---

## 收工 §5 補跑 —— 發 tag(v0.9.13)之後重跑 Phase 0 / Phase 1

驗的對象是**用 tag 裝的那份**(`UV_TOOL_DIR=$PWD/.tool uv tool install --python 3.12
--force "git+…@v0.9.13"`,commit `03ef59e`),不是本機工作樹 —— `uv tool install` 不讀
`uv.lock`,消費端每次自由解析相依。

### Phase 0:19 / 0

原本 17 項全過,版本 identity 那格改成 `0.9.13`(已 bump,版本字串這次分得出新舊,
但符號檢查保留 —— uv git cache 壞掉時版本號會騙人)。實裝相依:`notebooklm-py 0.8.0`、
`mcp 1.29.0`(與 `uv.lock` 一致)。`nblm-mcp --help` 印的是我們這支(撞名檢查過)。

新增兩條判別實驗,對照組是另裝一份 **v0.9.12**:

| 檢查 | v0.9.12 | v0.9.13 |
|---|---|---|
| 8-5 `kind=unknown`/`ready=false` 的孤兒算不算候選 | ❌ `卡在 ingest 的孤兒沒被撈到:[]` | ✅ PASS |
| 8-6 research handle 釘得回發起帳號 | ❌ `research_wait 少了 account` | ✅ PASS |

8-5 同時驗「沒放寬過頭」:已分類成 `web_page` 的、以及 baseline 內的 `media`,仍然不是候選。

### Phase 1:12 個事實,10 ✅ + 2 個「探針判準過嚴」

| # | 事實 | 結果 |
|---|---|---|
| 1 | stdio | ✅ exit 0、stdout **剛好 2 行**合法 JSON-RPC、`tools=35` |
| 2 | streamable-http 綁 127.0.0.1 | ✅ `POST /mcp` → 200 + SSE `event: message` + `serverInfo` |
| 3 | `--host 0.0.0.0` 無旗標 | ✅ 被拒 `exit=1`,stdout 全空,訊息逐字同上一輪 |
| 3b | 被拒那次的憑證落檔 | ✅ **7 → 7,一份都沒落**(守門確實在 lifespan 之前) |
| 4 | `0.0.0.0 --allow-insecure-remote` | ✅ 逃生門還在,`POST /mcp` → 200 |
| 5 | stderr 漏進 stdout | ⚠️ 見下 |

#### 更正上一輪的一句話:「全部 SDK log 都在 stderr」對 **stdio** 成立,對 HTTP 模式不成立

HTTP 模式的 stdout 有 uvicorn 的 **access log**:

```
INFO:     127.0.0.1:51754 - "GET /mcp HTTP/1.1" 406 Not Acceptable
INFO:     127.0.0.1:51758 - "POST /mcp HTTP/1.1" 200 OK
```

uvicorn 預設的 `LOGGING_CONFIG` 把 access handler 指向 stdout(error handler 才是 stderr)。
**不是缺陷**:HTTP 模式的 stdout 不是協定通道。而 stdio 模式沒有 uvicorn,實測 stdout
剛好 2 行。我那兩條 ❌ 是探針把「stdout 全空」當成通過條件,對 HTTP 模式訂錯了。

#### 新觀測(推翻 FINDING-1 的其中一句):**process group SIGINT 也不保證清乾淨**

FINDING-1 的表格記「SIGINT 給整個 process group → 乾淨,無殘留」。這次兩台 HTTP server
都用 group SIGINT 停(`setsid` 起、`kill -INT -<pgid>`),結果:

```
07:06:10 drwxrwxr-x  …-ck8rgxyp  [.slot-1.json.lock]
07:06:10 drwxrwxr-x  …-hg2a3b6p  [.slot-1.json.lock]
07:06:20 drwxrwxr-x  …-69eb46dx  [.slot-1.json.lock]
07:06:20 drwx------  …-heh9otai  [.slot-1.json.lock, slot-2.json]   ← 一份真憑證
```

三個是 FINDING-1 描述的「rmtree 之後被 filelock 用 umask 重建」的 0775 空目錄,
第四個是那個更壞的交錯(0700、留下真憑證)。所以上一輪那格是**樣本數 1 的錯覺**,
不是乾淨路徑。**stdio 正常結束(stdin EOF)才是唯一實測可靠的那條。**

另外注意:兩台 server 留下**四個**目錄 —— 因為 streamable-http 的 lifespan 是
**per-session**(上一輪已記),一個 process 會建出多個憑證目錄,清理時別只找一個。
`app.py` 的註解已照這個觀測更正(原本寫「要乾淨就送 group SIGINT」,是錯的)。

### 收工清理

刪掉 7 個孤兒憑證目錄(其中 3 個各含 5 份真憑證,是本機 MCP server 重啟累積的;
4 個是這次 Phase 1 留下的)。**保留 2 個正在跑的 server 在用的**(用 process 啟動時間
對出來:`20:36:35` 的 workspace `-c stg`、`06:22:59` 的全機 `-c prd`)。清理後
`auth_check` 仍 `ok`(300 notebooks),證明沒誤刪活的那份。
