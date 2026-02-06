---
name: notebooklm-unified
description: Unified NotebookLM assistant powered by notebooklm-py CLI. Use when user wants to (1) Research topics and auto-import web sources to NotebookLM, (2) Generate podcasts, slides, mind maps, quizzes, videos, reports from content, (3) Query notebooks for source-grounded answers, (4) Add URLs, YouTube, PDF, text sources to notebooks. Triggers on "research about", "generate podcast", "make slides", "create quiz", "ask my notebook", "NotebookLM", "上傳到筆記本", "生成播客", "做成PPT", "幫我講解", "學習".
---

# NotebookLM Unified Assistant

CLI 命令：`notebooklm`（需先安裝 notebooklm-py：`pip install notebooklm-py`）

---

## 🚨 子進程處理（最重要！必讀！）

NotebookLM 生成任務（audio, video, slide-deck 等）需要 **5-15 分鐘**。
**必須使用子進程（subagent / sessions_spawn）處理等待，避免超時失敗。**

### ⚠️ 執行命令前先查 --help

**不確定命令或參數時，先執行 `--help` 確認：**
```bash
notebooklm research --help      # 查看 research 子命令
notebooklm artifact --help      # 查看 artifact 子命令
notebooklm generate --help      # 查看 generate 子命令
```

### 正確流程

```
1. 主進程：觸發生成 → 取得 task-id → 告知用戶「生成中」
2. 子進程：等待完成 → 下載檔案 → 返回路徑
3. 主進程：收到結果 → 發送給用戶
```

### 使用子進程等待

當需要等待 `research wait` 或 `artifact wait` 時，**委派給子進程**：

```
使用 sessions_spawn 或 subagent 執行以下任務：

任務內容：
1. 執行 `notebooklm artifact wait <task-id> --timeout 300`
2. 如果成功完成 → 執行下載命令 → 返回檔案路徑
3. 如果超時 → 執行 `notebooklm artifact poll <task-id>` 檢查狀態：
   - 狀態是 processing → 繼續等待（再次 artifact wait --timeout 300）
   - 狀態是 completed → 執行下載
   - 狀態是 failed → 返回錯誤訊息

任務參數：
- task-id: <從 generate 命令取得>
- 類型: <audio/slide-deck/video/...>
- 輸出路徑: ~/Documents/NotebookLM/output/<filename>
- 語言: <zh_Hant 或其他>
```

### 範例：生成 Podcast 的完整流程

**主進程執行：**
```bash
# 1. 建立筆記本、加入來源
notebooklm create "Java Spring Boot 教學"
notebooklm use <notebook-id>
notebooklm source add-research "Java 25 Spring Boot 4" --mode deep
```

**子進程 #1（research wait）：**
```
委派子進程等待研究完成：
- 先執行 `notebooklm research --help` 確認可用命令
- 執行 `notebooklm research wait --import-all --timeout 180`
- 如果超時，用 `notebooklm artifact list` 檢查狀態
- 完成後返回
```

**主進程繼續：**
```bash
# 2. 觸發生成（這個很快，不需要子進程）
notebooklm generate audio "深度講解" --format deep-dive --language zh_Hant
# → 記錄返回的 task-id
```

**子進程 #2（artifact wait）：**
```
委派子進程等待生成完成：
- 執行 `notebooklm artifact wait <task-id> --timeout 300`
- 超時則用 `notebooklm artifact poll <task-id>` 檢查
- 完成後執行：
  mkdir -p ~/Documents/NotebookLM/output
  notebooklm download audio ~/Documents/NotebookLM/output/podcast.mp3
- 返回檔案路徑
```

**主進程最後：**
```
收到子進程返回的檔案路徑 → 發送給用戶
```

### 子進程超時處理邏輯

```
artifact wait --timeout 300
     │
     ├─ 成功 → 下載 → 返回路徑
     │
     └─ 超時 → artifact poll <task-id>
                    │
                    ├─ completed → 下載 → 返回路徑
                    ├─ processing → 再次 wait（重複此流程）
                    └─ failed → 返回錯誤
```

### ⚠️ 禁止事項

```
❌ 主進程直接執行 `artifact wait --timeout 600`（會阻塞 10 分鐘）
❌ 忘記把等待任務委派給子進程
❌ 超時後不檢查狀態就放棄
❌ 不確定命令就猜測（先用 --help 確認！）
```

---

## ⚠️ 行為規範（執行前必讀）

### 1. 確認輸出格式

用戶請求模糊時，**先確認想要的輸出格式**：

