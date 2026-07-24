# NotebookLM MCP 重構：決策與踩坑經驗庫

**日期**：2026-06-07 ~ 06-08
**對象**：把 NotebookLM skill 重構為「自建薄 MCP（建在 `notebooklm-py`）+ 薄 SKILL.md + 確定性續集工具」。

本文記錄**決策理由**與**驗證時踩到的真實坑**——這些是 offline 測試抓不到、只有 live 才暴露的東西，是最該留存的經驗。配套：spec(`2026-06-07-notebooklm-mcp-redesign-design.md`)、plan(`2026-06-07-notebooklm-mcp-redesign.md`)。

---

## 1. 關鍵決策（為何這樣設計）

### 引擎選 `notebooklm-py`，不換 Jacob `notebooklm-mcp-cli`
多代理對抗式研究推翻了「Jacob 比較好」的假設：
- Jacob 對語言是自由字串 passthrough、不驗證；**繁中為核心的 skill 反而 `notebooklm-py` 更對**。
- `notebooklm-py` 有 ADR、穩定性契約、RPC 健康監控、~7x 測試；認證會主動續命 cookie。Jacob 輪轉後不重存 cookie → 遠端 VM 靜默失效。

### MCP 自建薄層，不用現成（Pavel / Diet）
兩個現成 wrapper **雙雙在命脈掛掉**：零 `zh_Hant` plumbing、mp3 回傳未接線、生成類型不全，且都帶實證 bug。結論：**只要得改原始碼加 `zh_Hant`（一定要），「直接依賴」就出局——橫豎是 fork**，不如乾淨自建，從它們抄 transport / 契約測試紀律即可。

### 續集編排：不放 Workflow 工具、不放 LLM 在迴圈
連續性來自 **NotebookLM 轉錄回傳的前集 mp3**，不是我們的模型。判斷只發生在「大綱」前置點（Opus 規劃、人核可）；大綱定稿後是純程式碼迴圈 → **穩定可復現**。

### 認證：Doppler 為硬性需求（3 VM 同步）
`notebooklm-py` 原生讀 `NOTEBOOKLM_AUTH_JSON`，且該變數存在時**跳過磁碟寫回**（唯讀）→ 多 VM 各自唯讀不漂移。登入在有 GUI 的機器做一次 → `sync-auth.sh` 推 Doppler → 所有無頭機消費。

2026-07-06 修正：MCP 不再啟用 SDK `keepalive=`，且 inline auth 時會暫時設
`NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`。原因是 `from_storage()` 冷啟動與背景
keepalive 都會打 RotateCookies；env-var auth 沒有 storage path 可寫回,三台 VM / 多個
stdio process 會各自輪替出只存在記憶體的新 cookie,讓 Doppler 裡的基準 cookie 變成舊狀態。

### 2026-07-24 P0：可靠性靠 durable attempt，不靠長 request 活著

- `attempt_id` 是 MCP 先持久化的本地產製身分；`artifact_id` 只是 NotebookLM 對該
  attempt 的遠端映射。遠端受理不明時先 reconciliation，不能把 timeout 當失敗後重生。
- FastMCP request 可能隨 client 斷線被取消；可靠性來自每個副作用前後的 manifest
  checkpoint 與重呼。`podcast_series` 只在前集 postconditions 完成後推進下一集。
- `start=N` 是 execution lower bound／caller trust boundary，不是 regenerate flag；
  範圍內已完成集會 skip。要重生必須另走明確操作，P0 不把它塞進 `start`。
- manifest-backed attempt 優先由 `podcast_series` auto-resume 或
  `podcast_episode_reconcile` 補綁 artifact，再由 series／resume finalize；手帶
  `artifact_id` 的 `podcast_episode_resume` 保留作 standalone／legacy fallback。
- P0 fail-closed 拒絕 `manifest_path + prior_mp3_path`，避免未 checkpoint 的 continuity
  upload；legacy completed output 必須保存並驗證明確 `feedback_source_id`，不能只靠同名。
- completed finalize resume 仍會重新驗證遠端 feedback source 的 id、名稱、media kind
  與 ready 狀態；本機檔案 hash 正確不代表 continuity 仍存在。
- MCP 提供可靠生成 primitive，不強制人耳 QA、protected facts 或審批流程；host 可選用。
- `<manifest>.lock` 是 advisory lock：外部 maintenance script 若直接覆寫 JSON 仍可能
  lost update，必須改用 MCP tool 或同一 `ManifestStore`。

