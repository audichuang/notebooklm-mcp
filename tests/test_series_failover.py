"""這批 P0-P2 bugfix 補的迴歸測試(2026-08-09 核實輪),集中在 `podcast_series` 的
inline 重送分支與周邊的 attempt 生命週期。**注意檔名跟其中一條測試(retract)不完全對得
上**:分工限制只讓這批修正動 `tools_podcast.py` / `test_quota_failover.py` / 新建這一支,
retract 的正主檔案 `test_attempt_retract.py` 不在允許清單裡,所以那條測試借放這裡
(module docstring 講清楚原因,不是分類錯誤)。

- 陳舊 client:`podcast_series` 開頭抓一次的 `client` 區域變數不會跟著
  failover 走,inline 重送分支之後的 finalize/下載,以及後續各集的清理義務對帳／
  drift 複驗／baseline／認證預檢,都可能繼續打在被拒帳號上。
- inline 重送分支回報說謊:它無條件回 `not_accepted`,沒有像姊妹分支
  (`_run_episode` 那圈)一樣覆核 manifest 裡真正記下的狀態。
- retract 對「已 accepted、從未 promote」的 attempt 沒有出路。
"""
import json

import pytest

from notebooklm_mcp import runtime
from notebooklm_mcp import tools_podcast as p

from conftest import FakeClient


def _episode(manifest_path, episode_n=1):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return next(e for e in data["episodes"] if e["episode"] == episode_n)


async def test_inline_resend_finalize_uses_the_rotated_client_not_the_stale_one(
    tmp_path,
):
    """陳舊 client 的核心形狀:inline 重送分支 dispatch 之後,finalize 用的必須是**實際送出**
    的那個帳號,不是 `podcast_series` 開頭抓的那個陳舊區域變數。

    既有測試的結構性盲點是每個槽位裝**同一個** fake client 物件,「這通 RPC 實際發給
    哪個 client」不可見。這裡刻意用兩個不同的 `FakeClient()` 實例,直接斷言:被拒的
    那個帳號在它自己的 generate_audio 呼叫之後,不該再收到任何 RPC(wait/rename/
    download/add_file 全部不該出現在它的 `.calls` 裡);換到的那個帳號要收到全部。
    """
    client_a = FakeClient()
    client_b = FakeClient()
    b_default_generate = client_b.artifacts.generate_audio  # 第二輪要還原回這個

    async def a_always_refuses(*args, **kwargs):
        from notebooklm.exceptions import RateLimitError

        # 記進 `.calls`:預設的 generate_audio 實作會記,直接換掉整個方法就不會——
        # 補這行讓「A 之後有沒有再收到任何呼叫」這個斷言的證據完整。
        client_a.artifacts.calls.append(("generate_audio", {}))
        raise RateLimitError("每日配額已用盡")

    # 第一輪:兩個帳號都耗盡 → 安全停點,留下一個 not_accepted 的 attempt。
    client_a.artifacts.generate_audio = a_always_refuses
    client_b.artifacts.generate_audio = a_always_refuses
    runtime.set_clients([("a@x", client_a), ("b@x", client_b)])
    manifest_path = tmp_path / "series_manifest.json"
    stopped = await p.podcast_series(
        "nb-1", episodes=[{"title": "心法篇", "brief": "1"}],
        output_dir=str(tmp_path), start=1,
    )
    assert stopped["observed_state"] == "not_accepted"

    # 第二輪(隔天配額回來,同一 process 重呼):A 依然被拒,B 恢復正常——
    # 這一輪必須走 inline 重送分支(attempt 已存在、被 rearm 成 prepared)。
    runtime.set_clients([("a@x", client_a), ("b@x", client_b)])
    client_a.artifacts.generate_audio = a_always_refuses
    client_b.artifacts.generate_audio = b_default_generate

    out = await p.podcast_series(
        "nb-1", episodes=[{"title": "心法篇", "brief": "1"}],
        output_dir=str(tmp_path), start=1,
    )
    assert out["complete"] is True

    a_call_kinds = [call[0] for call in client_a.artifacts.calls]
    b_call_kinds = [call[0] for call in client_b.artifacts.calls]

    # A 只准出現 dispatch 前的 baseline `list`(在還沒 rotate 之前就會打)跟它自己
    # 被拒的 `generate_audio`——finalize 相關的呼叫(wait/rename/download)絕不能
    # 打在被拒的那個帳號上,那正是陳舊 `client` 區域變數的症狀。
    assert set(a_call_kinds) <= {"list", "generate_audio"}, (
        f"A 不該收到 finalize 相關呼叫,實際收到:{a_call_kinds}"
    )
    assert "wait" not in a_call_kinds
    assert "rename" not in a_call_kinds
    assert "download" not in a_call_kinds
    assert "wait" in b_call_kinds, "finalize 的 wait_for_completion 要打在換過去的 B"
    assert "rename" in b_call_kinds
    assert "download" in b_call_kinds
    assert any(call[0] == "add_file" for call in client_b.sources.calls), (
        "回錄 source 的自我上傳也要用 B 的身分"
    )
    # A 只收得到**來源筆數守門的唯讀 `list`**,一共三趟,每一趟都在 dispatch 之前、
    # 那時作用中帳號還是 A:①第一輪是全新一集,series 迴圈與 `_run_episode` 各驗一次
    # (兩個入口各自守得住,代價是同一集冗餘一趟唯讀 list);②第二輪走 inline 重送
    # 分支,只有 series 迴圈那一道。
    #
    # 這條紀律真正要防的是**寫入與 finalize 打在 stale client 上** —— 回錄上傳
    # (`add_file`)、改名、下載。原本寫成「完全空」是因為當時 A 一次都收不到,那是表象;
    # 但放寬成「只要都是 list 就好」又太鬆:finalize 階段的來源對帳若偷跑到 stale A、
    # 而後續寫入仍在 B,測試會照樣綠 —— 那正是這支測試要抓的東西。所以逐字鎖住次數
    # 與種類;之後誰改動守門位置,這裡會紅,而那時本來就該重新確認這條紀律。
    assert [call[0] for call in client_a.sources.calls] == ["list", "list", "list"], (
        f"A 只該收到守門的唯讀 list,實際收到:{client_a.sources.calls}"
    )


