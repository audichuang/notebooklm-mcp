---
name: notebooklm
description: "Route NotebookLM work through the local notebooklm MCP server: notebooks, sources, zh_Hant-first audio, source-grounded chat, deterministic episodic podcast seasons, per-episode slide/report attachments, and publishing a season as an Apple-Podcast RSS feed. Use when the user mentions NotebookLM, 上傳到筆記本, 幫我講解, 生成 podcast/播客/連續音檔, 生成簡報/研讀講義, or 發布 podcast/訂閱/RSS/Apple Podcast."
---

# NotebookLM

NotebookLM 操作都走 `notebooklm` MCP server;此 skill 只判斷意圖、組織輸入、串接流程,實作細節都在 MCP 工具內。

## MCP Tools

| Tool | 用途 |
|------|------|
| `notebook_create` | 建立筆記本，回傳 `notebook_id` |
| `notebook_list` | 列出筆記本 |
| `source_add_url` | 加 URL / YouTube 來源 |
| `source_add_text` | 加純文字來源 |
| `source_add_file` | 加本機檔案來源；mp3 回傳用 `mime_type="audio/mpeg"` |
| `source_delete` | 刪來源；重生壞集前先刪掉該集來源 |
| `source_list` | 列出筆記本來源(找 `source_id`、確認上傳落地) |
| `source_fulltext` | 取來源擷取到的全文(驗 PDF/Medium 是否吃進正文;CJK 字間有空格) |
| `notebook_get` | 取筆記本 metadata(標題/來源數/擁有者);生成前確認跑對筆記本 |
| `generate_audio` | 生成 audio overview；預設 `zh_Hant` |
| `artifact_list` | 列出筆記本現有 artifact(生成被打斷後救援/對帳,拿 `artifact_id` 再下載) |
| `artifact_wait` | 等 generation `task_id` 完成(fail-closed:失敗會 raise) |
| `artifact_download_audio` | 下載 audio artifact 到指定路徑 |
| `artifact_rename` | 重命名 artifact |
| `chat_ask` | 對筆記本做 source-grounded 問答;`source_ids` 聚焦單集原文、`conversation_id` 續問,回引用 |
| `podcast_episode` | 單集 podcast：生成→等待→命名 artifact 為 `EP{n:02d} 標題`(需傳 `title`)→下載→自上傳本集 mp3 為同名來源(可選 `prior_mp3_path` 做一次性續接) |
| `podcast_series` | 整季 podcast：純 Python 迴圈;每集命名 `EP{n:02d} 標題` 並自上傳本集 mp3,下一集生成時自然讀到筆記本內的同名前集來源 |
| `generate_slides` | 按需生某集簡報並下載 PDF,路徑回寫 manifest 的 `slides_pdf_path`(供 publish 附連結) |
| `generate_report` | 按需生某集研讀文件(預設 `study_guide`,可 `briefing_doc`/`blog_post`)下載 Markdown,回寫 `report_md_path` |
| `publish_series` | 把整季 manifest + mp3 發布成 Apple-Podcast 合規 RSS feed(內網 HTTP PUT 到 NAS uploader,Cloudflare Tunnel 對外 HTTPS);有 `slides_pdf_path`/`report_md_path` 的集會一併 host PDF/HTML 並把連結附進單集簡介;回傳 `feed_url` 供訂閱 |
| `feed_info` | 純計算,傳 `show_id`,回 `{show_id, token, feed_url, show_page_url}`,不含各集細節 |

完整參數與回傳格式見 [MCP 工具參考](references/tool-reference.md)。

## Simple Tasks

單一 podcast：

1. `notebook_create` 或使用既有 `notebook_id`
2. 用 `source_add_url` / `source_add_text` / `source_add_file` 加來源
3. 呼叫 `generate_audio`，必要時提供 `instructions`
4. 用 `artifact_wait` 等 `task_id`
5. 用 `artifact_download_audio` 下載，再用 `artifact_rename` 命名

問答：先確定 `notebook_id`，直接呼叫 `chat_ask`。

加來源：依來源型態呼叫 `source_add_url`、`source_add_text` 或 `source_add_file`(音檔的 `mime_type` 見工具表)。

## Episodic Podcast

連續系列先做大綱，不要直接開始生成。

1. 先用對話規劃整季大綱:每集都要有 `title`(本集標題)與 `brief`,brief 要包含主持人人設、節目風格、本集任務、承接要求。`title` 必填且不可為空——工作室 artifact 與來源都會命名成 `EP{n:02d} 標題`(例 `EP01 心法篇`)。
2. 讓使用者核可整季 `episodes` 陣列(每項形如 `{"title": ..., "brief": ...}`)。若最終會發布,
   核可時一併攤開**整包交付內容**讓使用者確認:show notes(必做)、簡報、研讀講義、封面——
   預設全做,請使用者挑要略過哪些,別自己預設只生音檔(見 §Publish)。
3. 呼叫 `podcast_series(notebook_id, episodes, output_dir, start=1)`。
4. 續製時傳「完整的 episodes 陣列」加 `start=N`（前提是同一個筆記本來源區已有 `EP{N-1:02d} 標題` 來源，這是前次跑時各集自上傳留下的）。`podcast_series` 不讀本機 `ep{N-1}.mp3`；本機檔只是下載與 manifest 的存放處。manifest 會合併保留前面集數,不會被覆寫。

