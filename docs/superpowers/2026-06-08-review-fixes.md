# 2026-06-08 NotebookLM MCP 第二意見審查與修復計畫

## 1. 摘要

本次獨立打開專案檔案、測試、文件，以及實際安裝的 `notebooklm-py==0.3.4` SDK 原始碼核對。現有基線在本機為：

```bash
uv run pytest -q
# 27 passed in 0.40s
```

清單文字宣稱 A-F 共 24 條，但實際列出的編號是 A-F 23 條，加上 R1-R5 5 條，合計 28 條；下表已逐一覆蓋所有實際列出的編號。

最終帳：

- `CONFIRMED`: 20 條
- `ADJUSTED`: 5 條
- `REFUTED`: 3 條
- 額外發現：1 條真實問題，屬 P1 錯誤路徑 bug

最高優先順序不是重寫架構，而是修好 resume 邊界與 manifest、補齊 SDK 契約 tripwire、把文件改回「每集自上傳同名來源」的新設計。A4 的決議是：**改文件對齊現況，不實作 disk-feed**。

## 2. 逐條判定表

| 編號 | Verdict | 證據 | 分歧 / 調整 |
|---|---|---|---|
| A1 | CONFIRMED, medium | `notebooklm_mcp/tools_podcast.py:124-126` 每次設定同一個 `series_manifest.json` 且 `results=[]`；`notebooklm_mcp/tools_podcast.py:145-146` 每集用 `"w"` 覆寫；重現顯示 start=3 後 manifest 只剩 ep3。 | 無。這不是目前測試保證的 per-run manifest；檔名與 resume 語意都指向 season manifest。 |
| A2 | CONFIRMED, medium | `notebooklm_mcp/tools_podcast.py:131-132` 直接 `range(start, ...)` 與 `episodes[episode_n - 1]`；重現 `start=0` 產生 episodes `[0,1,2,3]`，第一個 brief 取到最後一集。 | 無。 |
| A3 | CONFIRMED, low | `notebooklm_mcp/tools_podcast.py:131` 當 `start > len(episodes)` 時 range 為空；`notebooklm_mcp/tools_podcast.py:148` 仍回傳 success shape；重現 `start=4` 且三集時沒有 manifest。 | 無。 |
| A4 | ADJUSTED, medium | 文件宣稱 disk-feed：`SKILL.md:51`、`references/troubleshooting.md:142-150`、`references/series_example.md:59-60`、`references/cli-reference.md:270-271`；程式實際傳 `None`：`notebooklm_mcp/tools_podcast.py:133-139`。新設計明文說不需 prior threading：`docs/superpowers/notebooklm-mcp-findings.md:66-72`。 | drift confirmed；修法應改文件，不要加回 disk-feed。 |
| B1 | CONFIRMED, medium | MCP 用 `chat.ask` 與 `AskResult.answer`：`notebooklm_mcp/tools_basic.py:113-116`；SDK 簽章與欄位在 `.venv/.../notebooklm/_chat.py:61-67`、`.venv/.../notebooklm/types.py:1111-1129`；契約測試沒有覆蓋：`tests/test_contracts.py:12-86`。 | 無。 |
| B2 | CONFIRMED, low | MCP 讀 `.id`：`notebooklm_mcp/tools_basic.py:31-39`、`notebooklm_mcp/tools_podcast.py:35-43`、`:73-76`；SDK `Source.id` 在 `.venv/.../notebooklm/types.py:480-499`；contract 未 pin Source 欄位。 | 無。 |
| B3 | CONFIRMED, low | MCP 讀 `nb.id` 與 `getattr(nb, "title", ...)`：`notebooklm_mcp/tools_basic.py:17-25`；SDK Notebook 欄位在 `.venv/.../notebooklm/types.py:357-365`；Notebooks API 在 `.venv/.../notebooklm/_notebooks.py:44-73`。 | `id` 改名會爆；`title` 改名會被 `getattr` 靜默吞掉。 |
| B4 | CONFIRMED, low | MCP 呼叫 `sources.add_url/add_text`：`notebooklm_mcp/tools_basic.py:29-39`；SDK 簽章在 `.venv/.../notebooklm/_sources.py:283-289`、`:340-347`；contract 只 pin `add_file`：`tests/test_contracts.py:46-58`。 | 無。 |
| B5 | CONFIRMED, nit | MCP 呼叫 `sources.delete`：`notebooklm_mcp/tools_basic.py:60-64`；SDK 簽章在 `.venv/.../notebooklm/_sources.py:530-548`；contract 未 pin。 | 無。 |
| B6 | CONFIRMED, nit | Fake `add_file` 把 `wait/wait_timeout` 做成 keyword-only：`tests/conftest.py:54`，真 SDK 是 positional-or-keyword：`.venv/.../notebooklm/_sources.py:391-398`；Fake `wait_for_completion` 少 `initial_interval/max_interval/poll_interval`：`tests/conftest.py:35` vs `.venv/.../notebooklm/_artifacts.py:1763-1771`；contract 只比參數名：`tests/test_contracts.py:8-9`。 | 無。 |
| C1 | CONFIRMED, low | 行為測試只覆蓋部分工具：`tests/test_tools_basic.py:7-42`、`tests/test_tools_podcast.py:4-79`；server 註冊清單覆蓋所有工具但不是行為測試：`tests/test_server_lifespan.py:41-60`。 | 無。 |
| C2 | ADJUSTED, low -> P1 | 錯誤路徑測試缺席：`tests/test_tools_basic.py:7-42`、`tests/test_tools_podcast.py:4-79` 均為 happy path；`runtime.get_client()` 的錯誤只在 lifespan exit 被間接測：`tests/test_server_lifespan.py:31-38`。額外發現 X1 證明其中「生成失敗」不只是測試缺口。 | 測試缺口本身 low；生成失敗處理升為 P1 bug。 |
| D1 | CONFIRMED, medium | `CLAUDE.md:55-56` 要來源命名 `EP{n} 對話紀錄` 又說與工作室一致；實作 artifact/source 都是裸 `EP{n:02d}`：`notebooklm_mcp/tools_podcast.py:62`、`:76`；findings 鐵律是 identical string：`docs/superpowers/notebooklm-mcp-findings.md:66-72`。 | 無。 |
| D2 | CONFIRMED, low | `SKILL.md:25` 說 `podcast_episode` 「回傳前集 mp3」；實作只有 `prior_mp3_path` 有值才上傳前集：`notebooklm_mcp/tools_podcast.py:31-43`。 | 無。 |
| D3 | CONFIRMED, low | `references/episodic_prompts.md:104`、`:118-120` 引用未封裝的 `generate report --format`、`configure --persona`、`ask`；MCP 實際工具名在 `tests/test_server_lifespan.py:44-58`，只有 `chat_ask`，沒有那些 CLI 指令。 | 無。 |
| D4 | CONFIRMED, nit | `SKILL.md:55` 內文連到 `series_example.md`，但 References 清單 `SKILL.md:73-78` 未列。 | 無。 |
| E1 | CONFIRMED, low | `auth_cli.py:16` 未 catch `json.loads`；重現壞 JSON 直接 traceback。`auth_cli.py:17-24` 只檢查 key，`{"cookies":5}` 會先寫檔再在 `len(data["cookies"])` TypeError。 | 無密鑰外洩；仍是 UX 與資料完整性問題。 |
| E2 | CONFIRMED, low | `auth_cli.py:19-23` 先 `os.makedirs` + `open(...,"w")`，寫完才 chmod；SDK path helper 建目錄時會 `0o700` 並 chmod：`.venv/.../notebooklm/paths.py:27-53`。 | 無。 |
| E3 | CONFIRMED, low | 文件教 `--host 0.0.0.0`：`CLAUDE.md:17`、`docs/mcp-setup.md:16-23`；程式預設安全 `127.0.0.1`：`notebooklm_mcp/app.py:46-48`；未見信任網路 / 防火牆警語。 | 無。 |
| E4 | ADJUSTED, nit | `.gitignore:15` 是 repo 內 `.notebooklm/`，不是真的「蓋住 `~/.notebooklm/`」；但 repo 根 `storage_state.json` 沒忽略，`auth_cli.py:12` 允許 `--out` 任意路徑。 | 結論仍是要補 ignore；理由修正。 |
| E5 | CONFIRMED, nit | `.gitignore:16` 有 `captured_rpcs/`；`.gitignore:34-36` 保留 VCR cassettes 與 `tests/vcr_config.py` 註解；本 repo `rg --files` 沒有該測試配置。 | 無。 |
| F1 | CONFIRMED, nit | `podcast_episode` 直接允許 `episode_n=1` 搭配 `prior_mp3_path`；`notebooklm_mcp/tools_podcast.py:43` 會命名 `EP00`。 | 僅直接誤用單集工具會觸發。 |
| F2 | CONFIRMED, nit | `notebooklm_mcp/app.py:30` 直接 await `NotebookLMClient.from_storage()`；SDK 可丟 `FileNotFoundError/ValueError`：`.venv/.../notebooklm/auth.py:464-469`、`:445-459`；`notebooklm_mcp/server.py:10-13` 只是薄 re-export/launcher。 | fail-fast 正確，但訊息可更乾淨。 |
| R1 | ADJUSTED | Contract 確實只 pin dataclass 欄位：`tests/test_contracts.py:77-86`；SDK runtime 失敗可回 `GenerationStatus(task_id="", status="failed")`：`.venv/.../notebooklm/_artifacts.py:1979-1986`、`:2196-2198`；MCP 卻把 `task_id` 當可用 id：`notebooklm_mcp/tools_podcast.py:57-58`、`notebooklm_mcp/tools_basic.py:83-91`。 | 同意「不是 contract 測 runtime invariant」；但該 invariant 在失敗路徑為假，形成 X1。 |
| R2 | ADJUSTED | `tests/test_tools_podcast.py:72-79` 只斷言 start=3 回傳一集與 rename `EP03`，沒有斷言 manifest per-run；實作確實覆寫：`notebooklm_mcp/tools_podcast.py:124-146`。 | 翻案：測試本身不是 bug，但「manifest per-run」沒有被文件支持，A1 仍成立。 |
| R3 | REFUTED | FakeNotebooks/FakeChat 無 `.calls`：`tests/conftest.py:86-96`；現有 `chat_ask` 測試用 echo input 足以斷言：`tests/test_tools_basic.py:40-42`。 | 同意另一審查者：不是缺陷。未來要測 call args 時再加 calls。 |
| R4 | REFUTED | SDK 明文 task_id 即 artifact_id：`.venv/.../notebooklm/types.py:942-952`，解析處也只回 task_id：`.venv/.../notebooklm/_artifacts.py:2170-2194`；MCP 重複回傳是刻意相容欄位：`notebooklm_mcp/tools_basic.py:83-84`；findings 記錄在 `docs/superpowers/notebooklm-mcp-findings.md:38-42`。 | 同意另一審查者：不是 bug。 |
| R5 | REFUTED | `.mcp.json:12-18` 缺 `uv run --directory <repo>`，與正式文件 `docs/mcp-setup.md:7-10` 不一致；檔內沒有 secret。`git status --short` 顯示它目前是 untracked scratch。 | 同意「不應因此 commit」；可另行忽略或修成 canonical 後再討論。 |

