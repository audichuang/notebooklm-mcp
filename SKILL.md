---
name: notebooklm
description: "Manages Google NotebookLM notebooks via notebooklm-py CLI. Creates notebooks, adds sources (URLs, YouTube, PDFs, text, web research), generates content (podcasts, slides, videos, quizzes, reports, flashcards, infographics, mind maps, data tables), downloads artifacts, and queries notebooks for source-grounded answers. Use when working with NotebookLM or when the user wants to research topics, generate podcasts or slides, create quizzes, or ask questions grounded in notebook sources."
---

# NotebookLM

CLI: `notebooklm` (install: `pip install notebooklm-py`)

## Contents

* [子代理處理](#子代理處理) — 生成任務的必要模式
* [行為規範](#行為規範) — 語言設定陷阱、輸出確認
* [核心流程](#核心流程) — 認證 → 筆記本 → 來源 → 生成 → 下載
* [完整範例](#完整範例) — 研究 → 中文 Podcast
* [參考資料](#參考資料) — CLI 完整參考、疑難排解

***

## 子代理處理

生成任務需 **5-15 分鐘**，必須用 `sessions_spawn` 委派子代理等待，避免主代理阻塞超時。

不確定命令時，先執行 `notebooklm <command> --help` 確認。

### 流程

```
主代理：觸發 generate → 取得 task-id → sessions_spawn 委派子代理 → 告知用戶「生成中」
子代理：artifact wait <task-id> --timeout 300 → 完成後下載 → 返回路徑
主代理：收到路徑 → 發送給用戶
```

### 子代理等待模板

```
sessions_spawn task:"使用 exec 工具依序執行以下命令：
1. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id> --timeout 600
2. exec mkdir -p /tmp/notebooklm
3. exec doppler run -p notebooklm -c dev -- notebooklm download audio /tmp/notebooklm/podcast.mp3
4. exec ls -la /tmp/notebooklm/podcast.mp3
如果 step 1 超時，執行 exec doppler run -p notebooklm -c dev -- notebooklm artifact poll <task-id> 檢查狀態：
- in_progress → 再次 artifact wait
- completed → 繼續 step 2
- failed → 回報錯誤
最後回報下載的檔案路徑。"
label:"NotebookLM 生成"
```

### 多任務生成模板

當需要同時生成多個內容（如中英文音頻 + 簡報）時：

1. 在主代理中**依序**啟動所有 generate 任務，記錄每個 task-id
2. 用**一個**子代理依序 wait + download 所有任務
3. 下載路徑必須包含語言/類型標識，避免覆蓋

```
sessions_spawn task:"使用 exec 工具依序執行以下命令：
1. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-1> --timeout 600
2. exec doppler run -p notebooklm -c dev -- notebooklm download audio /tmp/notebooklm/<描述>_zh.mp3
3. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-2> --timeout 600
4. exec doppler run -p notebooklm -c dev -- notebooklm download audio /tmp/notebooklm/<描述>_en.mp3
5. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-3> --timeout 600
6. exec doppler run -p notebooklm -c dev -- notebooklm download slide-deck /tmp/notebooklm/<描述>_zh.pdf
7. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-4> --timeout 600
8. exec doppler run -p notebooklm -c dev -- notebooklm download slide-deck /tmp/notebooklm/<描述>_en.pdf
9. exec ls -la /tmp/notebooklm/
如果某個 wait 超時，用 artifact poll 檢查狀態後繼續。
最後回報所有下載的檔案路徑。"
label:"NotebookLM 多任務生成"
```

### 禁止事項

* ❌ 主代理直接執行 `artifact wait`（會阻塞）
* ❌ 超時後不檢查狀態就放棄
* ❌ 不確定命令就猜測（先用 `--help`）

***

## 行為規範

### 確認輸出格式

用戶請求模糊時，先確認想要的格式：

| 用戶說 | 詢問確認 |
|--------|----------|
| 「幫我講解」「完整整理」 | Podcast? PPT? 報告? 測驗? |
| 「學習這篇」 | Podcast? 思維導圖? 測驗題? |

### 語言設定

**預設輸出為英文！** 如需中文，必須明確指定：

| 命令 | 中文設定方式 |
|------|-------------|
| audio, slide-deck, video, infographic, report, data-table | `--language zh_Hant` |
| quiz, flashcards | DESCRIPTION 寫「請用繁體中文...」 |
| mind-map | 無法指定，取決於來源內容 |

⚠️ **語言代碼用底線不用連字號**：`zh_Hant`(繁中), `zh_Hans`(簡中), `en`, `ja`, `ko`

執行 `notebooklm language list` 查看所有支援的語言代碼。

***

## 核心流程

```
1. 認證 → 2. 筆記本 → 3. 加入來源 → 4. 生成內容 → 5. 下載
```

### Step 1: 認證

```bash
doppler run -p notebooklm -c dev -- notebooklm list          # 檢查登入狀態
doppler run -p notebooklm -c dev -- notebooklm login         # 若未登入，開啟瀏覽器認證
```

#### Doppler Integration

認證由 [Doppler](https://doppler.com) 統一管理，環境變數：`NOTEBOOKLM_AUTH_JSON`

```bash
# 首次設定
notebooklm login && bash scripts/sync-auth.sh

# 日常使用
doppler run -p notebooklm -c dev -- notebooklm list

# 認證過期
notebooklm login && bash scripts/sync-auth.sh
```

### Step 2: 筆記本

```bash
doppler run -p notebooklm -c dev -- notebooklm list                    # 列出所有筆記本
doppler run -p notebooklm -c dev -- notebooklm create "標題"           # 建立新筆記本
doppler run -p notebooklm -c dev -- notebooklm use <notebook-id>       # 選擇筆記本
```

### Step 3: 加入來源

```bash
doppler run -p notebooklm -c dev -- notebooklm source add-research "主題" --mode deep    # 網路研究
doppler run -p notebooklm -c dev -- notebooklm research wait --import-all --timeout 180  # 等待匯入

doppler run -p notebooklm -c dev -- notebooklm source add "https://..."                  # URL / YouTube
doppler run -p notebooklm -c dev -- notebooklm source add "/path/to/file.md"             # 本地檔案
```

**⚠️ 多個研究必須序列化**，不可同時啟動，否則 `research wait` 會卡住。

### Step 4: 生成內容

查詢參數：`doppler run -p notebooklm -c dev -- notebooklm generate <type> --help`

可用類型：`audio`, `video`, `slide-deck`, `quiz`, `report`, `flashcards`, `infographic`, `mind-map`, `data-table`

```bash
doppler run -p notebooklm -c dev -- notebooklm generate audio "教學講解" --format deep-dive --language zh_Hant
doppler run -p notebooklm -c dev -- notebooklm generate quiz "請用繁體中文製作測驗" --difficulty hard
```

### Step 5: 等待與下載

```bash
doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id> --timeout 600     # 等待完成
doppler run -p notebooklm -c dev -- notebooklm artifact poll <task-id>                    # 查詢狀態
doppler run -p notebooklm -c dev -- notebooklm artifact list                              # 列出所有 artifacts

mkdir -p /tmp/notebooklm
doppler run -p notebooklm -c dev -- notebooklm download audio /tmp/notebooklm/podcast.mp3
doppler run -p notebooklm -c dev -- notebooklm download slide-deck /tmp/notebooklm/slides.pdf
```

**⚠️** 沒有 `artifact status` 命令，用 `artifact poll` 或 `artifact list`。

### Step 6: 查詢筆記本

```bash
doppler run -p notebooklm -c dev -- notebooklm ask "你的問題"
```

***

## 完整範例

### 研究主題 → 中文 Podcast

```bash
doppler run -p notebooklm -c dev -- notebooklm create "AI 研究"
doppler run -p notebooklm -c dev -- notebooklm use <id>
doppler run -p notebooklm -c dev -- notebooklm source add-research "AI 發展趨勢 2026" --mode deep
doppler run -p notebooklm -c dev -- notebooklm research wait --import-all --timeout 180
doppler run -p notebooklm -c dev -- notebooklm generate audio "深度講解" --format deep-dive --length long --language zh_Hant
# 以下步驟應委派子代理執行（見子代理等待模板）
doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id> --timeout 600
doppler run -p notebooklm -c dev -- notebooklm download audio /tmp/notebooklm/podcast.mp3
```

***

## 常見錯誤

| 錯誤 | 解決方案 |
|------|----------|
| Not logged in | `notebooklm login` |
| No notebook selected | `notebooklm use <id>` |
| Generation timeout | 增加 `--timeout 600` |
| Unknown language code | 用底線：`zh_Hant` 非 `zh-TW`，執行 `language list` 查詢 |

## 參考資料

* [cli-reference.md](references/cli-reference.md) — 完整 CLI 命令參考（generate 各類型參數詳解）
* [troubleshooting.md](references/troubleshooting.md) — 疑難排解指南（認證、研究、生成、Doppler）
