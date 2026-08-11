# AGENTS.md — notebooklm-mcp

自建薄 MCP server(建在 `notebooklm-py` 之上)+ 確定性續集 podcast 工具。**本 repo 只含 MCP 程式碼**,
發布成 private repo `github.com/audichuang/notebooklm-mcp`,靠 `uv tool install` 裝成 console 命令
`nblm-mcp`(server,見 §Gotchas 撞名說明)/ `notebooklm-cover`(封面 CLI)。薄 `SKILL.md` 路由層 + `references/`
**已拆到 skill repo `audi-skill/notebooklm`(docs-only)**;兩者是一組配置(見底部 Cross-Repo Sync Checklist)。
完整設計理由與 live 驗證踩坑見 `docs/superpowers/notebooklm-mcp-findings.md`(經驗庫,值得先讀)。

## Commands

```bash
# 本 repo 開發（必須用 Python 3.12；3.14 會觸發 SDK 的 inspect.signature bug）
uv venv --python 3.12 && uv pip install -e ".[dev]"

# 全套離線測試（mock client，不需網路/認證）
uv run pytest -q
#   註:fresh venv + uv pip install 後首次 uv run 可能撞暫時性 re-sync churn(ModuleNotFoundError),
#   再跑一次或 rm -rf .venv 重建即收斂。

# 消費端安裝（3 VM / podcast-lab 各裝一次；pin tag,不追 master；換成最新 tag）
uv tool install --python 3.12 "git+https://github.com/audichuang/notebooklm-mcp.git@v0.9.11"

# 跑 MCP server（裝好後零路徑命令；認證由 doppler 注入 NOTEBOOKLM_AUTH_JSON）
doppler run -p notebooklm -c prd -- nblm-mcp --transport stdio
#   HTTP 模式：--transport streamable-http --host 127.0.0.1 --port 8484
#   ⚠️ streamable-http / sse「無認證」——勿綁非 loopback host(同網段可驅動帳號)。
#   repo 內開發時亦可 uv run python -m notebooklm_mcp.server --transport stdio

# 註冊進 Claude Code（細節見 docs/mcp-setup.md）。CLI 2.1.201 的 `claude mcp add … -- …`
# 會把 `--` 後整串當 prompt，改用 add-json：
claude mcp add-json notebooklm -s local \
  '{"command":"doppler","args":["run","-p","notebooklm","-c","prd","--","nblm-mcp","--transport","stdio"]}'
```


### 認證與發版(按需)

| 要做什麼 | 讀 |
|---|---|
| 設定帳號 / 換憑證 / 加 pool 槽位 / 搭測試環境 | [docs/auth-and-config.md](docs/auth-and-config.md) |
| 發 tag / 改工具契約 / 同步 skill repo | [docs/release-checklist.md](docs/release-checklist.md) |

**兩條在這裡先講,因為漏了會直接出事**:①`prd` 的 `PODCAST_TOKEN_SALT` 必須**逐字等於**
`dev` 的(feed 公開路徑是 `HMAC(salt, show_id)`,換 salt = 已發布節目全部換 URL、訂閱者掉光);
②認證一律 `doppler secrets set --raw`,**絕不用 `secrets upload`**(它做變數插值,cookie 裡的
`$` 會被吃掉,而指令回報成功)。

## Architecture

**結構問 [codegraph](.codegraph)**(`codegraph explore "<符號或問題>"`)或直接讀 code ——
有哪些檔、誰呼叫誰、某個符號長怎樣,那裡永遠最新。下面只給**角色導覽**與**動之前要知道的紅線**;
每個模組的設計決策與實作細節在 [docs/design-notes.md](docs/design-notes.md)。