## 3. 修復計畫

### P0-1：修正 `podcast_series` resume 邊界與 season manifest

問題：A1/A2/A3。  
根因：`start` 未驗證；manifest 每次以空 `results` 開始，完全不讀舊檔。

最小修法片段：

```diff
diff --git a/notebooklm_mcp/tools_podcast.py b/notebooklm_mcp/tools_podcast.py
@@
 import json
 import os
+from typing import Any
@@
+def _load_manifest_episodes(manifest_path: str, notebook_id: str, start: int) -> list[dict[str, Any]]:
+    if start == 1 or not os.path.exists(manifest_path):
+        return []
+    with open(manifest_path, encoding="utf-8") as f:
+        data = json.load(f)
+    if data.get("notebook_id") != notebook_id:
+        raise ValueError("Manifest notebook_id does not match this notebook_id")
+    return [
+        ep for ep in data.get("episodes", [])
+        if isinstance(ep, dict) and isinstance(ep.get("episode"), int) and ep["episode"] < start
+    ]
+
+
+def _write_manifest(manifest_path: str, notebook_id: str, episodes: list[dict[str, Any]]) -> None:
+    with open(manifest_path, "w", encoding="utf-8") as f:
+        json.dump({"notebook_id": notebook_id, "episodes": episodes}, f, ensure_ascii=False, indent=2)
+
+
 @mcp.tool()
 async def podcast_series(
@@
     """Generate a full podcast series deterministically."""
+    if start < 1:
+        raise ValueError("start must be >= 1")
+    if start > len(episodes):
+        raise ValueError(f"start must be <= len(episodes) ({len(episodes)})")
     os.makedirs(output_dir, exist_ok=True)
     manifest_path = os.path.join(output_dir, "series_manifest.json")
-    results: list[dict] = []
+    run_results: list[dict] = []
+    manifest_results = _load_manifest_episodes(manifest_path, notebook_id, start)
@@
         )
-        results.append(res)
-        with open(manifest_path, "w", encoding="utf-8") as f:
-            json.dump({"notebook_id": notebook_id, "episodes": results}, f, ensure_ascii=False, indent=2)
+        run_results.append(res)
+        manifest_results.append(res)
+        _write_manifest(manifest_path, notebook_id, manifest_results)
 
-    return {"notebook_id": notebook_id, "episodes": results, "manifest": manifest_path}
+    return {"notebook_id": notebook_id, "episodes": run_results, "manifest": manifest_path}
```

