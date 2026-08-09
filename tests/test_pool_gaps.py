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


async def test_download_after_failover_uses_the_new_accounts_credential(
    fake_client, tmp_path, monkeypatch
):
    """換帳號之後,finalize 的下載必須以**新帳號**的身分發出(F-1 ∩ failover)。

    SDK 的媒體下載在下載當下重讀 `NOTEBOOKLM_AUTH_JSON`,不是用 client 自己的 session。
    v0.8.0 的 bug 是 env 停在 pool 最後一個槽位;修法是 `set_clients`/`rotate_client`
    都同步 env。既有的 env 測試只驗 pool 層 rotate,沒有驗「一集真的撞到配額換帳號後,
    十幾分鐘後的下載用的是誰」——而那正是 v0.8.0 症狀出現的地方。

    **實跑驗不到這一項**:v0.8.1 的自動分享讓 pool 全員都看得到 notebook,
    身分錯了照樣下載成功。只有在這裡才能把 artifact 屬於誰和 env 是誰分開來看。
    """
    monkeypatch.setenv(runtime.AUTH_JSON_ENV, "CRED_ORIGINAL")
    runtime.set_clients(
        [("a@x", fake_client, "CRED_A"), ("b@x", fake_client, "CRED_B")]
    )
    assert os.environ[runtime.AUTH_JSON_ENV] == "CRED_A", "裝完 pool 就該是第一格"

    seen: list[str] = []
    original_download = fake_client.artifacts.download_audio

    async def recording_download(*args, **kwargs):
        seen.append(os.environ[runtime.AUTH_JSON_ENV])
        return await original_download(*args, **kwargs)

    fake_client.artifacts.download_audio = recording_download

    calls: list = []
    _flaky_generate(fake_client, calls, fail_first_n=1)
    manifest_path = tmp_path / "series_manifest.json"

    await _run(tmp_path, manifest_path)

    assert calls == ["a@x", "b@x"], "前提:這一集確實 failover 到 b@x 才生成成功"
    assert seen == ["CRED_B"], (
        "下載身分必須跟著作用中帳號走 —— 停在 CRED_A 就是「A 生的、B 抓的」那種"
        "事後查不回來的稽核失真"
    )
