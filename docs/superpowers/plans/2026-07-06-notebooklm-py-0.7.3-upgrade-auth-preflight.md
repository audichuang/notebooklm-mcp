# notebooklm-py 0.7.3 升級 + auth 預檢 fail-early 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **修訂紀錄**:v2 — 依 Codex(GPT-5.5)審查修訂:test import 別名(`p.`/`t.`)、`add_file(title=)`
> 實為內部兩步且吞錯 → podcast 流程維持顯式 rename、全 rename 呼叫點顯式 `return_object=False`、
> uv.lock 納入、source-add 參數 keyword-only 契約、預檢改在本地驗證之後、smoke 加 pipefail、
> `_research/` 唯讀措辭、版本升 0.2.0 + CI wheel glob。
> v3 — Codex 二審(verdict: execute with fixes)修訂:fake 簽名精確鏡射 0.7.3 的 `*` 位置
>(`mime_type` 是 positional;`return_object` 是 keyword-only)、補 add_url/add_text/rename 的
> fake 全套、`podcast_series` 的 probe 移到 manifest 載入**之後**(壞 manifest 的 ValueError
> 不得被認證錯誤蓋掉,test_resume.py 已有既有保護)、補 `artifact_rename` 的 fire-and-forget
> 測試、rollout 補 `docs/mcp-setup.md` 與 `README.md` 舊 pin 字樣。Codex 已同意 finding 2 的
> 反提案(podcast 維持顯式 rename)。

**Goal:** 把 notebooklm-py 從 0.4.1 升到最新 stable 0.7.3(跨 0.5/0.6/0.7 三個 breaking 版本),
對齊 0.7.3 的行為語意(`return_object`、keyword-only、`add_file(title=)` 的非 fail-loud 陷阱),
新增 `auth_check` 工具 + 長跑工具的認證預檢(fail-fast),並更新 AGENTS.md gotchas、
「上游情報站」指引與版本/rollout 文件。

**Architecture:** 本 repo 是 notebooklm-py 之上的薄 wrapper(所有 NotebookLM 協定委派上游)。
升級的防護網是 `tests/test_contracts.py`(以 `inspect.signature` 鎖實裝 API)+ 18 個離線 mock
測試檔;升級後以 doppler 注入真認證做一次 smoke 驗證。auth 預檢採「輕量真 RPC」
(`notebooks.list()`)——這是 jacob-bd/notebooklm-mcp-cli issue #250 的教訓:homepage probe 會
false-positive,必須打真 RPC。

**Tech Stack:** Python 3.12(唯一支援版本)、uv(pyproject + `uv.lock`)、pytest(離線 mock)、
notebooklm-py 0.7.3、FastMCP。

## Global Constraints

- Python **3.12 only**:`requires-python = ">=3.12,<3.13"` 不變(3.14 觸發 SDK inspect bug)。
- pin 格式:`notebooklm-py>=0.7.3,<0.8`,`dependencies` 與 `login` extra **兩處都要改**,
  且 **`uv.lock` 必須重新 lock 並一起 commit**(repo 用 `uv run`,它吃 lock 檔)。
- 全程繁體中文註解/文件(repo convention)。
- TDD:測試先紅再綠;每任務一 commit(使用者已授權本計畫的逐 task commit;**不 push、不打 tag**——tag 由使用者驗收後自行建立)。
- 絕不碰 `_research/`(唯讀參考 clone;**連 `git pull` 都不行**,更新 clone 需使用者自己做)。
- 新增 MCP 工具(`auth_check`)必須同步 skill repo `/home/user/research/audi-skill/notebooklm`
  (SKILL.md 工具表 + references/tool-reference.md),`uv run python scripts/check_skill_sync.py` 必須 exit 0。
- 契約以「**實裝版本**」為準,不信 `_research/notebooklm-py` HEAD(那是 0.8.0 開發版)。
- 測試檔的既有 import 別名:`tests/test_tools_podcast.py` 用 `from notebooklm_mcp import tools_podcast as p`、
  `tests/test_tools_basic.py` 用 `from notebooklm_mcp import tools_basic as t`——新測試碼一律沿用 `p.` / `t.`。

## 0.7.3 實裝 API 事實表(拋棄式 venv 實測 + Codex 讀 0.7.3 原始碼交叉驗證)

| API | 0.4.1(現況) | 0.7.3(目標) | 對我們的影響 |
|---|---|---|---|
| `from_storage` | coroutine,必須 `await` | **同步函式**,回傳 `_FromStorageContext`(可直接 `async with`;`await` 走 deprecation 相容路徑) | app.py 改 no-await 慣用法 |
| `from_storage` 參數 | `path,timeout,profile,keepalive,keepalive_min_interval` | 另加 `rate_limit_max_retries, server_error_max_retries, limits, max_concurrent_uploads, max_concurrent_rpcs, upload_timeout, on_rpc_event, chat_timeout`(全 optional) | 無;`keepalive=600` 照用 |
| `sources.add_url/add_text/add_file` 尾端參數 | 一般參數 | **0.7.0 起 keyword-only**(`*, wait, wait_timeout, …, title`) | 我們已全 keyword 呼叫;contract 加 KEYWORD_ONLY 斷言、fake 加 `*` 鏡射 |
| `sources.add_file(title=)` | 無 `title` | 有,**但內部仍是 add→rename 兩步**,且**改名失敗只 log 不 raise**、回傳舊 title 的 Source(`_source/upload.py`) | **podcast 流程維持顯式兩步 rename(fail-loud),不改用 title=**;僅 `source_add_file` 工具加 title= 並以回傳 title 後檢 fail-loud |
| `sources.add_text` | `…, wait, wait_timeout` | 尾端加 `idempotent` | 無(尾端預設) |
| `sources.get_fulltext` | 3 參數 | 尾端加 `output_format` | 無 |
| `sources.rename` / `artifacts.rename` | 無 `return_object` | 有,**預設 True:改名後再抓一次全量清單驗證,可能 raise not-found** | **所有 rename 呼叫點顯式傳 `return_object=False`**(保留 0.4.1 的 fire-and-forget 語意,不多打 RPC、不引入新失敗模式) |
| `wait_for_completion` | `…, poll_interval, max_not_found, min_not_found_window` | **`poll_interval` 移除**,尾端加 `on_status_change` | 無(已 grep 確認所有呼叫點只用 `timeout=`) |
| `download_audio/report/slide_deck` | 4-5 參數 | 尾端加 `artifacts_data` | 無 |
| `GenerationStatus` | 無 `artifact_id`(task_id 即 artifact id) | **不變** | gotcha 續留 |
| enums(AudioFormat/Length、SlideDeck*、ReportFormat) | — | **值全部不變** | 無 |
| 例外 | — | 新增 typed 階層(`AuthError`、`NotFoundError` umbrella 等) | 我們沒 catch 上游例外;`_status.py` duck-typed guards 照舊 |
| `notebooklm-py[browser]` extra | 存在 | **存在**(Codex 已驗) | login extra pin 直接改版本即可 |

