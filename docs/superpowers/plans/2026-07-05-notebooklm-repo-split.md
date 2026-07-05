# NotebookLM 拆 repo 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把現有 `notebooklm-skill` 拆成可 `uv tool install` 的 `notebooklm-mcp` 工具 repo 與 docs-only 的 `audi-skill/notebooklm` skill repo,消除絕對路徑與多份程式碼飄移。

**Architecture:** 沿用 `/home/user/research/audiskill/notebooklm-skill` 的 git history,先 merge 已核可分支,再把整個 checkout 搬成 `/home/user/research/audiskill/notebooklm-mcp`。MCP repo 只保留 Python package、tests、docs、CI 與 MCP/cover console scripts;skill repo 只保留 `SKILL.md`、`references/` 與 tracked `.mcp.example.json`。消費端逐台先安裝 tagged tool、驗證可啟動,再切 `.mcp.json`/`.codex/config.toml` 到零路徑命令。

**Tech Stack:** Python 3.12、`uv`/`uv tool`、FastMCP、`notebooklm-py>=0.3,<0.4`、Pillow、pytest/pytest-asyncio、GitHub private repo、Doppler `NOTEBOOKLM_AUTH_JSON`。

---

## Global Constraints

- `pyproject.toml` 的 `requires-python` 必須收緊為 `>=3.12,<3.13`;Python 3.14 會觸發 `notebooklm-py` SDK 的 `inspect.signature` bug。
- GitHub `notebooklm-mcp` 是 private repo;每台 VM 都要先用自己的 SSH key 或 GitHub HTTPS 認證通過 `git ls-remote`,再執行 `uv tool install`。
- 生產與消費端必須 pin git tag 或 SHA,本計畫使用 `v0.1.0`;不要追 `main`/`master`。
- 註解、文件、commit message 繼續用繁體中文;commit message 要寫清楚症狀、根因、修法。
- 消費端一律先裝新 `notebooklm-mcp`、驗證 MCP server 起得來、切設定並重連看到工具後,才刪舊碼或讓該機 pull docs-only skill。
- 不發 PyPI;本輪只支援 `uv tool install git+https://github.com/audichuang/notebooklm-mcp.git@v0.1.0`。
- 不改 auth 架構;仍由 `doppler run -p notebooklm -c dev -- ...` 注入 `NOTEBOOKLM_AUTH_JSON`。
- 不動 skill 部署或 symlink 機制;只清理 `audi-skill/notebooklm` 內容與提供 `.mcp.example.json`。

### Task 1: Merge observability branch into master

**Files:**
- Modify: `/home/user/research/audiskill/notebooklm-skill/.git`
- Test: git status/log only

- [ ] **Step 1: Verify current branch and clean worktree**

Run: `git -C /home/user/research/audiskill/notebooklm-skill status --short --branch`

Expected:

```text
## feat/mcp-read-observability
```

If any extra line appears after the branch line, stop and inspect with `git -C /home/user/research/audiskill/notebooklm-skill diff --stat`; do not stash or discard user work without explicit approval.

- [ ] **Step 2: Verify the branch contains the approved split spec**

Run: `git -C /home/user/research/audiskill/notebooklm-skill log --oneline master..feat/mcp-read-observability`

Expected output includes these 8 commits in newest-first order:

```text
9d7d90b docs(spec): 補進 Codex 盲點審查(遷移前置 + 已知風險)
31f4a8b docs(spec): NotebookLM 拆 repo 設計(MCP server 與 skill 分離)
4e44bbb docs(skill): 讓 workflow 指引真的用上新讀取工具(搭配 MCP,不只列在工具表)
2670ff6 test(make_cover): 收緊 preflight 斷言,擋假綠(Codex 複查 Low)
0cbd4b1 fix: 修掉 Codex code review 找到的問題(集中在 make_cover --manifest)
c8b2e56 docs: 補讀取工具 + 單集封面到 SKILL/tool-reference/AGENTS
284c23e feat(publish): 單集各自封面(每集 <item> 掛 itunes:image)
6b7f248 feat(tools): 補上 MCP 缺的讀取/觀測面 + 生成 wait fail-closed
```

The design spec said "領先 master 7 commit"; the real repo on 2026-07-05 has 8 because `9d7d90b` adds the Codex blind-spot review.

- [ ] **Step 3: Switch to master**

Run: `git -C /home/user/research/audiskill/notebooklm-skill switch master`

Expected:

```text
Switched to branch 'master'
Your branch is up to date with 'origin/master'.
```

- [ ] **Step 4: Fast-forward local master from current origin before merging**

Run: `git -C /home/user/research/audiskill/notebooklm-skill pull --ff-only origin master`

Expected:

```text
Already up to date.
```

- [ ] **Step 5: Merge the feature branch with an explicit merge commit**

Run: `git -C /home/user/research/audiskill/notebooklm-skill merge --no-ff feat/mcp-read-observability -m "merge: bring NotebookLM MCP observability work into master"`

Expected output contains:

```text
Merge made by the 'ort' strategy.
```

Rollback before Task 4 pushes the new repo: `git -C /home/user/research/audiskill/notebooklm-skill reset --hard ORIG_HEAD`

- [ ] **Step 6: Verify master now contains the approved spec and is clean**

Run: `git -C /home/user/research/audiskill/notebooklm-skill status --short --branch`

Expected:

```text
## master...origin/master [ahead N]
```

Run: `git -C /home/user/research/audiskill/notebooklm-skill log --oneline -1`

Expected output starts with the new merge commit SHA and contains:

```text
merge: bring NotebookLM MCP observability work into master
```

### Task 2: Rename MCP checkout and remove skill layer from MCP repo

**Files:**
- Modify: `/home/user/research/audiskill/notebooklm-skill` -> `/home/user/research/audiskill/notebooklm-mcp`
- Modify: `/home/user/research/audiskill/notebooklm-mcp/pyproject.toml`
- Modify: `/home/user/research/audiskill/notebooklm-mcp/README.md`
- Modify: `/home/user/research/audiskill/notebooklm-mcp/docs/mcp-setup.md`
- Delete: `/home/user/research/audiskill/notebooklm-mcp/SKILL.md`
- Delete: `/home/user/research/audiskill/notebooklm-mcp/references/`
- Test: `uv run pytest -q`

- [ ] **Step 1: Verify the old checkout exists and the target path is free**

Run: `test -d /home/user/research/audiskill/notebooklm-skill`

Expected: exit code 0 and no output.

Run: `test ! -e /home/user/research/audiskill/notebooklm-mcp`

Expected: exit code 0 and no output.

- [ ] **Step 2: Rename the checkout directory while preserving `.git`**

Run: `mv /home/user/research/audiskill/notebooklm-skill /home/user/research/audiskill/notebooklm-mcp`

Expected: no output.

Rollback before committing Task 2: `mv /home/user/research/audiskill/notebooklm-mcp /home/user/research/audiskill/notebooklm-skill`

- [ ] **Step 3: Verify git history still works from the new path**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp status --short --branch`

Expected:

```text
## master...origin/master [ahead N]
```

- [ ] **Step 4: Remove the skill routing layer from the MCP repo**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp rm -r SKILL.md references`

Expected output:

```text
rm 'SKILL.md'
rm 'references/episodic_prompts.md'
rm 'references/series_example.md'
rm 'references/tool-reference.md'
rm 'references/troubleshooting.md'
```

- [ ] **Step 5: Replace `pyproject.toml` with MCP package metadata and Python 3.12 bound**

