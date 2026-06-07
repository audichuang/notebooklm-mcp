# NotebookLM MCP Setup

Register the MCP server with Claude Code using stdio transport and Doppler
auth injection:

```bash
claude mcp add notebooklm -- doppler run -p notebooklm -c dev -- \
  uv run --directory /home/user/research/audiskill/notebooklm-skill \
  python -m notebooklm_mcp.server --transport stdio
```

Each VM runs its own local MCP server process. Doppler injects the same
`NOTEBOOKLM_AUTH_JSON` value into each process, and `notebooklm-py` treats that
env var as read-only storage state.

For HTTP transport:

```bash
doppler run -p notebooklm -c dev -- \
  uv run --directory /home/user/research/audiskill/notebooklm-skill \
  python -m notebooklm_mcp.server \
  --transport streamable-http --host 0.0.0.0 --port 8484
```

When auth expires, refresh on a local machine and sync the storage state back
to Doppler:

```bash
notebooklm login
bash scripts/sync-auth.sh
```

VMs pick up the refreshed secret on the next MCP server start.

Live server boot smoke test:

```bash
timeout 8 doppler run -p notebooklm -c dev -- \
  uv run python -m notebooklm_mcp.server --transport stdio < /dev/null
```

Expected: no traceback or auth error. Depending on stdin behavior, the command
may exit cleanly or be stopped by `timeout`.
