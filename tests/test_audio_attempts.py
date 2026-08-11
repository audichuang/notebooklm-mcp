import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from notebooklm.types import ArtifactType

from notebooklm_mcp import tools_basic as b
from notebooklm_mcp import tools_podcast as p


async def test_attempt_is_dispatching_before_generate_audio_side_effect(fake_client, tmp_path):
    """遠端 generation 一進入時，本地 attempt identity 與 dispatching 已 durable。"""
    manifest_path = tmp_path / "series_manifest.json"
    observed = {}

    def capture_manifest():
        observed.update(json.loads(manifest_path.read_text(encoding="utf-8")))

    fake_client.artifacts.on_generate_audio = capture_manifest

    out = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    episode = observed["episodes"][0]
    assert episode["active_attempt_id"]
    attempt = episode["attempts"][0]
    assert attempt["attempt_id"] == episode["active_attempt_id"]
    assert attempt["dispatch"]["status"] == "dispatching"
    assert attempt["remote"]["artifact_id"] is None
    assert out["attempt_id"] == attempt["attempt_id"]

async def test_transport_loss_marks_acceptance_unknown_without_regenerating(
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
    episode = stored["episodes"][0]
    attempt = episode["attempts"][0]
    assert episode["active_attempt_id"] == attempt["attempt_id"]
    assert attempt["dispatch"]["status"] == "acceptance_unknown"
    assert attempt["remote"]["artifact_id"] is None
    assert attempt["errors"][-1]["phase"] == "dispatch"
    assert attempt["errors"][-1]["type"] == "TimeoutError"
    assert [
        call[0] for call in fake_client.artifacts.calls
        if call[0] == "generate_audio"
    ] == ["generate_audio"]


async def test_retry_same_episode_after_transport_loss_does_not_generate_again(
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

    fake_client.artifacts.generate_audio_exc = None
    with pytest.raises(ValueError, match="durable active attempt|既有.*attempt"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(stored["episodes"][0]["attempts"]) == 1
    assert [c[0] for c in fake_client.artifacts.calls].count("generate_audio") == 1


def _remote_audio(artifact_id: str):
    return SimpleNamespace(
        id=artifact_id,
        title="Audio Overview",
        kind=ArtifactType.AUDIO,
        created_at=datetime.now(timezone.utc),
    )


async def _leave_acceptance_unknown(
    fake_client, tmp_path, candidates, wait_timeout: float = 1200.0
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_remote_artifacts_before_raise = list(candidates)
    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
            wait_timeout=wait_timeout,
        )
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest_path, stored["episodes"][0]["active_attempt_id"]


async def test_reconcile_adopts_the_only_unclaimed_audio_candidate(
    fake_client, tmp_path
):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [_remote_audio("remote-audio-1")]
    )

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "accepted"
    assert attempt["remote"]["artifact_id"] == "remote-audio-1"
    assert out["artifact_id"] == "remote-audio-1"
    assert [
        call[0] for call in fake_client.artifacts.calls
        if call[0] == "generate_audio"
    ] == ["generate_audio"]


async def test_reconcile_accepts_sdk_naive_local_artifact_timestamp(
    fake_client, tmp_path
):
    artifact = _remote_audio("remote-audio-naive")
    artifact.created_at = datetime.now()
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [artifact]
    )

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    assert out["artifact_id"] == "remote-audio-naive"
    assert out["safe_next_action"] == "podcast_episode_resume"


async def test_reconcile_with_no_candidate_stays_unknown(fake_client, tmp_path):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, []
    )

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "acceptance_unknown"
    assert attempt["remote"]["artifact_id"] is None
    assert out["complete"] is False
    assert out["observed_state"] == "acceptance_unknown"
    # F3(主迴圈裁決,採納審查者的反駁):dispatch 剛發生、候選窗還沒關
    # (見 test_source_selection.py 的詳細論證),晚幾分鐘再對帳可能就撈得到——
    # 這時 retract 會把還在飛的因果紀錄提前寫成墓碑(ADR-0009 禁止),所以停在
    # `podcast_episode_reconcile`;只有候選窗真的關了才給 `podcast_attempt_retract`
    # (見 test_reconcile_with_no_candidate_past_the_window_offers_retract)。
    assert out["safe_next_action"] == "podcast_episode_reconcile"


async def test_reconcile_with_no_candidate_past_the_window_offers_retract(
    fake_client, tmp_path
):
    """**F3 窗外分支(item 4)的鑑別測試。**

    候選窗(`dispatched_at` 到 `wait_timeout` 那段時間,含 1 分鐘時鐘容錯)已經關了
    ——未來任何 artifact 都會落在窗外,繼續對帳真的沒有用,這時才給
    `podcast_attempt_retract`。用 `_attempt_record` 直接把 `dispatched_at` 往回撥
    2 小時,模擬「窗早就關了」而不必真的等 `wait_timeout` 秒。
    """
    from datetime import datetime, timedelta, timezone

    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, []
    )
    store = p.ManifestStore(str(manifest_path))
    stale_dispatched_at = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()

    def backdate(manifest: dict) -> None:
        _, attempt = p._attempt_record(manifest, 1, attempt_id)
        attempt["dispatch"]["dispatched_at"] = stale_dispatched_at

    store.update(backdate)

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    assert out["safe_next_action"] == "podcast_attempt_retract"
    step = out["next_step"]
    assert "abandon_in_flight=true" in step, step
    assert "不需要" not in step, step
    assert "窗" in step and ("關" in step), step