Set `/home/user/research/audiskill/notebooklm-mcp/pyproject.toml` to exactly:

```toml
[project]
name = "notebooklm-mcp"
version = "0.1.0"
description = "Thin self-built MCP server over notebooklm-py for NotebookLM automation"
requires-python = ">=3.12,<3.13"
dependencies = [
    "notebooklm-py>=0.3,<0.4",
    "mcp[cli]>=1.0.0",
    "Pillow>=10,<12",
    "httpx>=0.27,<1",
    "markdown>=3.5",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]
# Only the LOCAL login machine needs a browser; the 3 VMs auth via Doppler
# (NOTEBOOKLM_AUTH_JSON) and never install this. After installing, run:
#   uv run playwright install chromium
login = ["notebooklm-py[browser]>=0.3,<0.4"]

[project.scripts]
notebooklm-mcp = "notebooklm_mcp.server:main"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.hatch.build.targets.wheel]
packages = ["notebooklm_mcp"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 6: Replace `README.md` with MCP-only installation and operation docs**

Set `/home/user/research/audiskill/notebooklm-mcp/README.md` to exactly:

````markdown
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
````

- [ ] **Step 7: Replace `docs/mcp-setup.md` with path-free MCP setup**

Set `/home/user/research/audiskill/notebooklm-mcp/docs/mcp-setup.md` to exactly:

````markdown
# NotebookLM MCP Setup

Install the tagged MCP package on each machine first:

```bash
git ls-remote https://github.com/audichuang/notebooklm-mcp.git refs/tags/v0.1.0
uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@v0.1.0"
```

Register the MCP server with Claude Code using stdio transport and Doppler auth injection:

```bash
claude mcp add notebooklm -- doppler run -p notebooklm -c dev -- \
  notebooklm-mcp --transport stdio
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
        "notebooklm-mcp",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

Each VM runs its own local MCP server process. Doppler injects the same `NOTEBOOKLM_AUTH_JSON` value into each process, and `notebooklm-py` treats that env var as read-only storage state.

For HTTP transport:

```bash
doppler run -p notebooklm -c dev -- \
  notebooklm-mcp --transport streamable-http --host 0.0.0.0 --port 8484
```

When auth expires, refresh on a GUI machine and sync the storage state back to Doppler:

```bash
notebooklm login
bash scripts/sync-auth.sh
```

Live server boot smoke test:

```bash
timeout 8 doppler run -p notebooklm -c dev -- \
  notebooklm-mcp --transport stdio < /dev/null
```

Expected: no traceback or auth error. Depending on stdin behavior, the command may exit cleanly or be stopped by `timeout`.
````

- [ ] **Step 8: Recreate the Python 3.12 environment after tightening `requires-python`**

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp venv --python 3.12`

Expected output contains:

```text
Using CPython 3.12
Creating virtual environment
```

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp pip install -e ".[dev]"`

Expected output contains:

```text
Installed
```

- [ ] **Step 9: Run offline tests**

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp run pytest -q`

Expected output ends with:

```text
passed
```

- [ ] **Step 10: Verify only the skill layer was deleted from MCP repo**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp status --short`

Expected output includes:

```text
D  SKILL.md
D  references/episodic_prompts.md
D  references/series_example.md
D  references/tool-reference.md
D  references/troubleshooting.md
M  README.md
M  docs/mcp-setup.md
M  pyproject.toml
```

- [ ] **Step 11: Commit the MCP repo rename/package identity change**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp add README.md docs/mcp-setup.md pyproject.toml`

Expected: no output.

Run: `git -C /home/user/research/audiskill/notebooklm-mcp commit -m "chore: split MCP server from NotebookLM skill layer"`

Expected output contains:

```text
chore: split MCP server from NotebookLM skill layer
```

Rollback after this commit and before pushing: `git -C /home/user/research/audiskill/notebooklm-mcp reset --hard HEAD~1`

### Task 3: Package `notebooklm-cover` with real TDD

**Files:**
- Create: `/home/user/research/audiskill/notebooklm-mcp/notebooklm_mcp/cover_cli.py` by moving `/home/user/research/audiskill/notebooklm-mcp/scripts/make_cover.py`
- Modify: `/home/user/research/audiskill/notebooklm-mcp/pyproject.toml`
- Modify: `/home/user/research/audiskill/notebooklm-mcp/tests/test_make_cover.py`
- Modify: `/home/user/research/audiskill/notebooklm-mcp/README.md`
- Test: `uv run pytest tests/test_make_cover.py -q`

**Interfaces:**
- New console script: `notebooklm-cover = "notebooklm_mcp.cover_cli:main"`
- Existing function kept: `notebooklm_mcp.cover_cli.make_cover(output, lines, tag, subtitle, byline, episode=None, font_cjk=None, font_mono=None) -> None`

- [ ] **Step 1: Change the subprocess tests to call the new console command before it exists**

Set `/home/user/research/audiskill/notebooklm-mcp/tests/test_make_cover.py` to exactly:

```python
"""notebooklm-cover --manifest 的 preflight 契約(跑真 CLI,只測錯誤路徑:壞資料在
任何算圖/寫回前就 fail,且不覆寫 manifest)。錯誤路徑不觸發字型/算圖,所以快且不依賴字型。

斷言鎖到 argparse error 的 exit code 2 + 特定錯誤訊息 + stderr 無 Traceback——
只看 `returncode != 0` 會假綠(import 錯、語法錯、任何 traceback 都是非零)。"""
import json
import subprocess
from pathlib import Path


def _run(manifest_path: Path):
    return subprocess.run(
        [
            "uv",
            "run",
            "notebooklm-cover",
            "--manifest",
            str(manifest_path),
            "--tag",
            "~/.claude/",
            "--byline",
            "x",
        ],
        capture_output=True,
        text=True,
    )


