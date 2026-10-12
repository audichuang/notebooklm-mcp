"""`attempt_state` 的轉換表:from-state × event → to-state 或拒絕(純記憶體 manifest)。

只走公開介面,不經 ManifestStore;落盤/鎖/CAS 另有 test_manifest_store 負責。
"""

import pytest

from notebooklm_mcp import attempt_state as st

DISPATCH_STATES = (
    "prepared",
    "not_accepted",
    "dispatching",
    "acceptance_unknown",
    "accepted",
    "reconciliation_ambiguous",
)


def _manifest(dispatch="dispatching", *, artifact_id=None, retracted=False, others=()):
    attempt = {
        "attempt_id": "a1",
        "notebook_id": "nb",
        "dispatch": {
            "status": dispatch,
            "artifact_ids_before": [],
            "candidate_artifact_ids": ["art-1"],
            "wait_timeout": 60.0,
            "account": "acct",
        },
        "remote": {
            "artifact_id": artifact_id,
            "status": "failed",
            "status_origin": None,
            "observed_at": None,
            "error": "old",
            "error_code": "OLD",
        },
        "errors": [],
    }
    if retracted:
        attempt["retraction"] = {"reason": "x"}
    episodes = [{"episode": 1, "attempts": [attempt]}]
    for n, (aid, art, status) in enumerate(others, start=2):
        episodes.append(
            {
                "episode": n,
                "attempts": [
                    {
                        "attempt_id": aid,
                        "notebook_id": "nb",
                        "dispatch": {"status": status},
                        "remote": {"artifact_id": art},
                        "errors": [],
                    }
                ],
            }
        )
    return {"episodes": episodes}, attempt


def _att(manifest):
    return st.find_attempt(manifest, 1, "a1")[1]


# ── find_attempt ──


def test_find_attempt_refuses_retracted_unless_opted_in():
    manifest, _ = _manifest(retracted=True)
    with pytest.raises(ValueError, match="was retracted .*work on the replacement"):
        st.find_attempt(manifest, 1, "a1")
    with pytest.raises(ValueError, match="generate a replacement instead of finalizing"):
        st.find_attempt(
            manifest, 1, "a1", retracted_hint="generate a replacement instead of finalizing it"
        )
    assert st.find_attempt(manifest, 1, "a1", allow_retracted=True)[1]["attempt_id"] == "a1"


def test_find_attempt_missing_episode_and_attempt():
    manifest, _ = _manifest()
    with pytest.raises(ValueError, match="episode 9 is missing"):
        st.find_attempt(manifest, 9, "a1")
    with pytest.raises(ValueError, match="attempt 'zz' is missing from episode 1"):
        st.find_attempt(manifest, 1, "zz")


# ── dispatch 狀態事件:只有 dispatching 會動,其餘 no-op ──


@pytest.mark.parametrize("state", DISPATCH_STATES)
def test_mark_acceptance_unknown_only_moves_dispatching(state):
    manifest, attempt = _manifest(state)
    st.mark_acceptance_unknown(manifest, 1, "a1", TimeoutError("lost"))
    if state == "dispatching":
        assert attempt["dispatch"]["status"] == "acceptance_unknown"
        assert [e["phase"] for e in attempt["errors"]] == ["dispatch"]
    else:
        assert attempt["dispatch"]["status"] == state
        assert attempt["errors"] == []


@pytest.mark.parametrize("state", DISPATCH_STATES)
def test_mark_acceptance_unknown_without_error_never_appends(state):
    manifest, attempt = _manifest(state)
    st.mark_acceptance_unknown(manifest, 1, "a1")
    assert attempt["dispatch"]["status"] == (
        "acceptance_unknown" if state == "dispatching" else state
    )
    assert attempt["errors"] == []


