# notebooklm-mcp

Thin self-built MCP server over `notebooklm-py>=0.3,<0.4` for NotebookLM automation: notebooks, sources, zh_Hant-first audio overview, source-grounded chat, deterministic episodic podcast seasons, per-episode attachments, and RSS publishing.

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
uv tool install --python 3.12 "git+https://github.com/audichuang/notebooklm-mcp.git@v0.1.0"
```

The install exposes `notebooklm-mcp` on PATH. `notebooklm-cover` is added in the packaging task of this migration.

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

## Local smoke

```bash
timeout 8 doppler run -p notebooklm -c dev -- notebooklm-mcp --transport stdio < /dev/null
```

Expected: no traceback and no auth error. Stdio may exit when stdin closes.

## Tests

```bash
uv run pytest -q
```

Tests are offline and use mock clients.