測試先紅：

```python
async def test_series_resume_preserves_existing_manifest(fake_client, tmp_path):
    (tmp_path / "series_manifest.json").write_text(
        json.dumps({"notebook_id": "nb-1", "episodes": [{"episode": 1}, {"episode": 2}]}),
        encoding="utf-8",
    )
    eps = [{"brief": "1"}, {"brief": "2"}, {"brief": "3"}]
    out = await p.podcast_series("nb-1", eps, str(tmp_path), start=3)
    assert [e["episode"] for e in out["episodes"]] == [3]
    manifest = json.loads((tmp_path / "series_manifest.json").read_text(encoding="utf-8"))
    assert [e["episode"] for e in manifest["episodes"]] == [1, 2, 3]

async def test_series_rejects_invalid_start(fake_client, tmp_path):
    eps = [{"brief": "1"}]
    with pytest.raises(ValueError, match="start must be >= 1"):
        await p.podcast_series("nb-1", eps, str(tmp_path), start=0)
    with pytest.raises(ValueError, match="start must be <= len"):
        await p.podcast_series("nb-1", eps, str(tmp_path), start=2)
    assert fake_client.artifacts.calls == []
```

驗證：

```bash
uv run pytest tests/test_tools_podcast.py -q
uv run pytest -q
```

