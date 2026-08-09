# NotebookLM Skill 重構設計：MCP 為底 + 薄 skill + 確定性續集

**日期**：2026-06-07
**狀態**：設計定案（待寫實作計畫）
**作者**：audichuang（與 Claude 協作 brainstorming + 多輪 ultracode 研究）

> ⚠️ **這是當時的設計快照,內文刻意不回填**。有一條已經被實作推翻,而它看起來仍像可行方案:
> **`NOTEBOOKLM_REFRESH_CMD` 自癒掛鉤(§4、§9 的表格)在 v0.9.0 起被 inline auth 模式顯式
> 壓成不設定**。理由:它會走到 SDK 帶 recovery 的 cookie loader,而多帳號 pool 改用
> storage_state 檔之後那條路會在本 process 內重鑄 cookie —— 新 cookie 只活在 temp 檔裡、
> 寫不回 Doppler,另外兩台 VM 下次啟動就掛。**憑證過期的正解仍然只有 GUI 機重登 +
> `scripts/sync-auth.sh`**(見 AGENTS.md §Gotchas 的 v0.9.0 那條)。

---

## 1. 背景與動機

現有 `notebooklm-skill` 把所有東西都寫成一支 271 行的 SKILL.md，靠 prompt 紀律提醒
agent「別忘 `--json`、別忘 `-a`、別忘 `-n`、別用 `use`」，並用 `sessions_spawn` 子代理
+ Telegram heartbeat 來等待長任務。這套的脆弱來源有三：

1. **規則靠提醒**：參數約束沒有程式保證，agent 容易漏。
2. **長任務等待耦合環境**：`sessions_spawn` + Telegram heartbeat 不可靠。
3. **連續性 podcast 序列回饋**靠多步手動指令，容易漏步。

重新定位：NotebookLM 整合本質上是「能力工具」，**應該是 MCP**，skill 只當薄薄的使用
指引；長序列流程應該是**確定性程式碼**，不靠 agent 即興。

## 2. 研究結論（決定設計方向的關鍵事實）

兩輪 ultracode 多代理研究（讀原始碼 + 對抗式查核）的結論：

### 2.1 引擎選 `notebooklm-py`（Teng-Lin），不換 Jacob

對 Jacob `notebooklm-mcp-cli` 的對抗式查核推翻了兩個關鍵假設：

- ❌ **zh_Hant 功能完整**：Jacob 對語言是自由字串 passthrough、不驗證；mind-map 語言
  只有 `notebooklm-py` 支援。對繁中為核心的 skill，`notebooklm-py` 反而更對。
- ❌ **維護/成熟度 ≥ notebooklm-py**：`notebooklm-py` 有 20 份 ADR、穩定性契約、
  RPC 健康監控、~7x 測試；Jacob 無 ADR、認證輪轉後不重存 cookie（遠端 VM 會靜默失效）。

✅ 命脈「把生成的 mp3 回傳為 source」兩者皆可；`notebooklm-py` 的
`_source/upload.py:252` 明確接受 `audio/`、`video/` MIME。

### 2.2 MCP 自建薄層，不用現成

兩個建在 `notebooklm-py` 上的現成 MCP（Pavel `mcp-notebooklm` 501 行 / Diet
`notebooklm-py-diet-mcp` 1124 行）**雙雙在命脈需求掛掉**：

- ❌ **zh_Hant**：兩者皆**零語言 plumbing**（`server.py:316-383`、
  `notebooklm_mcp_server.py:537`）。
- ❌ **mp3 回傳迴圈**：兩者皆未接線（Pavel upload 限文件 `server.py:202`；Diet 音檔
  下載成 `.wav` `:519`、無音檔來源分支）。
- ❌ **生成類型**：Pavel 6/9、Diet 5/9，皆不全。
- ❌ **「自建是無腦搬運」**：兩者皆帶實證 bug（Pavel `generate_report(topic=)` →
  TypeError、`download_audio` 參數順序顛倒；Diet 把字串餵 int-enum）。

