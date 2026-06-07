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


def test_generation_status_task_id_is_the_artifact_id():
    """The composite tools derive artifact_id from task_id. The SDK documents
    they are the SAME identifier and GenerationStatus exposes only task_id — it
    has NO artifact_id field. If a future SDK adds a distinct artifact_id, revisit
    _run_episode / generate_audio which currently treat task_id AS the artifact id."""
    from notebooklm import GenerationStatus

    fields = set(getattr(GenerationStatus, "__dataclass_fields__", {}))
    assert "task_id" in fields
    assert "artifact_id" not in fields
