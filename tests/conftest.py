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
    async def wait_for_completion(
        self, notebook_id, task_id, initial_interval=2.0, max_interval=10.0, timeout=300.0, poll_interval=None
    ):
        self.calls.append(("wait", dict(notebook_id=notebook_id, task_id=task_id, timeout=timeout)))
        self._wait_count += 1
        if self.fail_wait_on is not None and self._wait_count == self.fail_wait_on:
            raise TimeoutError(f"simulated generation timeout on wait #{self._wait_count}")
        return type("S", (), {"task_id": task_id, "is_failed": False})()

    async def download_audio(self, notebook_id, output_path, artifact_id=None):
        self.calls.append(
            ("download", dict(notebook_id=notebook_id, output_path=output_path, artifact_id=artifact_id))
        )
        return output_path

    async def rename(self, notebook_id, artifact_id, new_title):
        self.calls.append(("rename", dict(artifact_id=artifact_id, new_title=new_title)))
        return None


class FakeSources:
    """Models the NotebookLM notebook's SERVER-SIDE source set: `sources` persists
    independently of any process run, so it can model cross-process resume and the
    reject-then-delete flow. `calls` still records every invocation for assertions."""

    def __init__(self):
        self.calls = []
        self.sources = []  # [{"id", "title"}] — server-side persistent set
        self._counter = 0

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

    async def add_file(self, notebook_id, file_path, mime_type=None, wait=False, wait_timeout=120.0):
        self.calls.append(
            (
                "add_file",
                dict(
                    notebook_id=notebook_id,
                    file_path=str(file_path),
                    mime_type=mime_type,
                    wait=wait,
                    wait_timeout=wait_timeout,
                ),
            )
        )
        return type("Src", (), {"id": self._add(None)})()  # title set later via rename

    async def rename(self, notebook_id, source_id, new_title):
        self.calls.append(("rename", dict(source_id=source_id, new_title=new_title)))
        for s in self.sources:
            if s["id"] == source_id:
                s["title"] = new_title
        return None

    async def add_url(self, notebook_id, url, wait=False, wait_timeout=120.0):
        self.calls.append(("add_url", dict(notebook_id=notebook_id, url=url, wait=wait)))
        return type("Src", (), {"id": self._add(url)})()

    async def add_text(self, notebook_id, title, content, wait=False, wait_timeout=120.0):
        self.calls.append(("add_text", dict(title=title, wait=wait)))
        return type("Src", (), {"id": self._add(title)})()

    async def delete(self, notebook_id, source_id):
        self.calls.append(("delete", dict(source_id=source_id)))
        self.sources = [s for s in self.sources if s["id"] != source_id]
        return True


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
