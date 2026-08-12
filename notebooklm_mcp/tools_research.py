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
from notebooklm._research import _normalize_import_verification_url as _import_url_key
from notebooklm.research import extract_report_urls, normalize_citation_url

from . import runtime
from .app import mcp


def _require(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


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
    """啟動 NotebookLM 內建研究,**立即**回 task_id(不等完成)。

    `mode="fast"` 快速網路搜尋(預設,便宜);`mode="deep"` Deep Research,會產出一份
    引用導向的報告,但要數十分鐘且吃配額——只用在跨來源有爭議、需要引用地圖的題目。
    `source="web"`(預設)或 `"drive"`;**deep 只支援 web**。

    ⚠️ 這支 RPC **只送 query 字串**:筆記本裡已有的來源對搜尋內容毫無影響。要讓搜尋
    貼著你已查證的種子走,得把專有名詞、別名、版本號、時間界線寫進 `query` 本身。

    回傳的 task_id 請先落地,再呼叫 `research_wait`——中途斷線可以重跑 wait 接回來。"""
    query = _require(query, "query")
    res = await runtime.get_client().research.start(
        notebook_id, query, source=source, mode=mode
    )
    # SDK 對「後端沒建出 task」回 None(或 task_id 空),不是 raise。讓它靜默通過的話,
    # 呼叫端會拿空 id 去 wait,最後以誤導性的 timeout 收場。
    task_id = getattr(res, "task_id", "") if res is not None else ""
    if not task_id:
        raise RuntimeError(
            f"research 未啟動(後端沒有建立 task);query={query!r} mode={mode!r} source={source!r}"
        )
    return {
        "task_id": task_id,
        "report_id": getattr(res, "report_id", None),
        "query": getattr(res, "query", query),
        "mode": getattr(res, "mode", mode),
        "source": source,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def research_wait(
    notebook_id: str,
    task_id: str,
    timeout: float = 1800.0,
    max_report_chars: int = 0,
) -> dict:
    """等 research 完成,回**候選來源 + 報告**。不匯入任何東西。

    可重入:同一個 task_id 重跑就是繼續等(斷線救援用這支,不要重新 `research_start`)。
    `candidates` 每筆有 `url` / `title` / `cited`(該 URL 是否被報告引用)。挑完之後把
    URL 交給 `research_import`。

    `max_report_chars` 預設 **0 = 不回報告本文**,只回 `report_chars` 讓你知道有多長
    ——deep research 報告動輒上萬字,預設灌回 context 太貴,而你真正要挑的是
    `candidates`。要讀報告就傳一個上限(例如 4000);task 已完成時重呼本工具會立刻回,
    不會重等也不燒配額。`include_report=True` 的匯入**不需要**先把報告讀回來
    (`research_import` 會在 server 端重新 poll 取得完整報告)。

    `report_importable=True` 表示這次(deep)research 產出了一份報告,可以在 import 時用
    `include_report=True` 一併收進筆記本。"""
    task_id = _require(task_id, "task_id")
    if not isinstance(max_report_chars, int) or isinstance(max_report_chars, bool):
        raise ValueError("max_report_chars must be an int")
    if max_report_chars < 0:
        raise ValueError("max_report_chars must be >= 0(0 = 只回字數,不回本文)")
    task = await runtime.get_client().research.wait_for_completion(
        notebook_id, task_id, timeout=timeout
    )
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
    candidates = [
        _candidate(s, cited_urls) for s in sources if not getattr(s, "is_report", False)
    ]
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
) -> dict:
    """把 **host 指名的**候選來源匯入筆記本。沒指名的一律不進來。

    `urls` 用 `research_wait` 回的候選 URL 原樣傳(比對前會做正規化)。指名了不存在的
    URL 會直接 raise 並列出來——寧可爆掉,也不要靜默少匯入幾筆讓你以為都進去了。
    `include_report=True` 另外收進 deep research 的報告本身。

    ⚠️ 匯入的是**這個 notebook_id**。research 建議跑在拋棄式 scratch notebook,核可的
    來源再用 `source_add_url` 進 episode notebook,避免候選污染生成用的來源集
    (見 ADR-0008)。"""
    # 走 SDK 的 import_sources_with_verification:IMPORT_RESEARCH 在 deep 負載下常常超過
    # 30 秒、client 端先 timeout 但伺服器其實已經 commit,它用 source list 對帳只補送真的
    # 沒進去的那幾筆,不盲目重送造成重複來源。
    task_id = _require(task_id, "task_id")
    # 空字串／非字串**不靜默丟掉**:那是呼叫端組清單時出了錯,吞掉會讓「我選了 5 筆」
    # 變成「進了 3 筆」而沒人發現。
    raw_urls = list(urls or [])
    bad = [u for u in raw_urls if not isinstance(u, str) or not u.strip()]
    if bad:
        raise ValueError(f"urls 含空字串/非字串項目:{bad!r}")
    clean_urls = [u.strip() for u in raw_urls]        # normalizer 不 strip 空白,先處理掉
    if not clean_urls and not include_report:
        raise ValueError("urls 為空且 include_report=False —— 沒有任何東西要匯入")

    client = runtime.get_client()
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
        if key in seen:      # 呼叫端重複指名同一筆,去重(不是錯誤)
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
            s for s in sources
            if getattr(s, "is_report", False) and getattr(s, "report_markdown", "")
        ]
        if not report_entries:
            raise ValueError(
                f"include_report=True 但 task {task_id} 沒有可匯入的報告"
                "(fast mode 不產報告;deep 才有)"
            )
        selected = [*report_entries, *selected]

    imported = await client.research.import_sources_with_verification(
        notebook_id, task_id, selected, max_elapsed=max_elapsed
    )
    return {
        "imported": [
            {"source_id": entry.get("id"), "title": entry.get("title")}
            for entry in imported
        ],
        "requested": len(selected),
        # SDK 自己聲明回應可能少報幾筆(即使實際都匯入了);要精確對帳就看 source_list。
        "note": "回傳筆數可能少於實際匯入;用 source_list 對帳並確認 ready",
    }
