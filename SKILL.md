---
name: notebooklm
description: "Route NotebookLM work through the local notebooklm MCP server. Creates notebooks, adds sources, generates zh_Hant-first audio, asks source-grounded questions, and runs deterministic episodic podcast workflows. Use when working with NotebookLM, 上傳到筆記本, 生成 podcast, 生成播客, 生成連續音檔, 幫我講解."
---

# NotebookLM

NotebookLM 操作都走 `notebooklm` MCP server。Skill 只負責判斷意圖與組織輸入；認證、等待、下載、語言檢查、enum mapping、連續 podcast 迴圈都在 MCP tools 裡。

## MCP Tools

| Tool | 用途 |
|------|------|
| `notebook_create` | 建立筆記本，回傳 `notebook_id` |
| `notebook_list` | 列出筆記本 |
| `source_add_url` | 加 URL / YouTube 來源 |
| `source_add_text` | 加純文字來源 |
| `source_add_file` | 加本機檔案來源；mp3 回傳用 `mime_type="audio/mpeg"` |
| `source_delete` | 刪來源；重生壞集前先刪掉該集來源 |
| `generate_audio` | 生成 audio overview；預設 `zh_Hant` |
| `artifact_wait` | 等 generation `task_id` 完成 |
| `artifact_download_audio` | 下載 audio artifact 到指定路徑 |
| `artifact_rename` | 重命名 artifact |
| `chat_ask` | 對筆記本做 source-grounded 問答 |
| `podcast_episode` | 單集 podcast：生成→等待→命名 artifact 為 `EP{n:02d} 標題`(需傳 `title`)→下載→自上傳本集 mp3 為同名來源(可選 `prior_mp3_path` 做一次性續接) |
| `podcast_series` | 整季 podcast：純 Python 迴圈;每集命名 `EP{n:02d} 標題` 並自上傳本集 mp3,下一集生成時自然讀到筆記本內的同名前集來源 |

完整參數與回傳格式見 [MCP 工具參考](references/cli-reference.md)。

## Simple Tasks

單一 podcast：

1. `notebook_create` 或使用既有 `notebook_id`
2. 用 `source_add_url` / `source_add_text` / `source_add_file` 加來源
3. 呼叫 `generate_audio`，必要時提供 `instructions`
4. 用 `artifact_wait` 等 `task_id`
5. 用 `artifact_download_audio` 下載，再用 `artifact_rename` 命名

問答：先確定 `notebook_id`，直接呼叫 `chat_ask`。

加來源：依來源型態呼叫 `source_add_url`、`source_add_text` 或 `source_add_file`。音檔來源明確傳 `mime_type="audio/mpeg"`。

## Episodic Podcast

連續系列先做大綱，不要直接開始生成。

1. 先用對話規劃整季大綱:每集都要有 `title`(本集標題)與 `brief`,brief 要包含主持人人設、節目風格、本集任務、承接要求。`title` 必填且不可為空——工作室 artifact 與來源都會命名成 `EP{n:02d} 標題`(例 `EP01 心法篇`)。
2. 讓使用者核可整季 `episodes` 陣列(每項形如 `{"title": ..., "brief": ...}`)。
3. 呼叫 `podcast_series(notebook_id, episodes, output_dir, start=1)`。
4. 續製時傳「完整的 episodes 陣列」加 `start=N`（前提是同一個筆記本來源區已有 `EP{N-1:02d} 標題` 來源，這是前次跑時各集自上傳留下的）。`podcast_series` 不讀本機 `ep{N-1}.mp3`；本機檔只是下載與 manifest 的存放處。manifest 會合併保留前面集數,不會被覆寫。

Reject-then-delete 規則：人工聽完覺得某集要重生時，先用 `source_delete` 移除該集已回傳到筆記本的音檔來源，再用 `podcast_episode` 重生該集，避免壞集被下一集繼承。

Podcast brief 模板與策略見 [episodic_prompts.md](references/episodic_prompts.md)，`episodes` 範例見 [series_example.md](references/series_example.md)。

## Language

預設語言是 `zh_Hant`。語言代碼用底線，不用連字號。

| 內容 | 語言設定 |
|------|----------|
| Audio | `language="zh_Hant"`；不傳時 MCP 預設就是 `zh_Hant` |
| Quiz / flashcards | 在 brief / instructions 內明確要求繁體中文 |
| Mind map | v1 MCP 未封裝；若將來使用 CLI，語言取決於來源內容 |

常用代碼：`zh_Hant`、`zh_Hans`、`en`、`ja`、`ko`。

## Auth

MCP server 由 Claude Code 註冊命令用 `doppler run -p notebooklm -c dev -- ...` 包住，`notebooklm-py` 讀取 `NOTEBOOKLM_AUTH_JSON`。認證過期或 3 VM 同步問題見 [troubleshooting.md](references/troubleshooting.md)。

## References

- [MCP setup](docs/mcp-setup.md)
- [MCP 工具參考](references/cli-reference.md)
- [疑難排解](references/troubleshooting.md)
- [連續 podcast prompts](references/episodic_prompts.md)
- [episodes 範例](references/series_example.md)
