from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from notebooklm.exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
)
from notebooklm.rpc.types import AudioFormat, AudioLength
from notebooklm.types import ArtifactType

from notebooklm_mcp import tools_basic as t


def _fake_art(id, title, kind, completed=True):
    return type("A", (), {
        "id": id, "title": title, "kind": kind, "is_completed": completed,
        "status_str": "completed" if completed else "processing", "created_at": None,
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
    out = await t.notebook_get("nb-7")
    assert out["notebook_id"] == "nb-7" and out["sources_count"] == 2 and out["is_owner"] is True


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
                       "completed": True, "status": "completed", "created_at": None}
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


async def test_artifact_rename_is_fire_and_forget(fake_client):
    """artifact_rename 工具同樣必須顯式 return_object=False。"""
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
    """預設不清標記、照回 references(非破壞性:既有 caller 靠標記對照引用)。"""
    fake_client.chat.answer_override = "重點一 [1] 重點二 [3, 4]。"
    out = await t.chat_ask("nb-1", "重點?")
    assert out["answer"] == "重點一 [1] 重點二 [3, 4]。"
    assert out["references"]


async def test_chat_ask_strip_citations(fake_client):
    fake_client.chat.answer_override = "重點一 [1] 重點二 [3, 4] 收尾 [2-5]。"
    out = await t.chat_ask("nb-1", "重點?", strip_citations=True)
    assert "[" not in out["answer"] and "]" not in out["answer"]
    assert "重點一" in out["answer"] and "重點二" in out["answer"]


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