def _write(tmp_path, episodes, title="節目名"):
    p = tmp_path / "m.json"
    p.write_text(
        json.dumps({"title": title, "episodes": episodes}, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


def _assert_preflight_rejected(r, msg_substr):
    # argparse error -> exit 2;不是 traceback(exit 1)也不是任何其他非零。
    assert r.returncode == 2, f"expected argparse exit 2, got {r.returncode}: {r.stderr}"
    assert "Traceback" not in r.stderr, f"crashed instead of clean reject: {r.stderr}"
    assert msg_substr in r.stderr, f"missing preflight msg {msg_substr!r} in: {r.stderr}"


def test_string_episode_rejected_and_manifest_untouched(tmp_path):
    # "1"(字串)以前會撞上 f"EP{n:02d}" 崩潰或被靜默處理;現在 preflight 直接擋。
    p = _write(tmp_path, [{"episode": "1", "title": "心法篇"}])
    before = p.read_text(encoding="utf-8")
    _assert_preflight_rejected(_run(p), "整數")
    assert p.read_text(encoding="utf-8") == before   # 壞資料不半途覆寫(原子)


def test_missing_title_rejected(tmp_path):
    _assert_preflight_rejected(_run(_write(tmp_path, [{"episode": 1}])), "title")


def test_empty_episodes_rejected(tmp_path):
    _assert_preflight_rejected(_run(_write(tmp_path, [])), "episodes")


def test_duplicate_episode_rejected(tmp_path):
    r = _run(_write(tmp_path, [{"episode": 1, "title": "a"}, {"episode": 1, "title": "b"}]))
    _assert_preflight_rejected(r, "重複")
```

- [ ] **Step 2: Run the focused test and verify red**

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp run pytest tests/test_make_cover.py -q`

Expected: FAIL. The failure must be in `_assert_preflight_rejected`, and stderr must mention that `notebooklm-cover` cannot be spawned or is not an installed command. This red proves tests no longer execute `scripts/make_cover.py` by path.

- [ ] **Step 3: Move the CLI into the package**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp mv scripts/make_cover.py notebooklm_mcp/cover_cli.py`

Expected:

```text
```

No stdout is expected. Keep `/home/user/research/audiskill/notebooklm-mcp/scripts/sync-auth.sh` in place; login sync remains an MCP repo maintenance script.

- [ ] **Step 4: Add the `notebooklm-cover` entry point**

Edit the `[project.scripts]` table in `/home/user/research/audiskill/notebooklm-mcp/pyproject.toml` to exactly:

```toml
[project.scripts]
notebooklm-mcp = "notebooklm_mcp.server:main"
notebooklm-cover = "notebooklm_mcp.cover_cli:main"
```

- [ ] **Step 5: Update README to document both installed commands**

In `/home/user/research/audiskill/notebooklm-mcp/README.md`, replace this line:

```markdown
The install exposes `notebooklm-mcp` on PATH. `notebooklm-cover` is added in the packaging task of this migration.
```

with this line:

```markdown
The install exposes `notebooklm-mcp` and `notebooklm-cover` on PATH.
```

Then insert this section immediately after the "Local smoke" section:

````markdown
## Cover CLI

Generate a show cover:

```bash
notebooklm-cover --output cover.jpg --line Agentic --line 工程 --tag "~/.claude/" --subtitle "NotebookLM podcast" --byline audichuang
```

Generate per-episode covers from `series_manifest.json` and write absolute `cover_path` values back into the manifest:

```bash
notebooklm-cover --manifest series_manifest.json --show-name "節目名" --tag "~/.claude/" --byline audichuang --output-dir covers
```
````

- [ ] **Step 6: Run the focused test and verify green**

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp run pytest tests/test_make_cover.py -q`

Expected:

```text
4 passed
```

- [ ] **Step 7: Verify the new entry point is visible**

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp run notebooklm-cover --help`

Expected output contains:

```text
生成 Apple-Podcast 合規節目封面
```

- [ ] **Step 8: Run the full offline test suite**

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp run pytest -q`

Expected output ends with:

```text
passed
```

- [ ] **Step 9: Commit the cover packaging change**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp add pyproject.toml README.md tests/test_make_cover.py notebooklm_mcp/cover_cli.py`

Expected: no output.

Run: `git -C /home/user/research/audiskill/notebooklm-mcp commit -m "feat: package deterministic NotebookLM cover CLI"`

Expected output contains:

```text
feat: package deterministic NotebookLM cover CLI
```

Rollback after this commit and before pushing: `git -C /home/user/research/audiskill/notebooklm-mcp reset --hard HEAD~1`

### Task 4: Create or connect GitHub remote for `notebooklm-mcp`

**Files:**
- Modify: `/home/user/research/audiskill/notebooklm-mcp/.git/config`
- External: `https://github.com/audichuang/notebooklm-mcp.git`
- Test: `git ls-remote`, `gh repo view`

- [ ] **Step 1: Verify current remote still points to the old repo**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp remote -v`

Expected:

```text
origin	https://github.com/audichuang/notebooklm-skill.git (fetch)
origin	https://github.com/audichuang/notebooklm-skill.git (push)
```

- [ ] **Step 2: Verify GitHub CLI auth**

Run: `gh auth status -h github.com`

Expected output contains:

```text
Logged in to github.com
```

- [ ] **Step 3: Create the private GitHub repo only if it does not already exist**

Run: `gh repo view audichuang/notebooklm-mcp --json nameWithOwner,isPrivate,defaultBranchRef`

Expected on first migration:

```text
GraphQL: Could not resolve to a Repository with the name 'audichuang/notebooklm-mcp'.
```

If the repo is missing, run: `gh repo create audichuang/notebooklm-mcp --private --description "Thin MCP server for NotebookLM automation"`

Expected output contains:

```text
https://github.com/audichuang/notebooklm-mcp
```

If `gh repo view` already returns JSON with `"nameWithOwner":"audichuang/notebooklm-mcp"` and `"isPrivate":true`, do not run `gh repo create`.

- [ ] **Step 4: Point origin to the new SSH remote**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp remote set-url origin https://github.com/audichuang/notebooklm-mcp.git`

Expected: no output.

Rollback before pushing: `git -C /home/user/research/audiskill/notebooklm-mcp remote set-url origin https://github.com/audichuang/notebooklm-skill.git`

- [ ] **Step 5: Verify origin before pushing**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp remote -v`

Expected:

```text
origin	https://github.com/audichuang/notebooklm-mcp.git (fetch)
origin	https://github.com/audichuang/notebooklm-mcp.git (push)
```

- [ ] **Step 6: Push master to the new remote**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp push -u origin master`

Expected output contains:

```text
branch 'master' set up to track 'origin/master'
```

Rollback after an accidental push to the new repo and before any consumer installs from it: `gh repo delete audichuang/notebooklm-mcp --yes`

- [ ] **Step 7: Set and verify the GitHub default branch**

Run: `gh repo edit audichuang/notebooklm-mcp --default-branch master`

Expected: no output.

Run: `gh repo view audichuang/notebooklm-mcp --json nameWithOwner,isPrivate,defaultBranchRef`

Expected JSON:

```json
{"defaultBranchRef":{"name":"master"},"isPrivate":true,"nameWithOwner":"audichuang/notebooklm-mcp"}
```

- [ ] **Step 8: Verify remote read access by SSH**

Run: `git ls-remote https://github.com/audichuang/notebooklm-mcp.git refs/heads/master`

Expected: one line matching:

```text
^[0-9a-f]{40}[[:space:]]+refs/heads/master$
```

### Task 5: Tag the first pinned MCP release

**Files:**
- Modify: `/home/user/research/audiskill/notebooklm-mcp/.git/refs/tags/v0.1.0`
- External: `https://github.com/audichuang/notebooklm-mcp.git` tag `v0.1.0`
- Test: `git ls-remote --tags`

- [ ] **Step 1: Verify working tree is clean before tagging**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp status --short --branch`

Expected:

```text
## master...origin/master
```

- [ ] **Step 2: Verify no tag currently exists**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp tag --list v0.1.0`

Expected: no output.

- [ ] **Step 3: Create an annotated release tag**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp tag -a v0.1.0 -m "v0.1.0: split NotebookLM MCP server package"`

Expected: no output.

Rollback before pushing the tag: `git -C /home/user/research/audiskill/notebooklm-mcp tag -d v0.1.0`

- [ ] **Step 4: Push the release tag**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp push origin v0.1.0`

Expected output contains:

```text
* [new tag]         v0.1.0 -> v0.1.0
```

Rollback after pushing and before consumer rollout: `git -C /home/user/research/audiskill/notebooklm-mcp push origin :refs/tags/v0.1.0`

- [ ] **Step 5: Verify the tag can be read by install hosts**

Run: `git ls-remote https://github.com/audichuang/notebooklm-mcp.git refs/tags/v0.1.0`

Expected: one line matching:

```text
^[0-9a-f]{40}[[:space:]]+refs/tags/v0.1.0$
```

### Task 6: Slim `audi-skill/notebooklm` to docs-only skill

**Files:**
- Modify: `/home/user/research/audi-skill/.gitignore`
- Modify: `/home/user/research/audi-skill/notebooklm/SKILL.md`
- Modify: `/home/user/research/audi-skill/notebooklm/references/troubleshooting.md`
- Modify: `/home/user/research/audi-skill/notebooklm/references/tool-reference.md`
- Create: `/home/user/research/audi-skill/notebooklm/.mcp.example.json`
- Delete: `/home/user/research/audi-skill/notebooklm/notebooklm_mcp/`
- Delete: `/home/user/research/audi-skill/notebooklm/tests/`
- Delete: `/home/user/research/audi-skill/notebooklm/scripts/`
- Delete: `/home/user/research/audi-skill/notebooklm/docs/`
- Delete: `/home/user/research/audi-skill/notebooklm/README.md`
- Delete: `/home/user/research/audi-skill/notebooklm/AGENTS.md`
- Delete: `/home/user/research/audi-skill/notebooklm/CLAUDE.md`
- Delete: `/home/user/research/audi-skill/notebooklm/pyproject.toml`
- Delete: `/home/user/research/audi-skill/notebooklm/uv.lock`
- Delete: `/home/user/research/audi-skill/notebooklm/.gitignore`
- Test: tracked file list and grep checks

- [ ] **Step 1: Verify `audi-skill` is clean before deleting tracked files**

Run: `git -C /home/user/research/audi-skill status --short --branch`

Expected:

```text
## master...origin/master
```

- [ ] **Step 2: Install the new MCP tool on this machine before removing the old skill-side code**

Run: `git ls-remote https://github.com/audichuang/notebooklm-mcp.git refs/tags/v0.1.0`

Expected: one line matching:

```text
^[0-9a-f]{40}[[:space:]]+refs/tags/v0.1.0$
```

Run: `uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@v0.1.0"`

Expected output contains:

```text
Installed
```

Run: `notebooklm-mcp --help`

Expected output contains:

```text
NotebookLM MCP server
```

Run: `notebooklm-cover --help`

Expected output contains:

```text
生成 Apple-Podcast 合規節目封面
```

- [ ] **Step 3: Keep local `.mcp.json` ignored after removing the nested `.gitignore`**

Set `/home/user/research/audi-skill/.gitignore` to exactly:

```gitignore
# eval / 測試副產物，不進 skill repo
*-workspace/

.DS_Store
.venv/
__pycache__/
*.egg-info/
.pytest_cache/

# NotebookLM skill keeps a tracked example; each machine's live MCP config stays local.
notebooklm/.mcp.json
```

- [ ] **Step 4: Create the tracked zero-path MCP example**

Set `/home/user/research/audi-skill/notebooklm/.mcp.example.json` to exactly:

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
        "notebooklm-mcp",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

- [ ] **Step 5: Update cover commands in `SKILL.md`**

In `/home/user/research/audi-skill/notebooklm/SKILL.md`, replace:

```markdown
   - **節目封面**(掛 channel):`uv run python scripts/make_cover.py --output cover.jpg
     --line 標題行1 --line 標題行2 --subtitle ... --byline ...`(自帶 Apple 驗證器)。
   - **單集封面**(每集各自封面,選填):`make_cover.py --manifest series_manifest.json
     --show-name "節目名" --tag "~/.claude/" --byline ... --output-dir covers/` — 逐集生
```

with:

```markdown
   - **節目封面**(掛 channel):`notebooklm-cover --output cover.jpg
     --line 標題行1 --line 標題行2 --subtitle "副標" --byline audichuang`(自帶 Apple 驗證器)。
   - **單集封面**(每集各自封面,選填):`notebooklm-cover --manifest series_manifest.json
     --show-name "節目名" --tag "~/.claude/" --byline audichuang --output-dir covers/` — 逐集生
```

Then replace:

```markdown
- [ ] `cover_path` — 單集封面(每集各自;`make_cover.py --manifest` 批次生)
```

with:

```markdown
- [ ] `cover_path` — 單集封面(每集各自;`notebooklm-cover --manifest` 批次生)
```

Then replace the "## Auth" section with:

````markdown
## Auth

MCP server 由 Claude Code 註冊命令用 `doppler run -p notebooklm -c dev -- notebooklm-mcp --transport stdio` 包住，`notebooklm-py` 讀取 `NOTEBOOKLM_AUTH_JSON`。每台機器先安裝 pinned MCP tool:

```bash
uv tool install --python 3.12 "git+https://github.com/audichuang/notebooklm-mcp.git@v0.1.0"
```

專案設定可從 `.mcp.example.json` 複製成未追蹤的 `.mcp.json`。認證過期或 3 VM 同步問題見 [troubleshooting.md](references/troubleshooting.md)。
````

Then replace the "## References" list with:

```markdown
## References

- [MCP 工具參考](references/tool-reference.md)
- [疑難排解](references/troubleshooting.md)
- [連續 podcast prompts](references/episodic_prompts.md)
- [episodes 範例](references/series_example.md)
```

- [ ] **Step 6: Replace `references/troubleshooting.md` with skill-side troubleshooting that points to installed commands**

Set `/home/user/research/audi-skill/notebooklm/references/troubleshooting.md` to exactly:

````markdown
# NotebookLM MCP Troubleshooting

## MCP Server

### Server does not appear in Claude Code

Check registration:

```bash
claude mcp list
```

Register again with the installed console script:

```bash
claude mcp add notebooklm -- doppler run -p notebooklm -c dev -- \
  notebooklm-mcp --transport stdio
```

Expected: `claude mcp list` shows a `notebooklm` server whose command contains `notebooklm-mcp --transport stdio`.

### Server starts then exits

Run the live smoke test in a shell with Doppler access:

```bash
timeout 8 doppler run -p notebooklm -c dev -- \
  notebooklm-mcp --transport stdio < /dev/null
```

Expected: no traceback and no auth error. Stdio may exit when stdin closes; that is not itself a failure.

### Install or package errors

Reinstall the pinned MCP tool:

```bash
uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@v0.1.0"
notebooklm-mcp --help
notebooklm-cover --help
```

Expected: the first help output contains `NotebookLM MCP server`; the second contains `生成 Apple-Podcast 合規節目封面`.

## Doppler Auth

### `NOTEBOOKLM_AUTH_JSON` is empty

Refresh locally on a GUI machine, then sync from the MCP repo checkout:

```bash
cd /home/user/research/audiskill/notebooklm-mcp
notebooklm login
bash scripts/sync-auth.sh
```

Then verify:

```bash
doppler secrets get NOTEBOOKLM_AUTH_JSON -p notebooklm -c dev --plain | head -c 50
```

Expected: JSON beginning with `{"cookies":`.

### Invalid JSON or missing cookies

Rebuild the storage state on the GUI machine and sync again from the MCP repo checkout:

```bash
cd /home/user/research/audiskill/notebooklm-mcp
notebooklm login
bash scripts/sync-auth.sh
```

### 3 VM sync

Doppler is the source of truth. Do not log in independently on each VM.

1. Run `notebooklm login` on a GUI machine.
2. In `/home/user/research/audiskill/notebooklm-mcp`, run `bash scripts/sync-auth.sh`.
3. Restart each VM's MCP server so it receives the refreshed secret.

## Generation

### Audio generation takes a long time

Use `artifact_wait` with a large timeout. Long audio commonly needs more than 600 seconds:

```json
{"notebook_id": "nb-123", "task_id": "task-123", "timeout": 1200}
```

Timeout does not prove the server-side job failed. Re-run `artifact_wait` with the same `task_id`, or use `artifact_list` to see if it landed.

### Generation was interrupted or lost the task id

If a `generate_audio` or `podcast_*` call was cut off and you do not know whether the audio was produced, do not regenerate blindly. List notebook artifacts:

```json
{"notebook_id": "nb-123", "kind": "audio"}
```

`artifact_list` returns each artifact's `artifact_id` and `completed`. Find the episode, then call `artifact_download_audio` with that `artifact_id`.

### Download gets the wrong artifact

Always pass the `artifact_id` returned by `generate_audio` or `artifact_wait`:

```json
{
  "notebook_id": "nb-123",
  "output_path": "/tmp/notebooklm/ep01.mp3",
  "artifact_id": "art-123"
}
```

### Bad language code

Use `zh_Hant`, not `zh-TW`. The MCP server validates language codes before calling the SDK.

## Episodic Podcast

### Episode N ignores episode N-1

Check that episode N was generated through `podcast_series` or that `podcast_episode` received `prior_mp3_path`.

For manual continuation, verify the prior file exists:

```bash
ls -l /tmp/notebooklm/series/ep01.mp3
```

### Regenerating a bad episode

First find the bad episode's uploaded source id with `source_list` by matching the `EP{n:02d} {title}` source, then delete it before regenerating:

```json
{"notebook_id": "nb-123", "source_id": "src-123"}
```

Then rerun `podcast_episode` for that episode and continue the series from the next episode.

### Resume (`start=N`) and continuity

`podcast_series(..., start=N)` does not read a local `ep{N-1}.mp3`. Continuity on resume relies on the same NotebookLM notebook already containing prior episodes as sources named `EP{N-1:02d} {title}`.

If the notebook does not have the prior `EPxx ...` source because a crash happened before self-upload, regenerate that single episode with `podcast_episode(..., title="心法篇", prior_mp3_path="/tmp/notebooklm/series/ep01.mp3")` to re-seed it, then resume the series.

## Sources

### mp3 upload fails

Pass the MIME type explicitly:

```json
{
  "notebook_id": "nb-123",
  "file_path": "/tmp/notebooklm/ep01.mp3",
  "mime_type": "audio/mpeg",
  "wait": true
}
```

If the file is remote, download it locally first; `source_add_file` takes a local path.

### Source added but seems empty

Verify NotebookLM extracted the body with `source_fulltext(notebook_id, source_id)`, using `source_list` to get the id. Check `char_count` is non-trivial. NotebookLM inserts spaces between CJK chars, so compare keywords after `"".join(content.split())`. If empty, re-add the source as pasted text with `source_add_text`.
````

- [ ] **Step 7: Update `references/tool-reference.md` cover text**

In `/home/user/research/audi-skill/notebooklm/references/tool-reference.md`, replace:

```markdown
`scripts/make_cover.py --manifest` 批次生(見 AGENTS.md 封面段)。缺檔/不合規 **fail-fast**。
```

with:

```markdown
`notebooklm-cover --manifest` 批次生。缺檔/不合規 **fail-fast**。
```

- [ ] **Step 8: Remove tracked MCP code, tests, scripts, and old docs from the skill copy**

Run: `git -C /home/user/research/audi-skill rm -r notebooklm/notebooklm_mcp notebooklm/tests notebooklm/scripts notebooklm/docs notebooklm/README.md notebooklm/AGENTS.md notebooklm/CLAUDE.md notebooklm/pyproject.toml notebooklm/uv.lock notebooklm/.gitignore`

Expected output contains these lines:

```text
rm 'notebooklm/notebooklm_mcp/app.py'
rm 'notebooklm/tests/test_make_cover.py'
rm 'notebooklm/scripts/make_cover.py'
rm 'notebooklm/scripts/sync-auth.sh'
rm 'notebooklm/pyproject.toml'
rm 'notebooklm/uv.lock'
```

- [ ] **Step 9: Remove ignored generated leftovers inside the skill copy**

Run: `rm -rf /home/user/research/audi-skill/notebooklm/.venv /home/user/research/audi-skill/notebooklm/notebooklm_mcp/__pycache__ /home/user/research/audi-skill/notebooklm/notebooklm_mcp/publish/__pycache__ /home/user/research/audi-skill/notebooklm/tests/__pycache__`

Expected: no output.

- [ ] **Step 10: Add the docs-only skill files**

Run: `git -C /home/user/research/audi-skill add .gitignore notebooklm/SKILL.md notebooklm/references/tool-reference.md notebooklm/references/troubleshooting.md notebooklm/.mcp.example.json`

Expected: no output.

- [ ] **Step 11: Verify tracked skill content is docs-only**

Run: `git -C /home/user/research/audi-skill ls-files notebooklm | sort`

Expected:

```text
notebooklm/.mcp.example.json
notebooklm/SKILL.md
notebooklm/references/episodic_prompts.md
notebooklm/references/series_example.md
notebooklm/references/tool-reference.md
notebooklm/references/troubleshooting.md
```

Run: `find /home/user/research/audi-skill/notebooklm -type f ! -name .mcp.json | sort`

Expected:

```text
/home/user/research/audi-skill/notebooklm/.mcp.example.json
/home/user/research/audi-skill/notebooklm/SKILL.md
/home/user/research/audi-skill/notebooklm/references/episodic_prompts.md
/home/user/research/audi-skill/notebooklm/references/series_example.md
/home/user/research/audi-skill/notebooklm/references/tool-reference.md
/home/user/research/audi-skill/notebooklm/references/troubleshooting.md
```

Run: `rg -n "notebooklm_mcp|pytest|scripts/make_cover|uv run --directory|notebooklm-skill" /home/user/research/audi-skill/notebooklm/SKILL.md /home/user/research/audi-skill/notebooklm/references`

Expected: exit code 1 and no output.

- [ ] **Step 12: Commit and push docs-only skill**

Run: `git -C /home/user/research/audi-skill status --short`

Expected output includes:

```text
M  .gitignore
A  notebooklm/.mcp.example.json
M  notebooklm/SKILL.md
M  notebooklm/references/tool-reference.md
M  notebooklm/references/troubleshooting.md
```

Run: `git -C /home/user/research/audi-skill commit -m "chore(notebooklm): split MCP implementation into notebooklm-mcp package"`

Expected output contains:

```text
chore(notebooklm): split MCP implementation into notebooklm-mcp package
```

Run: `git -C /home/user/research/audi-skill push origin master`

Expected output contains:

```text
master -> master
```

Rollback after push if consumers cannot install the tagged MCP package: `git -C /home/user/research/audi-skill revert HEAD && git -C /home/user/research/audi-skill push origin master`

### Task 7: Add cross-repo sync checklist to MCP `AGENTS.md`

**Files:**
- Modify: `/home/user/research/audiskill/notebooklm-mcp/AGENTS.md`
- Test: grep checklist text

- [ ] **Step 1: Add the cross-repo sync section**

Append this section to `/home/user/research/audiskill/notebooklm-mcp/AGENTS.md`:

````markdown

## Cross-Repo Sync Checklist

MCP repo 與 skill repo 是一組配置。改動 MCP tools 時,同步更新 `/home/user/research/audi-skill/notebooklm/SKILL.md` 的工具表與 `/home/user/research/audi-skill/notebooklm/references/tool-reference.md`。

新增、移除或改名工具時,commit message 要明講 skill repo 是否已同步;若尚未同步,不要 push MCP release tag。若本 repo 已落地 CI hard check,PR 必須等該檢查綠燈後才能 tag release。
````

- [ ] **Step 2: Verify the checklist is present**

Run: `rg -n "Cross-Repo Sync Checklist|audi-skill/notebooklm|不要 push MCP release tag" /home/user/research/audiskill/notebooklm-mcp/AGENTS.md`

Expected:

```text
AGENTS.md:...:## Cross-Repo Sync Checklist
AGENTS.md:...:MCP repo 與 skill repo 是一組配置。改動 MCP tools 時,同步更新 `/home/user/research/audi-skill/notebooklm/SKILL.md` 的工具表與 `/home/user/research/audi-skill/notebooklm/references/tool-reference.md`。
AGENTS.md:...:新增、移除或改名工具時,commit message 要明講 skill repo 是否已同步;若尚未同步,不要 push MCP release tag。若本 repo 已落地 CI hard check,PR 必須等該檢查綠燈後才能 tag release。
```

- [ ] **Step 3: Commit the checklist**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp add AGENTS.md`

Expected: no output.

Run: `git -C /home/user/research/audiskill/notebooklm-mcp commit -m "docs: record NotebookLM skill sync checklist"`

Expected output contains:

```text
docs: record NotebookLM skill sync checklist
```

Run: `git -C /home/user/research/audiskill/notebooklm-mcp push origin master`

Expected output contains:

```text
master -> master
```

Rollback after push: `git -C /home/user/research/audiskill/notebooklm-mcp revert HEAD && git -C /home/user/research/audiskill/notebooklm-mcp push origin master`

### Task 8: Switch consumers one by one to zero-path `notebooklm-mcp`

**Files:**
- Modify: `/home/user/research/audiskill/podcast-lab/.mcp.json`
- Modify: `/home/user/research/audiskill/podcast-lab/.codex/config.toml`
- Modify per VM: each machine's Claude Code MCP registration for `notebooklm`
- Test: `git ls-remote`, `uv tool install`, `notebooklm-mcp --help`, `/mcp` tool list, config grep

- [ ] **Step 1: On the current machine, prove private repo access through SSH**

Run: `git ls-remote https://github.com/audichuang/notebooklm-mcp.git refs/tags/v0.1.0`

Expected: one line matching:

```text
^[0-9a-f]{40}[[:space:]]+refs/tags/v0.1.0$
```

If this command fails on a VM that uses HTTPS credentials instead of SSH, run: `git ls-remote https://github.com/audichuang/notebooklm-mcp.git refs/tags/v0.1.0`

Expected for the HTTPS fallback is the same one-line tag output. If both SSH and HTTPS fail, stop on that VM and fix GitHub authentication before editing configs.

- [ ] **Step 2: Install the pinned MCP tool before touching configs**

Run: `uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@v0.1.0"`

Expected output contains:

```text
Installed
```

For HTTPS-only machines, run this exact install command instead: `uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@v0.1.0"`

- [ ] **Step 3: Verify installed commands**

Run: `notebooklm-mcp --help`

Expected output contains:

```text
NotebookLM MCP server
```

Run: `notebooklm-cover --help`

Expected output contains:

```text
生成 Apple-Podcast 合規節目封面
```

- [ ] **Step 4: Smoke the installed MCP server under Doppler**

Run: `timeout 8 doppler run -p notebooklm -c dev -- notebooklm-mcp --transport stdio < /dev/null`

Expected: exit code 0 or 124, no `Traceback`, no `NOTEBOOKLM_AUTH_JSON`, no auth error in stderr.

- [ ] **Step 5: Update Claude Code MCP registration on this machine**

Run: `claude mcp remove notebooklm` (忽略「not found」,只是確保沒有殘留的舊註冊會撞名)

Run: `claude mcp add notebooklm -- doppler run -p notebooklm -c dev -- notebooklm-mcp --transport stdio`

Expected output contains:

```text
notebooklm
```

Run: `claude mcp list`

Expected output contains:

```text
notebooklm
```

and the command shown for `notebooklm` contains:

```text
notebooklm-mcp --transport stdio
```

Rollback for this registration: `claude mcp add notebooklm -- doppler run -p notebooklm -c dev -- uv run --directory /home/user/research/audi-skill/notebooklm python -m notebooklm_mcp.server --transport stdio`

- [ ] **Step 6: Update `podcast-lab/.mcp.json` without changing the `medium` server**

Set `/home/user/research/audiskill/podcast-lab/.mcp.json` to exactly:

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
        "notebooklm-mcp",
        "--transport",
        "stdio"
      ]
    },
    "medium": {
      "command": "doppler",
      "args": [
        "run",
        "-p",
        "medium",
        "-c",
        "dev",
        "--",
        "/home/user/research/audiskill/mediumscrapper/.venv/bin/python",
        "/home/user/research/audiskill/mediumscrapper/server.py"
      ]
    }
  }
}
```

Rollback content for `/home/user/research/audiskill/podcast-lab/.mcp.json` is the pre-migration file:

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
        "uv",
        "run",
        "--directory",
        "/home/user/research/audi-skill/notebooklm",
        "python",
        "-m",
        "notebooklm_mcp.server",
        "--transport",
        "stdio"
      ]
    },
    "medium": {
      "command": "doppler",
      "args": [
        "run",
        "-p",
        "medium",
        "-c",
        "dev",
        "--",
        "/home/user/research/audiskill/mediumscrapper/.venv/bin/python",
        "/home/user/research/audiskill/mediumscrapper/server.py"
      ]
    }
  }
}
```

