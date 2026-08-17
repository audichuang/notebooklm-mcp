"""附件家族(簡報 / 講義 / 改版單頁)的配額 failover —— 與音檔**同一個迴圈**。

v0.9.16 之前只有音檔會換帳號:`_rotate_for_quota` 只長在 `tools_podcast`,而
`generate_slides` / `generate_report` / `artifact_revise_slide` 走的是
`runtime.get_client()`(此刻游標指到的那一個),撞到 `RateLimitError` 直接拋。

實務上的形狀比「少一個功能」更難看:slides 撞限流**既不推游標也不記冷卻**,所以
呼叫端原樣重跑會**永遠**打同一個帳號,直到某次 podcast dispatch 恰好 rotate 才會漂走
——pool 裝了 5 個帳號,實測連敲三次全部落在同一格。

紅線與音檔完全共用(`_failover.py` 的模組 docstring 列著五條),本檔只驗**附件這三支
真的接上去了**、以及附件家族專屬的稽核面(episode 級,因為附件沒有 durable attempt)。
每一條都同時是「不補一半」的反向鎖:三支工具各驗一次,不是只驗 slides。
"""
import json

import pytest
from conftest import FakeClient

from notebooklm_mcp import runtime
from notebooklm_mcp import tools_artifacts as a
from notebooklm_mcp._errors import NotebookAccessDenied


