"""Coverage for podcast_series resume (start=N) continuity.

Closes the gap flagged in deep testing: the prior `test_series_start_offset`
used a stateless fake, so it could not verify what actually preserves continuity
on a cross-process resume — namely that prior episodes' mp3s are SERVER-SIDE
notebook sources that persist across process runs. (Verified live: a fresh
process listing the notebook still showed EP01/EP02 — RESUME_CONTINUITY_PRESERVED.)
FakeSources now models that persistent set, so these tests assert the real invariant.
"""

import json

import pytest

from notebooklm_mcp import tools_podcast as p

EPS3 = [
    {"title": "心法篇", "brief": "1"},
    {"title": "實戰篇", "brief": "2"},
    {"title": "收尾篇", "brief": "3"},
]


async def test_full_series_leaves_every_episode_as_a_named_source(fake_client, tmp_path):
    await p.podcast_series("nb-1", episodes=EPS3, output_dir=str(tmp_path), start=1)
    # Complete, name-matched record: every episode (incl. the last) self-uploaded.
    assert fake_client.sources.titles() == ["EP01 心法篇", "EP02 實戰篇", "EP03 收尾篇"]


async def test_resume_keeps_prior_sources_and_does_not_re_upload_them(fake_client, tmp_path):
    # Simulate a prior process run: EP01 + EP02 already exist as server-side sources.
    fake_client.sources.seed("EP01 心法篇", "EP02 實戰篇")

    await p.podcast_series("nb-1", episodes=EPS3, output_dir=str(tmp_path), start=3)

    # Prior episodes remain present (so generation still sees them for continuity),
    # and only EP03 is uploaded on resume — no redundant re-upload of EP01/EP02.
    assert fake_client.sources.titles() == ["EP01 心法篇", "EP02 實戰篇", "EP03 收尾篇"]
    add_files = [c for c in fake_client.sources.calls if c[0] == "add_file"]
    assert len(add_files) == 1


async def test_resume_preserves_existing_season_manifest(fake_client, tmp_path):
    # A prior run recorded episodes 1 & 2 in the season manifest.
    (tmp_path / "series_manifest.json").write_text(
        json.dumps({"notebook_id": "nb-1", "episodes": [{"episode": 1}, {"episode": 2}]}),
        encoding="utf-8",
    )
    out = await p.podcast_series("nb-1", episodes=EPS3, output_dir=str(tmp_path), start=3)
    # The CALL returns only the newly-run episode...
    assert [e["episode"] for e in out["episodes"]] == [3]
    # ...but the season manifest keeps the full record 1,2,3 (not overwritten).
    manifest = json.loads((tmp_path / "series_manifest.json").read_text(encoding="utf-8"))
    assert [e["episode"] for e in manifest["episodes"]] == [1, 2, 3]


async def test_resume_with_corrupt_manifest_fails_clearly(fake_client, tmp_path):
    # A truncated / corrupt local manifest must surface a clear ValueError on
    # resume (not a raw JSONDecodeError leaking the parse position), and must not
    # start any generation before the failure.
    (tmp_path / "series_manifest.json").write_text("{not valid json", encoding="utf-8")
    eps = [{"title": "心法篇", "brief": "1"}, {"title": "實戰篇", "brief": "2"}]
    with pytest.raises(ValueError, match="corrupt"):
        await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=2)
    assert fake_client.artifacts.calls == []


async def test_resume_with_non_object_manifest_fails_clearly(fake_client, tmp_path):
    # Valid JSON but the wrong shape (a list, not an object) would AttributeError
    # on data.get(...); it must also surface a clear ValueError instead.
    (tmp_path / "series_manifest.json").write_text("[1, 2, 3]", encoding="utf-8")
    eps = [{"title": "心法篇", "brief": "1"}, {"title": "實戰篇", "brief": "2"}]
    with pytest.raises(ValueError, match="corrupt"):
        await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=2)
    assert fake_client.artifacts.calls == []


async def test_series_rejects_invalid_start(fake_client, tmp_path):
    eps = [{"title": "心法篇", "brief": "1"}]
    with pytest.raises(ValueError, match="start must be >= 1"):
        await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=0)
    with pytest.raises(ValueError, match="start must be <="):
        await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path), start=2)
    # No generation happened for either invalid start.
    assert fake_client.artifacts.calls == []


@pytest.mark.parametrize("bad_wait_timeout", [float("nan"), 0, -5])
async def test_podcast_episode_resume_rejects_an_unsafe_wait_timeout(
    fake_client, tmp_path, bad_wait_timeout
):
    """T7:`podcast_episode_resume` 是四個吃 `wait_timeout` 的公開入口裡唯一沒過
    `_validate_wait_timeout` 的——`nan` 存進 `dispatch["wait_timeout"]` 後,
    `timedelta(seconds=nan)` 會在往後**每一次**對帳時炸掉,那顆 attempt 永久對帳
    不了;`0`/負數則直流 `wait_for_completion` 永不逾時。壞參數必須在任何遠端
    副作用之前秒退。
    """
    with pytest.raises(ValueError, match="wait_timeout must be a finite number greater than zero"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="art-1",
            output_dir=str(tmp_path),
            wait_timeout=bad_wait_timeout,
        )
    assert fake_client.artifacts.calls == []
