# NotebookLM MCP 重構 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 NotebookLM 整合從「萬能 skill」重構成「自建薄 MCP server（建在 `notebooklm-py` 上）+ 薄路由 skill + 確定性續集工具」，認證透過 Doppler 在 3 台 VM 同步。

**Architecture:** 單一 repo。FastMCP server 包 `notebooklm-py` 公開 `NotebookLMClient`，長駐單一程序（lifespan 管理 client，跨長生成不掉線）。每個 generate 工具 `language` 預設 `zh_Hant` 並走白名單檢查；字串→int-enum 映射。複合工具 `podcast_episode` / `podcast_series` 用純 Python 迴圈實現序列回饋法。認證走 `NOTEBOOKLM_AUTH_JSON`（Doppler 注入、唯讀）。

**Tech Stack:** Python 3.11+、`notebooklm-py>=0.3,<0.4`、`mcp[cli]`（FastMCP）、`pytest` + `pytest-asyncio`、`uv`、Doppler。

**參考設計**：`docs/superpowers/specs/2026-06-07-notebooklm-mcp-redesign-design.md`
**參考原始碼（唯讀，位於 repo 外）**：`/home/user/research/audiskill/_research/{notebooklm-py,pavel-notebooklm-mcp,diet-mcp}`

---

## File Structure

| 檔案 | 責任 |
|------|------|
| `pyproject.toml` | 套件定義、依賴 pin、pytest 設定 |
| `notebooklm_mcp/__init__.py` | 版本、公開匯出 |
| `notebooklm_mcp/runtime.py` | 長駐 client holder（`set_client`/`get_client`）— 讓工具取得 lifespan 管理的 client，便於測試 monkeypatch |
| `notebooklm_mcp/languages.py` | `SUPPORTED_LANGUAGES`、`DEFAULT_LANGUAGE="zh_Hant"`、`resolve_language()` 白名單檢查 |
| `notebooklm_mcp/enums.py` | 字串→`AudioFormat`/`AudioLength` 映射 |
| `notebooklm_mcp/server.py` | FastMCP app、lifespan（開 client + Doppler/env 認證）、argparse 多 transport main |
| `notebooklm_mcp/tools_basic.py` | 基本工具：notebook / source / generate / artifact / ask |
| `notebooklm_mcp/tools_podcast.py` | 複合工具：`podcast_episode` / `podcast_series` |
| `notebooklm_mcp/auth_cli.py` | 貼 cookie 建 storage_state（抄 Pavel，headless 友善） |
| `tests/test_contracts.py` | `inspect.signature` 契約測試（對 `notebooklm-py` 公開 API） |
| `tests/test_languages.py` | 語言白名單單元測試 |
| `tests/test_enums.py` | enum 映射單元測試 |
| `tests/test_tools_basic.py` | 基本工具（mock client）單元測試 |
| `tests/test_tools_podcast.py` | 複合工具（mock client）單元測試 |
| `tests/conftest.py` | `FakeClient` fixture |
| `SKILL.md` | 改寫成薄路由層 |
| `scripts/sync-auth.sh` | 保留並提升為必須（Doppler 同步） |
| `references/{cli-reference,troubleshooting,episodic_prompts}.md` | 改寫 / 精簡 / 保留 |

**刪除**：`scripts/episodic_podcast.py`、`scripts/series_config_example.yaml`（由 `podcast_series` + 新範例取代）。

---

## Phase 0：Repo 腳手架 + Doppler 驗證關卡

### Task 0.1：建立套件骨架與依賴

**Files:**
- Create: `pyproject.toml`
- Create: `notebooklm_mcp/__init__.py`

- [ ] **Step 1: 寫 `pyproject.toml`**

```toml
[project]
name = "notebooklm-mcp-skill"
version = "0.1.0"
description = "Thin self-built MCP server over notebooklm-py for the NotebookLM skill"
requires-python = ">=3.11"
dependencies = [
    "notebooklm-py>=0.3,<0.4",
    "mcp[cli]>=1.0.0",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[project.scripts]
notebooklm-mcp = "notebooklm_mcp.server:main"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 2: 寫 `notebooklm_mcp/__init__.py`**

```python
"""Thin MCP server over notebooklm-py."""

