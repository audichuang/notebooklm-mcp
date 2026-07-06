# notebooklm-mcp

Thin self-built MCP server over `notebooklm-py>=0.7.3,<0.8` for NotebookLM automation: notebooks, sources, zh_Hant-first audio overview, source-grounded chat, deterministic episodic podcast seasons, per-episode attachments, and RSS publishing.

## Requirements

- Python 3.12 only. Python 3.14 triggers a `notebooklm-py` SDK `inspect.signature` bug.
- `uv`
- Doppler CLI with project `notebooklm`, config `dev`, and `NOTEBOOKLM_AUTH_JSON`

## Development install

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest -q
```

## Tool install

```bash
uv tool install --python 3.12 "git+https://github.com/audichuang/notebooklm-mcp.git@v0.2.3"
```

The install exposes `notebooklm-mcp` and `notebooklm-cover` on PATH.

## Run the MCP server

```bash
doppler run -p notebooklm -c dev -- notebooklm-mcp --transport stdio
```

HTTP mode for trusted private networks:

```bash
doppler run -p notebooklm -c dev -- notebooklm-mcp --transport streamable-http --host 0.0.0.0 --port 8484
```

## Auth refresh

On a GUI machine:

```bash
uv pip install -e ".[login]"
uv run playwright install chromium
notebooklm login
bash scripts/sync-auth.sh
```

Headless machines consume the synced Doppler secret and do not run `notebooklm login`.
The MCP server disables NotebookLM SDK cookie keepalive/RotateCookies when
`NOTEBOOKLM_AUTH_JSON` is present, because that inline secret is read-only and
shared across machines.

## Local smoke

```bash
timeout 8 doppler run -p notebooklm -c dev -- notebooklm-mcp --transport stdio < /dev/null
```

Expected: no traceback and no auth error. Stdio may exit when stdin closes.

## Cover CLI

Generate a show cover:

```bash
notebooklm-cover --output cover.jpg --line Agentic --line 工程 --tag "~/.claude/" --subtitle "NotebookLM podcast" --byline audichuang
```

Generate per-episode covers from `series_manifest.json` and write absolute `cover_path` values back into the manifest:

```bash
notebooklm-cover --manifest series_manifest.json --show-name "節目名" --tag "~/.claude/" --byline audichuang --output-dir covers
```

## Tests

```bash
uv run pytest -q
```

Tests are offline and use mock clients.
