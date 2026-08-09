"""Composite podcast tools implementing sequential feedback.

Continuity comes from NotebookLM transcribing the prior episode mp3 that we
re-upload as a source. The generation loop is deterministic Python code.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from mcp.types import ToolAnnotations
from notebooklm.exceptions import (
    ArtifactFeatureUnavailableError,
    ClientError,
    RateLimitError,
)
from notebooklm.types import ArtifactType

from . import runtime
from ._sources import assert_sources_exist, to_source_ids
from ._status import TerminalGenerationError, ensure_completed, ensure_started
from .audio_finalize import (
    finalize_attempt,
    has_durable_output_evidence,
    has_hard_output_evidence,
    new_finalize_state,
)
from ._errors import NotebookAccessDenied, is_permission_denied
from .app import mcp
from .auth_probe import probe_auth
from .enums import to_audio_format, to_audio_length
from .generation_input import (
    load_frozen_generation_input,
    read_attempt_binding,
    rollback_attempt_binding,
    write_attempt_binding,
)
from .languages import resolve_language
from .manifest_store import ManifestStore

_TZ = timezone(timedelta(hours=8))
_RECONCILIATION_CLOCK_SKEW = timedelta(minutes=1)

# 生成 kickoff 的例外裡,**契約上保證「伺服器沒有建出任何 task」**的那幾種。
# notebooklm-py 0.8.0(ADR-0019 / #1342)把同步拒絕從「回傳 status='failed'」改成
# raise:`RateLimitError` 是伺服器的 USER_DISPLAYABLE_ERROR 拒絕(配額/限流),
# `ArtifactFeatureUnavailableError` 來自 `_parse_generation_result` 的
# 「a missing id means no task was created」。兩者都等同 0.7.x 的 not_accepted。
#
# **刻意不收 `RPCError` / `DecodingError` / 網路錯誤 / CancelledError**:那些都可能
# 發生在伺服器已經受理之後,歸成 not_accepted 會讓呼叫端直接重生 → 重複 artifact +
# 重燒配額。兩種誤判的代價不對稱——把拒絕誤判成 unknown 只是多跑一次撈不到東西的
# 對帳(便宜),把已受理誤判成拒絕是真的損失,所以這個集合只放契約講死的那兩種。
_REFUSED_WITHOUT_DISPATCH = (RateLimitError, ArtifactFeatureUnavailableError)

# `NotebookAccessDenied` / `_is_permission_denied` 已移到 `_errors.py`:v0.9.0 起
# `tools_basic.notebook_share_with_pool` 也要判同一件事,各寫一份等於埋一顆「上游改了
# rpc_code 只會有一處被改到」的地雷。這裡保留同名以免動到既有呼叫點。
_is_permission_denied = is_permission_denied

ACTION_ADOPT = "podcast_attempt_adopt"
ACTION_RECONCILE = "podcast_episode_reconcile"
ACTION_RESUME = "podcast_episode_resume"
ACTION_SERIES = "podcast_series"
ACTION_SOURCE_DELETE = "source_delete"
SAFE_NEXT_ACTIONS = frozenset(
    {
        ACTION_ADOPT,
        ACTION_RECONCILE,
        ACTION_RESUME,
        ACTION_SERIES,
        ACTION_SOURCE_DELETE,
    }
)


def _artifact_created_at_utc(value: object) -> datetime | None:
    """Normalize the SDK's local-naive artifact timestamp to aware UTC."""
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc)


def _episode_label(episode_n: int, title: str) -> str:
    """Unified name for BOTH the Studio artifact and the self-uploaded source:
    ``EP{n:02d} {title}`` (e.g. ``EP01 心法篇``). The EP prefix keeps ordering /
    resume / reconciliation addressable; the title makes it human-legible.
    Both sides use this identical string (the unified-naming iron rule)."""
    return f"EP{episode_n:02d} {title.strip()}"


def _attempt_record(
    manifest: dict,
    episode_n: int,
    attempt_id: str,
    *,
    allow_retracted: bool = False,
) -> tuple[dict, dict]:
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
    # Tombstone,default-deny:被 retract 的 attempt 是歷史紀錄,任何 attempt 級操作
    # (reconcile／adopt／resume／promote／supersede)都不得再改它——否則 adopt 之類的
    # 工具可以改寫它的 finalize checkpoint,讓已結案的清理義務指向錯的 source。
    # 唯一的例外是 `podcast_attempt_retract` 自己(冪等重呼要讀得到它)。
    if attempt.get("retraction") and not allow_retracted:
        raise ValueError(
            f"attempt {attempt_id!r} was retracted (episode {episode_n}); "
            "it is history — work on the replacement attempt instead"
        )
    return episode, attempt


def _audio_settings(
    language: str,
    audio_format: str | None,
    audio_length: str | None,
    source_ids: list[str] | None = None,
) -> dict:
    """The generation inputs an attempt must not silently change across a resume."""
    settings = {
        "language": language,
        "audio_format": audio_format,
        "audio_length": audio_length,
    }
    # 只有 caller 指名來源時才有這個 key:沒指名時 settings 必須與加這個功能之前逐字
    # 相同,否則 `podcast_series` 的 prepared-attempt 等值比對會把既有 manifest 判成
    # 「設定變了」。
    if source_ids is not None:
        settings["source_ids"] = list(source_ids)
    return settings


def _create_audio_attempt(
    store: ManifestStore,
    *,
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str,
    language: str,
    audio_format: str | None,
    audio_length: str | None,
    source_ids: list[str] | None = None,
    supersedes_attempt_id: str | None = None,
    attempt_id: str | None = None,
    input_bundle: dict | None = None,
) -> str:
    attempt_id = attempt_id or str(uuid.uuid4())
    attempt = {
        "attempt_id": attempt_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notebook_id": notebook_id,
        "episode": episode_n,
        "title": title.strip(),
        "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        "settings": _audio_settings(language, audio_format, audio_length, source_ids),
        "dispatch": {
            "status": "prepared",
            "artifact_ids_before": [],
            "dispatched_at": None,
            "accepted_at": None,
        },
        "remote": {
            "artifact_id": None,
            "status": "unknown",
            "status_origin": None,
            "observed_at": None,
            "error": None,
            "error_code": None,
        },
        "finalize": new_finalize_state(),
        "errors": [],
    }
    if supersedes_attempt_id is not None:
        attempt["supersedes_attempt_id"] = supersedes_attempt_id
    if input_bundle is not None:
        attempt["input_bundle"] = dict(input_bundle)

    def mutate(manifest: dict) -> str | None:
        """回傳「沿用的既有 attempt_id」;None = 照常新建上面那個 attempt。"""
        manifest.setdefault("notebook_id", notebook_id)
        episode = next(
            (ep for ep in manifest["episodes"] if ep.get("episode") == episode_n),
            None,
        )
        if episode is not None:
            # post-retract 的集用 podcast_episode 傳錯 notebook_id 會造成
            # episode(nb-A)/attempt(nb-B)身分分裂——`_ensure_resume_attempt` 兩條
            # 分支都已經有這道 guard,這裡補齊第三個新建 attempt 的入口(逐字對齊
            # resume 新建分支的寫法與錯誤訊息風格)。
            existing_notebook = episode.get("notebook_id")
            if existing_notebook not in (None, notebook_id):
                raise ValueError(
                    f"episode {episode_n} belongs to another notebook"
                )
            legacy_preparatory_output = (
                not episode.get("attempts")
                and has_durable_output_evidence(episode)
            )
            if (
                episode.get("output_attempt_id")
                or has_hard_output_evidence(episode)
                or legacy_preparatory_output
            ):
                # 訊息要能自己走完:「P0 does not support implicit regeneration」是內部
                # 術語(P0 指的是專案內部的能力分期),呼叫端讀不懂、也不知道下一步。
                # 講清楚為什麼擋、以及唯一的合法出路。
                raise ValueError(
                    f"episode {episode_n} already has a completed output; refusing to "
                    "silently overwrite it. To replace it: podcast_attempt_retract"
                    f"(manifest_path=..., episode_n={episode_n}, attempt_id=..., reason=...), "
                    "then source_delete each id it returns in stale_source_ids, then "
                    "generate again (the title cannot change)."
                )
            active_attempt_id = episode.get("active_attempt_id")
            if active_attempt_id:
                if supersedes_attempt_id != active_attempt_id:
                    _, prior = _attempt_record(manifest, episode_n, active_attempt_id)
                    if _is_resendable_same_request(
                        prior,
                        notebook_id=notebook_id,
                        title=title,
                        brief_sha256=attempt["brief_sha256"],
                        settings=attempt["settings"],
                        input_bundle=attempt.get("input_bundle"),
                    ):
                        # 原樣重呼 = 沿用同一個 attempt 重送(不建新的、不多燒配額)。
                        # 這步只在「已確認是同一個請求」之後才做 —— 驗證先於變更
                        # (F-7 的教訓)。重設用與 `_rearm_not_accepted_attempt` 同一顆
                        # helper:兩條路都必須讓 dispatch 與 remote 一起回到乾淨的
                        # prepared,只重設一半會讓 series 看到 remote.status="failed"
                        # 而誤判成「該 supersede」。診斷不會遺失——被拒的原因留在
                        # `errors[]`,那份是只 append 的。
                        _reset_attempt_for_resend(prior)
                        return active_attempt_id
                    raise ValueError(
                        f"episode {episode_n} already has durable active attempt "
                        f"{active_attempt_id!r} (dispatch="
                        f"{prior['dispatch'].get('status')!r}); reconcile or resume it "
                        "before creating another attempt. If it never dispatched, "
                        "re-call with the identical arguments to resend that same "
                        "attempt instead of creating a new one."
                    )
                _, prior_attempt = _attempt_record(
                    manifest, episode_n, active_attempt_id
                )
                if prior_attempt.get("remote", {}).get("status") not in (
                    "failed",
                    "removed",
                ):
                    raise ValueError(
                        f"attempt {active_attempt_id!r} is not terminal and "
                        "cannot be superseded"
                    )
            elif supersedes_attempt_id is not None:
                raise ValueError("superseded attempt is no longer active")
            # 取代版不得改標題:episode 級 title／label 與 cover_path 都是 setdefault
            # 或既有值,改了會留著舊標題的 label 與封面去發布。標題綁 label／工作室
            # artifact 名／回錄來源名／發布標題,是另一件事,不能夾在重生裡做。
            if (
                episode.get("retracted_attempt_ids")
                and episode.get("title") not in (None, title.strip())
            ):
                raise ValueError(
                    f"episode {episode_n} title cannot change in a retract "
                    f"replacement (manifest has {episode['title']!r}); keep the "
                    "title or rebuild the episode explicitly"
                )
        elif supersedes_attempt_id is not None:
            raise ValueError("cannot supersede an attempt from a missing episode")
        if episode is None:
            episode = {"episode": episode_n, "attempts": []}
            manifest["episodes"].append(episode)
        episode.setdefault("title", title.strip())
        episode.setdefault("label", _episode_label(episode_n, title))
        episode.setdefault("notebook_id", notebook_id)
        episode.setdefault("attempts", []).append(attempt)
        episode["active_attempt_id"] = attempt_id
        return None

    _, reused_attempt_id = store.update(mutate)
    # 沿用既有 attempt 時要回傳**它的** id,不是上面預先生成的那顆 uuid ——
    # 呼叫端拿這個 id 去 claim dispatch、綁 artifact、寫 checkpoint。
    return reused_attempt_id or attempt_id


def _is_resendable_same_request(
    attempt: dict,
    *,
    notebook_id: str,
    title: str,
    brief_sha256: str,
    settings: dict,
    input_bundle: dict | None,
) -> bool:
    """這個 active attempt 能不能由「原樣重呼」直接沿用重送?

    只有兩個條件同時成立才算:**(a) 它從未成功 dispatch**(所以沒有遠端 artifact 會被
    孤兒化、也沒有配額已經被燒掉),**(b) 這次請求與它逐字相同**(notebook / 標題 /
    brief 雜湊 / settings / frozen bundle 綁定)。

    存在的理由是一個真實死鎖(v0.7.1 驗收 F-8):`podcast_episode` 帶 `source_ids` 重生時
    若剛好撞到配額拒絕,該集就再也推不動——`podcast_series` 不認它的 settings
    (整季沒有 per-episode `source_ids`)、`reconcile` 拒收 not_accepted、`resume` 要的
    `artifact_id` 是 null、`retract` 只認已 promote 的輸出。兩個 owner 互相推,而唯一
    的脫出方式是手改 manifest —— 那是 ADR-0009 明令禁止的。

    修法選擇「讓建立它的那支工具自己重送」而不是新增作廢工具:attempt 是
    `podcast_episode` 建的,就該由 `podcast_episode` 推進,呼叫端的動作是**原樣重跑同一
    個呼叫**,與 series 的續跑語意一致,不必學新工具。這也與 `_reuse_frozen_input_attempt`
    對 frozen bundle 做的事同一個 pattern(讀回既有綁定、沿用同一 attempt_id、不重複建)。

    **請求只要有一個欄位不同就不沿用**(照舊 fail-loud)。「設定變了還沿用」等於靜默
    改掉生成輸入,比死鎖更糟。
    """
    if attempt["dispatch"].get("status") not in ("prepared", "not_accepted"):
        return False
    if attempt["remote"].get("artifact_id") is not None:
        # not_accepted 依契約不該有 artifact 對應;真有就是資料壞了,交給既有 guard 擋。
        return False
    if attempt.get("notebook_id") != notebook_id:
        return False
    if attempt.get("title") != title.strip():
        return False
    if attempt.get("brief_sha256") != brief_sha256:
        return False
    if attempt.get("settings") != settings:
        return False
    # frozen input bundle 的綁定是 attempt identity 的一部分:一邊有一邊沒有、
    # 或綁到不同 bundle,都不是同一個請求。
    return attempt.get("input_bundle") == input_bundle


