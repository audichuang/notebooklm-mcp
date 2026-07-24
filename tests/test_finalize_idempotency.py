import asyncio
import json

import pytest

from notebooklm_mcp import tools_podcast as p


async def test_manifest_backed_resume_of_completed_attempt_is_side_effect_free(
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
    before_artifacts = list(fake_client.artifacts.calls)
    before_sources = list(fake_client.sources.calls)

    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id=first["artifact_id"],
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert resumed["attempt_id"] == first["attempt_id"]
    assert resumed["artifact_id"] == first["artifact_id"]
    assert [
        call
        for call in fake_client.artifacts.calls[len(before_artifacts) :]
        if call[0] in {"rename", "download"}
    ] == []
    assert [
        call
        for call in fake_client.sources.calls[len(before_sources) :]
        if call[0] in {"add_file", "rename"}
    ] == []


async def test_completed_resume_fails_if_feedback_source_was_deleted(
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
    fake_client.sources.sources.clear()

    with pytest.raises(RuntimeError, match="source.*no longer exists|cannot be verified"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id=first["artifact_id"],
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )


async def test_source_upload_response_loss_is_reconciled_without_second_upload(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.sources.add_file_exc_after_create = TimeoutError("upload response lost")

    with pytest.raises(TimeoutError, match="upload response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = manifest["episodes"][0]["active_attempt_id"]
    assert len(fake_client.sources.sources) == 1
    assert len(
        [call for call in fake_client.sources.calls if call[0] == "add_file"]
    ) == 1

    # 模擬新 process：遠端 source 保留，client 不再丟失 response。
    fake_client.sources.add_file_exc_after_create = None
    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="task-123",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert len(fake_client.sources.sources) == 1
    assert len(
        [call for call in fake_client.sources.calls if call[0] == "add_file"]
    ) == 1
    assert fake_client.sources.sources[0]["title"] == "EP01 心法篇"
    assert resumed["attempt_id"] == attempt_id


async def test_concurrent_resume_only_one_caller_uploads_feedback_source(
    fake_client, tmp_path, monkeypatch
):
    manifest_path = tmp_path / "series_manifest.json"
    original_list = fake_client.sources.list

    async def fail_before_upload(_notebook_id):
        raise ConnectionError("stop before feedback upload")

    monkeypatch.setattr(fake_client.sources, "list", fail_before_upload)
    with pytest.raises(ConnectionError, match="stop before feedback upload"):
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
    upload = stored["episodes"][0]["attempts"][0]["finalize"][
        "feedback_source_upload"
    ]
    assert upload["status"] == "not_started"

    arrivals = 0
    both_arrived = asyncio.Event()

    async def synchronized_list(notebook_id):
        nonlocal arrivals
        arrivals += 1
        if arrivals <= 2:
            if arrivals == 2:
                both_arrived.set()
            await both_arrived.wait()
        return await original_list(notebook_id)

    monkeypatch.setattr(fake_client.sources, "list", synchronized_list)

    async def resume():
        return await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="task-123",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    results = await asyncio.gather(resume(), resume(), return_exceptions=True)

    assert len(
        [call for call in fake_client.sources.calls if call[0] == "add_file"]
    ) == 1
    assert len(fake_client.sources.sources) == 1
    assert any(isinstance(result, dict) for result in results)
    assert all(
        isinstance(result, dict)
        or (
            isinstance(result, RuntimeError)
            and "acceptance remains unknown" in str(result)
        )
        for result in results
    )
    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    final_upload = final["episodes"][0]["attempts"][0]["finalize"][
        "feedback_source_upload"
    ]
    assert final_upload["source_id"] == "src-1"
    assert final["episodes"][0]["active_attempt_id"] == attempt_id


async def test_unpromoted_new_attempt_does_not_replace_prior_output_bytes(
    fake_client, tmp_path, monkeypatch
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
    old_path = tmp_path / "ep01.mp3"
    old_bytes = old_path.read_bytes()
    fake_client.artifacts.download_audio_bytes = b"new-attempt-audio"

    async def fail_after_download(_notebook_id):
        raise ConnectionError("stop before new source upload")

    monkeypatch.setattr(fake_client.sources, "list", fail_after_download)
    with pytest.raises(ConnectionError, match="stop before new source upload"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="task-new",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    assert old_path.read_bytes() == old_bytes
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = stored["episodes"][0]
    assert episode["artifact_id"] == first["artifact_id"]
    assert episode["mp3_path"] == str(old_path)
    assert episode["output_attempt_id"] == first["attempt_id"]

    active_attempt_id = episode["active_attempt_id"]
    assert active_attempt_id != first["attempt_id"]
    active = next(
        row
        for row in episode["attempts"]
        if row["attempt_id"] == active_attempt_id
    )
    candidate_path = active["finalize"]["download"]["path"]
    assert candidate_path != str(old_path)
    assert candidate_path.endswith(f"{active_attempt_id}/ep01.mp3")
    with open(candidate_path, "rb") as candidate:
        assert candidate.read() == b"new-attempt-audio"


async def test_interrupted_download_does_not_replace_prior_successful_mp3(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    mp3_path = tmp_path / "ep01.mp3"
    mp3_path.write_bytes(b"known-good-audio")
    fake_client.artifacts.download_audio_partial_bytes = b"partial"
    fake_client.artifacts.download_audio_exc = ConnectionError("download interrupted")

    with pytest.raises(ConnectionError, match="download interrupted"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    assert mp3_path.read_bytes() == b"known-good-audio"


async def test_completed_attempt_with_missing_mp3_only_redownloads(
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
    mp3_path = tmp_path / "ep01.mp3"
    mp3_path.unlink()
    artifact_boundary = len(fake_client.artifacts.calls)
    source_boundary = len(fake_client.sources.calls)

    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id=first["artifact_id"],
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert resumed["attempt_id"] == first["attempt_id"]
    assert mp3_path.read_bytes() == fake_client.artifacts.download_audio_bytes
    assert [
        call[0]
        for call in fake_client.artifacts.calls[artifact_boundary:]
        if call[0] in {"rename", "download"}
    ] == ["download"]
    assert [
        call
        for call in fake_client.sources.calls[source_boundary:]
        if call[0] in {"add_file", "rename"}
    ] == []
