"""QA 拒收後的正門:podcast_attempt_retract。

存在理由是一個真實事故(EP35):durability guard 只擋掉隱式重生,卻沒給任何
「這是修正、不是誤觸」的出口,於是呼叫端改去手寫 attempt 進 manifest —— 五個時間戳
同一微秒、artifact_ids_before 填自己的 artifact_id、狀態字串在 repo 裡根本不存在。
guard 沒保護 manifest,只是把寫入趕出工具外。

這些測試鎖住三件事:retract 之後重生是合法的(且**必須先刪舊回錄 source**)、被作廢的
紀錄不會消失、以及被作廢的 attempt **不可能再被復活**——不論是 retract 之前就啟動的
in-flight finalizer、還是任何把指標寫回去的 writer。
"""
import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from notebooklm.types import ArtifactType

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


# 候選歸屬雖來自外部知識，仍已用 `candidate_selection_required` 維度收進
# capabilities。唯一例外是同名工具的 feedback-source 模式：它問的不是
# audio attempt 下一步，而是遠端 source 是否待 rename／cleanup，legacy 甚至沒有 attempt_id。
PUBLIC_GUIDANCE_EXCEPTIONS = {
    "podcast_attempt_adopt.feedback_source": (
        "safe_next_action 由遠端 source rename／cleanup 後狀態決定，"
        "不是 audio-attempt capabilities 的狀態空間。"
    )
}


def _assert_public_guidance_matches_capabilities(
    manifest_path: str,
    attempt_id: str,
    result: dict,
    *,
    tool: str,
    candidate_selection_required: bool = False,
    post_retract: bool = False,
) -> None:
    snapshot = ManifestStore(manifest_path).read()
    episode, attempt = p._attempt_record(
        snapshot, 1, attempt_id, allow_retracted=post_retract
    )
    caps = p._attempt_capabilities(
        episode,
        attempt,
        attempt_id,
        candidate_selection_required=candidate_selection_required,
        post_retract=post_retract,
    )
    exception_key = f"{tool}.artifact.{result['observed_state']}"
    if exception_key in PUBLIC_GUIDANCE_EXCEPTIONS:
        assert PUBLIC_GUIDANCE_EXCEPTIONS[exception_key].strip()
        return
    assert result["safe_next_action"] == caps["safe_next_action"]
    assert result["next_step"] == p._attempt_next_step(caps)


