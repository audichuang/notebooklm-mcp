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

---

## 3. 命名統一設計（工作室 + 來源一致）

讓筆記本兩個區域命名對齊、清楚可辨：
- **工作室 artifact**：生成完成後、下載前，`artifacts.rename(... "EP{n:02d}")`（先在 NotebookLM 命名，再下載）。
- **來源（回傳的前集 mp3）**：`add_file` 後 `sources.rename(... "EP{n-1:02d} 對話紀錄")`，補上 0.3.4 `add_file` 缺的標題。

順序：回傳前集 mp3 + 命名來源 → 生成本集 → 等 → **命名 artifact** → 下載。

---

## 4. 連續性驗證方式與結果（文字，非聽覺）

無法「聽」音檔，但可**文字驗證**：把第 N 集 mp3 上傳為來源 → `get_fulltext` 取逐字稿 → 讀內容判斷。

**驗證結果（2 集咖啡史測試，NB `44ee4e52`）：**
- ✅ **連續性成立**：EP02 開頭明確回顧 EP01 具體內容（「上一集…回顧…伊索比亞的牧羊人卡爾迪」「上次也提到」脂肪能量球），證明回傳 mp3 → 轉逐字稿 → 餵下一集的機制有效。
- ✅ **命名統一**：工作室 `['EP02','EP01']`、來源 `['EP01 對話紀錄','Seed']`。
- ⚠️ **系列重疊（非程式 bug，是提示詞問題）**：測試 brief 太鬆，EP01「只講起源」卻講到葉門壟斷+歐洲走私，導致 EP02 大量重疊。**教訓：每集 brief 必須明確劃界「本集只講 X，Y 留待下集」——這正是 Opus 大綱層的職責。**

**踩坑：逐字稿關鍵字比對的假陰性。** NotebookLM 的 `get_fulltext` 會在**每個 CJK 字之間插空格**（「上 一 集」），naive 子字串比對（搜 "上一集"）會找不到 → 假性「無連續性」。比對前先 `"".join(text.split())` 去空格。

**踩坑：逐字稿是對 AI 生成音檔的 STT，有同音錯字**（夜門/葉門、風迷/風靡、殖名地/殖民地、沖泡/充泡）。音檔本身應正確，但逐字稿會被下一集當記憶吃進去——錯字理論上會傳遞，實測仍能正確回顧，可接受。
