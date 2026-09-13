# AGENTS.md — notebooklm-mcp

自建薄 MCP server(建在 `notebooklm-py` 之上)+ 確定性續集 podcast 工具。**本 repo 只含 MCP 程式碼**,
發布成 private repo `github.com/audichuang/notebooklm-mcp`,靠 `uv tool install` 裝成 console 命令
`nblm-mcp`(server)/ `notebooklm-cover`(封面 CLI)。薄 `SKILL.md` 路由層 + `references/` **在 skill repo
`audi-skill/notebooklm`(docs-only)**;兩者是一組配置,同步規則見 [docs/release-checklist.md](docs/release-checklist.md)。
完整設計理由與 live 驗證踩坑見 `docs/superpowers/notebooklm-mcp-findings.md`(經驗庫,值得先讀)。

## Commands

```bash
# 本 repo 開發(Python 鎖在 pyproject `>=3.12,<3.13`,uv 自己選)。
# 別用 `uv pip install -e`:它不會把 venv 拉到 lock 的版本,測的跟鎖的不同版。
uv sync --extra dev

# 全套離線測試(mock client,不需網路/認證)
uv run pytest -q
#   ⚠️ 不要同時跑兩個 `uv run pytest`:`uv run` 會 uninstall/reinstall 共用 venv 裡的 editable 套件,
#   並行時另一邊會撞到套件消失的瞬間,產生與程式碼無關的假紅(v0.9.14 實際發生,序列重跑全綠)。
#   同一機制也會悄悄把 `uv pip install X==版本` 換上的套件拉回 lock 版——對特定版本跑測試見 release-checklist。

# 消費端安裝(3 VM / podcast-lab 各裝一次;@latest = 最新發版,不追 master)
uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@latest"

# 跑 MCP server(認證由 doppler 注入 NOTEBOOKLM_AUTH_JSON);repo 內開發用 uv run python -m notebooklm_mcp.server
doppler run -p notebooklm -c prd -- nblm-mcp --transport stdio
#   HTTP 模式:--transport streamable-http --host 127.0.0.1 --port 8484
#   ⚠️ streamable-http / sse「無認證」——勿綁非 loopback host(同網段可驅動帳號);
#      **且只能給單一 client**(每 session 一個 lifespan 會互相換掉 process 全域 pool,
#      見 gotchas-pool)。生產四台全走 stdio。
#   註冊進 Claude Code 見 docs/mcp-setup.md。
```

### 認證與發版(按需)

| 要做什麼 | 讀 |
|---|---|
| 設定帳號 / 換憑證 / 加 pool 槽位 / 搭測試環境 | [docs/auth-and-config.md](docs/auth-and-config.md) |
| 發 tag / 改工具契約 / 同步 skill repo / 對帳依賴版本 | [docs/release-checklist.md](docs/release-checklist.md) |

兩條不可逆的先講:①`prd` 的 `PODCAST_TOKEN_SALT` 必須**逐字等於** `dev` 的(換 salt = 已發布節目全部換 URL、訂閱者掉光);
②認證一律 `doppler secrets set --raw`,**絕不用 `secrets upload`**(它會吃掉 cookie 裡的 `$`,而指令回報成功)。

## Architecture

**結構問 codegraph**(`codegraph explore "<符號或問題>"`)或直接讀 code,那裡永遠最新;下面只給**角色導覽**與
**動之前要知道的紅線**,每個模組的設計決策在 [docs/design-notes.md](docs/design-notes.md)。

