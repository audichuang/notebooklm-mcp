from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from notebooklm.exceptions import (
    AuthError,
    ClientError,
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
)
from notebooklm.rpc.types import AudioFormat, AudioLength, SharePermission
from notebooklm.types import (
    ArtifactType,
    BlockKind,
    DocumentBlock,
    StructuredDocument,
    TextSpan,
    utf16_len,
)

from conftest import FakeClient, _structured_document

from notebooklm_mcp import runtime, tools_basic as t
from notebooklm_mcp._errors import NotebookAccessDenied


def _fake_art(id, title, kind, completed=True, source_ids=("src-1", "src-2")):
    return type("A", (), {
        "id": id, "title": title, "kind": kind, "is_completed": completed,
        "status_str": "completed" if completed else "processing", "created_at": None,
        "source_ids": source_ids,
    })()


async def test_generate_audio_defaults_zh_hant(fake_client):
    out = await t.generate_audio("nb-1", instructions="講解重點")
    assert out["task_id"] == "task-123"
    call = fake_client.artifacts.calls[0][1]
    assert call["language"] == "zh_Hant"
    assert call["instructions"] == "講解重點"


async def test_generate_audio_maps_enums(fake_client):
    await t.generate_audio("nb-1", audio_format="debate", audio_length="long")
    call = fake_client.artifacts.calls[0][1]
    assert call["audio_format"] == AudioFormat.DEBATE
    assert call["audio_length"] == AudioLength.LONG


async def test_generate_audio_rejects_bad_language(fake_client):
    with pytest.raises(ValueError):
        await t.generate_audio("nb-1", language="zh-TW")


async def test_source_add_file_passes_mime(fake_client):
    out = await t.source_add_file("nb-1", "/tmp/x.mp3", mime_type="audio/mpeg")
    assert out["source_id"].startswith("src-")
    assert fake_client.sources.calls[0][1]["mime_type"] == "audio/mpeg"


async def test_artifact_download_arg_order(fake_client):
    out = await t.artifact_download_audio("nb-1", "/tmp/out.mp3", artifact_id="art-9")
    assert out["path"] == "/tmp/out.mp3"
    c = fake_client.artifacts.calls[0][1]
    assert c["output_path"] == "/tmp/out.mp3" and c["artifact_id"] == "art-9"


async def test_ask(fake_client):
    out = await t.chat_ask("nb-1", "重點?")
    assert "重點" in out["answer"]


async def test_ask_passes_scope_and_returns_refs(fake_client):
    out = await t.chat_ask("nb-1", "重點?", source_ids=["src-1"], conversation_id="c9")
    call = fake_client.chat.calls[-1][1]
    assert call["source_ids"] == ["src-1"] and call["conversation_id"] == "c9"
    assert out["conversation_id"] == "c9"
    assert out["references"][0] == {"source_id": "src-1", "citation_number": 1, "cited_text": "引用片段"}


async def test_source_list(fake_client):
    fake_client.sources.seed("EP01 心法篇", "原文一")
    out = await t.source_list("nb-1")
    titles = [s["title"] for s in out["sources"]]
    assert "EP01 心法篇" in titles and "原文一" in titles
    assert out["sources"][0]["ready"] is True and out["sources"][0]["kind"] == "web_page"


async def test_source_fulltext(fake_client):
    out = await t.source_fulltext("nb-1", "src-9")
    assert out["source_id"] == "src-9" and out["content"] == "來源全文" and out["char_count"] == 4


async def test_notebook_get(fake_client):
    fake_client.notebooks.get = AsyncMock(return_value=SimpleNamespace(
        id="nb-7", title="Test", sources_count=2, is_owner=True,
        created_at=None, role=None,
    ))
    out = await t.notebook_get("nb-7")
    assert out["notebook_id"] == "nb-7" and out["sources_count"] == 2
    assert out["is_owner"] is True and out["role"] is None


async def test_notebook_get_forwards_role_name(fake_client):
    fake_client.notebooks.get = AsyncMock(return_value=SimpleNamespace(
        id="nb-7", title="Test", sources_count=2, is_owner=False,
        created_at=None, role=SharePermission.VIEWER,
    ))
    out = await t.notebook_get("nb-7")
    assert out["role"] == "VIEWER"


async def test_notebook_get_translates_permission_denied_with_the_repair_hint(fake_client):
    """v0.9.14 FINDING-F:`notebook_get` 撞權限不足時原本裸拋上游的 `ClientError`。

    上游那句講的是 `authuser` account-routing、指向 SDK issue #114/#294 —— **與多帳號
    pool 情境無關,而且沒有任何修復指引**。而 skill 正是引導呼叫端「生成前先
    `notebook_get` 確認目標對不對」,所以實務上這是最先撞到的一支
    (`_list_sources` 早就有這層翻譯,兩處不該只有一處對)。
    """
    fake_client.notebooks.get = AsyncMock(
        side_effect=ClientError("permission denied", rpc_code=7)
    )
    with pytest.raises(NotebookAccessDenied) as excinfo:
        await t.notebook_get("nb-7")
    assert "notebook_share_with_pool" in str(excinfo.value)
    assert "nb-7" in str(excinfo.value)
    # 繼承 RuntimeError 才會被兩個 dispatch 呼叫端當成「乾淨終態」處理。
    assert isinstance(excinfo.value, RuntimeError)


