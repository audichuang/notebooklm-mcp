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

### `artifact_wait`

Params:

```json
{"notebook_id": "nb-...", "task_id": "task-...", "timeout": 1200}
```

Returns:

```json
{"task_id": "task-...", "artifact_id": "art-..."}
```

Always pass the generation `task_id`, not a notebook id or source id.

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
{"notebook_id": "nb-...", "question": "請整理三個重點。"}
```

Returns:

```json
{"answer": "..."}
```

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

## Language

`zh_Hant` is the default for audio tools. Invalid codes fail before the SDK
call. Use underscore codes such as `zh_Hant`, not `zh-TW`.