| 模組 | 角色 | 動之前 |
|---|---|---|
| `app.py` | FastMCP app + 工具註冊 + lifespan(多帳號 client pool)+ 多 transport main | 憑證落檔與重鑄護欄見 [gotchas-pool](docs/gotchas-pool.md) —— **這一區破功不會當場出錯**(cookie 被本 process 重鑄是另外兩台 VM 下次啟動才掛;帳號記錯是兩邊都成功);決策推導 [ADR-0010](docs/adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md) |
| `server.py` | thin launcher(可當 `__main__` 跑) | — |
| `tools_basic.py` | notebook / source / artifact / `chat_ask` 的薄包 + 讀取觀測面 | — |
| `tools_podcast.py` | manifest-backed audio attempt:durable generate / reconcile / adopt / finalize / retract | **先讀 [ADR-0009](docs/adr/0009-retracted-attempts-are-tombstones.md)** —— 四個不變式各自被真實事故驗證過,而踩過的坑是**只補一條路徑**(attempt 建立有兩個分支、清理義務有三個入口)。**寫任何狀態相關的指引訊息前先讀 [gotchas-attempt.md](docs/gotchas-attempt.md) 的紅線**——一律由 `_attempt_capabilities()` / `_attempt_next_step()` 產生,不准手寫 if/else,這個根因已現形七次 |
| `naming.py` | Studio / 回錄鐵律 vs serial RSS 標題:剝掉匹配的 `EP{NN}. `/`EP{NN} ` 再套 `EP{NN} 正文`(鐵律正本在 skill §Episodic) | 改命名只動這裡(`tools_podcast._episode_label` 是別名);cover `__TITLE__` 與 finalize label 都走它,別各處自己拼字串 |
| `tools_artifacts.py` | 簡報 PDF / 研讀 Markdown 按需生,路徑回寫 manifest;三支生成都走共用配額 failover。也放兩支 episode 級 manifest writer(`episode_set_description` / `episode_set_publication_state`) | [gotchas-publish](docs/gotchas-publish.md);稽核面是 episode 級不是 attempt,見 [ADR-0011](docs/adr/0011-attachment-failover-buys-audit-with-an-episode-field-not-an-attempt.md) |
| `_failover.py` | 配額 failover 的**唯一**迴圈(音檔 `_dispatch_audio_with_failover` 與附件三支共用) | 五條紅線在它的模組 docstring,各是一次事故 —— 改分類/終止性要在那裡改,別在呼叫端加分支。家族差異用三個 callback 表達(`record_failover=None` = 沒稽核面 = 不換帳號),**別為了防半接線併成一個** —— v0.9.17 做過又退掉,理由在 ADR-0011 末段 |
| `tools_research.py` | Web / Deep Research 的薄包,**三支分開**(start / wait / import) | [gotchas-research](docs/gotchas-research.md);分開是為了對齊 ADR-0001 的 attempt/resume 紀律,不是為了彈性 |
| `tools_publish.py` + `publish/` | 整季發布成 Apple 合規 RSS;`publish/` 是純邏輯(離線可測)。`publish/state.py` = 發布層 manifest 欄位閘的正本(`publication_state` 白名單、`retired`;讀它的 publish 與寫它的 artifacts 共用) | [gotchas-publish](docs/gotchas-publish.md);**白名單別搬回 `tools_publish`** —— 會炸循環 import,實測踩過 |
| `generation_input.py` | frozen generation-input bundle + `attempt-binding.json` sidecar | 驗證與綁定都在**第一個遠端副作用之前**(ADR-0001);圍籬由 host 的 `workspace_root` 宣告(ADR-0012) |
| `_sources.py` | caller 指名的 `source_ids` 驗證與對帳 | **重生某一集不指名,後面各集的回錄會洩進那一集**;選哪幾筆的政策留 host(ADR-0007) |
| `_errors.py` | `NotebookAccessDenied` / `is_permission_denied`(`tools_basic` 與 `tools_podcast` 共用) | 各寫一份 = 上游改 `rpc_code` 時只有一處被改到 |
| `_status.py` `_atomic.py` `_text.py` `runtime.py` `languages.py` `enums.py` | generation-status 防護 / 原子寫入 / 文字正規化 / client holder / 白名單 / enum 映射 | `_atomic` 見 [gotchas-files](docs/gotchas-files.md) |
| `auth_probe.py` `auth_cli.py` `cover_cli.py` `assets/` | 認證預檢 / headless 建檔備援 / 封面 CLI + 凍結的 HTML template | 封面見 [gotchas-publish](docs/gotchas-publish.md) |
| `tests/test_contracts.py` | 用 `inspect.signature` 鎖住 `notebooklm-py` 公開 API 的離線 tripwire | 對**實裝版本**跑,別信 `_research/` 的 HEAD clone。**讀 body 的 `getsource` 斷言一律對 `notebooklm._web.*`**——0.8.2 起公開 facade 是 ABC(另一半是 android backend),對 ABC 抓原始碼不會爆、只會靜默恆真;簽名斷言(`_params`)留在 facade |
| `scripts/` | CI 硬檢查(`check_skill_sync.py`)、認證/測試環境腳本、`published_at` 的兩支一次性修復、`eval_harness/`(routing eval:真 schema + dry-run stub,不碰帳號) | backfill = 重生漂移、reorder = 亂序生成造成的集序錯位;都 dry-run 預設、走 `ManifestStore` |