async def test_notebook_get_does_not_swallow_other_failures(fake_client):
    """只轉權限那一種 —— 網路錯誤/認證過期吞下去只會把根因埋掉(同 `_list_sources` 的紀律)。"""
    fake_client.notebooks.get = AsyncMock(side_effect=ClientError("boom", rpc_code=5))
    with pytest.raises(ClientError, match="boom"):
        await t.notebook_get("nb-7")


async def test_artifact_wait_fail_closed(fake_client):
    # SDK 的 wait_for_completion 可能回 failed status(非丟例外);工具必須 fail-closed,
    # 不能把失敗當成功回傳 artifact_id。
    fake_client.artifacts.fail_complete = True
    with pytest.raises(RuntimeError):
        await t.artifact_wait("nb-1", "task-x")


async def test_artifact_list_maps_fields(fake_client):
    fake_client.artifacts.seed_artifacts(
        _fake_art("a1", "EP01 心法篇", ArtifactType.AUDIO),
        _fake_art("a2", "研讀講義", ArtifactType.REPORT, completed=False),
    )
    out = await t.artifact_list("nb-1")
    rows = out["artifacts"]
    assert [r["artifact_id"] for r in rows] == ["a1", "a2"]
    assert rows[0] == {"artifact_id": "a1", "title": "EP01 心法篇", "kind": "audio",
                       "completed": True, "status": "completed", "created_at": None,
                       "source_ids": ["src-1", "src-2"]}
    assert rows[1]["kind"] == "report" and rows[1]["completed"] is False
    assert rows[1]["status"] == "processing"


async def test_artifact_list_filters_by_kind(fake_client):
    fake_client.artifacts.seed_artifacts(
        _fake_art("a1", "EP01", ArtifactType.AUDIO),
        _fake_art("a2", "講義", ArtifactType.REPORT),
    )
    out = await t.artifact_list("nb-1", kind="audio")
    assert [r["artifact_id"] for r in out["artifacts"]] == ["a1"]
    assert fake_client.artifacts.calls[-1][1]["artifact_type"] == ArtifactType.AUDIO


async def test_artifact_list_rejects_bad_kind(fake_client):
    with pytest.raises(ValueError):
        await t.artifact_list("nb-1", kind="podcast")


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


async def test_artifact_rename_asks_for_no_object(fake_client):
    """artifact_rename 工具同樣必須顯式 return_object=False。

    改名(原 test_artifact_rename_is_fire_and_forget):0.8.0 起
    return_object=False **不再是**fire-and-forget(#1362,兩種模式都做存在性檢查,
    查不到就 raise)——名字講的是這件事現在不成立,斷言本身沒變(仍是驗
    return_object 有沒有傳 False)。"""
    fake_client.artifacts.seed_artifact("task-123")
    await t.artifact_rename("nb-123", "task-123", "EP01 心法篇")
    call = next(c[1] for c in fake_client.artifacts.calls if c[0] == "rename")
    assert call["return_object"] is False


async def test_artifact_rename_rejects_an_id_from_another_notebook_before_mutation(
    fake_client, monkeypatch
):
    """錯配 notebook 時 SDK 的 rename RPC 會先改、後對傳入的 notebook list
    做 miss-detection。公開工具必須在 mutation 前先驗歸屬。"""
    elsewhere = fake_client.artifacts.seed_artifact("task-elsewhere", title="原標題")

    async def absent_from_requested_notebook(notebook_id, artifact_id):
        assert (notebook_id, artifact_id) == ("nb-requested", "task-elsewhere")
        return None

    monkeypatch.setattr(
        fake_client.artifacts, "get_or_none", absent_from_requested_notebook
    )

    with pytest.raises(ValueError, match="not in notebook"):
        await t.artifact_rename("nb-requested", "task-elsewhere", "不應落地")

    assert elsewhere.title == "原標題"
    assert not [call for call in fake_client.artifacts.calls if call[0] == "rename"]


async def test_artifact_retry_failed_reuses_the_same_artifact_id(fake_client):
    """失敗的 artifact 原地重試(UI 的 Retry)——artifact_id 不變,省掉重生一次配額。
    薄包不等待:呼叫端接 artifact_wait + 對應的 download 工具。"""
    fake_client.artifacts.seed_artifact("deck-1", completed=False, failed=True)
    out = await t.artifact_retry_failed("nb-123", "deck-1")
    call = next(c[1] for c in fake_client.artifacts.calls if c[0] == "retry_failed")
    assert call == {"notebook_id": "nb-123", "artifact_id": "deck-1"}
    assert out == {"task_id": "deck-1", "artifact_id": "deck-1"}


async def test_artifact_retry_failed_requires_an_artifact_id(fake_client):
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="artifact_id"):
            await t.artifact_retry_failed("nb-123", bad)
    assert not fake_client.artifacts.calls


async def test_artifact_retry_failed_propagates_a_refusal(fake_client):
    """**與 generate_* 不同**:retry_failed 對伺服器端同步拒絕(rate limit / 配額)是
    raise,不吞成 failed status(SDK 說明是 ADR-0019 async-kickoff 契約)。工具是薄包,
    要原樣讓那個型別冒出去,呼叫端才分得出「配額爆了」與「生成中途失敗」。"""
    from notebooklm.exceptions import RateLimitError

    fake_client.artifacts.seed_artifact("deck-1", completed=False, failed=True)
    fake_client.artifacts.retry_exc = RateLimitError("daily quota exhausted")
    with pytest.raises(RateLimitError):
        await t.artifact_retry_failed("nb-123", "deck-1")


