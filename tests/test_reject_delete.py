"""Coverage for the reject-then-delete flow (source_delete + regenerate).

When an episode is rejected, the operator deletes its source so the bad episode
does not poison the next episode's continuity, then regenerates. Because prior
episodes are already server-side sources, the regen needs NO prior_mp3_path —
generation sees the priors automatically, and the new episode self-uploads.
"""
from notebooklm_mcp import tools_basic as t
from notebooklm_mcp import tools_podcast as p


async def test_source_delete_removes_the_source(fake_client):
    fake_client.sources.seed("EP01")
    sid = fake_client.sources.sources[0]["id"]
    out = await t.source_delete("nb-1", sid)
    assert out["deleted"] == sid
    assert fake_client.sources.titles() == []


async def test_reject_episode_then_regenerate(fake_client, tmp_path):
    # A finished notebook: Seed + EP01 + EP02 as server-side sources.
    fake_client.sources.seed("Seed", "EP01", "EP02")
    ep02_id = next(s["id"] for s in fake_client.sources.sources if s["title"] == "EP02")

    # Reject EP02: delete its source.
    await t.source_delete("nb-1", ep02_id)
    assert fake_client.sources.titles() == ["EP01", "Seed"]

    # Regenerate EP02. EP01 is already a source (continuity automatic), so no
    # prior_mp3_path is needed and EP01 must NOT be duplicated.
    await p.podcast_episode("nb-1", episode_n=2, brief="重生第二集", output_dir=str(tmp_path))

    assert fake_client.sources.titles() == ["EP01", "EP02", "Seed"]
    add_files = [c for c in fake_client.sources.calls if c[0] == "add_file"]
    assert len(add_files) == 1  # only the regenerated EP02 self-uploaded
