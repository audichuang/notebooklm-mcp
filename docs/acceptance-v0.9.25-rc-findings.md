# notebooklm-mcp v0.9.25-rc 真實環境驗收 — FINDINGS

開始時間:2026-09-03 12:00 (+08:00)
工作區:`/home/user/research/audiskill/nblm-acceptance-v0.9.25-rc`
帳號 pool:`doppler -p notebooklm -c stg`(測試帳號,CLAUDE.md §一)

---

## Phase 0 — 本機檢查(不打 RPC)

### 0.1 安裝前的機器狀態(baseline,收工還原的對照)

```
$ command -v nblm-mcp
/home/user/.local/bin/nblm-mcp
$ head -1 $(command -v nblm-mcp)
#!/home/user/.local/share/uv/tools/notebooklm-mcp/bin/python3
$ ~/.local/share/uv/tools/notebooklm-mcp/bin/python -c "...m.version(...)"
0.9.24 0.8.1 1.29.1        # notebooklm-mcp / notebooklm-py / mcp
```

- 待驗的 rc 內容 = `../notebooklm-mcp` HEAD **`a440e1c`**(工作樹 clean)。
  版本字串證明不了任何事(CLAUDE.md §〇),**build 的身分是這個 commit hash**。
- `mcp` 實裝 1.29.1,`uv.lock` 是 1.29.0 —— 已知落差,local-checks §0 只當 ℹ️ 印出。

### 0.2 安裝前的 process / 憑證落檔盤點(2026-09-03 12:00:37 +08:00)

| pid | config | 啟動時間 | 對應憑證目錄 | slot 數 |
|---|---|---|---|---|
| 479717 (parent 479640) | **`-c prd`(正式 pool)** | 06:20:45 | `/tmp/notebooklm-mcp-auth-ph3kbrjq` (ctime 06:20:52) | 5 |
| 598963 (parent 598881) | `-c stg` = **本 session 的 MCP server** | 11:59:36 | `/tmp/notebooklm-mcp-auth-jauru40n` (ctime 11:59:47) | 9 |

**孤兒目錄:0 個** —— 兩個目錄的 ctime 都對得上一個活著的 server 的 lstart
(CLAUDE.md §三 的對照法)。開工時環境是乾淨的。

實測權限(0.9.24 build,rc 安裝後要再驗一次):

```
drwx------  /tmp/notebooklm-mcp-auth-jauru40n          → 0700 ✅
-rw-------  .../slot-1.json … slot-9.json              → 0600 ✅
```

### 0.3 ⚠️ 本 session 的 MCP server 跑的是**舊 build**

session 啟動時(11:59:36)的 `nblm-mcp` 是 0.9.24/0.8.1。證據不是版本號,是**工具面**:
Claude Code 列出的 `mcp__notebooklm__*` deferred tools **沒有 `source_search`**
(有 `source_add_file` / `source_add_text` / `source_add_url` / `source_delete` /
`source_fulltext` / `source_list`,就是缺 search)。

→ Phase 0 安裝**只換磁碟上的 venv,換不掉已經跑起來的 pid 598963**。
   **在 MCP server 重連之前,一支 `mcp__notebooklm__*` 都不能呼叫** ——
   那個 process 是 0.9.24 的程式碼坐在被換掉的 venv 上,任何延遲 import 都會拉進
   rc 的 module,回傳值無法解讀。

### 0.4 安裝 rc build

```
$ uv tool install --python 3.12 --force /home/user/research/audiskill/notebooklm-mcp
 - notebooklm-mcp==0.9.24 (from git+https://github.com/audichuang/notebooklm-mcp.git@61789f5080ce29071d9ad68df3c0d6ed13df4509)
 + notebooklm-mcp==0.9.24 (from file:///home/user/research/audiskill/notebooklm-mcp)
 - notebooklm-py==0.8.1
 + notebooklm-py==0.8.2
Installed 2 executables: nblm-mcp, notebooklm-cover
```

**安裝前後版本字串一模一樣(0.9.24 → 0.9.24)**,正如 CLAUDE.md §〇 所述 ——
唯一動的是 `notebooklm-py 0.8.1 → 0.8.2`,以及被換掉的來源
(git `61789f5` → 本機工作樹 `a440e1c`)。

- **CLAUDE.md §二 的同名 script 衝突,這次沒發生**:`~/.local/bin/nblm-mcp` 的 entry
  point 是 `from notebooklm_mcp.server import main`(我們這支),不是上游那支。
- 安裝時 **`-c prd` server (pid 479717) 是活著的**(06:20:45 起,已跑 5h45m)。
  CLAUDE.md §九 明示接受這個取捨;記在這裡讓它可觀測,收工必須還原。

```
$ ~/.local/share/uv/tools/notebooklm-mcp/bin/python -c "...versions..."
0.9.24 0.8.2 1.29.1
```

### 0.5 `local-checks.sh` — 17 通過 / 0 失敗

第一次跑是 **16 通過 / 1 失敗**,失敗在 §2。查證後是 **check 自己過期,不是產品回歸**:

```
❌ _dispatch_audio_with_failover 的簽名或回傳不對 —— finalize 會鬆開身分
```

§2 想守的三件事實測**全部成立**:

```
$ inspect.signature(tools_podcast._dispatch_audio_with_failover)
(store, episode_n, attempt_id, generate, *, account: 'str | None',
 client: 'object', notebook_id: 'str | None' = None) -> 'tuple[str, str | None, object]'
```

1. `account` / `client` 是 keyword 參數 ✅
2. 回傳標註是三元組 `tuple[str, str | None, object]` ✅
3. `return ensure_started(status), account, client` **存在,只是搬家了** ——
   v0.9.16 把 failover 迴圈抽到 `notebooklm_mcp._failover.dispatch_with_failover`
   (簽名 `(dispatch, *, account, client, record_failover, …) -> tuple[str, str|None, Any]`),
   那一行現在住在那裡。`_dispatch_audio_with_failover` 只剩音檔家族專屬的稽核 callback。

**check 的寫法是對源碼做字面比對**,所以在函式被抽走之後變成假紅。
`grep` 確認這條字面比對從 v0.9.0 起被逐字複製到每個驗收工作區
(0.9.0 / 0.9.1 / 0.9.3 / 0.9.6 / 0.9.13-rc / 0.9.14 / 0.9.25-rc),而 v0.9.14 與
v0.9.25-rc 之間**沒有其他驗收工作區** —— 這是 v0.9.16 抽離之後第一次跑到它,
所以是這一輪第一次紅。

已就地修好(本工作區的 `local-checks.sh` §2):保留簽名與回傳標註的斷言,
把字面比對移到 `_failover.dispatch_with_failover`。修完 17/17 全綠。

> 📌 **要回寫**:這是 local-checks 範本的缺陷,不是 rc 的問題。
> 下一輪的工作區範本要帶上這個修正,否則會再假紅一次。

```
=== 0. 實裝版本 ===
  ℹ️  notebooklm-mcp metadata 版本 0.9.24(rc 未發 tag,不拿它當判準)
  ✅ 裝到的確實是這一輪的 build(source_search + backend 護欄都在)
  ✅ notebooklm-py 0.8.2(= pyproject 下界)
  ℹ️  mcp 1.29.1(lock 也是 1.29.0;若這裡不同,測試與實跑就不同版)
=== 1 ===  ✅ _sync_auth_env 已移除、snapshot() 已就位且無 suspend 點
=== 2 ===  ✅ _dispatch_audio_with_failover 吃 account/client 並回三元組
           ✅ 沒有殘留的「dispatch 後重讀全域」註解
=== 3 ===  ✅ 空值 PSIDTS 在落檔前就被擋掉(strict 同語義)
           ✅ 憑證檔是 0600
=== 4 ===  ✅ podcast_attempt_retract 有 abandon_in_flight(預設 False)
           ✅ series 有 notebook_access_denied 停點
=== 5 ===  ✅ VIEWER 不算已分享 / OWNER 算 / email 大小寫不敏感
=== 6 ===  ✅ add_user 預設仍是 VIEWER、permission 仍可位置傳、SharedUser 有 permission
=== 7 ===  ✅ 37 個 MCP 工具
=== 8 ===  ✅ 下載走每跳憑證政策,信任網域未變(0.8.1 必紅)          ← ⭐ 8a
           ✅ source_search 原樣轉發 + SDK 在 RPC 前擋掉三種壞輸入   ← ⭐ 8b
           ✅ concept_explanation 可用,靜態格式仍擋 custom_prompt   ← ⭐ 8c
           ✅ backend 預設仍是 web,_web 實作拿得到                  ← ⭐ 8d
=== 8z ===  ✅ skill 快照與上游逐字一致
            ℹ️  上游 skill HEAD: 0d83bfa docs(notebooklm): 配合 MCP 0.9.24 ——
                新增 source_search / concept_explanation,補 reference 目錄與 notebook_id 路由
──────────────────────────────────────────
  通過 17 / 失敗 0
```

**README 的兩個硬關卡 §0 與 §8a 都綠** —— 可以進真實生成。

### 0.6 順帶記下:`nblm-mcp` 沒有 verbose / log 旗標

```
$ nblm-mcp --help
usage: nblm-mcp [-h] [--transport {stdio,streamable-http,sse}] [--host HOST]
                [--port PORT] [--allow-insecure-remote]
```

README **Phase 3 §4** 寫「在 server log 或 `--verbose` 下確認下載發生了 redirect」
—— **那個旗標不存在**。但**也不需要**:`httpx` 的 logger 預設就以 INFO 打在 server 的
stderr 上,而且連 redirect 都印:

```
$ grep -oE 'HTTP/1.1 30[0-9] [A-Za-z]+' <一次啟動的 server stderr> | sort | uniq -c
     27 HTTP/1.1 302 Found
```

→ **Phase 3 §4 的作法定下來**:三支下載各用一次自起的 server 跑(`nblm_call.py`,
每次一份獨立的 `.err`),然後 `grep -E '302|googleusercontent' download.err`。
Phase 3 整階段都走自起的 server —— session server 的 stderr 進了 Claude Code 的
log 目錄,不好讀,而且本來就需要每次下載一份乾淨的 stderr。

### 0.7 🔴 Phase 1 前的硬關卡:MCP server 必須重連

Phase 0 換掉的是磁碟上的 venv。本 session 的 stg server(pid 598963,11:59:36 起)
仍是 **0.9.24 / notebooklm-py 0.8.1** 的程式碼,坐在一個已經被換掉的 venv 上。

**在使用者重連 MCP server 之前,一支 `mcp__notebooklm__*` 都沒有呼叫過。**

重連後的免費驗證:工具面應該出現 `mcp__notebooklm__source_search`(現在沒有)。

---

## Phase 1 — 協定層 + 認證層

⚠️ 本階段**全部用自起的 server** 完成,沒有動到 session 的 MCP 連線
(那台還是舊 build,見 0.7)。工具:`scratchpad/nblm_call.py` ——
`doppler run -p notebooklm -c stg -- nblm-mcp --transport stdio` + `mcp.client.stdio`,
呼叫完**正常結束**。Phase 1 §3/§4 本來就需要一台環境與生死都由自己掌控的 server。

### 1.1 協定層:37 支工具,`source_search` 在

```json
{"tool_count": 37, "has_source_search": true}
```

自起的 server 列得出 37 支且**包含 `source_search`** —— 這同時證明了自起的這台
確實是 rc build(session 那台沒有這支)。

### 1.2 `auth_check(all_slots=true)` — 9 個槽位**全部健康**

`ok: true` / `all_usable: true`。逐槽位(帳號 email 依 AGENTS.md 不落檔,只記槽位):

| slot | active | env | `usable` | `refreshable` | `heal_reason` | `psidts_domains` | `notebooks` |
|---|---|---|---|---|---|---|---|
| 1 | ✅ | `NOTEBOOKLM_AUTH_JSON` | true | true | null | `.google.com` | 0 |
| 2 | | `…_2` | true | true | null | `.google.com` | 0 |
| 3 | | `…_3` | true | true | null | `.google.com` | 0 |
| 4 | | `…_4` | true | true | null | `.google.com` | 4 |
| 5 | | `…_5` | true | true | null | `.google.com` | 9 |
| 6 | | `…_6` | true | true | null | `.google.com` | 0 |
| 7 | | `…_7` | true | true | null | `.google.com` | 2 |
| 8 | | `…_8` | true | true | null | `.google.com` | 20 |
| 9 | | `…_9` | true | true | null | `.google.com` | 1 |

**依 CLAUDE.md §七,這是驗收的輸入不是通過條件** —— 而這次的輸入是「全綠」,
意思是**這一輪拿不到免費的判別素材**:配額耗盡 / 認證失效 / 權限不足這三條分支
沒有現成的不健康槽位可以拿來驗。三條裡只有「權限不足」能人為製造
(建一本只分享給單一帳號的 notebook,CLAUDE.md §五),配額耗盡則要真的打到。

### 1.3 憑證落檔:權限對、正常結束會刪 —— 但**有間歇殘留**

存活期間(rc build,自起的 server):

```
/tmp/notebooklm-mcp-auth-35_ima6j   dir_mode 0o700     ✅
  slot-1.json … slot-9.json          0o600  ×9         ✅
  .slot-N.json.lock / .rotate.lock   0o600  ×18        ✅
```

`0700` / `0600` 全部正確,連 v0.9.x 之後多出來的 lock 檔也是 `0600`。

**但「正常結束一定刪乾淨」這條,實測不是每次都成立。**

| 批次 | client 關閉寬限 | 殘留 |
|---|---|---|
| 探索期(逐次手動) | 2.0s(SDK 預設) | 2 次(`35_ima6j`、`wnbv0l1l`) |
| 受控 10 連跑 | 2.0s(SDK 預設) | **1 / 10**(`jzw0uuo8`) |
| 受控 10 連跑 | 30.0s | **0 / 10** |

洩漏與不洩漏的 server stderr **逐行一樣、都是 317 行、都沒有 traceback**
—— server 端看不出任何異常。

**機制**(已用強迫觸發的判別實驗**直接證明**,不只是相關):

```python
# mcp/client/stdio/__init__.py:48
PROCESS_TERMINATION_TIMEOUT = 2.0
# :204  with anyio.fail_after(PROCESS_TERMINATION_TIMEOUT):
# :209      await _terminate_process_tree(process)     → killpg(SIGTERM) → SIGKILL
```

MCP client 關掉 stdin 之後**最多等 2.0 秒**,逾時就對整個 process group 送 SIGTERM。
而 CLAUDE.md §三 已記:**SIGTERM 不展開 `AsyncExitStack`** → 憑證目錄留在磁碟。

lifespan 展開實際耗時(寬限拉到 60s 量的,5 連跑):

```
{"startup_s": 11.16, "session_close_s": 0.0, "shutdown_s": 0.5}
{"startup_s": 11.17, "session_close_s": 0.0, "shutdown_s": 0.55}
{"startup_s": 12.24, "session_close_s": 0.0, "shutdown_s": 0.53}
{"startup_s": 11.32, "session_close_s": 0.0, "shutdown_s": 0.61}
{"startup_s": 16.12, "session_close_s": 0.0, "shutdown_s": 0.57}
shutdown_s: min=0.5 median=0.55 max=0.61
```

中位數 **0.55s**,離 2.0s 的預算還有 3.6 倍餘裕 —— 所以**不是「展開一律太慢」**。

**判別實驗(強迫觸發)**:把 `_terminate_process_tree` 包一層、逾時要送 SIGTERM 前先在
stderr 印 `TERMINATE_FIRED`,再把寬限分別壓到 0.1s(明顯低於 0.55s 的展開耗時)與拉到 30s:

| 寬限 | 殘留 | `TERMINATE_FIRED` |
|---|---|---|
| **0.1s** | **3 / 3** | **3 / 3** |
| **30.0s** | 0 / 3 | 0 / 3 |

**SIGTERM 送出 ⟺ 憑證目錄殘留**,兩邊各 3/3 完全對齊 —— 因果成立,
殘留就是 client 端寬限逾時打斷 `AsyncExitStack` 展開造成的,server 沒有 bug。
(預設 2.0s 下另跑 10 次,0 殘留、0 SIGTERM —— base rate 約 10% 時 0/10 不意外,
所以那一批沒有提供額外資訊,列出來只是不隱藏跑過的樣本。)

**仍然 inconclusive 的那一半**:為什麼展開偶爾會超過 2 秒(中位數只有 0.55s)。
最可能的嫌疑是 9 個槽位的 httpx client 關閉時偶爾卡住一個連線,但這一輪沒有直接證據。

**這件事的實務意義**:CLAUDE.md §三 寫「正常結束(lifespan 展開)會自己刪乾淨 ——
那條實測過」,並把殘留歸因於 `kill -9` / `SIGTERM`。實測補上一句:
**在 MCP client 的預設 2 秒寬限下,「正常結束」本身就有約 10~15% 的機率變成 SIGTERM**,
所以 §三 觀察到的「一個下午留下 3 個目錄」不需要有人 `timeout` 殺 process 也會發生。
這是既有取捨的**發生率**修正,不是新 bug —— 但它解釋了為什麼殘留比預期頻繁。

> 📌 **可考慮的修法**(不在本輪範圍,列給決策):lifespan 的 `finally` 收斂到
> **先 rmtree 再關 httpx client**(把最重要的清理放在最不可能被打斷的位置),
> 或給 rmtree 一個 `atexit` 兜底。目前的順序讓清理排在一堆網路關閉之後。

殘留目錄的清理:`35_ima6j` / `wnbv0l1l` / `jzw0uuo8` 都已對照 `ps` 確認沒有活著的
server 的 lstart 對得上(CLAUDE.md §三 的對照法),當場 `rm -rf` 收掉。

### 1.4 §4 判別實驗:`NOTEBOOKLM_BACKEND=android` 被 inline 刪除 ✅

shell 端 `export NOTEBOOKLM_BACKEND=android`,自起 server 後 `auth_check`:

```json
{"ok": true, "notebooks": 0}
```

**照常成功**,stderr 裡 `grpc` / `master token` 字樣出現 **0 次**。

這個實驗**不是空過的**,兩個控制都做了:

```
控制 1(變數真的傳到 server 端):
  $ NOTEBOOKLM_BACKEND=android doppler run -p notebooklm -c stg -- python -c "...os.environ..."
  server 端看到 NOTEBOOKLM_BACKEND = 'android'

控制 2(若沒被刪,這個值會真的改掉 backend):
  resolve_backend_preference(explicit=None, env=None)      -> BackendPreference(preferred='web',     reason='default')
  resolve_backend_preference(explicit=None, env='android') -> BackendPreference(preferred='android', reason='env')

  app._INLINE_AUTH_ENV_OVERRIDES = {
    'NOTEBOOKLM_DISABLE_KEEPALIVE_POKE': '1',
    'NOTEBOOKLM_HEADLESS_REAUTH': None,
    'NOTEBOOKLM_REFRESH_CMD': None,
    'NOTEBOOKLM_REFRESH_CMD_MIDSESSION': None,
    'NOTEBOOKLM_BACKEND': None,          ← None = 刪掉該變數
  }
```

變數傳得到、傳到了會生效、但 pool 照常認證成功 → **護欄有效**。

### 1.5 待補:重啟時觀察 session server 自己的憑證目錄

`jauru40n` 是 session 的 stg server(pid 598963)建的。使用者重啟 Claude Code 時,
**Claude Code 自己的 MCP client** 會示範它的關閉寬限夠不夠 —— 那一個觀測比上面 30 次
自起實驗更貼近正式情境(這台機器上跑正式節目的也是 Claude Code 的 MCP client)。
重啟後回填:`jauru40n` 消失 = Claude Code 的寬限足夠;還在 = 同一個殘留機制,
且對照 `ps` 沒有活著的 server 對得上就是孤兒,當場清掉。

