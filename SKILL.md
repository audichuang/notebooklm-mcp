---
name: notebooklm
description: "Manages Google NotebookLM notebooks via notebooklm-py CLI. Creates notebooks, adds sources (URLs, YouTube, PDFs, text, web research), generates content (podcasts, slides, videos, quizzes, reports, flashcards, infographics, mind maps, data tables), downloads artifacts, and queries notebooks for source-grounded answers. Use when working with NotebookLM or when the user wants to research topics, generate podcasts or slides, create quizzes, or ask questions grounded in notebook sources."
---

# NotebookLM

CLI: `notebooklm` (install: `uv tool install notebooklm-py`)

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

## 連續性 Podcast（季播型續集）

將資料轉化為多集連續性 Podcast。採用**序列回饋法**——每集完成後將音檔上傳回筆記本，AI 自動轉錄為逐字稿，下一集即擁有前集完整記憶。

核心循環：**生成 → 下載 → ♻️ 上傳音檔回筆記本 → 生成下一集**

### 自動化腳本

```bash
# 預覽模式
uv run scripts/episodic_podcast.py --config series_config.yaml --dry-run

# 生成全部集數（序列回饋法）
uv run scripts/episodic_podcast.py --config series_config.yaml

# 續製（從第 3 集開始）
uv run scripts/episodic_podcast.py --config series_config.yaml --start 3

# 加上知識蒸餾（可選）
uv run scripts/episodic_podcast.py --config series_config.yaml --distill
```

配置檔範例見 [series_config_example.yaml](scripts/series_config_example.yaml)。

### 手動執行流程

1. **建立筆記本** → `notebooklm create "節目名稱"`
2. **上傳來源** → `source add "file.md" -n <id>`
3. **生成該集** → `generate audio "身份綁定+本集指令" --format deep-dive --language zh_Hant --json -n <id>`
4. **等待+下載** → `artifact wait <task-id>` → `download audio -a <task-id> /tmp/ep01.mp3`
5. **重命名 artifact** → `artifact rename <task-id> "EP1 節目標題" -n <id>`
6. **♻️ 上傳音檔回筆記本** → `source add /tmp/ep01.mp3 --title "EP01_第1集對話紀錄" -n <id>`
7. **等待 source ready** → 確認 `source list` 中音檔狀態為 `ready`（~2-3 分鐘）
8. **生成下一集** → 提示詞中要求先讀取前集音檔來源，`-s` 加入前集 source ID
9. **循環步驟 4-8 至所有集數完成**

⚠️ **步驟 5-6 適用於每一集，包含最後一集！** 最後一集也必須重命名 artifact 並上傳音檔回筆記本，確保筆記本擁有完整記錄，可用 `source fulltext` 取得逐字稿審閱內容。

### 客製化指令模板（每集必須包含）

```
# 身份綁定（每集貼入）
This is the podcast called "節目名稱".
Male host: "Leo", Female host: "Ruby".
Announce the show name at the beginning.
Style: [風格描述]. No filler words. No interruptions.

# 續集銜接（第 2 集起追加）
IMPORTANT: First read the source titled "ep01_xxx.mp3" —
it contains the FULL transcript of your conversation from the previous episode.
Recap 2-3 specific points you discussed (reference actual things you said).
Do NOT re-introduce background already covered.
```

### 超級提示詞策略

| 策略 | 時機 | 效果 |
|------|------|------|
| 矛盾放大法 | 中期引入新觀點 | 雙主持人引用原文對辯 |
| 來源缺口探測 | 季末回顧 | 找出論述漏洞，為下季鋪陳 |
| 動態辯證法 | 解析單一視角文本 | 假設作者為不可靠敘事者 |
| 逐字朗讀駭客 | 經典文獻導讀 | 強制先朗讀原文再討論 |

完整模板見 [episodic_prompts.md](references/episodic_prompts.md)。

### 連續性製作禁忌

* ❌ 一次上傳全部章節（會被過度壓縮，深度不足）
* ❌ 忘記將前集音檔上傳回筆記本（AI 會「失憶」重新開始）
* ❌ 最後一集不上傳、不重命名（筆記本記錄不完整，無法用 `source fulltext` 取逐字稿）
* ❌ 每集使用不同的人設指令（角色會漂移）
* ❌ 來源命名隨意（用結構化命名如 `EP01_第1集對話紀錄`）
* ❌ 不等 source ready 就生成下一集（音檔未處理完成，AI 無法讀取前集內容）

***

## 參考資料

* [cli-reference.md](references/cli-reference.md) — 完整 CLI 命令參考（generate 各類型參數詳解）
* [troubleshooting.md](references/troubleshooting.md) — 疑難排解指南（認證、研究、生成、Doppler）
* [episodic_prompts.md](references/episodic_prompts.md) — 連續性 Podcast 超級提示詞模板庫