async def test_retract_clears_output_evidence_and_keeps_the_audit_trail(
    fake_client, tmp_path
):
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_id = before["output_attempt_id"]

    out = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="QA 拒收:把 hook 和權限邊界混為一談"
    )

    _assert_public_guidance_matches_capabilities(
        manifest_path,
        attempt_id,
        out,
        tool="podcast_attempt_retract",
        post_retract=True,
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


async def test_idempotent_retract_after_a_stuck_replacement_does_not_point_at_a_dead_end(
    fake_client, tmp_path
):
    """**F1(獨立盲審 P1 regression,v0.9.10 發版後現形):post_retract 分支從不看
    `episode["active_attempt_id"]`,永遠回 `regeneration_entry`。**

    盲審實跑重現的可達序列(全是純工具呼叫,沒有手改 manifest):
    1. `podcast_episode` EP1 → A 成為 output。
    2. `podcast_attempt_retract(A)` → 教「先把 stale_source_ids 全部
       source_delete,再用 regeneration_entry 重生」。
    3. `source_delete(stale)`。
    4. 重生 → response lost → B 成為 `active` + `acceptance_unknown`
       (`_assert_source_cleanup_done` 這一步已經清掉
       `episode["pending_source_cleanup"]`)。
    5. **再次 `podcast_attempt_retract(A)`**——MCP request 被取消後的正常重試,
       retract 明文冪等。

    修復前這裡教「用 podcast_series 重生」,照做會撞
    `ValueError: episode 1 already has durable active attempt`——`source_delete`
    冪等,所以舊版給的機器可讀 action 是可執行的,新版不是。
    """
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_a = before["output_attempt_id"]

    first = await p.podcast_attempt_retract(manifest_path, 1, attempt_a, reason="QA")
    assert first["safe_next_action"] == "source_delete"
    await b.source_delete("nb-1", before["feedback_source_id"])

    fake_client.artifacts.generate_audio_exc = TimeoutError("response lost")
    with pytest.raises(TimeoutError, match="response lost"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後的 brief",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    stuck = _episode(manifest_path)
    attempt_b = stuck["active_attempt_id"]
    assert attempt_b != attempt_a
    assert stuck.get("output_attempt_id") is None      # B 還沒 promote

    again = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_a, reason="重試(request 被取消後冪等重呼)"
    )

    # **鑑別點**:不准再教會撞牆的 podcast_series/podcast_episode。
    assert again["safe_next_action"] not in (p.ACTION_SERIES, p.ACTION_EPISODE), again
    assert "用 podcast_series 重生" not in again["next_step"], again
    # 照著回傳的下一步做,真的走得通——不是隨便換一個不撞牆的字面值交差。
    assert again["safe_next_action"] == p.ACTION_RECONCILE, again
    # **執行下一步只准用公開回傳裡的值**(P1 修復,docs/gotchas-attempt.md「回傳要
    # 自足」那條紅線):`again["attempt_id"]` 是被 retract 的 A(tombstone,稽核
    # 主體不會變),真正要用的是 `safe_next_attempt_id`——上一版這裡直接從 manifest
    # 私下讀 `attempt_b`,連「回傳裡有沒有這顆身分」都沒驗到。
    assert again["safe_next_attempt_id"] == attempt_b, again   # 交棒對象正是 B,不是 A
    out = await p.podcast_episode_reconcile(
        manifest_path, 1, again["safe_next_attempt_id"]
    )
    assert out["episode_n"] == 1     # 沒有 raise 就是走得通


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


async def _split_with_guarded_candidate(
    fake_client, tmp_path, *, add_blocker: bool = True
) -> tuple[str, str]:
    """建出 active=B/output=A，並讓另一顆 unresolved C 觸發單候選 guard。"""
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_b = "att-split-b"

    def add_candidates(manifest: dict) -> None:
        episode = manifest["episodes"][0]
        common = {
            "episode": 1,
            "title": EP["title"],
            "notebook_id": "nb-1",
            "settings": {
                "language": "zh",
                "audio_format": None,
                "audio_length": None,
                "source_ids": ["src-1"],
            },
            "remote": {"artifact_id": None, "status": None},
            "finalize": audio_finalize.new_finalize_state(),
        }
        episode["attempts"].append(
            {
                **common,
                "attempt_id": attempt_b,
                "dispatch": {
                    "status": "acceptance_unknown",
                    "artifact_ids_before": [before["artifact_id"]],
                    "dispatched_at": datetime.now(timezone.utc).isoformat(),
                    "wait_timeout": 1200.0,
                },
            }
        )
        if add_blocker:
            episode["attempts"].append(
                {
                    **common,
                    "attempt_id": "att-blocker-c",
                    "dispatch": {"status": "acceptance_unknown"},
                }
            )
        episode["active_attempt_id"] = attempt_b

    ManifestStore(manifest_path).update(add_candidates)
    fake_client.artifacts.seed_artifact(
        "artifact-b", kind=ArtifactType.AUDIO, title="Audio Overview"
    )
    return manifest_path, attempt_b


async def test_reconcile_public_guidance_respects_output_owner(fake_client, tmp_path):
    manifest_path, attempt_b = await _split_with_guarded_candidate(
        fake_client, tmp_path
    )

    out = await p.podcast_episode_reconcile(manifest_path, 1, attempt_b)

    _assert_public_guidance_matches_capabilities(
        manifest_path,
        attempt_b,
        out,
        tool="podcast_episode_reconcile",
        candidate_selection_required=True,
    )
    assert out["safe_next_action"] == p.ACTION_RETRACT
    assert "podcast_attempt_retract" in out["next_step"]
    assert "podcast_attempt_adopt" not in out["next_step"]


async def test_reconcile_unique_bind_guidance_respects_output_owner(
    fake_client, tmp_path
):
    manifest_path, attempt_b = await _split_with_guarded_candidate(
        fake_client, tmp_path, add_blocker=False
    )

    out = await p.podcast_episode_reconcile(manifest_path, 1, attempt_b)

    _assert_public_guidance_matches_capabilities(
        manifest_path, attempt_b, out, tool="podcast_episode_reconcile"
    )
    assert out["artifact_id"] == "artifact-b"
    assert out["safe_next_action"] == p.ACTION_RETRACT


async def test_adopt_public_guidance_respects_output_owner(fake_client, tmp_path):
    manifest_path, attempt_b = await _split_with_guarded_candidate(
        fake_client, tmp_path
    )
    await p.podcast_episode_reconcile(manifest_path, 1, attempt_b)

    out = await p.podcast_attempt_adopt(
        manifest_path, 1, attempt_id=attempt_b, artifact_id="artifact-b"
    )

    _assert_public_guidance_matches_capabilities(
        manifest_path, attempt_b, out, tool="podcast_attempt_adopt"
    )
    assert out["safe_next_action"] == p.ACTION_RETRACT
    assert "podcast_attempt_retract" in out["next_step"]
    assert "podcast_episode_resume" not in out["next_step"]

    reconciled_again = await p.podcast_episode_reconcile(
        manifest_path, 1, attempt_b
    )
    _assert_public_guidance_matches_capabilities(
        manifest_path,
        attempt_b,
        reconciled_again,
        tool="podcast_episode_reconcile",
    )
    assert reconciled_again["safe_next_action"] == p.ACTION_RETRACT


async def test_retract_public_guidance_keeps_the_existing_output(fake_client, tmp_path):
    manifest_path, attempt_b = await _split_with_guarded_candidate(
        fake_client, tmp_path
    )
    await p.podcast_episode_reconcile(manifest_path, 1, attempt_b)
    await p.podcast_attempt_adopt(
        manifest_path, 1, attempt_id=attempt_b, artifact_id="artifact-b"
    )

    out = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_b, reason="放棄未授權 candidate"
    )

    _assert_public_guidance_matches_capabilities(
        manifest_path,
        attempt_b,
        out,
        tool="podcast_attempt_retract",
        post_retract=True,
    )
    assert out["safe_next_action"] is None
    assert "不必重生" in out["next_step"]
    assert "podcast_episode" not in out["next_step"]
    assert "podcast_series" not in out["next_step"]


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


# ---- F2:upload 還沒落盤時的 retract —— 放行,但義務要留在 tombstone 上 ----------
#
# 舊版對這個狀態硬擋 retract(連 `abandon_in_flight` 都擋),而它靠一次 client
# cancellation 就能永久存在:`except Exception` 收不到 `CancelledError`,checkpoint
# 停在 `dispatching`,而唯一的出路 `_reconcile_source_upload` 只從 finalize 進得去
# ——「要作廢一顆輸入本來就錯的 attempt,得先把它完整 finalize、上傳、promote」,
# 比旗標本來要避免的後果還多一輪遠端副作用。


def _upload(manifest_path, attempt_id):
    snapshot = ManifestStore(manifest_path).read()
    _, attempt = p._attempt_record(
        snapshot, 1, attempt_id, allow_retracted=True
    )
    return attempt["finalize"]["feedback_source_upload"]


