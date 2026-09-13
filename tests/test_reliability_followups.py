import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from notebooklm.exceptions import NetworkError
from notebooklm.types import ArtifactType

from notebooklm_mcp import tools_podcast as p


async def test_source_adoption_rejects_prepared_attempt_before_remote_lookup(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(manifest_path)
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        language="zh_Hant",
        audio_format="deep-dive",
        audio_length="long",
    )
    source_id = fake_client.sources._add("EP01 心法篇", kind="media")
    artifact_boundary = len(fake_client.artifacts.calls)
    source_boundary = len(fake_client.sources.calls)

    with pytest.raises(ValueError, match="completed audio|download"):
        await p.podcast_attempt_adopt(
            str(manifest_path),
            episode_n=1,
            attempt_id=attempt_id,
            feedback_source_id=source_id,
        )

    assert fake_client.artifacts.calls[artifact_boundary:] == []
    assert fake_client.sources.calls[source_boundary:] == []


async def test_artifact_rename_response_loss_retries_when_title_did_not_land(
    fake_client, tmp_path, monkeypatch
):
    manifest_path = tmp_path / "series_manifest.json"
    original_rename = fake_client.artifacts.rename
    rename_count = 0

    async def lose_first_response(
        notebook_id, artifact_id, new_title, *, return_object=True
    ):
        nonlocal rename_count
        rename_count += 1
        if rename_count == 1:
            fake_client.artifacts.calls.append(
                (
                    "rename",
                    {
                        "artifact_id": artifact_id,
                        "new_title": new_title,
                        "return_object": return_object,
                    },
                )
            )
            raise ConnectionError("rename response lost before landing")
        return await original_rename(
            notebook_id,
            artifact_id,
            new_title,
            return_object=return_object,
        )

    monkeypatch.setattr(fake_client.artifacts, "rename", lose_first_response)

    with pytest.raises(ConnectionError, match="response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    interrupted = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = interrupted["episodes"][0]["attempts"][0]
    assert attempt["finalize"]["artifact_rename"]["status"] == "outcome_unknown"
    artifact = next(
        row
        for row in fake_client.artifacts.artifacts
        if row.id == "task-123"
    )
    assert artifact.title == "Audio Overview"

    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="task-123",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert resumed["artifact_id"] == "task-123"
    assert artifact.title == "EP01 心法篇"
    assert rename_count == 2


async def test_legacy_missing_audio_resumes_without_duplicate_adopted_source(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    missing_path = tmp_path / "missing-legacy-ep01.mp3"
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
                        "mp3_path": str(missing_path),
                        "published_at": "Wed, 01 Jan 2020 09:00:00 +0800",
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
    source_id = fake_client.sources._add("EP01 心法篇", kind="media")
    await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        feedback_source_id=source_id,
    )
    source_boundary = len(fake_client.sources.calls)

    stopped = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第一集"}],
        output_dir=str(tmp_path),
    )
    assert stopped["complete"] is False
    assert stopped["safe_next_action"] == "podcast_episode_resume"
    assert stopped["artifact_id"] == "legacy-artifact"

    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="legacy-artifact",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert resumed["feedback_source_id"] == source_id
    assert [
        call
        for call in fake_client.sources.calls[source_boundary:]
        if call[0] in {"add_file", "rename"}
    ] == []


