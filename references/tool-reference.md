# NotebookLM MCP Tool Reference

This repo now exposes NotebookLM through the local `notebooklm` MCP server.
The old CLI reference is intentionally replaced by tool contracts.

## Notebook Tools

### `notebook_create`

Params:

```json
{"title": "研究筆記本標題"}
```

Returns:

```json
{"notebook_id": "nb-...", "title": "研究筆記本標題"}
```

### `notebook_list`

Params: none

Returns:

```json
{"notebooks": [{"notebook_id": "nb-...", "title": "Title"}]}
```

### `notebook_get`

Params:

```json
{"notebook_id": "nb-..."}
```

Returns:

```json
{"notebook_id": "nb-...", "title": "Title", "sources_count": 7, "is_owner": true, "created_at": "2026-07-05T09:00:00"}
```

用來在生成/發布前確認「跑對筆記本」。找不到會 fail-fast。

## Source Tools

### `source_add_url`

Params:

```json
{"notebook_id": "nb-...", "url": "https://example.com/article", "wait": true}
```

Returns:

```json
{"source_id": "src-..."}
```

### `source_add_text`

Params:

```json
{
  "notebook_id": "nb-...",
  "title": "Seed notes",
  "content": "Text to add as a source.",
  "wait": true
}
```

Returns:

```json
{"source_id": "src-..."}
```

### `source_add_file`

Params:

```json
{
  "notebook_id": "nb-...",
  "file_path": "/tmp/notebooklm/ep01.mp3",
  "mime_type": "audio/mpeg",
  "wait": true
}
```

Returns:

```json
{"source_id": "src-..."}
```

Use `mime_type="audio/mpeg"` when feeding a generated mp3 back into the
notebook for episodic continuity.

### `source_delete`

Params:

```json
{"notebook_id": "nb-...", "source_id": "src-..."}
```

Returns:

```json
{"deleted": "src-..."}
```

Use before regenerating a rejected episode source.

### `source_list`

Params:

```json
{"notebook_id": "nb-..."}
```

Returns:

```json
{"sources": [{"source_id": "src-...", "title": "EP01 心法篇", "kind": "web_page", "ready": true}]}
```

找 `source_id`(改名/刪除被否決的 mp3 來源、或給 `generate_slides`/`generate_report` 聚焦的
`source_ids`),也確認上傳落地。`ready` 為 NotebookLM 是否吃完該來源。

### `source_fulltext`

Params:

```json
{"notebook_id": "nb-...", "source_id": "src-..."}
```

Returns:

```json
{"source_id": "src-...", "title": "...", "char_count": 1234, "content": "..."}
```

驗證 PDF / Medium / 貼上全文是否真的吃進正文,或讀回上傳 mp3 的逐字稿。**CJK 字間會被插空格**,
關鍵字比對前先 `"".join(text.split())`。

## Audio Artifact Tools

### `generate_audio`

Params:

```json
{
  "notebook_id": "nb-...",
  "instructions": "請用繁體中文深度講解重點。",
  "language": "zh_Hant",
  "audio_format": "deep-dive",
  "audio_length": "long"
}
```

Returns:

```json
{"task_id": "task-...", "artifact_id": "art-..."}
```

Defaults:

| Param | Default |
|-------|---------|
| `language` | `zh_Hant` |
| `audio_format` | `null` for SDK default |
| `audio_length` | `null` for SDK default |

Accepted `audio_format`: `deep-dive`, `brief`, `critique`, `debate`.
Accepted `audio_length`: `short`, `default`, `long`.

### `artifact_list`

Params:

```json
{"notebook_id": "nb-...", "kind": "audio"}
```

`kind` 可省(=全部),或篩:`audio` / `video` / `report` / `quiz` / `flashcards` /
`mind_map` / `infographic` / `slide_deck` / `data_table`。

Returns:

```json
{"artifacts": [{"artifact_id": "art-...", "title": "EP01 心法篇", "kind": "audio", "completed": true, "status": "completed", "created_at": "2026-07-05T09:00:00"}]}
```

**救援/對帳用**:生成被打斷、只知道生了卻沒 `task_id` 時,用它列出筆記本現有 artifact、
拿 `artifact_id` 再 `artifact_download_audio` 救回。

