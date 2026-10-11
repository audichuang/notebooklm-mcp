"""Attempt dispatch 狀態轉換的集中處。

每個函式都是「套在 manifest dict 上的轉換」,設計成在 `ManifestStore.update` 的 mutate
closure **裡面**呼叫(鎖、CAS、落盤仍歸 `manifest_store`,這裡不碰)。過去這些寫入散在
`tools_podcast.py` 十幾個 ad-hoc closure 裡,各自抄一份「綁定 artifact / 標記 dispatch
狀態」的欄位寫入,允許的來源狀態是各處的字面 tuple——補一條路徑、漏另一條的老毛病
(ADR-0009)。現在欄位寫入只有一份,**每個轉換自己的守門條件刻意保持各自原樣**
(它們本來就不同,見各函式 docstring;要不要統一是行為變更,不在這裡做)。

範圍:`dispatch.status` / `remote` / output 指標的轉換(含 claim/reset 這一對)。仍在模組外
的寫入:audio_finalize 的 remote 終態(`remote_terminal` / `remote_completed`)、retract
的 tombstone 與 episode 投影 pop、`_record_dispatch_failover` 的 `dispatch.account`、
`active_attempt_id` 指標、`_create_audio_attempt` 的初始 prepared 骨架。

不放在這裡的東西:
- 指引訊息(`_attempt_capabilities()` / `_attempt_next_step()` 在 `tools_podcast`,
  docs/gotchas-attempt.md 的紅線)——需要它們的守門(promotion 的 legacy 衝突)留在
  呼叫端,本模組只收守門之後的純欄位寫入。
- 鎖內/鎖外算 caps 的時機(F7)——呼叫端各自維持原樣。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from ._failover import describe_refusal

# `can_reconcile` 認的「還可能對帳得到東西」的 dispatch 狀態(T10:單一事實來源)。
RECONCILABLE_DISPATCH_STATES = frozenset(
    {"dispatching", "acceptance_unknown", "reconciliation_ambiguous"}
)

# 預設的 tombstone 拒絕提示;audio_finalize 傳自己的(finalize 場景的下一步不同)。
_DEFAULT_RETRACTED_HINT = "it is history — work on the replacement attempt instead"

# `mark_acceptance_unknown` 不留診斷時的 sentinel(reconcile 的升級路徑從來不 append
# errors[];None 本身可能是合法的 error 物件,所以不能用 None)。
NO_ERROR_RECORD = object()


def find_attempt(
    manifest: dict,
    episode_n: int,
    attempt_id: str,
    *,
    allow_retracted: bool = False,
    retracted_hint: str = _DEFAULT_RETRACTED_HINT,
) -> tuple[dict, dict]:
    """取 (episode, attempt);被 retract 的 attempt 預設拒絕(ADR-0009 tombstone)。

    被 retract 的 attempt 是歷史紀錄,任何 attempt 級操作(reconcile／adopt／resume／
    promote／supersede／finalize)都不得再改它——否則 adopt 之類的工具可以改寫它的
    finalize checkpoint,讓已結案的清理義務指向錯的 source。唯一的例外是
    `podcast_attempt_retract` 自己(冪等重呼要讀得到它),以 `allow_retracted=True` 明示。
    `retracted_hint` 只影響訊息尾巴,拒絕與否不受影響。
    """
    episode = next(
        (ep for ep in manifest["episodes"] if ep.get("episode") == episode_n),
        None,
    )
    if episode is None:
        raise ValueError(f"episode {episode_n} is missing from the manifest")
    attempt = next(
        (row for row in episode.get("attempts", []) if row.get("attempt_id") == attempt_id),
        None,
    )
    if attempt is None:
        raise ValueError(f"attempt {attempt_id!r} is missing from episode {episode_n}")
    if attempt.get("retraction") and not allow_retracted:
        raise ValueError(
            f"attempt {attempt_id!r} was retracted (episode {episode_n}); {retracted_hint}"
        )
    return episode, attempt


def claimed_artifact_ids(manifest: dict, excluding_attempt_id: str) -> set[str]:
    claimed: set[str] = set()
    for episode in manifest["episodes"]:
        for attempt in episode.get("attempts", []):
            if attempt.get("attempt_id") == excluding_attempt_id:
                continue
            remote = attempt.get("remote", {})
            if not isinstance(remote, dict):
                raise ValueError("attempt remote state must be an object")
            artifact_id = remote.get("artifact_id")
            if artifact_id is None:
                continue
            if not isinstance(artifact_id, str) or not artifact_id:
                raise ValueError("claimed remote artifact_id must be a non-empty string")
            claimed.add(artifact_id)
    return claimed


def unresolved_attempt_ids(
    manifest: dict, notebook_id: str, excluding_attempt_id: str
) -> list[str]:
    """同一本 notebook 底下，還有哪些「已送出但還沒被認領」的 attempt(排除自己)。

    回傳值只用來回答「能不能自動綁定」,不是稽核紀錄,所以不需要跨 notebook —— 別本
    notebook 的 attempt 不可能在這本 notebook 的 `artifacts.list` 裡冒出候選。
    """
    unresolved: list[str] = []
    for episode in manifest.get("episodes", []):
        for attempt in episode.get("attempts", []):
            attempt_id = attempt.get("attempt_id")
            if attempt_id == excluding_attempt_id or attempt.get("notebook_id") != notebook_id:
                continue
            remote = attempt.get("remote") or {}
            if remote.get("artifact_id") is not None:
                continue
            dispatch = attempt.get("dispatch") or {}
            if dispatch.get("status") in RECONCILABLE_DISPATCH_STATES:
                unresolved.append(attempt_id)
    return sorted(unresolved)


# ───────────────────────── 「attempt 擁有 artifact X、dispatch 已受理」 ─────────────────────────


def apply_accepted(
    attempt: dict, artifact_id: str, origin: str, *, reset_error: bool = False
) -> None:
    """四個綁定寫入點共用的欄位寫入:dispatch→accepted、remote→pending 並記來源。

    `origin` 是**誰說的**(`remote` = 對帳從伺服器列表撈到、`sdk_heuristic` = 派送回應
    帶的 id、`caller_verified` = 呼叫端指名並驗過),不是另一份實作。`reset_error` 只有
    adopt 傳 True:它是從 `acceptance_unknown` 人工收養,要清掉殘留的 error 欄位;
    其他路徑從來沒清過,維持原樣(已知分歧,保留不修)。
    """
    now = datetime.now(UTC).isoformat()
    attempt["dispatch"]["status"] = "accepted"
    attempt["dispatch"]["accepted_at"] = now
    fields = {
        "artifact_id": artifact_id,
        "status": "pending",
        "status_origin": origin,
        "observed_at": now,
    }
    if reset_error:
        fields.update({"error": None, "error_code": None})
    attempt["remote"].update(fields)


def new_resume_binding(artifact_id: str, now: str) -> tuple[dict, dict]:
    """`podcast_episode_resume` 新建 attempt 用的 (dispatch, remote)——已受理、caller 驗過。

    欄位順序與 `apply_accepted(reset_error=True)` 的結果一致(先建底稿再套同一份寫入),
    但 `dispatched_at` 維持 None、時間用呼叫端傳入的 `now`(整顆 attempt 共用一個時間戳)。
    """
    dispatch = {
        "status": "prepared",
        "artifact_ids_before": [],
        "dispatched_at": None,
        "accepted_at": None,
    }
    remote = {
        "artifact_id": None,
        "status": "unknown",
        "status_origin": None,
        "observed_at": None,
        "error": None,
        "error_code": None,
    }
    attempt = {"dispatch": dispatch, "remote": remote}
    apply_accepted(attempt, artifact_id, "caller_verified", reset_error=True)
    dispatch["accepted_at"] = now
    remote["observed_at"] = now
    return dispatch, remote


def bind_reconciled(manifest: dict, episode_n: int, attempt_id: str, artifact_id: str) -> list[str]:
    """對帳撈到唯一候選時的綁定(origin=remote)。回傳 blocking attempt ids,空 = 已綁。

    不檢查 dispatch 來源狀態(呼叫端 `podcast_episode_reconcile` 在進來前已限定)——
    與 `bind_sdk_heuristic` 的來源狀態白名單刻意不同,保留。
    """
    _, attempt = find_attempt(manifest, episode_n, attempt_id)
    remote = attempt.get("remote")
    if not isinstance(remote, dict) or remote.get("artifact_id") is not None:
        raise ValueError("attempt already has a remote artifact mapping")
    if artifact_id in claimed_artifact_ids(manifest, attempt_id):
        raise ValueError(f"artifact {artifact_id!r} was claimed during reconciliation")
    # F4(盲審 P3):`notebook_id` 的非空字串驗證已經在唯一呼叫端
    # `podcast_episode_reconcile` 進來前由 `_reconciliation_subject` 做過一次
    # ——逐位元組相同的 guard，而 `notebook_id` 是建立 attempt 時寫死、之後不會
    # 被任何 writer 改動的欄位，這裡重驗建構上不可達。不重複驗證，直接沿用。
    notebook_id = attempt.get("notebook_id")
    blocking_attempt_ids = unresolved_attempt_ids(
        manifest, notebook_id, excluding_attempt_id=attempt_id
    )
    if blocking_attempt_ids:
        # RPC 前的 snapshot 只是廉價早退；歸屬會在 await 期間變動，
        # 所以真正的綁定許可必須與 claimed 重驗共用這次原子 update。
        return blocking_attempt_ids
    apply_accepted(attempt, artifact_id, "remote")
    return []


_SDK_BIND_FROM = (
    "dispatching",
    "acceptance_unknown",
    "reconciliation_ambiguous",
    "accepted",
)


def bind_sdk_heuristic(manifest: dict, episode_n: int, attempt_id: str, artifact_id: str) -> None:
    """派送回應帶回 artifact id 時的綁定(origin=sdk_heuristic);同一顆重複綁是 no-op。"""
    _, attempt = find_attempt(manifest, episode_n, attempt_id)
    dispatch = attempt["dispatch"]
    if dispatch["status"] not in _SDK_BIND_FROM:
        raise ValueError(
            f"attempt {attempt_id!r} cannot accept an artifact from "
            f"dispatch state {dispatch['status']!r}"
        )
    existing_artifact_id = attempt["remote"].get("artifact_id")
    if existing_artifact_id not in (None, artifact_id):
        raise ValueError(
            f"attempt {attempt_id!r} is already mapped to another artifact {existing_artifact_id!r}"
        )
    if existing_artifact_id == artifact_id:
        return
    if existing_artifact_id is None and artifact_id in claimed_artifact_ids(manifest, attempt_id):
        raise ValueError(f"artifact {artifact_id!r} is already claimed")
    apply_accepted(attempt, artifact_id, "sdk_heuristic")


def bind_caller_verified(manifest: dict, attempt: dict, attempt_id: str, artifact_id: str) -> None:
    """`podcast_attempt_adopt` 的綁定(origin=caller_verified);唯一會清 error 欄位的一條。

    tombstone 拒絕由呼叫端先走 `find_attempt` 負責(本函式收 attempt 物件)。
    身分驗證(`_validate_adoption_identity`)與 caps 計算留在呼叫端的 mutate 裡,
    維持「caps 在鎖內算」的 F7 時機。
    """
    current_dispatch = attempt["dispatch"]
    current_status = current_dispatch.get("status")
    if current_status not in ("acceptance_unknown", "reconciliation_ambiguous"):
        raise ValueError("attempt reconciliation state changed")
    if current_status == "reconciliation_ambiguous" and artifact_id not in current_dispatch.get(
        "candidate_artifact_ids", []
    ):
        raise ValueError("artifact is no longer an ambiguity candidate")
    if current_status == "acceptance_unknown" and artifact_id in current_dispatch.get(
        "artifact_ids_before", []
    ):
        raise ValueError("artifact was already present before dispatch")
    if artifact_id in claimed_artifact_ids(manifest, attempt_id):
        raise ValueError(f"artifact {artifact_id!r} is already claimed")
    if attempt["remote"].get("artifact_id") is not None:
        raise ValueError("attempt already has a remote artifact mapping")
    apply_accepted(attempt, artifact_id, "caller_verified", reset_error=True)


# ───────────────────────── dispatch 狀態轉換 ─────────────────────────


def mark_acceptance_unknown(
    manifest: dict,
    episode_n: int,
    attempt_id: str,
    error: object = NO_ERROR_RECORD,
) -> None:
    """dispatching → acceptance_unknown;其他狀態 no-op。

    傳 `error` 才會 append 診斷(dispatch 失敗路徑);不傳(`NO_ERROR_RECORD`)是
    reconcile 先把 dispatching 升級成可對帳狀態的路徑——它從來不留 errors[] 紀錄,
    保留。`error` 不一定是例外(可能是 status 物件),一律走 `describe_refusal`。
    """
    _, attempt = find_attempt(manifest, episode_n, attempt_id)
    if attempt["dispatch"].get("status") != "dispatching":
        return
    if error is NO_ERROR_RECORD:
        attempt["dispatch"]["status"] = "acceptance_unknown"
        return
    error_type, message = describe_refusal(error)
    attempt["dispatch"]["status"] = "acceptance_unknown"
    attempt["errors"].append(
        {
            "phase": "dispatch",
            "type": error_type,
            "message": message,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
    )


def mark_not_accepted(manifest: dict, episode_n: int, attempt_id: str, status: object) -> None:
    """dispatching → not_accepted(伺服器明確拒絕、沒有建出任何 artifact);其他狀態 no-op。

    `status` 吃兩種形狀(SDK 錯誤契約在 0.8.0 換了邊):0.7.x 的 `GenerationStatus`
    (讀 `.error`/`.error_code`),或 0.8.0 起同步拒絕 raise 的例外本身。兩條路必須寫進
    同一個終態,否則同一件事會依 SDK 版本落到不同狀態。
    """
    _, attempt = find_attempt(manifest, episode_n, attempt_id)
    if attempt["dispatch"]["status"] != "dispatching":
        return
    now = datetime.now(UTC).isoformat()
    if isinstance(status, BaseException):
        # 例外沒有 .error/.error_code;不轉換的話 manifest 會留下一條只寫著
        # "failed" 的紀錄,把「為什麼被拒」這個唯一有用的資訊丟掉。
        error = str(status) or type(status).__name__
        error_code = type(status).__name__
    else:
        error = getattr(status, "error", None)
        error_code = getattr(status, "error_code", None)
    attempt["dispatch"]["status"] = "not_accepted"
    attempt["remote"].update(
        {
            "status": "failed",
            "observed_at": now,
            "error": error,
            "error_code": error_code,
        }
    )
    attempt["errors"].append(
        {
            "phase": "dispatch",
            "type": "not_accepted",
            "message": error or error_code or getattr(status, "status", "failed"),
            "recorded_at": now,
        }
    )


def mark_reconciliation_ambiguous(
    manifest: dict, episode_n: int, attempt_id: str, candidate_ids: list[str]
) -> None:
    _, attempt = find_attempt(manifest, episode_n, attempt_id)
    remote = attempt.get("remote")
    if not isinstance(remote, dict) or remote.get("artifact_id") is not None:
        raise ValueError("attempt already has a remote artifact mapping")
    if claimed_artifact_ids(manifest, attempt_id).intersection(candidate_ids):
        raise ValueError("candidate claim changed during reconciliation")
    attempt["dispatch"]["status"] = "reconciliation_ambiguous"
    attempt["dispatch"]["candidate_artifact_ids"] = candidate_ids


def reset_for_resend(attempt: dict) -> None:
    """把一個「從未成功 dispatch」的 attempt 就地清回乾淨的 prepared。

    dispatch 與 remote **必須一起**重設:只清 dispatch 會留下 `remote.status="failed"`,
    而 `podcast_series` 看到那個值會判定「該 supersede」,於是明明要沿用的 attempt 被
    換掉。被拒的原因不會遺失 —— 它在 `errors[]` 裡,那份只 append、從不清除。

    兩個呼叫端共用:`rearm_not_accepted`(series 續跑)與 `_create_audio_attempt`
    的同請求沿用分支(podcast_episode 原樣重呼,見 `_is_resendable_same_request`)。
    """
    dispatch = attempt["dispatch"]
    dispatch.update(
        {
            "status": "prepared",
            "artifact_ids_before": [],
            "dispatched_at": None,
            "accepted_at": None,
        }
    )
    dispatch.pop("candidate_artifact_ids", None)
    # 帳號也要 pop:留著會變成**過期值**——manifest 說 A 生的,實際是重送時的 B 送出的。
    # 缺漏至少看得出來,錯的值看不出來(v0.8.0 驗收 F-4)。
    dispatch.pop("account", None)
    # P2(Codex 實跑驗證):`wait_timeout` 是**這次 dispatch** 的持久化承諾(見
    # `_validate_wait_timeout` docstring),reset 回 `prepared` 就代表這次 dispatch
    # 已經不存在了——留著上一輪的值會被下一次對帳誤當成「這次」的窗判準。實跑重現:
    # `wait_timeout=7200` 被拒 → reset 成 prepared → 用 `wait_timeout=60` 重試 →
    # manifest 卻是 `status=prepared`／`dispatched_at=None`／`wait_timeout=7200.0`
    # ——沒有當前 dispatch,卻留著上一輪的窗。**這裡是所有 dispatch-specific 欄位
    # 該清的地方(寫入方是 `claim_prepared`),新增持久化欄位時記得跟上**——這正是「新增欄位卻沒跟著清理義務走」
    # 的典型補一半。
    dispatch.pop("wait_timeout", None)
    attempt["remote"].update(
        {
            "status": "unknown",
            "status_origin": None,
            "observed_at": None,
            "error": None,
            "error_code": None,
        }
    )


def claim_prepared(
    manifest: dict,
    episode_n: int,
    attempt_id: str,
    artifact_ids: list[str],
    account: str | None,
    wait_timeout: float,
) -> bool:
    """prepared → dispatching(取得 dispatch ownership);不是 prepared 回 False。

    與 `reset_for_resend` 是一對:這裡寫入的 dispatch 欄位(`dispatched_at` /
    `wait_timeout` / `account`)就是 reset 要清的那些,新增欄位時兩處一起改。
    `datetime.now` 在 mutate 內呼叫,時機與搬動前相同。
    """
    _, attempt = find_attempt(manifest, episode_n, attempt_id)
    dispatch = attempt["dispatch"]
    if dispatch["status"] != "prepared":
        return False
    dispatch.update(
        {
            "status": "dispatching",
            "artifact_ids_before": artifact_ids,
            "dispatched_at": datetime.now(UTC).isoformat(),
            "wait_timeout": float(wait_timeout),
        }
    )
    if account is not None:
        dispatch["account"] = account
    return True


def rearm_not_accepted(manifest: dict, episode_n: int, attempt_id: str) -> bool:
    """not_accepted → prepared(只在下一次明確呼叫時);其他狀態回 False。"""
    _, attempt = find_attempt(manifest, episode_n, attempt_id)
    if attempt["dispatch"]["status"] != "not_accepted":
        return False
    if attempt["remote"].get("artifact_id") is not None:
        raise ValueError("not_accepted attempt cannot have an artifact mapping")
    reset_for_resend(attempt)
    return True


# ───────────────────────── promotion(守門之後的純欄位寫入) ─────────────────────────


def first_published_at(episode: dict) -> str | None:
    """回頭重生一集時,pubDate 必須是首發時間、不能漂成重生當下(真實事故:
    saa-drill EP05/EP09——GUID 不變＝同集更新,episodic feed 按 pubDate 倒序,漂移
    會讓重生集跳到列表前面;RSS 排序假設 pubDate 隨集號遞增,見 tools_publish.py
    的 fallback 註解)。沿 attempts 建立順序(即 list 的 append 順序)找回第一筆
    非空的 retracted_output.published_at——那是 retract 當時從 episode 級投影搶救
    下來的首發時間。取「第一筆非空」而非「第一筆有 retraction」:
    abandons_unauthorized_candidate 分支(從未 promote 就被作廢的 candidate)留下的
    retracted_output 是空 dict,必須跳過,不能誤判成「這集從沒發過」。"""
    for attempt in episode.get("attempts", []):
        retraction = attempt.get("retraction")
        if not isinstance(retraction, dict):
            continue
        # `or {}` 而非 default {}:手改的 manifest 可能把 retracted_output 寫成 null,
        # default 只在 key 不存在時生效,擋不住顯式 null(本模組的前提就是手改會發生)。
        published_at = (retraction.get("retracted_output") or {}).get("published_at")
        if isinstance(published_at, str) and published_at:
            return published_at
    return None


def apply_promotion(episode: dict, attempt: dict, attempt_id: str, output: dict) -> None:
    """守門通過後,把 attempt 的產出掛成 episode 級 output 並寫回 `output["published_at"]`。

    守門(已 retract／owned-by-another／legacy 不同 artifact)留在
    `tools_podcast._promote_attempt_output`——legacy 那道要用 `_attempt_capabilities`。
    """
    attempt["remote"]["status"] = "completed"
    attempt["remote"]["observed_at"] = datetime.now(UTC).isoformat()
    episode["artifact_id"] = output["artifact_id"]
    episode["mp3_path"] = os.path.abspath(output["mp3_path"])
    episode["output_attempt_id"] = attempt_id
    for key in ("title", "label"):
        episode.setdefault(key, output[key])
    # published_at 不能跟 title／label 一樣用 output 自己的 setdefault:output 裡
    # 那個值是「這次生成完成的時刻」,取代版每一次重生都會不一樣。已驗證的安全
    # 互動(見 `find_attempt` 的 tombstone 與 retract 冪等分支的 ownership guard):
    # 取代版 B 帶著 A 的首發時間上任後,對 A 重打冪等 retract 不會誤刪 B 的投影——
    # 那條 guard 只在 output_attempt_id in (None, attempt_id) 時才動手,B 是現任
    # output 時整段會跳過。
    episode.setdefault("published_at", first_published_at(episode) or output["published_at"])
    # 回寫進 output:`_promote_attempt_output` 的四個呼叫點都是 `…(…, output); return output`,
    # 只改 manifest 會讓**回傳值**仍帶著「這次重生的時刻」——feed 對、呼叫端拿到的
    # 卻是錯的 pubDate(v0.6.0 實測抓到:manifest 12:07:20、回傳值 12:16:39)。
    # 修在這個匯流點,四條路徑一起正確。
    output["published_at"] = episode["published_at"]
    # episode 級 continuity 證據:attempt 裡的 source_id 是主要真相,但 legacy
    # projection 只認得 episode 級欄位。不寫 feedback_source_adopted_at——那是
    # podcast_attempt_adopt 的人工驗證標記,resume 靠它判斷可否沿用舊 source。
    source_id = output.get("feedback_source_id")
    if isinstance(source_id, str) and source_id:
        episode["feedback_source_id"] = source_id