def _attempt_next_step_for(manifest_path, attempt_id):
    snapshot = ManifestStore(manifest_path).read()
    episode, attempt = p._attempt_record(
        snapshot, 1, attempt_id, allow_retracted=True
    )
    caps = p._attempt_capabilities(episode, attempt, attempt_id, post_retract=True)
    return p._attempt_next_step(caps)


def _age_the_dispatch_window(manifest_path, attempt_id):
    """把 dispatched_at 推到候選窗之外(等真實時間過去是不可行的測法)。"""
    old = datetime.now(timezone.utc) - (
        audio_finalize.UPLOAD_DISPATCH_WINDOW + timedelta(minutes=1)
    )

    def mutate(manifest):
        _, attempt = p._attempt_record(manifest, 1, attempt_id, allow_retracted=True)
        attempt["finalize"]["feedback_source_upload"]["dispatched_at"] = old.isoformat()

    ManifestStore(manifest_path).update(mutate)


async def test_retract_abandons_an_in_flight_upload_and_the_gate_finds_the_orphan(
    fake_client, tmp_path, monkeypatch
):
    """add_file 已建出遠端 source、response 還沒回 checkpoint 時 retract。

    **旗標必須穿得過去**(F2),但 tombstone 要記下「可能有孤兒」;下一次生成前的 gate
    用候選窗撈出那筆 source、排進 pending 並 fail-closed,刪掉才放行。
    """
    manifest_path = str(tmp_path / "series_manifest.json")
    remote_created = asyncio.Event()
    release_response = asyncio.Event()
    real_add_file = fake_client.sources.add_file

    async def pause_after_remote_create(*args, **kwargs):
        source = await real_add_file(*args, **kwargs)
        remote_created.set()
        await release_response.wait()
        return source

    monkeypatch.setattr(fake_client.sources, "add_file", pause_after_remote_create)
    running = asyncio.create_task(
        p.podcast_episode(
            "nb-1",
            episode_n=1,
            title=EP["title"],
            brief=EP["brief"],
            output_dir=str(tmp_path),
            manifest_path=manifest_path,
        )
    )
    await remote_created.wait()
    attempt_id = _episode(manifest_path)["active_attempt_id"]
    orphan_id = fake_client.sources.sources[-1]["id"]
    assert _upload(manifest_path, attempt_id)["status"] == "dispatching"

    retracted = await p.podcast_attempt_retract(
        manifest_path,
        1,
        attempt_id,
        reason="輸入錯誤，放棄仍在 finalize 的 attempt",
        abandon_in_flight=True,
    )
    assert retracted["source_cleanup_unresolved"] is True
    assert retracted["authorization_basis"] == "abandon_in_flight"
    # 身分還對不出來,所以這一刻沒有具體 id 可刪——義務不是靠回傳值記住的。
    assert retracted["stale_source_ids"] == []
    assert retracted["safe_next_action"] is None
    _assert_public_guidance_matches_capabilities(
        manifest_path,
        attempt_id,
        retracted,
        tool="podcast_attempt_retract",
        post_retract=True,
    )

    # tombstone 之後,還在飛的 finalizer 不得復活它(ADR-0009)。
    release_response.set()
    with pytest.raises(ValueError, match="was retracted"):
        await running

    # 生成前的 gate 自己去 notebook 對帳,撈到那筆孤兒 → fail-closed 且零配額。
    dispatches_before = sum(
        call[0] == "generate_audio" for call in fake_client.artifacts.calls
    )
    with pytest.raises(ValueError, match="retracted feedback sources still in") as blocked:
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title=EP["title"],
            brief="修正後內容",
            output_dir=str(tmp_path),
            manifest_path=manifest_path,
        )
    assert orphan_id in str(blocked.value)
    assert sum(
        call[0] == "generate_audio" for call in fake_client.artifacts.calls
    ) == dispatches_before
    # 撈到的候選要變成耐久義務,不能只活在那句錯誤訊息裡。
    assert _episode(manifest_path)["pending_source_cleanup"] == [orphan_id]

    # **gate 撈到之後,retract 的冪等回傳要跟著改口。** 這一刻已經有具體 id 可刪,
    # 若還回 `reconcile_after`／`safe_next_action=None`,呼叫端會以為只能乾等,而生成
    # gate 同時正拿著這個 id 擋著 —— 兩個入口對同一狀態指向不同動作。
    replayed = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="輸入錯誤,放棄仍在 finalize 的 attempt"
    )
    assert replayed["safe_next_action"] == p.ACTION_SOURCE_DELETE
    assert orphan_id in _attempt_next_step_for(manifest_path, attempt_id)
    # tombstone 本身沒有被改寫(ADR-0009):候選只進 episode 級的 pending。
    retraction = next(
        row["retraction"]
        for row in _episode(manifest_path)["attempts"]
        if row["attempt_id"] == attempt_id
    )
    assert retraction["stale_source_ids"] == []

    # 刪掉孤兒還不夠:候選窗還開著,晚到的 upload 仍可能再冒一筆出來,所以
    # replacement 繼續 fail-closed(這一條就是「window 前 replacement 被擋」)。
    await b.source_delete("nb-1", orphan_id)
    with pytest.raises(ValueError, match="候選窗還沒關"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後內容",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    assert sum(
        call[0] == "generate_audio" for call in fake_client.artifacts.calls
    ) == dispatches_before

    _age_the_dispatch_window(manifest_path, attempt_id)
    out = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title=EP["title"],
        brief="修正後內容",
        output_dir=str(tmp_path),
        manifest_path=manifest_path,
    )
    assert out["episode"] == 1
    episode = _episode(manifest_path)
    assert "pending_source_cleanup" not in episode
    retraction = next(
        row["retraction"]
        for row in episode["attempts"]
        if row["attempt_id"] == attempt_id
    )
    assert "source_cleanup_unresolved" not in retraction