async def test_cross_episode_probe_auth_follows_the_rotated_account(tmp_path):
    """陳舊 client 的第二個形狀:同一次 `podcast_series` 呼叫裡,前一集 failover 換過帳號後,
    後面各集開頭的 `probe_auth(client)` 也要用新帳號,不能繼續驗被拒帳號的 cookie。

    用 `notebooks.list`(`probe_auth` 唯一打的 RPC)記下每次呼叫**打在哪個物件上**:
    預期序列是 [pre-loop probe: A]、[EP1 dispatch 前 probe: A]、
    [EP2 dispatch 前 probe: B]——EP1 在它自己的 dispatch 中途從 A rotate 到 B,
    陳舊的話 EP2 那次 probe 仍會停在 A。

    **記的是固定標籤,不是 `runtime.active_account()`**:後者是 process 全域狀態,
    不管呼叫實際打在 A 還是 B 的物件上都會回同一個值——用它記錄等於又踩進
    AGENTS.md 講的那個結構性盲點(兩個槽位裝同一個物件時看不出「這通 RPC 實際發給
    哪個 client」),這裡刻意繞開,直接綁定被呼叫的是哪個 fake 物件。
    """
    client_a = FakeClient()
    client_b = FakeClient()
    probed: list = []

    def _wrap_notebooks_list(client, label):
        original = client.notebooks.list

        async def wrapped():
            probed.append(label)
            return await original()

        client.notebooks.list = wrapped

    _wrap_notebooks_list(client_a, "a@x")
    _wrap_notebooks_list(client_b, "b@x")

    async def a_refuses_once(*args, **kwargs):
        from notebooklm.exceptions import RateLimitError

        raise RateLimitError("每日配額已用盡")

    client_a.artifacts.generate_audio = a_refuses_once
    # client_b 用預設成功實作。

    runtime.set_clients([("a@x", client_a), ("b@x", client_b)])
    await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "1"}, {"title": "實戰篇", "brief": "2"}],
        output_dir=str(tmp_path),
        start=1,
    )

    assert probed == ["a@x", "a@x", "b@x"], (
        "EP2 開頭的認證預檢要用 EP1 rotate 之後的帳號(B),不能還停在 A"
    )


