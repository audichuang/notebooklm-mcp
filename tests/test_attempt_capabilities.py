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
# P1(Codex adversarial review 實跑驗證):`active_not_output` 是 ADR-0009 amendment
# 明列、但這三格笛卡爾積生不出來的第四格——active=自己、output=另一顆不同的
# attempt。實跑用 helper 查這格的答案:查 A(output)→ podcast_attempt_retract,
# 但實際 retract A 會被 active guard 拒(要求先處理 active B);查 B(active)→
# can_resume/can_reconcile 可能算出 True 而指向 resume/reconcile,但實際會被
# `_ensure_resume_attempt` 的既有 output guard 拒收。正確且唯一的出口是先 retract
# B 自己——這一格補進來之前,`_attempt_capabilities`/`_attempt_next_step` 對 B 完全
# 沒有測試守著。
ROLES = ("active", "output", "historical", "active_not_output")
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
    elif role == "active_not_output":
        # active≠output 分岔(P1 補的第四格):自己是 active,但另一顆(att-other)
        # 才是這一集的正式輸出。
        episode["active_attempt_id"] = "att-me"
        episode["output_attempt_id"] = "att-other"
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
# **三態,不是布林**(第四輪修復):`None` = 呼叫端沒算過(五個呼叫點裡有四個是這樣),
# `False`/`True` = 明確算過。三者的 `can_reconcile` 值相同(`None`/`False` 都不擋
# reconcile),但 `_attempt_next_step()` 對 `None` 不准講任何窗宣稱——那正是這一輪
# 修的回歸(見 test_unevaluated_window_never_makes_a_window_claim)。
# F2 修復加的第二個維度:`candidate_selection_required`。它跟 window_closed 一樣只在
# 這兩條核心不變式測試裡加——`_attempt_capabilities()` 的五個呼叫點裡只有兩個
# (`podcast_episode_reconcile` 的候選分岔)會傳 `True`,但在這輪盲審之前，整個
# `test_attempt_capabilities.py` 沒有任何一格餵過它(永遠吃預設值 `False`),於是
# `candidate_selection_required=True` 這個新維度整個在笛卡爾網之外——`_attempt_
# capabilities()` 與 `_attempt_next_step()` 對它的分支順序不一致（F2 regression：
# 前者排在 `can_resume` 之前，後者原本排在之後）完全沒有測試碰過。
# **三種 unresolved 狀態都要進網,不只 `dispatching`**(F2 盲審):三者的共同後果都是
# 「遠端可能多出一筆 media,而 manifest 記不住它是誰」,只餵一種等於另外兩種完全沒有
# 不變式守著。`None` = source_id 已落盤或還沒 dispatch(不 unresolved)。
UNRESOLVED_UPLOAD_STATES = (
    None,
    "dispatching",
    "acceptance_unknown",
    "reconciliation_ambiguous",
)

ALL_CASES_WITH_WINDOW = [
    case
    for case in itertools.product(
        DISPATCH_STATES,
        REMOTE_STATES,
        ROLES,
        SETTINGS_SHAPES,
        (False, True, None),
        (False, True),
        UNRESOLVED_UPLOAD_STATES,
    )
    # Feedback upload 只可能在 generation 已 accepted、remote 已 completed 之後 dispatch。
    # `output` 那格排除掉:promote 的前提是四個 finalize step 都 completed,而那代表
    # source_id 早就落盤——不拿測試 fixture 製造 production 永遠生不出的矛盾狀態。
    # **`historical` 要留著**:supersede 一顆 remote 已終態的 attempt 之後,它的 upload
    # checkpoint 仍可能卡在 unresolved,而那正是「歷史紀錄」與「upload 未結案」兩個
    # 分支搶同一句話的那一格(分支順序回歸就長在這裡)。
    if case[-1] is None
    or (
        case[0] == "accepted"
        and case[1] == "completed"
        and case[2] in ("active", "historical")
    )
]


@pytest.mark.parametrize("status", UNRESOLVED_UPLOAD_STATES[1:])
def test_unresolved_feedback_upload_prefers_resume_but_never_blocks_retract(status):
    """**F2:未結案的 upload 不准取消 retract 能力。**

    舊版對 `dispatching` 硬擋(連 `abandon_in_flight` 都擋),而那個狀態靠一次 client
    cancellation 就能永久存在——`_reconcile_source_upload` 只從 finalize 進得去,於是
    唯一出路變成「要作廢一顆輸入本來就錯的 attempt,得先把它完整 finalize、上傳、
    promote」,比旗標本來要避免的後果還多一輪遠端副作用。

    現在的處置:優先建議 resume(那是唯一能把 source 身分認回來的路),但顯式旗標
    穿得過去,義務改由 tombstone 上的 `source_cleanup_unresolved` 接手。

    **T1(第十三次現形)**:`reconciliation_ambiguous` 是這三種狀態裡的例外——它已經
    帶著 server 對帳算出來的具體候選(`candidate_source_ids`),resume 對同兩個候選
    只會再拋一次同一個 ambiguous,零前進,所以這一格改教 `podcast_attempt_adopt`。
    這支測試舊版對三種狀態餵同一份 fixture(`candidate_source_ids` 一律空),於是
    ambiguous 那格鎖的答案(resume)其實是「問錯問題的 tripwire」——候選從未真的
    存在過,fixture 造不出真實狀態就永遠綠。這裡改成 ambiguous 時真的填候選。
    """
    episode, attempt = _case("accepted", "completed", "active", "series")
    upload = {"status": status, "source_id": None}
    if status == "reconciliation_ambiguous":
        upload["candidate_source_ids"] = ["src-a", "src-b"]
    attempt["finalize"] = {"feedback_source_upload": upload}

    caps = p._attempt_capabilities(episode, attempt, "att-me")

    assert caps["feedback_upload_unresolved"] is True
    assert caps["feedback_upload_status"] == status
    step = p._attempt_next_step(caps)
    if status == "reconciliation_ambiguous":
        assert caps["safe_next_action"] == p.ACTION_ADOPT
        assert caps["candidate_source_ids"] == ["src-a", "src-b"]
        assert p.ACTION_ADOPT in step
        assert "src-a" in step and "src-b" in step
    else:
        assert caps["safe_next_action"] == p.ACTION_RESUME
        assert caps["candidate_source_ids"] == []
        assert p.ACTION_RESUME in step
    assert "source_id 還沒落盤" in step
    # 旗標出口必須還在 —— 這一條就是 F2 的回歸鎖,對三種狀態都成立。
    assert caps["needs_abandon_flag"] is True
    assert "abandon_in_flight=true" in step

    # source_id 一落盤就不再 unresolved,回到既有的 stale_source_ids 路徑。
    attempt["finalize"]["feedback_source_upload"]["source_id"] = "src-1"
    caps = p._attempt_capabilities(episode, attempt, "att-me")
    assert caps["feedback_upload_unresolved"] is False
    assert caps["needs_abandon_flag"] is True


