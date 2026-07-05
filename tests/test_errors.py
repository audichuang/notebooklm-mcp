"""Coverage for error / edge paths: bad language, generation timeout, malformed input."""
import json
import os

import pytest

from notebooklm_mcp import tools_basic as t
from notebooklm_mcp import tools_podcast as p


async def test_bad_language_raises_before_any_sdk_call(fake_client):
    with pytest.raises(ValueError) as exc:
        await t.generate_audio("nb-1", language="zh-TW")
    msg = str(exc.value)
    assert "zh_Hant" in msg or "underscore" in msg.lower()
    # The bad code is rejected up front — no generation was triggered.
    assert not any(c[0] == "generate_audio" for c in fake_client.artifacts.calls)


async def test_series_generation_timeout_surfaces_with_partial_manifest(fake_client, tmp_path):
    # Episode 2's wait_for_completion raises (episode 1's wait is call #1, episode 2's is #2).
    fake_client.artifacts.fail_wait_on = 2
    eps = [
        {"title": "心法篇", "brief": "1"},
        {"title": "實戰篇", "brief": "2"},
        {"title": "收尾篇", "brief": "3"},
    ]

    with pytest.raises(TimeoutError):
        await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=1)

    # Manifest holds ONLY the completed episode 1 (no half-written 2/3).
    manifest = json.load(open(os.path.join(str(tmp_path), "series_manifest.json"), encoding="utf-8"))
    assert [e["episode"] for e in manifest["episodes"]] == [1]
    # Episode 1 fully recorded; episode 2 failed at wait (before rename/download/
    # self-upload), so it left NO orphaned source.
    assert fake_client.sources.titles() == ["EP01 心法篇"]


async def test_malformed_episodes_fail_fast_with_clear_error(fake_client, tmp_path):
    with pytest.raises(ValueError) as exc:
        await p.podcast_series("nb-1", episodes=["just a string"], output_dir=str(tmp_path))
    assert "brief" in str(exc.value)
    # Failed validation before any generation.
    assert not any(c[0] == "generate_audio" for c in fake_client.artifacts.calls)


async def test_failed_generation_status_fails_fast(fake_client, tmp_path):
    # SDK reports a failed/refused generation as a status (task_id="", is_failed=True),
    # NOT by raising. The wrapper must detect that instead of proceeding with an empty id.
    fake_client.artifacts.fail_generate = True
    with pytest.raises(RuntimeError, match="Generation failed"):
        await p.podcast_episode("nb-1", episode_n=1, title="開場篇", brief="x", output_dir=str(tmp_path))
    # It stopped right after generate — no wait/download on the empty id.
    kinds = [c[0] for c in fake_client.artifacts.calls]
    assert kinds == ["generate_audio"]


async def test_generate_audio_tool_fails_fast_on_failed_status(fake_client):
    fake_client.artifacts.fail_generate = True
    with pytest.raises(RuntimeError, match="Generation failed"):
        await t.generate_audio("nb-1", instructions="x")


async def test_failure_during_wait_fails_fast(fake_client, tmp_path):
    # Generation can START fine (valid task_id) but FAIL mid-poll. The real 0.3.4
    # wait_for_completion RETURNS that failed status (it only raises on timeout),
    # so ensure_completed must catch is_failed before we rename/download a dead
    # artifact. Without that guard the run would proceed on a failed generation.
    fake_client.artifacts.fail_complete = True
    with pytest.raises(RuntimeError, match="failed while waiting"):
        await p.podcast_episode("nb-1", episode_n=1, title="開場篇", brief="x", output_dir=str(tmp_path))
    # Stopped right after the wait — never reached rename/download/self-upload,
    # so the failed episode left NO orphaned source or artifact rename.
    assert [c[0] for c in fake_client.artifacts.calls] == ["generate_audio", "wait"]
    assert fake_client.sources.titles() == []
