from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from notebooklm.exceptions import RPCError
from notebooklm._types.research import (
    RESEARCH_RESULT_TYPE_REPORT,
    ResearchSource,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from notebooklm.rpc.types import SharePermission
from notebooklm.types import (
    ArtifactType,
    BlockKind,
    DocumentBlock,
    StructuredDocument,
    TextSpan,
    utf16_len,
)

from notebooklm_mcp import runtime


class FakeArtifacts:
    def __init__(self):
        self.calls = []
        self._generate_count = 0
        # Optional test hook invoked at the exact external side-effect seam,
        # before generate_audio records/returns anything.
        self.generate_audio_exc = None
        self.on_generate_audio = None
        self.generate_remote_artifacts_before_raise: list = []
        # Set to an integer N to make the N-th wait_for_completion call raise
        # TimeoutError (models a real generation timeout for error-path tests).
        self.fail_wait_on = None
        # Exception INSTANCE to raise on the fail_wait_on-th wait (defaults to a plain
        # TimeoutError). Set to a real SDK exception (e.g. ArtifactPendingTimeoutError,
        # whose constructor needs notebook_id/task_id/timeout) to prove the podcast
        # error path preserves the concrete type instead of reconstructing it.
        self.wait_exc = None
        self._wait_count = 0
        # When True, generate_audio returns a FAILED status (task_id="",
        # is_failed=True) — models a rate-limit/quota/refusal that the SDK reports
        # via status rather than by raising.
        self.fail_generate = False
        # When True, wait_for_completion RETURNS a failed status (is_failed=True)
        # instead of raising — models the real 0.3.4 behaviour where generation
        # fails mid-poll and wait_for_completion returns the final failed
        # GenerationStatus (it only raises TimeoutError on timeout).
        self.fail_complete = False
        # True 時 wait_for_completion 回 status="removed"、is_failed=False、is_removed=True
        # —— 模擬 0.6.0 起的配額下架語意(SDK 不再合成成 "failed")。
        self.fail_removed = False
        # Server-side artifact set — what artifacts.list() would return. Seed via
        # seed_artifacts() to model episodes/reports that already exist in the notebook.
        self.artifacts = []
        # Download fault injection: on failure, an optional partial payload is
        # written before the configured exception is raised. Durable finalize
        # must target a temp file so this never replaces a prior successful mp3.
        self.download_audio_exc = None
        self.download_audio_partial_bytes: bytes | None = None
        self.download_audio_bytes = b"fake mp3 bytes"
        # 同樣的 fault injection 給簡報/講義:bytes 先落地、再拋錯 = torn write。
        # 原子換檔必須讓既有的完整檔案毫髮無傷(v0.3.3)。
        self.download_slides_bytes = b"%PDF-1.4 fake"
        self.download_slides_exc = None
        self.download_report_bytes = "# 假講義\n\n- 重點一\n".encode("utf-8")
        self.download_report_exc = None
        # 設成別的字串,模擬 REVISE_SLIDE 回一個「不等於傳入 artifact_id」的 id
        # (SDK 沒有保證相等,只是 parse 回傳值)。
        self.revise_slide_returns_id = None
        # retry_failed 的真實拒絕形狀:exception INSTANCE(rate limit / 配額 / 不可重試)。
        self.retry_exc = None

    def seed_artifacts(self, *arts):
        """Test helper: pre-populate the notebook's artifact set."""
        self.artifacts.extend(arts)

    def seed_artifact(self, artifact_id, *, kind=ArtifactType.SLIDE_DECK, title="art",
                      completed=True, failed=False, status=None, source_ids=()):
        """建一筆帶完整 preflight 欄位的 artifact(kind / is_completed / is_failed /
        status_str)——`get_or_none` 的 preflight 靠這四個判斷。"""
        art = SimpleNamespace(
            id=artifact_id, title=title, kind=kind,
            is_completed=completed, is_failed=failed,
            status_str=status or ("failed" if failed else
                                  "completed" if completed else "processing"),
            created_at=datetime.now(timezone.utc),
            source_ids=tuple(source_ids),
        )
        self.artifacts.append(art)
        return art

    async def get_or_none(self, notebook_id, artifact_id):
        # 鏡射真 SDK:「list 一次再比對 id」——所以它同時回答「存不存在」與
        # 「屬不屬於這個 notebook」。找不到回 None(sanctioned,不發 DeprecationWarning)。
        self.calls.append(("get_or_none", dict(notebook_id=notebook_id,
                                               artifact_id=artifact_id)))
        return next((a for a in self.artifacts if a.id == artifact_id), None)

    # Signature mirrors notebooklm-py 0.3.4 ArtifactsAPI.list (filter by .kind).
    async def list(self, notebook_id, artifact_type=None):
        self.calls.append(("list", dict(notebook_id=notebook_id, artifact_type=artifact_type)))
        if artifact_type is None:
            return list(self.artifacts)
        return [a for a in self.artifacts if a.kind == artifact_type]

    async def generate_audio(
        self,
        notebook_id,
        source_ids=None,
        language="en",
        instructions=None,
        audio_format=None,
        audio_length=None,
    ):
        if self.on_generate_audio is not None:
            self.on_generate_audio()
        self.calls.append(
            (
                "generate_audio",
                dict(
                    notebook_id=notebook_id,
                    source_ids=source_ids,
                    language=language,
                    instructions=instructions,
                    audio_format=audio_format,
                    audio_length=audio_length,
                ),
            )
        )
        if self.generate_audio_exc is not None:
            self.artifacts.extend(self.generate_remote_artifacts_before_raise)
            raise self.generate_audio_exc
        # Faithful to the real SDK: GenerationStatus exposes ONLY task_id
        # (task_id IS the artifact id). No artifact_id attribute exists.
        if self.fail_generate:
            return type("S", (), {"task_id": "", "is_failed": True, "status": "failed", "error": "simulated failure"})()
        self._generate_count += 1
        task_id = f"task-{122 + self._generate_count}"
        self.artifacts.append(
            SimpleNamespace(
                id=task_id,
                title="Audio Overview",
                kind=ArtifactType.AUDIO,
                created_at=datetime.now(timezone.utc),
                source_ids=(),
            )
        )
        return type("S", (), {"task_id": task_id, "is_failed": False})()

    # Signature mirrors notebooklm-py 0.3.4 ArtifactsAPI.wait_for_completion.
    # 簽名鏡射 notebooklm-py 0.7.3:0.4.x 的 poll_interval 已移除、尾端新增 on_status_change。
    # 我方呼叫只用 (notebook_id, task_id, timeout=)。
    async def wait_for_completion(
        self, notebook_id, task_id, initial_interval=2.0, max_interval=10.0, timeout=300.0,
        max_not_found=5, min_not_found_window=10.0, on_status_change=None
    ):
        self.calls.append(("wait", dict(notebook_id=notebook_id, task_id=task_id, timeout=timeout)))
        self._wait_count += 1
        if self.fail_wait_on is not None and self._wait_count == self.fail_wait_on:
            raise self.wait_exc or TimeoutError(
                f"simulated generation timeout on wait #{self._wait_count}"
            )
        if self.fail_complete:
            return type(
                "S", (), {"task_id": task_id, "is_failed": True, "status": "failed", "error": "simulated mid-poll failure"}
            )()
        if self.fail_removed:
            return type(
                "S", (), {"task_id": task_id, "is_failed": False, "is_removed": True, "status": "removed"}
            )()
        return type("S", (), {"task_id": task_id, "is_failed": False, "is_removed": False})()

    async def download_audio(self, notebook_id, output_path, artifact_id=None):
        self.calls.append(
            ("download", dict(notebook_id=notebook_id, output_path=output_path, artifact_id=artifact_id))
        )
        output = Path(output_path)
        if self.download_audio_partial_bytes is not None:
            output.write_bytes(self.download_audio_partial_bytes)
        if self.download_audio_exc is not None:
            raise self.download_audio_exc
        if self.download_audio_bytes is not None:
            output.write_bytes(self.download_audio_bytes)
        return output_path

    async def rename(self, notebook_id, artifact_id, new_title, *, return_object=True):
        self.calls.append(("rename", dict(artifact_id=artifact_id, new_title=new_title,
                                          return_object=return_object)))
        for artifact in self.artifacts:
            if artifact.id == artifact_id:
                artifact.title = new_title
                break
        return None

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
            f.write(self.download_slides_bytes)
        if self.download_slides_exc is not None:
            raise self.download_slides_exc
        return output_path

    async def generate_report(self, notebook_id, report_format=None, source_ids=None,
                              language="en", custom_prompt=None, extra_instructions=None):
        self.calls.append(("generate_report", dict(
            notebook_id=notebook_id, report_format=report_format, source_ids=source_ids,
            language=language, custom_prompt=custom_prompt,
            extra_instructions=extra_instructions)))
        if self.fail_generate:
            return type("S", (), {"task_id": "", "is_failed": True, "status": "failed", "error": "sim"})()
        return type("S", (), {"task_id": "report-task", "is_failed": False})()

    async def revise_slide(self, notebook_id, artifact_id, slide_index, prompt):
        # REVISE_SLIDE 只吃 artifact_id(notebook_id 只設 source_path header)。0.7.3 觀察到
        # 回傳的 task_id 就是同一個 artifact,但 **SDK 並未強制**——它只是 parse RPC 回來的
        # 那個 id。所以工具端一律用回傳值,不假設相等;revise_slide_returns_id 讓測試能餵
        # 一個不同的 id,證明實作沒有依賴這個假設。
        self.calls.append(("revise_slide", dict(
            notebook_id=notebook_id, artifact_id=artifact_id,
            slide_index=slide_index, prompt=prompt)))
        if self.fail_generate:
            return type("S", (), {"task_id": "", "is_failed": True, "status": "failed", "error": "sim"})()
        return type("S", (), {"task_id": self.revise_slide_returns_id or artifact_id,
                              "is_failed": False})()

    async def retry_failed(self, notebook_id, artifact_id):
        # 0.7.3:同一個 artifact_id 原地重跑,回 status="in_progress"。**與 generate_* 不同**,
        # 伺服器端的同步拒絕(rate limit / 配額 / 不可重試)是 raise,而且 parse 出空 id 時
        # SDK 自己就丟 ArtifactFeatureUnavailableError —— 所以 task_id="" 這個回傳在真 SDK
        # 不可能發生(retry_exc 才是真實的拒絕形狀;fail_generate 分支僅為對稱保留)。
        self.calls.append(("retry_failed", dict(notebook_id=notebook_id, artifact_id=artifact_id)))
        if self.retry_exc is not None:
            raise self.retry_exc
        return type("S", (), {"task_id": artifact_id, "is_failed": False, "status": "in_progress"})()

    async def download_report(self, notebook_id, output_path, artifact_id=None):
        self.calls.append(("download_report", dict(output_path=output_path, artifact_id=artifact_id)))
        with open(output_path, "wb") as f:
            f.write(self.download_report_bytes)
        if self.download_report_exc is not None:
            raise self.download_report_exc
        return output_path


class FakeSources:
    """Models the NotebookLM notebook's SERVER-SIDE source set: `sources` persists
    independently of any process run, so it can model cross-process resume and the
    generic source-deletion flow. `calls` records invocations for assertions."""

    def __init__(self):
        self.calls = []
        # Server-side persistent set. File uploads are ``media``; seeded/url/text
        # sources stay ``web_page`` to preserve the existing source_list fixtures.
        self.sources = []  # [{"id", "title", "kind", "created_at", "is_ready"}]
        self._counter = 0
        # add_file first creates the remote source, then raises: a durable resume
        # must reconcile that source rather than upload a duplicate.
        self.add_file_exc_after_create = None
        # True(預設)= add_file(title=) 的內部改名成功;False 模擬 0.7.3 的
        # 靜默改名失敗(SDK 只 log,回傳舊 title)。
        self.title_lands = True
        # get_fulltext 的可調內容:預設維持舊語意("來源全文", char_count=4)。
        # 設 "" 模擬 paywall/空殼;fulltext_raises=True 模擬 probe RPC 失敗。
        self.fulltext_content = "來源全文"
        self.fulltext_raises = False

    def _add(
        self,
        title,
        *,
        kind="web_page",
        created_at=None,
        is_ready=True,
    ):
        self._counter += 1
        sid = f"src-{self._counter}"
        self.sources.append(
            {
                "id": sid,
                "title": title,
                "kind": kind,
                # 忠實模擬實裝 notebooklm-py **0.8.0**:Source.created_at 改回 tz-aware UTC
                # (`_datetime_from_timestamp` 現在傳 tz=timezone.utc)。0.7.x 是 host-local
                # naive,而那次 naive/aware 的落差讓 reconciliation 在測試綠、production
                # 卻濾掉每一筆 source —— 所以這裡必須跟著實裝版本走,不能兩邊各猜一個。
                # `_created_at_utc` 對 naive/aware 都正確(有專屬測試鎖著),換版本不會再爆。
                "created_at": created_at or datetime.now(timezone.utc),
                "is_ready": is_ready,
            }
        )
        return sid

    def titles(self):
        return sorted(s["title"] for s in self.sources if s["title"])

    def seed(self, *titles):
        """Test helper: pre-populate sources as if a prior process run created them."""
        for title in titles:
            self._add(title)

    async def add_file(self, notebook_id, file_path, mime_type=None, *, wait=False,
                       wait_timeout=120.0, title=None, on_progress=None):
        # 鏡射 notebooklm-py 0.7.3:mime_type 之後的參數 keyword-only;title= 內部
        # 其實是 add→rename 兩步,rename 失敗只 log 不 raise(回傳舊 title 的 Source)。
        # title_lands=False 模擬那個靜默失敗,供 source_add_file 後檢的紅路徑測試。
        # bytes 也記下來:source_add_file 的自動包裝會把內容複製到 TemporaryDirectory,
        # 工具回傳前那個目錄就被清掉了,只記路徑字串沒辦法驗「內容一字不差」。
        source_path = Path(file_path)
        self.calls.append(
            (
                "add_file",
                dict(
                    notebook_id=notebook_id,
                    file_path=str(file_path),
                    file_bytes=source_path.read_bytes() if source_path.exists() else None,
                    mime_type=mime_type,
                    wait=wait,
                    wait_timeout=wait_timeout,
                    title=title,
                ),
            )
        )
        initial_title = Path(file_path).name
        landed = title if title is not None and self.title_lands else initial_title
        source_id = self._add(landed, kind="media")
        if self.add_file_exc_after_create is not None:
            raise self.add_file_exc_after_create
        source = next(s for s in self.sources if s["id"] == source_id)
        return type(
            "Src",
            (),
            {
                "id": source_id,
                "title": landed,
                "kind": source["kind"],
                "created_at": source["created_at"],
                "is_ready": source["is_ready"],
            },
        )()

    async def rename(self, notebook_id, source_id, new_title, *, return_object=True):
        self.calls.append(("rename", dict(source_id=source_id, new_title=new_title,
                                          return_object=return_object)))
        for s in self.sources:
            if s["id"] == source_id:
                s["title"] = new_title
        return None

    async def list(self, notebook_id):
        self.calls.append(("list", dict(notebook_id=notebook_id)))
        return [
            type("Src", (), {"id": s["id"], "title": s["title"] or "",
                             "kind": s["kind"], "created_at": s["created_at"],
                             "is_ready": s["is_ready"]})()
            for s in self.sources
        ]

    async def get_fulltext(self, notebook_id, source_id):
        self.calls.append(("get_fulltext", dict(source_id=source_id)))
        if self.fulltext_raises:
            raise RuntimeError("simulated fulltext RPC failure")
        return type("FT", (), {"source_id": source_id, "title": "來源標題",
                               "content": self.fulltext_content,
                               "char_count": len(self.fulltext_content)})()

    # 鏡射 notebooklm-py 0.8.0:尾端加 title=(add 時直接命名;我們目前不傳)。
    async def add_url(self, notebook_id, url, *, wait=False, wait_timeout=120.0, title=None):
        self.calls.append(
            ("add_url", dict(notebook_id=notebook_id, url=url, wait=wait, title=title))
        )
        return type("Src", (), {"id": self._add(title or url)})()

    async def add_text(self, notebook_id, title, content, *, wait=False,
                       wait_timeout=120.0, idempotent=False):
        self.calls.append(("add_text", dict(title=title, wait=wait)))
        return type("Src", (), {"id": self._add(title)})()

    async def delete(self, notebook_id, source_id):
        self.calls.append(("delete", dict(source_id=source_id)))
        self.sources = [s for s in self.sources if s["id"] != source_id]
        return True


class FakeNotebooks:
    def __init__(self):
        # True 時 list() 擲認證死亡錯誤——模擬 cookie 過期(auth 預檢的紅路徑)。
        self.fail_list = False
        self.created: list[str] = []

    async def create(self, title):
        self.created.append("nb-123")
        return type("NB", (), {"id": "nb-123", "title": title})()

    async def list(self):
        if self.fail_list:
            # 鏡射一種真實 dead-cookie 形狀;其餘 HTTP/RPC 形狀有專屬契約測試。
            raise RPCError("The server rejected this request (unauthenticated).", rpc_code=16)
        return [type("NB", (), {"id": "nb-123", "title": "Test"})()]

    async def get(self, notebook_id):
        # `role=None` 鏡射 0.8.1 的真實形狀:`Notebook.role` 在 `GET_NOTEBOOK` 的 meta
        # 缺失/帶預期外 userRole 時就是 None,而 `is_owner` 會停在欄位預設 `True`
        # (上游 `__setattr__` 只在 `role is not None` 時同步兩者)。少了這個屬性,
        # 任何用預設 fake 寫的 `notebook_get` 測試會收到看不懂的 AttributeError 而不是斷言失敗。
        return type("NB", (), {"id": notebook_id, "title": "Test", "sources_count": 2,
                               "is_owner": True, "role": None, "created_at": None})()


def _structured_document(*paragraphs: str) -> StructuredDocument:
    """依段落文字組出一份真的 StructuredDocument,段落間 offset 依序累加、
    無分隔符(鏡射上游 `.text` 的語意)。用真 dataclass 建構,而不是
    SimpleNamespace,fake 才不會自己長出一套 rendering 語意——上一輪的 finding 根因正是
    fake 太扁測不出 `.text` 與 `.render()` 的差異。
    """
    blocks = []
    cursor = 0
    for text in paragraphs:
        end = cursor + utf16_len(text)
        blocks.append(
            DocumentBlock(
                start_index=cursor,
                end_index=end,
                spans=(TextSpan(start_index=cursor, end_index=end, text=text),),
                kind=BlockKind.PARAGRAPH,
            )
        )
        cursor = end
    return StructuredDocument(blocks=tuple(blocks))


class FakeChat:
    def __init__(self):
        self.calls = []
        # 設定後蓋掉預設 answer——供 strip_citations 測試餵帶 [n] 標記的回答。
        self.answer_override = None
        # 預設空文件(`StructuredDocument()` 的 `.text`/`.render()` 都回 ""),
        # 讓既有測試維持退回 `_CITATION_RE` regex 的行為;要驗證 render() 路徑
        # 的測試自己把這個換成 `_structured_document(...)` 建出來的非空文件。
        self.answer_document: StructuredDocument = StructuredDocument()

    # Signature mirrors notebooklm-py 0.3.4 ChatAPI.ask (source_ids + conversation_id).
    async def ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        self.calls.append(("ask", dict(question=question, source_ids=source_ids,
                                        conversation_id=conversation_id)))
        refs = [type("Ref", (), {"source_id": "src-1", "citation_number": 1,
                                 "cited_text": "引用片段"})()]
        return type("R", (), {"answer": self.answer_override or f"answer to {question}",
                              "answer_document": self.answer_document,
                              "conversation_id": conversation_id or "conv-1",
                              "references": refs})()


class FakeResearch:
    """鏡射 notebooklm-py 0.7.3 的 ResearchAPI。

    刻意用**真的** ResearchTask / ResearchSource / ResearchStart dataclass(純資料、
    離線可 import),這樣 is_report / result_type 這些判斷跟實裝完全同一份邏輯,
    fake 不會自己長出一套語意。"""

    def __init__(self):
        self.calls = []
        # True 時 start() 回 None —— 模擬「後端沒建出 task」(SDK 不 raise)。
        self.status = ResearchStatus.COMPLETED
        # 報告只引用 A 與 B;C 是 NotebookLM 找到但報告沒用到的邊緣命中。
        self.report = (
            "## 研究地圖\n\n見 [來源A](https://a.example/post) 與 https://b.example/spec 。\n"
        )
        self.sources = (
            ResearchSource(url="https://a.example/post", title="來源A"),
            ResearchSource(url="https://b.example/spec", title="來源B"),
            ResearchSource(url="https://c.example/blog", title="來源C(未被引用)"),
            ResearchSource(
                url="", title="Deep Research Report",
                result_type=RESEARCH_RESULT_TYPE_REPORT, report_markdown="## 研究地圖\n",
            ),
        )
        self.imported = [{"id": "src-r1", "title": "來源A"}]
        # 真 SDK 的 wait_for_completion 會**持續輪詢** in_progress / pinned no_research,
        # 逾時丟 ResearchTimeoutError(TimeoutError 子類)。這個 fake 立刻回傳,所以
        # timeout 語意要靠這顆注入:設成 exception INSTANCE 讓 wait 直接擲。
        self.wait_exc = None

    def _task(self, task_id="res-1"):
        return ResearchTask(
            task_id=task_id, status=self.status, query="advisor tool history forwarding",
            sources=self.sources, summary="摘要", report=self.report,
        )

    async def start(self, notebook_id, query, source="web", mode="fast"):
        self.calls.append(("start", dict(notebook_id=notebook_id, query=query,
                                         source=source, mode=mode)))
        return ResearchStart(task_id="res-1", report_id="rep-1", notebook_id=notebook_id,
                             query=query, mode=mode.lower())

    async def poll(self, notebook_id, task_id=None):
        self.calls.append(("poll", dict(notebook_id=notebook_id, task_id=task_id)))
        return self._task(task_id or "res-1")

    async def wait_for_completion(self, notebook_id, task_id=None, *, timeout=1800,
                                  interval=5, initial_interval=None):
        self.calls.append(("wait", dict(notebook_id=notebook_id, task_id=task_id,
                                        timeout=timeout)))
        if self.wait_exc is not None:
            raise self.wait_exc
        return self._task(task_id or "res-1")

    async def import_sources_with_verification(self, notebook_id, task_id, sources, *,
                                               max_elapsed=1800, initial_delay=5,
                                               backoff_factor=2, max_delay=60):
        self.calls.append(("import", dict(
            notebook_id=notebook_id, task_id=task_id, max_elapsed=max_elapsed,
            # 記標題而非物件:斷言看得懂,也證明「傳過去的就是候選裡那幾筆」。
            titles=[s.title for s in sources],
            is_report=[s.is_report for s in sources])))
        return list(self.imported)


class FakeSharing:
    """真 `SharingAPI` 的 fake。**三處刻意與真 SDK 同形,外加一條觀測面紀律**,因為它們
    各自對應一個「fake 說謊 ⇒ bug 溜過去」的事故形狀(AGENTS.md 的 `Source.created_at`
    tz 那課):

    1. `SharedUser` **有 `permission` 欄位**。舊版 fake 只吐 email,於是「已分享但只是
       VIEWER」與「已分享且是 EDITOR」在測試裡長得一樣,`notebook_share_with_pool`
       把前者當成完成也沒有任何測試會紅。
    2. `add_user` 的 `permission` **預設是 VIEWER**(`_sharing.py:144`),不是 None ——
       真 SDK 的預設值正是上面那個 bug 的成因。
    3. `add_user` 回的是 `get_status()` 的結果(真 SDK 最後一行就是
       `return await self.get_status(notebook_id)`),**含完整 shared_users**。舊版回空
       list,讓「檢查回傳值確認生效」這種後檢寫了也測不出差別。
    4. **觀測面要涵蓋你想斷言的呼叫**(上面三條講回傳值,這條講記錄)。`calls` 只記
       `add_user`,於是「不打任何 RPC」寫成 `assert calls == []` 時,對一支**只打
       `get_status`** 的實作永遠是綠的 —— v0.9.1 的單帳號回歸就這樣溜過 591 個測試。
       `get_status` 因此另記 `status_calls`,**刻意不併進 `calls`**:既有測試用
       `calls == []` 表達「沒打 add_user」(冪等那條),混在一起那個意思會消失。
    5. `set_users` 的 `notify` **預設是 `True`**,不是 `False`——真 SDK 就是這樣
       (`_sharing.py`)。`notebook_create` 的實作永遠顯式傳 `notify=False`(同一個
       人的帳號不用收通知信),把 fake 的預設值故意寫反成 `False` 會讓「拿掉那個
       顯式參數」的回歸測不出來:呼叫端沒傳,Python 用 fake 的預設值頂上,若那個
       預設值剛好也是 `False`,`calls` 記錄的還是 `False`,既有斷言全綠。預設值
       跟真 SDK 一致,這個回歸才會讓 `calls` 記錄的變成 `True`,測試才會紅。
    6. `set_users` 的非空、permission 白名單、exact 重複三項驗證鏡射
       `_sharing.py:set_users`;比上游寬鬆或嚴格都是 bug。
    """

    def __init__(self):
        self.calls: list[tuple] = []
        # `get_status` 另記一份,**刻意不併進 `calls`**:既有測試用 `calls == []` 表達
        # 「沒打 add_user」(冪等那條),混在一起會讓那個意思消失。而分開之後
        # 「單帳號模式不打任何 RPC」才第一次真的測得到 —— 舊斷言 `calls == []` 對
        # 一支只打 `get_status` 的實作永遠是綠的。
        self.status_calls: list[str] = []
        self.add_user_exc = None
        self.set_users_exc = None
        self.set_users_result = None
        # 只對這個 email 失敗(部分成功的進度回報要測得到「已經完成到哪裡」)。
        self.fail_on_email: str | None = None
        # 既有共享者。寫 `"a@x.com"` 等同 `("a@x.com", SharePermission.EDITOR)`;
        # 要測 VIEWER 就寫 tuple。
        self.existing: list = []
        # 伺服器靜默忽略(不 raise、也沒真的生效)的 email —— 對應 add_user 的
        # `allow_null=True`:RPC 回 null 不會拋,只有讀回傳值才看得出來。
        self.silently_ignore: set[str] = set()
        # 這個 client 看不到 notebook 時 get_status 要拋什麼。pool 裡不同槽位對同一個
        # notebook 的可見性不同,是 v0.9.0 Phase 9-1 那個死路的前提。
        self.get_status_exc: BaseException | None = None

    @staticmethod
    def _entry(item) -> tuple:
        return (item, SharePermission.EDITOR) if isinstance(item, str) else tuple(item)

    async def add_user(
        self,
        notebook_id,
        email,
        permission=SharePermission.VIEWER,
        notify=True,
        welcome_message="",
    ):
        if self.add_user_exc is not None and (
            self.fail_on_email is None or self.fail_on_email == email
        ):
            raise self.add_user_exc
        self.calls.append((notebook_id, email, permission, notify))
        if email not in self.silently_ignore:
            self.existing = [e for e in self.existing if self._entry(e)[0] != email]
            self.existing.append((email, permission))
        return await self.get_status(notebook_id)

    async def set_users(self, notebook_id, grants, notify=True, welcome_message=""):
        if self.set_users_exc is not None:
            raise self.set_users_exc
        if not grants:
            raise ValueError("Must provide at least one user grant")
        seen: set[str] = set()
        for email, permission in grants:
            if permission == SharePermission.OWNER:
                raise ValueError("Cannot assign OWNER permission")
            if permission == SharePermission._REMOVE:
                raise ValueError("Use remove_user() instead")
            if email in seen:
                raise ValueError(f"Duplicate email in grants: {email!r}")
            seen.add(email)
        self.calls.append((notebook_id, grants, notify))
        for email, permission in grants:
            if email not in self.silently_ignore:
                self.existing = [
                    e for e in self.existing
                    if self._entry(e)[0].casefold() != email.casefold()
                ]
                self.existing.append((email, permission))
        return self.set_users_result if self.set_users_result is not None else await self.get_status(notebook_id)

    async def get_status(self, notebook_id):
        self.status_calls.append(notebook_id)
        if self.get_status_exc is not None:
            raise self.get_status_exc
        return SimpleNamespace(
            notebook_id=notebook_id,
            shared_users=[
                SimpleNamespace(email=email, permission=permission)
                for email, permission in (self._entry(e) for e in self.existing)
            ],
        )


class FakeClient:
    def __init__(self):
        self.artifacts = FakeArtifacts()
        self.sources = FakeSources()
        self.notebooks = FakeNotebooks()
        self.chat = FakeChat()
        self.research = FakeResearch()
        self.sharing = FakeSharing()


@pytest.fixture
def fake_client():
    client = FakeClient()
    runtime.set_client(client)
    yield client
    runtime.set_client(None)
