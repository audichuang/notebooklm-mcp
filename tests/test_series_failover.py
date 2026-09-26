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
import uuid
from datetime import datetime, timezone

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
    預期序列是 [EP1 第一個遠端操作前: A]、[EP2 第一個遠端操作前: B]——
    EP1 在它自己的 dispatch 中途從 A rotate 到 B,
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

    assert probed == ["a@x", "b@x"], (
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


@pytest.mark.parametrize(
    "attempt_state",
    ["missing", "not_accepted", "acceptance_unknown"],
    ids=["fresh", "resend", "reconcile"],
)
async def test_series_auth_expiry_preserves_prior_results_without_mutation(
    fake_client, tmp_path, monkeypatch, attempt_state
):
    """EP2 認證失效時要回得出 EP1,而且不得建立、re-arm 或 supersede EP2。"""
    from notebooklm.exceptions import RateLimitError

    episodes = [
        {"title": "心法篇", "brief": "1"},
        {"title": "實戰篇", "brief": "2"},
    ]
    manifest_path = tmp_path / "series_manifest.json"
    attempt_id = None
    if attempt_state != "missing":
        store = p.ManifestStore(str(manifest_path))
        attempt_id = p._create_audio_attempt(
            store,
            notebook_id="nb-1",
            episode_n=2,
            title="實戰篇",
            brief="2",
            language=p.resolve_language(None),
            audio_format="deep-dive",
            audio_length="long",
        )
        assert p._claim_prepared_dispatch(
            store, 2, attempt_id, [], account="#1", wait_timeout=1200.0
        )
        if attempt_state == "not_accepted":
            p._mark_not_accepted(
                store, 2, attempt_id, RateLimitError("每日配額已用盡")
            )
        else:
            p._mark_acceptance_unknown(
                store, 2, attempt_id, TimeoutError("response lost")
            )

    original_generate = fake_client.artifacts.generate_audio
    generate_count = 0

    async def counted_generate(*args, **kwargs):
        nonlocal generate_count
        generate_count += 1
        return await original_generate(*args, **kwargs)

    fake_client.artifacts.generate_audio = counted_generate
    original_probe = p.probe_auth

    async def expire_after_first_episode(client):
        if generate_count == 1:
            raise p._AuthProbeError("authentication expired")
        return await original_probe(client)

    monkeypatch.setattr(p, "probe_auth", expire_after_first_episode)
    result = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path), start=1
    )

    assert result["complete"] is False
    assert result["stopped_at_episode"] == 2
    assert result["observed_state"] == "auth_expired"
    assert result["safe_next_action"] == "podcast_series"
    assert len(result["episodes"]) == 1
    assert generate_count == 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode_2 = next(
        (row for row in manifest["episodes"] if row.get("episode") == 2), None
    )
    if attempt_state != "missing":
        assert episode_2 is not None
        assert episode_2["active_attempt_id"] == attempt_id
        assert len(episode_2["attempts"]) == 1
        assert episode_2["attempts"][0]["dispatch"]["status"] == attempt_state
    else:
        assert episode_2 is None


@pytest.mark.parametrize(
    "attempt_state", ["prepared", "not_accepted"], ids=["prepared", "resend"]
)
async def test_auth_expiry_hands_a_pinned_attempt_to_its_real_owner(
    fake_client, tmp_path, monkeypatch, attempt_state
):
    """認證停點的 `safe_next_action` 不得寫死 `podcast_series`。

    寫死的後果不是「訊息不精準」而是**內容錯置**:這一集的 attempt 指名了來源,而
    `podcast_series` 生不出帶 `source_ids` 的 settings。呼叫端照著這個欄位重呼,
    series 會把它 supersede 成不指名的新 attempt、改讀整本筆記本(含後面各集的回錄
    音檔),然後回報 `complete=True`。

    docs/gotchas-attempt.md 的紅線正是為這種形狀存在:狀態相關的指引一律由
    `_attempt_capabilities()` 產生,不准手寫。
    """
    from notebooklm.exceptions import RateLimitError

    episodes = [
        {"title": "心法篇", "brief": "1"},
        {"title": "實戰篇", "brief": "2"},
    ]
    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=2,
        title="實戰篇",
        brief="2",
        language=p.resolve_language(None),
        audio_format="deep-dive",
        audio_length="long",
        source_ids=["src-1"],
    )
    if attempt_state == "not_accepted":
        assert p._claim_prepared_dispatch(
            store, 2, attempt_id, [], account="#1", wait_timeout=1200.0
        )
        p._mark_not_accepted(store, 2, attempt_id, RateLimitError("每日配額已用盡"))

    original_generate = fake_client.artifacts.generate_audio
    generate_count = 0

    async def counted_generate(*args, **kwargs):
        nonlocal generate_count
        generate_count += 1
        return await original_generate(*args, **kwargs)

    fake_client.artifacts.generate_audio = counted_generate
    original_probe = p.probe_auth

    async def expire_after_first_episode(client):
        if generate_count == 1:
            raise p._AuthProbeError("authentication expired")
        return await original_probe(client)

    monkeypatch.setattr(p, "probe_auth", expire_after_first_episode)
    result = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path), start=1
    )

    assert result["observed_state"] == "auth_expired"
    assert result["stopped_at_episode"] == 2
    assert len(result["episodes"]) == 1, "EP1 的結果不能因為交棒就丟掉"
    assert result["safe_next_action"] == "podcast_episode", result["safe_next_action"]
    # 指引的散文與欄位必須同源,而且要講得出「重生要帶回原本那組來源」。
    assert "source_ids" in result["next_step"], result["next_step"]
    # 交棒不得順手改狀態(認證失效時本來就什麼都不該動)。
    episode_2 = _episode(manifest_path, 2)
    assert episode_2["active_attempt_id"] == attempt_id
    assert len(episode_2["attempts"]) == 1
    assert episode_2["attempts"][0]["dispatch"]["status"] == attempt_state