async def test_reconciliation_window_closure_has_a_conservative_floor_the_caller_cannot_shrink(
    fake_client, tmp_path
):
    """**P1**:候選窗的**關閉判斷**曾經直接用這次呼叫的 `wait_timeout`,呼叫端傳小一點
    (甚至 1 秒)就能把窗縮小到早就「過期」——原生成可能承諾等 3600 秒,30 分鐘後拿
    `wait_timeout=1` 重呼 reconcile,約 61 秒(1 秒 + 1 分鐘時鐘容錯)後就會誤判成
    「窗已關」,建議 retract 一顆其實還在飛的 attempt(照做就是 ADR-0009 禁止的
    因果改寫)。修法是關閉判斷改用 `max(wait_timeout, _RECONCILIATION_MIN_WINDOW)`
    ——呼叫端只能放大這個窗,不能縮小。

    用 backdate `dispatched_at` 模擬「已經過了多久」,不用 `sleep` 真的等。
    """
    from datetime import timedelta

    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, []
    )
    store = p.ManifestStore(str(manifest_path))

    def backdate(seconds: float):
        def _mutate(manifest: dict) -> None:
            _, attempt = p._attempt_record(manifest, 1, attempt_id)
            attempt["dispatch"]["dispatched_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=seconds)
            ).isoformat()

        store.update(_mutate)

    # dispatch 是 2 分鐘前的事,這次呼叫傳 wait_timeout=1:舊窗(1 秒 + 1 分鐘時鐘
    # 容錯 = 61 秒)早就「關了」,但保守下限(_RECONCILIATION_MIN_WINDOW,1 小時)
    # 還沒到——不該被縮小到給 retract。
    backdate(120)
    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id, wait_timeout=1,
    )
    assert out["safe_next_action"] == p.ACTION_RECONCILE, (
        f"wait_timeout=1 不該把候選窗縮小到 61 秒就關掉:{out}"
    )

    # dispatch 是保守下限 + 時鐘容錯之後的事:這次窗真的關了,即使呼叫端仍然只傳
    # wait_timeout=1(下限保證窗不會比它更小,不代表窗永遠不關)。
    backdate(p._RECONCILIATION_MIN_WINDOW.total_seconds() + 120)
    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id, wait_timeout=1,
    )
    assert out["safe_next_action"] == p.ACTION_RETRACT, out


async def test_reconcile_does_not_auto_bind_when_another_attempt_is_still_unresolved(
    fake_client, tmp_path
):
    """**P1**(Codex adversarial review 實跑驗證,主迴圈裁決採納):時間窗本身判定不了
    artifact 歸屬。實跑重現:EP1 用 `wait_timeout=7200` dispatch 後 response lost;
    EP2 在 EP1 的候選窗內另外 dispatch,也 response lost、但遠端真的建出了
    artifact-created-by-b(同樣還沒 claim)。對 EP1 reconcile 時,這顆 artifact 落在
    EP1 的候選窗內、不在 EP1 的 baseline、也沒被 EP2 claim,舊行為會判成 EP1 的
    「唯一候選」而誤綁——Codex 實跑輸出正是 `artifact_id="artifact-created-by-b"`。

    修法:唯一候選出現時,若同一本 notebook 底下還有其他「未解決」的 attempt(這裡
    是 EP2),代表這顆 artifact 有可能是它的產物——不自動綁定,改成跟「真的有多筆
    候選」共用的安全停點(`reconciliation_ambiguous` + `podcast_attempt_adopt`)。

    ⚠️ 只有一顆 attempt 在飛的正常情境不該受這條 guard 影響——那條回歸鎖已經是
    `test_reconcile_adopts_the_only_unclaimed_audio_candidate`(上面第一條 reconcile
    測試),不重複造一份。
    """
    manifest_path = tmp_path / "series_manifest.json"

    async def _dispatch_and_lose_response(episode_n: int, title: str, wait_timeout: float):
        fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
        with pytest.raises(TimeoutError, match="response lost"):
            await p.podcast_episode(
                "nb-1",
                episode_n=episode_n,
                title=title,
                brief=f"第{episode_n}集",
                output_dir=str(tmp_path),
                manifest_path=str(manifest_path),
                wait_timeout=wait_timeout,
            )

    # EP1:T0 dispatch,承諾等 7200 秒,response lost,這次遠端(目前為止)還沒有
    # 任何 artifact。
    await _dispatch_and_lose_response(1, "心法篇", 7200)
    # EP2:在 EP1 的候選窗內另外 dispatch,也 response lost——但這次遠端真的建出了
    # artifact-created-by-b(模擬「response lost,但伺服器已經受理並開始生成」)。
    fake_client.artifacts.generate_remote_artifacts_before_raise = [
        _remote_audio("artifact-created-by-b")
    ]
    await _dispatch_and_lose_response(2, "續集", 1200)

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    ep1 = next(e for e in stored["episodes"] if e["episode"] == 1)
    ep2 = next(e for e in stored["episodes"] if e["episode"] == 2)
    ep1_attempt_id = ep1["active_attempt_id"]
    ep2_attempt_id = ep2["active_attempt_id"]

    # 模擬「EP1 在 T0 dispatch、EP2 在 T0+4000 dispatch、對帳發生在 T0+4100」——
    # 用 backdate 控制相對時間,不必真的等。
    store = p.ManifestStore(str(manifest_path))

    def backdate(episode_n: int, attempt_id: str, seconds_ago: float):
        def _mutate(manifest: dict) -> None:
            _, attempt = p._attempt_record(manifest, episode_n, attempt_id)
            attempt["dispatch"]["dispatched_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
            ).isoformat()

        store.update(_mutate)

    backdate(1, ep1_attempt_id, 4100)
    backdate(2, ep2_attempt_id, 100)

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=ep1_attempt_id, wait_timeout=1200,
    )

    # **鑑別點**:artifact-created-by-b 停在候選清單裡等呼叫端指名,不是被 EP1
    # 直接認領走。拿掉這輪的 guard(讓 `blocking_attempt_ids` 恆為 `[]`)會讓下面
    # 兩條斷言雙雙變回舊行為(`out["artifact_id"] == "artifact-created-by-b"` /
    # `safe_next_action == podcast_episode_resume`)。
    assert out["candidate_artifact_ids"] == ["artifact-created-by-b"], out
    assert out["safe_next_action"] == p.ACTION_ADOPT, out
    assert out.get("artifact_id") is None, out

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    ep1_attempt = next(
        a for a in stored["episodes"][0]["attempts"] if a["attempt_id"] == ep1_attempt_id
    )
    assert ep1_attempt["remote"]["artifact_id"] is None, (
        "誤綁的話這裡會被寫成 artifact-created-by-b —— EP1 從沒真的建出這顆 artifact"
    )


