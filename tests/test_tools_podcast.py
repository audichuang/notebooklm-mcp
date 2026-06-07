from notebooklm_mcp import tools_podcast as p


async def test_episode_first_no_prior(fake_client, tmp_path):
    out = await p.podcast_episode(
        "nb-1",
        episode_n=1,
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
    assert src_rename["new_title"] == "EP01"
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
        brief="第二集",
        output_dir=str(tmp_path),
        prior_mp3_path=str(prior),
    )
    # Two add_file calls: the PRIOR (ep01, for continuity) and this episode's OWN
    # mp3 (ep02). Both renamed to unified labels matching the Studio artifacts.
    add_files = [c for c in fake_client.sources.calls if c[0] == "add_file"]
    assert len(add_files) == 2
    assert all(c[1]["mime_type"] == "audio/mpeg" for c in add_files)
    rename_titles = [c[1]["new_title"] for c in fake_client.sources.calls if c[0] == "rename"]
    assert rename_titles == ["EP01", "EP02"]
    assert out["mp3_path"].endswith("ep02.mp3")


async def test_series_every_episode_self_uploads_named_source(fake_client, tmp_path):
    eps = [
        {"brief": "第一集"},
        {"brief": "第二集"},
        {"brief": "第三集"},
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
    assert rename_titles == ["EP01", "EP02", "EP03"]
    assert (tmp_path / "series_manifest.json").exists()


async def test_episode_rejects_prior_mp3_for_first_episode(fake_client, tmp_path):
    prior = tmp_path / "ep00.mp3"
    prior.write_bytes(b"x")
    import pytest

    with pytest.raises(ValueError, match="episode_n >= 2"):
        await p.podcast_episode("nb-1", episode_n=1, brief="x", output_dir=str(tmp_path), prior_mp3_path=str(prior))


async def test_series_start_offset(fake_client, tmp_path):
    eps = [{"brief": "1"}, {"brief": "2"}, {"brief": "3"}]
    out = await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=3)
    assert len(out["episodes"]) == 1
    assert out["episodes"][0]["episode"] == 3
    # Resume still self-uploads the resumed episode as a named source.
    rename_titles = [c[1]["new_title"] for c in fake_client.sources.calls if c[0] == "rename"]
    assert rename_titles == ["EP03"]