__version__ = "0.1.0"
```

- [ ] **Step 3: 安裝並驗證 import**

Run: `cd /home/user/research/audiskill/notebooklm-skill && uv venv && uv pip install -e ".[dev]"`
Expected: 安裝成功，無錯誤。

Run: `uv run python -c "import notebooklm; import mcp.server.fastmcp; print('ok')"`
Expected: 印出 `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml notebooklm_mcp/__init__.py
git commit -m "chore: scaffold notebooklm-mcp package skeleton"
```

### Task 0.2：Doppler 認證驗證關卡（live，手動）

**Files:** 無（手動驗證，結果記錄在 commit message）

- [ ] **Step 1: 確認 Doppler secret 存在**

Run: `doppler secrets get NOTEBOOKLM_AUTH_JSON -p notebooklm -c dev --plain | head -c 50`
Expected: 印出 JSON 開頭（`{"cookies":...`）。若空，先在本機 `notebooklm login && bash scripts/sync-auth.sh`。

- [ ] **Step 2: 驗證唯讀注入可登入**

Run: `doppler run -p notebooklm -c dev -- uv run python -c "import asyncio; from notebooklm import NotebookLMClient; asyncio.run((lambda: NotebookLMClient.from_storage().__aenter__())()) and None" 2>&1 | tail -5`
（若上行過於 tricky，改用簡單腳本）建立暫存腳本 `/tmp/nbm_check.py`：

```python
import asyncio
from notebooklm import NotebookLMClient

async def main():
    async with NotebookLMClient.from_storage() as client:
        nbs = await client.notebooks.list()
        print(f"OK: {len(nbs)} notebooks")

asyncio.run(main())
```

Run: `doppler run -p notebooklm -c dev -- uv run python /tmp/nbm_check.py`
Expected: `OK: N notebooks`（證明 Doppler env-var 唯讀認證可用）

- [ ] **Step 3: live mp3 round-trip（最高優先驗證項）**

建立 `/tmp/nbm_mp3_check.py`：

```python
import asyncio
from notebooklm import NotebookLMClient

async def main():
    async with NotebookLMClient.from_storage() as client:
        nb = await client.notebooks.create("MP3 roundtrip test")
        await client.sources.add_text(nb.id, "Seed", "This is a short seed document about cats." * 20)
        status = await client.artifacts.generate_audio(nb.id, language="zh_Hant")
        print("task:", status.task_id)
        await client.artifacts.wait_for_completion(nb.id, status.task_id, timeout=1200)
        path = await client.artifacts.download_audio(nb.id, "/tmp/nbm_ep.mp3")
        print("downloaded:", path)
        # 命脈：把 mp3 當來源回傳
        src = await client.sources.add_file(nb.id, "/tmp/nbm_ep.mp3", mime_type="audio/mpeg", wait=True, wait_timeout=600)
        print("re-uploaded source:", src.id)

asyncio.run(main())
```

Run: `doppler run -p notebooklm -c dev -- uv run python /tmp/nbm_mp3_check.py`
Expected: 印出 task / downloaded / re-uploaded source id（**證明序列回饋法的命脈在 live 環境成立**）。若此步失敗，停止計畫並回報——這是整個設計的前提。

- [ ] **Step 4: Commit 驗證結果（純記錄）**

```bash
git commit --allow-empty -m "test: Phase 0 live validation passed (doppler auth + mp3 roundtrip)"
```

---

## Phase 1：MCP server 基礎（client lifespan + transport + 契約測試）

### Task 1.1：契約測試（鎖定 `notebooklm-py` 公開 API 簽名）

**Files:**
- Create: `tests/test_contracts.py`

- [ ] **Step 1: 寫契約測試**

```python
"""Pin notebooklm-py PUBLIC API signatures. Breaks loudly if the SDK changes
under us — our single tripwire against silent upstream API drift."""
import inspect
from notebooklm import NotebookLMClient
from notebooklm.rpc.types import AudioFormat, AudioLength


def _params(func):
    return list(inspect.signature(func).parameters)


def test_generate_audio_signature():
    p = _params(NotebookLMClient.artifacts.fget.__annotations__ and
                __import__("notebooklm._artifacts", fromlist=["ArtifactsAPI"]).ArtifactsAPI.generate_audio)
    assert p[:6] == ["self", "notebook_id", "source_ids", "language", "instructions", "audio_format"]
    assert "audio_length" in p


def test_download_audio_arg_order():
    from notebooklm._artifacts import ArtifactsAPI
    assert _params(ArtifactsAPI.download_audio) == ["self", "notebook_id", "output_path", "artifact_id"]


def test_wait_for_completion_has_task_id_and_timeout():
    from notebooklm._artifacts import ArtifactsAPI
    p = _params(ArtifactsAPI.wait_for_completion)
    assert p[1] == "notebook_id" and p[2] == "task_id"
    assert "timeout" in p


def test_add_file_accepts_mime_and_wait():
    from notebooklm._sources import SourcesAPI
    p = _params(SourcesAPI.add_file)
    assert p[:3] == ["self", "notebook_id", "file_path"]
    assert "mime_type" in p and "wait" in p and "title" in p


def test_audio_enum_members():
    assert AudioFormat.DEEP_DIVE == 1 and AudioFormat.DEBATE == 4
    assert AudioLength.SHORT == 1 and AudioLength.DEFAULT == 2 and AudioLength.LONG == 3
```

- [ ] **Step 2: 跑測試確認通過（對真實 SDK）**

Run: `uv run pytest tests/test_contracts.py -v`
Expected: 全 PASS。若 `test_generate_audio_signature` 的 import hack 失敗，改成直接 `from notebooklm._artifacts import ArtifactsAPI` 取 `generate_audio`（與其他測試一致）。

- [ ] **Step 3: Commit**

```bash
git add tests/test_contracts.py
git commit -m "test: pin notebooklm-py public API contracts"
```

### Task 1.2：client holder（runtime）

**Files:**
- Create: `notebooklm_mcp/runtime.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: 寫 `runtime.py`**

```python
"""Holds the long-lived NotebookLMClient set up by the server lifespan,
so tool functions can fetch it without threading it through every call.
Tests monkeypatch `_CLIENT` with a fake."""
from __future__ import annotations
from typing import Any

_CLIENT: Any | None = None


def set_client(client: Any) -> None:
    global _CLIENT
    _CLIENT = client


def get_client() -> Any:
    if _CLIENT is None:
        raise RuntimeError("NotebookLM client not initialized (server lifespan not started)")
    return _CLIENT
```

- [ ] **Step 2: 寫 `tests/conftest.py`（FakeClient）**

```python
import pytest
from notebooklm_mcp import runtime


class FakeArtifacts:
    def __init__(self):
        self.calls = []

    async def generate_audio(self, notebook_id, source_ids=None, language="en",
                             instructions=None, audio_format=None, audio_length=None):
        self.calls.append(("generate_audio", dict(notebook_id=notebook_id, language=language,
                            instructions=instructions, audio_format=audio_format, audio_length=audio_length)))
        return type("S", (), {"task_id": "task-123", "artifact_id": "art-123"})()

    async def wait_for_completion(self, notebook_id, task_id, timeout=300.0, **kw):
        self.calls.append(("wait", dict(notebook_id=notebook_id, task_id=task_id, timeout=timeout)))
        return type("S", (), {"task_id": task_id, "artifact_id": "art-123"})()

    async def download_audio(self, notebook_id, output_path, artifact_id=None):
        self.calls.append(("download", dict(notebook_id=notebook_id, output_path=output_path, artifact_id=artifact_id)))
        return output_path

    async def rename(self, notebook_id, artifact_id, new_title, *, return_object=True):
        self.calls.append(("rename", dict(artifact_id=artifact_id, new_title=new_title)))
        return None


class FakeSources:
    def __init__(self):
        self.calls = []

    async def add_file(self, notebook_id, file_path, mime_type=None, *, wait=False,
                       wait_timeout=120.0, title=None, on_progress=None):
        self.calls.append(("add_file", dict(notebook_id=notebook_id, file_path=str(file_path),
                            mime_type=mime_type, wait=wait, title=title)))
        return type("Src", (), {"id": "src-123"})()

    async def add_url(self, notebook_id, url):
        self.calls.append(("add_url", dict(notebook_id=notebook_id, url=url)))
        return type("Src", (), {"id": "src-url"})()

    async def add_text(self, notebook_id, title, content):
        self.calls.append(("add_text", dict(title=title)))
        return type("Src", (), {"id": "src-text"})()

    async def delete(self, notebook_id, source_id):
        self.calls.append(("delete", dict(source_id=source_id)))


class FakeNotebooks:
    async def create(self, title):
        return type("NB", (), {"id": "nb-123", "title": title})()

    async def list(self):
        return [type("NB", (), {"id": "nb-123", "title": "Test"})()]


class FakeChat:
    async def ask(self, notebook_id, question):
        return type("R", (), {"answer": f"answer to {question}"})()


class FakeClient:
    def __init__(self):
        self.artifacts = FakeArtifacts()
        self.sources = FakeSources()
        self.notebooks = FakeNotebooks()
        self.chat = FakeChat()


@pytest.fixture
def fake_client():
    client = FakeClient()
    runtime.set_client(client)
    yield client
    runtime.set_client(None)
```

- [ ] **Step 3: Commit**

```bash
git add notebooklm_mcp/runtime.py tests/conftest.py
git commit -m "feat: add client runtime holder + fake client fixture"
```

---

## Phase 2：語言與 enum 輔助（zh_Hant 預設 + 白名單 + 字串→enum）

### Task 2.1：語言白名單

**Files:**
- Create: `notebooklm_mcp/languages.py`
- Create: `tests/test_languages.py`

- [ ] **Step 1: 寫失敗測試**

```python
import pytest
from notebooklm_mcp.languages import resolve_language, DEFAULT_LANGUAGE


def test_default_is_zh_hant():
    assert DEFAULT_LANGUAGE == "zh_Hant"
    assert resolve_language(None) == "zh_Hant"


def test_accepts_known_code():
    assert resolve_language("en") == "en"
    assert resolve_language("zh_Hant") == "zh_Hant"


def test_rejects_hyphen_form():
    with pytest.raises(ValueError) as e:
        resolve_language("zh-TW")
    assert "zh_Hant" in str(e.value)


def test_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_language("xx")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_languages.py -v`
Expected: FAIL（`ModuleNotFoundError: notebooklm_mcp.languages`）

- [ ] **Step 3: 寫 `languages.py`**

```python
"""Language whitelist. The SDK does NOT validate language codes — it forwards
whatever string you pass. We re-implement the whitelist (mirrors
notebooklm-py's cli/language_cmd.py SUPPORTED_LANGUAGES) and default to
Traditional Chinese, since the skill is zh_Hant-first."""
from __future__ import annotations

DEFAULT_LANGUAGE = "zh_Hant"

# Mirror of notebooklm-py cli/language_cmd.py SUPPORTED_LANGUAGES (codes only).
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "zh_Hant": "中文（繁體）",
    "zh_Hans": "中文（简体）",
    "ja": "日本語",
    "ko": "한국어",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
}


def resolve_language(code: str | None) -> str:
    """Return a validated language code, defaulting to zh_Hant. Raise ValueError
    with a helpful message on unknown / hyphenated codes."""
    if code is None:
        return DEFAULT_LANGUAGE
    if code in SUPPORTED_LANGUAGES:
        return code
    hint = ""
    if "-" in code:
        hint = " Use underscore form (e.g. 'zh_Hant', not 'zh-TW')."
    raise ValueError(
        f"Unknown language code {code!r}.{hint} "
        f"Supported: {', '.join(SUPPORTED_LANGUAGES)}"
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_languages.py -v`
Expected: 全 PASS

- [ ] **Step 5: 對齊真實白名單（防漂移）**

Run: `grep -oE '"[a-zA-Z_]+":' /home/user/research/audiskill/_research/notebooklm-py/src/notebooklm/cli/language_cmd.py | head -20`
比對我們的 `SUPPORTED_LANGUAGES` keys，補齊任何缺漏的碼（至少包含 `zh_Hant`/`zh_Hans`/`en`/`ja`/`ko`）。

- [ ] **Step 6: Commit**

```bash
git add notebooklm_mcp/languages.py tests/test_languages.py
git commit -m "feat: language whitelist with zh_Hant default"
```

### Task 2.2：enum 映射

**Files:**
- Create: `notebooklm_mcp/enums.py`
- Create: `tests/test_enums.py`

- [ ] **Step 1: 寫失敗測試**

```python
import pytest
from notebooklm.rpc.types import AudioFormat, AudioLength
from notebooklm_mcp.enums import to_audio_format, to_audio_length


def test_audio_format_strings():
    assert to_audio_format("deep-dive") == AudioFormat.DEEP_DIVE
    assert to_audio_format("debate") == AudioFormat.DEBATE
    assert to_audio_format(None) is None


def test_audio_length_strings():
    assert to_audio_length("short") == AudioLength.SHORT
    assert to_audio_length("default") == AudioLength.DEFAULT
    assert to_audio_length("long") == AudioLength.LONG
    assert to_audio_length(None) is None


def test_invalid_raises():
    with pytest.raises(ValueError):
        to_audio_format("bogus")
    with pytest.raises(ValueError):
        to_audio_length("medium")   # 'medium' is NOT a valid length (Diet's bug)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_enums.py -v`
Expected: FAIL（module 不存在）

- [ ] **Step 3: 寫 `enums.py`**

```python
"""String -> int-enum maps. The SDK's generate_audio wants AudioFormat /
AudioLength int-enums, NOT raw strings (passing 'deep-dive'/'medium' is the bug
both existing wrappers shipped). We map agent-friendly strings to enums and
reject anything invalid loudly."""
from __future__ import annotations
from notebooklm.rpc.types import AudioFormat, AudioLength

_FORMAT = {
    "deep-dive": AudioFormat.DEEP_DIVE,
    "brief": AudioFormat.BRIEF,
    "critique": AudioFormat.CRITIQUE,
    "debate": AudioFormat.DEBATE,
}
_LENGTH = {
    "short": AudioLength.SHORT,
    "default": AudioLength.DEFAULT,
    "long": AudioLength.LONG,
}


def to_audio_format(value: str | None) -> AudioFormat | None:
    if value is None:
        return None
    try:
        return _FORMAT[value]
    except KeyError:
        raise ValueError(f"Invalid audio format {value!r}. Choose from: {', '.join(_FORMAT)}")


def to_audio_length(value: str | None) -> AudioLength | None:
    if value is None:
        return None
    try:
        return _LENGTH[value]
    except KeyError:
        raise ValueError(f"Invalid audio length {value!r}. Choose from: {', '.join(_LENGTH)}")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_enums.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/enums.py tests/test_enums.py
git commit -m "feat: string->int-enum maps for audio format/length"
```

---

## Phase 3：基本工具

### Task 3.1：FastMCP app + lifespan + transport main

**Files:**
- Create: `notebooklm_mcp/server.py`

- [ ] **Step 1: 寫 `server.py`**

```python
"""FastMCP server entrypoint. Owns one long-lived NotebookLMClient (so the
SDK's in-process token keepalive survives multi-hour generations) and exposes
stdio / streamable-http / sse transports.

Auth comes from NOTEBOOKLM_AUTH_JSON (injected by `doppler run`) — read-only,
no disk write-back, so 3 VMs share one Doppler secret without drift."""
from __future__ import annotations
import argparse
import contextlib
from collections.abc import AsyncIterator

from mcp.server.fastmcp import FastMCP
from notebooklm import NotebookLMClient

from . import runtime


@contextlib.asynccontextmanager
async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
    # from_storage() picks up NOTEBOOKLM_AUTH_JSON automatically (precedence #2).
    async with NotebookLMClient.from_storage() as client:
        runtime.set_client(client)
        try:
            yield
        finally:
            runtime.set_client(None)


mcp = FastMCP("notebooklm", lifespan=_lifespan)

# Register tools (import for side effects: each module calls @mcp.tool()).
from . import tools_basic, tools_podcast  # noqa: E402,F401


def main() -> None:
    parser = argparse.ArgumentParser(description="NotebookLM MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8484)
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
```

> 註：`tools_basic` / `tools_podcast` 用 `from .server import mcp` 取得 app 來註冊。為避免循環匯入，`server.py` 在定義 `mcp` 之後才 import 它們（如上）。

- [ ] **Step 2: 驗證可 import（尚無工具模組會失敗，先建空模組）**

建立暫時空檔讓 import 通過（Task 3.2/4.x 會填內容）：

```bash
printf 'from .server import mcp  # noqa\n' > notebooklm_mcp/tools_basic.py
printf 'from .server import mcp  # noqa\n' > notebooklm_mcp/tools_podcast.py
```

Run: `uv run python -c "from notebooklm_mcp.server import mcp; print(mcp.name)"`
Expected: 印出 `notebooklm`

- [ ] **Step 3: Commit**

```bash
git add notebooklm_mcp/server.py notebooklm_mcp/tools_basic.py notebooklm_mcp/tools_podcast.py
git commit -m "feat: FastMCP app with client lifespan and multi-transport main"
```

### Task 3.2：基本工具（notebook / source / generate_audio / artifact / ask）

**Files:**
- Modify: `notebooklm_mcp/tools_basic.py`
- Create: `tests/test_tools_basic.py`

- [ ] **Step 1: 寫失敗測試**

```python
import pytest
from notebooklm.rpc.types import AudioFormat, AudioLength
from notebooklm_mcp import tools_basic as t


async def test_generate_audio_defaults_zh_hant(fake_client):
    out = await t.generate_audio("nb-1", instructions="講解重點")
    assert out["task_id"] == "task-123"
    call = dict(fake_client.artifacts.calls)["generate_audio"] if False else fake_client.artifacts.calls[0][1]
    assert call["language"] == "zh_Hant"
    assert call["instructions"] == "講解重點"


async def test_generate_audio_maps_enums(fake_client):
    await t.generate_audio("nb-1", audio_format="debate", audio_length="long")
    call = fake_client.artifacts.calls[0][1]
    assert call["audio_format"] == AudioFormat.DEBATE
    assert call["audio_length"] == AudioLength.LONG


async def test_generate_audio_rejects_bad_language(fake_client):
    with pytest.raises(ValueError):
        await t.generate_audio("nb-1", language="zh-TW")


async def test_source_add_file_passes_mime(fake_client):
    out = await t.source_add_file("nb-1", "/tmp/x.mp3", mime_type="audio/mpeg")
    assert out["source_id"] == "src-123"
    assert fake_client.sources.calls[0][1]["mime_type"] == "audio/mpeg"


async def test_artifact_download_arg_order(fake_client):
    out = await t.artifact_download_audio("nb-1", "/tmp/out.mp3", artifact_id="art-9")
    assert out["path"] == "/tmp/out.mp3"
    c = fake_client.artifacts.calls[0][1]
    assert c["output_path"] == "/tmp/out.mp3" and c["artifact_id"] == "art-9"


async def test_ask(fake_client):
    out = await t.chat_ask("nb-1", "重點?")
    assert "重點" in out["answer"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_tools_basic.py -v`
Expected: FAIL（函式未定義）

- [ ] **Step 3: 寫 `tools_basic.py`**

```python
"""Basic NotebookLM tools. Thin wrappers over the resident client, with
zh_Hant default + enum mapping baked in. Each returns a plain JSON-able dict."""
from __future__ import annotations

from .server import mcp
from . import runtime
from .languages import resolve_language
from .enums import to_audio_format, to_audio_length


@mcp.tool()
async def notebook_create(title: str) -> dict:
    """Create a new notebook. Returns its id."""
    nb = await runtime.get_client().notebooks.create(title)
    return {"notebook_id": nb.id, "title": getattr(nb, "title", title)}


@mcp.tool()
async def notebook_list() -> dict:
    """List all notebooks."""
    nbs = await runtime.get_client().notebooks.list()
    return {"notebooks": [{"notebook_id": n.id, "title": getattr(n, "title", "")} for n in nbs]}


@mcp.tool()
async def source_add_url(notebook_id: str, url: str) -> dict:
    """Add a URL or YouTube link as a source."""
    src = await runtime.get_client().sources.add_url(notebook_id, url)
    return {"source_id": src.id}


@mcp.tool()
async def source_add_text(notebook_id: str, title: str, content: str) -> dict:
    """Add plain text as a source."""
    src = await runtime.get_client().sources.add_text(notebook_id, title, content)
    return {"source_id": src.id}


@mcp.tool()
async def source_add_file(notebook_id: str, file_path: str, mime_type: str | None = None,
                          title: str | None = None, wait: bool = True) -> dict:
    """Add a LOCAL file as a source. For audio (e.g. a generated podcast mp3 fed
    back for episodic continuity) pass mime_type='audio/mpeg'."""
    src = await runtime.get_client().sources.add_file(
        notebook_id, file_path, mime_type=mime_type, wait=wait, wait_timeout=600.0, title=title
    )
    return {"source_id": src.id}


@mcp.tool()
async def source_delete(notebook_id: str, source_id: str) -> dict:
    """Delete a source (e.g. remove a rejected episode before generating the next)."""
    await runtime.get_client().sources.delete(notebook_id, source_id)
    return {"deleted": source_id}


@mcp.tool()
async def generate_audio(notebook_id: str, instructions: str | None = None,
                         language: str | None = None, audio_format: str | None = None,
                         audio_length: str | None = None) -> dict:
    """Generate an audio overview (podcast). Defaults to zh_Hant. Returns task_id
    for polling. audio_format: deep-dive|brief|critique|debate. audio_length: short|default|long."""
    status = await runtime.get_client().artifacts.generate_audio(
        notebook_id,
        language=resolve_language(language),
        instructions=instructions,
        audio_format=to_audio_format(audio_format),
        audio_length=to_audio_length(audio_length),
    )
    return {"task_id": status.task_id, "artifact_id": getattr(status, "artifact_id", None)}


@mcp.tool()
async def artifact_wait(notebook_id: str, task_id: str, timeout: float = 1200.0) -> dict:
    """Wait for a generation task to complete (audio default budget 1200s)."""
    status = await runtime.get_client().artifacts.wait_for_completion(notebook_id, task_id, timeout=timeout)
    return {"task_id": status.task_id, "artifact_id": getattr(status, "artifact_id", None)}


@mcp.tool()
async def artifact_download_audio(notebook_id: str, output_path: str, artifact_id: str | None = None) -> dict:
    """Download an audio artifact to output_path. Arg order: (notebook_id, output_path, artifact_id)."""
    path = await runtime.get_client().artifacts.download_audio(notebook_id, output_path, artifact_id)
    return {"path": path}


@mcp.tool()
async def artifact_rename(notebook_id: str, artifact_id: str, new_title: str) -> dict:
    """Rename an artifact so it stays identifiable in the notebook."""
    await runtime.get_client().artifacts.rename(notebook_id, artifact_id, new_title, return_object=False)
    return {"artifact_id": artifact_id, "title": new_title}


@mcp.tool()
async def chat_ask(notebook_id: str, question: str) -> dict:
    """Ask a source-grounded question."""
    res = await runtime.get_client().chat.ask(notebook_id, question)
    return {"answer": res.answer}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_tools_basic.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/tools_basic.py tests/test_tools_basic.py
git commit -m "feat: basic NotebookLM tools (notebook/source/generate_audio/artifact/ask)"
```

---

## Phase 4：複合工具（podcast_episode / podcast_series）

### Task 4.1：podcast_episode（單集 5 步）

**Files:**
- Modify: `notebooklm_mcp/tools_podcast.py`
- Create: `tests/test_tools_podcast.py`

- [ ] **Step 1: 寫失敗測試**

```python
import pytest
from notebooklm_mcp import tools_podcast as p


async def test_episode_first_no_prior(fake_client, tmp_path):
    out = await p.podcast_episode("nb-1", episode_n=1, brief="第一集講開場",
                                  output_dir=str(tmp_path))
    assert out["mp3_path"].endswith("ep01.mp3")
    kinds = [c[0] for c in fake_client.artifacts.calls]
    assert kinds == ["generate_audio", "wait", "download", "rename"]
    # zh_Hant defaulted
    assert fake_client.artifacts.calls[0][1]["language"] == "zh_Hant"
    # no prior re-upload on episode 1
    assert all(c[0] != "add_file" for c in fake_client.sources.calls)


async def test_episode_with_prior_reuploads_mp3(fake_client, tmp_path):
    prior = tmp_path / "ep01.mp3"
    prior.write_bytes(b"x")
    out = await p.podcast_episode("nb-1", episode_n=2, brief="第二集",
                                  output_dir=str(tmp_path), prior_mp3_path=str(prior))
    add = [c for c in fake_client.sources.calls if c[0] == "add_file"][0][1]
    assert add["mime_type"] == "audio/mpeg"
    assert out["mp3_path"].endswith("ep02.mp3")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_tools_podcast.py -v`
Expected: FAIL（函式未定義）

- [ ] **Step 3: 寫 `tools_podcast.py`（episode 部分）**

```python
"""Composite podcast tools implementing the sequential-feedback method:
generate -> wait -> download -> rename -> re-upload mp3 -> next episode.
Continuity comes from NotebookLM transcribing the prior episode's mp3 that we
re-upload as a source. NO LLM in the loop — pure deterministic code."""
from __future__ import annotations
import os

from .server import mcp
from . import runtime
from .languages import resolve_language
from .enums import to_audio_format, to_audio_length


async def _run_episode(notebook_id: str, episode_n: int, brief: str, output_dir: str,
                       prior_mp3_path: str | None, language: str | None,
                       audio_format: str | None, audio_length: str | None,
                       wait_timeout: float) -> dict:
    client = runtime.get_client()
    os.makedirs(output_dir, exist_ok=True)

    # 1. Re-upload prior episode mp3 as a source (continuity feedback edge).
    if prior_mp3_path:
        await client.sources.add_file(
            notebook_id, prior_mp3_path, mime_type="audio/mpeg",
            wait=True, wait_timeout=600.0,
            title=f"EP{episode_n - 1:02d}_第{episode_n - 1}集對話紀錄",
        )

    # 2. Generate this episode's audio.
    status = await client.artifacts.generate_audio(
        notebook_id,
        language=resolve_language(language),
        instructions=brief,
        audio_format=to_audio_format(audio_format),
        audio_length=to_audio_length(audio_length),
    )

    # 3. Wait for completion.
    completed = await client.artifacts.wait_for_completion(notebook_id, status.task_id, timeout=wait_timeout)
    artifact_id = getattr(completed, "artifact_id", None) or getattr(status, "artifact_id", None)

    # 4. Download mp3.
    mp3_path = os.path.join(output_dir, f"ep{episode_n:02d}.mp3")
    await client.artifacts.download_audio(notebook_id, mp3_path, artifact_id)

    # 5. Rename artifact so the notebook stays legible.
    await client.artifacts.rename(notebook_id, artifact_id, f"EP{episode_n:02d}", return_object=False)

    return {"episode": episode_n, "task_id": status.task_id, "artifact_id": artifact_id, "mp3_path": mp3_path}


@mcp.tool()
async def podcast_episode(notebook_id: str, episode_n: int, brief: str, output_dir: str,
                          prior_mp3_path: str | None = None, language: str | None = None,
                          audio_format: str | None = "deep-dive", audio_length: str | None = "long",
                          wait_timeout: float = 1200.0) -> dict:
    """Generate ONE podcast episode end-to-end. If prior_mp3_path is given, it is
    re-uploaded as a source first so this episode has the previous one's transcript
    (continuity). Returns the downloaded mp3 path — feed it as prior_mp3_path for n+1."""
    return await _run_episode(notebook_id, episode_n, brief, output_dir, prior_mp3_path,
                              language, audio_format, audio_length, wait_timeout)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_tools_podcast.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/tools_podcast.py tests/test_tools_podcast.py
git commit -m "feat: podcast_episode composite tool (sequential feedback)"
```

### Task 4.2：podcast_series（純程式碼整季迴圈 + resume + manifest）

**Files:**
- Modify: `notebooklm_mcp/tools_podcast.py`
- Modify: `tests/test_tools_podcast.py`

- [ ] **Step 1: 加失敗測試**

```python
async def test_series_threads_prior_mp3_and_resumes(fake_client, tmp_path):
    eps = [
        {"brief": "第一集"},
        {"brief": "第二集"},
        {"brief": "第三集"},
    ]
    out = await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=1)
    assert len(out["episodes"]) == 3
    assert out["episodes"][0]["mp3_path"].endswith("ep01.mp3")
    assert out["episodes"][2]["mp3_path"].endswith("ep03.mp3")
    # episode 2 and 3 each re-upload a prior mp3; episode 1 does not.
    add_files = [c for c in fake_client.sources.calls if c[0] == "add_file"]
    assert len(add_files) == 2
    # manifest written
    assert (tmp_path / "series_manifest.json").exists()


async def test_series_start_offset(fake_client, tmp_path):
    # pre-existing ep02 so start=3 can re-upload it
    (tmp_path / "ep02.mp3").write_bytes(b"x")
    eps = [{"brief": "1"}, {"brief": "2"}, {"brief": "3"}]
    out = await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=3)
    assert len(out["episodes"]) == 1
    assert out["episodes"][0]["episode"] == 3
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_tools_podcast.py -v`
Expected: 新測試 FAIL（`podcast_series` 未定義）

- [ ] **Step 3: 在 `tools_podcast.py` 追加 `podcast_series`**

```python
import json


@mcp.tool()
async def podcast_series(notebook_id: str, episodes: list[dict], output_dir: str,
                         start: int = 1, language: str | None = None,
                         audio_format: str | None = "deep-dive", audio_length: str | None = "long",
                         wait_timeout: float = 1200.0) -> dict:
    """Generate a full multi-episode series deterministically. Each episode dict
    needs a 'brief' (str) — the per-episode instructions (write these with Opus
    upfront). Episode N's downloaded mp3 is fed as prior_mp3_path to N+1, so
    NotebookLM carries continuity. `start` resumes from episode N (uses the
    already-downloaded ep{N-1}.mp3 in output_dir as the prior). Writes
    series_manifest.json after each episode for observability/resume.

    NO LLM and NO agent in this loop — pure code, fully reproducible."""
    os.makedirs(output_dir, exist_ok=True)
    manifest_path = os.path.join(output_dir, "series_manifest.json")
    results: list[dict] = []

    prior_mp3 = None
    if start > 1:
        candidate = os.path.join(output_dir, f"ep{start - 1:02d}.mp3")
        prior_mp3 = candidate if os.path.exists(candidate) else None

    for episode_n in range(start, len(episodes) + 1):
        brief = episodes[episode_n - 1]["brief"]
        res = await _run_episode(notebook_id, episode_n, brief, output_dir, prior_mp3,
                                 language, audio_format, audio_length, wait_timeout)
        results.append(res)
        prior_mp3 = res["mp3_path"]
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"notebook_id": notebook_id, "episodes": results}, f, ensure_ascii=False, indent=2)

    return {"notebook_id": notebook_id, "episodes": results, "manifest": manifest_path}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_tools_podcast.py -v`
Expected: 全 PASS

- [ ] **Step 5: 跑全測試套件**

Run: `uv run pytest -v`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add notebooklm_mcp/tools_podcast.py tests/test_tools_podcast.py
git commit -m "feat: podcast_series deterministic loop with resume + manifest"
```

---

## Phase 5：認證 CLI + Doppler + skill/docs 改寫

### Task 5.1：auth_cli（貼 cookie 建 storage_state，headless 友善）

**Files:**
- Create: `notebooklm_mcp/auth_cli.py`

- [ ] **Step 1: 寫 `auth_cli.py`**

```python
"""Headless-friendly auth helper: paste a storage_state JSON (or pull from a
browser via notebooklm-py's [cookies] extra) and write storage_state.json.
For the 3-VM setup you normally DON'T use this on the VMs — you run
`notebooklm login` + scripts/sync-auth.sh on a local machine and let Doppler
inject NOTEBOOKLM_AUTH_JSON. This is the fallback for building the JSON."""
from __future__ import annotations
import argparse
import json
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Write storage_state.json from pasted JSON")
    parser.add_argument("--out", default=os.path.expanduser("~/.notebooklm/storage_state.json"))
    args = parser.parse_args()
    print("Paste storage_state JSON, then Ctrl-D:", file=sys.stderr)
    raw = sys.stdin.read().strip()
    data = json.loads(raw)  # raises if invalid — fail loud
    if "cookies" not in data:
        raise SystemExit("Invalid storage_state: missing 'cookies' key")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f)
    if os.name != "nt":
        os.chmod(args.out, 0o600)
    print(f"Wrote {args.out} ({len(data['cookies'])} cookies)", file=sys.stderr)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 驗證可執行（無 JSON 時報錯）**

