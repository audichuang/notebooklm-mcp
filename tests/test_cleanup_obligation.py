"""`cleanup_obligation` 模組的介面層測試:只用假的 sources 清單,不經 tools_podcast。"""

from types import SimpleNamespace

import pytest

from notebooklm_mcp.cleanup_obligation import (
    assert_generation_allowed,
    drop_cleanup_obligations,
    list_obligations,
    queue_pending_source_cleanup,
    record_cleanup_obligation,
)
from notebooklm_mcp.manifest_store import ManifestStore


def _store(tmp_path, pending, *, notebook_id="nb-1"):
    store = ManifestStore(tmp_path / "m.json")
    episode = {"episode": 1, "notebook_id": notebook_id, "pending_source_cleanup": pending}
    store.update(lambda m: m["episodes"].append(episode))
    return store


def _lister(ids, calls):
    async def list_sources(notebook_id):
        calls.append(notebook_id)
        return [SimpleNamespace(id=i) for i in ids]

    return list_sources


def test_record_dedups_and_carries_identity():
    ep: dict = {}
    record_cleanup_obligation(ep, "s1", "nb-1")
    record_cleanup_obligation(ep, "s1", "nb-other")
    assert ep["pending_source_cleanup"] == [{"source_id": "s1", "notebook_id": "nb-1"}]


def test_legacy_string_rows_take_fallback_and_unknown_stays_none():
    ep = {"pending_source_cleanup": ["old", {"source_id": "new", "notebook_id": "nb-2"}]}
    assert list_obligations(ep, fallback_notebook="nb-1") == [
        {"source_id": "old", "notebook_id": "nb-1"},
        {"source_id": "new", "notebook_id": "nb-2"},
    ]
    assert list_obligations(ep)[0]["notebook_id"] is None


def test_drop_removes_key_when_empty_and_queue_returns_all_open():
    ep: dict = {}
    assert queue_pending_source_cleanup(
        ep, ["a", "b", "a", "keep"], exclude="keep", notebook_id="nb-1"
    ) == [
        "a",
        "b",
    ]
    drop_cleanup_obligations(ep, {"a"})
    assert [r["source_id"] for r in ep["pending_source_cleanup"]] == ["b"]
    drop_cleanup_obligations(ep, {"b"})
    assert "pending_source_cleanup" not in ep


async def test_no_obligation_never_lists(tmp_path):
    store = ManifestStore(tmp_path / "m.json")
    calls: list = []
    await assert_generation_allowed(_lister([], calls), store, "nb-1", 1)
    assert calls == []


async def test_live_source_blocks_and_absent_source_is_discharged(tmp_path):
    store = _store(tmp_path, [{"source_id": "s1", "notebook_id": "nb-1"}])
    calls: list = []
    with pytest.raises(ValueError, match="s1"):
        await assert_generation_allowed(_lister(["s1"], calls), store, "nb-1", 1)
    assert store.read()["episodes"][0]["pending_source_cleanup"]  # 還在就不結案

    await assert_generation_allowed(_lister([], calls), store, "nb-1", 1)
    assert "pending_source_cleanup" not in store.read()["episodes"][0]
    assert calls == ["nb-1", "nb-1"]  # 每次呼叫只 list 一次


async def test_unknown_identity_fails_closed_before_listing(tmp_path):
    store = _store(tmp_path, ["legacy"], notebook_id=None)
    calls: list = []
    with pytest.raises(ValueError, match="沒有可核對的 notebook 身分"):
        await assert_generation_allowed(_lister([], calls), store, "nb-1", 1)
    assert calls == []


async def test_other_notebook_obligation_is_not_discharged_here(tmp_path):
    store = _store(tmp_path, [{"source_id": "s1", "notebook_id": "nb-1"}])
    calls: list = []
    with pytest.raises(ValueError, match="清理義務屬於 notebook"):
        await assert_generation_allowed(_lister([], calls), store, "nb-2", 1)
    assert calls == []
    assert store.read()["episodes"][0]["pending_source_cleanup"]