---

### Task 1: pin 升 0.7.3(含 uv.lock)+ from_storage 慣用法 + contract 重鎖

**Files:**
- Modify: `pyproject.toml`(pin ×2)
- Modify: `uv.lock`(`uv lock` 重新生成)
- Modify: `notebooklm_mcp/app.py:26-36`(lifespan 慣用法)
- Modify: `tests/test_server_lifespan.py`(fake 鏡射 0.7.3 語意)
- Modify: `tests/test_contracts.py`(簽名斷言更新 + keyword-only 斷言)

**Interfaces:**
- Consumes: 無(首個 task)
- Produces: 實裝 notebooklm-py==0.7.3 的 venv 與 lock;`app._lifespan` 改用
  `async with NotebookLMClient.from_storage(keepalive=600) as client:`(no-await);後續 task 在此之上開發。

- [ ] **Step 1: 改寫 lifespan 回歸測試,鏡射 0.7.3 語意(先紅)**

`tests/test_server_lifespan.py` 把 `_fake_from_storage` 從「coroutine 回傳 CM」改成「**同步**函式回傳 CM」,並鎖 `keepalive=600` 有被傳入;測試改名。模組 docstring 第 1 點同步改寫:

```python
"""Regressions for the server app:

1. notebooklm-py 0.7.x from_storage() 是同步函式,回傳可直接 async with 的
   context(_FromStorageContext)。lifespan 必須用 no-await 慣用法,且必須帶
   keepalive=600(session 內背景 RotateCookies;掉了會讓長生成中途認證死)。

2. The canonical `mcp` must actually expose the tools. They were registered on a
   different instance than the one served when launched via `python -m
   notebooklm_mcp.server` (the __main__ double-import trap) — the MCP came up
   with ZERO tools. Tools now live on `notebooklm_mcp.app.mcp`; assert they're there.
"""
```

```python
def _fake_from_storage(*args, **kwargs):
    # 鏡射 0.7.3:同步函式,回傳可直接 async with 的 context。
    # lifespan 掉了 keepalive=600 這裡就紅(它是長生成不中途死的關鍵)。
    assert kwargs.get("keepalive") == 600
    return _FakeClientCM()


async def test_lifespan_enters_from_storage_context(monkeypatch):
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage)
    async with app._lifespan(app.mcp):
        # Inside the lifespan the client must be set (proves the CM entered).
        assert isinstance(runtime.get_client(), _FakeClientCM)
    # After exit the holder is cleared.
    with pytest.raises(RuntimeError):
        runtime.get_client()
```

(`_FakeClientCM` 不變;刪掉舊的 `async def _fake_from_storage` 與 `test_lifespan_awaits_from_storage`。)

- [ ] **Step 2: 跑該測試,確認紅**

Run: `uv run pytest tests/test_server_lifespan.py -v`
Expected: `test_lifespan_enters_from_storage_context` **FAIL**(app.py 的 `await` 施加在非 awaitable 的 `_FakeClientCM` 上 → TypeError)。

- [ ] **Step 3: pin 升級 + 重新 lock + 安裝**

`pyproject.toml` 兩處:

```toml
    "notebooklm-py>=0.7.3,<0.8",
```

```toml
login = ["notebooklm-py[browser]>=0.7.3,<0.8"]
```

Run:
```bash
cd /home/user/research/audiskill/notebooklm-mcp
uv lock
uv pip install -e ".[dev]"
grep -n -A1 'name = "notebooklm-py"' uv.lock | grep version | head -1
.venv/bin/python -c "import importlib.metadata as m; print(m.version('notebooklm-py'))"
```
Expected: uv.lock 內 `version = "0.7.3"`;stdout `0.7.3`。

- [ ] **Step 4: 改 app.py lifespan 為 no-await 慣用法**

`notebooklm_mcp/app.py` 的 `_lifespan`:

```python
@contextlib.asynccontextmanager
async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
    # notebooklm-py 0.7.x:from_storage() 是同步函式,回傳可直接 async with 的
    # context(0.4.x「coroutine 必須 await」慣用法已走入歷史)。
    # keepalive=600 開啟 session 內背景 RotateCookies task(Google 自宣告的輪替
    # 週期即 600s):process-scoped、隨 server 生滅,讓跨小時長生成不因
    # __Secure-1PSIDTS 過期中途死。env-var 唯讀模式下只轉記憶體、不落盤,
    # 跨 session 的 cookie 老化仍靠 GUI 機重登 + sync-auth.sh。
    async with NotebookLMClient.from_storage(keepalive=600) as client:
        runtime.set_client(client)
        try:
            yield
        finally:
            runtime.set_client(None)
```

Run: `uv run pytest tests/test_server_lifespan.py -v`
Expected: PASS(3 個測試全綠)

- [ ] **Step 5: 跑全測,清點 contract 紅名單**

Run: `uv run pytest -q`
Expected: **FAIL 約 7-9 個**,全部在 `tests/test_contracts.py`:
`test_from_storage_is_awaitable_and_supports_keepalive`(iscoroutinefunction 變 False)、
`test_download_audio_arg_order`(+`artifacts_data`)、
`test_read_surface_signatures_and_fields`(get_fulltext +`output_format`)、
`test_add_file_accepts_mime_and_wait`(+`title`,`on_progress`)、
`test_rename_signatures_have_no_return_object`(rename 有 `return_object` 了)、
`test_wait_for_completion_full_signature`(`poll_interval` 移除、+`on_status_change`)、
`test_source_signatures_and_fields`(add_text +`idempotent`)、
`test_slide_deck_signatures` / `test_report_signatures`(download +`artifacts_data`)。
若紅名單與此不符,逐一以**實裝簽名**為準修正(`inspect.signature` 印出來對)。

- [ ] **Step 6: 更新 contract 斷言到 0.7.3 實裝真相(含 keyword-only)**

`tests/test_contracts.py` 逐一替換:

```python
def test_from_storage_is_sync_context_factory_with_keepalive():
    """0.7.3:from_storage 是同步函式,回傳可直接 `async with` 的 context
    (0.4.x「coroutine 必須 await」慣用法已走入歷史;app.py 用 no-await 寫法)。
    keepalive= 是 session 內背景 RotateCookies 的開關,lifespan 依賴它。
    這裡紅了就要連同 app.py 的呼叫慣用法一起改。"""
    from notebooklm import NotebookLMClient

    assert not inspect.iscoroutinefunction(NotebookLMClient.from_storage)
    p = _params(NotebookLMClient.from_storage)
    assert "keepalive" in p and "keepalive_min_interval" in p
```

```python
def test_download_audio_arg_order():
    from notebooklm._artifacts import ArtifactsAPI

    # 0.7.x 尾端加 artifacts_data(預先抓好的 artifact 清單,避免重複 list;我們不傳)
    assert _params(ArtifactsAPI.download_audio) == [
        "self",
        "notebook_id",
        "output_path",
        "artifact_id",
        "artifacts_data",
    ]
```

`test_read_surface_signatures_and_fields` 中:

```python
    assert _params(SourcesAPI.get_fulltext) == ["self", "notebook_id", "source_id", "output_format"]
```

```python
def test_add_file_accepts_mime_wait_and_title():
    from notebooklm._sources import SourcesAPI

    p = _params(SourcesAPI.add_file)
    assert p[:3] == ["self", "notebook_id", "file_path"]
    # 0.7.x:title= 存在但內部仍是 add→rename 兩步、改名失敗只 log 不 raise
    #(podcast 流程因此維持顯式 rename;見 AGENTS.md gotcha)。on_progress 上傳進度 callback。
    assert p == [
        "self",
        "notebook_id",
        "file_path",
        "mime_type",
        "wait",
        "wait_timeout",
        "title",
        "on_progress",
    ]
```

```python
def test_source_add_tail_params_are_keyword_only():
    """0.7.0 起 source add API 的尾端參數是 keyword-only(位置呼叫會 TypeError)。
    我們的呼叫點已全 keyword;鎖住這件事,擋未來有人寫成位置參數。"""
    from notebooklm._sources import SourcesAPI

    for func, kwonly in (
        (SourcesAPI.add_url, {"wait", "wait_timeout"}),
        (SourcesAPI.add_text, {"wait", "wait_timeout", "idempotent"}),
        (SourcesAPI.add_file, {"wait", "wait_timeout", "title", "on_progress"}),
    ):
        params = inspect.signature(func).parameters
        for name in kwonly:
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, (func, name)
```

```python
def test_rename_signatures_gained_return_object():
    """0.7.0 起 rename() 有 return_object,**預設 True:改名後會再抓一次全量清單
    驗證、找不到會 raise**。我們所有 rename 呼叫點顯式傳 return_object=False,
    保留 0.4.1 的 fire-and-forget 語意(不多打 RPC、不引入新失敗模式)。"""
    from notebooklm._artifacts import ArtifactsAPI
    from notebooklm._sources import SourcesAPI

    assert _params(ArtifactsAPI.rename) == ["self", "notebook_id", "artifact_id", "new_title", "return_object"]
    assert _params(SourcesAPI.rename) == ["self", "notebook_id", "source_id", "new_title", "return_object"]
```

```python
def test_wait_for_completion_full_signature():
    # 0.7.x 移除 poll_interval(0.4.1 尚存)、尾端加 on_status_change callback。
    # 我們所有呼叫點只用 timeout=(tools_basic:124 / tools_podcast:110 /
    # tools_artifacts:54,84),不受影響。
    from notebooklm._artifacts import ArtifactsAPI

    assert _params(ArtifactsAPI.wait_for_completion) == [
        "self",
        "notebook_id",
        "task_id",
        "initial_interval",
        "max_interval",
        "timeout",
        "max_not_found",
        "min_not_found_window",
        "on_status_change",
    ]
```

`test_source_signatures_and_fields` 中:

```python
    assert _params(SourcesAPI.add_url) == ["self", "notebook_id", "url", "wait", "wait_timeout"]
    # 0.7.x add_text 尾端加 idempotent(重試防重複;我們不傳,預設即可)
    assert _params(SourcesAPI.add_text) == ["self", "notebook_id", "title", "content", "wait", "wait_timeout", "idempotent"]
```

`test_slide_deck_signatures` 中:

```python
    assert _params(ArtifactsAPI.download_slide_deck) == [
        "self", "notebook_id", "output_path", "artifact_id", "output_format", "artifacts_data",
    ]
```

`test_report_signatures` 中:

```python
    assert _params(ArtifactsAPI.download_report) == [
        "self", "notebook_id", "output_path", "artifact_id", "artifacts_data",
    ]
```

- [ ] **Step 7: 全測綠**

Run: `uv run pytest -q`
Expected: 全部 passed(0 failed;數量比 139 多 1,新增 keyword-only 契約)

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock notebooklm_mcp/app.py tests/test_server_lifespan.py tests/test_contracts.py
git commit -m "upgrade: notebooklm-py 0.4.1 → 0.7.3(跨 0.5/0.6/0.7 三個 breaking 版)

症狀:pin 停在 0.4.1 會越滯後越難升;RPC 漂移修復只落在上游新版。
根因:0.5.0 起連續 breaking(deprecated 移除/from_storage 改同步/source-add
參數 keyword-only/rename 回傳改)。
為何這樣修:我們沒用到任何被移除的 API、不 catch 上游 typed exception,實際
衝擊僅 from_storage 慣用法 + contract 重鎖(以實裝 0.7.3 簽名為準,含
keyword-only 斷言);lifespan 保留 keepalive=600;uv.lock 一併重鎖。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: 0.7.3 行為語意對齊(rename return_object=False、fake 鏡射 keyword-only、source_add_file 加 fail-loud title)

> **設計決定(對 Codex finding 2 的回應)**:0.7.3 的 `add_file(title=)` 內部仍是 add→rename
> 兩步,且**改名失敗只 log 不 raise**。podcast 的「source 與 Studio artifact 完全同名」是鐵律,
> 必須 fail-loud——所以 **podcast 上傳流程維持現行顯式 add_file → sources.rename 兩步,不改用
> title=**(顯式 rename 的 RPC 失敗會 raise)。`title=` 只用在 `source_add_file` 工具(手動補檔
> 便利),並以回傳物件的 title 做後檢,不符即 raise。