結論邏輯：**只要我們得改原始碼加 zh_Hant（一定要），「直接依賴現成」就出局——橫豎是
fork。** 既如此，乾淨自建一支 ~400–700 行薄 MCP，從 Pavel/Diet **抄組件不抄整包**。

### 2.3 認證：Doppler 是硬性需求（3 台 VM 同步）

`notebooklm-py` 原生支援：

- 讀 `NOTEBOOKLM_AUTH_JSON` 環境變數（inline storage_state JSON，免檔案 —
  `_auth/tokens.py:251`、`_auth/cookies.py:265-326`）。
- 該變數存在時**跳過磁碟 cookie 寫回 + PSIDTS recovery 寫入**（視為唯讀 —
  `_auth/storage.py:371-374`、`_auth/psidts_recovery.py:180,232`）→ 多 VM 各自唯讀、
  不漂移。
- `NOTEBOOKLM_REFRESH_CMD` 自癒掛鉤（`_auth/paths.py:18`）+ keepalive
  poke（`_auth/keepalive.py`，可用 `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 關閉）。

→ Doppler 成為這套架構的天然解，且是**必須**保留的。

## 3. 目標與非目標

**目標**

- NotebookLM 能力以**自建薄 MCP** 暴露，參數由 schema 強制、`zh_Hant` 為預設。
- 連續性 podcast 以**確定性 `podcast_series` 工具**（純程式碼迴圈）穩定復現。
- 認證透過 **Doppler** 在 3 台 VM 同步，唯讀注入、不漂移。
- SKILL.md 瘦身成薄路由層。

**非目標（v1 明確不做）**

- ❌ 逐集自動「讀逐字稿 + 重生」閘門（過度設計；連續性靠 NotebookLM mp3 回饋 +
  好大綱即可，生壞由人工重跑單集）。
- ❌ 生成迴圈內放 LLM 或 Claude Code Workflow 工具（與「穩定復現」矛盾）。
- ❌ 把 MCP 拆成獨立 PyPI 套件發佈（個人用、本機為主，YAGNI）。
- ❌ 閉環自動重生整季（級聯昂貴）。

## 4. 架構

```
意圖（本機 Claude Code / Telegram）
   │
┌──┴──────────────────────────────────────────────┐
│ 層 1  SKILL.md（薄路由層）                          │
│   簡單意圖 → 直接呼叫 MCP 工具                       │
│   連續系列 → 先出大綱（Opus）→ podcast_series        │
├─────────────────────────────────────────────────┤
│ 層 2  自建薄 MCP server（FastMCP, ~400–700 行）     │
│   schema 強制、zh_Hant 預設、字串→enum 映射          │
│   工具 ~12–14，含複合 podcast_episode / podcast_series│
│   依賴 notebooklm-py >=0.3,<0.4（pin 上限）          │
├─────────────────────────────────────────────────┤
│ 層 3  引擎 notebooklm-py（不改，當依賴）             │
│   Python API + 認證 + RPC                          │
└─────────────────────────────────────────────────┘
   認證：Doppler 注入 NOTEBOOKLM_AUTH_JSON（唯讀，3 VM 同步）
   大綱改進（可選）：一支前置一次性 workflow，不進生成迴圈
