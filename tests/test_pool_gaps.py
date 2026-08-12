"""v0.8.1 真實驗收(2026-08-09,stg 三個免費帳號)看到、而現有 fake 測不到的四件事。

現有 `tests/test_quota_failover.py` 已經覆蓋四個命題的主線,這裡**只補缺口**:

1. pool 是 **3 個**帳號、全部耗盡時,同一個 attempt 的 `errors[]` 長什麼樣
   —— 既有測試只用 2 個帳號,而且不驗 errors[] 的順序與形狀。
2. **`podcast_episode`** 原樣重呼那條重送路徑也要 failover
   —— 既有的重送測試只走 `podcast_series`。
3. **輪替跨「工具呼叫」持續**(同一 process、不重裝 pool)
   —— 既有測試每一輪都重呼 `set_clients`,那會把作用位置重設回第一格,
   等於每次都在模擬 process 重啟,永遠測不到「第二次呼叫從 B 開始」。
4. **failover 之後的下載用的是換過去那個帳號的憑證**(F-1 ∩ failover)
   —— 既有的 env 測試只驗 pool 層的 rotate,沒有驗「一集撞到配額換帳號後,
   finalize 的下載是以新帳號的身分發出」。實跑驗不到這一項:自動分享讓 pool
   全員都看得到 notebook,錯的身分照樣下載成功。
"""
import json
import os

import pytest
from conftest import FakeClient
from notebooklm.exceptions import RateLimitError

from notebooklm_mcp import runtime
from notebooklm_mcp import tools_podcast as p

# repo 測試自己的 helper:讓前 N 次 generate_audio 以配額拒絕收場,
# 並記下每次「當下作用中的帳號」。
from test_quota_failover import _flaky_generate


def _episode(manifest_path, episode_n=1):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return next(e for e in data["episodes"] if e["episode"] == episode_n)


