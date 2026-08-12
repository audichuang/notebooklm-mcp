# NotebookLM MCP Setup

Install the tagged MCP package on each machine first:

```bash
git ls-remote https://github.com/audichuang/notebooklm-mcp.git refs/tags/v0.9.12
uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@v0.9.12"
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
  nblm-mcp --transport streamable-http --host 127.0.0.1 --port 8484
```

HTTP/SSE transport 本身沒有認證，server 預設拒絕綁定非 loopback host。只有在前方已有
可信任的認證與網路邊界時，才明確加上 `--allow-insecure-remote`；不要直接把服務暴露到公網。

When auth expires, refresh on a GUI machine and sync the storage state back to Doppler:

```bash
uv run notebooklm login
bash scripts/sync-auth.sh
```

Live server boot smoke test:

```bash
timeout 8 doppler run -p notebooklm -c prd -- \
  nblm-mcp --transport stdio < /dev/null
```

Expected: no traceback or auth error. Depending on stdin behavior, the command may exit cleanly or be stopped by `timeout`.
