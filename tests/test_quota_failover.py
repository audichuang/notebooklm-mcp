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


async def test_has_id_but_failed_is_not_zero_side_effect_so_it_does_not_rotate(
    fake_client, tmp_path
):
    """SDK 契約上可達、但更危險的一種拒絕形狀:有 id 卻 `is_failed=True`。

    `_artifact/generation.py:586-590` 明寫「有 artifact_id 就回
    GenerationStatus(task_id=artifact_id, status=...)」,而 `_ARTIFACT_STATUS_MAP`
    含 FAILED → "failed",所以「有 id + failed」這個形狀在上游可達,不是 0.7.x 的
    `task_id=""` 那種零副作用拒絕。伺服器已經建出 task 了——rotate 重送會產生第二個
    artifact,manifest 卻只綁得到新帳號那個,第一個不在 artifact_ids_before 基線裡,
    日後 reconcile 會撞成 reconciliation_ambiguous,還多燒一次配額。
    """
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    calls: list = []
    original = fake_client.artifacts.generate_audio

    async def has_id_but_failed(*args, **kwargs):
        calls.append(runtime.active_account())
        return type(
            "S",
            (),
            {
                "task_id": "task-ghost-1",
                "is_failed": True,
                "status": "failed",
                "error": "upstream refused after building the task",
            },
        )()

    fake_client.artifacts.generate_audio = has_id_but_failed
    manifest_path = tmp_path / "series_manifest.json"

    with pytest.raises(RuntimeError, match="upstream refused after building the task"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    assert calls == ["a@x"], "有 id 卻 failed 不是零副作用——不准換帳號重送"
    attempt = _attempts(manifest_path)[0]
    assert attempt["dispatch"]["status"] == "acceptance_unknown", (
        "標成 not_accepted 會讓呼叫端誤以為可以安全重送,實際上伺服器可能已經建出 task"
    )
    del original


async def test_generic_runtime_error_still_gets_the_reconcile_hint(
    fake_client, tmp_path
):
    """普通 `RuntimeError`(非 ensure_started 判定的 not_accepted)一樣要拿到續跑指引。

    `_dispatch_audio_with_failover` 的泛用 except 分支把它標成 acceptance_unknown、
    原樣重拋;但它剛好也是 `RuntimeError` 型別,曾被 `_run_episode` 排在泛用分支之前的
    `except RuntimeError: raise` 攔住,續跑指引(podcast_episode_reconcile)整條蒸發
    ——重構帶進來的回歸,v0.7.2 舊碼走 `except Exception` 才拿得到。
    """
    runtime.set_clients([("solo@x", fake_client)])

    async def boom(*args, **kwargs):
        raise RuntimeError("weird transient failure")

    fake_client.artifacts.generate_audio = boom
    manifest_path = tmp_path / "series_manifest.json"

    with pytest.raises(RuntimeError, match="podcast_episode_reconcile"):
        await p.podcast_episode(
            "nb-1",
            episode_n=1,
            title="心法篇",
            brief="第一集",
            output_dir=str(tmp_path),
            manifest_path=str(manifest_path),
        )

    attempt = _attempts(manifest_path)[0]
    assert attempt["dispatch"]["status"] == "acceptance_unknown"


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

    with pytest.raises(p.NotebookAccessDenied, match="分享") as excinfo:
        await p.podcast_episode(
            "nb-1", episode_n=1, title="心法篇", brief="第一集",
            output_dir=str(tmp_path), manifest_path=str(manifest_path),
        )
    assert "notebook_share_with_pool" in str(excinfo.value), (
        "訊息要指名補分享的正門工具,不能只說『手動補分享』"
    )

    assert calls == ["a@x"], "權限問題不該一個一個帳號試過去"
    attempt = _attempts(manifest_path)[0]
    assert attempt["dispatch"]["status"] == "not_accepted", (
        "確定沒建出 task,不是受理不明"
    )
    assert "permission denied" in attempt["remote"]["error"]
    assert "notebook_share_with_pool" in attempt["remote"]["error"], (
        "manifest 的 remote.error 要帶出補分享的下一步,不是只留原始的 "
        "\"permission denied\"——原本 _mark_not_accepted 收到的是建 "
        "NotebookAccessDenied **之前**的原始 ClientError,指引整條蒸發"
    )


async def test_permission_denied_in_series_returns_a_safe_stop(fake_client, tmp_path):
    """series 路徑上同一件事要回結構化安全停點,而且要能跟『還在等配額』分開來看。

    舊斷言只驗 `observed_state == "not_accepted"` 與
    `safe_next_action != "podcast_episode_reconcile"`——那剛好是「等配額」的安全
    停點也會給出的一模一樣的形狀。呼叫端拿工具自己給的 `safe_next_action`
    (`podcast_series`)原樣重呼,只會撞回同一個沒權限的帳號,而
    `attempt_count`/`superseded_attempt_count`(partial() 唯一的『有沒有在原地打轉』
    依據)兩輪都是 1/0,跟等配額完全同一組數字,看不出自己在原地打轉。修法是給權限
    問題自己的 observed_state,並把帶著 `notebook_share_with_pool` 指引的
    `NotebookAccessDenied` 訊息透過 `error` 欄位帶出來。

    **v0.9.5 起 `safe_next_action` 也跟著分岔。** 原本刻意留 `podcast_series`,理由是
    「白名單只放真工具名,分辨兩者靠 observed_state 與 error 就好」—— 但那讓同一份回傳
    的兩個欄位互相矛盾:`error` 說去補分享,`safe_next_action` 說重呼 series,而 skill
    教呼叫端「拿不準就直接照 safe_next_action 做」。只讀那個欄位的自動化會原地重試同一
    個沒權限的帳號。`notebook_share_with_pool` 本來就是公開 MCP 工具,完全符合白名單的
    意義;v0.9.3 為完全相同的理由加過 `podcast_episode` / `podcast_attempt_retract`。
    """
    from notebooklm.exceptions import ClientError

    runtime.set_clients([("a@x", fake_client)])

    async def denied(*args, **kwargs):
        raise ClientError("permission denied", rpc_code=7)

    fake_client.artifacts.generate_audio = denied

    out = await p.podcast_series(
        "nb-1", episodes=[{"title": "心法篇", "brief": "1"}],
        output_dir=str(tmp_path), start=1,
    )
    assert out["observed_state"] == "notebook_access_denied", (
        "不能跟『等配額』長得一樣——否則原樣重呼會在同一集永遠卡死而看不出來"
    )
    assert out["safe_next_action"] == "notebook_share_with_pool", (
        "只讀 safe_next_action 的自動化會照它做 —— 指回 series 等於叫它重試同一個沒權限的帳號"
    )
    assert "notebook_share_with_pool" in out["error"]


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


async def test_rotate_for_quota_tells_runtime_which_account_was_actually_refused(
    tmp_path, monkeypatch
):
    """**P1**:`_rotate_for_quota` 手上的 `from_account` 是這次 dispatch 實際用過的
    那一個——它自己的 docstring 逐字這樣寫,卻只拿去記帳(`_record_dispatch_failover`),
    沒轉給 `runtime.rotate_client`。冷卻要進的是**真正被拒**的那個槽位,不是「此刻游標
    指到誰」——兩者在並行 dispatch 下可能不是同一個。

    這裡直接單測 `_rotate_for_quota`,不依賴 `runtime.rotate_client` 的實際冷卻邏輯
    (那由另一個修正同步改動 —— 見跨檔案契約),用 monkeypatch 斷言呼叫端**傳了什麼**。
    """
    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        language="en",
        audio_format=None,
        audio_length=None,
    )
    p._claim_prepared_dispatch(store, 1, attempt_id, [], account="a@x")

    captured: dict = {}

    def fake_rotate_client(*, refused=None, skip=frozenset()):
        captured["refused"] = refused
        return "b@x"

    monkeypatch.setattr(runtime, "rotate_client", fake_rotate_client)
    monkeypatch.setattr(runtime, "snapshot", lambda: ("b@x", object()))

    p._rotate_for_quota(store, 1, attempt_id, RuntimeError("quota"), "a@x", {"a@x"})

    assert captured.get("refused") == "a@x", (
        "必須是這次 dispatch 實際用過的帳號(呼叫端傳進來的 from_account),"
        "不是保守預設值 None——那等於退回『冷卻此刻游標指到誰』的舊行為"
    )