@pytest.mark.parametrize("status", UNRESOLVED_UPLOAD_STATES[1:])
def test_historical_attempt_wins_over_unresolved_upload(status):
    """已被取代的 attempt 就算 upload 卡在 unresolved,能動的也不是它。

    分支順序回歸:把 unresolved 判斷排在 historical 之前,這一格會教人去 resume 一顆
    歷史 attempt——而 output guard 會擋掉,又是一句在它自己產生的狀態下不可執行的指引。
    """
    episode, attempt = _case("accepted", "completed", "historical", "series")
    attempt["finalize"] = {
        "feedback_source_upload": {"status": status, "source_id": None}
    }

    caps = p._attempt_capabilities(episode, attempt, "att-me")

    assert caps["feedback_upload_unresolved"] is True
    assert caps["safe_next_action"] is None
    assert "歷史紀錄" in p._attempt_next_step(caps)


def test_post_retract_unresolved_upload_does_not_promise_regeneration():
    """retract 之後義務還沒對帳完時,不准把重生講成現在就做得到。

    `_assert_source_cleanup_done` 會把 `regeneration_entry` 擋成 ValueError,所以
    `safe_next_action` 必須是 `None`、指引必須講「等窗關上、gate 會自己對帳」。
    """
    episode, attempt = _case("accepted", "completed", "active", "series")
    attempt["finalize"] = {
        "feedback_source_upload": {"status": "dispatching", "source_id": None}
    }
    attempt["retraction"] = {"source_cleanup_unresolved": True, "stale_source_ids": []}
    episode.pop("active_attempt_id", None)

    caps = p._attempt_capabilities(episode, attempt, "att-me", post_retract=True)

    assert caps["source_cleanup_unresolved"] is True
    assert caps["cleanup_state"] == "reconcile_after"
    assert caps["safe_next_action"] is None
    step = p._attempt_next_step(caps)
    assert "沒人記得" in step and "候選窗" in step

    # **gate 撈到候選之後,retract 的冪等回傳要跟著改口。** 那些 id 只會進 episode 的
    # `pending_source_cleanup`(tombstone 不回寫,見 ADR-0009),所以只看
    # `stale_source_ids ∩ pending` 會永遠算不出 `pending_delete` —— retract 說「等窗關」、
    # 生成 gate 同時拿著具體 id 要人刪,兩個欄位對同一狀態指向不同動作。
    episode["pending_source_cleanup"] = ["src-orphan"]
    assert attempt["retraction"]["stale_source_ids"] == []  # tombstone 沒有被改寫
    caps = p._attempt_capabilities(episode, attempt, "att-me", post_retract=True)
    assert caps["cleanup_state"] == "pending_delete"
    assert caps["source_cleanup_unresolved"] is True   # 窗還沒關,義務仍未全部結案
    assert caps["safe_next_action"] == p.ACTION_SOURCE_DELETE
    assert "source_delete" in p._attempt_next_step(caps)

    # 義務結案(gate 對帳過)之後,重生才回到指引裡。
    attempt["retraction"].pop("source_cleanup_unresolved")
    episode.pop("pending_source_cleanup")
    caps = p._attempt_capabilities(episode, attempt, "att-me", post_retract=True)
    assert caps["cleanup_state"] is None
    assert caps["safe_next_action"] == caps["regeneration_entry"]