async def test_cancellation_before_the_remote_create_settles_once_the_window_closes(
    fake_client, tmp_path, monkeypatch
):
    """取消發生在 add_file 真的建出 source 之前:遠端零候選。

    **`CancelledError` 要被 checkpoint 收到**(它是 BaseException,`except Exception`
    收不到)——漏收會讓狀態永久停在 `dispatching`。而零候選在候選窗關上之前不算
    「真的沒有」,所以重生要先 fail-closed,窗關了才結案放行。
    """
    manifest_path = str(tmp_path / "series_manifest.json")
    entered = asyncio.Event()

    async def hang_before_remote_create(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(fake_client.sources, "add_file", hang_before_remote_create)
    running = asyncio.create_task(
        p.podcast_episode(
            "nb-1",
            episode_n=1,
            title=EP["title"],
            brief=EP["brief"],
            output_dir=str(tmp_path),
            manifest_path=manifest_path,
        )
    )
    await entered.wait()
    attempt_id = _episode(manifest_path)["active_attempt_id"]
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert _upload(manifest_path, attempt_id)["status"] == "acceptance_unknown"
    # **上傳 stub 要在這裡就還原。** 留著會讓「窗未關必須 fail-closed」那個 guard 的
    # 突變表現成永久 hang 而不是紅燈 —— 一條測不出東西的測試。
    monkeypatch.undo()

    retracted = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="放棄", abandon_in_flight=True
    )
    assert retracted["source_cleanup_unresolved"] is True

    dispatches_before = sum(
        call[0] == "generate_audio" for call in fake_client.artifacts.calls
    )
    with pytest.raises(ValueError, match="候選窗還沒關"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後內容",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    assert sum(
        call[0] == "generate_audio" for call in fake_client.artifacts.calls
    ) == dispatches_before

    _age_the_dispatch_window(manifest_path, attempt_id)
    out = await p.podcast_episode(
        "nb-1", episode_n=1, title=EP["title"], brief="修正後內容",
        output_dir=str(tmp_path), manifest_path=manifest_path,
    )
    assert out["episode"] == 1
    retraction = next(
        row["retraction"]
        for row in _episode(manifest_path)["attempts"]
        if row["attempt_id"] == attempt_id
    )
    assert "source_cleanup_unresolved" not in retraction


async def test_gate_queues_every_candidate_it_finds_not_just_the_first(
    fake_client, tmp_path, monkeypatch
):
    """遠端有兩筆同名 media 都落在候選窗內:身分對不出來,但範圍確定 —— 全部排進義務。

    只排第一筆等於把另一筆放生,而它會被之後每一集的生成讀進 context(finalize 是按
    source_id 驗的,同名重複沒人擋)。
    """
    manifest_path = str(tmp_path / "series_manifest.json")
    remote_created = asyncio.Event()
    release_response = asyncio.Event()
    real_add_file = fake_client.sources.add_file

    async def pause_after_remote_create(*args, **kwargs):
        source = await real_add_file(*args, **kwargs)
        remote_created.set()
        await release_response.wait()
        return source

    monkeypatch.setattr(fake_client.sources, "add_file", pause_after_remote_create)
    running = asyncio.create_task(
        p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief=EP["brief"],
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    )
    await remote_created.wait()
    attempt_id = _episode(manifest_path)["active_attempt_id"]
    first_orphan = fake_client.sources.sources[-1]["id"]
    # 重試造成的第二筆:同名、同 kind、同樣落在候選窗內。
    expected_title = _upload(manifest_path, attempt_id)["expected_title"]
    second_orphan = fake_client.sources._add(expected_title, kind="media")

    await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="放棄", abandon_in_flight=True
    )
    release_response.set()
    with pytest.raises(ValueError, match="was retracted"):
        await running

    with pytest.raises(ValueError, match="retracted feedback sources still in") as blocked:
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後內容",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    assert first_orphan in str(blocked.value)
    assert second_orphan in str(blocked.value)
    assert set(_episode(manifest_path)["pending_source_cleanup"]) == {
        first_orphan,
        second_orphan,
    }


async def test_gate_settles_verified_deletions_before_it_raises_on_the_window(
    fake_client, tmp_path
):
    """已確認不在的 id 要在同一次 mutation 結算,不能被 waiting 那條 raise 卡住。(盲審 P2)

    舊版把清除放在所有 raise 之後,於是已經刪掉的 id 永遠留在 `pending_source_cleanup`
    裡,而冪等 `podcast_attempt_retract` 會一直回 `source_delete`——指向一個早就不存在
    的東西。
    """
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_id = before["output_attempt_id"]

    def unresolve(manifest):
        _, attempt = p._attempt_record(manifest, 1, attempt_id)
        attempt["finalize"]["feedback_source_upload"].update(
            {"status": "acceptance_unknown", "source_id": None}
        )

    ManifestStore(manifest_path).update(unresolve)
    retracted = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="QA 拒收", abandon_in_flight=True
    )
    known_stale = retracted["stale_source_ids"]
    assert known_stale and _episode(manifest_path)["pending_source_cleanup"] == known_stale

    # 呼叫端照指引刪乾淨了,但 unresolved 的候選窗還開著。
    for source_id in known_stale:
        await b.source_delete("nb-1", source_id)

    with pytest.raises(ValueError, match="候選窗還沒關"):
        await p.podcast_episode(
            "nb-1", episode_n=2, title="實戰篇", brief="第二集",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )

    # **raise 了,但已驗證的清除照樣落盤。**
    assert "pending_source_cleanup" not in _episode(manifest_path)
    replayed = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="QA 拒收"
    )
    assert replayed["safe_next_action"] is None, (
        "已經刪掉的 id 還卡在 pending,指引繼續教一個做不到的 source_delete"
    )


