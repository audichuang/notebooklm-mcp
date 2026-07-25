"""QA 拒收後的正門:podcast_attempt_retract。

存在理由是一個真實事故(EP35):durability guard 只擋掉隱式重生,卻沒給任何
「這是修正、不是誤觸」的出口,於是呼叫端改去手寫 attempt 進 manifest —— 五個時間戳
同一微秒、artifact_ids_before 填自己的 artifact_id、狀態字串在 repo 裡根本不存在。
guard 沒保護 manifest,只是把寫入趕出工具外。

這些測試鎖住三件事:retract 之後重生是合法的(且**必須先刪舊回錄 source**)、被作廢的
紀錄不會消失、以及被作廢的 attempt **不可能再被復活**——不論是 retract 之前就啟動的
in-flight finalizer、還是任何把指標寫回去的 writer。
"""
import json

import pytest

from notebooklm_mcp import audio_finalize
from notebooklm_mcp import tools_basic as b
from notebooklm_mcp import tools_podcast as p
from notebooklm_mcp.manifest_store import ManifestStore


EP = {"title": "心法篇", "brief": "1"}


async def _complete_ep1(fake_client, tmp_path) -> tuple[str, dict]:
    manifest_path = str(tmp_path / "series_manifest.json")
    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title=EP["title"],
        brief=EP["brief"],
        output_dir=str(tmp_path),
        manifest_path=manifest_path,
    )
    episode = json.loads(open(manifest_path, encoding="utf-8").read())["episodes"][0]
    return manifest_path, episode


def _episode(manifest_path: str) -> dict:
    return json.loads(open(manifest_path, encoding="utf-8").read())["episodes"][0]


async def test_retract_clears_output_evidence_and_keeps_the_audit_trail(
    fake_client, tmp_path
):
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_id = before["output_attempt_id"]

    out = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="QA 拒收:把 hook 和權限邊界混為一談"
    )

    # 回傳給呼叫端的善後 handle
    assert out["stale_artifact_id"] == before["artifact_id"]
    assert out["stale_source_id"] == before["feedback_source_id"]
    assert out["retracted_mp3_path"] == before["mp3_path"]
    # 舊回錄 source 與取代版同名,continuity 複驗要求恰好一筆 → 必須先刪
    assert out["safe_next_action"] == "source_delete"
    assert out["observed_state"] == "retracted"

    after = _episode(manifest_path)
    # guard 讀的 episode 級證據必須「不存在」,不是 None:_promote_attempt_output 用
    # setdefault 寫 published_at,留 None 會讓取代版補不回真正的產製時間(publish 有
    # fallback,只會靜默發假日期)。
    for key in ("output_attempt_id", "artifact_id", "task_id", "mp3_path", "published_at"):
        assert key not in after, key
    assert "active_attempt_id" not in after
    # 作廢的 attempt 與它的 finalize 紀錄整份留著,只是多了 retraction
    retracted = after["attempts"][0]
    assert retracted["attempt_id"] == attempt_id
    assert retracted["finalize"]["download"]["sha256"]
    assert retracted["retraction"]["reason"].startswith("QA 拒收")
    assert retracted["retraction"]["retracted_output"]["artifact_id"] == before["artifact_id"]
    assert after["retracted_attempt_ids"] == [attempt_id]
    # 沿用 adopt 既有的歷史欄位 + 一筆「還沒做完」的清理義務
    assert after["previous_feedback_source_ids"] == [before["feedback_source_id"]]
    assert after["pending_source_cleanup"] == [before["feedback_source_id"]]


