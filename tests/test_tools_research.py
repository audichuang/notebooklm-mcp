"""research_start / research_wait / research_import。

核心不變式:**候選與匯入是兩件事**。wait 不匯入任何東西,import 只匯入 host 指名的
URL——研究能力進來的同時,不能讓 NotebookLM 自己找到的來源一股腦落進筆記本。
"""
import pytest
from conftest import FakeClient
from notebooklm._types.research import ResearchStatus

from notebooklm_mcp import runtime
from notebooklm_mcp import tools_research as r


# ---- start:先落地 task_id,再等 ---------------------------------------------------


async def test_research_start_returns_task_id_and_passes_mode(fake_client):
    out = await r.research_start("nb-1", "advisor tool history forwarding", mode="deep")
    call = next(c[1] for c in fake_client.research.calls if c[0] == "start")
    assert call["query"] == "advisor tool history forwarding"
    assert call["mode"] == "deep" and call["source"] == "web"
    assert out["task_id"] == "res-1" and out["mode"] == "deep"
    # 只啟動,不等待 —— 這正是 deep(可能數十分鐘)不能 start+wait 合一的理由。
    assert not [c for c in fake_client.research.calls if c[0] == "wait"]


async def test_research_start_requires_a_query(fake_client):
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="query"):
            await r.research_start("nb-1", bad)
    assert not fake_client.research.calls


async def test_research_start_fails_loud_when_no_task_was_created(fake_client):
    """SDK 對「後端沒建 task」回 None 而非 raise;放行會讓呼叫端拿空 id 去等到 timeout。"""
    fake_client.research.start_returns_none = True
    with pytest.raises(RuntimeError, match="research 未啟動"):
        await r.research_start("nb-1", "some query")


# ---- wait:只產候選 ---------------------------------------------------------------


async def test_research_wait_returns_candidates_and_never_imports(fake_client):
    out = await r.research_wait("nb-1", "res-1")

    assert out["status"] == "completed"
    # 報告 entry 不混進 candidates(它沒有 URL,無法用 URL 指名),獨立成旗標。
    assert [c["url"] for c in out["candidates"]] == [
        "https://a.example/post", "https://b.example/spec", "https://c.example/blog",
    ]
    assert out["report_importable"] is True
    # 預設不回報告本文,但字數是**全文**長度(決定要不要調高上限的依據)。
    assert out["report"] == "" and out["report_truncated"] is True
    assert out["report_chars"] == len(fake_client.research.report)
    # 沒有任何匯入發生。
    assert not [c for c in fake_client.research.calls if c[0] == "import"]


async def test_research_wait_marks_cited_urls(fake_client):
    """cited 是**事實標記**(URL 有沒有出現在報告引用裡),不是 MCP 幫忙做的篩選。"""
    out = await r.research_wait("nb-1", "res-1")
    cited = {c["url"]: c["cited"] for c in out["candidates"]}
    assert cited["https://a.example/post"] is True      # markdown 連結
    assert cited["https://b.example/spec"] is True      # 裸 URL
    assert cited["https://c.example/blog"] is False     # 報告沒引用
    assert out["cited_url_count"] == 2


async def test_research_wait_fails_loud_on_a_failed_task(fake_client):
    """SDK 對 FAILED 是回傳而非 raise —— 放行會讓空候選被誤讀成「這題沒東西」。"""
    fake_client.research.status = ResearchStatus.FAILED
    with pytest.raises(RuntimeError, match="未完成"):
        await r.research_wait("nb-1", "res-1")


async def test_research_wait_requires_a_task_id(fake_client):
    with pytest.raises(ValueError, match="task_id"):
        await r.research_wait("nb-1", "  ")
    assert not fake_client.research.calls


async def test_research_wait_propagates_the_typed_timeout(fake_client):
    """逾時必須原樣冒出去(ResearchTimeoutError 是 TimeoutError 子類),不能被包成別的錯——
    呼叫端就是靠「這是 timeout 不是失敗」決定「重跑 wait,別重開 task」。"""
    from notebooklm.exceptions import ResearchTimeoutError

    fake_client.research.wait_exc = ResearchTimeoutError(
        notebook_id="nb-1", task_id="res-1", timeout=1800
    )
    with pytest.raises(ResearchTimeoutError):
        await r.research_wait("nb-1", "res-1", timeout=1800)
    with pytest.raises(TimeoutError):        # 子類關係也鎖住
        await r.research_wait("nb-1", "res-1", timeout=1800)


