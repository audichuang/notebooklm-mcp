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


def test_default_backend_is_still_web():
    """0.8.2 起 client 有 web / android 兩個 backend,**我們整套都建在 web 上**。

    android 走 master token + gRPC,不吃 cookie —— 一旦上游把預設翻成 android,
    `_auth.psidts_recovery` / keepalive / `_sanitized_auth_entries` 這些 pool 的
    憑證機制會整組變成死碼,而**本檔其餘 `getsource` 斷言全都釘在 `_web.*` 上、
    照樣綠**(它們驗的是 web 實作,不是實際跑的那個 backend)。所以預設值本身
    要有人釘住。

    `explicit=None`(我們不傳 `backend=`)+ `env=None`(不設 NOTEBOOKLM_BACKEND)
    是我們的實際呼叫形狀。
    """
    import inspect as _inspect

    from notebooklm import _client_assembly

    pref = _client_assembly.resolve_backend_preference(explicit=None, env=None)
    assert (pref.preferred, pref.reason) == ("web", "default")

    # 但預設值只在「沒人設環境變數」時成立,而這條測試自己看不見環境。真正的 fail-closed
    # 在 `app._INLINE_AUTH_ENV_OVERRIDES` 把 `NOTEBOOKLM_BACKEND` 刪掉 —— 那道護欄綁的是
    # **字面字串**(上游沒有導出常數),所以名字要在這裡對回 SDK,否則上游改名之後我們的
    # 覆寫會靜默變成 no-op,而症狀是「某天 pool 整個換 backend」。
    from notebooklm_mcp.app import _BACKEND_ENV, _INLINE_AUTH_ENV_OVERRIDES

    assert f'os.environ.get("{_BACKEND_ENV}")' in _inspect.getsource(
        _client_assembly
    ), "SDK 讀的 backend 環境變數名改了,app 的 inline 覆寫要跟著改"
    assert _INLINE_AUTH_ENV_OVERRIDES[_BACKEND_ENV] is None


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
    # `role` 是 0.8.1 新增的,`notebook_get` 直取 `nb.role`(無 getattr 防護)——
    # 上游改名時要在這裡紅,而不是在生產拋 AttributeError。
    assert {"id", "title", "sources_count", "is_owner", "role"} <= {
        f.name for f in dataclasses.fields(Notebook)
    }