Run: `echo '{}' | uv run python -m notebooklm_mcp.auth_cli --out /tmp/test_storage.json 2>&1 || echo "rejected as expected"`
Expected: 因缺 `cookies` 而 `rejected as expected`。

Run: `echo '{"cookies":[],"origins":[]}' | uv run python -m notebooklm_mcp.auth_cli --out /tmp/test_storage.json`
Expected: `Wrote /tmp/test_storage.json (0 cookies)`

- [ ] **Step 3: Commit**

```bash
git add notebooklm_mcp/auth_cli.py
git commit -m "feat: headless auth_cli to build storage_state from pasted JSON"
```

### Task 5.2：確認 sync-auth.sh（保留為必須）

**Files:**
- Modify: `scripts/sync-auth.sh`（僅在頂部註解標明「必須」+ 確認內容無誤）

- [ ] **Step 1: 讀現有 `scripts/sync-auth.sh`，確認推送 `NOTEBOOKLM_AUTH_JSON` 到 `notebooklm/dev` 的邏輯正確**

Run: `cat scripts/sync-auth.sh | head -50`
Expected: 內容仍為推送 storage_state.json 到 Doppler。無需改動邏輯。

- [ ] **Step 2: 在檔頭註解標明這是多 VM 同步的必要步驟**（把原本「可選」措辭改為「必須」）

