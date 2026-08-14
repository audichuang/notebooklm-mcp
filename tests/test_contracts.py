"""Pin notebooklm-py PUBLIC API signatures. Breaks loudly if the SDK changes
under us - our single tripwire against silent upstream API drift."""
import inspect

import httpx
import pytest

from notebooklm.rpc.types import AudioFormat, AudioLength


def _params(func):
    return list(inspect.signature(func).parameters)


def test_from_storage_is_sync_context_factory():
    """0.7.3:from_storage 是同步函式,回傳可直接 `async with` 的 context
    (0.4.x「coroutine 必須 await」慣用法已走入歷史;app.py 用 no-await 寫法)。
    這裡紅了就要連同 app.py 的呼叫慣用法一起改。"""
    from notebooklm import NotebookLMClient

    assert not inspect.iscoroutinefunction(NotebookLMClient.from_storage)
    p = _params(NotebookLMClient.from_storage)
    assert p[:3] == ["path", "timeout", "profile"]


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

    # 0.8.1 加了 statuses/types 來源過濾。
    assert _params(SourcesAPI.list) == ["self", "notebook_id", "strict", "statuses", "types"]
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
    """rename() 的 return_object 仍在,且仍是 keyword-only。

    **語意在 0.8.0(#1362)變了**:`return_object=False` 不再短路。0.7.x 的 False
    是真 fire-and-forget(不查、不 raise、成功回 None);0.8.0 兩種模式都跑存在性
    檢查,False 只是「成功時回 None」。我們仍然一律傳 False——artifacts 兩邊等價,
    sources 的 False 在 RPC 有回 echo 時仍會短路(省一次 fetch),而且我們本來就
    不用回傳物件。行為差異記在 tools_podcast._finalize_episode 的註解與 AGENTS.md。
    """
    from notebooklm._artifacts import ArtifactsAPI
    from notebooklm._sources import SourcesAPI

    assert _params(ArtifactsAPI.rename) == ["self", "notebook_id", "artifact_id", "new_title", "return_object"]
    assert _params(SourcesAPI.rename) == ["self", "notebook_id", "source_id", "new_title", "return_object"]
    # return_object 是 keyword-only:鎖住它,擋未來有人寫成位置參數(我方一律 keyword 傳 False)。
    for func in (ArtifactsAPI.rename, SourcesAPI.rename):
        assert inspect.signature(func).parameters["return_object"].kind is inspect.Parameter.KEYWORD_ONLY


def test_rename_false_no_longer_short_circuits():
    """0.8.0 的 rename(return_object=False) **會**做存在性檢查(#1362)。

    這條鎖的是「行為」而非簽名:0.7.x 的短路是 `if not return_object …: return None`,
    0.8.0 拿掉了。上游哪天又改回短路,`audio_finalize` 的 rename 例外處理就會變成
    永遠走不到的死碼,而 `_finalize_episode` 的註解會變成謊話——這裡先紅。

    用原始碼比對而不是打 RPC:contract 測試必須離線。
    """
    import inspect as _inspect

    from notebooklm._artifacts import ArtifactsAPI
    from notebooklm._sources import SourcesAPI

    art_src = _inspect.getsource(ArtifactsAPI.rename)
    # 0.7.x 的短路長這樣;0.8.0 應該已經不存在。
    assert "if not return_object and not future_errors_enabled()" not in art_src
    assert "ArtifactNotFoundError" in art_src, "artifacts.rename 應該仍會在查不到時 raise"
    src_src = _inspect.getsource(SourcesAPI.rename)
    assert "SourceNotFoundError" in src_src, "sources.rename 應該仍會在查不到時 raise"