async def test_rotate_for_quota_does_not_give_up_when_the_first_scanned_slot_was_tried(
    fake_client, tmp_path, monkeypatch
):
    """**P1**:`runtime.rotate_client` 只回「游標後方第一個不在冷卻中的槽位」,
    它不知道呼叫端的 `tried` 集合——如果那個槽位剛好試過,`_rotate_for_quota`
    舊版就直接放棄,但游標後面可能還有完全沒試過、也沒在冷卻中的帳號。

    劇本(主迴圈實跑復現的形狀):pool a/b/c/d,這批 failover 已經試過 a、b
    (tried={a,b}),游標因為另一個並行 request 已經被推到 d,而 a 的冷卻剛好過期。
    不傳 `skip` 的話,`runtime.rotate_client` 從 d 往後掃到的第一個「不在冷卻中」
    候選就是 a——已經試過的那個;c 從沒被拒絕過也沒進冷卻表,卻因為排除只擋在
    回傳值上(而不是掃描裡)被漏試,`_rotate_for_quota` 因此白白回 None。

    ``fake_client`` 只為了借它的 fixture 收尾(`runtime.set_client(None)`)——這裡
    直接改寫 pool/`_ACTIVE`/`_COOLING`,沒有這個收尾會漏到同一個 session 後面的測試。
    """
    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        language="en",
        audio_format=None,
        audio_length=None,
    )
    p._claim_prepared_dispatch(store, 1, attempt_id, [], account="b@x")

    import time as time_mod

    runtime.set_clients([(label, fake_client) for label in ("a@x", "b@x", "c@x", "d@x")])
    now = {"t": 1000.0}
    monkeypatch.setattr(time_mod, "monotonic", lambda: now["t"])
    runtime._COOLING[0] = now["t"]  # a 先前被拒,先進冷卻
    runtime._ACTIVE = 3  # 模擬「並行 request 已經把游標推到 d」
    now["t"] += runtime._COOLDOWN_SECONDS + 1  # a 的冷卻剛好到期

    result = p._rotate_for_quota(
        store, 1, attempt_id, RuntimeError("quota"), "b@x", {"a@x", "b@x"}
    )

    assert result is not None, (
        "c@x 從沒試過也沒冷卻——不該因為 rotate 掃到的第一個候選剛好是已試過的 "
        "a@x 就整批放棄"
    )
    account, _client = result
    assert account == "c@x"


