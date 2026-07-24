"""Basic NotebookLM tools.

Thin wrappers over the resident client, with zh_Hant default and enum mapping
baked in. Each tool returns a plain JSON-able dict.
"""
from __future__ import annotations

from . import runtime
from ._status import ensure_completed, ensure_started
from ._text import _CITATION_RE, norm as _norm
from .auth_probe import probe_auth
from .enums import to_audio_format, to_audio_length
from .languages import resolve_language
from .app import mcp


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


@mcp.tool()
async def auth_check() -> dict:
    """輕量真 RPC 驗證 NotebookLM 認證(cookie)是否有效。

    長流程(整季生成、發布)前先跑,cookie 死了會秒退並回重登指引,
    避免燒掉數小時等待。回傳 {"ok": True, "notebooks": N}。
    """
    return await probe_auth(runtime.get_client())


@mcp.tool()
async def notebook_create(title: str) -> dict:
    """Create a new notebook. Returns its id."""
    nb = await runtime.get_client().notebooks.create(title)
    return {"notebook_id": nb.id, "title": getattr(nb, "title", title)}


@mcp.tool()
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
    title 可直接命名(如手動補一集時傳 "EP03 標題",與 Studio artifact 同名)。"""
    # SDK 會 strip title 後才落地;先在這裡 strip,後檢比較基準才會一致,
    # 否則呼叫端傳前後空白會被誤判成「title 未生效」而 raise(明明成功了)。
    title = title.strip() if title is not None else None
    src = await runtime.get_client().sources.add_file(
        notebook_id,
        file_path,
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
    if wait:
        out.update(await _probe_extraction(notebook_id, src.id, is_file=True))
    return out


@mcp.tool()
async def source_delete(notebook_id: str, source_id: str) -> dict:
    """Delete a caller-selected source that is no longer needed.

    這是 generic source 管理能力，不代表可覆寫 manifest-backed completed episode。
    注意:0.7.x 起 SDK 的 delete 是 idempotent —— 刪不存在的 source 也「成功」不 raise。
    因此 deleted 表示「呼叫後該 id 已不在筆記本」,不保證它先前存在(打錯 id 也回 deleted)。
    需要確認確實刪掉某個既有來源時,先用 source_list 取得真實 source_id。"""
    await runtime.get_client().sources.delete(notebook_id, source_id)
    return {"deleted": source_id}


@mcp.tool()
async def generate_audio(
    notebook_id: str,
    instructions: str | None = None,
    language: str | None = None,
    audio_format: str | None = None,
    audio_length: str | None = None,
) -> dict:
    """Generate an audio overview. Defaults to zh_Hant and returns task_id."""
    status = await runtime.get_client().artifacts.generate_audio(
        notebook_id,
        language=resolve_language(language),
        instructions=instructions,
        audio_format=to_audio_format(audio_format),
        audio_length=to_audio_length(audio_length),
    )
    # Fail fast if the SDK reported a failed/refused generation via status
    # (task_id="", is_failed=True) instead of raising. task_id IS the artifact_id.
    task_id = ensure_started(status)
    return {"task_id": task_id, "artifact_id": task_id}


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def notebook_get(notebook_id: str) -> dict:
    """Get a notebook's metadata (title, source count, owner) — confirm you're
    targeting the right notebook before generating or publishing."""
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