def test_generation_kickoff_refuses_by_raising():
    """0.8.0(ADR-0019 / #1342):同步拒絕改成 raise,不再回 status='failed'。

    這是 `_REFUSED_WITHOUT_DISPATCH` 分類的**唯一依據**。上游若退回舊契約,
    那個 except 分支會靜默失效、配額拒絕又會被標成 acceptance_unknown——
    所以把「這兩種例外型別存在」與「kickoff 的文件講明會 raise」一起鎖住。
    """
    import inspect as _inspect

    from notebooklm.exceptions import ArtifactFeatureUnavailableError, RateLimitError
    from notebooklm._artifact import generation

    assert issubclass(RateLimitError, Exception)
    assert issubclass(ArtifactFeatureUnavailableError, Exception)
    # 「artifact id 缺席 = 沒有建出 task」是我們把它歸成 not_accepted 的理由。
    parse_src = _inspect.getsource(
        generation.ArtifactGenerationService._parse_generation_result
    )
    assert "raise ArtifactFeatureUnavailableError" in parse_src


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

    # 0.8.0 add_url 尾端加 title(可在 add 時直接命名,省掉 add→rename 兩步;
    # podcast 流程目前仍走顯式兩步,因為 add_file 的 title= 內部就是那兩步且會靜默失敗)。
    assert _params(SourcesAPI.add_url) == ["self", "notebook_id", "url", "wait", "wait_timeout", "title"]
    # 0.7.x add_text 尾端加 idempotent(重試防重複;我們不傳,預設即可)
    assert _params(SourcesAPI.add_text) == ["self", "notebook_id", "title", "content", "wait", "wait_timeout", "idempotent"]
    assert _params(SourcesAPI.delete) == ["self", "notebook_id", "source_id"]
    assert "id" in getattr(Source, "__dataclass_fields__", {})


def test_source_created_at_is_timezone_aware():
    """0.8.0 起 `Source.created_at` 是 **aware UTC**(0.7.x 是 host-local naive)。

    `tests/conftest.py` 的 fake source 必須跟實裝版本同形,否則會重演那次事故:
    測試綠、production 把每一筆 source 都濾掉,response-loss 後永遠卡在
    acceptance_unknown。上游哪天再翻回 naive,這裡先紅,提醒同步改 fake。
    (`_created_at_utc` 本身兩種都吃,有專屬單元測試——這條鎖的是「fake 有沒有說謊」。)
    """
    from datetime import datetime, timezone

    from notebooklm._types.common import _datetime_from_timestamp

    stamped = _datetime_from_timestamp(1_785_000_000)
    assert isinstance(stamped, datetime)
    assert stamped.tzinfo is not None, "上游翻回 naive 了 —— conftest 的 fake 要跟著改"
    assert stamped.utcoffset() == timezone.utc.utcoffset(None)


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


def test_generation_status_has_is_removed_for_quota_removal():
    """0.6.0 起 GenerationStatus.is_removed 區分「被伺服器下架(通常配額)」與 is_failed。
    ensure_completed 依賴 is_removed 一併 fail-loud;此屬性消失就要回頭改 _status.py。"""
    from notebooklm import GenerationStatus

    s = GenerationStatus(task_id="x", status="removed")
    assert hasattr(s, "is_removed") and s.is_removed is True
    assert s.is_failed is False  # removed 不是 failed —— 正是 _status 必須各別擋的原因


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
    # CUSTOM 是 generate_report(custom_prompt=…) 的前提;它消失就要回頭改 enums 白名單。
    assert ReportFormat.CUSTOM.value == "custom"


def test_quota_rescue_signatures():
    """artifact_revise_slide / artifact_retry_failed 依賴這兩支省配額 API。"""
    from notebooklm._artifacts import ArtifactsAPI

    assert _params(ArtifactsAPI.revise_slide) == [
        "self", "notebook_id", "artifact_id", "slide_index", "prompt",
    ]
    assert _params(ArtifactsAPI.retry_failed) == ["self", "notebook_id", "artifact_id"]


