import asyncio
import json

import pytest

from notebooklm_mcp import tools_basic as b
from notebooklm_mcp import tools_podcast as p


def _visible_audio(artifact_id: str, title: str):
    return type(
        "Artifact",
        (),
        {
            "id": artifact_id,
            "title": title,
            "kind": p.ArtifactType.AUDIO,
        },
    )()


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


async def test_completed_resume_repairs_feedback_source_title_drift(
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
    source = next(
        row
        for row in fake_client.sources.sources
        if row["id"] == first["feedback_source_id"]
    )
    source["title"] = "人手誤改的名字"
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
    assert source["title"] == "EP01 心法篇"
    repair_calls = fake_client.sources.calls[source_boundary:]
    assert len([call for call in repair_calls if call[0] == "rename"]) == 1
    assert [call for call in repair_calls if call[0] == "add_file"] == []


async def test_completed_resume_repairs_artifact_title_drift(
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
    artifact = next(
        row
        for row in fake_client.artifacts.artifacts
        if row.id == first["artifact_id"]
    )
    artifact.title = "人手誤改的名字"
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
    assert artifact.title == "EP01 心法篇"
    repair_calls = fake_client.artifacts.calls[artifact_boundary:]
    assert len([call for call in repair_calls if call[0] == "rename"]) == 1
    assert [call for call in repair_calls if call[0] == "download"] == []
    assert [
        call
        for call in fake_client.sources.calls[source_boundary:]
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
    """取代版的下載不得蓋掉前一版的 bytes。

    狀態經**合法路徑**造出:已有 durable output 時 resume 另一個 artifact 已被禁止
    (那是無審計取代的後門),所以先 `podcast_attempt_retract` + 刪舊 source,再 resume。
    被作廢那版的 mp3 仍必須完好——`retracted_attempt_ids` 讓取代版一律下到
    attempts/<attempt_id>/,不再取決於當下剛好有沒有 cover_path。"""
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
    retraction = await p.podcast_attempt_retract(
        str(manifest_path), 1, first["attempt_id"], reason="QA 拒收"
    )
    assert retraction["retracted_mp3_path"] == str(old_path)
    await b.source_delete("nb-1", retraction["stale_source_id"])

    fake_client.artifacts.download_audio_bytes = b"new-attempt-audio"
    fake_client.artifacts.artifacts.append(
        _visible_audio("task-new", "Audio Overview")
    )

    real_source_list = fake_client.sources.list
    calls = {"n": 0}

    async def fail_after_download(notebook_id):
        # 第一次是 cleanup gate 的查詢(必須放行),第二次才是 finalize 的回錄上傳前檢查
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_source_list(notebook_id)
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
    # 作廢後 episode 級投影已清空,取代版尚未 promote → 不得有任何輸出證據回來
    assert "artifact_id" not in episode
    assert "mp3_path" not in episode
    assert "output_attempt_id" not in episode

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


async def test_legacy_partial_output_isolated_before_explicit_resume_promotion(
    fake_client, tmp_path, monkeypatch
):
    manifest_path = tmp_path / "series_manifest.json"
    old_path = tmp_path / "ep19.mp3"
    old_bytes = b"already published EP19"
    old_path.write_bytes(old_bytes)
    manifest_path.write_text(
        json.dumps(
            {
                "notebook_id": "nb-1",
                "episodes": [
                    {
                        "episode": 19,
                        "title": "既有第十九集",
                        "label": "EP19 既有第十九集",
                        "mp3_path": str(old_path),
                        "published_at": "Fri, 24 Jul 2026 09:00:00 +0800",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake_client.artifacts.download_audio_bytes = b"replacement audio"
    fake_client.artifacts.artifacts.append(
        _visible_audio("replacement-artifact", "Audio Overview")
    )

    async def fail_after_download(_notebook_id):
        raise ConnectionError("stop before replacement source upload")

    monkeypatch.setattr(fake_client.sources, "list", fail_after_download)
    with pytest.raises(ConnectionError, match="replacement source upload"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=19,
            title="既有第十九集",
            artifact_id="replacement-artifact",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    assert old_path.read_bytes() == old_bytes
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = stored["episodes"][0]
    assert episode["mp3_path"] == str(old_path)
    active_attempt_id = episode["active_attempt_id"]
    active = next(
        row
        for row in episode["attempts"]
        if row["attempt_id"] == active_attempt_id
    )
    candidate_path = active["finalize"]["download"]["path"]
    assert candidate_path != str(old_path)
    assert candidate_path.endswith(f"{active_attempt_id}/ep19.mp3")


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


async def test_first_mp3_download_is_not_left_private(fake_client, tmp_path):
    """mkstemp 給 0600;第一次下載沒有舊檔可繼承 mode,不該落成 0600(對齊
    _atomic.download_atomically 同一顧慮——否則之後 publish_series 讀不到)。"""
    import os
    import stat

    manifest_path = tmp_path / "series_manifest.json"
    out = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    assert stat.S_IMODE(os.stat(out["mp3_path"]).st_mode) == 0o644


async def test_interrupted_mp3_download_leaves_no_partial_file(fake_client, tmp_path):
    """失敗的 .part temp 沒清理會隨每次重試累積;要跟 _atomic.download_atomically 一樣
    在 except 分支清掉(清理失敗不得蓋掉原例外)。"""
    import os

    manifest_path = tmp_path / "series_manifest.json"
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

    leftovers = [name for name in os.listdir(tmp_path) if name.endswith(".part")]
    assert leftovers == []


async def test_completed_attempt_with_missing_mp3_only_redownloads(
    fake_client, tmp_path
):
    import os
    import stat

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
    # 鎖 manifest_store 的「os.stat 讀到既有檔案就沿用其 mode」分支:resume 這條真實
    # 路徑會再寫好幾次 manifest(finalize 每個 checkpoint 都是一次 update),寫完不該
    # 被打回預設的 0644。
    os.chmod(manifest_path, 0o600)
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
    assert stat.S_IMODE(os.stat(manifest_path).st_mode) == 0o600
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


async def test_cancelled_download_is_cleaned_up_without_being_marked_failed(
    fake_client, tmp_path
):
    """CancelledError 是 BaseException 子類,不會落進 `except Exception`——而 client
    cancellation(mcporter 預設 60s vs 單集動輒 20 分)正是這條路最常見的中斷來源。
    清理與 durable 語意都要對:.part 不留下一份,且 checkpoint 不能被標成
    "failed"(cancelled ≠ failed;resume 判定只看 status=="completed")。"""
    import asyncio
    import os

    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.download_audio_partial_bytes = b"partial"
    fake_client.artifacts.download_audio_exc = asyncio.CancelledError("client 60s timeout")

    with pytest.raises(asyncio.CancelledError, match="client 60s timeout"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    leftovers = [name for name in os.listdir(tmp_path) if name.endswith(".part")]
    assert leftovers == []

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = manifest["episodes"][0]
    attempt_id = episode["active_attempt_id"]
    attempt = next(
        row for row in episode["attempts"] if row["attempt_id"] == attempt_id
    )
    download = attempt["finalize"]["download"]
    assert download["status"] == "dispatching"  # 不是 "failed"

    # 之後真的能不重生地續完:清掉注入的 cancellation,resume 重下載一次即可。
    fake_client.artifacts.download_audio_exc = None
    fake_client.artifacts.download_audio_partial_bytes = None
    generate_boundary = len(fake_client.artifacts.calls)
    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id=attempt["remote"]["artifact_id"],
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    assert resumed["attempt_id"] == attempt_id
    assert not any(
        c[0] == "generate_audio"
        for c in fake_client.artifacts.calls[generate_boundary:]
    )


async def test_adopt_replacement_feedback_source_without_uploading_again(
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
    old_source_id = first["feedback_source_id"]
    fake_client.sources.sources.clear()
    replacement_source_id = fake_client.sources._add(
        "EP01 心法篇", kind="media"
    )
    source_boundary = len(fake_client.sources.calls)
    artifact_boundary = len(fake_client.artifacts.calls)

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        attempt_id=first["attempt_id"],
        feedback_source_id=replacement_source_id,
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    upload = attempt["finalize"]["feedback_source_upload"]
    assert adopted["feedback_source_id"] == replacement_source_id
    # 被取代的 old_source_id 現在要進清理義務(對齊 retract 的回傳形狀)——即使本測試
    # 用 sources.clear() 模擬它早就不在筆記本裡了,回傳的 hint 仍是「有待刪項」;真正
    # 的存在性驗證留給下一次 `_assert_source_cleanup_done`(它會查到已經不在、悄悄結案)。
    assert adopted["stale_source_ids"] == [old_source_id]
    assert adopted["safe_next_action"] == "source_delete"
    assert upload["source_id"] == replacement_source_id
    assert upload["status"] == "completed"
    assert upload["previous_source_ids"] == [old_source_id]
    assert attempt["finalize"]["feedback_source_rename"]["status"] == "completed"

    resumed = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )
    assert resumed["complete"] is True
    assert [
        call
        for call in fake_client.artifacts.calls[artifact_boundary:]
        if call[0] in {"generate_audio", "rename", "download"}
    ] == []
    assert [
        call
        for call in fake_client.sources.calls[source_boundary:]
        if call[0] in {"add_file", "rename"}
    ] == []
    # old_source_id 已經不在筆記本裡(sources.clear() 模擬),cleanup gate 查證後悄悄
    # 結案,不擋 series 續跑。
    assert "pending_source_cleanup" not in json.loads(
        manifest_path.read_text(encoding="utf-8")
    )["episodes"][0]
