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
from datetime import datetime, timedelta, timezone

import pytest

from notebooklm_mcp import audio_finalize
from notebooklm_mcp import tools_basic as b
from notebooklm_mcp import tools_podcast as p
from notebooklm_mcp._status import TerminalGenerationError
from notebooklm_mcp.manifest_store import ManifestStore


EP = {"title": "心法篇", "brief": "1"}


class _TickingDatetime(datetime):
    """單調遞增的假鐘,供 published_at 回歸測試用。

    conftest 的 fake finalize 用真的 `datetime.now()` 算 published_at;測試若跑得夠
    快,兩次生成可能落在同一秒,讓 bug(補回重生時間)跟 fix(釘住首發時間)巧合地
    產出同一個字串,測不出差別。換一個保證嚴格遞增的鐘,兩次「現在」永遠不同。"""

    _tick = 0

    @classmethod
    def now(cls, tz=None):
        _TickingDatetime._tick += 1
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            hours=_TickingDatetime._tick
        )
        return moment.astimezone(tz) if tz else moment


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


# ---- pubDate 不得漂移(真實事故:saa-drill EP05/EP09)---------------------------

def test_first_published_at_skips_abandoned_empty_retraction():
    """`abandons_unauthorized_candidate` 分支(從未 promote 就被作廢的 candidate)留下
    的 `retracted_output` 是空 dict——不能被誤判成「這集從沒發過」而提前 return None,
    要跳過去找下一筆真的非空的。"""
    episode = {
        "attempts": [
            {"attempt_id": "att-abandoned", "retraction": {"retracted_output": {}}},
            {
                "attempt_id": "att-a",
                "retraction": {
                    "retracted_output": {
                        "published_at": "Fri, 24 Jul 2026 09:00:00 +0800"
                    }
                },
            },
        ]
    }
    assert p._first_published_at(episode) == "Fri, 24 Jul 2026 09:00:00 +0800"
    assert p._first_published_at({"attempts": []}) is None


async def test_replacement_keeps_the_original_published_at_not_the_regeneration_time(
    fake_client, tmp_path, monkeypatch
):
    """真實事故(saa-drill EP05/EP09):重生一集不能讓 pubDate 漂成重生時間——GUID 不變
    ＝同集更新,episodic feed 按 pubDate 倒序(tools_publish.py 的 fallback 註解自己
    宣告 pubDate 隨集號遞增是系統不變量),漂移會讓重生集跳到列表最前面。"""
    monkeypatch.setattr(audio_finalize, "datetime", _TickingDatetime)
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    first_published_at = before["published_at"]
    await p.podcast_attempt_retract(manifest_path, 1, before["output_attempt_id"], reason="QA 拒收")
    await b.source_delete("nb-1", before["feedback_source_id"])

    returned = await p.podcast_episode(
        "nb-1", episode_n=1, title=EP["title"], brief="修正後的 brief",
        output_dir=str(tmp_path), manifest_path=manifest_path,
    )

    after = _episode(manifest_path)
    assert after["published_at"] == first_published_at
    # **回傳值也要是首發時間**。第一版只在寫 manifest 時蓋掉,回傳的仍是 finalize 產的
    # 「這次生成完成的時刻」——feed 對、但任何相信回傳值的呼叫端拿到錯的 pubDate
    # (v0.6.0 實測:manifest 12:07:20、回傳值 12:16:39)。這就是本 repo 的頭號教訓
    # 「補一半等於沒補」,所以這條斷言必須跟著 manifest 那條一起在。
    assert returned["published_at"] == first_published_at


