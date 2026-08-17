"""按需生成單集附加產物(簡報 slide deck / 研讀 report),路徑回寫 series_manifest.json。
不碰音檔迴圈;想幫哪集加就對哪集跑。產物只餵傳入的 source_ids(不傳則 SDK 用全部來源)。"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from mcp.types import ToolAnnotations

from . import runtime
from ._atomic import download_atomically
from ._failover import describe_refusal, dispatch_with_failover
from ._sources import assert_sources_exist, to_source_ids
from ._status import ensure_completed
from .enums import to_report_format, to_slide_format, to_slide_length
from .languages import resolve_language
from .app import mcp
from ._text import _CITATION_RE, strip_inline_emphasis
from .manifest_store import ManifestStore
from .publish import notes_html


def _episode_in(data: dict, episode_n: int, manifest_path: str) -> dict:
    """從 snapshot 裡取出該集,取不到就 fail loud(不 setdefault 生一個空的出來)。"""
    ep = next(
        (
            e for e in data.get("episodes", [])
            if isinstance(e, dict) and e.get("episode") == episode_n
        ),
        None,
    )
    if ep is None:
        raise ValueError(f"episode {episode_n} not found in manifest {manifest_path}")
    return ep


# provenance 欄位名**直接寫在寫入點**(`_finish_slides` 的 `slides_account` /
# `slides_artifact_id`、`_finish_report` 的 `report_*`),不走 kind→欄位對照表:
# 那張表只有兩個消費端而兩邊都解析成常數(`_finish_report` 查字面 `"report"`;
# `_finish_slides` 的 `slides` 與 `revise_slide` 映到**同一組**欄位),等於用一層動態
# kwargs 把欄位名從 grep 藏起來,還誘導未來的人以為多加一個 kind 就會自動有 provenance。
# `kind` 只留在真正需要它的地方 —— `_dispatch_attachment`,那裡它區分三種稽核事件。


def _append_attachment_event(
    manifest_path: str,
    episode_n: int,
    kind: str,
    phase: str,
    reason: object,
    **extra,
) -> None:
    """往 episode 級的 append-only `attachment_errors` 寫一筆稽核。

    **為什麼是 episode 級,不是 attempt 級**:附件沒有 durable attempt(那是
    slides/report 家族既有的架構債,ADR-0011 決定不在這一版還它)。而 ADR-0010
    §Transparency 要的是兩個問題答得出來 —— 「這集簡報是哪個帳號生的」(provenance
    欄位)與「哪個帳號被拒過」(這裡)—— 兩者都不需要 attempt 結構就寫得下。

    **只 append,不清除**,理由與音檔的 `attempt["errors"]` 相同:重生會覆寫
    `slides_pdf_path` 與 provenance,診斷放在會被覆寫的欄位裡等於下一次成功就把
    「這個帳號今天被拒過」抹掉,而那正是唯一能看出「pool 有個 cookie 快死了」的訊號
    (ADR-0010:一個靜默吸收過期憑證的 pool 會一路吸到沒帳號可用)。

    **三種 phase 都走這一支**(換帳號 / 最後一腿被拒 / 受理不明)—— 各寫一個 writer
    就是「補一半」的溫床,而這裡真的踩過:第一版只寫了換帳號那一種,於是耗盡時最後
    一個被拒的帳號一筆都不留(單帳號 pool 則是完全沒有紀錄)。
    """
    reason_type, message = describe_refusal(reason)

    def mutate(data: dict) -> None:
        episode = _episode_in(data, episode_n, manifest_path)
        events = episode.setdefault("attachment_errors", [])
        entry = {
            "phase": phase,
            "kind": kind,
            "type": reason_type,
            "message": message,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        # **連續的同一件事只留一筆,用 `repeated` 記次數。** 這張表 append-only 且永不清除,
        # 而它最大的產生者是**確定性重複**的:權限被拒是設定問題(不 rotate),呼叫端每次
        # 重試都會寫一筆逐字相同的紀錄,而那一筆帶著整段補分享指引 —— 量過約 590 bytes。
        # 代價不只在這一集:`ManifestStore` 每次操作都整份 parse + 驗證兩次 + deepcopy 兩次
        # + 重新序列化,而且全在獨占 flock 內,所以脹大的表會拖慢**每一個**碰 manifest 的
        # 工具(量過:500 筆 → `read` 2.6ms、`update` 6.7ms,各是空表的 4.5× / 2.7×)。
        # 用「合併 + 計數」而不是「跳過」:診斷價值在「發生過、而且反覆發生」,次數不能丟。
        # 比對刻意不含 `recorded_at`(每次必然不同),只看事件身分。
        previous = events[-1] if events else None
        if previous is not None and all(
            previous.get(k) == v for k, v in entry.items() if k != "recorded_at"
        ):
            previous["repeated"] = previous.get("repeated", 1) + 1
            previous["last_recorded_at"] = entry["recorded_at"]
            return
        events.append(entry)

    ManifestStore(manifest_path).update(mutate)


# **provenance 只在成品真的落地時寫,而且與路徑同一次 `update`。**
#
# v0.9.16 的第二版試過「受理憑據」——受理成功之後、開始等生成之前就先把 account +
# artifact_id 落盤,想擋「等待途中被砍 → 重跑多燒一顆 artifact」。**獨立複審把它連同它的
# 配套 fence 一起打掉,而理由是對的**:那組欄位的語意是「**現在磁碟上這份**是誰、哪一顆
# 生的」,受理時就寫等於把「在飛的」與「已交付的」塞進同一組欄位。實測(probe)後果:
#
#     舊成品 X 還在磁碟上,重生 Y 受理後 wait 回 failed
#     → 檔案 = %PDF-OLD(X),manifest 卻聲稱 slides_artifact_id=Y / slides_account=B
#
# 而配套的 fence(下載前檢查「憑據還是不是我這一顆」)自己是 TOCTOU:檢查通過後在下載的
# `await` 裡被接手,最終「檔案 X、manifest Y」而**兩個工具呼叫都回成功** —— 那正是 fence
# 的 docstring 聲稱它換掉的東西(「把安靜地服務錯的 PDF 換成大聲失敗」),它其實沒做到。
#
# 所以退回最小且誠實的形狀:寫入時機 = 成品落地時,寫入內容 = 這一顆 artifact + 這一次
# 實際送出的帳號(救援下載寫 `None`,那是它真的不知道)。**殘留的兩件事誠實留著,不假裝
# 修好了**:①`download_atomically` 的 `os.replace` 與 manifest `update` 是兩個資源,兩者
# 之間 crash 會留下「新檔配舊 provenance」(要跨資源原子得有 journal);②同一集同一 kind
# 的並行仍是 last-writer-wins。要關掉②得把 `download_atomically` 拆成「抓到 temp + 驗證」
# 與「持 manifest 鎖做 artifact-id CAS 後才 replace」兩段 —— 那是 ADR-0011 延後的 attachment
# attempt 的第一步,不是一個檢查擋得住的。


async def _dispatch_attachment(
    dispatch,
    *,
    account: str | None,
    client: object,
    manifest_path: str,
    episode_n: int,
    kind: str,
    notebook_id: str,
) -> tuple[str, str | None, object]:
    """附件家族的 dispatch:共用 failover 迴圈 + episode 級稽核。

    三支工具(`generate_slides` / `generate_report` / `artifact_revise_slide`)共用這一支,
    **不各自接一次線** —— 三段一樣的 wiring 就是「只補一條路徑」的溫床,而這個 repo
    在 v0.8.0 已經因此吃過一次(failover 只補了 `_run_episode`,漏了 series 的重送分支)。

    回 `(artifact_id, account, client)`:後兩個是**實際送出的**那一組,呼叫端必須拿它們
    去做「等完成 → 下載」(推導見 `_failover.dispatch_with_failover` 的 docstring)。
    """
    def audit(phase: str, reason: object, **fields) -> None:
        # 三種 phase 全部進同一個 append-only 清單(這正是與音檔家族的差異 —— 音檔的三種
        # 落在 attempt 的三個不同位置)。所以這裡只需要把 phase 冠上 `attachment_` 前綴,
        # 不必分支;`manifest_path` / `episode_n` / `kind` 也只綁一次,不是三個 lambda
        # 各抄一遍那組參數。
        _append_attachment_event(
            manifest_path, episode_n, kind, f"attachment_{phase}", reason, **fields
        )

    return await dispatch_with_failover(
        dispatch,
        account=account,
        client=client,
        notebook_id=notebook_id,
        audit=audit,
    )


def _load_ep_and_write(manifest_path: str, episode_n: int, **fields) -> dict:
    """在 locked fresh snapshot 上合併單集欄位，避免其他 writer 的更新被覆蓋。"""
    def mutate(data):
        ep = _episode_in(data, episode_n, manifest_path)
        if "description" in fields:
            title = (ep.get("title") or "").strip()
            if title and fields["description"] == title:
                raise ValueError("description must not equal title(需真 show notes,publish 會擋)")
        ep.update(fields)
        return dict(ep)

    _, episode = ManifestStore(manifest_path).update(mutate)
    return episode


@mcp.tool()
async def episode_set_description(
    manifest_path: str,
    episode_n: int,
    description: str,
    strip_citations: bool = True,
) -> dict:
    """把單集 show notes 寫進 manifest 的 description(預設先清引用標記 [n])。

    取代「host 開 bash 改 JSON」那步:本工具與 generate_slides/generate_report 的
    回寫同在 server process 事件迴圈內同步讀改寫,天然不 interleave——chat_ask 產完
    show notes 即可回寫,不用等三個生成到齊。注意這不是跨 process 檔案鎖,別再用
    外部腳本同時改同一份 manifest。並前置驗 publish 的**全部** preflight 條件(非空、
    不等於標題、渲染後自包含),讓錯誤在寫入當下就爆,不留到發布才 fail。"""
    desc = description.strip()
    if strip_citations:
        # 引用標記 + inline 星號強調一起清:v0.9.14 驗收實測,含 `**粗體**` 的字串
        # 原樣寫進 manifest 再原樣進公開 RSS。清理放這裡(不是只放 chat_ask)是因為
        # 呼叫端也可能自己組 show notes,而這支是進 manifest 的唯一正門。
        desc = strip_inline_emphasis(_CITATION_RE.sub("", desc)).strip()
    if not desc:
        raise ValueError("description is empty(清完引用標記後也不可為空)")
    # 跑 publish 端那顆一模一樣的 guard。少了這道,夾帶 markdown 圖片／javascript: 連結的
    # show notes 會安穩寫進 manifest,直到整季生完、跑 publish_series 才 fail-closed——
    # docstring 自己宣告「錯誤在寫入當下就爆」,那就要真的做到(chat_ask 產的 notes 來自
    # 可被 prompt injection 的外部文章,這不是理論風險)。
    notes_html.render_episode_notes_html(desc, [])
    _load_ep_and_write(manifest_path, episode_n, description=desc)
    return {"episode": episode_n, "description": desc, "stripped": strip_citations}


def _validate_pdf(path: str) -> None:
    """簡報必須真的是 PDF。torn write 的 partial 檔常常前幾 byte 還在、尾巴沒了,
    但「非空」檢查會放行——magic 只是最便宜的一層,擋掉完全不成形的那種。"""
    with open(path, "rb") as handle:
        magic = handle.read(5)
    if magic != b"%PDF-":
        raise ValueError(f"downloaded slides are not a PDF (magic={magic!r}): {path}")


def _validate_utf8_text(path: str) -> None:
    """講義是 Markdown;截在多位元組字元中間的半份檔會在這裡被抓到。"""
    try:
        with open(path, encoding="utf-8") as handle:
            handle.read()
    except UnicodeDecodeError as exc:
        raise ValueError(f"downloaded report is not valid UTF-8 text: {path}") from exc


def _require_artifact_id(artifact_id: str) -> str:
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id must be a non-empty string")
    return artifact_id.strip()


def _require_episode(manifest_path: str, episode_n: int) -> None:
    """在**第一個遠端副作用之前**確認該集存在於 manifest。

    原本這個檢查只在 `_load_ep_and_write` —— 也就是生成/改版跑完、下載完之後才驗:
    `episode_n` 打錯就是燒完一次配額才 raise,而遠端那份 artifact 已經產生了。
    這裡仍**不驗** artifact ↔ episode 的 binding:v0.9.16 起 manifest 有
    `slides_artifact_id` / `report_artifact_id` 了,但它記的是「現在磁碟上這份是哪一顆生的」
    —— 是 provenance,不是「這一集只准用這一顆」的白名單。拿它當 binding gate 會把合法的
    重生擋掉。這支只擋打錯集號這種可預判的錯。"""
    # 走 `_episode_in` 而不是自己 inline 一次:那句 `episode N not found in manifest …`
    # 是被測試 match 的字串(`test_generate_slides_unknown_episode_errors`),各寫一份等於
    # 生成前的守門與回寫時的守門有兩份可以各自漂的訊息。
    _episode_in(ManifestStore(manifest_path).read(), episode_n, manifest_path)


async def _require_completed_slide_deck(client: object, notebook_id: str, artifact_id: str):
    """遠端 mutation 前驗 artifact:存在、屬於這個 notebook、是簡報、已完成。

    `get_or_none` 是「list 一次再比對 id」,所以一次呼叫同時回答「存不存在」與
    「屬不屬於這個 notebook」——`revise_slide` 的 RPC 只靠 artifact_id 定位,
    notebook_id 只是 routing header,錯配的 ID 不會被伺服器擋下來。
    (`get()` 在 0.8.0 會改成 raise,故用 sanctioned 的 `get_or_none`。)"""
    art = await client.artifacts.get_or_none(notebook_id, artifact_id)  # type: ignore[attr-defined]
    if art is None:
        raise ValueError(
            f"artifact {artifact_id} 不在 notebook {notebook_id}"
            "(用 artifact_list 確認 ID 與筆記本)"
        )
    kind = getattr(art.kind, "value", str(art.kind))
    if kind != "slide_deck":
        raise ValueError(
            f"artifact {artifact_id} 的 kind 是 {kind!r},不是 slide_deck —— "
            "artifact_revise_slide 只能改簡報"
        )
    if not art.is_completed:
        raise ValueError(
            f"artifact {artifact_id} 尚未完成(status={art.status_str!r}),不能改版;"
            "先 artifact_wait 等它完成"
        )
    return art


async def _finish_slides(
    client: object,
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    artifact_id: str,
    wait_timeout: float,
    account: str | None = None,
) -> dict:
    """生成之後的共用尾段:等完成→下載→回寫 manifest。

    抽出來是為了讓「已經生好、但 client 端斷線／timeout 丟掉結果」的 artifact 能只走
    這段救回來(`artifact_download_slides`),而不必重生一次燒配額。

    `account` = **實際送出這次生成的**帳號(failover 換過就是換過之後那個)。
    **救援下載傳 `None`,那不是偷懶而是事實**:它的存在前提是「生成那次的回應遺失了」,
    所以它知道成品是哪一顆 artifact、但不知道是誰生的 —— 寫一個明確的 `None` 讓事後查稽核
    的人看得出要另尋線索,比留著上一次的帳號好(那個值看起來是權威的,而它講的是別的成品)。
    provenance 與 `slides_pdf_path` **同一次 `update`**,理由見上方那段紀律。"""
    # client 是 public tool 入口固定下來的同一個帳號，不回頭讀全域 active slot。
    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    out = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), f"ep{episode_n:02d}-slides.pdf")
    # 固定檔名 → 重生就是就地覆寫。原子換檔,失敗時舊那份完整簡報原封不動。
    await download_atomically(
        out,
        lambda dest: client.artifacts.download_slide_deck(
            notebook_id, dest, artifact_id=artifact_id, output_format="pdf"
        ),
        _validate_pdf,
    )
    _load_ep_and_write(
        manifest_path, episode_n,
        slides_pdf_path=out, slides_account=account, slides_artifact_id=artifact_id,
    )
    return {"episode": episode_n, "slides_pdf_path": out, "artifact_id": artifact_id}


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
async def generate_slides(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    source_ids: list[str] | None = None,
    language: str | None = None,
    instructions: str | None = None,
    slide_format: str | None = "detailed",
    slide_length: str | None = "default",
    wait_timeout: float = 1800.0,
) -> dict:
    """生成該集簡報並下載 PDF,路徑回寫 manifest 的 slides_pdf_path。

    配額被拒(`RateLimitError`)時會**換 pool 裡下一個帳號原地重送**,與音檔家族同一個
    迴圈(`_failover.dispatch_with_failover`);實際生成的帳號寫進 `slides_account`,
    每一次換帳號往 `attachment_errors` append 一筆。全部帳號都被拒才原樣拋出。"""
    selected = to_source_ids(source_ids)
    _require_episode(manifest_path, episode_n)      # 打錯集號別燒一次生成配額
    # 記帳與送出同源:`snapshot()` 一次取 `(label, client)`,之後任何並行的 rotate 都
    # 影響不到這一次(ADR-0010——分兩次讀全域會讓紀錄記下 A、實際由 B 送出)。
    account, client = runtime.snapshot()
    if selected is not None:
        # 打錯/已刪的 source_id 伺服器不擋——燒完一次生成配額才發現拿到聚焦錯誤的簡報。
        # 用起始帳號驗就夠:failover 換過去的帳號若看不到這本 notebook,共用迴圈會把它
        # 翻成 NotebookAccessDenied 並**停止**輪替(權限是設定問題,不是配額問題)。
        await assert_sources_exist(client, notebook_id, selected)

    # **純本地的轉換一律在 closure 外面做。** 放進 closure 的話,`resolve_language` /
    # `to_slide_format` 拋的 `ValueError`(打錯 language code、打錯 enum)會落進共用迴圈的
    # 泛用 except,被記成一筆 `attachment_acceptance_unknown` —— 實測 SDK 呼叫次數是 0,
    # 卻在 append-only 的稽核裡永久留下「遠端受理不明」。那是憑空造出來的假紀錄,而且
    # 清不掉(獨立複審第二輪 F4)。
    resolved_language = resolve_language(language)
    resolved_slide_format = to_slide_format(slide_format)
    resolved_slide_length = to_slide_length(slide_length)

    async def _generate(active_client):
        return await active_client.artifacts.generate_slide_deck(
            notebook_id,
            source_ids=selected,
            instructions=instructions,
            language=resolved_language,
            slide_format=resolved_slide_format,
            slide_length=resolved_slide_length,
        )

    # 後兩個回傳值是**實際送出的**帳號與 client。下載一定要用它們 —— 風險視窗幾乎全在
    # 後半段(dispatch 幾秒,等生成 + 下載是數十分鐘),回頭讀全域會在十幾分鐘後爆 401
    # 而根因在遠處(推導見 `_failover.dispatch_with_failover` 的 docstring)。
    artifact_id, account, client = await _dispatch_attachment(
        _generate,
        account=account,
        client=client,
        manifest_path=manifest_path,
        episode_n=episode_n,
        kind="slides",
        notebook_id=notebook_id,
    )
    return await _finish_slides(
        client, notebook_id, manifest_path, episode_n, artifact_id, wait_timeout,
        account=account,
    )


@mcp.tool()
async def artifact_download_slides(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    artifact_id: str,
    wait_timeout: float = 1800.0,
) -> dict:
    """把**已經生成**的簡報用 artifact_id 下載並回寫 manifest,不重新生成。

    救援用:client timeout 砍掉 `generate_slides` 時雲端那份其實生完了,用
    `artifact_list(kind="slide_deck")` 找回 ID 就能省一次配額。不確定是哪一筆別猜
    ——重生比綁錯便宜。"""
    client = runtime.get_client()
    return await _finish_slides(
        client,
        notebook_id,
        manifest_path,
        episode_n,
        _require_artifact_id(artifact_id),
        wait_timeout,
    )


@mcp.tool()
async def artifact_revise_slide(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    artifact_id: str,
    slide_index: int,
    prompt: str,
    wait_timeout: float = 1800.0,
) -> dict:
    """改**已生成簡報中的單一頁**(0-based `slide_index`),再重新下載回寫 manifest。

    省配額用:一頁改一句話不必整份重生(那還會連帶改動其他頁)。結束後走與生成相同的
    尾段(等完成→原子換檔下載→回寫 `slides_pdf_path`)。
    `artifact_id` 用 `artifact_list(kind="slide_deck")` 找。

    ⚠️ **這不是就地修改:遠端會多出一顆新 artifact,舊的留著。** v0.9.0 真實驗收實測
    (本 docstring 原本寫「artifact 不變」,是錯的):revise 之後 `artifact_list` 多一顆
    `<原標題> (2)`,原本那顆**原封不動還在**。回傳的 `artifact_id` 是**新的那顆**
    (manifest 也回寫成它),`superseded_artifact_id` 是被取代的舊那顆。

    所以連續 revise 會讓遠端堆出 `(2)`、`(3)`… 而它們**標題只差一個序號**——正好放大
    「artifact_list 分不出這是哪一集的 deck」那個既有風險。要清乾淨的話,拿
    `superseded_artifact_id` 自己決定要不要刪(本工具刻意不自動刪:那是遠端破壞性動作,
    而且萬一新的那顆有問題,舊的是唯一的退路)。"""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string(空 prompt 等於白改一次)")
    artifact_id = _require_artifact_id(artifact_id)
    account, client = runtime.snapshot()
    # 所有可預判的錯都擋在第一個遠端副作用之前。**注意這不是 durable attempt**:
    # 外層 client 在 revise 成功之後、下載回寫之前斷線,重跑仍會再 revise 一次
    # ——那是整個 slides/report 家族共有的架構債(generate_slides 逐字同形),
    # 要修得連同 generate 一起做成 attachment attempt,不在本工具的範圍(ADR-0011)。
    _require_episode(manifest_path, episode_n)
    # 用起始帳號驗:pool 全員看得到同一本 notebook 是 ADR-0010 的前置條件,而 artifact
    # 跨帳號可見已在 v0.8.0 驗收量過。換過去的帳號若看不到,`revise_slide` 會 permission
    # denied,共用迴圈翻成 NotebookAccessDenied 並停止輪替(設定問題不靠輪替掩蓋)。
    await _require_completed_slide_deck(client, notebook_id, artifact_id)

    async def _revise(active_client):
        return await active_client.artifacts.revise_slide(
            notebook_id, artifact_id, slide_index, prompt.strip()
        )

    # **改版也建 artifact,所以走同一個 failover 迴圈** —— 它不是唯讀救援。
    # ⚠️ 但「也會被同步拒絕」這半句**沒有實測支持**:v0.9.16 驗收在 slide_deck 生成配額
    # 已完全耗盡的槽位上連送 9 次 revise,9 次全部受理且 fork 都真的完成 ——
    # `REVISE_SLIDE` 與生成用的 `CREATE_ARTIFACT` 是**不同 RPC、不同配額桶**。
    # 所以這條路的 failover 對「限流」沒有已知觸發條件(仍可能由
    # `ArtifactFeatureUnavailableError` 觸發,那條沒被排除),而「被拒時會不會 fork 出
    # 一顆孤兒 `(2)`」也就一直是**待確認**(ADR-0011)。接著它是為了不留缺口,不是因為
    # 已經證明過它冪等。
    revised_id, account, client = await _dispatch_attachment(
        _revise,
        account=account,
        client=client,
        manifest_path=manifest_path,
        episode_n=episode_n,
        kind="revise_slide",
        notebook_id=notebook_id,
    )
    # **以回傳值為準,不假設它是同一顆** —— 這個「不自己假設」的寫法救了這支工具:
    # v0.9.0 真實驗收證明 REVISE_SLIDE **不是**就地改版,伺服器會 fork 出一顆新的
    # `<原標題> (2)`,舊的留著。當初若照 docstring 寫死用輸入的 artifact_id,下載到的
    # 會是**沒改過的舊那份**,而且看起來完全成功。
    out = await _finish_slides(
        client, notebook_id, manifest_path, episode_n, revised_id, wait_timeout,
        account=account,
    )
    out["slide_index"] = slide_index
    # 讓呼叫端看得出 id 換了、舊的還在遠端 —— 否則它只會拿到一個「artifact_id 跟我傳的
    # 不一樣」的回傳值,無從判斷是 fork 還是自己記錯。
    if revised_id != artifact_id:
        out["superseded_artifact_id"] = artifact_id
    return out


async def _finish_report(
    client: object,
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    artifact_id: str,
    report_format: str,
    wait_timeout: float,
    account: str | None = None,
) -> dict:
    """生成之後的共用尾段(同 `_finish_slides`,`account` 的語意也同那裡)。"""
    # client 是 public tool 入口固定下來的同一個帳號，不回頭讀全域 active slot。
    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    out = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), f"ep{episode_n:02d}-report.md")
    # 同 _finish_slides:固定檔名的就地覆寫換成原子換檔。
    await download_atomically(
        out,
        lambda dest: client.artifacts.download_report(notebook_id, dest, artifact_id=artifact_id),
        _validate_utf8_text,
    )
    _load_ep_and_write(
        manifest_path, episode_n,
        report_md_path=out, report_format=report_format,
        report_account=account, report_artifact_id=artifact_id,
    )
    return {"episode": episode_n, "report_md_path": out, "report_format": report_format, "artifact_id": artifact_id}


def _validate_report_prompt(
    report_format: str, custom_prompt: str | None, extra_instructions: str | None
) -> str | None:
    """`custom` 與 `custom_prompt` 必須成對,且 custom 不吃 extra_instructions。

    三種誤用 SDK 全都**靜默吞掉**,要燒完一次配額、拿到一份不是你要的講義才會發現:
      - `custom` 無 prompt → `_report_config` 套 "Create a report based on the
        provided sources."(payloads.py:538)
      - 靜態格式帶 prompt → `_report_config` 走 `_STATIC_REPORT_CONFIGS`,prompt 丟掉
      - `custom` 帶 extra_instructions → `payloads.py:219` 明確跳過串接
    """
    prompt = custom_prompt.strip() if isinstance(custom_prompt, str) else None
    if report_format == "custom":
        if not prompt:
            raise ValueError(
                "report_format='custom' 需要非空的 custom_prompt"
                "(否則 SDK 靜默套用通用預設句,白燒一次配額)"
            )
        if extra_instructions:
            raise ValueError(
                "report_format='custom' 不吃 extra_instructions(SDK 不串接,會靜默消失);"
                "把要求併進 custom_prompt"
            )
    elif prompt:
        raise ValueError(
            f"custom_prompt 只在 report_format='custom' 有效"
            f"(現在是 {report_format!r},SDK 會套靜態模板並丟掉 prompt)"
        )
    return prompt


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
async def generate_report(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    report_format: str = "study_guide",
    source_ids: list[str] | None = None,
    language: str | None = None,
    extra_instructions: str | None = None,
    custom_prompt: str | None = None,
    wait_timeout: float = 1800.0,
) -> dict:
    """生成該集研讀文件(預設 study_guide)並下載 Markdown,路徑回寫 report_md_path。

    `report_format="custom"` + `custom_prompt` = 完全自訂講義結構(三種靜態模板
    study_guide / briefing_doc / blog_post 之外的形狀)。兩者必須成對,且 custom
    格式不吃 `extra_instructions`——要求併進 `custom_prompt`。

    配額 failover 與 `generate_slides` 逐字同形(共用同一個迴圈);實際生成的帳號寫進
    `report_account`。"""
    custom_prompt = _validate_report_prompt(report_format, custom_prompt, extra_instructions)
    selected = to_source_ids(source_ids)
    _require_episode(manifest_path, episode_n)      # 同上
    account, client = runtime.snapshot()            # 同 generate_slides:記帳與送出同源
    if selected is not None:
        # 同 generate_slides:打錯/已刪的 source_id 伺服器不擋,先唯讀對帳。
        await assert_sources_exist(client, notebook_id, selected)

    # 同 generate_slides:純本地轉換擋在 closure 外,否則打錯 report_format / language
    # 會被記成假的「遠端受理不明」。
    resolved_report_format = to_report_format(report_format)
    resolved_language = resolve_language(language)

    async def _generate(active_client):
        return await active_client.artifacts.generate_report(
            notebook_id,
            source_ids=selected,
            custom_prompt=custom_prompt,
            extra_instructions=extra_instructions,
            report_format=resolved_report_format,
            language=resolved_language,
        )

    artifact_id, account, client = await _dispatch_attachment(
        _generate,
        account=account,
        client=client,
        manifest_path=manifest_path,
        episode_n=episode_n,
        kind="report",
        notebook_id=notebook_id,
    )
    return await _finish_report(
        client,
        notebook_id,
        manifest_path,
        episode_n,
        artifact_id,
        report_format,
        wait_timeout,
        account=account,
    )


@mcp.tool()
async def artifact_download_report(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    artifact_id: str,
    report_format: str = "study_guide",
    wait_timeout: float = 1800.0,
) -> dict:
    """把**已經生成**的講義用 artifact_id 下載並回寫 manifest,不重新生成。

    救援用,同 `artifact_download_slides`(client timeout 丟掉結果時省一次配額)。
    `report_format` 只影響回寫 manifest 的標記,傳當初生成用的那個值。"""
    client = runtime.get_client()
    return await _finish_report(
        client,
        notebook_id,
        manifest_path,
        episode_n,
        _require_artifact_id(artifact_id),
        report_format,
        wait_timeout,
    )