### 1.6 `MCP_TOOL_TIMEOUT` 未設定 —— 重啟必須帶上

```
$ echo "${MCP_TOOL_TIMEOUT:-unset}"
unset
```

skill 的硬規則要求 Phase 6 之前設 `MCP_TOOL_TIMEOUT=1800000`,而 Claude Code
是在**啟動時**讀這個環境變數 —— `/mcp` 重連換不到它。所以重連與重啟要合成一次:
`MCP_TOOL_TIMEOUT=1800000 claude --continue`。


---

## Phase 0-b — 重啟之後的三個確認(2026-09-03 13:48 +08:00)

session 於 **13:47:30** 重啟(`.mcp.json` 的 stg server = pid 628950/629026,lstart 13:47:32)。

### 0b.1 工具面出現 `source_search` ✅

Claude Code 列出的 `mcp__notebooklm__*` deferred tools **共 37 支,`source_search` 在內**:

```
artifact_download_audio artifact_download_report artifact_download_slides artifact_list
artifact_rename artifact_retry_failed artifact_revise_slide artifact_wait auth_check
chat_ask episode_set_description episode_set_publication_state feed_info generate_audio
generate_report generate_slides notebook_create notebook_get notebook_list
notebook_share_with_pool podcast_attempt_adopt podcast_attempt_retract podcast_episode
podcast_episode_reconcile podcast_episode_resume podcast_series publish_series
research_import research_start research_wait source_add_file source_add_text
source_add_url source_delete source_fulltext source_list source_search
```

對照 0.3(重啟前**缺** `source_search`)→ **session 連上的這台是 rc build**。
0.7 的硬關卡解除,`mcp__notebooklm__*` 從這裡開始可以呼叫。

### 0b.2 `MCP_TOOL_TIMEOUT` 生效 ✅

```
$ echo "${MCP_TOOL_TIMEOUT:-unset}"
1800000
```

來源是 `.claude/settings.json` 的 `env`(README 已備好)。1.6 記的 `unset` 是**上一個
session 的狀態** —— 那次是從別處啟動的。本 session 從工作區目錄啟動,讀到了。

**補(14:0x,事後回填):環境變數讀到 ≠ 逾時真的生效**,所以補上效果面的觀測 ——
`podcast_series` 跑到 120 秒時,Claude Code **把它移進背景繼續跑**
(`task kzny0l89m`),而不是砍掉 request。那才是「長跑工具不會被 client 半路砍掉」
的實際證據。README Phase 0-b §2 要的是這一件,不是 `echo $MCP_TOOL_TIMEOUT`。

### 0b.3 🔴 憑證目錄:自然觀測落在**殘留**那一格,而且是「一個檔都沒刪」

重啟前的基線(README 記的)是 `jauru40n` 有主人(pid 598963)。重啟後:

| 目錄 | dir ctime | 檔數 | 對得上的活 process | 判定 |
|---|---|---|---|---|
| `…-ph3kbrjq` | 06:20:52 | 15(5 slot) | 479640/479717 `-c prd` lstart 06:20:45 | 有主人,**不准動** |
| `…-451ahwj4` | 13:47:53 | 27(9 slot) | 628950/629026 `-c stg` lstart 13:47:32 | 本 session 的 server |
| `…-jauru40n` | **13:47:25** | **27(9 slot)** | **無** —— 原主人 598963 已不存在 | **孤兒** |

**`jauru40n` 完整殘留:27 個檔案一個都沒少,9 份真憑證都在。**
與 `451ahwj4` 逐名比對,**檔名集合完全相同**(`diff` 無輸出)。

→ Claude Code 自己的 MCP client 關閉這台 server 時,**`rmtree` 根本沒開始跑**
(不是跑到一半被打斷 —— 那樣會少檔案)。這與 1.3 推的「清理排在一堆 httpx 關閉之後」
一致:展開卡在網路關閉那一段,還沒走到 rmtree 就吃到 SIGTERM。

**新事實:關閉序列裡有一次憑證回寫。** `jauru40n/slot-9.json` 的 mtime 是
**13:47:25.001**,其餘 8 個 slot 仍是 11:59:3x–11:59:46(建目錄時寫的):

```
12770  2026-09-03 13:47:25.001223539  slot-9.json     ← 只有這一個被重寫
12779  2026-09-03 11:59:46.219396717  slot-8.json
12783  2026-09-03 11:59:45.232333942  slot-7.json
  …(1–6 同為 11:59:3x–11:59:44)
```

目錄的 ctime 也是 13:47:25(寫檔走 temp+rename → 目錄項目變更)。
13:47:25 距新 server 起來(13:47:32)只有 7 秒,**早於**它 —— 所以這一寫是舊 server
(598963)做的,不可能是新 server。

**這一輪判斷不了的**:那次回寫是「關閉序列的一部分(把 refresh 過的 cookie persist 回去)」
還是「恰好在關閉前發生的一次 keepalive / 刷新」。兩者對殘留結論沒有影響,但
**若它屬於展開序列,就代表展開確實跑起來了、只是停在 rmtree 之前** —— 那會把 1.3 的
「📌 可考慮的修法」從猜測升級成有依據的建議。**標 inconclusive。**

### 0b.4 這個觀測對 1.5 的回填

1.5 問的是「Claude Code 的關閉寬限夠不夠」。答案:**這一次不夠**。
自起實驗量到的 base rate 約 10~15%,而正式情境下的這 1 次就中了 ——
樣本 n=1 不能拿來修正發生率,但它證明**這個殘留在真實 Claude Code 重啟下會發生**,
不需要有人動手 kill(CLAUDE.md §三 的警語成立,且原因比它寫的更前面:
不是「偶爾超過 2 秒」,是 rmtree 連跑都沒跑到)。

**處置**:`jauru40n` 依 CLAUDE.md §三 的對照法確認無主(上表),含 9 份真憑證,
當場 `rm -rf` 收掉。`ph3kbrjq` / `451ahwj4` 有主人,不動。

### 0b.5 讀碼把 0b.3 的 inconclusive 解掉了 —— 而且推翻了 1.3 的修法建議

`notebooklm_mcp/app.py:305 _lifespan` 的展開順序(逐字):

```python
cred_dir = Path(tempfile.mkdtemp(prefix="notebooklm-mcp-auth-"))
# 先註冊 → LIFO 展開時最後才跑:client 關閉時 SDK 可能回寫 cookie,
# 目錄要活到那之後。例外路徑也走同一條 stack,不會漏刪。
stack.callback(shutil.rmtree, cred_dir, ignore_errors=True)          # app.py:354
for slot, cred in enumerate(creds, start=1):
    path = _write_credential_file(cred, cred_dir / f"slot-{slot}.json", slot)
    ...
    client = await stack.enter_async_context(
        NotebookLMClient.from_storage(path=str(path), allow_headless=False))
```

三件因此成立:

1. **`rmtree` 是最先註冊的 → LIFO 展開時最後才跑。** 所以「27 個檔一個都沒少」
   =**展開沒跑到最後一步**,與 0b.3 的觀測一致。
2. **client 關閉時 SDK 會回寫 cookie —— 這是原始碼註解明講的預期行為。**
   而 slot-9 是**最後註冊**的 client → LIFO 下**第一個**被關。
   `slot-9.json` mtime 13:47:25、其餘 8 個原封不動,正好是「展開跑了一步就被打斷」的形狀。
3. 「那次回寫是 keepalive 嗎?」—— **不是**。`_INLINE_AUTH_ENV_OVERRIDES` 把
   `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 設成 `'1'`,而 rotation flock 由 lifespan
   持有整段、擋掉 heal 的 POST(app.py:360 註解)。這個 process 在 session 中途
   本來就不該有憑證寫入。

→ **0b.3 的 inconclusive 結案**:展開**確實啟動了**,關掉 slot-9 的 client(觸發
cookie 回寫)之後、在關完其餘 8 個 client 與跑 `rmtree` 之前,吃到 SIGTERM。
2 秒寬限要塞 9 個 client 的關閉 + rmtree,這次只跑完約 1 個。

> 🔴 **因此 1.3 的「📌 可考慮的修法」是錯的,不要照做。** 那裡建議「先 rmtree 再關
> httpx client」—— 那會直接違反 app.py:354 上方註解宣告的不變式(**目錄要活到
> cookie 回寫之後**),把「關閉時回寫的 cookie」寫進一個已被刪除的目錄。
> 這個順序是刻意的,不是疏忽。
>
> 站得住的方向只剩「**不改順序**」的那些:給 `rmtree` 一個 `atexit` / SIGTERM handler
> 兜底(在既有 stack 之外多一層,不動 LIFO),或縮短展開耗時本身。
> 本輪不做,列給決策 —— 但把錯誤的那條劃掉,免得下一輪照著改。

### 0b.6 build 身分:HEAD 已從 `a440e1c` 前進到 `dc4c6f2`,**runtime 未變**

```
$ git log --oneline a440e1c..HEAD
dc4c6f2 docs(gotchas-pool): 憑證殘留不需要有人 kill —— 正常關 session 就有約 10~15%
$ git diff --stat a440e1c..HEAD
 docs/gotchas-pool.md | 27 +++++++++++++++++++++++++++