- [ ] **Step 7: Update `podcast-lab/.codex/config.toml` without changing `medium`**

Set `/home/user/research/audiskill/podcast-lab/.codex/config.toml` to exactly:

```toml
[mcp_servers.medium]
args = [
    "run",
    "-p",
    "medium",
    "-c",
    "dev",
    "--",
    "/home/user/research/audiskill/mediumscrapper/.venv/bin/python",
    "/home/user/research/audiskill/mediumscrapper/server.py",
]
command = "doppler"

[mcp_servers.notebooklm]
args = [
    "run",
    "-p",
    "notebooklm",
    "-c",
    "dev",
    "--",
    "notebooklm-mcp",
    "--transport",
    "stdio",
]
command = "doppler"
```

Rollback content for `/home/user/research/audiskill/podcast-lab/.codex/config.toml` is the pre-migration file:

```toml
[mcp_servers.medium]
args = [
    "run",
    "-p",
    "medium",
    "-c",
    "dev",
    "--",
    "/home/user/research/audiskill/mediumscrapper/.venv/bin/python",
    "/home/user/research/audiskill/mediumscrapper/server.py",
]
command = "doppler"

[mcp_servers.notebooklm]
args = [
    "run",
    "-p",
    "notebooklm",
    "-c",
    "dev",
    "--",
    "uv",
    "run",
    "--directory",
    "/home/user/research/audi-skill/notebooklm",
    "python",
    "-m",
    "notebooklm_mcp.server",
    "--transport",
    "stdio",
]
command = "doppler"
```