def _ensure_resume_attempt(
    store: ManifestStore,
    *,
    notebook_id: str,
    episode_n: int,
    title: str,
    artifact_id: str,
) -> str:
    """以 caller 已知的 artifact identity 找回或建立 durable resume attempt。"""

    def mutate(manifest: dict) -> str:
        manifest.setdefault("notebook_id", notebook_id)
        claimed = [
            (ep, attempt)
            for ep in manifest["episodes"]
            for attempt in ep.get("attempts", [])
            if attempt.get("remote", {}).get("artifact_id") == artifact_id
        ]
        if len(claimed) > 1:
            raise ValueError(
                f"artifact {artifact_id!r} is claimed by multiple attempts"
            )
        if claimed:
            episode, discovered = claimed[0]
            if episode.get("episode") != episode_n:
                raise ValueError(
                    f"artifact {artifact_id!r} belongs to another episode"
                )
            # 走 `_attempt_record` 而不是直接用掃到的那筆:tombstone 的 default-deny 在
            # 那裡,這條 claimed 分支曾是唯一繞過它的路。
            episode, attempt = _attempt_record(
                manifest, episode_n, discovered["attempt_id"]
            )
            if attempt.get("notebook_id") != notebook_id:
                raise ValueError(
                    f"artifact {artifact_id!r} belongs to another notebook"
                )
            if attempt.get("title") != title.strip():
                raise ValueError(
                    f"artifact {artifact_id!r} belongs to another title"
                )
            # 與新建分支同樣的兩道 gate。少了它們,一筆「歷史上曾被 claim 過」的 artifact
            # 就能把 active 從現任 output 手上搶走(active=B／output=A 的死鎖),或用不同
            # 標題把 episode 身分拆成兩半。
            output_attempt_id = episode.get("output_attempt_id")
            if output_attempt_id not in (None, attempt["attempt_id"]):
                raise ValueError(
                    f"episode {episode_n} already has durable output "
                    f"{output_attempt_id!r}; retract it (podcast_attempt_retract) and "
                    "delete the stale feedback source before resuming another artifact"
                )
            existing_title = episode.get("title")
            if (
                isinstance(existing_title, str)
                and existing_title.strip() != title.strip()
            ):
                raise ValueError(
                    f"episode {episode_n} title does not match the manifest "
                    f"({existing_title!r}); resume cannot rename an episode"
                )
            current_active_id = episode.get("active_attempt_id")
            if (
                current_active_id
                and current_active_id != attempt["attempt_id"]
                and current_active_id != output_attempt_id
            ):
                raise ValueError(
                    f"episode {episode_n} has another active attempt "
                    f"{current_active_id!r}; resume it before switching artifacts"
                )
            attempt.setdefault("finalize", new_finalize_state())
            episode["active_attempt_id"] = attempt["attempt_id"]
            return attempt["attempt_id"]

        episode = next(
            (ep for ep in manifest["episodes"] if ep.get("episode") == episode_n),
            None,
        )
        if episode is None:
            episode = {"episode": episode_n, "attempts": []}
            manifest["episodes"].append(episode)
        # 已經有 durable output 時,不得為「另一個 artifact」開新 attempt。舊行為允許
        # (active == output 就放行),結果是:resume 把 B 下載、回錄上傳完,promotion 才
        # 因為 output 仍屬 A 而失敗,manifest 卡在 active=B／output=A —— retract A 被
        # active 檢查擋、retract B 不是 output 也被擋、resume B 重複同樣錯誤,series 又
        # 只看 A。取代版的唯一合法順序是 retract → 刪舊 source → 再生／resume。
        output_attempt_id = episode.get("output_attempt_id")
        if output_attempt_id is not None:
            raise ValueError(
                f"episode {episode_n} already has durable output "
                f"{output_attempt_id!r}; retract it (podcast_attempt_retract) and "
                "delete the stale feedback source before resuming another artifact"
            )
        active_attempt_id = episode.get("active_attempt_id")
        if active_attempt_id:
            raise ValueError(
                f"episode {episode_n} has active attempt {active_attempt_id!r}; "
                "reconcile or resume that attempt before supplying another artifact"
            )
        existing_notebook = episode.get("notebook_id")
        if existing_notebook not in (None, notebook_id):
            raise ValueError(
                f"episode {episode_n} belongs to another notebook"
            )
        # 標題必須對得上 manifest。episode 級 title／label 是 setdefault 寫的,resume 帶一個
        # 不同標題只會讓 artifact／回錄 source 改名成新 label,而 episode 與發布仍用舊標題
        # ——身分就此分岔。既有 claimed 分支本來就擋「belongs to another title」,這裡補齊
        # 新建分支(retract 之後改標題重生就是走這條)。
        existing_title = episode.get("title")
        if isinstance(existing_title, str) and existing_title.strip() != title.strip():
            raise ValueError(
                f"episode {episode_n} title does not match the manifest "
                f"({existing_title!r}); resume cannot rename an episode"
            )
        attempt_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        finalize = new_finalize_state()
        legacy_artifact_id = (
            episode.get("artifact_id") or episode.get("task_id")
        )
        adopted_source_id = episode.get("feedback_source_id")
        adopted_at = episode.get("feedback_source_adopted_at")
        if (
            isinstance(adopted_source_id, str)
            and adopted_source_id
            and isinstance(adopted_at, str)
            and adopted_at
            and legacy_artifact_id == artifact_id
        ):
            finalize["feedback_source_upload"].update(
                {
                    "status": "completed",
                    "source_id": adopted_source_id,
                    "adopted_at": adopted_at,
                }
            )
            finalize["feedback_source_rename"].update(
                {"status": "completed", "adopted_at": adopted_at}
            )
        attempt = {
            "attempt_id": attempt_id,
            "created_at": now,
            "notebook_id": notebook_id,
            "episode": episode_n,
            "title": title.strip(),
            "brief_sha256": None,
            "settings": {"origin": "explicit_resume"},
            "dispatch": {
                "status": "accepted",
                "artifact_ids_before": [],
                "dispatched_at": None,
                "accepted_at": now,
            },
            "remote": {
                "artifact_id": artifact_id,
                "status": "pending",
                "status_origin": "caller_verified",
                "observed_at": now,
                "error": None,
                "error_code": None,
            },
            "finalize": finalize,
            "errors": [],
        }
        episode.setdefault("title", title.strip())
        episode.setdefault("label", _episode_label(episode_n, title))
        episode["notebook_id"] = notebook_id
        episode.setdefault("attempts", []).append(attempt)
        episode["active_attempt_id"] = attempt_id
        return attempt_id

    _, attempt_id = store.update(mutate)
    return attempt_id