Reject-then-delete 規則：人工聽完覺得某集要重生時，先用 `source_delete` 移除該集已回傳到筆記本的音檔來源，再用 `podcast_episode` 重生該集，避免壞集被下一集繼承。

Podcast brief 模板與策略見 [episodic_prompts.md](references/episodic_prompts.md)，`episodes` 範例見 [series_example.md](references/series_example.md)。

## Publish to Apple Podcast(RSS feed)

一主題 = 一節目 = 一 feed。`publish_series` 吃 `podcast_series` 產出的 `series_manifest.json`
(**單集也走 `podcast_series`**、episodes 放一集才有 manifest;`podcast_episode` 不產 manifest)。

端到端固化流程(生成 → 加料 → 封面 → 發布 → 訂閱)。**預設整包全做**——只有使用者在 §Episodic
核可關卡明講不要某項才略過,不要自己靜默跳過。發布前用下方「交付清單」逐集核對才算完成。

1. **生成音檔** — 依 §Episodic 跑 `podcast_series`,得 `series_manifest.json` + 各集 mp3。
2. **單集簡介** — 用 `chat_ask` 生繁中 show notes(約 100–150 字),**先清引用標記**
   `\[[\d,\s\-–]+\]` 再寫進該集 manifest 的 `description`。
3. **簡報 / 研讀講義** — `generate_slides` + `generate_report`,路徑自動回寫 manifest;發布時
   content-hash → host(講義 Markdown 渲染成自包含 HTML),具名連結附進單集簡介。
4. **封面** — Apple 硬規格:正方形、1400–3000px、PNG/JPG、RGB、**無透明通道**。
   - **節目封面**(掛 channel):`uv run python scripts/make_cover.py --output cover.jpg
     --line 標題行1 --line 標題行2 --subtitle ... --byline ...`(自帶 Apple 驗證器)。
   - **單集封面**(每集各自封面,選填):`make_cover.py --manifest series_manifest.json
     --show-name "節目名" --tag "~/.claude/" --byline ... --output-dir covers/` — 逐集生
     「集標大標 + EP 徽章 + 每集不同色(集號決定性)」並把 `cover_path` 寫回 manifest。
     `publish_series` 看到 `cover_path` 就讓該集 `<item>` 掛 `itunes:image`;沒給則 fallback 節目封面。
5. **發布** — `publish_series(show_id, notebook_id, manifest_path, show_title, show_description,
   author, owner_name, owner_email, artwork_path)`。`show_id` 是穩定 slug(feed identity、決定
   URL、**永不改**、`[a-z0-9-]`);`owner_email` Apple 必填。
6. **訂閱** — 回傳 `feed_url`,在 Apple Podcast「用 URL 加入節目」貼上。續製重跑 `publish_series`
   (同 URL/GUID)Apple 自動抓新集;重生壞集內容 hash 變 → 換音檔 URL、GUID 不變(視為同集更新)。

**交付清單(跑 `publish_series` 前逐集核對,缺項=還沒做完,只有使用者明講不要才准空):**

- [ ] `mp3_path` — 音檔
- [ ] `description` — 真 show notes,**不可等於標題**(缺漏時 publish 會 fallback 成標題,
      播放器上簡介跟標題一字不差、看起來像壞掉)
- [ ] `slides_pdf_path` — 簡報
- [ ] `report_md_path` — 研讀講義
- [ ] `cover_path` — 單集封面(每集各自;`make_cover.py --manifest` 批次生)
- [ ] 封面過 Apple 驗證;show 層 `show_id`/`show_title`/`show_description`/`author`/
      `owner_name`/`owner_email`/`artwork_path` 七欄齊(`publish_series` 全必填)

清單全綠再發布;報告完成時,對照本清單說明每項的狀態(已做 / 使用者略過),不要只說「已發布」。

前提:四個 Doppler secret(`PODCAST_PUBLIC_BASE_URL` / `PODCAST_TOKEN_SALT` /
`PODCAST_UPLOAD_URL` / `PODCAST_UPLOAD_TOKEN`),MCP 內網 PUT 到 NAS uploader,讀站經
Cloudflare Tunnel 對外;托管見 [podcast-feed-host](https://github.com/audichuang/podcast-feed-host)。

## Language

預設語言是 `zh_Hant`,代碼用底線不用連字號。`generate_audio` / `generate_slides` /
`generate_report` 不傳 `language` 時 MCP 都預設 `zh_Hant`;要別的語言在該工具傳
`language=`,或直接在 brief / instructions 內指明。

常用代碼:`zh_Hant`、`zh_Hans`、`en`、`ja`、`ko`。

## Auth

MCP server 由 Claude Code 註冊命令用 `doppler run -p notebooklm -c dev -- ...` 包住，`notebooklm-py` 讀取 `NOTEBOOKLM_AUTH_JSON`。認證過期或 3 VM 同步問題見 [troubleshooting.md](references/troubleshooting.md)。

## References

- [MCP setup](docs/mcp-setup.md)
- [MCP 工具參考](references/tool-reference.md)
- [疑難排解](references/troubleshooting.md)
- [連續 podcast prompts](references/episodic_prompts.md)
- [episodes 範例](references/series_example.md)