async def test_reconcile_rechecks_unresolved_attempts_atomically_before_binding(
    fake_client, tmp_path, monkeypatch
):
    """A 讀完 snapshot 後 B 才 dispatch，綁定區段仍必須看得到 B。"""
    manifest_path, attempt_a = await _leave_acceptance_unknown(
        fake_client, tmp_path, [], wait_timeout=7200
    )
    original_list = fake_client.artifacts.list

    async def list_after_b_dispatches(notebook_id, artifact_type=None):
        # 只在 A 已讀完 manifest、尚未綁定的 await 期間插入 B。
        monkeypatch.setattr(fake_client.artifacts, "list", original_list)
        fake_client.artifacts.generate_remote_artifacts_before_raise = [
            _remote_audio("artifact-from-b")
        ]
        fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
        with pytest.raises(TimeoutError, match="response lost"):
            await p.podcast_episode(
                "nb-1",
                episode_n=2,
                title="續集",
                brief="第二集",
                output_dir=str(tmp_path),
                manifest_path=str(manifest_path),
            )
        return await original_list(notebook_id, artifact_type=artifact_type)

    monkeypatch.setattr(fake_client.artifacts, "list", list_after_b_dispatches)

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_a
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode_a = next(row for row in stored["episodes"] if row["episode"] == 1)
    episode_b = next(row for row in stored["episodes"] if row["episode"] == 2)
    attempt_a_row = next(
        row for row in episode_a["attempts"] if row["attempt_id"] == attempt_a
    )
    attempt_b_row = next(
        row
        for row in episode_b["attempts"]
        if row["attempt_id"] == episode_b["active_attempt_id"]
    )
    assert attempt_b_row["dispatch"]["status"] == "acceptance_unknown"
    assert attempt_a_row["remote"]["artifact_id"] is None
    assert out["candidate_artifact_ids"] == ["artifact-from-b"]
    assert out["safe_next_action"] == p.ACTION_ADOPT


async def test_tombstone_blocker_offers_and_executes_the_negative_candidate_path(
    fake_client, tmp_path
):
    """晚到 orphan 仍被 tombstone 擋住；確認不屬於 A 後要有真正走得通的路。"""
    manifest_path = tmp_path / "series_manifest.json"

    async def dispatch_and_lose(episode_n: int, title: str) -> str:
        fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
        with pytest.raises(TimeoutError, match="response lost"):
            await p.podcast_episode(
                "nb-1",
                episode_n=episode_n,
                title=title,
                brief=f"第{episode_n}集",
                output_dir=str(tmp_path),
                manifest_path=str(manifest_path),
            )
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
        episode = next(row for row in stored["episodes"] if row["episode"] == episode_n)
        return episode["active_attempt_id"]

    tombstone_id = await dispatch_and_lose(2, "早先已作廢的續集")
    await p.podcast_attempt_retract(
        str(manifest_path),
        2,
        tombstone_id,
        reason="已確認原生成要作廢",
        abandon_in_flight=True,
    )
    attempt_a = await dispatch_and_lose(1, "心法篇")
    fake_client.artifacts.seed_artifacts(_remote_audio("late-orphan-from-tombstone"))

    out = await p.podcast_episode_reconcile(
        str(manifest_path), 1, attempt_a
    )

    assert out["candidate_artifact_ids"] == ["late-orphan-from-tombstone"]
    assert out["blocking_attempt_ids"] == [tombstone_id]
    assert out["safe_next_action"] == p.ACTION_ADOPT
    assert "確認不屬於這次" in out["next_step"]
    assert "abandon_in_flight=true" in out["next_step"]
    with pytest.raises(ValueError, match="retracted"):
        await p.podcast_attempt_adopt(
            str(manifest_path),
            2,
            attempt_id=tombstone_id,
            artifact_id="late-orphan-from-tombstone",
        )

    retracted = await p.podcast_attempt_retract(
        str(manifest_path),
        1,
        attempt_a,
        reason="已確認候選不屬於這次",
        abandon_in_flight=True,
    )
    assert retracted["safe_next_action"] == p.ACTION_SERIES

    fake_client.artifacts.generate_audio_exc = None
    regenerated = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "第1集"}],
        output_dir=str(tmp_path),
    )
    assert regenerated["complete"] is True