### `artifact_wait`

Params:

```json
{"notebook_id": "nb-...", "task_id": "task-...", "timeout": 1200}
```

Returns:

```json
{"task_id": "task-...", "artifact_id": "art-..."}
```

Always pass the generation `task_id`, not a notebook id or source id. **Fail-closed**:
SDK 若回 failed status(非丟例外),這裡會 raise,不會把失敗當成功回傳。

### `artifact_download_audio`

Params:

```json
{
  "notebook_id": "nb-...",
  "output_path": "/tmp/notebooklm/podcast.mp3",
  "artifact_id": "art-..."
}
```

Returns:

```json
{"path": "/tmp/notebooklm/podcast.mp3"}
```

The SDK argument order is `(notebook_id, output_path, artifact_id)`.

### `artifact_rename`

Params:

```json
{"notebook_id": "nb-...", "artifact_id": "art-...", "new_title": "EP01"}
```

Returns:

```json
{"artifact_id": "art-...", "title": "EP01"}
```

## Chat Tool

### `chat_ask`

Params:

```json
{"notebook_id": "nb-...", "question": "請整理三個重點。", "source_ids": ["src-..."], "conversation_id": "conv-..."}
```

`source_ids` 聚焦特定來源(如只看該集原文、排除前集音檔,避免單集 show notes 被污染);
`conversation_id` 續問同一串。兩者皆可省。

Returns:

```json
{"answer": "...", "conversation_id": "conv-...", "references": [{"source_id": "src-...", "citation_number": 1, "cited_text": "..."}]}
```

`answer` 夾帶引用標記(`[1]`/`[3, 4]`);當公開文字前用 regex `\[[\d,\s\-–]+\]` 清掉。

## Podcast Tools

### `podcast_episode`

Params:

```json
{
  "notebook_id": "nb-...",
  "episode_n": 2,
  "title": "案例篇",
  "brief": "第二集 brief：承接上一集並進入新主題。",
  "output_dir": "/tmp/notebooklm/series",
  "prior_mp3_path": "/tmp/notebooklm/series/ep01.mp3",
  "language": "zh_Hant",
  "audio_format": "deep-dive",
  "audio_length": "long",
  "wait_timeout": 1200
}
```

`title` is required and non-empty: the Studio artifact and the self-uploaded
source are both renamed to `EP{episode_n:02d} {title}` (e.g. `EP02 案例篇`) — the
same string on both sides.

Returns:

```json
{
  "episode": 2,
  "title": "案例篇",
  "label": "EP02 案例篇",
  "task_id": "task-...",
  "artifact_id": "art-...",
  "mp3_path": "/tmp/notebooklm/series/ep02.mp3"
}
```