```

**唯一的新 commit 是 docs-only**(把 1.3 的結論收進 `docs/gotchas-pool.md`),
沒碰任何 `notebooklm_mcp/*.py`。工作樹 clean。
→ 13:47 裝的那份 build(內容 = `a440e1c` 的工作樹)**仍然等於現在的 runtime**,
0.4 的安裝不需要重做。

### 0b.7 收工覆蓋自檢的注意事項(現在記下,免得到時假通過)

0b.1 把 37 支工具名逐字列進了 FINDINGS.md,README〈收工〉§0 的
`grep -q "$t" FINDINGS.md` 會**全數假通過**。收工時要先把 0b.1 那個 code block
排除掉再 grep:

```bash
sed '/^### 0b.1/,/^### 0b.2/d' FINDINGS.md > /tmp/coverage-check.md   # 再對這份 grep
```

---

## Phase 1(補)— session server 的 `auth_check` 對帳(13:49)

第一支經由 **Claude Code 自己的 MCP client** 打出去的 RPC。`auth_check(all_slots=true)`
回傳與 1.2(12:0x,自起 server)**完全一致**:`ok: true` / `all_usable: true`,
9 槽全部 `usable=true` `refreshable=true` `heal_reason=null`
`psidts_domains=[".google.com"]`,`notebooks` 數也一個沒變(0/0/0/4/9/0/2/20/1)。

→ 兩條路徑(自起 server vs session server)看到的 pool 狀態相同;
這一小時內沒有槽位變不健康。**依 CLAUDE.md §七 這是輸入不是通過條件** ——
本輪仍然拿不到「配額耗盡 / 認證失效」的免費判別素材(見 1.2)。

---

## Phase 2 — 讀取面回歸(新傳輸層的地基)

notebook:**`8fd51473-6758-48d2-896d-1861ec2e29da`** = `ZZ-TEST v0.9.25-rc A`

### 2.1 `notebook_create` — 自動分享給 pool ✅

```json
{"notebook_id": "8fd51473-…", "title": "ZZ-TEST v0.9.25-rc A",
 "shared_with": [<其餘 8 個槽位帳號>]}
```

active slot 是 1,建完自動把其餘 **8** 個帳號加成 EDITOR(9 槽 - owner 1 = 8)。

### 2.2 三支 `source_add_*` — `char_count` 的形狀**不一致**

| 工具 | source_id | `char_count` | 備註 |
|---|---|---|---|
| `source_add_file`(A,`sources/a-queue.md`) | `2e1542ad-…` | **580** | 檔案 1552 bytes,CJK 3 bytes/字 → 對得上 |
| `source_add_url`(`https://www.rfc-editor.org/rfc/rfc2119.txt`) | `219cd7a9-…` | **4893** | 非零,無 paywall warning |
| `source_add_text`(B,雜湊碰撞) | `a146e709-…` | **(沒有這個欄位)** | 見下 |

📌 **`source_add_text` 的回傳沒有 `char_count`**,只有 `{"source_id": …}`。
另外兩支都有。查 docstring:`source_add_url` 明寫「wait=True 時回傳附帶
best-effort 落地驗證:char_count」,`source_add_text` 的 docstring 只有一行
「Add plain text as a source.」—— **文件與實作一致,不是 bug**。
但 README Phase 2 寫「**每支**的 `char_count` 非零」,那個期待對 `source_add_text`
不成立。**這是 README 的期待寫寬了,不是產品缺陷**;`source_add_text` 的內容是
呼叫端自己給的,本來就不需要 best-effort 落地驗證。
(下方 2.4 用 `source_fulltext` 補驗了它真的落地。)

### 2.3 `source_list` — `kind` 值(0.8.2 `SourceType` 的實測落點)

```json
[{"source_id":"2e1542ad-…","title":"A 佇列延遲與吞吐","kind":"markdown","ready":true},
 {"source_id":"a146e709-…","title":"B 雜湊碰撞與生日界","kind":"markdown","ready":true},
 {"source_id":"219cd7a9-…","title":"https://www.rfc-editor.org/rfc/rfc2119.txt",
  "kind":"pasted_text","ready":true}]
```

- **沒有出現 `google_drive`** —— README 要盯的那個 code-14 改判沒被踩到 ✅
- 三筆 `ready` 全 `true`
- ℹ️ **URL 來源的 `kind` 是 `pasted_text`,不是某種 url/website 型別**。
  素材是 `.txt`(純文字 URL),伺服器把它收成貼上文字是說得通的 ——
  但**這一筆不能拿來蓋章「URL 來源一定回 pasted_text」**(只有一個樣本,
  且是純文字 URL 這個特例)。記錄形狀,不下通則。
- 兩份 `.md` 都判 `markdown`(A 走 `source_add_file`,B 走 `source_add_text`)。

### 2.4 `source_fulltext(max_chars=0, contains=[...])` — 內容隔離成立

對 A 那筆查 5 個詞(3 個 A 領域 + 2 個 B 領域):

```json
{"source_id":"2e1542ad-…","title":"A 佇列延遲與吞吐","char_count":580,
 "hits":{"百分位":true,"反壓":true,"吞吐":true,"生日":false,"簽章":false},
 "truncated":true,"content":""}
```

- `hits` 是**布林**(README 要對帳的那件事)✅
- A 領域 3 個詞全 `true`、B 領域 2 個詞全 `false` → **A 與 B 沒有互相污染**,
  Phase 4a 的正負向對照有乾淨的邊界
- `char_count: 580` 與 `source_add_file` 回的一致
- `truncated: true` 在 `max_chars=0` 下出現 —— 與 docstring 一致(不是異常)

### 2.5 `artifact_list` — 空 ✅

```json
{"artifacts": []}
```

`kind` 未過濾,notebook 剛建、還沒生成任何東西。Phase 6 之後要回來對照。

### 2.6 `notebook_get` — `role` 欄位是 0.8.1 起的新形狀

```json
{"notebook_id":"8fd51473-…","title":"ZZ-TEST v0.9.25-rc A","sources_count":3,
 "is_owner":true,"role":"OWNER","created_at":"2026-09-03T05:54:18+00:00"}
```

`sources_count: 3` 對得上 2.3。**`is_owner: true` 且 `role: "OWNER"`** ——
docstring 說升版前多帳號 pool 模式下 `is_owner` 實務上恆為 `False`(有共享者就判 False),
0.8.1 起改讀真正的 userRole。這本 notebook **有 8 個共享者而 `is_owner` 仍為 `true`**,
正是升版後的行為 → **`is_owner` 的語意修正在真帳號上實測成立** ✅
(這是 SDK 大版跳躍「沒改到但被波及」那一半裡,唯一會改變回傳值的一支。)

### 2.7 `chat_ask` — 引用對得上來源

問「這些來源分別在講什麼主題」。回傳 `conversation_id: c0b36beb-…`,
`references` 共 7 筆,`source_id` 全部落在 2.3 的三筆之內(A×1、B×1、RFC×5),
`cited_text` 是**來源原文**(不是生成句)。三份素材各被正確歸類:
A→排隊效能、B→密碼學雜湊、RFC 2119→需求層級關鍵字。

ℹ️ 回答尾端伺服器主動附了一句「我可以為您設計一份隨堂測驗」的推銷句
(`💡 **想要檢驗…**`)。這是 NotebookLM 伺服器端行為,不是我們加的 ——
`strip_citations=False`(預設)下原樣穿透。做 show notes 時要留意。

### 2.8 `notebook_share_with_pool` — 冪等成立 ✅

```json
{"notebook_id":"8fd51473-…","shared_by":"<slot-1>",
 "shared_with":[],
 "already_shared":[<2.1 那 8 個帳號,逐字相同>]}
```

`notebook_create` 已經分享過 → 這次 `shared_with` 為**空**、8 個全落在
`already_shared`。**重跑安全的冪等語義實測成立**,而且 `shared_by` 就是 owner
(active slot 1),沒有動用 `_resolve_share_executor` 的掃描分支。

---

## Phase 4a — ⭐ `source_search` 首次上線(探針先活著)

原始 JSON 落在 scratchpad(`offsets_raw.json` / `s4a.json`),下面的數字都是從那裡讀出來的,
**沒有經過我轉錄** —— 這一階段要比對字元 offset,轉錄失真會直接產生假結論。

### 4a.1 探針活著 ✅

`source_search(query="為什麼要看尾端百分位而不是平均延遲?")`(不限 source):

```
count = 11   ranks = [1,2,3,4,5,6,7,8,9,10,11]
rank 1 → A 佇列(source 2e1542ad)
rank 2 → B 雜湊(source a146e709)
rank 3..11 → RFC 2119(source 219cd7a9)
```

非空,且 **A 領域的查詢把 A 排在 rank 1** —— 探針有效,4a 其餘各項的結論站得住。

### 4a.2 🔴 **`source_search` 回的是「全部 chunk 排序後」,不是「相關的那些」**

這是本階段最重要的行為事實,README / docstring 都沒有講明:

| 查詢範圍 | `count` | 該範圍的 chunk 總數 |
|---|---|---|
| 整本 notebook | **11** | A 1 + B 1 + RFC 9 = **11** |
| `source_ids=[A]` | **1** | A = **1** |
| `source_ids=[RFC]` | **9** | RFC = **9** |

**`count` 恆等於範圍內的 chunk 總數,與查詢內容無關。** 換掉查詢詞(A 領域 / B 領域 /
英文 RFC 詞)`count` 都不變,變的只有排序。所以它是一個 **ranker,不是有門檻的 retriever**。

~~⚠️ caveat:整本只有 11 段,「回全部」與「有門檻但全過門檻」分不開。~~
✅ **這個 caveat 在 4b 被消掉了(見 4b.2)**:用 27 段的 mp3 逐字稿 + 一個
**完全無關**的查詢(「烘焙麵包的酵母發酵時間」)實測,回的仍是**全部 27 段**、
span 完整覆蓋 0..26927。任何合理的相關度門檻都不會讓「排隊理論 podcast 的 27 段」
全數通過「麵包酵母」的查詢 → **「有門檻」這個解釋被排除,ranker 結論成立。**

三個對呼叫端有實際後果的推論:

1. **`limit` 實務上是必要的,不是選配。** 不傳就會把整本的每一段原文都灌進 context ——
   這本只有 3 筆小素材就已經是 11 段;正式節目一本動輒數十筆來源。
2. **「回空 list」的語意比 README 想的更強。** README 警告「回空不等於沒被索引,
   可能只是查詢詞不對」—— 但既然不相關的 chunk 也照樣回,**查詢詞不對根本不會造成回空**。
   回空 ⟺ 範圍內**一段都沒有被索引**。這反而讓 4b(mp3 逐字稿)的判讀變乾淨了。
3. **README 4a §6 的「負向檢查」在這個行為下不可能以「回空」成立** —— 見 4a.5。

### 4a.3 `rank` 的實際分布:**一次都沒有出現 0**

跨 6 次查詢(整本 ×2、限定 A ×2、限定 RFC ×2)收到的 rank 全部是
**從 1 開始、步長 1、無重複、無 0 的稠密整數**:

```
[1..11]  [1..11]  [1]  [1]  [1,2]  [1..9]
```

→ README 擔心的「若大量是 0,『越小越相關』就沒作用」**在這個 pool / 這種來源上沒有發生**。
docstring 寫的「`0` 代表伺服器沒給排名(不是最相關)」這條路徑**一次都沒走到**。

⚠️ **但不要把這寫成「伺服器一定給排名」**:樣本是 1 本 notebook / 3 筆來源 / 11 段。
可以回寫的只有「實測 6 次查詢、11 段 ×2 + 9 + 2 + 1 + 1,rank 全部非零且稠密」。

### 4a.4 🔴 **`start`/`end` 切不動 `source_fulltext` —— 而且會靜默切錯**

README 4a §2 要對帳的那一條。結論是**對不上,且失敗形狀很危險**。

`start`/`end` **自己內部是自洽的**:RFC 的 9 段完美無縫拼接(`(0,541) (541,1104)
(1104,1645) (1645,2188) (2188,2689) (2689,3251) (3251,3783) (3783,4354) (4354,4723)`,
`span[i].end == span[i+1].start` 全部成立)。

**但它們索引的不是 `source_fulltext` 回的那份文字**:

| 來源 | chunk span 覆蓋 | `source_fulltext` 的 `char_count` | 差 |
|---|---|---|---|
| RFC 2119 | 0 .. **4723** | **4893** | 170 |
| A 佇列 | 0 .. **536** | **580** | 44 |

拿 offset 去切,切出來的東西**看起來像那份文件、但起訖位置是錯的**:

```
RFC chunk [541:1104]
  chunk 自己的 text 開頭 : 'Abstract\n\nIn many standards track se…'
  fulltext[541:1104] 開頭: 'emo is unlimited.\n\n\n\nAbstract\n\n\n\n   In many st…'   ← 從句中開始

RFC chunk [0:541]
  chunk 自己的 text 開頭 : 'Network Working Group                 …'
  fulltext[0:541] 開頭   : '\n\n\n\n\n\n\n\n\n\n\n\nNetwork Working Group  …'      ← 多 12 個換行

A chunk [0:536]
  chunk 自己的 text 開頭 : '### 排隊系統的延遲與吞吐:一份給驗收用的素材…'
  fulltext[0:536] 開頭   : '排隊系統的延遲與吞吐:一份給驗收用的素材…'                  ← 少了 '### '
```

原因看得出來:兩邊是**兩種不同的 rendering**。chunk 的 `text` **保留 markdown**
(`### `、`#### `、`**粗體**`,而且在 `**` 前後補了空格);`source_fulltext` 的
`content` **把標記拆成換行**。第三種表示是磁碟原檔(A = 620 字)。三者長度全不同:

```
source_fulltext content  580
source_search chunk text 641
磁碟原檔 a-queue.md      620
chunk 宣告的 end         536      ← 四個數字互不相等
```

**這是要回寫文件的東西。** tool-reference / docstring 現在寫「`start` / `end` 是
source 內的**字元 offset**」,那句話會誘導呼叫端去做 `fulltext[start:end]` ——
而那個操作**不會報錯,只會回一段位移了幾十個字元的文字**。正確的用法是
**直接用 `chunk.text`**,offset 只能拿來判斷「哪一段在前面」與「兩段是否相鄰」。

> 📌 **這條建議在 4b 被修正過,以 4b.3 的版本為準** —— offset 對**沒有標記的來源
> (媒體逐字稿)是對得上的**,對有標記的來源才錯位。所以問題不是「offset 一律沒用」,
> 而是「呼叫端從外面分辨不出哪一種」。最終建議見 4b.3。

### 4a.5 `source_ids` 限定 ✅ / 負向檢查要換判準

- **限定成立**:`source_ids=[A]` 回的 1 段全部來自 A;`source_ids=[RFC]` 回的 9 段全部來自 RFC。
- **README §6 的負向檢查(B 領域詞 + `source_ids=[A]` → 應回空或明顯不相關)**:
  實測**回 A 那一段,rank 1**。依 4a.2,這是必然的 —— A 只有一段,而它一定會被回。
  **「回空」在這個 API 形狀下不可能成立**,所以那個判準本身要換掉。

  換成**排序判別**才有鑑別力,而它**通過**了:同一組 B 領域查詢詞
  (`"簽章 碰撞 生日界"`)不限 source 打整本 →

  ```
  rank 1 → B 雜湊 (a146e709)        ← B 領域查詢把 B 排第一
  rank 2 → RFC (219cd7a9) 尾頁
  rank 3 → RFC (219cd7a9) 首頁
  …A 佇列排在更後面
  ```

  對照 4a.1(A 領域查詢 → A rank 1),**語意排序在兩個方向上都把對的來源放到 rank 1** ✅

### 4a.6 壞輸入:三種都在打 RPC 前擋掉 ✅(但擋在**兩個不同的層**)

| 輸入 | 結果 | 訊息 | 擋在哪一層 |
|---|---|---|---|
| `query=""` | **raise** ✅ | `query must be a non-empty string` | SDK `validate_search` |
| `limit=0` | **raise** ✅ | `limit must be a positive integer` | SDK `validate_search` |
| `source_ids="<str>"`(非 list) | **raise** ✅ | `1 validation error for source_searchArguments / source_ids / Input should be a valid list` | **pydantic(MCP tool schema)** |

- **README §7 的前提成立**:`query=""` **raise 而不是回空 list**。
  這正是「驗證交給 SDK、不自己重寫一份」那個決定所依賴的前提 ——
  若它回空 list,呼叫端會讀成「來源裡沒有」。**不需要修。**
- ℹ️ 第三條的細節與 docstring 的說法有出入:docstring 說三種驗證「一律由 SDK 的
  `_sources.validate_search` 在打 RPC 之前擋掉」,但 `source_ids` 型別實際上被
  **MCP 的 pydantic schema 先攔下**,SDK 那條檢查對有型別的 MCP client 是走不到的。
  **結果相同(RPC 前擋掉),不影響安全性**,只是「擋在哪一層」與文件描述不同。
  記錄而不判為缺陷。

---

## Phase 3 前置 — 下載路徑的**實際**組態(讀碼,先於實跑)

README 把「下載路徑改成每跳憑證政策」列為本輪第一優先。實跑之前先把**這台機器上
實際會走的那條路**釘清楚,否則實跑通過也不知道通過的是什麼。

### 3.0a 走的是 **httpx 分支**,不是 curl_cffi 分支

```
$ resolve_transport_factory()
<class 'httpx.AsyncClient'>          # 是 httpx.AsyncClient 嗎? True
$ import curl_cffi
ModuleNotFoundError: No module named 'curl_cffi'
```

`NOTEBOOKLM_TRANSPORT` 未設 → `httpx.AsyncClient`,而 `curl_cffi` **根本沒裝**。

### 3.0b 🔴 **`local-checks.sh` §8a 的 `cookies=None` 斷言,描述的不是我們走的那條分支**

§8a 斷言:

```python
src = inspect.getsource(dc._make_download_client)
assert "cookies=None" in src, "還在把 cookie jar 交給 constructor —— 不是 0.8.2 的每跳模型"
```

但 `_make_download_client` 裡 `cookies=None` **只出現在 curl_cffi 那一支**:

```python
if factory is httpx.AsyncClient:
    client = httpx.AsyncClient(
        cookies=cookies,                    # ← 我們走這條:jar 仍然交給 constructor
        follow_redirects=True, timeout=timeout,
        event_hooks=redirect_revalidation_hooks(_is_trusted_download_host,
                                                resolved_credential_for))
else:
    client = factory(cookies=None, follow_redirects=False, timeout=timeout)   # ← 斷言看到的是這一行
```

**這條 check 是對整份原始碼做字面 `in` 比對**,兩個分支的原始碼都在同一個字串裡,
所以不論實際走哪一支都會綠。**它證明的是「0.8.2 的程式碼在」,不是「我們走的那條路
改成了每跳模型」。** §8a 仍然有價值(`credential_for` 在、信任網域沒被改、
`_on_response` 在,這三條都是真的判別),但**「不再把 jar 交給 constructor」這句話
對我們的組態是錯的**。

**公平起見,§8a 的其餘四條都是行為斷言,是真的判別**:

```python
assert dc._TRUSTED_DOWNLOAD_DOMAINS == (".google.com", ".googleusercontent.com", ".googleapis.com")
assert dc._is_trusted_download_host("evil%2egoogleapis.com") is False      # 百分號繞過
assert dc._is_trusted_download_host("x.googleusercontent.com") is True
hooks = redirect_revalidation_hooks(dc._is_trusted_download_host, lambda _u: None)
assert "response" in hooks                                                 # _on_response 在
```

有問題的只有那兩條 `in src` 字面比對(`"credential_for" in src` 也一樣寬 ——
那是**參數名**,兩個分支共用一個簽名,永遠會命中)。

**同一種弱點在 §8b / §8c / §8d 沒有出現** —— 逐條看過,那三節全部是「呼叫函式、
斷言回傳值」的行為判別(§8b 甚至用假 client 驗參數原樣轉發、`rank=0` 與
`start/end=None` 沒被吃掉)。**這是 §8a 獨有的問題**,不是 local-checks 的通病。

📌 **建議修 check**:把兩條 `in src` 換成對**實際解析出來的分支**求值 —— 直接斷言
httpx 分支掛上了 `event_hooks`,那才是 httpx 路徑上 0.8.2 真正新增的東西:

```python
import httpx
assert dc.resolve_transport_factory() is httpx.AsyncClient   # 先講清楚驗的是哪條路
client, _get = dc._make_download_client(httpx.Cookies(), timeout=1.0)
assert client.event_hooks["request"], "httpx 分支沒掛上每跳 request hook"
assert client.event_hooks["response"], "httpx 分支沒掛上 _on_response"
```

### 3.0c 每跳政策的實際內容:**預設政策對每一個「可信」跳都給整份 jar**

`_redirect_guard.redirect_revalidation_hooks._on_request` 每一跳做兩件事:

```python
_assert_trusted_download_request(request, is_trusted_host)   # 非 https 或不在白名單 → raise
...
credentials = credential_for(str(request.url))
request.headers.pop("cookie", None)                          # 先拔:constructor jar 建好的
for name in {"authorization", "proxy-authorization"}: request.headers.pop(name, None)
if credentials is None: return
if credentials.cookies is not None:
    credentials.cookies.set_cookie_header(request)           # 再套:政策算出來的
```

而 `downloads._credential_policy` 的預設政策是:

```python
def credential_for(url):
    parsed = urlparse(url)
    if parsed.scheme == "https" and _is_trusted_download_host(parsed.hostname):
        return HopCredentials(cookies=cookie_jar)     # 整份 jar
    return None
```

`_TRUSTED_DOWNLOAD_DOMAINS = ('.google.com', '.googleusercontent.com', '.googleapis.com')`,
而 `_assert_trusted_download_request` **已經先把不可信的跳擋掉並 raise** ——
所以**能活到套憑證那一步的跳,一定在白名單上,一定拿到整份 jar**。

### 3.0d ⭐ 由此推出的預測(實跑前先寫下來,免得事後合理化)

- **我們 repo 沒有安裝任何自訂 policy**(`grep credential_for|HopCredentials|CredentialPolicy`
  在 `notebooklm_mcp/` 下 **0 命中**),SDK 也只在 `downloads.py` 用預設那個。
- 預設政策「每個可信跳都給整份 jar」+ httpx 本來就會做 cookie 的 domain/path 比對
  → **「先拔再套」對這個組態在結果上是恆等的**。
- 因此 **README 排第一優先的那個風險,在我們的組態上結構性地是 no-op**:
  它只有在裝了**更窄**的政策(某些跳刻意不給憑證)時才會改變行為,而那個政策不存在。
- **但這不代表不必實跑**:上面是靜態推導,真正要驗的是「redirect 之後檔案真的拿得到、
  不是 HTML 錯誤頁」。實跑仍照 README Phase 3 全做。

**預測**:三支下載都成功、`file` 判為 Audio/PDF/text,且 stderr 看得到往
`googleusercontent` 的 302。若三支都成功但**完全沒有 redirect**,那 README 的
「下一輪不必再花這個力氣」結論成立得更強。

---

## Phase 5(部分)— `report_format` 的壞輸入閘(不燒配額,先做)

用**不存在的 manifest 路徑** `/nonexistent/path/series_manifest.json` 跑,
這樣「有沒有擋在 manifest 之前」自己會現形。

| # | 傳入 | 結果 | 訊息 |
|---|---|---|---|
| 0 | `concept_explanation` + `custom_prompt="x"` | **raise** ✅ | `custom_prompt 只在 report_format='custom' 有效(現在是 'concept_explanation',SDK 會套靜態模板並丟掉 prompt)` |
| 1 | `custom`,**不給** `custom_prompt` | **raise** ✅ | `report_format='custom' 需要非空的 custom_prompt(否則 SDK 靜默套用通用預設句,白燒一次配額)` |
| 2 | `custom` + `custom_prompt` + `extra_instructions` | **raise** ✅ | `report_format='custom' 不吃 extra_instructions(SDK 不串接,會靜默消失);把要求併進 custom_prompt` |
| 3 | `concept_explanation` + `extra_instructions`(**合法組合**) | 通過格式閘,**死在 manifest** | `[Errno 13] Permission denied: '/nonexistent'` |
| 4 | `report_format="no_such_format"` | 通過格式閘,**死在 manifest** | `[Errno 13] Permission denied: '/nonexistent'` |

**README Phase 5 §4 通過**:`concept_explanation` + `custom_prompt` 確實 raise `ValueError`,
沒有讓 SDK 靜默丟掉 prompt。

**順序也被證明了**(這是用假路徑換來的額外資訊):#0–#2 在**碰到 manifest 之前**就 raise,
#3/#4 才走到讀 manifest 那一步 →`_validate_report_prompt` 確實是
`generate_report` 的第一行(`tools_artifacts.py:672`),排在 `_require_episode`
與 `runtime.snapshot()` 之前。

**README Phase 5 §5 的前提成立**:#3 顯示 `extra_instructions` 對**靜態格式是合法的**
(只有 `custom` 那條被擋)。它實際有沒有影響產出,要等生成完才驗。

ℹ️ #4 順帶量到:**未知的 `report_format` 不由 `_validate_report_prompt` 攔**,
它走到 `to_report_format()`(`tools_artifacts.py:682`)才轉換失敗 —— 那仍在 RPC 之前
(在 `_dispatch_attachment` 前),所以**不會燒配額**,只是報錯的位置比前三條晚。
不是缺陷,記錄形狀。

---

## Phase 6.1 — `podcast_series` 單集(進行中的可觀測事實)

呼叫:`podcast_series(notebook_id=8fd51473-…, episodes=[1 集], output_dir=output/)`,
其餘用預設(`audio_format="deep-dive"` / `audio_length="long"` / `wait_timeout=1200`)。
依 skill §Episodic 的入口表,這一格(共用 notebook、1 集 ≤5、3 筆來源 <10、不需指名來源)
的**唯一入口**就是 `podcast_series` ✅

### 6.1a 派送成功,attempt 落檔(manifest revision 4)

```json
"attempt_id": "b994fa15-1ef0-468c-98d7-bc29bb19f60a",
"created_at": "2026-09-03T06:03:21.879209+00:00",
"title": "EP01. 尾端延遲與碰撞機率",
"brief_sha256": "8bc76a23f9542112d79e9add6aaf615569c023879c30f58ca82a0db43f0469b1",
"settings": {"language":"zh_Hant","audio_format":"deep-dive","audio_length":"long"},
"dispatch": {"status":"accepted","artifact_ids_before":[],
             "dispatched_at":"2026-09-03T06:03:22.578650+00:00",
             "accepted_at":"2026-09-03T06:03:29.213417+00:00",
             "wait_timeout":1200.0,"account":"<slot-1>"},
"remote": {"artifact_id":"48528c43-b498-4657-b6a2-09d59e0f7e7e",
           "status":"pending","status_origin":"sdk_heuristic"}
```

要對帳的事實:

- **`language` 沒傳,manifest 記成 `zh_Hant`** —— skill §Language 說的預設實測成立 ✅
- **派送用 slot 1**(`account` = active slot),與 `auth_check` 的 `active: true` 一致;
  尚未發生 failover(這一輪 9 槽全綠,見 1.2 / Phase 1 補)
- **`brief` 全文內嵌在 attempt 裡**(v0.9.23 起的行為)且附 `brief_sha256` ✅
- `dispatch.status` 從 `prepared` 走到 **`accepted`**(7 秒),`artifact_ids_before` 為空
  (notebook 本來就沒有 artifact,對得上 2.5)

### 6.1b 命名鐵律:`EP{NN}. ` 前綴被正確剝掉 ✅

```json
"title": "EP01. 尾端延遲與碰撞機率",     ← RSS 用(serial 要求的形狀)
"label": "EP01 尾端延遲與碰撞機率",      ← 工作室 artifact / 回錄 source 用
```

skill 寫「`naming.episode_label` 會剝掉匹配的 `EP{n:02d}. ` / `EP{n:02d} ` 前綴」——
**實測就是這個形狀**,不是舊版的雙前綴 `EP01 EP01. …` ✅

### 6.1c 憑證殘留:三台自起 server **0/3 殘留**

Phase 4a / 5 用掉三台自起 server,各自建的目錄:

```
offsets_raw.json -> /tmp/notebooklm-mcp-auth-xb3z9hfi  0o700  27 files
s4a.json         -> /tmp/notebooklm-mcp-auth-y3dxiuza  0o700  27 files
p5bad.json       -> /tmp/notebooklm-mcp-auth-00ebkx5b  0o700  27 files
```

跑完後磁碟上**一個都不剩**,只留下兩個有主人的:

```
/tmp/notebooklm-mcp-auth-451ahwj4   ← pid 629026  -c stg(本 session)
/tmp/notebooklm-mcp-auth-ph3kbrjq   ← pid 479717  -c prd(正式節目,不准動)
```

→ 這一批 **0/3 殘留**,權限也都對(`0o700` / 27 檔)。累計到目前為止:
探索期 2 次殘留、受控 10 連跑 1/10、0.1s 寬限 3/3、30s 寬限 0/3、
Claude Code 重啟 1/1(0b.3)、本批 0/3。與 1.3 的「預設寬限下約 10~15%」相容。

### 6.1d Phase 7 的前置:封面光柵化用的 Chrome **在** ✅

```
google-chrome         /usr/bin/google-chrome
google-chrome-stable  /usr/bin/google-chrome-stable
chromium              (無)
NOTEBOOKLM_COVER_CHROME=unset
```

skill §Publish 4 要求「產封面的機器需裝 google-chrome / chromium」——**有**,
且 `notebooklm-cover` 找得到預設名稱,不必設 `NOTEBOOKLM_COVER_CHROME`。
→ Phase 7 若獲准,`cover_path` 這一項做得出來,不會卡在缺瀏覽器。

### 6.1e `artifact_download_audio` **不回寫 manifest** ℹ️

```
artifact_download_audio(notebook_id, output_path, artifact_id=None)
```

與 `artifact_download_slides` / `artifact_download_report` **不同** —— 那兩支吃
`manifest_path` + `episode_n` 並回寫路徑,audio 這支只把檔案落到 `output_path`。
→ Phase 3 的 audio 腿**對已 finalize 的集重新下載是安全的**,不會覆蓋
`mp3_path` / `sha256`。(這件事影響 Phase 3 能不能在 finalize 之後做,先確認過再跑。)

### 2.9 `notebook_list` — active slot 的可見範圍(補記)

Phase 3 找既有 artifact 時跑的:

```json
{"notebooks": [{"notebook_id": "8fd51473-…", "title": "ZZ-TEST v0.9.25-rc A"}]}
```

**只有這一本。** `auth_check` 顯示 slot 4/5/7/8/9 各自有 4/9/2/20/1 本 notebook,
但 `notebook_list` 走的是**作用中帳號(slot 1)**,而那些是別的槽位帳號自己的 notebook,
沒有分享給 slot 1 → 看不到。與 `auth_check(all_slots=true)` 的 `notebooks` 欄位不衝突,
兩者問的是不同帳號。

**對驗收流程的實際後果**:Phase 3(下載路徑,本輪第一優先)**沒有現成的已完成 artifact
可用**,必須先跑 Phase 6.1 生一集再回頭做 —— 這是 README Phase 3 §1 講的那個分支。

---

## Phase 3 — ⭐ 下載路徑(audio 腿)

### 3.1 `podcast_series` 的 finalize 下載 = 第一次實跑

manifest revision 10,`finalize.download.status = "completed"`:

```json
"remote": {"artifact_id":"48528c43-b498-4657-b6a2-09d59e0f7e7e",
           "status":"completed","status_origin":"remote",
           "observed_at":"2026-09-03T06:20:30.717805+00:00"},
"finalize": {"download": {"status":"completed",
   "path":"…/output/ep01.mp3","bytes":88016514,
   "sha256":"3f8835e1813729578b1a4b2cc14f99f2305a834f71594060135dfbf62f28eedd"}}
```

生成 14:03:22 派送 → 14:20:30 遠端完成(**17 分鐘**)→ 下載完成。

### 3.2 檔案對帳:**不是 HTML**、讀得出時長 ✅

```
$ file ep01.mp3
ep01.mp3: ISO Media, MPEG v4 system, Dynamic Adaptive Streaming over HTTP
$ head -c 16 ep01.mp3 | xxd
00000000: 0000 0018 6674 7970 6461 7368 0000 0000  ....ftypdash....
$ sha256sum ep01.mp3
3f8835e1813729578b1a4b2cc14f99f2305a834f71594060135dfbf62f28eedd     ← 與 manifest 逐字相同 ✅
$ ffprobe …
codec_name=aac  codec_type=audio
format_name=mov,mp4,m4a,3gp,3g2,mj2
duration=2734.776599        ← 45 分 35 秒,不是截斷空殼 ✅
bit_rate=257473
```

⚠️ **README 對 `file` 的期待寫窄了,但不是失敗**。README Phase 3 §2 寫「`file` 指令應報
`Audio file` / `MPEG`」—— 實際報的是 `ISO Media, MPEG v4 system, DASH`,magic 是
`ftypdash`。這**正是 skill §Publish 5 已經記載的已知形狀**:

> 發布前先 `ffprobe` 原始 `mp3_path` 確認實際格式(NotebookLM 下載常是**偽 `.mp3` 的
> fragmented MP4/AAC**;publisher 會正向辨識並正規化,不支援的格式 fail-closed)

**真正有鑑別力的兩條都通過**:(a) 檔頭不是 HTML(`grep -ci html` 對前 200 bytes 回 0),
(b) `ffprobe` 解得出 aac 音訊軌與 2734.78 秒時長。
📌 建議把 README 那句改成這兩條,別寫 `file` 的字面輸出。

### 3.3 ⭐ redirect **確實發生,而且是跨 4 個 host 的 4 跳**

用自起 server(乾淨 stderr)以 `artifact_download_audio` 重下載一次,
`httpx` 的 INFO log 攤平後抽出下載那一段的跳序:

```
302  lh3.googleusercontent.com    /notebooklm/AKYWMX-Tcld-…=m140-dv-mp2
302  lh3.google.com               /rd-notebooklm/AKYWMX_jFFtpq…=s512-m140-dv-mp2
302  lh3.googleusercontent.com    /rd-notebooklm/AKYWMX8L7BGmWDKF9RHVas…
200  drum.usercontent.google.com  /download/AGzoIKG-SkyAfuhyZS99nTLrm…    ← 真正給 bytes 的那一跳
```

**這正面回答了 README Phase 3 §4 的問題**:

- **不是 no-op 路徑** —— redirect 真的發生,而且**跨 host**(`googleusercontent` →
  `google.com` → `googleusercontent` → `usercontent.google.com`),
  所以 `_on_request` 的「先拔再套」在**每一跳**都真的執行到了。
- **README 點名的那個風險跳(`googleusercontent` 取檔)實測拿得到檔** ——
  終點 `drum.usercontent.google.com` 回 200,bytes 完整。
- **全部 4 跳都落在信任白名單內、全部 https**(程式化檢查:非白名單 host = 無、
  非 https = 無)。`drum.usercontent.google.com` 靠的是 `.google.com` 這條,
  不是 `.googleusercontent.com` —— 兩者是不同網域,白名單剛好都涵蓋。

### 3.4 兩次獨立下載**位元組相同**

| 來源 | 大小 | sha256 |
|---|---|---|
| `podcast_series` finalize(session server) | 88,016,514 | `3f8835e1…f28eedd` |
| `artifact_download_audio`(自起 server,獨立 client) | 88,016,514 | `3f8835e1…f28eedd` |

**完全一致** → 下載路徑是確定性的,不是「偶爾拿到半份」。

### 3.5 對 3.0d 預測的修正

3.0d 預測「這個組態上結構性是 no-op」。**一半對、一半要修正**:

- ✅ **對的那半**:預設政策對每個可信跳都給整份 jar,所以**結果**上與 0.8.1 等價 ——
  沒有任何一跳因為換了憑證模型而拿不到檔。
- ❌ **要修正的那半**:「no-op」不能理解成「路徑沒被走到」。實測有 **4 跳跨 4 個 host**,
  `_on_request` 每一跳都執行了 strip-then-reapply。**程式碼路徑被大量執行**,
  只是輸出恰好與舊模型相同。

→ **對下一輪的建議**:這一塊**不必再花第一優先的力氣**(行為已釘死、兩次下載位元組相同),
但**不能從測試計畫裡拿掉** —— 它每一跳都在跑,而且信任白名單就是靠這條路生效的。
降級成「跟著任一次生成順帶對帳 sha256 + 不是 HTML」即可。

---

## Phase 4b — ⭐ mp3 逐字稿的索引涵蓋範圍

### 4b.1 🎯 **索引涵蓋 mp3 逐字稿 —— 文件的「未實測」警語可以拿掉**

回錄 source(`podcast_series` 的 `_finalize_episode` 上傳的那筆):

```json
{"source_id":"f1e18d04-a24b-4ffb-b6e3-2daf9d01f4dc",
 "title":"EP01 尾端延遲與碰撞機率","kind":"media","ready":true}
```

- `kind` 是 **`media`**(0.8.2 `SourceType` 的另一個實測落點,對照 2.3 的
  `markdown` / `pasted_text`)
- 標題 = manifest 的 `label`,不是上傳時的 `expected_title: "ep01.mp3"`
  → `feedback_source_rename` 那一步實測有效 ✅

`source_search(source_ids=[那筆 mp3])` **回 27 段真實逐字稿**,不是空:

```
count = 27   rank = 1..27(無 0)
span  = 0 .. 26927,連續無縫,除最後一段外每段剛好 1000 字
       (最後一段 = (26000, 26927),對得上 source_fulltext 的 char_count 26927)
```

chunk 內容是**真的逐字稿**(含 ASR 失真的字:「通土量」=吞吐量、「食物現場」=實務現場、
「雜含函數」/「雜草函數」=雜湊函數、「生日界 Bthday Bond」=Birthday Bound)。

**本輪唯一預期可能 inconclusive 的那一條,拿到了明確的正面結論。**
`tools_basic.source_search` 的 docstring 與 skill `tool-reference.md` 現在都寫
「**索引涵蓋範圍未實測** —— 上傳的 mp3 轉錄稿搜不搜得到,還沒有實跑證據」,
📌 **要改寫成:實測涵蓋(v0.9.25-rc 驗收,45 分鐘中文 podcast,26,927 字轉錄稿
切成 27 段全部可檢索)。**

### 4b.2 ✅ 順帶把 4a.2 的 caveat 消掉:**確定是 ranker,不是有門檻的 retriever**

同一筆 mp3 source,兩個查詢:

| 查詢 | `count` | span 覆蓋 | rank 1 落在哪一段 |
|---|---|---|---|
| `"P95 P99 百分位"`(**高度相關**) | **27** | 0..26927 | (2000, 3000) ← 正是在講 P95/P99 那段 |
| `"完全無關的主題:烘焙麵包的酵母發酵時間"` | **27** | 0..26927 | (6000, 7000) |

**完全無關的查詢照樣回全部 27 段**,只有排序變了。
27 段講的是排隊理論與密碼學,**任何合理的相關度門檻都不可能讓它們全數通過
「麵包酵母發酵」這個查詢** → 4a.2 那個「有門檻但剛好全過」的替代解釋**被排除**。

另外整本不限 source 的查詢回 **38 段** = A 1 + B 1 + RFC 9 + mp3 27 ✅
(對照 4a.2 加入 mp3 前的 11 段,加法成立。)

→ **`limit` 是必要的**這條因此更硬:一本有回錄的正式節目,一集 45 分鐘就多 27 段;
十集的 notebook 不傳 `limit` 會把整季逐字稿灌回 context。

### 4b.3 🔴 4a.4 的修正:offset **對「沒有標記的來源」是對得上的**

同樣的切片實驗,對 media 逐字稿做:

```
[0:1000]     切片頭 '想 像 一 下 這 個 畫 面 ， 你 現 在 正 '
             chunk頭 '想 像 一 下 這 個 畫 面 ， 你 現 在 正 '     相符? True
[1000:2000]  切片頭 '今 天 桌 上 的 素 材 非 常 特 別 ， 表 '
             chunk頭 '今 天 桌 上 的 素 材 非 常 特 別 ， 表 '     相符? True
```

**逐字相符**,而且 span 覆蓋 0..26927 **恰好等於** `source_fulltext` 的 `char_count`。

所以 4a.4 觀察到的錯位**不是「offset 沒有意義」,而是 rendering 差異**:

| 來源型別 | chunk rendering | `source_fulltext` rendering | offset 對得上? |
|---|---|---|---|
| `media`(逐字稿,**無標記**) | 純文字 | 純文字 | **✅ 對得上** |
| `markdown`(A 佇列) | 保留 `###` / `**粗體**` | 標記拆成換行 | ❌ 差 44 字 |
| `pasted_text`(RFC 2119) | 壓掉多餘空行 | 保留原始空行 | ❌ 差 170 字 |

**📌 最終建議的文件改寫(取代 4a.4 那版)**:

> `start`/`end` 是伺服器**正規化文字**的字元 offset,同一筆來源內連續無縫,
> 可用來排序與判斷相鄰。**能不能拿來切 `source_fulltext` 的 `content`,取決於
> 那筆來源有沒有標記**:純文字/媒體逐字稿對得上,markdown 與貼上的純文字不對
> (兩邊 rendering 不同,實測差 44~170 字),而**呼叫端從回傳值分辨不出是哪一種**。
> 所以:**要原文一律直接讀 `chunk.text`,不要自己切。**

這個版本比 4a.4 那版準確 —— 它解釋了為什麼會錯位,也講清楚「不要切」的理由不是
「offset 是假的」,而是「你分辨不出這一筆會不會對」。

---

## Phase 5 — ⭐ `concept_explanation`:🔴 **在 web backend 上根本送不出去**

### 5.1 🔴 **FINDING(本輪最重要):`concept_explanation` 對我們是死選項**

```
generate_report(report_format="concept_explanation", …)
→ Error executing tool generate_report: Unsupported report format
  <ReportFormat.CONCEPT_EXPLANATION: 'concept_explanation'>;
  expected one of: briefing_doc, study_guide, blog_post, custom
```

**不是我們的閘擋的,是 SDK 的 web backend 擋的**
(`notebooklm/_web/params/artifacts.py:653`,`_report_config` → `_STATIC_REPORT_CONFIGS[…]` KeyError)。

```python
$ from notebooklm._web.params.artifacts import _STATIC_REPORT_CONFIGS
  BRIEFING_DOC / STUDY_GUIDE / BLOG_POST        ← 只有三個
  CONCEPT_EXPLANATION 在裡面嗎: False
```

**它只在 android backend 有實作**:

```
$ grep -rn "CONCEPT_EXPLANATION" <sdk>
  _android/artifact_creation.py:52:    ReportFormat.CONCEPT_EXPLANATION: (
      "Concept Explanation", "Clear explanations of key concepts", "Explain the key …")
  _types/artifacts.py:80:  "Concept Explanation": ReportFormat.CONCEPT_EXPLANATION
  _types/enums.py:338:    CONCEPT_EXPLANATION = "concept_explanation"
$ resolve_backend_preference(explicit=None, env=None)
  BackendPreference(preferred='web', reason='default')
```

而 **CLAUDE.md §一 / local-checks §8d 明確把整套釘在 web**(android 要 master token,我們沒有)。
→ **v0.9.25-rc 對外開了一個在我們唯一能用的 backend 上必定失敗的選項。**

**README Phase 5 的整個 ⭐ 目的(比對 `concept_explanation` 與 `study_guide` 的形狀)
因此執行不了** —— 它產不出任何形狀。這比「形狀跟 study_guide 太像」嚴重得多:
那是「選項是假的」,這是「選項會爆」。

### 5.2 為什麼 `local-checks.sh` §8c 綠燈:它驗的是 enum,不是 backend 的 dispatch table

```python
assert to_report_format("concept_explanation") == ReportFormat.CONCEPT_EXPLANATION   # ✅ 真
missing = {m.name for m in ReportFormat} - {v.name for v in _REPORT_FORMAT.values()}
assert not missing                                                                    # ✅ 真
a._validate_report_prompt("concept_explanation", "some prompt", None)  → ValueError    # ✅ 真
```

三條都是真的,但**沒有一條碰到 `_web.params.artifacts._STATIC_REPORT_CONFIGS`**。

⚠️ 更糟的是第二條**方向相反**:它要求「`ReportFormat` 的每個成員都要在我們的白名單裡」
—— 也就是說,**它主動要求我們開放 web backend 支援不了的格式**。
`concept_explanation` 進白名單,正是為了讓這條 assert 綠。

📌 **建議修 check**(用實際的 dispatch table 當真相來源,而不是 enum):

```python
from notebooklm._web.params.artifacts import _STATIC_REPORT_CONFIGS
supported = {f.value for f in _STATIC_REPORT_CONFIGS} | {ReportFormat.CUSTOM.value}
exposed   = set(_REPORT_FORMAT)                 # 我們對外開放的字串
assert exposed <= supported, f"開放了 web backend 送不出去的格式:{sorted(exposed - supported)}"
```

**這條 check 在現在的程式碼上會紅** —— 那正是它該做的事。

### 5.3 🔴 第二個 FINDING:純本地的 `ValueError` 被記成「遠端受理不明」

失敗那一次在 manifest 的 episode 級 `attachment_errors` 留下:

```json
{"phase": "attachment_acceptance_unknown",
 "kind": "report", "type": "ValueError",
 "message": "Unsupported report format <ReportFormat.CONCEPT_EXPLANATION…>",
 "recorded_at": "2026-09-03T06:26:52.454923+00:00",
 "account": "<slot-1>"}
```

**`attachment_acceptance_unknown` 的語意是「送出去了,但不知道遠端收了沒」** ——
那是會驅動後續對帳行為的狀態。而實測**根本沒有送出去**:

用**複製的 manifest** 重跑一次(不污染正本),抓自起 server 的 stderr:

```
POST 次數: 1
出現過的 rpcids: ['rLM1Ne']

$ RPCMethod.GET_NOTEBOOK    = rLM1Ne      ← 唯讀,failover 迴圈的 probe
$ RPCMethod.CREATE_ARTIFACT = R7cb6c      ← 一次都沒出現
```

**零生成 RPC、零配額、零遠端副作用**,`report_md_path` / `report_account` /
`report_artifact_id` 三個欄位也都沒有被建立 —— 但稽核裡寫著「受理不明」。

**而這正是程式碼明說要避免的事**(`tools_artifacts.py:680`):

```python
# 同 generate_slides:純本地轉換擋在 closure 外,否則打錯 report_format / language
# 會被記成假的「遠端受理不明」。
resolved_report_format = to_report_format(report_format)     # ← 這道護欄
```

**護欄有洞**:它只涵蓋**我們自己的** enum 轉換。`to_report_format("concept_explanation")`
**成功**(白名單有它),真正的失敗發生在更深的 SDK param builder,而那一段在 closure **裡面**。
→ 修 5.2 那條白名單就會順帶關掉這個洞;若要獨立防守,護欄要往下延伸到
「這個 backend 建不建得出 params」,不能只驗自家 enum。

### 5.4 `extra_instructions` 對靜態格式**確實有效** ✅(README Phase 5 §5)

改跑 `study_guide` 並帶一條**可在輸出裡驗證**的指令
(「在整份文件的最開頭,獨立一行,原樣輸出:【驗收標記 ZZ-9925】」):

```
$ head -3 output/ep01-report.md
【驗收標記 ZZ-9925】

# 系統效能與密碼學雜湊研究指南
```

**第 1 行,逐字相符,沒有改寫也沒有翻譯** → 靜態格式吃 `extra_instructions` 實測成立。
(對照 Phase 5 壞輸入表 #2:`custom` 格式**不吃**它,那條也已驗過會 raise。)

### 5.5 `study_guide` 的實際形狀(留作下一輪的對照基準)

```
report_md_path : output/ep01-report.md      artifact_id: ac710f0d-f300-46de-8ea4-0adc1dab911c
file           : Unicode text, UTF-8 text   ← 不是 HTML ✅
大小 / 行數    : 4,913 bytes / 63 行 / 4,913 字元
sha256         : f7c319bf91ed4fc3a04d4410427b04d3c432ffbee186c1d98593c483f7e15c72
標題結構       : H1 ×1、H2 ×4、H3 ×2
```

四個 H2 小節:

```
# 系統效能與密碼學雜湊研究指南
## 一、 核心概念分析      (### 1. 排隊系統與效能特徵 / ### 2. 密碼學雜湊與碰撞安全性)
## 二、 簡答練習題        (每題附 *參考答案*)
## 三、 進階申論題
## 四、 核心術語表        (Markdown 表格,7 個術語)
```

**study_guide 的特徵齊全**:短答題 + 參考答案、申論題、術語表 —— 與 SDK 那段
static prompt(`"key concepts, short-answer practice questions, essay prompts …
and a glossary of important terms"`)逐項對得上。
下一輪若 `concept_explanation` 修好了,這份就是判別「兩者是不是同一個東西」的基準。

---

## Phase 3(report 腿)

`generate_report` 內含的下載走同一條 `_make_download_client`:

| 對帳項 | 結果 |
|---|---|
| `file` 型別 | `Unicode text, UTF-8 text` —— **不是 HTML** ✅(`grep -ci html` 對前 300 bytes 回 0) |
| 大小 | 4,913 bytes > 0 ✅ |
| sha256 | `f7c319bf91ed4fc3a04d4410427b04d3c432ffbee186c1d98593c483f7e15c72` |
| 內容可讀 | Markdown 結構完整(H1/H2/H3 + 表格),不是截斷 ✅ |

→ **`_reject_html_download` 的判準對 Markdown 輸出也對** —— README Phase 3 §3 要問的那件事,
report 腿成立。slides 腿待 Phase 6.4。

---

## Phase 8(部分)— skill × MCP 路由(照著做)

⚠️ 這幾條**被測的是我自己**(host 照 skill 走會不會選對工具)。如實記錄實際選了什麼,
不事後修正。表格第 1 列(「拿 manifest 開新 session」)我開不了新 session,**交給使用者**,
見 8.4。

### 8.1 §Sources「`contains` 精確、`search` 語意,別互換」✅

**題目**:「這篇**有沒有出現**『協調遺漏』這個詞?」
**我選**:`source_fulltext(max_chars=0, contains=["協調遺漏","原像阻抗"])` ——
問的是「詞有沒有進到 body」,是精確子字串比對,不是語意。

```json
{"char_count":580,"hits":{"協調遺漏":true,"原像阻抗":false},"truncated":true,"content":""}
```

選對 ✅。順帶又一次確認 A/B 內容隔離(A 有佇列詞、無密碼學詞)。

### 8.2 §Sources「要『哪幾段在談 X』用 `source_search`」✅

**題目**:「**哪幾段**在講原像阻抗與碰撞阻抗的差別?」
**我選**:`source_search(query=…, limit=2)` —— 問的是位置與證據原文,是語意排序。
**而且主動加了 `limit`**,理由是 4a.2/4b.2 已經量到不加會回全部 38 段。

`rank 1` 落在 mp3 逐字稿 span **(18000, 19000)**,內容正是:

> 「…原向主抗就像是你要猜中我設定好的特定密碼…但碰撞主抗就像是的生日論,
> 我不指定特定的雜草值,只要駭克能找到任何兩組不同的輸入…顯然找碰撞容易太多太多了,
> 因為他受自於生日界。」

(ASR 失真:原像→原向/原主、碰撞阻抗→碰撞主抗、雜湊→雜草。內容推理正確。)
**語意定位準確** ✅

### 8.3 🔴 順帶抓到 4a.2 的一個實務後果:`limit=N` 回的 N 筆**不保證都相關**

同一次查詢的 `rank 2` 落在 span **(8000, 9000)** —— 那一段在講**壓測工具的突發流量陷阱**,
跟原像阻抗**毫無關係**。

因為 `source_search` 是 ranker(4a.2/4b.2):`limit=2` 的語意是
「**全部 38 段排序後取前 2**」,不是「回 2 段相關的」。當實際只有 1 段相關時,
第 2 名就是個不相關的段落,而**回傳值裡沒有任何欄位能區分**
(`rank` 只是名次,沒有分數;沒有 score / distance 欄位)。

📌 **要回寫進 skill §Sources 與 tool-reference**:

> `limit=N` 取的是**全域排序的前 N 名**,不是「N 筆相關結果」。來源裡只有 1 段在談 X 時,
> 第 2..N 筆就是不相關的段落,而回傳值分辨不出來(`rank` 是名次不是分數)。
> **一律讀 `chunk.text` 自己判斷,不要把「有回傳」當成「相關」。**

這條與 skill 現有的「`chat_ask` 不能當驗收,它會順著你的編號幻覺附和」是同一類陷阱
(工具的回傳形狀誘導呼叫端把「有東西」讀成「有答案」),值得放在一起。

### 8.4 待使用者執行的兩條

| skill 指引 | 為什麼我做不了 |
|---|---|
| §MCP Tools「已有 `series_manifest.json` → `notebook_id` 就在裡面」 | 要**開新 session、只給節目名**,看它會不會多打 `notebook_list`。我開不了新 session |
| §MCP Tools「使用者直接給了 id → 就用它」 | 同上,要一個乾淨的 host 起手式;在本 session 裡 id 早就在上下文中,測不出來 |

📌 **給使用者的執行方式**:驗收結束、`nblm-mcp` 還原之前,在本工作區
`claude` 開一個新 session,只說「用 `output/series_manifest.json` 這個節目,
把 EP01 的來源列出來」,看它是否直接從 manifest 讀 `notebook_id`(不打 `notebook_list`)。

### 8.5 ⚠️ 第 7 列的路由**現在會走進死路**

| skill 指引 | 通過條件 | 實際 |
|---|---|---|
| `generate_report` 四種靜態模板;說「不要 study_guide,要逐概念」 | 選 `concept_explanation`,不是 `custom` + 手寫 prompt | **選對了也會爆**(5.1) |

skill 的 tool-reference 與 SKILL.md 都把 `concept_explanation` 列為可用的靜態模板。
host **照著選是對的**,但 web backend 送不出去。
→ **這一列在 `concept_explanation` 修好之前無法通過**,而且**問題不在 skill 的路由指引,
在產品開放了一個 backend 不支援的選項**。修法二選一:
(a) 從 `_REPORT_FORMAT` 白名單拿掉它(並同步改 skill 文件與 CHANGELOG 的能力宣告),
(b) 等上游把它加進 `_web` 的 `_STATIC_REPORT_CONFIGS`。

### 5.6 根因與**精確修法**(不在本輪動手,留給決策)

根因寫在 `notebooklm_mcp/enums.py:54`,而且是明講出來的推理:

```python
# CONCEPT_EXPLANATION 跟 CUSTOM 同一個根因:上游一直有,白名單漏了,整個形狀對
# 呼叫端等於不存在。這一筆是 0.8.2 升級時逐項比對 enum 值才發現的(0.8.1 就漏著)。
"concept_explanation": ReportFormat.CONCEPT_EXPLANATION,
```

**推理本身有一個隱含前提:「enum 有這個成員」= 「這個能力存在」。**
在 0.8.2 把 client 拆成 web / android **之後,那個前提不再成立** ——
`ReportFormat` 是**跨 backend 共用**的 enum,而能力由**各自的 dispatch table** 決定:

| | `_web/params/artifacts.py:_STATIC_REPORT_CONFIGS` | `_android/artifact_creation.py` |
|---|---|---|
| BRIEFING_DOC | ✅ | ✅ |
| STUDY_GUIDE | ✅ | ✅ |
| BLOG_POST | ✅ | ✅ |
| **CONCEPT_EXPLANATION** | **❌** | ✅ |

`CUSTOM` 那一筆的類比是**對的**(web 有 `ReportFormat.CUSTOM` 分支);
`CONCEPT_EXPLANATION` 套用同一個類比是**錯的**。這正是「依賴大版跳躍」會產生的那種錯誤:
**舊版只有一個 backend 時,enum ≈ 能力;拆成兩個之後就不是了。**

**修法(最小,而且用現成機制)**:

1. `notebooklm_mcp/enums.py` 的 `_REPORT_FORMAT` **拿掉** `"concept_explanation"` 那一筆。
2. `tests/test_enums.py` 的 `_DECLINED`(目前是空 dict,註解寫「下次上游長出新成員時這裡
   才會再有東西 —— 而那必須是一個寫得出理由的決定」)填進去:

   ```python
   _DECLINED: dict[str, dict[str, str]] = {
       "ReportFormat": {
           "CONCEPT_EXPLANATION":
               "只在 _android backend 有 dispatch config;web 的 _STATIC_REPORT_CONFIGS "
               "沒有它,開放等於保證 ValueError。我們釘在 web(CLAUDE.md §一)。"
               "上游把它加進 _web 之後再開放。",
       },
   }
   ```
3. 刪掉 / 反轉 `tests/test_enums.py:61` 的
   `test_to_report_format_supports_concept_explanation`(它現在斷言的正是要拿掉的行為)。
4. **同步**:skill 的 `SKILL.md` §MCP Tools 與 `references/tool-reference.md`
   把 `concept_explanation` 從「四種靜態模板」的說法改回三種
   (SKILL.md `generate_report` 那一列、tool-reference 的 `generate_report` 小節),
   以及 `CHANGELOG.md` v0.9.24「配上 0.8.2 的新能力」那一節的能力宣告。
5. `local-checks.sh` §8c 換成 5.2 那條(以 backend dispatch table 為真相來源)。

**已驗證這條 check 在現在的程式碼上確實會紅,而且只命中這一筆**:

```
web backend 支援 : ['blog_post', 'briefing_doc', 'custom', 'study_guide']
我們對外開放     : ['blog_post', 'briefing_doc', 'concept_explanation', 'custom', 'study_guide']
開放但送不出去   : ['concept_explanation']
AssertionError: 開放了 web backend 送不出去的格式:['concept_explanation']
```

`briefing_doc` / `blog_post` / `study_guide` / `custom` **四個都在 web 的表裡,不受影響**
(`study_guide` 已實跑成功,見 5.4/5.5)。**影響半徑就是這一個選項。**

---

## Phase 6.2 — `podcast_episode` + `source_ids`(另一條 attempt 建立分支)

```json
{"episode":2,"title":"EP02. 反壓與原像阻抗","label":"EP02 反壓與原像阻抗",
 "artifact_id":"edb4fdc3-65ef-4823-ac69-a6d2e6505512",
 "attempt_id":"103515c3-2404-49e2-b9cd-a40ec8a18fcf",
 "mp3_path":"…/output/ep02.mp3",
 "published_at":"Thu, 03 Sep 2026 14:51:02 +0800",
 "feedback_source_id":"887eeb42-1e34-497b-8519-b7650a3e01bb"}
```

- **`source_ids` 存進 attempt settings** ✅(manifest 的 `attempts[0].settings.source_ids`
  有 3 筆 —— 對照 EP01 走 `podcast_series` 那顆是 `None`)。docstring 說
  「它與 language/format/length 一樣算生成輸入,會存進 attempt settings」實測成立。
- **`EP{NN}. ` 前綴同樣正確剝成 `label`** ✅(`EP02. 反壓與原像阻抗` → `EP02 反壓與原像阻抗`)
- 派送 14:39 → 遠端完成 → 下載完成 14:49,`published_at` 14:51:02 **晚於 EP01 的 14:22:03**
  → skill §Publish 交付清單那條「`published_at` 隨集號遞增」自然成立 ✅
- 檔案:`90,756,201` bytes、`aac` / `mov,mp4,m4a` 容器(同 EP01 的偽 `.mp3` 形狀)、
  `duration=2819.90`(47 分)、`sha256=51e1fc7799f23823aa8e5c507cff3bd210677e98b39ef5e58aa6198f741c16f0`
  —— **與 EP01 的 sha256 不同** ✅(不是把同一份檔案抄兩次)
- **`attachment_errors: 0`**、finalize 四步全 `completed`(rename / download /
  feedback_source_upload / feedback_source_rename)

→ **Phase 3 的 audio 腿在第二條入口(`podcast_episode`)上重現一次**,結果一致。

---

## Phase 6.4(進行中)+ 🎯 意外拿到的 failover 判別素材

### 6.4a 🎯 **`RateLimitError` → 換帳號重送,實測走對了那一條**

CLAUDE.md §七 說「不健康的槽位才是免費的判別實驗」,而 1.2 / Phase 1補 兩次
`auth_check` 都是 9 槽全綠 —— 本輪**本來拿不到**這個素材。

`generate_slides`(EP01)自己撞出來了:

```json
{"phase": "attachment_dispatch_failover",
 "kind": "slides",
 "type": "RateLimitError",
 "message": "API rate limit or quota exceeded. Please wait before retrying. (Upstream: Resource exhausted.)",
 "recorded_at": "2026-09-03T06:35:41.005350+00:00",
 "from_account": "<slot-1>",      ← slot 1
 "to_account":   "<slot-2>"}  ← slot 2
```

**對照 CLAUDE.md §七 的三條路表**:

| 狀態 | 應該走 | 絕對不該走 | 實測 |
|---|---|---|---|
| **配額耗盡 / 限流** | `RateLimitError` → **rotate 換帳號重送** | 當成永久失敗停下來 | ✅ **走對了** —— 換到 slot 2 繼續,工具沒有拋錯給呼叫端 |

三件同時被驗到:

1. **限流被正確辨識成 `RateLimitError`**(不是被吞成 generic error,也不是被當成認證失效)
2. **rotate 生效**:`from_account` slot 1 → `to_account` slot 2,**依槽位順序往後**,
   與 skill §Auth 說的「環狀 + 每槽位冷卻」一致
3. **稽核寫進 episode 級 `attachment_errors`**,而且 `phase` 是
   **`attachment_dispatch_failover`** —— 與 5.3 那筆誤標成
   `attachment_acceptance_unknown` 的形成對照:**這一筆的 phase 是對的**。
   → 5.3 的問題**不是三種 phase 都壞**,而是**只有「純本地例外」那一種被歸錯格**。
   換帳號這一種歸得準確。

⚠️ **這也證實了 skill §Auth 的一句話**:「`generate_slides` / `generate_report` 與
podcast 家族走同一個 failover 迴圈(v0.9.16 起)」—— slides 這一支實測有 failover。
(對照:低階 `generate_audio` **沒有** failover,那條本輪沒有素材可驗,見覆蓋率自檢。)

📌 **順帶更新 1.2 的結論**:「本輪拿不到免費的判別素材」那句話**只對了一半** ——
`auth_check` 當下全綠,但**配額是會在使用中被打完的**,而這一輪連跑
1 集 series + 1 集 episode + 1 份 report 之後,slot 1 的 slides 配額就滿了。
**「開跑前全綠」不等於「整輪都拿不到限流素材」。**

---

## Phase 6.5 — `episode_set_description` / `episode_set_publication_state`

### 6.5a 🔴 **FINDING:`chat_ask` 的伺服器推銷尾巴會直通公開 RSS**

skill §Publish 2 的配方是:

> `chat_ask(strip_citations=True, include_references=False)` 生繁中 show notes
> (約 100–150 字),**傳該集原文的 `source_ids` 聚焦** …… 再 `episode_set_description` 回寫

照著做,回傳是:

```
本集聚焦系統設計中的非線性關係。前半探討排隊系統的尾端延遲,剖析為何平均值無害而
百分位數才是真實體驗;…並釐清碰撞阻抗與原像阻抗的本質差異。
🧠 想挑戰看看自己對尾端延遲與生日界的理解嗎?我可以為這兩個主題設計一份難度適中的互動小測驗。
```

**最後那一行是 NotebookLM 伺服器自己接上去的推銷句,`strip_citations=True` 沒有清掉它**
(它不是 citation 標記,`_CITATION_RE` 與 `render()` 都不會動它)。

**兩次 `chat_ask` 兩次都出現**,只是 emoji 不同:

| 呼叫 | 參數 | 尾巴 |
|---|---|---|
| 2.7(Phase 2) | 預設(`strip_citations=False`) | `💡 **想要檢驗自己對這些排隊系統、密碼學雜湊與標準規範的掌握程度嗎?我可以為您設計一份涵蓋這些主題的隨堂測驗。**` |
| 6.5(Phase 6) | `strip_citations=True, include_references=False` | `🧠 想挑戰看看自己對尾端延遲與生日界的理解嗎?我可以為這兩個主題設計一份難度適中的互動小測驗。` |

**為什麼這件事嚴重**:skill 的配方是「`chat_ask` 的輸出 → `episode_set_description`」,
中間**沒有任何一步會攔下這句話**。`episode_set_description` 的 preflight 只驗
「非空、不等於標題、渲染後自包含」—— 一句推銷句三條全過。
→ **host 若照 skill 直通,推銷句會出現在 Apple Podcast 的單集簡介裡。**

我實際的處置:**手動剝掉最後一行**再傳給 `episode_set_description`。
回傳 `{"episode":1,"description":"…本質差異。","stripped":true}` —— 乾淨。

📌 **要回寫進 skill §Publish 2**:

> ⚠️ `chat_ask` 的回答結尾**常被伺服器接上一句自我推銷**(「我可以為您設計一份測驗」
> 之類,前面帶 💡 / 🧠 等 emoji)。`strip_citations=True` **清不掉它**(那不是 citation),
> `episode_set_description` 的 preflight 也攔不住(非空、不等於標題、自包含三條它都過)。
> **回寫前自己看最後一行,把它刪掉**,否則它會出現在公開 feed 的單集簡介裡。
> 實測 2/2 次都出現。

(這與 skill 既有的「`chat_ask` 不能當驗收,它會順著你的編號幻覺附和」是同一家族:
**`chat_ask` 的輸出不能直接當成成品**。)

### 6.5b `episode_set_description` ✅

manifest 前後差異(revision 31 → 32):EP01 多出 `description` 欄位,
內容 = 傳入值,`stripped: true`。preflight(非空 / 不等於標題 / 自包含)全過。

### 6.5c `episode_set_publication_state` ✅

對 **EP02** 標記 `deferred`(它要留給 Phase 6.3 的 retract 流程,不該進 feed):

```json
{"episode":2,"publication_state":"deferred","previous_state":null,
 "reason":"v0.9.25-rc 驗收:EP02 保留作 retract 流程的素材,刻意不進 feed",
 "changed":true,"withheld_from_publish":true}
```

manifest 落下三個欄位:`publication_state` / `publication_state_reason` /
`publication_state_changed_at`(docstring 說「`state=None` = 解除,三個欄位一起移除」)。

- `previous_state: null` 正確(這集之前沒有被標記過)
- **回傳刻意沒有 `has_output`** —— docstring 解釋了為什麼刪掉那個欄位
  (四種情形都會說謊)。實測回傳確實沒有它 ✅
- `withheld_from_publish: true` 明確告訴呼叫端「`publish_series` 會跳過它」

→ **這也讓 Phase 7 的形狀變乾淨**:若獲准發布,feed 應該只有 EP01,
且回傳的 `deferred_episodes` 要含 `2`。那是一個可對帳的預測。

---

## Phase 6.7 — `research_start` / `research_wait` / `research_import`

依 skill §Research **鐵律**跑在拋棄式 scratch notebook,不跑在 episode notebook:
`ZZ-TEST v0.9.25-rc research-scratch` = **`6cf99fb6-1e1a-4fb2-b5d0-a1b3a548dec6`**

### 6.7a 🎯 免費旁證:輪替游標**真的**移到 slot 2 了

建 scratch notebook 時回傳的 `shared_with`:

```
<slot-1>          ← slot 1 出現在「被分享」名單裡
<slot-3>  …      ← slot 3..9
(沒有 <slot-2> = slot 2)
```

對照 2.1(EP01 那本):`shared_with` 含 slot 2..9、**不含 slot 1**,因為當時 owner 是 slot 1。
→ **這一本的 owner 是 slot 2** = 作用中帳號已從 slot 1 換成 slot 2。
原因就是 6.4a 的 slides `RateLimitError` failover 推動了游標。

`research_start` 的回傳也直接印證:

```json
{"task_id":"bc1de320-e2fe-4f26-a6f5-69f3bc774907","report_id":null,
 "query":"…","mode":"fast","source":"web",
 "account":"<slot-2>"}      ← slot 2
```

**三個獨立訊號互相對得上**(`attachment_errors` 的 `to_account`、
`notebook_create` 的 `shared_with` 缺誰、`research_start` 的 `account`)。

### 6.7b `research_start` → `research_wait`:`account` 原樣傳回 ✅

docstring 的硬規則:「`account` 也要一起落地,並原樣傳回給 `research_wait` /
`research_import`」—— **照做了**,兩支都傳 `account="<slot-2>"`。

`research_wait(timeout=600)` 回:

```json
{"status":"completed","report":"","report_chars":0,"report_truncated":false,
 "report_importable":false,"cited_url_count":0,
 "summary":"This selection of technical resources provides engineering insights into
            measuring tail latency and implementing backpressure in distributed systems.",
 "candidates":[…10 筆…]}
```

- **`report_importable: false` / `report_chars: 0` / `cited_url_count: 0`** ——
  與 docstring 一致(`mode="fast"` 不產報告,只有 `deep` 會)✅
- `max_report_chars` 用預設 **0**,所以 `report` 是空字串而不是把報告灌回 context ✅
- 10 筆候選的 `cited` **全部 false** —— 沒有報告就沒有引用,自洽 ✅
  (📌 這也說明「`cited` 只在 deep 模式有判別力」,fast 模式下它恆為 false,
  不能拿它當篩選依據。skill §Research 的 `cited` 語意說明在 references/research.md,
  本輪沒有讀進來對帳,**標為未驗**。)

### 6.7c `research_import`:只收指名的,而且工具自己指引對帳 ✅

```json
{"imported":[{"source_id":"e4de9949-…","title":"On Coordinated Omission | ScyllaDB"},
             {"source_id":"59da3b41-…","title":"Everything You Know About Latency Is Wrong | Brave New Geek"}],
 "requested":2,
 "note":"回傳筆數可能少於實際匯入;用 source_list 對帳並確認 ready"}
```

**照著 `note` 的指引執行 `source_list`**(不是讀過覺得對):

```json
[{"source_id":"59da3b41-…","title":"Everything You Know About Latency Is Wrong | Brave New Geek",
  "kind":"web_page","ready":true},
 {"source_id":"e4de9949-…","title":"On Coordinated Omission | ScyllaDB",
  "kind":"web_page","ready":true}]
```

- **剛好 2 筆,沒有多**(10 個候選裡只有指名的 2 個進來)→ docstring 的
  「沒指名的一律不進來」實測成立 ✅
- 兩筆都 `ready: true` ✅
- **`requested: 2` 與實際落地 2 筆一致**,`note` 提醒的落差這次沒發生

### 6.7d ℹ️ `kind: "web_page"` —— 順帶解掉 2.3 留的疑問

2.3 記過「URL 來源的 `kind` 是 `pasted_text`,不是某種 url/website 型別」,
並謹慎地沒有下通則。這裡拿到了對照:

| 來源 | 加入方式 | `kind` |
|---|---|---|
| `rfc2119.txt`(純文字 URL) | `source_add_url` | **`pasted_text`** |
| ScyllaDB / Brave New Geek(HTML 頁) | `research_import` | **`web_page`** |
| `a-queue.md`(本機檔) | `source_add_file` | `markdown` |
| B 雜湊(貼上文字) | `source_add_text` | `markdown` |
| EP01 回錄 mp3 | `_finalize_episode` | `media` |

→ **`pasted_text` 那一筆是「純文字 URL」的特例**,不是「URL 來源一律 pasted_text」。
2.3 的節制是對的。**五種 `SourceType` 值實測落點都記下來了,沒有一筆是 `google_drive`**
(README 要盯的 code-14 改判始終沒被踩到)。

---

## Phase 6.3 — QA 拒收正門(retract → source_delete → artifact_rename)

**照回傳值逐筆執行,不自己拼**(README 的「照著做」要求)。

### 6.3a `podcast_attempt_retract`(EP02,`103515c3…`)

`dispatch_status_at_retraction: "accepted"` / `remote_status_at_retraction: "completed"` /
`authorization_basis: "output_owner"` → **依 docstring 屬於「已經是該集 output_attempt_id
的正式輸出」那一格,不需要 `abandon_in_flight`**。實測回傳 `abandon_in_flight: false`、
`source_cleanup_unresolved: false`,與判斷一致 ✅

回傳的可執行身分(全部來自同一份回傳,沒有去翻 manifest、沒有用 `artifact_list` 自己挑):

```json
"observed_state": "retracted",
"safe_next_action": "source_delete",
"source_cleanup_obligations": [{"source_id":"887eeb42-…","notebook_id":"8fd51473-…"}],
"stale_artifact_id": "edb4fdc3-65ef-4823-ac69-a6d2e6505512",
"regeneration_source_ids": ["2e1542ad-…","a146e709-…","f1e18d04-…"],
"safe_next_attempt_id": null, "safe_next_artifact_id": null
```

`next_step` 甚至把指令寫成可貼的形式,並主動警告:

> **重生時必須帶回原本那組 `source_ids`** …… 這一集的生成輸入指名了來源,
> 改用 `podcast_series` 會靜默改成讀整本筆記本。

→ **CLAUDE.md §五 / skill「retract 後一律用 `podcast_episode` + `source_ids`」那條鐵律,
工具自己在回傳裡講了**,不必靠人記得 ✅

### 6.3b 照 `source_cleanup_obligations` 逐筆 `source_delete` ✅

```json
{"deleted":"887eeb42-1e34-497b-8519-b7650a3e01bb","was_present":true}
```

`was_present: true` → 真的刪到那筆存在的回錄 source(不是「本來就不在」的空過)。
obligations 只有一筆,已清完。

### 6.3c `artifact_rename` 標記作廢 ✅

```json
{"artifact_id":"edb4fdc3-…","title":"✗作廢 EP02 反壓與原像阻抗"}
```

`artifact_list` 覆核:那顆 audio 的 title 確實變成 `✗作廢 EP02 反壓與原像阻抗` ✅
(MCP 沒有 artifact 刪除工具,所以標記是唯一能讓它在 Studio 裡與現用版區分的手段。)

### 6.3d 🔴 **FINDING:配額 failover 在雲端留下一顆孤兒 artifact**

`artifact_list` 撈到 **兩顆 slide_deck**:

```json
{"artifact_id":"159cb274-b547-4339-8b98-8e156db33019","kind":"slide_deck",
 "status":"pending",     "completed":false, "created_at":"2026-09-03T06:35:40+00:00"}
{"artifact_id":"8f936010-6b1a-4517-9097-1a34a7a00ef9","kind":"slide_deck",
 "status":"in_progress", "completed":false, "created_at":"2026-09-03T06:35:42+00:00"}
```

對照 6.4a 的 failover 稽核時間 **06:35:41**:

```
06:35:40  159cb274 建立     ← slot 1 的那一次派送
06:35:41  RateLimitError → failover  slot 1 → slot 2
06:35:42  8f936010 建立     ← slot 2 的重送
```

**所以「配額被拒」那一次派送並不是零遠端副作用** —— 它在 notebook 裡留下了一顆
`pending` 的 slide_deck。工具只追蹤重送成功那一顆(`8f936010`),
`159cb274` 沒有任何欄位記著它,也沒有工具會去收它。

⚠️ **這與 `podcast_attempt_retract` docstring 對音檔家族講的「遠端那個 artifact 會成為孤兒」
是同一類問題,但附件家族連 attempt 記錄都沒有**(ADR-0011 明示這是既有架構債),
所以孤兒**完全不可追蹤**:`attachment_errors` 只記了 `from_account`/`to_account`,
沒記被丟掉那顆的 `artifact_id`。

📌 **建議**(列給決策,本輪不修):`attachment_dispatch_failover` 這筆稽核**多記一個欄位**
—— 被放棄那一次派送的 `artifact_id`(若拿得到)。理由與 ADR-0010 §Transparency 同源:
「哪個帳號被拒過」已經記了,「被拒那次在雲端留下什麼」現在查不出來,
而 Studio 裡會累積看不懂的 `pending` 卡片。

ℹ️ **待觀察**:`159cb274` 會不會自己走到 `failed`(那樣至少 `artifact_retry_failed`
有素材),或永遠停在 `pending`。見 6.6。

### 6.3e ℹ️ `artifact_list` 的 `source_ids` 觀測欄位:本樣本**全部正確**

docstring 說它是 0.8.1 的觀測欄位、「尚未在生產資料驗收,**不得拿它當 gate**」。
本輪五顆 artifact 的 `source_ids` 與實際傳入值逐筆比對:

| artifact | kind | `source_ids` | 實際傳入 | 對? |
|---|---|---|---|---|
| `48528c43` EP01 audio | audio | A, B, RFC | `podcast_series` 不指名 → 當時整本 3 筆 = A,B,RFC | ✅ |
| `edb4fdc3` EP02 audio | audio | A, B, mp3-EP01 | 我傳的 3 筆 | ✅ |
| `ac710f0d` Study Guide | report | A, B | 我傳的 2 筆 | ✅ |
| `159cb274` / `8f936010` slides | slide_deck | A, B | 我傳的 2 筆 | ✅ |

**5/5 正確。** 這是一個正面樣本(可以收進離線測試的 fixture),
但**不足以解除「不得當 gate」那條** —— 樣本只有一本 notebook、沒有涵蓋
retract / 重生 / 手動在網頁上建的 artifact。**維持現狀的紀律。**

ℹ️ 另記:report artifact 的 title 是 SDK static config 的 **`"Study Guide"`**,
不是集標 —— 與音檔 artifact(用 `label`)不同。做 Studio 清理時要知道這個差異。

---

## Phase 6.6 — `artifact_retry_failed`:**有自然素材了**(原本預期要寫「刻意不測」)

### 6.6a 簡報生成**自然失敗** —— 這是免費素材,不是我製造的

`generate_slides` 在 failover 到 slot 2 之後跑了 24 分鐘,然後:

```
Task failed: Error executing tool generate_slides: Generation failed while waiting: failed
```

`artifact_list(kind="slide_deck")` 覆核兩顆的終態:

```json
{"artifact_id":"8f936010-…","status":"failed",  "created_at":"06:35:42"}   ← slot 2 的重送,失敗
{"artifact_id":"159cb274-…","status":"pending", "created_at":"06:35:40"}   ← slot 1 被限流那顆,仍 pending
```

→ **6.3d 的孤兒確認了**:`159cb274` 在 24 分鐘後仍是 `pending`,不會自己走到 `failed`,
沒有任何工具追蹤它。**這顆會永久留在 Studio 裡。**

→ **`generate_slides` 把伺服器端失敗吞成 `failed` status 再由 `artifact_wait` 報出**,
與 `artifact_retry_failed` docstring 說的對比(「SDK 對伺服器端的同步拒絕是 **raise**,
不像 `generate_*` 吞成 failed status」)一致 ✅

### 6.6b `artifact_retry_failed` —— `artifact_id` 不變 ✅

```json
{"task_id":"8f936010-6b1a-4517-9097-1a34a7a00ef9",
 "artifact_id":"8f936010-6b1a-4517-9097-1a34a7a00ef9"}
```

**`task_id` == `artifact_id` == 原本那顆** → docstring 說的「重用同一個 artifact,
省配額;舊路徑是刪掉重生等於再花一次生成配額」實測成立。
照 docstring 指引接 `artifact_wait(task_id=…)`(不自己輪詢 `artifact_list`)。

---

## Phase 7(前置)— feed 身分與封面

### 7.0a `feed_info` — 純計算,零寫入 ✅

```json
{"show_id":"zz-test-v0925rc",
 "token":"kc5jpt72xydkfevg7w673lie",
 "feed_url":"https://podcast.example.com/feeds/FEED_TOKEN/feed.xml",
 "show_page_url":"https://podcast.example.com/feeds/FEED_TOKEN/index.html"}
```

token = HMAC(salt, show_id)。**CLAUDE.md §四 的隔離在這裡看得見**:salt 來自
`-c stg`,所以這個 token 與正式節目的 URL 空間完全不相交,不會互撞。
(docstring:「the MCP keeps no state, so per-episode detail is NOT returned」——
回傳確實只有身分四欄,沒有集層資訊 ✅)

### 7.0b 🎯 `notebooklm-cover` 的品牌護欄**擋對了**,而且訊息可執行

第一次呼叫(不加旗標)被**拒絕**:

```
拒絕產封面:節目 'ZZ-TEST v0.9.25-rc' 不是 Audicast。cover_show.html /
cover_episode.html 這兩個模板含寫死的 Audicast 品牌內容(副標「AI AGENTIC
ENGINEERING」與裝飾用的 audicast@dev-env / TypeScript 程式碼區塊),不適用於
其他節目 —— 照產會把 Audicast 品牌印上你的封面,而**發布端驗證只驗尺寸與色彩空間,
擋不住**。確定要沿用 Audicast 視覺請加 --allow-audicast-branding。
```

**這是一個做對的 fail-loud**:它守的正是下游驗不到的那一段(Apple 驗證器只看
尺寸 / 色彩空間 / 透明通道,看不出品牌印錯)。
依 CLAUDE.md「遇到工具訊息給的指引就照著執行」,加上它指名的旗標
(本輪是使用者自己的品牌 + 拋棄式 feed,沿用可接受)。

### 7.0c 三張封面都過 Apple 硬規格 ✅

```
output/covers/show.jpg   3000x3000  JPEG baseline  components 3(RGB)  518,065 bytes
output/covers/EP01.jpg   3000x3000  JPEG baseline  components 3(RGB)  464,590 bytes
output/covers/EP02.jpg   3000x3000  JPEG baseline  components 3(RGB)  446,411 bytes
Manifest updated: output/series_manifest.json      ← cover_path 已回寫
```

對照 skill §Publish 4 的硬規格:**正方形 ✅ / 1400–3000px ✅(剛好上界)/
JPG ✅ / RGB ✅(`components 3`,不是 CMYK 的 4)/ 無透明通道 ✅(JPEG 本身沒有 alpha)**

ℹ️ EP02 雖然已標 `deferred`,批次模式仍為它產了封面並回寫 `cover_path` ——
`notebooklm-cover` **不看 `publication_state`**。不是缺陷(封面是本機產物,
發布端才會跳過那一集),但值得知道:deferred 的集數也會佔一張封面檔。

### 6.3f manifest 的 retract 後形狀,以及 `pending_source_cleanup` 的**設計性殘留**

EP02 被清掉的欄位(retract 前有、後沒有):

```
mp3_path / artifact_id / output_attempt_id / published_at
feedback_source_id / active_attempt_id
```

新增的欄位:

```json
"previous_feedback_source_ids": ["887eeb42-1e34-497b-8519-b7650a3e01bb"],
"pending_source_cleanup":       [{"source_id":"887eeb42-…","notebook_id":"8fd51473-…"}],
"retracted_attempt_ids":        ["103515c3-2404-49e2-b9cd-a40ec8a18fcf"]
```

attempt `103515c3` 本身沒被刪,多了一個 `retraction` key(tombstone),
`brief` / `brief_sha256` / `settings` / `dispatch` / `remote` / `finalize` 全部留著 →
**audit 完整保住** ✅(這正是 `publication_state=deferred` 那個欄位存在的理由)。

⚠️ **`pending_source_cleanup` 在我照 obligations 做完 `source_delete` 之後仍然列著那一筆。**

先讀碼再判(不燒一次生成去試),結論是**設計如此,不是漏清**:

- `source_delete` 是 **generic source 管理能力**,它的 docstring 明說
  「不代表可覆寫 manifest-backed completed episode」—— 它**刻意不知道 manifest 的存在**。
- 義務由**下一次生成前的 gate** 結案:`tools_podcast.py:2178` 那段 docstring 寫
  「(1) `pending_source_cleanup` —— 身分已確定的 id,**驗『已經不在 notebook』**」,
  實際清除動作是 `_drop_cleanup_obligations`(:3427,「清掉**這次真的查過、確認不在**
  的那幾筆,逐筆比對,不整欄 pop」),跑在那次 `sources.list` 掃描的同一個 CAS mutation 裡。
- 那段 docstring 甚至**預先講出了我看到的這個現象**:
  > 「已經刪掉的 id 會卡在 `pending_source_cleanup` 裡(因為這一輪先在 waiting 那裡拋了),
  > 而 `podcast_attempt_retract` 的冪等回傳會繼續說 `source_delete`,指向一個早就不存在的東西」

📌 **仍值得記的一件事**:在 **retract 之後、下一次生成之前**這段窗口裡,
**host 從 manifest 分辨不出「已經刪掉」與「還欠著」**。兩者的 `pending_source_cleanup`
長得一樣。這在本輪無害(gate 會擋),但若有人拿 manifest 當狀態儀表板就會誤讀。
**不建議改行為**(gate 對帳才是唯一可信的判準,而 `source_delete` 不該碰 manifest);
若要改善,改的是**文件**:在 `podcast_attempt_retract` 的回傳或 skill 的 QA 流程裡
講明「`source_delete` 完成後 `pending_source_cleanup` **不會**當場消失,
它由下一次生成的 preflight 對帳結案」。

### 6.6c `artifact_retry_failed` 生效,但這顆簡報**兩次都沒生完**

retry 後狀態確實從 `failed` → **`in_progress`**(`artifact_list` 覆核)→ **工具有效** ✅

但 `artifact_wait(timeout=900)` 逾時:

```
Error executing tool artifact_wait: Task 8f936010-… in notebook 8fd51473-… timed out
after 900.0s (last status: in_progress; status history: in_progress)
```

逾時訊息帶 `last status` 與 `status history`,能分辨「沒動過」與「動了但沒完成」——
這裡是 `in_progress` 全程,**遠端在跑、只是慢**(不是又失敗)。

**累計時序**(同一顆 slide_deck):

```
06:35:42  slot 2 建立(failover 後的重送)
~07:00    generate_slides 的 wait_timeout 內回報 failed
07:0x     artifact_retry_failed → in_progress
+900s     artifact_wait 逾時,仍 in_progress
```

ℹ️ 這本 notebook 的 slide_deck 生成**在真實環境下不穩** —— 一次 failed、一次 15 分鐘
未收斂。**不是 v0.9.25-rc 的回歸**(生成在伺服器端,我們只負責派送與等待),
但它讓 Phase 3 的 **slides 腿在本輪拿不到成品**。

`artifact_revise_slide` 連帶**無素材**:它要求「**已生成**簡報」的 `artifact_id`,
而這顆從未 `completed`。→ **刻意不測,理由是沒有已完成的簡報可改**(README 6.6 允許)。

---

## Phase 7 — 發布(使用者已核准:完整發布 + read-back)

### 7.1 🎯 判別實驗:**fail-closed 交付閘在任何上傳之前就 raise** ✅

先**不傳** `require_slides=False` 直接發布:

```
Error executing tool publish_series: episode 1: slides_pdf_path 未回寫(簡報可能還在
生成中)。等 generate_slides/generate_report 回寫後再發布;使用者明講整季不做這項
才傳 require_slides=False
```

**「在任何上傳之前」這句話實測成立** —— 立刻對公網讀回:

```
$ curl -o /dev/null -w '%{http_code}' https://podcast.example.com/feeds/FEED_TOKEN/feed.xml
404
$ …/index.html
404
```

**零 PUT、零 blob 上傳。** skill §Publish 3 說的
「`publish_series` 預設要求兩者齊備,路徑沒回寫就在任何上傳之前 raise,
**不會靜默 fallback**」—— 用 404 直接證明了,不是只信工具回傳 ✅

而且錯誤訊息**指名了是哪一集哪一項**(`episode 1: slides_pdf_path`),
並直接給出兩條出路(等回寫 / 使用者明講不做才傳旗標)—— 可執行 ✅

ℹ️ 同時證明 `require_report` 那一側**已經滿足**(`report_md_path` 在 5.4 回寫了),
所以 gate 只點 slides 一項,沒有連帶誤報 report。

### 6.3g 狀態機其餘三個入口:守門與冪等各驗一次

本輪**沒有自然的 `acceptance_unknown` 狀態**(EP01 完整、EP02 已 retract),
所以這三支測的是**守門正確性**與**冪等性**,而不是救援路徑本身。
依 CLAUDE.md §五 的紀律,**沒有為了湊覆蓋率去人為製造壞狀態**。

**`podcast_episode_reconcile`(對 EP02 的 tombstone attempt)** —— 正確拒絕 ✅

```
Error: attempt '103515c3-…' was retracted (episode 2);
       it is history — work on the replacement attempt instead
```

與 `podcast_attempt_retract` docstring 的「已被 supersede 的**歷史** attempt ——
誰都動不了它,旗標傳了也一樣被拒;工具會告訴你現在真正的 active/output 是哪顆」一致。

**`podcast_attempt_adopt`(不給任何 id)** —— 正確拒絕 ✅

```
Error: provide exactly one non-empty artifact_id or feedback_source_id
```

docstring 說「必填 `feedback_source_id` 或 `artifact_id` 之一」——
而且是 **exactly one**(不是「至少一個」),訊息講得比 docstring 更精確。

**`podcast_episode_resume`(對 EP01 已完成的集)** —— **冪等實測成立** ✅

這一支刻意選在 **EP01(已完成)** 而不是 EP02:docstring 承諾
「各 finalize 步驟皆依 checkpoint 與 postcondition 冪等接續,
**重複呼叫不會新增第二筆 source**」,那正是值得驗的性質。
(**沒有**對 EP02 跑 resume —— 它的 artifact 還在雲端,若守門不足會重新下載並
**重建**我剛按 obligations 刪掉的回錄 source,把清理義務復活。風險不值得。)

回傳與原本逐字相同:

```json
{"episode":1,"title":"EP01. 尾端延遲與碰撞機率","label":"EP01 尾端延遲與碰撞機率",
 "task_id":"48528c43-…","artifact_id":"48528c43-…",
 "mp3_path":"…/output/ep01.mp3",
 "published_at":"Thu, 03 Sep 2026 14:22:03 +0800",     ← 沒有被刷新成現在
 "attempt_id":"b994fa15-…",
 "feedback_source_id":"f1e18d04-…"}                    ← 同一筆,不是新的
```

**副作用對帳(呼叫前後各一次 `source_list`)**:

| | 前 | 後 |
|---|---|---|
| source 筆數 | **4** | **4** |
| 回錄 source | `f1e18d04` | `f1e18d04`(同一個) |

**沒有新增第二筆 source** ✅,`published_at` 沒被刷新 ✅,`attempts` 仍是 1 顆 ✅
→ checkpoint 冪等的承諾在真帳號上成立。

---

## 覆蓋率自檢(誠實版)

README〈收工〉§0 的 `grep -q "$t" FINDINGS.md` **37/37 全過**
(已依 0b.7 用 `sed '/^### 0b.1/,/^### 0b.2/d'` 排除那段工具清單,不是假通過)。

**但 grep 只證明「被提到」,不證明「被呼叫」。** 逐支對帳實際呼叫紀錄:

### 實際呼叫過的 33 支

| 工具 | 在哪一節 |
|---|---|
| `auth_check` | 1.2 / Phase 1補 |
| `notebook_create` | 2.1 / 6.7a |
| `notebook_get` | 2.6 |
| `notebook_list` | 2.9 |
| `notebook_share_with_pool` | 2.8 |
| `source_add_file` | 2.2 |
| `source_add_url` | 2.2 |
| `source_add_text` | 2.2 |
| `source_list` | 2.3 / 4b.1 / 6.3g / 6.7c |
| `source_fulltext` | 2.4 / 4a.4 / 4b.1 / 8.1 |
| `source_search` | 4a.1–4a.6 / 4b.1–4b.3 / 8.2 |
| `source_delete` | 6.3b |
| `artifact_list` | 2.5 / 6.3c–6.3e / 6.6a / 6.6c |
| `artifact_rename` | 6.3c |
| `artifact_retry_failed` | 6.6b |
| `artifact_wait` | 6.6b / 6.6c |
| `artifact_download_audio` | 3.3 / 3.4 |
| `chat_ask` | 2.7 / 6.5a |
| `podcast_series` | 6.1 |
| `podcast_episode` | 6.2 |
| `podcast_episode_reconcile` | 6.3g |
| `podcast_attempt_adopt` | 6.3g |
| `podcast_attempt_retract` | 6.3a |
| `podcast_episode_resume` | 6.3g |
| `generate_report` | 5.1 / 5.4 / Phase 5(部分) |
| `generate_slides` | 6.4a / 6.6a |
| `episode_set_description` | 6.5b |
| `episode_set_publication_state` | 6.5c |
| `research_start` | 6.7b |
| `research_wait` | 6.7b |
| `research_import` | 6.7c |
| `feed_info` | 7.0a |
| `publish_series` | 7.1(gate raise)+ 7.2(實際發布) |

### 🔴 **沒有實際呼叫的 4 支 —— 逐支寫明理由**

| 工具 | 為什麼沒測 | 這是可接受的嗎 |
|---|---|---|
| **`generate_audio`** | 低階單集入口,**沒有 failover、沒有 `manifest_path`**(skill §Auth 明記)。本輪的音檔覆蓋走 `podcast_series` 與 `podcast_episode` 兩條高階入口,它們踩的是同一個 SDK 傳輸層與同一條下載路徑 —— 本輪要驗的地基**已被覆蓋**。單獨測它只會多燒一次音檔配額,換到「低階入口也能生成」這個與本版改動無關的資訊 | ✅ **刻意不測**。⚠️ 但要知道:troubleshooting 對 `acceptance_unknown` 死結建議的備援路徑建在它上面,而它**沒有** failover —— 那條救援路本輪仍未實測(與 v0.9.3 FINDING-4 同一個未結案前提) |
| **`artifact_revise_slide`** | 它要求**已生成**簡報的 `artifact_id`。本輪唯一那顆 slide_deck 兩次都沒 `completed`(6.6a / 6.6c)—— **無素材** | ✅ **無素材,刻意不測**(README 6.6 明文允許)。不為湊覆蓋率去製造 |
| **`artifact_download_slides`** | 同上:沒有已完成的簡報可下載。Phase 3 的 **slides 腿因此在本輪缺口** | ⚠️ **真缺口**。audio 腿(3.2–3.4)與 report 腿(Phase 3 report 腿)都驗過了,但 `_reject_html_download` 對 **PDF** 的判準**本輪沒有實測** |
| **`artifact_download_report`** | 講義的下載走的是 `generate_report` 內含的那一段(同一條 `_make_download_client`),**這一支是 client timeout 丟掉結果時的救援入口**,本輪沒有發生那種中斷 | ✅ **無自然入口,刻意不測**。下載路徑本身已由 report 腿覆蓋(同一個 code path,只差入口) |

**結論**:33/37 實跑,4 支寫明理由。**唯一的實質缺口是 PDF 下載型別判定**
(`artifact_download_slides` + `_reject_html_download` 對 PDF)——
下一輪若簡報生成正常,那一條要補。

### 7.2 實際發布(使用者核准)

⚠️ **傳了 `require_slides=False`,而這是驗收權宜,不是季級政策決定。**
理由:那顆 slide_deck 在伺服器端**三次都沒生出來** ——
slot 1 被限流(留下孤兒 `pending`)、slot 2 `failed`、`artifact_retry_failed` 後
`in_progress` 再 `failed`。skill 說這個旗標是「使用者明講整季不做這項才傳」,
本輪用它是為了讓 `publish_series` / read-back 這條路測得到。
📌 **這個旗標會存進 manifest 沿用**(季級政策)—— 本輪是拋棄式 manifest 所以無害,
但**正式節目不可以這樣用來繞過生成失敗**。

```json
{"feed_url":"https://podcast.example.com/feeds/FEED_TOKEN/feed.xml",
 "show_page_url":"…/index.html",
 "token":"kc5jpt72xydkfevg7w673lie",
 "episode_count":1,
 "deferred_episodes":[2],
 "sha_unverified_episodes":[],
 "episodes":[{"n":1,"title":"EP01. 尾端延遲與碰撞機率",
   "guid":"1994a7e4d0ffd849e7c7ce621e7def5c780d8899",
   "url":"…/EP01-9a6eade1.mp3",
   "cover_url":"…/EP01-cover-fa228cad.jpg",
   "pdf_url":null,                              ← require_slides=False 的可見後果
   "html_url":"…/EP01-bd42e34c.html",
   "duration":"00:45:35"}]}
```

**6.5c 的三個預測全部命中** ✅:`episode_count: 1` / `deferred_episodes: [2]` /
`token` 與 `feed_info`(7.0a)算出的**逐字相同**(`kc5jpt72xydkfevg7w673lie`)
→ 「deterministic: same show_id → same URL/token」實測成立。
`sha_unverified_episodes: []` = mp3 provenance 閘全過。

### 7.3 🎯 公網讀回驗證(**從 feed 解析,不是吃工具回傳**)

skill §Publish 6 / publish-verify.md 的判準逐條:

#### 7.3a feed.xml 結構(`ET.parse` 解析,非 grep)

```
HTTP 200  application/rss+xml; charset=utf-8  3087B     ← 發布前是 404(7.1)
channel title    : ZZ-TEST v0.9.25-rc
itunes:type      : serial                ← ⭐ 連載宣告在,集序不會被 Apple 倒排
itunes:author    : audichuang
channel image    : …/artwork-c9ecf1a1.jpg
item 數          : 1                      ← EP02 deferred,確實不在 feed ✅
  title          : EP01. 尾端延遲與碰撞機率   ← EP{NN}. 前綴在,集號對得上
  itunes:episode : 1
  guid           : 1994a7e4d0ffd849e7c7ce621e7def5c780d8899
  pubDate        : Thu, 03 Sep 2026 14:22:03 +0800
  enclosure url  : …/EP01-9a6eade1.mp3   ← .mp3 副檔名 ✅
  enclosure type : audio/mpeg            ✅
  enclosure length: 87979478
  itunes:duration: 00:45:35
  item image     : …/EP01-cover-fa228cad.jpg
  description    : 235 字,**沒有 6.5a 那句推銷尾巴** ✅(我手動剝掉了)
```

#### 7.3b ⭐ **publisher 的正規化實測成立** —— 這是最有價值的一條

| | 本機 `output/ep01.mp3` | 公網 enclosure |
|---|---|---|
| 大小 | **88,016,514** B | **87,979,478** B |
| `file` | `ISO Media, MPEG v4 system, DASH` | `Audio file with ID3 v2.3.0, MPEG ADTS, layer III, v1, 256 kbps, 44.1 kHz, Stereo` |
| `format_name` | `mov,mp4,m4a,3gp,3g2,mj2` | **`mp3`** |
| `codec_name` | `aac` | **`mp3`** |
| `duration` | 2734.7766 | 2734.8114 |

**本機是偽 `.mp3` 的 fragmented MP4/AAC,公網那份是真正的 MP3。**
skill §Publish 5 說「publisher 會正向辨識並正規化,不支援的格式 fail-closed」——
**實測就是這件事**,而且時長只差 0.035 秒(轉檔誤差),沒有截斷。
ℹ️ `ffprobe` 另報 `codec_name=mjpeg / codec_type=video` —— 那是內嵌的 ID3 封面圖,正常。

#### 7.3c HTTP range 與全檔 decode

```
$ curl -r 0-0        → HTTP/2 206   content-range: bytes 0-0/87979478   content-type: audio/mpeg
$ curl -r 100-199    → HTTP 206     下載 100B
$ curl(整檔)         → HTTP 200     87,979,478B(與 enclosure length 逐位元組相同)
$ ffmpeg -v error -i readback.mp3 -f null -   → 無任何輸出 = 全檔 decode 零錯誤 ✅
```

**range 206 支援 ✅**(Apple 的播放器靠它 seek)、**全檔 decode 無損 ✅**。

#### 7.3d ⭐ content-hash 定址:**4/4 逐項對帳命中**

URL 檔名裡的 hash 前綴,與該檔實際 sha256 的前 8 位:

| 公網檔名 | 檔案 sha256 前 8 | 對? |
|---|---|---|
| `EP01-**9a6eade1**.mp3` | `9a6eade1` | ✅ |
| `EP01-cover-**fa228cad**.jpg` | `fa228cad` | ✅ |
| `artwork-**c9ecf1a1**.jpg` | `c9ecf1a1` | ✅ |
| `EP01-**bd42e34c**.html` | `bd42e34c` | ✅ |

→ docstring 的「mp3 content → stable enclosure URL」與「重生的集會拿到**新的**不可變 URL、
舊 URL 仍然可讀」這個設計,**定址機制實測正確**。
(順帶:mp3 的 hash 是**正規化後**那份的,不是本機原檔的 —— 所以本機檔案 byte 有變
但轉出來的 mp3 相同時,URL 不會變。這是對的行為。)

#### 7.3e 講義 HTML 自包含 ✅

```
HTTP 200  text/html  7,179B
外部 <script|link|img src/href="http...">  命中 0 筆   ← 自包含,不外連 ✅
「驗收標記 ZZ-9925」                        命中 1 筆   ← 5.4 的 extra_instructions 一路帶到公網
```

**5.4 的驗收標記從 `generate_report` 的 `extra_instructions` 一路活到公開 HTML** ——
這條把「靜態格式吃 `extra_instructions`」驗到了端到端,不只是本機那份 `.md`。

#### 7.3f 封面過 Apple 硬規格(公網那份,不是本機那份)

```
EP01-cover-fa228cad.jpg  image/jpeg  3000x3000  baseline  components 3(RGB)  464,590B
artwork-c9ecf1a1.jpg     image/jpeg  3000x3000  baseline  components 3(RGB)  518,065B
```

正方形 ✅ / 1400–3000px ✅ / JPG ✅ / RGB ✅ / 無 alpha ✅ ——
**公網讀回的那份也過**(不是只驗本機產出)。

#### 7.3g show page

```html
<title>ZZ-TEST v0.9.25-rc</title>
<h1>ZZ-TEST v0.9.25-rc</h1>
<p>notebooklm-mcp v0.9.25-rc 真實環境驗收用的拋棄式測試節目。…</p>
<p>RSS: <a href="…/feed.xml">…</a></p>
<ul><li>EP01 — 尾端延遲與碰撞機率 (<a href="…/EP01-9a6eade1.mp3">mp3</a>)</li></ul>
```

只列 EP01,**EP02 不在** ✅(與 feed 一致)。
ℹ️ show page 的條目寫的是 `EP01 — 尾端延遲與碰撞機率`(破折號分隔的 label 形式),
不是 feed item 的 `EP01. 尾端延遲與碰撞機率`。兩處刻意不同形狀,不是 bug。

### 7.4 ⚠️ 這次發布留在 NAS 上的東西(uploader 不刪檔)

```
…/kc5jpt72xydkfevg7w673lie/feed.xml
…/kc5jpt72xydkfevg7w673lie/index.html
…/kc5jpt72xydkfevg7w673lie/EP01-9a6eade1.mp3          ← 87,979,478 B,永久
…/kc5jpt72xydkfevg7w673lie/EP01-cover-fa228cad.jpg
…/kc5jpt72xydkfevg7w673lie/artwork-c9ecf1a1.jpg
…/kc5jpt72xydkfevg7w673lie/EP01-bd42e34c.html
```

**token `kc5jpt72xydkfevg7w673lie` 由 `-c stg` 的 `PODCAST_TOKEN_SALT` 推出**,
與正式節目的 URL 空間不相交(CLAUDE.md §四)。這些檔案**不會**出現在任何正式 feed 裡,
但它們是真實的公網資源、**uploader 不刪檔**,所以會永久佔著約 88 MB。
📌 收工清理**不包含**它們(README §八 只要求刪 notebook 與本機產物)——
若要清,需要在 NAS 上手動處理 `feeds/kc5jpt72xydkfevg7w673lie/`。

---

## 收工 — 🔴 FINDING:`notebook_delete` 缺席,CLAUDE.md §八 的清理義務在 MCP 上無路可走

CLAUDE.md §八 明文要求:

> 標題含 `ZZ-TEST v0.9.25-rc`,收尾時 `notebook_list` 找出來刪乾淨。

但 **37 支工具裡沒有 `notebook_delete`**(覆蓋率自檢的工具清單可覆核)。
而 SDK **有**這個能力:

```
$ NotebooksAPI 的公開方法
['copy', 'create', 'delete', 'get', 'get_description', 'get_metadata', 'get_or_none',
 'get_raw', 'get_share_url', 'get_source_ids', 'get_summary', 'list',
 'remove_from_recent', 'rename', 'set_emoji', 'suggest_next_steps',
 'suggest_prompts', 'update']
                    ↑ delete 在
```

→ **這與 v0.9.18 修掉的 `episode_set_publication_state` 是同一個形狀**:
一個被文件要求、SDK 做得到、但**沒有任何 MCP 工具寫得動**的動作。
那次的結論是「解除在受支持的路徑上是死路」,這次是「清理在受支持的路徑上是死路」。

**實務後果**:每一輪驗收都建 1~2 本 `ZZ-TEST` notebook,而 §八 的清理只能靠
(a) 在 NotebookLM 網頁 UI 手動刪,或 (b) 繞過 MCP 直接呼叫 SDK。
兩者都不是「照著 skill / MCP 走」。**測試帳號 pool 會慢慢累積 ZZ-TEST notebook**
—— 對照 1.2 的 `auth_check`:slot 8 已經有 **20 本**、slot 5 有 9 本。

📌 **建議**(列給決策):補一支 `notebook_delete(notebook_id)`。
它是破壞性動作,所以值得加上 MCP 這一層特有的護欄 —— 例如
「先 `notebook_get` 比對 title、回傳被刪的 title 讓呼叫端對帳」,
與 `source_delete` 現有的「先 `source_list` 驗證歸屬,查無此 id 就不發那個
destructive RPC」同一個模式。

**本輪的處置**:依 §八 的指示清理,但只能走 SDK(記在這裡讓它可觀測)。
清理腳本**嚴格只比對標題含 `ZZ-TEST v0.9.25-rc`**,先印出要刪的清單再刪,刪完覆核。

---

## 收工前的意外發現 — 🔴 **`notebook_list` 不是「拿得到的 notebook」的可靠索引**

清理腳本的 dry-run 顯示 notebook A 只有 **slot 1、2** 看得到,而 2.1 的
`notebook_create` 明明把它分享給了 8 個帳號。逐槽位做判別實驗
(`notebooks.list()` 有沒有它 vs `notebooks.get(id)` 拿不拿得到):

| slot | 在 `list()` 裡 | `list()` 共幾本 | `get(id)` | `role` |
|---|---|---|---|---|
| 1 | **True** | 1 | OK,4 sources | **OWNER** |
| 2 | **True** | 2 | OK,4 sources | EDITOR |
| 3 | **False** | 0 | **OK**,4 sources | EDITOR |
| 4 | **False** | 4 | **OK**,4 sources | EDITOR |
| 5 | **False** | 9 | **OK**,4 sources | EDITOR |
| 6 | **False** | 0 | **OK**,4 sources | EDITOR |
| 7 | **False** | 2 | **OK**,4 sources | EDITOR |
| 8 | **False** | 20 | **OK**,4 sources | EDITOR |
| 9 | **False** | 1 | **OK**,4 sources | EDITOR |

**9/9 都 `get` 得到、role 都是 EDITOR/OWNER;但只有 2/9 出現在 `list()` 裡。**
(slot 1 是 owner;slot 2 是 6.4a failover 之後真的動過這本的那個。
slot 3–9 被分享了但從未觸碰,所以不在它們的清單裡。)

### 這件事的兩個後果,方向相反

**✅ 好消息:pool failover 的前提**(v0.8.0 F-2 修的那件事)**成立且更強** ——
failover 是「拿新帳號對**同一個 `notebook_id`** 送出」,靠的是 `get`/存取權,
**不是**靠它出現在清單裡。9/9 都存取得到 → `notebook_create` 的自動分享有效。

**🔴 壞消息:skill 的 `notebook_id` 路由建議有一條會在 failover 之後失效。**
skill §MCP Tools 寫:

> - 已有 `series_manifest.json` → `notebook_id` 就在裡面(讀檔,不必打 RPC)。
> - **只有名字 → `notebook_list()` 比對標題。**

第二條在**輪替游標移動過之後**會回答「找不到」:配額 failover 把作用中帳號換成
slot 5(它從未觸碰過該節目的 notebook),使用者說「audicast 第 12 集」,
host 照指引打 `notebook_list()` 比對標題 → **清單裡沒有那本** →
host 可能結論「這個節目不存在」並**建一本重複的**。
而那本 notebook 其實完全存取得到(`get` 會成功)。

📌 **要回寫進 skill §MCP Tools**:

> ⚠️ **`notebook_list()` 只列「自己擁有的 + 自己實際開過的」**,不列「被分享但沒碰過的」。
> 多帳號 pool 下配額 failover 會換掉作用中帳號,而**新帳號的清單裡通常沒有那本節目**
> (實測 9 槽 pool:9/9 都 `notebook_get` 得到、role 都是 EDITOR/OWNER,
> 但只有 owner 與真的動過的那一個出現在 `notebook_list()` 裡)。
> **所以「只有名字 → `notebook_list()` 比對標題」在 failover 之後會假答「找不到」。**
> 有 `series_manifest.json` 就一律讀檔拿 `notebook_id`;清單比對只在剛建立、
> 還沒發生輪替時可靠。找不到時**先懷疑是這個原因,不要建新的 notebook**。

**這條「manifest 優先」原本的理由是省一次 RPC;現在它有一個更硬的理由 —— 正確性。**

ℹ️ 順帶:這也解釋了 2.9 觀察到的「`notebook_list` 只有一本,而 `auth_check` 說
slot 8 有 20 本」—— 那不只是「別人的 notebook 看不到」,還包含「被分享但沒碰過的
也不會出現」。兩個機制疊在一起。

### 機制的直接證據(意外的對照實驗)

跑清理腳本時,同一支 dry-run 前後兩次的結果**不同**:

```
第一次(判別實驗之前):
  8fd51473…  'ZZ-TEST v0.9.25-rc A'   看得到的槽位: [1, 2]

第二次(判別實驗之後,只隔了一次 probe_visibility.py):
  8fd51473…  'ZZ-TEST v0.9.25-rc A'   看得到的槽位: [1, 2, 3, 4, 5, 6, 7, 8, 9]
```

中間唯一發生的事,就是 `probe_visibility.py` 對**每一個槽位**打了一次
`notebooks.get(NB)`。

→ **「被分享的 notebook 在該帳號實際觸碰過之後才會進 `notebooks.list()`」——
因果直接成立**,不只是相關。(我的觀測動作改變了被觀測的狀態,而這剛好把機制釘死了。)

📌 這讓上面那條 skill 建議更精確:失效條件不是「換了帳號」,而是
**「換到一個從未觸碰過那本 notebook 的帳號」**。而 pool 裡的帳號在正常運作下
本來就大多沒碰過(只有 owner 與曾經 failover 到的那幾個碰過)。
**補救也很便宜**:`notebook_get(id)` 一次就會讓它進清單 —— 但前提是你已經知道 id,
那正是「找不到」時你缺的東西。**所以只能靠 manifest,清單比對救不了自己。**

---

## 收工 §八 — 刪掉本輪建的 notebook ✅

```
=== 標題含 'ZZ-TEST v0.9.25-rc' 的 notebook:2 本 ===
  8fd51473-6758-48d2-896d-1861ec2e29da  'ZZ-TEST v0.9.25-rc A'
  6cf99fb6-1e1a-4fb2-b5d0-a1b3a548dec6  'ZZ-TEST v0.9.25-rc research-scratch'

=== 開始刪 ===
  ✅ 刪掉 8fd51473-… 'ZZ-TEST v0.9.25-rc A'(用 slot 1 = owner)
  ✅ 刪掉 6cf99fb6-… 'ZZ-TEST v0.9.25-rc research-scratch'(用 slot 2 = owner)

=== 覆核 ===
=== 標題含 'ZZ-TEST v0.9.25-rc' 的 notebook:0 本 ===
```

清理腳本(`scratchpad/cleanup_notebooks.py`)的安全性質:
**dry-run 預設**、嚴格 `MARKER in title` 字串比對、逐槽位嘗試(只有 owner 刪得動)、
刪完覆核。**兩本都是 owner 那一槽刪掉的**,沒有誤刪任何其他 notebook
(第一次 dry-run 印出的清單就是最終刪除清單,零誤判)。

⚠️ **雲端的 artifact 沒有跟著清乾淨的部分**:notebook 刪掉了,所以裡面的 artifact
(含 6.3d 那顆孤兒 `pending` slide_deck)一起走了。但 **7.4 列的 NAS 上那 6 個檔案
仍然公開存在**(uploader 不刪檔),需要另外處理。

---

## 收工 §九 — 還原 `nblm-mcp`,但 🔴 **還原結果不等於開工基線**

```
$ uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@latest"
 - notebooklm-mcp==0.9.24 (from file:///home/user/research/audiskill/notebooklm-mcp)
 + notebooklm-mcp==0.9.24 (from git+…@61789f5080ce29071d9ad68df3c0d6ed13df4509)
Installed 2 executables: nblm-mcp, notebooklm-cover
```

**內容驗證(不看版本字串,CLAUDE.md §〇 的紀律)**:

```
工具數 36 | source_search 在嗎: False              ← 發布版沒有這支,對 ✅
concept_explanation 在白名單嗎: False              ← 發布版沒開放,對 ✅
entry point: from notebooklm_mcp.server import main  ← 還是我們那支,不是上游 ✅
61789f5 = tag `latest`                             ← 裝到的確實是發布版
```

### 🔴 FINDING:`notebooklm-py` **停在 0.8.2,不是 CLAUDE.md §九 預期的 0.8.1**

```
還原後: notebooklm-mcp 0.9.24 | notebooklm-py 0.8.2 | mcp 1.29.1
開工前: notebooklm-mcp 0.9.24 | notebooklm-py 0.8.1 | mcp 1.29.1   ← 0.1 的 baseline
```

CLAUDE.md §九 寫「還原之後 `notebooklm-py` 會回到 **0.8.1**(發布版的 pin 下界)」——
**那個預期是錯的**。原因:

```
$ git show v0.9.24:pyproject.toml | grep notebooklm-py
    "notebooklm-py>=0.8.1,<0.9",          ← 是範圍,不是釘死在 0.8.1
```

`>=0.8.1,<0.9` 是**範圍**,而 **`uv tool install git+…` 不讀 `uv.lock`**
(CLAUDE.md §〇 自己也講了這件事,只是講的是另一個方向)→ 重新解析就拿**範圍內最新的**
= 0.8.2。開工前那次之所以是 0.8.1,只是因為那次安裝時 0.8.2 還沒進解析結果。

**所以還原**沒有**回到基線,而是變成一個新組合:發布版程式碼(為 0.8.1 寫的)+ 0.8.2 SDK。**

### 這件事影響的不只本工作區 —— 它影響**正式節目**

這台機器同時跑 `-c prd` 的正式 server(pid 479717)。它**現在**還活著、用的是
啟動時(06:20)載進記憶體的 0.9.24/0.8.1 程式碼。但**下次重啟就會拿到 0.9.24 + 0.8.2**。
而且這不是我的還原造成的特例 —— **任何一台機器跑
`uv tool install …@latest` 都會得到同一個組合**(四台機器都適用,見 AGENTS.md §Release Pin Sites)。

### ✅ 實測:這個組合**跑得起來**

用自起 server 對還原後的 build 做真 RPC:

```json
{"tool_count": 36, "has_source_search": false,
 "calls": [{"tool":"auth_check","isError":false,"content":"{\"ok\": true, \"notebooks\": 0}"}]}

$ grep -ciE "traceback|attributeerror|importerror|modulenotfound" restore_check.err
0
```

**36 支工具列得出、`auth_check` 真 RPC 過、stderr 零 traceback / 零 import 錯誤。**
→ 發布版 0.9.24 在 notebooklm-py 0.8.2 上**沒有立即壞掉**。

⚠️ **但這只是 smoke test,不是「這個組合安全」**:只驗了啟動 + 一次認證 RPC。
rc 這一輪之所以要改程式碼,就是因為 0.8.2 動了 enum / dispatch table / 傳輸層 ——
發布版沒有那些調整。**沒測到的**:生成、下載、`report_format` 對映、
`tests/test_contracts.py` 的 getsource 斷言(rc 的 pyproject 註解明說那些斷言因
「web 實作搬到 `notebooklm._web.*`」而要改)。

📌 **兩件要決策的事**:

1. **CLAUDE.md §九 的那句預期要改掉** —— 它會讓下一輪的人以為還原後是 0.8.1,
   於是不會去檢查這個組合。建議改成:
   > 還原之後 `notebooklm-py` 會是**範圍 `>=0.8.1,<0.9` 內最新的那一版**
   > (`uv tool install git+…` 不讀 `uv.lock`)。**驗版本號時要記下實際拿到什麼**,
   > 不要預期它回到某個特定版本。
2. **發版順序**:既然驗收通過(除了 5.1),照 AGENTS.md §Release Pin Sites 發 tag 之後,
   `@latest` 就會是「rc 程式碼 + 0.8.2」——**那才是測過的組合**。
   在發 tag 之前,四台機器上任何 `@latest` 重裝都會落在未測組合上。
   **這讓發 tag 從「讓四台機器裝得到新功能」升級成「讓四台機器離開未測組合」。**

---

## 收工 §三 — 憑證殘留清理:**0 孤兒**,沒有東西要刪

照 CLAUDE.md §三 的對照法(**不用 `fuser` / `lsof`** —— 那會把活著的 server 正在用的
目錄判成無主,而這台當下就有一個 `-c prd` 的正式 pool):

```
2026-09-03 16:25:17 +0800
⚠️ 還有活著的 nblm-mcp —— 不自動刪。對照啟動時間自己挑:
 479717  Thu Sep  3 06:20:46 2026  nblm-mcp --transport stdio     ← -c prd 正式節目
 629026  Thu Sep  3 13:47:32 2026  nblm-mcp --transport stdio     ← 本 session 的 MCP server

--- 憑證目錄(ctime)---
  2026-09-03 13:47:53  /tmp/notebooklm-mcp-auth-451ahwj4  (700)   ↔ 629026(差 21s,建目錄後寫 9 個 slot)
  2026-09-03 06:20:52  /tmp/notebooklm-mcp-auth-ph3kbrjq  (700)   ↔ 479717(差 6s,5 個 slot)
```

**兩個目錄的 ctime 都對得上一個活著的 server 的 lstart → 0 孤兒,不刪任何東西。**

本輪期間所有自起的 server(Phase 1 的 30+ 次、Phase 3/4/5/6 的 6 次、
收工的 2 次)都自己清乾淨了。唯一一次殘留是 **0b.3 的 `jauru40n`** ——
Claude Code 自己關掉上一個 session 的 server 時留下的,已在 0b.3 處置。

⚠️ **給使用者的最後一步**:`451ahwj4` 是**本 session 的 MCP server** 建的。
依 0b.3 / 0b.5 的發現,**關掉這個 session 時它有機會殘留**
(`rmtree` 是最先註冊 → LIFO 最後才跑,2 秒寬限經常來不及)。
關掉之後跑一次:

```bash
pgrep -af nblm-mcp                        # 只該剩 -c prd 那一組(479640/479717)
ls -ld /tmp/notebooklm-mcp-auth-*          # 只該剩 ph3kbrjq
# 若 451ahwj4 還在,且沒有活 server 的 lstart 對得上它的 ctime,就是孤兒:
rm -rf /tmp/notebooklm-mcp-auth-451ahwj4
```

🔴 **`ph3kbrjq` 絕對不要刪** —— 正式帳號 pool 正在用。

---

## 🔴 待使用者執行的三件事

### (1) Phase 8 前兩列 —— 需要一個乾淨的新 session

我開不了新 session,而這兩條測的正是「host 的起手式」(見 8.4)。

⚠️ **我的順序失誤**:使用者選了「收工時一起做」,而我在那之前就依 §八 把 notebook 刪了。
兩件事該反過來。

**但這兩條仍然測得出來**,因為它們測的是**路由決策**(host 的**第一支**工具選什麼),
不是 notebook 存不存在:

- 讀 manifest → 拿 `notebook_id` → `notebook_get` → 得到 NotFound = **通過**
  (它走了正確的路,只是目標已被刪)
- **第一步就打 `notebook_list` 比對標題 = 失敗**

而且 `notebook_get` / `notebook_list` **發布版都有**(36 支裡),
`source_search` 不在這兩條的路徑上 → **不必重裝 rc**。

**可貼的兩句 prompt**(在本工作區另開 `claude`,一句一個 session):

```
用 output/series_manifest.json 這個節目,列出 EP01 的來源。
```
→ 通過條件:第一支工具是**讀檔**(不是 `notebook_list`),拿到 manifest 裡的
`notebook_id` 之後才打 RPC。

```
notebook_id 是 8fd51473-6758-48d2-896d-1861ec2e29da,幫我列出它的來源。
```
→ 通過條件:**直接** `source_list(notebook_id=…)`,沒有為了「保險」先打
`notebook_get` 或 `notebook_list`。

📌 **順帶要改 README**:Phase 8 前兩列有**順序依賴**(必須排在 §八 刪 notebook 之前,
否則觀察到的是 NotFound 而不是正常結果),README 現在沒寫。
本輪這兩條記為 **未驗(可由使用者用上面的 prompt 補測)**。

### (2) `publish_series` 留在 NAS 上的東西(7.4)

```
feeds/kc5jpt72xydkfevg7w673lie/
  feed.xml  index.html
  EP01-9a6eade1.mp3            ← 87,979,478 B
  EP01-cover-fa228cad.jpg  artwork-c9ecf1a1.jpg  EP01-bd42e34c.html
```

uploader **不刪檔**,這些是真實的公網資源(約 88 MB)。
token 由 `-c stg` 的 salt 推出,與正式節目 URL 空間不相交,**不會影響任何正式 feed**。
要清需要在 NAS 上手動處理那個目錄。**不清也不會壞任何東西**,只是佔空間。

### (3) 關掉 session 之後檢查 `451ahwj4`(見上面 §三)

---

## 本輪結論摘要

### 🔴 建議**擋發版**的一件

**5.1 `concept_explanation` 在 web backend 上必定失敗。** 這一版對外宣告的兩個新能力之一,
在我們唯一能用的 backend 上是死選項。修法在 5.6(用 repo 現成的 `_DECLINED` 機制,
影響半徑就這一個選項,已驗證新 check 會紅且只命中它)。

### 🔴 其餘要修 / 要回寫的(依重要性)

| # | 發現 | 要動的地方 |
|---|---|---|
| 5.3 | 純本地 `ValueError` 被記成 `attachment_acceptance_unknown`(實測 `CREATE_ARTIFACT` 零次) | `tools_artifacts.py` 的護欄要延伸到 backend param builder |
| **收工** | **`notebook_list` 不列「被分享但沒碰過的」** → failover 之後照 skill 用標題比對會假答「找不到」 | skill §MCP Tools 的路由建議 |
| **收工** | **還原後是 0.9.24 + notebooklm-py 0.8.2 這個未測組合**;`@latest` 在四台機器上都一樣 | CLAUDE.md §九 的預期 + 發版優先度 |
| 6.5a | `chat_ask` 的伺服器推銷尾巴 `strip_citations=True` 清不掉,skill 配方直通公開 feed | skill §Publish 2(考慮加 preflight) |
| 4a.4/4b.3 | `start`/`end` 對無標記來源對得上、對 markdown 錯位 44~170 字,呼叫端分辨不出 | docstring + tool-reference |
| 8.3 | `limit=N` 是「全域前 N 名」不是「N 筆相關」,回傳無欄位可分辨 | skill §Sources + tool-reference |
| 6.3d | 配額 failover 在雲端留下不可追蹤的孤兒 artifact | `attachment_errors` 多記被棄置的 `artifact_id` |
| 3.0b | `local-checks.sh` §8a 的 `cookies=None` 斷言描述錯分支(其餘四條沒問題) | 工作區範本(替代 check 已驗可跑) |
| 5.2 | `local-checks.sh` §8c 驗 enum 不驗 backend dispatch table,第二條斷言方向相反 | 同上(替代 check 已驗會紅) |
| 0.5 | `local-checks.sh` §2 的字面比對在 v0.9.16 抽離後假紅 | 已就地修好,要帶回範本 |
| 6.3f | `pending_source_cleanup` 在 `source_delete` 後不消失(設計如此) | 文件講明,不改行為 |

### ✅ 可以把「未實測」警語改成實測結論的

- **4b.1 mp3 逐字稿在索引裡** —— 45 分鐘中文 podcast、26,927 字、27 段全可檢索。
  `tools_basic.source_search` docstring 與 skill tool-reference 都要改寫。
- **4a.3 `rank` 一次都沒出現 0** —— 6 次查詢全部稠密 1..N(節制:樣本是 1 本 notebook)。
- **3.3/3.4 下載路徑** —— 4 跳跨 4 host(`googleusercontent`→`google.com`→
  `googleusercontent`→`usercontent.google.com`),全在信任白名單、全 https,
  兩次獨立下載位元組相同。建議下一輪**降級**成順帶對帳。

### ✅ 驗過且行為正確的(正面樣本)

`is_owner` 語意修正(2.6)、`NOTEBOOKLM_BACKEND` inline 刪除護欄(1.4)、
三種壞輸入 RPC 前擋掉(4a.6)、`report_format` 配對驗證(Phase 5 部分)、
**配額 failover 走對了 §七 的路**(6.4a)、`EP{NN}. ` 前綴剝離(6.1b/6.2)、
`podcast_attempt_retract` 的 `next_step` 自帶重生鐵律(6.3a)、
三個狀態機入口的守門與 `resume` 冪等(6.3g)、`research_*` 的 `account` 綁定(6.7)、
**fail-closed 交付閘在任何上傳之前 raise(7.1,404 實證)**、
**publisher 把偽 mp3 正規化成真 MP3(7.3b)**、content-hash 定址 4/4(7.3d)、
`notebooklm-cover` 的品牌護欄(7.0b)。

### 覆蓋率

**33/37 工具實跑**,4 支寫明理由(見〈覆蓋率自檢〉)。
**唯一實質缺口:PDF 下載的型別判定** —— 那顆簡報三次都沒生出來
(slot 1 限流 / slot 2 failed / retry failed),`artifact_download_slides` 與
`_reject_html_download` 對 PDF 的判準本輪測不到。**下一輪要補這一條。**

---

## 紀律自檢:帳號資料

AGENTS.md「no secrets, internal URLs, or account data in any context file」,
而 1.2 自己也訂了「帳號 email 依 AGENTS.md 不落檔,只記槽位」——
**但我在貼原始 JSON 時漏了 10 處**(2.8 `shared_by`、5.3 / 6.1a / 6.7b 的 `account`、
6.4a 的 `from_account`/`to_account`、6.7a 的 `shared_with` 清單、7.2 的 `owner_email`)。

已全部替換成 `<slot-N>` / `<owner-email>` 並覆核:

```
$ grep -coE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' FINDINGS.md
0
```

📌 **這件事本身要記進下一輪的紀律**:貼工具回傳的原始 JSON 是本輪 FINDINGS 的主要
記錄手法(那是對的 —— 「記可觀測的事實」),但**多帳號 pool 的回傳普遍帶 email**
(`account` / `shared_by` / `shared_with` / `from_account` / `to_account` / `owner_email`)。
**貼之前就該替換,不要等收工才 scrub** —— 收工才做的話,中間任何一次
「把 FINDINGS 複製出去」都會漏出去。