# ---- import:只匯入指名的 ----------------------------------------------------------


async def test_research_import_only_imports_named_urls(fake_client):
    out = await r.research_import("nb-1", "res-1", urls=["https://c.example/blog"])
    call = next(c[1] for c in fake_client.research.calls if c[0] == "import")
    assert call["titles"] == ["來源C(未被引用)"]        # A/B 沒被指名就不進來
    assert call["is_report"] == [False]
    assert out["imported"] == [{"source_id": "src-r1", "title": "來源A"}]
    assert out["requested"] == 1


async def test_research_import_pins_client_between_poll_and_import(fake_client):
    other = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", other)])
    original = fake_client.research.poll

    async def poll_then_rotate(*args, **kwargs):
        task = await original(*args, **kwargs)
        runtime.rotate_client()
        return task

    fake_client.research.poll = poll_then_rotate

    await r.research_import("nb-1", "res-1", urls=["https://c.example/blog"])

    assert not [call for call in other.research.calls if call[0] == "import"]


async def test_research_import_dedupes_and_keeps_caller_order(fake_client):
    out = await r.research_import(
        "nb-1", "res-1",
        urls=["https://b.example/spec", "https://a.example/post", "https://b.example/spec"],
    )
    call = next(c[1] for c in fake_client.research.calls if c[0] == "import")
    assert call["titles"] == ["來源B", "來源A"]
    assert out["requested"] == 2


async def test_research_import_rejects_urls_not_in_the_candidate_set(fake_client):
    """寧可爆掉,也不要靜默少匯入幾筆讓呼叫端以為都進去了。"""
    with pytest.raises(ValueError, match="不在 task res-1 的候選清單裡"):
        await r.research_import("nb-1", "res-1", urls=["https://a.example/post",
                                                       "https://typo.example/x"])
    assert not [c for c in fake_client.research.calls if c[0] == "import"]


async def test_research_import_needs_something_to_import(fake_client):
    with pytest.raises(ValueError, match="沒有任何東西要匯入"):
        await r.research_import("nb-1", "res-1", urls=[])
    assert not fake_client.research.calls


async def test_research_import_can_include_the_report_entry(fake_client):
    await r.research_import("nb-1", "res-1", urls=["https://a.example/post"],
                            include_report=True)
    call = next(c[1] for c in fake_client.research.calls if c[0] == "import")
    assert call["is_report"] == [True, False]           # 報告排在前面
    assert call["titles"] == ["Deep Research Report", "來源A"]


async def test_research_import_report_without_a_report_entry_fails(fake_client):
    """fast mode 不產報告;要求 include_report 卻沒有,是呼叫端弄錯 mode。"""
    fake_client.research.sources = tuple(
        s for s in fake_client.research.sources if not s.is_report
    )
    with pytest.raises(ValueError, match="沒有可匯入的報告"):
        await r.research_import("nb-1", "res-1", urls=[], include_report=True)


async def test_research_import_on_an_empty_task_blames_the_task_not_the_urls(fake_client):
    """task 查不到/已過期時,不能報成「你 URL 打錯了」——診斷方向會整個歪掉。"""
    fake_client.research.sources = ()
    with pytest.raises(RuntimeError, match="沒有任何候選來源"):
        await r.research_import("nb-1", "res-1", urls=["https://a.example/post"])
    assert not [c for c in fake_client.research.calls if c[0] == "import"]


@pytest.mark.parametrize("status", [ResearchStatus.IN_PROGRESS, ResearchStatus.FAILED,
                                    ResearchStatus.NO_RESEARCH, ResearchStatus.NOT_FOUND])
async def test_research_import_refuses_a_task_that_is_not_completed(fake_client, status):
    """沒有這道 gate 就能繞過 research_wait:in_progress 收到半套候選,
    **failed 的 task 仍可能留著解析出來的 sources** —— 兩種都會靜默匯入錯東西。"""
    fake_client.research.status = status
    with pytest.raises(RuntimeError, match="尚未完成"):
        await r.research_import("nb-1", "res-1", urls=["https://a.example/post"])
    assert not [c for c in fake_client.research.calls if c[0] == "import"]