async def test_gate_refuses_to_write_ownership_it_computed_before_the_await(
    fake_client, tmp_path, monkeypatch
):
    """`await sources.list()` 期間 manifest 動過 → 不拿舊 ownership 寫新狀態。(盲審 P1)

    競態:snapshot 顯示 src-X 還沒被認領 → await → 另一個 finalizer 把 src-X 認領成它那
    一集的合法 continuity source → 我們仍把它排進待刪清單 → 呼叫端照指引刪掉合法來源。
    """
    manifest_path, attempt_id = await _abandon_an_unresolved_upload(
        fake_client, tmp_path, monkeypatch
    )
    expected_title = _upload(manifest_path, attempt_id)["expected_title"]
    contested = fake_client.sources._add(expected_title, kind="media")
    real_list = fake_client.sources.list

    async def claim_during_the_await(notebook_id):
        result = await real_list(notebook_id)

        def another_writer(manifest):
            manifest["episodes"][0]["attempts"][0].setdefault("note", "touched")

        ManifestStore(manifest_path).update(another_writer)
        return result

    monkeypatch.setattr(fake_client.sources, "list", claim_during_the_await)

    with pytest.raises(ValueError, match="manifest 被改動過"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後內容",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    # 過期的歸屬一個字都不准落盤。
    assert contested not in (_episode(manifest_path).get("pending_source_cleanup") or [])


async def test_gate_keeps_the_obligation_when_the_notebook_cannot_be_listed(
    fake_client, tmp_path, monkeypatch
):
    """對帳打不通(認證死了／notebook 讀不到)絕不能把義務誤清成已結案。"""
    manifest_path = str(tmp_path / "series_manifest.json")
    entered = asyncio.Event()

    async def hang_before_remote_create(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(fake_client.sources, "add_file", hang_before_remote_create)
    running = asyncio.create_task(
        p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief=EP["brief"],
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    )
    await entered.wait()
    attempt_id = _episode(manifest_path)["active_attempt_id"]
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="放棄", abandon_in_flight=True
    )
    _age_the_dispatch_window(manifest_path, attempt_id)

    async def dead_auth(*args, **kwargs):
        raise RuntimeError("auth is dead")

    monkeypatch.setattr(fake_client.sources, "list", dead_auth)
    with pytest.raises(RuntimeError, match="auth is dead"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後內容",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    retraction = next(
        row["retraction"]
        for row in _episode(manifest_path)["attempts"]
        if row["attempt_id"] == attempt_id
    )
    assert retraction["source_cleanup_unresolved"] is True


async def _abandon_an_unresolved_upload(fake_client, tmp_path, monkeypatch):
    """把一集帶到「retract 過、清理義務未結案、遠端零候選」的狀態。"""
    manifest_path = str(tmp_path / "series_manifest.json")
    entered = asyncio.Event()

    async def hang_before_remote_create(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(fake_client.sources, "add_file", hang_before_remote_create)
    running = asyncio.create_task(
        p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief=EP["brief"],
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    )
    await entered.wait()
    attempt_id = _episode(manifest_path)["active_attempt_id"]
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    monkeypatch.undo()
    await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="放棄", abandon_in_flight=True
    )
    return manifest_path, attempt_id


async def test_gate_refuses_to_discharge_an_obligation_it_cannot_identify(
    fake_client, tmp_path, monkeypatch
):
    """零候選 + 窗已關,但這一集沒有 canonical notebook_id → 仍然不准結案。

    與 `clearable` 同一條紀律:拿呼叫端隨手傳的 notebook 查到「沒有」,不代表當初上傳的
    那本筆記本也沒有。少了這道判斷,legacy 列的義務會被誤清,而誤清之後再也沒有東西擋
    那筆舊來源污染後續生成。
    """
    manifest_path, attempt_id = await _abandon_an_unresolved_upload(
        fake_client, tmp_path, monkeypatch
    )
    _age_the_dispatch_window(manifest_path, attempt_id)

    def strip_notebook_identity(manifest):
        manifest.pop("notebook_id", None)
        manifest["episodes"][0].pop("notebook_id", None)
        # attempt 自己的 notebook_id 是清理義務的**權威來源**(tombstone 允許保留建立時
        # 綁的舊 notebook),所以三層都清掉才是真正的「身分不明」。
        for attempt in manifest["episodes"][0]["attempts"]:
            attempt.pop("notebook_id", None)

    ManifestStore(manifest_path).update(strip_notebook_identity)

    with pytest.raises(ValueError, match="沒有可核對的 notebook 身分"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後內容",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    retraction = next(
        row["retraction"]
        for row in _episode(manifest_path)["attempts"]
        if row["attempt_id"] == attempt_id
    )
    assert retraction["source_cleanup_unresolved"] is True


async def test_gate_never_nominates_a_candidate_from_an_unverified_notebook(
    fake_client, tmp_path, monkeypatch
):
    """**身分未確認時,連「撈候選」都不准做。**(盲審 P1)

    舊版先篩候選、只有零候選才檢查身分,於是「碰巧同名、同 kind、同時間窗」的別本
    筆記本來源會被寫進 `pending_source_cleanup`,錯誤訊息再叫呼叫端去 `source_delete`
    它 —— 那是對無關 notebook 下的破壞性指令,比漏掉義務更糟。
    """
    manifest_path, attempt_id = await _abandon_an_unresolved_upload(
        fake_client, tmp_path, monkeypatch
    )
    expected_title = _upload(manifest_path, attempt_id)["expected_title"]
    # 呼叫端傳來的那本 notebook 裡剛好有一筆長得一模一樣的來源。
    decoy = fake_client.sources._add(expected_title, kind="media")

    def strip_notebook_identity(manifest):
        manifest.pop("notebook_id", None)
        manifest["episodes"][0].pop("notebook_id", None)
        for attempt in manifest["episodes"][0]["attempts"]:
            attempt.pop("notebook_id", None)

    ManifestStore(manifest_path).update(strip_notebook_identity)

    with pytest.raises(ValueError, match="沒有可核對的 notebook 身分") as blocked:
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後內容",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    # 那筆 decoy 不准出現在任何地方:不被指名、不被持久化。
    assert decoy not in str(blocked.value)
    assert "pending_source_cleanup" not in _episode(manifest_path)
    # 也不准在身分驗證之前就打 RPC。
    assert not [call for call in fake_client.sources.calls if call[0] == "delete"]


async def test_cleanup_identity_follows_the_attempt_not_the_episode(
    fake_client, tmp_path, monkeypatch
):
    """清理義務綁的是**當初上傳的那本 notebook**,不是 episode 當下那本。(盲審 P1)

    `manifest_store._validate` 明文允許 tombstone 保留建立時綁的舊 notebook(否則
    retract 之後想換 notebook 重生就寫不進去)。拿 episode 當下的 notebook 去查,
    等於「換一本 notebook 重生」就能把舊本的孤兒洗掉 —— 永久失憶。
    """
    manifest_path, attempt_id = await _abandon_an_unresolved_upload(
        fake_client, tmp_path, monkeypatch
    )
    _age_the_dispatch_window(manifest_path, attempt_id)

    def move_the_episode_to_another_notebook(manifest):
        manifest["episodes"][0]["notebook_id"] = "nb-new"
        # attempt 仍綁著當初真的上傳過的那一本。
        for attempt in manifest["episodes"][0]["attempts"]:
            if attempt["attempt_id"] == attempt_id:
                attempt["notebook_id"] = "nb-old"

    ManifestStore(manifest_path).update(move_the_episode_to_another_notebook)

    # 在**新**的 notebook 生成:舊本的義務不屬於這裡,不該被這一次查詢結案。
    await p.podcast_episode(
        "nb-new", episode_n=2, title="實戰篇", brief="第二集",
        output_dir=str(tmp_path), manifest_path=manifest_path,
    )
    retraction = next(
        row["retraction"]
        for row in _episode(manifest_path)["attempts"]
        if row["attempt_id"] == attempt_id
    )
    assert retraction["source_cleanup_unresolved"] is True, (
        "舊 notebook 的清理義務被另一本 notebook 的查詢誤清了"
    )


async def test_gate_persists_discoveries_even_when_a_later_checkpoint_is_broken(
    fake_client, tmp_path, monkeypatch
):
    """A 撈到候選、B 的 checkpoint 壞掉時,A 的發現必須先落盤。(盲審 P1/P2)

    舊版在迴圈裡直接 raise,永遠到不了寫入 —— 那批 id 從此沒人記得。
    """
    manifest_path = str(tmp_path / "series_manifest.json")
    remote_created = asyncio.Event()
    release_response = asyncio.Event()
    real_add_file = fake_client.sources.add_file

    async def pause_after_remote_create(*args, **kwargs):
        source = await real_add_file(*args, **kwargs)
        remote_created.set()
        await release_response.wait()
        return source

    monkeypatch.setattr(fake_client.sources, "add_file", pause_after_remote_create)
    running = asyncio.create_task(
        p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief=EP["brief"],
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    )
    await remote_created.wait()
    attempt_a = _episode(manifest_path)["active_attempt_id"]
    orphan_a = fake_client.sources.sources[-1]["id"]
    await p.podcast_attempt_retract(
        manifest_path, 1, attempt_a, reason="放棄 A", abandon_in_flight=True
    )
    release_response.set()
    with pytest.raises(ValueError, match="was retracted"):
        await running
    monkeypatch.undo()

    # B:同一本 notebook 的另一集,義務未結案但 checkpoint 缺 dispatched_at。
    def add_a_broken_sibling(manifest):
        source_episode = manifest["episodes"][0]
        broken = copy.deepcopy(
            next(a for a in source_episode["attempts"] if a["attempt_id"] == attempt_a)
        )
        broken["attempt_id"] = "att-broken"
        broken["episode"] = 9
        broken["finalize"]["feedback_source_upload"]["dispatched_at"] = None
        manifest["episodes"].append(
            {
                "episode": 9,
                "title": "壞掉的那一集",
                "notebook_id": "nb-1",
                "attempts": [broken],
                "retracted_attempt_ids": ["att-broken"],
            }
        )

    ManifestStore(manifest_path).update(add_a_broken_sibling)

    with pytest.raises(ValueError) as failure:
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後內容",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    # A 的發現要落盤(不是只活在錯誤訊息裡),而 B 的問題照樣要被報出來。
    assert _episode(manifest_path)["pending_source_cleanup"] == [orphan_a]
    assert orphan_a in str(failure.value)


async def test_gate_names_the_attempt_when_its_checkpoint_cannot_be_reconciled(
    fake_client, tmp_path, monkeypatch
):
    """checkpoint 缺欄位(手改過/跨版本)時 fail-closed,但訊息要說得出是哪一顆。

    生成前突然冒出一句「feedback source dispatch time is missing」沒人查得動。
    """
    manifest_path, attempt_id = await _abandon_an_unresolved_upload(
        fake_client, tmp_path, monkeypatch
    )

    def corrupt_checkpoint(manifest):
        _, attempt = p._attempt_record(manifest, 1, attempt_id, allow_retracted=True)
        attempt["finalize"]["feedback_source_upload"]["dispatched_at"] = None

    ManifestStore(manifest_path).update(corrupt_checkpoint)

    with pytest.raises(ValueError, match="對不出候選") as failure:
        await p.podcast_episode(
            "nb-1", episode_n=1, title=EP["title"], brief="修正後內容",
            output_dir=str(tmp_path), manifest_path=manifest_path,
        )
    assert attempt_id in str(failure.value)
    assert "episode 1" in str(failure.value)


@pytest.mark.parametrize(
    "status", ("dispatching", "acceptance_unknown", "reconciliation_ambiguous")
)
async def test_every_unresolved_upload_status_carries_the_obligation(
    fake_client, tmp_path, status
):
    """**三種 unresolved 狀態都要記義務,不只 `dispatching`。**

    三者的共同後果相同(遠端可能多出一筆 media、manifest 記不住它是誰),只涵蓋一種
    等於另外兩種的孤兒照樣沒人記得。`reconciliation_ambiguous` 已經算出候選時,那組
    候選要直接排進 stale ids ——身分不確定但範圍確定,全刪掉最保守。
    """
    manifest_path, before = await _complete_ep1(fake_client, tmp_path)
    attempt_id = before["output_attempt_id"]
    candidates = ["src-ambiguous-a", "src-ambiguous-b"]

    def unresolve(manifest):
        _, attempt = p._attempt_record(manifest, 1, attempt_id)
        attempt["finalize"]["feedback_source_upload"].update(
            {
                "status": status,
                "source_id": None,
                "candidate_source_ids": (
                    candidates if status == "reconciliation_ambiguous" else []
                ),
            }
        )

    ManifestStore(manifest_path).update(unresolve)

    retracted = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="QA 拒收", abandon_in_flight=True
    )
    assert retracted["source_cleanup_unresolved"] is True
    # `_complete_ep1` 留下的 episode 級 feedback_source_id 投影本來就是確定的義務,
    # 所以三種狀態都先指向 source_delete;unresolved 的部分由 gate 之後再對帳。
    assert retracted["safe_next_action"] == p.ACTION_SOURCE_DELETE
    if status == "reconciliation_ambiguous":
        # 身分不確定但範圍確定:算出來的候選直接全部排進清理義務,最保守。
        assert set(candidates) <= set(retracted["stale_source_ids"])
        assert set(candidates) <= set(_episode(manifest_path)["pending_source_cleanup"])
    else:
        assert not set(candidates) & set(retracted["stale_source_ids"])


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
    assert stopped["safe_next_action"] == p.ACTION_ADOPT, stopped
    # **只用公開回傳取身分,不從 manifest 私下讀**(docs/gotchas-attempt.md「回傳要
    # 自足」那條紅線)——`stopped["attempt_id"]` / `stopped["candidate_source_ids"]`
    # 本來就帶著正確的值,舊版在候選清單這裡繞過去直接讀 manifest,連「回傳給不給得出
    # 這份候選」都沒被驗到(P1 修復)。
    attempt_id = stopped["attempt_id"]
    candidates = stopped["candidate_source_ids"]
    assert len(candidates) == 2
    candidate_a, candidate_b = candidates

    adopted = await p.podcast_attempt_adopt(
        str(manifest_path), episode_n=1, attempt_id=attempt_id,
        feedback_source_id=candidate_a,
    )
    assert PUBLIC_GUIDANCE_EXCEPTIONS[
        "podcast_attempt_adopt.feedback_source"
    ].strip()
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


