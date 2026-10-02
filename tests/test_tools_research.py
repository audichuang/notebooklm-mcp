"""research_start / research_wait / research_import。

核心不變式:**候選與匯入是兩件事**。wait 不匯入任何東西,import 只匯入 host 指名的
URL——研究能力進來的同時,不能讓 NotebookLM 自己找到的來源一股腦落進筆記本。
"""
import pytest
from conftest import FakeClient
from notebooklm._types.research import ResearchStatus

from notebooklm_mcp import runtime, tools_research as r

# ---- start:先落地 task_id,再等 ---------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected_task_id"),
    [("fast", "res-1"), ("deep", "rep-1"), ("DEEP", "rep-1")],
)
async def test_research_start_returns_the_mode_specific_poll_handle(
    fake_client, mode, expected_task_id
):
    out = await r.research_start("nb-1", "advisor tool history forwarding", mode=mode)
    call = next(c[1] for c in fake_client.research.calls if c[0] == "start")
    assert call["query"] == "advisor tool history forwarding"
    assert call["mode"] == mode and call["source"] == "web"
    assert out["task_id"] == expected_task_id
    assert out["mode"] == mode.lower()
    # 只啟動,不等待 —— 這正是 deep(可能數十分鐘)不能 start+wait 合一的理由。
    assert not [c for c in fake_client.research.calls if c[0] == "wait"]


async def test_deep_research_threads_report_id_from_start_into_wait(fake_client):
    started = await r.research_start("nb-1", "some query", mode="deep")
    await r.research_wait("nb-1", task_id=started["task_id"])

    wait = next(c[1] for c in fake_client.research.calls if c[0] == "wait")
    assert wait["task_id"] == "rep-1"


@pytest.mark.parametrize("report_id", [None, "", "   "])
async def test_deep_research_fails_loud_without_a_report_id(
    fake_client, monkeypatch, report_id
):
    from notebooklm import DecodingError, ResearchStart

    async def start_without_report_id(notebook_id, query, source="web", mode="deep"):
        return ResearchStart(
            task_id="session-1",
            report_id=report_id,
            notebook_id=notebook_id,
            query=query,
            mode=mode,
        )

    monkeypatch.setattr(fake_client.research, "start", start_without_report_id)
    with pytest.raises(DecodingError, match="report_id") as excinfo:
        await r.research_start("nb-1", "some query", mode="deep")
    assert "retry" not in str(excinfo.value).lower()
    assert "may already have succeeded" in str(excinfo.value)


async def test_research_start_requires_a_query(fake_client):
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="query"):
            await r.research_start("nb-1", bad)
    assert not fake_client.research.calls


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


async def test_research_ids_remain_required_in_the_mcp_schema():
    from notebooklm_mcp.app import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert tools["research_wait"].inputSchema["required"] == ["notebook_id", "task_id"]
    assert tools["research_import"].inputSchema["required"] == ["notebook_id", "task_id"]


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
    out = await r.research_import("nb-1", task_id="res-1", urls=["https://c.example/blog"])
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

    await r.research_import("nb-1", task_id="res-1", urls=["https://c.example/blog"])

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


# ---- handle 綁帳號:pool 輪替之後仍要回到發起它的那個 -----------------------------


async def test_research_start_names_the_account_that_owns_the_handle(fake_client):
    other = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", other)])
    out = await r.research_start("nb-1", "some query")
    # 記帳與送出同源(runtime.snapshot()),不是事後補讀 active_account()。
    assert out["account"] == "a@x"


async def test_research_wait_polls_the_account_that_started_it(fake_client):
    """research session 綁在發起它的帳號上,notebook 分享給全 pool 也沒用。

    v0.9.13 真實驗收:同一個 task_id,發起它的 server 立刻回 completed,另一個帳號的
    server 輪詢 900 秒只拿到 no_research。start/wait 是兩次獨立呼叫,中間只要一次配額
    failover 游標就換人 ——「斷線救援用 research_wait、不要重新 start」那條指引就走不通。
    """
    other = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", other)])
    started = await r.research_start("nb-1", "some query")
    runtime.rotate_client()                      # 配額 failover 把游標推到 b@x

    await r.research_wait(
        "nb-1", task_id=started["task_id"], account=started["account"]
    )

    assert [c[0] for c in fake_client.research.calls] == ["start", "wait"]
    assert other.research.calls == []
    assert runtime.active_account() == "b@x"     # 游標不動:這裡不是配額輪替


async def test_research_import_uses_the_account_that_owns_the_handle(fake_client):
    other = FakeClient()
    runtime.set_clients([("a@x", other), ("b@x", fake_client)])
    await r.research_import(
        "nb-1", task_id="res-1", urls=["https://c.example/blog"], account="b@x"
    )
    assert [c[0] for c in fake_client.research.calls] == ["poll", "import"]
    assert other.research.calls == []