async def test_second_replacement_still_keeps_the_original_published_at(
    fake_client, tmp_path, monkeypatch
):
    """多次重生:再 retract 取代版、再生一次,仍要等於最初首發值——不是「上一版」的
    時間漂進來,是本來就沒動過的第一筆。"""
    monkeypatch.setattr(audio_finalize, "datetime", _TickingDatetime)
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    first_published_at = before["published_at"]

    await p.podcast_attempt_retract(manifest_path, 1, before["output_attempt_id"], reason="QA")
    await b.source_delete("nb-1", before["feedback_source_id"])
    await p.podcast_episode(
        "nb-1", episode_n=1, title=EP["title"], brief="第二版",
        output_dir=str(tmp_path), manifest_path=manifest_path,
    )
    replacement = _episode(manifest_path)
    assert replacement["published_at"] == first_published_at

    await p.podcast_attempt_retract(
        manifest_path, 1, replacement["output_attempt_id"], reason="QA 再拒收"
    )
    await b.source_delete("nb-1", replacement["feedback_source_id"])
    await p.podcast_episode(
        "nb-1", episode_n=1, title=EP["title"], brief="第三版",
        output_dir=str(tmp_path), manifest_path=manifest_path,
    )

    final = _episode(manifest_path)
    assert final["published_at"] == first_published_at


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
    # setdefault 欄位補回來了,而且是首發時間,不是這次重生的完成時間
    assert after["published_at"] == before["published_at"]
    # 拒收版的 mp3 沒被覆寫(episode 仍有 durable evidence → 取代版走 attempts/ 子目錄)
    assert after["mp3_path"] != before["mp3_path"]
    assert f"attempts/{after['output_attempt_id']}" in after["mp3_path"]
    # 筆記本裡只剩取代版那一筆同名來源
    assert fake_client.sources.titles().count("EP01 心法篇") == 1


