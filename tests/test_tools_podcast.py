import pytest

from notebooklm_mcp import tools_podcast as p


async def test_series_names_artifact_and_source_with_title(fake_client, tmp_path):
    # The outline gives every episode a title; both the Studio artifact and the
    # self-uploaded source must be renamed to "EP{n:02d} {title}" — the SAME
    # string for both (unified-naming iron rule).
    eps = [{"title": "心法篇", "brief": "1"}, {"title": "實戰篇", "brief": "2"}]
    await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=1)
    artifact_renames = [c[1]["new_title"] for c in fake_client.artifacts.calls if c[0] == "rename"]
    source_renames = [c[1]["new_title"] for c in fake_client.sources.calls if c[0] == "rename"]
    assert artifact_renames == ["EP01 心法篇", "EP02 實戰篇"]
    assert source_renames == ["EP01 心法篇", "EP02 實戰篇"]


async def test_series_rejects_missing_title(fake_client, tmp_path):
    eps = [{"brief": "no title here"}]
    with pytest.raises(ValueError, match="title"):
        await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path))
    # Rejected up front — no generation burned.
    assert fake_client.artifacts.calls == []


async def test_series_rejects_empty_title(fake_client, tmp_path):
    eps = [{"title": "   ", "brief": "blank title"}]
    with pytest.raises(ValueError, match="title"):
        await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path))
    assert fake_client.artifacts.calls == []


async def test_episode_names_with_title(fake_client, tmp_path):
    out = await p.podcast_episode(
        "nb-1", episode_n=2, title="實戰篇", brief="第二集", output_dir=str(tmp_path)
    )
    artifact_rename = next(c[1]["new_title"] for c in fake_client.artifacts.calls if c[0] == "rename")
    source_rename = next(c[1]["new_title"] for c in fake_client.sources.calls if c[0] == "rename")
    assert artifact_rename == "EP02 實戰篇"
    assert source_rename == "EP02 實戰篇"
    assert out["title"] == "實戰篇"
    assert out["label"] == "EP02 實戰篇"
    assert isinstance(out["published_at"], str) and out["published_at"]


async def test_episode_first_no_prior(fake_client, tmp_path):
    out = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="開場篇",
        brief="第一集講開場",
        output_dir=str(tmp_path),
    )
    assert out["mp3_path"].endswith("ep01.mp3")
    kinds = [c[0] for c in fake_client.artifacts.calls]
    # Artifact is renamed in NotebookLM BEFORE the mp3 is downloaded.
    assert kinds == ["generate_audio", "wait", "rename", "download"]
    assert fake_client.artifacts.calls[0][1]["language"] == "zh_Hant"
    # Episode 1 has no PRIOR to upload, but it self-uploads its own mp3 as a named
    # source so the notebook keeps a complete, Studio-matching record.
    add_files = [c for c in fake_client.sources.calls if c[0] == "add_file"]
    assert len(add_files) == 1
    src_rename = next(c[1] for c in fake_client.sources.calls if c[0] == "rename")
    assert src_rename["new_title"] == "EP01 開場篇"
    # Regression: download AND rename must target the real artifact id (== task_id),
    # never None. GenerationStatus has no artifact_id field, so the code must derive
    # it from task_id. A None here means download falls back to "latest" (wrong
    # artifact in a multi-artifact notebook) and rename targets nothing.
    download_call = next(c[1] for c in fake_client.artifacts.calls if c[0] == "download")
    rename_call = next(c[1] for c in fake_client.artifacts.calls if c[0] == "rename")
    assert download_call["artifact_id"] == "task-123"
    assert rename_call["artifact_id"] == "task-123"
    assert out["artifact_id"] == "task-123"


async def test_episode_with_prior_reuploads_mp3(fake_client, tmp_path):
    prior = tmp_path / "ep01.mp3"
    prior.write_bytes(b"x")
    out = await p.podcast_episode(
        "nb-1",
        episode_n=2,
        title="實戰篇",
        brief="第二集",
        output_dir=str(tmp_path),
        prior_mp3_path=str(prior),
    )
    # Two add_file calls: the PRIOR (ep01, for continuity) and this episode's OWN
    # mp3 (ep02). The prior re-seed keeps a bare EP01 (caller may not know its
    # title); this episode's own source is the titled label "EP02 實戰篇".
    add_files = [c for c in fake_client.sources.calls if c[0] == "add_file"]
    assert len(add_files) == 2
    assert all(c[1]["mime_type"] == "audio/mpeg" for c in add_files)
    rename_titles = [c[1]["new_title"] for c in fake_client.sources.calls if c[0] == "rename"]
    assert rename_titles == ["EP01", "EP02 實戰篇"]
    assert out["mp3_path"].endswith("ep02.mp3")