async def test_research_import_rejects_blank_url_entries(fake_client):
    """空項目不靜默丟掉:吞掉會讓「我選了 3 筆」變成「進了 2 筆」而沒人發現。"""
    for bad in ([""], ["   "], [None], ["https://a.example/post", ""]):
        with pytest.raises(ValueError, match="空字串/非字串"):
            await r.research_import("nb-1", "res-1", urls=bad)
    assert not fake_client.research.calls


async def test_research_import_strips_surrounding_whitespace(fake_client):
    """normalizer 不 strip 空白(contract 測試有鎖)——工具端沒先 strip 的話,
    貼過來多一個空格就會變成「不在候選清單裡」。"""
    await r.research_import("nb-1", "res-1", urls=["  https://a.example/post  "])
    call = next(c[1] for c in fake_client.research.calls if c[0] == "import")
    assert call["titles"] == ["來源A"]


async def test_research_import_uses_import_identity_not_citation_identity(fake_client):
    """候選帶 fragment 時,host 傳回原樣 URL 仍要對得上。

    citation normalizer 會保留 fragment、import normalizer 會丟掉 —— 用錯那顆的話,
    SDK 的 timeout readback 對帳(它用 import identity)就會跟我們算的筆數不一致。"""
    from notebooklm._types.research import ResearchSource

    fake_client.research.sources = (
        ResearchSource(url="https://a.example/post#intro", title="帶 fragment 的來源"),
    )
    await r.research_import("nb-1", "res-1", urls=["https://a.example/post#intro"])
    call = next(c[1] for c in fake_client.research.calls if c[0] == "import")
    assert call["titles"] == ["帶 fragment 的來源"]


async def test_research_import_refuses_ambiguous_candidates(fake_client):
    """兩筆候選只差 fragment = 伺服器眼中同一個來源。先到先贏會讓 host 挑了「官方 spec」
    卻匯入「轉載」——正是本工具要防的那類靜默錯誤,所以 raise 並列出兩個標題。"""
    from notebooklm._types.research import ResearchSource

    fake_client.research.sources = (
        ResearchSource(url="https://a.example/spec#v1", title="官方 spec"),
        ResearchSource(url="https://a.example/spec#v2", title="轉載"),
    )
    with pytest.raises(RuntimeError, match="對應到 task res-1 的多筆候選"):
        await r.research_import("nb-1", "res-1", urls=["https://a.example/spec#v1"])
    assert not [c for c in fake_client.research.calls if c[0] == "import"]


async def test_research_import_forwards_max_elapsed(fake_client):
    """deep 匯入常在 client 端先 timeout;預算要能由呼叫端調。"""
    await r.research_import("nb-1", "res-1", urls=["https://a.example/post"], max_elapsed=60.0)
    call = next(c[1] for c in fake_client.research.calls if c[0] == "import")
    assert call["max_elapsed"] == 60.0


async def test_collision_on_an_unselected_candidate_does_not_block(fake_client):
    """碰撞只看**被選取的** identity。候選清單裡兩筆不相干的來源剛好 canonical 相同,
    不該讓一次合法的 selection 整批失敗(那是過度 fail-closed)。"""
    from notebooklm._types.research import ResearchSource

    fake_client.research.sources = (
        ResearchSource(url="https://a.example/spec#v1", title="碰撞 A1"),
        ResearchSource(url="https://a.example/spec#v2", title="碰撞 A2"),
        ResearchSource(url="https://b.example/only", title="乾淨的 B"),
    )
    await r.research_import("nb-1", "res-1", urls=["https://b.example/only"])
    call = next(c[1] for c in fake_client.research.calls if c[0] == "import")
    assert call["titles"] == ["乾淨的 B"]


async def test_research_wait_report_cap(fake_client):
    """預設 0 = 只回字數不回本文;傳上限才截斷,report_chars 永遠是全文長度。"""
    full = fake_client.research.report
    out = await r.research_wait("nb-1", "res-1", max_report_chars=10)
    assert out["report"] == full[:10]
    assert out["report_chars"] == len(full) and out["report_truncated"] is True

    out = await r.research_wait("nb-1", "res-1", max_report_chars=len(full) + 50)
    assert out["report"] == full and out["report_truncated"] is False


async def test_research_wait_rejects_a_negative_cap(fake_client):
    with pytest.raises(ValueError, match="max_report_chars"):
        await r.research_wait("nb-1", "res-1", max_report_chars=-1)
    assert not fake_client.research.calls