async def test_auth_handoff_carries_the_sources_the_next_step_needs(
    fake_client, tmp_path, monkeypatch
):
    """交棒的回傳必須**自足**(docs/gotchas-attempt.md 的自足性紅線)。

    停點說「下一步是 podcast_episode」而那顆 attempt 指名了來源時,呼叫端如果拿不到
    那組 id 就只能省略 —— `podcast_episode` 的 `source_ids` 預設是 `None`,於是整條
    官方復原路徑靜默改讀整本筆記本。這條測試**只用公開回傳**續跑,不從 manifest
    私下取值,否則它證明不了任何事。
    """
    from notebooklm.exceptions import RateLimitError

    fake_client.sources.seed("EP01 題目", "EP02 題目")
    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=2,
        title="實戰篇",
        brief="2",
        language=p.resolve_language(None),
        audio_format="deep-dive",
        audio_length="long",
        source_ids=["src-2"],
    )
    assert p._claim_prepared_dispatch(
        store, 2, attempt_id, [], account="#1", wait_timeout=1200.0
    )
    p._mark_not_accepted(store, 2, attempt_id, RateLimitError("每日配額已用盡"))

    async def always_expired(client):
        raise p._AuthProbeError("authentication expired")

    monkeypatch.setattr(p, "probe_auth", always_expired)
    result = await p.podcast_series(
        "nb-1",
        episodes=[
            {"title": "心法篇", "brief": "1"},
            {"title": "實戰篇", "brief": "2"},
        ],
        output_dir=str(tmp_path),
        start=2,
    )
    assert result["safe_next_action"] == "podcast_episode"
    assert result["regeneration_source_ids"] == ["src-2"], result

    # 重登後照回傳續跑:只用 `regeneration_source_ids`,不碰 manifest。
    monkeypatch.undo()
    out = await p.podcast_episode(
        "nb-1",
        episode_n=2,
        title="實戰篇",
        brief="2",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
        source_ids=result["regeneration_source_ids"],
    )
    assert out["episode"] == 2
    dispatched = [
        c[1]["source_ids"]
        for c in fake_client.artifacts.calls
        if c[0] == "generate_audio"
    ]
    assert dispatched == [["src-2"]], dispatched
    # 沿用同一顆重送,不新建 attempt。
    assert [a["attempt_id"] for a in _episode(manifest_path, 2)["attempts"]] == [
        attempt_id
    ]


async def test_auth_handoff_spells_out_a_drifted_argument(
    fake_client, tmp_path, monkeypatch
):
    """指對工具還不夠 —— 附帶條件沒講,呼叫端照做一樣走不通。

    這一集的 attempt 是 series 自己建的,所以停點回 `podcast_series` 是對的;但**這次
    呼叫的 language 與它不同**,原樣重呼會撞 settings 守門而裸拋。`next_step` 必須把
    「要用原本那組參數」講出來。
    """
    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        brief="1",
        language=p.resolve_language("en"),
        audio_format="deep-dive",
        audio_length="long",
    )

    async def always_expired(client):
        raise p._AuthProbeError("authentication expired")

    monkeypatch.setattr(p, "probe_auth", always_expired)
    result = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "1"}],
        output_dir=str(tmp_path),
        language="ja",
    )
    assert result["safe_next_action"] == "podcast_series"
    assert "language" in result["next_step"], result.get("next_step")