### P0-2：A4 決議，改文件，不實作 disk-feed

問題：A4/D1/D2。  
根因：文件仍描述舊設計「前集 mp3 threading」，但 findings 與程式已改成「每集生成後自上傳自己的 mp3，下一集自然看到來源」。

最小文件修法：

```diff
diff --git a/SKILL.md b/SKILL.md
@@
-| `podcast_episode` | 單集 podcast：回傳前集 mp3、生成、等待、下載、重命名 |
-| `podcast_series` | 整季 podcast：純 Python 迴圈，逐集把前集 mp3 餵回下一集 |
+| `podcast_episode` | 單集 podcast：可選上傳前集 mp3、生成、等待、下載、重命名，並自上傳本集 mp3 |
+| `podcast_series` | 整季 podcast：純 Python 迴圈；每集自上傳本集 mp3，下一集自然讀到同名來源 |
@@
-4. 續製時用 `start=N`，並確認 `output_dir/ep{N-1}.mp3` 存在。
+4. 續製時用 `start=N`；前提是同一個 NotebookLM 筆記本來源區已經有 `EP{N-1:02d}` 來源。
@@
 - [連續 podcast prompts](references/episodic_prompts.md)
+- [episodes 範例](references/series_example.md)
```

```diff
diff --git a/CLAUDE.md b/CLAUDE.md
@@
-  `EP{n} 對話紀錄`,讓工作室與來源命名一致、記錄完整。
+  `EP{n:02d}`,讓工作室 artifact 與來源使用完全相同名稱、記錄完整。
```