async def _run(tmp_path, manifest_path, episode_n=1, title="心法篇"):
    return await p.podcast_episode(
        "nb-1",
        episode_n=episode_n,
        title=title,
        brief=f"第 {episode_n} 集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )


async def test_three_accounts_exhausted_leaves_two_failovers_in_one_attempt(
    fake_client, tmp_path
):
    """三個帳號全部耗盡 → 同一個 attempt 裡兩筆 failover + 一筆 dispatch。

    v0.8.0 的驗收結論寫「同一 attempt 不可能有兩筆 failover」,那個結論的前提是
    **中間有一次成功**(受理之後就不再 rotate)。全員耗盡時 while 迴圈會一路 rotate
    到底,兩筆 failover 落在同一個 attempt —— 2026-08-09 在 stg 實測到的就是這個形狀
    (A→B 1.30s、B→C 1.54s、最後 dispatch not_accepted,全程 4.32 秒)。

    只有 2 個帳號的測試永遠看不到「第二筆 failover 的 from_account 是第一筆的
    to_account」這個鏈,而那正是「不回頭撞舊帳號」的證據。
    """
    runtime.set_clients(
        [("a@x", fake_client), ("b@x", fake_client), ("c@x", fake_client)]
    )
    calls: list = []
    _flaky_generate(fake_client, calls, fail_first_n=99)
    manifest_path = tmp_path / "series_manifest.json"

    with pytest.raises(RateLimitError):
        await _run(tmp_path, manifest_path)

    assert calls == ["a@x", "b@x", "c@x"], "每個帳號各試一次,不重複、不回頭"

    attempt = _episode(manifest_path)["attempts"][0]
    assert attempt["dispatch"]["status"] == "not_accepted"
    assert attempt["dispatch"]["account"] == "c@x", "記的是**最後實際送出**的那個帳號"

    phases = [e["phase"] for e in attempt["errors"]]
    assert phases == ["dispatch_failover", "dispatch_failover", "dispatch"], (
        "兩筆 failover + 一筆終態,順序就是實測的時間序"
    )
    hops = [
        (e["from_account"], e["to_account"])
        for e in attempt["errors"]
        if e["phase"] == "dispatch_failover"
    ]
    assert hops == [("a@x", "b@x"), ("b@x", "c@x")], "鏈式接龍,不是每次都從 a 重來"


async def test_podcast_episode_resend_rotates_too(fake_client, tmp_path):
    """`podcast_episode` 原樣重呼也要 failover(不只 `podcast_series`)。

    工具自己在配額拒絕時給的續跑指引就是「用**完全相同的參數**重呼 podcast_episode」,
    所以這條路跟 series 的重送路徑一樣是「pool 最該起作用的時刻」。既有的 F-4 測試
    只證明了 series 那一支。2026-08-09 實測:分享修好之後原樣重呼,第二次從 B 開始
    → B 被拒 → rotate 到 C,沿用同一個 attempt。
    """
    manifest_path = tmp_path / "series_manifest.json"

    # 第一輪:兩個帳號都耗盡 → not_accepted,attempt 留在 manifest。
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls: list = []
    _flaky_generate(fake_client, calls, fail_first_n=99)
    with pytest.raises(RateLimitError):
        await _run(tmp_path, manifest_path)
    first_attempt_id = _episode(manifest_path)["attempts"][0]["attempt_id"]

    # 第二輪(隔天、新 process):完全相同的參數重呼。
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls.clear()
    _flaky_generate(fake_client, calls, fail_first_n=1)
    await _run(tmp_path, manifest_path)

    assert calls == ["a@x", "b@x"], "重送路徑沒 failover 的話 pool 對重試無效"
    attempts = _episode(manifest_path)["attempts"]
    assert len(attempts) == 1, "沿用同一個 attempt,不得新建、不得 supersede"
    assert attempts[0]["attempt_id"] == first_attempt_id
    assert attempts[0]["dispatch"]["account"] == "b@x", "記這次實際送出的,不是上次的殘值"


async def test_rotation_persists_across_separate_tool_calls(fake_client, tmp_path):
    """輪替是 **per-process 持續**,不是 per-call 重設。

    ADR-0010:今天耗盡的帳號今天就是耗盡了,每集都先撞一次等於每集多燒一趟 RPC、
    多一筆假的 failover 紀錄。既有測試每輪都重呼 `set_clients`(等於模擬 process
    重啟),把作用位置重設回第一格,所以測不到這件事。

    這裡**只裝一次 pool**,連續呼叫兩次工具 —— 第二集必須直接從 b@x 出發。
    """
    runtime.set_clients(
        [("a@x", fake_client), ("b@x", fake_client), ("c@x", fake_client)]
    )
    calls: list = []
    _flaky_generate(fake_client, calls, fail_first_n=1)
    manifest_path = tmp_path / "series_manifest.json"

    await _run(tmp_path, manifest_path, episode_n=1, title="心法篇")
    await _run(tmp_path, manifest_path, episode_n=2, title="實作篇")

    assert calls == ["a@x", "b@x", "b@x"], "第二集不該回頭再撞一次 a@x"
    assert _episode(manifest_path, 1)["attempts"][0]["dispatch"]["account"] == "b@x"
    ep2 = _episode(manifest_path, 2)["attempts"][0]
    assert ep2["dispatch"]["account"] == "b@x"
    assert not [e for e in ep2["errors"] if e["phase"] == "dispatch_failover"], (
        "第二集根本不該產生 failover 紀錄 —— 它一開始就在 b@x"
    )


async def test_download_after_failover_runs_on_the_new_accounts_client(
    fake_client, tmp_path, monkeypatch
):
    """換帳號之後,finalize 的下載必須由**新帳號的 client** 發出(F-1 ∩ failover)。

    v0.8.0 的 bug:身分放在 process 全域 `NOTEBOOKLM_AUTH_JSON`(SDK 的媒體下載在
    下載當下重讀它),pool 建完停在最後一個槽位 → 整個 server 生命週期所有下載都以
    那個帳號發出,而症狀十幾分鐘後才在 finalize 浮現。當時的修法是「set_clients /
    rotate_client 同步 env」,測試也就只驗 env。

    v0.9.0 換成 client 自帶 storage_path(`from_storage(path=…)`,SDK 只有
    `_storage_path is None` 才回頭讀 env)。**保證因此變強而不是變弱**:env 只證明
    「當下那一刻的全域變數是對的」,這裡證明的是**實際收到這通 download 的物件**。
    非做不可的理由是並行:MCP 對每則 message `tg.start_soon`,一個全域槽沒辦法同時
    是兩個值 —— EP05 正在 finalize(client 已 pin 住)、EP06 撞配額 rotate,
    EP05 的下載就以別人的身分發出。同步 env 修不掉這個,因為它本來就是同一個變數。

    所以兩個槽位必須是**不同的 fake 物件**:pool 測試長期以來每格塞同一個 fake,
    「這通 RPC 實際發給誰」這一維在測試裡根本不存在。

    **實跑驗不到這一項**:v0.8.1 的自動分享讓 pool 全員都看得到 notebook,
    身分錯了照樣下載成功。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "CRED_ORIGINAL")
    account_b = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", account_b)])

    calls: list = []
    _flaky_generate(fake_client, calls, fail_first_n=99)  # a@x 一律配額耗盡
    _flaky_generate(account_b, calls, fail_first_n=0)  # b@x 照常受理
    manifest_path = tmp_path / "series_manifest.json"

    await _run(tmp_path, manifest_path)

    assert calls == ["a@x", "b@x"], "前提:這一集確實 failover 到 b@x 才生成成功"

    def downloads(client):
        return [c for c in client.artifacts.calls if c[0] == "download"]

    assert downloads(account_b), "下載必須由實際生成那一集的 client 發出"
    assert not downloads(fake_client), (
        "停在 a@x 就是「B 生的、A 抓的」那種事後查不回來的稽核失真"
    )
    # 身分不再是 process 全域狀態 —— pool 從頭到尾不碰這個 env。
    assert os.environ["NOTEBOOKLM_AUTH_JSON"] == "CRED_ORIGINAL"


async def test_standalone_finalize_keeps_dispatch_client(fake_client, tmp_path):
    account_b = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", account_b)])
    original = fake_client.artifacts.generate_audio

    async def generate_then_rotate(*args, **kwargs):
        status = await original(*args, **kwargs)
        runtime.rotate_client()
        return status

    fake_client.artifacts.generate_audio = generate_then_rotate

    await p.podcast_episode(
        "nb-1", episode_n=1, title="心法篇", brief="b", output_dir=str(tmp_path)
    )

    assert account_b.artifacts.calls == []
    assert any(call[0] == "download" for call in fake_client.artifacts.calls)


async def test_episode_dispatch_uses_the_client_that_passed_auth_probe(fake_client, tmp_path):
    account_b = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", account_b)])
    original = fake_client.notebooks.list

    async def probe_then_rotate():
        notebooks = await original()
        runtime.rotate_client()
        return notebooks

    fake_client.notebooks.list = probe_then_rotate

    await p.podcast_episode(
        "nb-1", episode_n=1, title="心法篇", brief="b", output_dir=str(tmp_path)
    )

    assert any(call[0] == "generate_audio" for call in fake_client.artifacts.calls)
    assert account_b.artifacts.calls == []


async def test_series_dispatch_uses_the_client_that_passed_per_episode_probe(
    fake_client, tmp_path
):
    account_b = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", account_b)])
    original = fake_client.notebooks.list
    probes = 0

    async def rotate_during_per_episode_probe():
        nonlocal probes
        notebooks = await original()
        probes += 1
        if probes == 2:
            runtime.rotate_client()
        return notebooks

    fake_client.notebooks.list = rotate_during_per_episode_probe

    await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "b"}],
        output_dir=str(tmp_path),
    )

    assert probes >= 2
    assert any(call[0] == "generate_audio" for call in fake_client.artifacts.calls)
    assert account_b.artifacts.calls == []


async def test_series_ignores_an_external_rotation_between_episodes(fake_client, tmp_path):
    """別的 request 推進 global pool，不得讓同一個 series 的下一集偷換帳號。"""
    account_b = FakeClient()
    account_b.artifacts._generate_count = 100
    runtime.set_clients([("a@x", fake_client), ("b@x", account_b)])
    generated = 0

    def rotate_during_first_episode():
        nonlocal generated
        generated += 1
        if generated == 1:
            runtime.rotate_client()

    fake_client.artifacts.on_generate_audio = rotate_during_first_episode

    await p.podcast_series(
        "nb-1",
        episodes=[
            {"title": "心法篇", "brief": "1"},
            {"title": "實戰篇", "brief": "2"},
        ],
        output_dir=str(tmp_path),
    )

    assert generated == 2
    assert not [call for call in account_b.artifacts.calls if call[0] == "generate_audio"]


async def test_series_carries_its_own_failover_client_to_the_next_episode(fake_client, tmp_path):
    """A 被本 series 的配額 failover 換成 B 後，即使 global 又被推到 C，EP2 仍用 B。"""
    account_b = FakeClient()
    account_c = FakeClient()
    account_c.artifacts._generate_count = 200
    runtime.set_clients(
        [("a@x", fake_client), ("b@x", account_b), ("c@x", account_c)]
    )
    calls: list[str] = []

    async def refuse_a(*_args, **_kwargs):
        calls.append("a@x")
        raise RateLimitError("每日配額已用盡")

    original_b = account_b.artifacts.generate_audio
    b_calls = 0

    async def generate_b_then_external_rotate(*args, **kwargs):
        nonlocal b_calls
        calls.append("b@x")
        b_calls += 1
        status = await original_b(*args, **kwargs)
        if b_calls == 1:
            assert runtime.rotate_client() == "c@x"
        return status

    fake_client.artifacts.generate_audio = refuse_a
    account_b.artifacts.generate_audio = generate_b_then_external_rotate

    await p.podcast_series(
        "nb-1",
        episodes=[
            {"title": "心法篇", "brief": "1"},
            {"title": "實戰篇", "brief": "2"},
        ],
        output_dir=str(tmp_path),
    )

    assert calls == ["a@x", "b@x", "b@x"]
    assert not [call for call in account_c.artifacts.calls if call[0] == "generate_audio"]


async def test_resume_pins_client_after_auth_probe(fake_client, tmp_path):
    account_b = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", account_b)])
    original = fake_client.notebooks.list

    async def probe_then_rotate():
        notebooks = await original()
        runtime.rotate_client()
        return notebooks

    fake_client.notebooks.list = probe_then_rotate

    await p.podcast_episode_resume(
        "nb-1", 1, "心法篇", "art-1", str(tmp_path)
    )

    assert account_b.artifacts.calls == []
    assert any(call[0] == "download" for call in fake_client.artifacts.calls)


async def test_failover_credits_the_account_that_actually_dispatched(fake_client, tmp_path):
    """並行的另一個呼叫在 `generate` 的 await 期間 rotate 掉全域游標時,failover 紀錄的
    `from_account` 必須是**這次實際送出**的那個帳號。

    `generate` 是一趟真 RPC(數秒到數十秒),而 MCP 是並行的(每則 message 一個 task,
    `notebooklm_mcp` 全域零鎖)。舊碼在那個 await **之後**才用 `runtime.active_account()`
    讀 `from_account`,讀到的是「此刻剛好輪到誰」而不是「這次是誰被拒的」——稽核紀錄
    於是指認一個根本沒參與這次 dispatch 的帳號,而且兩個帳號後來都成功,事後無從發現
    (ADR-0010 §Transparency:manifest 是唯一的稽核憑據)。

    修法是呼叫端 `runtime.snapshot()` 一次取好 (label, client) 往下傳,failover 換帳號時
    兩者一起換 —— 全程不回頭讀全域。
    """
    account_b, account_c = FakeClient(), FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", account_b), ("c@x", account_c)])
    manifest_path = tmp_path / "series_manifest.json"

    async def rotated_away_mid_flight(*args, **kwargs):
        # 模擬「另一個工具呼叫在這趟 RPC 期間撞到配額並換了帳號」:全域游標被推到 b@x,
        # 而**這次** dispatch 用的自始至終是 a@x 的 client。
        runtime.rotate_client()
        raise RateLimitError("每日配額已用盡")

    fake_client.artifacts.generate_audio = rotated_away_mid_flight

    await _run(tmp_path, manifest_path)

    attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0]["attempts"][0]
    failovers = [e for e in attempt["errors"] if e["phase"] == "dispatch_failover"]
    assert len(failovers) == 1, "只有 a@x 被拒,應該只有一筆 failover"
    assert failovers[0]["from_account"] == "a@x", (
        "被拒的是 a@x;記成 b@x 表示讀的是『此刻輪到誰』而不是『這次是誰送的』"
    )
    assert failovers[0]["to_account"] == "c@x"
    assert attempt["dispatch"]["account"] == "c@x", "最終成功送出的帳號"
    # 生成必須真的由 c@x 的 client 發出——不是靠全域碰巧對上。
    assert any(c[0] == "generate_audio" for c in account_c.artifacts.calls)
    assert not account_b.artifacts.calls, "b@x 只是被並行呼叫推到的游標位置,不該參與這次 dispatch"


async def test_dispatch_and_finalize_both_ride_the_snapshotted_client(fake_client, tmp_path):
    """並行 rotate 落在 **snapshot 之後、dispatch 之前**那個縫時,這一集的 dispatch
    與 finalize 都必須留在快照到的那個 client 上。

    這是整個「身分跟著 client 走」重構的核心斷言,而它需要**這個**時序才驗得到:
    `runtime.snapshot()` 之後到 `generate` 之間隔著 baseline `artifacts.list` 與
    `_claim_prepared_dispatch`,並行的另一個工具呼叫正是在這裡把全域游標換掉。
    (在 `generate_audio` **內部**才 rotate 的測試驗不到這件事——那時 client 已經
    傳進去了,`generate(runtime.get_client())` 這個突變照樣會綠。)

    三個突變都必須讓這條紅:
    ①`generate(client)` → `generate(runtime.get_client())`
    ②`account=dispatch_account` → `account=runtime.active_account()`
    ③finalize 前補回 `client = runtime.get_client()`
    """
    account_b = FakeClient()
    runtime.set_clients([("a@x", fake_client), ("b@x", account_b)])
    manifest_path = tmp_path / "series_manifest.json"

    original_list = fake_client.artifacts.list
    rotated: list[str] = []

    async def list_then_rotate(*args, **kwargs):
        # 模擬並行呼叫在 baseline 這個 await 裡撞到配額、把全域游標換到 b@x。
        if not rotated:
            rotated.append(runtime.rotate_client())
        return await original_list(*args, **kwargs)

    fake_client.artifacts.list = list_then_rotate

    await _run(tmp_path, manifest_path)

    assert rotated == ["b@x"], "前提:全域游標確實在 dispatch 之前就被換走了"
    attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0]["attempts"][0]
    assert attempt["dispatch"]["account"] == "a@x", (
        "記帳要記快照到的帳號;記成 b@x 表示 claim 回頭讀了全域"
    )
    kinds = [c[0] for c in account_b.artifacts.calls]
    assert not kinds, (
        f"b@x 只是游標被推到的位置,這一集不該碰它——實際收到 {kinds}。"
        "generate 落在它身上 = dispatch 讀了全域;wait/download/rename 落在它身上 = "
        "finalize 讀了全域(而 finalize 是數十分鐘的長窗口,風險幾乎全在那一半)"
    )
    assert any(c[0] == "generate_audio" for c in fake_client.artifacts.calls)
    assert any(c[0] == "download" for c in fake_client.artifacts.calls), (
        "finalize 的下載也要留在同一個 client 上"
    )