async def test_auth_expiry_keeps_pointing_at_series_for_a_finished_episode(
    fake_client, tmp_path, monkeypatch
):
    """反向護欄:已完成的一集不能被交棒指引拖去 retract。

    完成的那一集的 output attempt 也帶著 `source_ids`,但 series 走到它時只會跳過。
    若交棒判準只看「settings 認不認得」,這裡會回 `podcast_attempt_retract` ——
    叫人作廢一集已經產出的正式輸出。判準必須是「series 會不會在這顆上重新 dispatch」。
    """
    fake_client.sources.seed("EP01 題目")
    manifest_path = tmp_path / "series_manifest.json"
    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="1",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
        source_ids=["src-1"],
    )
    assert _episode(manifest_path, 1)["output_attempt_id"] is not None

    async def always_expired(client):
        raise p._AuthProbeError("authentication expired")

    monkeypatch.setattr(p, "probe_auth", always_expired)
    result = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "1"}],
        output_dir=str(tmp_path),
    )
    assert result["observed_state"] == "auth_expired"
    assert result["safe_next_action"] == "podcast_series", result["safe_next_action"]


async def test_series_transient_auth_probe_failure_preserves_prior_results(
    fake_client, tmp_path, monkeypatch
):
    """EP2 的 auth probe 暫時斷線時回可重試停點，不裸拋或丟失 EP1。"""
    from notebooklm.exceptions import NetworkError

    episodes = [
        {"title": "心法篇", "brief": "1"},
        {"title": "實戰篇", "brief": "2"},
    ]
    manifest_path = tmp_path / "series_manifest.json"
    original_generate = fake_client.artifacts.generate_audio
    generate_count = 0

    async def counted_generate(*args, **kwargs):
        nonlocal generate_count
        generate_count += 1
        return await original_generate(*args, **kwargs)

    fake_client.artifacts.generate_audio = counted_generate
    original_probe = p.probe_auth

    async def disconnect_after_first_episode(client):
        if generate_count == 1:
            raise NetworkError("temporary auth probe outage")
        return await original_probe(client)

    monkeypatch.setattr(p, "probe_auth", disconnect_after_first_episode)
    result = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path), start=1
    )

    assert result["complete"] is False
    assert result["stopped_at_episode"] == 2
    assert result["observed_state"] == "verification_incomplete"
    assert result["safe_next_action"] == "podcast_series"
    assert len(result["episodes"]) == 1
    assert result["episodes"][0]["episode"] == 1
    assert "temporary auth probe outage" in result["error"]
    assert generate_count == 1
    assert all(
        row.get("episode") != 2
        for row in json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"]
    )


async def test_series_auth_expiry_stops_before_pending_cleanup_rpc(
    fake_client, tmp_path, monkeypatch
):
    """每集的認證守門排在清理義務之前,且保住已完成集的結果。"""
    from notebooklm.exceptions import RPCError, RateLimitError

    episodes = [
        {"title": "心法篇", "brief": "1"},
        {"title": "實戰篇", "brief": "2"},
    ]
    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=2,
        title="實戰篇",
        brief="2",
        language=p.resolve_language(None),
        audio_format="deep-dive",
        audio_length="long",
    )
    assert p._claim_prepared_dispatch(
        store, 2, attempt_id, [], account="#1", wait_timeout=1200.0
    )
    p._mark_not_accepted(store, 2, attempt_id, RateLimitError("每日配額已用盡"))

    expired = False
    original_finalize = p.finalize_attempt
    original_probe = fake_client.notebooks.list
    original_sources = fake_client.sources.list

    def add_cleanup(manifest):
        row = next(item for item in manifest["episodes"] if item["episode"] == 2)
        p._record_cleanup_obligation(row, "stale", "nb-1")

    async def finalize_then_expire(*args, **kwargs):
        nonlocal expired
        result = await original_finalize(*args, **kwargs)
        store.update(add_cleanup)
        expired = True
        return result

    async def fail_probe_when_expired():
        if expired:
            raise RPCError("unauthenticated", rpc_code=16)
        return await original_probe()

    async def reject_cleanup_before_probe(*args, **kwargs):
        if expired:
            raise AssertionError("cleanup ran before auth guard")
        return await original_sources(*args, **kwargs)

    monkeypatch.setattr(p, "finalize_attempt", finalize_then_expire)
    fake_client.notebooks.list = fail_probe_when_expired
    fake_client.sources.list = reject_cleanup_before_probe

    result = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path), start=1
    )

    episode_2 = _episode(manifest_path, 2)
    assert result["observed_state"] == "auth_expired"
    assert result["stopped_at_episode"] == 2
    assert len(result["episodes"]) == 1
    assert result["attempt_count"] == 1
    assert episode_2["active_attempt_id"] == attempt_id
    assert episode_2["attempts"][0]["dispatch"]["status"] == "not_accepted"