```diff
diff --git a/references/series_example.md b/references/series_example.md
@@
-`start=3` uses `/tmp/notebooklm/my-series/ep02.mp3` as the prior episode if it
-exists.
+`start=3` assumes the same NotebookLM notebook already contains a source named
+`EP02`. `podcast_series` does not read local `ep02.mp3`; local disk output is only
+the download/manifest location.
```

同樣修改 `references/troubleshooting.md:142-150` 與 `references/cli-reference.md:270-271`：明講 `start=N` 依賴 NotebookLM 來源區已有 `EP{N-1:02d}`，若只有本機 mp3，先用 `podcast_episode(..., prior_mp3_path=...)` 做單集續接，或手動 `source_add_file` 後 `source_rename`（目前沒有 source_rename MCP tool，故文件應優先推薦 `podcast_episode`）。

驗證：

```bash
rg -n "ep\\{N-1\\}|prior episode when that file exists|逐集把前集 mp3|對話紀錄|回傳前集" SKILL.md CLAUDE.md references
uv run pytest -q
```

### P1-1：補齊 SDK 契約測試與 fake client 簽章

問題：B1-B6。  
根因：`tests/test_contracts.py` 是唯一 tripwire，但目前只 pin 了部分方法；fake 與真 SDK 參數 kind 不一致。

最小契約測試片段：

```python
import inspect

def _signature(func):
    return inspect.signature(func)

def _param_names(func):
    return list(_signature(func).parameters)

def _param_kinds(func):
    return {name: p.kind for name, p in _signature(func).parameters.items()}

def test_chat_ask_signature_and_result_answer():
    from notebooklm._chat import ChatAPI
    from notebooklm import AskResult

    assert _param_names(ChatAPI.ask) == [
        "self", "notebook_id", "question", "source_ids", "conversation_id"
    ]
    assert "answer" in getattr(AskResult, "__dataclass_fields__", {})

def test_source_contracts():
    from notebooklm._sources import SourcesAPI
    from notebooklm import Source

    assert _param_names(SourcesAPI.add_url) == ["self", "notebook_id", "url", "wait", "wait_timeout"]
    assert _param_names(SourcesAPI.add_text) == [
        "self", "notebook_id", "title", "content", "wait", "wait_timeout"
    ]
    assert _param_names(SourcesAPI.delete) == ["self", "notebook_id", "source_id"]
    assert "id" in getattr(Source, "__dataclass_fields__", {})
    kinds = _param_kinds(SourcesAPI.add_file)
    assert kinds["wait"] is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert kinds["wait_timeout"] is inspect.Parameter.POSITIONAL_OR_KEYWORD

def test_notebook_contracts():
    from notebooklm._notebooks import NotebooksAPI
    from notebooklm import Notebook

    assert _param_names(NotebooksAPI.create) == ["self", "title"]
    assert _param_names(NotebooksAPI.list) == ["self"]
    fields = getattr(Notebook, "__dataclass_fields__", {})
    assert {"id", "title"} <= set(fields)

def test_wait_for_completion_full_signature():
    from notebooklm._artifacts import ArtifactsAPI

    assert _param_names(ArtifactsAPI.wait_for_completion) == [
        "self", "notebook_id", "task_id", "initial_interval",
        "max_interval", "timeout", "poll_interval",
    ]
```

Fake 修法：

```diff
diff --git a/tests/conftest.py b/tests/conftest.py
@@
-    async def wait_for_completion(self, notebook_id, task_id, timeout=300.0, **kw):
+    async def wait_for_completion(
+        self, notebook_id, task_id, initial_interval=2.0,
+        max_interval=10.0, timeout=300.0, poll_interval=None
+    ):
@@
-    async def add_file(self, notebook_id, file_path, mime_type=None, *, wait=False, wait_timeout=120.0):
+    async def add_file(self, notebook_id, file_path, mime_type=None, wait=False, wait_timeout=120.0):
```