**文件的分工**:[CHANGELOG](CHANGELOG.md) = 各版本改了什麼/為什麼/踩到什麼事故(**版本敘事只寫在那裡,不要回填進本檔**);
[docs/adr/](docs/adr/) = 能力邊界決策(**砍掉已規劃的 scope 也要留一支** —— research namespace 曾因此隱形 38 集);
[docs/design-notes.md](docs/design-notes.md) = 模組設計細節;`docs/gotchas-*.md` = 按需載入的雷區(路由表見 §Gotchas 末尾);
`docs/superpowers/` = 設計/計畫/findings。**SKILL.md 路由層 + references 不在本 repo**,在 `audi-skill/notebooklm`。

## Gotchas(notebooklm-py 0.8.2,pin `>=0.8.2,<0.9`;以**實裝版本**為準,不是 GitHub HEAD)

- **`mcp[cli]` 必須有上界(`>=1.27,<2`)**:`uv tool install git+…` **不讀 `uv.lock`**,消費端每次安裝都自由解析成當下
  最新 —— 上界擋 2.0 被靜默吃進去。lock 與實裝之間的漂移會自己長回來(已量到三次,且曾帶行為差異),
  發版時照 release-checklist 對帳。
- **server 命令叫 `nblm-mcp`,不是 `notebooklm-mcp`**:`notebooklm-py` 自己宣告了同名 script,同一個 tool venv 只留
  最後寫入的那份、實測上游贏(見 [docs/notebooklm-py-0.8-upgrade.md](docs/notebooklm-py-0.8-upgrade.md) §撞名)。
  **別裝 `notebooklm-py[mcp]`**。
- **上游/NotebookLM 行為突變時的情報站**:`_research/notebooklm-mcp-cli`(jacob-bd)的 CHANGELOG.md 與
  docs/KNOWN_ISSUES.md —— bl 漂移、cookie 語意、RPC schema 變動幾乎都最先出現在那;再對照 notebooklm-py 的 GitHub issues。
- **文字清理一律走 `_text`**(`strip_inline_emphasis` / `_CITATION_RE` / `norm`),新程式碼別自己寫 regex。
  `chat_ask` 的 `strip_citations` 預設 **False**(`episode_set_description` 預設 True)——上游三種靜默
  行為與各自的緩解在 [gotchas-sdk](docs/gotchas-sdk.md)。
- **`Artifact.source_ids` 是生成當下的歷史快照**:可以看「這顆用哪些來源生的」,**不可反查現存 source**(會查到已刪除的 id)。
- **`source_delete` 查無此 id 就不發 destructive RPC、也不 raise**(回 `was_present=False`):retract 的清理迴圈要能在
  response 遺失後重放。刪除後 `source_fulltext` 仍讀得回全文是預期,清理義務仍有效 —— 前提由 `tests/test_contracts.py`
  釘住,它紅就要重新論證(見 [gotchas-attempt](docs/gotchas-attempt.md))。
- **遠端 mutation 前用 `artifacts.get_or_none` 做便宜 preflight**(RPC 只靠 `artifact_id` 定位,錯配 ID 伺服器不擋),但那
  不是 durable attempt:slides/report 家族的**重入**是共有架構債,修法是 attachment attempt、不是替單支拆 kickoff/finalize
  (ADR-0011;見 [gotchas-publish](docs/gotchas-publish.md))。

### 按需載入的 gotchas(只在動到那一塊時讀)

