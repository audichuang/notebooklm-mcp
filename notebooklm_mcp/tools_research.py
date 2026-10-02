"""NotebookLM 內建 Web / Deep Research 的薄包:啟動 → 等待 → 選擇性匯入。

三支分開的工具,不是一支 `research_discover`——理由是**與既有 attempt/resume 紀律
一致**(ADR-0001):`research_start` 先把 task_id 交給呼叫端落地,`research_wait` 可重入,
外層 client timeout(mcporter 預設 60s)砍掉等待時,重跑 wait 就接得回來,不會又起一個
新的 research task 燒一次配額。deep mode 動輒 30 分鐘,start+wait 合一等於保證踩到。

**只產候選,不自動匯入**(ADR-0008):`research_wait` 回候選清單與報告,由 host 依
provenance / 日期 / 版本 / 重複性判斷,再把選中的 URL 交給 `research_import`。
`cited` 只是**事實標記**(該 URL 有沒有出現在報告的引用裡),不是 MCP 幫你做的篩選決定。
"""

from __future__ import annotations

# 兩個 normalizer 不能混用,SDK 自己的 docstring 就寫明「Distinct from
# normalize_citation_url」:
#   normalize_citation_url —— 給「報告 markdown 裡引用的 URL」比對用,strip 尾端標點、
#     **保留 fragment**(free-form 文字撈出來的)
#   _normalize_import_verification_url —— 給 import 的 identity 用,**丟掉 fragment**
#     (伺服器存的時候就把 fragment 剝掉)、不 strip 標點(來自結構化 sources.list)
# 選來源要用後者:host 挑的 URL 最後會被 SDK 的 timeout readback 拿去對帳,identity 不一致時
# `#a` / `#b` 兩個候選在我們眼中是兩筆、在 verification 眼中是同一筆,對帳數字就會錯。
# 是私有 API,故 test_contracts 有鎖(改名會在發版前紅,不會等到 production ImportError)。
from mcp.types import ToolAnnotations
from notebooklm import DecodingError
from notebooklm._research import _normalize_import_verification_url as _import_url_key
from notebooklm.research import extract_report_urls, normalize_citation_url

from . import runtime
from ._errors import reconcile_hint_if_unconfirmed
from .app import mcp