驗證：

```bash
uv run pytest tests/test_contracts.py -q
uv run pytest -q
```

### P1-2：補 basic tools 行為測試

問題：C1。  
根因：工具註冊測試不等於 SDK 呼叫行為測試。

測試先紅片段：

```python
async def test_notebook_create_and_list(fake_client):
    created = await t.notebook_create("標題")
    listed = await t.notebook_list()
    assert created == {"notebook_id": "nb-123", "title": "標題"}
    assert listed["notebooks"] == [{"notebook_id": "nb-123", "title": "Test"}]

async def test_source_add_url_and_text_and_delete(fake_client):
    assert await t.source_add_url("nb-1", "https://example.com") == {"source_id": "src-url"}
    assert await t.source_add_text("nb-1", "T", "body") == {"source_id": "src-text"}
    assert await t.source_delete("nb-1", "src-url") == {"deleted": "src-url"}
    assert [c[0] for c in fake_client.sources.calls] == ["add_url", "add_text", "delete"]

async def test_artifact_wait_and_rename(fake_client):
    waited = await t.artifact_wait("nb-1", "task-9", timeout=10.0)
    renamed = await t.artifact_rename("nb-1", "task-9", "EP09")
    assert waited["artifact_id"] == "task-9"
    assert renamed == {"artifact_id": "task-9", "title": "EP09"}
```

驗證：

```bash
uv run pytest tests/test_tools_basic.py -q
uv run pytest -q
```

### P1-3：生成失敗與 download 失敗要 fail fast 並寫 manifest 狀態

問題：C2 + 額外發現 X1。  
根因：SDK 生成失敗可能回傳 failed status 而非丟例外；MCP wrapper 只取 `status.task_id`，沒有檢查 `status.is_failed` 或空 task id。

證據：

- SDK 失敗回傳：`.venv/.../notebooklm/_artifacts.py:1979-1986`
- SDK parser 也可能回 `task_id=""`：`.venv/.../notebooklm/_artifacts.py:2196-2198`
- `_run_episode` 直接用 task id：`notebooklm_mcp/tools_podcast.py:57-58`
- `generate_audio` / `artifact_wait` 不回傳 status/error：`notebooklm_mcp/tools_basic.py:76-91`

最小修法片段：

```python
def _status_error_message(status: object) -> str:
    error = getattr(status, "error", None)
    error_code = getattr(status, "error_code", None)
    status_text = getattr(status, "status", None)
    return str(error or error_code or status_text or "missing task_id")


def _ensure_generation_started(status: object) -> str:
    task_id = getattr(status, "task_id", "")
    if getattr(status, "is_failed", False) or not task_id:
        raise RuntimeError(f"Audio generation failed: {_status_error_message(status)}")
    return task_id


def _ensure_generation_completed(status: object) -> None:
    if getattr(status, "is_failed", False):
        raise RuntimeError(f"Audio generation failed while waiting: {_status_error_message(status)}")
```

在 `tools_basic.generate_audio()` 與 `_run_episode()` 產生後使用 `_ensure_generation_started(status)`；在 wait 後使用 `_ensure_generation_completed(final_status)`。`artifact_wait()` 建議保留既有欄位並加上 `status/error/error_code`，屬向後相容：

```python
return {
    "task_id": status.task_id,
    "artifact_id": status.task_id,
    "status": getattr(status, "status", None),
    "error": getattr(status, "error", None),
    "error_code": getattr(status, "error_code", None),
}
```

`podcast_series` 對 `_run_episode` 加 try/except，在生成、wait、download、self-upload 任一步失敗時，寫入該集失敗狀態再 re-raise：

```python
try:
    res = await _run_episode(...)
except Exception as exc:
    failed = {"episode": episode_n, "status": "failed", "error": str(exc)}
    _write_manifest(manifest_path, notebook_id, manifest_results + [failed])
    raise
```

測試先紅：

