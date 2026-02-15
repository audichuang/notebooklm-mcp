# NotebookLM CLI Reference

Complete command reference for notebooklm-py CLI.

## Table of Contents

* [Session Commands](#session-commands)
* [Notebook Commands](#notebook-commands)
* [Source Commands](#source-commands)
* [Research Commands](#research-commands)
* [Generate Commands](#generate-commands) ⭐ 詳細參數
* [Download Commands](#download-commands)
* [Artifact Commands](#artifact-commands)
* [Chat Commands](#chat-commands)
* [Note Commands](#note-commands)
* [Share Commands](#share-commands)

***

## Session Commands

```bash
notebooklm login                    # 開啟瀏覽器 Google 認證
notebooklm list                     # 列出所有筆記本
notebooklm use <notebook-id>        # 選擇筆記本
notebooklm status                   # 顯示目前狀態
notebooklm clear                    # 清除目前 context
```

***

## Notebook Commands

```bash
notebooklm create "Title"                      # 建立筆記本
notebooklm rename <notebook-id> "New Title"    # 重命名
notebooklm delete <notebook-id>                # 刪除
notebooklm summary                             # 取得摘要
```

***

## Source Commands

```bash
# 加入來源
notebooklm source add "https://..."                    # URL
notebooklm source add "https://youtube.com/watch?v=..." # YouTube
notebooklm source add --text "Content here"            # 純文字
notebooklm source add "/path/to/file.md" --title "標題" # 本地檔案
notebooklm source add-drive "drive-file-id"            # Google Drive

# 研究（自動網路搜尋）
notebooklm source add-research "topic" --mode fast|deep

# 管理
notebooklm source list
notebooklm source delete <source-id>
notebooklm source rename <source-id> "New Name"
notebooklm source fulltext <source-id>
notebooklm source guide <source-id>      # AI 摘要與關鍵字
notebooklm source refresh <source-id>    # 重新整理
```

***

## Research Commands

```bash
notebooklm research status                           # 檢查狀態
notebooklm research wait --import-all --timeout 300  # 等待完成並匯入
notebooklm research list                             # 列出研究結果
notebooklm research delete                           # 刪除研究
```

***

## Generate Commands

### ⚠️ 語言設定重要提醒

| 命令 | `--language` 支援 | 中文設定方式 |
|------|-------------------|-------------|
| audio | ✅ | `--language zh_Hant` |
| slide-deck | ✅ | `--language zh_Hant` |
| video | ✅ | `--language zh_Hant` |
| infographic | ✅ | `--language zh_Hant` |
| report | ✅ | `--language zh_Hant` |
| data-table | ✅ | `--language zh_Hant` |
| quiz | ❌ | DESCRIPTION 寫「請用繁體中文...」 |
| flashcards | ❌ | DESCRIPTION 寫「請用繁體中文...」 |
| mind-map | ❌ | 無法指定 |

### 共用參數（所有 generate 命令）

```
-n, --notebook TEXT    指定筆記本 ID
-s, --source TEXT      限定特定來源 ID（可多次使用）
--retry N              速率限制時自動重試 N 次
--json                 JSON 格式輸出
--wait / --no-wait     是否等待完成
```

***

### 🎙️ Audio (Podcast)

```bash
notebooklm generate audio [DESCRIPTION] [OPTIONS]

Options:
  --format [deep-dive|brief|critique|debate]
           deep-dive: 深度探討（預設）
           brief: 簡短摘要
           critique: 評論分析
           debate: 辯論風格
  --length [short|default|long]
           short: 短版
           default: 標準（預設）
           long: 長版
  --language TEXT      語言代碼，如 "zh_Hant", "en", "ja"

Examples:
notebooklm generate audio "深度講解重點" --format deep-dive --length long --language zh_Hant
notebooklm generate audio "正反辯論" --format debate --language zh_Hant
```

***

### 📊 Slide Deck (PPT)

```bash
notebooklm generate slide-deck [DESCRIPTION] [OPTIONS]

Options:
  --format [detailed|presenter]
           detailed: 詳細版（預設）
           presenter: 簡報者版（含講者筆記）
  --length [default|short]
           default: 標準（預設）
           short: 精簡版
  --language TEXT      語言代碼

Examples:
notebooklm generate slide-deck "教學簡報" --format detailed --language zh_Hant
notebooklm generate slide-deck "重點摘要" --format presenter --length short --language zh_Hant
```

***

### 📝 Quiz (測驗)

```bash
notebooklm generate quiz [DESCRIPTION] [OPTIONS]

Options:
  --quantity [fewer|standard|more]
           fewer: 較少題目
           standard: 標準數量（預設）
           more: 更多題目
  --difficulty [easy|medium|hard]
           easy: 簡單
           medium: 中等（預設）
           hard: 困難
  # ⚠️ 無 --language！在 DESCRIPTION 指定語言

Examples:
notebooklm generate quiz "請用繁體中文製作測驗，附解析" --quantity more --difficulty hard
notebooklm generate quiz "請用繁體中文製作基礎測驗" --difficulty easy
```

***

### 🎬 Video (影片)

```bash
notebooklm generate video [DESCRIPTION] [OPTIONS]

Options:
  --format [explainer|brief]
           explainer: 講解型（預設）
           brief: 簡短型
  --style [auto|classic|whiteboard|kawaii|anime|watercolor|retro-print|heritage|paper-craft]
           auto: 自動選擇（預設）
           classic: 經典
           whiteboard: 白板
           kawaii: 可愛
           anime: 動漫
           watercolor: 水彩
           retro-print: 復古印刷
           heritage: 傳統
           paper-craft: 紙藝
  --language TEXT      語言代碼

Examples:
notebooklm generate video "專業講解" --format explainer --style whiteboard --language zh_Hant
notebooklm generate video "輕鬆解說" --style kawaii --language zh_Hant
```

***

### 📄 Report (報告)

```bash
notebooklm generate report [DESCRIPTION] [OPTIONS]

Options:
  --format [briefing-doc|study-guide|blog-post|custom]
           briefing-doc: 簡報文件
           study-guide: 學習指南
           blog-post: 部落格文章
           custom: 自訂格式
  --language TEXT      語言代碼

Examples:
notebooklm generate report "完整分析" --format study-guide --language zh_Hant
notebooklm generate report "部落格風格" --format blog-post --language zh_Hant
```

***

### 📋 Flashcards (學習卡)

```bash
notebooklm generate flashcards [DESCRIPTION] [OPTIONS]

Options:
  --quantity [fewer|standard|more]
  --difficulty [easy|medium|hard]
  # ⚠️ 無 --language！在 DESCRIPTION 指定語言

Examples:
notebooklm generate flashcards "請用繁體中文製作詞彙卡" --quantity more --difficulty easy
```

***

### 📈 Infographic (資訊圖表)

```bash
notebooklm generate infographic [DESCRIPTION] [OPTIONS]

Options:
  --orientation [landscape|portrait|square]
           landscape: 橫向
           portrait: 直向
           square: 方形
  --detail [concise|standard|detailed]
           concise: 精簡
           standard: 標準
           detailed: 詳細
  --language TEXT      語言代碼

Examples:
notebooklm generate infographic "重點統計" --orientation portrait --detail detailed --language zh_Hant
```

***

### 🗺️ Mind Map (思維導圖)

```bash
notebooklm generate mind-map [OPTIONS]

Options:
  -s, --source TEXT    限定特定來源
  # ⚠️ 無法指定語言，取決於來源內容

Example:
notebooklm generate mind-map
```

***

### 📊 Data Table (數據表)

```bash
notebooklm generate data-table [DESCRIPTION] [OPTIONS]

Options:
  --language TEXT      語言代碼

Example:
notebooklm generate data-table "整理關鍵數據" --language zh_Hant
```

***

## Download Commands

### 共用參數（所有 download 命令）

```
output_path            輸出檔案路徑
-n, --notebook TEXT    指定筆記本 ID
-a, --artifact TEXT    指定 artifact ID
--latest               下載最新的（預設）
--earliest             下載最舊的
--all                  下載該類別所有檔案
--name TEXT            按標題模糊匹配
--force                強制覆蓋現有檔案
--no-clobber           跳過現有檔案
--dry-run              預覽不下載
--json                 JSON 格式輸出
```

### 下載命令

```bash
notebooklm download audio ~/output/podcast.mp3
notebooklm download slide-deck ~/output/slides.pdf
notebooklm download video ~/output/video.mp4
notebooklm download quiz ~/output/quiz.md --format markdown|json|html
notebooklm download flashcards ~/output/cards.md --format markdown|json|html
notebooklm download report ~/output/report.md
notebooklm download infographic ~/output/info.png
notebooklm download mind-map ~/output/mindmap.json
notebooklm download data-table ~/output/data.csv
```

### ⚠️ 正確副檔名對照表

| 類型 | 正確副檔名 | 說明 |
|------|-----------|------|
| audio | `.mp3` | 音檔 |
| slide-deck | `.pdf` | **注意：是 PDF 不是 PPTX！** |
| video | `.mp4` | 影片 |
| report | `.md` | Markdown 文字 |
| infographic | `.png` | 圖片 |
| quiz | `.json` / `.md` / `.html` | 用 `--format` 指定 |
| flashcards | `.json` / `.md` / `.html` | 用 `--format` 指定 |
| mind-map | `.json` | JSON 結構 |
| data-table | `.csv` | CSV 表格 |

***

## Artifact Commands

```bash
notebooklm artifact list                              # 列出所有 artifacts
notebooklm artifact list --type audio                 # 按類型篩選
notebooklm artifact get <artifact-id>                 # 取得詳情
notebooklm artifact poll <task-id>                    # 單次檢查狀態
notebooklm artifact wait <artifact-id> --timeout 600  # 等待完成
notebooklm artifact rename <artifact-id> "New Title"  # 重命名
notebooklm artifact delete <artifact-id>              # 刪除
notebooklm artifact export <artifact-id> --type docs  # 匯出到 Google Docs
notebooklm artifact suggestions                       # AI 推薦的報告主題
```

***

## Chat Commands

```bash
notebooklm ask "Your question"           # 問問題
notebooklm configure --persona "expert"  # 設定 AI 角色
notebooklm history                       # 查看對話歷史
notebooklm chat clear                    # 清除對話
```

***

## Note Commands

```bash
notebooklm note create "Note content"    # 建立筆記
notebooklm note list                     # 列出筆記
notebooklm note get <note-id>            # 取得筆記
notebooklm note delete <note-id>         # 刪除筆記
notebooklm note clear-all                # 清除所有筆記
```

***

## Share Commands

```bash
notebooklm share status                              # 分享狀態
notebooklm share list                                # 列出協作者
notebooklm share public --enable                     # 公開筆記本
notebooklm share invite "email@example.com" --role editor|viewer
notebooklm share remove "email@example.com"          # 移除協作者
notebooklm share link                                # 取得分享連結
```

***

## Global Options

```bash
--version              顯示版本
--storage PATH         storage_state.json 路徑
-v, --verbose          增加輸出詳細度（-v INFO, -vv DEBUG）
--help                 顯示說明
```

***

## Output Directory

預設輸出位置：

```
~/Documents/NotebookLM/output/
```

建立目錄：

```bash
mkdir -p ~/Documents/NotebookLM/output
```
