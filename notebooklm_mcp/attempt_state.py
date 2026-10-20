"""Attempt dispatch 狀態轉換的集中處。

每個轉換函式都是「套在 manifest dict 上的轉換」,設計成在 `ManifestStore.update` 的 mutate
closure **裡面**呼叫(鎖、CAS、落盤仍歸 `manifest_store`,這裡不碰)。過去這些寫入散在
`tools_podcast.py` 十幾個 ad-hoc closure 裡,各自抄一份「綁定 artifact / 標記 dispatch
狀態」的欄位寫入,允許的來源狀態是各處的字面 tuple——補一條路徑、漏另一條的老毛病
(ADR-0009)。現在欄位寫入只有一份,**每個轉換自己的守門條件刻意保持各自原樣**
(它們本來就不同,見各函式 docstring;要不要統一是行為變更,不在這裡做)。

store-bound 的那一面是 `AttemptRef`:`(store, episode_n, attempt_id)` 一組位址,讀
(`read`)、寫(`mutate`)、各轉換(同名方法)都從它進,`find_attempt` 的 tombstone
default-deny 在它裡面——呼叫端不必再記得「包進 `store.update`、先查 `find_attempt`」。
`tools_podcast` 與 `audio_finalize` 共用它(後者以 `retracted_hint` 換訊息尾巴)。

範圍:`dispatch.status` / `remote` / `dispatch.account` 的轉換(含 claim/reset 這一對與
failover 稽核)。仍在模組外的寫入:audio_finalize 的 remote 終態(`remote_terminal` /
`remote_completed`)與 finalize checkpoint 形狀、retract 的 tombstone 組裝、
`active_attempt_id` 指標、`_create_audio_attempt` 的初始 prepared 骨架。Episode 級 output
投影(鍵集合、promotion、retract 的 pop、述詞)在 `output_projection`。

不放在這裡的東西:
- 指引訊息(`attempt_capabilities()` / `attempt_next_step()` 在 `attempt_guidance`,
  docs/gotchas-attempt.md 的紅線)——需要它們的守門(promotion 的 legacy 衝突)留在
  呼叫端,本模組只收守門之後的純欄位寫入。
- 鎖內/鎖外算 caps 的時機(F7)——呼叫端各自維持原樣(`AttemptRef.mutate` 的 callback
  在鎖內跑,caps 要在鎖內算就放進 callback)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ._errors import describe_refusal
from .manifest_store import ManifestStore

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


def _refuse_retracted(attempt: dict, attempt_id: str | None = None) -> None:
    """吃 raw attempt dict 的轉換點用的 tombstone 守門(與 `find_attempt` 同一個拒絕語意)。

    這些函式的呼叫端都已走過 `find_attempt`;這裡是縱深防禦,讓「每個狀態轉換都拒絕
    被 retract 的 attempt」不依賴呼叫端記得先查(ADR-0009)。
    """
    if attempt.get("retraction"):
        aid = attempt_id or attempt.get("attempt_id")
        raise ValueError(f"attempt {aid!r} was retracted; {_DEFAULT_RETRACTED_HINT}")


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

    為什麼需要:兩顆並行、各自 response lost 的 attempt 候選窗會重疊,窗內出現的 artifact
    可能是「另一顆」的產物,時間窗本身判定不了歸屬(Codex 實跑驗證)。唯一候選出現時,若
    還有其他未解決者就不自動綁定,交給呼叫端明確指名(`podcast_attempt_adopt`)。
    「未解決」= dispatch 狀態在 `RECONCILABLE_DISPATCH_STATES` 且尚未 claim artifact
    (與 `can_reconcile` 共用同一份集合);只有一顆在飛的正常情境不受影響。
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
    _refuse_retracted(attempt)
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

    本函式收 attempt 物件,tombstone 拒絕由 `_refuse_retracted` 在入口補上
    (呼叫端也已先走 `find_attempt`)。
    身分驗證(`_validate_adoption_identity`)與 caps 計算留在呼叫端的 mutate 裡,
    維持「caps 在鎖內算」的 F7 時機。
    """
    _refuse_retracted(attempt, attempt_id)
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