```python
async def test_episode_generation_failed_fails_fast(fake_client, tmp_path):
    fake_client.artifacts.fail_generate = True
    with pytest.raises(RuntimeError, match="Audio generation failed"):
        await p.podcast_episode("nb-1", 1, "brief", str(tmp_path))
    assert [c[0] for c in fake_client.artifacts.calls] == ["generate_audio"]

async def test_series_records_failed_episode_when_download_fails(fake_client, tmp_path):
    fake_client.artifacts.fail_download = True
    with pytest.raises(RuntimeError):
        await p.podcast_series("nb-1", [{"brief": "1"}], str(tmp_path))
    manifest = json.loads((tmp_path / "series_manifest.json").read_text(encoding="utf-8"))
    assert manifest["episodes"][0]["episode"] == 1
    assert manifest["episodes"][0]["status"] == "failed"
```

驗證：

```bash
uv run pytest tests/test_tools_basic.py tests/test_tools_podcast.py -q
uv run pytest -q
```

### P1-4：修 auth_cli 壞 JSON、壞 schema 與檔案權限窗口

問題：E1/E2。  
根因：驗證不足；先寫檔再報錯；`open` 後 chmod 有短暫權限窗口；目錄沒有強制 `0700`。

最小修法片段：

```python
from pathlib import Path


def _parse_storage_state(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid storage_state JSON: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit("Invalid storage_state: root must be an object")
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        raise SystemExit("Invalid storage_state: 'cookies' must be a list")
    return data


def _write_private_json(path: str, data: dict) -> None:
    out = Path(path).expanduser()
    parent = out.parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt" and parent != Path("."):
            parent.chmod(0o700)
    if os.name == "nt":
        out.write_text(json.dumps(data), encoding="utf-8")
        return
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
```

測試先紅可用 `pytest` 的 `monkeypatch` + `tmp_path` 驅動 `auth_cli.main()`，或保留 subprocess 測試：

```bash
printf 'not json' | uv run python -m notebooklm_mcp.auth_cli --out /tmp/bad.json
printf '{"cookies":5}' | uv run python -m notebooklm_mcp.auth_cli --out /tmp/bad.json
printf '{"cookies":[]}' | uv run python -m notebooklm_mcp.auth_cli --out /tmp/good.json
```

期望：前兩個一行錯誤且不寫檔；第三個寫出 `0600` 檔案。

驗證：

```bash
uv run pytest -q
```

### P2-1：補安全與設定衛生文件

問題：E3/E4/E5/R5。  
根因：文件教 HTTP 綁 `0.0.0.0` 卻沒警語；ignore 與本專案現況不一致；scratch `.mcp.json` 不是 canonical。

最小修法：

```diff
diff --git a/docs/mcp-setup.md b/docs/mcp-setup.md
@@
 For HTTP transport:
+Only bind to `0.0.0.0` on a trusted network behind firewall rules. The MCP
+server itself does not add authentication; stdio or `127.0.0.1` is safer for
+normal local use.
```

同樣在 `CLAUDE.md:17` 旁補短警語。

```diff
diff --git a/.gitignore b/.gitignore
@@
 .notebooklm/
+/storage_state.json
+/storage_state*.json
 captured_rpcs/
@@
-# VCR cassettes - committed after security review
-# Cassettes are scrubbed of sensitive data (cookies, tokens, user IDs, emails)
-# See tests/vcr_config.py for scrubbing patterns
```

`.mcp.json` 不建議直接 commit；若要保留範例，應改成文件中的 canonical `uv run --directory /home/...` 形式，並明確標成 example。

驗證：

```bash
rg -n "0\\.0\\.0\\.0|storage_state|VCR|captured_rpcs|vcr_config|--directory" CLAUDE.md docs .gitignore .mcp.json
uv run pytest -q
```

### P2-2：清理其餘文件漂移

問題：D3/D4。  
根因：prompt 參考仍混用舊 CLI 語彙。

最小修法：

- `references/episodic_prompts.md:104`：改成「此模板可作為 `chat_ask` 的問題或人工 brief 草稿；目前 MCP 未封裝 report generation」。
- `references/episodic_prompts.md:118-120`：改成「目前 MCP 未封裝 persona/configure；若要規劃大綱，用對話層或 `chat_ask` 直接詢問」。
- `SKILL.md:73-78`：補 `series_example.md`。

