import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from notebooklm.types import ArtifactType

from notebooklm_mcp import tools_podcast as p


async def test_attempt_is_dispatching_before_generate_audio_side_effect(fake_client, tmp_path):
    """遠端 generation 一進入時，本地 attempt identity 與 dispatching 已 durable。"""
    manifest_path = tmp_path / "series_manifest.json"
    observed = {}

    def capture_manifest():
        observed.update(json.loads(manifest_path.read_text(encoding="utf-8")))

    fake_client.artifacts.on_generate_audio = capture_manifest

    out = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    episode = observed["episodes"][0]
    assert episode["active_attempt_id"]
    attempt = episode["attempts"][0]
    assert attempt["attempt_id"] == episode["active_attempt_id"]
    assert attempt["dispatch"]["status"] == "dispatching"
    assert attempt["remote"]["artifact_id"] is None
    assert out["attempt_id"] == attempt["attempt_id"]

async def test_transport_loss_marks_acceptance_unknown_without_regenerating(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")

    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = stored["episodes"][0]
    attempt = episode["attempts"][0]
    assert episode["active_attempt_id"] == attempt["attempt_id"]
    assert attempt["dispatch"]["status"] == "acceptance_unknown"
    assert attempt["remote"]["artifact_id"] is None
    assert attempt["errors"][-1]["phase"] == "dispatch"
    assert attempt["errors"][-1]["type"] == "TimeoutError"
    assert [
        call[0] for call in fake_client.artifacts.calls
        if call[0] == "generate_audio"
    ] == ["generate_audio"]


async def test_retry_same_episode_after_transport_loss_does_not_generate_again(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")

    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    fake_client.artifacts.generate_audio_exc = None
    with pytest.raises(ValueError, match="durable active attempt|既有.*attempt"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(stored["episodes"][0]["attempts"]) == 1
    assert [c[0] for c in fake_client.artifacts.calls].count("generate_audio") == 1


def _remote_audio(artifact_id: str):
    return SimpleNamespace(
        id=artifact_id,
        title="Audio Overview",
        kind=ArtifactType.AUDIO,
        created_at=datetime.now(timezone.utc),
    )


async def _leave_acceptance_unknown(fake_client, tmp_path, candidates):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_remote_artifacts_before_raise = list(candidates)
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest_path, stored["episodes"][0]["active_attempt_id"]


async def test_reconcile_adopts_the_only_unclaimed_audio_candidate(
    fake_client, tmp_path
):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [_remote_audio("remote-audio-1")]
    )

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "accepted"
    assert attempt["remote"]["artifact_id"] == "remote-audio-1"
    assert out["artifact_id"] == "remote-audio-1"
    assert [
        call[0] for call in fake_client.artifacts.calls
        if call[0] == "generate_audio"
    ] == ["generate_audio"]


async def test_reconcile_with_no_candidate_stays_unknown(fake_client, tmp_path):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, []
    )

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "acceptance_unknown"
    assert attempt["remote"]["artifact_id"] is None
    assert out["complete"] is False
    assert out["observed_state"] == "acceptance_unknown"
    assert out["safe_next_action"] == "wait_and_reconcile"


async def test_explicit_resume_cannot_replace_an_unreconciled_active_attempt(
    fake_client, tmp_path
):
    manifest_path, original_attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, []
    )
    fake_client.artifacts.generate_audio_exc = None

    with pytest.raises(ValueError, match="active attempt|先.*reconcile"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="caller-guessed-artifact",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = stored["episodes"][0]
    assert episode["active_attempt_id"] == original_attempt_id
    assert len(episode["attempts"]) == 1
    assert episode["attempts"][0]["remote"]["artifact_id"] is None


async def test_claimed_artifact_resume_cannot_hide_another_active_attempt(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    first = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    store = p.ManifestStore(manifest_path)
    newer_attempt_id = p._ensure_resume_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="newer-artifact",
    )

    with pytest.raises(ValueError, match="another active attempt|另一個.*attempt"):
        p._ensure_resume_attempt(
            store,
            notebook_id="nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id=first["artifact_id"],
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["active_attempt_id"] == newer_attempt_id


async def test_late_dispatch_outcomes_cannot_downgrade_or_replace_mapping(
    fake_client, tmp_path
):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [_remote_audio("remote-audio-1")]
    )
    await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    store = p.ManifestStore(manifest_path)
    p._mark_acceptance_unknown(
        store, episode_n=1, attempt_id=attempt_id, error=TimeoutError("late timeout")
    )
    after_timeout = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = after_timeout["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "accepted"
    assert attempt["remote"]["artifact_id"] == "remote-audio-1"

    failed_status = SimpleNamespace(
        error="late failure", error_code=None, status="failed"
    )
    p._mark_not_accepted(
        store, episode_n=1, attempt_id=attempt_id, status=failed_status
    )
    after_failure = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = after_failure["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "accepted"
    assert attempt["remote"]["status"] == "pending"

    with pytest.raises(ValueError, match="another artifact|不同"):
        p._bind_accepted_artifact(
            store, episode_n=1, attempt_id=attempt_id, artifact_id="remote-audio-2"
        )
    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert final["episodes"][0]["attempts"][0]["remote"]["artifact_id"] == (
        "remote-audio-1"
    )

    def mark_completed(manifest):
        _, current = p._attempt_record(manifest, 1, attempt_id)
        current["remote"]["status"] = "completed"

    store.update(mark_completed)
    p._bind_accepted_artifact(
        store,
        episode_n=1,
        attempt_id=attempt_id,
        artifact_id="remote-audio-1",
    )
    completed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert completed["episodes"][0]["attempts"][0]["remote"]["status"] == (
        "completed"
    )


async def test_reconcile_with_multiple_candidates_is_ambiguous(
    fake_client, tmp_path
):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client,
        tmp_path,
        [_remote_audio("remote-audio-1"), _remote_audio("remote-audio-2")],
    )

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "reconciliation_ambiguous"
    assert attempt["remote"]["artifact_id"] is None
    assert out["complete"] is False
    assert out["observed_state"] == "reconciliation_ambiguous"
    assert out["candidate_artifact_ids"] == [
        "remote-audio-1",
        "remote-audio-2",
    ]


async def test_sdk_failed_status_is_not_accepted_not_transport_unknown(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.fail_generate = True

    with pytest.raises(RuntimeError, match="Generation failed"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "not_accepted"
    assert attempt["remote"]["artifact_id"] is None
    assert attempt["remote"]["status"] == "failed"
    assert attempt["remote"]["error"] == "simulated failure"
    assert attempt["errors"][-1]["phase"] == "dispatch"