If `prior_mp3_path` is provided, the tool uploads it first as
`mime_type="audio/mpeg"` and waits for the source to become ready. (The prior
re-seed source keeps a bare `EP{n-1:02d}` name, since the caller may not know the
prior episode's title.)

### `podcast_series`

Params:

```json
{
  "notebook_id": "nb-...",
  "episodes": [
    {"title": "開場篇", "brief": "第一集 brief"},
    {"title": "案例篇", "brief": "第二集 brief"}
  ],
  "output_dir": "/tmp/notebooklm/series",
  "start": 1,
  "language": "zh_Hant",
  "audio_format": "deep-dive",
  "audio_length": "long",
  "wait_timeout": 1200
}
```

Each episode must be a dict with a non-empty `title` AND `brief` (validated up
front, before any generation). The Studio artifact and self-uploaded source for
episode N are both named `EP{N:02d} {title}` (e.g. `EP01 開場篇`).

Returns:

```json
{
  "notebook_id": "nb-...",
  "episodes": [
    {"episode": 1, "title": "開場篇", "label": "EP01 開場篇", "task_id": "task-...", "artifact_id": "art-...", "mp3_path": "..."}
  ],
  "manifest": "/tmp/notebooklm/series/series_manifest.json"
}
```

`start=N` resumes from episode N (validated: `1 <= N <= len(episodes)`; pass the
FULL episodes list). It relies on the same notebook already holding the prior
`EP{N-1:02d} {title}` source (self-uploaded on a prior run); it does NOT read a
local `ep{N-1}.mp3`. The season manifest is merged across resumes, so earlier
episodes are preserved.

### `generate_slides`

Params:

```json
{
  "notebook_id": "nb-...",
  "manifest_path": "/tmp/notebooklm/series/series_manifest.json",
  "episode_n": 1,
  "source_ids": ["src-..."],
  "language": "zh_Hant",
  "instructions": "選填:簡報產生指示",
  "slide_format": "detailed",
  "slide_length": "default",
  "wait_timeout": 1800
}
```

按需生某集簡報並下載 PDF。`slide_format`:`detailed`(內容較完整)/ `presenter`;
`slide_length`:`default` / `short`。`source_ids` 不傳則用全部來源(要聚焦原文請明確傳)。
下載到 manifest 同目錄 `ep{N:02d}-slides.pdf`,並把路徑回寫該集 `slides_pdf_path`。

Returns:

```json
{"episode": 1, "slides_pdf_path": ".../ep01-slides.pdf", "artifact_id": "art-..."}
```

### `generate_report`

Params:

```json
{
  "notebook_id": "nb-...",
  "manifest_path": "/tmp/notebooklm/series/series_manifest.json",
  "episode_n": 1,
  "report_format": "study_guide",
  "source_ids": ["src-..."],
  "language": "zh_Hant",
  "extra_instructions": "選填:附加指示",
  "wait_timeout": 1800
}
```

按需生某集研讀文件並下載 Markdown。`report_format`:`study_guide`(預設)/ `briefing_doc`
/ `blog_post`。下載到 `ep{N:02d}-report.md`,回寫該集 `report_md_path` + `report_format`。
發布時 `publish_series` 會把它渲染成自包含 HTML 再 host。

Returns:

```json
{"episode": 1, "report_md_path": ".../ep01-report.md", "report_format": "study_guide", "artifact_id": "art-..."}
```

### `publish_series`

Params:

```json
{
  "show_id": "my-show",
  "notebook_id": "nb-...",
  "manifest_path": "/tmp/notebooklm/series/series_manifest.json",
  "show_title": "節目名",
  "show_description": "節目描述",
  "author": "作者",
  "owner_name": "擁有者",
  "owner_email": "owner@example.com",
  "artwork_path": "/path/cover.jpg",
  "category": "Technology",
  "explicit": false
}
```

把整季 manifest + mp3 發布成 Apple 合規 RSS feed(內網 HTTP PUT 到 NAS uploader)。
`show_id` 是穩定 slug(feed identity、決定 URL、**永不改**、`[a-z0-9-]`)。`artwork_path` 是
**節目層**封面(掛 channel 的 `itunes:image`)。manifest 每集可帶**選填**欄位:
`description`(→ 單集 `<description>` 純文字 + `<content:encoded>` 富文字)、
`slides_pdf_path` / `report_md_path`(→ content-hash 後 host 附件,具名連結附進單集簡介)、
`cover_path`(→ **單集封面**,同 `validate_artwork` 規格驗證,host 成 `EP0n-cover-<hash>`,
該集 `<item>` 掛 `itunes:image`;沒給就 fallback 節目封面)。單集封面用
`scripts/make_cover.py --manifest` 批次生(見 AGENTS.md 封面段)。缺檔/不合規 **fail-fast**。
提交順序:媒體(mp3 + 單集封面 + 附件 + 節目封面)→ show.json → feed.xml/index.html。

Returns:

```json
{
  "feed_url": "https://.../feeds/<token>/feed.xml",
  "show_page_url": "https://.../feeds/<token>/index.html",
  "token": "<token>",
  "episode_count": 1,
  "episodes": [{"n": 1, "title": "...", "guid": "...", "url": "https://.../EP01-<hash>.mp3"}]
}
```

### `feed_info`

Params:

```json
{"show_id": "my-show"}
```

純計算(token = HMAC(salt, show_id)),不含各集細節。

Returns:

```json
{"show_id": "my-show", "token": "<token>", "feed_url": "https://.../feed.xml", "show_page_url": "https://.../index.html"}
```

## Language

`zh_Hant` is the default for audio tools. Invalid codes fail before the SDK
call. Use underscore codes such as `zh_Hant`, not `zh-TW`.
