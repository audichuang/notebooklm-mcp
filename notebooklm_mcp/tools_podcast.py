"""Composite podcast tools implementing sequential feedback.

Continuity comes from NotebookLM transcribing the prior episode mp3 that we
re-upload as a source. The generation loop is deterministic Python code.
"""
from __future__ import annotations

import json
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

    # Re-upload the prior episode's mp3 as a source AND rename that source to a
    # clear, unified label, so the Sources area mirrors the Studio artifact names
    # ("EP01" artifact <-> "EP01 對話紀錄" source). 0.3.4's add_file has no title
    # param, so the explicit sources.rename is the only way to label it.
    if prior_mp3_path:
        prior_src = await client.sources.add_file(
            notebook_id,
            prior_mp3_path,
            mime_type="audio/mpeg",
            wait=True,
            wait_timeout=600.0,
        )
        await client.sources.rename(
            notebook_id, prior_src.id, f"EP{episode_n - 1:02d} 對話紀錄"
        )

    status = await client.artifacts.generate_audio(
        notebook_id,
        language=resolve_language(language),
        instructions=brief,
        audio_format=to_audio_format(audio_format),
        audio_length=to_audio_length(audio_length),
    )

    # task_id IS the artifact_id — notebooklm-py _types/artifacts.py:421 states
    # "task_id and artifact_id are the same identifier"; GenerationStatus has NO
    # artifact_id field, so we must use task_id for the download/rename targeting
    # (otherwise download falls back to "latest" and rename targets None).
    artifact_id = status.task_id
    await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)

    # Rename the Studio artifact BEFORE downloading: name it in NotebookLM first so
    # the notebook stays legible regardless of the download outcome, then pull the mp3.
    await client.artifacts.rename(notebook_id, artifact_id, f"EP{episode_n:02d}")

    mp3_path = os.path.join(output_dir, f"ep{episode_n:02d}.mp3")
    await client.artifacts.download_audio(notebook_id, mp3_path, artifact_id)

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


@mcp.tool()
async def podcast_series(
    notebook_id: str,
    episodes: list[dict],
    output_dir: str,
    start: int = 1,
    language: str | None = None,
    audio_format: str | None = "deep-dive",
    audio_length: str | None = "long",
    wait_timeout: float = 1200.0,
) -> dict:
    """Generate a full podcast series deterministically."""
    os.makedirs(output_dir, exist_ok=True)
    manifest_path = os.path.join(output_dir, "series_manifest.json")
    results: list[dict] = []

    prior_mp3 = None
    if start > 1:
        candidate = os.path.join(output_dir, f"ep{start - 1:02d}.mp3")
        prior_mp3 = candidate if os.path.exists(candidate) else None

    for episode_n in range(start, len(episodes) + 1):
        brief = episodes[episode_n - 1]["brief"]
        res = await _run_episode(
            notebook_id,
            episode_n,
            brief,
            output_dir,
            prior_mp3,
            language,
            audio_format,
            audio_length,
            wait_timeout,
        )
        results.append(res)
        prior_mp3 = res["mp3_path"]
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"notebook_id": notebook_id, "episodes": results}, f, ensure_ascii=False, indent=2)

    return {"notebook_id": notebook_id, "episodes": results, "manifest": manifest_path}