async def test_the_audit_record_names_the_authorization_basis(fake_client, tmp_path):
    """**F4:v0.9.6 補的 `authorization_basis` / `remote_status_at_retraction` 零測試。**

    `grep -rn authorization_basis tests/` 舊只命中 `_attempt_capabilities` 的**回傳值**
    斷言(`test_attempt_capabilities.py`),沒有一處讀 retraction **落盤紀錄**;
    `remote_status_at_retraction` 在 tests/ 舊全零命中。這兩個欄位是 v0.9.6 用來取代
    「只記呼叫端傳了什麼」(`abandon_in_flight`)的**實際生效授權依據**——欄位名稱、
    值域拼錯或漏寫都不會被任何既有測試抓到,補上落盤斷言,至少蓋 `settled` 與
    `output_owner` 兩種 basis。
    """
    from notebooklm.exceptions import RateLimitError

    # settled:從沒送出去,manifest 自己就知道結果,不需要旗標。
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain_path = str(plain_dir / "series_manifest.json")
    fake_client.artifacts.generate_audio_exc = RateLimitError("每日配額已用盡")
    refused = await p.podcast_series("nb-1", episodes=[EP], output_dir=str(plain_dir))
    fake_client.artifacts.generate_audio_exc = None
    await p.podcast_attempt_retract(
        plain_path, 1, refused["attempt_id"], reason="brief 寫錯"
    )
    stored = json.loads(open(plain_path, encoding="utf-8").read())
    retraction = stored["episodes"][0]["attempts"][0]["retraction"]
    assert retraction["authorization_basis"] == "settled"
    assert retraction["remote_status_at_retraction"] == "failed"

    # output_owner:正常 QA 拒收一顆已完成的正式輸出,同樣不需要旗標。
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    output_path, episode = await _complete_ep1(fake_client, output_dir)
    await p.podcast_attempt_retract(
        output_path, 1, episode["output_attempt_id"], reason="QA 拒收"
    )
    stored2 = json.loads(open(output_path, encoding="utf-8").read())
    retraction2 = stored2["episodes"][0]["attempts"][0]["retraction"]
    assert retraction2["authorization_basis"] == "output_owner"
    assert retraction2["remote_status_at_retraction"] == "completed"