**Files:**
- Modify: `tests/conftest.py`(FakeSources/FakeArtifacts 簽名鏡射 0.7.3:keyword-only、title、return_object)
- Modify: `tests/test_tools_basic.py`(source_add_file title 測試)
- Modify: `notebooklm_mcp/tools_basic.py`(`source_add_file` 加 title + 後檢;`artifact_rename` 傳 `return_object=False`)
- Modify: `notebooklm_mcp/tools_podcast.py:94,115,129`(三個 rename 呼叫點加 `return_object=False`)

**Interfaces:**
- Consumes: Task 1 裝好的 0.7.3。
- Produces: 所有 `*.rename(...)` 呼叫帶 `return_object=False`;`source_add_file` 簽名變為
  `(notebook_id, file_path, mime_type=None, wait=True, title=None)`(工具**名**不變,
  `check_skill_sync` 不受影響);fakes 與 0.7.3 同形。

- [ ] **Step 1: conftest fakes 精確鏡射 0.7.3 簽名**

> **`*` 位置是二審重點**:0.7.3 的 `add_file` 中 `mime_type` 是 **positional-or-keyword**,
> `*` 在它之後;`rename` 的 `return_object` 是 **keyword-only**。fake 簽名必須逐字元對齊,
> 否則契約防護是假的。

`tests/conftest.py`:

`FakeSources.add_url` / `FakeSources.add_text` 簽名加 `*`(鏡射 0.7.3 keyword-only;記錄欄位不變,
`add_text` 記錄可不含 `idempotent`——我們不傳它):

```python
    async def add_url(self, notebook_id, url, *, wait=False, wait_timeout=120.0):
```

```python
    async def add_text(self, notebook_id, title, content, *, wait=False,
                       wait_timeout=120.0, idempotent=False):
```

(兩者的方法本體與既有 `self.calls.append(...)` 記錄邏輯照舊,只換簽名行。)

`FakeSources.add_file` 整段替換(**`mime_type` 在 `*` 之前**;title 由內部 rename 達成、
可能靜默失敗 → 用 `title_lands` 開關模擬):

```python
    async def add_file(self, notebook_id, file_path, mime_type=None, *, wait=False,
                       wait_timeout=120.0, title=None, on_progress=None):
        # 鏡射 notebooklm-py 0.7.3:mime_type 之後的參數 keyword-only;title= 內部
        # 其實是 add→rename 兩步,rename 失敗只 log 不 raise(回傳舊 title 的 Source)。
        # title_lands=False 模擬那個靜默失敗,供 source_add_file 後檢的紅路徑測試。
        self.calls.append(
            (
                "add_file",
                dict(
                    notebook_id=notebook_id,
                    file_path=str(file_path),
                    mime_type=mime_type,
                    wait=wait,
                    wait_timeout=wait_timeout,
                    title=title,
                ),
            )
        )
        landed = title if self.title_lands else None
        return type("Src", (), {"id": self._add(landed), "title": landed})()
```

`FakeSources.__init__` 加開關(緊接 `self._counter = 0` 之後):

```python
        # True(預設)= add_file(title=) 的內部改名成功;False 模擬 0.7.3 的
        # 靜默改名失敗(SDK 只 log,回傳舊 title)。
        self.title_lands = True
```

`FakeSources.rename` 與 `FakeArtifacts.rename` 加 **keyword-only** 的 `return_object`
(鏡射 0.7.3;記錄進 calls 以便斷言呼叫端有顯式傳 False):

```python
    async def rename(self, notebook_id, source_id, new_title, *, return_object=True):
        self.calls.append(("rename", dict(source_id=source_id, new_title=new_title,
                                          return_object=return_object)))
        for s in self.sources:
            if s["id"] == source_id:
                s["title"] = new_title
        return None
```

(`FakeArtifacts.rename` 同法:簽名尾端加 `*, return_object=True`,記錄 dict 加同名 key;
其原有記錄欄位不動。)

- [ ] **Step 2: 寫紅測試**

`tests/test_tools_basic.py` 末尾新增(該檔 import 別名是 `t`):

```python
async def test_source_add_file_passes_title_and_returns_id(fake_client, tmp_path):
    """0.7.3 add_file 有 title=;工具下傳並回 source_id。"""
    f = tmp_path / "ep03.mp3"
    f.write_bytes(b"x")
    result = await t.source_add_file("nb-123", str(f), mime_type="audio/mpeg", title="EP03 進階篇")
    call = next(c[1] for c in fake_client.sources.calls if c[0] == "add_file")
    assert call["title"] == "EP03 進階篇"
    assert result["source_id"].startswith("src-")


async def test_source_add_file_fails_loud_when_title_does_not_land(fake_client, tmp_path):
    """0.7.3 SDK 的內部改名失敗只 log 不 raise;工具端必須後檢 fail-loud
    (「source 與 artifact 同名」鐵律不容靜默破功)。"""
    fake_client.sources.title_lands = False
    f = tmp_path / "ep03.mp3"
    f.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="title"):
        await t.source_add_file("nb-123", str(f), title="EP03 進階篇")
```

`tests/test_tools_podcast.py` 末尾新增(斷言 podcast 路徑的 rename 全部顯式 fire-and-forget):

```python
async def test_all_renames_are_fire_and_forget(fake_client, tmp_path):
    """0.7.3 rename 預設 return_object=True 會多抓一次全量清單且可能 raise
    not-found;我們所有呼叫點必須顯式傳 False(保留 0.4.1 語意)。"""
    await p.podcast_episode(
        "nb-123", episode_n=1, title="心法篇", brief="b", output_dir=str(tmp_path)
    )
    renames = [c[1] for c in fake_client.sources.calls if c[0] == "rename"]
    renames += [c[1] for c in fake_client.artifacts.calls if c[0] == "rename"]
    assert renames, "podcast flow 必須有 rename 呼叫"
    assert all(r["return_object"] is False for r in renames)
```

`tests/test_tools_basic.py` 末尾新增(`artifact_rename` 工具路徑也要鎖——二審指出只測 podcast
入口有覆蓋缺口):

```python
async def test_artifact_rename_is_fire_and_forget(fake_client):
    """artifact_rename 工具同樣必須顯式 return_object=False。"""
    await t.artifact_rename("nb-123", "task-123", "EP01 心法篇")
    call = next(c[1] for c in fake_client.artifacts.calls if c[0] == "rename")
    assert call["return_object"] is False
```