- [ ] **Step 8: Verify podcast-lab configs contain no old NotebookLM source path**

Run: `rg -n "uv run|--directory|notebooklm_mcp.server|audi-skill/notebooklm" /home/user/research/audiskill/podcast-lab/.mcp.json /home/user/research/audiskill/podcast-lab/.codex/config.toml`

Expected: exit code 1 and no output.

Run: `rg -n "notebooklm-mcp" /home/user/research/audiskill/podcast-lab/.mcp.json /home/user/research/audiskill/podcast-lab/.codex/config.toml`

Expected:

```text
/home/user/research/audiskill/podcast-lab/.mcp.json:13:        "notebooklm-mcp",
/home/user/research/audiskill/podcast-lab/.codex/config.toml:21:    "notebooklm-mcp",
```

- [ ] **Step 9: Reconnect and verify tools before moving to the next VM**

In Claude Code on the current machine, run `/mcp`.

Expected: the `notebooklm` server is connected and exposes these tools:

```text
notebook_create
notebook_list
source_add_url
source_add_text
source_add_file
source_delete
source_list
source_fulltext
notebook_get
generate_audio
artifact_list
artifact_wait
artifact_download_audio
artifact_rename
chat_ask
podcast_episode
podcast_series
generate_slides
generate_report
publish_series
feed_info
```

