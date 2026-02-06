# Troubleshooting Guide

## Authentication Issues

### "Not logged in"
```bash
notebooklm login
```
Browser opens → Complete Google login → Wait for NotebookLM homepage → Press Enter.

### "Missing required cookies: {'SID'}"
You pressed Enter before fully logging in. Ensure you see the NotebookLM homepage before pressing Enter.

```bash
# Clear and retry
rm -rf ~/.notebooklm/storage_state.json ~/.notebooklm/browser_profile
notebooklm login
```

### "Authentication expired"
```bash
notebooklm login
```

### 認證資料存放位置
```
~/.notebooklm/
├── storage_state.json    # 認證狀態（cookies、session）
└── browser_profile/      # 瀏覽器設定檔
```

## Research Issues

### "Research wait" 卡住（多個研究同時進行）

**問題：** 同一個 Notebook 啟動多個研究後，`research wait` 一直顯示等待中，即使某些研究已完成。

**原因：** `research wait` 只會追蹤清單中的第一個任務。如果第一個任務卡住或較慢，後面已完成的任務會被忽略。

**解決方案：**

1. **序列化執行（推薦）**
```bash
# 一個完成後再啟動下一個
notebooklm source add-research "Topic 1" --mode deep
notebooklm research wait --import-all --timeout 300
notebooklm source add-research "Topic 2" --mode deep
notebooklm research wait --import-all --timeout 300
```

2. **如果已經卡住**
```bash
# 檢查來源是否已匯入（可能伺服器端已完成）
notebooklm source list

# 或直接跳過 wait，繼續下一步
```

3. **多個主題用不同 Notebook**
```bash
notebooklm create "Research A"
notebooklm use <id-a>
notebooklm source add-research "Topic A"
# 切換到另一個 notebook
notebooklm create "Research B"
notebooklm use <id-b>
notebooklm source add-research "Topic B"
```

### "Research timeout"
Increase timeout or use fast mode:
```bash
notebooklm source add-research "topic" --mode fast
notebooklm research wait --import-all --timeout 300
```

### "No research running"
Start research first:
```bash
notebooklm source add-research "topic" --mode deep
```

## Generation Issues

### "Generation timeout" (Timeout after 600s)

**重要：超時不代表失敗！** 伺服器端任務通常仍在執行中。

#### 處理步驟：

```bash
# 1. 檢查狀態（單次查詢）
notebooklm artifact poll <task-id>

# 2. 如果狀態是 processing，可以繼續等待
notebooklm artifact wait <task-id> --timeout 600

# 3. 如果狀態是 completed，直接下載
notebooklm download audio ~/output/podcast.mp3
```

#### 不知道 task-id？直接下載最新的：
```bash
# 自動下載最新完成的同類型 artifact（推薦！）
notebooklm download audio ~/output/podcast.mp3
notebooklm download slide-deck ~/output/slides.pdf
notebooklm download video ~/output/video.mp4
```

#### 查看所有 artifacts 狀態：
```bash
notebooklm artifact list
```

### "Generation pending" (takes too long)
Poll status or increase timeout:
```bash
notebooklm artifact poll <task-id>
notebooklm artifact wait <task-id> --timeout 600
```

### "No artifact found"
Ensure notebook has sources before generating:
```bash
notebooklm source list
```

### Generation fails
- Content too short (<500 chars)
- Content too long (>500K chars)
- Try different generation type

## Notebook Issues

### "No notebook selected"
```bash
notebooklm list
notebooklm use <notebook-id>
```

### "Notebook not found"
Verify ID with:
```bash
notebooklm list
```

## Rate Limiting

### "Rate limit exceeded"
- Free tier: ~50 queries/day
- Wait 24 hours or use different Google account

## Installation Issues

### "Playwright not installed"
```bash
pip install playwright
playwright install chromium
```

### "notebooklm command not found"
安裝 notebooklm-py：
```bash
pip install notebooklm-py
playwright install chromium
```

或確認安裝路徑在 PATH 中：
```bash
which notebooklm
# 如果找不到，用 pip show 找到安裝位置
pip show notebooklm-py
```

## Debug Mode

Add verbose flag:
```bash
notebooklm -v list
notebooklm -vv source add-research "topic"
```

## Output Directory

Create if not exists:
```bash
mkdir -p ~/Documents/NotebookLM/output
```