Run: `uv run pytest tests/test_tools_basic.py tests/test_tools_podcast.py -q`
Expected: 新增 4 個測試 **FAIL**(source_add_file 無 title 參數;rename 未傳 return_object=False)。
若 conftest 簽名改動使其他既有測試紅(add_file call-dict 多了 `title` key、rename call-dict 多了
`return_object` key),先跑 `grep -n "add_file\|rename" tests/*.py` 清點,凡對 call-dict 做**整包
相等**斷言處補上新 key 的期望值——這是 fake 鏡射升級的機械性連動,不是行為變更。

- [ ] **Step 3: 實作**

`notebooklm_mcp/tools_basic.py` 的 `source_add_file`(保留原 docstring 語意,加 title):

```python
@mcp.tool()
async def source_add_file(
    notebook_id: str,
    file_path: str,
    mime_type: str | None = None,
    wait: bool = True,
    title: str | None = None,
) -> dict:
    """Add a local file as a source. mp3 回饋來源用 mime_type="audio/mpeg";
    title 可直接命名(如手動補一集時傳 "EP03 標題",與 Studio artifact 同名)。"""
    src = await runtime.get_client().sources.add_file(
        notebook_id,
        file_path,
        mime_type=mime_type,
        wait=wait,
        wait_timeout=600.0,
        title=title,
    )
    # 0.7.3 的 title= 內部是 add→rename,改名失敗只 log 不 raise(回傳舊 title)。
    # 命名是鐵律的一部分,靜默破功不可接受 → 後檢 fail-loud。
    if title is not None and getattr(src, "title", None) != title:
        raise RuntimeError(
            f"來源已上傳(source_id={src.id})但 title 未生效"
            f"(期望 {title!r},實際 {getattr(src, 'title', None)!r});"
            f"請用 sources.rename 補命名或刪除重傳。"
        )
    return {"source_id": src.id}
```

`notebooklm_mcp/tools_basic.py` 的 `artifact_rename`(原 145 行呼叫處):

```python
    await runtime.get_client().artifacts.rename(
        notebook_id, artifact_id, new_title, return_object=False
    )
```

`notebooklm_mcp/tools_podcast.py` 三個 rename 呼叫點(94 / 115 / 129 行)各加 `return_object=False`:

```python
        await client.sources.rename(
            notebook_id, prior_src.id, f"EP{episode_n - 1:02d}", return_object=False
        )
```

```python
    # fire-and-forget:0.7.3 預設 return_object=True 會再抓全量清單驗證且可能
    # raise not-found;顯式 False 保留 0.4.1 語意(RPC 層錯誤仍會 raise)。
    await client.artifacts.rename(notebook_id, artifact_id, label, return_object=False)
```

```python
    await client.sources.rename(notebook_id, own_src.id, label, return_object=False)
```

(podcast 的 add_file → rename 兩步結構**不動**——顯式 rename 才 fail-loud,見 task 前言。)

- [ ] **Step 4: 全測綠**

Run: `uv run pytest -q`
Expected: 全綠(比 Task 1 結束時多 3 個測試)

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/test_tools_basic.py tests/test_tools_podcast.py notebooklm_mcp/tools_basic.py notebooklm_mcp/tools_podcast.py
git commit -m "fix: 對齊 notebooklm-py 0.7.3 行為語意(rename/return_object、add_file title)

症狀:0.7.3 rename 預設 return_object=True 會多抓一次全量清單且可能 raise
not-found;add_file(title=) 內部改名失敗只 log 不 raise,會靜默破壞
「source 與 artifact 同名」鐵律。
為何這樣修:所有 rename 呼叫點顯式 return_object=False 保留 0.4.1 fire-and-
forget 語意;podcast 流程維持顯式兩步 rename(fail-loud);source_add_file
提供 title= 便利但以回傳 title 後檢,未生效即 raise。fakes 同步鏡射
keyword-only 簽名,擋未來位置呼叫。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: auth 預檢 fail-early(probe_auth + auth_check 工具 + 長跑工具接入)+ skill repo 同步

> **設計決定(對 Codex finding 6 的回應)**:預檢是網路 RPC,必須排在**所有本地驗證之後**
> ——壞參數要用 ValueError 秒退,不浪費 RPC、不讓認證錯誤蓋掉參數錯誤。作法:把
> `_run_episode` 開頭的兩條本地驗證抽成 `_validate_episode_args()`,`podcast_episode` 先驗再
> probe;`podcast_series` 的 probe 放在 **manifest 載入之後**(二審修正:
> `_load_prior_manifest_episodes` 也是本地驗證,壞 manifest 的 ValueError 有 test_resume.py
> 既有保護,不得被認證錯誤蓋掉)、迴圈之前。`_run_episode` 本身不 probe(series 逐集呼叫,
> 避免每集多一個 RPC;跑到一半的鮮度由 lifespan keepalive=600 背景續命)。

**Files:**
- Create: `notebooklm_mcp/auth_probe.py`
- Modify: `notebooklm_mcp/tools_basic.py`(新增 `auth_check` 工具)
- Modify: `notebooklm_mcp/tools_podcast.py`(抽 `_validate_episode_args`;兩個入口接預檢)
- Modify: `tests/conftest.py`(`FakeNotebooks` 加 fail 開關)
- Modify: `tests/test_tools_basic.py`、`tests/test_tools_podcast.py`(新測試)
- Modify: `tests/test_server_lifespan.py`(expected 工具集 + `auth_check`)
- Modify: `/home/user/research/audi-skill/notebooklm/SKILL.md`、`/home/user/research/audi-skill/notebooklm/references/tool-reference.md`(skill repo 同步)

**Interfaces:**
- Consumes: `runtime.get_client()`;fake 端 `FakeNotebooks.list()`。
- Produces: `async def probe_auth(client) -> dict`(活著回 `{"ok": True, "notebooks": N}`,
  死了 raise `RuntimeError`,訊息含 `sync-auth.sh` 重登指引);MCP 工具 `auth_check()`;
  `_validate_episode_args(episode_n, title, prior_mp3_path)`(純本地驗證,壞參數 raise ValueError)。

- [ ] **Step 1: 寫 fake 開關 + 失敗路徑測試(先紅)**

`tests/conftest.py` 的 `FakeNotebooks` 整段替換(加 `__init__` 與 fail 開關):

```python
class FakeNotebooks:
    def __init__(self):
        # True 時 list() 擲認證死亡錯誤——模擬 cookie 過期(auth 預檢的紅路徑)。
        self.fail_list = False

    async def create(self, title):
        return type("NB", (), {"id": "nb-123", "title": title})()

    async def list(self):
        if self.fail_list:
            raise ValueError("Authentication expired or invalid. Please re-authenticate.")
        return [type("NB", (), {"id": "nb-123", "title": "Test"})()]

    async def get(self, notebook_id):
        return type("NB", (), {"id": notebook_id, "title": "Test", "sources_count": 2,
                               "is_owner": True, "created_at": None})()
```

