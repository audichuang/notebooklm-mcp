"""Composite podcast tools implementing sequential feedback.

Continuity comes from NotebookLM transcribing the prior episode mp3 that we
re-upload as a source. The generation loop is deterministic Python code.
"""
from __future__ import annotations

import json
import os

from . import runtime
from ._status import ensure_completed, ensure_started
from .enums import to_audio_format, to_audio_length
from .languages import resolve_language
from .app import mcp


def _episode_label(episode_n: int, title: str) -> str:
    """Unified name for BOTH the Studio artifact and the self-uploaded source:
    ``EP{n:02d} {title}`` (e.g. ``EP01 心法篇``). The EP prefix keeps ordering /
    resume / reject-then-delete addressable; the title makes it human-legible.
    Both sides use this identical string (the unified-naming iron rule)."""
    return f"EP{episode_n:02d} {title.strip()}"


def _load_prior_manifest_episodes(manifest_path: str, notebook_id: str, start: int) -> list[dict]:
    """On resume (start>1), load episodes < start from the existing season manifest
    so resuming does not wipe earlier episodes from the record."""
    if start == 1 or not os.path.exists(manifest_path):
        return []
    # Validate before trusting it: a corrupt/truncated manifest must surface a
    # clear error (not a raw JSONDecodeError leaking the parse position) so the
    # caller knows resume can't safely preserve the earlier episodes.
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Existing series_manifest.json is corrupt and cannot be read for resume: {exc}"
        ) from None
    if not isinstance(data, dict):
        raise ValueError("Existing series_manifest.json is corrupt: top level is not an object")
    if data.get("notebook_id") not in (None, notebook_id):
        raise ValueError("Existing series_manifest.json belongs to a different notebook_id")
    return [
        ep for ep in data.get("episodes", [])
        if isinstance(ep, dict) and isinstance(ep.get("episode"), int) and ep["episode"] < start
    ]


def _write_manifest(manifest_path: str, notebook_id: str, episodes: list[dict]) -> None:
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"notebook_id": notebook_id, "episodes": episodes}, f, ensure_ascii=False, indent=2)


async def _run_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
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

    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"episode {episode_n} requires a non-empty 'title'")
    label = _episode_label(episode_n, title)

    if prior_mp3_path and episode_n <= 1:
        raise ValueError("prior_mp3_path requires episode_n >= 2 (there is no prior to episode 1)")

    # Standalone continuity: if the caller hands us a prior episode's mp3 that is
    # NOT yet in the notebook (one-off podcast_episode use), upload + name it so
    # this episode can recap it. In a full podcast_series this is unnecessary —
    # each episode self-uploads at the end (below), so the prior is already there.
    if prior_mp3_path:
        prior_src = await client.sources.add_file(
            notebook_id,
            prior_mp3_path,
            mime_type="audio/mpeg",
            wait=True,
            wait_timeout=600.0,
        )
        await client.sources.rename(notebook_id, prior_src.id, f"EP{episode_n - 1:02d}")

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
    # ensure_started guards the failed/empty-task_id case (rate limit / quota / refusal).
    artifact_id = ensure_started(status)
    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    # Rename the Studio artifact BEFORE downloading: name it in NotebookLM first so
    # the notebook stays legible regardless of the download outcome, then pull the mp3.
    await client.artifacts.rename(notebook_id, artifact_id, label)

    mp3_path = os.path.join(output_dir, f"ep{episode_n:02d}.mp3")
    await client.artifacts.download_audio(notebook_id, mp3_path, artifact_id)

    # Re-upload THIS episode's own mp3 as a source named IDENTICALLY to its Studio
    # artifact ("EP02" artifact <-> "EP02" source — same string, no suffix). This is
    # the heart of the sequential-feedback method AND keeps a complete record:
    #  - EVERY episode (including the last) ends up in Sources, name-matched to Studio.
    #  - the NEXT episode's generation automatically sees this source for continuity,
    #    so podcast_series needs no separate prior-upload step.
    own_src = await client.sources.add_file(
        notebook_id, mp3_path, mime_type="audio/mpeg", wait=True, wait_timeout=600.0
    )
    await client.sources.rename(notebook_id, own_src.id, label)

    return {
        "episode": episode_n,
        "title": title.strip(),
        "label": label,
        "task_id": status.task_id,
        "artifact_id": artifact_id,
        "mp3_path": mp3_path,
    }


@mcp.tool()
async def podcast_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str,
    output_dir: str,
    prior_mp3_path: str | None = None,
    language: str | None = None,
    audio_format: str | None = "deep-dive",
    audio_length: str | None = "long",
    wait_timeout: float = 1200.0,
) -> dict:
    """Generate one podcast episode end-to-end.

    The Studio artifact and self-uploaded source are both named ``EP{n:02d} {title}``
    (e.g. ``EP02 實戰篇``), so pass the outline's episode title.
    """
    return await _run_episode(
        notebook_id,
        episode_n,
        title,
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
    # Validate shape up front so a malformed episodes list fails with a clear
    # message instead of a raw KeyError/TypeError mid-loop (after burning a
    # generation). Each episode must be a dict carrying a 'brief' AND a non-empty
    # 'title' (the title names the Studio artifact + source as "EP{n:02d} {title}").
    for i, ep in enumerate(episodes, start=1):
        if not isinstance(ep, dict) or "brief" not in ep:
            raise ValueError(
                f"episode {i} must be a dict with a 'brief' key, got: {ep!r}"
            )
        title = ep.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(
                f"episode {i} must have a non-empty 'title' (got: {title!r})"
            )

    # Validate the resume cursor so start=0 (would index episodes[-1]) or an
    # out-of-range start fail clearly instead of silently producing wrong output.
    if start < 1:
        raise ValueError("start must be >= 1")
    if start > len(episodes):
        raise ValueError(f"start must be <= len(episodes) ({len(episodes)})")

    os.makedirs(output_dir, exist_ok=True)
    manifest_path = os.path.join(output_dir, "series_manifest.json")

    run_results: list[dict] = []  # episodes generated THIS call (the return value)
    # Season manifest preserves earlier episodes across a resume.
    manifest_results = _load_prior_manifest_episodes(manifest_path, notebook_id, start)

    # No prior-mp3 threading: each episode self-uploads its mp3 as a named source
    # at the end of _run_episode, so the next episode (and a `start`-based resume
    # on the same notebook) automatically sees prior episodes already present.
    for episode_n in range(start, len(episodes) + 1):
        ep = episodes[episode_n - 1]
        res = await _run_episode(
            notebook_id,
            episode_n,
            ep["title"],
            ep["brief"],
            output_dir,
            None,
            language,
            audio_format,
            audio_length,
            wait_timeout,
        )
        run_results.append(res)
        manifest_results.append(res)
        _write_manifest(manifest_path, notebook_id, manifest_results)

    return {"notebook_id": notebook_id, "episodes": run_results, "manifest": manifest_path}