def _bouncing_rotate_client(pool, state):
    """冷卻永遠已過期的假 `runtime.rotate_client`:在 `pool` 裡的帳號間無限乒乓,
    從不回 None。用呼叫次數當保險絲(**不是牆上時鐘**)——`tried` guard 一旦失效,
    `_dispatch_audio_with_failover` 的 `while True` 會不斷跟這支要下一個帳號,
    次數一過 `len(pool) + 1` 就直接指名根因,而不是讓測試在真實秒數上偶發逾時
    (F7:單跑 0.3~0.4 秒的原子寫入撞上 2 秒的 `asyncio.wait_for` 只有 ~5 倍餘裕,
    機器一忙就穿;而且『tried 失效』與『機器慢』紅的都是同一種 TimeoutError,
    看紅字分不出是 regression 還是雜訊)。
    """
    calls = {"n": 0}

    def fake_rotate_client(*, refused=None, skip=frozenset()):
        calls["n"] += 1
        if calls["n"] > len(pool) + 1:
            raise AssertionError("rotate 被無限呼叫 —— tried guard 失效")
        idx = pool.index(state["active"])
        state["active"] = pool[(idx + 1) % len(pool)]
        return state["active"]

    return fake_rotate_client


async def test_dispatch_failover_terminates_even_if_rotate_never_reports_exhaustion(
    fake_client, tmp_path, monkeypatch
):
    """**F7**:終止性不能依賴時鐘。

    v0.9.7 之前 `_ACTIVE` 只增不減,繞一圈必定回到起點,終止性無條件成立;加了冷卻後
    `rotate_client()` 改成「冷卻過期就重新可用」——若每一腿被拒都耗時夠久(SDK
    backoff、伺服器慢回 429),繞回來時第一格冷卻可能已經過期,`rotate_client()`
    就會**永遠**回得出帳號。這裡直接模擬那個最壞情況(monkeypatch 讓它在兩個帳號間
    無限乒乓、從不回 None),證明 `_dispatch_audio_with_failover` 自己用 `tried`
    擋住,而不是靠 `rotate_client` 繞完一圈就停。走的是 `_REFUSED_WITHOUT_DISPATCH`
    這條 raise 路徑(0.8.0 起的同步拒絕)。
    """
    from notebooklm.exceptions import RateLimitError

    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        language="en",
        audio_format=None,
        audio_length=None,
    )
    p._claim_prepared_dispatch(store, 1, attempt_id, [], account="a@x")

    calls: list = []

    async def always_refuse(client):
        calls.append(client)
        raise RateLimitError("每日配額已用盡")

    pool = ["a@x", "b@x"]
    state = {"active": "a@x"}
    monkeypatch.setattr(runtime, "rotate_client", _bouncing_rotate_client(pool, state))
    monkeypatch.setattr(runtime, "snapshot", lambda: (state["active"], fake_client))

    with pytest.raises(RateLimitError):
        await p._dispatch_audio_with_failover(
            store, 1, attempt_id, always_refuse,
            account="a@x", client=fake_client,
        )

    assert len(calls) == 2, (
        "終止性:一輪之內最多試『沒被 tried 過』的帳號數,不能靠 rotate_client "
        "自己回 None 才停"
    )
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["attempts"][0]["dispatch"]["status"] == "not_accepted"