async def test_completed_fast_path_rejects_missing_remote_artifact(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    first = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    fake_client.artifacts.artifacts.clear()

    with pytest.raises(RuntimeError, match="artifact.*cannot be verified"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id=first["artifact_id"],
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )


async def test_acceptance_unknown_can_adopt_late_verified_artifact(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = stored["episodes"][0]["active_attempt_id"]
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="late-artifact",
            title="Audio Overview",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        attempt_id=attempt_id,
        artifact_id="late-artifact",
    )

    assert adopted["artifact_id"] == "late-artifact"
    assert adopted["safe_next_action"] == "podcast_episode_resume"


async def test_acceptance_unknown_adoption_uses_episode_notebook_in_rolling_manifest(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "episode-notebook",
            episode_n=40,
            title="跨供應商委派",
            brief="第四十集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored["notebook_id"] = "legacy-show-notebook"
    manifest_path.write_text(
        json.dumps(stored, ensure_ascii=False), encoding="utf-8"
    )
    attempt_id = stored["episodes"][0]["active_attempt_id"]
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="late-episode-artifact",
            title="Audio Overview",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=40,
        attempt_id=attempt_id,
        artifact_id="late-episode-artifact",
    )

    assert adopted["artifact_id"] == "late-episode-artifact"
    assert adopted["safe_next_action"] == "podcast_episode_resume"


def test_legacy_task_id_is_durable_output_evidence():
    assert p.has_durable_output_evidence({"task_id": "legacy-task"})


async def test_published_legacy_output_blocks_implicit_supersede_after_failed_resume(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    mp3_path = tmp_path / "ep19.mp3"
    published_bytes = b"already published EP19"
    mp3_path.write_bytes(published_bytes)
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
                        "published_at": "Fri, 24 Jul 2026 09:00:00 +0800",
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
    fake_client.artifacts.fail_complete = True

    with pytest.raises(RuntimeError, match="Generation failed"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="legacy-artifact",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    fake_client.artifacts.fail_complete = False
    generate_boundary = len(
        [
            call
            for call in fake_client.artifacts.calls
            if call[0] == "generate_audio"
        ]
    )
    with pytest.raises(ValueError, match="refusing to silently overwrite"):
        await p.podcast_series(
            "nb-1",
            episodes=[{"title": "心法篇", "brief": "第一集"}],
            output_dir=str(tmp_path),
        )

    assert mp3_path.read_bytes() == published_bytes
    assert len(
        [
            call
            for call in fake_client.artifacts.calls
            if call[0] == "generate_audio"
        ]
    ) == generate_boundary


def _legacy_manifest_with_adopted_source(
    manifest_path, mp3_path, old_source_id: str
) -> None:
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
                        "feedback_source_id": old_source_id,
                        "feedback_source_adopted_at": (
                            "2026-07-25T00:00:00+00:00"
                        ),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


async def test_legacy_source_is_not_carried_to_a_different_artifact(
    fake_client, tmp_path
):
    """T2(P1 的另一半,wp-a2):flat v1 legacy 集(只有 episode 級硬證據,沒有
    attempts)resume 到**不同**的 artifact 現在在 `_ensure_resume_attempt` 的
    新建分支就 fail-fast——不再讓它先 rename 遠端 artifact、下載、把 mp3 上傳成
    同名回錄 source **三個遠端副作用**都跑完,才在 `_promote_attempt_output`
    (wp-a1 的落點,defense-in-depth 仍保留)被擋。對一個打錯 `artifact_id` 的
    呼叫,舊行為會留下三個遠端副作用與一筆清理義務;新行為是**打錯就打錯,一個
    副作用都不發生**。

    resume 到**同一顆** legacy artifact 仍要繼續放行(下一支測試)——那是唯一能
    幫這種 flat v1 episode 建出可 retract attempt 的路(retract amendment (2),
    `legacy_audio_missing` 停點靠它)。
    """
    manifest_path = tmp_path / "series_manifest.json"
    mp3_path = tmp_path / "legacy-ep01.mp3"
    mp3_path.write_bytes(b"legacy audio")
    old_source_id = fake_client.sources._add("EP01 心法篇", kind="media")
    _legacy_manifest_with_adopted_source(manifest_path, mp3_path, old_source_id)
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="replacement-artifact",
            title="Audio Overview",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )

    with pytest.raises(ValueError, match="legacy-artifact"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="replacement-artifact",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    # fail-fast:不 rename、不 download、不上傳回錄——一個遠端副作用都不該發生。
    assert fake_client.artifacts.calls == []
    assert fake_client.sources.calls == []

    # episode 級投影欄位一個字都沒被換掉,也沒有半成品候選 attempt 留下。
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = stored["episodes"][0]
    assert episode["artifact_id"] == "legacy-artifact"
    assert episode["feedback_source_id"] == old_source_id
    assert "output_attempt_id" not in episode
    assert "active_attempt_id" not in episode
    assert episode.get("attempts", []) == []


async def test_legacy_output_resume_to_the_same_artifact_still_succeeds(
    fake_client, tmp_path
):
    """T4 的放行閘:resume 到**同一顆** legacy artifact 必須繼續放行——這是唯一能
    幫這種 flat v1 episode 建出可 retract attempt 的路(retract amendment (2)),
    擋掉它會讓 legacy 集永遠無法被合法置換。"""
    manifest_path = tmp_path / "series_manifest.json"
    mp3_path = tmp_path / "legacy-ep01.mp3"
    mp3_path.write_bytes(b"legacy audio")
    old_source_id = fake_client.sources._add("EP01 心法篇", kind="media")
    _legacy_manifest_with_adopted_source(manifest_path, mp3_path, old_source_id)
    fake_client.artifacts.artifacts.append(
        SimpleNamespace(
            id="legacy-artifact",
            title="EP01 心法篇",
            kind=ArtifactType.AUDIO,
            created_at=datetime.now(timezone.utc),
        )
    )
    source_boundary = len(fake_client.sources.calls)

    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="legacy-artifact",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert resumed["feedback_source_id"] == old_source_id
    assert [
        call
        for call in fake_client.sources.calls[source_boundary:]
        if call[0] in {"add_file", "rename"}
    ] == []


async def test_ambiguous_uploaded_source_candidate_can_be_adopted_before_rename(
    fake_client, tmp_path
):
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
    assert stopped["safe_next_action"] == "podcast_attempt_adopt"

    # **只用公開回傳取身分**(docs/gotchas-attempt.md「回傳要自足」那條紅線)——
    # `stopped["attempt_id"]` / `stopped["candidate_source_ids"]` 本來就帶著正確的值,
    # 不必也不該從 manifest 私下讀(P1 修復:候選清單原本沒有進公開回傳,這裡曾經
    # 繞過去直接讀 manifest,「回傳給不給得出候選」從未被驗證過)。
    attempt_id = stopped["attempt_id"]
    candidates = stopped["candidate_source_ids"]
    add_boundary = len(
        [call for call in fake_client.sources.calls if call[0] == "add_file"]
    )
    unselected = candidates[1]
    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        attempt_id=attempt_id,
        feedback_source_id=candidates[0],
    )

    assert adopted["complete"] is False
    # 未被選中的那筆同名 candidate 現在要進清理義務——它跟選中的那筆一樣同名,漏了記錄
    # 就會從此沒人記得,resume 前必須先刪(見 fix #4/#16 review 的 candidate_source_ids
    # 清空即遺忘問題)。
    assert adopted["stale_source_ids"] == [unselected]
    assert adopted["safe_next_action"] == "source_delete"

    with pytest.raises(
        ValueError, match="retracted feedback sources still in the notebook"
    ):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="task-123",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    await fake_client.sources.delete("nb-1", unselected)
    resumed = await p.podcast_episode_resume(
        "nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="task-123",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    assert resumed["feedback_source_id"] == candidates[0]
    assert len(
        [call for call in fake_client.sources.calls if call[0] == "add_file"]
    ) == add_boundary
    assert "pending_source_cleanup" not in json.loads(
        manifest_path.read_text(encoding="utf-8")
    )["episodes"][0]


async def test_completed_episode_transport_failure_returns_structured_partial(
    fake_client, tmp_path, monkeypatch
):
    """已完成集的 drift 複驗途中傳輸失敗不得裸拋,否則整季進度回報全丟。"""
    manifest_path = tmp_path / "series_manifest.json"
    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="1",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    async def transport_failure(_notebook_id):
        raise NetworkError("network blip while verifying completed episode")

    monkeypatch.setattr(fake_client.sources, "list", transport_failure)
    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "1"}, {"title": "實戰篇", "brief": "2"}],
        output_dir=str(tmp_path),
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 1
    assert out["observed_state"] == "verification_incomplete"
    assert out["safe_next_action"] == "podcast_series"
    assert [
        call for call in fake_client.artifacts.calls if call[0] == "generate_audio"
    ] == [call for call in fake_client.artifacts.calls if call[0] == "generate_audio"][:1]


async def test_new_episode_network_error_returns_structured_partial(
    fake_client, tmp_path
):
    """首次 dispatch 的 SDK transport failure 也要保留 series recovery contract。"""
    fake_client.artifacts.generate_audio_exc = NetworkError(
        "network blip during first dispatch"
    )

    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "1"}],
        output_dir=str(tmp_path),
    )

    assert out["complete"] is False
    assert out["stopped_at_episode"] == 1
    assert out["observed_state"] == "acceptance_unknown"
    assert out["safe_next_action"] == "podcast_episode_reconcile"


