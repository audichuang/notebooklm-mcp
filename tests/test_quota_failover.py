"""配額 failover:被拒就換下一個帳號,原地重送同一個 attempt(ADR-0010)。

實測(2026-08-08)配額耗盡是**同步拒絕**——1.35 秒、`dispatch.status=not_accepted`、
`remote.artifact_id=null`,落在 `_REFUSED_WITHOUT_DISPATCH` 這條「契約保證沒建出 task」
的路上。所以換帳號重送是冪等的:同一個 attempt_id、不 supersede、不新建 attempt。

**已 dispatch 之後的失敗一律不換帳號**(那要 retract + 取代版,自動做等於讓 MCP 在背後
改寫 manifest 的因果紀錄,ADR-0009 禁止)。
"""
import json

import pytest

from notebooklm_mcp import runtime
from notebooklm_mcp import tools_podcast as p


def _flaky_generate(fake_client, calls, fail_first_n):
    """讓前 N 次 generate_audio 以配額拒絕收場,其餘照常。

    每次呼叫記下**當下作用中的帳號**——這是「第二次真的換了帳號發出去」的直接證據,
    比檢查 manifest 欄位更難造假。

    可重入(同一個 fake 上套第二次不會層層包住前一個 wrapper)。
    """
    from notebooklm.exceptions import RateLimitError

    original = getattr(fake_client.artifacts, "_pristine_generate", None)
    if original is None:
        original = fake_client.artifacts.generate_audio
        fake_client.artifacts._pristine_generate = original

    async def flaky(*args, **kwargs):
        calls.append(runtime.active_account())
        if len(calls) <= fail_first_n:
            raise RateLimitError("每日配額已用盡")
        return await original(*args, **kwargs)

    fake_client.artifacts.generate_audio = flaky


def _attempts(manifest_path):
    return json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0]["attempts"]


async def test_quota_refusal_rotates_and_reuses_the_same_attempt(fake_client, tmp_path):
    """帳號 A 被拒 → 換 B 重送 → 成功。全程一個 attempt。"""
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls: list = []
    _flaky_generate(fake_client, calls, fail_first_n=1)
    manifest_path = tmp_path / "series_manifest.json"

    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    # 第二次 dispatch 是用另一個帳號發的。
    assert calls == ["a@x", "b@x"]

    attempts = _attempts(manifest_path)
    assert len(attempts) == 1, "failover 不得新建 attempt——那會多燒一次配額紀錄"
    attempt = attempts[0]
    assert attempt["dispatch"]["status"] != "not_accepted"
    # 稽核:對 client 透明可以,對紀錄不行。「哪個帳號生的」必須答得出來。
    assert attempt["dispatch"]["account"] == "b@x"
    failovers = [e for e in attempt["errors"] if e["phase"] == "dispatch_failover"]
    assert len(failovers) == 1
    assert failovers[0]["from_account"] == "a@x"
    assert failovers[0]["to_account"] == "b@x"
    assert "每日配額已用盡" in failovers[0]["message"]