async def test_replacement_requires_deleting_the_stale_source_first(
    fake_client, tmp_path
):
    """漏刪舊回錄 source 不能只靠文件提醒:finalize 是按 source_id 驗的,不擋同名,
    漏了會靜默留下兩筆同名 media 污染後續每一次生成的 context。故生成前 fail-closed。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    stale = before["feedback_source_id"]
    await p.podcast_attempt_retract(manifest_path, 1, before["output_attempt_id"], reason="QA")
    call_boundary = len(fake_client.artifacts.calls)

    with pytest.raises(ValueError, match="retracted feedback sources still in the notebook"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後的 brief",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    # 一次生成配額都沒燒,也沒建新 attempt
    assert not [c for c in fake_client.artifacts.calls[call_boundary:] if c[0] == "generate_audio"]
    assert len(_episode(manifest_path)["attempts"]) == 1

    await b.source_delete("nb-1", stale)
    await p.podcast_episode(
        "nb-1", episode_n=1, title=EP["title"], brief="修正後的 brief",
        output_dir=str(tmp_path), manifest_path=manifest_path,
    )

    after = _episode(manifest_path)
    assert "pending_source_cleanup" not in after           # 義務結案
    assert len(after["attempts"]) == 2
    assert after["output_attempt_id"] == after["attempts"][1]["attempt_id"]
    assert after["published_at"]                            # setdefault 欄位補回來了
    # 拒收版的 mp3 沒被覆寫(episode 仍有 durable evidence → 取代版走 attempts/ 子目錄)
    assert after["mp3_path"] != before["mp3_path"]
    assert f"attempts/{after['output_attempt_id']}" in after["mp3_path"]
    # 筆記本裡只剩取代版那一筆同名來源
    assert fake_client.sources.titles().count("EP01 心法篇") == 1


async def test_retract_replacement_cannot_change_the_title(fake_client, tmp_path):
    """標題綁 label／工作室 artifact 名／回錄來源名／發布標題與封面——改標題不是重生。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    await p.podcast_attempt_retract(manifest_path, 1, before["output_attempt_id"], reason="QA")
    await b.source_delete("nb-1", before["feedback_source_id"])

    with pytest.raises(ValueError, match="title cannot change in a retract replacement"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title="改過的標題", brief="修正後的 brief",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    assert _episode(manifest_path)["title"] == EP["title"]


# ---- 復活防護:retract 之後,被作廢的 attempt 不得再被 finalize／promote --------------

async def test_inflight_finalizer_cannot_resurrect_a_retracted_attempt(
    fake_client, tmp_path
):
    """真實併發窗口:finalize 在 retract 之前啟動,retract 提交,finalize 才回來寫。

    promotion 是它最後一個寫入點,也是唯一會復活 episode 級投影欄位的地方;所有
    finalize checkpoint 的讀寫也都走 audio_finalize._record。兩邊都要擋。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_id = before["output_attempt_id"]
    store = ManifestStore(manifest_path)
    stale_output = {                      # in-flight finalizer 手上的舊結果
        "artifact_id": before["artifact_id"],
        "mp3_path": before["mp3_path"],
        "title": before["title"],
        "label": before["label"],
        "published_at": before["published_at"],
        "feedback_source_id": before["feedback_source_id"],
    }

    await p.podcast_attempt_retract(manifest_path, 1, attempt_id, reason="QA")

    with pytest.raises(ValueError, match="was retracted"):
        p._promote_attempt_output(store, 1, attempt_id, stale_output)
    with pytest.raises(ValueError, match="was retracted"):
        await audio_finalize.finalize_attempt(
            fake_client, store, episode_n=1, attempt_id=attempt_id,
            output_dir=str(tmp_path), wait_timeout=5,
        )

    after = _episode(manifest_path)
    assert "output_attempt_id" not in after
    assert "published_at" not in after


async def test_manifest_write_rejects_a_pointer_back_to_a_retracted_attempt(
    fake_client, tmp_path
):
    """最後一道背壩:任何 writer(含手改)把指標指回作廢的 attempt,那次寫入就失敗。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_id = before["output_attempt_id"]
    await p.podcast_attempt_retract(manifest_path, 1, attempt_id, reason="QA")

    store = ManifestStore(manifest_path)
    for pointer in ("output_attempt_id", "active_attempt_id"):
        with pytest.raises(ValueError, match="points at retracted attempt"):
            store.update(
                lambda manifest, key=pointer: manifest["episodes"][0].update(
                    {key: attempt_id}
                )
            )
    assert "output_attempt_id" not in _episode(manifest_path)


async def test_retract_is_idempotent_and_reasserts_the_cleared_state(
    fake_client, tmp_path
):
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_id = before["output_attempt_id"]

    first = await p.podcast_attempt_retract(manifest_path, 1, attempt_id, reason="QA")
    again = await p.podcast_attempt_retract(manifest_path, 1, attempt_id, reason="別的理由")
    assert again["retracted_at"] == first["retracted_at"]      # 不重寫審計紀錄
    assert again["reason"] == first["reason"]
    assert _episode(manifest_path)["retracted_attempt_ids"] == [attempt_id]

    # 有 writer 把 episode 級投影欄位塞回來(pointer 有 _validate 擋,值沒有):
    # 冪等重呼必須重新讓 postcondition 成立,不能只回舊紀錄就走。
    ManifestStore(manifest_path).update(
        lambda manifest: manifest["episodes"][0].update(
            {"artifact_id": before["artifact_id"], "published_at": before["published_at"]}
        )
    )
    await p.podcast_attempt_retract(manifest_path, 1, attempt_id, reason="QA")
    after = _episode(manifest_path)
    assert "artifact_id" not in after and "published_at" not in after


async def test_resume_cannot_bypass_the_cleanup_obligation(fake_client, tmp_path):
    """resume 也會 upload 回錄 source,所以清理義務的 gate 不能只擺在生成那條路。

    (第一輪修補真的漏了這條:complete → retract → 不刪 → resume 另一個 artifact,
    finalize 照跑完,結果 notebook 裡兩筆同名 media,而義務還掛著。)"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    await p.podcast_attempt_retract(manifest_path, 1, before["output_attempt_id"], reason="QA")
    fake_client.artifacts.seed_artifacts(
        type("A", (), {"id": "art-rescued", "kind": None, "title": "Audio Overview",
                       "is_completed": True, "status_str": "completed", "created_at": None})()
    )

    with pytest.raises(ValueError, match="retracted feedback sources still in the notebook"):
        await p.podcast_episode_resume(
            "nb-1", episode_n=1, title=EP["title"], artifact_id="art-rescued",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    assert len(_episode(manifest_path)["attempts"]) == 1     # 連 attempt 都沒建


async def test_resume_cannot_rename_the_episode(fake_client, tmp_path):
    """標題守衛不能只擺在 _create_audio_attempt:resume 也會建 attempt。

    帶新標題進來只會把 artifact／回錄 source 改名成新 label,而 episode 與發布仍用舊
    標題——身分就此分岔。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    await p.podcast_attempt_retract(manifest_path, 1, before["output_attempt_id"], reason="QA")
    await b.source_delete("nb-1", before["feedback_source_id"])

    with pytest.raises(ValueError, match="title does not match the manifest"):
        await p.podcast_episode_resume(
            "nb-1", episode_n=1, title="改過的標題", artifact_id="art-rescued",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    assert _episode(manifest_path)["title"] == EP["title"]


async def test_adopt_cannot_rewrite_a_retracted_attempt(fake_client, tmp_path):
    """作廢的 attempt 是歷史紀錄:adopt 改寫它的 finalize source checkpoint,會讓已登記的
    清理義務指向錯的 source(刪了記錄那筆、真正的舊來源還在)。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_id = before["output_attempt_id"]
    await p.podcast_attempt_retract(manifest_path, 1, attempt_id, reason="QA")
    other = fake_client.sources._add(
        f"EP01 {EP['title']}", kind="media", is_ready=True
    )

    with pytest.raises(ValueError, match="was retracted"):
        await p.podcast_attempt_adopt(
            manifest_path, 1, attempt_id=attempt_id, feedback_source_id=other
        )
    retracted = _episode(manifest_path)["attempts"][0]
    assert retracted["finalize"]["feedback_source_upload"]["source_id"] == (
        before["feedback_source_id"]
    )


async def test_delayed_retract_retry_does_not_destroy_the_replacement(
    fake_client, tmp_path
):
    """A 作廢、B 已成為取代版之後,再重呼 retract(A) 不得清掉 B 的輸出投影。

    冪等只能對「自己留下的殘留值」生效——無條件重跑 pop 會把 B 的 artifact_id／
    mp3_path／published_at 一起清掉,等於毀掉取代版。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_a = before["output_attempt_id"]
    await p.podcast_attempt_retract(manifest_path, 1, attempt_a, reason="QA")
    await b.source_delete("nb-1", before["feedback_source_id"])
    await p.podcast_episode(
        "nb-1", episode_n=1, title=EP["title"], brief="修正後的 brief",
        output_dir=str(tmp_path), manifest_path=manifest_path,
    )
    replacement = _episode(manifest_path)

    await p.podcast_attempt_retract(manifest_path, 1, attempt_a, reason="QA")

    after = _episode(manifest_path)
    assert after["output_attempt_id"] == replacement["output_attempt_id"]
    assert after["artifact_id"] == replacement["artifact_id"]
    assert after["mp3_path"] == replacement["mp3_path"]
    assert after["published_at"] == replacement["published_at"]


async def test_claimed_artifact_resume_cannot_steal_active_from_the_output(
    fake_client, tmp_path
):
    """第三個入口:`_ensure_resume_attempt` 的 **claimed** 分支。

    一筆「歷史上曾被 claim 過」的 artifact 不必新建 attempt,所以第二輪加在新建分支的
    output／標題兩道 gate 完全繞過去 —— 直接把 active 從現任 output 手上搶走,復刻
    active=B／output=A 的死鎖。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    store = ManifestStore(manifest_path)
    store.update(                                  # 歷史上被 claim 過的另一筆 artifact
        lambda manifest: manifest["episodes"][0]["attempts"].append(
            {
                "attempt_id": "att-historical",
                "episode": 1,
                "title": EP["title"],
                "notebook_id": "nb-1",
                "dispatch": {"status": "accepted"},
                "remote": {"artifact_id": "art-historical", "status": "pending"},
                "finalize": audio_finalize.new_finalize_state(),
            }
        )
    )

    with pytest.raises(ValueError, match="already has durable output"):
        p._ensure_resume_attempt(
            store, notebook_id="nb-1", episode_n=1, title=EP["title"],
            artifact_id="art-historical",
        )
    after = _episode(manifest_path)
    assert after["active_attempt_id"] == before["output_attempt_id"]   # 沒被搶走


async def test_retract_can_abandon_an_unauthorized_candidate_to_break_a_split(
    fake_client, tmp_path
):
    """舊版工具/手改留下的 active=B／output=A 分岔必須有出路,而且要留審計。

    B 從未 promote,所以作廢它**不得動到** episode 級投影(那些屬於 A)。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    store = ManifestStore(manifest_path)
    store.update(
        lambda manifest: manifest["episodes"][0]["attempts"].append(
            {
                "attempt_id": "att-split",
                "episode": 1,
                "title": EP["title"],
                "notebook_id": "nb-1",
                "dispatch": {"status": "accepted"},
                "remote": {"artifact_id": "art-split", "status": "pending"},
                "finalize": audio_finalize.new_finalize_state(),
            }
        )
        or manifest["episodes"][0].update({"active_attempt_id": "att-split"})
    )

    # 先作廢那個沒人授權的 candidate:A 的輸出投影一個字都不能少
    await p.podcast_attempt_retract(manifest_path, 1, "att-split", reason="放棄的 candidate")
    mid = _episode(manifest_path)
    assert mid["output_attempt_id"] == before["output_attempt_id"]
    assert mid["artifact_id"] == before["artifact_id"]
    assert mid["mp3_path"] == before["mp3_path"]
    assert "active_attempt_id" not in mid
    # 分岔解開後,才輪到正常的 retract output
    out = await p.podcast_attempt_retract(
        manifest_path, 1, before["output_attempt_id"], reason="QA 拒收"
    )
    assert out["safe_next_action"] == "source_delete"
    assert "output_attempt_id" not in _episode(manifest_path)


async def test_promote_refuses_when_another_attempt_owns_the_output(
    fake_client, tmp_path
):
    """promotion 的歸屬檢查:沒有它,resume 後門一 finalize 成功就換掉 output 指標。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    store = ManifestStore(manifest_path)
    store.update(
        lambda manifest: manifest["episodes"][0]["attempts"].append(
            {
                "attempt_id": "att-other",
                "episode": 1,
                "title": EP["title"],
                "notebook_id": "nb-1",
                "dispatch": {"status": "accepted"},
                "remote": {"artifact_id": "art-other", "status": "pending"},
                "finalize": audio_finalize.new_finalize_state(),
            }
        )
    )
    with pytest.raises(ValueError, match="output is owned by"):
        p._promote_attempt_output(
            store, 1, "att-other",
            {
                "artifact_id": "art-other",
                "mp3_path": str(tmp_path / "other.mp3"),
                "title": EP["title"],
                "label": before["label"],
                "published_at": before["published_at"],
            },
        )
    assert _episode(manifest_path)["output_attempt_id"] == before["output_attempt_id"]


async def test_cleanup_cannot_be_discharged_from_another_notebook(
    fake_client, tmp_path
):
    """義務綁在 manifest 那個 notebook 上:拿別的(空的)notebook 來查會「查無此 source」,
    把義務誤判成已結案,而舊來源其實還躺在真正的筆記本裡。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    await p.podcast_attempt_retract(manifest_path, 1, before["output_attempt_id"], reason="QA")

    with pytest.raises(ValueError, match="belongs to notebook"):
        await p._assert_source_cleanup_done(
            fake_client, ManifestStore(manifest_path), "nb-wrong", 1
        )
    assert _episode(manifest_path)["pending_source_cleanup"] == [
        before["feedback_source_id"]
    ]


async def test_cleanup_does_not_swallow_an_obligation_added_during_the_check(
    fake_client, tmp_path
):
    """gate 先 snapshot、再 await sources.list、再寫回:await 期間追加的新義務不得被吞掉。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    await p.podcast_attempt_retract(manifest_path, 1, before["output_attempt_id"], reason="QA")
    await b.source_delete("nb-1", before["feedback_source_id"])
    store = ManifestStore(manifest_path)
    real_list = fake_client.sources.list

    async def racing_list(notebook_id):
        store.update(                       # 另一個 writer 在 await 中追加義務
            lambda manifest: manifest["episodes"][0]
            .setdefault("pending_source_cleanup", [])
            .append("src-added-during-await")
        )
        return await real_list(notebook_id)

    fake_client.sources.list = racing_list
    try:
        await p._assert_source_cleanup_done(fake_client, store, "nb-1", 1)
    finally:
        fake_client.sources.list = real_list
    assert _episode(manifest_path)["pending_source_cleanup"] == ["src-added-during-await"]


async def test_retract_refuses_an_attempt_that_is_not_the_durable_output(
    fake_client, tmp_path
):
    """生成中／未 promote 的 attempt 不是 retract 的守備範圍(該用 reconcile／resume)。"""
    manifest_path = str(tmp_path / "series_manifest.json")
    fake_client.artifacts.fail_wait_on = 1        # 等待階段逾時 → attempt 在飛但沒 output
    stopped = await p.podcast_series(
        "nb-1", episodes=[EP], output_dir=str(tmp_path)
    )
    assert stopped["complete"] is False
    episode = _episode(manifest_path)
    assert "output_attempt_id" not in episode or episode["output_attempt_id"] is None

    with pytest.raises(ValueError, match="is not episode 1's durable output"):
        await p.podcast_attempt_retract(
            manifest_path, 1, stopped["attempt_id"], reason="想抄捷徑"
        )


async def test_retract_refuses_while_another_attempt_is_active(fake_client, tmp_path):
    """resume 可以把 active 指到另一個 artifact 而 output 不變;此時作廢 output 會讓那個
    attempt 變成無人授權的取代版,被 series 直接 promote。先收斂它再 retract。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    fake_client.artifacts.seed_artifacts(
        type("A", (), {"id": "art-other", "kind": None, "title": "EP01 心法篇"})()
    )
    ManifestStore(manifest_path).update(          # 模擬 resume 造出的第二個 candidate
        lambda manifest: manifest["episodes"][0]["attempts"].append(
            {
                "attempt_id": "att-other",
                "episode": 1,
                "title": EP["title"],
                "notebook_id": "nb-1",
                "dispatch": {"status": "accepted"},
                "remote": {"artifact_id": "art-other", "status": "pending"},
                "finalize": audio_finalize.new_finalize_state(),
            }
        )
        or manifest["episodes"][0].update({"active_attempt_id": "att-other"})
    )

    with pytest.raises(ValueError, match="still has active attempt"):
        await p.podcast_attempt_retract(
            manifest_path, 1, before["output_attempt_id"], reason="QA"
        )
    assert _episode(manifest_path)["output_attempt_id"] == before["output_attempt_id"]


async def test_retract_rejects_bad_arguments_before_touching_the_manifest(
    fake_client, tmp_path
):
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_id = before["output_attempt_id"]

    with pytest.raises(ValueError, match="reason"):       # 無聲作廢正是要取代的東西
        await p.podcast_attempt_retract(manifest_path, 1, attempt_id, reason="  ")
    with pytest.raises(ValueError, match="episode_n"):
        await p.podcast_attempt_retract(manifest_path, 0, attempt_id, reason="QA")
    with pytest.raises(ValueError, match="attempt_id"):
        await p.podcast_attempt_retract(manifest_path, 1, "", reason="QA")
    with pytest.raises(ValueError, match="attempt .* is missing"):
        await p.podcast_attempt_retract(manifest_path, 1, "att-nope", reason="QA")

    assert _episode(manifest_path)["output_attempt_id"] == attempt_id   # 一個字都沒改