def test_notebook_is_owner_only_tracks_role_when_role_decoded():
    """`notebook_get` 的 docstring 對呼叫端保證的那個不變式,鎖在這裡。

    0.8.1 修好了 `is_owner`(從 `meta[1]`「有沒有共享者」搬到 `meta[0]` userRole),
    但只在 **`role` 解得出值** 時才與它同步:`role is None`(meta 缺失/太短,或
    userRole 帶了預期外的值)時 `is_owner` 停在欄位預設 `True`。

    **升版前這個欄位錯的方向是「恆為 False」(保守),0.8.1 之後 schema drift 時
    錯的方向變成「宣稱自己是 owner」(樂觀)** —— 所以 `notebook_get` 必須同時
    轉發 `role`,呼叫端才分得出「真的是 owner」與「role 未知的樂觀預設」。
    這條紅了代表那個 docstring 的保證不再成立,兩處要一起改。
    """
    from notebooklm.rpc.types import SharePermission
    from notebooklm.types import Notebook

    assert Notebook(id="x", title="t").role is None
    assert Notebook(id="x", title="t").is_owner is True  # 樂觀預設,不是「查到是 owner」
    assert Notebook(id="x", title="t", role=SharePermission.VIEWER).is_owner is False
    assert Notebook(id="x", title="t", role=SharePermission.OWNER).is_owner is True


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

    **0.8.2 起 `getsource` 一律要對 `_web.*` 的實作**:那一版把公開 facade
    (`ArtifactsAPI` / `SourcesAPI` / `NotebooksAPI` …)改成 ABC,方法體只剩
    `raise NotImplementedError`,實作搬到 `_web.*`(另一半是新的 android backend)。
    對 ABC 抓原始碼**不會爆,只會靜默失去意義** —— `"短路字串" not in stub` 恆真,
    這條測試會從此永遠綠。簽名斷言(`_params`)留在 facade 是對的(那是兩個 backend
    共同的契約、也是我們實際呼叫的東西),**只有讀 body 的斷言要下沉**。
    """
    import inspect as _inspect

    from notebooklm._web.artifacts import WebArtifactsAPI
    from notebooklm._web.sources import WebSourcesAPI

    art_src = _inspect.getsource(WebArtifactsAPI.rename)
    # 0.7.x 的短路長這樣;0.8.0 應該已經不存在。
    assert "if not return_object and not future_errors_enabled()" not in art_src
    assert "ArtifactNotFoundError" in art_src, "artifacts.rename 應該仍會在查不到時 raise"
    src_src = _inspect.getsource(WebSourcesAPI.rename)
    assert "SourceNotFoundError" in src_src, "sources.rename 應該仍會在查不到時 raise"


def test_generation_kickoff_refuses_by_raising():
    """0.8.0(ADR-0019 / #1342):同步拒絕改成 raise,不再回 status='failed'。

    這是 `_REFUSED_WITHOUT_DISPATCH` 分類的**唯一依據**。上游若退回舊契約,
    那個 except 分支會靜默失效、配額拒絕又會被標成 acceptance_unknown——
    所以把「這兩種例外型別存在」與「kickoff 的文件講明會 raise」一起鎖住。
    """
    import inspect as _inspect

    from notebooklm.exceptions import ArtifactFeatureUnavailableError, RateLimitError
    from notebooklm._web.artifact import generation

    assert issubclass(RateLimitError, Exception)
    assert issubclass(ArtifactFeatureUnavailableError, Exception)
    # 「artifact id 缺席 = 沒有建出 task」是我們把它歸成 not_accepted 的理由。
    parse_src = _inspect.getsource(
        generation.ArtifactGenerationService._parse_generation_result
    )
    assert "raise ArtifactFeatureUnavailableError" in parse_src


async def test_conftest_rate_limit_fake_matches_the_user_displayable_producer():
    """`RateLimitError` 有兩個生產者(round2 獨立複審 V-B),`REFUSED_WITHOUT_DISPATCH`
    的『伺服器沒建出 task』契約只保證其中一個:decoder 把伺服器回應裡的
    `USER_DISPLAYABLE_ERROR` 解碼成例外時,附帶 `rpc_code="USER_DISPLAYABLE_ERROR"`
    (`_web/wire/decoder.py::extract_rpc_result`)。另一個生產者(transport 層的 HTTP
    429,`_web/transport/executor.py`)`rpc_code=None`——請求已經送達才被限流打回來,
    上游自己的 `with_rate_limit_retry` 對它也是原地重送,判別特徵只有 `rpc_code`
    可靠。

    這裡釘兩件事:①decoder 那條分支確實建出 `rpc_code="USER_DISPLAYABLE_ERROR"`
    的例外;②`tests/conftest.py` 的 `refuse_first`(整套 failover 測試共用的假件)
    預設拒絕形狀要跟它同型——在這條測試補上之前,fake 的 `rpc_code` 是 `None`,
    兩種生產者都不像,整套測試驗的其實是 decoder 產不出的形狀。
    """
    import inspect as _inspect

    from conftest import FakeClient, refuse_first
    from notebooklm._web.wire.decoder import extract_rpc_result

    decoder_src = _inspect.getsource(extract_rpc_result)
    assert 'rpc_code="USER_DISPLAYABLE_ERROR"' in decoder_src

    client = FakeClient()
    refuse_first(client, "generate_audio", [], fail_first_n=1)
    with pytest.raises(Exception) as excinfo:
        await client.artifacts.generate_audio("nb-1")
    assert getattr(excinfo.value, "rpc_code", None) == "USER_DISPLAYABLE_ERROR"


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
    assert ReportFormat.CONCEPT_EXPLANATION.value == "concept_explanation"
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
    render() 對 list_info.glyph 與 heading 的 BlockStyle 都不外洩標記；
    這不是驗證字面上湊巧沒有 "[1]" 這個子字串。
    這條紅了代表要重新決定 chat_ask 該用 .text 還是 .render()。
    """
    import dataclasses
    from typing import get_type_hints

    from notebooklm import AskResult
    from notebooklm.types import (
        BlockStyle,
        DocumentBlock,
        ListInfo,
        ListStyle,
        StructuredDocument,
        TextSpan,
    )

    fields = {f.name for f in dataclasses.fields(AskResult)}
    assert "answer_document" in fields
    assert get_type_hints(AskResult)["answer_document"] is StructuredDocument
    doc = StructuredDocument(
        blocks=(
            DocumentBlock(
                0,
                5,
                spans=(TextSpan(0, 5, "Hello"),),
                list_info=ListInfo(style=ListStyle.UNORDERED, glyph="* "),
            ),
            DocumentBlock(
                5,
                10,
                spans=(TextSpan(5, 10, "World"),),
                style=BlockStyle.HEADING_1,
            ),
        ),
    )
    assert doc.render() == "Hello\nWorld"
    assert "\n" not in doc.text and doc.text == "HelloWorld"


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
    """`_cookies.would_trigger_inline_heal` 依賴這幾個上游私有函式判斷
    「這份憑證會不會讓 0.8.1 的載入路徑觸發 inline RotateCookies」。

    這是私有 API,所以更需要 tripwire:紅了就要回頭確認 heal 的觸發條件有沒有換地方。

    `_recover_psidts_inline` 一併鎖在這裡,理由是**它的內部結構就是我們的防線**:
    `app._lifespan` 靠持有 `_rotation_lock_path(…)` 的 flock 擋掉重鑄,而那把鎖是
    `_recover_psidts_inline` 的第 4 個前提——它一定要先搶到鎖才發 POST。上游把它
    inline 進 `load_with_recovery`、或改掉它解析 storage path 的方式,那道 flock 會
    **靜默失效**(載入照樣成功,只是 3 VM 共用的 cookie 被重鑄)。簽名這條紅了,
    就要回頭確認 flock 擋的還是不是同一件事。
    """
    from notebooklm._auth import cookies, psidts_recovery

    assert _params(psidts_recovery._storage_cookie) == ["entry"]
    assert _params(psidts_recovery._recover_psidts_inline) == ["path"]
    assert _params(cookies._sanitized_auth_entries) == ["storage_state"]

    # `_cookies.describe_inline_heal_reason` 另外用這兩個把 routability 失敗拆成
    # expired / wrong_scope / missing。**它只產生訊息、不參與任何 gate**,所以這裡
    # 紅了不代表接受條件破功 —— 是 warning 的措辭會退回「講不出是哪一種」,
    # 而那正是這組訊息當初被誤讀成「登入要死了」的原因。
    assert _params(psidts_recovery._iter_routable_psidts_cookies) == [
        "entries",
        "to_cookie",
        "now",
    ]
    assert psidts_recovery._PSIDTS_COOKIE == "__Secure-1PSIDTS"


def test_notebooklm_py_lower_bound_excludes_versions_we_cannot_import():
    """分發路徑不讀 lock,所以 pyproject 的版本下界是唯一實裝約束。

    `uv tool install git+…` 不讀 `uv.lock`;Codex 將兩處下界改回 `>=0.8` 時,
    117 個測試仍全綠,但 0.8.0 wheel 實際 import 會因缺少私有符號而炸掉。
    這證明版本字串本身需要行為性契約測試,不能只依賴目前 venv 的實裝版本。

    被擋掉的版本各有理由:**0.8.0** 缺我們 import 的私有符號;**0.8.1** 沒有
    `notebooklm._web.*`,而本檔的 `getsource` 斷言全釘在那裡 —— production code
    在 0.8.1 仍跑得起來,但「測的」與「裝的」會是不同版,而那正是 release-checklist
    §依賴版本對帳整節在防的事。
    """
    import tomllib
    from pathlib import Path

    from packaging.requirements import Requirement
    from packaging.version import Version

    # 用 `__file__` 定位而不是 CWD:`audiskill/` 是多專案容器,從上一層跑
    # `pytest notebooklm-mcp/tests/...` 是合理的操作,而 CWD 相依會讓它變成
    # `FileNotFoundError` —— 症狀看起來像「契約被違反」(tripwire 觸發),
    # 實際上只是 harness 位置不同。tripwire 給錯訊號比不給還糟。
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    requirements = metadata["project"]["dependencies"] + metadata["project"][
        "optional-dependencies"
    ]["login"]
    notebooklm_requirements = [
        Requirement(spec) for spec in requirements if Requirement(spec).name == "notebooklm-py"
    ]

    assert len(notebooklm_requirements) == 2
    for requirement in notebooklm_requirements:
        assert Version("0.8.0") not in requirement.specifier
        assert Version("0.8.1") not in requirement.specifier
        assert Version("0.8.2") in requirement.specifier


def test_rotation_lock_and_file_lock_semantics_that_app_lifespan_depends_on(tmp_path):
    """`app._lifespan` 的 flock 防線靠這兩條上游私有 API 的語意撐著;
    這裡紅了代表那道防線可能已經失效。"""
    from notebooklm._auth.keepalive import _file_lock_try_exclusive
    from notebooklm._auth.psidts_recovery import _rotation_lock_path

    path_a = tmp_path / "slot-a" / "storage_state.json"
    path_b = tmp_path / "slot-b" / "storage_state.json"
    assert _rotation_lock_path(path_a) == _rotation_lock_path(path_a)
    assert _rotation_lock_path(path_a) != _rotation_lock_path(path_b)
    assert _rotation_lock_path(None) is None

    lock_path = tmp_path / "rotate.lock"
    with _file_lock_try_exclusive(lock_path) as outer:
        assert outer is True
        with _file_lock_try_exclusive(lock_path) as inner:
            assert inner is False
    with _file_lock_try_exclusive(lock_path) as reacquired:
        assert reacquired is True


def test_psidts_routes_to_rotate_gates_on_routability_not_existence():
    """`_cookies.would_trigger_inline_heal` 依賴 routability,不是 existence。

    **這個判準不是接受條件**:routability 問的是「這個 PSIDTS 能不能被 *refresh*」,
    不是「能不能被 *use*」(上游 `load_with_recovery` 的 docstring 講死的),所以拿它
    拒收憑證會誤拒——實測 prd 槽位 1 的 PSIDTS scope 在 `.youtube.com`,不 routable
    卻一直在服役。它現在只用來(a)發一則「這個槽位該換憑證了」的 warning,
    (b)標示 `app._lifespan` 那把 rotation flock 擋掉的正是哪一種憑證。

    第二列的 notebooklm scope 到不了 accounts.google.com,所以不能誤放行。
    第四列的 session cookie 是未過期,所以不能誤拒絕。
    第五列的重複身分只要有一筆過期,保守規則就不能誤放行。
    這條紅了代表 warning 的判準漂了,要回頭確認 flock 擋的對象是否還是同一個 predicate。
    """
    from notebooklm._auth import psidts_recovery

    now = 1000.0

    def entry(domain=".google.com", expires=now + 3600):
        return {
            "name": "__Secure-1PSIDTS",
            "value": "tok",
            "domain": domain,
            "path": "/",
            "expires": expires,
            "httpOnly": True,
            "secure": True,
        }

    cases = [
        ([entry()], True),
        ([entry(".notebooklm.google.com")], False),
        ([entry(expires=now - 3600)], False),
        ([entry(expires=-1)], True),
        ([entry(expires=now - 10), entry(expires=now + 10)], False),
        ([], False),
    ]
    for entries, expected in cases:
        assert (
            psidts_recovery._psidts_routes_to_rotate(
                entries,
                to_cookie=psidts_recovery._storage_cookie,
                now=now,
            )
            is expected
        )


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


def test_web_research_neutralizes_not_found_into_no_research():
    """`tools_research._explain_no_research`(:78 附近的 `"no_research" not in
    str(exc)` 判準)承重於上游 `WebResearchAPI._wait_observed_status` 這條私有
    覆寫:它把 `NOT_FOUND` 中和成 `NO_RESEARCH`,所以「換帳號輪詢到逾時」與
    「這個 task 根本沒被 NOT_FOUND 找到過」在 wait 的錯誤訊息裡長得一樣,都只看
    得到 `no_research` 字樣(round2 獨立複審 V-D)。上面
    `test_research_task_and_source_fields` 只釘了 enum 成員存在,沒釘這個中和
    行為——round2 的獨立審查一開始就是漏看這條覆寫才誤判成 REFUTED。

    **這條紅了,正確的反應是重新檢查
    `tools_research._explain_no_research` 的判準(目前是字串比對
    `"no_research" in str(exc)`),不是刪掉這條測試**——上游若改了中和邏輯或
    拿掉這條覆寫,那個判準就可能跟著失準或失去意義。
    """
    import inspect as _inspect

    from notebooklm._web.research import WebResearchAPI

    src = _inspect.getsource(WebResearchAPI._wait_observed_status)
    assert "ResearchStatus.NOT_FOUND" in src
    assert "return ResearchStatus.NO_RESEARCH" in src


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

    # 與上面 pyproject 那條同一個根因:用 `__file__` 定位,不吃 CWD
    # (從 `audiskill/` 容器層跑 `pytest notebooklm-mcp/tests/...` 會 FileNotFoundError,
    #  而那個症狀看起來像契約被違反)。
    script = (Path(__file__).resolve().parents[1] / "scripts/sync-auth.sh").read_text(encoding="utf-8")
    assert "get_storage_path" in script
    assert 'NBLM_HOME=' not in script


def test_auth_probe_matches_the_sdk_http_auth_error_shape():
    """The compatibility shim must follow the HTTP cause retained by SDK 0.8.0."""
    from notebooklm._web.transport.executor import RpcExecutor
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

    三環都釘在 `_web.*` 實作上,理由見
    `test_rename_false_no_longer_short_circuits`(對 ABC facade 抓原始碼會靜默恆真)。
    """
    from notebooklm._web.artifact.generation import ArtifactGenerationService
    from notebooklm._web.notebooks import WebNotebooksAPI
    from notebooklm._web.sources.content import SourceContentRenderer
    from notebooklm._web.sources.listing import SourceLister

    gen = inspect.getsource(ArtifactGenerationService.generate_audio)
    assert "get_source_ids" in gen, (
        "生成不再於 client 端撈 notebook 的來源清單 —— 若改成伺服器自己挑,"
        "『source_delete 之後不會進生成』就完全失去依據"
    )

    assert "GET_NOTEBOOK" in inspect.getsource(WebNotebooksAPI.get_raw)
    assert "GET_NOTEBOOK" in inspect.getsource(SourceLister.list), (
        "sources.list 與 get_source_ids 不再同源 —— source_list 的觀察結果推不出生成端行為"
    )

    fulltext = inspect.getsource(SourceContentRenderer)
    assert "[[source_id]" in fulltext and "notebook_id" not in fulltext.split("params =")[1][:120], (
        "get_fulltext 開始帶 notebook_id 了 —— 那樣『刪掉還讀得回』就變成真的異常,要重查"
    )


def test_source_search_signature_and_chunk_fields():
    """`source_search` 直接把三個參數轉給 SDK,不自己重寫任何驗證。

    **空 query 的拒絕留給 SDK**(`ValidationError`):自己再寫一份 `if not query.strip()`
    就是第二份會漂的規則。代價是這裡要釘住「SDK 真的會擋」,否則哪天它改成回空
    list,我們的工具會安靜地把一次無意義的 RPC 當成「查無結果」回給呼叫端。

    `source_ids` / `limit` 是 keyword-only —— 跟 source add 那一組同紀律(位置呼叫
    會 TypeError),鎖住免得有人寫成位置參數之後上游再插參數就靜默錯位。
    """
    import dataclasses

    from notebooklm._sources import SourcesAPI
    from notebooklm.types import RelevantChunk

    params = inspect.signature(SourcesAPI.search).parameters
    assert list(params) == ["self", "notebook_id", "query", "source_ids", "limit"]
    for name in ("source_ids", "limit"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name

    # 工具原樣轉發這五個欄位;少一個就是回傳契約破掉。
    assert {f.name for f in dataclasses.fields(RelevantChunk)} == {
        "source_id",
        "text",
        "rank",
        "start",
        "end",
    }


def test_source_search_inputs_are_validated_by_the_sdk_before_any_rpc():
    """接續上一條:證明「不自己驗」是安全的,而不是漏驗。

    驗證住在 **backend-neutral 的 `_sources.validate_search`**(web / android 共用),
    不是 web 實作裡 —— 所以這條不必跟著 `_web.*` 下沉,也不會因為哪天多一個 backend
    而失效。三樣都由它擋:空 query、`source_ids` 不是字串序列、`limit` 非正整數。

    這三樣**全部**發生在打 RPC 之前。少了這個前提,`source_search` 那句「參數驗證交給
    SDK」就會從紀律變成漏洞。
    """
    from notebooklm._sources import validate_search
    from notebooklm.exceptions import ValidationError

    for bad_query in ("", "   ", None):
        with pytest.raises(ValidationError):
            validate_search(bad_query, None, None)

    # str 是 Sequence,不擋就會把 "abc" 拆成三個 id 打出去。
    with pytest.raises(ValidationError):
        validate_search("q", "not-a-list", None)
    with pytest.raises(ValidationError):
        validate_search("q", ["ok", ""], None)
    for bad_limit in (0, -1, True, 1.5):
        with pytest.raises(ValidationError):
            validate_search("q", None, bad_limit)

    # 正常輸入:query 去頭尾空白、source_ids 去重且保序、limit 原樣。
    assert validate_search("  q  ", ["b", "a", "b"], 3) == ("q", ("b", "a"), 3)
