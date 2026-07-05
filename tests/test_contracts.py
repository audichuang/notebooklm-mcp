"""Pin notebooklm-py PUBLIC API signatures. Breaks loudly if the SDK changes
under us - our single tripwire against silent upstream API drift."""
import inspect

from notebooklm.rpc.types import AudioFormat, AudioLength


def _params(func):
    return list(inspect.signature(func).parameters)


def test_generate_audio_signature():
    from notebooklm._artifacts import ArtifactsAPI

    p = _params(ArtifactsAPI.generate_audio)
    assert p[:6] == [
        "self",
        "notebook_id",
        "source_ids",
        "language",
        "instructions",
        "audio_format",
    ]
    assert "audio_length" in p


def test_download_audio_arg_order():
    from notebooklm._artifacts import ArtifactsAPI

    assert _params(ArtifactsAPI.download_audio) == [
        "self",
        "notebook_id",
        "output_path",
        "artifact_id",
    ]


def test_wait_for_completion_has_task_id_and_timeout():
    from notebooklm._artifacts import ArtifactsAPI

    p = _params(ArtifactsAPI.wait_for_completion)
    assert p[1] == "notebook_id" and p[2] == "task_id"
    assert "timeout" in p


def test_add_file_accepts_mime_and_wait():
    from notebooklm._sources import SourcesAPI

    p = _params(SourcesAPI.add_file)
    assert p[:3] == ["self", "notebook_id", "file_path"]
    assert p == [
        "self",
        "notebook_id",
        "file_path",
        "mime_type",
        "wait",
        "wait_timeout",
    ]


def test_rename_signatures_have_no_return_object():
    """0.3.4 rename() takes only (notebook_id, id, new_title) — NO return_object
    (that kwarg exists on GitHub HEAD but not the pinned 0.3.x). Passing it raises
    TypeError at runtime. Pin both so the drift is caught offline."""
    from notebooklm._artifacts import ArtifactsAPI
    from notebooklm._sources import SourcesAPI

    assert _params(ArtifactsAPI.rename) == ["self", "notebook_id", "artifact_id", "new_title"]
    assert _params(SourcesAPI.rename) == ["self", "notebook_id", "source_id", "new_title"]


def test_audio_enum_members():
    assert AudioFormat.DEEP_DIVE == 1 and AudioFormat.DEBATE == 4
    assert AudioLength.SHORT == 1 and AudioLength.DEFAULT == 2 and AudioLength.LONG == 3


def test_wait_for_completion_full_signature():
    # Installed 0.3.4 ends with poll_interval (GitHub HEAD differs — trust installed).
    from notebooklm._artifacts import ArtifactsAPI

    assert _params(ArtifactsAPI.wait_for_completion) == [
        "self",
        "notebook_id",
        "task_id",
        "initial_interval",
        "max_interval",
        "timeout",
        "poll_interval",
    ]


def test_chat_ask_signature_and_answer_field():
    from notebooklm import AskResult
    from notebooklm._chat import ChatAPI

    assert _params(ChatAPI.ask) == ["self", "notebook_id", "question", "source_ids", "conversation_id"]
    assert "answer" in getattr(AskResult, "__dataclass_fields__", {})


def test_source_signatures_and_fields():
    from notebooklm import Source
    from notebooklm._sources import SourcesAPI

    assert _params(SourcesAPI.add_url) == ["self", "notebook_id", "url", "wait", "wait_timeout"]
    assert _params(SourcesAPI.add_text) == ["self", "notebook_id", "title", "content", "wait", "wait_timeout"]
    assert _params(SourcesAPI.delete) == ["self", "notebook_id", "source_id"]
    assert "id" in getattr(Source, "__dataclass_fields__", {})


def test_notebook_signatures_and_fields():
    from notebooklm import Notebook
    from notebooklm._notebooks import NotebooksAPI

    assert _params(NotebooksAPI.create) == ["self", "title"]
    assert _params(NotebooksAPI.list) == ["self"]
    assert {"id", "title"} <= set(getattr(Notebook, "__dataclass_fields__", {}))


def test_generation_status_has_is_failed_for_failure_detection():
    # ensure_started/ensure_completed rely on is_failed to fail fast on a failed status.
    from notebooklm import GenerationStatus

    assert hasattr(GenerationStatus(task_id="x", status="completed"), "is_failed")


def test_generation_status_task_id_is_the_artifact_id():
    """The composite tools derive artifact_id from task_id. The SDK documents
    they are the SAME identifier and GenerationStatus exposes only task_id — it
    has NO artifact_id field. If a future SDK adds a distinct artifact_id, revisit
    _run_episode / generate_audio which currently treat task_id AS the artifact id."""
    from notebooklm import GenerationStatus

    fields = set(getattr(GenerationStatus, "__dataclass_fields__", {}))
    assert "task_id" in fields
    assert "artifact_id" not in fields


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
