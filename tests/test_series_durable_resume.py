"""PR5：整季工具從 durable attempt state 安全續跑。"""

import json

import pytest

from notebooklm_mcp import tools_podcast as p


EPS3 = [
    {"title": "心法篇", "brief": "1"},
    {"title": "實戰篇", "brief": "2"},
    {"title": "收尾篇", "brief": "3"},
]


def _generate_briefs(fake_client, start: int = 0) -> list[str]:
    return [
        call[1]["instructions"]
        for call in fake_client.artifacts.calls[start:]
        if call[0] == "generate_audio"
    ]



async def test_safe_next_action_vocabulary_names_public_mcp_tools():
    assert p.SAFE_NEXT_ACTIONS == {
        "podcast_attempt_adopt",
        # 來源筆數守門的停點:series 生不出帶 `source_ids` 的 settings,所以下一步
        # 只能換工具。指回 `podcast_series` 會叫呼叫端撞回同一道牆,而 `attempt_count`
        # 兩輪都不變 —— 正是 `partial()` 註解說的「看不出自己在原地打轉」。
        "podcast_episode",
        "podcast_episode_reconcile",
        # 權限停點的下一步。`error` 說去補分享而 `safe_next_action` 卻回 podcast_series
        # 的話,只讀後者的自動化會原地重試同一個沒權限的帳號 —— 同一份回傳的兩個欄位
        # 互相矛盾。白名單的意義是「一定是真的 MCP 工具名」,這支正是。
        "notebook_share_with_pool",
        "podcast_episode_resume",
        # 筆數守門撞上既有 active attempt 時的停點:那顆 attempt 的 settings 是
        # 「不指名來源」,指回 `podcast_episode` 會被 `_is_resendable_same_request`
        # 拒絕、不指名又撞回守門 —— 得先 tombstone 才走得出去。
        "podcast_attempt_retract",
        "podcast_series",
        "source_delete",
    }
    # 白名單的意義是「一定是真的 MCP 工具名」,不是「一定在本模組」——source_delete 住在
    # tools_basic,所以對真正的工具註冊表驗,而不是對模組屬性。
    from notebooklm_mcp import app

    registered = {tool.name for tool in await app.mcp.list_tools()}
    assert p.SAFE_NEXT_ACTIONS <= registered



async def _complete_episode(fake_client, tmp_path, episode_n: int) -> None:
    episode = EPS3[episode_n - 1]
    await p.podcast_episode(
        "nb-1",
        episode_n=episode_n,
        title=episode["title"],
        brief=episode["brief"],
        output_dir=str(tmp_path),
        manifest_path=str(tmp_path / "series_manifest.json"),
    )


async def test_default_start_skips_completed_attempts_and_generates_only_ep3(
    fake_client, tmp_path
):
    await _complete_episode(fake_client, tmp_path, 1)
    await _complete_episode(fake_client, tmp_path, 2)
    call_boundary = len(fake_client.artifacts.calls)

    out = await p.podcast_series(
        "nb-1", episodes=EPS3, output_dir=str(tmp_path), start=1
    )

    assert _generate_briefs(fake_client, call_boundary) == ["3"]
    assert [episode["episode"] for episode in out["episodes"]] == [3]


async def test_start_two_skips_completed_ep2_and_generates_only_ep3(
    fake_client, tmp_path
):
    await _complete_episode(fake_client, tmp_path, 2)
    call_boundary = len(fake_client.artifacts.calls)

    out = await p.podcast_series(
        "nb-1", episodes=EPS3, output_dir=str(tmp_path), start=2
    )

    assert _generate_briefs(fake_client, call_boundary) == ["3"]
    assert [episode["episode"] for episode in out["episodes"]] == [3]