async def test_dispatch_failover_terminates_via_the_ensure_started_path_too(
    fake_client, tmp_path, monkeypatch
):
    """**F7 的孿生測試**:上面那條只走得到 `_REFUSED_WITHOUT_DISPATCH`(raise)分支,
    `_dispatch_audio_with_failover` 還有第二條路——0.7.x 風格的『不 raise,回
    `task_id="", is_failed=True` 由 `ensure_started` 判定』——兩條各自呼叫一次
    `_rotate_for_quota`,是分開補的兩處(AGENTS.md 點名的『補一半』形狀,這次
    輪到測試層:曾經只把其中一處的 tried guard 修好,全套照樣全綠)。
    """
    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        language="en",
        audio_format=None,
        audio_length=None,
    )
    p._claim_prepared_dispatch(store, 1, attempt_id, [], account="a@x")

    calls: list = []

    class _RefusedStatus:
        task_id = ""
        is_failed = True
        status = "failed"
        error = "每日配額已用盡"
        error_code = "RateLimitError"

    async def always_refuse(client):
        calls.append(client)
        return _RefusedStatus()

    pool = ["a@x", "b@x"]
    state = {"active": "a@x"}
    monkeypatch.setattr(runtime, "rotate_client", _bouncing_rotate_client(pool, state))
    monkeypatch.setattr(runtime, "snapshot", lambda: (state["active"], fake_client))

    with pytest.raises(RuntimeError, match="每日配額已用盡"):
        await p._dispatch_audio_with_failover(
            store, 1, attempt_id, always_refuse,
            account="a@x", client=fake_client,
        )

    assert len(calls) == 2, (
        "終止性:一輪之內最多試『沒被 tried 過』的帳號數,ensure_started 這條路徑"
        "也要靠 tried 擋住,不能靠 rotate_client 自己回 None 才停"
    )
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["episodes"][0]["attempts"][0]["dispatch"]["status"] == "not_accepted"


async def test_tried_guard_does_not_leave_a_phantom_failover_record(
    fake_client, tmp_path, monkeypatch
):
    """**P1**:`tried` 命中前,`_rotate_for_quota` 已經做完兩個副作用——
    `runtime.rotate_client(refused=...)`(冷卻)與 `_record_dispatch_failover`
    (把 `dispatch.account` 改寫成 to_account、往 append-only 的 `errors[]` 多寫
    一筆)。若 `tried` 這道門放在呼叫端、事後才丟棄回傳值,冷卻確實該進沒錯,但
    `_record_dispatch_failover` 那筆『換成 to_account』的紀錄從未真正生效
    (呼叫端沒有真的拿它去送),manifest 就會留下一次從未發生的換帳號,還覆寫掉
    上一輪才寫下的、真正生效的 `dispatch.account`。

    劇本:兩帳號 a/b 乒乓、冷卻永遠已過期、generate 永遠配額拒絕。
    第一次 rotate(a→b)真正生效,第二次 rotate 打回已經 tried 過的 a——這一次
    不該被送出去,也不該留下稽核紀錄。
    """
    from notebooklm.exceptions import RateLimitError

    manifest_path = tmp_path / "series_manifest.json"
    store = p.ManifestStore(str(manifest_path))
    attempt_id = p._create_audio_attempt(
        store,
        notebook_id="nb-1",
        episode_n=1,
        title="心法篇",
        brief="第一集",
        language="en",
        audio_format=None,
        audio_length=None,
    )
    p._claim_prepared_dispatch(store, 1, attempt_id, [], account="a@x")

    calls: list = []

    async def always_refuse(client):
        calls.append(client)
        raise RateLimitError("每日配額已用盡")

    pool = ["a@x", "b@x"]
    state = {"active": "a@x"}
    monkeypatch.setattr(runtime, "rotate_client", _bouncing_rotate_client(pool, state))
    monkeypatch.setattr(runtime, "snapshot", lambda: (state["active"], fake_client))

    with pytest.raises(RateLimitError):
        await p._dispatch_audio_with_failover(
            store, 1, attempt_id, always_refuse,
            account="a@x", client=fake_client,
        )

    assert len(calls) == 2, "真正送出兩次——第二次 rotate 打回已試過的帳號,不該再送第三次"

    attempt = _attempts(manifest_path)[0]
    assert attempt["dispatch"]["account"] == "b@x", (
        "manifest 要記『最後真正送出的那個帳號』,不是一次被丟棄、從未真正送出的換帳號"
    )
    failovers = [e for e in attempt["errors"] if e["phase"] == "dispatch_failover"]
    assert [(f["from_account"], f["to_account"]) for f in failovers] == [("a@x", "b@x")], (
        "只有一次換帳號真的發生過;b@x -> a@x 那次從未生效,不該留下稽核紀錄"
    )