| 動到什麼 | 先讀 |
|---|---|
| research 三支工具 | [docs/gotchas-research.md](docs/gotchas-research.md) |
| 發布 / 附件 / 封面 | [docs/gotchas-publish.md](docs/gotchas-publish.md) |
| 多帳號 pool 的憑證 / lifespan / dispatch(`_cookies.py`) | [docs/gotchas-pool.md](docs/gotchas-pool.md) |
| 直接呼叫 notebooklm-py(含文字/來源/probe 的行為差異) | [docs/gotchas-sdk.md](docs/gotchas-sdk.md) |
| attempt 狀態機 / manifest / 清理義務(`manifest_store.py` / `audio_finalize.py`) | [docs/gotchas-attempt.md](docs/gotchas-attempt.md) |
| 寫檔 / 上傳來源(`audio_finalize.py`) | [docs/gotchas-files.md](docs/gotchas-files.md) |

## Conventions

- 全程繁體中文註解/文件。TDD:測試先紅再綠,每任務一 commit,**在實作階段達成,不要事後切 patch**:
  `git apply --cached --unidiff-zero` 實測把一整個 helper 插進另一個函式的註解中間、三個 commit 全錯,而 `uv run pytest`
  跑的是 working tree 所以照樣全綠。派 agent 做多條修正時逐條做完逐條 commit;混在一起就寧可一個 commit。
  **commit 的內容 = 驗證過的內容,比 commit 粒度重要。**
- commit 訊息寫清楚「症狀 + 根因 + 為何這樣修」(commit 與 docs 是團隊經驗庫)。
- **驗收分三層,而且三層都自己跑 —— 不要產出「可貼的啟動 prompt」叫使用者開 session。**
  headless `claude -p --model sonnet` 每題約 $0.2,所以跑得起「每題 n 次取平均 + 盲評 + 每次改完重跑」;
  交 prompt 只跑得到一次、拿不到數字,而且改一次就要再麻煩人一次。手段與**三個會讓數字說謊的坑**見
  [docs/acceptance-testing.md](docs/acceptance-testing.md),實跑紀錄見
  [docs/acceptance-v0.9.26-eval.md](docs/acceptance-v0.9.26-eval.md)。
  1. **沒碰遠端副作用** → 離線測試 + 用既有實測前提推導結案(`source_delete` 的清理義務、v0.9.2 的 owner
     定位都是這樣結的)。**推導要逐條指出前提在哪次實測被證明**,不能只說「應該沒事」。
  2. **改到呼叫端讀得到的那一面**(工具描述 / `_INSTRUCTIONS` / skill 文字)→ `claude -p` + dry-run stub
     (`scripts/eval_harness/`):供應**真實** schema 但不執行任何呼叫,零帳號風險、零配額。
  3. **碰到 `generate` / `add_user` / `delete`** 這類會在雲端留下東西的呼叫 → 真帳號,同樣自己跑
     (`doppler run -c dev -- claude -p …`),跑完自己清測試產物。**只有不可逆的才停下來問**:
     發布到公網 feed、刪既有 notebook/source、動到別人已訂閱得到的東西。
- 🔴 **「push 了」不等於「出貨了」。** 消費端裝的是 **tag**,而 `retag-latest.sh` 只把 `latest` 指到
  **最高的 semver** —— 發版後才修的東西不再發一版,等於只修給自己看(v0.9.27 就是為這件事發的)。
  收工前**從中性目錄**驗實裝的那一份並印 `module.__file__`:cwd 排在 `sys.path` 最前面,在 repo 目錄下
  驗會載到 repo 副本,行為看起來全對。詳見 [docs/release-checklist.md](docs/release-checklist.md)。
- **tripwire 要驗「我們真的做得到什麼」,不是「上游宣告了什麼」。** v0.9.25 的 blocker:
  `test_every_sdk_enum_member_…` 問「每個 SDK enum 成員有沒有被交代」,於是「加進白名單」看起來
  就是讓它變綠的正解 —— 但 enum 是 backend-neutral、能力是 backend-specific,那個白名單開出一個
  伺服器生不出來的死選項,真實驗收才擋下。**一條問錯問題的 tripwire 比沒有更糟:它會把人推向錯答案。**
  寫 contract test 時先問「這條紅了,正確的反應是什麼」——如果答案是「照它說的加一筆」就重寫它。
- **大型修復輪派 agent:實作 → 獨立審查 → 主迴圈裁決**,四條操作紀律(各是一次事故)見
  [docs/agent-review-playbook.md](docs/agent-review-playbook.md);修正輪之後再派一輪**只看成品、不看原 findings**的複審。