async def test_auth_check_ok(fake_client):
    """認證活著:輕量真 RPC 成功,回 ok + 筆記本數。"""
    result = await t.auth_check()
    assert result == {"ok": True, "notebooks": 1}


async def test_auth_check_dead_gives_relogin_hint(fake_client):
    """認證死亡:fail-fast 並給出可操作的重登指引(不是裸 stack trace)。"""
    fake_client.notebooks.fail_list = True
    with pytest.raises(RuntimeError, match="sync-auth"):
        await t.auth_check()


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST", "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
    )
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(str(status_code), request=request, response=response)


def _rpc_http_status_error(status_code: int) -> RPCError:
    cause = _http_status_error(status_code)
    error = RPCError("RPC transport failed")
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(AuthError("expired"), id="auth-error"),
        pytest.param(RPCError("auth", rpc_code=401), id="rpc-401"),
        pytest.param(RPCError("unauthenticated", rpc_code=16), id="grpc-16"),
        pytest.param(_rpc_http_status_error(401), id="mapped-http-401"),
        pytest.param(_rpc_http_status_error(403), id="mapped-http-403"),
        pytest.param(_http_status_error(401), id="raw-http-401"),
        pytest.param(_http_status_error(403), id="raw-http-403"),
    ],
)
async def test_auth_check_hints_relogin_for_real_sdk_auth_shapes(fake_client, error):
    fake_client.notebooks.list = AsyncMock(side_effect=error)

    with pytest.raises(RuntimeError) as caught:
        await t.auth_check()

    message = str(caught.value)
    assert "uv run notebooklm login" in message
    assert "sync-auth.sh" in message
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(NetworkError("offline"), id="network"),
        pytest.param(RateLimitError("busy"), id="rate-limit"),
        pytest.param(ServerError("down", status_code=503), id="server"),
        pytest.param(DecodingError("malformed response"), id="decoding"),
        pytest.param(
            RPCError("explicitly non-auth", rpc_code=999),
            id="explicit-non-auth-rpc-code",
        ),
    ],
)
async def test_auth_check_preserves_non_auth_error_type(fake_client, error):
    fake_client.notebooks.list = AsyncMock(side_effect=error)
    with pytest.raises(type(error)) as caught:
        await t.auth_check()


async def test_auth_check_prefers_explicit_non_auth_code_over_http_cause(fake_client):
    error = RPCError("explicitly non-auth", rpc_code=999)
    error.__cause__ = _http_status_error(401)
    fake_client.notebooks.list = AsyncMock(side_effect=error)

    with pytest.raises(RPCError) as caught:
        await t.auth_check()

    assert caught.value is error
    assert caught.value is error


async def test_source_add_url_pins_client_across_probe(fake_client):
    from notebooklm_mcp import runtime

    class FirstSources:
        async def add_url(self, *_args, **_kwargs):
            runtime.set_client(second_client)
            return SimpleNamespace(id="src-a")

        async def get_fulltext(self, *_args):
            return SimpleNamespace(char_count=7)

    class SecondSources:
        async def get_fulltext(self, *_args):
            raise AssertionError("probe switched account mid-tool")

    first_client = SimpleNamespace(sources=FirstSources())
    second_client = SimpleNamespace(sources=SecondSources())
    runtime.set_client(first_client)

    assert await t.source_add_url("nb-1", "https://example.com") == {
        "source_id": "src-a",
        "char_count": 7,
    }


async def test_source_add_file_pins_client_across_probe(fake_client, tmp_path):
    from notebooklm_mcp import runtime

    upload = tmp_path / "episode.mp3"
    upload.write_bytes(b"audio")

    class FirstSources:
        async def add_file(self, *_args, **_kwargs):
            runtime.set_client(second_client)
            return SimpleNamespace(id="src-file", title="episode.mp3")

        async def get_fulltext(self, *_args):
            return SimpleNamespace(char_count=9)

    class SecondSources:
        async def get_fulltext(self, *_args):
            raise AssertionError("file probe switched account mid-tool")

    first_client = SimpleNamespace(sources=FirstSources())
    second_client = SimpleNamespace(sources=SecondSources())
    runtime.set_client(first_client)

    out = await t.source_add_file("nb-1", str(upload))

    assert out["source_id"] == "src-file"
    assert out["char_count"] == 9


async def test_source_add_file_title_whitespace_not_false_positive(fake_client, tmp_path):
    """SDK 會 strip title;呼叫端傳前後空白不該讓後檢誤判 fail。"""
    f = tmp_path / "ep.mp3"
    f.write_bytes(b"x")
    result = await t.source_add_file("nb-123", str(f), title="  EP03 進階篇  ")
    call = next(c[1] for c in fake_client.sources.calls if c[0] == "add_file")
    assert call["title"] == "EP03 進階篇"  # 已 strip 後才下傳
    assert result["source_id"].startswith("src-")


# ---- v0.2.9 token-diet:P1 source_fulltext 輕量對帳 ----------------------------

async def test_source_fulltext_default_shape_unchanged(fake_client):
    """不傳新參數 = 現行輸出(非破壞性硬約束)。"""
    out = await t.source_fulltext("nb-1", "src-9")
    assert out == {"source_id": "src-9", "title": "來源標題",
                   "char_count": 4, "content": "來源全文"}


