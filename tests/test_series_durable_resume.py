"""PR5：整季工具從 durable attempt state 安全續跑。"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from notebooklm.types import ArtifactType

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


async def test_new_episode_transient_error_during_unresolved_upload_directs_via_caps(
    fake_client, tmp_path
):
    """T1:全新一集第一次 dispatch 時,回錄 source 上傳卡在 acceptance_unknown,途中又
    撞 transient transport error——series 的「brand-new episode」分支那個
    `_TRANSIENT_TRANSPORT_ERRORS` handler過去無條件回 `ACTION_SERIES`(整季重跑),
    現在改走 caps:can_resume 為真時該教 `podcast_episode_resume` 續完 finalize,不是
    重跑整季(V-A2 R2-P2b)。"""
    fake_client.sources.add_file_exc_after_create = ConnectionError(
        "network blip during feedback source upload"
    )

    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )

    assert out["complete"] is False
    assert out["observed_state"] == "acceptance_unknown"
    assert out["safe_next_action"] == "podcast_episode_resume"
    assert "next_step" in out
    assert out["candidate_source_ids"] == []


async def test_series_transient_error_during_ambiguous_reconciliation_directs_via_caps(
    fake_client, tmp_path, monkeypatch
):
    """T1:已經停在 `reconciliation_ambiguous` 的 attempt,series 續跑時
    `sources.list` 撞 transient transport error——active-attempt 分支的
    `_TRANSIENT_TRANSPORT_ERRORS` handler 過去無條件回 `ACTION_SERIES` 且不帶候選,
    現在改走 caps,答案與(不撞 transient error 時)`except RuntimeError:` 那格一致:
    都是 `podcast_attempt_adopt` + 候選。"""
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.sources.add_file_exc_after_create = TimeoutError(
        "upload response lost"
    )
    with pytest.raises(TimeoutError, match="upload response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )
    fake_client.sources.add_file_exc_after_create = None
    fake_client.sources._add("ep01.mp3", kind="media")

    stopped = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )
    assert stopped["observed_state"] == "reconciliation_ambiguous"
    candidates = stopped["candidate_source_ids"]
    assert len(candidates) == 2

    async def transport_failure(_notebook_id):
        raise ConnectionError("network blip during reconciliation")

    monkeypatch.setattr(fake_client.sources, "list", transport_failure)

    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )

    assert out["complete"] is False
    assert out["observed_state"] == "reconciliation_ambiguous"
    assert out["safe_next_action"] == "podcast_attempt_adopt"
    assert out["candidate_source_ids"] == candidates
    assert "next_step" in out


async def test_attempt_backed_adopt_with_matching_title_directs_via_caps(
    fake_client, tmp_path
):
    """T2:`needs_rename=False`(候選標題已經對)時,`safe_next_action` 必須跟 caps
    一致——舊版寫死 `podcast_series`,但 finalize 還沒 promote 成 output,照做 host
    直接 publish 會撞 `_ensure_local_mp3`(缺 mp3_path/artifact_id)。caps 給的是
    `podcast_episode_resume`(續完 finalize),而且要帶得出 resume 需要的 artifact_id
    ——舊版回傳完全沒有 `next_step`/`safe_next_artifact_id` 這兩個欄位。"""
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.sources.add_file_exc_after_create = TimeoutError(
        "upload response lost"
    )
    with pytest.raises(TimeoutError, match="upload response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )
    fake_client.sources.add_file_exc_after_create = None
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = stored["episodes"][0]["active_attempt_id"]
    # 候選一開始就用正確的標題建立——不必經過 reconciliation_ambiguous 那條
    # rename-required 的路,直接驗「標題已經對」這一格。
    replacement = fake_client.sources._add("EP01 心法篇", kind="media")

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        attempt_id=attempt_id,
        feedback_source_id=replacement,
    )

    assert adopted["observed_state"] == "continuity_verified"
    assert adopted["complete"] is False
    assert adopted["safe_next_action"] == "podcast_episode_resume"
    assert "next_step" in adopted and adopted["next_step"]
    assert adopted["safe_next_attempt_id"] == attempt_id
    assert adopted["safe_next_artifact_id"] == "task-123"
    assert "stale_source_ids" not in adopted


async def test_attempt_backed_adopt_needing_rename_directs_via_caps(
    fake_client, tmp_path
):
    """T2:`needs_rename=True` 只能經由 ambiguity 擇一放行(guard 逼的),而 ambiguous
    定義上至少兩個候選——擇一之後**必然**剩下未選中的同名候選要清,所以
    `safe_next_action` 會被 stale-source 覆寫成 `source_delete`。這正是 T2 的另一個
    修復點:覆寫之後 `next_step` 不能只講清理、把 caps 原本的建議(resume)吃掉
    ——那會變成兩個欄位對同一個狀態指不同工具(紅線①)。清理句子必須**排在前面**、
    caps 的話接在後面,兩者都要看得到。"""
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.sources.add_file_exc_after_create = TimeoutError(
        "upload response lost"
    )
    with pytest.raises(TimeoutError, match="upload response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )
    fake_client.sources.add_file_exc_after_create = None
    # 補一筆同名(以檔名為準,未 rename 過)的候選,湊出 reconciliation_ambiguous。
    fake_client.sources._add("ep01.mp3", kind="media")
    stopped = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )
    assert stopped["observed_state"] == "reconciliation_ambiguous"
    attempt_id = stopped["attempt_id"]
    candidate = stopped["candidate_source_ids"][0]

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        attempt_id=attempt_id,
        feedback_source_id=candidate,
    )

    assert adopted["observed_state"] == "accepted"
    assert adopted["complete"] is False
    assert adopted["stale_source_ids"], "未選中的同名候選必須進清理義務"
    assert adopted["safe_next_action"] == "source_delete"
    assert "next_step" in adopted and adopted["next_step"]
    assert "source_delete" in adopted["next_step"]
    # caps 原本教的續完 finalize 不能被覆寫掉——先刪、刪完之後照這句做。
    assert "podcast_episode_resume" in adopted["next_step"]
    assert adopted["safe_next_attempt_id"] == attempt_id
    assert adopted["safe_next_artifact_id"] == "task-123"


async def test_series_retract_of_a_middle_episode_does_not_regenerate_reading_the_whole_notebook(
    fake_client, tmp_path
):
    """T3(P1,`scratchpad/verify-A1/r2_series_retract.py` 的序列):整季跑完 →
    retract 中段集(EP02)→ 刪完 stale source → 照 safe_next_action 重呼
    podcast_series。舊行為:EP02 沒有 output,`_regeneration_entry_point` 對 series
    自己建的 settings 回 `ACTION_SERIES`,series 就對 EP02 全新一集 dispatch——不指名
    來源 = 讀整本筆記本,把 EP03 的回錄音檔洩進 EP02。

    修法落點:每集迴圈頂端、`_assert_source_cleanup_done` 之後,一旦發現「本集無
    output,但存在 episode > n 已有 output 的列」就停下,回 `podcast_episode`
    (不指名來源會靜默讀整本筆記本,這裡改成明講要指名)。"""
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.sources.seed("整季講義")

    first = await p.podcast_series(
        "nb-1", episodes=EPS3, output_dir=str(tmp_path), start=1
    )
    assert first["complete"] is True

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    ep2 = next(row for row in stored["episodes"] if row["episode"] == 2)
    ep2_attempt_id = ep2["output_attempt_id"]

    retracted = await p.podcast_attempt_retract(
        str(manifest_path), 2, ep2_attempt_id, reason="QA 拒收 EP02"
    )
    for obligation in retracted["source_cleanup_obligations"]:
        await fake_client.sources.delete(
            obligation["notebook_id"], obligation["source_id"]
        )

    generate_boundary = len(
        [c for c in fake_client.artifacts.calls if c[0] == "generate_audio"]
    )

    out = await p.podcast_series(
        "nb-1", episodes=EPS3, output_dir=str(tmp_path), start=1
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 2
    assert out["observed_state"] == "later_episode_has_output"
    assert out["safe_next_action"] == "podcast_episode"
    assert "next_step" in out and "source_ids" in out["next_step"]
    assert [
        c for c in fake_client.artifacts.calls if c[0] == "generate_audio"
    ][generate_boundary:] == [], "重生前必須停下,不准對 EP02 重新 dispatch"


async def test_series_retract_of_a_middle_episode_with_a_stuck_attempt_offers_retract_not_series(
    fake_client, tmp_path
):
    """T3:同一道守門也要涵蓋「EP02 有一顆卡住、即將被 series 重新 dispatch 的
    attempt」(not_accepted re-arm / failed-removed supersede,不只是全新一集這條
    路)。這裡用 not_accepted 構造:直接對 EP02 dispatch 一次(先建出 active
    attempt),失敗留下 not_accepted;之後 EP03 才單獨補上 output。這種形狀下
    `podcast_episode` 對已有 active attempt 的 EP02 是死路(會撞
    `_create_audio_attempt` 的「already has durable active attempt」),所以這一格
    改教 `podcast_attempt_retract`(純本機、免旗標),retract 之後再用
    `podcast_episode(..., source_ids=[...])` 指名重生。"""
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.sources.seed("整季講義")
    fake_client.artifacts.fail_generate = True

    stopped_ep2 = await p.podcast_series(
        "nb-1", episodes=EPS3, output_dir=str(tmp_path), start=2
    )
    assert stopped_ep2["complete"] is False
    fake_client.artifacts.fail_generate = False

    await p.podcast_episode(
        "nb-1",
        episode_n=3,
        title=EPS3[2]["title"],
        brief=EPS3[2]["brief"],
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
        source_ids=[fake_client.sources.sources[0]["id"]],
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    ep2 = next(row for row in stored["episodes"] if row["episode"] == 2)
    assert ep2["attempts"][0]["dispatch"]["status"] == "not_accepted"

    generate_boundary = len(
        [c for c in fake_client.artifacts.calls if c[0] == "generate_audio"]
    )

    out = await p.podcast_series(
        "nb-1", episodes=EPS3, output_dir=str(tmp_path), start=2
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 2
    assert out["observed_state"] == "later_episode_has_output"
    assert out["safe_next_action"] == "podcast_attempt_retract"
    assert "next_step" in out and "podcast_episode" in out["next_step"]
    assert [
        c for c in fake_client.artifacts.calls if c[0] == "generate_audio"
    ][generate_boundary:] == [], "重生前必須停下,不准對 EP02 重新 dispatch"


async def test_legacy_audio_missing_hands_off_to_adopt_when_not_yet_adopted(
    fake_client, tmp_path
):
    """T5(P1,`scratchpad/verify-G/g2_legacy_audio_missing.py`):`legacy_audio_missing`
    停點過去無條件交棒 `podcast_episode_resume`,但 `_ensure_resume_attempt` 的 seed
    條件(把回錄 source 的身分接回來、不重複上傳)要求 `feedback_source_adopted_at`
    ——只有 `podcast_attempt_adopt` 會寫這個人工標記(`_promote_attempt_output` 明講
    不寫)。真實的 flat v1 manifest(從沒跑過 adopt,只是 episode 級 `feedback_source_id`
    剛好記著)照做走壞的那支:resume 把它當全新 upload,同名 source 再上傳一次。

    這裡改成:`feedback_source_adopted_at` 不存在時交棒 `podcast_attempt_adopt`
    (series 已經用 id+label 對帳驗過這筆 source,`feedback_source_id` 直接帶出);
    存在時才交棒 resume(下一支測試鎖住這條路徑不受影響)。"""
    manifest_path = tmp_path / "series_manifest.json"
    mp3_path = tmp_path / "gone-ep01.mp3"
    source_id = fake_client.sources._add("EP01 心法篇", kind="media")
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
                        "mp3_path": str(mp3_path),  # 本機檔案不存在
                        "published_at": "Fri, 25 Jul 2026 00:00:00 +0800",
                        "feedback_source_id": source_id,
                        # 刻意不帶 feedback_source_adopted_at——從沒跑過 adopt 的
                        # 真實 legacy manifest 就長這樣。
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="legacy-artifact",
            title="EP01 心法篇",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )
    add_file_boundary = len(
        [c for c in fake_client.sources.calls if c[0] == "add_file"]
    )

    stopped = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )

    assert stopped["complete"] is False
    assert stopped["observed_state"] == "legacy_audio_missing"
    assert stopped["safe_next_action"] == "podcast_attempt_adopt"
    assert stopped["feedback_source_id"] == source_id

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        feedback_source_id=stopped["feedback_source_id"],
    )
    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="legacy-artifact",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert adopted["feedback_source_id"] == source_id
    assert resumed["feedback_source_id"] == source_id
    assert (
        len([c for c in fake_client.sources.calls if c[0] == "add_file"])
        - add_file_boundary
        == 0
    ), "照公開回傳做下去不准第二次上傳同名回錄"
    assert [
        s["title"] for s in fake_client.sources.sources if s["kind"] == "media"
    ].count("EP01 心法篇") == 1


async def test_legacy_audio_missing_hands_off_to_resume_when_already_adopted(
    fake_client, tmp_path
):
    """T5:已經跑過 adopt(`feedback_source_adopted_at` 存在)時,原本的交棒對象
    `podcast_episode_resume` 必須繼續放行——這條是既有
    `test_legacy_missing_audio_resumes_without_duplicate_adopted_source` 鎖住的路,
    這裡只是同一份不變式換個角度驗一次(series 停點本身,不是走到 resume 之後)。"""
    manifest_path = tmp_path / "series_manifest.json"
    mp3_path = tmp_path / "gone-ep01.mp3"
    source_id = fake_client.sources._add("EP01 心法篇", kind="media")
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
                        "published_at": "Fri, 25 Jul 2026 00:00:00 +0800",
                        "feedback_source_id": source_id,
                        "feedback_source_adopted_at": "2026-07-25T00:00:00+00:00",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="legacy-artifact",
            title="EP01 心法篇",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )

    stopped = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )

    assert stopped["safe_next_action"] == "podcast_episode_resume"
    assert stopped["artifact_id"] == "legacy-artifact"
