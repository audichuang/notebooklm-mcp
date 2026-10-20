"""`podcast_series` active 分支的自動轉換表:`_series_redispatch_kind` 是唯一來源。

series 進到一集已有 active attempt 時,要自己決定「re-arm / supersede / 直接送 / 只收尾」。
這張表以前是 inline 的 if 鏈,判準又在 `_series_will_redispatch`、接手守門、洩漏檢查
各抄一份 —— daa6bc2 修的就是其中一份漏了 `accepted`+`failed` 那格。這裡鎖三件事:
與 `_series_will_redispatch` 等價、鏈的優先序與 `after=` 的「只往後走」、以及 series
會自動重生的每一格,caps 都必須免旗標放行 retract(v0.9.4 那一類漂移)。
"""

import itertools

import pytest
from test_attempt_capabilities import _case

from notebooklm_mcp import attempt_guidance as g, tools_podcast as tp
from notebooklm_mcp.attempt_state import RECONCILABLE_DISPATCH_STATES

DISPATCH_STATES = (
    "prepared",
    "not_accepted",
    "dispatching",
    "acceptance_unknown",
    "reconciliation_ambiguous",
    "accepted",
    None,
)
REMOTE_STATES = (None, "pending", "completed", "failed", "removed")
ARTIFACT_IDS = (None, "a1")
GRID = list(itertools.product(DISPATCH_STATES, REMOTE_STATES))


def _attempt(dispatch, remote, artifact_id=None):
    return {
        "dispatch": {"status": dispatch},
        "remote": {"status": remote, "artifact_id": artifact_id},
    }


def _series_case(dispatch, remote):
    return _case(dispatch, remote, "active", "series")


@pytest.mark.parametrize(
    "dispatch,remote,artifact_id",
    list(itertools.product(DISPATCH_STATES, REMOTE_STATES, ARTIFACT_IDS)),
)
def test_kind_matches_will_redispatch(dispatch, remote, artifact_id):
    """T1:kind 非 None ⟺ series 會產生新 dispatch(判準引用具名常數,不手抄)。"""
    attempt = _attempt(dispatch, remote, artifact_id)
    expected = dispatch in g.NEVER_DISPATCHED or remote in g.TERMINAL_REMOTE
    assert (tp._series_redispatch_kind(attempt) is not None) == expected
    assert tp._series_will_redispatch(attempt) == expected


@pytest.mark.parametrize(
    "attempt",
    [{}, {"dispatch": None, "remote": None}, {"dispatch": {}}, {"remote": {"status": None}}],
)
def test_missing_or_null_keys_mean_finalize_path(attempt):
    assert tp._series_redispatch_kind(attempt) is None
    assert tp._series_will_redispatch(attempt) is False


@pytest.mark.parametrize(
    "dispatch,remote,expected",
    [
        ("not_accepted", "failed", "rearm"),
        ("prepared", "failed", "supersede"),
        ("accepted", "removed", "supersede"),
        ("prepared", "pending", "send"),
        ("prepared", None, "send"),
        ("accepted", "completed", None),
    ],
)
def test_chain_priority(dispatch, remote, expected):
    """T2:優先序 = 舊 if 鏈的順序(rearm → supersede → send)。"""
    assert tp._series_redispatch_kind(_attempt(dispatch, remote)) == expected


@pytest.mark.parametrize("dispatch,remote", GRID)
def test_after_only_moves_forward(dispatch, remote):
    """T2:mutation 後的重讀只檢查排在後面的轉換,和舊的循序 if 相同。"""
    attempt = _attempt(dispatch, remote)
    assert tp._series_redispatch_kind(attempt, after="rearm") != "rearm"
    assert tp._series_redispatch_kind(attempt, after="supersede") in ("send", None)


def test_rearm_then_terminal_remote_supersedes_like_the_old_chain():
    """T2 並發邊角:re-arm 後重讀到 not_accepted+failed,舊鏈接著 supersede,不再 re-arm。"""
    attempt = _attempt("not_accepted", "failed")
    assert tp._series_redispatch_kind(attempt, after="rearm") == "supersede"
    assert tp._series_redispatch_kind(attempt, after="supersede") is None


@pytest.mark.parametrize("dispatch,remote", GRID)
def test_every_auto_redispatch_is_a_settled_retract(dispatch, remote):
    """T3:series 自動 retract+重生的每一格,caps 都必須免旗標(且原樣重送的格可重送)。

    ⚠️ 這條紅了,正確的反應是先核對 `podcast_series` 的鏈順序或 `attempt_capabilities`,
    **不是加一列例外**:紅代表 series 自己會做的事,對呼叫端卻要旗標/不准做 —— 兩張表
    已經分歧。刻意不比 `safe_next_action`:active 分支的終態停點回 SERIES、caps 回
    RETRACT,那是現行行為。
    """
    if dispatch is None:
        pytest.skip("caps 不處理缺 dispatch 的 attempt")
    kind = tp._series_redispatch_kind(_attempt(dispatch, remote))
    if kind is None:
        pytest.skip("series 在這格只收尾,不自動重生")
    episode, attempt = _series_case(dispatch, remote)
    caps = g.attempt_capabilities(episode, attempt, "att-me")
    assert caps["authorization_basis"] == "settled", (dispatch, remote, kind)
    if kind in ("rearm", "send"):
        assert caps["can_resend"], (dispatch, remote, kind)


def test_reconcilable_dispatch_states():
    """T4:series 的 reconcile 判準改引用常數,值必須與舊字面 tuple 相同。"""
    assert {
        "dispatching",
        "acceptance_unknown",
        "reconciliation_ambiguous",
    } == RECONCILABLE_DISPATCH_STATES
