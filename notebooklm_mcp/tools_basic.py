"""Basic NotebookLM tools.

Thin wrappers over the resident client, with zh_Hant default and enum mapping
baked in. Each tool returns a plain JSON-able dict.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from mcp.types import ToolAnnotations
from notebooklm.rpc.types import SharePermission

from . import runtime
from ._errors import NotebookAccessDenied, is_permission_denied, raise_if_access_denied
from ._sources import (
    assert_source_count_is_safe,
    assert_sources_exist,
    to_source_ids,
)
from ._status import ensure_completed, ensure_started
from ._text import _CITATION_RE, norm as _norm, strip_inline_emphasis
from .auth_probe import probe_auth
from .enums import to_audio_format, to_audio_length
from .languages import resolve_language
from .app import mcp


# 不該「只改成 .md」就送上去的副檔名。三類、三個理由,合成一份是因為行為相同
# (原樣交給 SDK):
#  (a) 文件/表格 —— 官方或實測可直接上傳的格式;包成 Markdown 會丟掉 NotebookLM 對
#      表格(CSV/TSV)、簡報(PPTX)等來源的原生處理語意。
#  (b) 圖片/音訊/影片 —— 實裝 SDK 有 SourceType.IMAGE / MEDIA;小圖片可能碰巧解得開
#      UTF-8,不能讓它進自動包裝。
#  (c) HTML family —— 上游 _source/upload.py:262 的 _HTML_UPLOAD_SUFFIXES 刻意
#      ValidationError 擋掉,要求 caller 先轉成乾淨文字;只改副檔名會把 script/style/
#      導覽 markup 偷渡進去並繞過那道驗證,所以原樣送、保留上游自己的清楚錯誤。
# 這份清單**不是** endpoint support allowlist(我們無法從外部證明那件事),語意只有
# 「這些格式不適合用改副檔名來處理」。也刻意不用 mimetypes.guess_type():它會讀
# /etc/mime.types,同一支 .ts 在有/無該檔的機器上分類不同,3 VM + podcast-lab 會得到
# 不決定性的轉換行為。
_NO_AUTO_WRAP_SUFFIXES = {
    ".pdf", ".txt", ".md", ".markdown", ".doc", ".docx", ".rtf", ".odt",
    ".csv", ".tsv", ".epub", ".pptx",                                       # (a)
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",                       # (b) 圖片
    ".mp3", ".m4a", ".wav", ".aac", ".mp4", ".mov", ".webm",                # (b) 音訊/影片
    ".html", ".htm", ".xhtml", ".xht",                                      # (c)
}
_MAX_CONVERT_BYTES = 25 * 1024 * 1024


def _as_uploadable_text(file_path: str, tmpdir: str) -> tuple[str, str | None]:
    """回傳 (實際上傳路徑, converted_from)。

    NotebookLM 的 upload endpoint 對 `.json`/`.ts`/`.py`/`.yaml` 這類副檔名直接回
    400,而每次新檔案的副檔名都不同——靠文件提醒等於每次都要有人先踩一次(EP36 的
    fixture-output.json)。所以在唯一的 caller-facing 檔案入口自動繞過:非上列副檔名
    且讀得開的小 UTF-8 文字檔複製成 `<原檔名>.md`(保留原副檔名做出處,`a.ts` 與
    `a.json` 不會撞成同名),交給 SDK 從 `.md` 推導 text/markdown。

    caller 顯式傳 `mime_type` 時呼叫端根本不會進來——它比我們清楚那是什麼。
    ponytail: 天花板是「不傳 mime_type 的小 ASCII .bin 會被包成 .md」——結果是上傳
              成功而不是 400,可接受;要更嚴格再加 magic-byte 嗅探。"""
    p = Path(file_path)
    if p.suffix.lower() in _NO_AUTO_WRAP_SUFFIXES:
        return file_path, None
    try:
        if p.stat().st_size > _MAX_CONVERT_BYTES:
            return file_path, None
        text = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return file_path, None
    if "\x00" in text:      # NUL 是合法 UTF-8;二進位常見,decode 擋不掉
        return file_path, None
    dest = Path(tmpdir) / (p.name + ".md")
    dest.write_text(text, encoding="utf-8")
    return str(dest), p.name


async def _probe_extraction(
    client: object, notebook_id: str, source_id: str, *, is_file: bool
) -> dict:
    """加來源後的 best-effort 落地驗證:只回 char_count(+空殼 warning),不回全文。

    probe 失敗不連坐 add(來源已成功上傳),回 char_count=None + note。
    措辭分流:URL 空殼多半是 paywall/動態頁;檔案空殼可能只是音檔/掃描 PDF,不能亂指控。"""
    try:
        ft = await client.sources.get_fulltext(notebook_id, source_id)  # type: ignore[attr-defined]
        n = ft.char_count
    except Exception as exc:  # noqa: BLE001 — probe 是加值檢查,任何失敗都不該讓 add 白做
        return {"char_count": None,
                "note": "extraction probe failed (best-effort, source 已上傳): "
                f"{type(exc).__name__}: {exc}"}
    out: dict = {"char_count": n}
    if not n:
        out["warning"] = (
            "extracted text is empty — 檔案可能是音檔/掃描 PDF(無文字層)或壞檔;"
            "若應為文字內容,請 source_delete 後改 source_add_text 貼全文"
            if is_file else
            "extracted text is empty — 疑似 paywall/登入牆/動態頁空殼;"
            "請 source_delete 後抓全文改用 source_add_text/source_add_file"
        )
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def auth_check() -> dict:
    """輕量真 RPC 驗證 NotebookLM 認證(cookie)是否有效。

    長流程(整季生成、發布)前先跑,cookie 死了會秒退並回重登指引,
    避免燒掉數小時等待。回傳 {"ok": True, "notebooks": N}。
    """
    return await probe_auth(runtime.get_client())


def _pool_peers(context: str, active_label: str) -> list[str]:
    """pool 裡除了 `active_label` 之外的帳號 email。

    `active_label` 由呼叫端 `runtime.snapshot()` 一次取好傳進來,這裡不回頭讀
    `runtime.active_account()`——雖然目前所有呼叫點在讀 snapshot 到呼叫這支之間
    都沒有 await(這段本身是乾淨的),但把「排除誰」的決定權放在參數上,下一個
    改動就算在中間插進一個 await 也不會又踩回同一種縫。

    `dict.fromkeys` 去重是 defense-in-depth,**不是生產可達路徑**:`app.py` 的
    `_reject_duplicate_accounts` 在 `runtime.set_clients()` 之前就擋掉重複帳號
    的 pool,production 的 `_POOL` 不可能出現重複 label。這裡留著只防**繞過
    lifespan 直接呼叫 `runtime.set_clients()`** 的呼叫端——測試就是這樣做的
    (`test_duplicate_pool_slots_are_deduped`),那條路徑上 server 本來就不會啟動。

    label 退回 "#N" 表示啟動時拿不到那個帳號的 email,分享不了。靜默略過會讓它
    永遠沒有權限,而症狀要等 failover 換過去才出現——中間隔著整段生成時間。

    `context` 只用來組錯誤訊息:`notebook_create` 的驗證在 create() 之前跑,
    還沒有 notebook_id,傳 title;`notebook_share_with_pool` 傳真正的 notebook_id。
    """
    others = list(dict.fromkeys(
        a for a in runtime.all_accounts() if a != active_label
    ))
    unresolved = [a for a in others if "@" not in a]
    if unresolved:
        raise RuntimeError(
            f"{context!r}:pool 裡的 {unresolved} 沒有可用的帳號 email,"
            "無法分享 —— 這些槽位在 server 啟動時取不到 email。請手動分享或重啟 server。"
        )
    return others


def _has_sufficient_permission(status, email: str) -> bool:
    """email 在 ShareStatus.shared_users 裡是否已經是 EDITOR 或以上(含 OWNER)。

    email 比對用 `.casefold()`:大小寫不同不該恆為 False——比對永遠失敗會讓
    `add_user` 明明成功、後檢卻判定「未生效」而 raise,留下孤兒分享(P2,
    fail-closed,不影響安全,只是多噴一次錯誤)。**刻意不**額外正規化
    dot/plus-alias(Gmail 個人帳號的 `a.b@x.com`==`ab@x.com`、
    `a+tag@x.com`==`a@x.com`)——那是 Google 個人帳號的規則,不是所有 workspace
    網域都遵守,做了等於替對方猜信箱政策,過猶不及。

    只認 email 不夠:`add_user` 的預設是 VIEWER(`_sharing.py:144`),使用者在
    NotebookLM 網頁上手動分享過的既有 notebook 極可能就是 VIEWER,那樣 pool 其實
    還是壞的(failover 換過去一樣 permission denied)。把 OWNER 也算進「已足夠」
    順帶擋掉另一件事:`_pool_peers` 只排除作用中帳號、不知道誰是 notebook owner,
    failover 之後 peers 可能含 owner 本人——SDK 只擋 `permission==OWNER` 這個
    **參數**,不擋「對象就是 owner」,對 owner 呼叫 add_user(EDITOR) 等於把他降權。

    ✅ **前提已於 v0.9.0 真實驗收結案(2026-08-09)**:`get_status()` 回的
    `shared_users` **確實包含 owner 自己那一列**,`permission` 就是
    `SharePermission.OWNER`。9 帳號 pool 的實測回應:owner 一列 OWNER + 其餘 8 列
    EDITOR,共 9 列(唯讀 `sharing.get_status()`,沒有動到任何人的權限)。
    所以這條 OWNER 豁免是對的,而它要防的那件事**真的會發生**——同一輪的實跑反例:
    pool 因配額 rotate 到 slot 4 之後,對 slot 1 擁有的既有 notebook 跑
    `notebook_share_with_pool`,owner 確實落進 peers,而它被判定「已足夠」放進
    `already_shared`、**沒有**被打 `add_user(EDITOR)`。少了這條豁免,那次呼叫就會
    把 owner 降權,且後檢看到 EDITOR 會判定「已生效」直接放行——不 raise、無痕跡。
    離線側由 `tests/test_pool_gaps.py` 釘住(fake 也必須帶 `permission` 欄位,
    否則 VIEWER 那個 P0 沒有任何測試會紅)。
    """
    target = email.casefold()
    return any(
        (getattr(u, "email", None) or "").casefold() == target
        and getattr(u, "permission", None) in (SharePermission.EDITOR, SharePermission.OWNER)
        for u in getattr(status, "shared_users", [])
    )


def _owner_slot(status) -> tuple[str, object] | None:
    """`status` 裡的 owner 若正好是 pool 的某個槽位,回它的 `(label, client)`。

    **一趟 `get_status` 就能定位 owner,不必逐槽掃**:回傳的 `shared_users` 含 owner
    自己那一列(v0.9.0 真實驗收實測,`_has_sufficient_permission` 的 docstring 記著
    那次 9 帳號 pool 的回應形狀)。分享狀態是 notebook 的屬性、與誰查無關,所以
    **owner 的 client 不必再查一次** —— 手上這份就是它會看到的那份。

    找不到(owner 不在 pool——notebook 是外部帳號分享進來的)回 None,呼叫端就退回
    「看得到的那個」去試。那可能被伺服器拒絕,但那是遠端的答案,不該預先替它判死。
    """
    owner = next(
        (
            (getattr(u, "email", None) or "")
            for u in getattr(status, "shared_users", [])
            if getattr(u, "permission", None) == SharePermission.OWNER
        ),
        "",
    ).casefold()
    if not owner:
        return None
    return next(
        (
            (label, client)
            for label, client in runtime.all_clients()
            if label.casefold() == owner
        ),
        None,
    )


async def _resolve_share_executor(notebook_id: str) -> tuple[str, object, object]:
    """找出 pool 裡**分享得動這個 notebook** 的帳號,回 `(label, client, share_status)`。

    存在理由是一個真實驗收抓到的死路(v0.9.0 Phase 9-1):`podcast_series` 因權限被拒
    停下,停點的 `error` 指引呼叫端來跑 `notebook_share_with_pool` —— 但當時作用中帳號
    **正是那個看不到 notebook 的**(配額 failover 換過去了),於是那支工具自己也
    permission denied,呼叫端只是換一個死路。既有 notebook 只有能看到它的帳號分享得動。

    **先試作用中的**(絕大多數情況就是它,零額外成本),permission denied 才依槽位順序
    試其餘的。**只對 permission denied 往下試** —— 網路錯誤、認證過期之類的問題,每個
    槽位都會遇到,一路吞下去只會把真正的根因埋掉,所以原樣拋出。

    **查得到 ≠ 改得動:成功之後還要換到 owner。** 作用中帳號在 pool 裡通常是個
    EDITOR(owner 早就把 notebook 分享給全 pool 了,所以它看得到),而改分享設定要的
    是 owner。只認「`get_status` 過得了」就挑中它,`add_user` 會由一個很可能無權的
    帳號發出,而 pool 裡真正的 owner 從沒被試過——症狀還會偽裝成「這個 notebook
    沒救」。owner 由手上這份 status 直接定位(見 `_owner_slot`),不需要額外 RPC。

    掃描全是唯讀 `get_status`,而且**不動輪替游標**(`_ACTIVE` 的語意是「配額走到哪」,
    借去做別的事會讓 failover 的帳號記帳失去意義)。
    """
    active_label, active_client = runtime.snapshot()
    ordered: list[tuple[str, object]] = [(active_label, active_client)]
    ordered += [
        (label, client)
        for label, client in runtime.all_clients()
        if label != active_label
    ]

    denied: list[str] = []
    for label, client in ordered:
        try:
            status = await client.sharing.get_status(notebook_id)
        except Exception as exc:  # noqa: BLE001 —— 下一行就把非權限問題原樣拋回去
            if not is_permission_denied(exc):
                raise
            denied.append(label)
            continue
        owner = _owner_slot(status)
        return (*owner, status) if owner else (label, client, status)

    raise NotebookAccessDenied(
        f"notebook {notebook_id!r}:pool 裡**沒有任何帳號**看得到它({denied} 全部 "
        "permission denied),所以沒有人分享得動。這個 notebook 不屬於這個 pool ——"
        "請用它真正的擁有者帳號在 NotebookLM 網頁上分享給 pool 成員(EDITOR),"
        "或改用 notebook_create 建一個新的(pool 模式下會自動分享)。"
    )


async def _share_each(notebook_id: str, emails: list[str], client) -> list[str]:
    """一次送出全部分享,失敗時提供 notebook 對帳指引。

    0.8.1 的 `set_users` 是 upsert,所以不需要逐個 RPC 與進度回報；但取消或逾時
    仍可能發生在伺服器受理後,錯誤訊息必須保留 notebook id 並指引重跑本工具對帳。

    `client` 由呼叫端 `runtime.get_client()` 一次取好傳進來,這裡不回頭讀
    `runtime.get_client()`。呼叫端與這裡之間隔著至少一次 await
    (`notebooks.create()` 或 `sharing.get_status()`),MCP 是並行的
    (`mcp/server/lowlevel/server.py` 對每則 message `tg.start_soon`),另一個工具
    呼叫可能在那個 await 裡撞到配額並 `rotate_client()`——回頭讀全域會讓分享由
    **跟建立/查詢時不同的帳號**發出,對方甚至還看不到這個剛建的 notebook。
    """
    if not emails:
        return []
    grants = [(email, SharePermission.EDITOR) for email in emails]
    try:
        status = await client.sharing.set_users(notebook_id, grants, notify=False)
    except (Exception, asyncio.CancelledError) as exc:
        exc.args = (
            f"notebook {notebook_id!r} set_users 分享失敗({exc})。"
            "沒分享到的帳號在 failover 換過去時會 permission denied。"
            "請改跑 notebook_share_with_pool 重試對帳。",
        )
        raise
    for email in emails:
        if not _has_sufficient_permission(status, email):
            # set_users 回傳的 ShareStatus 是零成本的後檢(那趟 RPC 本來就打了):
            # workspace 網域政策擋外部分享、email 打錯字、或伺服器靜默忽略,都不會
            # raise,回應成功但實際沒生效——根因在這裡,症狀要等十幾分鐘後
            # failover 才爆(source_add_file 的 title= 後檢立的同一條紀律)。
            raise RuntimeError(
                f"notebook {notebook_id!r} 分享給 {email} 呼叫成功但未生效"
                "(get_status 後檢仍不是 EDITOR/OWNER——可能是 workspace 網域政策"
                f"擋外部分享,或伺服器靜默忽略)。請改跑 notebook_share_with_pool 重試對帳。"
            )
    return list(emails)


def _nothing_to_share(notebook_id: str, executor_label: str | None) -> dict:
    """沒有 peers 要分享時的回傳。**欄位必須與正常路徑完全一致**——少一個
    `shared_by` 就等於呼叫端統一讀它時會 KeyError,而那條路徑(單帳號)最常見。
    """
    return {
        "notebook_id": notebook_id,
        "shared_by": executor_label,
        "shared_with": [],
        "already_shared": [],
    }


@mcp.tool()
async def notebook_share_with_pool(notebook_id: str) -> dict:
    """把**既有** notebook 分享給多帳號 pool 裡的其餘帳號(EDITOR)。

    `notebook_create` 從 v0.8.1 起會自動做這件事;這支是給**既有** notebook 補的
    ——v0.8.1 之前建的、或在 NotebookLM 網頁上手動建的。沒有這個前置狀態,配額耗盡
    後 failover 換帳號時會 `NotebookAccessDenied`。

    **冪等**:已經是 EDITOR/OWNER 的帳號直接跳過,重跑安全。單帳號模式是
    no-op(不打任何 RPC)。

    **自己找得動手的那個帳號,不看游標。** 這支工具的典型使用時機,就是
    `podcast_series` 因為權限被拒而停下、指引呼叫端來跑它 —— 而那個當下,
    作用中帳號**正好是看不到這個 notebook 的那一個**(配額 failover 換過去了)。
    照著游標走的話,指引自己也 permission denied,呼叫端只是從一個死路換到另一個
    (v0.9.0 真實驗收 Phase 9-1 實測)。所以這裡改成掃 pool 找出**分享得動它**的
    帳號來執行(見 `_resolve_share_executor`:先試作用中的,再依槽位順序,成功之後
    還要換到 owner)。掃描只打唯讀 `get_status`,而且**不動輪替游標** —— 那個游標的
    語意是「配額走到哪」,借去做別的事會讓 failover 的帳號記帳失去意義。
    """
    # **順序有意義:沒有 peers 就先走人,不要用一趟遠端呼叫換一個純本機的答案。**
    # v0.9.1 一度把 executor 掃描擺在這之前,於是單帳號模式對一個不屬於自己的
    # notebook 呼叫這支工具,會從「安靜回 no-op」變成拋 NotebookAccessDenied,
    # 而那個訊息還叫人「分享給 pool 成員」——單帳號模式根本沒有 pool。
    if runtime.account_count() == 1:
        return _nothing_to_share(notebook_id, runtime.active_account())
    executor_label, executor_client, status = await _resolve_share_executor(notebook_id)
    peers = _pool_peers(notebook_id, executor_label)
    if not peers:
        return _nothing_to_share(notebook_id, executor_label)
    already = [e for e in peers if _has_sufficient_permission(status, e)]
    todo = [e for e in peers if e not in already]
    return {
        "notebook_id": notebook_id,
        "shared_by": executor_label,
        "shared_with": await _share_each(notebook_id, todo, executor_client),
        "already_shared": already,
    }


@mcp.tool()
async def notebook_create(title: str) -> dict:
    """Create a new notebook. Returns its id.

    多帳號 pool 模式下會自動把它分享給其餘帳號(EDITOR):failover 換帳號後是拿新
    帳號對**同一個 notebook_id** 送出,新帳號看不到它的話整條 pool 是空談
    (v0.8.0 驗收 F-2:實測其餘帳號 `notebooks.get` 一律 permission denied)。
    單帳號模式完全不打額外 RPC,行為不變。既有 notebook 用 `notebook_share_with_pool`。
    """
    # 驗證一律先於變更:_pool_peers 是純本機檢查(只讀 runtime.all_accounts()),
    # 卻原本放在 create() 之後——某槽位啟動時拿不到 email 是**決定性**失敗
    # (重啟前不會變),放在 create 之後等於「呼叫端每重試一次就多一個雲端孤兒
    # notebook」。這裡還沒有 notebook_id,錯誤訊息用 title 代替。
    #
    # `runtime.snapshot()` 只取一次:create() 是 await,MCP 並行——另一個工具
    # 呼叫可能在這個 await 裡撞到配額並 rotate_client(),之後若再回頭讀
    # `runtime.get_client()` 分享,就會變成用**換過去那個帳號**去分享一個它自己
    # 都還看不到的 notebook(而且 `rotate_client()` 不會回頭,重啟前這個 process
    # 全部後續呼叫都錯位)。`label`/`client` 綁死在這一次呼叫上,全程不再回頭讀。
    label, client = runtime.snapshot()
    peers = _pool_peers(title, label)
    nb = await client.notebooks.create(title)
    try:
        shared = await _share_each(nb.id, peers, client)
    except (Exception, asyncio.CancelledError) as exc:
        # notebook 已經建出來了,分享失敗(含 CancelledError)必須說清楚這件事
        # —— 否則呼叫端不知道雲端多了一個孤兒 notebook,也無從手動補分享。
        exc.args = (f"notebook {nb.id!r} **已建立**,但{exc}",)
        raise
    return {
        "notebook_id": nb.id,
        "title": getattr(nb, "title", title),
        "shared_with": shared,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def notebook_list() -> dict:
    """List all notebooks."""
    nbs = await runtime.get_client().notebooks.list()
    return {"notebooks": [{"notebook_id": n.id, "title": getattr(n, "title", "")} for n in nbs]}


@mcp.tool()
async def source_add_url(notebook_id: str, url: str, wait: bool = True) -> dict:
    """Add a URL or YouTube link as a source. wait=True(預設)時回傳附帶 best-effort
    落地驗證:char_count(擷取字數;0 = 疑似 paywall/空殼,附 warning)——多數情況
    看回傳即完成對帳,不用再跑 source_list + source_fulltext。"""
    client = runtime.get_client()
    src = await client.sources.add_url(notebook_id, url, wait=wait, wait_timeout=600.0)
    out = {"source_id": src.id}
    if wait:
        out.update(await _probe_extraction(client, notebook_id, src.id, is_file=False))
    return out


@mcp.tool()
async def source_add_text(notebook_id: str, title: str, content: str, wait: bool = True) -> dict:
    """Add plain text as a source."""
    src = await runtime.get_client().sources.add_text(notebook_id, title, content, wait=wait, wait_timeout=600.0)
    return {"source_id": src.id}


@mcp.tool()
async def source_add_file(
    notebook_id: str,
    file_path: str,
    mime_type: str | None = None,
    wait: bool = True,
    title: str | None = None,
) -> dict:
    """Add a local file as a source. mp3 回饋來源用 mime_type="audio/mpeg";
    title 可直接命名(如手動補一集時傳 "EP03 標題",與 Studio artifact 同名)。

    endpoint 不吃的副檔名(`.json`/`.ts`/`.py`/`.yaml`…)若是純文字會自動包成
    `<原檔名>.md` 上傳,回傳帶 `converted_from`——caller 不必自己先改名。傳了
    `mime_type` 就照傳入值原樣送(顯式宣告優先);HTML 維持上游的 fail-loud。
    注意這是 caller-facing 的通用檔案入口;podcast finalize 的已知 mp3 路徑
    直接走 SDK,不經過這裡。"""
    # SDK 會 strip title 後才落地;先在這裡 strip,後檢比較基準才會一致,
    # 否則呼叫端傳前後空白會被誤判成「title 未生效」而 raise(明明成功了)。
    title = title.strip() if title is not None else None
    client = runtime.get_client()
    with tempfile.TemporaryDirectory() as tmpdir:
        # 判定 + 複製最多 _MAX_CONVERT_BYTES 的同步 I/O 丟到 thread:直接跑在事件迴圈上
        # 會卡住整個 MCP server(其他 request、取消、長跑狀態查詢全停,外層 client 可能
        # 先 timeout),而這個 process 是常駐、跨長生成共用的。
        upload_path, converted_from = (
            (file_path, None) if mime_type is not None       # 顯式宣告優先,不猜
            else await asyncio.to_thread(_as_uploadable_text, file_path, tmpdir)
        )
        src = await client.sources.add_file(
            notebook_id,
            upload_path,
            mime_type=mime_type,
            wait=wait,
            wait_timeout=600.0,
            title=title,
        )
    # 0.7.3 的 title= 內部是 add→rename,改名失敗只 log 不 raise(回傳舊 title)。
    # 命名是鐵律的一部分,靜默破功不可接受 → 後檢 fail-loud。
    if title is not None and getattr(src, "title", None) != title:
        raise RuntimeError(
            f"來源已上傳(source_id={src.id})但 title 未生效"
            f"(期望 {title!r},實際 {getattr(src, 'title', None)!r});"
            f"請用 sources.rename 補命名或刪除重傳。"
        )
    # probe 在 title 後檢之後:加值驗證不得吞掉既有 fail-loud 路徑。
    out = {"source_id": src.id}
    if converted_from is not None:
        # 只有真的轉換過才出現;未傳 title 時來源會以 `<原檔名>.md` 落地,對帳看得到。
        out["converted_from"] = converted_from
    if wait:
        out.update(await _probe_extraction(client, notebook_id, src.id, is_file=True))
    return out


@mcp.tool(
    annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True)
)
async def source_delete(notebook_id: str, source_id: str) -> dict:
    """Delete a caller-selected source that is no longer needed.

    這是 generic source 管理能力，不代表可覆寫 manifest-backed completed episode。
    SDK 的 delete 允許刪不存在的 id，但 RPC 只送 source_id、notebook_id 只是 routing
    header；工具會先以 source_list 驗證它屬於指定 notebook，**查無此 id 時不發那個
    destructive RPC**（否則打錯 notebook 會刪到別本的來源），回 was_present=False。
    `deleted` 一律代表「呼叫後該 id 已不在這個 notebook」；要區分「本來就不在」看
    `was_present`。"""
    client = runtime.get_client()
    sources = await client.sources.list(notebook_id)
    if not any(getattr(source, "id", None) == source_id for source in sources):
        # **查無此 id:不打 RPC,也不 raise。**
        # 不打 —— 歸屬無法確認時發 DELETE_SOURCE 就是拿別本筆記本的來源賭一把。
        # 不 raise —— `podcast_attempt_retract` 的清理契約要求呼叫端把回傳的
        # `stale_source_ids`「逐一 source_delete」,而 response 遺失後重放整個迴圈是
        # **預期操作**;對已經刪掉的那一筆拋錯會讓自動化 host 停在半路,剩下的 id
        # 從此沒人刪,而 `_assert_source_cleanup_done` 只列「還在」的 id,不會替它
        # 補回來。fail-loud 保護的是打錯參數,代價卻是打斷冪等的清理迴圈 —— 這裡
        # 兩者都要:安全性質留在「不打 RPC」,冪等留在「不 raise」。
        return {"deleted": source_id, "was_present": False}
    await client.sources.delete(notebook_id, source_id)
    return {"deleted": source_id, "was_present": True}


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
async def generate_audio(
    notebook_id: str,
    instructions: str | None = None,
    language: str | None = None,
    audio_format: str | None = None,
    audio_length: str | None = None,
    source_ids: list[str] | None = None,
) -> dict:
    """Generate an audio overview. Defaults to zh_Hant and returns task_id.

    ``source_ids`` 指名只讀哪幾筆來源(用 source_list 取得真實 id);省略則用筆記本
    全部來源。要排除哪些是呼叫端的政策。

    ⚠️ **帶進生成的來源 >= 10 筆會在打 RPC 之前 raise**,與 ``podcast_episode`` 同一道
    守門(實測 11–15 筆會讓模型拿別的來源內容填空,而 task_id／時長全部正常)。
    這支是低階救援入口,但**失效模式跟高階完全一樣**——守門只掛在 podcast 家族的話,
    這裡就是繞過它的公開後門。

    ⚠️ **這支沒有配額 failover**:它用 ``runtime.get_client()``(此刻作用中的那一個),
    撞到配額就直接 raise,不會換帳號重送。多帳號 pool 裝了幾個帳號都一樣。
    根因是它**連 `manifest_path` 參數都沒有** —— 共用迴圈 (`_failover`) 收到
    ``record_failover=None`` 就一律不換,因為沒地方寫「A 拒絕 → 換 B」的稽核紀錄
    (ADR-0010:對 client 透明可以,對紀錄不行)。
    **v0.9.16 起 `generate_slides` / `generate_report` / `artifact_revise_slide` 都接上了**
    ——它們吃 `manifest_path` + `episode_n`,稽核寫進 episode 的
    ``slides_account`` / ``report_account`` 與 append-only ``attachment_errors``(ADR-0011)。
    這支要跟上得先有地方落帳,那等於把它升成 manifest-backed 能力,不在本工具範圍。
    所以拿這支當 ``acceptance_unknown`` 的備援出路時,配額拒絕要由呼叫端自己處理。"""
    selected = to_source_ids(source_ids)
    client = runtime.get_client()
    await assert_source_count_is_safe(client, notebook_id, selected)
    if selected is not None:
        await assert_sources_exist(client, notebook_id, selected)
    status = await client.artifacts.generate_audio(
        notebook_id,
        source_ids=selected,
        language=resolve_language(language),
        instructions=instructions,
        audio_format=to_audio_format(audio_format),
        audio_length=to_audio_length(audio_length),
    )
    # Fail fast if the SDK reported a failed/refused generation via status
    # (task_id="", is_failed=True) instead of raising. task_id IS the artifact_id.
    task_id = ensure_started(status)
    return {"task_id": task_id, "artifact_id": task_id}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def artifact_list(notebook_id: str, kind: str | None = None) -> dict:
    """List artifacts already in a notebook, so you can see and recover them —
    e.g. an audio episode whose download got interrupted (find its artifact_id
    here, then artifact_download_audio). Pass kind to filter: "audio", "video",
    "report", "quiz", "flashcards", "mind_map", "infographic", "slide_deck",
    "data_table"; omit for everything.

    `source_ids` 是 0.8.1 的觀測欄位，僅原樣回傳供人工查看；尚未在生產資料驗收，
    不得拿它當 gate 或驗證條件。
    """
    from notebooklm.types import ArtifactType

    try:
        artifact_type = ArtifactType(kind) if kind else None
    except ValueError:
        valid = ", ".join(e.value for e in ArtifactType if e.value != "unknown")
        raise ValueError(f"unknown kind {kind!r}; use one of: {valid}")
    arts = await runtime.get_client().artifacts.list(notebook_id, artifact_type=artifact_type)
    return {
        "artifacts": [
            {
                "artifact_id": a.id,
                "title": a.title,
                "kind": getattr(a.kind, "value", str(a.kind)),
                "completed": a.is_completed,
                "status": a.status_str,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "source_ids": list(a.source_ids),
            }
            for a in arts
        ]
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def artifact_wait(notebook_id: str, task_id: str, timeout: float = 1200.0) -> dict:
    """Wait for a generation task to complete."""
    status = await runtime.get_client().artifacts.wait_for_completion(notebook_id, task_id, timeout=timeout)
    # Fail-closed: the SDK returns a FAILED status (not an exception) when generation
    # fails mid-poll; without this a failed wait would be reported as success.
    ensure_completed(status)
    return {"task_id": status.task_id, "artifact_id": status.task_id}


@mcp.tool()
async def artifact_download_audio(
    notebook_id: str,
    output_path: str,
    artifact_id: str | None = None,
) -> dict:
    """Download an audio artifact to output_path."""
    path = await runtime.get_client().artifacts.download_audio(notebook_id, output_path, artifact_id)
    return {"path": path}


@mcp.tool()
async def artifact_rename(notebook_id: str, artifact_id: str, new_title: str) -> dict:
    """Rename an artifact so it stays identifiable in the notebook."""
    client = runtime.get_client()
    if await client.artifacts.get_or_none(notebook_id, artifact_id) is None:
        raise ValueError(
            f"artifact {artifact_id} not in notebook {notebook_id}; "
            "(use artifact_list to select an artifact from that notebook)"
        )
    await client.artifacts.rename(
        notebook_id, artifact_id, new_title, return_object=False
    )
    return {"artifact_id": artifact_id, "title": new_title}


@mcp.tool()
async def artifact_retry_failed(notebook_id: str, artifact_id: str) -> dict:
    """把**失敗的** artifact 原地重跑(UI 的 Retry),artifact_id 不變。

    省配額用:舊路徑是刪掉重生,等於再花一次生成配額;這裡重用同一個 artifact。
    用 `artifact_list` 找出 completed=False 的那筆,retry 後接 `artifact_wait`,
    再用對應的 download 工具。

    注意 SDK 對伺服器端的同步拒絕(rate limit / 配額 / 不可重試的 artifact)是
    **raise**(不像 generate_* 吞成 failed status),所以拒絕會直接冒出來。

    ⚠️ **部分驗證的邊界**:AUDIO 的 retry 這裡**沒有**來源筆數守門(`generate_audio` /
    `podcast_episode` 都有)。RETRY_ARTIFACT 只送 artifact_id,伺服器沿用該 artifact
    原本的來源集合、不重抓筆記本當下全部——**v0.9.3 真實驗收(2026-08-10)實測過一次**:
    對一顆 failed AUDIO,在失敗之後才把 4 篇全新領域的來源加進筆記本(共 12 筆、已超標),
    retry 完的逐字稿對那 4 篇的 16 個獨有指紋 **0/16 命中**;同一批來源用
    `podcast_episode(source_ids=[...])` 生一集當正向對照則 **16/16 命中**(證明探針有效)。
    所以守門仍然不加(加了會廢掉一條救援路)。

    **但這只結案了一半,別讀成「這支工具安全」**:n=1,而且只測了「失敗**之後新增**
    來源」。**沒測**失敗之後**刪掉**原來源(原集合的 id 失效時伺服器行為未知)、
    也沒測跨帳號重跑。要重跑一顆來源集合已經**變動過**的 failed AUDIO,走
    `podcast_episode(..., source_ids=[...])` 重生仍然比較保險。"""
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id must be a non-empty string")
    artifact_id = artifact_id.strip()
    client = runtime.get_client()
    # 遠端 mutation 前 preflight。RETRY_ARTIFACT 只靠 artifact_id 定位(notebook_id 是
    # routing header),錯配的 ID 伺服器不會擋——而這支跟 download 類工具不同,它改的是
    # 遠端狀態,不是本機檔案。`get_or_none` 列表後比對 id,同時驗了存在與歸屬。
    # 刻意**不限制 kind**:retry_failed 本來就是跨 artifact 種類的通用能力。
    art = await client.artifacts.get_or_none(notebook_id, artifact_id)
    if art is None:
        raise ValueError(
            f"artifact {artifact_id} 不在 notebook {notebook_id}(用 artifact_list 確認)"
        )
    if not art.is_failed:
        raise ValueError(
            f"artifact {artifact_id} 不是 failed 狀態"
            f"(kind={getattr(art.kind, 'value', art.kind)!r} status={art.status_str!r});"
            "retry 只用於失敗的 artifact,要重生請用對應的 generate_* 工具"
        )
    status = await client.artifacts.retry_failed(notebook_id, artifact_id)
    task_id = ensure_started(status)
    return {"task_id": task_id, "artifact_id": task_id}


# 這些 block 本來就沒有文字,空的不代表內容掉了。
_TEXTLESS_BLOCK_KINDS = frozenset({"HORIZONTAL_RULE"})


def _dropped_blocks(document) -> list[str]:
    """回傳「有 block 但一個字都沒解出來」的 block kind —— 那些內容在輸出裡是**靜默消失**的。

    **v0.9.14 真實驗收 FINDING-E。** 上游文件說 `CODE_BLOCK` / `THOUGHT` 目前不解碼,
    而 `.text` 會用 U+FFFC 填補那些位置 —— 於是 README 與我們都以為輸出裡至少看得到 `￼`。
    **實測不是這樣**:那顆 block 的 `spans` 是空的,`render()` 與 `.text` **兩邊都不產生
    任何佔位符**,整段就是不見了。危險在於**剩下的字讀起來完全通順**:實測回答是
    「以下是一段示範程式碼:」後面**直接接下一段**,呼叫端沒有任何訊號可以發現東西掉了
    (同一個 conversation 用 `strip_citations=False` 追問,程式碼完整回來 —— 判別實驗)。

    所以判準是 span 文字,不是 U+FFFC,也不是 block kind:`CODE_BLOCK` **有** spans 時
    `render()` 照樣輸出它(離線實測 `print(1)` 有出現),那種情況不該擋。
    """
    dropped, considered = [], 0
    for block in getattr(document, "blocks", ()) or ():
        kind = getattr(getattr(block, "kind", None), "name", None) or "UNKNOWN"
        if kind in _TEXTLESS_BLOCK_KINDS:
            continue
        considered += 1
        if not any((getattr(s, "text", "") or "").strip() for s in getattr(block, "spans", ()) or ()):
            dropped.append(kind)
    # **整份**都沒有文字 ≠ 部分丟失:那是「上游根本沒給結構化文件」,既有的
    # `render().strip()` fallback 會退回 `_CITATION_RE` 清 `res.answer`,那條路是對的
    # (`test_chat_ask_strip_citations_falls_back_when_document_is_whitespace_only` 守著)。
    # 這裡要抓的是「有些 block 有字、有些沒有」——那才是靜默少一段。
    if considered and len(dropped) == considered:
        return []
    return dropped


def _assert_no_dropped_blocks(document) -> None:
    """只在產公開文案那條路(`strip_citations=True`)擋 —— 預設路徑行為不變。"""
    dropped = _dropped_blocks(document)
    if not dropped:
        return
    raise RuntimeError(
        f"這個回答有 {len(dropped)} 個 block 上游沒有解出任何文字({', '.join(sorted(set(dropped)))}),"
        "它們在 strip_citations=True 的輸出裡會**靜默消失**且不留佔位符,而剩下的句子讀起來"
        "仍然通順(實測:「以下是一段示範程式碼:」後面直接接下一段)—— 這串字會直接進"
        "公開 RSS 的 <description>,所以這裡擋下來而不是讓你發不出去才發現。"
        "要完整內容就用 strip_citations=False 自己處理標記;"
        "產 show notes 的話改問一個不會引出程式碼/圖片區塊的問題。"
    )


@mcp.tool()
async def chat_ask(
    notebook_id: str,
    question: str,
    source_ids: list[str] | None = None,
    conversation_id: str | None = None,
    strip_citations: bool = False,
    include_references: bool = True,
) -> dict:
    """Ask a source-grounded question.

    Pass source_ids to focus on specific sources (e.g. one episode's article,
    excluding earlier episodes' audio) so show notes don't get polluted; pass
    conversation_id to continue a thread. Returns answer + citation references +
    conversation_id. NOTE: answer carries citation markers like [1]/[3, 4].
    產公開文案(show notes)時傳 strip_citations=True 會優先取 0.8.1 起新增的
    `answer_document.render()`——上游把同一份文件的三種 rendering 分工寫死:
    `.text` 是 offset-faithful layout,為了讓 citation `slice()` 精確,刻意不插入
    任何分隔符(段落會黏在一起),且用 U+FFFC 填補圖片/程式碼區塊等無法解碼的位置
    (文字裡會留下可見的 ￼);`render()` 才是上游文件寫明「唯一為閱讀而造」的
    rendering——join 同一個 block 內的文字、分隔不同 block,同樣不帶 markdown
    標記。render() 為空或全空白時才退回 server 端 `_CITATION_RE` 清標記。
    include_references=False 省掉引用清單——省 token 也免手動 regex;
    預設兩者不動(既有 caller 依標記對照 references 的行為不變)。

    **v0.9.14 真實驗收補了兩道**(只作用在 `strip_citations=True` 這條路):

    - `render()` 只拿掉 **block 級**標記(`###` 標題、`*` 條列),**inline 的
      `**粗體**` 原樣留著**(實測)。這裡再過一次 `strip_inline_emphasis`。
      **底線不清** —— `_斜體_` 與識別碼(`NOTEBOOKLM_AUTH_JSON`、`source_id`)撞得太兇。
    - 上游沒解出文字的 block(`CODE_BLOCK` 等)會**整段靜默消失且不留 U+FFFC**,
      而剩下的句子讀起來仍然通順 —— `_assert_no_dropped_blocks` 在這裡 fail-loud,
      不讓它流進公開 RSS。要完整內容就用 `strip_citations=False`。
    """
    res = await runtime.get_client().chat.ask(
        notebook_id, question, source_ids=source_ids, conversation_id=conversation_id
    )
    answer = res.answer
    if strip_citations:
        _assert_no_dropped_blocks(res.answer_document)
        rendered = res.answer_document.render()
        answer = rendered if rendered.strip() else _CITATION_RE.sub("", answer)
        # render() 只管 block 級標記;inline 的 `**粗體**` 要另外清(FINDING-D)。
        answer = strip_inline_emphasis(answer)
    return {
        "answer": answer,
        "conversation_id": getattr(res, "conversation_id", None),
        "references": [
            {
                "source_id": getattr(r, "source_id", None),
                "citation_number": getattr(r, "citation_number", None),
                "cited_text": getattr(r, "cited_text", None),
            }
            for r in getattr(res, "references", None) or []
        ] if include_references else [],
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def source_list(notebook_id: str) -> dict:
    """List a notebook's sources — find a source_id for explicit source
    management or a focused generate_slides/report source_ids set, and confirm
    uploads landed. Each entry has ready=True once
    NotebookLM finished ingesting it."""
    srcs = await runtime.get_client().sources.list(notebook_id)
    return {
        "sources": [
            {
                "source_id": s.id,
                "title": s.title,
                "kind": getattr(s.kind, "value", str(s.kind)),
                "ready": s.is_ready,
            }
            for s in srcs
        ]
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def source_fulltext(
    notebook_id: str,
    source_id: str,
    max_chars: int | None = None,
    contains: list[str] | None = None,
) -> dict:
    """Get a source's extracted full text — verify a PDF / Medium / pasted article
    actually ingested its body, or read back an uploaded mp3's transcript.

    對帳省 token 姿勢:`max_chars=0, contains=["關鍵詞", …]` → 只回
    {char_count, hits, content:""},不把全文灌進 host context(關鍵詞比對在
    server 端做,已處理 NotebookLM 對 CJK 插空格的問題)。`max_chars` 截斷時回
    truncated=True;char_count 永遠是全文長度。兩參數都不傳 = 照舊回全文。"""
    if max_chars is not None and max_chars < 0:
        raise ValueError("max_chars must be >= 0")
    if contains is not None and any(not _norm(k) for k in contains):
        raise ValueError("contains 的關鍵詞不可為空/純空白(normalize 後永遠命中)")
    ft = await runtime.get_client().sources.get_fulltext(notebook_id, source_id)
    out: dict = {
        "source_id": ft.source_id,
        "title": ft.title,
        "char_count": ft.char_count,
    }
    if contains is not None:
        body = _norm(ft.content)
        out["hits"] = {kw: _norm(kw) in body for kw in contains}
    content = ft.content
    if max_chars is not None and len(content) > max_chars:
        content = content[:max_chars]
        out["truncated"] = True
    out["content"] = content
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def notebook_get(notebook_id: str) -> dict:
    """Get a notebook's metadata (title, source count, owner) — confirm you're
    targeting the right notebook before generating or publishing.

    `is_owner` 由上游 0.8.1(#2125)重新推導:欄位來源從「有沒有共享者」
    (有共享者就恆為 False,即使呼叫的正是 owner 本人)改成真正的 userRole,
    當 `role` 有解出值時才成立 `is_owner == (role is SharePermission.OWNER)`
    (`Notebook.__setattr__` 在設定 `role` 時同步維持這個不變式)。`role is None`
    時代表該筆資料沒有講出等級,`is_owner` 停在樂觀預設 `True`,呼叫端無法分辨
    「真的是 owner」與「role 未知」。回傳的 `role` 欄位就是用來讓呼叫端自己分辨。
    **同一支呼叫在升版前後回傳值會不同**——升版前多帳號 pool 模式下這個
    欄位實務上恆為 False,升版後才反映真實歸屬。上游同時新增了語意更完整的
    `Notebook.role: SharePermission | None`(能分辨 EDITOR/VIEWER,不只是
    「是不是 owner」),之後要更細緻的權限判斷可以改讀回傳的 `role` 欄位。
    """
    try:
        nb = await runtime.get_client().notebooks.get(notebook_id)
    except Exception as exc:
        # 權限被拒翻成帶指引的 NotebookAccessDenied(v0.9.14 FINDING-F):
        # 上游那句講的是 authuser routing、指向 SDK issue #114/#294,對 pool 情境無用。
        raise_if_access_denied(exc, notebook_id)
        raise
    # SDK 0.3.4 的 get() 不一定回 None——找不到可能回帶空 id 的物件,兩種都當「找不到」。
    if nb is None or not getattr(nb, "id", None):
        raise RuntimeError(f"notebook not found: {notebook_id}")
    return {
        "notebook_id": nb.id,
        "title": nb.title,
        "sources_count": nb.sources_count,
        "is_owner": nb.is_owner,
        # `is not None` 而不是 truthiness:`SharePermission` 是 proto 衍生的 int enum,
        # 上游哪天照 proto 慣例補一個 `UNSPECIFIED = 0`,truthiness 會把它回報成 None
        # ——而同一份 payload 的 `is_owner` 會被上游的 `__setattr__` 設成 False
        # (它判的是 `value is not None`),呼叫端就會拿到自相矛盾的 `is_owner=False, role=None`,
        # 而下面那段 docstring 說 role=None 代表「is_owner 停在樂觀預設 True」。
        "role": nb.role.name if nb.role is not None else None,
        "created_at": nb.created_at.isoformat() if nb.created_at else None,
    }