async def test_research_wait_explains_no_research_on_timeout(fake_client, monkeypatch):
    """v0.9.14 FINDING-G:傳了 **pool 內、但不是發起者**的帳號時,沒有人擋得住。

    `_handle_client` 只認得「account 根本不在 pool 裡」;pool 內傳錯的那條會一路輪詢到
    timeout,而 `timeout` 預設 **1800 秒** —— 實測要等滿才看到一句 `last status: no_research`,
    完全沒提「你可能傳錯帳號了」。而正確的診斷文字這個模組裡本來就有(`_handle_client`)。
    """
    other = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", other)])

    async def never_finishes(*args, **kwargs):
        raise TimeoutError(
            "Research task res-1 in notebook nb-1 timed out after 1800.0s "
            "(last status: no_research)"
        )

    monkeypatch.setattr(other.research, "wait_for_completion", never_finishes)
    with pytest.raises(RuntimeError) as excinfo:
        await r.research_wait("nb-1", task_id="res-1", account="b@x")
    msg = str(excinfo.value)
    assert "no_research" in msg                     # 原訊息保留,不是換掉
    assert "b@x" in msg                             # 這次用了誰
    assert "research_start" in msg                  # 怎麼修
    assert "a@x" in msg                             # pool 裡還有誰
    assert "不要重新 research_start" in msg          # 別再燒一次配額
    assert isinstance(excinfo.value.__cause__, TimeoutError)


async def test_research_wait_does_not_dress_up_an_ordinary_timeout(fake_client, monkeypatch):
    """判別力那一半:單純等太久(有在跑、只是慢)不該被說成「你傳錯帳號」。

    這一輪就實際撞到過:一顆 artifact 在遠端卡了 85 分鐘,狀態全程是 in_progress。
    """

    async def slow(*args, **kwargs):
        raise TimeoutError("Research task res-1 timed out after 60.0s (last status: in_progress)")

    monkeypatch.setattr(fake_client.research, "wait_for_completion", slow)
    with pytest.raises(TimeoutError) as excinfo:
        await r.research_wait("nb-1", task_id="res-1")
    assert "research_start" not in str(excinfo.value)


@pytest.mark.parametrize("tool", ("wait", "import"))
async def test_research_refuses_an_account_that_is_not_in_this_pool(fake_client, tool):
    """指名了 pool 裡沒有的帳號就當場說明白 —— 讓人輪詢滿 timeout 才拿到
    no_research 是最糟的失敗方式,而那正是這條 FINDING 觀測到的形狀。"""
    runtime.set_clients([("a@x", fake_client)])
    call = (
        r.research_wait("nb-1", task_id="res-1", account="gone@x")
        if tool == "wait"
        else r.research_import(
            "nb-1", task_id="res-1", urls=["https://c.example/blog"], account="gone@x"
        )
    )
    with pytest.raises(ValueError, match="gone@x") as excinfo:
        await call
    assert "a@x" in str(excinfo.value)           # 可用的有哪些要講出來
    assert fake_client.research.calls == []


async def test_research_import_lost_response_names_candidates_instead_of_bare_failure(
    fake_client,
):
    """0.8.3 起 IMPORT_RESEARCH 回應遺失**不再補送**:只 import 一次、輪詢看得到哪些,然後
    一律 raise(掛 `unconfirmed` + reconciliation report)。deep 負載下這很常見 —— 裸拋等於
    叫 host 重呼,而報告條目沒有 URL、上游去重擋不住,會重複匯入。"""
    from notebooklm._idempotency import (
        attach_reconciliation_report,
        mark_unconfirmed,
        reconciliation_report,
    )
    from notebooklm.exceptions import NetworkError

    async def lost(*args, **kwargs):
        exc = mark_unconfirmed(NetworkError("IMPORT_RESEARCH read timeout"))
        attach_reconciliation_report(
            exc,
            reconciliation_report(["src-a"], ["https://b.example/spec"]),
            operation="research.import_sources",
        )
        raise exc

    fake_client.research.import_sources_with_verification = lost
    with pytest.raises(NetworkError) as info:
        await r.research_import(
            "nb-1", "res-1", urls=["https://a.example/post", "https://b.example/spec"]
        )
    msg = str(info.value)
    assert "source_list" in msg
    assert "src-a" in msg
    assert "https://b.example/spec" in msg


async def test_research_import_reports_sources_that_were_already_present(fake_client):
    """0.8.3 的重呼會先對 baseline 去重:已在 notebook 的回到 `.already_present`,
    `imported` 是空的。只回 imported 的話,host 會以為「什麼都沒進去」。"""
    from notebooklm._research_import import _ImportedResearchSources

    fake_client.research.imported = []

    async def rerun(*args, **kwargs):
        return _ImportedResearchSources([], [{"id": "src-a", "title": "A"}])

    fake_client.research.import_sources_with_verification = rerun
    out = await r.research_import("nb-1", "res-1", urls=["https://a.example/post"])
    assert out["imported"] == []
    assert out["already_present"] == [{"source_id": "src-a", "title": "A"}]