@pytest.mark.parametrize(
    "dispatch,remote,role,shape,window_closed,candidate_selection_required,"
    "upload_status",
    ALL_CASES_WITH_WINDOW,
)
def test_every_state_combination_yields_executable_guidance(
    dispatch,
    remote,
    role,
    shape,
    window_closed,
    candidate_selection_required,
    upload_status,
):
    """**核心不變式:訊息教的每一個動作,在那個狀態下都必須真的做得到。**

    這一條直接對應五次現形的共同形狀。它不預設哪一格會錯 —— 把 next_step 的文字與
    capabilities 對照,教了做不到的事就紅。

    `window_closed` 是 P2 修復加的維度:候選窗關了之後,`podcast_episode_reconcile`
    這個字不准再出現在指引裡。**這裡故意不只做自證**(`if 提到 reconcile: assert
    can_reconcile`)——把 `can_reconcile` 算式裡 `and not reconciliation_window_closed`
    拿掉的突變,`caps["can_reconcile"]` 會跟著訊息一起變成 True,自證測不出來;
    下面直接拿 `window_closed` 這個輸入去斷言才抓得到。

    **`None`(第四輪修復加的第三態)額外斷言「不講窗」**:沒算過窗的呼叫端,訊息裡
    不准出現「候選窗」三個字——這是不變式抓不到的那種 bug(訊息本身自洽、可執行,
    但對「現在幾點」做了一個沒有計算過的宣稱,會跟真的算過窗的另一支工具打對台)。

    `candidate_selection_required` 是 F2 修復加的維度:在這輪盲審之前，整個檔案
    從沒餵過 `True`，`podcast_attempt_adopt` 這個字面值完全沒有不變式守著。
    """
    episode, attempt = _case(dispatch, remote, role, shape)
    if upload_status is not None:
        attempt["finalize"] = {
            "feedback_source_upload": {"status": upload_status, "source_id": None}
        }
    caps = p._attempt_capabilities(
        episode,
        attempt,
        "att-me",
        reconciliation_window_closed=window_closed,
        candidate_selection_required=candidate_selection_required,
    )
    step = p._attempt_next_step(caps)
    assert caps["feedback_upload_unresolved"] is (upload_status is not None)
    if upload_status is not None and role == "active":
        # **F2 回歸鎖:未結案的 upload 不准把 retract 講成做不到。** 舊版在這一格回
        # 「retract 暫時不可用,abandon_in_flight 也不能越過」——而那條路才是唯一出口。
        assert caps["needs_abandon_flag"] is (caps["authorization_basis"] is None)
        assert "不可用" not in step

    if "原樣重呼" in step:
        assert caps["can_resend"], (
            f"教了原樣重送,但這個狀態送不了({dispatch}/{remote}/{role}):{step}"
        )
    if "podcast_attempt_adopt" in step and "候選 artifact 的歸屬" in step:
        assert caps["candidate_selection_required"] and caps["can_reconcile"], (
            f"教了 adopt,但候選歸屬不需要外部知識或這個狀態對不了帳:{step}"
        )
    if "podcast_episode_resume" in step:
        assert caps["can_resume"], f"教了 resume 但沒有 artifact 可續:{step}"
    if "podcast_episode_reconcile" in step:
        assert caps["can_reconcile"], f"教了 reconcile 但這個狀態對不了帳:{step}"
        assert not window_closed, f"候選窗已經關了,卻還教 reconcile:{step}"
        if window_closed is None:
            assert "候選窗" not in step, (
                f"沒算過窗(None),卻在訊息裡宣稱窗狀態:{step}"
            )
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
    # T10(測試債):改引用具名常數而不是手抄一份 tuple——守門的觸發條件（`_NEVER_
    # DISPATCHED`/`_TERMINAL_REMOTE`）哪天前移，這裡若還是手抄字面值就會靜默 skip
    # 掉，不會紅。
    guard_trips = dispatch in p._NEVER_DISPATCHED or remote in p._TERMINAL_REMOTE
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


@pytest.mark.parametrize("dispatch,remote,role,shape", ALL_CASES)
def test_can_resend_hint_never_forbids_the_source_change_it_just_offered(
    dispatch, remote, role, shape
):
    """**P2 修復:can_resend 分支的自相矛盾回歸。**

    v0.9.9 讓 `_attempt_next_step` 的 can_resend 分支同時說「要換來源就先 retract
    重生」+「重生時**必須帶回原本那組** `source_ids`」——前半刻意允許換來源,後半又
    要求來源不能換,是同一句話裡的矛盾指引。can_resend 為真時,`source_ids` 那句的
    正確措辭是區分兩種意圖(只換 brief 保留原樣;刻意換來源就走 retract+新
    source_ids),不能無條件講死「必須帶回原本」。
    """
    episode, attempt = _case(dispatch, remote, role, shape)
    caps = p._attempt_capabilities(episode, attempt, "att-me")
    step = p._attempt_next_step(caps)

    if caps["can_resend"] and "要換 brief 或來源就先" in step:
        assert "必須帶回原本那組" not in step, (
            f"can_resend 分支已經提供「換來源」的選項,不該同時講死"
            f"「必須帶回原本」自相矛盾:{step}"
        )


@pytest.mark.parametrize("dispatch,remote,role,shape", ALL_CASES)
def test_settled_and_output_still_require_the_original_sources_back(
    dispatch, remote, role, shape
):
    """**can_resend 為假時,`source_ids` 的警告不能被 P2 修復連帶弱化。**

    只有 can_resend 分支的措辭要改;is_output／settled 分支只有 retract 重生一條路,
    「必須帶回原本那組 source_ids」仍然是唯一正確的話,不能被一起改成「兩種情境都可
    以」的模糊講法(那對這兩個分支是假的——它們根本沒有「原樣重送」這個選項)。
    """
    episode, attempt = _case(dispatch, remote, role, shape)
    caps = p._attempt_capabilities(episode, attempt, "att-me")
    hint = caps["regeneration_hint"]
    settings = attempt.get("settings") or {}

    if not caps["can_resend"] and settings.get("source_ids"):
        assert "必須帶回原本那組" in hint, (
            f"can_resend 為假、attempt 指名了來源,理應維持「必須帶回原本」的措辭:{hint}"
        )