async def test_all_accounts_exhausted_keeps_the_legacy_not_accepted_terminal(
    fake_client, tmp_path
):
    """全部帳號都被拒 → 完全維持既有行為(not_accepted + raise)。

    series 圈靠 `dispatch.status == "not_accepted"` 決定要不要回結構化安全停點,
    所以耗盡的終態一個字都不能變,否則整季會把例外拋給呼叫端。
    """
    from notebooklm.exceptions import RateLimitError

    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls: list = []
    _flaky_generate(fake_client, calls, fail_first_n=99)
    manifest_path = tmp_path / "series_manifest.json"

    with pytest.raises(RateLimitError, match="每日配額已用盡"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    assert calls == ["a@x", "b@x"], "每個帳號都要試過一次,而且只試一次"
    attempt = _attempts(manifest_path)[0]
    assert attempt["dispatch"]["status"] == "not_accepted"
    assert attempt["remote"]["artifact_id"] is None
    assert "每日配額已用盡" in attempt["remote"]["error"]
    assert attempt["remote"]["error_code"] == "RateLimitError"


async def test_generic_failure_never_rotates(fake_client, tmp_path):
    """反向鎖:只有契約保證「沒建出 task」的那兩種才准換帳號。

    RPCError 可能發生在伺服器已經受理之後——換帳號重送會變成重複 artifact
    + 重燒配額,正是 `_REFUSED_WITHOUT_DISPATCH` 刻意不長大的理由。
    """
    from notebooklm.exceptions import RPCError

    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls: list = []
    original = fake_client.artifacts.generate_audio

    async def boom(*args, **kwargs):
        calls.append(runtime.active_account())
        raise RPCError("伺服器 500")

    fake_client.artifacts.generate_audio = boom
    manifest_path = tmp_path / "series_manifest.json"

    with pytest.raises(RPCError):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    assert calls == ["a@x"], "受理結果不明時不得換帳號重送"
    attempt = _attempts(manifest_path)[0]
    assert attempt["dispatch"]["status"] == "acceptance_unknown"
    assert not [e for e in attempt["errors"] if e["phase"] == "dispatch_failover"]
    del original


async def test_status_shaped_refusal_also_rotates(fake_client, tmp_path):
    """0.7.x 的形狀(回 `task_id=""` 而不是 raise)也要 failover。

    這是本 repo 反覆出事的「補一半」:同一個語意有兩條路徑進來,只補 raise 那條,
    另一條照樣把整條線停住。兩條都由 `ensure_started` 匯流成 not_accepted。
    """
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls: list = []
    original = fake_client.artifacts.generate_audio

    async def refuse_by_status(*args, **kwargs):
        calls.append(runtime.active_account())
        if len(calls) == 1:
            return type("S", (), {"task_id": "", "is_failed": True, "status": "failed",
                                  "error": "quota exhausted"})()
        return await original(*args, **kwargs)

    fake_client.artifacts.generate_audio = refuse_by_status
    manifest_path = tmp_path / "series_manifest.json"

    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    assert calls == ["a@x", "b@x"]
    attempts = _attempts(manifest_path)
    assert len(attempts) == 1
    assert attempts[0]["dispatch"]["account"] == "b@x"


async def test_rotation_persists_across_the_rest_of_a_series(fake_client, tmp_path):
    """EP01 因配額換到 B 之後,EP02 直接從 B 開始,不回頭再撞一次 A。

    輪替狀態刻意是 **per-process 持續**而不是 per-attempt 重設:今天耗盡的帳號
    今天就是耗盡了,每集都先去撞一次 A 等於每集多燒一趟 RPC、多一筆假的 failover 紀錄。
    """
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls: list = []
    _flaky_generate(fake_client, calls, fail_first_n=1)

    await p.podcast_series(
        "nb-1",
        episodes=[{"title": "心法篇", "brief": "1"}, {"title": "實戰篇", "brief": "2"}],
        output_dir=str(tmp_path),
        start=1,
    )

    # a 被拒一次 → b 成功;第二集直接就是 b。
    assert calls == ["a@x", "b@x", "b@x"]
    episodes = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )["episodes"]
    assert [e["attempts"][-1]["dispatch"]["account"] for e in episodes] == ["b@x", "b@x"]


async def test_resend_path_rotates_and_records_the_account_too(fake_client, tmp_path):
    """**失敗後重跑**那條路徑(v0.8.0 驗收 F-4)。

    `podcast_series` 有兩條 dispatch 路徑:全新一集走 `_run_episode`,而「已有 prepared
    attempt(重送/supersede)」是 `podcast_series` 自己 inline 送出的。v0.8.0 只補了
    前者 —— 於是配額耗盡後隔天原樣重呼(**工具自己給的 `safe_next_action`**)不會換帳號,
    pool 對「重試」這條最需要它的路完全無效,而 `dispatch.account` 也是 null。

    既有的 series 測試跑的是兩集**全新生成**,兩集都走路徑 1,所以斷言會過 —— 這條
    測試專門對準那個盲區。
    """
    manifest_path = tmp_path / "series_manifest.json"
    episodes = [{"title": "心法篇", "brief": "1"}]

    # 第一輪:兩個帳號都被拒 → 安全停點,留下一個 not_accepted 的 attempt。
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls: list = []
    _flaky_generate(fake_client, calls, fail_first_n=99)
    stopped = await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path), start=1
    )
    assert stopped["observed_state"] == "not_accepted"
    assert calls == ["a@x", "b@x"]

    # 第二輪(隔天配額回來):原樣重呼。attempt 被 rearm 成 prepared → 走**路徑 2**。
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls.clear()
    _flaky_generate(fake_client, calls, fail_first_n=1)
    await p.podcast_series(
        "nb-1", episodes=episodes, output_dir=str(tmp_path), start=1
    )

    assert calls == ["a@x", "b@x"], "重送路徑也必須 failover,否則 pool 對重試無效"
    attempts = _attempts(manifest_path)
    assert len(attempts) == 1, "沿用同一個 attempt 重送,不得新建"
    dispatch = attempts[0]["dispatch"]
    # 過期值比缺漏危險:缺漏看得出來,錯的帳號看不出來。
    assert dispatch["account"] == "b@x", "重送要記下**這次**實際送出的帳號"
    failovers = [
        e for e in attempts[0]["errors"] if e["phase"] == "dispatch_failover"
    ]
    assert failovers[-1]["from_account"] == "a@x"
    assert failovers[-1]["to_account"] == "b@x"