def _manifest(tmp_path, episodes=None):
    path = tmp_path / "series_manifest.json"
    path.write_text(
        json.dumps(
            {"notebook_id": "nb-1", "episodes": episodes or [{"episode": 1, "title": "EP01"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(path)


def _episode(manifest_path, episode_n=1):
    data = json.loads(open(manifest_path, encoding="utf-8").read())
    return next(e for e in data["episodes"] if e["episode"] == episode_n)


def _failovers(manifest_path, episode_n=1):
    return [
        entry
        for entry in _episode(manifest_path, episode_n).get("attachment_errors", [])
        if entry["phase"] == "attachment_dispatch_failover"
    ]


def _refuse_first(client, method_name, calls, fail_first_n, exc=None):
    """讓前 N 次某個 generate 以配額拒絕收場,其餘照常。

    每次呼叫記下**當下作用中的帳號**——這是「第二次真的換了帳號發出去」的直接證據,
    比檢查 manifest 欄位更難造假(欄位可能是回寫時才算出來的)。
    """
    from notebooklm.exceptions import RateLimitError

    original = getattr(client.artifacts, method_name)

    async def flaky(*args, **kwargs):
        calls.append(runtime.active_account())
        if len(calls) <= fail_first_n:
            raise exc or RateLimitError("每日配額已用盡")
        return await original(*args, **kwargs)

    setattr(client.artifacts, method_name, flaky)


# ---- 三支工具都要換帳號(不補一半)------------------------------------------------


async def test_slides_quota_refusal_rotates_and_records_who_generated(fake_client, tmp_path):
    """帳號 A 被拒 → 換 B 重送 → 成功,而且「這集簡報是誰生的」答得出來。

    稽核是 ADR-0010 §Transparency 的硬要求,也是這條路徑此前不准換帳號的**唯一**原因
    (`_rotate_for_quota` 的 `store is None` 那道門):對 client 透明可以,對紀錄不行。
    """
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    calls: list = []
    _refuse_first(fake_client, "generate_slide_deck", calls, fail_first_n=1)

    res = await a.generate_slides("nb-1", manifest_path, 1)

    assert calls == ["a@x", "b@x"], "第二次 dispatch 要由另一個帳號發出"
    assert res["slides_pdf_path"].endswith("ep01-slides.pdf")

    episode = _episode(manifest_path)
    assert episode["slides_account"] == "b@x", "記的是實際生成的那個帳號,不是起始那個"
    failovers = _failovers(manifest_path)
    assert len(failovers) == 1
    assert failovers[0]["kind"] == "slides"
    assert failovers[0]["from_account"] == "a@x"
    assert failovers[0]["to_account"] == "b@x"
    assert failovers[0]["type"] == "RateLimitError"
    assert "每日配額已用盡" in failovers[0]["message"]


async def test_report_quota_refusal_rotates(fake_client, tmp_path):
    """講義同理。**分開驗**:三支各自呼叫一次共用迴圈,只驗 slides 正是「補一半」的形狀。"""
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    calls: list = []
    _refuse_first(fake_client, "generate_report", calls, fail_first_n=1)

    await a.generate_report("nb-1", manifest_path, 1)

    assert calls == ["a@x", "b@x"]
    episode = _episode(manifest_path)
    assert episode["report_account"] == "b@x"
    assert [f["kind"] for f in _failovers(manifest_path)] == ["report"]


async def test_revise_slide_quota_refusal_rotates(fake_client, tmp_path):
    """改版單頁也燒配額,也會被同步拒絕 —— 它是 generate 家族的第三支,不是唯讀救援。"""
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    fake_client.artifacts.seed_artifacts(
        _completed_slide_deck("deck-1"),
    )
    calls: list = []
    _refuse_first(fake_client, "revise_slide", calls, fail_first_n=1)

    await a.artifact_revise_slide("nb-1", manifest_path, 1, "deck-1", 0, "把第一頁改短")

    assert calls == ["a@x", "b@x"]
    assert [f["kind"] for f in _failovers(manifest_path)] == ["revise_slide"]
    assert _episode(manifest_path)["slides_account"] == "b@x"


def _completed_slide_deck(artifact_id):
    """`_require_completed_slide_deck` 認得的最小 artifact 替身。"""
    from notebooklm.types import ArtifactType

    return type(
        "Art",
        (),
        {
            "id": artifact_id,
            "kind": ArtifactType.SLIDE_DECK,
            "is_completed": True,
            "status_str": "completed",
            "title": "EP01 題目",
        },
    )()


# ---- 終止性(紅線④)-------------------------------------------------------------


async def test_all_accounts_exhausted_raises_after_trying_each_once(fake_client, tmp_path):
    """全部被拒 → 原樣拋給呼叫端,而且每個帳號只試一次。

    附件沒有 durable attempt,所以終態就是原樣拋 —— **沒有** `not_accepted` 可標,也
    沒有東西要對帳(遠端什麼都沒建出來)。呼叫端的下一步就是原樣重跑。
    """
    from notebooklm.exceptions import RateLimitError

    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client), ("c@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    calls: list = []
    _refuse_first(fake_client, "generate_slide_deck", calls, fail_first_n=99)

    with pytest.raises(RateLimitError, match="每日配額已用盡"):
        await a.generate_slides("nb-1", manifest_path, 1)

    assert calls == ["a@x", "b@x", "c@x"], "每個帳號都要試過一次,而且只試一次"
    assert "slides_pdf_path" not in _episode(manifest_path)
    assert len(_failovers(manifest_path)) == 2, "兩腿換帳號,兩筆稽核"

    # **最後那一腿也要有 durable 紀錄**(codex 獨立複審 Review B #5):`record_failover`
    # 只在**找得到下一個帳號**時才寫,所以 A→B、B→C 兩筆之後,C 被拒這件事只存在於
    # 往外拋的例外裡。response 一遺失,「哪個帳號被拒過」就答不出來 —— 而那正是
    # ADR-0010/0011 拿來justify「可以換帳號」的兩個問題之一。
    refused = [
        e for e in _episode(manifest_path)["attachment_errors"]
        if e["phase"] == "attachment_dispatch_refused"
    ]
    assert [e["account"] for e in refused] == ["c@x"], (
        "耗盡時最後被拒的那個帳號要留一筆,不能只活在例外裡"
    )


async def test_single_account_pool_still_records_the_refusal(fake_client, tmp_path):
    """**單帳號 pool 是最刻薄的那一格**:一次 rotate 都沒發生,所以第一版一筆紀錄都不留。

    這正是 ADR-0011 聲稱「哪個帳號被拒過答得出來」最容易破功的情況 —— 開發機、單帳號
    部署都是這一格,而它偏偏是 `record_failover` 永遠不會被呼叫到的那一格。
    """
    from notebooklm.exceptions import RateLimitError

    runtime.set_clients([("only@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    calls: list = []
    _refuse_first(fake_client, "generate_slide_deck", calls, fail_first_n=99)

    with pytest.raises(RateLimitError):
        await a.generate_slides("nb-1", manifest_path, 1)

    assert calls == ["only@x"]
    events = _episode(manifest_path)["attachment_errors"]
    assert [(e["phase"], e["account"]) for e in events] == [
        ("attachment_dispatch_refused", "only@x")
    ]


async def test_acceptance_unknown_leaves_a_breadcrumb(fake_client, tmp_path):
    """受理不明 → 遠端**可能**有一顆沒人綁得到的 artifact,要留一筆看得見的紀錄。

    走的是「有 task_id 卻 is_failed」那條(紅線②:不 rotate)。沒有紀錄的話,這條路
    只會拋一個例外,而遠端那顆半死的 artifact 從此沒有任何線索。
    """
    runtime.set_clients([("a@x", fake_client)])
    manifest_path = _manifest(tmp_path)

    async def accepted_then_failed(*args, **kwargs):
        return type("S", (), {"task_id": "art-9", "is_failed": True,
                              "status": "failed", "error": "伺服器端生成失敗"})()

    fake_client.artifacts.generate_slide_deck = accepted_then_failed

    with pytest.raises(RuntimeError):
        await a.generate_slides("nb-1", manifest_path, 1)

    events = _episode(manifest_path)["attachment_errors"]
    assert [e["phase"] for e in events] == ["attachment_acceptance_unknown"]
    assert events[0]["account"] == "a@x"
    assert "伺服器端生成失敗" in events[0]["message"], (
        "稽核要記得出真正的原因,不是 status 物件的 repr"
    )


def _bouncing_rotate_client(pool, state):
    """冷卻永遠已過期的假 `runtime.rotate_client`:在 `pool` 裡無限乒乓,從不回 None。

    用呼叫次數當保險絲(**不是牆上時鐘**):`tried` guard 一旦失效,`while True` 會不斷
    跟這支要下一個帳號,次數一過就直接指名根因,而不是讓測試在真實秒數上偶發逾時。
    """

    def fake_rotate_client(*, refused=None, skip=frozenset()):
        state["n"] += 1
        if state["n"] > len(pool) + 1:
            raise AssertionError("rotate 被無限呼叫 —— tried guard 失效")
        idx = pool.index(state["active"])
        state["active"] = pool[(idx + 1) % len(pool)]
        return state["active"]

    return fake_rotate_client


@pytest.mark.parametrize("shape", ["raise", "empty_task_id"])
async def test_terminates_even_if_rotate_never_reports_exhaustion(
    fake_client, tmp_path, monkeypatch, shape
):
    """**F7**:終止性不能依賴時鐘 —— 附件這條路也要靠自己的 `tried` 擋住。

    `rotate_client()` 的冷卻會過期:每一腿被拒都夠久(SDK backoff、伺服器慢回 429)時,
    繞回第一格時它的冷卻可能已經到期,`rotate_client()` 就會**永遠**回得出帳號。上面那條
    「全部耗盡」測試看不出這件事 —— 真實 `rotate_client` 在測試的時間尺度下自己就回 None,
    所以把 `tried` 整道拿掉它照樣綠(實測)。這裡直接模擬最壞情況。

    **兩種形狀各驗一次**:共用迴圈有兩處 rotate(0.8.0 的 raise、0.7.x 的空 task_id),
    歷史上正是分開補的兩處,只驗一種就是「補一半」。
    """
    from notebooklm.exceptions import RateLimitError

    manifest_path = _manifest(tmp_path)
    calls: list = []

    async def always_refuse(*args, **kwargs):
        calls.append(state["active"])
        if shape == "raise":
            raise RateLimitError("每日配額已用盡")
        return type("S", (), {"task_id": "", "is_failed": True, "status": "failed",
                              "error": "每日配額已用盡", "error_code": "RateLimitError"})()

    pool = ["a@x", "b@x"]
    state = {"active": "a@x", "n": 0}
    runtime.set_clients([(label, fake_client) for label in pool])
    fake_client.artifacts.generate_slide_deck = always_refuse
    monkeypatch.setattr(runtime, "rotate_client", _bouncing_rotate_client(pool, state))
    monkeypatch.setattr(runtime, "snapshot", lambda: (state["active"], fake_client))

    # 兩條路徑的終態例外**型別不同**:0.8.0 的同步拒絕原樣重拋 SDK 的 `RateLimitError`
    # (不是 RuntimeError 子類);0.7.x 的空 task_id 是 `ensure_started` 自己拋的
    # `RuntimeError`。寫死一種會讓其中一格「因為型別不符而紅」,看起來像 regression。
    expected = RateLimitError if shape == "raise" else RuntimeError
    with pytest.raises(expected, match="每日配額已用盡"):
        await a.generate_slides("nb-1", manifest_path, 1)

    assert len(calls) == 2, (
        "終止性:一輪之內最多試『沒被 tried 過』的帳號數,不能靠 rotate_client 自己回 None"
    )


# ---- 反向鎖:不該換的時候不准換 --------------------------------------------------


async def test_generic_failure_never_rotates(fake_client, tmp_path):
    """只有契約保證「沒建出 task」的那兩種才准換帳號(紅線①)。

    `RPCError` 可能發生在伺服器已經受理之後 —— 換帳號重送會變成重複 artifact + 重燒
    配額,而呼叫端只綁得到後面那顆。
    """
    from notebooklm.exceptions import RPCError

    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    calls: list = []
    _refuse_first(fake_client, "generate_slide_deck", calls, fail_first_n=99,
                  exc=RPCError("伺服器 500"))

    with pytest.raises(RPCError):
        await a.generate_slides("nb-1", manifest_path, 1)

    assert calls == ["a@x"], "受理結果不明時不得換帳號重送"
    assert _failovers(manifest_path) == []


async def test_accepted_but_failed_status_never_rotates(fake_client, tmp_path):
    """**有 task_id 卻 is_failed ≠ 零副作用拒絕**(紅線②)。

    SDK `_artifact/generation.py:586-590` 明寫「有 artifact_id 就回
    GenerationStatus(task_id=artifact_id, status=…)」,而 `_ARTIFACT_STATUS_MAP` 含
    FAILED → "failed",所以這個形狀在上游可達。遠端已經建出 task,重送會產生第二顆。
    """
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    calls: list = []

    class _AcceptedThenFailed:
        task_id = "slide-task"
        is_failed = True
        status = "failed"
        error = "generation failed"

    async def accepted_then_failed(*args, **kwargs):
        calls.append(runtime.active_account())
        return _AcceptedThenFailed()

    fake_client.artifacts.generate_slide_deck = accepted_then_failed

    with pytest.raises(RuntimeError):
        await a.generate_slides("nb-1", manifest_path, 1)

    assert calls == ["a@x"], "已受理的失敗不得換帳號重送 —— 會多燒一顆 artifact"
    assert _failovers(manifest_path) == []


async def test_empty_task_id_refusal_rotates(fake_client, tmp_path):
    """0.7.x 形狀的拒絕(不 raise,回 `task_id=""` + is_failed)也要換帳號。

    這是共用迴圈的**第二條** rotate 路徑。兩條各自呼叫一次 rotate,歷史上正是分開補的
    兩處(v0.9.8 F7 的孿生測試點名過:曾經只把其中一處修好,全套照樣全綠)。
    """
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    calls: list = []
    original = fake_client.artifacts.generate_slide_deck

    async def refuse_then_ok(*args, **kwargs):
        calls.append(runtime.active_account())
        if len(calls) == 1:
            return type("S", (), {"task_id": "", "is_failed": True,
                                  "status": "failed", "error": "配額用盡",
                                  "error_code": "RateLimitError"})()
        return await original(*args, **kwargs)

    fake_client.artifacts.generate_slide_deck = refuse_then_ok

    await a.generate_slides("nb-1", manifest_path, 1)

    assert calls == ["a@x", "b@x"]
    failover = _failovers(manifest_path)[0]
    assert failover["type"] == "RateLimitError", "非例外的拒絕也要記得出型別"
    assert "配額用盡" in failover["message"]


async def test_permission_denied_never_rotates_and_names_the_fix(fake_client, tmp_path):
    """權限被拒是**設定問題**,一個個帳號試過去只會埋掉根因(紅線③)。"""
    from notebooklm.exceptions import ClientError

    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    calls: list = []
    _refuse_first(fake_client, "generate_slide_deck", calls, fail_first_n=99,
                  exc=ClientError("permission denied", rpc_code=7))

    with pytest.raises(NotebookAccessDenied, match="分享") as excinfo:
        await a.generate_slides("nb-1", manifest_path, 1)

    assert calls == ["a@x"], "權限問題不該一個一個帳號試過去"
    message = str(excinfo.value)
    assert "notebook_share_with_pool" in message, "要指名補分享的正門工具"
    assert "a@x" in message, "要指名是哪個帳號被拒"
    assert "nb-1" in message, "要指名是哪一本 notebook"
    assert _failovers(manifest_path) == []


# ---- 身分要一路釘到下載(ADR-0010 的 download identity)---------------------------


async def test_download_uses_the_account_that_dispatched_after_failover(tmp_path):
    """換過帳號之後,**等完成與下載必須用換過去那個 client**。

    ADR-0010 講得很清楚:風險視窗幾乎全落在後半段(dispatch 幾秒,等生成 + 下載是
    數十分鐘)。回頭讀 `runtime.get_client()` 的話,分享不完整時會在十幾分鐘後爆 401
    而根因在遠處;分享完整時退化成稽核失真(紀錄說 B 生成,實際下載的是別人)。

    用**兩個不同的 fake client** 才驗得出來 —— 同一個 client 放兩格的話,不管釘不釘
    身分,呼叫都落在同一顆假件上。
    """
    from notebooklm.exceptions import RateLimitError

    first, second = FakeClient(), FakeClient()
    runtime.set_clients([("a@x", first), ("b@x", second)])
    manifest_path = _manifest(tmp_path)

    async def always_refuse(*args, **kwargs):
        raise RateLimitError("每日配額已用盡")

    first.artifacts.generate_slide_deck = always_refuse

    await a.generate_slides("nb-1", manifest_path, 1)

    downloads = [c for c in second.artifacts.calls if c[0] == "download_slide_deck"]
    assert downloads, "下載要落在實際送出生成的那個 client 上"
    assert [c for c in first.artifacts.calls if c[0] == "download_slide_deck"] == [], (
        "被拒的那個帳號不該碰下載"
    )
    assert _episode(manifest_path)["slides_account"] == "b@x"


@pytest.mark.parametrize("kind", ["report", "revise_slide"])
async def test_download_identity_holds_for_report_and_revise_too(tmp_path, kind):
    """**不補一半**:三支的 wiring 是分開的三段,只驗 slides 等於只驗三分之一。

    既有的 `test_generation_pins_client_through_download` 只涵蓋**沒有 failover** 的情況
    (它讓 generate 成功之後才 rotate),所以「換過帳號之後下載用誰」在 report 與
    revise_slide 上此前完全沒有測試。
    """
    from notebooklm.exceptions import RateLimitError

    first, second = FakeClient(), FakeClient()
    runtime.set_clients([("a@x", first), ("b@x", second)])
    manifest_path = _manifest(tmp_path)

    async def always_refuse(*args, **kwargs):
        raise RateLimitError("每日配額已用盡")

    if kind == "report":
        first.artifacts.generate_report = always_refuse
        await a.generate_report("nb-1", manifest_path, 1)
        download, account_field = "download_report", "report_account"
    else:
        for client in (first, second):
            client.artifacts.seed_artifacts(_completed_slide_deck("deck-1"))
        first.artifacts.revise_slide = always_refuse
        await a.artifact_revise_slide("nb-1", manifest_path, 1, "deck-1", 0, "改短")
        download, account_field = "download_slide_deck", "slides_account"

    assert [c for c in second.artifacts.calls if c[0] == download], (
        "下載要落在實際送出生成的那個 client 上"
    )
    assert [c for c in first.artifacts.calls if c[0] == download] == [], (
        "被拒的那個帳號不該碰下載"
    )
    assert _episode(manifest_path)[account_field] == "b@x"


# ---- 不換帳號時不留噪音 ----------------------------------------------------------


async def test_no_failover_record_when_the_first_account_succeeds(fake_client, tmp_path):
    """一次就成功:不留 `attachment_errors`,但「誰生的」還是要記。"""
    runtime.set_clients([("a@x", fake_client)])
    manifest_path = _manifest(tmp_path)

    await a.generate_slides("nb-1", manifest_path, 1)

    episode = _episode(manifest_path)
    assert "attachment_errors" not in episode, "沒換帳號就不要留空清單當噪音"
    assert episode["slides_account"] == "a@x"


async def test_rescue_download_never_claims_to_know_who_generated(fake_client, tmp_path):
    """救援下載一律把 `slides_account` 寫成 `None` —— 那是它真的不知道。

    劇本(獨立複審第一輪 B#3):舊簡報由 a@x 生成、manifest 記著 a@x;b@x 生了新的
    deck-NEW,但回寫前斷線;呼叫端用這支救回 deck-NEW —— 本機那份 PDF 其實來自 b@x,
    而 manifest 若留著 a@x 就**永久聲稱錯的帳號**。一個過時的值看起來是權威的,比明確的
    「不知道」更糟。

    **第一版的行為是「不知道就不要動舊值」,而它的測試把那個結果鎖成預期。**
    第二版改成「憑據對得上才留」,但那個憑據本身在受理時就落盤(見 `_finish_slides` 上方
    那段紀律),自己會造出「檔案舊、manifest 新」的錯配,所以連憑據一起退掉了。
    現在的規則最簡單也最誠實:artifact_id 跟著成品更新,account 寫 `None`。
    """
    runtime.set_clients([("a@x", fake_client), ("b@x", fake_client)])
    manifest_path = _manifest(
        tmp_path,
        [{
            "episode": 1, "title": "EP01",
            "slides_account": "a@x", "slides_artifact_id": "deck-OLD",
        }],
    )
    runtime.rotate_client(refused="a@x")     # 游標推到 b@x —— 不該被寫進紀錄

    await a.artifact_download_slides("nb-1", manifest_path, 1, "deck-NEW")

    episode = _episode(manifest_path)
    assert episode["slides_account"] is None, (
        "救援不知道是誰生的:要寫明確的『不知道』,不能留舊帳號、也不能寫當下游標指到的那個"
    )
    assert episode["slides_artifact_id"] == "deck-NEW", "本機成品現在是這一顆"


async def test_failed_generation_leaves_the_previous_provenance_untouched(
    fake_client, tmp_path
):
    """生成失敗時,舊成品的 provenance **一個字都不能動**。

    這是第二版「受理憑據」被退掉的直接原因(獨立複審第二輪 F1,實測重現):憑據在受理成功
    時就落盤,而接下來的 `wait` 回 failed —— 磁碟上還是舊那份 PDF,manifest 卻已經聲稱
    當前附件是新帳號、新 artifact_id。provenance 的語意是「**現在磁碟上這份**是誰生的」,
    所以它只能在成品真的落地時寫。
    """
    pdf = tmp_path / "ep01-slides.pdf"
    pdf.write_bytes(b"%PDF-OLD")
    manifest_path = _manifest(
        tmp_path,
        [{
            "episode": 1, "title": "EP01", "slides_pdf_path": str(pdf),
            "slides_account": "old@x", "slides_artifact_id": "deck-OLD",
        }],
    )
    runtime.set_clients([("new@x", fake_client)])

    async def failed_wait(*args, **kwargs):
        return type("S", (), {"is_completed": False, "is_failed": True,
                              "status_str": "failed", "status": "failed"})()

    fake_client.artifacts.wait_for_completion = failed_wait

    with pytest.raises(Exception):
        await a.generate_slides("nb-1", manifest_path, 1)

    episode = _episode(manifest_path)
    assert episode["slides_account"] == "old@x", "失敗的生成不得改寫 provenance"
    assert episode["slides_artifact_id"] == "deck-OLD"
    assert pdf.read_bytes() == b"%PDF-OLD", "舊成品原封不動"


# ---- 本地錯誤不准偽造成遠端事件 --------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, tool",
    [
        ({"language": "zh-TW"}, "slides"),
        ({"slide_format": "no-such-format"}, "slides"),
        ({"report_format": "no-such-format"}, "report"),
        ({"language": "zh-TW"}, "report"),
    ],
)
async def test_local_argument_errors_are_not_recorded_as_remote_events(
    fake_client, tmp_path, kwargs, tool
):
    """打錯 language code / enum 是**純本地**錯誤:零 RPC,所以不准留任何稽核事件。

    這些轉換原本寫在 dispatch closure 裡,於是它們拋的 `ValueError` 落進共用迴圈的泛用
    except,被記成一筆 `attachment_acceptance_unknown`(實測 SDK 呼叫次數 0)。
    `attachment_errors` 是 append-only 的,所以那筆憑空造出來的「遠端受理不明」**永遠清不掉**
    ——事後查配額問題的人會被它帶去查一個從未發生的遠端狀態(獨立複審第二輪 F4)。
    """
    runtime.set_clients([("a@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    call = a.generate_slides if tool == "slides" else a.generate_report

    with pytest.raises(ValueError):
        await call("nb-1", manifest_path, 1, **kwargs)

    assert fake_client.artifacts.calls == [], "本地錯誤不該發出任何 RPC"
    assert "attachment_errors" not in _episode(manifest_path), (
        "零遠端動作就不准留遠端事件 —— 而且 append-only 清不掉"
    )


async def test_audit_write_post_commit_failure_does_not_mask_the_quota_error(
    fake_client, tmp_path, monkeypatch
):
    """終態稽核的 parent-dir fsync 失敗**不得**遮蔽掉原本要拋的配額例外。

    `rotate_for_quota` 早就對 rotation recorder 做了這件事,終態那半漏了(獨立複審第二輪
    F5,實測):`os.replace` 已經成功 → 紀錄**是** durable 的,只有額外的 crash-durability
    fsync 回 EIO,呼叫端卻收到 `ManifestPostCommitError`,而它承諾的是原樣重拋。
    """
    from notebooklm.exceptions import RateLimitError

    from notebooklm_mcp import manifest_store

    runtime.set_clients([("only@x", fake_client)])
    manifest_path = _manifest(tmp_path)
    _refuse_first(fake_client, "generate_slide_deck", [], fail_first_n=99)

    def fsync_eio(_path):
        raise manifest_store.ManifestPostCommitError("fsync EIO")

    monkeypatch.setattr(manifest_store, "_fsync_parent", fsync_eio)

    with pytest.raises(RateLimitError, match="每日配額已用盡"):
        await a.generate_slides("nb-1", manifest_path, 1)

    assert [e["phase"] for e in _episode(manifest_path)["attachment_errors"]] == [
        "attachment_dispatch_refused"
    ], "紀錄本身是 commit 過的,所以要看得到"
