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
    assert kinds == ["generate_audio", "wait", "download", "rename"]
    assert fake_client.artifacts.calls[0][1]["language"] == "zh_Hant"
    assert all(c[0] != "add_file" for c in fake_client.sources.calls)
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
    add = [c for c in fake_client.sources.calls if c[0] == "add_file"][0][1]
    assert add["mime_type"] == "audio/mpeg"
    assert out["mp3_path"].endswith("ep02.mp3")


async def test_series_threads_prior_mp3_and_resumes(fake_client, tmp_path):
    eps = [
        {"brief": "第一集"},
        {"brief": "第二集"},
        {"brief": "第三集"},
    ]
    out = await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=1)
    assert len(out["episodes"]) == 3
    assert out["episodes"][0]["mp3_path"].endswith("ep01.mp3")
    assert out["episodes"][2]["mp3_path"].endswith("ep03.mp3")
    add_files = [c for c in fake_client.sources.calls if c[0] == "add_file"]
    assert len(add_files) == 2
    assert (tmp_path / "series_manifest.json").exists()


async def test_series_start_offset(fake_client, tmp_path):
    (tmp_path / "ep02.mp3").write_bytes(b"x")
    eps = [{"brief": "1"}, {"brief": "2"}, {"brief": "3"}]
    out = await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=3)
    assert len(out["episodes"]) == 1
    assert out["episodes"][0]["episode"] == 3
