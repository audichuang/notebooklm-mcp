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
from ._sources import assert_sources_exist, to_source_ids
from ._status import ensure_completed, ensure_started
from ._text import _CITATION_RE, norm as _norm
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


async def _probe_extraction(notebook_id: str, source_id: str, *, is_file: bool) -> dict:
    """加來源後的 best-effort 落地驗證:只回 char_count(+空殼 warning),不回全文。

    probe 失敗不連坐 add(來源已成功上傳),回 char_count=None + note。
    措辭分流:URL 空殼多半是 paywall/動態頁;檔案空殼可能只是音檔/掃描 PDF,不能亂指控。"""
    try:
        ft = await runtime.get_client().sources.get_fulltext(notebook_id, source_id)
        n = ft.char_count
    except Exception as exc:  # noqa: BLE001 — probe 是加值檢查,任何失敗都不該讓 add 白做
        return {"char_count": None,
                "note": f"extraction probe failed (best-effort, source 已上傳): {exc}"}
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


def _pool_peers(notebook_id: str) -> list[str]:
    """pool 裡除了作用中之外的帳號 email。

    label 退回 "#N" 表示啟動時拿不到那個帳號的 email,分享不了。靜默略過會讓它
    永遠沒有權限,而症狀要等 failover 換過去才出現——中間隔著整段生成時間。
    """
    others = [a for a in runtime.all_accounts() if a != runtime.active_account()]
    unresolved = [a for a in others if "@" not in a]
    if unresolved:
        raise RuntimeError(
            f"notebook {notebook_id!r}:pool 裡的 {unresolved} 沒有可用的帳號 email,"
            "無法分享 —— 這些槽位在 server 啟動時取不到 email。請手動分享或重啟 server。"
        )
    return others


async def _share_each(notebook_id: str, emails: list[str]) -> list[str]:
    """逐一分享,失敗時說出已經完成到哪裡。"""
    client = runtime.get_client()
    shared: list[str] = []
    for email in emails:
        try:
            await client.sharing.add_user(
                notebook_id, email, SharePermission.EDITOR, notify=False
            )
        except Exception as exc:
            raise RuntimeError(
                f"notebook {notebook_id!r} 分享給 {email} 失敗({exc})。已分享:{shared}。"
                "手動補分享(EDITOR)——沒分享到的帳號在 failover 換過去時會 permission denied。"
            ) from exc
        shared.append(email)
    return shared


@mcp.tool()
async def notebook_share_with_pool(notebook_id: str) -> dict:
    """把**既有** notebook 分享給多帳號 pool 裡的其餘帳號(EDITOR)。

    `notebook_create` 從 v0.8.1 起會自動做這件事;這支是給**既有** notebook 補的
    ——v0.8.1 之前建的、或在 NotebookLM 網頁上手動建的。沒有這個前置狀態,配額耗盡
    後 failover 換帳號時會 `NotebookAccessDenied`。

    **冪等**:已經有權限的帳號直接跳過,重跑安全。單帳號模式是 no-op(不打任何 RPC)。
    """
    peers = _pool_peers(notebook_id)
    if not peers:
        return {"notebook_id": notebook_id, "shared_with": [], "already_shared": []}
    status = await runtime.get_client().sharing.get_status(notebook_id)
    existing = {getattr(u, "email", None) for u in getattr(status, "shared_users", [])}
    already = [e for e in peers if e in existing]
    todo = [e for e in peers if e not in existing]
    return {
        "notebook_id": notebook_id,
        "shared_with": await _share_each(notebook_id, todo),
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
    nb = await runtime.get_client().notebooks.create(title)
    # notebook 已經建出來了,分享失敗必須說清楚這件事 —— 否則呼叫端不知道雲端多了
    # 一個孤兒 notebook,也無從手動補分享。
    try:
        shared = await _share_each(nb.id, _pool_peers(nb.id))
    except RuntimeError as exc:
        raise RuntimeError(f"notebook {nb.id!r} **已建立**,但{exc}") from exc
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
    src = await runtime.get_client().sources.add_url(notebook_id, url, wait=wait, wait_timeout=600.0)
    out = {"source_id": src.id}
    if wait:
        out.update(await _probe_extraction(notebook_id, src.id, is_file=False))
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
    with tempfile.TemporaryDirectory() as tmpdir:
        # 判定 + 複製最多 _MAX_CONVERT_BYTES 的同步 I/O 丟到 thread:直接跑在事件迴圈上
        # 會卡住整個 MCP server(其他 request、取消、長跑狀態查詢全停,外層 client 可能
        # 先 timeout),而這個 process 是常駐、跨長生成共用的。
        upload_path, converted_from = (
            (file_path, None) if mime_type is not None       # 顯式宣告優先,不猜
            else await asyncio.to_thread(_as_uploadable_text, file_path, tmpdir)
        )
        src = await runtime.get_client().sources.add_file(
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
        out.update(await _probe_extraction(notebook_id, src.id, is_file=True))
    return out


@mcp.tool(
    annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True)
)
async def source_delete(notebook_id: str, source_id: str) -> dict:
    """Delete a caller-selected source that is no longer needed.

    這是 generic source 管理能力，不代表可覆寫 manifest-backed completed episode。
    注意:0.7.x 起 SDK 的 delete 是 idempotent —— 刪不存在的 source 也「成功」不 raise。
    因此 deleted 表示「呼叫後該 id 已不在筆記本」,不保證它先前存在(打錯 id 也回 deleted)。
    需要確認確實刪掉某個既有來源時,先用 source_list 取得真實 source_id。"""
    await runtime.get_client().sources.delete(notebook_id, source_id)
    return {"deleted": source_id}


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
    全部來源。要排除哪些是呼叫端的政策。"""
    selected = to_source_ids(source_ids)
    client = runtime.get_client()
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
    await runtime.get_client().artifacts.rename(
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
    **raise**(不像 generate_* 吞成 failed status),所以拒絕會直接冒出來。"""
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
    產公開文案(show notes)時傳 strip_citations=True 由 server 清標記、
    include_references=False 省掉引用清單——省 token 也免手動 regex;
    預設兩者不動(既有 caller 依標記對照 references 的行為不變)。
    """
    res = await runtime.get_client().chat.ask(
        notebook_id, question, source_ids=source_ids, conversation_id=conversation_id
    )
    answer = res.answer
    if strip_citations:
        answer = _CITATION_RE.sub("", answer)
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

    ⚠️ `is_owner` 在 notebook **有任何共享者時一律回 False**,即使呼叫的就是 owner
    本人(v0.8.1 驗收 G-1:同一份 owner 憑證,移除共享者之後同一個欄位才變 True)。
    多帳號 pool 模式下自動分享是常態,所以這個欄位實務上恆為 False,**不能拿來判斷
    歸屬**。行為來自上游 SDK 對 share status 的解讀,不是這一版引入的;我們照實轉發。
    """
    nb = await runtime.get_client().notebooks.get(notebook_id)
    # SDK 0.3.4 的 get() 不一定回 None——找不到可能回帶空 id 的物件,兩種都當「找不到」。
    if nb is None or not getattr(nb, "id", None):
        raise RuntimeError(f"notebook not found: {notebook_id}")
    return {
        "notebook_id": nb.id,
        "title": nb.title,
        "sources_count": nb.sources_count,
        "is_owner": nb.is_owner,
        "created_at": nb.created_at.isoformat() if nb.created_at else None,
    }