async def test_series_every_episode_self_uploads_named_source(fake_client, tmp_path):
    eps = [
        {"title": "心法篇", "brief": "第一集"},
        {"title": "實戰篇", "brief": "第二集"},
        {"title": "收尾篇", "brief": "第三集"},
    ]
    out = await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=1)
    assert len(out["episodes"]) == 3
    assert out["episodes"][0]["mp3_path"].endswith("ep01.mp3")
    assert out["episodes"][2]["mp3_path"].endswith("ep03.mp3")
    # Every episode (including the LAST) self-uploads exactly one named source, so
    # the notebook record is complete and unified with the Studio artifacts.
    add_files = [c for c in fake_client.sources.calls if c[0] == "add_file"]
    assert len(add_files) == 3
    rename_titles = [c[1]["new_title"] for c in fake_client.sources.calls if c[0] == "rename"]
    assert rename_titles == ["EP01 心法篇", "EP02 實戰篇", "EP03 收尾篇"]
    assert (tmp_path / "series_manifest.json").exists()


async def test_episode_rejects_prior_mp3_for_first_episode(fake_client, tmp_path):
    prior = tmp_path / "ep00.mp3"
    prior.write_bytes(b"x")

    with pytest.raises(ValueError, match="episode_n >= 2"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title="開場篇", brief="x", output_dir=str(tmp_path), prior_mp3_path=str(prior)
        )


async def test_series_start_offset(fake_client, tmp_path):
    eps = [
        {"title": "心法篇", "brief": "1"},
        {"title": "實戰篇", "brief": "2"},
        {"title": "收尾篇", "brief": "3"},
    ]
    out = await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=3)
    assert len(out["episodes"]) == 1
    assert out["episodes"][0]["episode"] == 3
    # Resume still self-uploads the resumed episode as a named source.
    rename_titles = [c[1]["new_title"] for c in fake_client.sources.calls if c[0] == "rename"]
    assert rename_titles == ["EP03 收尾篇"]


async def test_all_renames_are_fire_and_forget(fake_client, tmp_path):
    """0.7.3 rename 預設 return_object=True 會多抓一次全量清單且可能 raise
    not-found;我們所有呼叫點必須顯式傳 False(保留 0.4.1 語意)。"""
    await p.podcast_episode(
        "nb-123", episode_n=1, title="心法篇", brief="b", output_dir=str(tmp_path)
    )
    renames = [c[1] for c in fake_client.sources.calls if c[0] == "rename"]
    renames += [c[1] for c in fake_client.artifacts.calls if c[0] == "rename"]
    assert renames, "podcast flow 必須有 rename 呼叫"
    assert all(r["return_object"] is False for r in renames)


async def test_podcast_series_fails_fast_when_auth_dead(fake_client, tmp_path):
    """整季開跑前先預檢:cookie 死了要秒退,一個生成都不能燒。"""
    fake_client.notebooks.fail_list = True
    with pytest.raises(RuntimeError, match="sync-auth"):
        await p.podcast_series(
            "nb-123",
            episodes=[{"title": "心法篇", "brief": "b"}],
            output_dir=str(tmp_path),
        )
    assert fake_client.artifacts.calls == []


async def test_podcast_episode_fails_fast_when_auth_dead(fake_client, tmp_path):
    fake_client.notebooks.fail_list = True
    with pytest.raises(RuntimeError, match="sync-auth"):
        await p.podcast_episode(
            "nb-123", episode_n=1, title="心法篇", brief="b", output_dir=str(tmp_path)
        )
    assert fake_client.artifacts.calls == []


async def test_local_validation_beats_auth_probe(fake_client, tmp_path):
    """壞參數必須在打任何網路 RPC 之前用 ValueError 秒退——認證錯誤不得蓋掉參數錯誤。"""
    fake_client.notebooks.fail_list = True  # 若先 probe 會變 RuntimeError → 測試失敗
    with pytest.raises(ValueError, match="title"):
        await p.podcast_episode(
            "nb-123", episode_n=1, title="  ", brief="b", output_dir=str(tmp_path)
        )
