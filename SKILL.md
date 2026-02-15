---
name: notebooklm
description: "Manages Google NotebookLM notebooks via notebooklm-py CLI. Creates notebooks, adds sources (URLs, YouTube, PDFs, text, web research), generates content (podcasts, slides, videos, quizzes, reports, flashcards, infographics, mind maps, data tables), downloads artifacts, and queries notebooks for source-grounded answers. Use when working with NotebookLM or when the user wants to research topics, generate podcasts or slides, create quizzes, or ask questions grounded in notebook sources."
---

# NotebookLM

CLI: `notebooklm` (install: `pip install notebooklm-py`)

所有命令前綴：`doppler run -p notebooklm -c dev --`

## 核心流程

```
認證 → 筆記本 → 加入來源 → 生成內容 → 等待 → 下載
```

### 認證

```bash
doppler run -p notebooklm -c dev -- notebooklm list    # 同時檢查登入狀態
```

認證過期時見 [troubleshooting.md](references/troubleshooting.md)。

### 筆記本

```bash
notebooklm list                          # 列出所有筆記本
notebooklm create "標題"                 # 建立新筆記本
```

⚠️ **避免使用 `use` 命令**（需寫入本地 `~/.notebooklm/context.json`，VM 環境常失敗）。改用 `-n` 參數直接指定筆記本：

```bash
notebooklm source list -n <notebook-id>
notebooklm artifact list -n <notebook-id>
```

### 加入來源

```bash
notebooklm source add "/path/to/file.pdf" -n <notebook-id>           # 本地檔案
notebooklm source add "https://..." -n <notebook-id>                  # URL / YouTube
notebooklm source add-research "主題" --mode deep -n <notebook-id>    # 網路研究
notebooklm research wait --import-all --timeout 180                   # 等待匯入
```

⚠️ 多個研究必須**序列化**，不可同時啟動。

### 生成內容

查詢參數：`notebooklm generate <type> --help`

可用類型：`audio`, `video`, `slide-deck`, `quiz`, `report`, `flashcards`, `infographic`, `mind-map`, `data-table`

```bash
notebooklm generate audio "教學講解" --format deep-dive --language zh_Hant --json -n <notebook-id>
```

**必須加 `--json`** 才能取得 task-id 供後續下載使用。

### 等待與下載

⚠️ **download 預設下載 `--latest`（最新一個同類型 artifact）。多個同類型 artifact 時，不加 `-a` 會重複下載同一個檔案。**

```bash
# 等待與查詢
notebooklm artifact wait <task-id> --timeout 600      # 等待完成
notebooklm artifact poll <task-id>                     # 單次查詢狀態
notebooklm artifact list -n <notebook-id>              # 列出所有 artifacts

# 下載——必須用 -a 指定 artifact ID
notebooklm download audio -a <task-id> /tmp/podcast.mp3
notebooklm download slide-deck -a <task-id> /tmp/slides.pdf
```

沒有 `artifact status` 命令，用 `artifact poll` 或 `artifact list`。

***

## 子代理處理

生成任務需 5-15 分鐘，**主代理禁止直接執行 `artifact wait`**（會阻塞超時）。

流程：

```
主代理：generate --json → 取得 task-id → sessions_spawn → 告知用戶「生成中」
子代理：artifact wait → download -a <task-id> → 返回路徑
主代理：收到路徑 → 發送給用戶
```

模板與多任務範例見 [subagent-templates.md](references/subagent-templates.md)。

### 禁止事項

* ❌ 主代理直接執行 `artifact wait`（會阻塞）
* ❌ `download` 不加 `-a <task-id>`（多個同類型 artifact 會下載到同一個）
* ❌ `generate` 不加 `--json`（無法取得 task-id）
* ❌ 超時後不檢查狀態就放棄
* ❌ 不確定命令就猜測（先用 `--help`）

***

## 語言設定

**預設輸出為英文！** 語言代碼用底線不用連字號。

| 命令 | 中文設定方式 |
|------|-------------|
| audio, slide-deck, video, infographic, report, data-table | `--language zh_Hant` |
| quiz, flashcards | DESCRIPTION 寫「請用繁體中文...」 |
| mind-map | 無法指定，取決於來源內容 |

可用代碼：`zh_Hant`(繁中), `zh_Hans`(簡中), `en`, `ja`, `ko`。執行 `notebooklm language list` 查看完整清單。

***

## 常見錯誤

| 錯誤 | 解決方案 |
|------|---------|
| Not logged in | `notebooklm login` |
| No notebook selected | 改用 `-n <id>` 指定 |
| Generation timeout | 增加 `--timeout 600`，超時不代表失敗 |
| Unknown language code | 用底線：`zh_Hant` 非 `zh-TW` |
| 重複下載同一檔案 | `download` 加 `-a <task-id>` |

## 參考資料

* [cli-reference.md](references/cli-reference.md) — 完整 CLI 命令參考（generate 各類型參數詳解）
* [subagent-templates.md](references/subagent-templates.md) — 子代理等待模板（單任務 / 多任務）
* [troubleshooting.md](references/troubleshooting.md) — 疑難排解指南（認證、研究、生成、Doppler）
