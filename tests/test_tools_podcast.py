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