`tests/test_tools_basic.py` 末尾新增(別名 `t`):

```python
async def test_auth_check_ok(fake_client):
    """認證活著:輕量真 RPC 成功,回 ok + 筆記本數。"""
    result = await t.auth_check()
    assert result == {"ok": True, "notebooks": 1}


async def test_auth_check_dead_gives_relogin_hint(fake_client):
    """認證死亡:fail-fast 並給出可操作的重登指引(不是裸 stack trace)。"""
    fake_client.notebooks.fail_list = True
    with pytest.raises(RuntimeError, match="sync-auth"):
        await t.auth_check()
```

`tests/test_tools_podcast.py` 末尾新增(別名 `p`):

```python
async def test_podcast_series_fails_fast_when_auth_dead(fake_client, tmp_path):
    """整季開跑前先預檢:cookie 死了要秒退,一個生成都不能燒。"""
    fake_client.notebooks.fail_list = True
    with pytest.raises(RuntimeError, match="sync-auth"):
        await p.podcast_series(
            "nb-123",
            episodes=[{"title": "心法篇", "brief": "b"}],
            output_dir=str(tmp_path),
        )
    assert fake_client.artifacts.calls == []


async def test_podcast_episode_fails_fast_when_auth_dead(fake_client, tmp_path):
    fake_client.notebooks.fail_list = True
    with pytest.raises(RuntimeError, match="sync-auth"):
        await p.podcast_episode(
            "nb-123", episode_n=1, title="心法篇", brief="b", output_dir=str(tmp_path)
        )
    assert fake_client.artifacts.calls == []


async def test_local_validation_beats_auth_probe(fake_client, tmp_path):
    """壞參數必須在打任何網路 RPC 之前用 ValueError 秒退——認證錯誤不得蓋掉參數錯誤。"""
    fake_client.notebooks.fail_list = True  # 若先 probe 會變 RuntimeError → 測試失敗
    with pytest.raises(ValueError, match="title"):
        await p.podcast_episode(
            "nb-123", episode_n=1, title="  ", brief="b", output_dir=str(tmp_path)
        )
```

Run: `uv run pytest tests/test_tools_basic.py tests/test_tools_podcast.py -q`
Expected: 新增 5 個測試 **FAIL**(`auth_check` 不存在;podcast 入口沒有預檢)。

- [ ] **Step 2: 實作 auth_probe.py**

Create `notebooklm_mcp/auth_probe.py`:

```python
"""認證預檢:用「輕量真 RPC」驗 cookie 是否還活著。

jacob-bd/notebooklm-mcp-cli 的教訓(其 issue #250):homepage probe 會
false-positive,必須打真正的 NotebookLM RPC 才算數。notebooks.list() 是
最便宜的真 RPC。用在長跑工具(podcast_episode / podcast_series)開頭
fail-fast:cookie 已死時秒退並給重登指引,而不是燒掉數小時等待後才炸。
"""
from __future__ import annotations

RELOGIN_HINT = (
    "NotebookLM 認證失效或無法連線。請在有 GUI 的機器重登後同步:\n"
    "  notebooklm login && bash scripts/sync-auth.sh\n"
    "再重啟 MCP server(Doppler 會注入新的 NOTEBOOKLM_AUTH_JSON)。"
)


async def probe_auth(client) -> dict:
    """輕量真 RPC 探測。活著回 {'ok': True, 'notebooks': N};死了 raise RuntimeError。

    刻意攔一切例外:對呼叫端而言,無論 AuthError、RPC 錯誤還是網路斷線,
    可操作的下一步都一樣(檢查認證/連線再重跑),原始錯誤附在訊息尾供診斷。
    """
    try:
        nbs = await client.notebooks.list()
    except Exception as exc:
        raise RuntimeError(f"{RELOGIN_HINT}\n原始錯誤:{exc!r}") from exc
    return {"ok": True, "notebooks": len(nbs)}
```

- [ ] **Step 3: 抽本地驗證 helper + 兩個入口接預檢 + auth_check 工具**

`notebooklm_mcp/tools_podcast.py`:import 區加 `from .auth_probe import probe_auth`;
在 `_run_episode` 之前新增 helper,並把 `_run_episode` 開頭的兩條驗證改為呼叫它:

```python
def _validate_episode_args(episode_n: int, title: str, prior_mp3_path: str | None) -> None:
    """單集參數的純本地驗證(不打網路)。壞參數 ValueError 秒退——必須在
    auth 預檢之前跑,認證錯誤不得蓋掉參數錯誤。"""
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"episode {episode_n} requires a non-empty 'title'")
    if prior_mp3_path and episode_n <= 1:
        raise ValueError("prior_mp3_path requires episode_n >= 2 (there is no prior to episode 1)")
```

`_run_episode` 開頭(`client = runtime.get_client()` 之後)原本的 title / prior_mp3_path 兩段
if-驗證替換為一行:

```python
    _validate_episode_args(episode_n, title, prior_mp3_path)
```

`podcast_episode` 的 `return await _run_episode(...)` 之前插入:

```python
    # 本地驗證先行(壞參數 ValueError 秒退,不浪費 RPC),再做認證預檢:
    # 單集也要等最多 20 分鐘,cookie 死了先秒退(見 auth_probe docstring)。
    _validate_episode_args(episode_n, title, prior_mp3_path)
    await probe_auth(runtime.get_client())
```

`podcast_series`:插在 `manifest_results = _load_prior_manifest_episodes(...)` **之後**、
`for episode_n in range(...)` 迴圈**之前**(episodes 形狀、start 邊界、manifest 載入這三重
本地驗證全部先行——壞 manifest 的 ValueError 不得被認證錯誤蓋掉):

```python
    # 所有本地驗證(episodes 形狀、start 邊界、manifest 載入)通過後才做認證
    # 預檢:整季動輒數小時,cookie 死了要在燒任何生成之前秒退。只在季開頭驗
    # 一次;跑到一半的鮮度由 lifespan 的 keepalive=600 背景續命。
    await probe_auth(runtime.get_client())
```

`notebooklm_mcp/tools_basic.py`:import 區加 `from .auth_probe import probe_auth`,工具區新增:

