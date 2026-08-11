"""attempt 狀態 → 「現在能做什麼」的**窮舉**對帳。

同一個根因在這個 repo 現形過五次,形狀都一樣:**指引在它自己產生的狀態下不可執行**。
每次修法都是「補那一格」,而判斷分散在三個各自為政的布林加十幾處手寫訊息裡 ——
於是修第 N 條時在新訊息裡種下第 N+1 條。第五次是最露骨的:`_outcome_is_settled()`
回答「retract 需不需要旗標」,卻被拿去當「可不可以原樣重送」的判準,而
`failed`/`removed` 這兩者的答案**相反**。

前幾輪都做過突變驗證,卻照樣漏 —— 因為突變只打在**我想得到的**那幾處。獨立複審用兩個
突變證明了這個盲區:把 `input_bundle` 判斷整個拿掉、把 `removed` 從 settled 集合刪掉,
既有測試都全綠(fixture 只造了另一半)。

所以這裡不挑情境寫,而是走**狀態組合的笛卡爾積**,對每一格驗**不變式**。不變式的好處是
不必預先知道哪一格會出事:想不到的組合也在裡面。
"""
import itertools

import pytest

from notebooklm_mcp import tools_podcast as p


DISPATCH_STATES = (
    "prepared",
    "not_accepted",
    "dispatching",
    "acceptance_unknown",
    "accepted",
    "reconciliation_ambiguous",
)
REMOTE_STATES = (None, "pending", "completed", "failed", "removed")
ROLES = ("active", "output", "historical")
SETTINGS_SHAPES = {
    # `podcast_series` 不指名來源時的形狀(逐字,由 test_source_selection 鎖著)
    "series": ({"language": "zh", "audio_format": None, "audio_length": None}, None),
    "pinned": (
        {
            "language": "zh",
            "audio_format": None,
            "audio_length": None,
            "source_ids": ["src-1"],
        },
        None,
    ),
    "bundle": ({"language": "zh"}, {"path": "bundle", "sha256": "x"}),
    # `podcast_episode_resume` 建的 attempt:**沒有來源 provenance**。武斷判成 series
    # 會把「只讀這幾筆」擴大成「讀整本筆記本」(真實重現:`[["src-1"], None]`)。
    "resume": ({"origin": "explicit_resume"}, None),
    # falsy-but-present:正常 API 產不出來,但 ManifestStore 讀取刻意寬鬆,舊或手改的
    # manifest 會出現。用 truthiness 判斷會 fail-open 成來源擴大。
    "empty_pinned": (
        {
            "language": "zh",
            "audio_format": None,
            "audio_length": None,
            "source_ids": [],
        },
        None,
    ),
}


def _case(dispatch, remote, role, shape):
    settings, bundle = SETTINGS_SHAPES[shape]
    attempt = {
        "attempt_id": "att-me",
        "dispatch": {"status": dispatch},
        "remote": {
            "status": remote,
            "artifact_id": "art-1" if remote in ("pending", "completed") else None,
        },
        "settings": settings,
    }
    if bundle is not None:
        attempt["input_bundle"] = bundle
    episode = {"episode": 1, "attempts": [attempt]}
    if role == "active":
        episode["active_attempt_id"] = "att-me"
    elif role == "output":
        episode["active_attempt_id"] = "att-me"
        episode["output_attempt_id"] = "att-me"
    else:  # historical:已被別顆取代
        episode["active_attempt_id"] = "att-other"
        episode["output_attempt_id"] = "att-other"
    return episode, attempt


ALL_CASES = list(
    itertools.product(DISPATCH_STATES, REMOTE_STATES, ROLES, SETTINGS_SHAPES)
)

# P2 修復加的時間性維度:候選窗有沒有關。跟其餘測試無關的地方(series 白名單、
# resend/flag 語意……)不必跟著翻倍,只有這條核心不變式真的會讀 `can_reconcile`,
# 所以只在這裡加(笛卡爾積照 AGENTS.md 要求翻倍,不是每個測試都要背這個維度)。
ALL_CASES_WITH_WINDOW = list(
    itertools.product(
        DISPATCH_STATES, REMOTE_STATES, ROLES, SETTINGS_SHAPES, (False, True)
    )
)