def test_share_permission_importable_from_rpc_types():
    """`tools_basic` 在 module level `from notebooklm.rpc.types import
    SharePermission`——上游搬家 = 整個 tools_basic import 失敗 = server 起不來。
    數值也鎖住:`_has_sufficient_permission` 靠 OWNER(1) < EDITOR(2) < VIEWER(3)
    的排序把 OWNER 也歸進「已足夠」。"""
    from notebooklm.rpc.types import SharePermission

    assert SharePermission.OWNER.value == 1
    assert SharePermission.EDITOR.value == 2
    assert SharePermission.VIEWER.value == 3


def test_add_user_permission_is_positional():
    """`tools_basic._share_each` 位置傳
    `add_user(notebook_id, email, SharePermission.EDITOR, notify=False)`。
    0.7.0 曾把 source add 的尾端參數改成 keyword-only 過一次(上面已鎖),同樣的事
    發生在 sharing 就是 TypeError——這裡先紅,而不是等到呼叫時才炸。"""
    from notebooklm._sharing import SharingAPI

    assert _params(SharingAPI.add_user) == [
        "self", "notebook_id", "email", "permission", "notify", "welcome_message",
    ]
    kind = inspect.signature(SharingAPI.add_user).parameters["permission"].kind
    assert kind is not inspect.Parameter.KEYWORD_ONLY


def test_set_users_signature():
    """0.8.1 的批次分享契約變動時要同步改 tools_basic._share_each。

    它是 upsert 而不是覆寫；add_user 現在只是它的 wrapper。
    上游若拿掉尾端 `return await self.get_status(...)` 改回傳 None，
    `_has_sufficient_permission(None, email)` 會誤判每次成功分享未生效，觸發重跑。
    """
    from typing import get_type_hints

    from notebooklm._sharing import SharingAPI
    from notebooklm.types import ShareStatus

    assert _params(SharingAPI.set_users) == [
        "self", "notebook_id", "grants", "notify", "welcome_message",
    ]
    assert get_type_hints(SharingAPI.set_users)["return"] is ShareStatus


def test_rpc_permission_code_helpers():
    """這條紅了代表要跟著改 notebooklm_mcp/_errors.py 的權限判斷。

    _errors.is_permission_denied 依賴上游的 GrpcStatusCode 與 normalize_rpc_code。
    """
    from notebooklm.rpc.types import GrpcStatusCode, normalize_rpc_code

    assert int(GrpcStatusCode.PERMISSION_DENIED) == 7
    assert normalize_rpc_code(7) == 7
    assert normalize_rpc_code("7") == 7
    assert normalize_rpc_code(None) is None


def test_ask_result_has_structured_answer_document():
    """這條紅了代表要跟著改 notebooklm_mcp/tools_basic.py 的 chat_ask。

    chat_ask 的 strip_citations 依賴 answer_document 不帶 inline [N] 標記；
    上游文件保證無法解碼時它是 empty，而不是 None。
    tools_basic.chat_ask 的 strip_citations 也依賴 render() 的可讀性(block 之間可分隔)；
    這條紅了代表要重新決定 chat_ask 該用 .text 還是 .render()。
    """
    import dataclasses
    from typing import get_type_hints

    from notebooklm import AskResult
    from notebooklm.types import DocumentBlock, StructuredDocument, TextSpan

    fields = {f.name for f in dataclasses.fields(AskResult)}
    assert "answer_document" in fields
    assert get_type_hints(AskResult)["answer_document"] is StructuredDocument
    doc = StructuredDocument(
        blocks=(
            DocumentBlock(0, 5, spans=(TextSpan(0, 5, "Hello"),)),
            DocumentBlock(5, 10, spans=(TextSpan(5, 10, "World"),)),
        ),
    )
    assert doc.render() == "Hello\nWorld"
    assert "\n" not in doc.text and doc.text == "HelloWorld"
    assert "[1]" not in doc.render()


