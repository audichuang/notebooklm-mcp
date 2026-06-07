"""Basic NotebookLM tools.

Thin wrappers over the resident client, with zh_Hant default and enum mapping
baked in. Each tool returns a plain JSON-able dict.
"""
from __future__ import annotations

from . import runtime
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
    # task_id IS the artifact_id (notebooklm-py _types/artifacts.py:421).
    return {"task_id": status.task_id, "artifact_id": status.task_id}


@mcp.tool()
async def artifact_wait(notebook_id: str, task_id: str, timeout: float = 1200.0) -> dict:
    """Wait for a generation task to complete."""
    status = await runtime.get_client().artifacts.wait_for_completion(notebook_id, task_id, timeout=timeout)
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
async def chat_ask(notebook_id: str, question: str) -> dict:
    """Ask a source-grounded question."""
    res = await runtime.get_client().chat.ask(notebook_id, question)
    return {"answer": res.answer}