- [ ] **Step 3: Commit**

```bash
git add scripts/sync-auth.sh
git commit -m "docs: mark sync-auth.sh as required for multi-VM Doppler sync"
```

### Task 5.3：MCP client 註冊設定（doppler run 包裹）

**Files:**
- Create: `docs/mcp-setup.md`

- [ ] **Step 1: 寫 `docs/mcp-setup.md`，記錄如何把這個 MCP 註冊進 Claude Code**

內容包含（實際指令）：

```bash
# 註冊 MCP（stdio），用 doppler run 注入認證，3 台 VM 各跑一份
claude mcp add notebooklm -- doppler run -p notebooklm -c dev -- \
  uv run --directory /home/user/research/audiskill/notebooklm-skill \
  python -m notebooklm_mcp.server --transport stdio
```

並說明：HTTP 模式改 `--transport streamable-http --host 0.0.0.0 --port 8484`；
認證過期時本機 `notebooklm login && bash scripts/sync-auth.sh`，VM 端下次啟動自動取得。

- [ ] **Step 2: 驗證 server 在 doppler 下能起來（stdio 啟動後即等待，Ctrl-C 結束）**

Run: `timeout 8 doppler run -p notebooklm -c dev -- uv run python -m notebooklm_mcp.server --transport stdio < /dev/null; echo "exit=$?"`
Expected: 程序啟動後因無 stdin 輸入而在 timeout 結束（`exit=124`）或乾淨退出；**不應出現 traceback / 認證錯誤**。若出現 `from_storage` 認證錯誤，回到 Task 0.2 檢查 Doppler。

