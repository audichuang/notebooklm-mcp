"""Coverage for generic source deletion plus fresh standalone generation.

This compatibility case has no prior manifest-backed completed episode and does
not authorize implicit regeneration or replacement of durable output.
"""
import pytest

from notebooklm_mcp import tools_basic as t, tools_podcast as p


async def test_source_delete_removes_the_source(fake_client):
    fake_client.sources.seed("EP01")
    sid = fake_client.sources.sources[0]["id"]
    out = await t.source_delete("nb-1", sid)
    assert out == {"deleted": sid, "was_present": True}
    assert fake_client.sources.titles() == []


async def test_source_delete_replayed_after_success_stays_idempotent(fake_client):
    """**清理迴圈要能重放。** `podcast_attempt_retract` 的契約是「把回傳的
    stale_source_ids 逐一 source_delete」,而 response 遺失後 host 重放整個迴圈是預期
    操作。對已經刪掉的那一筆拋錯會讓自動化 host 停在半路,剩下的 id 從此沒人刪
    (`_assert_source_cleanup_done` 只列「還在」的 id,不會替它補回來)。"""
    fake_client.sources.seed("EP01")
    sid = fake_client.sources.sources[0]["id"]
    await t.source_delete("nb-1", sid)
    delete_calls = len([c for c in fake_client.sources.calls if c[0] == "delete"])

    replay = await t.source_delete("nb-1", sid)

    assert replay == {"deleted": sid, "was_present": False}
    # 重放不得再發一次 destructive RPC。
    assert len([c for c in fake_client.sources.calls if c[0] == "delete"]) == delete_calls


async def test_source_delete_never_touches_an_id_from_another_notebook(
    fake_client, monkeypatch
):
    """DELETE_SOURCE 只送 source_id；歸屬無法確認時**不發那個 RPC**。

    這是 fail-loud 換成 `was_present=False` 之後仍然必須成立的那個安全性質:別本筆記本
    的來源不能因為呼叫端打錯 notebook 就被刪掉。"""
    fake_client.sources.seed("別本筆記的來源")
    elsewhere_id = fake_client.sources.sources[0]["id"]

    async def requested_notebook_sources(notebook_id):
        assert notebook_id == "nb-requested"
        return []

    monkeypatch.setattr(fake_client.sources, "list", requested_notebook_sources)

    out = await t.source_delete("nb-requested", elsewhere_id)

    assert out == {"deleted": elsewhere_id, "was_present": False}
    assert fake_client.sources.titles() == ["別本筆記的來源"]
    assert not [call for call in fake_client.sources.calls if call[0] == "delete"]


async def test_source_delete_propagates_a_failed_list_instead_of_faking_absence(
    fake_client, monkeypatch
):
    """preflight 打不通時不准把它當成「本來就不在」—— 那會靜默吞掉一筆清理義務。"""
    fake_client.sources.seed("EP01")
    sid = fake_client.sources.sources[0]["id"]

    async def dead_auth(notebook_id):
        raise RuntimeError("auth is dead")

    monkeypatch.setattr(fake_client.sources, "list", dead_auth)

    with pytest.raises(RuntimeError, match="auth is dead"):
        await t.source_delete("nb-1", sid)

    assert fake_client.sources.titles() == ["EP01"]
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