async def test_source_fulltext_max_chars_truncates(fake_client):
    fake_client.sources.fulltext_content = "零一二三四五六七八九" * 10   # 100 chars
    out = await t.source_fulltext("nb-1", "src-9", max_chars=10)
    assert out["content"] == "零一二三四五六七八九"
    assert out["truncated"] is True
    assert out["char_count"] == 100          # char_count 永遠是全文長度,不因截斷變小


async def test_source_fulltext_contains_normalizes_cjk_spaces(fake_client):
    # NotebookLM 對 CJK 會插空格;比對前兩邊都要 normalize
    fake_client.sources.fulltext_content = "來 源 全 文 有 harness 工 程"
    out = await t.source_fulltext("nb-1", "src-9", max_chars=0,
                                  contains=["來源全文", "harness", "沒有的詞"])
    assert out["hits"] == {"來源全文": True, "harness": True, "沒有的詞": False}
    assert out["content"] == ""              # max_chars=0:對帳只要 char_count+hits,不灌全文


async def test_source_fulltext_rejects_bad_args(fake_client):
    with pytest.raises(ValueError):
        await t.source_fulltext("nb-1", "src-9", max_chars=-1)
    with pytest.raises(ValueError):
        await t.source_fulltext("nb-1", "src-9", contains=["ok", "  "])   # 空關鍵詞永遠命中


# ---- v0.2.9 token-diet:P2 add source 自帶落地驗證(best-effort probe)---------

async def test_source_add_url_returns_char_count(fake_client):
    fake_client.sources.fulltext_content = "文章正文" * 50
    out = await t.source_add_url("nb-1", "https://example.com/post")
    assert out["char_count"] == 200
    assert "warning" not in out
    assert any(c[0] == "get_fulltext" for c in fake_client.sources.calls)


async def test_source_add_url_empty_extraction_warns_paywall(fake_client):
    fake_client.sources.fulltext_content = ""
    out = await t.source_add_url("nb-1", "https://medium.com/paywalled")
    assert out["char_count"] == 0
    assert "paywall" in out["warning"] or "空殼" in out["warning"]


async def test_source_add_file_empty_extraction_warns_file_wording(fake_client, tmp_path):
    # 檔案來源 char_count=0 不等於 paywall(可能是音檔/掃描 PDF),措辭必須不同
    fake_client.sources.fulltext_content = ""
    f = tmp_path / "scan.pdf"
    f.write_bytes(b"x")
    out = await t.source_add_file("nb-1", str(f))
    assert out["char_count"] == 0
    assert "paywall" not in out["warning"]


async def test_source_add_no_probe_when_wait_false(fake_client):
    out = await t.source_add_url("nb-1", "https://example.com", wait=False)
    assert "char_count" not in out
    assert not any(c[0] == "get_fulltext" for c in fake_client.sources.calls)


async def test_source_add_probe_failure_is_best_effort(fake_client):
    fake_client.sources.fulltext_raises = True
    out = await t.source_add_url("nb-1", "https://example.com/post")
    assert out["source_id"].startswith("src-")   # add 本身成功,probe 掛掉不連坐
    assert out["char_count"] is None and "note" in out
    assert "RuntimeError" in out["note"]


async def test_source_add_file_title_check_still_fails_loud_before_probe(fake_client, tmp_path):
    # probe 是加法,不得吞掉既有的 title fail-loud 後檢
    fake_client.sources.title_lands = False
    f = tmp_path / "ep.mp3"
    f.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="title"):
        await t.source_add_file("nb-1", str(f), title="EP03 進階篇")


# ---- v0.3.3:source_add_file 對非原生副檔名自動包成 .md -------------------------
# EP36:fixture-output.json 直接 400 Bad Request。endpoint 不吃的副檔名每次都不同,
# 靠文件提醒等於每次都要有人先踩一次,所以在唯一 caller-facing 入口自動繞過。


def _add_file_call(fake_client):
    return next(c[1] for c in fake_client.sources.calls if c[0] == "add_file")


async def test_source_add_file_wraps_unsupported_text_as_markdown(fake_client, tmp_path):
    f = tmp_path / "fixture-output.json"
    f.write_text('{"ok": true}\n', encoding="utf-8")
    out = await t.source_add_file("nb-1", str(f))

    call = _add_file_call(fake_client)
    # 保留原副檔名做出處:a.ts 與 a.json 不會撞成同一個 a.md。
    assert call["file_path"].endswith("fixture-output.json.md")
    assert call["file_bytes"] == b'{"ok": true}\n'      # 內容一字不差
    assert call["mime_type"] is None                    # 讓 SDK 從 .md 推 text/markdown
    assert out["converted_from"] == "fixture-output.json"


async def test_source_add_file_explicit_mime_wins_over_wrapping(fake_client, tmp_path):
    """第二道閘:caller 顯式宣告 mime_type 就照它送,不自動包裝。

    這條必須用「會被包裝的副檔名」測 —— 用 .mp3 測證明不了任何事,它早就被第一道
    suffix 閘放行了,mime 判斷被刪掉測試照樣綠。"""
    f = tmp_path / "fixture.json"
    f.write_text('{"ok": true}', encoding="utf-8")
    out = await t.source_add_file("nb-1", str(f), mime_type="application/json")

    call = _add_file_call(fake_client)
    assert call["file_path"] == str(f)                  # 原樣,沒被改名
    assert call["mime_type"] == "application/json"
    assert "converted_from" not in out


