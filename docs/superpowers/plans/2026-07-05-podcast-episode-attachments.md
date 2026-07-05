# 每集附加簡報 PDF + 研讀講義 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓每集 podcast 除了音檔,還能按需附上 NotebookLM 生成的簡報 PDF 與研讀講義,並在單集 RSS 簡介附上下載連結。

**Architecture:** 兩個按需的獨立 MCP 工具(`generate_slides` / `generate_report`)呼叫 SDK 生成、下載檔案、把路徑回寫 `series_manifest.json`;`publish_series` 發布時把有附件的集 content-hash 後 PUT 到 uploader(PDF 直傳、Markdown 先渲染成 HTML 再傳),並把公開 URL append 到單集 `<description>`。uploader 白名單放寬收 pdf/html。

**Tech Stack:** Python 3.12、`notebooklm-py 0.3.4`、FastMCP、`httpx`(publish PUT)、`markdown`(md→HTML,本案新增)、Pillow(既有)、pytest + pytest-asyncio。

## Global Constraints

- Python 3.12(3.14 觸發 SDK 的 `inspect.signature` bug)。
- `notebooklm-py>=0.3,<0.4` —— 對**實裝 0.3.4** 寫程式,別信 `_research/` HEAD。
- 語言預設 `zh_Hant`(用 `resolve_language`,底線不連字號)。
- 產物只餵**文章來源**時由呼叫端明確傳 `source_ids`;不傳則 SDK 用全部來源(v1 不自動排除音檔來源)。
- TDD:先寫失敗測試 → 跑紅 → 最小實作 → 跑綠 → commit。每任務一 commit。
- 測試全離線:MCP 側用 `fake_client` fixture;publish 側用 `httpx.MockTransport`(見既有 `tests/test_publish_tools.py::_install_mock`)。零真實網路/認證。
- 全程繁體中文註解/文件。commit 訊息寫「症狀/需求 + 根因 + 為何這樣做」,結尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- content-addressed 檔名:`EP{n:02d}-{hash8}.{ext}`,hash8 = sha256(內容)前 8 hex。

---

### Task 1: Contract pins — 鎖住 SDK 新方法簽名

**Files:**
- Modify: `tests/test_contracts.py`(檔尾 append)

**Interfaces:**
- Consumes: `notebooklm._artifacts.ArtifactsAPI`、`notebooklm.types.{SlideDeckFormat,SlideDeckLength,ReportFormat}`
- Produces: 無(純測試 tripwire)

- [ ] **Step 1: 寫失敗測試** — append 到 `tests/test_contracts.py`:

```python
def test_slide_deck_signatures():
    from notebooklm._artifacts import ArtifactsAPI

    assert _params(ArtifactsAPI.generate_slide_deck) == [
        "self", "notebook_id", "source_ids", "language",
        "instructions", "slide_format", "slide_length",
    ]
    assert _params(ArtifactsAPI.download_slide_deck) == [
        "self", "notebook_id", "output_path", "artifact_id", "output_format",
    ]


def test_report_signatures():
    from notebooklm._artifacts import ArtifactsAPI

    assert _params(ArtifactsAPI.generate_report) == [
        "self", "notebook_id", "report_format", "source_ids",
        "language", "custom_prompt", "extra_instructions",
    ]
    assert _params(ArtifactsAPI.generate_study_guide) == [
        "self", "notebook_id", "source_ids", "language", "extra_instructions",
    ]
    assert _params(ArtifactsAPI.download_report) == [
        "self", "notebook_id", "output_path", "artifact_id",
    ]


def test_slide_and_report_enum_members():
    from notebooklm.types import SlideDeckFormat, SlideDeckLength, ReportFormat

    assert SlideDeckFormat.DETAILED_DECK == 1 and SlideDeckFormat.PRESENTER_SLIDES == 2
    assert SlideDeckLength.DEFAULT == 1 and SlideDeckLength.SHORT == 2
    assert ReportFormat.STUDY_GUIDE.value == "study_guide"
    assert ReportFormat.BRIEFING_DOC.value == "briefing_doc"
    assert ReportFormat.BLOG_POST.value == "blog_post"
```

- [ ] **Step 2: 跑測試確認通過**（這些鎖的是已存在的實裝 API,應直接綠 —— 目的是「未來漂移才紅」的 tripwire）

Run: `uv run pytest tests/test_contracts.py -q`
Expected: PASS(全部,含新增 3 個)

- [ ] **Step 3: Commit**