def _require(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _handle_client(account: str | None) -> object:
    """research handle 綁在**發起它的帳號**上,不是「現在作用中的那個」。

    v0.9.13 真實驗收:同一個 task_id、同一本(已分享給全 pool 的)notebook,發起它的
    server 立刻回 `completed`,另一個帳號的 server 輪詢 900 秒只拿到 `no_research`
    —— research session 不跟著 notebook 分享走。而 start / wait / import 是三次獨立
    呼叫,中間只要一次配額 failover 游標就換人,「斷線救援用 `research_wait`、不要重新
    `research_start`」那條指引就永遠走不通(而重新 start 要再燒一次配額)。

    `account` 沒給 = 舊行為(作用中那個),單帳號與同一次呼叫內都不受影響。指名了但
    pool 裡沒有就**當場 raise**:讓人輪詢滿 timeout 才拿到 `no_research`,是這條
    FINDING 觀測到的最糟失敗形狀。

    **游標一律不動** —— `_ACTIVE` 的語意是「配額輪替走到哪」,這裡只是「這件事只有
    某個帳號做得到」,正是 `runtime.all_clients()` 的用途(見它的 docstring)。
    """
    if account is None:
        return runtime.get_client()
    for label, client in runtime.all_clients():
        if label == account:
            return client
    raise ValueError(
        f"account {account!r} 不在這個 server 的 pool 裡"
        f"(可用:{runtime.all_accounts()});research handle 只有發起它的那個帳號"
        "輪詢得到,換帳號會拿到 no_research"
    )


def _explain_no_research(exc: BaseException, account: str | None) -> BaseException:
    """輪詢到 `no_research` 才逾時的話,把「你可能傳錯帳號了」講出來。

    **v0.9.14 真實驗收 FINDING-G。** `_handle_client` 只擋得住「account 根本不在 pool 裡」
    (那條當場 raise 並列出可用帳號,實測完好);**傳了 pool 內、但不是發起者的帳號**
    走的是另一條路 —— SDK 一路輪詢到 timeout,而 `timeout` 預設是 **1800 秒**,
    也就是要等滿 30 分鐘才知道自己傳錯,訊息裡只有一句 `last status: no_research`。

    諷刺的是正確的診斷文字這個檔案裡本來就有(見 `_handle_client`),只差沒有帶到這條
    路徑上。判準用訊息裡的 `no_research` 而不是例外型別:SDK 對逾時用的是內建
    `TimeoutError`,型別分不出「等太久」與「這個帳號根本看不到它」。
    """
    if "no_research" not in str(exc):
        return exc
    who = account or f"{runtime.active_account()!r}(未傳 account,用的是此刻作用中的帳號)"
    return RuntimeError(
        f"{exc}\n"
        f"全程只輪詢到 no_research,而這次用的帳號是 {who} —— research handle **只有發起它的"
        "那個帳號**輪詢得到(把 notebook 分享給全 pool 也沒用)。請把 `research_start` 回傳的 "
        f"`account` 原樣傳進來重跑(這個 server 的 pool:{runtime.all_accounts()});"
        "不要重新 research_start,那會再燒一次配額。"
    )


def _status_str(task) -> str:
    status = getattr(task, "status", None)
    return getattr(status, "value", None) or str(status)


def _candidate(source, cited_urls: set[str]) -> dict:
    url = getattr(source, "url", "") or ""
    return {
        "url": url,
        "title": getattr(source, "title", "") or "",
        # 該 URL 是否被報告本文引用。NotebookLM 找到但報告沒用到的,通常是邊緣命中。
        "cited": bool(url) and normalize_citation_url(url) in cited_urls,
    }


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
async def research_start(
    notebook_id: str,
    query: str,
    source: str = "web",
    mode: str = "fast",
) -> dict:
    """啟動 NotebookLM 內建研究,**立即**回可輪詢的 `task_id`(不等完成)。

    `mode`:`fast`(預設,快速網搜、便宜)/ `deep`(引用導向報告,數十分鐘且吃配額,只用在
    跨來源有爭議的題目)。`source`:`web`(預設)/ `drive`,deep 只支援 web。
    ⚠️ 這支 RPC **只送 query 字串**,筆記本既有來源對搜尋毫無影響 —— 種子的專有名詞、版本號、
    時間界線都要寫進 `query`。
    ⚠️ `task_id` 與 `account` 都要落地並原樣傳給 `research_wait` / `research_import`(一律傳
    `task_id`,不是 `report_id`);handle 綁發起帳號,換帳號輪詢會等滿 timeout 拿到 `no_research`。"""
    query = _require(query, "query")
    # 記帳與送出同源:snapshot() 一次取 (label, client),之後任何 rotate 都影響不到
    # 這一次——分兩次讀會讓回傳的 account 記到別人身上(同 ADR-0010 ③)。
    account, client = runtime.snapshot()
    res = await client.research.start(notebook_id, query, source=source, mode=mode)
    if res.mode == "deep":
        if not isinstance(res.report_id, str) or not res.report_id.strip():
            raise DecodingError(
                f"deep research start returned no report_id (session {res.task_id!r}); "
                "this run cannot be polled or cancelled; the remote start may already "
                "have succeeded"
            )
        task_id = res.report_id
    else:
        task_id = res.task_id
    return {
        "task_id": task_id,
        "report_id": res.report_id,
        "query": res.query,
        "mode": res.mode,
        "source": source,
        "account": account,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def research_wait(
    notebook_id: str,
    task_id: str,
    timeout: float = 1800.0,
    max_report_chars: int = 0,
    account: str | None = None,
) -> dict:
    """等 research 完成,回**候選來源 + 報告**(預設只回報告長度),不匯入任何東西。

    可重入:同 `task_id` 重跑就是繼續等 —— 斷線救援用這支,**不要**重新 `research_start`
    (會再燒一次配額)。`account` 傳 `research_start` 回的那個值(換帳號會等滿 timeout)。
    `candidates` 每筆 `url`/`title`/`cited`(是否被報告引用,事實標記非篩選決定),挑完把 URL
    交給 `research_import`。
    `max_report_chars` 預設 **0 = 不回報告本文**,只回全文長度 `report_chars`;要讀就傳上限
    (例如 4000)。`report_importable=True` = 這次有報告,`research_import(include_report=True)`
    收得進來,不必先讀回。"""
    task_id = _require(task_id, "task_id")
    if not isinstance(max_report_chars, int) or isinstance(max_report_chars, bool):
        raise ValueError("max_report_chars must be an int")
    if max_report_chars < 0:
        raise ValueError("max_report_chars must be >= 0(0 = 只回字數,不回本文)")
    # `_handle_client` 要留在 try **外面**:它自己就會對「account 不在 pool 裡」當場 raise,
    # 而那句訊息裡也有 `no_research` 三個字 —— 包進去會被下面的判準二次包裝,把一條乾淨的
    # ValueError 變成 RuntimeError(既有測試 test_research_refuses_an_account_that_is_not_in_this_pool 守著)。
    client = _handle_client(account)
    try:
        task = await client.research.wait_for_completion(notebook_id, task_id, timeout=timeout)
    except Exception as exc:
        raise _explain_no_research(exc, account) from exc
    status_str = _status_str(task)
    if status_str != "completed":
        # SDK 對 FAILED 是「回傳」而非 raise;放行的話呼叫端會拿到空候選清單,
        # 誤以為「這題沒東西可找」。
        raise RuntimeError(
            f"research task {task_id} 未完成:status={status_str!r}"
            f"(failed 通常是配額或後端拒絕;稍後重試或改窄 query)"
        )

    report = getattr(task, "report", "") or ""
    cited_urls = extract_report_urls(report)
    sources = list(getattr(task, "sources", ()) or ())
    # 報告本身也是一筆可匯入的 source(deep research),但它沒有 URL、無法用 URL 指名,
    # 所以獨立成一個旗標,不混進 candidates 讓呼叫端困惑。
    report_importable = any(
        getattr(s, "is_report", False) and getattr(s, "report_markdown", "") for s in sources
    )
    candidates = [_candidate(s, cited_urls) for s in sources if not getattr(s, "is_report", False)]
    body = report[:max_report_chars] if max_report_chars else ""
    return {
        "status": status_str,
        "task_id": getattr(task, "task_id", task_id),
        "query": getattr(task, "query", ""),
        "summary": getattr(task, "summary", ""),
        "report": body,
        # 永遠是**全文**長度,不受 max_report_chars 影響——這是你決定要不要調高上限的依據。
        "report_chars": len(report),
        "report_truncated": len(body) < len(report),
        "report_importable": report_importable,
        "cited_url_count": len(cited_urls),
        "candidates": candidates,
    }


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
async def research_import(
    notebook_id: str,
    task_id: str,
    urls: list[str] | None = None,
    include_report: bool = False,
    max_elapsed: float = 1800.0,
    account: str | None = None,
) -> dict:
    """把 **host 指名的**候選來源匯入筆記本,沒指名的一律不進來。

    `urls` 用 `research_wait` 回的候選 URL 原樣傳,不在候選清單裡的會直接 raise 並列出。
    `include_report=True` 另收 deep 報告本身(沒有 URL,只能靠這個旗標)。`account` 同
    `research_wait`。
    ⚠️ 匯入目標就是傳入的 `notebook_id`。research 跑拋棄式 scratch notebook,核可的來源再用
    `source_add_url` 進 episode notebook,否則被否決的候選會污染來源集。
    ⚠️ 外層 timeout 砍掉本呼叫時伺服器可能已 commit,**別直接重呼**(會重複匯入),先用
    `source_list` 對帳。"""
    # 走 SDK 的 import_sources_with_verification:先對 baseline 去重(已在 notebook 的回到
    # `already_present`)。IMPORT_RESEARCH 在 deep 負載下常常超過 30 秒、client 端先 timeout
    # 但伺服器其實已經 commit —— 0.8.2 會對帳後補送缺的那幾筆;**0.8.3 起不補送**,輪詢完
    # 一律 raise(`unconfirmed` + 候選/未對上清單)。報告條目沒有 URL,重呼時上游去重擋不住。
    task_id = _require(task_id, "task_id")
    # 空字串／非字串**不靜默丟掉**:那是呼叫端組清單時出了錯,吞掉會讓「我選了 5 筆」
    # 變成「進了 3 筆」而沒人發現。
    raw_urls = list(urls or [])
    bad = [u for u in raw_urls if not isinstance(u, str) or not u.strip()]
    if bad:
        raise ValueError(f"urls 含空字串/非字串項目:{bad!r}")
    clean_urls = [u.strip() for u in raw_urls]  # normalizer 不 strip 空白,先處理掉
    if not clean_urls and not include_report:
        raise ValueError("urls 為空且 include_report=False —— 沒有任何東西要匯入")

    client = _handle_client(account)
    # 從 task 重新取回完整 source 物件:呼叫端只需要傳 URL,不必把 research 報告與
    # 每筆 metadata 原封不動 round-trip 過 host context。
    task = await client.research.poll(notebook_id, task_id)
    status_str = _status_str(task)
    if status_str != "completed":
        # 沒有這道 gate 就能繞過 research_wait 直接匯入:in_progress 會收到半套候選,
        # failed 的 task **仍可能留著解析出來的 sources**,兩種都會靜默匯入錯東西。
        raise RuntimeError(
            f"task {task_id} 尚未完成(status={status_str!r}),不匯入;"
            "先跑 research_wait 等到 completed 再挑"
        )
    sources = list(getattr(task, "sources", ()) or ())
    if not sources:
        # 不讓它掉進下面的「這些 URL 不在候選清單裡」——那會把「task 查不到/已過期」
        # 誤報成「你 URL 打錯了」,診斷方向整個歪掉。
        raise RuntimeError(
            f"task {task_id} 完成了但沒有任何候選來源;"
            "task_id 打錯、跑錯 notebook,或該次 research 真的沒找到東西"
        )

    # identity 碰撞**只看被選取的**:候選清單裡兩筆不相干的來源剛好 canonical 相同,
    # 不該讓一次合法的 selection 整批失敗(那是過度 fail-closed)。
    by_url: dict[str, list] = {}
    for s in sources:
        url = getattr(s, "url", "") or ""
        if not url or getattr(s, "is_report", False):
            continue
        by_url.setdefault(_import_url_key(url), []).append(s)

    selected: list[object] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for url in clean_urls:
        key = _import_url_key(url)
        if key in seen:  # 呼叫端重複指名同一筆,去重(不是錯誤)
            continue
        seen.add(key)
        matches = by_url.get(key) or []
        if not matches:
            unknown.append(url)
        elif len(matches) > 1:
            # 被選中的 identity 一對多才拒絕。標題可能完全不同,先到先贏會讓 host
            # 挑了「官方 spec」卻匯入「轉載」——正是本工具要防的那類靜默錯誤。
            titles = [getattr(m, "title", "") for m in matches]
            raise RuntimeError(
                f"選中的 {url!r} 對應到 task {task_id} 的多筆候選(canonical={key!r}):"
                f"{titles};伺服器會把它們視為同一個來源,"
                "請直接用 source_add_url 指定你要的那份"
            )
        else:
            selected.append(matches[0])
    if unknown:
        raise ValueError(
            f"這些 URL 不在 task {task_id} 的候選清單裡:{unknown}"
            f"(候選共 {len(by_url)} 個 identity;請用 research_wait 回的 url 原樣傳)"
        )

    if include_report:
        report_entries = [
            s
            for s in sources
            if getattr(s, "is_report", False) and getattr(s, "report_markdown", "")
        ]
        if not report_entries:
            raise ValueError(
                f"include_report=True 但 task {task_id} 沒有可匯入的報告"
                "(fast mode 不產報告;deep 才有)"
            )
        selected = [*report_entries, *selected]

    with reconcile_hint_if_unconfirmed(notebook_id):
        imported = await client.research.import_sources_with_verification(
            notebook_id, task_id, selected, max_elapsed=max_elapsed
        )
    return {
        "imported": [
            {"source_id": entry.get("id"), "title": entry.get("title")} for entry in imported
        ],
        # 重呼時上游先對 baseline 去重:已經在 notebook 裡的不再匯入、只從這裡回報。
        "already_present": [
            {"source_id": entry.get("id"), "title": entry.get("title")}
            for entry in getattr(imported, "already_present", ())
        ],
        "requested": len(selected),
        # SDK 自己聲明回應可能少報幾筆(即使實際都匯入了);要精確對帳就看 source_list。
        "note": "回傳筆數可能少於實際匯入;用 source_list 對帳並確認 ready",
    }
