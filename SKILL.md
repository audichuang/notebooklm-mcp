***

name: notebooklm
description: Manages Google NotebookLM notebooks via notebooklm-py CLI. Creates notebooks, adds sources (URLs, YouTube, PDFs, text, web research), generates content (podcasts, slides, videos, quizzes, reports, flashcards, infographics, mind maps, data tables), downloads artifacts, and queries notebooks for source-grounded answers. Use when working with NotebookLM or when the user wants to research topics, generate podcasts or slides, create quizzes, or ask questions grounded in notebook sources.
--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

# NotebookLM

CLI: `notebooklm` (install: `pip install notebooklm-py`)

## Contents

* [核心流程](#核心流程) — 認證 → 筆記本 → 來源 → ✅ 驗證 → 生成 → 等待 → 下載 → 傳送
* [背景生成](#背景生成) — 長時間生成的 exec background 模式
* [語言設定](#語言設定) — 預設英文，中文需明確指定
* [完整範例](#完整範例) — 研究 → 中文 Podcast（含驗證與傳送）
* [參考資料](#參考資料) — CLI 完整參考、疑難排解

***

## 核心流程

### Step 1: 認證

```bash
notebooklm list          # 檢查登入狀態
notebooklm login         # 若未登入，開啟瀏覽器認證
```

#### Doppler Integration

認證由 [Doppler](https://doppler.com) 統一管理，環境變數：`NOTEBOOKLM_AUTH_JSON`

```bash
notebooklm login && bash scripts/sync-auth.sh    # 首次 / 認證過期
doppler run -p notebooklm -c dev -- notebooklm list  # 日常使用
```

### Step 2: 筆記本

```bash
notebooklm list                    # 列出所有筆記本
notebooklm create "標題"           # 建立新筆記本
notebooklm use <notebook-id>       # 選擇筆記本
```

### Step 3: 加入來源

```bash
notebooklm source add-research "主題" --mode deep    # 網路研究
notebooklm research wait --import-all --timeout 180  # 等待匯入

notebooklm source add "https://..."                  # URL / YouTube
notebooklm source add "/path/to/file.md"             # 本地檔案
```

**⚠️ 多個研究必須序列化**，不可同時啟動，否則 `research wait` 會卡住。

### ✅ 驗證來源（必須）

生成前**必須**確認所有來源已匯入：

```bash
notebooklm source list
```

來源數量不符預期 → **禁止**進入生成步驟，先排查。

### Step 4: 生成內容

查詢參數：`notebooklm generate <type> --help`

可用類型：`audio`, `video`, `slide-deck`, `quiz`, `report`, `flashcards`, `infographic`, `mind-map`, `data-table`

```bash
notebooklm generate audio "教學講解" --format deep-dive --language zh-TW
notebooklm generate quiz "請用繁體中文製作測驗" --difficulty hard
```

### Step 5: 等待與下載

```bash
notebooklm artifact wait <task-id> --timeout 600     # 等待完成
notebooklm artifact poll <task-id>                    # 查詢狀態
notebooklm artifact list                              # 列出所有 artifacts

mkdir -p /tmp/notebooklm
notebooklm download audio /tmp/notebooklm/podcast.mp3
notebooklm download slide-deck /tmp/notebooklm/slides.pdf
```

**⚠️ 必須下載到 `/tmp/` 目錄**，其他路徑（`~/Documents/` 等）會被安全限制攔截，導致無法傳送給用戶。

### ✅ 驗證產出

```bash
ls -la /tmp/notebooklm/podcast.mp3    # 音檔應 > 1MB
```

如果傳送時因檔案太大而失敗，用 ffmpeg 壓縮後重試：

```bash
ffmpeg -i /tmp/notebooklm/podcast.mp3 -b:a 64k -ar 22050 /tmp/notebooklm/podcast_compressed.mp3
```

### Step 6: 傳送檔案給用戶

**你可以傳送本地檔案。** 只要在回覆文字中加入 `MEDIA:` token，OpenClaw 會自動將該檔案透過 Telegram/其他頻道傳送給用戶。不需要任何額外工具。

```
這是你的 Podcast：
MEDIA:/tmp/notebooklm/podcast.mp3
```

⚠️ 關鍵規則：

* **路徑必須在 `/tmp/`** 下，其他路徑會被安全限制攔截
* **不要說「我無法傳送檔案」**— 用 `MEDIA:` token 就能傳送
* 如果用戶要求傳送檔案而你回覆「做不到」，那是錯誤的回應

### 查詢筆記本

```bash
notebooklm ask "你的問題"
```

***

## 背景生成

生成任務需 5-15 分鐘。使用 `sessions_spawn` 委派 sub-agent 處理，它會自主完成後回報：

```
sessions_spawn task:"執行以下 NotebookLM 任務：
0. 先閱讀 notebooklm skill 的 SKILL.md 了解完整用法和限制
1. notebooklm artifact wait <task-id> --timeout 600
2. mkdir -p /tmp/notebooklm && notebooklm download audio /tmp/notebooklm/podcast.mp3
3. ls -la 確認檔案 > 1MB
4. 回報：MEDIA:/tmp/notebooklm/podcast.mp3
如果傳送失敗因檔案太大，用 ffmpeg 壓縮後重試。
超時則 artifact poll <task-id> 檢查狀態，processing 就再次 wait。"
label:"NotebookLM 生成"
```

Sub-agent 完成後會自動回報結果（包含檔案路徑）。主 agent 收到後，**必須在回覆中寫入 `MEDIA:` token** 把檔案傳給用戶：

```
Sub-agent 回報：下載完成，路徑 /tmp/notebooklm/podcast.mp3
主 agent 回覆用戶：「你的 Podcast 生成好了！」
MEDIA:/tmp/notebooklm/podcast.mp3
```

**⚠️ 禁止主 agent 直接執行 `artifact wait`** — 會阻塞超時。
**⚠️ 禁止回覆「我無法傳送檔案」**— `MEDIA:` token 就是傳送方式。

***

## 語言設定

**預設輸出為英文！** 如需中文，必須明確指定：

| 命令 | 中文設定方式 |
|------|-------------|
| audio, slide-deck, video, infographic, report, data-table | `--language zh-TW` |
| quiz, flashcards | DESCRIPTION 寫「請用繁體中文...」 |
| mind-map | 無法指定，取決於來源內容 |

常用語言代碼：`zh-TW`(繁中), `zh-CN`(簡中), `en`, `ja`, `ko`

***

## 完整範例

### 研究主題 → 中文 Podcast

```bash
# 1. 設定
notebooklm create "AI 研究"
notebooklm use <id>

# 2. 來源
notebooklm source add-research "AI 發展趨勢 2026" --mode deep
notebooklm research wait --import-all --timeout 180

# 3. ✅ 驗證來源
notebooklm source list              # 確認研究結果已匯入

# 4. 生成（取得 task-id）
notebooklm generate audio "深度講解" --format deep-dive --length long --language zh-TW

# 5. 委派 sub-agent 等待 + 下載 + 壓縮 + 回報
sessions_spawn task:"執行以下任務：
0. 先閱讀 notebooklm skill 的 SKILL.md
1. notebooklm artifact wait <task-id> --timeout 600
2. mkdir -p /tmp/notebooklm && notebooklm download audio /tmp/notebooklm/podcast.mp3
3. ls -la 確認檔案 > 1MB
4. 回報：MEDIA:/tmp/notebooklm/podcast.mp3
如果傳送失敗因檔案太大，用 ffmpeg 壓縮後重試。" label:"生成 AI Podcast"

# 6. 主 agent 告知用戶「正在生成中」，sub-agent 完成後自動回報
```

***

## 常見錯誤

| 錯誤 | 解決方案 |
|------|----------|
| Not logged in | `notebooklm login` |
| No notebook selected | `notebooklm use <id>` |
| Generation timeout | 增加 `--timeout 600` |
| 無法傳送檔案 | 確認下載到 `/tmp/` + 用 `MEDIA:` token |
| 檔案太大上傳失敗 | `ffmpeg -i input.mp3 -b:a 64k -ar 22050 output.mp3` |
| 不確定命令 | 先 `notebooklm <command> --help` |
| `artifact status` 不存在 | 用 `artifact poll` 或 `artifact list` |

## 參考資料

* [cli-reference.md](references/cli-reference.md) — 完整 CLI 命令參考（generate 各類型參數詳解）
* [troubleshooting.md](references/troubleshooting.md) — 疑難排解指南（認證、研究、生成、Doppler）