In Codex from `/home/user/research/audiskill/podcast-lab`, start a new session and verify its MCP server list includes `notebooklm`. Only after both Claude Code and Codex see the server, move to the next VM.

- [ ] **Step 10: Pull docs-only skill on this consumer only after Step 9 passes**

Run: `git -C /home/user/research/audi-skill pull --ff-only origin master`

Expected output contains:

```text
Fast-forward
```

Run: `git -C /home/user/research/audi-skill ls-files notebooklm | sort`

Expected:

```text
notebooklm/.mcp.example.json
notebooklm/SKILL.md
notebooklm/references/episodic_prompts.md
notebooklm/references/series_example.md
notebooklm/references/tool-reference.md
notebooklm/references/troubleshooting.md
```

Rollback if this VM cannot reconnect after pulling docs-only skill: restore the rollback contents from Steps 6 and 7, then run `claude mcp add notebooklm -- doppler run -p notebooklm -c dev -- uv run --directory /home/user/research/audi-skill/notebooklm python -m notebooklm_mcp.server --transport stdio`; do not move to the next VM until the current VM is healthy.

- [ ] **Step 11: Repeat Steps 1-10 on each of the other two VMs**

Expected completion condition for each VM:

```text
git ls-remote sees refs/tags/v0.1.0
uv tool install uses Python 3.12
notebooklm-mcp --help works
Doppler stdio smoke has no traceback
Claude Code /mcp shows notebooklm tools
Codex config, if present on that VM, uses notebooklm-mcp without uv run --directory
```