async def test_series_reconcile_reuses_per_episode_auth_probe(fake_client, tmp_path):
    """acceptance_unknown 的對帳不重複燒一次認證 RPC。"""
    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        brief="1",
        language=p.resolve_language(None),
        audio_format="deep-dive",
        audio_length="long",
    )
    assert p._claim_prepared_dispatch(
        store, 1, attempt_id, [], account="#1", wait_timeout=1200.0
    )
    p._mark_acceptance_unknown(store, 1, attempt_id, TimeoutError("response lost"))
    probes = 0
    original_probe = fake_client.notebooks.list

    async def counted_probe():
        nonlocal probes
        probes += 1
        return await original_probe()

    fake_client.notebooks.list = counted_probe
    result = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "1"}],
        output_dir=str(tmp_path),
    )

    assert result["safe_next_action"] == "podcast_episode_reconcile"
    assert probes == 1


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

    T5(P1 修復,wp-a2):這顆 candidate 的前置狀態**不再靠呼叫
    `_ensure_resume_attempt(artifact_id="bad-artifact-1")` 產生**——那正是它現在
    要 fail-fast 擋下的形狀(legacy 硬證據綁著別的 artifact,resume 指名第三顆會
    先 rename/download/上傳三個遠端副作用才在 promote 被擋)。這支測試驗的是
    **retract** 能不能作廢一顆「accepted、從未 promote」的候選,不是那條建立路徑,
    所以前置狀態改成直接寫 manifest fixture(手搭一顆同形狀的 attempt),繞過建立
    路徑本身,斷言不變。
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
    bad_attempt_id = str(uuid.uuid4())

    def _inject_accepted_never_promoted_candidate(manifest: dict) -> None:
        # 手搭與 `_ensure_resume_attempt` 新建分支同形狀的一筆(dispatch.status=
        # "accepted"、remote.artifact_id 指向與 legacy 硬證據不同的第三顆
        # artifact、未 promote),繞過現在會 fail-fast 擋下這個組合的建立路徑本身。
        episode = manifest["episodes"][0]
        now = datetime.now(timezone.utc).isoformat()
        episode["attempts"].append(
            {
                "attempt_id": bad_attempt_id,
                "created_at": now,
                "notebook_id": "nb-1",
                "episode": 1,
                "title": "心法篇",
                "brief_sha256": None,
                "settings": {"origin": "explicit_resume"},
                "dispatch": {
                    "status": "accepted",
                    "artifact_ids_before": [],
                    "dispatched_at": None,
                    "accepted_at": now,
                },
                "remote": {
                    "artifact_id": "bad-artifact-1",
                    "status": "pending",
                    "status_origin": "caller_verified",
                    "observed_at": now,
                    "error": None,
                    "error_code": None,
                },
                "finalize": p.new_finalize_state(),
                "errors": [],
            }
        )
        episode["active_attempt_id"] = bad_attempt_id

    store.update(_inject_accepted_never_promoted_candidate)

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


async def test_series_unconfirmed_429_on_a_later_episode_keeps_prior_results(
    fake_client, tmp_path
):
    """第 2 集 dispatch 撞到 0.8.3 標成 `unconfirmed` 的 429:failover 把它標成
    acceptance_unknown 原樣拋。`podcast_series` 的 `_run_episode` handler 對
    「不是 not_accepted」曾經 bare raise —— 第 1 集已完成的 run_results 整份丟掉(F-4 形狀)。
    要回結構化停點、指向 reconcile。"""
    from notebooklm._idempotency import mark_unconfirmed
    from notebooklm.exceptions import RateLimitError

    runtime.set_clients([("a@x", fake_client)])
    ok_generate = fake_client.artifacts.generate_audio
    calls = {"n": 0}

    async def second_is_throttled(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise mark_unconfirmed(RateLimitError("429 Too Many Requests"))
        return await ok_generate(*args, **kwargs)

    fake_client.artifacts.generate_audio = second_is_throttled

    out = await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "1"}, {"title": "實戰篇", "brief": "2"}],
        output_dir=str(tmp_path),
        start=1,
    )

    assert out["complete"] is False
    assert len(out["episodes"]) == 1, "第 1 集的結果不得因第 2 集的受理不明而丟掉"
    assert out["observed_state"] == "acceptance_unknown"
    assert out["safe_next_action"] == p.ACTION_RECONCILE
