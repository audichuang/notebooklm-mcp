"""Composite podcast tools implementing sequential feedback.

Continuity comes from NotebookLM transcribing the prior episode mp3 that we
re-upload as a source. The generation loop is deterministic Python code.
"""
from __future__ import annotations

import os

from . import runtime
from .enums import to_audio_format, to_audio_length
from .languages import resolve_language
from .server import mcp


async def _run_episode(
    notebook_id: str,
    episode_n: int,
    brief: str,
    output_dir: str,
    prior_mp3_path: str | None,
    language: str | None,
    audio_format: str | None,
    audio_length: str | None,
    wait_timeout: float,
) -> dict:
    client = runtime.get_client()
    os.makedirs(output_dir, exist_ok=True)

    if prior_mp3_path:
        await client.sources.add_file(
            notebook_id,
            prior_mp3_path,
            mime_type="audio/mpeg",
            wait=True,
            wait_timeout=600.0,
        )

    status = await client.artifacts.generate_audio(
        notebook_id,
        language=resolve_language(language),
        instructions=brief,
        audio_format=to_audio_format(audio_format),
        audio_length=to_audio_length(audio_length),
    )

    completed = await client.artifacts.wait_for_completion(notebook_id, status.task_id, timeout=wait_timeout)
    artifact_id = getattr(completed, "artifact_id", None) or getattr(status, "artifact_id", None)

    mp3_path = os.path.join(output_dir, f"ep{episode_n:02d}.mp3")
    await client.artifacts.download_audio(notebook_id, mp3_path, artifact_id)
    await client.artifacts.rename(notebook_id, artifact_id, f"EP{episode_n:02d}")

    return {
        "episode": episode_n,
        "task_id": status.task_id,
        "artifact_id": artifact_id,
        "mp3_path": mp3_path,
    }


@mcp.tool()
async def podcast_episode(
    notebook_id: str,
    episode_n: int,
    brief: str,
    output_dir: str,
    prior_mp3_path: str | None = None,
    language: str | None = None,
    audio_format: str | None = "deep-dive",
    audio_length: str | None = "long",
    wait_timeout: float = 1200.0,
) -> dict:
    """Generate one podcast episode end-to-end."""
    return await _run_episode(
        notebook_id,
        episode_n,
        brief,
        output_dir,
        prior_mp3_path,
        language,
        audio_format,
        audio_length,
        wait_timeout,
    )