@pytest.mark.parametrize("state", DISPATCH_STATES)
def test_mark_not_accepted_only_moves_dispatching(state):
    manifest, attempt = _manifest(state)
    st.mark_not_accepted(manifest, 1, "a1", RuntimeError("quota"))
    if state == "dispatching":
        assert attempt["dispatch"]["status"] == "not_accepted"
        assert attempt["remote"]["status"] == "failed"
        assert attempt["remote"]["error"] == "quota"
        assert attempt["remote"]["error_code"] == "RuntimeError"
        assert attempt["errors"][0]["type"] == "not_accepted"
    else:
        assert attempt["dispatch"]["status"] == state
        assert attempt["remote"]["error"] == "old"


@pytest.mark.parametrize("state", DISPATCH_STATES)
def test_rearm_only_from_not_accepted_and_clears_dispatch_and_remote_together(state):
    manifest, attempt = _manifest(state)
    assert st.rearm_not_accepted(manifest, 1, "a1") is (state == "not_accepted")
    if state == "not_accepted":
        assert attempt["dispatch"] == {
            "status": "prepared",
            "artifact_ids_before": [],
            "dispatched_at": None,
            "accepted_at": None,
        }
        assert attempt["remote"]["status"] == "unknown"
        assert attempt["remote"]["error"] is None
    else:
        assert attempt["dispatch"]["status"] == state


def test_rearm_refuses_when_artifact_mapped():
    manifest, _ = _manifest("not_accepted", artifact_id="art-1")
    with pytest.raises(ValueError, match="cannot have an artifact mapping"):
        st.rearm_not_accepted(manifest, 1, "a1")


# ── 綁定:來源狀態白名單 × origin ──


@pytest.mark.parametrize("state", DISPATCH_STATES)
def test_bind_sdk_heuristic_source_states(state):
    manifest, attempt = _manifest(state)
    if state in ("dispatching", "acceptance_unknown", "reconciliation_ambiguous", "accepted"):
        st.bind_sdk_heuristic(manifest, 1, "a1", "art-9")
        assert attempt["dispatch"]["status"] == "accepted"
        assert attempt["remote"]["artifact_id"] == "art-9"
        assert attempt["remote"]["status_origin"] == "sdk_heuristic"
        # 只有 adopt 會清 error 欄位。
        assert attempt["remote"]["error"] == "old"
    else:
        with pytest.raises(ValueError, match="cannot accept an artifact"):
            st.bind_sdk_heuristic(manifest, 1, "a1", "art-9")


def test_bind_sdk_heuristic_is_idempotent_and_rejects_other_or_claimed():
    manifest, attempt = _manifest("dispatching", others=[("b1", "art-claimed", "accepted")])
    with pytest.raises(ValueError, match="already claimed"):
        st.bind_sdk_heuristic(manifest, 1, "a1", "art-claimed")
    st.bind_sdk_heuristic(manifest, 1, "a1", "art-9")
    first_stamp = attempt["dispatch"]["accepted_at"]
    st.bind_sdk_heuristic(manifest, 1, "a1", "art-9")
    assert attempt["dispatch"]["accepted_at"] == first_stamp
    with pytest.raises(ValueError, match="already mapped to another artifact"):
        st.bind_sdk_heuristic(manifest, 1, "a1", "art-10")


@pytest.mark.parametrize("state", DISPATCH_STATES)
def test_bind_caller_verified_source_states_and_error_reset(state):
    manifest, attempt = _manifest(state)
    if state in ("acceptance_unknown", "reconciliation_ambiguous"):
        st.bind_caller_verified(manifest, attempt, "a1", "art-1")
        assert attempt["dispatch"]["status"] == "accepted"
        assert attempt["remote"]["status_origin"] == "caller_verified"
        assert attempt["remote"]["error"] is None
        assert attempt["remote"]["error_code"] is None
    else:
        with pytest.raises(ValueError, match="reconciliation state changed"):
            st.bind_caller_verified(manifest, attempt, "a1", "art-1")


