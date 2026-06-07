# NotebookLM MCP Troubleshooting

## MCP Server

### Server does not appear in Claude Code

Check registration:

```bash
claude mcp list
```

Register again if needed:

```bash
claude mcp add notebooklm -- doppler run -p notebooklm -c dev -- \
  uv run --directory /home/user/research/audiskill/notebooklm-skill \
  python -m notebooklm_mcp.server --transport stdio
```

### Server starts then exits

Run the live smoke test in a shell with Doppler access:

```bash
timeout 8 doppler run -p notebooklm -c dev -- \
  uv run python -m notebooklm_mcp.server --transport stdio < /dev/null
```

There should be no traceback or auth error. Stdio may exit when stdin closes;
that is not itself a failure.

### Import or package errors

Reinstall the local package:

```bash
uv pip install -e ".[dev]"
uv run python -c "from notebooklm_mcp.server import mcp; print(mcp.name)"
```

Expected output: `notebooklm`.

## Doppler Auth

### `NOTEBOOKLM_AUTH_JSON` is empty

Refresh locally and sync:

```bash
notebooklm login
bash scripts/sync-auth.sh
```

Then verify:

```bash
doppler secrets get NOTEBOOKLM_AUTH_JSON -p notebooklm -c dev --plain | head -c 50
```

Expected: JSON beginning with `{"cookies":`.

### Invalid JSON or missing cookies

Rebuild the storage state and sync again:

```bash
notebooklm login
bash scripts/sync-auth.sh
```

The fallback helper can write pasted storage state JSON:

```bash
uv run python -m notebooklm_mcp.auth_cli --out ~/.notebooklm/storage_state.json
```

### 3 VM sync

Doppler is the source of truth. Do not log in independently on each VM.

1. Run `notebooklm login` on a local machine.
2. Run `bash scripts/sync-auth.sh`.
3. Restart each VM's MCP server so it receives the refreshed secret.

## Generation

### Audio generation takes a long time

Use `artifact_wait` with a large timeout. Long audio commonly needs more than
600 seconds:

```json
{"notebook_id": "nb-...", "task_id": "task-...", "timeout": 1200}
```

Timeout does not prove the server-side job failed. Re-run `artifact_wait` with
the same `task_id` or inspect the notebook in the UI.

### Download gets the wrong artifact

Always pass the `artifact_id` returned by `generate_audio` or
`artifact_wait`:

```json
{
  "notebook_id": "nb-...",
  "output_path": "/tmp/notebooklm/ep01.mp3",
  "artifact_id": "art-..."
}
```

### Bad language code

Use `zh_Hant`, not `zh-TW`. The MCP server validates language codes before
calling the SDK.

## Episodic Podcast

### Episode N ignores episode N-1

Check that episode N was generated through `podcast_series` or that
`podcast_episode` received `prior_mp3_path`.

For manual continuation, verify the prior file exists:

```bash
ls -l /tmp/notebooklm/series/ep01.mp3
```

### Regenerating a bad episode

Delete the uploaded source for the bad episode before regenerating:

```json
{"notebook_id": "nb-...", "source_id": "src-..."}
```

Then rerun `podcast_episode` for that episode and continue the series from the
next episode.

### Resume does not feed the prior episode

For `podcast_series(..., start=N)`, ensure this file exists:

```text
output_dir/ep{N-1}.mp3
```

Example: `start=3` needs `output_dir/ep02.mp3`.

## Sources

### mp3 upload fails

Pass the MIME type explicitly:

```json
{
  "notebook_id": "nb-...",
  "file_path": "/tmp/notebooklm/ep01.mp3",
  "mime_type": "audio/mpeg",
  "wait": true
}
```

If the file is remote, download it locally first; `source_add_file` takes a
local path.