async def test_completed_episode_with_missing_feedback_source_stops_series(
    fake_client, tmp_path
):
    await _complete_episode(fake_client, tmp_path, 1)
    fake_client.sources.sources.clear()
    call_boundary = len(fake_client.artifacts.calls)

    out = await p.podcast_series(
        "nb-1", episodes=EPS3[:2], output_dir=str(tmp_path), start=1
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 1
    assert out["observed_state"] == "continuity_unverified"
    assert out["safe_next_action"] == "podcast_attempt_adopt"
    assert _generate_briefs(fake_client, call_boundary) == []


async def test_start_three_does_not_validate_earlier_plan_entries(
    fake_client, tmp_path
):
    out = await p.podcast_series(
        "nb-1",
        episodes=[None, {"ignored": True}, EPS3[2]],
        output_dir=str(tmp_path),
        start=3,
    )

    assert out["complete"] is True
    assert _generate_briefs(fake_client) == ["3"]


async def test_series_treats_flat_v1_artifact_as_legacy_completed_output(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    legacy_source_id = fake_client.sources._add("EP01 心法篇", kind="media")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "notebook_id": "nb-1",
                "episodes": [
                    {
                        "episode": 1,
                        "title": "心法篇",
                        "artifact_id": "legacy-artifact",
                        "mp3_path": str(tmp_path / "legacy-ep01.mp3"),
                        "feedback_source_id": legacy_source_id,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "legacy-ep01.mp3").write_bytes(b"legacy audio")

    out = await p.podcast_series(
        "nb-1", episodes=EPS3[:2], output_dir=str(tmp_path), start=1
    )

    assert _generate_briefs(fake_client) == ["2"]
    assert [episode["episode"] for episode in out["episodes"]] == [2]
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy = stored["episodes"][0]
    assert legacy["artifact_id"] == "legacy-artifact"
    assert legacy.get("attempts", []) == []
    assert "output_attempt_id" not in legacy


async def test_unverified_legacy_stub_stops_before_next_episode(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "notebook_id": "nb-1",
                "episodes": [
                    {
                        "episode": 1,
                        "title": "心法篇",
                        "artifact_id": "legacy-artifact",
                        "mp3_path": str(tmp_path / "unlinked-ep01.mp3"),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "unlinked-ep01.mp3").write_bytes(b"legacy audio")
    fake_client.sources._add("EP01 心法篇", kind="media")

    out = await p.podcast_series(
        "nb-1", episodes=EPS3[:2], output_dir=str(tmp_path), start=1
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 1
    assert out["attempt_id"] is None
    assert out["observed_state"] == "legacy_output_unverified"
    assert out["safe_next_action"] == "podcast_attempt_adopt"
    assert out["artifact_id"] == "legacy-artifact"
    assert _generate_briefs(fake_client) == []


async def test_partial_legacy_output_never_regenerates_or_overwrites_audio(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    mp3_path = tmp_path / "ep19.mp3"
    original_audio = b"already published EP19"
    mp3_path.write_bytes(original_audio)
    manifest_path.write_text(
        json.dumps(
            {
                "notebook_id": "nb-1",
                "episodes": [
                    {
                        "episode": 19,
                        "title": "既有第十九集",
                        "label": "EP19 既有第十九集",
                        "mp3_path": str(mp3_path),
                        "published_at": "Fri, 24 Jul 2026 09:00:00 +0800",
                        "description": "已發布過的 show notes",
                        "cover_path": str(tmp_path / "ep19.jpg"),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    episodes = [None] * 18 + [
        {"title": "既有第十九集", "brief": "不得靜默重生"}
    ]

    out = await p.podcast_series(
        "nb-1",
        episodes=episodes,
        output_dir=str(tmp_path),
        start=19,
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 19
    assert out["observed_state"] == "legacy_output_unverified"
    assert out["safe_next_action"] == "podcast_attempt_adopt"
    assert _generate_briefs(fake_client) == []
    assert mp3_path.read_bytes() == original_audio
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["description"] == "已發布過的 show notes"
    assert stored["episodes"][0].get("attempts", []) == []


async def test_acceptance_unknown_stops_series_without_generating_ep2_or_ep3(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=2,
            title=EPS3[1]["title"],
            brief=EPS3[1]["brief"],
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )
    fake_client.artifacts.generate_audio_exc = None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = manifest["episodes"][0]["active_attempt_id"]
    call_boundary = len(fake_client.artifacts.calls)

    out = await p.podcast_series(
        "nb-1", episodes=EPS3, output_dir=str(tmp_path), start=2
    )

    assert _generate_briefs(fake_client, call_boundary) == []
    assert out["complete"] is False
    assert out["stopped_at_episode"] == 2
    assert out["attempt_id"] == attempt_id
    assert out["observed_state"] == "acceptance_unknown"
    # F3(主迴圈裁決,採納審查者的反駁):dispatch 剛發生、候選窗還沒關,重呼
    # reconcile 未必是死結——晚幾分鐘可能撈得到,retract 反而會把還在飛的因果
    # 紀錄提前寫成墓碑(ADR-0009)。series 重包這個停點時(P2 修法)也要原樣帶出
    # 這個 safe_next_action,不能停在舊值或漏轉。
    assert out["safe_next_action"] == "podcast_episode_reconcile"
    assert "next_step" in out, "series 重包時不能把 next_step 漏傳"


async def test_first_call_not_accepted_returns_structured_safe_stop(
    fake_client, tmp_path
):
    fake_client.artifacts.fail_generate = True

    out = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 1
    assert out["observed_state"] == "not_accepted"
    assert out["safe_next_action"] == "podcast_series"
    stored = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    assert stored["episodes"][0]["attempts"][0]["dispatch"]["status"] == "not_accepted"
    attempt = stored["episodes"][0]["attempts"][0]
    attempt_id = attempt["attempt_id"]
    errors = list(attempt["errors"])
    assert len(_generate_briefs(fake_client)) == 1

    fake_client.artifacts.fail_generate = False
    resumed = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )

    final = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    episode = final["episodes"][0]
    assert resumed["complete"] is True
    assert episode["output_attempt_id"] == attempt_id
    assert len(episode["attempts"]) == 1
    assert episode["attempts"][0]["attempt_id"] == attempt_id
    assert episode["attempts"][0]["dispatch"]["status"] == "accepted"
    assert episode["attempts"][0]["errors"] == errors
    assert len(_generate_briefs(fake_client)) == 2


async def test_series_retries_same_prepared_attempt_after_pre_dispatch_crash(
    fake_client, tmp_path, monkeypatch
):
    original_claim = p._claim_prepared_dispatch
    crashed = False

    def crash_once(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise ConnectionError("crash before dispatch")
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(p, "_claim_prepared_dispatch", crash_once)
    first = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )

    manifest_path = tmp_path / "series_manifest.json"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = stored["episodes"][0]["active_attempt_id"]
    assert first["complete"] is False
    assert first["observed_state"] == "prepared"
    assert _generate_briefs(fake_client) == []

    resumed = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )

    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = final["episodes"][0]
    assert resumed["complete"] is True
    assert len(episode["attempts"]) == 1
    assert episode["output_attempt_id"] == attempt_id
    assert episode["attempts"][0]["dispatch"]["status"] == "accepted"
    assert _generate_briefs(fake_client) == ["1"]


async def test_terminal_failed_attempt_is_superseded_on_next_series_call(
    fake_client, tmp_path
):
    fake_client.artifacts.fail_complete = True
    first = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )
    assert first["complete"] is False
    assert first["observed_state"] == "failed"
    assert first["safe_next_action"] == "podcast_series"
    assert len(_generate_briefs(fake_client)) == 1

    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(manifest_path)
    store.update(
        lambda manifest: manifest["episodes"][0].update(
            {"cover_path": "/existing/episode-cover.png"}
        )
    )
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_attempt_id = stored["episodes"][0]["active_attempt_id"]

    fake_client.artifacts.fail_complete = False
    resumed = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )

    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = final["episodes"][0]
    assert resumed["complete"] is True
    assert len(episode["attempts"]) == 2
    old_attempt, new_attempt = episode["attempts"]
    assert old_attempt["attempt_id"] == old_attempt_id
    assert old_attempt["remote"]["status"] == "failed"
    assert new_attempt["attempt_id"] != old_attempt_id
    assert new_attempt["supersedes_attempt_id"] == old_attempt_id
    assert episode["output_attempt_id"] == new_attempt["attempt_id"]
    assert len(_generate_briefs(fake_client)) == 2


async def test_series_repairs_completed_artifact_and_source_title_drift(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    first = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="1",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    artifact = next(
        row
        for row in fake_client.artifacts.artifacts
        if row.id == first["artifact_id"]
    )
    source = next(
        row
        for row in fake_client.sources.sources
        if row["id"] == first["feedback_source_id"]
    )
    artifact.title = "被改掉的 artifact 名稱"
    source["title"] = "被改掉的 source 名稱"
    artifact_boundary = len(fake_client.artifacts.calls)
    source_boundary = len(fake_client.sources.calls)

    resumed = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )

    assert resumed["complete"] is True
    assert artifact.title == "EP01 心法篇"
    assert source["title"] == "EP01 心法篇"
    assert [call[0] for call in fake_client.artifacts.calls[artifact_boundary:]].count(
        "rename"
    ) == 1
    assert [
        call
        for call in fake_client.sources.calls[source_boundary:]
        if call[0] == "add_file"
    ] == []


async def test_series_rejects_manifest_from_another_notebook(
    fake_client, tmp_path
):
    await _complete_episode(fake_client, tmp_path, 1)
    call_boundary = len(fake_client.artifacts.calls)

    with pytest.raises(ValueError, match="different notebook|另一個 notebook"):
        await p.podcast_series(
            "nb-2",
            episodes=EPS3,
            output_dir=str(tmp_path),
            start=1,
        )

    assert _generate_briefs(fake_client, call_boundary) == []
    manifest = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["notebook_id"] == "nb-1"


async def test_adopt_explicit_source_id_migrates_legacy_output_without_upload(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    mp3_path = tmp_path / "legacy-ep01.mp3"
    mp3_path.write_bytes(b"legacy audio")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "notebook_id": "nb-1",
                "episodes": [
                    {
                        "episode": 1,
                        "title": "心法篇",
                        "artifact_id": "legacy-artifact",
                        "mp3_path": str(mp3_path),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source_id = fake_client.sources._add("EP01 心法篇", kind="media")
    source_boundary = len(fake_client.sources.calls)

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        feedback_source_id=source_id,
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert adopted["feedback_source_id"] == source_id
    assert stored["episodes"][0]["feedback_source_id"] == source_id
    assert stored["episodes"][0].get("attempts", []) == []

    out = await p.podcast_series(
        "nb-1", episodes=EPS3[:1], output_dir=str(tmp_path)
    )
    assert out["complete"] is True
    assert _generate_briefs(fake_client) == []
    assert [
        call
        for call in fake_client.sources.calls[source_boundary:]
        if call[0] in {"add_file", "rename"}
    ] == []


async def test_series_raised_rate_limit_stops_as_not_accepted(fake_client, tmp_path):
    """整季路徑的同一條契約(0.8.0 #1342):配額拒絕 → not_accepted + 可直接重跑整季。

    這裡的損害比單集版更明顯:回 acceptance_unknown 會讓自動化 host 依
    `safe_next_action` 去跑 podcast_episode_reconcile,而那支必然回報「沒有這個
    artifact」,整季就卡在一個根本不存在的疑點上。
    """
    from notebooklm.exceptions import RateLimitError

    fake_client.artifacts.generate_audio_exc = RateLimitError("每日配額已用盡")

    out = await p.podcast_series("nb-1", episodes=EPS3[:1], output_dir=str(tmp_path))

    assert out["complete"] is False
    assert out["observed_state"] == "not_accepted"
    assert out["safe_next_action"] == "podcast_series"
    stored = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "not_accepted"
    assert "每日配額已用盡" in attempt["remote"]["error"]