@pytest.mark.parametrize(
    "name, content",
    [
        ("notes.md", b"# already markdown\n"),          # 原生格式
        ("table.csv", b"a,b\n1,2\n"),                   # 表格:包成 .md 會丟掉原生語意
        ("deck.pptx", b"PK\x03\x04binary"),             # 官方列為可上傳來源
        ("ep03.mp3", b"x"),                             # 不傳 mime 的假 mp3:仍不得包裝
        ("shot.png", b"\x89PNG\r\n\x1a\nIHDR"),         # 圖片(SourceType.IMAGE)
        ("diagram.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),  # 合法 UTF-8 的圖片
        ("page.html", b"<html><script>x</script></html>"),  # 上游刻意 fail-loud,不偽轉換
        ("blob.bin", b"\xff\xfe\x00binary"),            # 無效 UTF-8
        ("nulls.dat", b"abc\x00def"),                   # NUL 是合法 UTF-8,decode 擋不掉
    ],
)
async def test_source_add_file_passes_through_untouched(fake_client, tmp_path, name, content):
    f = tmp_path / name
    f.write_bytes(content)
    out = await t.source_add_file("nb-1", str(f))

    call = _add_file_call(fake_client)
    assert call["file_path"] == str(f)
    assert call["file_bytes"] == content
    assert "converted_from" not in out


async def test_source_add_file_wrap_size_cap_is_exclusive(fake_client, tmp_path, monkeypatch):
    """鎖 `>` 邊界語意:等於上限仍轉換,超過一 byte 就原樣送。

    用 monkeypatch 把上限縮小來測,不建 25MiB 檔也不 mock stat/read_text——sparse file
    測不了這件事(全 NUL,會先被 NUL guard 放行,兩個分支都 passthrough 卻理由不同)。"""
    monkeypatch.setattr(t, "_MAX_CONVERT_BYTES", 4)

    at_cap = tmp_path / "at.json"
    at_cap.write_bytes(b"abcd")
    out = await t.source_add_file("nb-1", str(at_cap))
    assert out["converted_from"] == "at.json"

    over_cap = tmp_path / "over.json"
    over_cap.write_bytes(b"abcde")
    out = await t.source_add_file("nb-1", str(over_cap))
    assert "converted_from" not in out
    last = [c[1] for c in fake_client.sources.calls if c[0] == "add_file"][-1]
    assert last["file_path"] == str(over_cap)        # 超過上限:原樣送,沒進包裝


def test_max_convert_bytes_value_is_locked():
    # 邊界語意由上面那個測試鎖;這裡只鎖住實際門檻值不被無聲調動。
    assert t._MAX_CONVERT_BYTES == 25 * 1024 * 1024


# ---- v0.2.9 token-diet:P4 chat_ask 清引用 + 可關 references --------------------

async def test_chat_ask_default_keeps_citations_and_references(fake_client):
    """預設不清標記、照回 references(非破壞性:既有 caller 靠標記對照引用)。
    answer_document 刻意設成非空、內容完全不同——證明 strip_citations=False
    這條路徑完全不讀 answer_document,不會因為它非空就被污染。
    """
    fake_client.chat.answer_override = "重點一 [1] 重點二 [3, 4]。"
    fake_client.chat.answer_document = _structured_document("不相關的文件內容")
    out = await t.chat_ask("nb-1", "重點?")
    assert out["answer"] == "重點一 [1] 重點二 [3, 4]。"
    assert out["references"]


async def test_chat_ask_strip_citations(fake_client):
    fake_client.chat.answer_override = "重點一 [1] 重點二 [3, 4] 收尾 [2-5]。"
    out = await t.chat_ask("nb-1", "重點?", strip_citations=True)
    assert "[" not in out["answer"] and "]" not in out["answer"]
    assert "重點一" in out["answer"] and "重點二" in out["answer"]


async def test_chat_ask_strip_citations_prefers_answer_document(fake_client):
    fake_client.chat.answer_override = "回答 [1-2] 會被 regex 留下不同內容"
    fake_client.chat.answer_document = _structured_document("乾淨純文字，不含引用標記")

    out = await t.chat_ask("nb-1", "重點?", strip_citations=True)

    assert out["answer"] == "乾淨純文字，不含引用標記"
    assert out["answer"] != "回答 會被 regex 留下不同內容"


async def test_chat_ask_strip_citations_falls_back_when_document_is_whitespace_only(
    fake_client,
):
    """render() 非空但全是空白時一樣要退回 `_CITATION_RE`——`.strip()` 判斷
    要留著,上游只保證解碼不出東西時是**空字串**,沒保證不是全空白。
    """
    fake_client.chat.answer_override = "重點一 [1] 收尾。"
    fake_client.chat.answer_document = _structured_document("   \n")
    out = await t.chat_ask("nb-1", "重點?", strip_citations=True)
    assert out["answer"] == "重點一 收尾。"


def _doc_with_kinds(*pairs) -> StructuredDocument:
    """`(text, BlockKind)` 序列 → 真的 StructuredDocument;text 為空表示**上游沒解出 spans**。

    這正是 v0.9.14 驗收在真實回答上看到的形狀:`CODE_BLOCK` 的 spans 是空的。
    """
    blocks, cursor = [], 0
    for text, kind in pairs:
        end = cursor + utf16_len(text)
        spans = (TextSpan(start_index=cursor, end_index=end, text=text),) if text else ()
        blocks.append(DocumentBlock(start_index=cursor, end_index=end, spans=spans, kind=kind))
        cursor = end
    return StructuredDocument(blocks=tuple(blocks))


async def test_chat_ask_strip_citations_removes_inline_bold(fake_client):
    """v0.9.14 FINDING-D:`render()` 只拿掉 block 級標記,inline `**粗體**` 會原樣留著。

    skill `tool-reference.md` 對外宣告「strip_citations=true 回的是純文字,沒有 Markdown
    強調」,而這串字會流進公開 Apple Podcast 的 `<description>`(實測真的帶著 `**` 進 RSS)。
    """
    fake_client.chat.answer_document = _structured_document(
        "這是一種**競態條件**的問題。", "解法是**檔案鎖**。"
    )
    out = await t.chat_ask("nb-1", "重點?", strip_citations=True)
    assert out["answer"] == "這是一種競態條件的問題。\n解法是檔案鎖。"
    assert "*" not in out["answer"]


async def test_chat_ask_strip_citations_keeps_underscores_and_bullets(fake_client):
    """清強調不可以誤傷識別碼與條列符號 —— 這兩種形狀在真實 show notes 裡都會出現。

    底線刻意**不清**:`NOTEBOOKLM_AUTH_JSON` / `source_id` 這類識別碼與 `_斜體_` 同形,
    清掉會毀掉正文(所以 skill 那句「`_斜體_` 之類」要跟著改成精確描述)。
    """
    fake_client.chat.answer_document = _structured_document(
        "設定 NOTEBOOKLM_AUTH_JSON_2 這個環境變數。", "* 條列一", "算式 2 * 3 * 4 的結果。"
    )
    out = await t.chat_ask("nb-1", "重點?", strip_citations=True)
    assert "NOTEBOOKLM_AUTH_JSON_2" in out["answer"]
    assert "* 條列一" in out["answer"]
    assert "2 * 3 * 4" in out["answer"]


async def test_chat_ask_strip_citations_refuses_when_a_block_decoded_to_nothing(fake_client):
    """v0.9.14 FINDING-E:上游解不出的 block **整段靜默消失,連 U+FFFC 都不留**。

    實測形狀:回答說「以下是一段示範程式碼:」然後**直接接下一段**,呼叫端沒有任何訊號
    能發現東西掉了(同一個 conversation 用 strip_citations=False 追問,程式碼完整回來)。
    這串字是要進公開 RSS 的,所以在這裡 fail-loud 而不是讓人發布後才發現。
    """
    fake_client.chat.answer_document = _doc_with_kinds(
        ("以下是一段示範程式碼:", BlockKind.PARAGRAPH),
        ("", BlockKind.CODE_BLOCK),
        ("關鍵概念說明:", BlockKind.PARAGRAPH),
    )
    with pytest.raises(RuntimeError, match="CODE_BLOCK") as excinfo:
        await t.chat_ask("nb-1", "給我程式碼", strip_citations=True)
    assert "strip_citations=False" in str(excinfo.value)


async def test_chat_ask_strip_citations_allows_code_block_that_did_decode(fake_client):
    """判別力那一半:判準是**有沒有解出文字**,不是 block 的 kind。

    離線實測過 `CODE_BLOCK` 帶 spans 時 `render()` 照樣輸出它 —— 按 kind 擋會把
    「程式碼有好好回來」的正常回答也擋掉。
    """
    fake_client.chat.answer_document = _doc_with_kinds(
        ("前段", BlockKind.PARAGRAPH),
        ("print(1)", BlockKind.CODE_BLOCK),
        ("後段", BlockKind.PARAGRAPH),
    )
    out = await t.chat_ask("nb-1", "給我程式碼", strip_citations=True)
    assert out["answer"] == "前段\nprint(1)\n後段"


async def test_chat_ask_strip_citations_ignores_blocks_that_never_carry_text(fake_client):
    """`HORIZONTAL_RULE` 本來就沒有文字,空的不代表內容掉了。"""
    fake_client.chat.answer_document = _doc_with_kinds(
        ("上半", BlockKind.PARAGRAPH),
        ("", BlockKind.HORIZONTAL_RULE),
        ("下半", BlockKind.PARAGRAPH),
    )
    out = await t.chat_ask("nb-1", "重點?", strip_citations=True)
    assert out["answer"] == "上半\n下半"


async def test_chat_ask_default_path_is_untouched_by_both_guards(fake_client):
    """兩道都只掛在 `strip_citations=True`;預設路徑逐字不變(既有 caller 依標記對照 references)。"""
    fake_client.chat.answer_override = "重點**一** [1] 收尾。"
    fake_client.chat.answer_document = _doc_with_kinds(
        ("重點", BlockKind.PARAGRAPH), ("", BlockKind.CODE_BLOCK)
    )
    out = await t.chat_ask("nb-1", "重點?")
    assert out["answer"] == "重點**一** [1] 收尾。"


async def test_chat_ask_strip_citations_render_keeps_paragraph_breaks(fake_client):
    """`.render()` 才會在 block 之間插入 `\n`;`.text` 是 offset-faithful、
    完全不插分隔符,段落會黏在一起。這條測試是 (1) 的絆線——有人把
    `chat_ask` 改回讀 `.text` 這裡會紅。
    """
    fake_client.chat.answer_document = _structured_document(
        "重點一", "重點二", "重點三"
    )
    out = await t.chat_ask("nb-1", "重點?", strip_citations=True)
    assert out["answer"] == "重點一\n重點二\n重點三"


async def test_chat_ask_strip_citations_leaves_no_gap_before_punctuation(fake_client):
    """清掉標記不能留下標點前的空格 —— 這支的用途就是產 show notes(公開文案)。

    v0.9.13 驗收實錄:「…等待被處理的時間** 。」標記前面那個空格留著,標點前多一格。
    只清標記**前面**的 space/tab:兩邊都清會把 `see [1] and` 黏成 `seeand`,而換行不能
    吃 —— 行首的引用連著換行清掉會把 markdown 結構拉平。
    """
    fake_client.chat.answer_override = (
        "延遲來自等待 [1]。利用率放大 [3, 4],而不是服務時間 [2-5]!\n第二段 [1] 說明"
    )
    out = await t.chat_ask("nb-1", "重點?", strip_citations=True)
    assert out["answer"] == "延遲來自等待。利用率放大,而不是服務時間!\n第二段 說明"


async def test_chat_ask_exclude_references(fake_client):
    out = await t.chat_ask("nb-1", "重點?", include_references=False)
    assert out["references"] == []   # show notes 路徑不需要 references,省 token


async def test_source_add_file_wrapping_does_not_block_the_event_loop(fake_client, tmp_path, monkeypatch):
    """包裝會做 stat + 最多 25 MiB 的 read/write。同步跑在 async 工具裡會卡住整個 MCP
    event loop——其他 request、取消、長跑狀態查詢全被凍住,外層 client 可能先 timeout。"""
    import asyncio, time

    def slow_wrap(file_path, tmpdir):
        time.sleep(0.2)                      # 模擬慢速掛載上的大檔 I/O
        return file_path, None

    monkeypatch.setattr(t, "_as_uploadable_text", slow_wrap)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    f = tmp_path / "payload.json"
    f.write_text('{"ok": true}', encoding="utf-8")
    await t.source_add_file("nb-1", str(f))
    beat.cancel()
    assert ticks >= 5                        # 慢 I/O 期間 event loop 仍在轉


async def test_artifact_retry_failed_rejects_an_artifact_from_another_notebook(fake_client):
    """RETRY_ARTIFACT 也只靠 artifact_id 定位;錯配的 ID 會 mutate 到別人的 artifact。"""
    with pytest.raises(ValueError, match="不在 notebook nb-123"):
        await t.artifact_retry_failed("nb-123", "somewhere-else")
    assert not [c for c in fake_client.artifacts.calls if c[0] == "retry_failed"]


async def test_artifact_retry_failed_refuses_a_healthy_artifact(fake_client):
    """retry 一個已完成的 artifact 會在遠端就地重跑,可能把一份好的產物換掉。
    刻意不限制 kind —— retry_failed 本來就是跨 artifact 種類的通用能力。"""
    fake_client.artifacts.seed_artifact("deck-1")           # completed、非 failed
    with pytest.raises(ValueError, match="不是 failed 狀態"):
        await t.artifact_retry_failed("nb-123", "deck-1")
    assert not [c for c in fake_client.artifacts.calls if c[0] == "retry_failed"]


# ---- auth_check 的射程:預設只量作用中那一槽 -------------------------------------

def _pool(*clients):
    """裝一個多槽 pool,label 用 index 區分。"""
    runtime.set_clients([(f"acct{i}@x", c) for i, c in enumerate(clients, start=1)])


async def test_auth_check_default_only_measures_the_active_slot(fake_client):
    """**這是要修的那個射程問題,先把現況釘住。**

    預設模式只探作用中那一個 client —— 別的槽位死光了它照樣回綠。而配額 failover
    會在生成中途換帳號,所以「長跑前 fail-fast」這個承諾預設只覆蓋 1/N。
    這條紅了代表預設模式的成本或語義變了,要回頭確認 `all_slots` 那條路還在不在。
    """
    dead = FakeClient()
    dead.notebooks.fail_list = True
    _pool(fake_client, dead)

    assert await t.auth_check() == {"ok": True, "notebooks": 1}


async def test_auth_check_all_slots_reports_every_slot_with_refreshability(fake_client):
    """`all_slots=True` 把整個 pool 攤開,而且 `usable` 與 `refreshable` 是兩件事。

    prd 槽位 1 的真實狀態就是 `usable=True` + `refreshable=False`(scope 在
    `.youtube.com`)—— 那是已知可服役的狀態,不是故障,所以兩欄一定要分開回報:
    合成一盞燈的話,這個槽位會被讀成壞掉,而那正是這輪要修的誤讀。
    """
    dead = FakeClient()
    dead.notebooks.fail_list = True
    _pool(fake_client, dead)
    runtime.set_slot_diagnostics([
        {"slot": 1, "env": "NOTEBOOKLM_AUTH_JSON", "refreshable": False,
         "heal_reason": "wrong_scope", "psidts_domains": [".youtube.com"]},
        {"slot": 2, "env": "NOTEBOOKLM_AUTH_JSON_2", "refreshable": True,
         "heal_reason": None, "psidts_domains": [".google.com"]},
    ])

    result = await t.auth_check(all_slots=True)

    assert result["ok"] is True          # 作用中那一槽活著 —— 與預設模式同義
    assert result["all_usable"] is False  # 但 pool 不是全綠
    first, second = result["slots"]
    assert first["slot"] == 1 and first["active"] is True
    assert first["usable"] is True and first["notebooks"] == 1
    assert first["refreshable"] is False and first["heal_reason"] == "wrong_scope"
    assert first["psidts_domains"] == [".youtube.com"]
    assert second["usable"] is False and second["error"] == "auth_expired"
    assert second["active"] is False


async def test_auth_check_all_slots_raises_only_when_the_whole_pool_is_dead(fake_client):
    """全滅才 fail-fast(整條路都走不動,等同預設模式的秒退);還有一槽活著就回表,
    因為丟例外會把你叫它去查的診斷一起丟掉。"""
    dead_a, dead_b = FakeClient(), FakeClient()
    dead_a.notebooks.fail_list = dead_b.notebooks.fail_list = True
    _pool(dead_a, dead_b)

    with pytest.raises(RuntimeError, match="sync-auth"):
        await t.auth_check(all_slots=True)


async def test_auth_check_all_slots_marks_non_auth_errors_as_unmeasured(fake_client):
    """網路 / 限流不是「認證壞了」—— 回 `usable=None`(沒量到),不要記成 False。

    `probe_auth` 刻意讓非認證錯誤保留原型別讓呼叫端退避;掃描模式不能把那個區別
    壓成一個布林,否則一次限流會被讀成「這個帳號的憑證死了」而引出不必要的重登。
    """
    flaky = FakeClient()

    async def _boom():
        raise TimeoutError("upstream slow")

    flaky.notebooks.list = _boom
    _pool(fake_client, flaky)

    result = await t.auth_check(all_slots=True)

    assert result["all_usable"] is False
    assert result["slots"][1]["usable"] is None
    assert "TimeoutError" in result["slots"][1]["error"]


async def test_auth_check_all_slots_ok_is_null_when_the_active_slot_was_not_measured(
    fake_client,
):
    """作用中槽位撞到**非認證**錯誤時,`ok` 要回 `null`(沒量到),不是 `false`。

    早一版寫成 `any(active and usable is True)`,於是一次 Timeout 就讓 `ok` 變 false ——
    把「沒量到」壓回「不能用」,正好是這支工具存在要消滅的那個混淆。而且預設模式在
    同一種情況下是**原樣拋出**那個例外(`probe_auth` 的契約),根本產生不了 `ok:false`。
    """
    flaky = FakeClient()

    async def _boom():
        raise TimeoutError("upstream slow")

    flaky.notebooks.list = _boom
    _pool(flaky, fake_client)          # 作用中 = 第一槽 = 探測不到的那個

    result = await t.auth_check(all_slots=True)

    assert result["slots"][0]["active"] is True
    assert result["slots"][0]["usable"] is None
    assert result["ok"] is None, "沒量到被壓成了「不能用」"
    assert result["all_usable"] is False


class TestSourceSearch:
    """`source_search`:排序過的段落檢索,取代「整份全文拉回來自己比對」。

    與 `source_fulltext(contains=…)` **不重疊也不取代**:那支是單一 source 的
    **精確子字串**比對(驗「這個詞有沒有真的進到 body」,語意檢索反而會答錯);
    這支是 notebook 範圍的**語意排序**檢索(問「哪幾段在談 X」)。
    """

    async def test_passes_every_argument_through_untouched(self, fake_client):
        from notebooklm.types import RelevantChunk
        from notebooklm_mcp import tools_basic as t

        fake_client.sources.search_results = [
            RelevantChunk(source_id="s1", text="第一段", rank=1, start=0, end=3),
            RelevantChunk(source_id="s2", text="第二段", rank=2, start=None, end=None),
        ]
        out = await t.source_search(
            "nb1", "  什麼是 X  ", source_ids=["s1", "s2"], limit=5
        )

        call = [c for c in fake_client.sources.calls if c[0] == "search"][-1]
        assert call[1] == {
            "notebook_id": "nb1",
            "query": "  什麼是 X  ",
            "source_ids": ["s1", "s2"],
            "limit": 5,
        }, "參數要原樣轉給 SDK —— 去空白/去重/驗證都是它的事(見 contract 測試)"
        assert out["count"] == 2
        assert out["chunks"][0] == {
            "source_id": "s1",
            "text": "第一段",
            "rank": 1,
            "start": 0,
            "end": 3,
        }
        # span 缺席時原樣回 None,不要自作主張補 0(0 是合法 offset)。
        assert out["chunks"][1]["start"] is None and out["chunks"][1]["end"] is None

    async def test_defaults_search_the_whole_notebook(self, fake_client):
        from notebooklm_mcp import tools_basic as t

        await t.source_search("nb1", "X")
        call = [c for c in fake_client.sources.calls if c[0] == "search"][-1]
        assert call[1]["source_ids"] is None and call[1]["limit"] is None

    async def test_sdk_validation_error_is_not_swallowed(self, fake_client):
        """空 query 必須爆,不可以變成「查無結果」——那會讓呼叫端以為來源裡沒有。"""
        from notebooklm.exceptions import ValidationError
        from notebooklm_mcp import tools_basic as t

        with pytest.raises(ValidationError):
            await t.source_search("nb1", "   ")

    async def test_empty_result_is_a_real_answer_not_an_error(self, fake_client):
        from notebooklm_mcp import tools_basic as t

        fake_client.sources.search_results = []
        assert await t.source_search("nb1", "X") == {"count": 0, "chunks": []}