async def test_permission_denied_is_not_acceptance_unknown(fake_client, tmp_path):
    """換到的帳號看不到那個 notebook(v0.8.0 驗收 F-2)。

    pool 建的 notebook 只屬於當下作用中的帳號;failover 換帳號後拿新帳號對**同一個
    notebook_id** 送出,新帳號可能根本看不到它。permission denied 走的是泛用 except
    → `acceptance_unknown` → 「先對帳、禁止直接重生」,把一個**確定沒發出去**的請求
    叫去跑一次註定撈不到東西的 reconcile —— 正是 v0.7.1 那類死鎖的形狀。

    **不繼續 rotate**:權限是設定問題不是暫時性問題,一個一個試過去只會掩蓋根因,
    還每次多燒一輪 RPC。
    """
    from notebooklm.exceptions import ClientError

    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls: list = []

    async def denied(*args, **kwargs):
        calls.append(runtime.active_account())
        raise ClientError("permission denied", rpc_code=7)

    fake_client.artifacts.generate_audio = denied
    manifest_path = tmp_path / "series_manifest.json"

    with pytest.raises(p.NotebookAccessDenied, match="分享"):
        await p.podcast_episode(
            "nb-1", episode_n=1, title="心法篇", brief="第一集",
            output_dir=str(tmp_path), manifest_path=str(manifest_path),
        )

    assert calls == ["a@x"], "權限問題不該一個一個帳號試過去"
    attempt = _attempts(manifest_path)[0]
    assert attempt["dispatch"]["status"] == "not_accepted", (
        "確定沒建出 task,不是受理不明"
    )
    assert "permission denied" in attempt["remote"]["error"]


async def test_permission_denied_in_series_returns_a_safe_stop(fake_client, tmp_path):
    """series 路徑上同一件事要回結構化安全停點,而不是 acceptance_unknown/reconcile。"""
    from notebooklm.exceptions import ClientError

    runtime.set_clients([("a@x", fake_client)])

    async def denied(*args, **kwargs):
        raise ClientError("permission denied", rpc_code=7)

    fake_client.artifacts.generate_audio = denied

    out = await p.podcast_series(
        "nb-1", episodes=[{"title": "心法篇", "brief": "1"}],
        output_dir=str(tmp_path), start=1,
    )
    assert out["observed_state"] == "not_accepted"
    assert out["safe_next_action"] != "podcast_episode_reconcile"


async def test_reset_for_resend_clears_the_stale_account(fake_client, tmp_path):
    """attempt 被 rearm 回 prepared 時,舊的 `account` 必須清掉。

    留著會變成**過期值**:manifest 說 A 生的,實際是 B 送出的。缺漏至少看得出來,
    錯的值看不出來。
    """
    attempt = {
        "dispatch": {
            "status": "not_accepted",
            "account": "a@x",
            "artifact_ids_before": ["x"],
            "dispatched_at": "2026-08-09T00:00:00+00:00",
            "accepted_at": None,
        },
        "remote": {"status": "failed", "error": "boom", "error_code": "RateLimitError",
                   "artifact_id": None, "observed_at": "2026-08-09T00:00:00+00:00"},
        "errors": [],
    }
    p._reset_attempt_for_resend(attempt)
    assert "account" not in attempt["dispatch"], "過期的帳號要 pop 掉,不是留著"


async def test_single_account_records_the_account_without_any_failover(
    fake_client, tmp_path
):
    """單帳號(現行所有機器的樣子):照舊,但 dispatch 仍要記下是誰生的。"""
    runtime.set_clients([("solo@x", fake_client)])
    manifest_path = tmp_path / "series_manifest.json"

    await p.podcast_episode(
        "nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    attempt = _attempts(manifest_path)[0]
    assert attempt["dispatch"]["account"] == "solo@x"
    assert not [e for e in attempt["errors"] if e["phase"] == "dispatch_failover"]