async def test_retract_sends_a_pinned_episode_back_to_the_single_entry_point(
    fake_client, tmp_path
):
    """**`safe_next_action` 不能把帶 `source_ids` 的一集丟回 `podcast_series`。**

    v0.9.4 Codex 複審抓到的最重一條,已重現:`podcast_episode(source_ids=["src-1"])`
    撞配額停在 `not_accepted` → retract(沒有 stale source)→ 回
    `safe_next_action="podcast_series"` → 呼叫端照做 → series 建的新 attempt
    **不帶 source_ids**,兩次 dispatch 實際送出 `[["src-1"], None]`。

    生成輸入被靜默改掉,而那正是本 repo 最忌諱的內容錯置形狀 —— skill 也明寫「帶
    `source_ids` 的 attempt 不能用 `podcast_series` 續」。`input_bundle` 同理:
    series 生不出那個形狀。
    """
    from notebooklm.exceptions import RateLimitError

    manifest_path = str(tmp_path / "series_manifest.json")
    fake_client.sources.seed("EP01 題目", "EP01 附錄", "EP01 補充")
    fake_client.artifacts.generate_audio_exc = RateLimitError("每日配額已用盡")
    with pytest.raises(RateLimitError):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="1",
            output_dir=str(tmp_path),
            manifest_path=manifest_path,
            source_ids=["src-1"],
        )
    fake_client.artifacts.generate_audio_exc = None
    attempt_id = _episode(manifest_path)["active_attempt_id"]

    out = await p.podcast_attempt_retract(
        manifest_path, 1, attempt_id, reason="brief 改了"
    )

    assert out["safe_next_action"] == "podcast_episode", (
        "帶 source_ids 的一集丟回 series 會靜默改掉生成輸入"
    )
    assert "source_ids" in out["next_step"]