```python
@mcp.tool()
async def auth_check() -> dict:
    """輕量真 RPC 驗證 NotebookLM 認證(cookie)是否有效。

    長流程(整季生成、發布)前先跑,cookie 死了會秒退並回重登指引,
    避免燒掉數小時等待。回傳 {"ok": True, "notebooks": N}。
    """
    return await probe_auth(runtime.get_client())
```

- [ ] **Step 4: 跑紅測試轉綠**

Run: `uv run pytest tests/test_tools_basic.py tests/test_tools_podcast.py -q`
Expected: PASS(含 `test_local_validation_beats_auth_probe`)

- [ ] **Step 5: expected 工具集 + 全測**

`tests/test_server_lifespan.py` 的 `expected` set 加 `"auth_check"`。

Run: `uv run pytest -q`
Expected: 全綠

- [ ] **Step 6: skill repo 同步(SKILL.md 工具表 + tool-reference.md + 安裝範例 tag)**

`/home/user/research/audi-skill/notebooklm/SKILL.md`:

工具表(`notebook_create` 那行之前)插入:

```
| `auth_check` | 輕量真 RPC 驗證認證是否有效;整季生成/發布等長流程前先跑 fail-fast |
```

`source_add_file` 那行描述更新為:

```
| `source_add_file` | 加本機檔案來源;mp3 回傳用 `mime_type="audio/mpeg"`,可傳 `title` 直接命名(未生效會 raise) |
```

安裝範例(SKILL.md:124 附近)`@v0.1.0` → `@v0.2.0`(與 Task 4 版本升級一致;tag 由使用者驗收後建立)。

`/home/user/research/audi-skill/notebooklm/references/tool-reference.md`:先讀該檔照既有
條目格式,新增 `auth_check` 條目(無參數;回傳 `{"ok": true, "notebooks": N}`;失敗 raise 帶
重登指引;使用時機:`podcast_series` / `publish_series` 前),並在 `source_add_file` 條目補
`title` 參數說明(含「未生效會 raise」)。

Run: `uv run python scripts/check_skill_sync.py`
Expected: exit 0(無 missing)

- [ ] **Step 7: Commit(MCP repo 與 skill repo 各一)**

```bash
git add notebooklm_mcp/auth_probe.py notebooklm_mcp/tools_basic.py notebooklm_mcp/tools_podcast.py tests/conftest.py tests/test_tools_basic.py tests/test_tools_podcast.py tests/test_server_lifespan.py
git commit -m "feat: auth 預檢 fail-early(auth_check 工具 + podcast 長跑入口接入)

症狀:cookie 過期時,整季生成會在燒掉數小時等待後才以裸錯誤炸掉。
根因:長跑工具開頭沒有認證探測;keepalive 只保「跑到一半」,救不了「一開始就是死的」。
為何這樣修:用最便宜的真 RPC(notebooks.list)探測——jacob-bd #250 教訓,homepage
probe 會 false-positive;本地驗證先行(參數錯誤不被認證錯誤蓋掉),預檢排在所有
本地驗證之後;死了秒退並回可操作的重登指引(sync-auth.sh)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

skill repo(`/home/user/research/audi-skill`,commit message 明講對應 MCP 變更):

```bash
git -C /home/user/research/audi-skill add notebooklm/SKILL.md notebooklm/references/tool-reference.md
git -C /home/user/research/audi-skill commit -m "sync: notebooklm MCP 新增 auth_check、source_add_file 支援 title(對應 notebooklm-mcp 0.7.3 升級批次,消費端 tag 改 v0.2.0)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 版本/rollout 對齊(0.2.0)+ AGENTS.md 更新 + doppler smoke 驗證

**Files:**
- Modify: `pyproject.toml:3`(version)、`notebooklm_mcp/__init__.py:3`(__version__)
- Modify: `.github/workflows/ci.yml:41`(wheel 路徑改 glob)
- Modify: `AGENTS.md`(Gotchas 區塊 + 安裝範例 tag)
- Modify: `README.md`(安裝範例 tag + 開頭 `notebooklm-py>=0.3,<0.4` 舊 pin 敘述 → `>=0.7.3,<0.8`)
- Modify: `docs/mcp-setup.md`(`v0.1.0` 安裝範例 → `v0.2.0`;二審抓到的遺漏)
- 驗證:doppler 真實 smoke(不改碼)

> 收尾前跑 `grep -rn "v0\.1\.0\|>=0\.3,<0\.4\|>=0\.4" --include="*.md" --include="*.toml" --include="*.yml" .`
> 掃殘留,任何過時版本字樣一併清掉(排除 docs/superpowers/ 歷史文件與 CHANGELOG 性質內容)。

**Interfaces:**
- Consumes: Task 1-3 全部落地。
- Produces: 套件版本 0.2.0、CI 不再硬編碼 wheel 檔名、文件與實裝一致、真實環境驗證證據。

- [ ] **Step 1: 版本 bump + CI wheel glob**

`pyproject.toml:3` → `version = "0.2.0"`;`notebooklm_mcp/__init__.py:3` → `__version__ = "0.2.0"`。

`.github/workflows/ci.yml:41` 的硬編碼 wheel 檔名改 glob(與版本解耦):

```yaml
          uv tool install --python 3.12 --force dist/*.whl
```

Run: `uv run pytest -q`(確認版本 bump 沒破壞任何測試——若有測試斷言 __version__,同步更新)
Expected: 全綠

- [ ] **Step 2: 重寫 AGENTS.md Gotchas 區塊 + 安裝範例**

Gotchas 標頭與 SDK 版本相關條目改為(其餘條目——CJK 空格、chat_ask 引用標記、X 長文、封面、
發布順序等——**不動**,與 SDK 版本無關;podcast 命名鐵律條目保留):

