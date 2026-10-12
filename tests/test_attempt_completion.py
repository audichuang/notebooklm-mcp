"""`_complete_attempt`:finalize + promote 的單一入口,以及它對失敗的分類。

四個呼叫點(podcast_episode / resume / series 的 output 複驗與 active 收尾)各自決定
要 raise 還是回 partial;這裡只鎖「拿到的是哪一種結果」,讓分類只有一份。
"""

import asyncio

import pytest

from notebooklm_mcp import tools_podcast as p
from notebooklm_mcp.audio_finalize import feedback_upload
from notebooklm_mcp.manifest_store import ManifestStore


async def _completed_episode(fake_client, tmp_path):
    manifest_path = tmp_path / "series_manifest.json"
    first = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    return ManifestStore(str(manifest_path)), first


async def _complete(fake_client, store, attempt_id, tmp_path):
    return await p._complete_attempt(
        fake_client,
        store,
        episode_n=1,
        attempt_id=attempt_id,
        output_dir=str(tmp_path),
        wait_timeout=30.0,
    )


async def test_completed_attempt_is_done_with_its_output(fake_client, tmp_path):
    store, first = await _completed_episode(fake_client, tmp_path)

    outcome = await _complete(fake_client, store, first["attempt_id"], tmp_path)

    assert isinstance(outcome, p.CompletionDone)
    assert outcome.output["artifact_id"] == first["artifact_id"]
    assert outcome.output["published_at"] == first["published_at"]


async def test_missing_remote_artifact_is_unverifiable(fake_client, tmp_path):
    store, first = await _completed_episode(fake_client, tmp_path)
    fake_client.artifacts.artifacts.clear()

    outcome = await _complete(fake_client, store, first["attempt_id"], tmp_path)

    assert isinstance(outcome, p.CompletionStopped)
    assert outcome.kind == "unverifiable"
    assert isinstance(outcome.exc, p.RemoteArtifactUnverifiableError)


async def test_vanished_feedback_source_is_runtime_and_exposes_its_upload(fake_client, tmp_path):
    store, first = await _completed_episode(fake_client, tmp_path)
    fake_client.sources.sources.clear()

    outcome = await _complete(fake_client, store, first["attempt_id"], tmp_path)

    assert isinstance(outcome, p.CompletionStopped)
    assert outcome.kind == "runtime"
    _, attempt = outcome.record()
    assert isinstance(feedback_upload(attempt)["source_id"], str)


async def test_transport_failure_is_transient(fake_client, tmp_path, monkeypatch):
    store, first = await _completed_episode(fake_client, tmp_path)

    async def offline(*_args, **_kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(fake_client.artifacts, "list", offline)

    outcome = await _complete(fake_client, store, first["attempt_id"], tmp_path)

    assert isinstance(outcome, p.CompletionStopped)
    assert outcome.kind == "transient"


async def test_cancellation_is_returned_unmodified(fake_client, tmp_path, monkeypatch):
    store, first = await _completed_episode(fake_client, tmp_path)
    cancelled = asyncio.CancelledError()

    async def cancel(*_args, **_kwargs):
        raise cancelled

    monkeypatch.setattr(fake_client.artifacts, "list", cancel)

    outcome = await _complete(fake_client, store, first["attempt_id"], tmp_path)

    assert isinstance(outcome, p.CompletionStopped)
    assert outcome.kind == "other"
    assert outcome.exc is cancelled


async def test_record_is_read_lazily_and_keeps_tombstone_default_deny(fake_client, tmp_path):
    store, first = await _completed_episode(fake_client, tmp_path)
    fake_client.sources.sources.clear()
    outcome = await _complete(fake_client, store, first["attempt_id"], tmp_path)
    assert isinstance(outcome, p.CompletionStopped)

    await p.podcast_attempt_retract(str(store.path), 1, first["attempt_id"], reason="QA 拒收")

    with pytest.raises(ValueError, match="was retracted"):
        outcome.record()
    _, attempt = outcome.record(allow_retracted=True)
    assert attempt["retraction"]


async def test_non_refusal_promote_failure_is_its_own_kind(fake_client, tmp_path, monkeypatch):
    store, first = await _completed_episode(fake_client, tmp_path)
    boom = RuntimeError("manifest write failed")

    def fail(*_args, **_kwargs):
        raise boom

    monkeypatch.setattr(p, "_promote_attempt_output", fail)

    outcome = await _complete(fake_client, store, first["attempt_id"], tmp_path)

    # 不是 `runtime`:series 不得把 promote 階段的 RuntimeError 當成回錄 source 問題處置。
    assert isinstance(outcome, p.CompletionStopped)
    assert outcome.kind == "promote"
    assert outcome.exc is boom


async def test_promotion_refusal_is_refused(fake_client, tmp_path, monkeypatch):
    store, first = await _completed_episode(fake_client, tmp_path)
    refusal = p.PromotionRefusedError("owned elsewhere", episode_n=1, attempt_id="x")

    def refuse(*_args, **_kwargs):
        raise refusal

    monkeypatch.setattr(p, "_promote_attempt_output", refuse)

    outcome = await _complete(fake_client, store, first["attempt_id"], tmp_path)

    assert isinstance(outcome, p.CompletionRefused)
    assert outcome.exc is refusal