```bash
git add tests/test_contracts.py
git commit -m "test(contracts): pin slide_deck/report SDK 簽名與 enum

擋上游 API 漂移:generate_slide_deck/generate_report/generate_study_guide/
download_slide_deck/download_report 簽名 + SlideDeck/Report enum 值。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: enums — slide/report 字串→enum 映射

**Files:**
- Modify: `notebooklm_mcp/enums.py`
- Test: `tests/test_enums.py`

**Interfaces:**
- Produces:
  - `to_slide_format(value: str | None) -> SlideDeckFormat | None`（`"detailed"|"presenter"`)
  - `to_slide_length(value: str | None) -> SlideDeckLength | None`（`"default"|"short"`）
  - `to_report_format(value: str | None) -> ReportFormat | None`（`"study_guide"|"briefing_doc"|"blog_post"`）
  - 三者對非法值 raise `ValueError`,對 `None` 回 `None`。

- [ ] **Step 1: 寫失敗測試** — append 到 `tests/test_enums.py`:

```python
def test_to_slide_format_and_length():
    from notebooklm.types import SlideDeckFormat, SlideDeckLength
    from notebooklm_mcp.enums import to_slide_format, to_slide_length

    assert to_slide_format("detailed") == SlideDeckFormat.DETAILED_DECK
    assert to_slide_format("presenter") == SlideDeckFormat.PRESENTER_SLIDES
    assert to_slide_format(None) is None
    assert to_slide_length("short") == SlideDeckLength.SHORT
    with pytest.raises(ValueError, match="slide format"):
        to_slide_format("bogus")


def test_to_report_format():
    from notebooklm.types import ReportFormat
    from notebooklm_mcp.enums import to_report_format

    assert to_report_format("study_guide") == ReportFormat.STUDY_GUIDE
    assert to_report_format("briefing_doc") == ReportFormat.BRIEFING_DOC
    assert to_report_format("blog_post") == ReportFormat.BLOG_POST
    assert to_report_format(None) is None
    with pytest.raises(ValueError, match="report format"):
        to_report_format("bogus")
```

（`tests/test_enums.py` 若尚未 import pytest,在檔首加 `import pytest`。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_enums.py -q`
Expected: FAIL（`ImportError: cannot import name 'to_slide_format'`）

- [ ] **Step 3: 最小實作** — 在 `notebooklm_mcp/enums.py` 檔尾加(注意 import 來自 `notebooklm.types`,與檔首的 `notebooklm.rpc.types` 是不同模組):

```python
from notebooklm.types import SlideDeckFormat, SlideDeckLength, ReportFormat

_SLIDE_FORMAT = {
    "detailed": SlideDeckFormat.DETAILED_DECK,
    "presenter": SlideDeckFormat.PRESENTER_SLIDES,
}
_SLIDE_LENGTH = {
    "default": SlideDeckLength.DEFAULT,
    "short": SlideDeckLength.SHORT,
}
_REPORT_FORMAT = {
    "study_guide": ReportFormat.STUDY_GUIDE,
    "briefing_doc": ReportFormat.BRIEFING_DOC,
    "blog_post": ReportFormat.BLOG_POST,
}


def to_slide_format(value: str | None) -> SlideDeckFormat | None:
    if value is None:
        return None
    try:
        return _SLIDE_FORMAT[value]
    except KeyError:
        raise ValueError(f"Invalid slide format {value!r}. Choose from: {', '.join(_SLIDE_FORMAT)}") from None


def to_slide_length(value: str | None) -> SlideDeckLength | None:
    if value is None:
        return None
    try:
        return _SLIDE_LENGTH[value]
    except KeyError:
        raise ValueError(f"Invalid slide length {value!r}. Choose from: {', '.join(_SLIDE_LENGTH)}") from None


def to_report_format(value: str | None) -> ReportFormat | None:
    if value is None:
        return None
    try:
        return _REPORT_FORMAT[value]
    except KeyError:
        raise ValueError(f"Invalid report format {value!r}. Choose from: {', '.join(_REPORT_FORMAT)}") from None
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_enums.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/enums.py tests/test_enums.py
git commit -m "feat(enums): slide/report 字串→SDK enum 映射

generate_slides/generate_report 工具要把使用者友善字串轉 SDK enum;
沿用 to_audio_* 風格,非法值 fail-fast。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: layout.attachment_filename — 附件 content-addressed 檔名

**Files:**
- Modify: `notebooklm_mcp/publish/layout.py`
- Test: `tests/test_publish_layout.py`

**Interfaces:**
- Produces: `attachment_filename(episode_n: int, hash8: str, ext: str) -> str` → `"EP{n:02d}-{hash8}.{ext}"`

- [ ] **Step 1: 寫失敗測試** — append 到 `tests/test_publish_layout.py`:

```python
def test_attachment_filename():
    from notebooklm_mcp.publish.layout import attachment_filename

    assert attachment_filename(1, "deadbeef", "pdf") == "EP01-deadbeef.pdf"
    assert attachment_filename(12, "0a1b2c3d", "html") == "EP12-0a1b2c3d.html"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_publish_layout.py::test_attachment_filename -q`
Expected: FAIL（`ImportError`）

- [ ] **Step 3: 最小實作** — 在 `notebooklm_mcp/publish/layout.py` 檔尾加:

```python
def attachment_filename(episode_n: int, hash8: str, ext: str) -> str:
    """單集附件(pdf/html)的 content-addressed 檔名,與 media_filename 同族。"""
    return f"EP{episode_n:02d}-{hash8}.{ext}"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_publish_layout.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/publish/layout.py tests/test_publish_layout.py