@pytest.mark.parametrize(
    "dispatch,remote,role,shape,window_closed,candidate_selection_required,"
    "upload_status",
    ALL_CASES_WITH_WINDOW,
)
def test_safe_next_action_agrees_with_the_tool_the_message_actually_teaches(
    dispatch,
    remote,
    role,
    shape,
    window_closed,
    candidate_selection_required,
    upload_status,
):
    """**P2 修復:`_attempt_capabilities()` 直接產生 `safe_next_action`。**

    這條把它跟 `_attempt_next_step()` 挑的分支對照鎖住——兩個函式各自算「哪支工具」
    跟「怎麼講」,答案不准分岔(那正是 `podcast_episode_reconcile` 零候選出口原本要
    自己手寫 if/else 的原因:沒有單一事實來源可用)。

    **F2 修復**:`candidate_selection_required` 這個維度加進來之前,這條測試從沒
    驗過 `caps["safe_next_action"] == p.ACTION_ADOPT` 那一格——`_attempt_next_step()`
    原本把這個分支排在 `can_resume` 之後,跟這裡（=`_attempt_capabilities()`）的
    優先序相反,兩個函式對同一顆 caps 指向不同工具卻沒有任何測試看得到。
    """
    episode, attempt = _case(dispatch, remote, role, shape)
    if upload_status is not None:
        attempt["finalize"] = {
            "feedback_source_upload": {"status": upload_status, "source_id": None}
        }
    caps = p._attempt_capabilities(
        episode,
        attempt,
        "att-me",
        reconciliation_window_closed=window_closed,
        candidate_selection_required=candidate_selection_required,
    )
    action = caps["safe_next_action"]
    assert caps["feedback_upload_unresolved"] is (upload_status is not None)

    if not caps["is_active"] and not caps["is_output"]:
        assert action is None, "歷史紀錄沒有可執行的下一步"
        return
    assert action is None or action in p.SAFE_NEXT_ACTIONS or action == p.ACTION_EPISODE
    if caps["feedback_upload_unresolved"]:
        # 優先序:歷史紀錄(上面已 return)→ upload 未結案 → is_output → ……。續完是
        # 唯一能把那筆 source 的身分認回來的路,所以排在其他建議之前。
        assert action == (
            p.ACTION_RESUME
            if caps["can_resume"]
            else p.ACTION_RECONCILE if caps["can_reconcile"] else None
        )
        if action == p.ACTION_RESUME:
            assert "podcast_episode_resume" in p._attempt_next_step(caps)
        return
    if caps["is_output"]:
        assert action == p.ACTION_RETRACT
    elif caps["authorization_basis"] == "output_owner":
        # P1:active≠output 分岔(自己是 active,但另有一顆不同的 output)——can_resend
        # /can_resume/can_reconcile 對這格可能仍算出 True,但那三條路都會被既有 output
        # guard 擋下來,safe_next_action 必須無條件指向先 retract 自己,優先序排在
        # 它們前面(見 `_attempt_capabilities` 裡 `elif basis == "output_owner":` 那格)。
        assert action == p.ACTION_RETRACT
    elif caps["can_resend"]:
        assert action == caps["regeneration_entry"], (
            "can_resend 為真時,safe_next_action 該是原樣重呼的那支工具"
        )
    elif caps["authorization_basis"] == "settled":
        assert action == p.ACTION_RETRACT
    elif caps["candidate_selection_required"] and caps["can_reconcile"]:
        # F2:必須排在 `can_resume` 之前,跟 `_attempt_capabilities()` 的優先序對齊
        # ——這個 elif 的順序本身就是斷言的一部分（`_attempt_next_step()` 若排錯，
        # 下面 `_attempt_next_step` 對照測試會抓到，這裡先鎖住 `safe_next_action` 值）。
        assert action == p.ACTION_ADOPT
    elif caps["can_resume"]:
        assert action == p.ACTION_RESUME
    elif caps["can_reconcile"]:
        assert action == p.ACTION_RECONCILE
    else:
        assert action == p.ACTION_RETRACT

    # **這條測試的名字要求驗證「文字教的工具」,不是只驗 `safe_next_action` 這個值。**
    # F2 的實際 regression 在 `_attempt_next_step()` 那邊——兩個函式各自算，這裡直接
    # 對照文字裡點名的工具跟 `action` 一致，才是名字承諾的那件事。
    step = p._attempt_next_step(caps)
    if action == p.ACTION_ADOPT:
        assert "podcast_attempt_adopt" in step, (
            f"safe_next_action 是 adopt,文字卻沒教這支工具:{step}"
        )
        assert "podcast_episode_resume" not in step, (
            f"adopt 優先序更高，文字不該再教 resume:{step}"
        )
    elif action == p.ACTION_RESUME:
        assert "podcast_episode_resume" in step, step


@pytest.mark.parametrize("dispatch,remote,role,shape", ALL_CASES)
def test_window_closed_narrative_never_names_the_tool_it_just_ruled_out(
    dispatch, remote, role, shape
):
    """**P2 修復:候選窗關閉的說明文字由 `_attempt_next_step()` 產生,不是呼叫點手寫。**

    只在 dispatch 落在可對帳的狀態集合、但呼叫端已經算出候選窗關了時才會走到這句;
    這句話**不准提到 `podcast_episode_reconcile`**(那個工具名不准在窗關了之後
    出現在指引裡,跟核心不變式測試的 window_closed 斷言同一條紅線)。
    """
    # T10(測試債):改引用 SUT 的具名常數，不再手抄一份 tuple —— 手抄的那份哪天
    # 跟 `can_reconcile` 的判準前移／改了範圍，這裡會靜默變成 skip 而不是紅。
    if dispatch not in p._RECONCILABLE_DISPATCH_STATES:
        pytest.skip("這個 dispatch 狀態走不到窗關閉分支")
    episode, attempt = _case(dispatch, remote, role, shape)
    caps = p._attempt_capabilities(
        episode, attempt, "att-me", reconciliation_window_closed=True
    )
    if (
        caps["can_resend"]
        or caps["is_output"]
        or caps["authorization_basis"] == "settled"
        or caps["can_resume"]
        # P1:active≠output 分岔(`authorization_basis == "output_owner"` 但
        # `is_output` 為 False)也是更高優先序的分支——見
        # `test_safe_next_action_agrees_with_the_tool_the_message_actually_teaches`
        # 同一顆 caps 的對應斷言。
        or caps["authorization_basis"] == "output_owner"
    ):
        pytest.skip("更高優先序的分支先接手,不會走到窗狀態說明")
    if not caps["is_active"] and not caps["is_output"]:
        pytest.skip("歷史紀錄分支不會走到窗狀態說明")
    step = p._attempt_next_step(caps)

    assert "已經關了" in step, step
    assert "podcast_episode_reconcile" not in step, step