async def test_dispatch_persists_the_original_wait_timeout_promise(
    fake_client, tmp_path
):
    """**P1 前置**:兩個 `_claim_prepared_dispatch` 呼叫端(`_run_episode` 與
    `podcast_series` 的 inline 重送分支)都要把**原始承諾**的秒數存進
    `dispatch["wait_timeout"]`——只補一條正是 AGENTS.md 點名的病灶(v0.8.0 的
    failover 就是這樣只補一條)。這裡先鎖 `_run_episode` 那條(`podcast_episode`
    是它唯一的公開入口)。
    """
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [], wait_timeout=7200
    )
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["attempt_id"] == attempt_id
    assert attempt["dispatch"]["wait_timeout"] == 7200.0, attempt["dispatch"]


async def test_series_resend_dispatch_also_persists_the_wait_timeout_promise(
    fake_client, tmp_path
):
    """同一條紀律的第二個呼叫端:`podcast_series` re-arm 一顆 `not_accepted` attempt
    後、真正 dispatch 前也要走 `_claim_prepared_dispatch`(:3549 附近的 inline 分支)
    ——這條路徑跟 `_run_episode` 是**分開**補的,漏一條全季重送都測不出來(第一次
    dispatch 就失敗才會走到這裡)。
    """
    episodes = [{"title": "心法篇", "brief": "1"}]

    fake_client.artifacts.fail_generate = True
    await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path), wait_timeout=4321
    )
    fake_client.artifacts.fail_generate = False

    # 第二次呼叫走 re-arm → prepared → `_claim_prepared_dispatch` 那條 inline 分支。
    await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path), wait_timeout=4321
    )

    stored = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["wait_timeout"] == 4321.0, attempt["dispatch"]


def test_reset_attempt_for_resend_clears_the_stale_wait_timeout_promise():
    """**P2(Codex adversarial review 實跑驗證):新增的持久化欄位沒有跟著清理義務
    走。**

    `wait_timeout` 是**這次 dispatch** 的持久化承諾(見 `_validate_wait_timeout`
    docstring),`_reset_attempt_for_resend` 把 attempt 就地清回乾淨的 `prepared`
    時代表這次 dispatch 已經不存在了,留著上一輪的 `wait_timeout` 會被下一次對帳
    誤當成「這次」的窗判準。Codex 實跑重現:`wait_timeout=7200` 的 dispatch 被拒 →
    `not_accepted` → 以 `wait_timeout=60` 重試 → reset 成 `prepared` → claim 前的
    baseline RPC 失敗,留下的 manifest 是 `status=prepared`／`dispatched_at=None`／
    `wait_timeout=7200.0`——沒有當前 dispatch,卻留著上一輪的 dispatch-specific
    timeout。同一個函式已經在清 `account`(v0.8.0 驗收 F-4 的教訓),`wait_timeout`
    要跟著清在旁邊。

    突變驗證:把新加的 `dispatch.pop("wait_timeout", None)` 拿掉就會紅。
    """
    attempt = {
        "dispatch": {
            "status": "not_accepted",
            "artifact_ids_before": ["stale-baseline-id"],
            "dispatched_at": "2026-01-01T00:00:00+00:00",
            "accepted_at": "2026-01-01T00:00:01+00:00",
            "account": "stale-account",
            "wait_timeout": 7200.0,
        },
        "remote": {
            "status": "failed",
            "status_origin": "remote",
            "observed_at": "2026-01-01T00:00:02+00:00",
            "error": "quota exceeded",
            "error_code": "RESOURCE_EXHAUSTED",
        },
    }

    p._reset_attempt_for_resend(attempt)

    dispatch = attempt["dispatch"]
    assert "wait_timeout" not in dispatch, dispatch
    assert "account" not in dispatch, dispatch
    assert dispatch["status"] == "prepared"
    assert dispatch["artifact_ids_before"] == []
    assert dispatch["dispatched_at"] is None
    assert dispatch["accepted_at"] is None


@pytest.mark.parametrize("bad_wait_timeout", [0, -1, float("nan"), float("inf"), float("-inf")])
async def test_podcast_episode_rejects_a_bad_wait_timeout_before_any_dispatch(
    fake_client, tmp_path, bad_wait_timeout
):
    """**item 3(第四輪修復)**:`wait_timeout` 被 `_claim_prepared_dispatch` 持久化
    之後,升級成之後每一次對帳的安全窗判準——一個沒有信任邊界檢查的呼叫端輸入,不該
    直接變成長期有效的安全參數。`podcast_episode` 之前完全不驗證這個參數,`nan` 存進
    `dispatch["wait_timeout"]` 後,`timedelta(seconds=nan)` 會在往後每一次 reconcile
    都炸掉——那顆 attempt 永久對帳不了。`<= 0` 也擋不住 `nan`(`nan <= 0` 恆假),
    所以要單獨測 `math.isfinite`。驗證必須在任何 dispatch 之前(`fake_client.artifacts.calls`
    保持空)。
    """
    with pytest.raises(ValueError, match="wait_timeout must be a finite number greater than zero"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            wait_timeout=bad_wait_timeout,
        )
    assert fake_client.artifacts.calls == []


