# Spec:每集附加簡報 PDF + 研讀講義(episode attachments)

## 1. 目的與背景

現況一集只有音檔:`podcast_series` 生 mp3 → 寫 `series_manifest.json` → `publish_series`
發布成 RSS。使用者要的「完整包」是**音檔 + 簡報 PDF + 研讀講義**,三者都能從單集介面拿到。

NotebookLM SDK(`notebooklm-py 0.3.4`,已實裝)本就支援這些產物,只是我們的 MCP 沒包:

- `artifacts.generate_slide_deck(...)` → `download_slide_deck(output_path, output_format="pdf"|"pptx")`
  —— 產出**二進位 PDF**(已實測:11 頁繁中簡報,設計精美)。
- `artifacts.generate_report(report_format=BRIEFING_DOC|STUDY_GUIDE|BLOG_POST|CUSTOM, ...)`
  → `download_report(output_path)` —— 產出 **Markdown 文字**。
- `generate_study_guide(...)` = `generate_report(report_format=STUDY_GUIDE)` 的便捷包裝。

發布端 uploader(`podcast-feed-host/uploader/server.py`)對檔名有**嚴格白名單**
(`_NAME` regex,信任邊界),只收 `feed.xml|index.html|show.json|artwork.(png|jpg)|EP\d{2}-<hash8>.mp3`。
非音檔一律 404 —— 這是「PDF 進不了 feed」的根因,本案要放寬。

`publish/feed.py:57` 早已支援 per-episode `<description>`(`ep.get('description', ep['title'])`);
`publish_series` 也已支援 manifest 帶 `description`(2026-07-05 前一次改動)。附件連結會**附加**在該描述之後。

## 2. 使用模式與範圍

錨定既有模式:**單人自用 / 一次發整季 / 事後不補集**。本案在此之上加「附件」:

- 附件**選填、按需生**:想幫哪集加簡報/講義,就對那集跑對應工具。不強制每集都有。
- 不動音檔生成迴圈(`podcast_series` / `_run_episode` 不變),避免拖慢與增加失敗面。
- 產物只餵**文章來源**(不含 EP mp3 來源),讓簡報/講義聚焦原文(與實測一致)。

**範圍內**:slide deck(PDF)+ report(Markdown,預設 `study_guide`,可選 briefing/blog)。
**範圍外(YAGNI)**:infographic、data table、quiz、flashcards、mind map、video、pptx 下載、
每集獨立 landing page、自動每集生成(podcast_series 旗標)。將來要再加。

## 3. 資料流

```
podcast_series → series_manifest.json（每集：episode/title/mp3_path/…）
      │
      ├─ generate_slides(manifest, ep_n)  → 下載 ep{n:02d}-slides.pdf，回寫 ep.slides_pdf_path
      └─ generate_report(manifest, ep_n)  → 下載 ep{n:02d}-report.md，回寫 ep.report_md_path (+report_format)
      │
publish_series（每集）:
      1) PUT mp3（現況）
      2) slides_pdf_path 存在 → hash → PUT EP{n:02d}-<hash8>.pdf
      3) report_md_path 存在 → md→HTML → hash → PUT EP{n:02d}-<hash8>.html
      4) 組單集 <description> = (manifest description or title) + 附件連結區塊
      → show.json / feed.xml / index.html（現況順序：媒體→state→derived）
```

## 4. 元件

### 4.1 兩個新 MCP 工具(`notebooklm_mcp/tools_artifacts.py`,新檔)

```python
generate_slides(notebook_id, manifest_path, episode_n,
                source_ids=None, language=None,          # 預設 zh_Hant
                instructions=None,
                slide_format="detailed",                 # detailed | presenter
                slide_length="default",                  # default | short
                wait_timeout=1800.0) -> dict
```
- `source_ids=None` → 預設只取文章來源。**取法**:呼叫端可傳；不傳則用全部來源
  但排除副檔名判定為音檔的來源(見 §7 開放項)。v1 先接受 `source_ids` 明確傳入,
  不傳則 fallback「全部來源」並在回傳提示;不做音檔來源自動排除(記入 gotcha)。
- 流程:`generate_slide_deck` → `ensure_started` → `wait_for_completion` → `ensure_completed`
  → `download_slide_deck(output_format="pdf")`,存 `dirname(manifest)/ep{n:02d}-slides.pdf`。
- **回寫 manifest**:載入 manifest,找 `episode==episode_n` 的 dict,設 `slides_pdf_path`,寫回。
- 回傳 `{episode, slides_pdf_path, artifact_id}`。

```python
generate_report(notebook_id, manifest_path, episode_n,
                report_format="study_guide",             # study_guide | briefing_doc | blog_post
                source_ids=None, language=None,          # 預設 zh_Hant
                extra_instructions=None,
                wait_timeout=1800.0) -> dict
```
- 流程同上,`generate_report(report_format=…)` → `download_report`,存 `ep{n:02d}-report.md`。
- 回寫 `report_md_path` + `report_format`。回傳 `{episode, report_md_path, report_format, artifact_id}`。

共用 helper `_load_ep_and_write(manifest_path, episode_n, **fields)`:讀 manifest、驗證該集存在、
merge 欄位、寫回(沿用 `tools_podcast._write_manifest` 的直接 `json.dump` 寫法,不另做原子替換)。
找不到該集 → 清楚 ValueError。