def test_window_closed_narrative_reaches_the_assertion_on_exactly_fifteen_cases():
    """T11(測試債):上面那支參數化測試掛了 600 格(`ALL_CASES`),但三層 skip
    (dispatch 不在可對帳集合、更高優先序分支先接手、歷史紀錄分支)吸收掉了
    絕大多數——只有 15 格真的跑到最後兩條斷言。這本身不是問題(skip 條件都有
    各自的理由),但代表：**優先序的回歸如果剛好把某一格從「會走到斷言」變成
    「被前面的 skip 條件吸收」,測試套件看到的是 0 failed,不是紅。**(突變實測:
    把 `_TERMINAL_REMOTE` 拿掉一項,`test_window_closed_narrative_…` 的 skip
    數量與 pass 數量都會變,但這個變化本身不會被任何斷言擋下——它只會讓某些格
    從「執行斷言」變成「被 skip」或反過來,而 parametrize 的 skip 不算 failure。)

    這支非參數化的 meta 測試把「真的跑到斷言的格數」釘死:複製同一套 skip 條件
    但用 `continue` 取代 `pytest.skip`,數出真正執行到底的格數,斷言等於 15。
    這個數字一變,不管是往上(某個 skip 條件變鬆)還是往下(變嚴),都代表上面
    那支測試的覆蓋範圍動了,要重新確認是不是故意的。
    """
    reached = 0
    for dispatch, remote, role, shape in ALL_CASES:
        if dispatch not in p._RECONCILABLE_DISPATCH_STATES:
            continue
        episode, attempt = _case(dispatch, remote, role, shape)
        caps = p._attempt_capabilities(
            episode, attempt, "att-me", reconciliation_window_closed=True
        )
        if (
            caps["can_resend"]
            or caps["is_output"]
            or caps["authorization_basis"] == "settled"
            or caps["can_resume"]
            or caps["authorization_basis"] == "output_owner"
        ):
            continue
        if not caps["is_active"] and not caps["is_output"]:
            continue
        reached += 1

    assert reached == 15


@pytest.mark.parametrize("dispatch,remote,role,shape", ALL_CASES)
def test_the_default_reconciliation_window_state_is_unevaluated_not_open(
    dispatch, remote, role, shape
):
    """**第四輪修復的鑑別測試:預設值必須是 `None`(沒算過),不是 `False`(已確認未關)。**

    `_attempt_capabilities()` 有五個呼叫點,只有 `podcast_episode_reconcile` 真的
    讀時鐘算過候選窗;另外四個(attempt 建立衝突、resume 的兩個停點、
    `_reuse_frozen_input_attempt` 卡在 frozen 重呼)手上根本沒有 `dispatched_at`,
    全部吃預設值。實跑復現的矛盾:attempt 停在 acceptance_unknown、`dispatched_at`
    是 3 天前 —— 用預設值的四個呼叫點若把「沒算過」講成「候選窗還沒關」,會跟真的
    算過窗、判定「已經關了」的 `podcast_episode_reconcile` 對同一顆 attempt 打對台。

    這裡只鎖預設值本身;「沒算過窗的訊息不准講窗宣稱」這條不變式已經在
    `test_every_state_combination_yields_executable_guidance` 的 `window_closed=None`
    分支測過(逐格覆蓋,含優先序更高的分支會先接手的狀態),不在這裡重複斷言逐字文案。

    突變驗證:把 `_attempt_capabilities` 的 `reconciliation_window_closed` 參數預設值
    改回 `False`,這條就會紅。
    """
    episode, attempt = _case(dispatch, remote, role, shape)
    caps = p._attempt_capabilities(episode, attempt, "att-me")  # 不傳 → 預設值
    assert caps["reconciliation_window_closed"] is None, (
        "預設值必須是 None(沒算過),不是 False(已確認未關)"
    )


@pytest.mark.parametrize("dispatch,remote,shape", [
    (d, r, s) for d in DISPATCH_STATES for r in REMOTE_STATES for s in SETTINGS_SHAPES
])
def test_active_not_output_narrative_teaches_retracting_itself_not_resume_or_resend(
    dispatch, remote, shape
):
    """**P1(Codex adversarial review 實跑驗證):`_attempt_next_step` 的文字要跟著
    `safe_next_action` 的新分支走,不能只改一邊。**

    Codex 實跑用 helper 查這格(active=B、output=A)的答案:查 A(output)→
    `podcast_attempt_retract`,但實際 retract A 會被 active guard 拒(要求先處理
    active B);查 B(active)→ can_resume/can_reconcile 可能算出 True 而指向
    resume/reconcile,但實際會被 `_ensure_resume_attempt` 的既有 output guard 拒收。
    正確且唯一的出口是先 retract B 自己。

    這條直接鎖 `_attempt_next_step` 產生的文字(不只是 `safe_next_action` 那個
    工具名欄位)——把這裡的分支拿掉,文字會落回 can_resend/can_resume/can_reconcile
    分支,教出 resume/reconcile/原樣重呼這些會被既有 output 擋下來的死路,而純不變式
    測試(`test_every_state_combination_yields_executable_guidance`)照不到這個盲區
    (can_resume/can_resend 這些 caps 本身在這格可能就是 True,自洽,但系統性是錯的
    ——跟 AGENTS.md 記錄的「獨立複審用兩個突變證明的盲區」同一個形狀)。

    突變驗證:把 `_attempt_next_step` 裡 `caps["authorization_basis"] ==
    "output_owner"` 那個分支拿掉,這條就會紅(而純不變式測試全綠,抓不到)。
    """
    episode, attempt = _case(dispatch, remote, "active_not_output", shape)
    caps = p._attempt_capabilities(episode, attempt, "att-me")
    step = p._attempt_next_step(caps)

    assert "podcast_attempt_retract" in step, step
    assert "podcast_episode_resume" not in step, step
    assert "podcast_episode_reconcile" not in step, step
    assert "原樣重呼" not in step, step