def record_dispatch_failover(
    manifest: dict,
    episode_n: int,
    attempt_id: str,
    reason: object,
    from_account: str | None,
    to_account: str,
) -> None:
    """記下「A 拒絕 → 改用 B 重送」,並把 dispatch 的 account 換成 B。

    **對 client 透明可以,對稽核紀錄不行**(ADR-0010)。errors[] 是唯一只 append、
    從不被清除的欄位 —— `reset_for_resend` 會把 dispatch/remote 清回
    prepared,診斷放那裡會被抹掉,放這裡不會。
    """
    _, attempt = find_attempt(manifest, episode_n, attempt_id)
    # 兩種拒絕形狀的判讀共用 `_errors.describe_refusal`(附件家族的 recorder 也用
    # 同一支)——各寫一份等於上游改了 status 欄位名時只有一處被改到。
    reason_type, message = describe_refusal(reason)
    attempt["dispatch"]["account"] = to_account
    attempt["errors"].append(
        {
            "phase": "dispatch_failover",
            "type": reason_type,
            "message": message,
            "from_account": from_account,
            "to_account": to_account,
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
    的同請求沿用分支(podcast_episode 原樣重呼,見 `attempt_admission.is_resendable_same_request`)。
    """
    _refuse_retracted(attempt)
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


def record_retraction(episode: dict, attempt: dict, attempt_id: str, retraction: dict) -> None:
    """寫下 tombstone(ADR-0009):此後 `find_attempt` 對這顆一律拒絕(default-deny)。

    tombstone 的內容(`retracted_output`、清理義務、授權依據…)由
    `podcast_attempt_retract` 組好傳進來——要讀 finalize checkpoint,那個形狀只在
    `audio_finalize`。前後兩次 `attempt_capabilities` 也留在呼叫端的 mutate 裡(F7)。
    """
    attempt["retraction"] = retraction
    episode.setdefault("retracted_attempt_ids", []).append(attempt_id)


# ───────────────────────── store-bound 的單一入口 ─────────────────────────


@dataclass(frozen=True)
class AttemptRef:
    """一顆 attempt 在某個 store 裡的位址;store-bound 的讀、寫、狀態轉換都從這裡進。

    每條路徑都經 `find_attempt`(tombstone default-deny,ADR-0009):`read` / `mutate`
    用本物件的 `retracted_hint`,轉換方法由各轉換函式自己查。唯一放行 tombstone 的是
    `mutate(..., allow_retracted=True)`——只給 `podcast_attempt_retract` 的冪等重呼用。
    轉換方法是同名 manifest 級函式的 store-bound 版:一次 `store.update`、回傳轉換的結果,
    守門差異全在那些函式裡(這裡不加任何判斷)。
    """

    store: ManifestStore
    episode_n: int
    attempt_id: str
    retracted_hint: str = _DEFAULT_RETRACTED_HINT

    def find(self, manifest: dict, *, allow_retracted: bool = False) -> tuple[dict, dict]:
        """在一份 manifest(snapshot 或 mutate 內)裡取 (episode, attempt)。"""
        return find_attempt(
            manifest,
            self.episode_n,
            self.attempt_id,
            allow_retracted=allow_retracted,
            retracted_hint=self.retracted_hint,
        )

    def read(self) -> tuple[dict, dict]:
        """鎖外 snapshot 的 (episode, attempt)。"""
        return self.find(self.store.read())

    def mutate(
        self,
        fn: Callable[[dict, dict, dict], Any],
        *,
        allow_retracted: bool = False,
    ) -> Any:
        """在 `store.update` 的鎖內取 (episode, attempt) 後跑 `fn(manifest, episode, attempt)`。"""

        def mutate(manifest: dict) -> Any:
            episode, attempt = self.find(manifest, allow_retracted=allow_retracted)
            return fn(manifest, episode, attempt)

        _, result = self.store.update(mutate)
        return result

    def _apply(self, transition: Callable[..., Any], *args: Any) -> Any:
        _, result = self.store.update(
            lambda manifest: transition(manifest, self.episode_n, self.attempt_id, *args)
        )
        return result

    def claim_prepared(
        self, artifact_ids: list[str], account: str | None = None, *, wait_timeout: float
    ) -> bool:
        """原子保存 baseline 並取得 prepared attempt 的 dispatch ownership。

        `account` 是「這次由哪個帳號送出」(ADR-0010 的 pool)。單帳號時它只是一條事實,
        多帳號 failover 時它會被 `record_dispatch_failover` 更新成實際成功的那個 ——
        沒有它,「EP35 是誰生的」事後答不出來。

        `wait_timeout` 是**這次 dispatch 原始承諾要等多久**(呼叫端傳給
        `podcast_episode`/`podcast_series` 的那個秒數,不是之後 reconcile 時想等多久)。
        存原始秒數而不是算好的 window_end——window_end 還混進了時鐘容錯常數
        (`_RECONCILIATION_CLOCK_SKEW`),那個常數以後可能改,秒數不會變(P1 修復,見
        `_RECONCILIATION_MIN_WINDOW` 的常數註解)。**兩個呼叫端都要傳**
        (`_run_episode` 與 `podcast_series` 的 inline 重送分支)——只補一條正是
        docs/gotchas-attempt.md 紅線①點名的病灶。
        """
        return self._apply(claim_prepared, artifact_ids, account, wait_timeout)

    def record_dispatch_failover(
        self, reason: object, from_account: str | None, to_account: str
    ) -> None:
        self._apply(record_dispatch_failover, reason, from_account, to_account)

    def mark_acceptance_unknown(self, error: object) -> None:
        """標成「受理不明」並留診斷。

        **`error` 不一定是例外。** 「有 `task_id` 卻 `is_failed`」那條分支傳進來的是
        **status 物件**(`_failover.dispatch_with_failover` 的 `on_acceptance_unknown(status)`),
        而舊版本用 `type(error).__name__` / `str(error)` 直接處理 —— 實測落盤的是
        `type: "GenerationStatus"` / `message: "<... object at 0x7f...>"`,**真正的原因
        (`status.error`)整條蒸發**,卡在 `acceptance_unknown` 的人打開 manifest 只看到一個
        記憶體地址。簽名原本宣告 `BaseException` 也是錯的(從來就有非例外的呼叫端)。
        改用 `_errors.describe_refusal`,與 `record_dispatch_failover` / 附件家族同源。
        """
        self._apply(mark_acceptance_unknown, error)

    def mark_not_accepted(self, status: object) -> None:
        """把 attempt 標成「伺服器明確拒絕、沒有建出任何 artifact」。

        `status` 吃兩種形狀,因為 SDK 的錯誤契約在 0.8.0 換了邊:
          - 0.7.x 的 `GenerationStatus(task_id="", status="failed")`(由 `ensure_started`
            判定後傳進來)——讀 `.error` / `.error_code`;
          - 0.8.0 起同步拒絕改成 **raise**(ADR-0019 #1342),傳進來的是例外本身。
        兩條路都必須寫進同一個 not_accepted 終態,否則同一件事會依 SDK 版本落到不同狀態。
        """
        self._apply(mark_not_accepted, status)

    def rearm_not_accepted(self) -> bool:
        """只在下一次明確呼叫時，將未受理的同一 attempt 恢復為 prepared。"""
        return self._apply(rearm_not_accepted)

    def bind_reconciled(self, artifact_id: str) -> list[str]:
        return self._apply(bind_reconciled, artifact_id)

    def mark_reconciliation_ambiguous(self, candidate_ids: list[str]) -> None:
        self._apply(mark_reconciliation_ambiguous, candidate_ids)

    def bind_sdk_heuristic(self, artifact_id: str) -> None:
        self._apply(bind_sdk_heuristic, artifact_id)