async def test_repeated_quota_supersede_is_visible_to_the_caller(
    fake_client, tmp_path
):
    """removed(每日配額下架)自動 supersede 時,呼叫端必須看得出在原地打轉。"""
    fake_client.artifacts.fail_removed = True
    episodes = [{"title": "心法篇", "brief": "1"}]

    first = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path)
    )
    second = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path)
    )
    third = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path)
    )

    assert [out["observed_state"] for out in (first, second, third)] == [
        "removed",
        "removed",
        "removed",
    ]
    assert [out["attempt_count"] for out in (first, second, third)] == [1, 2, 3]
    assert [out["superseded_attempt_count"] for out in (first, second, third)] == [
        0,
        1,
        2,
    ]


async def test_promoted_episode_records_the_verified_feedback_source_id(
    fake_client, tmp_path
):
    """promote 要留下 episode 級 continuity 證據,attempts 日後被裁剪也還在。"""
    manifest_path = tmp_path / "series_manifest.json"
    output = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="1",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = stored["episodes"][0]
    assert episode["feedback_source_id"] == output["feedback_source_id"]
    assert "feedback_source_adopted_at" not in episode


# ---- finalize 失敗訊息:完整 recovery payload + 狀態權威,兩者並存(F1) ------------
#
# 這整段訊息在盲審之前**一條測試都沒有**,所以「把完整呼叫換成 capabilities 散文」這個
# 回歸(丟掉 `podcast_episode_resume` 的五個必填參數)沒有任何東西看得到。

