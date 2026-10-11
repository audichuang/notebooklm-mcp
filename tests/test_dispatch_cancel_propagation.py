"""dispatch 期間的 CancelledError 必須端到端往外傳,不得被吞成 partial／結構化回傳。

CancelledError 是 BaseException:`_dispatch_prepared_attempt` 把它收進 `DispatchStopped`,
各呼叫端必須再 re-raise。行為對照重構前(7821672):podcast_episode 把它帶上 reconcile
指引後原樣重拋、manifest 記 acceptance_unknown;podcast_series 兩條路徑都不得回 partial。
"""

import asyncio
import json

import pytest

from notebooklm_mcp import tools_podcast as p

EPS = [{"title": "心法篇", "brief": "1"}]


def _dispatch_status(tmp_path) -> str:
    stored = json.loads((tmp_path / "series_manifest.json").read_text(encoding="utf-8"))
    return stored["episodes"][0]["attempts"][0]["dispatch"]["status"]


async def test_podcast_episode_reraises_cancel_with_reconcile_guidance(fake_client, tmp_path):
    fake_client.artifacts.generate_audio_exc = asyncio.CancelledError("client timeout")

    with pytest.raises(asyncio.CancelledError) as excinfo:
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="1",
            output_dir=str(tmp_path),
            manifest_path=str(tmp_path / "series_manifest.json"),
        )

    assert "podcast_episode_reconcile" in str(excinfo.value)
    assert _dispatch_status(tmp_path) == "acceptance_unknown"


async def test_podcast_series_fresh_episode_reraises_cancel(fake_client, tmp_path):
    fake_client.artifacts.generate_audio_exc = asyncio.CancelledError("client timeout")

    with pytest.raises(asyncio.CancelledError):
        await p.podcast_series("nb-1", episodes=EPS, output_dir=str(tmp_path))

    assert _dispatch_status(tmp_path) == "acceptance_unknown"


async def test_podcast_series_prepared_resend_reraises_cancel(fake_client, tmp_path, monkeypatch):
    original_claim = p._claim_prepared_dispatch
    crashed = False

    def crash_once(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise ConnectionError("crash before dispatch")
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(p, "_claim_prepared_dispatch", crash_once)
    first = await p.podcast_series("nb-1", episodes=EPS, output_dir=str(tmp_path))
    assert first["observed_state"] == "prepared"  # 留下一顆 prepared attempt 走重送路徑

    fake_client.artifacts.generate_audio_exc = asyncio.CancelledError("client timeout")
    with pytest.raises(asyncio.CancelledError):
        await p.podcast_series("nb-1", episodes=EPS, output_dir=str(tmp_path))

    assert _dispatch_status(tmp_path) == "acceptance_unknown"