| 模組 | 角色 | 動之前 |
|---|---|---|
| `app.py` | FastMCP app + 工具註冊 + lifespan(多帳號 client pool)+ 多 transport main | 憑證落檔與重鑄護欄,見 §Gotchas |
| `server.py` | thin launcher(可當 `__main__` 跑) | — |
| `tools_basic.py` | notebook / source / artifact / `chat_ask` 的薄包 + 讀取觀測面 | — |
| `tools_podcast.py` | manifest-backed audio attempt:durable generate / reconcile / adopt / finalize / retract | **先讀 [ADR-0009](docs/adr/0009-retracted-attempts-are-tombstones.md)** —— 四個不變式各自被真實事故驗證過,而踩過的坑是**只補一條路徑**(attempt 建立有兩個分支、清理義務有三個入口)。**寫任何狀態相關的指引訊息前先讀 [gotchas-attempt.md](docs/gotchas-attempt.md) 的紅線**——一律由 `_attempt_capabilities()` / `_attempt_next_step()` 產生,不准手寫 if/else,這個根因已現形七次 |
| `tools_artifacts.py` | 簡報 PDF / 研讀 Markdown 按需生,路徑回寫 manifest | [gotchas-publish](docs/gotchas-publish.md) |
| `tools_research.py` | Web / Deep Research 的薄包,**三支分開**(start / wait / import) | [gotchas-research](docs/gotchas-research.md);分開是為了對齊 ADR-0001 的 attempt/resume 紀律,不是為了彈性 |
| `tools_publish.py` + `publish/` | 整季發布成 Apple 合規 RSS;`publish/` 是純邏輯(離線可測) | [gotchas-publish](docs/gotchas-publish.md) |
| `generation_input.py` | frozen generation-input bundle + `attempt-binding.json` sidecar | 驗證與綁定都在**第一個遠端副作用之前**(ADR-0001) |
| `_sources.py` | caller 指名的 `source_ids` 驗證與對帳 | **重生某一集不指名,後面各集的回錄會洩進那一集**;選哪幾筆的政策留 host(ADR-0007) |
| `_errors.py` | `NotebookAccessDenied` / `is_permission_denied`(`tools_basic` 與 `tools_podcast` 共用) | 各寫一份 = 上游改 `rpc_code` 時只有一處被改到 |
| `_status.py` `_atomic.py` `_text.py` `runtime.py` `languages.py` `enums.py` | generation-status 防護 / 原子寫入 / 文字正規化 / client holder / 白名單 / enum 映射 | `_atomic` 見 [gotchas-files](docs/gotchas-files.md) |
| `auth_probe.py` `auth_cli.py` `cover_cli.py` `assets/` | 認證預檢 / headless 建檔備援 / 封面 CLI + 凍結的 HTML template | 封面見 [gotchas-publish](docs/gotchas-publish.md) |
| `tests/test_contracts.py` | 用 `inspect.signature` 鎖住 `notebooklm-py` 公開 API 的離線 tripwire | 對**實裝版本**跑,別信 `_research/` 的 HEAD clone |
| `scripts/` | `check_skill_sync.py`(CI 硬檢查)/ `sync-auth.sh` / `setup-test-config.sh` / `backfill_published_at.py` | — |

**判斷只在「大綱」前置點**(Opus 規劃每集 brief,人核可);大綱定稿後是確定性腳本,迴圈內無 LLM。

**文件的分工**:[CHANGELOG](CHANGELOG.md) = 各版本改了什麼/為什麼/踩到什麼事故(**版本敘事只寫在那裡,不要回填進本檔**);
[docs/adr/](docs/adr/) = 能力邊界決策(**砍掉已規劃的 scope 也要留一支** —— research namespace 曾因此隱形 38 集);
[docs/design-notes.md](docs/design-notes.md) = 模組設計細節;`docs/gotchas-*.md` = 按需載入的雷區(路由表見 §Gotchas 末尾);
`docs/superpowers/` = 設計/計畫/findings。**SKILL.md 路由層 + references 不在本 repo**,在 `audi-skill/notebooklm`。

## Gotchas(notebooklm-py 0.8.0,pin `>=0.8,<0.9`;與 GitHub HEAD 不同,以**實裝版本**為準)