- [ ] **Step 3: Commit**

```bash
git add docs/mcp-setup.md
git commit -m "docs: MCP registration via doppler run"
```

### Task 5.4：改寫 SKILL.md（薄路由層）

**Files:**
- Modify: `SKILL.md`（全面改寫）
- Delete: `scripts/episodic_podcast.py`、`scripts/series_config_example.yaml`

- [ ] **Step 1: 改寫 `SKILL.md`**，內容結構：

  1. frontmatter（保留 name/description，更新 description 指出改用 MCP）。
  2. 「能力都在 MCP」：列出工具名與用途對照表（notebook_create/list、source_add_url/text/file、generate_audio、artifact_wait/download_audio/rename、chat_ask、podcast_episode、podcast_series）。
  3. 「簡單情境 → 直接呼叫工具」：單一 podcast / 問答 / 加來源，舉例。
  4. 「連續系列 → 先大綱後 podcast_series」：說明先請 Opus 規劃整季 `episodes`（每集一個 `brief`），使用者核可，再呼叫 `podcast_series`。附 reject-then-delete（用 `source_delete` 移除壞集再重生單集）。
  5. 語言設定表（zh_Hant 預設；quiz/flashcards 在 brief 內指定語言；mind-map 無法指定）。
  6. 認證：一行說明走 Doppler，過期見 troubleshooting。
  7. references 指路（cli-reference / troubleshooting / episodic_prompts）。

  **刪除**：doppler 前綴提醒、`--json`/`-a`/`-n` 提醒、sessions_spawn 單/多任務模板、禁止事項清單（皆被 MCP schema / 長駐程序吸收）。

