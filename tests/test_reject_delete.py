"""Coverage for generic source deletion plus fresh standalone generation.

This compatibility case has no prior manifest-backed completed episode and does
not authorize implicit regeneration or replacement of durable output.
"""
import pytest

from notebooklm_mcp import tools_basic as t
from notebooklm_mcp import tools_podcast as p


async def test_source_delete_removes_the_source(fake_client):
    fake_client.sources.seed("EP01")
    sid = fake_client.sources.sources[0]["id"]
    out = await t.source_delete("nb-1", sid)
    assert out["deleted"] == sid
    assert fake_client.sources.titles() == []


async def test_source_delete_rejects_an_id_from_another_notebook_before_mutation(
    fake_client, monkeypatch
):
    """DELETE_SOURCE 只送 source_id；公開工具必須在破壞性 RPC 前驗歸屬。"""
    fake_client.sources.seed("別本筆記的來源")
    elsewhere_id = fake_client.sources.sources[0]["id"]

    async def requested_notebook_sources(notebook_id):
        assert notebook_id == "nb-requested"
        return []

    monkeypatch.setattr(fake_client.sources, "list", requested_notebook_sources)

    with pytest.raises(ValueError, match="not in notebook"):
        await t.source_delete("nb-requested", elsewhere_id)

    assert fake_client.sources.titles() == ["別本筆記的來源"]
    assert not [call for call in fake_client.sources.calls if call[0] == "delete"]


async def test_delete_source_then_generate_fresh_standalone_episode(fake_client, tmp_path):
    # Existing remote sources, but no manifest-backed EP02 output.
    fake_client.sources.seed("Seed", "EP01 心法篇", "EP02 實戰篇")
    ep02_id = next(s["id"] for s in fake_client.sources.sources if s["title"] == "EP02 實戰篇")

    # Generic deletion and a separate fresh generation remain compatible.
    await t.source_delete("nb-1", ep02_id)
    assert fake_client.sources.titles() == ["EP01 心法篇", "Seed"]

    # EP01 is already a source and must not be duplicated.
    await p.podcast_episode("nb-1", episode_n=2, title="實戰篇", brief="重生第二集", output_dir=str(tmp_path))

    assert fake_client.sources.titles() == ["EP01 心法篇", "EP02 實戰篇", "Seed"]
    add_files = [c for c in fake_client.sources.calls if c[0] == "add_file"]
    assert len(add_files) == 1  # only the regenerated EP02 self-uploaded