@pytest.mark.parametrize("bad_wait_timeout", [0, -1, float("nan"), float("inf"), float("-inf")])
async def test_podcast_series_rejects_a_bad_wait_timeout_before_any_dispatch(
    fake_client, tmp_path, bad_wait_timeout
):
    """同一條紀律的第二個入口(AGENTS.md 紀律①:兩個 dispatch 入口都要補,只補一個
    正是反覆出現的病灶)。`podcast_series` 是全季的另一條路,不驗證的話同樣的 `nan`
    會透過它的 `_claim_prepared_dispatch` 呼叫點(re-arm inline 分支)存進 manifest。
    """
    episodes = [{"title": "心法篇", "brief": "1"}]
    with pytest.raises(ValueError, match="wait_timeout must be a finite number greater than zero"):
        await p.podcast_series(
            "nb-1", episodes=episodes, output_dir=str(tmp_path), wait_timeout=bad_wait_timeout,
        )
    assert fake_client.artifacts.calls == []


async def test_reconcile_honors_the_original_promise_over_a_smaller_retry_timeout(
    fake_client, tmp_path
):
    """**P1 真實復現(整合 repro,三個 agent 獨立收斂)**:原始
    `podcast_episode(wait_timeout=7200)` 因 response lost 留在 acceptance_unknown,
    4000 秒後才用 `wait_timeout=1`(或忘記傳、落回預設 1200)對帳。原始承諾的 7200 秒
    根本還沒到,`max(這次呼叫的 wait_timeout, _RECONCILIATION_MIN_WINDOW=3600)` 那種
    floor 猜法會把它判成「窗已關」——這裡鎖住:有持久化的原始承諾就必須贏過 floor。

    突變驗證:把 `_promised_reconciliation_window_seconds` 改回只看這次呼叫的
    `wait_timeout`(忽略 `dispatch.get("wait_timeout")`),這條會從 `ACTION_RECONCILE`
    變成 `ACTION_RETRACT` 而紅——因為 floor 只有 3600 秒,4000 秒早就超過,但真正
    承諾的 7200 秒還沒到。
    """
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [], wait_timeout=7200
    )
    store = p.ManifestStore(str(manifest_path))

    def backdate(manifest: dict) -> None:
        _, attempt = p._attempt_record(manifest, 1, attempt_id)
        attempt["dispatch"]["dispatched_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=4000)
        ).isoformat()

    store.update(backdate)

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id, wait_timeout=1,
    )
    assert out["safe_next_action"] == p.ACTION_RECONCILE, (
        f"原始承諾 7200 秒還沒到(只過了 4000 秒),不該被 wait_timeout=1 誤判成窗已關:{out}"
    )
    assert "已經關了" not in out["next_step"], out["next_step"]


async def test_legacy_attempt_without_a_persisted_promise_still_uses_the_floor(
    fake_client, tmp_path
):
    """**legacy fallback**:v0.9.9 以前建立的 attempt,dispatch 裡沒有 `wait_timeout`
    欄位(那時還沒有這個修復)。讀不到持久化值時必須退回 v0.9.9 的保守下限
    (`max(這次呼叫的 wait_timeout, _RECONCILIATION_MIN_WINDOW)`),不能因為讀不到
    就當作 0 秒或直接爆炸。用 `del` 移掉欄位模擬「這顆是 fix 之前建立的」。

    跟上一條(`test_reconcile_honors_the_original_promise_over_a_smaller_retry_timeout`)
    互補:同樣 backdate 4000 秒,那條有持久化的 7200 秒承諾 → 窗還沒關;這條沒有
    持久化值 → 退回 floor(3600 秒)→ 4000 秒已經超過 → 窗真的關了。兩者的反差正是
    這條 fallback 存在的理由。
    """
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [], wait_timeout=7200
    )
    store = p.ManifestStore(str(manifest_path))

    def strip_and_backdate(manifest: dict) -> None:
        _, attempt = p._attempt_record(manifest, 1, attempt_id)
        del attempt["dispatch"]["wait_timeout"]  # 模擬 legacy manifest 沒有這個欄位
        attempt["dispatch"]["dispatched_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=4000)
        ).isoformat()

    store.update(strip_and_backdate)

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id, wait_timeout=1,
    )
    assert out["safe_next_action"] == p.ACTION_RETRACT, out