def test_bind_caller_verified_guards():
    manifest, attempt = _manifest("reconciliation_ambiguous")
    with pytest.raises(ValueError, match="no longer an ambiguity candidate"):
        st.bind_caller_verified(manifest, attempt, "a1", "art-other")
    manifest, attempt = _manifest("acceptance_unknown")
    attempt["dispatch"]["artifact_ids_before"] = ["art-1"]
    with pytest.raises(ValueError, match="already present before dispatch"):
        st.bind_caller_verified(manifest, attempt, "a1", "art-1")
    manifest, attempt = _manifest("acceptance_unknown", others=[("b1", "art-1", "accepted")])
    with pytest.raises(ValueError, match="already claimed"):
        st.bind_caller_verified(manifest, attempt, "a1", "art-1")
    manifest, attempt = _manifest("acceptance_unknown", artifact_id="art-0")
    with pytest.raises(ValueError, match="already has a remote artifact mapping"):
        st.bind_caller_verified(manifest, attempt, "a1", "art-1")


def test_bind_reconciled_binds_with_remote_origin_without_state_whitelist():
    # 與 sdk_heuristic 不同:這條不檢查 dispatch 來源狀態(呼叫端已限定),保留。
    manifest, attempt = _manifest("prepared")
    assert st.bind_reconciled(manifest, 1, "a1", "art-9") == []
    assert attempt["remote"]["status_origin"] == "remote"
    assert attempt["remote"]["error"] == "old"


def test_bind_reconciled_returns_blockers_without_writing():
    manifest, attempt = _manifest("dispatching", others=[("b1", None, "dispatching")])
    assert st.bind_reconciled(manifest, 1, "a1", "art-9") == ["b1"]
    assert attempt["remote"]["artifact_id"] is None
    assert attempt["dispatch"]["status"] == "dispatching"


def test_bind_reconciled_refusals():
    manifest, _ = _manifest("dispatching", artifact_id="art-0")
    with pytest.raises(ValueError, match="already has a remote artifact mapping"):
        st.bind_reconciled(manifest, 1, "a1", "art-9")
    manifest, _ = _manifest("dispatching", others=[("b1", "art-9", "accepted")])
    with pytest.raises(ValueError, match="claimed during reconciliation"):
        st.bind_reconciled(manifest, 1, "a1", "art-9")


@pytest.mark.parametrize(
    "call",
    [
        lambda m: st.mark_acceptance_unknown(m, 1, "a1", TimeoutError("x")),
        lambda m: st.mark_not_accepted(m, 1, "a1", RuntimeError("x")),
        lambda m: st.rearm_not_accepted(m, 1, "a1"),
        lambda m: st.bind_sdk_heuristic(m, 1, "a1", "art-9"),
        lambda m: st.bind_reconciled(m, 1, "a1", "art-9"),
        lambda m: st.mark_reconciliation_ambiguous(m, 1, "a1", ["art-9"]),
        lambda m: st.claim_prepared(m, 1, "a1", [], None, 60.0),
        lambda m: st.bind_caller_verified(m, m["episodes"][0]["attempts"][0], "a1", "art-9"),
        lambda m: st.apply_promotion(
            m["episodes"][0], m["episodes"][0]["attempts"][0], "a1", {"artifact_id": "art-9"}
        ),
        lambda m: st.reset_for_resend(m["episodes"][0]["attempts"][0]),
        lambda m: st.apply_accepted(m["episodes"][0]["attempts"][0], "art-9", "remote"),
    ],
    ids=[
        "unknown",
        "not_accepted",
        "rearm",
        "sdk_bind",
        "reconciled_bind",
        "ambiguous",
        "claim_prepared",
        "caller_verified_bind",
        "promotion",
        "reset_for_resend",
        "apply_accepted",
    ],
)
def test_every_transition_refuses_a_retracted_attempt(call):
    manifest, _ = _manifest("dispatching", retracted=True)
    with pytest.raises(ValueError, match="was retracted"):
        call(manifest)