Do not update the next VM's skill checkout until the current VM has passed every line above.

### Task 9 Optional: Add CI and hard skill-sync check to MCP repo

**Files:**
- Create: `/home/user/research/audiskill/notebooklm-mcp/.github/workflows/ci.yml`
- Create: `/home/user/research/audiskill/notebooklm-mcp/scripts/check_skill_sync.py`
- Test: `python scripts/check_skill_sync.py`, GitHub Actions

**Interfaces:**
- Script: `python scripts/check_skill_sync.py`
- Exit code 0 when every MCP tool name appears in both `audi-skill/notebooklm/SKILL.md` and `audi-skill/notebooklm/references/tool-reference.md`
- Exit code 1 with missing names printed when skill docs drift

- [ ] **Step 1: Create `.github/workflows/ci.yml`**

Set `/home/user/research/audiskill/notebooklm-mcp/.github/workflows/ci.yml` to exactly:

```yaml
name: ci

on:
  push:
    branches: ["master"]
  pull_request:
    branches: ["master"]

jobs:
  test-build-smoke:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout notebooklm-mcp
        uses: actions/checkout@v4

      - name: Checkout audi-skill for sync check
        uses: actions/checkout@v4
        with:
          repository: audichuang/audi-skill
          path: audi-skill

      - name: Install uv
        uses: astral-sh/setup-uv@v6

      - name: Install Python 3.12
        run: uv python install 3.12

      - name: Install project
        run: uv sync --dev

      - name: Run pytest
        run: uv run pytest -q

      - name: Build wheel
        run: uv build

      - name: uv tool install wheel smoke
        run: |
          uv tool install --python 3.12 --force dist/notebooklm_mcp-0.1.0-py3-none-any.whl
          notebooklm-mcp --help
          notebooklm-cover --help

      - name: Skill docs sync check
        run: uv run python scripts/check_skill_sync.py
```

- [ ] **Step 2: Create `scripts/check_skill_sync.py`**

Set `/home/user/research/audiskill/notebooklm-mcp/scripts/check_skill_sync.py` to exactly:

```python
"""Fail when MCP tool names drift from the notebooklm skill docs."""
from __future__ import annotations

import asyncio
from pathlib import Path

from notebooklm_mcp import app


ROOT = Path(__file__).resolve().parents[1]
CI_SKILL_DIR = ROOT / "audi-skill" / "notebooklm"
LOCAL_SKILL_DIR = Path("/home/user/research/audi-skill/notebooklm")
SKILL_DIR = CI_SKILL_DIR if CI_SKILL_DIR.exists() else LOCAL_SKILL_DIR
SKILL_MD = SKILL_DIR / "SKILL.md"
TOOL_REFERENCE = SKILL_DIR / "references" / "tool-reference.md"


async def _tool_names() -> list[str]:
    tools = await app.mcp.list_tools()
    return sorted(tool.name for tool in tools)


def _missing(tool_names: list[str], path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [name for name in tool_names if f"`{name}`" not in text]


async def main() -> int:
    missing_files = [path for path in (SKILL_MD, TOOL_REFERENCE) if not path.exists()]
    if missing_files:
        for path in missing_files:
            print(f"missing skill file: {path}")
        return 1

    names = await _tool_names()
    failures: list[str] = []
    for path in (SKILL_MD, TOOL_REFERENCE):
        missing = _missing(names, path)
        if missing:
            failures.append(f"{path}: missing {', '.join(missing)}")

    if failures:
        print("\n".join(failures))
        return 1

    print(f"skill docs cover {len(names)} MCP tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 3: Run the sync check locally from the MCP repo**

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp run python scripts/check_skill_sync.py`

Expected output:

```text
skill docs cover 21 MCP tools
```

- [ ] **Step 4: Run tests and build smoke locally**

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp run pytest -q`

Expected output ends with:

```text
passed
```

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp build`

Expected output contains:

```text
Successfully built dist/notebooklm_mcp-0.1.0-py3-none-any.whl
```

Run: `uv tool install --python 3.12 --force /home/user/research/audiskill/notebooklm-mcp/dist/notebooklm_mcp-0.1.0-py3-none-any.whl`

Expected output contains:

```text
Installed
```

Run: `notebooklm-mcp --help`

Expected output contains:

```text
NotebookLM MCP server
```

Run: `notebooklm-cover --help`

Expected output contains:

```text
生成 Apple-Podcast 合規節目封面
```

- [ ] **Step 5: Commit and push optional CI**

Run: `git -C /home/user/research/audiskill/notebooklm-mcp add .github/workflows/ci.yml scripts/check_skill_sync.py`

Expected: no output.

Run: `git -C /home/user/research/audiskill/notebooklm-mcp commit -m "ci: test NotebookLM MCP package and skill docs sync"`

Expected output contains:

```text
ci: test NotebookLM MCP package and skill docs sync
```

Run: `git -C /home/user/research/audiskill/notebooklm-mcp push origin master`

Expected output contains:

```text
master -> master
```

Rollback after push: `git -C /home/user/research/audiskill/notebooklm-mcp revert HEAD && git -C /home/user/research/audiskill/notebooklm-mcp push origin master`

### Task 10: Final acceptance and spec coverage audit

**Files:**
- Test only: `/home/user/research/audiskill/notebooklm-mcp`
- Test only: `/home/user/research/audi-skill/notebooklm`
- Test only: `/home/user/research/audiskill/podcast-lab/.mcp.json`
- Test only: `/home/user/research/audiskill/podcast-lab/.codex/config.toml`