def test_window_open_narrative_names_the_closure_window_not_the_candidate_window():
    """**P1(Codex adversarial review 實跑驗證):兩個窗上一輪才刻意拆開,文案不准
    再把它們合併。**

    `can_reconcile` 依據的是「關閉判斷窗」(`reconciliation_window_closed`,含
    `_RECONCILIATION_MIN_WINDOW` 1 小時保守下限),`podcast_episode_reconcile` 篩選
    候選用的是另一個「候選窗」(`promised` 原始值,不套下限)。Codex 實跑抓到的反例:
    `promised=60`、dispatch 在 1500 秒前、`wait_timeout=1` 重呼——候選窗早在 120 秒
    就關了,但 1 小時的關閉判斷窗還沒關,舊文案卻回「候選窗還沒關」,把兩個窗混成
    一個講。這裡直接餵「明確算過、窗還沒關」的三態值(`False`),鎖住訊息不准再講
    「候選窗」,必須改講它實際依據的關閉判斷窗。

    突變驗證:把 `_attempt_next_step` 這句話的措辭改回「候選窗還沒關」就會紅。
    """
    episode, attempt = _case("dispatching", None, "active", "series")
    caps = p._attempt_capabilities(
        episode, attempt, "att-me", reconciliation_window_closed=False
    )
    step = p._attempt_next_step(caps)

    assert "候選窗" not in step, step
    assert "關閉判斷窗" in step, step


def test_window_closed_narrative_offers_the_widen_wait_timeout_rescue_not_an_absolute_claim():
    """**P1(Codex adversarial review 實跑驗證):窗關了之後不准再宣稱「重呼必然
    相同」——那句話否定了指引自己提供的救援路徑。**

    Codex 實跑對比:`promised=60`、dispatch 在 7200 秒前、artifact 建於
    dispatch+5000 秒——第一次用 `wait_timeout=1` 得到這句話並宣稱「重呼會得到一模
    一樣的回傳」,第二次用本 commit 自己定義的救援值 `wait_timeout=10000` 重呼,
    同一顆 artifact 立刻被綁定並回 `podcast_episode_resume`。舊文案的「未來任何
    artifact 都會落在窗外」只在**沿用同一個 wait_timeout** 時成立,寫成無條件宣稱
    就是自我矛盾。

    突變驗證:把「未來任何 artifact 都會落在窗外」這句絕對宣稱放回去,或拿掉
    「放大 wait_timeout」的揭露,這條就會紅。
    """
    episode, attempt = _case("dispatching", None, "active", "series")
    caps = p._attempt_capabilities(
        episode, attempt, "att-me", reconciliation_window_closed=True
    )
    step = p._attempt_next_step(caps)

    assert "候選窗" not in step, step
    assert "已經關了" in step, step
    assert "未來任何 artifact 都會落在窗外" not in step, step
    assert "放大" in step and "wait_timeout" in step, (
        f"沒有揭露放大 wait_timeout 的救援路徑:{step}"
    )


def test_reconciliation_ambiguous_with_a_stray_remote_artifact_still_teaches_adopt():
    """**F2(獨立盲審實跑探針):`safe_next_action` 與 `_attempt_next_step()` 對同一顆
    caps 指向不同工具。**

    探針:`dispatch=reconciliation_ambiguous`、`remote.artifact_id="art-1"`、
    `candidate_selection_required=True`。修復前:`safe_next_action` 是
    `podcast_attempt_adopt`,但 `next_step` 教「先 podcast_episode_resume 續完
    finalize」——同一份回傳的兩個欄位教不同的工具,只讀其中一個欄位的 host 會被
    另一個欄位誤導。

    `_mark_reconciliation_ambiguous`(候選分岔唯一產生點)禁止 `remote.artifact_id`
    已存在時再標記 ambiguous,所以這個精確組合單 process 不可達——但順序不一致
    本身是明確的契約違反,這裡直接餵這個組合驗證兩個函式的優先序一致。

    突變驗證:把 F2 修復(`_attempt_next_step()` 裡 `candidate_selection_required`
    分支挪回 `can_resume` 之後)還原,這條就會紅。
    """
    episode, attempt = _case("reconciliation_ambiguous", "pending", "active", "series")
    caps = p._attempt_capabilities(
        episode, attempt, "att-me", candidate_selection_required=True
    )
    step = p._attempt_next_step(caps)

    assert caps["safe_next_action"] == p.ACTION_ADOPT, caps
    assert "podcast_attempt_adopt" in step, step
    assert "podcast_episode_resume" not in step, (
        f"safe_next_action 教 adopt,next_step 卻教 resume,兩個欄位互相矛盾:{step}"
    )


def test_a_retracted_prepared_attempt_with_pinned_sources_is_not_taught_to_retract_itself_again():
    """**F3(獨立盲審):`resend_possible=can_resend and not post_retract` 這個 guard
    零覆蓋——盲審實測拿掉 `and not post_retract` 之後全套 8785 仍然全綠。**

    `can_resend` 不看 retraction 狀態,只看 `never_dispatched and not is_output`——
    一顆已經 retract 的 `prepared`/`not_accepted`、帶 `source_ids` 的 attempt,
    `can_resend` 照樣算出 `True`。若把 `post_retract` 這半個條件拿掉,
    `_regeneration_hint()` 會教它「若刻意更換來源,先 podcast_attempt_retract
    (不需要 abandon_in_flight)後帶新的 source_ids」——教一顆已經是 tombstone 的
    attempt 去 retract 自己,正是 v0.9.6 FINDING-4 的形狀。

    突變驗證:把 `_attempt_capabilities()` 裡
    `resend_possible=can_resend and not post_retract` 改成
    `resend_possible=can_resend`,這條就會紅。
    """
    episode, attempt = _case("prepared", None, "active", "pinned")
    caps = p._attempt_capabilities(episode, attempt, "att-me", post_retract=True)

    assert caps["can_resend"] is True, "前提:can_resend 真的不看 retraction 狀態"
    hint = caps["regeneration_hint"]
    assert "必須帶回原本那組" in hint, hint
    assert "podcast_attempt_retract" not in hint, (
        f"教一顆已經是 tombstone 的 attempt 去 retract 自己:{hint}"
    )


