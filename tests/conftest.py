import pytest

from notebooklm_mcp import runtime


class FakeArtifacts:
    def __init__(self):
        self.calls = []

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
        return type("S", (), {"task_id": "task-123", "artifact_id": "art-123"})()

    async def wait_for_completion(self, notebook_id, task_id, timeout=300.0, **kw):
        self.calls.append(("wait", dict(notebook_id=notebook_id, task_id=task_id, timeout=timeout)))
        return type("S", (), {"task_id": task_id, "artifact_id": "art-123"})()

    async def download_audio(self, notebook_id, output_path, artifact_id=None):
        self.calls.append(
            ("download", dict(notebook_id=notebook_id, output_path=output_path, artifact_id=artifact_id))
        )
        return output_path

    async def rename(self, notebook_id, artifact_id, new_title):
        self.calls.append(("rename", dict(artifact_id=artifact_id, new_title=new_title)))
        return None


class FakeSources:
    def __init__(self):
        self.calls = []

    async def add_file(self, notebook_id, file_path, mime_type=None, *, wait=False, wait_timeout=120.0):
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
        return type("Src", (), {"id": "src-123"})()

    async def add_url(self, notebook_id, url, wait=False, wait_timeout=120.0):
        self.calls.append(("add_url", dict(notebook_id=notebook_id, url=url, wait=wait)))
        return type("Src", (), {"id": "src-url"})()

    async def add_text(self, notebook_id, title, content, wait=False, wait_timeout=120.0):
        self.calls.append(("add_text", dict(title=title, wait=wait)))
        return type("Src", (), {"id": "src-text"})()

    async def delete(self, notebook_id, source_id):
        self.calls.append(("delete", dict(source_id=source_id)))
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