def test_mark_reconciliation_ambiguous_writes_candidates_and_checks_claims():
    manifest, attempt = _manifest("acceptance_unknown")
    st.mark_reconciliation_ambiguous(manifest, 1, "a1", ["art-1", "art-2"])
    assert attempt["dispatch"]["status"] == "reconciliation_ambiguous"
    assert attempt["dispatch"]["candidate_artifact_ids"] == ["art-1", "art-2"]
    manifest, _ = _manifest("acceptance_unknown", others=[("b1", "art-2", "accepted")])
    with pytest.raises(ValueError, match="candidate claim changed"):
        st.mark_reconciliation_ambiguous(manifest, 1, "a1", ["art-1", "art-2"])


# ── resume 新建分支的 binding 與 in-place adopt 寫入同形 ──


def test_new_resume_binding_matches_caller_verified_shape():
    dispatch, remote = st.new_resume_binding("art-7", "2026-01-01T00:00:00+00:00")
    assert list(dispatch) == ["status", "artifact_ids_before", "dispatched_at", "accepted_at"]
    assert dispatch == {
        "status": "accepted",
        "artifact_ids_before": [],
        "dispatched_at": None,
        "accepted_at": "2026-01-01T00:00:00+00:00",
    }
    assert list(remote) == [
        "artifact_id",
        "status",
        "status_origin",
        "observed_at",
        "error",
        "error_code",
    ]
    assert remote == {
        "artifact_id": "art-7",
        "status": "pending",
        "status_origin": "caller_verified",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "error": None,
        "error_code": None,
    }


def test_apply_promotion_writes_pointers_and_echoes_published_at(tmp_path):
    manifest, attempt = _manifest("accepted", artifact_id="art-1")
    episode = manifest["episodes"][0]
    output = {
        "artifact_id": "art-1",
        "mp3_path": str(tmp_path / "e.mp3"),
        "title": "T",
        "label": "EP01 T",
        "published_at": "now",
        "feedback_source_id": "src-1",
    }
    episode["attempts"].append(
        {"attempt_id": "old", "retraction": {"retracted_output": {"published_at": "first"}}}
    )
    st.apply_promotion(episode, attempt, "a1", output)
    assert attempt["remote"]["status"] == "completed"
    assert episode["output_attempt_id"] == "a1"
    assert episode["published_at"] == "first" == output["published_at"]
    assert episode["feedback_source_id"] == "src-1"


def test_every_key_promotion_writes_is_either_output_projection_or_identity(tmp_path):
    """ADR-0009:retract 把 output 投影整批 pop 掉,取代版才能合法重生。

    這條紅了代表 `apply_promotion` 開始寫一個新的 episode 欄位。**正確的反應是決定它是哪一種**,
    不是隨手加進其中一個清單:
      - output 證據(取代版必須重寫它)→ `OUTPUT_PROJECTION_KEYS`,retract 會 pop 它;
      - 集的身分(取代版不得改,如標題)→ `EPISODE_IDENTITY_KEYS`,retract 不碰它。
    漏判成身分 = 作廢後殘留舊值擋住重生或被 setdefault 沿用(published_at 事故的形狀)。
    """
    manifest, attempt = _manifest("accepted", artifact_id="art-1")
    episode = manifest["episodes"][0]
    output = {
        "artifact_id": "art-1",
        "mp3_path": str(tmp_path / "e.mp3"),
        "title": "T",
        "label": "EP01 T",
        "published_at": "now",
        "feedback_source_id": "src-1",
    }
    for key in (*st.OUTPUT_PROJECTION_KEYS, *st.EPISODE_IDENTITY_KEYS):
        episode.pop(key, None)
    before = set(episode)

    st.apply_promotion(episode, attempt, "a1", output)

    written = set(episode) - before
    assert written <= {*st.OUTPUT_PROJECTION_KEYS, *st.EPISODE_IDENTITY_KEYS}
    assert not set(st.OUTPUT_PROJECTION_KEYS) & set(st.EPISODE_IDENTITY_KEYS)
