import pytest
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
    await t.artifact_rename("nb-123", "task-123", "EP01 心法篇")
    call = next(c[1] for c in fake_client.artifacts.calls if c[0] == "rename")
    assert call["return_object"] is False


async def test_auth_check_ok(fake_client):
    """認證活著:輕量真 RPC 成功,回 ok + 筆記本數。"""
    result = await t.auth_check()
    assert result == {"ok": True, "notebooks": 1}


async def test_auth_check_dead_gives_relogin_hint(fake_client):
    """認證死亡:fail-fast 並給出可操作的重登指引(不是裸 stack trace)。"""
    fake_client.notebooks.fail_list = True
    with pytest.raises(RuntimeError, match="sync-auth"):
        await t.auth_check()


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


async def test_source_add_file_title_check_still_fails_loud_before_probe(fake_client, tmp_path):
    # probe 是加法,不得吞掉既有的 title fail-loud 後檢
    fake_client.sources.title_lands = False
    f = tmp_path / "ep.mp3"
    f.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="title"):
        await t.source_add_file("nb-1", str(f), title="EP03 進階篇")


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
