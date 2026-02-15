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

⚠️ **必須嚴格照抄下方模板構建 `sessions_spawn` 的 task 參數**，不可自由改寫格式。子代理是 minimal mode，只有 `exec` 和 `process` 兩個工具可用。

### 關鍵機制

artifact wait 會執行 5-15 分鐘，但 exec 在 10 秒後就會將命令送到背景，返回 `Command still running (session <session-id>)`。所以必須用以下模式：

1. `exec {"command": "...", "background": true}` → 取得 session-id
2. `process {"action": "poll", "sessionId": "<session-id>"}` → 反覆查詢直到 `Process exited`
3. 完成後繼續下一步

### sessions\_spawn 模板（單任務）

```
sessions_spawn task:"你有 exec 和 process 兩個工具。依序執行以下步驟：

STEP 1: exec {"command": "mkdir -p <output-dir>"}
STEP 2: exec {"command": "doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id> --timeout 600", "background": true}
  → 取得回傳的 session-id
STEP 3: 反覆呼叫 process {"action": "poll", "sessionId": "<上一步的session-id>"} 每 30 秒一次，直到回傳包含 'Process exited' 為止
  → 如果 exit code 不是 0，回報錯誤
STEP 4: exec {"command": "doppler run -p notebooklm -c dev -- notebooklm download <type> -a <task-id> <output-path>"}
STEP 5: exec {"command": "ls -la <output-path>"}

最後回報下載的檔案路徑。"
label:"NotebookLM 生成"
runTimeoutSeconds: 900
```

### sessions\_spawn 模板（多任務）

當同時生成多個內容（如中英文音頻 + 簡報）時，用**一個**子代理依序 wait + download 所有任務：

```
sessions_spawn task:"你有 exec 和 process 兩個工具。依序執行以下步驟：

STEP 1: exec {"command": "mkdir -p <output-dir>"}

對以下每一組 (task-id, type, output-path)，依序執行 STEP A → B → C：
- (<task-id-1>, audio, <output-dir>/podcast_zh.mp3)
- (<task-id-2>, audio, <output-dir>/podcast_en.mp3)
- (<task-id-3>, slide-deck, <output-dir>/slides_zh.pdf)
- (<task-id-4>, slide-deck, <output-dir>/slides_en.pdf)

STEP A: exec {"command": "doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id> --timeout 600", "background": true}
  → 取得回傳的 session-id
STEP B: 反覆呼叫 process {"action": "poll", "sessionId": "<session-id>"} 每 30 秒一次，直到 'Process exited'
  → exit code 非 0 就跳過這組，繼續下一組
STEP C: exec {"command": "doppler run -p notebooklm -c dev -- notebooklm download <type> -a <task-id> <output-path>"}

全部完成後：
exec {"command": "ls -la <output-dir>/"}
回報所有下載的檔案路徑。"
label:"NotebookLM 多任務生成"
runTimeoutSeconds: 2700
```

⚠️ **超時計算**：`runTimeoutSeconds = 任務數 × 單次 wait timeout + 300`

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