async def test_reconciliation_closure_floor_can_exceed_the_candidate_window(
    fake_client, tmp_path
):
    """**item 4(第四輪修復,補一條真的走 floor 的測試)**:上一輪的
    `test_reconciliation_window_closure_has_a_conservative_floor_the_caller_cannot_shrink`
    兩個 backdate 值不管有沒有套 floor 都會得到同一個答案(1200 秒的承諾／3600 秒的
    floor 對那兩個 backdate 值來說誰贏都無所謂)——那條測試從沒有真的證明 floor 生效。

    這裡用一個**很小**的持久化承諾(60 秒)同時證兩件事(核心裁決:兩個窗的保守
    方向相反,不能共用同一個判準):
    - **關閉判斷窗**必須靠 `_RECONCILIATION_MIN_WINDOW`(1 小時)撐開,不能只信
      60 秒的承諾——backdate 1500 秒還沒超過 floor,窗不該被判成已關。
    - **候選篩選窗**不能被同一個 floor 撐大——backdate 1500 秒後才冒出的 artifact
      早就落在 60 秒承諾之外,不該被誤判成這次 dispatch 的候選。

    突變驗證:
    ①拿掉關閉判斷的 floor(`max(promised, _RECONCILIATION_MIN_WINDOW...)` 改回只用
    `promised`),這條會從 `ACTION_RECONCILE` 變成 `ACTION_RETRACT` 而紅
    (60+60 秒的窗遠遠撐不到 1500 秒)。
    ②讓候選篩選窗也套 floor(跟關閉判斷共用同一個值),這條會從「0 候選」變成
    「1 候選、resume」而紅(floor=3600 秒遠大於 1500 秒,遲來的 artifact 會被誤判
    成這次 dispatch 的候選,綁進錯的 attempt)。
    """
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [], wait_timeout=60,
    )
    store = p.ManifestStore(str(manifest_path))

    def backdate(manifest: dict) -> None:
        _, attempt = p._attempt_record(manifest, 1, attempt_id)
        attempt["dispatch"]["dispatched_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1500)
        ).isoformat()

    store.update(backdate)
    # 建立時間是「現在」,相對於 1500 秒前的 dispatched_at、60 秒的承諾窗,這是一顆
    # 跟這次 dispatch 無關的遲到 artifact——候選窗不該收它。
    fake_client.artifacts.artifacts = [_remote_audio("unrelated-late-artifact")]

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id, wait_timeout=1,
    )
    assert out.get("artifact_id") is None, (
        f"候選窗(60 秒承諾,不套 floor)早就關了,這顆遲來的 artifact 不該被當成候選:{out}"
    )
    assert out["safe_next_action"] == p.ACTION_RECONCILE, (
        f"關閉判斷窗有 floor(1 小時)撐著,持久化承諾只有 60 秒不該讓它被判成已關:{out}"
    )
    assert "已經關了" not in out["next_step"], out["next_step"]


async def test_candidate_window_also_honors_the_original_promise(
    fake_client, tmp_path
):
    """**P1 的第二個窗**:候選 artifact 篩選窗(`candidate_window_end`)也要讀
    `_promised_reconciliation_window_seconds`(原始承諾與這次呼叫取大),不能只信
    這次呼叫的 `wait_timeout=1`——那會先把「4000 秒後才建立」的真 artifact 排除在
    候選窗外,零候選出口再誤判窗已關建議 retract——兩個 bug 疊在一起,實際後果是
    明明有 artifact 卻教人 retract。

    ⚠️ **第四輪修復後,候選窗不再套 `_RECONCILIATION_MIN_WINDOW` 保守下限**(核心
    裁決:兩個窗的保守方向相反,候選窗大才危險)。這個測試的 persisted 承諾是 7200
    秒、比下限(3600)大,所以候選窗恰好等於關閉判斷窗的有效秒數——這是**這個案例
    的巧合**,不是兩者共用同一個值的結構性保證;見
    `test_reconciliation_closure_floor_can_exceed_the_candidate_window` 那條才是
    兩者真正分岔的案例。

    突變驗證:把 `candidate_window_end` 改回直接吃 `wait_timeout`(不經
    `_promised_reconciliation_window_seconds`),這條會從「單一候選、resume」變成
    「零候選、retract」而紅。
    """
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [], wait_timeout=7200
    )
    store = p.ManifestStore(str(manifest_path))

    def backdate(manifest: dict) -> None:
        _, attempt = p._attempt_record(manifest, 1, attempt_id)
        attempt["dispatch"]["dispatched_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=4000)
        ).isoformat()

    store.update(backdate)
    # 建立時間是「現在」,相對於 4000 秒前的 dispatched_at 就是遲來的真 artifact。
    fake_client.artifacts.artifacts = [_remote_audio("late-real-artifact")]

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id, wait_timeout=1,
    )
    assert out.get("artifact_id") == "late-real-artifact", out
    assert out["safe_next_action"] == p.ACTION_RESUME, out