async def test_retract_does_not_offer_the_flag_to_a_superseded_attempt(
    fake_client, tmp_path
):
    """**歷史 attempt 不是旗標的守備範圍,訊息不可以教它傳。**

    `abandon_in_flight` 只放行 `active_attempt_id` 那一顆。A 被 supersede、B 接手之後,
    拿 A 的 id 來 retract:傳 False 與傳 True 得到**同一句**指引 —— v0.9.4 新寫的訊息
    卻教它「帶 abandon_in_flight=True 重呼」,那是永遠無效的動作,又一個死路指引。

    正確做法是先講清楚 ownership:它已經被取代,要動的是現在的 active／output。
    """
    manifest_path, _ = await _complete_ep1(fake_client, tmp_path)
    episode = _episode(manifest_path)
    output_attempt_id = episode["output_attempt_id"]
    ManifestStore(manifest_path).update(
        lambda manifest: manifest["episodes"][0]["attempts"].append(
            {
                "attempt_id": "att-superseded",
                "episode": 1,
                "title": "心法篇",
                "dispatch": {"status": "accepted"},
                "remote": {"status": "failed", "artifact_id": None},
                "finalize": {},
            }
        )
    )

    with pytest.raises(ValueError) as caught:
        await p.podcast_attempt_retract(
            manifest_path, 1, "att-superseded", reason="想清掉歷史"
        )

    msg = str(caught.value)
    # 訊息**可以**提到旗標(用來解釋「它對你無效」),但不可以**教它傳** ——
    # 那才是死路指引。所以禁的是指示形式,不是字眼本身。
    assert "abandon_in_flight=True" not in msg, f"對歷史 attempt 教了無效的旗標: {msg}"
    assert "只放行 active" in msg, f"沒說清楚旗標為什麼對它無效: {msg}"
    assert output_attempt_id in msg, f"沒指出現在真正的 output 是哪顆: {msg}"


async def test_a_retracted_frozen_bundle_is_not_offered_for_reuse(
    fake_client, tmp_path
):
    """**frozen bundle 的重生指引不能說「用同一份 bundle」。**

    `attempt-binding.json` 刻意只能建立一次,而它綁的正是這顆已成為 tombstone 的
    attempt —— 沿用同一份 bundle 重生會撞 `was retracted`,**新 dispatch 數 = 0**。
    v0.9.5 的 `next_step` 卻寫著「或同一份 frozen bundle」,照做完全生不出東西。
    """
    from test_generation_input_bundle import _write_bundle
    from notebooklm.exceptions import RateLimitError

    workspace = tmp_path / "workspace"
    manifest_path = workspace / "manifest" / "series_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    bundle, _ = _write_bundle(workspace)
    args = dict(
        episode_n=1,
        title="心法篇",
        brief=None,
        output_dir=str(workspace / "output"),
        manifest_path=str(manifest_path),
        input_bundle_path=str(bundle.relative_to(workspace)),
    )
    fake_client.artifacts.generate_audio_exc = RateLimitError("每日配額已用盡")
    with pytest.raises(RateLimitError):
        await p.podcast_episode("nb-1", **args)
    fake_client.artifacts.generate_audio_exc = None
    attempt_id = _episode(str(manifest_path))["active_attempt_id"]

    out = await p.podcast_attempt_retract(
        str(manifest_path), 1, attempt_id, reason="輸入要重做"
    )

    assert out["safe_next_action"] == "podcast_episode"
    assert "新的、尚未綁定" in out["next_step"], out["next_step"]
    assert "同一份 frozen bundle" not in out["next_step"], out["next_step"]
    # 而且真的照舊 bundle 做會被 tombstone 擋 —— 證明那句指引若沒改就是死路。
    before = len([c for c in fake_client.artifacts.calls if c[0] == "generate_audio"])
    with pytest.raises(ValueError, match="retracted"):
        await p.podcast_episode("nb-1", **args)
    after = len([c for c in fake_client.artifacts.calls if c[0] == "generate_audio"])
    assert after == before, "撞 tombstone 時不該有任何新 dispatch"
