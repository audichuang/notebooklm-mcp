import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from notebooklm.types import ArtifactType

from notebooklm_mcp import tools_podcast as p


async def test_source_adoption_rejects_prepared_attempt_before_remote_lookup(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(manifest_path)
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        language="zh_Hant",
        audio_format="deep-dive",
        audio_length="long",
    )
    source_id = fake_client.sources._add("EP01 心法篇", kind="media")
    artifact_boundary = len(fake_client.artifacts.calls)
    source_boundary = len(fake_client.sources.calls)

    with pytest.raises(ValueError, match="completed audio|download"):
        await p.podcast_attempt_adopt(
            str(manifest_path),
            episode_n=1,
            attempt_id=attempt_id,
            feedback_source_id=source_id,
        )

    assert fake_client.artifacts.calls[artifact_boundary:] == []
    assert fake_client.sources.calls[source_boundary:] == []


async def test_artifact_rename_response_loss_retries_when_title_did_not_land(
    fake_client, tmp_path, monkeypatch
):
    manifest_path = tmp_path / "series_manifest.json"
    original_rename = fake_client.artifacts.rename
    rename_count = 0

    async def lose_first_response(
        notebook_id, artifact_id, new_title, *, return_object=True
    ):
        nonlocal rename_count
        rename_count += 1
        if rename_count == 1:
            fake_client.artifacts.calls.append(
                (
                    "rename",
                    {
                        "artifact_id": artifact_id,
                        "new_title": new_title,
                        "return_object": return_object,
                    },
                )
            )
            raise ConnectionError("rename response lost before landing")
        return await original_rename(
            notebook_id,
            artifact_id,
            new_title,
            return_object=return_object,
        )

    monkeypatch.setattr(fake_client.artifacts, "rename", lose_first_response)

    with pytest.raises(ConnectionError, match="response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    interrupted = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = interrupted["episodes"][0]["attempts"][0]
    assert attempt["finalize"]["artifact_rename"]["status"] == "outcome_unknown"
    artifact = next(
        row
        for row in fake_client.artifacts.artifacts
        if row.id == "task-123"
    )
    assert artifact.title == "Audio Overview"

    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="task-123",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert resumed["artifact_id"] == "task-123"
    assert artifact.title == "EP01 心法篇"
    assert rename_count == 2


async def test_legacy_missing_audio_resumes_without_duplicate_adopted_source(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    missing_path = tmp_path / "missing-legacy-ep01.mp3"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "notebook_id": "nb-1",
                "episodes": [
                    {
                        "episode": 1,
                        "title": "心法篇",
                        "artifact_id": "legacy-artifact",
                        "mp3_path": str(missing_path),
                        "published_at": "Wed, 01 Jan 2020 09:00:00 +0800",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="legacy-artifact",
            title="EP01 心法篇",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )
    source_id = fake_client.sources._add("EP01 心法篇", kind="media")
    await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        feedback_source_id=source_id,
    )
    source_boundary = len(fake_client.sources.calls)

    stopped = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )
    assert stopped["complete"] is False
    assert stopped["safe_next_action"] == "podcast_episode_resume"
    assert stopped["artifact_id"] == "legacy-artifact"

    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="legacy-artifact",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert resumed["feedback_source_id"] == source_id
    assert [
        call
        for call in fake_client.sources.calls[source_boundary:]
        if call[0] in {"add_file", "rename"}
    ] == []


async def test_completed_fast_path_rejects_missing_remote_artifact(
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
    fake_client.artifacts.artifacts.clear()

    with pytest.raises(RuntimeError, match="artifact.*cannot be verified"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id=first["artifact_id"],
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )


async def test_acceptance_unknown_can_adopt_late_verified_artifact(
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
    attempt_id = stored["episodes"][0]["active_attempt_id"]
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="late-artifact",
            title="Audio Overview",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        attempt_id=attempt_id,
        artifact_id="late-artifact",
    )

    assert adopted["artifact_id"] == "late-artifact"
    assert adopted["safe_next_action"] == "podcast_episode_resume"


async def test_acceptance_unknown_adoption_uses_episode_notebook_in_rolling_manifest(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "episode-notebook",
            episode_n=40,
            title="跨供應商委派",
            brief="第四十集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored["notebook_id"] = "legacy-show-notebook"
    manifest_path.write_text(
        json.dumps(stored, ensure_ascii=False), encoding="utf-8"
    )
    attempt_id = stored["episodes"][0]["active_attempt_id"]
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="late-episode-artifact",
            title="Audio Overview",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=40,
        attempt_id=attempt_id,
        artifact_id="late-episode-artifact",
    )

    assert adopted["artifact_id"] == "late-episode-artifact"
    assert adopted["safe_next_action"] == "podcast_episode_resume"


def test_legacy_task_id_is_durable_output_evidence():
    assert p.has_durable_output_evidence({"task_id": "legacy-task"})


async def test_published_legacy_output_blocks_implicit_supersede_after_failed_resume(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    mp3_path = tmp_path / "ep19.mp3"
    published_bytes = b"already published EP19"
    mp3_path.write_bytes(published_bytes)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "notebook_id": "nb-1",
                "episodes": [
                    {
                        "episode": 1,
                        "title": "心法篇",
                        "artifact_id": "legacy-artifact",
                        "mp3_path": str(mp3_path),
                        "published_at": "Fri, 24 Jul 2026 09:00:00 +0800",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="legacy-artifact",
            title="EP01 心法篇",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )
    fake_client.artifacts.fail_complete = True

    with pytest.raises(RuntimeError, match="Generation failed"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="legacy-artifact",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    fake_client.artifacts.fail_complete = False
    generate_boundary = len(
        [
            call
            for call in fake_client.artifacts.calls
            if call[0] == "generate_audio"
        ]
    )
    with pytest.raises(ValueError, match="durable output"):
        await p.podcast_series(
            "nb-1",
            episodes=[{"title": "心法篇", "brief": "第一集"}],
            output_dir=str(tmp_path),
        )

    assert mp3_path.read_bytes() == published_bytes
    assert len(
        [
            call
            for call in fake_client.artifacts.calls
            if call[0] == "generate_audio"
        ]
    ) == generate_boundary


async def test_legacy_source_is_not_carried_to_a_different_artifact(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    mp3_path = tmp_path / "legacy-ep01.mp3"
    mp3_path.write_bytes(b"legacy audio")
    old_source_id = fake_client.sources._add("EP01 心法篇", kind="media")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "notebook_id": "nb-1",
                "episodes": [
                    {
                        "episode": 1,
                        "title": "心法篇",
                        "artifact_id": "legacy-artifact",
                        "mp3_path": str(mp3_path),
                        "feedback_source_id": old_source_id,
                        "feedback_source_adopted_at": (
                            "2026-07-25T00:00:00+00:00"
                        ),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="replacement-artifact",
            title="Audio Overview",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )
    source_boundary = len(fake_client.sources.calls)

    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="replacement-artifact",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert resumed["feedback_source_id"] != old_source_id
    assert len(
        [
            call
            for call in fake_client.sources.calls[source_boundary:]
            if call[0] == "add_file"
        ]
    ) == 1


async def test_ambiguous_uploaded_source_candidate_can_be_adopted_before_rename(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.sources.add_file_exc_after_create = TimeoutError(
        "upload response lost"
    )

    with pytest.raises(TimeoutError, match="upload response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    fake_client.sources.add_file_exc_after_create = None
    fake_client.sources._add("ep01.mp3", kind="media")
    stopped = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )
    assert stopped["observed_state"] == "reconciliation_ambiguous"
    assert stopped["safe_next_action"] == "podcast_attempt_adopt"

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = stored["episodes"][0]["active_attempt_id"]
    candidates = (
        stored["episodes"][0]["attempts"][0]["finalize"]
        ["feedback_source_upload"]["candidate_source_ids"]
    )
    add_boundary = len(
        [call for call in fake_client.sources.calls if call[0] == "add_file"]
    )
    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        attempt_id=attempt_id,
        feedback_source_id=candidates[0],
    )

    assert adopted["complete"] is False
    assert adopted["safe_next_action"] == "podcast_episode_resume"
    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="task-123",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    assert resumed["feedback_source_id"] == candidates[0]
    assert len(
        [call for call in fake_client.sources.calls if call[0] == "add_file"]
    ) == add_boundary


async def test_completed_episode_transport_failure_returns_structured_partial(
    fake_client, tmp_path, monkeypatch
):
    """已完成集的 drift 複驗途中傳輸失敗不得裸拋,否則整季進度回報全丟。"""
    manifest_path = tmp_path / "series_manifest.json"
    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="1",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    async def transport_failure(_notebook_id):
        raise ConnectionError("network blip while verifying completed episode")

    monkeypatch.setattr(fake_client.sources, "list", transport_failure)
    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "1"}, {"title": "實戰篇", "brief": "2"}],
        output_dir=str(tmp_path),
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 1
    assert out["observed_state"] == "verification_incomplete"
    assert out["safe_next_action"] == "podcast_series"
    assert [
        call for call in fake_client.artifacts.calls if call[0] == "generate_audio"
    ] == [call for call in fake_client.artifacts.calls if call[0] == "generate_audio"][:1]


async def test_repeated_quota_supersede_is_visible_to_the_caller(
    fake_client, tmp_path
):
    """removed(每日配額下架)自動 supersede 時,呼叫端必須看得出在原地打轉。"""
    fake_client.artifacts.fail_removed = True
    episodes = [{"title": "心法篇", "brief": "1"}]

    first = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path)
    )
    second = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path)
    )
    third = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path)
    )

    assert [out["observed_state"] for out in (first, second, third)] == [
        "removed",
        "removed",
        "removed",
    ]
    assert [out["attempt_count"] for out in (first, second, third)] == [1, 2, 3]
    assert [out["superseded_attempt_count"] for out in (first, second, third)] == [
        0,
        1,
        2,
    ]


async def test_promoted_episode_records_the_verified_feedback_source_id(
    fake_client, tmp_path
):
    """promote 要留下 episode 級 continuity 證據,attempts 日後被裁剪也還在。"""
    manifest_path = tmp_path / "series_manifest.json"
    output = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="1",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = stored["episodes"][0]
    assert episode["feedback_source_id"] == output["feedback_source_id"]
    assert "feedback_source_adopted_at" not in episode
