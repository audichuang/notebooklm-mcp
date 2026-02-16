---
name: notebooklm
description: "Manages Google NotebookLM notebooks via notebooklm-py CLI. Creates notebooks, adds sources (URLs, YouTube, PDFs, text, web research), generates content (podcasts, slides, videos, quizzes, reports, flashcards, infographics, mind maps, data tables), downloads artifacts, and queries notebooks for source-grounded answers. Use when working with NotebookLM or when the user wants to research topics, generate podcasts or slides, create quizzes, or ask questions grounded in notebook sources."
---

# NotebookLM

CLI: `notebooklm` (install: `pip install notebooklm-py`)

所有命令前綴：`doppler run -p notebooklm -c dev --`

不確定命令時，先執行 `notebooklm <command> --help` 確認。

## 核心流程

```
認證 → 筆記本 → 加入來源 → 生成內容 → 子代理等待 → 下載
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
doppler run -p notebooklm -c dev -- notebooklm generate audio "教學講解" --format deep-dive --language zh_Hant --json -n <notebook-id>
```

**必須加 `--json`** 才能取得 task-id 供後續下載使用。

### 等待與下載

```bash
# 查詢狀態
doppler run -p notebooklm -c dev -- notebooklm artifact poll <task-id> -n <notebook-id>
doppler run -p notebooklm -c dev -- notebooklm artifact list -n <notebook-id>

# 下載——必須用 -a 指定 artifact ID
mkdir -p /tmp/notebooklm
doppler run -p notebooklm -c dev -- notebooklm download audio -a <task-id> -n <notebook-id> /tmp/notebooklm/podcast.mp3
doppler run -p notebooklm -c dev -- notebooklm download slide-deck -a <task-id> -n <notebook-id> /tmp/notebooklm/slides.pdf
```

沒有 `artifact status` 命令，用 `artifact poll` 或 `artifact list`。

### 查詢筆記本

```bash
doppler run -p notebooklm -c dev -- notebooklm ask "你的問題" -n <notebook-id>
```

***

## 子代理處理

生成任務需 **5-15 分鐘**，必須用 `sessions_spawn` 委派子代理等待，避免主代理阻塞超時。

### 流程

```
主代理：觸發 generate --json → 取得 task-id → sessions_spawn 委派子代理 → 告知用戶「生成中」
子代理：artifact wait <task-id> → 完成後下載 → 返回路徑
主代理：收到子代理結果 → 發送給用戶
```

### 單任務子代理模板

```
sessions_spawn task:"使用 exec 工具依序執行以下命令：
1. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id> -n <notebook-id> --timeout 600
2. exec mkdir -p /tmp/notebooklm
3. exec doppler run -p notebooklm -c dev -- notebooklm download <type> -a <task-id> -n <notebook-id> /tmp/notebooklm/<filename>
4. exec ls -la /tmp/notebooklm/<filename>
如果 step 1 超時，執行 exec doppler run -p notebooklm -c dev -- notebooklm artifact poll <task-id> -n <notebook-id> 檢查狀態：
- in_progress → 再次 artifact wait
- completed → 繼續 step 3
- failed → 回報錯誤
最後回報下載的檔案路徑。"
label:"NotebookLM 生成"
```

### 多任務子代理模板

當需要同時生成多個內容（如中英文音頻 + 簡報）時：

1. 在主代理中**依序**啟動所有 generate 任務，記錄每個 task-id
2. 用**一個**子代理依序 wait + download 所有任務
3. 下載路徑必須包含語言/類型標識，避免覆蓋

```
sessions_spawn task:"使用 exec 工具依序執行以下命令：
1. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-1> -n <notebook-id> --timeout 600
2. exec doppler run -p notebooklm -c dev -- notebooklm download audio -a <task-id-1> -n <notebook-id> /tmp/notebooklm/<描述>_zh.mp3
3. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-2> -n <notebook-id> --timeout 600
4. exec doppler run -p notebooklm -c dev -- notebooklm download audio -a <task-id-2> -n <notebook-id> /tmp/notebooklm/<描述>_en.mp3
5. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-3> -n <notebook-id> --timeout 600
6. exec doppler run -p notebooklm -c dev -- notebooklm download slide-deck -a <task-id-3> -n <notebook-id> /tmp/notebooklm/<描述>_zh.pdf
7. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-4> -n <notebook-id> --timeout 600
8. exec doppler run -p notebooklm -c dev -- notebooklm download slide-deck -a <task-id-4> -n <notebook-id> /tmp/notebooklm/<描述>_en.pdf
9. exec ls -la /tmp/notebooklm/
如果某個 wait 超時，用 artifact poll -n <notebook-id> 檢查狀態後繼續。
最後回報所有下載的檔案路徑。"
label:"NotebookLM 多任務生成"
```

### 禁止事項

* ❌ 主代理直接執行 `artifact wait`（會阻塞）
* ❌ 用 `exec background:true` 替代 `sessions_spawn`（heartbeat 通知在 Telegram 上不可靠）
* ❌ `artifact poll` / `download` 不加 `-n <notebook-id>`（會讀到 context.json 的舊 notebook）
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
|------|----------|
| Not logged in | `notebooklm login` |
| No notebook selected | 改用 `-n <id>` 指定 |
| Generation timeout | 增加 `--timeout 600`，超時不代表失敗 |
| Unknown language code | 用底線：`zh_Hant` 非 `zh-TW` |
| 重複下載同一檔案 | `download` 加 `-a <task-id>` |

## 參考資料

* [cli-reference.md](references/cli-reference.md) — 完整 CLI 命令參考（generate 各類型參數詳解）
* [troubleshooting.md](references/troubleshooting.md) — 疑難排解指南（認證、研究、生成、Doppler）