def _claim_prepared_dispatch(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    artifact_ids: list[str],
    account: str | None = None,
) -> bool:
    """原子保存 baseline 並取得 prepared attempt 的 dispatch ownership。

    `account` 是「這次由哪個帳號送出」(ADR-0010 的 pool)。單帳號時它只是一條事實,
    多帳號 failover 時它會被 `_record_dispatch_failover` 更新成實際成功的那個 ——
    沒有它,「EP35 是誰生的」事後答不出來。
    """

    def mutate(manifest: dict) -> bool:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        dispatch = attempt["dispatch"]
        if dispatch["status"] != "prepared":
            return False
        dispatch.update(
            {
                "status": "dispatching",
                "artifact_ids_before": artifact_ids,
                "dispatched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if account is not None:
            dispatch["account"] = account
        return True

    _, claimed = store.update(mutate)
    return claimed


def _record_dispatch_failover(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    reason: object,
    from_account: str | None,
    to_account: str,
) -> None:
    """記下「A 拒絕 → 改用 B 重送」,並把 dispatch 的 account 換成 B。

    **對 client 透明可以,對稽核紀錄不行**(ADR-0010)。errors[] 是唯一只 append、
    從不被清除的欄位 —— `_reset_attempt_for_resend` 會把 dispatch/remote 清回
    prepared,診斷放那裡會被抹掉,放這裡不會。
    """

    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        if isinstance(reason, BaseException):
            message = str(reason) or type(reason).__name__
            reason_type = type(reason).__name__
        else:
            message = getattr(reason, "error", None) or getattr(reason, "status", "failed")
            reason_type = getattr(reason, "error_code", None) or "not_accepted"
        attempt["dispatch"]["account"] = to_account
        attempt["errors"].append(
            {
                "phase": "dispatch_failover",
                "type": reason_type,
                "message": message,
                "from_account": from_account,
                "to_account": to_account,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    store.update(mutate)


def _rotate_for_quota(
    store: ManifestStore | None,
    episode_n: int,
    attempt_id: str,
    reason: object,
    from_account: str | None,
) -> tuple[str | None, object] | None:
    """還有沒試過的帳號就換過去,回**換過去那一個**的 `(label, client)`;沒有就回 None。

    `store is None`(standalone、沒有 manifest)一律不換:沒有地方寫稽核紀錄,
    靜默換帳號違反 ADR-0010 自己立的「對紀錄不透明」。

    **回 (label, client) 而不是 bool**:呼叫端要拿著這一份往下送,不能回頭再讀一次
    全域(`rotate_client()` 與下一次 `get_client()` 之間有 await,並行的另一次
    rotate 會落在那個縫裡)。`from_account` 也由呼叫端傳進來、不在這裡重讀,理由同上
    ——`_record_dispatch_failover` 記的「從哪個帳號換到哪個」必須是**這次 dispatch
    實際用過的**那一個,不是「此刻剛好輪到誰」。
    """
    if store is None:
        return None
    to_account = runtime.rotate_client()
    if to_account is None:
        return None
    _record_dispatch_failover(
        store, episode_n, attempt_id, reason, from_account, to_account
    )
    # rotate_client() 與 snapshot() 之間沒有 await,所以取到的必然是剛換過去那個槽位。
    return runtime.snapshot()


async def _dispatch_audio_with_failover(
    store: ManifestStore | None,
    episode_n: int,
    attempt_id: str,
    generate,
    *,
    account: str | None,
    client: object,
) -> tuple[str, str | None, object]:
    """送出音檔生成並回傳 artifact_id;配額拒絕就換帳號**原地重送同一個 attempt**。

    兩條路徑收在同一個函式裡是刻意的:同一個「伺服器拒絕、沒建出 task」語意有兩種
    形狀進來(0.8.0 起 raise,0.7.x 回 `task_id=""` 由 `ensure_started` 判定),分兩處
    各補一次 failover 正是本 repo 反覆出事的「補一半」。**兩個呼叫端也共用它**:
    `_run_episode`(全新一集)與 `podcast_series` 的重送/supersede 分支
    (v0.8.0 只補了前者,於是 pool 對「重試」這條最需要它的路完全無效 —— 驗收 F-4)。

    **除了那兩種,一律不換帳號**:其餘失敗都可能發生在伺服器已經受理之後,重送會變成
    重複 artifact + 重燒配額(`_REFUSED_WITHOUT_DISPATCH` 刻意不長大的同一個理由)。

    **續跑指引由呼叫端各自加在例外訊息上**,不在這裡:`podcast_episode` 的續跑動作是
    原樣重呼自己,`podcast_series` 是重呼整季 —— 而 `_mark_not_accepted` 會把
    `str(exc)` 寫進 `remote.error` 與 `errors[]`,寫死一種指引等於讓**錯的**工具名
    落進 manifest,事後查錯的人會照著跑錯的東西。

    **`account` / `client` 由呼叫端用 `runtime.snapshot()` 一次取好傳進來**,函式內
    不再自己讀全域。MCP 是並行的(每則 message 一個 task),而記帳點
    `_claim_prepared_dispatch(..., account=…)` 與這裡真正的 `generate(client)` 之間
    隔著至少一次 await —— 分兩次讀全域時,另一個工具呼叫在那個縫裡撞到配額並 rotate,
    manifest 就會記下 A、實際卻由 B 送出。ADR-0010 §Transparency 說 manifest 是
    **唯一**的稽核憑據,記錯帳等於憑據失真,而且事後無法發現(兩個帳號都成功,只是
    掛在錯的名下)。failover 換帳號時 `account`/`client` 一起換,兩者永遠同源。

    **回 `(artifact_id, account, client)`,呼叫端必須接住後兩個往下用。** 只回 artifact_id
    的話,呼叫端要做 finalize 只能回頭 `runtime.get_client()` —— 又變成「此刻游標指到誰」
    而不是「這次是誰送的」,而且**風險視窗幾乎全落在那一半**:dispatch 只有幾秒,
    finalize(等生成 → 下載 → 回錄上傳 → rename)是數十分鐘。分享不完整時症狀是十幾分鐘後
    在 finalize 爆 401、根因在遠處(正是 ADR-0010 以為已經退休掉的那個);分享完整時退化成
    稽核失真:manifest 記 A 生成、實際下載與回錄上傳的是 B,而 finalize 身分不寫進任何欄位,
    事後查不出來。
    """
    while True:
        try:
            status = await generate(client)
        except _REFUSED_WITHOUT_DISPATCH as exc:
            # 伺服器明確拒絕、沒有建出 task(0.8.0 起改成 raise;0.7.x 走下面的
            # ensure_started 分支)。這是**乾淨的終態**,不是「結果不明」——標成
            # not_accepted 讓呼叫端可以直接重試,不必先跑一次註定撈不到東西的對帳。
            rotated = _rotate_for_quota(store, episode_n, attempt_id, exc, account)
            if rotated is not None:
                account, client = rotated
                continue
            if store is not None:
                _mark_not_accepted(store, episode_n, attempt_id, exc)
            raise
        except (Exception, asyncio.CancelledError) as exc:
            if _is_permission_denied(exc):
                # 這個帳號看不到那個 notebook —— 伺服器沒建出任何 task,所以是
                # **乾淨的終態**而不是「受理不明」。走泛用分支會把它標成
                # acceptance_unknown,把呼叫端叫去跑一次註定撈不到東西的 reconcile。
                # **不 rotate**:權限是設定問題不是暫時性問題,一個一個帳號試過去
                # 只會掩蓋根因,還每次多燒一輪 RPC。
                denied = NotebookAccessDenied(
                    # 用這次 dispatch 實際持有的 account,不重讀全域:並行 rotate 會
                    # 讓訊息指認錯的帳號,而這條訊息會被寫進 manifest 當稽核紀錄。
                    f"{exc}\n帳號 {account!r} 對這個 notebook 沒有存取權。"
                    "多帳號 pool 模式要求 notebook 對 pool 全員可存取 —— 呼叫 "
                    "notebook_share_with_pool(notebook_id=...) 補分享給其餘帳號"
                    "(EDITOR)後再重試;MCP 自建 notebook(v0.8.1 起)已自動分享,"
                    "這通常是舊版建立或在網頁上手動建立的既有 notebook。"
                )
                # 先建好帶指引的例外、再標 manifest —— manifest 的 remote.error／
                # errors[] 記下的要是**這份帶著下一步動作**的訊息,不是上游原始的
                # "permission denied"。呼叫端事後只看得到 manifest 時(例如
                # podcast_series 的結構化 partial 只回訊息不回原始例外),指引才不會
                # 整條蒸發。
                if store is not None:
                    _mark_not_accepted(store, episode_n, attempt_id, denied)
                raise denied from exc
            if store is not None:
                _mark_acceptance_unknown(store, episode_n, attempt_id, exc)
            raise

        # task_id IS the artifact_id — notebooklm-py _types/artifacts.py:421 states
        # "task_id and artifact_id are the same identifier"; GenerationStatus has NO
        # artifact_id field, so we must use task_id for the download/rename targeting
        # (otherwise download falls back to "latest" and rename targets None).
        # ensure_started guards the failed/empty-task_id case (rate limit / quota / refusal).
        try:
            return ensure_started(status), account, client
        except RuntimeError:
            if getattr(status, "task_id", None):
                # 有 id 卻 is_failed=True:這**不是**零副作用拒絕。SDK
                # `_artifact/generation.py:586-590` 明寫「有 artifact_id 就回
                # GenerationStatus(task_id=artifact_id, status=...)」,而
                # `_ARTIFACT_STATUS_MAP` 含 FAILED → "failed",所以「有 id + failed」
                # 這個形狀在上游可達。rotate 重送在這個形狀下會產生第二個 artifact:
                # A 已經建出 task,manifest 卻只綁得到 B 那個,第一個不在
                # artifact_ids_before 基線裡,日後 reconcile 會撞成
                # reconciliation_ambiguous、還多燒一次配額——正是 ADR-0010 紀律②要擋
                # 的「把已受理誤判成拒絕」。標成 acceptance_unknown(不是
                # not_accepted)讓呼叫端先對帳,而不是盲目重試。
                if store is not None:
                    _mark_acceptance_unknown(store, episode_n, attempt_id, status)
                raise
            rotated = _rotate_for_quota(store, episode_n, attempt_id, status, account)
            if rotated is not None:
                account, client = rotated
                continue
            if store is not None:
                _mark_not_accepted(store, episode_n, attempt_id, status)
            raise


def _mark_acceptance_unknown(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    error: BaseException,
) -> None:
    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        if attempt["dispatch"]["status"] != "dispatching":
            return
        attempt["dispatch"]["status"] = "acceptance_unknown"
        attempt["errors"].append(
            {
                "phase": "dispatch",
                "type": type(error).__name__,
                "message": str(error),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    store.update(mutate)


def _mark_not_accepted(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    status: object,
) -> None:
    """把 attempt 標成「伺服器明確拒絕、沒有建出任何 artifact」。

    `status` 吃兩種形狀,因為 SDK 的錯誤契約在 0.8.0 換了邊:
      - 0.7.x 的 `GenerationStatus(task_id="", status="failed")`(由 `ensure_started`
        判定後傳進來)——讀 `.error` / `.error_code`;
      - 0.8.0 起同步拒絕改成 **raise**(ADR-0019 #1342),傳進來的是例外本身。
    兩條路都必須寫進同一個 not_accepted 終態,否則同一件事會依 SDK 版本落到不同狀態。
    """

    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        if attempt["dispatch"]["status"] != "dispatching":
            return
        now = datetime.now(timezone.utc).isoformat()
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

    store.update(mutate)


def _assert_series_owns_attempt(
    attempt: dict,
    series_settings: dict,
    episode_n: int,
    attempt_id: str,
) -> None:
    """整季能不能接手這個 attempt —— 不能的話 fail-loud,**且指出誰能**。

    `podcast_series` 刻意不開 per-episode `source_ids`(整季共用一組沒有意義,而每集的
    回錄 source 要跑到那一集才存在、規劃階段填不出來)。所以帶 `source_ids` 的 attempt
    ——那是 `podcast_episode` 重生時建的——series 生不出相同的 settings,不能靜默改用
    全部來源續生。

    但只講「設定變了」會把人卡死:v0.7.1 驗收 F-8 就是這樣走進死路的,而
    troubleshooting 對 not_accepted 的指示恰好是「重呼 podcast_series」。訊息因此要
    明講正確的續跑者是 `podcast_episode` 原樣重呼(見 `_is_resendable_same_request`)。
    """
    stored = attempt.get("settings")
    if stored == series_settings:
        return
    owner_hint = (
        " This attempt carries per-episode source_ids, so podcast_series cannot"
        " reproduce its settings — continue it by re-calling podcast_episode with the"
        " identical arguments (same title/brief/source_ids), which resends that same"
        " attempt without burning a new one."
        if isinstance(stored, dict) and "source_ids" in stored
        else " Restore the original language/format/length, or retract the attempt."
    )
    raise ValueError(
        f"episode {episode_n} attempt {attempt_id!r} settings do not match this"
        f" podcast_series call.{owner_hint}"
    )


def _reset_attempt_for_resend(attempt: dict) -> None:
    """把一個「從未成功 dispatch」的 attempt 就地清回乾淨的 prepared。

    dispatch 與 remote **必須一起**重設:只清 dispatch 會留下 `remote.status="failed"`,
    而 `podcast_series` 看到那個值會判定「該 supersede」,於是明明要沿用的 attempt 被
    換掉。被拒的原因不會遺失 —— 它在 `errors[]` 裡,那份只 append、從不清除。

    兩個呼叫端共用:`_rearm_not_accepted_attempt`(series 續跑)與 `_create_audio_attempt`
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
    attempt["remote"].update(
        {
            "status": "unknown",
            "status_origin": None,
            "observed_at": None,
            "error": None,
            "error_code": None,
        }
    )


def _rearm_not_accepted_attempt(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
) -> bool:
    """只在下一次明確呼叫時，將未受理的同一 attempt 恢復為 prepared。"""

    def mutate(manifest: dict) -> bool:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        dispatch = attempt["dispatch"]
        if dispatch["status"] != "not_accepted":
            return False
        if attempt["remote"].get("artifact_id") is not None:
            raise ValueError("not_accepted attempt cannot have an artifact mapping")
        _reset_attempt_for_resend(attempt)
        return True

    _, rearmed = store.update(mutate)
    return rearmed

def _claimed_artifact_ids(manifest: dict, excluding_attempt_id: str) -> set[str]:
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


def _reconciliation_subject(
    manifest: dict,
    episode_n: int,
    attempt_id: str,
) -> tuple[dict, str, set[str], datetime]:
    episode, attempt = _attempt_record(manifest, episode_n, attempt_id)
    notebook_id = attempt.get("notebook_id")
    if not isinstance(notebook_id, str) or not notebook_id:
        raise ValueError("attempt notebook_id must be a non-empty string")
    if episode.get("notebook_id") not in (None, notebook_id):
        raise ValueError("episode notebook_id does not match the attempt")

    dispatch = attempt.get("dispatch")
    if not isinstance(dispatch, dict):
        raise ValueError("attempt dispatch state must be an object")
    baseline = dispatch.get("artifact_ids_before")
    if not isinstance(baseline, list) or any(
        not isinstance(value, str) or not value for value in baseline
    ):
        raise ValueError("attempt artifact baseline must be a list of ids")
    dispatched_at = dispatch.get("dispatched_at")
    if not isinstance(dispatched_at, str):
        raise ValueError("attempt dispatched_at is required for reconciliation")
    try:
        dispatched = datetime.fromisoformat(dispatched_at)
    except ValueError:
        raise ValueError("attempt dispatched_at is invalid") from None
    if dispatched.tzinfo is None:
        raise ValueError("attempt dispatched_at must include a timezone")
    return attempt, notebook_id, set(baseline), dispatched.astimezone(timezone.utc)


def _bind_reconciled_artifact(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    artifact_id: str,
) -> None:
    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        remote = attempt.get("remote")
        if not isinstance(remote, dict) or remote.get("artifact_id") is not None:
            raise ValueError("attempt already has a remote artifact mapping")
        if artifact_id in _claimed_artifact_ids(manifest, attempt_id):
            raise ValueError(f"artifact {artifact_id!r} was claimed during reconciliation")
        now = datetime.now(timezone.utc).isoformat()
        attempt["dispatch"]["status"] = "accepted"
        attempt["dispatch"]["accepted_at"] = now
        remote.update(
            {
                "artifact_id": artifact_id,
                "status": "pending",
                "status_origin": "remote",
                "observed_at": now,
            }
        )

    store.update(mutate)


def _mark_reconciliation_ambiguous(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    candidate_ids: list[str],
) -> None:
    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        remote = attempt.get("remote")
        if not isinstance(remote, dict) or remote.get("artifact_id") is not None:
            raise ValueError("attempt already has a remote artifact mapping")
        if _claimed_artifact_ids(manifest, attempt_id).intersection(candidate_ids):
            raise ValueError("candidate claim changed during reconciliation")
        attempt["dispatch"]["status"] = "reconciliation_ambiguous"
        attempt["dispatch"]["candidate_artifact_ids"] = candidate_ids

    store.update(mutate)


def _bind_accepted_artifact(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    artifact_id: str,
) -> None:
    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        dispatch = attempt["dispatch"]
        if dispatch["status"] not in (
            "dispatching",
            "acceptance_unknown",
            "reconciliation_ambiguous",
            "accepted",
        ):
            raise ValueError(
                f"attempt {attempt_id!r} cannot accept an artifact from "
                f"dispatch state {dispatch['status']!r}"
            )
        remote = attempt["remote"]
        existing_artifact_id = remote.get("artifact_id")
        if existing_artifact_id not in (None, artifact_id):
            raise ValueError(
                f"attempt {attempt_id!r} is already mapped to another artifact "
                f"{existing_artifact_id!r}"
            )
        if existing_artifact_id == artifact_id:
            return
        if existing_artifact_id is None and artifact_id in _claimed_artifact_ids(
            manifest, attempt_id
        ):
            raise ValueError(f"artifact {artifact_id!r} is already claimed")
        now = datetime.now(timezone.utc).isoformat()
        attempt["dispatch"]["status"] = "accepted"
        attempt["dispatch"]["accepted_at"] = now
        attempt["remote"].update(
            {
                "artifact_id": artifact_id,
                "status": "pending",
                "status_origin": "sdk_heuristic",
                "observed_at": now,
            }
        )

    store.update(mutate)


def _first_published_at(episode: dict) -> str | None:
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


def _promote_attempt_output(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    output: dict,
) -> None:
    def mutate(manifest: dict) -> None:
        episode, attempt = _attempt_record(manifest, episode_n, attempt_id)
        # 一個 retract 之前就啟動的 finalizer 不得把被拒收的那版重新掛回 output——
        # promotion 是它最後一個寫入點,也是唯一會復活 episode 級投影欄位的地方。
        if attempt.get("retraction"):
            raise ValueError(
                f"attempt {attempt_id!r} was retracted; it cannot be promoted to "
                f"episode {episode_n}'s output"
            )
        if episode.get("output_attempt_id") not in (None, attempt_id):
            raise ValueError(
                f"episode {episode_n} output is owned by "
                f"{episode['output_attempt_id']!r}; refusing to promote {attempt_id!r}"
            )
        attempt["remote"]["status"] = "completed"
        attempt["remote"]["observed_at"] = datetime.now(timezone.utc).isoformat()
        episode["artifact_id"] = output["artifact_id"]
        episode["mp3_path"] = os.path.abspath(output["mp3_path"])
        episode["output_attempt_id"] = attempt_id
        for key in ("title", "label"):
            episode.setdefault(key, output[key])
        # published_at 不能跟 title／label 一樣用 output 自己的 setdefault:output 裡
        # 那個值是「這次生成完成的時刻」,取代版每一次重生都會不一樣。已驗證的安全
        # 互動(見上方 _attempt_record 的 tombstone 與 :1867 附近 retract 冪等分支的
        # ownership guard):取代版 B 帶著 A 的首發時間上任後,對 A 重打冪等 retract
        # 不會誤刪 B 的投影——那條 guard 只在 output_attempt_id in (None, attempt_id)
        # 時才動手,B 是現任 output 時整段會跳過。
        episode.setdefault(
            "published_at", _first_published_at(episode) or output["published_at"]
        )
        # 回寫進 output:這支函式的四個呼叫點都是 `_promote…(…, output); return output`,
        # 只改 manifest 會讓**回傳值**仍帶著「這次重生的時刻」——feed 對、呼叫端拿到的
        # 卻是錯的 pubDate(v0.6.0 實測抓到:manifest 12:07:20、回傳值 12:16:39)。
        # 修在這個匯流點,四條路徑一起正確。
        output["published_at"] = episode["published_at"]
        # 回寫進 output:這支函式的四個呼叫點都是 `_promote…(…, output); return output`,
        # 只改 manifest 會讓**回傳值**still 帶著「這次重生的時刻」——feed 對、呼叫端拿到的
        # 卻是錯的 pubDate(v0.6.0 實測抓到:manifest 12:07:20、回傳值 12:16:39)。
        # 修在這個匯流點,四條路徑一起正確。
        # episode 級 continuity 證據:attempt 裡的 source_id 是主要真相,但 legacy
        # projection 只認得 episode 級欄位。不寫 feedback_source_adopted_at——那是
        # podcast_attempt_adopt 的人工驗證標記,resume 靠它判斷可否沿用舊 source。
        source_id = output.get("feedback_source_id")
        if isinstance(source_id, str) and source_id:
            episode["feedback_source_id"] = source_id

    store.update(mutate)


def _validate_episode_args(episode_n: int, title: str, prior_mp3_path: str | None) -> None:
    """單集參數的純本地驗證(不打網路)。壞參數 ValueError 秒退——必須在
    auth 預檢之前跑,認證錯誤不得蓋掉參數錯誤。"""
    # episode_n 必須是 >= 1 的整數(bool 是 int 子類,明確擋掉):否則會生出 EP00/
    # 負集號、還燒掉一次生成 quota。podcast_series 有 start>=1 守衛,單集入口也要有。
    if not isinstance(episode_n, int) or isinstance(episode_n, bool) or episode_n < 1:
        raise ValueError(f"episode_n must be an int >= 1, got: {episode_n!r}")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"episode {episode_n} requires a non-empty 'title'")
    if prior_mp3_path and episode_n <= 1:
        raise ValueError("prior_mp3_path requires episode_n >= 2 (there is no prior to episode 1)")


def _require_existing_manifest(
    manifest_path: str, *, missing_hint: str | None = None
) -> None:
    """resume／reconcile／adopt／retract 動的一定是既有 attempt/episode。

    reconcile／adopt／retract 三個只做 ``ManifestStore.read()``(唯讀);缺檔時本來
    就會在下游用「episode is missing from the manifest」的 ValueError 乾淨失敗,不會
    寫出任何檔案、更不會長出分岔的新 manifest——這裡秒退純粹是把「路徑打錯」講得更
    直白、更早,不是在防什麼結構性風險。真正會 bootstrap(在缺檔路徑上新建一份空
    schema 再 ``store.update()`` 寫下去)並緊接著打遠端 RPC(download／rename／
    source add)的只有 resume:``_ensure_resume_attempt`` 找不到既有 episode 時會
    就地建一筆——路徑打錯就等於在錯的地方生出一份新 manifest,而且真的燒了下載與
    回錄上傳,不只是白讀一次。"""
    if os.path.isdir(manifest_path):
        raise ValueError(
            f"manifest_path is a directory, not a file: {manifest_path!r}"
        )
    if not os.path.isfile(manifest_path):
        hint = f" ({missing_hint})" if missing_hint else ""
        raise ValueError(
            f"manifest_path does not exist: {manifest_path!r}; check for a typo — "
            f"this tool only continues an existing manifest, it never creates one{hint}"
        )


async def _finalize_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
    artifact_id: str,
    output_dir: str,
    wait_timeout: float,
) -> dict:
    """單集後半段(生成之後):等完成 → 命名 → 下載 → 自上傳回錄 → 回傳 manifest 列。

    抽成獨立函式,讓 `podcast_episode_resume` 能拿一個「已在雲端啟動」的 artifact_id
    直接續完,不重生、不燒 quota。artifact_id 就是 generate_audio 的 task_id
    (task_id ≡ artifact id;GenerationStatus 無 artifact_id 欄位)。"""
    client = runtime.get_client()
    os.makedirs(output_dir, exist_ok=True)
    label = _episode_label(episode_n, title)

    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    # Rename the Studio artifact BEFORE downloading: name it in NotebookLM first so
    # the notebook stays legible regardless of the download outcome, then pull the mp3.
    #
    # ⚠️ `return_object=False` **不再是 fire-and-forget**(0.8.0 / #1362):它現在照樣跑
    # 一次 LIST_ARTIFACTS,查不到就 raise ArtifactNotFoundError,只是成功時回 None。
    # 0.7.x 那個「False 直接短路、不查也不 raise」的行為已經拿掉了,所以這一行從
    # 「絕不會因為找不到而失敗」變成「會」。
    # 保持現狀不加防護是刻意的:走到這裡代表 ensure_completed 剛通過(而 poll_status
    # 本來就是靠 artifact list 判定的),artifact 幾毫秒前還在清單裡;此刻查不到基本上
    # 只有一種解釋——被伺服器下架(配額)。那種情況下面的 download_audio 一樣會失敗,
    # 提早爆掉反而誠實。失敗語意由呼叫端補完:podcast_episode 的 except 會附上
    # artifact_id + podcast_episode_resume 的續跑指引。
    await client.artifacts.rename(notebook_id, artifact_id, label, return_object=False)

    mp3_path = os.path.join(output_dir, f"ep{episode_n:02d}.mp3")
    await client.artifacts.download_audio(notebook_id, mp3_path, artifact_id)

    # Re-upload THIS episode's own mp3 as a source named IDENTICALLY to its Studio
    # artifact ("EP02" artifact <-> "EP02" source — same string, no suffix). This is
    # the heart of the sequential-feedback method AND keeps a complete record:
    #  - EVERY episode (including the last) ends up in Sources, name-matched to Studio.
    #  - the NEXT episode's generation automatically sees this source for continuity,
    #    so podcast_series needs no separate prior-upload step.
    own_src = await client.sources.add_file(
        notebook_id, mp3_path, mime_type="audio/mpeg", wait=True, wait_timeout=600.0
    )
    await client.sources.rename(notebook_id, own_src.id, label, return_object=False)

    return {
        "episode": episode_n,
        "title": title.strip(),
        "label": label,
        "task_id": artifact_id,
        "artifact_id": artifact_id,
        "mp3_path": mp3_path,
        "published_at": format_datetime(datetime.now(_TZ)),  # 產出時間 → 進 manifest
    }


async def _assert_source_cleanup_done(
    client: object, store: ManifestStore, notebook_id: str, episode_n: int
) -> None:
    """`podcast_attempt_retract` 留下的清理義務,在下一次生成前強制結案。

    retract 不打 RPC(刻意),所以刪舊回錄 source 是呼叫端的動作;但「靠文件提醒」等於
    沒有防護——finalize 是按 source_id 驗的,漏刪會靜默留下兩筆同名 media,之後每次
    生成都把兩份逐字稿餵進 context。故把它變成生成的 precondition:真的去 notebook
    查,還在就 fail-closed;已經不在才清掉義務、放行。

    聚合範圍是**整個 canonical notebook**,不是只有 `episode_n` 這一列:retract EP_a
    後漏刪其 stale source,若只驗當前這一集,回頭跳過 dirty 的 EP_a、改生成同 notebook
    的 EP_b(例如 start 跳過較早集)會直接放行——EP_b 未指名 source_ids 時就把 EP_a
    的拒收逐字稿讀進 context。canonical notebook 取
    ``episode.get("notebook_id") or manifest.get("notebook_id")``;canonical 不同的集
    互不影響(不同 notebook 的義務不該互相卡住)。"""
    snapshot = store.read()
    episodes = snapshot.get("episodes", [])
    manifest_notebook_id = snapshot.get("notebook_id")

    def canonical_notebook(row: dict) -> object:
        return row.get("notebook_id") or manifest_notebook_id

    target_episode = next(
        (row for row in episodes if row.get("episode") == episode_n), None
    )
    if target_episode is not None and (target_episode.get("pending_source_cleanup") or []):
        # 先驗這一集本身的 notebook 身分,再拿它查。`notebook_id` 是呼叫端給的,而清理
        # 義務是綁在 manifest 那個 notebook 上——拿一個空的別的 notebook 來查,會「查無
        # 此 source」而把義務誤判成已結案(舊來源其實還躺在真正的筆記本裡)。
        canonical = canonical_notebook(target_episode)
        if canonical is not None and canonical != notebook_id:
            raise ValueError(
                f"episode {episode_n} belongs to notebook {canonical!r}, not "
                f"{notebook_id!r}; cannot discharge its source cleanup from another notebook"
            )

    pending_by_episode = [
        (row.get("episode"), list(row["pending_source_cleanup"]))
        for row in episodes
        if row.get("pending_source_cleanup")
        and canonical_notebook(row) in (None, notebook_id)
    ]
    if not pending_by_episode:
        return

    live = {
        getattr(source, "id", None) for source in await client.sources.list(notebook_id)
    }
    violations = [
        (ep_n, source_id)
        for ep_n, pending in pending_by_episode
        for source_id in pending
        if source_id in live
    ]
    if violations:
        details = ", ".join(f"episode {ep_n}: {sid}" for ep_n, sid in violations)
        raise ValueError(
            f"notebook {notebook_id!r} has retracted feedback sources still in the "
            f"notebook: {details}. Delete them first — "
            f"source_delete(notebook_id={notebook_id!r}, source_id=...) — then retry; "
            "leaving them creates two identically named sources."
        )

    # 只有「身分確定就是這個 notebook」的列才可以結案。canonical 為 None(episode 與
    # manifest 都沒有 notebook_id 的 legacy 列)納入上面的違規檢查是保守的正確做法,但
    # 不能拿呼叫端隨手傳的 notebook 查不到就當它結案——那筆舊來源可能正躺在別的筆記本裡,
    # 一旦誤清就再也沒有東西擋它污染後續生成。
    clearable = {
        row.get("episode")
        for row in episodes
        if row.get("pending_source_cleanup") and canonical_notebook(row) == notebook_id
    }
    checked_absent_by_episode = {
        ep_n: set(pending)
        for ep_n, pending in pending_by_episode
        if ep_n in clearable
    }

    def clear(manifest: dict) -> None:
        for row in manifest["episodes"]:
            checked_absent = checked_absent_by_episode.get(row.get("episode"))
            if checked_absent is None:
                continue
            # 只清「這次真的查過、確認不在」的那幾筆:await 期間可能又有一次 retract 追加
            # 新義務,無條件 pop 整個欄位會把它一起吞掉。
            left = [
                source_id
                for source_id in row.get("pending_source_cleanup", [])
                if source_id not in checked_absent
            ]
            if left:
                row["pending_source_cleanup"] = left
            else:
                row.pop("pending_source_cleanup", None)

    store.update(clear)


def _reuse_frozen_input_attempt(
    store: ManifestStore,
    *,
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str,
    settings: dict,
    attempt_id: str,
    input_bundle: dict,
) -> bool:
    """Reuse a locally bound attempt only when its whole identity is unchanged."""
    snapshot = store.read()
    episode = next(
        (row for row in snapshot["episodes"] if row.get("episode") == episode_n),
        None,
    )
    if episode is None:
        return False
    attempts = [
        row for row in episode.get("attempts", []) if row.get("attempt_id") == attempt_id
    ]
    if not attempts:
        return False
    if len(attempts) != 1:
        raise ValueError("frozen generation attempt identity is duplicated")
    _, attempt = _attempt_record(snapshot, episode_n, attempt_id)
    if (
        episode.get("active_attempt_id") != attempt_id
        or episode.get("output_attempt_id") is not None
        or attempt.get("notebook_id") != notebook_id
        or attempt.get("title") != title.strip()
        or attempt.get("brief_sha256")
        != hashlib.sha256(brief.encode("utf-8")).hexdigest()
        # source_ids 跟 brief 一樣是生成輸入:換了來源集合就不是同一次生成,沿用
        # 這個 attempt 會讓 binding 說謊(「哪一份輸入產出了哪一集」)。
        or attempt.get("settings") != settings
        or attempt.get("input_bundle") != input_bundle
    ):
        raise ValueError("existing attempt does not match frozen generation input")
    status = attempt.get("dispatch", {}).get("status")
    if status == "prepared":
        return True
    if status == "not_accepted":
        if not _rearm_not_accepted_attempt(store, episode_n, attempt_id):
            raise RuntimeError("frozen attempt changed while being rearmed")
        return True
    raise ValueError(
        f"frozen attempt is {status!r}; reconcile or resume it instead of redispatching"
    )


async def _run_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str,
    output_dir: str,
    prior_mp3_path: str | None,
    language: str | None,
    audio_format: str | None,
    audio_length: str | None,
    wait_timeout: float,
    manifest_path: str | None = None,
    prepared_generation_input: dict | None = None,
    source_ids: list[str] | None = None,
) -> dict:
    client = runtime.get_client()
    os.makedirs(output_dir, exist_ok=True)

    _validate_episode_args(episode_n, title, prior_mp3_path)
    resolved_language = resolve_language(language)
    resolved_audio_format = to_audio_format(audio_format)
    resolved_audio_length = to_audio_length(audio_length)
    selected_source_ids = to_source_ids(source_ids)
    settings = _audio_settings(
        resolved_language, audio_format, audio_length, selected_source_ids
    )
    # 唯讀對帳,在建 attempt 與任何副作用之前:一筆打錯/已刪的 id 不會被伺服器擋下來。
    if selected_source_ids is not None:
        await assert_sources_exist(client, notebook_id, selected_source_ids)

    store = ManifestStore(manifest_path) if manifest_path else None
    attempt_id = None
    if store is not None:
        # 生成是不可逆的副作用(燒配額),所以清理義務在建 attempt 之前就要結案。
        await _assert_source_cleanup_done(client, store, notebook_id, episode_n)
        fixed_attempt_id = None
        binding_bytes = None
        binding_created = False
        input_bundle = None
        if prepared_generation_input is not None:
            existing_binding = read_attempt_binding(prepared_generation_input)
            if existing_binding is None:
                fixed_attempt_id = str(uuid.uuid4())
                input_bundle, binding_bytes = write_attempt_binding(
                    prepared_generation_input, attempt_id=fixed_attempt_id
                )
                binding_created = True
            else:
                fixed_attempt_id, input_bundle, binding_bytes = existing_binding
        try:
            reused = (
                prepared_generation_input is not None
                and fixed_attempt_id is not None
                and input_bundle is not None
                and _reuse_frozen_input_attempt(
                    store,
                    notebook_id=notebook_id,
                    episode_n=episode_n,
                    title=title,
                    brief=brief,
                    settings=settings,
                    attempt_id=fixed_attempt_id,
                    input_bundle=input_bundle,
                )
            )
            if reused:
                attempt_id = fixed_attempt_id
            else:
                attempt_id = _create_audio_attempt(
                    store,
                    notebook_id=notebook_id,
                    episode_n=episode_n,
                    title=title,
                    brief=brief,
                    language=resolved_language,
                    audio_format=audio_format,
                    audio_length=audio_length,
                    source_ids=selected_source_ids,
                    attempt_id=fixed_attempt_id,
                    input_bundle=input_bundle,
                )
        except BaseException as error:
            if (
                binding_created
                and prepared_generation_input is not None
                and binding_bytes is not None
            ):
                # 清理失敗只回一則 note、絕不 raise —— 在 except handler 裡再拋會把
                # 真正該讀的那個錯誤蓋掉。把殘留訊息掛回原例外,兩件事都看得到。
                note = rollback_attempt_binding(prepared_generation_input, binding_bytes)
                if note:
                    error.add_note(note)
            raise

    # Standalone continuity: if the caller hands us a prior episode's mp3 that is
    # NOT yet in the notebook (one-off podcast_episode use), upload + name it so
    # this episode can recap it. In a full podcast_series this is unnecessary —
    # each episode self-uploads at the end (below), so the prior is already there.
    if prior_mp3_path:
        prior_src = await client.sources.add_file(
            notebook_id,
            prior_mp3_path,
            mime_type="audio/mpeg",
            wait=True,
            wait_timeout=600.0,
        )
        await client.sources.rename(
            notebook_id, prior_src.id, f"EP{episode_n - 1:02d}", return_object=False
        )

    # 一次取好「送出這次生成的帳號 + 它的 client」:baseline / 記帳 / 實際送出三者
    # 必須同源。分開讀 `active_account()` 與 `get_client()` 中間隔著 await,並行的
    # 另一個工具呼叫在那個縫裡 rotate,就會變成 baseline 是 A 看到的、manifest 記 A、
    # 實際卻由 B 送出(ADR-0010 §Transparency:manifest 是唯一的稽核憑據)。
    dispatch_account, dispatch_client = runtime.snapshot()
    if store is not None:
        baseline = await dispatch_client.artifacts.list(
            notebook_id, artifact_type=ArtifactType.AUDIO
        )
        claimed = _claim_prepared_dispatch(
            store,
            episode_n,
            attempt_id,
            [artifact.id for artifact in baseline],
            account=dispatch_account,
        )
        if not claimed:
            raise RuntimeError(
                f"attempt {attempt_id!r} is no longer prepared; retry by its durable state"
            )

    async def _generate(dispatch_client: object):
        return await dispatch_client.artifacts.generate_audio(
            notebook_id,
            source_ids=selected_source_ids,
            language=resolved_language,
            instructions=brief,
            audio_format=resolved_audio_format,
            audio_length=resolved_audio_length,
        )

    try:
        # 三元組的後兩個是**實際送出這次生成的**帳號與 client(failover 換過就是換過
        # 之後那個)。finalize 一定要用它們 —— 見下方 `client = …` 那行的註解。
        artifact_id, dispatch_account, client = await _dispatch_audio_with_failover(
            store,
            episode_n,
            attempt_id,
            _generate,
            account=dispatch_account,
            client=dispatch_client,
        )
    except _REFUSED_WITHOUT_DISPATCH as exc:
        # 裸拋的話呼叫端只看到 SDK 的「rate limit exceeded」,不知道 attempt 已經被
        # 持久化、也不知道該怎麼續(v0.7.1 驗收 F-9)。docstring 承諾「依錯誤中的
        # attempt_id 續跑」,兩個分支都要兌現。
        if store is not None:
            exc.args = (
                f"{exc}\n伺服器拒絕了這次生成,**沒有**建立任何 artifact"
                f"(attempt_id={attempt_id!r},已標記 not_accepted)。"
                "配額/限流回復後,用**完全相同的參數**重呼 podcast_episode 即可沿用"
                "同一個 attempt 重送——不會新建 attempt、也不會多燒一次配額。",
            )
        raise
    except (Exception, asyncio.CancelledError) as exc:
        # 是不是「乾淨的 not_accepted」不能只看例外型別:`RuntimeError` 既是
        # `ensure_started`/`NotebookAccessDenied` 判定的 not_accepted(訊息已經自帶
        # 指引,bare raise 即可),也可能是 `_dispatch_audio_with_failover` 的泛用
        # except 分支剛好撞上這個型別、被標成 acceptance_unknown 的失敗 —— 只看型別
        # 分不出來,會讓後者的續跑指引被前者的 bare-raise 攔住(v0.7.2 舊碼走
        # `except Exception` 才拿得到這段指引,這是重構帶進來的回歸)。真正的
        # 判準是 manifest 裡記下的 dispatch 狀態,例外型別只是入場券——not_accepted
        # 才 bare-raise,其餘一律補上 reconcile 指引。
        if store is not None:
            # 讀 manifest 只為了「要不要貼指引」,不能讓它自己變成新的失敗來源:
            # `_attempt_record` 對已 retract 的 attempt 會 raise,而
            # `abandon_in_flight` 讓「並行 retract 掉一個還在飛的 attempt」成為**受支援
            # 的操作**——`_active_attempt_or_reraise` 當初判定「可達性極低」的前提不再
            # 成立。在 except handler 裡再拋會蓋掉真正該讓人看到的那個例外
            # (與 `generation_input.rollback` 的清理失敗同一條紀律)。
            try:
                current = store.read()
                _, stopped_attempt = _attempt_record(current, episode_n, attempt_id)
                unknown = stopped_attempt["dispatch"]["status"] != "not_accepted"
            except Exception:
                unknown = False
            if unknown:
                exc.args = (
                    f"{exc}\n生成受理結果不明(attempt_id={attempt_id!r})；"
                    "先對帳，禁止直接重生："
                    f"podcast_episode_reconcile(manifest_path={manifest_path!r}, "
                    f"episode_n={episode_n}, attempt_id={attempt_id!r})",
                )
        raise
    # `client` 已由上面的三元組換成**實際送出這次生成的**那一個。這裡曾經是
    # `client = runtime.get_client()`,註解宣稱「用實際送出的那一個」,但那行結構上
    # 給不出這個保證:它讀的是「此刻游標指到誰」。而 finalize(等生成 → 下載 → 回錄
    # 上傳 → rename)是數十分鐘,並行的另一集在這段期間 rotate 掉游標是常態不是意外。
    if store is not None:
        _bind_accepted_artifact(store, episode_n, attempt_id, artifact_id)

    # 生成一旦送出,artifact 就在 NotebookLM 雲端建立並跑到完成,不靠本地連線活著。
    try:
        if store is not None:
            output = await finalize_attempt(
                client,
                store,
                episode_n=episode_n,
                attempt_id=attempt_id,
                output_dir=output_dir,
                wait_timeout=wait_timeout,
            )
            _promote_attempt_output(store, episode_n, attempt_id, output)
        else:
            output = await _finalize_episode(
                notebook_id, episode_n, title, artifact_id, output_dir, wait_timeout
            )
        return output
    except TerminalGenerationError:
        # 伺服器端終態(failed / removed,如每日配額耗盡):artifact 已被下架,resume
        # 也救不回——原樣往上拋,不誤導成「可續跑」。用專屬型別而非 except RuntimeError,
        # 才不會把下載/命名步驟意外的 RuntimeError 也當成不可續跑。
        raise
    except Exception as exc:
        # 其餘失敗(本地 wait 超時、下載中斷、網路斷)發生在生成之後,artifact 仍在雲端
        # 完好。就地改寫 exc.args 附上 artifact_id + 現成的 podcast_episode_resume 呼叫,
        # 再原樣 re-raise —— 保留原例外「型別與結構化欄位」(SDK 的 ArtifactTimeoutError
        # 等 constructor 需 notebook_id/task_id/timeout 多個必填參數,type(exc)(str) 會
        # 反而 TypeError 吞掉真錯;改寫 args 對內建與 SDK 例外都能把 hint 帶進 str(exc))。
        # 呼叫端據此續完(不重生、不燒 quota),不必再 artifact_list 撈 id。
        durable_argument = (
            f", manifest_path={manifest_path!r}" if manifest_path else ""
        )
        exc.args = (
            f"{exc}\n音檔已在雲端生成(artifact_id={artifact_id!r})但後續步驟失敗。"
            f"既有 attempt_id={attempt_id!r}；用 podcast_episode_resume "
            "續完(不會重新生成):"
            f"podcast_episode_resume(notebook_id={notebook_id!r}, episode_n={episode_n}, "
            f"title={title.strip()!r}, artifact_id={artifact_id!r}, "
            f"output_dir={output_dir!r}{durable_argument})",
        )
        raise


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
async def podcast_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str | None,
    output_dir: str,
    prior_mp3_path: str | None = None,
    language: str | None = None,
    audio_format: str | None = "deep-dive",
    audio_length: str | None = "long",
    wait_timeout: float = 1200.0,
    manifest_path: str | None = None,
    input_bundle_path: str | None = None,
    source_ids: list[str] | None = None,
) -> dict:
    """生成、命名、下載並回錄一集 podcast。

    Studio artifact 與回錄 source 都命名為 ``EP{n:02d} {title}``。

    傳 ``manifest_path`` 時，attempt 會在任何遠端 generation 副作用前持久化；
    若另傳 ``input_bundle_path``（相對 workspace 的路徑），``brief`` 必須為 ``None``，
    provider 輸入只來自驗過雜湊的 frozen bytes。timeout／斷線後須依錯誤中的
    ``attempt_id`` 呼叫 ``podcast_episode_reconcile``，不可重送本工具來重生。
    finalize 各步驟皆 checkpoint，可用 ``podcast_episode_resume`` 接續。不傳 manifest
    則保留 standalone best-effort 行為。

    ``source_ids`` 指名這一集只讀哪幾筆來源（用 ``source_list`` 取得真實 id）；省略則用
    筆記本全部來源。**回頭重生某一集時要傳**：筆記本此時已有後續各集的題目與音檔回錄，
    不指名就會讓那些內容洩進這一集。它與 language／format／length 一樣算生成輸入，會存進
    attempt settings，resume 時不得改動。
    """
    # 本地驗證先行(壞參數 ValueError 秒退,不浪費 RPC),再做認證預檢:
    # 單集也要等最多 20 分鐘,cookie 死了先秒退(見 auth_probe docstring)。
    _validate_episode_args(episode_n, title, prior_mp3_path)
    source_ids = to_source_ids(source_ids)
    if manifest_path and prior_mp3_path:
        raise ValueError(
            "prior_mp3_path with manifest_path is not checkpointed in P0; "
            "use an already verified notebook source or standalone best-effort"
        )
    prepared_generation_input = None
    if input_bundle_path is not None:
        if brief is not None:
            raise ValueError(
                "brief must be None when input_bundle_path is provided; "
                "the provider input comes only from frozen bytes"
            )
        if not isinstance(manifest_path, str) or not manifest_path:
            raise ValueError("input_bundle_path requires manifest_path")
        prepared_generation_input = load_frozen_generation_input(
            manifest_path=manifest_path,
            input_bundle_path=input_bundle_path,
            episode_n=episode_n,
        )
        # Existing sidecars are replay authority. Validate their request/workspace
        # identity before auth or any cleanup/baseline RPC; a copied bound bundle
        # must be rejected without touching the provider.
        read_attempt_binding(prepared_generation_input)
        brief = prepared_generation_input["brief"]
    elif not isinstance(brief, str) or not brief.strip():
        raise ValueError("brief must be a non-empty string without input_bundle_path")
    await probe_auth(runtime.get_client())
    return await _run_episode(
        notebook_id,
        episode_n,
        title,
        brief,
        output_dir,
        prior_mp3_path,
        language,
        audio_format,
        audio_length,
        wait_timeout,
        manifest_path=manifest_path,
        prepared_generation_input=prepared_generation_input,
        source_ids=source_ids,
    )


@mcp.tool()
async def podcast_episode_reconcile(
    manifest_path: str,
    episode_n: int,
    attempt_id: str,
    wait_timeout: float = 1200.0,
) -> dict:
    """在不重新生成的前提下，找回遺失的 ``generate_audio`` response。

    只收養一筆建立時間符合持久化 dispatch window、且尚未被認領的 audio artifact；
    零筆或多筆候選都安全停止，不做猜測。
    """
    if not isinstance(manifest_path, str) or not manifest_path:
        raise ValueError("manifest_path must be a non-empty string")
    if not isinstance(episode_n, int) or isinstance(episode_n, bool) or episode_n < 1:
        raise ValueError("episode_n must be an int >= 1")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("attempt_id must be a non-empty string")
    if (
        not isinstance(wait_timeout, (int, float))
        or isinstance(wait_timeout, bool)
        or wait_timeout <= 0
    ):
        raise ValueError("wait_timeout must be greater than zero")
    _require_existing_manifest(manifest_path)

    store = ManifestStore(manifest_path)
    snapshot = store.read()
    attempt, notebook_id, baseline, dispatched_at = _reconciliation_subject(
        snapshot, episode_n, attempt_id
    )
    dispatch_status = attempt["dispatch"].get("status")
    remote_artifact_id = attempt["remote"].get("artifact_id")
    if remote_artifact_id is not None:
        if dispatch_status != "accepted":
            raise ValueError("attempt has an inconsistent remote artifact mapping")
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "accepted",
            "artifact_id": remote_artifact_id,
            "safe_next_action": ACTION_RESUME,
        }
    if dispatch_status == "dispatching":
        def mark_unknown(manifest: dict) -> None:
            _, current = _attempt_record(manifest, episode_n, attempt_id)
            if current["dispatch"].get("status") == "dispatching":
                current["dispatch"]["status"] = "acceptance_unknown"

        snapshot, _ = store.update(mark_unknown)
        attempt, notebook_id, baseline, dispatched_at = _reconciliation_subject(
            snapshot, episode_n, attempt_id
        )
        dispatch_status = attempt["dispatch"].get("status")
    if dispatch_status not in ("acceptance_unknown", "reconciliation_ambiguous"):
        raise ValueError(
            f"attempt state {dispatch_status!r} cannot be reconciled"
        )

    client = runtime.get_client()
    await probe_auth(client)
    artifacts = await client.artifacts.list(
        notebook_id, artifact_type=ArtifactType.AUDIO
    )
    claimed = _claimed_artifact_ids(snapshot, attempt_id)
    window_start = dispatched_at - _RECONCILIATION_CLOCK_SKEW
    window_end = (
        dispatched_at
        + timedelta(seconds=float(wait_timeout))
        + _RECONCILIATION_CLOCK_SKEW
    )
    candidates: set[str] = set()
    for artifact in artifacts:
        artifact_id = getattr(artifact, "id", None)
        kind = getattr(getattr(artifact, "kind", None), "value", None)
        created_at = _artifact_created_at_utc(
            getattr(artifact, "created_at", None)
        )
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or artifact_id in baseline
            or artifact_id in claimed
            or kind != ArtifactType.AUDIO.value
            or created_at is None
        ):
            continue
        if window_start <= created_at <= window_end:
            candidates.add(artifact_id)

    candidate_ids = sorted(candidates)
    if len(candidate_ids) == 1:
        artifact_id = candidate_ids[0]
        _bind_reconciled_artifact(
            store, episode_n, attempt_id, artifact_id
        )
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "accepted",
            "artifact_id": artifact_id,
            "safe_next_action": ACTION_RESUME,
        }
    if len(candidate_ids) > 1:
        _mark_reconciliation_ambiguous(
            store, episode_n, attempt_id, candidate_ids
        )
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "reconciliation_ambiguous",
            "candidate_artifact_ids": candidate_ids,
            "safe_next_action": ACTION_ADOPT,
        }

    latest = store.read()
    latest_attempt, _, _, _ = _reconciliation_subject(
        latest, episode_n, attempt_id
    )
    latest_artifact_id = latest_attempt["remote"].get("artifact_id")
    if latest_artifact_id is not None:
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "accepted",
            "artifact_id": latest_artifact_id,
            "safe_next_action": ACTION_RESUME,
        }
    return {
        "complete": False,
        "episode_n": episode_n,
        "attempt_id": attempt_id,
        "observed_state": latest_attempt["dispatch"]["status"],
        "candidate_artifact_ids": [],
        "safe_next_action": ACTION_RECONCILE,
    }


@mcp.tool()
async def podcast_episode_resume(
    notebook_id: str,
    episode_n: int,
    title: str,
    artifact_id: str,
    output_dir: str,
    wait_timeout: float = 1200.0,
    manifest_path: str | None = None,
) -> dict:
    """接續一個「已在 NotebookLM 雲端啟動」的音檔生成,續完後半段而**不重新生成**。

    ``artifact_id`` 應來自原呼叫的 durable attempt 或
    ``podcast_episode_reconcile`` 唯一收養結果，不可自行猜「最新」artifact。

    傳 ``manifest_path`` 時，各 finalize 步驟皆依 checkpoint 與 postcondition
    冪等接續，重複呼叫不會新增第二筆 source。不傳時僅 best-effort。
    """
    # 本地驗證先行(壞參數 ValueError 秒退),再認證預檢——與 podcast_episode 同一
    # fail-fast 順序:認證錯誤不得蓋掉參數錯誤。
    _validate_episode_args(episode_n, title, None)
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id 必填(從 durable attempt 或 reconcile 結果取得)")
    if manifest_path:
        _require_existing_manifest(
            manifest_path,
            missing_hint=(
                "for a legacy artifact with no manifest yet, call this tool "
                "without manifest_path (standalone best-effort)"
            ),
        )
    await probe_auth(runtime.get_client())
    if manifest_path:
        store = ManifestStore(manifest_path)
        # 與 `_run_episode` 同一道 gate:retract 留下的清理義務未結案前不得繼續產出。
        # resume 也會 upload 回錄 source,漏這道就等於留一條繞過去的路(舊版真的漏了)。
        await _assert_source_cleanup_done(
            runtime.get_client(), store, notebook_id, episode_n
        )
        attempt_id = _ensure_resume_attempt(
            store,
            notebook_id=notebook_id,
            episode_n=episode_n,
            title=title,
            artifact_id=artifact_id.strip(),
        )
        output = await finalize_attempt(
            runtime.get_client(),
            store,
            episode_n=episode_n,
            attempt_id=attempt_id,
            output_dir=output_dir,
            wait_timeout=wait_timeout,
        )
        _promote_attempt_output(store, episode_n, attempt_id, output)
        return output

    output = await _finalize_episode(
        notebook_id, episode_n, title, artifact_id.strip(), output_dir, wait_timeout
    )
    output["durability_warning"] = (
        "manifest_path 未提供；這次 standalone resume 只有 best-effort，"
        "重呼可能重做 download 或 feedback source upload。"
    )
    return output


def _source_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


def _source_is_ready(source: object) -> bool:
    ready = getattr(source, "is_ready", None)
    if isinstance(ready, bool):
        return ready
    return _source_value(getattr(source, "status", None)) in (
        "ready",
        "completed",
    )


async def _continuity_source_verified(
    client: object,
    notebook_id: str,
    label: str,
    *,
    source_id: str | None,
) -> bool:
    sources = await client.sources.list(notebook_id)
    matches = [
        source
        for source in sources
        if (source_id is None or getattr(source, "id", None) == source_id)
        and getattr(source, "title", None) == label
        and _source_value(getattr(source, "kind", None)) == "media"
        and _source_is_ready(source)
    ]
    return len(matches) == 1


def _validate_adoption_identity(
    manifest: dict,
    episode: dict,
    attempt: dict | None,
    *,
    notebook_id: str,
    episode_n: int,
    title: str,
) -> None:
    if episode.get("episode") != episode_n:
        raise ValueError("adoption subject belongs to a different episode")
    if episode.get("notebook_id") not in (None, notebook_id):
        raise ValueError("adoption subject belongs to a different notebook")
    current_title = episode.get("title")
    if not isinstance(current_title, str) or current_title.strip() != title.strip():
        raise ValueError("adoption subject title changed")
    if attempt is None:
        return
    if attempt.get("notebook_id") != notebook_id:
        raise ValueError("attempt belongs to a different notebook")
    if attempt.get("episode") not in (None, episode_n):
        raise ValueError("attempt belongs to a different episode")
    attempt_title = attempt.get("title")
    if (
        not isinstance(attempt_title, str)
        or attempt_title.strip() != title.strip()
    ):
        raise ValueError("attempt title does not match the episode")


def _attempt_can_adopt_source(attempt: dict) -> bool:
    remote = attempt.get("remote", {})
    download = attempt.get("finalize", {}).get("download", {})
    return (
        isinstance(remote.get("artifact_id"), str)
        and bool(remote["artifact_id"])
        and remote.get("status") == "completed"
        and download.get("status") == "completed"
        and isinstance(download.get("path"), str)
        and bool(download["path"])
        and isinstance(download.get("bytes"), int)
        and download["bytes"] > 0
        and isinstance(download.get("sha256"), str)
        and bool(download["sha256"])
    )


def _queue_pending_source_cleanup(
    episode: dict, source_ids: list[str], *, exclude: str
) -> list[str]:
    """把 adopt 換掉的舊 source(s)排進這一集的 ``pending_source_cleanup``(去重、
    排除這次選中的那筆),讓 `_assert_source_cleanup_done` 在下一次生成前逼刪——
    否則同名重複 source 從此沒人記得,後續每集生成都讀到它。

    回傳的是**這一集尚未結案的全部義務**,不是只有這次新增的那幾筆:adopt 回應遺失時
    host 會冪等重呼,若第二次回傳空清單、`safe_next_action` 就會從 source_delete 翻回
    series,host 照著走卻被 gate 硬擋成 ValueError(狀態冪等、指引卻不冪等,等於把
    自動化 host 導進死路)。"""
    to_queue = [
        source_id
        for source_id in dict.fromkeys(source_ids)  # 去重、保留順序
        if isinstance(source_id, str) and source_id and source_id != exclude
    ]
    if to_queue:
        # 只在真的有東西要排時才建 key,維持「沒有義務就沒有這個欄位」的既有形狀。
        pending = episode.setdefault("pending_source_cleanup", [])
        for source_id in to_queue:
            if source_id not in pending:
                pending.append(source_id)
    return list(episode.get("pending_source_cleanup", []))


@mcp.tool()
async def podcast_attempt_adopt(
    manifest_path: str,
    episode_n: int,
    attempt_id: str | None = None,
    artifact_id: str | None = None,
    feedback_source_id: str | None = None,
) -> dict:
    """將 caller 明確選定且已驗證的遠端 identity 原子綁回既有產製記錄。

    本工具只做 identity adoption，不會 generate、upload、rename 或 download。
    """
    if not isinstance(manifest_path, str) or not manifest_path:
        raise ValueError("manifest_path must be a non-empty string")
    if not isinstance(episode_n, int) or isinstance(episode_n, bool) or episode_n < 1:
        raise ValueError("episode_n must be an int >= 1")
    if attempt_id is not None and (
        not isinstance(attempt_id, str) or not attempt_id
    ):
        raise ValueError("attempt_id must be a non-empty string when provided")

    remote_ids = [
        value
        for value in (artifact_id, feedback_source_id)
        if isinstance(value, str) and value
    ]
    if len(remote_ids) != 1 or (
        artifact_id is not None
        and (not isinstance(artifact_id, str) or not artifact_id)
    ) or (
        feedback_source_id is not None
        and (
            not isinstance(feedback_source_id, str)
            or not feedback_source_id
        )
    ):
        raise ValueError(
            "provide exactly one non-empty artifact_id or feedback_source_id"
        )
    _require_existing_manifest(manifest_path)

    store = ManifestStore(manifest_path)
    snapshot = store.read()
    episode = next(
        (
            row
            for row in snapshot["episodes"]
            if row.get("episode") == episode_n
        ),
        None,
    )
    if episode is None:
        raise ValueError(f"episode {episode_n} is missing from the manifest")
    title = episode.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"episode {episode_n} title is missing")
    label = _episode_label(episode_n, title)

    attempt = None
    if attempt_id is not None:
        _, attempt = _attempt_record(snapshot, episode_n, attempt_id)
        notebook_id = attempt.get("notebook_id")
    else:
        notebook_id = episode.get("notebook_id", snapshot.get("notebook_id"))
    if not isinstance(notebook_id, str) or not notebook_id:
        raise ValueError("adoption subject has no notebook_id")

    episode_notebook_id = episode.get("notebook_id")
    if episode_notebook_id not in (None, notebook_id):
        raise ValueError("attempt belongs to a different notebook than the episode")
    if attempt is not None:
        if attempt.get("episode") not in (None, episode_n):
            raise ValueError("attempt episode identity does not match the manifest")
        attempt_title = attempt.get("title")
        if (
            not isinstance(attempt_title, str)
            or attempt_title.strip() != title.strip()
        ):
            raise ValueError("attempt title does not match the episode")

    if (
        feedback_source_id is not None
        and attempt is not None
        and not _attempt_can_adopt_source(attempt)
    ):
        raise ValueError("feedback source adoption requires completed audio download")

    client = runtime.get_client()
    await probe_auth(client)

    if artifact_id is not None:
        if attempt_id is None or attempt is None:
            raise ValueError("artifact adoption requires attempt_id")
        dispatch = attempt.get("dispatch", {})
        dispatch_status = dispatch.get("status")
        if dispatch_status not in (
            "acceptance_unknown",
            "reconciliation_ambiguous",
        ):
            raise ValueError(
                "artifact adoption requires an unresolved reconciliation state"
            )
        candidate_ids = dispatch.get("candidate_artifact_ids", [])
        if (
            dispatch_status == "reconciliation_ambiguous"
            and artifact_id not in candidate_ids
        ):
            raise ValueError(
                f"artifact {artifact_id!r} is not an ambiguity candidate"
            )
        artifacts = await client.artifacts.list(
            notebook_id, artifact_type=ArtifactType.AUDIO
        )
        matches = [
            artifact
            for artifact in artifacts
            if getattr(artifact, "id", None) == artifact_id
            and _source_value(getattr(artifact, "kind", None))
            == ArtifactType.AUDIO.value
        ]
        if len(matches) != 1:
            raise ValueError(
                f"artifact {artifact_id!r} is not one unique audio artifact"
            )

        if dispatch_status == "acceptance_unknown":
            _, _, baseline, dispatched_at = _reconciliation_subject(
                snapshot, episode_n, attempt_id
            )
            created_at = _artifact_created_at_utc(
                getattr(matches[0], "created_at", None)
            )
            if (
                artifact_id in baseline
                or created_at is None
            ):
                raise ValueError(
                    "explicit artifact is not a post-dispatch candidate"
                )
            if not (
                dispatched_at - _RECONCILIATION_CLOCK_SKEW
                <= created_at
                <= datetime.now(timezone.utc) + _RECONCILIATION_CLOCK_SKEW
            ):
                raise ValueError("explicit artifact is outside the dispatch window")

        def adopt_artifact(manifest: dict) -> None:
            current_episode, current = _attempt_record(
                manifest, episode_n, attempt_id
            )
            _validate_adoption_identity(
                manifest, current_episode, current,
                notebook_id=notebook_id, episode_n=episode_n, title=title,
            )
            current_dispatch = current["dispatch"]
            current_status = current_dispatch.get("status")
            if current_status not in (
                "acceptance_unknown",
                "reconciliation_ambiguous",
            ):
                raise ValueError("attempt reconciliation state changed")
            if (
                current_status == "reconciliation_ambiguous"
                and artifact_id not in current_dispatch.get(
                    "candidate_artifact_ids", []
                )
            ):
                raise ValueError("artifact is no longer an ambiguity candidate")
            if (
                current_status == "acceptance_unknown"
                and artifact_id in current_dispatch.get("artifact_ids_before", [])
            ):
                raise ValueError("artifact was already present before dispatch")
            if artifact_id in _claimed_artifact_ids(manifest, attempt_id):
                raise ValueError(f"artifact {artifact_id!r} is already claimed")
            if current["remote"].get("artifact_id") is not None:
                raise ValueError("attempt already has a remote artifact mapping")
            now = datetime.now(timezone.utc).isoformat()
            current_dispatch["status"] = "accepted"
            current_dispatch["accepted_at"] = now
            current["remote"].update(
                {
                    "artifact_id": artifact_id,
                    "status": "pending",
                    "status_origin": "caller_verified",
                    "observed_at": now,
                    "error": None,
                    "error_code": None,
                }
            )

        store.update(adopt_artifact)
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "artifact_id": artifact_id,
            "observed_state": "accepted",
            "safe_next_action": ACTION_RESUME,
        }

    assert feedback_source_id is not None
    upload = (
        attempt.get("finalize", {}).get("feedback_source_upload", {})
        if attempt is not None
        else {}
    )
    candidate_pending_rename = (
        upload.get("status") == "reconciliation_ambiguous"
        and feedback_source_id in upload.get("candidate_source_ids", [])
    )
    allowed_titles = {label}
    expected_upload_title = upload.get("expected_title")
    if (
        candidate_pending_rename
        and isinstance(expected_upload_title, str)
        and expected_upload_title
    ):
        allowed_titles.add(expected_upload_title)
    sources = await client.sources.list(notebook_id)
    matches = [
        source
        for source in sources
        if getattr(source, "id", None) == feedback_source_id
        and getattr(source, "title", None) in allowed_titles
        and _source_value(getattr(source, "kind", None)) == "media"
        and _source_is_ready(source)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"feedback source {feedback_source_id!r} must uniquely match "
            "a ready media source with the expected identity"
        )
    needs_rename = getattr(matches[0], "title", None) != label

    def source_claimed_elsewhere(manifest: dict) -> bool:
        for row in manifest["episodes"]:
            if (
                row.get("episode") != episode_n
                and row.get("feedback_source_id") == feedback_source_id
            ):
                return True
            for current in row.get("attempts", []):
                if (
                    current.get("attempt_id") == attempt_id
                    and row.get("episode") == episode_n
                ):
                    continue
                source_id = (
                    current.get("finalize", {})
                    .get("feedback_source_upload", {})
                    .get("source_id")
                )
                if source_id == feedback_source_id:
                    return True
        return False

    def adopt_source(manifest: dict) -> list[str]:
        current_episode = next(
            row
            for row in manifest["episodes"]
            if row.get("episode") == episode_n
        )
        current_attempt = None
        if attempt_id is not None:
            _, current_attempt = _attempt_record(
                manifest, episode_n, attempt_id
            )
        _validate_adoption_identity(
            manifest, current_episode, current_attempt,
            notebook_id=notebook_id, episode_n=episode_n, title=title,
        )
        if source_claimed_elsewhere(manifest):
            raise ValueError(
                f"feedback source {feedback_source_id!r} is already claimed"
            )
        now = datetime.now(timezone.utc).isoformat()
        if attempt_id is None:
            if current_episode.get("attempts") or not has_durable_output_evidence(
                current_episode
            ):
                raise ValueError(
                    "attempt_id may be omitted only for legacy durable output"
                )
            previous = current_episode.get("feedback_source_id")
            if previous not in (None, feedback_source_id):
                history = current_episode.setdefault(
                    "previous_feedback_source_ids", []
                )
                if previous not in history:
                    history.append(previous)
            current_episode["feedback_source_id"] = feedback_source_id
            current_episode["feedback_source_adopted_at"] = now
            # 被取代的舊 id 只記進歷史還不夠——同名重複 source 從此沒人記得,故也排進
            # 清理義務,讓下一次生成前的 gate 逼刪(見 `_queue_pending_source_cleanup`)。
            return _queue_pending_source_cleanup(
                current_episode,
                [previous] if isinstance(previous, str) else [],
                exclude=feedback_source_id,
            )

        assert current_attempt is not None
        if not _attempt_can_adopt_source(current_attempt):
            raise ValueError(
                "feedback source adoption requires completed audio download"
            )
        current = current_attempt
        upload = current["finalize"]["feedback_source_upload"]
        if needs_rename and not (
            upload.get("status") == "reconciliation_ambiguous"
            and feedback_source_id
            in upload.get("candidate_source_ids", [])
            and upload.get("expected_title") == expected_upload_title
        ):
            raise ValueError(
                "feedback source is no longer an ambiguity candidate"
            )
        previous = upload.get("source_id")
        # 未被選中的 candidate(reconciliation_ambiguous 遺留)與被取代的 previous 一樣是
        # 同名重複 source——`upload.update` 下面即將把 candidate_source_ids 清空,先在
        # 清空前抓下來,連同 previous 一起排入清理義務。
        stale_candidates = [
            source_id
            for source_id in upload.get("candidate_source_ids", [])
            if source_id != feedback_source_id
        ]
        if previous not in (None, feedback_source_id):
            history = upload.setdefault("previous_source_ids", [])
            if previous not in history:
                history.append(previous)
        upload.update(
            {
                "status": "accepted" if needs_rename else "completed",
                "source_id": feedback_source_id,
                "adopted_at": now,
                "candidate_source_ids": [],
            }
        )
        rename = current["finalize"]["feedback_source_rename"]
        if needs_rename:
            rename.update(
                {
                    "status": "not_started",
                    "source_name": None,
                    "verified_at": None,
                }
            )
        else:
            rename.update({"status": "completed", "adopted_at": now})
        return _queue_pending_source_cleanup(
            current_episode,
            [*stale_candidates, previous] if isinstance(previous, str) else stale_candidates,
            exclude=feedback_source_id,
        )

    _, stale_source_ids = store.update(adopt_source)
    result = {
        "complete": not needs_rename,
        "episode_n": episode_n,
        "attempt_id": attempt_id,
        "feedback_source_id": feedback_source_id,
        "observed_state": (
            "accepted" if needs_rename else "continuity_verified"
        ),
        "safe_next_action": ACTION_RESUME if needs_rename else ACTION_SERIES,
    }
    if stale_source_ids:
        result["stale_source_ids"] = stale_source_ids
        result["safe_next_action"] = ACTION_SOURCE_DELETE
    return result


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True))
async def podcast_attempt_retract(
    manifest_path: str,
    episode_n: int,
    attempt_id: str,
    reason: str,
    abandon_in_flight: bool = False,
) -> dict:
    """QA 拒收的唯一正門：純本機 manifest mutation，不打 RPC、不動遠端或本機檔案。

    ``attempt_id`` 必須是該集的 ``output_attempt_id``，或掛在 ``active_attempt_id`` 但
    從未 promote 的未授權 candidate；``reason`` 必填非空。

    ``abandon_in_flight=True`` 是「**輸入就是錯的**」那一種作廢：伺服器已經受理生成
    （dispatch=accepted），但送進去的 brief 本身有問題，等它跑完也沒有意義。這件事
    manifest **推導不出來**——它與「第一次 dispatch、還在飛」的狀態逐欄位相同，差別只在
    呼叫端握有的外部知識，所以必須顯式宣告，預設不開。沒有這個旗標時那個狀態刻意留給
    ``podcast_episode_reconcile`` / ``podcast_episode_resume``。

    **它省不了配額**：生成已經在燒，retract 是純本機動作、取消不了遠端。省的是整整
    一輪 finalize（下載 mp3 → 上傳回錄 source → promote → 再 retract → ``source_delete``）
    以及那筆回錄 source 對後續各集 context 的污染。遠端那個 artifact 會成為孤兒，但
    ``_claimed_artifact_ids`` 掃所有 attempt（含 retracted），所以它不會被後續 reconcile
    誤 claim。

    retract 後必須把回傳的 ``stale_source_ids`` 逐一 ``source_delete``：下一次生成或
    resume 前會實際查 notebook 驗證，還在就 fail-closed。標題不可在取代時改。同一
    attempt 重呼冪等。

    詳見 skill ``references/tool-reference.md`` 與 ADR-0009。
    """
    if not isinstance(manifest_path, str) or not manifest_path:
        raise ValueError("manifest_path must be a non-empty string")
    if not isinstance(episode_n, int) or isinstance(episode_n, bool) or episode_n < 1:
        raise ValueError("episode_n must be an int >= 1")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("attempt_id must be a non-empty string")
    if not isinstance(reason, str) or not reason.strip():
        # retract 是審計事件:沒有理由的作廢等於無聲覆寫,正是本工具要取代的東西。
        raise ValueError("reason must be a non-empty string")
    if not isinstance(abandon_in_flight, bool):
        raise ValueError("abandon_in_flight must be a bool")
    _require_existing_manifest(manifest_path)

    _RETRACTED_EPISODE_KEYS = (
        "output_attempt_id",
        "artifact_id",
        "task_id",
        "mp3_path",
        "published_at",
        "feedback_source_id",
        "feedback_source_adopted_at",
    )

    def mutate(manifest: dict) -> dict:
        episode, attempt = _attempt_record(
            manifest, episode_n, attempt_id, allow_retracted=True
        )
        existing = attempt.get("retraction")
        if existing is not None:
            # 冪等,但**只對自己的殘留值**:此時 output 可能已經是取代版 B,無條件重跑
            # pop 會把 B 的 artifact_id／mp3_path／published_at 全清掉(等於毀掉取代版)。
            # 所以只在「output 還是自己或空」時,清掉值仍等於當初被作廢那份的欄位。
            if episode.get("output_attempt_id") in (None, attempt_id):
                for key, value in existing.get("retracted_output", {}).items():
                    if key in episode and episode[key] == value:
                        del episode[key]
                if episode.get("active_attempt_id") == attempt_id:
                    del episode["active_attempt_id"]
            return dict(existing)  # 不重寫 retracted_at／reason

        output_attempt_id = episode.get("output_attempt_id")
        active_attempt_id = episode.get("active_attempt_id")
        # 受審計的 abandon 涵蓋兩種無路可走的分岔(ADR-0009 補記:output=None 也可以是
        # active/output divergence 的一種——「這集從沒有過任何合法輸出,卻已經有 legacy
        # 硬證據卡在 episode 級投影上」——不是全新分支):
        #  (1) active=B／output=A(舊版工具或手改造出的——現在 `_ensure_resume_attempt`
        #      兩條分支都擋掉了)。
        #  (2) active=B、output_attempt_id 是 None,但 episode 已有
        #      `has_hard_output_evidence`(v0.5.0 前直接掛在 episode 上的
        #      artifact_id/mp3_path 等):B 的 dispatch 已 accepted(伺服器已受理生成)
        #      但從未 promote(真實事故:透過 `podcast_episode_resume` 接上錯的
        #      artifact,brief 失真但已經送出)。這個 legacy 證據存在,結構上只有
        #      `_ensure_resume_attempt` 的新建分支能繞過 `has_hard_output_evidence`
        #      guard 造出來(`_create_audio_attempt` 一律先擋掉),不會跟「單純第一次
        #      dispatch、還在飛」混在一起。
        # **刻意不是「output_attempt_id is None 就算」**:一個全新 episode 的第一顆
        # attempt 剛 dispatch、`wait_for_completion` 還沒回來,同樣是
        # active=self／output=None,但那是 reconcile／resume 的守備範圍,不是
        # retract 的(`test_retract_refuses_an_attempt_that_is_not_the_durable_output`
        # 鎖著)——沒有任何 legacy 證據時,「還沒完成」不等於「無路可走的分岔」。
        # 兩種都無路可走時:retract 被「只有 promoted output 才能 retract」卡、原樣
        # 重呼被 `_is_resendable_same_request` 擋(dispatch 已 accepted)、傳
        # supersedes_attempt_id 撞「is not terminal」、adopt/reconcile 都要求已
        # finalize。允許作廢這個「從未 promote、無人授權」的 candidate 就是唯一出口,
        # 而且留下理由。
        # (3) `abandon_in_flight`:呼叫端顯式宣告「這顆的**輸入**就是錯的」。
        # 那是 manifest 推導不出來的外部知識——(2) 之外的 in-flight 第一顆 attempt 與
        # 「正常還在跑」逐欄位相同,所以只能由呼叫端說,不能由狀態猜。預設 False 讓
        # reconcile/resume 的守備範圍原封不動;宣告了就等同 (2):從未 promote、無人
        # 授權的 candidate,作廢它並留下理由。
        abandons_unauthorized_candidate = (
            attempt_id == active_attempt_id
            and output_attempt_id != attempt_id
            and (
                output_attempt_id is not None
                or has_hard_output_evidence(episode)
                or abandon_in_flight
            )
        )
        if output_attempt_id != attempt_id and not abandons_unauthorized_candidate:
            raise ValueError(
                f"attempt {attempt_id!r} is not episode {episode_n}'s durable "
                "output; only a promoted output attempt can be retracted"
            )
        if not abandons_unauthorized_candidate and active_attempt_id not in (
            None,
            attempt_id,
        ):
            raise ValueError(
                f"episode {episode_n} still has active attempt "
                f"{active_attempt_id!r}; retract that unauthorized candidate first "
                "(it was never promoted), then retract the output"
            )
        if abandons_unauthorized_candidate and output_attempt_id is not None:
            # (1):episode 級投影屬於現任 output(A),一個字都不能動。
            retracted_output: dict = {}
            del episode["active_attempt_id"]
        else:
            # 正常 retract,或 (2):這集從沒有過合法輸出,episode 級投影(若有,通常是
            # v0.5.0 前直接掛在 episode 上的 legacy 硬證據)不屬於任何人,必須跟著清掉
            # ——否則下一次重生會撞上 `has_hard_output_evidence` 的另一個死路(補一半的
            # 又一例:只開了 retract 這道門,沒清 legacy 欄位等於換了個地方繼續擋死)。
            # pop 而非設 None:_promote_attempt_output 用 setdefault 寫 published_at,
            # 留一個 None 值會讓取代版補不回真正的產製時間(publish 有 fallback、不會爆,
            # 只會靜默發出假日期,而 podcast-lab 的完成門會先擋下來)。
            retracted_output = {
                key: episode.pop(key) for key in _RETRACTED_EPISODE_KEYS if key in episode
            }
            if episode.get("active_attempt_id") == attempt_id:
                del episode["active_attempt_id"]
        # 清理義務要以 attempt 的 finalize checkpoint 為準,不能只信 episode 級投影:
        # 投影是 legacy 相容欄位,可能缺、可能被 adopt 改寫,而真正上傳了哪一筆 source
        # 只有 checkpoint 知道。
        upload = attempt.get("finalize", {}).get("feedback_source_upload", {})
        stale_source_ids = [
            source_id
            for source_id in dict.fromkeys(
                [retracted_output.get("feedback_source_id"), upload.get("source_id")]
            )
            if isinstance(source_id, str) and source_id
        ]
        if stale_source_ids:
            # 沿用 adopt 既有的 episode 級歷史欄位,呼叫端忽略回傳值時仍留得住。
            history = episode.setdefault("previous_feedback_source_ids", [])
            # 未完成的清理義務。這不只是提示:下一次生成／resume 前會真的去 notebook
            # 驗它已經不在(見 `_assert_source_cleanup_done`),還在就 fail-closed。
            pending = episode.setdefault("pending_source_cleanup", [])
            for source_id in stale_source_ids:
                if source_id not in history:
                    history.append(source_id)
                if source_id not in pending:
                    pending.append(source_id)
        retraction = {
            "episode": episode_n,
            "attempt_id": attempt_id,
            "retracted_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason.strip(),
            "retracted_output": retracted_output,
            "stale_artifact_id": retracted_output.get("artifact_id")
            or attempt.get("remote", {}).get("artifact_id"),
            "stale_source_id": stale_source_ids[0] if stale_source_ids else None,
            "stale_source_ids": stale_source_ids,
            "retracted_mp3_path": retracted_output.get("mp3_path")
            or attempt.get("finalize", {}).get("download", {}).get("path"),
        }
        attempt["retraction"] = retraction
        episode.setdefault("retracted_attempt_ids", []).append(attempt_id)
        return dict(retraction)

    _, retraction = ManifestStore(manifest_path).update(mutate)
    return {
        **retraction,
        "observed_state": "retracted",
        "safe_next_action": (
            ACTION_SOURCE_DELETE if retraction.get("stale_source_id") else ACTION_SERIES
        ),
    }


def _active_attempt_or_reraise(current: dict, episode_n: int) -> str:
    """`podcast_series` 在候選集區間內、`_run_episode` 建立 attempt 之前就撞到的例外,
    要原樣浮上去給呼叫端看——沒有 default 的 `next()`／dict 下標會把 StopIteration／
    KeyError 蓋掉真正該讓人看到的原始例外。三個 except handler(TerminalGenerationError／
    RuntimeError／(TimeoutError, ConnectionError))結構完全相同,抽成這裡共用,「補一半」
    就不可能發生。裸 `raise` 合法:沿用的是呼叫端當下正在處理的例外(bare raise 讀
    thread-local 的「目前處理中例外」,不是詞法綁定),只要這裡是同步呼叫、沒有中間再
    冒出別的例外就成立。
    殘留同類路徑(已評估可達性極低,留待真的踩到再處理):handler 接下來的
    `_attempt_record` 對「找不到」或「已 retract」的 attempt 也會 raise,一樣會蓋掉
    原例外——但這裡的 attempt_id 是剛從 manifest 讀回的 active_attempt_id,理論上
    _attempt_record 找不到的機率極低。"""
    row = next(
        (item for item in current["episodes"] if item.get("episode") == episode_n),
        None,
    )
    attempt_id = row.get("active_attempt_id") if row else None
    if attempt_id is None:
        raise
    return attempt_id


def _classify_not_accepted_stop(exc: BaseException) -> tuple[str, dict]:
    """回 (observed_state, extra) 給 `partial()`,分辨『等配額』與『notebook 沒分享給
    這個帳號』——manifest 的 dispatch.status 對兩者都寫 not_accepted(契約上都是零
    副作用拒絕,這個分類本身沒有錯),但呼叫端拿到的結構化停點如果也只寫
    `not_accepted`,兩者長得一模一樣。權限問題不 rotate(見
    `_dispatch_audio_with_failover`),於是呼叫端拿工具自己給的 `safe_next_action`
    (`podcast_series`)原樣重呼,只會撞回同一個沒權限的帳號,而
    `attempt_count`/`superseded_attempt_count`(partial() 註解說的唯一『有沒有在原地
    打轉』依據)兩輪都是 1/0——跟等配額完全同一組數字。

    **刻意不新增 `safe_next_action` 字面值**:`SAFE_NEXT_ACTIONS` 是「一定是真的
    MCP 工具名」的白名單(`tests/test_series_durable_resume.py` 鎖著逐字集合),
    這裡分辨兩者靠 observed_state 與帶著下一步(`notebook_share_with_pool`)的
    error 訊息,不靠新增一個字面值去改變那個集合。
    """
    if isinstance(exc, NotebookAccessDenied):
        return "notebook_access_denied", {"error": str(exc)}
    return "not_accepted", {}


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
async def podcast_series(
    notebook_id: str,
    episodes: list[dict],
    output_dir: str,
    start: int = 1,
    language: str | None = None,
    audio_format: str | None = "deep-dive",
    audio_length: str | None = "long",
    wait_timeout: float = 1200.0,
) -> dict:
    """依 manifest 的安全續點，確定性地生成整季 podcast。"""
    # start 是執行下界，不是重生旗標；N 以前的 plan 是 caller 明示的 trust
    # boundary，不讀、不驗證。範圍錯誤仍須在任何遠端副作用前失敗。
    if start < 1:
        raise ValueError("start must be >= 1")
    if start > len(episodes):
        raise ValueError(f"start must be <= len(episodes) ({len(episodes)})")

    # 只驗證 candidate range，避免執行中途才因壞 plan 消耗 generation 額度。
    for i, ep in enumerate(episodes[start - 1 :], start=start):
        if not isinstance(ep, dict) or "brief" not in ep:
            raise ValueError(
                f"episode {i} must be a dict with a 'brief' key, got: {ep!r}"
            )
        title = ep.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(
                f"episode {i} must have a non-empty 'title' (got: {title!r})"
            )
        # brief=None 會撐到迴圈內 `.encode()` 才炸(EP1 已燒配額);brief="" 更會
        # 靜默生出空 brief 的一集。跟 title 同一道前驗、同一時機。
        brief = ep.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            raise ValueError(
                f"episode {i} must have a non-empty 'brief' (got: {brief!r})"
            )

    os.makedirs(output_dir, exist_ok=True)
    manifest_path = os.path.join(output_dir, "series_manifest.json")

    store = ManifestStore(manifest_path)
    initial_snapshot = store.read()
    manifest_notebook_id = initial_snapshot.get("notebook_id")
    if manifest_notebook_id not in (None, notebook_id):
        raise ValueError(
            "series manifest belongs to a different notebook"
        )
    client = runtime.get_client()
    run_results: list[dict] = []

    def partial(
        episode_n: int,
        attempt_id: str | None,
        observed_state: str,
        safe_next_action: str,
        **extra: object,
    ) -> dict:
        if safe_next_action not in SAFE_NEXT_ACTIONS:
            raise ValueError(
                f"unknown safe_next_action {safe_next_action!r}"
            )
        # 每個安全停止都回報這一集已經燒掉幾個 attempt、其中幾個是自動 supersede。
        # 配額耗盡(removed)時 safe_next_action 會一直指回 podcast_series;呼叫端靠
        # 這兩個數字才看得出自己在原地打轉,而不是無限重呼再燒一次生成配額。
        row = next(
            (
                item
                for item in store.read()["episodes"]
                if item.get("episode") == episode_n
            ),
            None,
        )
        attempts = row.get("attempts", []) if row else []
        return {
            "notebook_id": notebook_id,
            "episodes": run_results,
            "manifest": manifest_path,
            "complete": False,
            "stopped_at_episode": episode_n,
            "attempt_id": attempt_id,
            "observed_state": observed_state,
            "safe_next_action": safe_next_action,
            "attempt_count": len(attempts),
            "superseded_attempt_count": sum(
                1 for attempt in attempts if attempt.get("supersedes_attempt_id")
            ),
            **extra,
        }

    # Resume/finalize 也需要有效認證；每個新 generation 前會再 probe 一次，
    # 避免數小時 series 中途 cookie 失效後仍燒 submit。
    await probe_auth(client)

    for episode_n in range(start, len(episodes) + 1):
        # 前一集可能已經 failover 換過帳號 —— `client` 是函式開頭抓的區域變數,不會
        # 自動跟著 runtime 的作用中帳號走。每集開頭重新抓一次,否則清理義務對帳／
        # drift 複驗／baseline `artifacts.list`／認證預檢會繼續打在被拒帳號上
        # (v0.8.1 的自動分享讓 pool 全員都看得到 notebook,這件事被巧合遮住,
        # 不是設計上安全)。
        client = runtime.get_client()
        plan = episodes[episode_n - 1]
        expected_title = plan["title"].strip()
        expected_brief_hash = hashlib.sha256(
            plan["brief"].encode("utf-8")
        ).hexdigest()
        # retract 留下的清理義務先結案,才輪到這一集的任何分支(finalize 續跑、prepared
        # 重送、supersede 重生都會產出或上傳)。只在真的有義務時才打 RPC。
        await _assert_source_cleanup_done(client, store, notebook_id, episode_n)
        snapshot = store.read()
        episode = next(
            (
                row
                for row in snapshot["episodes"]
                if row.get("episode") == episode_n
            ),
            None,
        )

        if episode is not None:
            output_attempt_id = episode.get("output_attempt_id")
            if output_attempt_id is not None:
                _, output_attempt = _attempt_record(
                    snapshot, episode_n, output_attempt_id
                )
                if output_attempt.get("title") != expected_title:
                    raise ValueError(
                        f"episode {episode_n} title differs from completed attempt"
                    )
                stored_hash = output_attempt.get("brief_sha256")
                if stored_hash not in (None, expected_brief_hash):
                    raise ValueError(
                        f"episode {episode_n} brief differs from completed attempt"
                    )
                try:
                    repaired = await finalize_attempt(
                        client,
                        store,
                        episode_n=episode_n,
                        attempt_id=output_attempt_id,
                        output_dir=output_dir,
                        wait_timeout=wait_timeout,
                    )
                except RuntimeError:
                    current = store.read()
                    _, stopped_attempt = _attempt_record(
                        current, episode_n, output_attempt_id
                    )
                    source_id = (
                        stopped_attempt.get("finalize", {})
                        .get("feedback_source_upload", {})
                        .get("source_id")
                    )
                    if isinstance(source_id, str):
                        sources = await client.sources.list(notebook_id)
                        if not any(
                            getattr(source, "id", None) == source_id
                            for source in sources
                        ):
                            return partial(
                                episode_n,
                                output_attempt_id,
                                "continuity_unverified",
                                ACTION_ADOPT,
                            )
                    raise
                except (TimeoutError, ConnectionError):
                    # 已完成集的 drift 複驗途中傳輸失敗:manifest checkpoint 仍是真相,
                    # 回結構化 partial 讓呼叫端重跑同一組 series,別裸拋掉整季進度回報
                    # (與下方 active attempt 分支同一處置)。
                    return partial(
                        episode_n,
                        output_attempt_id,
                        "verification_incomplete",
                        ACTION_SERIES,
                    )
                _promote_attempt_output(
                    store, episode_n, output_attempt_id, repaired
                )
                continue

            is_legacy_output = (
                not episode.get("attempts")
                and has_durable_output_evidence(episode)
            )
            if is_legacy_output:
                if episode.get("title") not in (None, expected_title):
                    raise ValueError(
                        f"episode {episode_n} title differs from legacy output"
                    )
                legacy_path = episode.get("mp3_path")
                try:
                    local_audio_ok = (
                        isinstance(legacy_path, str)
                        and os.path.isfile(legacy_path)
                        and os.path.getsize(legacy_path) > 0
                    )
                except OSError:
                    local_audio_ok = False
                legacy_source_id = episode.get("feedback_source_id")
                source_verified = (
                    isinstance(legacy_source_id, str)
                    and await _continuity_source_verified(
                        client,
                        notebook_id,
                        _episode_label(episode_n, expected_title),
                        source_id=legacy_source_id,
                    )
                )
                if local_audio_ok and source_verified:
                    continue

                legacy_artifact_id = episode.get("artifact_id")
                if not local_audio_ok and source_verified:
                    if not isinstance(legacy_artifact_id, str):
                        raise ValueError(
                            f"episode {episode_n} local audio is missing and "
                            "no legacy artifact_id can restore it"
                        )
                    return partial(
                        episode_n,
                        None,
                        "legacy_audio_missing",
                        ACTION_RESUME,
                        artifact_id=legacy_artifact_id,
                    )

                return partial(
                    episode_n,
                    None,
                    "legacy_output_unverified",
                    ACTION_ADOPT,
                    **(
                        {"artifact_id": legacy_artifact_id}
                        if isinstance(legacy_artifact_id, str)
                        else {}
                    ),
                )

            active_attempt_id = episode.get("active_attempt_id")
            if active_attempt_id is not None:
                _, attempt = _attempt_record(
                    snapshot, episode_n, active_attempt_id
                )
                if attempt.get("title") != expected_title:
                    raise ValueError(
                        f"episode {episode_n} title changed during an active attempt"
                    )
                stored_hash = attempt.get("brief_sha256")
                if stored_hash not in (None, expected_brief_hash):
                    raise ValueError(
                        f"episode {episode_n} brief changed during an active attempt"
                    )

                dispatch_state = attempt.get("dispatch", {}).get("status")
                remote = attempt.get("remote", {})
                remote_state = remote.get("status")
                artifact_id = remote.get("artifact_id")
                # 整季能不能接手這個 attempt,由 settings 是否可重現決定。**這件事要在
                # 任何變更之前判斷**:下面的 rearm 會把 remote.error / error_code /
                # dispatched_at 全部清掉,擺在驗證之前的話,一個註定失敗的呼叫仍然會先
                # 毀掉「為什麼被拒」的診斷(v0.7.1 驗收 F-7:抹完的 manifest 看起來就像
                # 分類從未生效過)。
                series_settings = _audio_settings(
                    resolve_language(language), audio_format, audio_length
                )
                if dispatch_state in ("prepared", "not_accepted"):
                    _assert_series_owns_attempt(
                        attempt, series_settings, episode_n, active_attempt_id
                    )
                if dispatch_state == "not_accepted":
                    rearmed = _rearm_not_accepted_attempt(
                        store, episode_n, active_attempt_id
                    )
                    if not rearmed:
                        latest = store.read()
                        _, latest_attempt = _attempt_record(
                            latest, episode_n, active_attempt_id
                        )
                        return partial(
                            episode_n,
                            active_attempt_id,
                            latest_attempt["dispatch"]["status"],
                            ACTION_SERIES,
                        )
                    snapshot = store.read()
                    _, attempt = _attempt_record(
                        snapshot, episode_n, active_attempt_id
                    )
                    dispatch_state = attempt["dispatch"]["status"]
                    remote = attempt["remote"]
                    remote_state = remote["status"]
                    artifact_id = remote["artifact_id"]
                if remote_state in ("failed", "removed"):
                    superseded_attempt_id = active_attempt_id
                    active_attempt_id = _create_audio_attempt(
                        store,
                        notebook_id=notebook_id,
                        episode_n=episode_n,
                        title=expected_title,
                        brief=plan["brief"],
                        language=resolve_language(language),
                        audio_format=audio_format,
                        audio_length=audio_length,
                        supersedes_attempt_id=superseded_attempt_id,
                    )
                    snapshot = store.read()
                    _, attempt = _attempt_record(
                        snapshot, episode_n, active_attempt_id
                    )
                    dispatch_state = attempt["dispatch"]["status"]
                    remote = attempt["remote"]
                    remote_state = remote["status"]
                    artifact_id = remote["artifact_id"]

                if dispatch_state == "prepared":
                    # settings 可重現性已在上面(任何變更之前)驗過。這裡再驗一次是因為
                    # 上面那圈只涵蓋「進入本輪時就是 prepared/not_accepted」的 attempt,
                    # 而 remote failed/removed 的 supersede 分支會**新建**一個 prepared
                    # attempt 落到這裡——那顆是 series 自己建的,必然相符,但顯式驗過
                    # 才不會在未來有人改動 supersede 分支時靜默漏掉。
                    _assert_series_owns_attempt(
                        attempt, series_settings, episode_n, active_attempt_id
                    )
                    # baseline / 記帳 / 實際送出三者同源,理由同 `_run_episode`
                    # 那條路徑(並行 rotate 會讓 manifest 記 A、實際 B 送出)。
                    dispatch_account, dispatch_client = runtime.snapshot()
                    baseline = await dispatch_client.artifacts.list(
                        notebook_id, artifact_type=ArtifactType.AUDIO
                    )
                    claimed = _claim_prepared_dispatch(
                        store,
                        episode_n,
                        active_attempt_id,
                        [row.id for row in baseline],
                        account=dispatch_account,
                    )
                    if not claimed:
                        latest = store.read()
                        _, latest_attempt = _attempt_record(
                            latest, episode_n, active_attempt_id
                        )
                        return partial(
                            episode_n,
                            active_attempt_id,
                            latest_attempt["dispatch"]["status"],
                            ACTION_SERIES,
                        )
                    async def _generate_resend(dispatch_client: object):
                        return await dispatch_client.artifacts.generate_audio(
                            notebook_id,
                            language=series_settings["language"],
                            instructions=plan["brief"],
                            audio_format=to_audio_format(audio_format),
                            audio_length=to_audio_length(audio_length),
                        )

                    # 與 `_run_episode` 共用同一個 dispatch helper —— 它負責配額
                    # failover 與 not_accepted / acceptance_unknown 的落盤。這裡只把
                    # 例外翻成 series 的**結構化安全停點**(return partial,不外拋),
                    # 那是 podcast_series 對呼叫端的契約。
                    try:
                        # 後兩個是實際送出的帳號與 client,finalize 必須用它們
                        # (理由見 helper 的 docstring)。
                        (
                            artifact_id,
                            dispatch_account,
                            client,
                        ) = await _dispatch_audio_with_failover(
                            store,
                            episode_n,
                            active_attempt_id,
                            _generate_resend,
                            account=dispatch_account,
                            client=dispatch_client,
                        )
                    except asyncio.CancelledError:
                        raise
                    except (RuntimeError, *_REFUSED_WITHOUT_DISPATCH) as exc:
                        # 明確拒絕(配額/限流/權限)沒有建出 task,或 ensure_started
                        # 判定的同一件事——但**不能無條件回 not_accepted**(跟
                        # `_run_episode` 那圈同一種「補一半」):
                        # `_dispatch_audio_with_failover` 的泛用 except 分支也可能把
                        # 一個 acceptance_unknown 的普通失敗包成 RuntimeError 重新拋出。
                        # 真正的判準是 manifest 裡記下的狀態,例外型別只是入場券,與
                        # 下面 `_run_episode` 分支的判準完全同一個道理(F-4 的姊妹修正:
                        # 這裡曾經是唯一沒有覆核 manifest 就回報的分支)。
                        # 讀 manifest 不能自己變成新的失敗來源:`abandon_in_flight` 讓
                        # 並行 retract 掉在飛的 attempt 成為受支援操作,`_attempt_record`
                        # 因此會對 tombstone raise,而在 except handler 裡再拋會蓋掉
                        # 原例外。**三態,不是 bool**:
                        #   not_accepted → 結構化停點(可直接重跑整季)
                        #   讀得到、但不是 not_accepted → 一樣回結構化停點,只是要先對帳
                        #   讀不到       → 我們什麼都不知道,原例外原樣交出去
                        # 中間那一態原本是 bare raise,於是同一個 acceptance_unknown 只因
                        # 例外型別是不是 RuntimeError 就分岔:一邊裸拋(**前面幾集已經跑完
                        # 的 run_results 整份丟掉**),一邊回 partial。而 series 對呼叫端的
                        # 契約是「預期內的停止用回傳值表達」,下面那個 `except Exception`
                        # 也正是這樣做的。
                        try:
                            current = store.read()
                            _, stopped_attempt = _attempt_record(
                                current, episode_n, active_attempt_id
                            )
                            recorded = stopped_attempt["dispatch"]["status"]
                        except Exception:
                            recorded = None
                        if recorded is None:
                            raise
                        if recorded != "not_accepted":
                            return partial(
                                episode_n,
                                active_attempt_id,
                                "acceptance_unknown",
                                ACTION_RECONCILE,
                            )
                        observed_state, extra = _classify_not_accepted_stop(exc)
                        return partial(
                            episode_n,
                            active_attempt_id,
                            observed_state,
                            ACTION_SERIES,
                            **extra,
                        )
                    except Exception:
                        return partial(
                            episode_n,
                            active_attempt_id,
                            "acceptance_unknown",
                            ACTION_RECONCILE,
                        )
                    # `client` 已由上面的三元組換成實際送出的那一個。這裡曾經是
                    # `client = runtime.get_client()` —— 以「殺掉分開讀全域」為目的的
                    # 那一輪修正,自己在這條路上又種了一顆同型的(補一半的又一例)。
                    _bind_accepted_artifact(
                        store, episode_n, active_attempt_id, artifact_id
                    )

                if not artifact_id:
                    if dispatch_state not in (
                        "dispatching",
                        "acceptance_unknown",
                        "reconciliation_ambiguous",
                    ):
                        raise ValueError(
                            f"episode {episode_n} attempt has no remote artifact "
                            f"in state {dispatch_state!r}"
                        )
                    reconciled = await podcast_episode_reconcile(
                        manifest_path,
                        episode_n=episode_n,
                        attempt_id=active_attempt_id,
                        wait_timeout=wait_timeout,
                    )
                    if reconciled["observed_state"] != "accepted":
                        return partial(
                            episode_n,
                            active_attempt_id,
                            reconciled["observed_state"],
                            reconciled["safe_next_action"],
                            candidate_artifact_ids=reconciled.get(
                                "candidate_artifact_ids", []
                            ),
                        )

                try:
                    result = await finalize_attempt(
                        client,
                        store,
                        episode_n=episode_n,
                        attempt_id=active_attempt_id,
                        output_dir=output_dir,
                        wait_timeout=wait_timeout,
                    )
                except TerminalGenerationError:
                    current = store.read()
                    _, failed_attempt = _attempt_record(
                        current, episode_n, active_attempt_id
                    )
                    return partial(
                        episode_n,
                        active_attempt_id,
                        failed_attempt["remote"]["status"],
                        ACTION_SERIES,
                    )
                except (TimeoutError, ConnectionError):
                    current = store.read()
                    _, stopped_attempt = _attempt_record(
                        current, episode_n, active_attempt_id
                    )
                    upload_state = stopped_attempt.get("finalize", {}).get(
                        "feedback_source_upload", {}
                    ).get("status")
                    observed = (
                        upload_state
                        if upload_state in (
                            "acceptance_unknown",
                            "reconciliation_ambiguous",
                        )
                        else stopped_attempt["remote"]["status"]
                    )
                    return partial(
                        episode_n,
                        active_attempt_id,
                        observed,
                        ACTION_SERIES,
                    )
                except RuntimeError:
                    current = store.read()
                    _, stopped_attempt = _attempt_record(
                        current, episode_n, active_attempt_id
                    )
                    upload_state = stopped_attempt.get("finalize", {}).get(
                        "feedback_source_upload", {}
                    ).get("status")
                    if upload_state in (
                        "acceptance_unknown",
                        "reconciliation_ambiguous",
                    ):
                        action = (
                            ACTION_ADOPT
                            if upload_state == "reconciliation_ambiguous"
                            else ACTION_SERIES
                        )
                        return partial(
                            episode_n,
                            active_attempt_id,
                            upload_state,
                            action,
                        )
                    raise
                _promote_attempt_output(
                    store, episode_n, active_attempt_id, result
                )
                run_results.append(result)
                continue

        # candidate range 內完全沒有 attempt 才能產生新的遠端副作用。
        await probe_auth(client)
        try:
            result = await _run_episode(
                notebook_id,
                episode_n,
                plan["title"],
                plan["brief"],
                output_dir,
                None,
                language,
                audio_format,
                audio_length,
                wait_timeout,
                manifest_path=manifest_path,
            )
        except TerminalGenerationError:
            current = store.read()
            attempt_id = _active_attempt_or_reraise(current, episode_n)
            _, failed_attempt = _attempt_record(
                current, episode_n, attempt_id
            )
            return partial(
                episode_n,
                attempt_id,
                failed_attempt["remote"]["status"],
                ACTION_SERIES,
            )
        # RuntimeError 是 0.7.x 的形狀(ensure_started 判定失敗後自己拋的);
        # 0.8.0 起伺服器的同步拒絕**直接 raise SDK 例外**,型別完全不同(RateLimitError
        # 繼承 NotebookLMError,不是 RuntimeError),漏收的話整季會把原始例外拋給
        # 呼叫端,而不是回這裡的結構化安全停點 —— podcast_series「預期內的停止用回傳
        # 值表達」這個契約就破了。真正的判準是下面那行「attempt 是不是 not_accepted」,
        # 例外型別只是入場券,所以兩種形狀都收。
        except (RuntimeError, *_REFUSED_WITHOUT_DISPATCH) as exc:
            current = store.read()
            attempt_id = _active_attempt_or_reraise(current, episode_n)
            _, stopped_attempt = _attempt_record(
                current, episode_n, attempt_id
            )
            if stopped_attempt["dispatch"]["status"] != "not_accepted":
                raise
            # 「等配額」與「notebook 沒分享給這個帳號」manifest 都寫 not_accepted,
            # 但呼叫端拿到的結構化停點要分得出來(見 `_classify_not_accepted_stop`)。
            observed_state, extra = _classify_not_accepted_stop(exc)
            return partial(
                episode_n,
                attempt_id,
                observed_state,
                ACTION_SERIES,
                **extra,
            )
        except (TimeoutError, ConnectionError):
            current = store.read()
            attempt_id = _active_attempt_or_reraise(current, episode_n)
            _, stopped_attempt = _attempt_record(
                current, episode_n, attempt_id
            )
            if stopped_attempt["remote"].get("artifact_id") is None:
                observed = stopped_attempt["dispatch"]["status"]
            else:
                upload_state = stopped_attempt.get("finalize", {}).get(
                    "feedback_source_upload", {}
                ).get("status")
                observed = (
                    upload_state
                    if upload_state in (
                        "acceptance_unknown",
                        "reconciliation_ambiguous",
                    )
                    else stopped_attempt["remote"]["status"]
                )
            needs_artifact_reconcile = (
                stopped_attempt["remote"].get("artifact_id") is None
                and observed == "acceptance_unknown"
            )
            action = ACTION_RECONCILE if needs_artifact_reconcile else ACTION_SERIES
            return partial(
                episode_n,
                attempt_id,
                observed,
                action,
            )
        run_results.append(result)

    return {
        "notebook_id": notebook_id,
        "episodes": run_results,
        "manifest": manifest_path,
        "complete": True,
    }