def test_from_storage_has_allow_headless_guard():
    """這條紅了代表要跟著改 notebooklm_mcp/app.py 的認證護欄。

    app.py 會顯式傳 False，阻止 L3 headless re-auth。
    """
    from notebooklm import NotebookLMClient

    parameter = inspect.signature(NotebookLMClient.from_storage).parameters["allow_headless"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False


def test_artifact_exposes_source_ids():
    """這條紅了代表要跟著改 notebooklm_mcp/tools_basic.py 的 artifact_list。

    artifact_list 會把 source_ids 帶出來作為觀測面；本輪尚未以真實驗收把它當 gate。
    """
    import dataclasses
    from datetime import datetime
    from typing import get_type_hints

    from notebooklm.types import Artifact
    import notebooklm.types as notebooklm_types

    assert "source_ids" in {f.name for f in dataclasses.fields(Artifact)}
    hints = get_type_hints(
        Artifact,
        globalns={**vars(notebooklm_types), "datetime": datetime},
    )
    assert hints["source_ids"] == tuple[str, ...]


def test_psidts_recovery_and_cookie_sanitizer_private_surface():
    """`_cookies.assert_usable_storage_state` 依賴這幾個上游私有函式判斷
    「這份憑證會不會讓 0.8.1 的載入路徑觸發 inline RotateCookies」。

    這是私有 API,所以更需要 tripwire:紅了就要回頭確認 heal 的觸發條件
    有沒有換地方(F1 那個 agent 正在寫用到它們的程式碼,這裡只負責鎖)。
    """
    from notebooklm._auth import cookies, psidts_recovery

    assert _params(psidts_recovery._psidts_routes_to_rotate) == [
        "entries", "to_cookie", "now",
    ]
    assert _params(psidts_recovery._storage_cookie) == ["entry"]
    assert _params(cookies._sanitized_auth_entries) == ["storage_state"]


def test_share_status_and_shared_user_fields():
    """`notebook_share_with_pool` / `_has_sufficient_permission` 依賴
    `ShareStatus.shared_users[].email` / `.permission` 這兩個欄位形狀。"""
    import dataclasses

    from notebooklm.types import ShareStatus, SharedUser

    assert "shared_users" in {f.name for f in dataclasses.fields(ShareStatus)}
    assert {"email", "permission"} <= {f.name for f in dataclasses.fields(SharedUser)}


def test_research_api_surface():
    """research_start / research_wait / research_import 依賴的 ResearchAPI 契約。

    注意 `select_cited_sources` **不在** ResearchAPI 上——它是 notebooklm.research 的
    module-level 純函式(不打 RPC)。cited 判定因此是本地計算,MCP 只回事實標記,
    要不要 cited-only 由 host 決定(見 ADR-0008)。"""
    from notebooklm._research import ResearchAPI
    from notebooklm.research import extract_report_urls, normalize_citation_url

    assert _params(ResearchAPI.start) == ["self", "notebook_id", "query", "source", "mode"]
    assert _params(ResearchAPI.poll) == ["self", "notebook_id", "task_id"]
    p = _params(ResearchAPI.wait_for_completion)
    assert p[:3] == ["self", "notebook_id", "task_id"] and "timeout" in p
    p = _params(ResearchAPI.import_sources_with_verification)
    assert p[:4] == ["self", "notebook_id", "task_id", "sources"] and "max_elapsed" in p
    # 純函式,不是 API 方法——MCP 用它們算 cited 標記。
    assert callable(extract_report_urls) and callable(normalize_citation_url)


def test_import_identity_differs_from_citation_identity():
    """`research_import` 的選取 key 必須用 **import** normalizer,不是 citation 的那顆。

    SDK 自己的 docstring 就寫明兩者 distinct:citation 版 strip 尾端標點、保留 fragment;
    import 版丟掉 fragment(伺服器存的時候剝掉)、不 strip 標點。用錯會讓 `#a`/`#b` 兩個
    候選在我們眼中是兩筆、在 SDK 的 timeout readback 對帳中是同一筆。

    `_normalize_import_verification_url` 是私有 API —— 這個測試就是它的 tripwire:
    上游改名時這裡先紅,而不是等到 production 匯入時 ImportError。"""
    from notebooklm._research import _normalize_import_verification_url as import_key
    from notebooklm.research import normalize_citation_url as cite_key

    frag = "https://Example.com/a/#section"
    assert import_key(frag) == "https://example.com/a"        # fragment 丟掉
    assert cite_key(frag) == "https://example.com/a#section"  # fragment 保留
    dotted = "https://example.com/a."
    assert import_key(dotted) == "https://example.com/a."     # 標點保留
    assert cite_key(dotted) == "https://example.com/a"        # 標點 strip
    # 兩顆都不 strip 前後空白 —— 呼叫端傳進來的 URL 必須自己先 strip。
    assert import_key(" https://example.com/a ") != import_key("https://example.com/a")


def test_research_task_and_source_fields():
    import dataclasses

    from notebooklm._types.research import ResearchSource, ResearchStart, ResearchStatus, ResearchTask

    assert {"task_id", "status", "query", "sources", "summary", "report"} <= {
        f.name for f in dataclasses.fields(ResearchTask)
    }
    assert {"url", "title", "result_type", "research_task_id"} <= {
        f.name for f in dataclasses.fields(ResearchSource)
    }
    assert {"task_id", "report_id", "mode"} <= {f.name for f in dataclasses.fields(ResearchStart)}
    # research_wait 依 status 決定成功/fail-loud;這四個值都要在。
    assert {ResearchStatus.COMPLETED, ResearchStatus.IN_PROGRESS,
            ResearchStatus.FAILED, ResearchStatus.NOT_FOUND} <= set(ResearchStatus)
    assert ResearchSource(url="u", title="t").is_report is False


def test_console_script_name_does_not_collide_with_upstream():
    """我們的 server 命令必須是 `nblm-mcp`,**不能**叫 `notebooklm-mcp`。

    notebooklm-py 0.8.0 起自己也宣告了一支 `notebooklm-mcp`(它自己的 MCP server)。
    兩個套件裝進同一個 uv tool venv,`bin/` 只留最後寫入的那份 —— 實測全新
    `uv tool install` **3/3 都是上游贏**,而上游那支缺 `fastmcp` 就 ModuleNotFoundError,
    等於裝完就是壞的。改名是唯一能讓它確定性正確的辦法。

    這條同時是**退場觸發器**:上游哪天不再宣告這支,撞名就消失,那時可以考慮把
    `notebooklm-mcp` 這個名字收回來(舊 config 就能自動痊癒)。
    """
    import importlib.metadata as md

    ours = {ep.name: ep.value for ep in md.distribution("notebooklm-mcp").entry_points}
    assert ours.get("nblm-mcp") == "notebooklm_mcp.server:main"
    assert "notebooklm-mcp" not in ours, (
        "重新宣告 notebooklm-mcp 會撞上 notebooklm-py 的同名 script,全新安裝會拿到壞的那支"
    )

    upstream = {ep.name for ep in md.distribution("notebooklm-py").entry_points}
    assert "notebooklm-mcp" in upstream, (
        "上游不再宣告 notebooklm-mcp —— 撞名消失了,可以考慮把這個命令名收回來"
    )


def test_upstream_login_accepts_rebrand_host():
    """The pinned SDK's native login must accept Google's rebranded landing host."""
    from notebooklm.cli.services.playwright_login import url_matches_base_host

    assert url_matches_base_host("https://notebook.google.com/")


def test_relogin_hint_uses_the_native_login_command():
    from notebooklm_mcp.auth_probe import RELOGIN_HINT

    assert "uv run notebooklm login" in RELOGIN_HINT
    assert "sync-auth.sh" in RELOGIN_HINT


def test_sync_auth_uses_the_sdk_profile_path_resolver():
    """Default and named profiles must follow the SDK's storage migration rules."""
    from pathlib import Path

    script = Path("scripts/sync-auth.sh").read_text(encoding="utf-8")
    assert "get_storage_path" in script
    assert 'NBLM_HOME=' not in script


def test_auth_probe_matches_the_sdk_http_auth_error_shape():
    """The compatibility shim must follow the HTTP cause retained by SDK 0.8.0."""
    from notebooklm._rpc_executor import RpcExecutor
    from notebooklm._runtime import is_auth_error
    from notebooklm.exceptions import RPCError
    from notebooklm.rpc.types import RPCMethod

    from notebooklm_mcp.auth_probe import _is_probe_auth_error

    assert callable(is_auth_error)
    request = httpx.Request("POST", "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute")
    for status in (401, 403):
        response = httpx.Response(status, request=request)
        http_error = httpx.HTTPStatusError(
            response.reason_phrase, request=request, response=response
        )
        with pytest.raises(RPCError) as caught:
            RpcExecutor.raise_rpc_error_from_http_status(
                None, http_error, RPCMethod.LIST_NOTEBOOKS
            )
        assert caught.value.__cause__ is http_error
        assert _is_probe_auth_error(caught.value)


def test_generation_takes_its_source_list_from_the_notebook_not_the_server():
    """不傳 `source_ids` 時,生成用的來源清單是**在 client 端從 notebook 撈出來的**。

    這條是「`source_delete` 之後那筆不會再進入生成」的**唯一依據**,而那件事無法從
    外部觀察 —— v0.9.0 驗收就卡在這裡判 INCONCLUSIVE:刪掉之後 `source_list` 看不到它,
    但 `source_fulltext` 55 分鐘後仍讀得回全文,所以「從筆記本移除」與「後端不再持有」
    顯然不是同一件事,而 ADR-0009 那條清理義務的效果因此證不出來。

    解法不是再燒配額做生成對照,是把三環釘住(全部可離線驗):

    1. `generate_audio(source_ids=None)` 會呼叫 `notebooks.get_source_ids()` 拿清單,
       再把**明確的 id 列表**送進 RPC —— 不是讓伺服器自己挑。
    2. `get_source_ids` 走 `get_raw()` → **`GET_NOTEBOOK`**,而 `sources.list`
       (我方 `source_list`)走的**也是** `GET_NOTEBOOK`。同一支 RPC、同一份資料 ⇒
       `source_list` 看不到 ⟺ 生成的清單裡也沒有它。
    3. `get_fulltext` 的 params **只有 source_id、沒有 notebook_id** —— 它直接查 source
       物件,繞過 notebook。所以「刪掉還讀得回」是預期的,不是清理失敗。

    三環有任何一環被上游改掉,這個推導就失效,清理義務要重新論證 —— 那正是這條測試
    要攔的。**不要因為它綠就以為驗過了生成端**:它證明的是推導的前提,不是端到端行為。
    """
    from notebooklm._artifact.generation import ArtifactGenerationService
    from notebooklm._notebooks import NotebooksAPI
    from notebooklm._source.content import SourceContentRenderer
    from notebooklm._source.listing import SourceLister

    gen = inspect.getsource(ArtifactGenerationService.generate_audio)
    assert "get_source_ids" in gen, (
        "生成不再於 client 端撈 notebook 的來源清單 —— 若改成伺服器自己挑,"
        "『source_delete 之後不會進生成』就完全失去依據"
    )

    assert "GET_NOTEBOOK" in inspect.getsource(NotebooksAPI.get_raw)
    assert "GET_NOTEBOOK" in inspect.getsource(SourceLister.list), (
        "sources.list 與 get_source_ids 不再同源 —— source_list 的觀察結果推不出生成端行為"
    )

    fulltext = inspect.getsource(SourceContentRenderer)
    assert "[[source_id]" in fulltext and "notebook_id" not in fulltext.split("params =")[1][:120], (
        "get_fulltext 開始帶 notebook_id 了 —— 那樣『刪掉還讀得回』就變成真的異常,要重查"
    )
