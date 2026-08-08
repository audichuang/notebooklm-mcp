import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from notebooklm.types import ArtifactType

from notebooklm_mcp import tools_basic as b
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


async def test_reconcile_accepts_sdk_naive_local_artifact_timestamp(
    fake_client, tmp_path
):
    artifact = _remote_audio("remote-audio-naive")
    artifact.created_at = datetime.now()
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [artifact]
    )

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    assert out["artifact_id"] == "remote-audio-naive"
    assert out["safe_next_action"] == "podcast_episode_resume"


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
    assert out["safe_next_action"] == "podcast_episode_reconcile"


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


async def test_resume_refuses_another_artifact_while_a_durable_output_exists(
    fake_client, tmp_path
):
    """已有 durable output 時,resume 不得為「另一個 artifact」開新 attempt。

    舊行為放行(只要 active == output),而當時的 `_promote_attempt_output` 沒有歸屬檢查
    ——finalize 成功就把 output 指標換掉,等於從 resume 後門做了一次無審計的取代:
    沒有 retract、沒有理由、舊紀錄不留。這是 durability guard 想擋的同一件事,只是走
    另一條路。現在必須先 `podcast_attempt_retract` 才有取代版。"""
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

    with pytest.raises(ValueError, match="already has durable output"):
        p._ensure_resume_attempt(
            store,
            notebook_id="nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="newer-artifact",
        )
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["output_attempt_id"] == first["attempt_id"]
    assert len(stored["episodes"][0]["attempts"]) == 1


async def test_claimed_artifact_resume_cannot_hide_another_active_attempt(
    fake_client, tmp_path
):
    """有 attempt 在飛時,resume 不得改抓另一個 artifact —— 包含**已被別的 attempt claim**
    的那筆(claimed 分支是 `_ensure_resume_attempt` 裡另一條建立路徑,兩條都要驗)。

    狀態經合法的 retract 造出:已有 durable output 時 resume 換 artifact 本身已被禁止。"""
    manifest_path = tmp_path / "series_manifest.json"
    first = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    retraction = await p.podcast_attempt_retract(
        str(manifest_path), 1, first["attempt_id"], reason="QA 拒收"
    )
    await b.source_delete("nb-1", retraction["stale_source_id"])

    store = p.ManifestStore(manifest_path)
    newer_attempt_id = p._ensure_resume_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="newer-artifact",
    )

    # (a) 全新、未被 claim 的第三個 artifact
    with pytest.raises(ValueError, match="has active attempt|another active attempt"):
        p._ensure_resume_attempt(
            store,
            notebook_id="nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="second-newer-artifact",
        )
    # (b) 被作廢的那個 attempt 所 claim 的 artifact:tombstone 擋在最前面
    with pytest.raises(ValueError, match="was retracted"):
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


async def test_adopt_selects_one_ambiguous_artifact_without_remote_side_effects(
    fake_client, tmp_path
):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client,
        tmp_path,
        [_remote_audio("remote-audio-1"), _remote_audio("remote-audio-2")],
    )
    reconciled = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )
    assert reconciled["observed_state"] == "reconciliation_ambiguous"
    artifact_boundary = len(fake_client.artifacts.calls)
    source_boundary = len(fake_client.sources.calls)

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        attempt_id=attempt_id,
        artifact_id="remote-audio-2",
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert adopted["artifact_id"] == "remote-audio-2"
    assert adopted["observed_state"] == "accepted"
    assert attempt["dispatch"]["status"] == "accepted"
    assert attempt["remote"]["artifact_id"] == "remote-audio-2"
    assert [
        call
        for call in fake_client.artifacts.calls[artifact_boundary:]
        if call[0] in {"generate_audio", "rename", "download"}
    ] == []
    assert fake_client.sources.calls[source_boundary:] == []


async def test_adopt_rejects_attempt_identity_drift_before_remote_lookup(
    fake_client, tmp_path
):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client,
        tmp_path,
        [_remote_audio("remote-audio-1"), _remote_audio("remote-audio-2")],
    )
    await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    # 繞過 ManifestStore 直接改寫 JSON 檔:真實的手改破壞本來就長這樣。只留 title
    # drift——episode/attempt notebook 分裂現在會被 manifest_store._validate 的一致性
    # guard 在任何 store.read() 就擋下(見 test_manifest_store.py),連不到這裡要測的
    # podcast_attempt_adopt 下游 identity gate,那個場景已經換一層測、由別的測試鎖住。
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    corrupted_attempt = next(
        a for a in stored["episodes"][0]["attempts"] if a["attempt_id"] == attempt_id
    )
    corrupted_attempt["title"] = "另一集"
    manifest_path.write_text(
        json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    artifact_boundary = len(fake_client.artifacts.calls)
    source_boundary = len(fake_client.sources.calls)

    with pytest.raises(ValueError, match="different notebook|title"):
        await p.podcast_attempt_adopt(
            str(manifest_path),
            episode_n=1,
            attempt_id=attempt_id,
            artifact_id="remote-audio-1",
        )

    assert fake_client.artifacts.calls[artifact_boundary:] == []
    assert fake_client.sources.calls[source_boundary:] == []


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
