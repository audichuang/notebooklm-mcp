import pytest

from notebooklm_mcp import runtime


class FakeArtifacts:
    def __init__(self):
        self.calls = []
        # Set to an integer N to make the N-th wait_for_completion call raise
        # TimeoutError (models a real generation timeout for error-path tests).
        self.fail_wait_on = None
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

    def seed_artifacts(self, *arts):
        """Test helper: pre-populate the notebook's artifact set."""
        self.artifacts.extend(arts)

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
        self.calls.append(
            (
                "generate_audio",
                dict(
                    notebook_id=notebook_id,
                    language=language,
                    instructions=instructions,
                    audio_format=audio_format,
                    audio_length=audio_length,
                ),
            )
        )
        # Faithful to the real SDK: GenerationStatus exposes ONLY task_id
        # (task_id IS the artifact id). No artifact_id attribute exists.
        if self.fail_generate:
            return type("S", (), {"task_id": "", "is_failed": True, "status": "failed", "error": "simulated failure"})()
        return type("S", (), {"task_id": "task-123", "is_failed": False})()

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
            raise TimeoutError(f"simulated generation timeout on wait #{self._wait_count}")
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
        return output_path

    async def rename(self, notebook_id, artifact_id, new_title, *, return_object=True):
        self.calls.append(("rename", dict(artifact_id=artifact_id, new_title=new_title,
                                          return_object=return_object)))
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


class FakeSources:
    """Models the NotebookLM notebook's SERVER-SIDE source set: `sources` persists
    independently of any process run, so it can model cross-process resume and the
    reject-then-delete flow. `calls` still records every invocation for assertions."""

    def __init__(self):
        self.calls = []
        self.sources = []  # [{"id", "title"}] — server-side persistent set
        self._counter = 0
        # True(預設)= add_file(title=) 的內部改名成功;False 模擬 0.7.3 的
        # 靜默改名失敗(SDK 只 log,回傳舊 title)。
        self.title_lands = True

    def _add(self, title):
        self._counter += 1
        sid = f"src-{self._counter}"
        self.sources.append({"id": sid, "title": title})
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
                             "kind": "web_page", "is_ready": True})()
            for s in self.sources
        ]

    async def get_fulltext(self, notebook_id, source_id):
        self.calls.append(("get_fulltext", dict(source_id=source_id)))
        return type("FT", (), {"source_id": source_id, "title": "來源標題",
                               "content": "來源全文", "char_count": 4})()

    async def add_url(self, notebook_id, url, *, wait=False, wait_timeout=120.0):
        self.calls.append(("add_url", dict(notebook_id=notebook_id, url=url, wait=wait)))
        return type("Src", (), {"id": self._add(url)})()

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

    async def create(self, title):
        return type("NB", (), {"id": "nb-123", "title": title})()

    async def list(self):
        if self.fail_list:
            raise ValueError("Authentication expired or invalid. Please re-authenticate.")
        return [type("NB", (), {"id": "nb-123", "title": "Test"})()]

    async def get(self, notebook_id):
        return type("NB", (), {"id": notebook_id, "title": "Test", "sources_count": 2,
                               "is_owner": True, "created_at": None})()


class FakeChat:
    def __init__(self):
        self.calls = []

    # Signature mirrors notebooklm-py 0.3.4 ChatAPI.ask (source_ids + conversation_id).
    async def ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        self.calls.append(("ask", dict(question=question, source_ids=source_ids,
                                        conversation_id=conversation_id)))
        refs = [type("Ref", (), {"source_id": "src-1", "citation_number": 1,
                                 "cited_text": "引用片段"})()]
        return type("R", (), {"answer": f"answer to {question}",
                              "conversation_id": conversation_id or "conv-1",
                              "references": refs})()


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
