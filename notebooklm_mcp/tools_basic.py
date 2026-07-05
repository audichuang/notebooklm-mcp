"""Basic NotebookLM tools.

Thin wrappers over the resident client, with zh_Hant default and enum mapping
baked in. Each tool returns a plain JSON-able dict.
"""
from __future__ import annotations

from . import runtime
from ._status import ensure_completed, ensure_started
from .enums import to_audio_format, to_audio_length
from .languages import resolve_language
from .app import mcp


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
    """Add a URL or YouTube link as a source."""
    src = await runtime.get_client().sources.add_url(notebook_id, url, wait=wait, wait_timeout=600.0)
    return {"source_id": src.id}


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
) -> dict:
    """Add a local file as a source, including audio/mp3 feedback sources."""
    src = await runtime.get_client().sources.add_file(
        notebook_id,
        file_path,
        mime_type=mime_type,
        wait=wait,
        wait_timeout=600.0,
    )
    return {"source_id": src.id}


@mcp.tool()
async def source_delete(notebook_id: str, source_id: str) -> dict:
    """Delete a source, e.g. remove a rejected episode before regenerating."""
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
    await runtime.get_client().artifacts.rename(notebook_id, artifact_id, new_title)
    return {"artifact_id": artifact_id, "title": new_title}


@mcp.tool()
async def chat_ask(
    notebook_id: str,
    question: str,
    source_ids: list[str] | None = None,
    conversation_id: str | None = None,
) -> dict:
    """Ask a source-grounded question.

    Pass source_ids to focus on specific sources (e.g. one episode's article,
    excluding earlier episodes' audio) so show notes don't get polluted; pass
    conversation_id to continue a thread. Returns answer + citation references +
    conversation_id. NOTE: answer carries citation markers like [1]/[3, 4]; strip
    with regex `\\[[\\d,\\s\\-–]+\\]` before using as public text.
    """
    res = await runtime.get_client().chat.ask(
        notebook_id, question, source_ids=source_ids, conversation_id=conversation_id
    )
    return {
        "answer": res.answer,
        "conversation_id": getattr(res, "conversation_id", None),
        "references": [
            {
                "source_id": getattr(r, "source_id", None),
                "citation_number": getattr(r, "citation_number", None),
                "cited_text": getattr(r, "cited_text", None),
            }
            for r in getattr(res, "references", None) or []
        ],
    }


@mcp.tool()
async def source_list(notebook_id: str) -> dict:
    """List a notebook's sources — find a source_id (e.g. to rename or delete a
    rejected episode's mp3 source, or feed generate_slides/report a focused
    source_ids set) and confirm uploads landed. Each entry has ready=True once
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
async def source_fulltext(notebook_id: str, source_id: str) -> dict:
    """Get a source's extracted full text — verify a PDF / Medium / pasted article
    actually ingested its body, or read back an uploaded mp3's transcript. NOTE:
    NotebookLM inserts spaces between CJK chars; `"".join(text.split())` before
    keyword matching."""
    ft = await runtime.get_client().sources.get_fulltext(notebook_id, source_id)
    return {
        "source_id": ft.source_id,
        "title": ft.title,
        "char_count": ft.char_count,
        "content": ft.content,
    }


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