def test_post_retract_defers_to_the_replacement_attempt_already_in_flight():
    """**F1(獨立盲審 P1 regression,發版後現形):post_retract 分支從不看
    `episode["active_attempt_id"]`,永遠回 `regeneration_entry`。**

    端到端重現見 ``tests/test_attempt_retract.py`` 的對應測試;這裡直接構造同一個
    episode 形狀做鑑別測試:A 已 retract、B 是 `active_attempt_id` 指向的替代版
    (重生成功但 response lost,停在 `acceptance_unknown`,還沒 promote 成
    output)。修復前這裡永遠回 `regeneration_entry`(這個 settings 形狀下是
    `podcast_series`),照做會撞 `already has durable active attempt`——
    `_create_audio_attempt`／series 一看到 `active_attempt_id` 已經指向別顆就直接
    拒收。`source_delete` 是冪等的,所以舊版教的「先清 stale source 再重呼」在這裡
    走不通不是因為清理沒做,是 regeneration_entry 本身在這個狀態下就是死路。

    突變驗證:把 `_attempt_capabilities()` 裡 `elif replacement_caps is not None:`
    這個分支拿掉,這條就會紅。
    """
    attempt_a = {
        "attempt_id": "att-a",
        "dispatch": {"status": "accepted"},
        "remote": {"status": "completed", "artifact_id": "art-a"},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None},
        "retraction": {"stale_source_ids": []},
    }
    attempt_b = {
        "attempt_id": "att-b",
        "dispatch": {"status": "acceptance_unknown"},
        "remote": {},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None},
    }
    episode = {
        "episode": 1,
        "active_attempt_id": "att-b",
        "attempts": [attempt_a, attempt_b],
    }

    caps = p._attempt_capabilities(episode, attempt_a, "att-a", post_retract=True)

    assert caps["safe_next_action"] != p.ACTION_SERIES, caps
    assert caps["safe_next_action"] == p.ACTION_RECONCILE, caps
    step = p._attempt_next_step(caps)
    assert "用 podcast_series 重生" not in step, step
    assert "podcast_episode_reconcile" in step, step
    # P1(這一輪,同一根因第十一次現形):`safe_next_action` 換成了 B 的,但目標身分
    # 若還是 A(`attempt_id` 參數本身),`podcast_episode_reconcile(A)` 會撞
    # tombstone。目標必須是 B。
    assert caps["safe_next_attempt_id"] == "att-b", caps
    assert caps["safe_next_attempt_id"] != "att-a", caps


def test_post_retract_replacement_identity_wins_even_when_the_replacement_is_settled():
    """**P1(Codex 獨立審查,同一根因第十一次現形)最硬的同型**:B 的 `remote.status`
    是 `failed`(結果已定),`safe_next_action` 會是 `podcast_attempt_retract`——若
    目標身分不是 B、仍是這顆(A)自己,呼叫端照做就是**冪等 retract A、拿到一模
    一樣的回傳、無限迴圈**(Codex 實跑重現)。上一輪(F1)只把 `safe_next_action`
    換成 B 的,`attempt_id` 這個參數本身(=A)完全沒有跟著換。

    突變驗證:把 `_safe_next_target()` 裡「sibling 交棒時身分整段換成替代 attempt
    自己的」那個分支拿掉(退化成永遠用呼叫者自己的 `attempt_id`/`remote`),這條就
    會紅 —— `safe_next_attempt_id` 會變回 `"att-a"`。
    """
    attempt_a = {
        "attempt_id": "att-a",
        "dispatch": {"status": "accepted"},
        "remote": {"status": "completed", "artifact_id": "art-a"},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None},
        "retraction": {"stale_source_ids": []},
    }
    attempt_b = {
        "attempt_id": "att-b",
        "dispatch": {"status": "accepted"},
        "remote": {"status": "failed", "artifact_id": None},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None},
    }
    episode = {
        "episode": 1,
        "active_attempt_id": "att-b",
        "attempts": [attempt_a, attempt_b],
    }

    caps = p._attempt_capabilities(episode, attempt_a, "att-a", post_retract=True)

    assert caps["safe_next_action"] == p.ACTION_RETRACT, caps
    assert caps["safe_next_attempt_id"] == "att-b", caps
    assert caps["safe_next_attempt_id"] != "att-a", (
        "目標身分仍是 A 的話,照著回傳冪等 retract A 會拿到一模一樣的回傳"
        f":{caps}"
    )


def test_post_retract_replacement_identity_carries_the_artifact_id_for_resume():
    """**同一根因,resume 分支**:B 已被受理且有 artifact_id(`remote.status` 還沒
    終態),`safe_next_action` 是 `podcast_episode_resume`——該工具認的是
    `artifact_id`,不是 `attempt_id`。目標身分必須整組換成 B 的
    (`safe_next_attempt_id` 與 `safe_next_artifact_id` 都是),否則呼叫端連
    B 的 artifact_id 都拿不到。

    突變驗證:同上,拿掉 `_safe_next_target()` 的 sibling 交棒分支就會紅
    ——`safe_next_artifact_id` 會變成 `None`(A 沒有走 resume 分支,`remote` 是
    `_attempt_capabilities` 這一層自己的 `remote` 變數,不會是 B 的 `art-b`)。
    """
    attempt_a = {
        "attempt_id": "att-a",
        "dispatch": {"status": "accepted"},
        "remote": {"status": "completed", "artifact_id": "art-a"},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None},
        "retraction": {"stale_source_ids": []},
    }
    attempt_b = {
        "attempt_id": "att-b",
        "dispatch": {"status": "accepted"},
        "remote": {"status": "pending", "artifact_id": "art-b"},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None},
    }
    episode = {
        "episode": 1,
        "active_attempt_id": "att-b",
        "attempts": [attempt_a, attempt_b],
    }

    caps = p._attempt_capabilities(episode, attempt_a, "att-a", post_retract=True)

    assert caps["safe_next_action"] == p.ACTION_RESUME, caps
    assert caps["safe_next_attempt_id"] == "att-b", caps
    assert caps["safe_next_artifact_id"] == "art-b", caps