@pytest.mark.parametrize(
    "exc_type", [TerminalGenerationError, RuntimeError, ConnectionError]
)
async def test_series_does_not_mask_an_early_error_with_a_lookup_crash(
    fake_client, tmp_path, monkeypatch, exc_type
):
    """retract 之後 active_attempt_id／output_attempt_id 都已被刪掉;series 重跑該集若
    在建立新 attempt 之前就撞到被攔的例外(例如清理驗證途中斷線、或伺服器終態失敗),
    except handler 裡無 default 的 next() 與 row["active_attempt_id"] 不能把它換成
    StopIteration／KeyError——呼叫端必須看到原始例外「型別與訊息」(L4)。三種例外
    型別各對應 `podcast_series` 攔的三個 except handler
    (TerminalGenerationError／RuntimeError／(TimeoutError, ConnectionError)),缺一就會
    漏鎖對應那條路徑——第一輪修復只鎖了 ConnectionError 那條,revert 另兩條照樣全綠。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    stale = before["feedback_source_id"]
    await p.podcast_attempt_retract(manifest_path, 1, before["output_attempt_id"], reason="QA 拒收")
    await b.source_delete("nb-1", stale)  # 清理義務結案,series 才會落到「無 attempt」分支

    async def boom(*args, **kwargs):
        raise exc_type("network blip during cleanup verification")

    monkeypatch.setattr(p, "_run_episode", boom)

    with pytest.raises(exc_type, match="network blip"):
        await p.podcast_series("nb-1", [EP], output_dir=str(tmp_path), start=1)


async def test_series_does_not_mask_an_early_error_when_no_row_exists_yet(
    fake_client, tmp_path, monkeypatch
):
    """`row` 本身是 None 的那一半(全新集,manifest 還沒有這一列,例如它是 start 指向
    的第一個候選)也要原樣拋出原始例外——`if row else None` 的另一半分支,上面的
    parametrize(既有 attempt、只是指標被清掉)測不到它。"""

    async def boom(*args, **kwargs):
        raise ConnectionError("network blip before any attempt exists")

    monkeypatch.setattr(p, "_run_episode", boom)

    with pytest.raises(ConnectionError, match="network blip before any attempt exists"):
        await p.podcast_series("nb-1", [EP], output_dir=str(tmp_path), start=1)


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
    # 新語意:取代版的 published_at 本來就該是 A 的首發時間,不是自己重生的完成時間
    # ——否則下面「重呼不改變它」的斷言測不出「它本來就是對的值」。
    assert replacement["published_at"] == before["published_at"]

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


# ---- 清理義務要聚合整個 canonical notebook,不是只看 episode_n 那一列(review #3) ----


async def test_cleanup_gate_blocks_generating_a_different_episode_in_the_same_notebook(
    fake_client, tmp_path
):
    """retract EP1 之後漏刪其 stale source,回頭跳過 dirty 的 EP1、改生成同一
    notebook 的 EP2(例如 start 跳過較早集)不得放行——EP2 未指名 source_ids 時會把
    EP1 的拒收逐字稿讀進 context。gate 現在聚合整個 canonical notebook 的所有集,
    不是只看即將生成的那一集。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    await p.podcast_attempt_retract(
        manifest_path, 1, before["output_attempt_id"], reason="QA 拒收"
    )
    call_boundary = len(fake_client.artifacts.calls)

    with pytest.raises(
        ValueError, match="retracted feedback sources still in the notebook"
    ):
        await p.podcast_episode(
            "nb-1", episode_n=2, title="實戰篇", brief="第二集",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    # 一次生成配額都沒燒,EP2 也沒被寫進 manifest
    assert not [
        c for c in fake_client.artifacts.calls[call_boundary:] if c[0] == "generate_audio"
    ]
    stored = json.loads(open(manifest_path, encoding="utf-8").read())
    assert len(stored["episodes"]) == 1

    # 刪掉 source 後重跑 → 通過,且 EP1 的 pending 被清
    await b.source_delete("nb-1", before["feedback_source_id"])
    out = await p.podcast_episode(
        "nb-1", episode_n=2, title="實戰篇", brief="第二集",
        output_dir=str(tmp_path), manifest_path=manifest_path,
    )
    assert out["episode"] == 2
    stored = json.loads(open(manifest_path, encoding="utf-8").read())
    ep1 = next(e for e in stored["episodes"] if e["episode"] == 1)
    assert "pending_source_cleanup" not in ep1


async def test_cleanup_gate_blocks_series_start_skipping_the_dirty_episode(
    fake_client, tmp_path
):
    """同一情境透過 `podcast_series(start=2)` 觸發——series 每集前導的 gate 一樣要看
    整個 notebook,不能只看即將生成的那一集。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    await p.podcast_attempt_retract(
        manifest_path, 1, before["output_attempt_id"], reason="QA 拒收"
    )
    call_boundary = len(fake_client.artifacts.calls)

    with pytest.raises(
        ValueError, match="retracted feedback sources still in the notebook"
    ):
        await p.podcast_series(
            "nb-1",
            [EP, {"title": "實戰篇", "brief": "第二集"}],
            output_dir=str(tmp_path),
            start=2,
        )
    assert not [
        c for c in fake_client.artifacts.calls[call_boundary:] if c[0] == "generate_audio"
    ]


async def test_cleanup_gate_does_not_cross_different_notebooks(fake_client, tmp_path):
    """canonical notebook 不同的集互不影響:EP1 的清理義務若實際上屬於另一個
    notebook,生成同一批次裡不同 notebook 的 EP2 不該被卡住。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    await p.podcast_attempt_retract(
        manifest_path, 1, before["output_attempt_id"], reason="QA 拒收"
    )
    ManifestStore(manifest_path).update(
        lambda manifest: manifest["episodes"][0].update({"notebook_id": "nb-other"})
    )

    out = await p.podcast_episode(
        "nb-1", episode_n=2, title="實戰篇", brief="第二集",
        output_dir=str(tmp_path), manifest_path=manifest_path,
    )
    assert out["episode"] == 2
    stored = json.loads(open(manifest_path, encoding="utf-8").read())
    ep1 = next(e for e in stored["episodes"] if e["episode"] == 1)
    # EP1 的義務原封不動,沒被誤判成已結案
    assert ep1["pending_source_cleanup"] == [before["feedback_source_id"]]


# ---- _create_audio_attempt 也要驗 episode notebook 一致性(review #9) -------------


async def test_create_audio_attempt_rejects_notebook_mismatch_after_retract(
    fake_client, tmp_path
):
    """post-retract 的集用 podcast_episode 傳錯 notebook_id 會造成
    episode(nb-A)/attempt(nb-B)身分分裂——`_create_audio_attempt` 補上跟
    `_ensure_resume_attempt` 兩條分支一致的 guard。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    await p.podcast_attempt_retract(
        manifest_path, 1, before["output_attempt_id"], reason="QA 拒收"
    )
    await b.source_delete("nb-1", before["feedback_source_id"])

    store = ManifestStore(manifest_path)
    with pytest.raises(ValueError, match="belongs to another notebook"):
        p._create_audio_attempt(
            store,
            notebook_id="nb-B",
            episode_n=1,
            title=EP["title"],
            brief="修正後的 brief",
            language="zh_Hant",
            audio_format="deep-dive",
            audio_length="long",
        )
    # 一個字都沒改
    after = _episode(manifest_path)
    assert "active_attempt_id" not in after
    assert len(after["attempts"]) == 1


# ---- adopt 換 source 後,舊 id 與未選中 candidate 都要進清理義務(review #4) -------


async def test_adopt_source_replacement_queues_stale_ids_for_cleanup(
    fake_client, tmp_path
):
    """adopt 換掉 attempt 的 feedback source 後,被取代的舊 id 與同一輪未被選中的
    candidate 都要進 pending_source_cleanup——否則同名重複 source 從此沒人記得,
    後續每集生成都讀到它。"""
    manifest_path = tmp_path / "series_manifest.json"
    # add_file 在拋錯之前已經在遠端建好 source(真實 SDK 語意):失敗的自我回錄上傳
    # 本身就留下一筆同名 source。再手動補一筆同名的,湊出真正的歧義(reconciliation
    # 只看名字/時窗,分不出哪筆才是「自己那次」上傳的)。
    fake_client.sources.add_file_exc_after_create = TimeoutError("upload response lost")
    with pytest.raises(TimeoutError, match="upload response lost"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief=EP["brief"],
            output_dir=str(tmp_path), manifest_path=str(manifest_path),
        )
    fake_client.sources.add_file_exc_after_create = None
    fake_client.sources._add("ep01.mp3", kind="media")

    stopped = await p.podcast_series(
        "nb-1", episodes=[EP], output_dir=str(tmp_path),
    )
    assert stopped["observed_state"] == "reconciliation_ambiguous"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt_id = stored["episodes"][0]["active_attempt_id"]
    candidates = (
        stored["episodes"][0]["attempts"][0]["finalize"]
        ["feedback_source_upload"]["candidate_source_ids"]
    )
    assert len(candidates) == 2
    candidate_a, candidate_b = candidates

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path), episode_n=1, attempt_id=attempt_id,
        feedback_source_id=candidate_a,
    )
    assert adopted["stale_source_ids"] == [candidate_b]
    assert adopted["safe_next_action"] == "source_delete"
    pending = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0][
        "pending_source_cleanup"
    ]
    assert pending == [candidate_b]

    # 修正:把選錯的 candidate_a 換成已就位、已改名成最終 label 的 candidate_c——
    # 被取代的 candidate_a 也要排進清理義務,而不是只留在 previous_source_ids 裡。
    candidate_c = fake_client.sources._add("EP01 心法篇", kind="media")
    corrected = await p.podcast_attempt_adopt(
        str(manifest_path), episode_n=1, attempt_id=attempt_id,
        feedback_source_id=candidate_c,
    )
    # 回傳的是「這一集尚未結案的全部義務」,不是只有這次新增的那筆——host 照著
    # stale_source_ids 刪才刪得乾淨。
    assert set(corrected["stale_source_ids"]) == {candidate_a, candidate_b}
    assert corrected["safe_next_action"] == "source_delete"
    pending = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0][
        "pending_source_cleanup"
    ]
    assert set(pending) == {candidate_a, candidate_b}

    # 冪等重呼:狀態不累積,**指引也不能翻回 series**。義務還掛著就必須還是
    # source_delete,否則自動化 host 照著回傳去跑 series,會被 gate 硬擋成 ValueError。
    again = await p.podcast_attempt_adopt(
        str(manifest_path), episode_n=1, attempt_id=attempt_id,
        feedback_source_id=candidate_c,
    )
    assert set(again["stale_source_ids"]) == {candidate_a, candidate_b}
    assert again["safe_next_action"] == "source_delete"
    assert set(
        json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0][
            "pending_source_cleanup"
        ]
    ) == {candidate_a, candidate_b}          # 不累積

    # 下一次生成前不刪就 fail-closed
    store = ManifestStore(manifest_path)
    with pytest.raises(
        ValueError, match="retracted feedback sources still in the notebook"
    ):
        await p._assert_source_cleanup_done(fake_client, store, "nb-1", 1)

    # 全部刪掉後通過
    await b.source_delete("nb-1", candidate_a)
    await b.source_delete("nb-1", candidate_b)
    await p._assert_source_cleanup_done(fake_client, store, "nb-1", 1)
    assert "pending_source_cleanup" not in json.loads(
        manifest_path.read_text(encoding="utf-8")
    )["episodes"][0]


async def test_legacy_adopt_source_replacement_queues_previous_for_cleanup(
    fake_client, tmp_path
):
    """legacy 分支(attempt_id=None)換 source 時,被取代的舊 id 一樣要進
    pending_source_cleanup——不能只留在 previous_feedback_source_ids 裡沒人清。"""
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
                        "mp3_path": str(tmp_path / "legacy-ep01.mp3"),
                        "published_at": "Wed, 01 Jan 2020 09:00:00 +0800",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "legacy-ep01.mp3").write_bytes(b"legacy audio")
    label = "EP01 心法篇"
    old_source_id = fake_client.sources._add(label, kind="media")
    # 同名的第二筆:正是這個機制要防的——同名重複 source 若沒人記得清理義務就會一直
    # 留在筆記本裡。
    new_source_id = fake_client.sources._add(label, kind="media")

    await p.podcast_attempt_adopt(
        str(manifest_path), episode_n=1, feedback_source_id=old_source_id,
    )
    result = await p.podcast_attempt_adopt(
        str(manifest_path), episode_n=1, feedback_source_id=new_source_id,
    )

    assert result["stale_source_ids"] == [old_source_id]
    assert result["safe_next_action"] == "source_delete"
    episode = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0]
    assert episode["pending_source_cleanup"] == [old_source_id]
    assert episode["previous_feedback_source_ids"] == [old_source_id]


async def test_retract_abandons_an_in_flight_attempt_when_the_caller_declares_it(
    fake_client, tmp_path
):
    """`abandon_in_flight=True`:輸入就是錯的那一種作廢。

    真實事故形狀:伺服器已受理生成(dispatch=accepted),但送進去的 brief 是失真版,
    等它跑完毫無意義。這個狀態與「第一次 dispatch、還在飛」在 manifest 裡**逐欄位相同**
    ——差別只在呼叫端握有的外部知識,所以只能顯式宣告(上一條測試鎖住預設不開)。

    沒有這條出路時五條路全被擋死:retract 撞「只有 promoted output 才能 retract」、
    傳乾淨 brief 撞「already has durable active attempt」、原樣重呼被
    `_is_resendable_same_request` 擋(dispatch 已 accepted)、傳 supersedes_attempt_id
    撞「is not terminal」、adopt/reconcile 都要求已 finalize。唯一走得通的是讓錯的版本
    跑完 → promote → retract → source_delete → 重生。
    """
    manifest_path = str(tmp_path / "series_manifest.json")
    fake_client.artifacts.fail_wait_on = 1        # 等待階段逾時 → attempt 在飛但沒 output
    stopped = await p.podcast_series("nb-1", episodes=[EP], output_dir=str(tmp_path))
    assert stopped["complete"] is False
    attempt_id = stopped["attempt_id"]

    result = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="brief 失真", abandon_in_flight=True
    )

    assert result["attempt_id"] == attempt_id
    assert result["observed_state"] == "retracted"
    episode = _episode(manifest_path)
    # 作廢的是「無人授權的 candidate」:它從 active 位置被摘掉,而這集本來就沒有 output。
    assert episode.get("active_attempt_id") is None
    assert episode.get("output_attempt_id") is None
    assert attempt_id in episode.get("retracted_attempt_ids", [])
    retracted = next(a for a in episode["attempts"] if a["attempt_id"] == attempt_id)
    assert retracted["retraction"]["reason"] == "brief 失真"
    # 沒有 finalize checkpoint ⇒ 沒有回錄 source 要刪(生成還沒跑完就被作廢)。
    assert result["stale_source_ids"] == []


async def test_abandon_in_flight_still_refuses_a_foreign_attempt(fake_client, tmp_path):
    """旗標只放行「作用中那一顆」——它不是萬用的 manifest 改寫鍵。

    ADR-0009 的其餘不變式照舊:不是 active、也不是 output 的 attempt,宣告了也不能作廢,
    否則這個旗標會變成繞過 tombstone 三層 default-deny 的後門。
    """
    manifest_path = str(tmp_path / "series_manifest.json")
    fake_client.artifacts.fail_wait_on = 1
    await p.podcast_series("nb-1", episodes=[EP], output_dir=str(tmp_path))

    with pytest.raises(ValueError, match="is missing from episode 1"):
        await p.podcast_attempt_retract(
            manifest_path, 1, "att-does-not-belong", reason="亂試", abandon_in_flight=True
        )


async def test_retract_clears_an_attempt_that_never_left_this_machine(
    fake_client, tmp_path
):
    """`prepared` / `not_accepted` 的 attempt **不需要 `abandon_in_flight`** 就能作廢。

    真實死結,撞過三次:一次 dispatch 被配額或 502 同步拒絕後,attempt 停在
    `not_accepted`;retract 拒收它,而 `podcast_episode` 給的唯一出路「用 identical
    arguments 重送」要求 brief 逐字相同 —— 中途改過 brief 產生器就重現不了。呼叫端
    只剩「低階 `generate_audio` + `podcast_attempt_adopt`」這條繞路,還多燒一次生成配額。

    准入判準原本掛在「該集有沒有其他 output 證據」這個 **proxy** 上,而真正的變因是
    **這顆有沒有可能在遠端留下東西** —— 那件事 manifest 自己就記著(`dispatch.status`),
    契約保證 `prepared`/`not_accepted` 沒有建出 task,不需要呼叫端提供任何外部知識。

    放寬**只涵蓋這兩種狀態**:`acceptance_unknown` / 已 dispatch 還在飛的仍然要顯式
    宣告,由 `test_retract_refuses_an_attempt_that_is_not_the_durable_output` 鎖著。
    """
    from notebooklm.exceptions import RateLimitError

    manifest_path = str(tmp_path / "series_manifest.json")
    fake_client.artifacts.generate_audio_exc = RateLimitError("每日配額已用盡")
    stopped = await p.podcast_series("nb-1", episodes=[EP], output_dir=str(tmp_path))
    assert stopped["observed_state"] == "not_accepted"

    out = await p.podcast_attempt_retract(
        manifest_path, 1, stopped["attempt_id"], reason="brief 寫錯了,重寫一份"
    )

    assert out["observed_state"] == "retracted"
    # 從沒產出過音檔,所以沒有回錄 source 要清 —— 清理義務不會擋住下一步。
    assert out["stale_source_ids"] == []

    # 作廢之後這一集回到乾淨狀態:重生走得通,而且全程不必碰 `abandon_in_flight`。
    fake_client.artifacts.generate_audio_exc = None
    again = await p.podcast_series("nb-1", episodes=[EP], output_dir=str(tmp_path))
    assert again["complete"] is True


async def test_the_refusal_message_points_at_the_way_out(fake_client, tmp_path):
    """**拒絕訊息本身要說得出正門。**

    v0.9.3 真實驗收 FINDING-2:訊息逐字是「only a promoted output attempt can be
    retracted」,而那句話在 `acceptance_unknown` / `accepted` 下**是假的** —— 傳
    `abandon_in_flight=True` 就 retract 得掉(同一輪驗收下一步就實測了)。只讀工具回傳
    的呼叫端會判定「這條路關著」,而那正是 v0.9.1 FAIL-1 的同型:**指引在它自己產生的
    狀態下不可執行**。v0.9.3 修了 docstring,漏了 runtime 訊息 —— 等於修了給人讀的那份、
    漏了給機器讀的那份。

    訊息要帶兩件事:正門的名字(`abandon_in_flight`),以及**它現在是什麼狀態** ——
    呼叫端得知道自己落在需要外部知識的那一格,才會先去 `artifact_list` 查雲端。
    """
    manifest_path = str(tmp_path / "series_manifest.json")
    fake_client.artifacts.fail_wait_on = 1        # dispatch 成功、等待階段斷掉
    stopped = await p.podcast_series("nb-1", episodes=[EP], output_dir=str(tmp_path))
    assert stopped["complete"] is False

    with pytest.raises(ValueError) as caught:
        await p.podcast_attempt_retract(
            manifest_path, 1, stopped["attempt_id"], reason="輸入就是錯的"
        )

    msg = str(caught.value)
    assert "abandon_in_flight" in msg, f"訊息沒指向正門: {msg}"
    assert "artifact_list" in msg, f"沒教它先確認雲端有沒有東西: {msg}"
    # 要說出它落在哪一格,否則呼叫端分不出「該傳旗標」與「真的不該碰」。
    assert stopped["observed_state"] in msg or "accepted" in msg, msg


async def test_the_audit_record_says_whether_the_flag_was_used(fake_client, tmp_path):
    """**兩種 retract 在稽核紀錄上必須分得出來。**

    v0.9.3 真實驗收 FINDING-3:一次是 `prepared`(純本機、零遠端後果、不需宣告),
    一次是 `acceptance_unknown` + `abandon_in_flight=True`(呼叫端顯式宣告了 manifest
    推導不出的外部知識,而且遠端**可能真的有東西在燒**)。兩者的 `retraction` 區塊
    欄位完全相同,事後只能去讀 `dispatch.status` 反推 —— 而那個欄位在 retract 之後
    還會被後續操作改動。

    `reason` 必填的理由是「retract 是審計事件」(ADR-0009);同一個理由要求記下
    **這次動用了哪一種權限**。ADR-0010 §Transparency:manifest 是唯一的稽核憑據。
    """
    from notebooklm.exceptions import RateLimitError

    # ① 從沒送出去的那種:不需要旗標。
    plain_path = str(tmp_path / "plain" / "series_manifest.json")
    (tmp_path / "plain").mkdir()
    fake_client.artifacts.generate_audio_exc = RateLimitError("每日配額已用盡")
    refused = await p.podcast_series(
        "nb-1", episodes=[EP], output_dir=str(tmp_path / "plain")
    )
    fake_client.artifacts.generate_audio_exc = None
    plain = await p.podcast_attempt_retract(
        plain_path, 1, refused["attempt_id"], reason="brief 寫錯"
    )

    assert plain["abandon_in_flight"] is False
    assert plain["dispatch_status_at_retraction"] == "not_accepted"

    # ② 需要外部知識的那種:顯式宣告。
    flagged_dir = tmp_path / "flagged"
    flagged_dir.mkdir()
    flagged_path = str(flagged_dir / "series_manifest.json")
    fake_client.artifacts.fail_wait_on = 1
    stopped = await p.podcast_series("nb-1", episodes=[EP], output_dir=str(flagged_dir))
    fake_client.artifacts.fail_wait_on = None
    flagged = await p.podcast_attempt_retract(
        flagged_path,
        1,
        stopped["attempt_id"],
        reason="artifact_list 查過雲端零 artifact",
        abandon_in_flight=True,
    )

    assert flagged["abandon_in_flight"] is True
    assert flagged["dispatch_status_at_retraction"] == "accepted"

    # 落盤的也要有 —— 回傳值看得到但 manifest 沒記等於沒記。
    stored = json.loads(open(flagged_path, encoding="utf-8").read())
    retraction = stored["episodes"][0]["attempts"][0]["retraction"]
    assert retraction["abandon_in_flight"] is True
    assert retraction["dispatch_status_at_retraction"] == "accepted"