git commit -m "feat(layout): attachment_filename 附件內容定址檔名

publish 要 host 每集 pdf/html,沿用 mp3 的 EP{n}-<hash8>.ext 規則。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: notes_html — Markdown 研讀講義渲染成 HTML

**Files:**
- Create: `notebooklm_mcp/publish/notes_html.py`
- Modify: `pyproject.toml`（dependencies 加 `markdown`）
- Test: `tests/test_notes_html.py`

**Interfaces:**
- Produces: `render_report_html(markdown_text: str, title: str) -> str` — 回一個自包含 HTML 字串(inline CSS、無外部資源、深淺色自適應)。

- [ ] **Step 1: 加依賴** — 編輯 `pyproject.toml`,在 `dependencies` 陣列加一行 `"markdown>=3.5"`,然後同步環境:

Run: `uv pip install -e ".[dev]"`
Expected: 安裝 `markdown` 成功

- [ ] **Step 2: 寫失敗測試** — 新檔 `tests/test_notes_html.py`:

```python
from notebooklm_mcp.publish.notes_html import render_report_html


def test_renders_markdown_structure():
    md = "# 標題\n\n- 一\n- 二\n\n```py\nx = 1\n```"
    html = render_report_html(md, "EP01 測試")
    assert "<h1" in html and "標題" in html
    assert "<ul" in html and "<li>一</li>" in html
    assert "<code>" in html or "<pre>" in html


def test_self_contained_and_titled():
    html = render_report_html("內文", "我的講義")
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "我的講義" in html                 # title 有帶入
    assert "http://" not in html and "https://" not in html  # 無外部資源
    assert "<style" in html                    # CSS inline
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/test_notes_html.py -q`
Expected: FAIL（`ModuleNotFoundError: notebooklm_mcp.publish.notes_html`）

- [ ] **Step 4: 最小實作** — 新檔 `notebooklm_mcp/publish/notes_html.py`:

```python
"""把 NotebookLM report/study-guide 的 Markdown 渲染成自包含、手機好讀的 HTML。
純函式、可離線測;inline CSS,無外部資源(才能被 uploader 白名單當單一 .html 檔服務)。"""
from __future__ import annotations

from html import escape

import markdown as _md

_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; background: #ffffff; color: #1a1a1a;
       font: 17px/1.7 -apple-system, "Noto Sans TC", "PingFang TC", sans-serif; }
main { max-width: 44rem; margin: 0 auto; padding: 2rem 1.2rem 4rem; }
h1 { font-size: 1.8rem; line-height: 1.3; }
h2 { margin-top: 2rem; border-bottom: 1px solid #8883; padding-bottom: .3rem; }
code { background: #8882; padding: .1em .35em; border-radius: 4px; }
pre { background: #8882; padding: 1rem; border-radius: 8px; overflow-x: auto; }
pre code { background: none; padding: 0; }
a { color: #2563eb; }
@media (prefers-color-scheme: dark) {
  body { background: #0f1115; color: #e6e6e6; }
  a { color: #7dd3fc; }
}
"""


def render_report_html(markdown_text: str, title: str) -> str:
    body = _md.markdown(markdown_text, extensions=["extra", "sane_lists"])
    return (
        "<!doctype html>\n"
        '<html lang="zh-Hant"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>{_CSS}</style></head>\n"
        f"<body><main>\n<h1>{escape(title)}</h1>\n{body}\n</main></body></html>\n"
    )
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_notes_html.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml notebooklm_mcp/publish/notes_html.py tests/test_notes_html.py
git commit -m "feat(publish): notes_html 把研讀講義 Markdown 渲染成 HTML

使用者選『研讀講義渲染成好讀網頁』;加 markdown 依賴(該用現成的別自刻),
輸出自包含 inline-CSS HTML,才能被 uploader 當單一 .html 服務。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: generate_slides 工具

**Files:**
- Create: `notebooklm_mcp/tools_artifacts.py`
- Modify: `notebooklm_mcp/app.py:41`（tool 註冊 import 加 `tools_artifacts`）
- Modify: `tests/conftest.py`（`FakeArtifacts` 加 slide/report 假方法)
- Test: `tests/test_tools_artifacts.py`

**Interfaces:**
- Consumes: `enums.to_slide_format`/`to_slide_length`(Task 2)、`runtime.get_client`、`_status.ensure_started`/`ensure_completed`、`languages.resolve_language`
- Produces:
  - `_load_ep_and_write(manifest_path: str, episode_n: int, **fields) -> dict`
  - `generate_slides(notebook_id, manifest_path, episode_n, source_ids=None, language=None, instructions=None, slide_format="detailed", slide_length="default", wait_timeout=1800.0) -> dict`（回 `{"episode","slides_pdf_path","artifact_id"}`)

- [ ] **Step 1: 擴充 fake client** — 在 `tests/conftest.py` 的 `FakeArtifacts` class 內(接在 `download_audio` 後)加:

```python
    async def generate_slide_deck(self, notebook_id, source_ids=None, language="en",
                                  instructions=None, slide_format=None, slide_length=None):
        self.calls.append(("generate_slide_deck", dict(
            notebook_id=notebook_id, source_ids=source_ids, language=language,
            instructions=instructions, slide_format=slide_format, slide_length=slide_length)))
        if self.fail_generate:
            return type("S", (), {"task_id": "", "is_failed": True, "status": "failed", "error": "sim"})()
        return type("S", (), {"task_id": "slide-task", "is_failed": False})()

    async def download_slide_deck(self, notebook_id, output_path, artifact_id=None, output_format="pdf"):
        self.calls.append(("download_slide_deck", dict(output_path=output_path,
                          artifact_id=artifact_id, output_format=output_format)))
        with open(output_path, "wb") as f:      # 落一個非空檔,讓 publish 的存在性檢查過
            f.write(b"%PDF-1.4 fake")
        return output_path

    async def generate_report(self, notebook_id, report_format=None, source_ids=None,
                              language="en", custom_prompt=None, extra_instructions=None):
        self.calls.append(("generate_report", dict(
            notebook_id=notebook_id, report_format=report_format, source_ids=source_ids,
            language=language, extra_instructions=extra_instructions)))
        if self.fail_generate:
            return type("S", (), {"task_id": "", "is_failed": True, "status": "failed", "error": "sim"})()
        return type("S", (), {"task_id": "report-task", "is_failed": False})()

    async def download_report(self, notebook_id, output_path, artifact_id=None):
        self.calls.append(("download_report", dict(output_path=output_path, artifact_id=artifact_id)))
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("# 假講義\n\n- 重點一\n")
        return output_path
```

- [ ] **Step 2: 寫失敗測試** — 新檔 `tests/test_tools_artifacts.py`:

```python
import json

import pytest

from notebooklm_mcp import tools_artifacts as a


def _manifest(tmp_path, episodes):
    p = tmp_path / "series_manifest.json"
    p.write_text(json.dumps({"notebook_id": "nb-1", "episodes": episodes}, ensure_ascii=False),
                 encoding="utf-8")
    return str(p)


