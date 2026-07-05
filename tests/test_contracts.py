"""Pin notebooklm-py PUBLIC API signatures. Breaks loudly if the SDK changes
under us - our single tripwire against silent upstream API drift."""
import inspect

from notebooklm.rpc.types import AudioFormat, AudioLength


def _params(func):
    return list(inspect.signature(func).parameters)


def test_from_storage_is_sync_context_factory_with_keepalive():
    """0.7.3:from_storage 是同步函式,回傳可直接 `async with` 的 context
    (0.4.x「coroutine 必須 await」慣用法已走入歷史;app.py 用 no-await 寫法)。
    keepalive= 是 session 內背景 RotateCookies 的開關,lifespan 依賴它。
    這裡紅了就要連同 app.py 的呼叫慣用法一起改。"""
    from notebooklm import NotebookLMClient

    assert not inspect.iscoroutinefunction(NotebookLMClient.from_storage)
    p = _params(NotebookLMClient.from_storage)
    assert "keepalive" in p and "keepalive_min_interval" in p


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

    # 0.7.x 尾端加 artifacts_data(預先抓好的 artifact 清單,避免重複 list;我們不傳)
    assert _params(ArtifactsAPI.download_audio) == [
        "self",
        "notebook_id",
        "output_path",
        "artifact_id",
        "artifacts_data",
    ]


def test_list_signature_and_artifact_fields():
    """artifact_list depends on ArtifactsAPI.list + these Artifact attrs."""
    import dataclasses

    from notebooklm._artifacts import ArtifactsAPI
    from notebooklm.types import Artifact

    assert _params(ArtifactsAPI.list) == ["self", "notebook_id", "artifact_type"]
    fields = {f.name for f in dataclasses.fields(Artifact)}
    assert {"id", "title"} <= fields
    assert all(hasattr(Artifact, p) for p in ("kind", "is_completed", "status_str"))


def test_read_surface_signatures_and_fields():
    """source_list / source_fulltext / notebook_get / chat_ask depend on these."""
    import dataclasses

    from notebooklm._chat import ChatAPI
    from notebooklm._notebooks import NotebooksAPI
    from notebooklm._sources import SourcesAPI
    from notebooklm.types import Notebook, Source, SourceFulltext

    # 0.4.1 加了尾端 strict=False(malformed 回應改可 fail-loud;預設維持舊寬鬆行為)
    assert _params(SourcesAPI.list) == ["self", "notebook_id", "strict"]
    assert _params(SourcesAPI.get_fulltext) == ["self", "notebook_id", "source_id", "output_format"]
    assert _params(NotebooksAPI.get) == ["self", "notebook_id"]
    # chat_ask focuses on a subset / continues a thread via these kwargs.
    assert _params(ChatAPI.ask) == ["self", "notebook_id", "question", "source_ids", "conversation_id"]

    assert {"id", "title"} <= {f.name for f in dataclasses.fields(Source)}
    assert all(hasattr(Source, p) for p in ("kind", "is_ready"))
    assert {"source_id", "content", "char_count"} <= {f.name for f in dataclasses.fields(SourceFulltext)}
    assert {"id", "title", "sources_count", "is_owner"} <= {f.name for f in dataclasses.fields(Notebook)}


def test_wait_for_completion_has_task_id_and_timeout():
    from notebooklm._artifacts import ArtifactsAPI

    p = _params(ArtifactsAPI.wait_for_completion)
    assert p[1] == "notebook_id" and p[2] == "task_id"
    assert "timeout" in p


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


def test_rename_signatures_gained_return_object():
    """0.7.0 起 rename() 有 return_object,**預設 True:改名後會再抓一次全量清單
    驗證、找不到會 raise**。我們所有 rename 呼叫點顯式傳 return_object=False,
    保留 0.4.1 的 fire-and-forget 語意(不多打 RPC、不引入新失敗模式)。"""
    from notebooklm._artifacts import ArtifactsAPI
    from notebooklm._sources import SourcesAPI

    assert _params(ArtifactsAPI.rename) == ["self", "notebook_id", "artifact_id", "new_title", "return_object"]
    assert _params(SourcesAPI.rename) == ["self", "notebook_id", "source_id", "new_title", "return_object"]


def test_audio_enum_members():
    assert AudioFormat.DEEP_DIVE == 1 and AudioFormat.DEBATE == 4
    assert AudioLength.SHORT == 1 and AudioLength.DEFAULT == 2 and AudioLength.LONG == 3


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


def test_chat_ask_signature_and_answer_field():
    from notebooklm import AskResult
    from notebooklm._chat import ChatAPI

    assert _params(ChatAPI.ask) == ["self", "notebook_id", "question", "source_ids", "conversation_id"]
    assert "answer" in getattr(AskResult, "__dataclass_fields__", {})


def test_source_signatures_and_fields():
    from notebooklm import Source
    from notebooklm._sources import SourcesAPI

    assert _params(SourcesAPI.add_url) == ["self", "notebook_id", "url", "wait", "wait_timeout"]
    # 0.7.x add_text 尾端加 idempotent(重試防重複;我們不傳,預設即可)
    assert _params(SourcesAPI.add_text) == ["self", "notebook_id", "title", "content", "wait", "wait_timeout", "idempotent"]
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
        "self", "notebook_id", "output_path", "artifact_id", "output_format", "artifacts_data",
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
        "self", "notebook_id", "output_path", "artifact_id", "artifacts_data",
    ]


def test_slide_and_report_enum_members():
    from notebooklm.types import SlideDeckFormat, SlideDeckLength, ReportFormat

    assert SlideDeckFormat.DETAILED_DECK == 1 and SlideDeckFormat.PRESENTER_SLIDES == 2
    assert SlideDeckLength.DEFAULT == 1 and SlideDeckLength.SHORT == 2
    assert ReportFormat.STUDY_GUIDE.value == "study_guide"
    assert ReportFormat.BRIEFING_DOC.value == "briefing_doc"
    assert ReportFormat.BLOG_POST.value == "blog_post"