```markdown
## Gotchas(notebooklm-py 0.7.3,pin `>=0.7.3,<0.8`;與 GitHub HEAD 不同,以**實裝版本**為準)

- **上游/NotebookLM 行為突變時的情報站**:讀 `_research/notebooklm-mcp-cli` 既有 clone 的
  CHANGELOG.md 與 docs/KNOWN_ISSUES.md(jacob-bd,全生態追 Google 改版最快;bl 漂移、cookie
  語意、RPC schema 變動幾乎都最先出現在那),再對照 notebooklm-py 的 GitHub issues。
  **`_research/` 唯讀:連 `git pull` 都不做**,clone 更新請使用者自行決定。
- `from_storage()` 是**同步函式**,回傳可直接 `async with` 的 context →
  `async with NotebookLMClient.from_storage(keepalive=600)`(0.4.x「coroutine 必須 await」
  已走入歷史)。`keepalive=600`:session 內背景 RotateCookies task(process-scoped,隨
  server 生滅),長生成不因 `__Secure-1PSIDTS` 過期中途死;**env-var 唯讀模式只轉記憶體、
  不落盤**,跨 session 老化照舊 2–4 週 GUI 機重登 + `sync-auth.sh`。網路擋
  `accounts.google.com` 時設 `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` 關閉。
- 長跑工具(`podcast_episode`/`podcast_series`)在**本地驗證之後**有 `probe_auth` 認證預檢
  (輕量真 RPC;homepage probe 會 false-positive,jacob-bd #250);獨立工具版是 `auth_check`。
- `GenerationStatus` **無 `artifact_id`**;`task_id` 本身就是 artifact id(download/rename 用它)。
- `sources.add_file` 有 `title`(0.7.x),**但內部仍是 add→rename 兩步且改名失敗只 log 不
  raise** → podcast 流程維持顯式 add_file → rename 兩步(fail-loud);`source_add_file` 工具
  的 title= 有回傳後檢,未生效會 raise。
- `rename()` 的 `return_object` **預設 True 會再抓一次全量清單驗證、可能 raise not-found**
  → 我們所有 rename 呼叫點顯式傳 `return_object=False`(fire-and-forget,RPC 錯誤仍會 raise)。
- 0.7.0 起 source add API 尾端參數(`wait`/`wait_timeout`/`title` 等)**keyword-only**,
  位置呼叫直接 TypeError(contract 測試有鎖)。
- `wait_for_completion` 的 `poll_interval` 已移除(0.7.x);呼叫只用 `timeout=`。
- 改 contract 測試時對「**實裝版本**」跑,別信 `_research/` 的 HEAD clone。
```

AGENTS.md Commands 區的消費端安裝範例 `@v0.1.0` → `@v0.2.0`;`README.md` 若有 `@v0.1.0`
安裝範例一併更新(tag 由使用者驗收後建立,文件先行對齊)。

- [ ] **Step 3: 全測最終確認**

Run: `uv run pytest -q`
Expected: 全綠(0 failed;總數 = 139 + Task 1-3 新增數)

- [ ] **Step 4: doppler 真實 smoke(唯讀,fail-loud)**

```bash
set -euo pipefail
cd /home/user/research/audiskill/notebooklm-mcp
doppler run -p notebooklm -c dev -- .venv/bin/python - <<'PY' | tee /tmp/nlm-smoke.log
import asyncio, logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.WARNING)
from notebooklm import NotebookLMClient
from notebooklm_mcp.auth_probe import probe_auth

async def main():
    async with NotebookLMClient.from_storage(keepalive=600) as client:
        r = await probe_auth(client)
        print("SMOKE OK:", r)

asyncio.run(main())
PY
grep -q "SMOKE OK" /tmp/nlm-smoke.log
grep -qi "keepalive task started" /tmp/nlm-smoke.log || echo "WARN: keepalive log 未見(檢查 DEBUG 級別)"
```

Expected: python 程式 exit 0(`set -euo pipefail` 保證失敗不被吞);log 含
`Keepalive task started (interval=600.0s)` 與 `SMOKE OK: {'ok': True, 'notebooks': <N>}`;
無 traceback。順手記錄 `notebooks.list()` 在 209 本帳號上的耗時(supra Codex「Need to verify」:
若 probe 明顯超過數秒,在 AGENTS.md gotcha 註記)。

- [ ] **Step 5: Commit + 收尾註記**

```bash
git add pyproject.toml notebooklm_mcp/__init__.py .github/workflows/ci.yml AGENTS.md README.md docs/mcp-setup.md
git commit -m "docs+release: 版本 0.2.0、CI wheel glob、Gotchas 對齊 0.7.3 實裝行為

症狀:Gotchas 還在描述 0.4.1 的限制;CI 硬編碼 0.1.0 wheel 檔名,版本一動就斷;
上游壞掉時沒有固定的查案入口。
為何這樣修:逐條對實裝 0.7.3 重驗後反轉/保留(含 add_file title 吞錯、
return_object 預設 refetch 兩個行為陷阱);CI 改 dist/*.whl 與版本解耦;
情報站指到 _research/ 的 notebooklm-mcp-cli(唯讀,不 pull)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

收尾提醒使用者(不自動執行):消費端(3 VM / podcast-lab)pin 的是 git tag——
驗收後請自行 `git tag v0.2.0 && git push origin main --tags`,並在各機
`uv tool install --python 3.12 "git+https://github.com/audichuang/notebooklm-mcp.git@v0.2.0"`
重裝;skill repo 的變更也要一併 push。

---

## Self-Review 紀錄

- **Spec 覆蓋**:大版本升級(Task 1)、0.7.3 行為語意對齊(Task 2)、auth 預檢 fail-early
  (Task 3)、情報站指引 + 版本/rollout + 文件對齊 + 真實驗證(Task 4)——需求全覆蓋。
- **Codex 審查回應**:一審 9 條全數處置——BLOCKER(import 別名)、uv.lock、keyword-only、
  smoke pipefail、`_research/` 措辭、版本/CI/文件 rollout 為「接受並修入」;finding 2
  (add_file title)接受事實但改採「podcast 維持顯式兩步」的更簡修法(**二審已同意**);
  finding 6(預檢順序)以 `_validate_episode_args` helper 實現「本地驗證先行」,並補
  `test_local_validation_beats_auth_probe` 鎖住順序。二審三條 fix 全收:fake 簽名 `*` 位置
  精確對齊(mime_type positional、return_object keyword-only)+ 補 add_url/add_text/rename
  全套 fake、series probe 移到 manifest 載入後、artifact_rename fire-and-forget 測試、
  rollout 補 docs/mcp-setup.md 與 README 舊 pin 字樣。
- **佔位符**:Task 2 Step 2 與 Task 3 Step 6 各有一處「照實際檔案格式微調」,已附 grep/讀檔
  指令與具體目標內容,執行者有明確操作,非空泛 TODO。
- **型別/命名一致性**:`probe_auth(client) -> dict`、`RELOGIN_HINT` 含 `sync-auth` 字串
  (測試 `match="sync-auth"` 依賴它)、`_validate_episode_args` 在 tools_podcast 內定義並被
  `podcast_episode` / `_run_episode` 共用、`keepalive=600` 貫穿 app.py/測試/smoke、
  測試別名 `p.` / `t.` 與既有檔案一致,已交叉核對。