@pytest.mark.parametrize("dispatch,remote,role,shape,window_closed", ALL_CASES_WITH_WINDOW)
def test_every_state_combination_yields_executable_guidance(
    dispatch, remote, role, shape, window_closed
):
    """**核心不變式:訊息教的每一個動作,在那個狀態下都必須真的做得到。**

    這一條直接對應五次現形的共同形狀。它不預設哪一格會錯 —— 把 next_step 的文字與
    capabilities 對照,教了做不到的事就紅。

    `window_closed` 是 P2 修復加的維度:候選窗關了之後,`podcast_episode_reconcile`
    這個字不准再出現在指引裡。**這裡故意不只做自證**(`if 提到 reconcile: assert
    can_reconcile`)——把 `can_reconcile` 算式裡 `and not reconciliation_window_closed`
    拿掉的突變,`caps["can_reconcile"]` 會跟著訊息一起變成 True,自證測不出來;
    下面直接拿 `window_closed` 這個輸入去斷言才抓得到。
    """
    episode, attempt = _case(dispatch, remote, role, shape)
    caps = p._attempt_capabilities(
        episode, attempt, "att-me", reconciliation_window_closed=window_closed
    )
    step = p._attempt_next_step(caps)

    if "原樣重呼" in step:
        assert caps["can_resend"], (
            f"教了原樣重送,但這個狀態送不了({dispatch}/{remote}/{role}):{step}"
        )
    if "podcast_episode_resume" in step:
        assert caps["can_resume"], f"教了 resume 但沒有 artifact 可續:{step}"
    if "podcast_episode_reconcile" in step:
        assert caps["can_reconcile"], f"教了 reconcile 但這個狀態對不了帳:{step}"
        assert not window_closed, f"候選窗已經關了,卻還教 reconcile:{step}"
    if "不需要** abandon_in_flight" in step or "不需要 abandon_in_flight" in step:
        assert caps["authorization_basis"] is not None, (
            f"說不需要旗標,但這個狀態的 retract 沒有免旗標理由:{step}"
        )
    if "abandon_in_flight=true" in step:
        assert caps["needs_abandon_flag"], (
            f"教了帶旗標,但那顆 attempt 傳了也沒用({role}):{step}"
        )


@pytest.mark.parametrize("dispatch,remote,role,shape", ALL_CASES)
def test_series_is_only_ever_offered_for_settings_series_can_reproduce(
    dispatch, remote, role, shape
):
    """**回 `podcast_series` 就等於宣稱「series 生得出同樣的 settings」。**

    宣稱錯了的後果是靜默把「只讀這幾筆」變成「讀整本筆記本」—— 本 repo 最忌諱的內容
    錯置形狀,而且從任何成功訊號都看不出來。所以判準必須是白名單:**只有認得出是 series
    自己建的形狀才回它**,其餘一律走能明示來源的單集入口。
    """
    episode, attempt = _case(dispatch, remote, role, shape)
    caps = p._attempt_capabilities(episode, attempt, "att-me")

    if caps["regeneration_entry"] == p.ACTION_SERIES:
        assert shape == "series", (
            f"{shape!r} 的 settings series 重現不出來,卻被指回 series —— 會靜默擴大來源"
        )
    else:
        assert caps["regeneration_entry"] == p.ACTION_EPISODE


@pytest.mark.parametrize("dispatch,remote,role,shape", ALL_CASES)
def test_settled_hint_never_mentions_series_when_the_entry_is_episode(
    dispatch, remote, role, shape
):
    """**F2 修正暴露出的既有缺陷(主迴圈裁決,item 6)的回歸鎖。**

    settled 分支的收尾曾經寫死「整季流程也可以直接重呼 podcast_series 讓它自動
    supersede」,沒有跟著同一顆 caps 的 `regeneration_entry` 走 —— 而
    `_reuse_frozen_input_attempt` 這條路上 `input_bundle is not None` 恆真、
    `regeneration_entry` 必定是 `podcast_episode`,於是同一句話前半教
    `podcast_episode`、後半卻教 `podcast_series`。照後半句做的後果是 series 走
    supersede 分支,用不指名來源的 `_audio_settings()` 建新 attempt,讀整本筆記本
    (含後面各集的回錄)進這一集——正是 v0.9.5 花整輪在防的內容錯置形狀。

    這條不變式蓋掉整個笛卡爾積:任何組合只要 `regeneration_entry` 落在
    `podcast_episode`,產生的句子就不准**推薦** `podcast_series`。

    ⚠️ **P2 修復後(caps["regeneration_hint"] 一律附加)**:「pinned」形狀的 hint
    會說「改用 podcast_series 會靜默改成讀整本筆記本」——這是**警告不要用**,不是
    「也可以用」,兩者語意相反但都含 `ACTION_SERIES` 這個字面值。所以判準改成比對
    v0.9.5 那句具體的推薦措辭(`also_series` 變數的逐字內容),不能再用「有沒有出現
    這個字」當代理判準——那個代理現在會把合法的警告句也一起打成違規。
    """
    episode, attempt = _case(dispatch, remote, role, shape)
    caps = p._attempt_capabilities(episode, attempt, "att-me")
    step = p._attempt_next_step(caps)

    if caps["regeneration_entry"] == p.ACTION_EPISODE:
        assert "也可以直接重呼 podcast_series" not in step, (
            f"regeneration_entry 是 podcast_episode,句子卻推薦重呼 podcast_series:{step}"
        )