async def test_generate_slides_downloads_and_writes_manifest(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    res = await a.generate_slides("nb-1", m, 1, source_ids=["src-1"], slide_format="detailed")

    # 呼叫了對的 SDK 方法,帶對的 enum
    from notebooklm.types import SlideDeckFormat
    gen = next(c[1] for c in fake_client.artifacts.calls if c[0] == "generate_slide_deck")
    assert gen["source_ids"] == ["src-1"]
    assert gen["slide_format"] == SlideDeckFormat.DETAILED_DECK
    assert gen["language"] == "zh_Hant"                       # resolve_language 預設

    # 下載到 manifest 同目錄的預期檔名
    assert res["slides_pdf_path"].endswith("ep01-slides.pdf")
    # 路徑回寫進 manifest
    data = json.loads(open(m, encoding="utf-8").read())
    assert data["episodes"][0]["slides_pdf_path"] == res["slides_pdf_path"]


async def test_generate_slides_unknown_episode_errors(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    with pytest.raises(ValueError, match="episode 2 not found"):
        await a.generate_slides("nb-1", m, 2)
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/test_tools_artifacts.py -q`
Expected: FAIL（`ModuleNotFoundError: notebooklm_mcp.tools_artifacts`）

- [ ] **Step 4: 最小實作** — 新檔 `notebooklm_mcp/tools_artifacts.py`:

```python
"""按需生成單集附加產物(簡報 slide deck / 研讀 report),路徑回寫 series_manifest.json。
不碰音檔迴圈;想幫哪集加就對哪集跑。產物只餵傳入的 source_ids(不傳則 SDK 用全部來源)。"""
from __future__ import annotations

import json
import os

from . import runtime
from ._status import ensure_completed, ensure_started
from .enums import to_report_format, to_slide_format, to_slide_length
from .languages import resolve_language
from .app import mcp


def _load_ep_and_write(manifest_path: str, episode_n: int, **fields) -> dict:
    """讀 manifest、找 episode==episode_n 的集、merge 欄位、寫回(沿用直接 json.dump)。"""
    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)
    ep = next(
        (e for e in data.get("episodes", []) if isinstance(e, dict) and e.get("episode") == episode_n),
        None,
    )
    if ep is None:
        raise ValueError(f"episode {episode_n} not found in manifest {manifest_path}")
    ep.update(fields)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return ep


@mcp.tool()
async def generate_slides(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    source_ids: list[str] | None = None,
    language: str | None = None,
    instructions: str | None = None,
    slide_format: str | None = "detailed",
    slide_length: str | None = "default",
    wait_timeout: float = 1800.0,
) -> dict:
    """生成該集簡報並下載 PDF,路徑回寫 manifest 的 slides_pdf_path。"""
    client = runtime.get_client()
    status = await client.artifacts.generate_slide_deck(
        notebook_id,
        source_ids=source_ids,
        language=resolve_language(language),
        instructions=instructions,
        slide_format=to_slide_format(slide_format),
        slide_length=to_slide_length(slide_length),
    )
    artifact_id = ensure_started(status)
    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    out = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), f"ep{episode_n:02d}-slides.pdf")
    await client.artifacts.download_slide_deck(notebook_id, out, artifact_id=artifact_id, output_format="pdf")
    _load_ep_and_write(manifest_path, episode_n, slides_pdf_path=out)
    return {"episode": episode_n, "slides_pdf_path": out, "artifact_id": artifact_id}
```

- [ ] **Step 5: 註冊工具** — 編輯 `notebooklm_mcp/app.py:41`,把:

```python
from . import tools_basic, tools_podcast, tools_publish  # noqa: E402,F401
```
改成:
```python
from . import tools_artifacts, tools_basic, tools_podcast, tools_publish  # noqa: E402,F401
```

- [ ] **Step 6: 跑測試確認通過**

Run: `uv run pytest tests/test_tools_artifacts.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add notebooklm_mcp/tools_artifacts.py notebooklm_mcp/app.py tests/conftest.py tests/test_tools_artifacts.py
git commit -m "feat(artifacts): generate_slides 工具生簡報 PDF 並回寫 manifest

按需生成單集簡報:SDK generate_slide_deck→wait→download PDF,路徑寫進
series_manifest 該集 slides_pdf_path,供 publish_series host。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: generate_report 工具

**Files:**
- Modify: `notebooklm_mcp/tools_artifacts.py`（加 `generate_report`）
- Test: `tests/test_tools_artifacts.py`（加案例）

**Interfaces:**
- Consumes: Task 5 的 `_load_ep_and_write`、`enums.to_report_format`
- Produces: `generate_report(notebook_id, manifest_path, episode_n, report_format="study_guide", source_ids=None, language=None, extra_instructions=None, wait_timeout=1800.0) -> dict`（回 `{"episode","report_md_path","report_format","artifact_id"}`)

- [ ] **Step 1: 寫失敗測試** — append 到 `tests/test_tools_artifacts.py`:

```python
async def test_generate_report_downloads_md_and_writes_manifest(fake_client, tmp_path):
    m = _manifest(tmp_path, [{"episode": 1, "title": "EP01"}])
    res = await a.generate_report("nb-1", m, 1, report_format="study_guide", source_ids=["src-1"])

    from notebooklm.types import ReportFormat
    gen = next(c[1] for c in fake_client.artifacts.calls if c[0] == "generate_report")
    assert gen["report_format"] == ReportFormat.STUDY_GUIDE
    assert gen["language"] == "zh_Hant"

    assert res["report_md_path"].endswith("ep01-report.md")
    assert res["report_format"] == "study_guide"
    data = json.loads(open(m, encoding="utf-8").read())
    assert data["episodes"][0]["report_md_path"] == res["report_md_path"]
    assert data["episodes"][0]["report_format"] == "study_guide"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_tools_artifacts.py::test_generate_report_downloads_md_and_writes_manifest -q`