- [ ] **Step 2: 刪除被取代的腳本**

```bash
git rm scripts/episodic_podcast.py scripts/series_config_example.yaml
```

- [ ] **Step 3: 驗證 SKILL.md frontmatter 合法**

Run: `head -5 SKILL.md`
Expected: 合法 YAML frontmatter（`name: notebooklm`、`description: ...`）。

- [ ] **Step 4: Commit**

```bash
git add SKILL.md
git commit -m "refactor: rewrite SKILL.md as thin MCP router; drop episodic script"
```

### Task 5.5：改寫 references

**Files:**
- Modify: `references/cli-reference.md` → 改成「MCP 工具參考」
- Modify: `references/troubleshooting.md` → 補 MCP / Doppler 段落，刪過時 CLI 段落
- Keep: `references/episodic_prompts.md`（不動）
- Create: `references/series_example.md`（`podcast_series` 的 `episodes` 輸入範例，取代舊 YAML）

- [ ] **Step 1: 改寫 `references/cli-reference.md`** 成 MCP 工具表（每個工具：參數、回傳、範例呼叫），對齊 `tools_basic.py` / `tools_podcast.py` 的實際簽名。

- [ ] **Step 2: 改寫 `references/troubleshooting.md`**：保留認證/研究/生成核心；新增「MCP server 起不來」「Doppler NOTEBOOKLM_AUTH_JSON 空/壞」「3 VM 同步」段落；刪除 `use`/`context.json`/sessions_spawn 相關過時內容。