_RESUME_REQUIRED_ARGS = (
    "notebook_id",
    "episode_n",
    "title",
    "artifact_id",
    "output_dir",
)


async def test_finalize_failure_message_carries_every_resume_argument(
    fake_client, tmp_path
):
    """訊息要能直接複製執行 —— 逐一斷言 resume 的五個必填參數 + manifest_path。

    `podcast_episode_resume` 的簽章是五個位置必填參數(即使傳了 manifest_path 也一樣),
    而這段訊息存在的唯一理由就是「呼叫端據此續完,不必再 artifact_list 撈 id」。
    """
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.download_audio_exc = ConnectionError("download interrupted")

    with pytest.raises(ConnectionError) as failure:
        await p.podcast_episode(
            "nb-1",
            episode_n=7,
            title="  心法篇  ",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    message = str(failure.value)
    for argument in _RESUME_REQUIRED_ARGS:
        assert f"{argument}=" in message, f"resume 的必填參數 {argument} 不在訊息裡:{message}"
    assert "podcast_episode_resume(" in message
    assert repr(str(manifest_path)) in message
    assert "episode_n=7" in message
    assert repr("心法篇") in message          # title 已 strip,可直接貼
    assert "download interrupted" in message  # 原例外訊息不准被蓋掉
    assert not message.rstrip().endswith("；")


async def test_finalize_failure_message_survives_an_unreadable_manifest(
    fake_client, tmp_path, monkeypatch
):
    """capabilities 算不出來時**不能留空**:完整呼叫照給,並說清楚它未經狀態核對。

    舊版在這裡回 `next_step = ""`,訊息以一個分號結尾、什麼都沒教。
    """
    manifest_path = tmp_path / "series_manifest.json"
    original_read = p.ManifestStore.read
    calls = {"n": 0}

    def flaky_read(self):
        calls["n"] += 1
        # 前幾次(dispatch 前的驗證)正常,之後一律炸掉 —— finalize 本身與錯誤處理那次
        # 讀取都讀不到,正是「狀態算不出來」那一格。
        if calls["n"] > 3 and str(self.path) == str(manifest_path):
            raise OSError("manifest is unreadable")
        return original_read(self)

    monkeypatch.setattr(p.ManifestStore, "read", flaky_read)

    with pytest.raises(OSError) as failure:
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    message = str(failure.value)
    for argument in _RESUME_REQUIRED_ARGS:
        assert f"{argument}=" in message
    assert "無法計算最新狀態" in message
    assert not message.rstrip().endswith("；")


async def test_finalize_failure_message_defers_to_capabilities_after_a_parallel_retract(
    fake_client, tmp_path, monkeypatch
):
    """finalize 失敗與並行 retract 撞在一起時,訊息不准宣稱 resume 是現在可執行的動作。

    完整呼叫仍然附上(它是 recovery payload,不是指令),但狀態權威那句要說「這顆已經
    被作廢」——`abandon_in_flight` 讓「retract 一顆還在飛的 attempt」變成受支援的操作。
    """
    manifest_path = tmp_path / "series_manifest.json"
    retracted = {"done": False}
    original_download = fake_client.artifacts.download_audio

    async def retract_then_fail(*args, **kwargs):
        if not retracted["done"]:
            retracted["done"] = True
            snapshot = p.ManifestStore(manifest_path).read()
            attempt_id = snapshot["episodes"][0]["active_attempt_id"]
            await p.podcast_attempt_retract(
                str(manifest_path),
                1,
                attempt_id,
                reason="並行作廢:輸入本來就錯",
                abandon_in_flight=True,
            )
        raise ConnectionError("download interrupted")

    monkeypatch.setattr(fake_client.artifacts, "download_audio", retract_then_fail)

    with pytest.raises(Exception) as failure:
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    message = str(failure.value)
    assert "podcast_episode_resume(" in message, message
    assert "只有下面這句仍指向 resume 時才執行" in message
    # 狀態權威那句必須反映 tombstone,不能教 resume。
    tail = message.split("只有下面這句仍指向 resume 時才執行")[-1]
    assert "podcast_episode_resume" not in tail, tail
    assert original_download is not None
