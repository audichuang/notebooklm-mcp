"""Attempt admission:「這個請求能不能在這一集建立 / 沿用 attempt」的單一決策。

過去同一組守門(notebook 身分、標題不可變、既有輸出擋寫、另一顆 active attempt)抄在
四個入口:`_create_audio_attempt`、`_ensure_resume_attempt` 的 claimed 與新建兩支分支、
`_reuse_frozen_input_attempt`,`podcast_series` 又自帶 title/brief 雜湊的自檢。每次補
一條路徑就漏另一條(ADR-0009「兩條建立分支都要有」就是這樣來的)。這裡把「請求來源」
(`Origin`)當資料,每個分支的守門**順序與述詞**列在 `_GUARDS` 一張表裡:

- 每個分支保留自己原本的守門集合與順序(先擋哪一條決定呼叫端讀到哪句訊息,不能換);
- 差異是資料,不是 if/else —— 要統一哪一格(例如標題不可變在 generate 只於 retract 後
  才擋)是行為變更,不在這個模組做。已知差異見 docs/design-notes.md 的
  attempt_admission 一節。

純函式:只讀 manifest dict,回 `Admit`(要寫什麼)或 `Refusal`(例外型別 + 訊息,逐字等於
原本拋的那句)。不寫 manifest、不打遠端(ADR-0001:驗證先於任何變更)。唯一會自己 raise 的是
`find_attempt` 的 tombstone default-deny(ADR-0009),那是原本就有的行為。

指引文字不在這裡產生(docs/gotchas-attempt.md 紅線):呼叫端傳入
`next_step(episode, attempt, attempt_id)`,實際是
`_attempt_next_step(_attempt_capabilities(...))`;本模組只決定「在哪個守門點、對哪顆
attempt 問它」。

「原樣重送」(`is_resendable_same_request`)與 capabilities 的 `can_resend` 是刻意不同的兩個
判準(前者問「這次請求是否逐字相同」,後者問「這個狀態能不能重送」),不要合併。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .attempt_state import find_attempt, new_resume_binding
from .audio_finalize import new_finalize_state
from .naming import episode_label
from .output_projection import (
    has_hard_output_evidence,
    output_blocks_generate,
)

NextStep = Callable[[dict, dict, str], str]


class Origin(Enum):
    """請求來源。RESUME 在判定時依 artifact 是否已被某顆 attempt claim 分成兩支。"""

    GENERATE = "generate"
    RESUME = "resume"
    FROZEN_REUSE = "frozen_reuse"


class Branch(Enum):
    GENERATE = "generate"
    RESUME_CLAIMED = "resume_claimed"
    RESUME_NEW = "resume_new"
    FROZEN_REUSE = "frozen_reuse"


# Admit.kind
CREATE = "create"  # 新建 attempt(generate 與 supersede 的寫入形狀相同)
RESEND = "resend"  # generate:沿用既有 active attempt 並 reset 成乾淨 prepared
CLAIM = "claim"  # resume:接手已 claim 該 artifact 的 attempt
RESUME_NEW = "resume_new"  # resume:為 caller 驗過的 artifact 新建 attempt
REUSE = "reuse"  # frozen:已綁定的 prepared attempt 直接沿用
REARM = "rearm"  # frozen:not_accepted 先 rearm 再沿用


@dataclass(frozen=True)
class Request:
    origin: Origin
    notebook_id: str
    episode_n: int
    title: str
    brief_sha256: str | None = None
    settings: dict | None = None
    input_bundle: dict | None = None
    supersedes_attempt_id: str | None = None  # GENERATE
    artifact_id: str | None = None  # RESUME
    attempt_id: str | None = None  # FROZEN_REUSE:bundle 綁定的固定 attempt id


@dataclass(frozen=True)
class Admit:
    kind: str
    attempt_id: str | None = None


@dataclass(frozen=True)
class Refusal:
    exc_type: type[Exception]
    message: str

    def raise_(self):
        raise self.exc_type(self.message)


Decision = Admit | Refusal


def brief_sha256(brief: str) -> str:
    return hashlib.sha256(brief.encode("utf-8")).hexdigest()


# ---- 守門 ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    manifest: dict
    req: Request
    episode: dict  # 該集不存在時為 {}(所有守門對空集都放行)
    next_step: NextStep
    attempt: dict | None = None  # RESUME_CLAIMED / FROZEN_REUSE 的目標 attempt
    attempt_id: str | None = None


def is_resendable_same_request(
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
    個呼叫**,與 series 的續跑語意一致,不必學新工具。這也與 frozen bundle 沿用既有綁定
    同一個 pattern(讀回既有綁定、沿用同一 attempt_id、不重複建)。

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


def _g_notebook_episode(ctx: _Ctx):
    # post-retract 的集用 podcast_episode 傳錯 notebook_id 會造成
    # episode(nb-A)/attempt(nb-B)身分分裂;generate 與 resume 新建都用 episode 級身分。
    existing = ctx.episode.get("notebook_id")
    if existing not in (None, ctx.req.notebook_id):
        return Refusal(ValueError, f"episode {ctx.req.episode_n} belongs to another notebook")
    return None


def _g_notebook_attempt(ctx: _Ctx):
    if ctx.attempt.get("notebook_id") != ctx.req.notebook_id:
        return Refusal(ValueError, f"artifact {ctx.req.artifact_id!r} belongs to another notebook")
    return None


def _g_title_attempt(ctx: _Ctx):
    if ctx.attempt.get("title") != ctx.req.title.strip():
        return Refusal(ValueError, f"artifact {ctx.req.artifact_id!r} belongs to another title")
    return None


def _g_output_generate(ctx: _Ctx):
    if output_blocks_generate(ctx.episode):
        n = ctx.req.episode_n
        # 訊息要能自己走完:講清楚為什麼擋、以及唯一的合法出路。
        return Refusal(
            ValueError,
            f"episode {n} already has a completed output; refusing to "
            "silently overwrite it. To replace it: podcast_attempt_retract"
            f"(manifest_path=..., episode_n={n}, attempt_id=..., reason=...), "
            "then source_delete each id it returns in stale_source_ids, then "
            "generate again (the title cannot change).",
        )
    return None


def _g_output_resume(ctx: _Ctx):
    # claimed:output 屬於別顆才擋(自己就是 output 放行);新建:任何 output 都擋。
    # 兩者同一個式子,新建時 attempt_id 為 None。
    output_attempt_id = ctx.episode.get("output_attempt_id")
    if output_attempt_id not in (None, ctx.attempt_id):
        return Refusal(
            ValueError,
            f"episode {ctx.req.episode_n} already has durable output "
            f"{output_attempt_id!r}; retract it (podcast_attempt_retract) and "
            "delete the stale feedback source before resuming another artifact",
        )
    return None


def _g_legacy_artifact_mismatch(ctx: _Ctx):
    # T2:flat v1 legacy 集 resume 到**不同** artifact 在這裡 fail-fast;同一顆放行。
    episode = ctx.episode
    legacy_artifact_id = episode.get("artifact_id") or episode.get("task_id")
    if (
        legacy_artifact_id
        and has_hard_output_evidence(episode)
        and legacy_artifact_id != ctx.req.artifact_id
    ):
        return Refusal(
            ValueError,
            f"episode {ctx.req.episode_n} has legacy output evidence bound to artifact "
            f"{legacy_artifact_id!r}; to bring it into the auditable attempt flow, "
            f"call podcast_episode_resume(artifact_id={legacy_artifact_id!r}) — "
            "resuming the SAME artifact is always allowed (the "
            "legacy_audio_missing stop depends on it). To replace the output: "
            f"resume(artifact_id={legacy_artifact_id!r}) to turn it into an "
            "attempt, then podcast_attempt_retract it, then regenerate — do not "
            "resume a different artifact directly",
        )
    return None


def _g_title_episode_resume(ctx: _Ctx):
    # resume 永遠比對 episode 級標題(generate 只在 retract 後才比,見 _g_title_if_retracted)。
    existing_title = ctx.episode.get("title")
    if isinstance(existing_title, str) and existing_title.strip() != ctx.req.title.strip():
        return Refusal(
            ValueError,
            f"episode {ctx.req.episode_n} title does not match the manifest "
            f"({existing_title!r}); resume cannot rename an episode",
        )
    return None


def _g_title_if_retracted(ctx: _Ctx):
    # 取代版不得改標題:episode 級 title／label 與 cover_path 都是 setdefault
    # 或既有值,改了會留著舊標題的 label 與封面去發布。
    episode = ctx.episode
    if episode.get("retracted_attempt_ids") and episode.get("title") not in (
        None,
        ctx.req.title.strip(),
    ):
        return Refusal(
            ValueError,
            f"episode {ctx.req.episode_n} title cannot change in a retract "
            f"replacement (manifest has {episode['title']!r}); keep the "
            "title or rebuild the episode explicitly",
        )
    return None


def _g_active_generate(ctx: _Ctx):
    """generate 的 active attempt 閘:原樣重送 / supersede / 拒絕。"""
    req, episode = ctx.req, ctx.episode
    n = req.episode_n
    active_attempt_id = episode.get("active_attempt_id")
    if not active_attempt_id:
        if req.supersedes_attempt_id is not None:
            return Refusal(ValueError, "superseded attempt is no longer active")
        return None
    _, prior = find_attempt(ctx.manifest, n, active_attempt_id)
    if req.supersedes_attempt_id != active_attempt_id:
        if is_resendable_same_request(
            prior,
            notebook_id=req.notebook_id,
            title=req.title,
            brief_sha256=req.brief_sha256,
            settings=req.settings,
            input_bundle=req.input_bundle,
        ):
            # 原樣重呼 = 沿用同一個 attempt 重送(不建新的、不多燒配額)。驗證先於變更。
            return Admit(RESEND, active_attempt_id)
        # **指引按 attempt 的實際狀態產生**(capabilities),不手寫。
        way_out = ctx.next_step(episode, prior, active_attempt_id)
        return Refusal(
            ValueError,
            f"episode {n} already has durable active attempt "
            f"{active_attempt_id!r} (dispatch="
            f"{prior['dispatch'].get('status')!r}); {way_out}",
        )
    if prior.get("remote", {}).get("status") not in ("failed", "removed"):
        return Refusal(
            ValueError,
            f"attempt {active_attempt_id!r} is not terminal and cannot be superseded",
        )
    return None


def _g_active_resume_claimed(ctx: _Ctx):
    episode = ctx.episode
    current_active_id = episode.get("active_attempt_id")
    if (
        current_active_id
        and current_active_id != ctx.attempt_id
        and current_active_id != episode.get("output_attempt_id")
    ):
        # 與新建分支共用同一顆指引產生器,「補一半」就不可能發生。
        _, current_active = find_attempt(ctx.manifest, ctx.req.episode_n, current_active_id)
        return Refusal(
            ValueError,
            f"episode {ctx.req.episode_n} has another active attempt "
            f"{current_active_id!r}; " + ctx.next_step(episode, current_active, current_active_id),
        )
    return None


def _g_active_resume_new(ctx: _Ctx):
    episode = ctx.episode
    active_attempt_id = episode.get("active_attempt_id")
    if active_attempt_id:
        _, active = find_attempt(ctx.manifest, ctx.req.episode_n, active_attempt_id)
        hint = ctx.next_step(episode, active, active_attempt_id)
        return Refusal(
            ValueError,
            f"episode {ctx.req.episode_n} has active attempt {active_attempt_id!r} "
            f"(dispatch={active.get('dispatch', {}).get('status')!r}); {hint}",
        )
    return None


def _g_frozen_identity(ctx: _Ctx):
    req, episode, attempt = ctx.req, ctx.episode, ctx.attempt
    if (
        episode.get("active_attempt_id") != req.attempt_id
        or episode.get("output_attempt_id") is not None
        or attempt.get("notebook_id") != req.notebook_id
        or attempt.get("title") != req.title.strip()
        or attempt.get("brief_sha256") != req.brief_sha256
        # source_ids 跟 brief 一樣是生成輸入:換了來源集合就不是同一次生成。
        or attempt.get("settings") != req.settings
        or attempt.get("input_bundle") != req.input_bundle
    ):
        return Refusal(ValueError, "existing attempt does not match frozen generation input")
    return None


def _g_frozen_status(ctx: _Ctx):
    status = ctx.attempt.get("dispatch", {}).get("status")
    if status == "prepared":
        return Admit(REUSE, ctx.req.attempt_id)
    if status == "not_accepted":
        return Admit(REARM, ctx.req.attempt_id)
    # 同一顆 attempt 餵進 capabilities 就有正確答案,不要自己手寫指引(第七次現形)。
    return Refusal(
        ValueError,
        f"frozen attempt is {status!r}; "
        + ctx.next_step(ctx.episode, ctx.attempt, ctx.req.attempt_id),
    )


# 每個分支的守門順序 —— 順序即「同時違反兩條時呼叫端讀到哪一句」,不可調換。
_GUARDS: dict[Branch, tuple] = {
    Branch.GENERATE: (
        _g_notebook_episode,
        _g_output_generate,
        _g_active_generate,
        _g_title_if_retracted,
    ),
    Branch.RESUME_CLAIMED: (
        _g_notebook_attempt,
        _g_title_attempt,
        _g_output_resume,
        _g_title_episode_resume,
        _g_active_resume_claimed,
    ),
    Branch.RESUME_NEW: (
        _g_output_resume,
        _g_legacy_artifact_mismatch,
        _g_active_resume_new,
        _g_notebook_episode,
        _g_title_episode_resume,
    ),
    Branch.FROZEN_REUSE: (_g_frozen_identity, _g_frozen_status),
}

# 守門通過後的預設放行種類。
_DEFAULT_ADMIT = {
    Branch.GENERATE: CREATE,
    Branch.RESUME_CLAIMED: CLAIM,
    Branch.RESUME_NEW: RESUME_NEW,
}


def _episode_of(manifest: dict, episode_n: int) -> dict | None:
    return next((ep for ep in manifest["episodes"] if ep.get("episode") == episode_n), None)


def _run(branch: Branch, ctx: _Ctx) -> Decision:
    for guard in _GUARDS[branch]:
        outcome = guard(ctx)
        if outcome is not None:
            return outcome
    return Admit(_DEFAULT_ADMIT[branch], ctx.attempt_id)


def decide(manifest: dict, req: Request, next_step: NextStep) -> Decision:
    """回傳 `Admit`(寫什麼)或 `Refusal`(拒絕的例外型別與訊息)。"""
    n = req.episode_n
    episode = _episode_of(manifest, n)

    if req.origin is Origin.GENERATE:
        if episode is None:
            if req.supersedes_attempt_id is not None:
                return Refusal(ValueError, "cannot supersede an attempt from a missing episode")
            return Admit(CREATE)
        return _run(Branch.GENERATE, _Ctx(manifest, req, episode, next_step))

    if req.origin is Origin.RESUME:
        claimed = [
            (ep, attempt)
            for ep in manifest["episodes"]
            for attempt in ep.get("attempts", [])
            if attempt.get("remote", {}).get("artifact_id") == req.artifact_id
        ]
        if len(claimed) > 1:
            return Refusal(
                ValueError, f"artifact {req.artifact_id!r} is claimed by multiple attempts"
            )
        if claimed:
            claimed_episode, discovered = claimed[0]
            if claimed_episode.get("episode") != n:
                return Refusal(
                    ValueError, f"artifact {req.artifact_id!r} belongs to another episode"
                )
            # 走 find_attempt 而不是直接用掃到的那筆:tombstone default-deny 在那裡。
            episode, attempt = find_attempt(manifest, n, discovered["attempt_id"])
            return _run(
                Branch.RESUME_CLAIMED,
                _Ctx(manifest, req, episode, next_step, attempt, attempt["attempt_id"]),
            )
        return _run(Branch.RESUME_NEW, _Ctx(manifest, req, episode or {}, next_step))

    # FROZEN_REUSE:沒有綁定的 attempt 就退回「照常新建」(呼叫端接 CREATE)。
    if episode is None:
        return Admit(CREATE)
    matches = [
        row for row in episode.get("attempts", []) if row.get("attempt_id") == req.attempt_id
    ]
    if not matches:
        return Admit(CREATE)
    if len(matches) != 1:
        return Refusal(ValueError, "frozen generation attempt identity is duplicated")
    _, attempt = find_attempt(manifest, n, req.attempt_id)
    return _run(
        Branch.FROZEN_REUSE, _Ctx(manifest, req, episode, next_step, attempt, req.attempt_id)
    )


# ---- podcast_series 的自檢(title / brief 雜湊) ----------------------------------------

# subject -> (標題不符訊息尾巴, brief 不符訊息尾巴)。三種 subject 的比對對象不同,
# 所以訊息是資料;legacy 沒有 brief 可比。
_SERIES_IDENTITY_MESSAGES = {
    "active": ("title changed during an active attempt", "brief changed during an active attempt"),
    "output": ("title differs from completed attempt", "brief differs from completed attempt"),
    "legacy": ("title differs from legacy output", None),
}


def check_series_identity(
    subject: str,
    *,
    episode_n: int,
    holder: dict,
    expected_title: str,
    expected_brief_hash: str,
) -> Refusal | None:
    """series 每集重入時,對既有 attempt(active/output)或 legacy episode 的身分自檢。

    `holder` 對 active/output 是 attempt dict,對 legacy 是 episode dict(只比標題,且
    None 放行)。呼叫端 `.raise_()`。
    """
    title_msg, brief_msg = _SERIES_IDENTITY_MESSAGES[subject]
    if subject == "legacy":
        if holder.get("title") not in (None, expected_title):
            return Refusal(ValueError, f"episode {episode_n} {title_msg}")
        return None
    if holder.get("title") != expected_title:
        return Refusal(ValueError, f"episode {episode_n} {title_msg}")
    stored_hash = holder.get("brief_sha256")
    if stored_hash not in (None, expected_brief_hash):
        return Refusal(ValueError, f"episode {episode_n} {brief_msg}")
    return None


# ---- 新 attempt 的紀錄 literal(各只此一份) ----------------------------------------------


def new_prepared_attempt(
    *,
    attempt_id: str,
    created_at: str,
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str,
    settings: dict,
    supersedes_attempt_id: str | None = None,
    input_bundle: dict | None = None,
) -> dict:
    """generate 建的 prepared attempt(`podcast_episode` / `podcast_series` 共用)。"""
    attempt = {
        "attempt_id": attempt_id,
        "created_at": created_at,
        "notebook_id": notebook_id,
        "episode": episode_n,
        "title": title.strip(),
        "brief_sha256": brief_sha256(brief),
        # 全文與雜湊並存:雜湊只能證明「不一樣」,證明不了「送了什麼」。實測一季五集的
        # 送出 brief 與磁碟 brief.md 全數 sha 不符(貼上剝檔尾換行+貼上瞬間手改),其中
        # 一集手改未記錄,事後永遠重建不出實際生成輸入——manifest 是唯一進版控的正本。
        "brief": brief,
        "settings": settings,
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
    return attempt


def new_resume_attempt(
    *,
    attempt_id: str,
    now: str,
    notebook_id: str,
    episode_n: int,
    title: str,
    artifact_id: str,
    episode: dict,
) -> dict:
    """resume 為 caller 驗過的 artifact 新建的 attempt(已受理、caller_verified)。"""
    finalize = new_finalize_state()
    legacy_artifact_id = episode.get("artifact_id") or episode.get("task_id")
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
            {"status": "completed", "source_id": adopted_source_id, "adopted_at": adopted_at}
        )
        finalize["feedback_source_rename"].update({"status": "completed", "adopted_at": adopted_at})
    resume_dispatch, resume_remote = new_resume_binding(artifact_id, now)
    return {
        "attempt_id": attempt_id,
        "created_at": now,
        "notebook_id": notebook_id,
        "episode": episode_n,
        "title": title.strip(),
        "brief_sha256": None,
        "settings": {"origin": "explicit_resume"},
        "dispatch": resume_dispatch,
        "remote": resume_remote,
        "finalize": finalize,
        "errors": [],
    }


def attach_new_attempt(
    manifest: dict,
    episode_n: int,
    attempt: dict,
    *,
    title: str,
    notebook_id: str,
    overwrite_notebook: bool,
) -> None:
    """把新 attempt 掛到該集(不存在就建)並設為 active。generate / resume 新建共用。

    `overwrite_notebook`:resume 新建歷來是直接指派 `episode["notebook_id"]`,generate 是
    `setdefault`;守門已保證兩者在此刻等值,保留差異只為逐字不改行為。
    """
    episode = _episode_of(manifest, episode_n)
    if episode is None:
        episode = {"episode": episode_n, "attempts": []}
        manifest["episodes"].append(episode)
    episode.setdefault("title", title.strip())
    episode.setdefault("label", episode_label(episode_n, title))
    if overwrite_notebook:
        episode["notebook_id"] = notebook_id
    else:
        episode.setdefault("notebook_id", notebook_id)
    episode.setdefault("attempts", []).append(attempt)
    episode["active_attempt_id"] = attempt["attempt_id"]