- [ ] **Step 3: 寫 `references/series_example.md`**：示範一個 4 集 `episodes` 陣列（每集 `brief`），對應 `podcast_series` 呼叫。

- [ ] **Step 4: Commit**

```bash
git add references/
git commit -m "docs: rewrite references for MCP tools + series example"
```

---

## Phase 6：端到端驗證

### Task 6.1：MCP 工具 live smoke test

**Files:** 無（手動）

- [ ] **Step 1: 啟動 MCP 並用 MCP inspector 或 Claude Code 列出工具**

Run: `claude mcp list`（在已註冊後）
Expected: 看到 `notebooklm` server 且狀態為 connected，工具清單含 `podcast_episode` / `podcast_series`。

- [ ] **Step 2: 跑一次最小 `podcast_episode`（單集）對真實筆記本**

在 Claude Code 對話中請 agent 呼叫 `podcast_episode`（用 Task 0.2 建立的測試筆記本、`episode_n=1`、簡短 brief、`output_dir=/tmp/notebooklm`）。
Expected: 回傳 `mp3_path` 且檔案存在、為 zh_Hant 音檔。

- [ ] **Step 3: 跑一次 2 集 `podcast_series`，驗證第 2 集有回傳第 1 集 mp3**

Expected: `series_manifest.json` 列出 2 集；第 2 集的逐字稿（`get_fulltext` 或聽感）顯示有回顧第 1 集 → 連續性成立。