async def test_inline_resend_does_not_lie_about_acceptance_unknown(fake_client, tmp_path):
    """inline 重送分支不能無條件回 `not_accepted`。

    `_dispatch_audio_with_failover` 的泛用 except 分支會把一個普通 `RuntimeError`
    標成 `acceptance_unknown` 並原樣重拋——但它剛好也是 `RuntimeError` 型別,舊碼的
    `except (RuntimeError, *_REFUSED_WITHOUT_DISPATCH):` 不覆核 manifest 就無條件
    回傳 `observed_state="not_accepted"` 的結構化停點,manifest 卻寫著
    `acceptance_unknown`——回報說謊。修法是覆核 manifest 裡真正記下的狀態,照它回報。

    判準是**三態**而不是「是/不是 not_accepted」:記著 `not_accepted` → 可直接重跑整季的
    停點;記著別的狀態 → 一樣回結構化停點,只是 `safe_next_action` 換成先對帳;
    **manifest 根本讀不到** → 我們什麼都不知道,原例外原樣交出去(`abandon_in_flight`
    讓「並行 retract 掉在飛的 attempt」變成受支援操作,`_attempt_record` 會對 tombstone
    raise,而在 except handler 裡再拋會蓋掉真正該讀的錯誤)。
    """
    from notebooklm.exceptions import RateLimitError

    manifest_path = tmp_path / "series_manifest.json"
    episodes = [{"title": "心法篇", "brief": "1"}]

    # 第一輪:全部耗盡 → not_accepted 的 attempt 留在 manifest(進入 inline 重送分支
    # 的前提:attempt 已存在)。
    runtime.set_clients([("solo@x", fake_client)])

    async def refuse(*args, **kwargs):
        raise RateLimitError("每日配額已用盡")

    fake_client.artifacts.generate_audio = refuse
    stopped = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path), start=1
    )
    assert stopped["observed_state"] == "not_accepted"

    # 第二輪:同一個帳號,這次改丟一個跟配額/權限都無關的普通 RuntimeError——
    # `_dispatch_audio_with_failover` 的泛用分支會標成 acceptance_unknown。
    async def boom(*args, **kwargs):
        raise RuntimeError("weird transient failure, not a refusal")

    fake_client.artifacts.generate_audio = boom
    runtime.set_clients([("solo@x", fake_client)])

    result = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path), start=1
    )

    attempt = _episode(manifest_path)["attempts"][0]
    assert attempt["dispatch"]["status"] == "acceptance_unknown", (
        "manifest 記的才是真相——回報不能跟它不一致"
    )
    # 回報要與 manifest 一致,**而且要是結構化停點**。這一版曾經在這裡裸拋:同一個
    # acceptance_unknown,只因為例外型別剛好是 RuntimeError 就走不同的路——底下那個
    # `except Exception` 對一模一樣的狀態是回 partial 的。裸拋的實際代價是**前面幾集
    # 已經跑完的 `run_results` 整份丟掉**,而 `podcast_series` 對呼叫端的契約正是
    # 「預期內的停止用回傳值表達」。acceptance_unknown 是預期內的(要先對帳),不是異常。
    assert result["complete"] is False
    assert result["observed_state"] == "acceptance_unknown"
    assert result["safe_next_action"] == "podcast_episode_reconcile"
    assert result["attempt_id"] == attempt["attempt_id"]


async def test_retract_can_abandon_an_accepted_attempt_that_was_never_promoted(
    fake_client, tmp_path
):
    """dispatch 已 accepted、從未 promote 的 candidate 也要有出口。

    真實事故形狀:某集透過 `podcast_episode_resume` 接上一個「已在雲端 accepted」的
    artifact,但那顆 artifact 對應的 brief 是失真版——finalize 還沒跑完就想作廢。
    `output_attempt_id` 這時是 None(從未 promote),原本的 `abandons_unauthorized_
    candidate` 只認「output_attempt_id 指向別的 attempt」這一種分岔,把 None 排除
    在外,於是 retract 唯一走得通的路反而被「only a promoted output attempt can be
    retracted」擋死。

    這一集刻意帶著 v0.5.0 前風格的 episode 級 legacy 硬證據(`artifact_id`/`mp3_path`
    直接掛在 episode 上,沒有 `output_attempt_id` 這個概念)——確認 retract 連同它一起
    清掉,而不是隨手放行卻讓下一次重生撞上 `has_hard_output_evidence` 的另一個死路。

    **不能跟『單純還在飛的第一顆 attempt』混在一起**(該用 reconcile/resume,不是
    retract——`test_attempt_retract.py::test_retract_refuses_an_attempt_that_is_not_
    the_durable_output` 鎖著):所以只在 episode 已有 legacy 硬證據時才放行,這正是
    上面那個「legacy 硬證據」前提存在的理由,不是可省的裝飾。
    """
    manifest_path = tmp_path / "series_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "episodes": [
                    {
                        "episode": 1,
                        "title": "心法篇",
                        "label": "EP01 心法篇",
                        "artifact_id": "legacy-art-1",
                        "mp3_path": str(tmp_path / "legacy-ep01.mp3"),
                        "published_at": "Mon, 01 Jan 2024 00:00:00 +0800",
                        "feedback_source_id": "legacy-src-1",
                        "attempts": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = p.ManifestStore(str(manifest_path))
    bad_attempt_id = p._ensure_resume_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        artifact_id="bad-artifact-1",
    )

    episode_before = _episode(manifest_path)
    assert episode_before["active_attempt_id"] == bad_attempt_id
    assert episode_before.get("output_attempt_id") is None
    assert episode_before["artifact_id"] == "legacy-art-1", "先確認 legacy 欄位還在"

    out = await p.podcast_attempt_retract(
        manifest_path=str(manifest_path),
        episode_n=1,
        attempt_id=bad_attempt_id,
        reason="brief 失真,重灌前作廢",
    )
    assert out["observed_state"] == "retracted"
    assert out["stale_source_ids"] == ["legacy-src-1"]

    episode_after = _episode(manifest_path)
    assert "active_attempt_id" not in episode_after
    assert "artifact_id" not in episode_after, (
        "legacy 硬證據不清掉,重生時 has_hard_output_evidence 會擋出另一個死路"
    )
    assert "mp3_path" not in episode_after
    assert "output_attempt_id" not in episode_after

    # 作廢之後,這集必須真的能重新生成——這才是「有出口」的驗收標準,不是只驗欄位。
    runtime.set_clients([("solo@x", fake_client)])
    result = await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="重灌版",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    assert result["episode"] == 1