def test_post_retract_delegation_also_swaps_regeneration_source_ids():
    """**同一根因的第十二次現形(v0.9.26 三視角複核確認,實跑重現)。**

    交棒給替代 attempt 時,`safe_next_action` / `safe_next_attempt_id` /
    `safe_next_artifact_id` 三個欄位都已經整組換成 B 的,**只有
    `regeneration_source_ids` 還是從 A 的 settings 讀**。

    後果:host 拒收 A(來源 s1+s2)之後改用 s1+s3 重生成 B,B 撞配額停在
    `not_accepted`;host 冪等重呼 retract(A) 想確認狀態,拿到的
    `regeneration_source_ids` 是 **A 的 s1+s2**。照著帶回去重生,host 那次刻意
    的來源置換被**靜默還原**,而工具全程回報成功。

    突變驗證:把 `_safe_next_target()` 裡交棒那一段的 `regeneration_source_ids`
    拿掉、改回讀 `attempt`,這條就會紅。
    """
    attempt_a = {
        "attempt_id": "att-a",
        "dispatch": {"status": "accepted"},
        "remote": {"status": "completed", "artifact_id": "art-a"},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None,
                     "source_ids": ["s1", "s2"]},
        "retraction": {"stale_source_ids": []},
    }
    attempt_b = {
        "attempt_id": "att-b",
        "dispatch": {"status": "acceptance_unknown"},
        "remote": {},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None,
                     "source_ids": ["s1", "s3"]},
    }
    episode = {
        "episode": 1,
        "active_attempt_id": "att-b",
        "attempts": [attempt_a, attempt_b],
    }

    caps = p._attempt_capabilities(episode, attempt_a, "att-a", post_retract=True)

    # 前提:這確實是交棒的那一格(身分已經換成 B)。
    assert caps["safe_next_attempt_id"] == "att-b", caps
    # 本條要守的:來源集合也必須是 B 的,不是被 retract 的 A 的。
    assert caps["regeneration_source_ids"] == ["s1", "s3"], caps


def test_regeneration_source_ids_follows_the_replacement_even_when_next_action_is_cleanup():
    """**替代 attempt 在飛、但下一步是清理**(`safe_next_action=source_delete`)。

    這一格是獨立複審抓到的:身分(`safe_next_attempt_id`)只在「動作就是替代那顆的」
    時才換,而 `regeneration_source_ids` 原本綁在**同一個** action 條件上,於是這裡
    回被 retract 那顆的來源 —— 而同一份 payload 的 `next_step` 正說著「替代 attempt
    已經在飛,不能再走重生」。**欄位與散文互相矛盾**,照欄位做的自動化拿到的正是這個
    欄位當初要防的靜默還原。

    重生的**意圖**只要有替代版就是它的,與這一步要做什麼無關 —— 所以兩者的條件不同。

    突變驗證:把 `regeneration_source_ids` 綁回 `_safe_next_target()` 的 action 條件,
    這條就會紅(舊寫法在這一格回 `["s1", "s2"]`)。
    """
    attempt_a = {
        "attempt_id": "att-a",
        "dispatch": {"status": "accepted"},
        "remote": {"status": "completed", "artifact_id": "art-a"},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None,
                     "source_ids": ["s1", "s2"]},
        "retraction": {"stale_source_ids": ["src-old"],
                       "source_cleanup_obligations": [{"notebook_id": "nb", "source_id": "src-old"}]},
    }
    attempt_b = {
        "attempt_id": "att-b",
        "dispatch": {"status": "acceptance_unknown"},
        "remote": {},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None,
                     "source_ids": ["s1", "s3"]},
    }
    episode = {
        "episode": 1,
        "active_attempt_id": "att-b",
        "attempts": [attempt_a, attempt_b],
        "pending_source_cleanup": [{"notebook_id": "nb", "source_id": "src-old"}],
    }

    caps = p._attempt_capabilities(episode, attempt_a, "att-a", post_retract=True)

    # 前提:這一格的下一步是清理,**不是**替代那顆的動作 —— 所以身分不換。
    assert caps["safe_next_action"] == p.ACTION_SOURCE_DELETE, caps["safe_next_action"]
    assert caps["safe_next_attempt_id"] is None, caps
    # 但重生的意圖仍然是替代版的,才不會與 next_step 打架。
    assert caps["regeneration_source_ids"] == ["s1", "s3"], caps
    assert "不能再走「重生」" in p._attempt_next_step(caps)


def test_regeneration_source_ids_stays_own_when_there_is_no_replacement():
    """沒有替代版時就是這顆自己的來源集合 —— 擋掉「一律讀 replacement」那種修法。"""
    attempt_a = {
        "attempt_id": "att-a",
        "dispatch": {"status": "accepted"},
        "remote": {"status": "completed", "artifact_id": "art-a"},
        "settings": {"language": "zh", "audio_format": None, "audio_length": None,
                     "source_ids": ["s1", "s2"]},
        "retraction": {"stale_source_ids": []},
    }
    episode = {"episode": 1, "active_attempt_id": "att-a", "attempts": [attempt_a]}

    caps = p._attempt_capabilities(episode, attempt_a, "att-a", post_retract=True)

    assert caps["post_retract_replacement_caps"] is None, caps
    assert caps["regeneration_source_ids"] == ["s1", "s2"], caps