@pytest.mark.parametrize("dispatch,remote,role,shape", ALL_CASES)
def test_resend_and_flag_free_retract_are_different_questions(
    dispatch, remote, role, shape
):
    """**第五次現形的直接回歸。**

    `failed`/`removed` 兩個問題的答案相反:免旗標 retract 可以(結果已定),原樣重送不行
    (`_is_resendable_same_request` 只收 `prepared`/`not_accepted`,重送走的是 supersede)。
    共用一個布林就必然教錯一邊 —— 那正是 v0.9.5 訊息裡發生的事。
    """
    episode, attempt = _case(dispatch, remote, role, shape)
    caps = p._attempt_capabilities(episode, attempt, "att-me")

    if caps["can_resend"]:
        assert dispatch in ("prepared", "not_accepted"), (
            "宣稱可原樣重送,但 `_is_resendable_same_request` 只收從沒 dispatch 的"
        )
        assert not caps["is_output"], "已 promote 的 output 不該被教去重送"
    if remote in ("failed", "removed") and dispatch not in ("prepared", "not_accepted"):
        assert not caps["can_resend"], "遠端已終態要走 supersede／重生,不是沿用這顆重送"


@pytest.mark.parametrize("dispatch,remote,shape", [
    (d, r, s) for d in DISPATCH_STATES for r in REMOTE_STATES for s in SETTINGS_SHAPES
])
def test_a_historical_attempt_is_never_offered_the_flag(dispatch, remote, shape):
    """歷史 attempt 誰都動不了它:`abandon_in_flight` 只放行 active 那一顆,
    傳 True 與 False 得到同一句話。教它傳旗標就是給一個永遠做不到的動作。"""
    episode, attempt = _case(dispatch, remote, "historical", shape)
    caps = p._attempt_capabilities(episode, attempt, "att-me")

    assert caps["needs_abandon_flag"] is False
    assert caps["authorization_basis"] is None
    step = p._attempt_next_step(caps)
    assert "abandon_in_flight" not in step, step


@pytest.mark.parametrize(
    "dispatch,remote", [(d, r) for d in DISPATCH_STATES for r in REMOTE_STATES]
)
def test_every_state_that_trips_the_source_guard_retracts_without_a_flag(
    dispatch, remote
):
    """**單向包含:來源守門停在 retract 的每一個狀態,retract 都必須免旗標收下它。**

    這是「停點照著做走得通」的形式化。守門的觸發條件寫在 `podcast_series` 裡、免旗標
    條件寫在 `_attempt_capabilities` 裡,**兩份程式碼分開** —— 動一邊沒動另一邊,指引
    就變死路。v0.9.4 正是如此:守門涵蓋了 `failed`/`removed`,免旗標條件沒有,照著
    `safe_next_action` 做必然被拒。

    ⚠️ **這條與上面的不變式測試互補,不是重複。** 不變式抓的是「自相矛盾」——訊息教的
    動作做不到。但把 `removed` 從 settled 集合拿掉時,所有不變式仍然自洽(它只是變成
    「需要旗標」,訊息也會跟著改口),**只有這條會紅**。獨立複審就是用這個突變證明了
    純不變式測試的盲區:語意選擇錯了,自洽性看不出來。
    """
    guard_trips = dispatch in ("prepared", "not_accepted") or remote in (
        "failed",
        "removed",
    )
    if not guard_trips:
        pytest.skip("這個狀態不會觸發來源守門")
    episode, attempt = _case(dispatch, remote, "active", "series")
    caps = p._attempt_capabilities(episode, attempt, "att-me")

    assert caps["authorization_basis"] == "settled", (
        f"守門會停在這裡並指向 retract,但 retract 不肯免旗標收下它({dispatch}/{remote})"
    )
    assert caps["needs_abandon_flag"] is False


@pytest.mark.parametrize("dispatch,remote,role,shape", ALL_CASES)
def test_the_regeneration_hint_never_claims_sources_that_are_not_there(
    dispatch, remote, role, shape
):
    """**警告句不可以宣稱 manifest 裡沒有的東西。**

    v0.9.6 驗收 FINDING-4:retract 對 `settings={"origin": "explicit_resume"}` 的 attempt
    回了「**重生時必須帶回原本那組 `source_ids`**」—— 而那顆根本沒有 source_ids。
    根因是入口判斷用白名單(認不出來就保守導向 `podcast_episode`),而警告句另外用
    if/else 猜,猜錯的正好是白名單特意涵蓋的那一類。**收斂做了一半就是這個下場:
    action 對了,附帶的話還是錯的。**

    這條把「話」與「事實」綁在一起,而且是窮舉的 —— 之後任何新的 settings 形狀進來,
    都不可能再靠猜。
    """
    episode, attempt = _case(dispatch, remote, role, shape)
    caps = p._attempt_capabilities(episode, attempt, "att-me")
    hint = caps["regeneration_hint"]

    if "必須帶回原本那組" in hint:
        assert (attempt.get("settings") or {}).get("source_ids"), (
            f"{shape!r} 沒有 source_ids,卻被叫去「帶回原本那組」:{hint}"
        )
    if "新的、尚未綁定" in hint:
        assert attempt.get("input_bundle") is not None, hint
    # 認不出來的形狀必須**明說認不出來**,不可以靜默當成 series 或假裝知道來源。
    if shape in ("resume",):
        assert "認不出" in hint, f"{shape!r} 應該明說 manifest 裡沒有來源紀錄:{hint}"