---

## 2. Live 驗證踩到的坑（0.3.4 實際行為 vs 最新文件）

> 教訓：**pin 的版本（0.3.4）與 GitHub HEAD 文件/原始碼有落差**。契約測試該對「實際安裝的版本」跑，clone 的參考源只能當線索。

### 坑 1：`from_storage()` 在 0.3.x 是必須 `await` 的 coroutine
- 最新文件（v0.5.0+）寫「免 await」的 `async with NotebookLMClient.from_storage()`；**0.3.4 早於 v0.5.0，必須 `async with await ...`**。
- 影響：`server.py` lifespan 寫成免 await → **一啟動就 `TypeError`**。offline 測試用 FakeClient 從不碰真 `from_storage`，所以漏掉；server boot 才暴露。
- 修法：`async with await NotebookLMClient.from_storage()`，加 lifespan 回歸測試（用 fake from_storage 驅動，漏 await 即 TypeError）。
- Commit：`fix: await from_storage() in lifespan`

### 坑 2：`GenerationStatus` 沒有 `artifact_id`，`task_id` 本身就是 artifact id
- SDK docstring 明寫：「task_id and artifact_id are the same identifier」。`GenerationStatus` 欄位只有 `task_id, status, url, error, error_code, metadata`。
- 影響：`getattr(status, "artifact_id", None)` **永遠是 None** → `download` 退化抓「最新」（多 artifact 時會抓錯）、`rename` 傳 None（不生效）。1–2 集序列剛好「最新=目標」掩蓋了問題；manifest 的 `"artifact_id": null` 才露餡。
- 修法：用 `status.task_id` 當 artifact id；把 FakeClient 改成忠於真實 SDK（只給 `task_id`）逼出正確性；加契約測試（`GenerationStatus` 無 `artifact_id` 欄位）。live 確認 `ARTIFACT_TITLES: ['EP01']`（rename 真的命中）。
- Commit：`fix: derive artifact_id from task_id`

### 坑 3：0.3.4 的 `sources.add_file` 沒有 `title` 參數
- clone 的 HEAD 有 `title`，但 pin `<0.4` 裝到的 0.3.4 沒有。契約測試（抄 Diet 的 `inspect.signature` 紀律、改測公開 API）**正是為此而設**，它在實作期就擋下了這個差異。
- 影響：回傳的 mp3 來源無法在 `add_file` 當下命名。
- 對策：用獨立的 `sources.rename(notebook_id, source_id, new_title)`（0.3.4 有）在上傳後補命名。

### 坑 4：無頭機無法跑 `notebooklm login`（需 X server）
- 互動式 Google OAuth 要有頭瀏覽器；無頭 VM 報 `Missing X server or $DISPLAY`。
- 對策：登入在有 GUI 的機器做，推 Doppler，無頭機消費——這正是 Doppler 架構的用意。本機登入機才裝 `[browser]` extra（pyproject 可選 `login` 群組）。

### 坑 5：`python -m server` 的 `__main__` 雙載入 → MCP 暴露 0 個工具（最隱蔽）
- 第一版把 `mcp = FastMCP(...)` 放在 `server.py`，工具用 `from .server import mcp` 註冊。但 `python -m notebooklm_mcp.server` 把 server 載成 `__main__`，工具的 `from .server import mcp` 又把它當 `notebooklm_mcp.server` **再載一次** → **兩個 `mcp` 實例**：工具註冊在一個，`run()` 服務另一個（空的）。
- 影響：server 啟動、MCP 握手成功，但 `tools/list` 回傳 **0 個工具**。**單元測試（直接 import 函式）與直連函式的 live 測試全繞過協定 → 完全沒抓到**；只有真正的 MCP `tools/list` 才照出來（`TOOL_COUNT: 0`）。註冊進 Claude Code 會是「連上但零工具」。
- 修法：把 app + 工具註冊抽到專屬 `app.py`（**永不當 `__main__`**），`server.py` 變薄只 `from .app import main`。確保任何啟動方式（`-m`、console script、import）都是同一個 `mcp`。
- 教訓：**MCP 一定要用真實協定 `tools/list` 測**(`mcp.client.stdio`)，別只測「函式可呼叫」或「server 啟動無 traceback」。並注意 `stdio_client` 預設不傳完整 env，需 `StdioServerParameters(env=dict(os.environ))` 才帶得到 `NOTEBOOKLM_AUTH_JSON`。
- Commit：`fix: serve tools from a dedicated app module`

---