- **`mcp[cli]` 必須有上界(`>=1.27,<2`)**:`uv tool install git+…` **不讀 `uv.lock`**,消費端
  每次安裝都自由解析成當下最新——曾經因為寫成 `>=1.0.0` 而出現「dev venv 鎖 1.27.2、四台
  生產實裝 1.28.1」的落差(測試與實跑不同版),且 mcp 2.0 一出就會被靜默吃進去。改版本時
  **對 lock 版本與消費端實裝版本各跑一次全套**,再更新這裡的下界。
  **這個落差會自己重新長出來,要定期對帳**:2026-08-09 又漂成「lock 1.28.1 / 實裝 1.29.0」,
  而且不是純帳面差異——1.29.0 啟動時多印一行 `pydantic_settings … IncompleteFieldDefinitionWarning:
  Field 'lifespan' has an incomplete definition`,1.28.1 完全不印。走的是 stderr 所以沒破壞
  stdio 協定,但它證明實裝版本有 CI 從沒跑過的行為(已把 lock 拉到 1.29.0 對齊並跑過全套)。
  查法:`grep -A1 'name = "mcp"' uv.lock` vs
  `~/.local/share/uv/tools/notebooklm-mcp/bin/python -c "import importlib.metadata as m; print(m.version('mcp'))"`。
  **對齊要用 `uv sync --extra dev`,不是 `uv pip install -e .`** —— 後者不會把 venv 拉到 lock 的版本。
- **(0.8.0)server 命令改叫 `nblm-mcp`,不是 `notebooklm-mcp`** —— 因為 `notebooklm-py`
  自己也宣告了一支同名 script,同一個 tool venv 只留最後寫入的那份,**實測全新安裝 3/3
  都是上游贏**(而上游那支缺 `fastmcp` 會直接 ModuleNotFoundError)。
  **發版前一定要真的跑一次 `<command> --help`** ——`uv tool list` 列的是被指名套件的
  entry point 名稱,不是 bin 裡的實際內容,看不出撞名。**別裝 `notebooklm-py[mcp]`**。
  repo 內開發一律 `uv run python -m notebooklm_mcp.server`。
- **上游/NotebookLM 行為突變時的情報站**:讀 `_research/notebooklm-mcp-cli` 既有 clone 的
  CHANGELOG.md 與 docs/KNOWN_ISSUES.md(jacob-bd,全生態追 Google 改版最快;bl 漂移、cookie
  語意、RPC schema 變動幾乎都最先出現在那),再對照 notebooklm-py 的 GitHub issues。
  **`_research/` 唯讀:連 `git pull` 都不做**,clone 更新請使用者自行決定。
- **(0.8.0)這條紀律延伸到所有「在本 process 內重鑄 cookie」的機制,inline auth 一律關掉。**
  新的兩個是 **L3 headless re-auth**(`NOTEBOOKLM_HEADLESS_REAUTH`,`app.py` lifespan 會顯式
  刪掉這個 env)與 **master-token headless auth**(`headless` extra,**刻意不採用**)。
  理由與取捨見[升級筆記](docs/notebooklm-py-0.8-upgrade.md)的「新能力」一節。