- [ ] **Step 4: 全測試 + 契約測試最終確認**

Run: `uv run pytest -v`
Expected: 全 PASS

- [ ] **Step 5: Commit（記錄 e2e 通過）**

```bash
git commit --allow-empty -m "test: e2e validation passed (episode + series continuity)"
```

---

## Self-Review（計畫對規格的覆蓋檢查）

- **§2.1 引擎 notebooklm-py**：Task 0.1 pin `>=0.3,<0.4`。✅
- **§2.2 自建薄 MCP / 抄組件**：Phase 1-4 自建；transport（Task 3.1 抄 Pavel argparse）、auth_cli（Task 5.1 抄 Pavel 貼 cookie）、契約測試（Task 1.1 抄 Diet 但測公開 API）。✅
- **§2.3 / §5.5 Doppler 必須**：Task 0.2 驗證、Task 5.2 sync-auth 必須、Task 5.3 doppler run 註冊。✅
- **§5.1 工具清單 ~12-14 + zh_Hant 預設 + enum**：Task 2.1/2.2/3.2 + 4.x。✅（`generate_artifact` 泛型未做 → 見下方開放項）
- **§5.2 podcast_episode/series + reject-then-delete**：Task 4.1/4.2 + `source_delete`（Task 3.2）+ SKILL.md 說明（Task 5.4）。✅
- **§9 驗證關卡（live mp3 round-trip）**：Task 0.2 Step 3。✅
- **§6 檔案處置**：Task 5.4/5.5 改寫 SKILL/references、刪 episodic_podcast.py。✅

**Placeholder scan**：無 TBD/TODO；所有 code step 含完整程式碼。
**Type consistency**：`generate_audio` 回傳 `{task_id, artifact_id}`、`artifact_wait` 同；`podcast_episode` 回傳含 `mp3_path`、被 `podcast_series` 當 `prior_mp3` 串接——一致。`source_add_file` 與 `_run_episode` 都用 `mime_type='audio/mpeg'`——一致。

**規格開放項（保留至實作時依工作量決定，非 placeholder）**：
- §10：`generate_artifact` 泛型工具（其餘 8 類型）v1 是否做。**建議**：v1 先只做 `generate_audio`（podcast 是核心）；其餘類型若需要，另開一個 Task 仿 Task 3.2 加 `generate_artifact(type, ...)` 分派到 `generate_video/report/...`。本計畫不含，避免 scope 膨脹。
- §10：大綱改進 workflow 形狀——非 v1，未列任務。