async def test_explicit_resume_cannot_replace_an_unreconciled_active_attempt(
    fake_client, tmp_path
):
    manifest_path, original_attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, []
    )
    fake_client.artifacts.generate_audio_exc = None

    with pytest.raises(ValueError, match="active attempt|先.*reconcile"):
        await p.podcast_episode_resume(
            "nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="caller-guessed-artifact",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode = stored["episodes"][0]
    assert episode["active_attempt_id"] == original_attempt_id
    assert len(episode["attempts"]) == 1
    assert episode["attempts"][0]["remote"]["artifact_id"] is None


async def test_resume_refuses_another_artifact_while_a_durable_output_exists(
    fake_client, tmp_path
):
    """已有 durable output 時,resume 不得為「另一個 artifact」開新 attempt。

    舊行為放行(只要 active == output),而當時的 `_promote_attempt_output` 沒有歸屬檢查
    ——finalize 成功就把 output 指標換掉,等於從 resume 後門做了一次無審計的取代:
    沒有 retract、沒有理由、舊紀錄不留。這是 durability guard 想擋的同一件事,只是走
    另一條路。現在必須先 `podcast_attempt_retract` 才有取代版。"""
    manifest_path = tmp_path / "series_manifest.json"
    first = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    store = p.ManifestStore(manifest_path)

    with pytest.raises(ValueError, match="already has durable output"):
        p._ensure_resume_attempt(
            store,
            notebook_id="nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="newer-artifact",
        )
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["output_attempt_id"] == first["attempt_id"]
    assert len(stored["episodes"][0]["attempts"]) == 1


async def test_claimed_artifact_resume_cannot_hide_another_active_attempt(
    fake_client, tmp_path
):
    """有 attempt 在飛時,resume 不得改抓另一個 artifact —— 包含**已被別的 attempt claim**
    的那筆(claimed 分支是 `_ensure_resume_attempt` 裡另一條建立路徑,兩條都要驗)。

    狀態經合法的 retract 造出:已有 durable output 時 resume 換 artifact 本身已被禁止。"""
    manifest_path = tmp_path / "series_manifest.json"
    first = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    retraction = await p.podcast_attempt_retract(
        str(manifest_path), 1, first["attempt_id"], reason="QA 拒收"
    )
    await b.source_delete("nb-1", retraction["stale_source_id"])

    store = p.ManifestStore(manifest_path)
    newer_attempt_id = p._ensure_resume_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="newer-artifact",
    )

    # (a) 全新、未被 claim 的第三個 artifact
    with pytest.raises(ValueError, match="has active attempt|another active attempt"):
        p._ensure_resume_attempt(
            store,
            notebook_id="nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id="second-newer-artifact",
        )
    # (b) 被作廢的那個 attempt 所 claim 的 artifact:tombstone 擋在最前面
    with pytest.raises(ValueError, match="was retracted"):
        p._ensure_resume_attempt(
            store,
            notebook_id="nb-1",
            episode_n=1,
            title="心法篇",
            artifact_id=first["artifact_id"],
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["active_attempt_id"] == newer_attempt_id


async def test_late_dispatch_outcomes_cannot_downgrade_or_replace_mapping(
    fake_client, tmp_path
):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client, tmp_path, [_remote_audio("remote-audio-1")]
    )
    await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    store = p.ManifestStore(manifest_path)
    p._mark_acceptance_unknown(
        store, episode_n=1, attempt_id=attempt_id, error=TimeoutError("late timeout")
    )
    after_timeout = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = after_timeout["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "accepted"
    assert attempt["remote"]["artifact_id"] == "remote-audio-1"

    failed_status = SimpleNamespace(
        error="late failure", error_code=None, status="failed"
    )
    p._mark_not_accepted(
        store, episode_n=1, attempt_id=attempt_id, status=failed_status
    )
    after_failure = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = after_failure["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "accepted"
    assert attempt["remote"]["status"] == "pending"

    with pytest.raises(ValueError, match="another artifact|不同"):
        p._bind_accepted_artifact(
            store, episode_n=1, attempt_id=attempt_id, artifact_id="remote-audio-2"
        )
    final = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert final["episodes"][0]["attempts"][0]["remote"]["artifact_id"] == (
        "remote-audio-1"
    )

    def mark_completed(manifest):
        _, current = p._attempt_record(manifest, 1, attempt_id)
        current["remote"]["status"] = "completed"

    store.update(mark_completed)
    p._bind_accepted_artifact(
        store,
        episode_n=1,
        attempt_id=attempt_id,
        artifact_id="remote-audio-1",
    )
    completed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert completed["episodes"][0]["attempts"][0]["remote"]["status"] == (
        "completed"
    )


async def test_reconcile_with_multiple_candidates_is_ambiguous(
    fake_client, tmp_path
):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client,
        tmp_path,
        [_remote_audio("remote-audio-1"), _remote_audio("remote-audio-2")],
    )

    out = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "reconciliation_ambiguous"
    assert attempt["remote"]["artifact_id"] is None
    assert out["complete"] is False
    assert out["observed_state"] == "reconciliation_ambiguous"
    assert out["candidate_artifact_ids"] == [
        "remote-audio-1",
        "remote-audio-2",
    ]
    # F6(獨立盲審):單候選+blocking 的 `reconciliation_ambiguous` 出口有
    # `blocking_attempt_ids`,這條多候選的出口原本沒有——同一個 `observed_state`
    # 的兩個出口 return shape 不對稱,keying 在這個欄位上的 host 在這條路會
    # KeyError。這裡真的有 2 筆以上候選,不是被別的 attempt 卡住,固定是空陣列。
    assert out["blocking_attempt_ids"] == []


async def test_adopt_selects_one_ambiguous_artifact_without_remote_side_effects(
    fake_client, tmp_path
):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client,
        tmp_path,
        [_remote_audio("remote-audio-1"), _remote_audio("remote-audio-2")],
    )
    reconciled = await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )
    assert reconciled["observed_state"] == "reconciliation_ambiguous"
    artifact_boundary = len(fake_client.artifacts.calls)
    source_boundary = len(fake_client.sources.calls)

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path),
        episode_n=1,
        attempt_id=attempt_id,
        artifact_id="remote-audio-2",
    )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert adopted["artifact_id"] == "remote-audio-2"
    assert adopted["observed_state"] == "accepted"
    assert attempt["dispatch"]["status"] == "accepted"
    assert attempt["remote"]["artifact_id"] == "remote-audio-2"
    assert [
        call
        for call in fake_client.artifacts.calls[artifact_boundary:]
        if call[0] in {"generate_audio", "rename", "download"}
    ] == []
    assert fake_client.sources.calls[source_boundary:] == []