`slide_format` / `report_format` 字串 → SDK enum 的映射放 `enums.py`(延續既有 `to_audio_*` 風格)。

### 4.2 Markdown→HTML 渲染(`notebooklm_mcp/publish/notes_html.py`,新檔)

- 依賴 **`markdown`**(PyPI,純 Python)—— 已核可加入依賴。
- `render_report_html(markdown_text, title) -> str`:`markdown.markdown(md, extensions=["extra","sane_lists"])`
  後,包進一個**自包含、深淺色自適應、手機好讀**的 HTML 殼(inline CSS,無外部資源)。
- 純函式、可離線測。

### 4.3 publish_series 擴充(`notebooklm_mcp/tools_publish.py`)

- 新 helper `attachment_filename(n, hash8, ext)`(放 `publish/layout.py`,與 `media_filename` 並列)
  → `EP{n:02d}-{hash8}.{ext}`。
- 迴圈內每集 mp3 之後:
  - `slides_pdf_path` 存在且檔案在 → 讀 bytes → hash8 → PUT `EP{n:02d}-<hash8>.pdf` → 記 URL。
  - `report_md_path` 存在且檔案在 → 讀 md → `render_report_html` → hash8(對 **HTML bytes**)
    → PUT `EP{n:02d}-<hash8>.html` → 記 URL。
- **描述組裝**:`show["episodes"][n]["description"]` =
  `base = (ep.get("description") or "").strip() or ep["title"]`,再 append:
  ```
  \n\n📄 本集簡報:<pdf_url>
  📖 研讀講義:<html_url>
  ```
  只 append 存在的附件。純文字 URL(podcast app 會自動可點)。
- 附件缺檔(路徑在 manifest 但檔案不見)→ **fail-fast**(與 mp3 的 `_ensure_local_mp3` 同精神;
  但附件不做 re-download,直接報清楚錯誤,叫使用者重跑對應工具)。
- 記憶體紀律不變:一次一檔 read→hash→PUT→drop。

### 4.4 uploader 白名單 + 部署(`podcast-feed-host` repo)

- `server.py` `_NAME` regex:
  ```python
  _NAME = r"(?:feed\.xml|index\.html|show\.json|artwork\.(?:png|jpg)|EP\d{2}-[0-9a-f]{8}\.(?:mp3|pdf|html))"
  ```
- `uploader/test_server.py` 加案例:`EP01-deadbeef.pdf` / `EP01-deadbeef.html` PUT 通過(201);
  非白名單(如 `.txt`、`evil.pdf`)仍 404。
- **部署**:NAS 上 `docker compose build uploader && docker compose up -d uploader`(唯一對外部署動作)。
  Caddy 唯讀端不變(它服務同目錄,新副檔名自然可讀)。

## 5. manifest 結構(向後相容)

每集 dict 新增兩個**選填**欄位:

```json
{
  "episode": 1, "title": "…", "mp3_path": "…", "published_at": "…",
  "slides_pdf_path": "…/ep01-slides.pdf",   // 選填,generate_slides 寫入
  "report_md_path":  "…/ep01-report.md",    // 選填,generate_report 寫入
  "report_format":   "study_guide"          // 選填,伴隨 report_md_path
}
```

沒有這些欄位的舊 manifest 照跑(publish 只是不 PUT 附件、描述不 append 連結)。

## 6. 測試(TDD:先紅後綠)

- **contract**(`tests/test_contracts.py`):用 `inspect.signature` 鎖
  `generate_slide_deck`、`generate_report`、`generate_study_guide`、`download_slide_deck`、
  `download_report` 的公開參數,擋上游漂移。
- **新工具**(`tests/test_tools_artifacts.py`):fake client,斷言
  ① 呼叫 SDK 對的方法與參數;② 檔案下載到預期路徑;③ **路徑回寫進 manifest 該集**;
  ④ episode_n 不存在 → ValueError。
- **notes_html**(`tests/test_notes_html.py`):標題/清單/程式塊有正確 HTML;輸出自包含(無外部連結)。
- **publish_series**(`tests/test_publish_tools.py`):manifest 帶 `slides_pdf_path`/`report_md_path`
  → 對應 `EP\d{2}-<hash8>.pdf`/`.html` 有 PUT;show.json 該集 description 尾端含兩個 URL;
  無附件的集 → 行為同現況;附件路徑在但檔案缺 → fail-fast。
- **layout**:`attachment_filename` 單元測試。
- **uploader**(`podcast-feed-host/uploader/test_server.py`):pdf/html 白名單通過、其他仍拒。

離線全綠(mock client + httpx.MockTransport),零真實網路。

## 7. 開放項 / 已知取捨(記入 AGENTS.md gotchas)

- **source_ids 音檔排除**:v1 不自動排除音檔來源;要聚焦原文請明確傳 `source_ids`。
  自動排除留待需求出現(避免猜 SDK 來源型別欄位)。
- **slide_deck 語言碼**:沿用 `resolve_language`(實測 `zh_Hant` 可用)。
- **report 只 host HTML**:`.md` 留在本機(manifest 記路徑)當來源,不 host `.md`(使用者選「渲染成網頁」)。
- **content-addressed**:pdf/html 檔名帶內容 hash,重生內容變→新 URL、舊快取不壞,GUID/描述其餘不變。
- **部署順序**:先部署 uploader 新白名單,再跑帶附件的 `publish_series`(否則附件 PUT 會 404)。
