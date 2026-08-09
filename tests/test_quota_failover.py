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
    """
    from notebooklm.exceptions import RateLimitError

    original = fake_client.artifacts.generate_audio

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