```

**判斷只發生在「大綱」前置點；大綱定稿後全是確定性腳本。**

## 5. 元件設計

### 5.1 自建薄 MCP server

**位置**：`mcp/notebooklm_mcp_server.py`（單檔）+ `mcp/auth_cli.py`（抄 Pavel 的貼
cookie 流程）。

**抄組件來源**

- Pavel `server.py:474-497`：argparse 多 transport（stdio + streamable-http + sse、
  `--host/--port`）。
- Pavel `auth_cli.py`：browser-free 貼 cookie → storage_state（headless 友善）。
- Diet `tests/test_sdk_contracts.py` 模式：`inspect.signature` 契約測試，**改測
  `notebooklm-py` 公開 `NotebookLMClient` 介面**（Diet 原版測私有 `_module`，脆）。

**自烤行為**

- 每個 generate 工具 `language` 預設 `zh_Hant`。
- 重新實作語言白名單檢查（SDK 不驗證碼，白名單在 `cli/language_cmd.py`）。
- 字串 → int-enum 映射（`AudioFormat`/`AudioLength`，後者只有 SHORT/DEFAULT/LONG）。
- `notebooklm-py` 版本上限 pin `>=0.3,<0.4`。

**工具清單（~12–14）**

| 工具 | 說明 |
|------|------|
| `notebook_create`, `notebook_list` | 筆記本 CRUD（薄包） |
| `source_add_url`, `source_add_text` | 加來源（URL / 純文字） |
| `source_add_file` | 加本機檔案，**明確支援 mp3/audio**（命脈） |
| `source_delete` | 刪來源（reject 重生時用） |
| `research_start`, `research_wait_import` | 網路研究（複合：啟動 + 等待匯入） |
| `generate_audio` | 生成音頻，`zh_Hant` 預設，回傳 `task_id` |
| `generate_artifact(type)` | 泛型生成其餘 8 類型（slide-deck/quiz/report/…） |
| `artifact_wait` | 包 `wait_for_completion(notebook_id, task_id)`（注意是 task_id） |
| `artifact_download` | 下載，**正確參數順序** `(notebook_id, output_path, artifact_id)` |
| `artifact_rename` | 重命名 artifact |
| `chat_ask` | 對筆記本提問 |
| `podcast_episode` | **複合**：單集 5 步全包（見 5.2） |
| `podcast_series` | **複合**：純程式碼迴圈跑整季（見 5.2） |

**認證生命週期**：MCP server 在 `doppler run -p notebooklm -c dev -- ...` 下啟動，
`notebooklm-py` 自動讀 `NOTEBOOKLM_AUTH_JSON`（唯讀）。Server 為**長駐單一程序**
（keepalive poke 維持 session，跨 1200s+ 長生成不掉線），client 不每次請求重建。

### 5.2 複合工具：podcast_episode / podcast_series

`podcast_episode(notebook_id, episode_brief, prior_mp3_path?, output_dir)`：

```
1. if prior_mp3_path: source_add_file(notebook_id, prior_mp3_path, mime="audio/mpeg")
                      → 等 source ready（NotebookLM 自動轉逐字稿）
