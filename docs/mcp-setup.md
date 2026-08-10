# NotebookLM MCP Setup

Install the tagged MCP package on each machine first:

```bash
git ls-remote https://github.com/audichuang/notebooklm-mcp.git refs/tags/v0.4.2
uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@v0.9.6"
```

Register the MCP server with Claude Code using stdio transport and Doppler auth injection.
NOTE: on Claude CLI 2.1.201 the `claude mcp add … -- …` form treats everything after `--` as a
prompt and does NOT register — use `add-json`:

```bash
claude mcp add-json notebooklm -s local \
  '{"command":"doppler","args":["run","-p","notebooklm","-c","dev","--","nblm-mcp","--transport","stdio"]}'
```

Project `.mcp.json` entries should use the same zero-path command:

```json
{
  "mcpServers": {
    "notebooklm": {
      "command": "doppler",
      "args": [
        "run",
        "-p",
        "notebooklm",
        "-c",
        "dev",
        "--",
        "nblm-mcp",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

Each VM runs its own local MCP server process. Doppler injects the same
`NOTEBOOKLM_AUTH_JSON` value into each process, and the MCP server disables SDK
RotateCookies/keepalive for that inline read-only auth source.

For HTTP transport:

```bash
doppler run -p notebooklm -c prd -- \
  nblm-mcp --transport streamable-http --host 0.0.0.0 --port 8484
```

When auth expires, refresh on a GUI machine and sync the storage state back to Doppler:

```bash
notebooklm login
bash scripts/sync-auth.sh
```

Live server boot smoke test:

```bash
timeout 8 doppler run -p notebooklm -c prd -- \
  nblm-mcp --transport stdio < /dev/null
```

Expected: no traceback or auth error. Depending on stdin behavior, the command may exit cleanly or be stopped by `timeout`.
