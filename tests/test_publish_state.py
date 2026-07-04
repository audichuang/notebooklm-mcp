import json
from notebooklm_mcp.publish import state


def test_episode_guid_is_stable_and_source_decoupled():
    g1 = state.episode_guid("ai-news", 1)
    g2 = state.episode_guid("ai-news", 1)
    assert g1 == g2
    assert g1 != state.episode_guid("ai-news", 2)
    assert g1 != state.episode_guid("other", 1)


def test_load_show_returns_none_when_absent(tmp_path):
    assert state.load_show(str(tmp_path)) is None


def test_save_then_load_round_trips(tmp_path):
    show = {
        "show_id": "ai-news",
        "token": "tok",
        "notebook_id": "nb1",
        "title": "AI 新聞",
        "episodes": {"1": {"title": "EP1", "guid": "g1", "tombstone": False}},
    }
    state.save_show(str(tmp_path), show)
    loaded = state.load_show(str(tmp_path))
    assert loaded == show
    # File is real JSON, UTF-8, human-readable.
    raw = json.loads((tmp_path / "show.json").read_text(encoding="utf-8"))
    assert raw["title"] == "AI 新聞"


def test_save_show_is_atomic_no_tmp_left(tmp_path):
    state.save_show(str(tmp_path), {"show_id": "x", "episodes": {}})
    import os
    assert [n for n in os.listdir(tmp_path) if n.startswith(".tmp-")] == []