- **(v0.9.0)pool 的憑證落檔,把好幾條原本靠「env 模式」早退的重鑄護欄一起降級了。**
  動 `app.py` 的憑證/lifespan 那一塊時三件事要一起看:①`_write_credential_file` 的預驗證
  **必須與 strict cookie loader 同語義**(必要 cookie 的 **value 也要非空**,不能只檢查 key
  在不在)—— 那是 L2 inline PSIDTS `RotateCookies` 唯一入口的門,而 L2 **不受
  `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 管**;②L3/L4 現在只靠「env 已刪」與「`master_token.json`
  不存在」擋著;③等價前提由
  `tests/test_client_pool.py::test_precheck_agrees_with_the_sdk_strict_loader` 守著,**它紅就
  代表這條路又開了**。破功的後果:3 VM 共用的 cookie 被本 process 重鑄、新的寫不回 Doppler,
  另外兩台下次啟動就掛,而本機正常啟動只有 debug 訊息。
  推導與取捨見 [ADR-0010](docs/adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md) 的 v0.9.0 amendment。
- 長跑工具(`podcast_episode`/`podcast_series`)在**本地驗證之後**有 `probe_auth` 認證預檢
  (輕量真 RPC;homepage probe 會 false-positive,jacob-bd #250);獨立工具版是 `auth_check`。
- `sources.delete` 是 **idempotent**(0.7.0 起):刪不存在的 source 也「成功」不 raise。
  `source_delete` 回的 `deleted` 只代表「呼叫後該 id 已不在筆記本」,**不保證它先前存在**
  (打錯 id 也回 deleted)。要確認刪掉某既有來源,先用 `source_list` 拿真實 `source_id`。
  ⚠️ **刪除後 `source_fulltext` 用同一個 `source_id` 仍讀得回全文(實測 55 分鐘後仍可)
  —— 那是預期的,不是清理失敗**:它繞過 notebook 直接查 source 物件,而生成用的來源清單
  與 `source_list` 同走 `GET_NOTEBOOK`。所以 ADR-0009 的清理義務有效。推導的三個前提由
  `tests/test_contracts.py::test_generation_takes_its_source_list_from_the_notebook_not_the_server`
  釘住(**它紅就代表推導失效、清理義務要重新論證**),完整論證在該測試的 docstring。
- **多帳號 pool 動 dispatch/認證前必讀 [ADR-0010](docs/adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md)**
  (含 v0.9.0 / v0.9.7 amendment)。**v0.9.7 起冷卻不是永久除名**:被拒的帳號 600 秒後會
  重新變成候選(真實驗收量到同帳號被拒 26 分鐘後又被受理),`rotate_client()` 的游標會
  環狀 wrap 回去,別再假設「一輪之內同一個帳號只會被拒一次」。四條實作紀律,每條都被
  真實事故驗證過、都有測試鎖:
  ①**`podcast_series` 有兩條 dispatch 路徑**(全新一集走 `_run_episode`、重送/supersede 是
  series 自己 inline),**兩條共用 `_dispatch_audio_with_failover`** —— v0.8.0 只補了一條,
  於是 pool 對「重試」這條最需要它的路完全無效。
  ②**身分跟著 client 走,不准放進 process 全域**:每個槽位各自一份 storage_state 檔,
  `from_storage(path=…)`。MCP 是並行的,一個全域槽不可能同時是兩個值,**加鎖也救不了**。
  ③**記帳與送出必須同源**:`runtime.snapshot()` 一次取 `(label, client)` 往下傳,
  `_dispatch_audio_with_failover` 回 `(artifact_id, account, client)`,failover 換帳號時
  一起換。分開讀 `active_account()` 與 `get_client()`,並行 rotate 會落在 await 的縫裡 ——
  manifest 記 A、實際 B 送出,而**兩個帳號都成功,所以事後查不出來**。
  ④failover **只掛在 `_REFUSED_WITHOUT_DISPATCH` 那一條 except**(契約保證沒建出 task)。
  **`ensure_started` 拋了不等於沒建出 task** —— 它的條件是 `is_failed or not task_id`,而
  「有 id + failed」在 SDK 路徑上可達,那種形狀 rotate 重送會產生**第二個 artifact**;
  只有真的沒有 id 才算零副作用。已 dispatch 之後換帳號 = ADR-0009 禁止的改寫因果紀錄。
  `tests/test_pool_gaps.py`(經突變驗證)是這區最敏感的守門員。
- 命名鐵律(每集 mp3 回錄 + 工作室 artifact **完全同名** `EP{n:02d} 標題`)的正本在 skill
  §Episodic;**實作上唯一要記的是命名邏輯集中在 `_episode_label()`**,改流程時對照那裡,別各處自己拼字串。
- `get_fulltext` 會在 CJK 字元間插空格;關鍵字比對前先 `"".join(text.split())`
  (`_text.norm` 已封裝)。
- **`chat_ask` 回答夾帶引用標記**(`[1]`/`[3, 4]`/`[8-10]`)。工具已內建
  `strip_citations` 由 server 端清(`_text._CITATION_RE`),`episode_set_description` 也預設再清一次
  ——**新程式碼別再自己寫 regex**,要改清理規則改 `_CITATION_RE` 一處。
- **遠端 mutation 前要有便宜 preflight,但那不是 durable attempt**:`artifact_revise_slide`
  與 `artifact_retry_failed` 改的是**遠端狀態**(不像 download 類救援只寫本機檔),而兩支
  RPC 都只靠 `artifact_id` 定位、`notebook_id` 只是 routing header,錯配 ID 伺服器不會擋 →
  用 `artifacts.get_or_none`(它是 list 後比對 id,一次同時驗存在與歸屬)先驗 kind/status。
  `generate_slides` / `generate_report` / `revise_slide` 也在生成前先驗 `episode_n` 存在,
  免得打錯集號要燒完一次配額才 raise。
  **仍未解的是重入**:外層在 mutation 成功後、回寫前斷線,重跑會再 mutate 一次。這是整個
  slides/report 家族共有的架構債(`generate_slides` 逐字同形),要修得做成涵蓋 generate 與
  revise 的 attachment attempt(含 manifest 存 `slides_artifact_id` 才能驗 episode binding),
  不是替 revise 單獨拆 kickoff/finalize。

### 按需載入的 gotchas(只在動到那一塊時讀)

| 動到什麼 | 先讀 |
|---|---|
| research 三支工具 | [docs/gotchas-research.md](docs/gotchas-research.md) |
| 發布 / 附件 / 封面 | [docs/gotchas-publish.md](docs/gotchas-publish.md) |
| 直接呼叫 notebooklm-py | [docs/gotchas-sdk.md](docs/gotchas-sdk.md) |
| attempt 狀態機 / manifest | [docs/gotchas-attempt.md](docs/gotchas-attempt.md) |
| 寫檔 / 上傳來源 | [docs/gotchas-files.md](docs/gotchas-files.md) |

## Conventions

- 全程繁體中文註解/文件。TDD:測試先紅再綠,每任務一 commit。
  **「每任務一 commit」要在實作階段達成,不要事後切 patch。** 派 agent 做多條修正時,
  要求它逐條做完逐條回報、主迴圈逐條 commit;**一旦全部混在一起,寧可一個 commit,
  也不要用 `git apply --cached --unidiff-zero` 事後切** —— `-U0` 的 patch 沒有 context 行,
  連續套用時前一份造成的行號偏移不會被吸收,實測把一整個 helper 函式插進了另一個函式的
  註解中間,三個 commit 的內容全錯。**而 `uv run pytest` 跑的是 working tree、不是 commit
  的內容,所以照樣全綠** —— 這種錯只有 `git status` 不乾淨或事後 checkout 才看得出來。
  **commit 的內容 = 驗證過的內容,這件事比 commit 粒度重要。**
- commit 訊息寫清楚「症狀 + 根因 + 為何這樣修」(commit 與 docs 是團隊經驗庫)。
- **要不要真實驗收,看改動有沒有碰到遠端副作用路徑**(`generate` / `add_user` / `delete`
  這類會在雲端留下東西的呼叫)。沒碰 → 離線測試 + 用既有實測前提推導結案(`source_delete`
  的清理義務、v0.9.2 的 owner 定位都是這樣結的);碰了 → 開 `acceptance-workspace`。
  **推導要逐條指出前提在哪次實測被證明**,不能只說「應該沒事」。
- 不污染 `_research/`(唯讀參考 clone)。
- **大型修復輪派 agent 時:Sonnet 實作 → Opus 審查 → 主模型裁決**(採納/駁回 findings、
  最終驗證、commit 留在主迴圈)。這個分層被實績驗證過:Opus 抓到的正是本 repo 反覆出現的
  **「補一半」**——guard 放進上傳迴圈內、`except Exception` 漏掉 `CancelledError`、
  測試只鎖三個 handler 之一。三條操作紀律:①**同一個檔案不可讓兩個 agent 並行編輯**,
  按檔案分區,小雜項主迴圈自己 inline 改;②修正輪要把「審查者的具體建議 + 裁決取捨」
  寫進 agent prompt,別讓它重新發明一次;③**派獨立複審時不要先餵自己的 findings** ——
  v0.9.2 那輪刻意沒餵,兩邊各自指出同一組問題,那個「獨立收斂」才是信號;先餵只會
  換到一份同意書;④**「修正有沒有修好原問題」與「修正自己有沒有引入新問題」是兩個
  不同的審查,要分開問** —— v0.9.8 派了三個 opus 視角、每條修正都做過突變驗證、全套
  綠,發版後外部 review 仍抓到四條,**其中兩條長在那一輪新加的東西上**(為了不 tombstone
  還在飛的 attempt 而加的安全窗,自己可以被呼叫端縮小到同樣後果;為了終止性而加的
  tried guard,自己會漏試可用帳號)。根因是 prompt:審查者拿到的是「這幾條修正對不對」,
  於是他們對照原缺陷逐條驗證,**沒有人對成品重新問一次「這裡面有什麼是新的、而且沒被
  任何測試守住的」**。修正輪之後要再派一輪只看成品、不看原 findings 的複審。