- [ ] **Step 1: MCP repo acceptance**

Run: `uv --directory /home/user/research/audiskill/notebooklm-mcp run pytest -q`

Expected output ends with:

```text
138 passed
```

Run: `git ls-remote https://github.com/audichuang/notebooklm-mcp.git refs/tags/v0.1.0`

Expected: one line matching:

```text
^[0-9a-f]{40}[[:space:]]+refs/tags/v0.1.0$
```

Run: `uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@v0.1.0"`

Expected output contains:

```text
Installed
```

Run: `timeout 8 doppler run -p notebooklm -c dev -- notebooklm-mcp --transport stdio < /dev/null`

Expected: exit code 0 or 124, no traceback, no auth error.

- [ ] **Step 2: `notebooklm-cover` acceptance**

Run: `mkdir -p /tmp/notebooklm-cover-smoke/covers`

Expected: no output.

Run: `printf '%s\n' '{"title":"測試節目","episodes":[{"episode":1,"title":"心法篇"}]}' > /tmp/notebooklm-cover-smoke/series_manifest.json`

Expected: no output.

Run: `notebooklm-cover --manifest /tmp/notebooklm-cover-smoke/series_manifest.json --show-name "測試節目" --tag "~/.claude/" --byline audichuang --output-dir /tmp/notebooklm-cover-smoke/covers`

Expected output contains both lines:

```text
OK /tmp/notebooklm-cover-smoke/covers/EP01.jpg ->
Manifest updated: /tmp/notebooklm-cover-smoke/series_manifest.json
```

Run: `test -s /tmp/notebooklm-cover-smoke/covers/EP01.jpg`

Expected: exit code 0 and no output.

Run: `rg -n '"cover_path": "/tmp/notebooklm-cover-smoke/covers/EP01.jpg"' /tmp/notebooklm-cover-smoke/series_manifest.json`

Expected:

```text
5:      "cover_path": "/tmp/notebooklm-cover-smoke/covers/EP01.jpg"
```

- [ ] **Step 3: Skill repo acceptance**

Run: `git -C /home/user/research/audi-skill ls-files notebooklm | sort`

Expected:

```text
notebooklm/.mcp.example.json
notebooklm/SKILL.md
notebooklm/references/episodic_prompts.md
notebooklm/references/series_example.md
notebooklm/references/tool-reference.md
notebooklm/references/troubleshooting.md
```

Run: `find /home/user/research/audi-skill/notebooklm -type f ! -name .mcp.json | sort`

Expected:

```text
/home/user/research/audi-skill/notebooklm/.mcp.example.json
/home/user/research/audi-skill/notebooklm/SKILL.md
/home/user/research/audi-skill/notebooklm/references/episodic_prompts.md
/home/user/research/audi-skill/notebooklm/references/series_example.md
/home/user/research/audi-skill/notebooklm/references/tool-reference.md
/home/user/research/audi-skill/notebooklm/references/troubleshooting.md
```

Run: `find /home/user/research/audi-skill/notebooklm -name '*.py' -print`

Expected: no output.

Run: `rg -n "notebooklm_mcp|pytest|scripts/make_cover|uv run --directory|notebooklm-skill" /home/user/research/audi-skill/notebooklm/SKILL.md /home/user/research/audi-skill/notebooklm/references`

Expected: exit code 1 and no output.

- [ ] **Step 4: podcast-lab consumer acceptance**

Run: `rg -n "uv run|--directory|notebooklm_mcp.server|audi-skill/notebooklm" /home/user/research/audiskill/podcast-lab/.mcp.json /home/user/research/audiskill/podcast-lab/.codex/config.toml`

Expected: exit code 1 and no output.

Run: `rg -n "notebooklm-mcp" /home/user/research/audiskill/podcast-lab/.mcp.json /home/user/research/audiskill/podcast-lab/.codex/config.toml`

Expected:

```text
/home/user/research/audiskill/podcast-lab/.mcp.json:13:        "notebooklm-mcp",
/home/user/research/audiskill/podcast-lab/.codex/config.toml:21:    "notebooklm-mcp",
```

In Claude Code, run `/mcp`.

Expected: `notebooklm` connected, with all tools listed in Task 8 Step 9.

In Codex from `/home/user/research/audiskill/podcast-lab`, start a fresh session.

Expected: the NotebookLM MCP server loads without referencing `/home/user/research/audi-skill/notebooklm`.

- [ ] **Step 5: Three-VM rollout acceptance**

For each of the three VMs, record the following exact result block in the migration notes:

```text
VM NotebookLM MCP rollout:
- git ls-remote refs/tags/v0.1.0: pass
- uv tool install Python 3.12 pinned tag: pass
- notebooklm-mcp Doppler stdio smoke: pass
- Claude Code /mcp tools visible: pass
- Codex config, if present, uses notebooklm-mcp: pass
- skill checkout pulled after MCP verification: pass
```

Do not mark this step complete until all three VM result blocks are present.

- [ ] **Step 6: Spec coverage self-check**

Use this checklist to confirm every section of `/home/user/research/audiskill/notebooklm-mcp/docs/superpowers/specs/2026-07-05-notebooklm-repo-split-design.md` is implemented:

```text
背景與問題: Task 2 removes source-path coupling from MCP repo, Task 6 removes duplicate skill-side code, Task 8 removes consumer absolute paths.
目標: Task 3 packages console scripts, Task 4 publishes a single MCP repo, Task 6 leaves one docs-only skill, Task 8 uses zero-path configs.
§1 定位切分: Task 2 makes MCP repo code-only; Task 6 makes skill repo SKILL.md + references + .mcp.example.json only.
§2 發布 / 安裝: Task 3 adds notebooklm-cover; Task 4 creates/connects GitHub remote; Task 5 creates v0.1.0; Task 8 installs with uv tool.
§3 遷移: Task 1 merges feature branch; Task 2 renames checkout and removes skill layer; Task 3 packages cover; Task 4 switches remote; Task 6 slims skill; Task 8 switches consumers.
§4 跨 repo 同步耦合: Task 7 writes AGENTS.md checklist; Task 9 optionally enforces it in CI.
驗收: Task 10 Steps 1-5 cover MCP tests/install/smoke, skill docs-only state, consumer zero-path config, and 3 VM rollout.
版本策略: Task 5 creates v0.1.0; Task 8 and Task 10 install pinned v0.1.0 instead of master.
安全 / 回滾: Tasks 1,2,4,5,6,7,8,9 include verification and rollback lines for merge, rename, remote, tag, pushed skill changes, config changes, and CI.
非目標: Global Constraints keep PyPI, auth redesign, and skill deployment mechanism out of scope.
```

If any line above is not true in the final repo state, reopen the corresponding task and fix it before declaring migration complete.

- [ ] **Step 7: Placeholder scan for this plan**

Run:

```bash
python - <<'PY'
from pathlib import Path

path = Path("/home/user/research/audiskill/notebooklm-mcp/docs/superpowers/plans/2026-07-05-notebooklm-repo-split.md")
needles = [
    "TB" + "D",
    "TO" + "DO",
    "implement " + "later",
    "fill in " + "details",
    "適當" + "處理",
]
text = path.read_text(encoding="utf-8")
hits = [needle for needle in needles if needle in text]
if hits:
    raise SystemExit(f"placeholder markers found: {hits}")
print("no placeholder markers found")
PY
```

Expected:

```text
no placeholder markers found
```