2. task = generate_audio(notebook_id, language="zh_Hant", instructions=episode_brief)
3. artifact_wait(notebook_id, task.task_id)            # task_id 非 artifact_id
4. artifact_download → output_dir/ep{n}.mp3            # 正確參數順序
5. artifact_rename(task.artifact_id, "EP{n} ...")
6. return ep{n}.mp3 路徑
```

`podcast_series(notebook_id, episodes[], output_dir, start=1)`：純 Python 迴圈，逐集
呼叫 `podcast_episode`，把第 N 集回傳的 mp3 當第 N+1 集的 `prior_mp3_path`。
支援 `start` 續製。**迴圈內無 LLM、無 agent** → 穩定復現。

⚠️ **reject-then-delete 規則**：若人工判定某集需重生，先 `source_delete` 掉該集已上傳
的來源，再重生，避免壞集被餵給下一集。

### 5.3 薄 SKILL.md（路由層）

- 簡單情境（單一 podcast、問答、做 PPT/quiz/report 等）→ 直接呼叫對應 MCP 工具，零儀式。
- 連續系列情境 → 引導：先由 Opus 規劃整季大綱 + 每集 brief（使用者核可）→ 呼叫
  `podcast_series`。
- 保留語言設定表、`episodic_prompts.md` 指路；刪除 doppler 前綴提醒、`--json`/`-a`/`-n`
  提醒、sessions_spawn 模板、禁止事項清單（皆被 MCP schema / 長駐程序吸收）。

### 5.4 大綱階段（唯一判斷點）

- **v1**：Opus 在對話中一次規劃整季大綱 + 每集 brief → 使用者核可 → 才開始生成。
- **可選升級**：一支「**只做大綱改進**」的小 workflow（產數個候選大綱 → 評比 → 綜合）。
  **前置一次性，不進生成迴圈。** 等使用者想投資大綱品質時才用。

### 5.5 認證與 Doppler（3 VM 同步）

- **真相來源**：Doppler secret `NOTEBOOKLM_AUTH_JSON`（project `notebooklm`,
  config `dev`），內容為 `storage_state.json`。
- **各 VM**：MCP 在 `doppler run` 下啟動 → `notebooklm-py` 唯讀載入 env var →
  不寫回磁碟、不漂移。
- **re-auth 流程**：本機 `notebooklm login` → `scripts/sync-auth.sh` 推送到 Doppler →
  3 台 VM 下次啟動即取得最新；或設 `NOTEBOOKLM_REFRESH_CMD` 為「從 Doppler 重拉」
  腳本以免重啟自癒。
- `sync-auth.sh` **從可選提升為必須**（多 VM 同步的核心）。

## 6. 現有檔案處置

| 現有 | 處置 |
|------|------|
| `SKILL.md`（271 行） | 改寫成薄路由層 |
| `scripts/episodic_podcast.py` | **刪**，由 MCP `podcast_series` 取代 |
| `scripts/sync-auth.sh` | **保留並提升為必須**（Doppler 多 VM 同步） |
| `scripts/series_config_example.yaml` | 改為 `podcast_series` 的 episodes 輸入範例 |
| `references/cli-reference.md` | 改寫成 MCP 工具參考 |
| `references/episodic_prompts.md` | **原封保留**（純價值） |
| `references/troubleshooting.md` | 精簡，補 MCP / Doppler 段落 |
| `mcp/`（新增） | MCP server 單檔 + auth_cli + 契約測試 |

## 7. 結構決策

**單一 repo（monorepo）**：薄 SKILL.md + MCP 單檔 + 契約測試 + (可選) 大綱 workflow，
全在 `notebooklm-skill` repo 一起版控、一起 pin `notebooklm-py` 版本。將來真要給別人
用再抽成獨立套件。

## 8. 風險與緩解

| 風險 | 緩解 |
|------|------|
| `notebooklm-py` 升版改公開 API | pin `<0.4` 上限 + 公開介面契約測試（CI tripwire） |
| Google 靜默改 RPC | 依賴 `notebooklm-py` 的 RPC 健康監控；契約測試先報警 |
| Doppler secret 過期 | 本機 re-login + `sync-auth.sh`；`NOTEBOOKLM_REFRESH_CMD` 自癒 |
| 長生成（1200s+）掉線 | MCP 長駐單程序 + keepalive poke；`artifact_wait` timeout >=600 |
| mp3 回傳實際行為未跑過 live e2e | 實作 Phase 0 先跑一次真實 mp3 round-trip 驗證 |
| 自建 MCP 重蹈現成 bug | 寫單元測試覆蓋參數順序 / enum 映射 / 語言 plumbing |

## 9. 驗證關卡（實作 Phase 0，寫工具前先打勾）

1. `uv tool install notebooklm-py`（或 pin 版）+ Doppler 注入 `NOTEBOOKLM_AUTH_JSON`
   能通過 `notebooklm list`。
2. **live mp3 round-trip**：生成一段 audio → 下載 mp3 → `source_add_file` 回傳 →
   確認變成 ready 的音檔來源（兩個現成 wrapper 都沒跑過此 e2e，最高優先）。
3. `zh_Hant` 對 audio + report 實際產出繁中（不只接受 flag）。
4. `artifact_wait` 對 `task_id`（非 artifact_id）行為正確。
5. 3 台 VM 用同一 Doppler secret 唯讀啟動皆可用、互不干擾。

## 10. 開放問題

- 大綱改進 workflow 的具體形狀（候選數、評比準則）留待真正要做時再 brainstorm。
- 是否需要 `generate_artifact` 泛型工具，或 v1 只先做 `generate_audio`（其餘類型用
  既有 CLI 過渡）——實作計畫時依工作量決定。