async def test_adopt_rejects_attempt_identity_drift_before_remote_lookup(
    fake_client, tmp_path
):
    manifest_path, attempt_id = await _leave_acceptance_unknown(
        fake_client,
        tmp_path,
        [_remote_audio("remote-audio-1"), _remote_audio("remote-audio-2")],
    )
    await p.podcast_episode_reconcile(
        str(manifest_path), episode_n=1, attempt_id=attempt_id
    )

    # 繞過 ManifestStore 直接改寫 JSON 檔:真實的手改破壞本來就長這樣。只留 title
    # drift——episode/attempt notebook 分裂現在會被 manifest_store._validate 的一致性
    # guard 在任何 store.read() 就擋下(見 test_manifest_store.py),連不到這裡要測的
    # podcast_attempt_adopt 下游 identity gate,那個場景已經換一層測、由別的測試鎖住。
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    corrupted_attempt = next(
        a for a in stored["episodes"][0]["attempts"] if a["attempt_id"] == attempt_id
    )
    corrupted_attempt["title"] = "另一集"
    manifest_path.write_text(
        json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    artifact_boundary = len(fake_client.artifacts.calls)
    source_boundary = len(fake_client.sources.calls)

    with pytest.raises(ValueError, match="different notebook|title"):
        await p.podcast_attempt_adopt(
            str(manifest_path),
            episode_n=1,
            attempt_id=attempt_id,
            artifact_id="remote-audio-1",
        )

    assert fake_client.artifacts.calls[artifact_boundary:] == []
    assert fake_client.sources.calls[source_boundary:] == []


async def test_sdk_failed_status_is_not_accepted_not_transport_unknown(
    fake_client, tmp_path
):
    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.fail_generate = True

    with pytest.raises(RuntimeError, match="Generation failed"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = stored["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["status"] == "not_accepted"
    assert attempt["remote"]["artifact_id"] is None
    assert attempt["remote"]["status"] == "failed"
    assert attempt["remote"]["error"] == "simulated failure"
    assert attempt["errors"][-1]["phase"] == "dispatch"


async def test_sdk_raised_rate_limit_is_not_accepted_not_acceptance_unknown(
    fake_client, tmp_path
):
    """notebooklm-py 0.8.0(ADR-0019 / #1342)把伺服器的同步拒絕從「回傳
    status='failed'」改成 **raise**。同一件事(配額/限流)不能因為 SDK 換了表達方式
    就掉進不同的終態:

      - 0.7.x:回傳 failed → ensure_started → not_accepted → 直接重試即可
      - 0.8.0 若不分類:落進通用 except → acceptance_unknown → 逼使用者先跑一次
        註定撈不到東西的 podcast_episode_reconcile 才准重生

    後者不會弄壞資料,但把一個**乾淨的終態**謊報成「結果不明」。拒絕的契約明說
    沒有建出 task,所以標 not_accepted 才誠實。
    """
    from notebooklm.exceptions import RateLimitError

    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_audio_exc = RateLimitError("每日配額已用盡")

    with pytest.raises(RateLimitError, match="每日配額已用盡"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0][
        "attempts"
    ][0]
    assert attempt["dispatch"]["status"] == "not_accepted"
    assert attempt["remote"]["artifact_id"] is None
    assert attempt["remote"]["status"] == "failed"
    # 拒絕的**理由**必須留下來——例外沒有 .error 欄位,不轉換的話 manifest 只會
    # 留一句 "failed",而「為什麼」正是操作者唯一需要的資訊。
    assert "每日配額已用盡" in attempt["remote"]["error"]
    assert attempt["remote"]["error_code"] == "RateLimitError"
    assert attempt["errors"][-1]["phase"] == "dispatch"


async def test_generic_rpc_failure_stays_acceptance_unknown(fake_client, tmp_path):
    """反向鎖:除了契約講死「沒建出 task」的那兩種,其餘一律留在 acceptance_unknown。

    兩種誤判的代價不對稱——把拒絕誤判成 unknown 只是多跑一次對帳(便宜);把
    **已受理**誤判成拒絕會讓呼叫端直接重生,變成重複 artifact + 重燒配額。所以
    `_REFUSED_WITHOUT_DISPATCH` 不能為了訊息好看而長大。RPCError 是父類,更要確認
    它沒有因為 RateLimitError 是它的子類就被一起吃進去。
    """
    from notebooklm.exceptions import RPCError

    manifest_path = tmp_path / "series_manifest.json"
    fake_client.artifacts.generate_audio_exc = RPCError("伺服器 500")

    with pytest.raises(RPCError):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0][
        "attempts"
    ][0]
    assert attempt["dispatch"]["status"] == "acceptance_unknown"