Expected: FAIL（`AttributeError: module ... has no attribute 'generate_report'`)

- [ ] **Step 3: 最小實作** — 在 `notebooklm_mcp/tools_artifacts.py` 檔尾加:

```python
@mcp.tool()
async def generate_report(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    report_format: str = "study_guide",
    source_ids: list[str] | None = None,
    language: str | None = None,
    extra_instructions: str | None = None,
    wait_timeout: float = 1800.0,
) -> dict:
    """生成該集研讀文件(預設 study_guide)並下載 Markdown,路徑回寫 report_md_path。"""
    client = runtime.get_client()
    status = await client.artifacts.generate_report(
        notebook_id,
        report_format=to_report_format(report_format),
        source_ids=source_ids,
        language=resolve_language(language),
        extra_instructions=extra_instructions,
    )
    artifact_id = ensure_started(status)
    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    out = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), f"ep{episode_n:02d}-report.md")
    await client.artifacts.download_report(notebook_id, out, artifact_id=artifact_id)
    _load_ep_and_write(manifest_path, episode_n, report_md_path=out, report_format=report_format)
    return {"episode": episode_n, "report_md_path": out, "report_format": report_format, "artifact_id": artifact_id}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_tools_artifacts.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/tools_artifacts.py tests/test_tools_artifacts.py
git commit -m "feat(artifacts): generate_report 工具生研讀講義 Markdown 並回寫 manifest

按需生成單集研讀文件(study_guide/briefing_doc/blog_post),路徑寫進
report_md_path + report_format,供 publish_series 渲染 HTML 後 host。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: publish_series host 附件 + 單集簡介附連結

**Files:**
- Modify: `notebooklm_mcp/tools_publish.py`（episode 迴圈 + import）
- Test: `tests/test_publish_tools.py`（加案例)

**Interfaces:**
- Consumes: `publish/layout.attachment_filename`(Task 3)、`publish/notes_html.render_report_html`(Task 4)、manifest 集的 `slides_pdf_path`/`report_md_path` 欄位(Task 5/6)
- Produces: publish 後 show.json 該集 `description` = 原描述 + 附件連結區塊;pdf/html 檔以 `EP{n}-<hash8>.{ext}` PUT

- [ ] **Step 1: 寫失敗測試** — append 到 `tests/test_publish_tools.py`:

```python
async def test_attachments_hosted_and_linked(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    pdf = _write_mp3(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 x")   # _write_mp3 只是寫 bytes
    md = tmp_path / "ep01-report.md"
    md.write_text("# 講義\n\n- 重點", encoding="utf-8")
    manifest = _manifest(tmp_path, [{
        "episode": 1, "title": "第1集", "description": "本集重點。",
        "mp3_path": _write_mp3(tmp_path, "e1.mp3", b"a"),
        "slides_pdf_path": pdf, "report_md_path": str(md),
    }], "att.json")

    res = await _publish(manifest, artwork_png)
    names = [c["name"] for c in captured]
    assert any(n.startswith("EP01-") and n.endswith(".pdf") for n in names)
    assert any(n.startswith("EP01-") and n.endswith(".html") for n in names)

    show = json.loads(next(c["content"] for c in captured if c["name"] == "show.json"))
    desc = show["episodes"]["1"]["description"]
    assert desc.startswith("本集重點。")
    assert "本集簡報" in desc and ".pdf" in desc
    assert "研讀講義" in desc and ".html" in desc

    # PUT 上去的 html 是渲染後的(含 <h1>),不是原始 markdown
    html_put = next(c["content"] for c in captured if c["name"].endswith(".html") and c["name"].startswith("EP01-"))
    assert b"<h1" in html_put and "講義".encode() in html_put


async def test_missing_attachment_file_fails_fast(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    manifest = _manifest(tmp_path, [{
        "episode": 1, "title": "第1集",
        "mp3_path": _write_mp3(tmp_path, "e1b.mp3", b"a"),
        "slides_pdf_path": str(tmp_path / "does-not-exist.pdf"),
    }], "att_missing.json")
    with pytest.raises(ValueError, match="slides_pdf_path"):
        await _publish(manifest, artwork_png)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_publish_tools.py::test_attachments_hosted_and_linked -q`
Expected: FAIL（沒有 pdf/html PUT;description 無連結）

- [ ] **Step 3: 實作** — 編輯 `notebooklm_mcp/tools_publish.py`。

先在檔首 import 區(`from .publish.layout import media_filename` 那行)改為:
```python
from .publish.layout import attachment_filename, media_filename
from .publish import notes_html
```

再把 episode 迴圈裡「PUT mp3 + 組 new_eps」那段(現況 `for ep in manifest_eps:` 內、`new_eps[str(n)] = {...}` 一段)替換為下面版本。**保留** `_ensure_local_mp3` + mp3 PUT + `del mp3_bytes`,只在 mp3 之後、組 `new_eps` 之前插入附件處理,並用算好的 `description`:

```python
        for ep in manifest_eps:                                    # 1) media: mp3
            n = int(ep["episode"])
            local = await _ensure_local_mp3(notebook_id, ep)
            with open(local, "rb") as f:
                mp3_bytes = f.read()
            hash8 = hashlib.sha256(mp3_bytes).hexdigest()[:8]
            mfile = media_filename(n, hash8)
            await _put(client, upload_url, token, upload_token, mfile, mp3_bytes)
            del mp3_bytes

            # 1b) media: 選填附件(簡報 PDF / 研讀講義 HTML),content-addressed,
            #     公開 URL append 到單集 description。缺檔 fail-fast(不 re-download)。
            base_pub = base_url.rstrip("/")
            desc = (ep.get("description") or "").strip() or ep["title"]
            links: list[str] = []

            spath = ep.get("slides_pdf_path")
            if spath:
                if not (os.path.exists(spath) and os.path.getsize(spath) > 0):
                    raise ValueError(f"episode {n}: slides_pdf_path missing file: {spath}")
                with open(spath, "rb") as f:
                    pdf_bytes = f.read()
                pfile = attachment_filename(n, hashlib.sha256(pdf_bytes).hexdigest()[:8], "pdf")
                await _put(client, upload_url, token, upload_token, pfile, pdf_bytes)
                links.append(f"📄 本集簡報:{base_pub}/feeds/{token}/{pfile}")
                del pdf_bytes

            rpath = ep.get("report_md_path")
            if rpath:
                if not (os.path.exists(rpath) and os.path.getsize(rpath) > 0):
                    raise ValueError(f"episode {n}: report_md_path missing file: {rpath}")
                with open(rpath, encoding="utf-8") as f:
                    html_bytes = notes_html.render_report_html(f.read(), ep["title"]).encode("utf-8")
                hfile = attachment_filename(n, hashlib.sha256(html_bytes).hexdigest()[:8], "html")
                await _put(client, upload_url, token, upload_token, hfile, html_bytes)
                links.append(f"📖 研讀講義:{base_pub}/feeds/{token}/{hfile}")

            if links:
                desc = desc + "\n\n" + "\n".join(links)

            new_eps[str(n)] = {
                "title": ep["title"],
                "description": desc,
                "guid": identity.episode_guid(show_id, n),
                "pub_date": ep.get("published_at") or _fallback_pub_date(n),
                "media_file": mfile,
                "length": os.path.getsize(local),   # mp3_bytes 已 del,用檔案大小(同值)
            }
            published.append({
                "n": n, "title": ep["title"], "guid": new_eps[str(n)]["guid"],
                "url": f"{base_pub}/feeds/{token}/{mfile}",
            })
```

> 註:`length` 原本用 `len(mp3_bytes)`,但這裡 `mp3_bytes` 為省記憶體已 `del`;改用 `os.path.getsize(local)`(同值,mp3 已在本機)。

- [ ] **Step 4: 跑測試確認通過(含既有 publish 測試不回歸)**

Run: `uv run pytest tests/test_publish_tools.py -q`
Expected: PASS(新 2 案 + 既有全過;既有無附件 manifest 的集 description 行為不變)

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/tools_publish.py tests/test_publish_tools.py
git commit -m "feat(publish): host 每集簡報 PDF/研讀 HTML 並在單集簡介附連結

manifest 帶 slides_pdf_path/report_md_path 的集:PDF 直傳、Markdown 渲染成
HTML 再傳(content-addressed),公開 URL append 到單集 <description>。
缺檔 fail-fast。length 改用 os.path.getsize(mp3_bytes 已釋放)。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: uploader 白名單放寬收 pdf/html + 部署(podcast-feed-host repo)

**Files:**
- Modify: `podcast-feed-host/uploader/server.py`（`_NAME` regex,約 line 29）
- Test: `podcast-feed-host/uploader/test_server.py`

**Interfaces:**
- Produces: uploader 接受 `EP{n:02d}-<hash8>.(mp3|pdf|html)`;其他非白名單仍 404

- [ ] **Step 1: 寫失敗測試** — 在 `podcast-feed-host/uploader/test_server.py` 加(沿用該檔既有 PUT 測試風格;`<TOKEN>` 用該檔既有的合法 token fixture/常數):

```python
def test_put_accepts_pdf_and_html(tmp_data, client, auth):
    for name in ("EP01-deadbeef.pdf", "EP01-0a1b2c3d.html"):
        r = client.put(f"/feeds/{VALID_TOKEN}/{name}", data=b"x", headers=auth)
        assert r.status_code == 201, name


def test_put_still_rejects_non_whitelisted(tmp_data, client, auth):
    for name in ("notes.txt", "evil.pdf", "EP1-deadbeef.pdf"):   # 錯副檔名/錯 EP 格式
        r = client.put(f"/feeds/{VALID_TOKEN}/{name}", data=b"x", headers=auth)
        assert r.status_code == 404, name
```

> 執行者注意:先讀 `test_server.py` 現有測試,沿用它既有的 fixture 名(client/auth/token)與 helper;上面的 `tmp_data/client/auth/VALID_TOKEN` 若與現況命名不同,對齊現況即可 —— 斷言邏輯不變(pdf/html→201,txt/錯格式→404)。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd podcast-feed-host/uploader && python -m pytest test_server.py -q`（依該 repo README 的測試指令)
Expected: FAIL（pdf/html 目前回 404)

- [ ] **Step 3: 實作** — 編輯 `podcast-feed-host/uploader/server.py` 的 `_NAME`(約 line 29):

```python
_NAME = r"(?:feed\.xml|index\.html|show\.json|artwork\.(?:png|jpg)|EP\d{2}-[0-9a-f]{8}\.(?:mp3|pdf|html))"
```
（原本結尾是 `EP\d{2}-[0-9a-f]{8}\.mp3` —— 只把 `\.mp3` 換成 `\.(?:mp3|pdf|html)`。）

- [ ] **Step 4: 跑測試確認通過**

Run: `cd podcast-feed-host/uploader && python -m pytest test_server.py -q`
Expected: PASS

- [ ] **Step 5: Commit(在 podcast-feed-host repo)**

```bash
cd podcast-feed-host
git add uploader/server.py uploader/test_server.py
git commit -m "feat(uploader): 白名單放寬收 EP<n>-<hash>.pdf/.html

單集要附簡報 PDF 與研讀講義 HTML;沿用 content-addressed 檔名,
只擴副檔名,非白名單仍 404(信任邊界不鬆)。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 6: 部署 NAS uploader(對外寫入服務,需使用者確認)**

在 NAS(192.0.2.10)專案目錄:
```bash
docker compose build uploader && docker compose up -d uploader
```
驗證:`curl -sI http://192.0.2.10:8086/healthz` 應含 `X-Podcast-Uploader: 1`。
Caddy 唯讀端不必動(服務同目錄,新副檔名自然可讀)。

> **順序鐵律**:先部署此白名單,再跑帶附件的 `publish_series`,否則附件 PUT 會 404。

---

## 收尾(全部任務後)

- [ ] 全套測試綠:`uv run pytest -q`(notebooklm-skill)
- [ ] 更新 `SKILL.md` §Publish:記兩個新工具 `generate_slides`/`generate_report` 與「附件連結進單集簡介」的流程。
- [ ] 更新 `AGENTS.md`:MCP 工具清單加兩工具;Gotchas 記「v1 不自動排除音檔來源、要聚焦原文請明確傳 source_ids」「附件缺檔 fail-fast」「先部署 uploader 白名單再發布」。
- [ ] 用實際 notebook(e2e-test-01 那本或新的)跑一次真流程驗收:generate_slides → generate_report → publish_series → curl 附件 URL(200 pdf/html)+ 看 feed 單集 description 帶連結。

---

## Self-Review

- **Spec 覆蓋**:①兩工具→Task 5/6;②manifest 兩欄位→Task 5/6 寫入 + Task 7 讀取;③publish host+連結→Task 7;④md→HTML→Task 4;⑤uploader 白名單+部署→Task 8;⑥contract→Task 1;⑦enums→Task 2;⑧layout 檔名→Task 3。§6 測試面全數對應。無遺漏。
- **Placeholder**:無 TBD/TODO;每個 code step 附完整程式。Task 8 的 fixture 名明確標示「對齊現況」屬跨 repo 必要彈性,斷言邏輯給死。
- **型別一致**:`slides_pdf_path`/`report_md_path`/`report_format` 三欄位名在 Task 5/6(寫)與 Task 7(讀)一致;`attachment_filename(n, hash8, ext)` 在 Task 3 定義、Task 7 使用一致;`render_report_html(md, title)` 在 Task 4 定義、Task 7 使用一致。