驗證：

```bash
rg -n "generate report|configure --persona|`ask`|series_example" references SKILL.md
uv run pytest -q
```

### P2-3：單集誤用與啟動錯誤訊息

問題：F1/F2。  
根因：`podcast_episode` 沒防 `episode_n=1` + `prior_mp3_path`；server lifespan auth 錯誤直接 traceback。

最小修法：

```diff
diff --git a/notebooklm_mcp/tools_podcast.py b/notebooklm_mcp/tools_podcast.py
@@
 async def _run_episode(...):
     client = runtime.get_client()
     os.makedirs(output_dir, exist_ok=True)
+    if prior_mp3_path and episode_n <= 1:
+        raise ValueError("prior_mp3_path requires episode_n >= 2")
```

測試：

```python
async def test_episode_rejects_prior_for_first_episode(fake_client, tmp_path):
    prior = tmp_path / "ep00.mp3"
    prior.write_bytes(b"x")
    with pytest.raises(ValueError, match="episode_n >= 2"):
        await p.podcast_episode("nb-1", 1, "brief", str(tmp_path), prior_mp3_path=str(prior))
```

啟動錯誤訊息可在 `notebooklm_mcp/app.py:44-55` 的 `main()` 外層包一層清楚訊息；注意不要吞掉非 auth 的程式錯誤：

```python
try:
    mcp.run(transport="stdio")
except (FileNotFoundError, ValueError) as exc:
    raise SystemExit(f"NotebookLM authentication failed: {exc}") from None
```

驗證：

```bash
uv run pytest tests/test_tools_podcast.py tests/test_server_lifespan.py -q
uv run pytest -q
```

## 4. A4 取捨決議

決議：**改文件對齊現況，不實作 disk-feed**。

理由：

- 現行設計已在 findings 明確定稿：每集下載後自上傳自己的 mp3，來源名與 Studio artifact 完全相同，下一集生成時 SDK `generate_audio` 使用全部 sources，因此自然看到前集來源：`docs/superpowers/notebooklm-mcp-findings.md:66-72`。
- 重新實作 `podcast_series` disk-feed 會在 resume 時再次上傳本機 `ep{N-1}.mp3`，可能造成來源重複，並與「每集含最後一集都自上傳同名來源」鐵律衝突。
- disk-feed 仍可保留在 `podcast_episode(prior_mp3_path=...)`，供一次性手動續接使用；這與 `_run_episode` 的註解一致：`notebooklm_mcp/tools_podcast.py:31-34`。
- resume 的正確契約應是：「同一筆記本已含前面集數的 `EPxx` 來源；`start=N` 只控制從第 N 集開始跑迴圈與 manifest 合併」。這比同時支援來源區與本機磁碟兩套 truth source 更薄、更符合專案哲學。

## 5. 額外發現

### X1：生成失敗 status 未被 MCP wrapper 處理

Severity：medium。  
證據：

- SDK `_call_generate` 對 `USER_DISPLAYABLE_ERROR` 會回 `GenerationStatus(task_id="", status="failed", error=...)`：`.venv/.../notebooklm/_artifacts.py:1979-1986`
- parser 沒拿到 artifact id 時也回 `task_id=""`：`.venv/.../notebooklm/_artifacts.py:2196-2198`
- `_run_episode` 直接 `artifact_id = status.task_id`，再 wait/rename/download：`notebooklm_mcp/tools_podcast.py:57-65`
- `tools_basic.generate_audio` 直接回空 id：`notebooklm_mcp/tools_basic.py:76-84`

影響：限流、配額、API 拒絕或生成立即失敗時，basic tool 可能回 `{task_id: "", artifact_id: ""}`；podcast 複合工具可能拿空 task id 等到 timeout，或在 fake 測試中繼續 rename/download。修法已併入 P1-3。

本輪在指定檔案與 SDK 0.3.4 範圍內，未再發現會超過上述優先序的破壞性問題。