| 用戶說 | 詢問確認 |
|--------|----------|
| 「幫我講解」「完整整理」 | Podcast? PPT? 報告? 測驗? |
| 「學習這篇」 | Podcast教學? 思維導圖? 測驗題? |
| 「分析內容」 | 文字回答(ask)? 報告? 簡報? |

### 2. 語言設定陷阱

**預設輸出為英文！** 如需中文，必須明確指定：

| 命令 | 中文設定方式 |
|------|-------------|
| audio, slide-deck, video, infographic, report, data-table | 加 `--language zh_Hant` |
| quiz, flashcards | DESCRIPTION 開頭寫「請用繁體中文...」 |
| mind-map | 無法指定，取決於來源內容 |

**常用語言代碼：** `zh_Hant`(繁中), `zh_Hans`(簡中), `en`, `ja`, `ko`

## 核心流程

```
1. 認證 → 2. 選擇筆記本 → 3. 加入來源 → 4. 生成內容 → 5. 下載
```

### Step 1: 認證
```bash
notebooklm list          # 檢查登入狀態
notebooklm login         # 若未登入，開啟瀏覽器認證
```

### Step 2: 筆記本
```bash
notebooklm list                    # 列出所有筆記本
notebooklm create "標題"           # 建立新筆記本
notebooklm use <notebook-id>       # 選擇筆記本
```

### Step 3: 加入來源
```bash
# 網路研究（自動搜尋並匯入）
notebooklm source add-research "主題" --mode deep
notebooklm research wait --import-all --timeout 180

# 手動加入
notebooklm source add "https://..."              # URL
notebooklm source add "/path/to/file.md"         # 本地檔案
```

**⚠️ 多個研究任務注意：** 同一個 Notebook 中，**必須序列化執行**，不要同時啟動多個研究，否則 `research wait` 可能會卡住。
```bash
# ✅ 正確：一個完成後再啟動下一個
notebooklm source add-research "Topic 1" --mode deep
notebooklm research wait --import-all
notebooklm source add-research "Topic 2" --mode deep
notebooklm research wait --import-all
```

### Step 4: 生成內容

**查詢完整參數：** `notebooklm generate <type> --help`

可用類型：`audio`, `video`, `slide-deck`, `quiz`, `report`, `flashcards`, `infographic`, `mind-map`, `data-table`

**中文範例：**
```bash
# Podcast（支援 --language）
notebooklm generate audio "教學講解" --format deep-dive --language zh_Hant

# Quiz（不支援 --language，用 DESCRIPTION）
notebooklm generate quiz "請用繁體中文製作測驗" --difficulty hard
```

### Step 5: 等待與下載

```bash
# 等待生成完成
notebooklm artifact wait <task-id> --timeout 600

# 查詢狀態（如果超時或想檢查進度）
notebooklm artifact poll <task-id>    # 查詢特定任務
notebooklm artifact list              # 列出所有 artifacts

# 下載（不需要 task-id，自動下載最新完成的）
mkdir -p ~/Documents/NotebookLM/output
notebooklm download audio ~/Documents/NotebookLM/output/podcast.mp3
notebooklm download slide-deck ~/Documents/NotebookLM/output/slides.pdf
```

**⚠️ 注意：** 沒有 `artifact status` 命令！用 `artifact poll` 或 `artifact list`。

**查詢下載選項：** `notebooklm download <type> --help`

### Step 6: 查詢筆記本
```bash
notebooklm ask "你的問題"
```

## 完整範例

### 研究主題 → 中文 Podcast
```bash
notebooklm create "AI 研究"
notebooklm use <id>
notebooklm source add-research "AI 發展趨勢 2026" --mode deep
notebooklm research wait --import-all --timeout 180
notebooklm generate audio "請用繁體中文深度講解" --format deep-dive --length long --language zh_Hant
notebooklm artifact wait <task-id> --timeout 600
notebooklm download audio ~/Documents/NotebookLM/output/podcast.mp3
```

## 查詢更多參數

執行任何命令前，用 `--help` 查看完整選項：
```bash
notebooklm --help                      # 所有命令
notebooklm generate --help             # 所有生成類型
notebooklm generate audio --help       # audio 完整參數
notebooklm source --help               # source 子命令
notebooklm download --help             # download 選項
```

## 常見錯誤

| 錯誤 | 解決方案 |
|------|----------|
| Not logged in | `notebooklm login` |
| No notebook selected | `notebooklm use <id>` |
| Generation timeout | 增加 `--timeout 600` |

## 參考資料

- `references/cli-reference.md` - 完整 CLI 命令參考
- `references/troubleshooting.md` - 疑難排解指南