## 3. 命名統一 + 完整記錄（每集自上傳）

⚠️ **重構時弄丟、又補回的鐵律**：舊 skill 明寫「每一集**包含最後一集**都要上傳音檔回筆記本」。第一版 `podcast_series` 只在「生下一集之前」上傳**前一集**當回饋 → **最後一集永遠不進來源區**，記錄不完整、命名與工作室對不齊（使用者實際從 UI 抓到：來源出現裸檔名 `ep02.mp3`）。

**修正後設計——每集生成後上傳「自己」的 mp3 為命名來源：**
- **工作室 artifact**：生成完成後、下載前 `artifacts.rename(... "EP{n:02d}")`（先命名再下載）。
- **來源（本集自己的 mp3）**：下載後 `add_file(mp3, mime="audio/mpeg")` → `sources.rename(... "EP{n:02d}")` — identical to the Studio artifact name。

效果：
- 每集（含最後一集）都在來源區、命名與工作室一致（`EP01` artifact ↔ `EP01` source (identical string)）。
- 下一集生成時自動看到前集來源（`generate_audio` 用全部 sources）→ 連續性照成立，`podcast_series` **不需要**單獨的「前集上傳」步驟，也不需 prior-mp3 threading。
- 教訓：**移植舊流程時，逐項對照原始 skill 的「禁止事項/鐵律」清單**，別只看 happy path。

---

## 4. 連續性驗證方式與結果（文字，非聽覺）

無法「聽」音檔，但可**文字驗證**：把第 N 集 mp3 上傳為來源 → `get_fulltext` 取逐字稿 → 讀內容判斷。

**驗證結果（2 集咖啡史測試，NB `44ee4e52`）：**
- ✅ **連續性成立**：EP02 開頭明確回顧 EP01 具體內容（「上一集…回顧…伊索比亞的牧羊人卡爾迪」「上次也提到」脂肪能量球），證明回傳 mp3 → 轉逐字稿 → 餵下一集的機制有效。
- ✅ **命名統一**：工作室 `['EP02','EP01']`、來源含 `EP01`（與工作室同名）。
- ⚠️ **系列重疊（非程式 bug，是提示詞問題）**：測試 brief 太鬆，EP01「只講起源」卻講到葉門壟斷+歐洲走私，導致 EP02 大量重疊。**教訓：每集 brief 必須明確劃界「本集只講 X，Y 留待下集」——這正是 Opus 大綱層的職責。**

**踩坑：逐字稿關鍵字比對的假陰性。** NotebookLM 的 `get_fulltext` 會在**每個 CJK 字之間插空格**（「上 一 集」），naive 子字串比對（搜 "上一集"）會找不到 → 假性「無連續性」。比對前先 `"".join(text.split())` 去空格。

**踩坑：逐字稿是對 AI 生成音檔的 STT，有同音錯字**（夜門/葉門、風迷/風靡、殖名地/殖民地、沖泡/充泡）。音檔本身應正確，但逐字稿會被下一集當記憶吃進去——錯字理論上會傳遞，實測仍能正確回顧，可接受。

---

## 5. 深度測試結果（Workflow 多維度對抗式驗證）

以「先 scout 收集 live 證據 → Workflow 對證據做多維度對抗式驗證」的混合法跑了一輪深度測試（2 集系列 + 真實 MCP `tools/list` + pytest + SKILL.md）。

- **happy path 7/7 維度 PASS**：MCP 協定暴露 13 工具、SKILL↔MCP 零漂移、命名統一、連續性+範圍、zh_Hant、manifest、pytest。
- **真 bug（已修）**：見坑 5（`python -m` 暴露 0 工具）——這是 scout 階段揪出的最重要問題。
- **critic 報的 P0「resume 斷連續性」→ 實證駁回**：critic 誤以為自上傳來源只活在單一 process。實際上 `add_file` 上傳到 NotebookLM **伺服器端**，跨 process 持久；用全新 process 列同一筆記本得到 `['EP01','EP02','Seed']`，`start=3` resume 時前集來源本就還在 → 連續性不斷。**教訓：對 AI 給的審查發現要工程驗證,不盲信。**
- **真正有效的覆蓋缺口 → 已補測試**：resume 連續性（模擬伺服器端持久 + 不重複上傳）、reject-then-delete（`source_delete` + 重生不污染）、錯誤路徑（壞語言碼 fail-fast、生成 timeout 只留完成集的 manifest、malformed `episodes` 前置驗證清楚報錯）。
