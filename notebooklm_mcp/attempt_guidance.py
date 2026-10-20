"""Attempt 指引:「這顆 attempt 現在能做什麼、該怎麼講」的單一事實來源。

docs/gotchas-attempt.md 的紅線:**狀態相關的指引訊息一律由 `attempt_capabilities()` /
`attempt_next_step()` 產生,不准在呼叫端手寫 if/else**(這個根因已現形七次)。這一群函式
原本是 `tools_podcast` 裡的私有符號,搬出來是為了讓「唯一合法來源」有自己的模組邊界;
訊息文字逐字未變。

純函式:只讀傳進來的 episode / attempt dict,不打 RPC、不寫 manifest、不讀時鐘
(`reconciliation_window_closed` 由呼叫端算好傳入)。`safe_next_action` 的合法值是
`SAFE_NEXT_ACTIONS`(`ACTION_*` 工具名字面值也住在這裡)。

`attempt_admission` 不 import 本模組:它的指引由呼叫端注入 `next_step`
(`tools_podcast._admission_next_step`),那是刻意保留的 seam。
"""

from __future__ import annotations

from .attempt_state import RECONCILABLE_DISPATCH_STATES
from .audio_finalize import feedback_upload, unresolved_upload_descriptor
from .cleanup_obligation import list_obligations
from .output_projection import has_hard_output_evidence

ACTION_ADOPT = "podcast_attempt_adopt"
ACTION_EPISODE = "podcast_episode"
ACTION_RECONCILE = "podcast_episode_reconcile"
ACTION_RESUME = "podcast_episode_resume"
ACTION_RETRACT = "podcast_attempt_retract"
ACTION_SERIES = "podcast_series"
ACTION_SHARE_WITH_POOL = "notebook_share_with_pool"
ACTION_SOURCE_DELETE = "source_delete"
SAFE_NEXT_ACTIONS = frozenset(
    {
        ACTION_ADOPT,
        # 來源筆數守門的停點。`_classify_not_accepted_stop` 刻意不為「權限 vs 配額」
        # 新增字面值(那兩者的下一步都仍是 podcast_series),但這裡不同:series 生不出
        # 帶 `source_ids` 的 settings,所以**下一步真的是另一支工具**。指回
        # `podcast_series` 會叫呼叫端撞回同一道牆,而 `attempt_count` 兩輪都不變 ——
        # 正是 `partial()` 註解說的「看不出自己在原地打轉」。
        ACTION_EPISODE,
        ACTION_RECONCILE,
        ACTION_RESUME,
        # 筆數守門撞上**既有 active attempt** 時的停點。不能指回 `ACTION_EPISODE`:
        # 那顆 attempt 的 settings 是「不指名來源」,呼叫端拿指名版進來會被
        # `attempt_admission.is_resendable_same_request` 判成不同請求而拒絕(`already has durable
        # active attempt`),不指名又撞回守門 —— **死路**。retract 是純本機動作,
        # 那顆從未 dispatch 的 attempt 沒有 stale source 要清,tombstone 掉才走得出去。
        ACTION_RETRACT,
        ACTION_SERIES,
        # 權限停點的下一步。曾經刻意不放進來(理由是「不想為分辨兩種 not_accepted 而
        # 新增字面值」),但那個理由只對「下一步仍然是 podcast_series」的情況成立 ——
        # `notebook_access_denied` 的下一步**真的是另一支工具**,而白名單的意義本來就
        # 是「一定是真的 MCP 工具名」。回 `podcast_series` 會讓只讀這個欄位的自動化
        # 原地重試同一個沒權限的帳號,而 error 文字裡寫的才是真正該做的事 —— 同一支
        # 工具的兩個欄位互相矛盾。v0.9.3 為完全相同的理由加過 ACTION_EPISODE 與
        # ACTION_RETRACT,那個「不新增」的理由自己已經被推翻。
        ACTION_SHARE_WITH_POOL,
        ACTION_SOURCE_DELETE,
    }
)


# `podcast_series` 不指名來源時產生的 settings 形狀(由
# `test_unpinned_settings_stay_shaped_like_pre_source_ids_manifests` 逐字鎖著)。
_SERIES_SETTINGS_KEYS = frozenset({"language", "audio_format", "audio_length"})

# 遠端已經回報終態:不存在「可能還在飛」,manifest 自己就知道結果。
TERMINAL_REMOTE = frozenset({"failed", "removed"})
# dispatch 從沒離開本機:契約保證伺服器沒建出 task(ADR-0010 紀律④的立論基礎)。
NEVER_DISPATCHED = frozenset({"prepared", "not_accepted"})


def regeneration_entry_point(attempt: dict) -> str:
    """重生這一集要用**哪一支**工具。

    `podcast_series` 生不出帶 `source_ids` 或 frozen bundle 的 settings,所以那種 attempt
    只有 `podcast_episode` 續得下去。指錯的後果是**靜默改掉生成輸入**:實測
    `podcast_episode(source_ids=["src-1"])` 撞配額 → retract → 照 `safe_next_action` 重呼
    series,兩次 dispatch 送出 `[["src-1"], None]` —— 第二次改讀整本筆記本,正是本 repo
    最忌諱的內容錯置形狀。

    **判準是白名單不是黑名單**:只有「認得出是 series 自己建的」才回 series,其餘一律回
    `podcast_episode`(它會要求呼叫端明示來源,不可能靜默擴大)。黑名單版本漏過兩種真實
    形狀 —— `origin="explicit_resume"` 的 attempt 沒有來源 provenance 卻被判成 series,
    以及 `source_ids=[]` / `input_bundle={}` 這類 falsy-but-present 的舊 manifest。
    fail-safe 的方向是「不確定就要求明示」。
    """
    if attempt.get("input_bundle") is not None:
        return ACTION_EPISODE
    if set(attempt.get("settings") or {}) == _SERIES_SETTINGS_KEYS:
        return ACTION_SERIES
    return ACTION_EPISODE


def _regeneration_hint(attempt: dict, *, resend_possible: bool) -> str:
    """重生**這一顆**時要注意什麼 —— 與 `regeneration_entry_point()` 同源判斷。

    v0.9.6 把入口收斂了,卻讓這句話在 retract 的回傳裡另外用 if/else 猜:「有 bundle」
    以外一律說「必須帶回原本那組 `source_ids`」。而入口的白名單**特意**把「認不出來的
    形狀」也導向 `podcast_episode`(保守要求明示),於是那句話對
    `settings={"origin": "explicit_resume"}` 的 attempt 是**假的** —— manifest 裡根本
    沒有那組 source_ids(v0.9.6 真實驗收 FINDING-4)。收斂做了一半就是這個下場:
    action 對了,附帶的話還是錯的。

    `resend_possible`(v0.9.10 修復):是不是也能**原樣重呼**這顆(`caps["can_resend"]`)。
    只有 `source_ids` 那句在兩種情境下的正確措辭不同 —— can_resend 為真時,呼叫端除了
    原樣重送,還有「retract 之後刻意換來源重生」這個選項,這時無條件講「必須帶回原本」
    會跟 `attempt_next_step` 的 can_resend 分支(「要換來源就先 retract」)互相矛盾:
    一句話同時說「可以換」又說「不准換」(v0.9.9 引入的回歸)。can_resend 為假(is_output
    /settled)時只有 retract 重生一條路,維持「必須帶回原本」不做選擇。
    """
    if attempt.get("input_bundle") is not None:
        # binding 只能建立一次,而它還綁著這顆即將成為 tombstone 的 attempt。
        return (
            "**重生要用一份新的、尚未綁定的 frozen bundle** —— 舊 bundle 的 "
            "attempt-binding.json 還綁著這顆已作廢的 attempt,沿用它會被 tombstone 擋下來。"
        )
    settings = attempt.get("settings") or {}
    if set(settings) == _SERIES_SETTINGS_KEYS:
        return ""
    pinned = settings.get("source_ids")
    if pinned:
        # **列得出 id,不能只報欄位名。** 同一條紀律在 `attempt_next_step()` 的清理義務
        # 分支已經寫過:呼叫端照著公開回傳就要做得到,不准要求它自己去翻 manifest
        # (docs/gotchas-attempt.md 的自足性紅線)。同一組值也在 caps 的
        # `regeneration_source_ids` 欄位裡,給只讀欄位的自動化用。
        listed = f"(逐字是 {list(pinned)!r})"
        if resend_possible:
            return (
                f"**若只更換 brief,必須保留原 `source_ids`{listed}**;若刻意更換來源,"
                "podcast_attempt_retract(**不需要** abandon_in_flight)後帶新的 "
                "`source_ids` 明確重生 —— 兩種情況都不會靜默改成讀整本筆記本。"
            )
        return (
            f"**重生時必須帶回原本那組 `source_ids`{listed}** —— 這一集的生成輸入指名了"
            "來源,改用 podcast_series 會靜默改成讀整本筆記本。"
        )
    # 認不出來的形狀(例如 resume 建的 `{"origin": "explicit_resume"}`)。**不能假裝
    # 知道**它原本讀了哪幾筆 —— manifest 裡沒有那個資訊。
    return (
        f"⚠️ 這顆 attempt 的 settings 認不出原本用了哪些來源(逐字是 {sorted(settings)!r})"
        " —— **重生時要自己指名 `source_ids`**,不指名會讀整本筆記本。用 source_list "
        "挑「本集自己的來源 + 最近 5 集的音檔回錄」。"
    )


def _safe_next_target(
    action: str | None,
    *,
    attempt_id: str,
    remote: dict,
    replacement_caps: dict | None,
) -> tuple[str | None, str | None]:
    """把 `safe_next_action` 翻成呼叫端**照做時要傳的目標身分**(attempt_id／artifact_id)。

    P1(Codex 獨立審查實跑驗證):`podcast_attempt_retract` 委派給 sibling(retract 之後
    發現 `active_attempt_id` 指向另一顆還在飛的 attempt B)時,`safe_next_action` 換成了
    B 的,但公開回傳的 `attempt_id` 仍是被 retract 的 A(那是 retraction 稽核紀錄的本體,
    不能改)——呼叫端照著回傳的 `safe_next_action` + `attempt_id` 執行,實際上是拿 A 的
    id 去做 B 的事:`podcast_episode_reconcile(A)` 撞 tombstone、`podcast_attempt_retract(A)`
    冪等重跑撞回一模一樣的回傳形成無限迴圈。

    這裡不重覆 `attempt_capabilities` 那條「哪個動作」的優先序 if/else(重覆等於下一次
    改優先序時兩邊漏改一邊,正是 docs/gotchas-attempt.md 記錄的第 N 次現形形狀)——只認
    **已經算好的 `action` 字面值**對應哪種身分,單一映射,呼叫方永遠只有一份。

    sibling 交棒(`replacement_caps` 存在,且它的 `safe_next_action` 就是這裡要用的
    `action`):目標身分整段換成替代 attempt 自己算出來的身分,不能沿用這顆(A)的
    `attempt_id`/`remote`——那正是 P1 的根因。
    """
    if action is None:
        return None, None
    if replacement_caps is not None and action == replacement_caps["safe_next_action"]:
        return (
            replacement_caps["safe_next_attempt_id"],
            replacement_caps["safe_next_artifact_id"],
        )
    if action == ACTION_RESUME:
        # `podcast_episode_resume` 認 artifact_id,不是 attempt_id。
        return attempt_id, remote.get("artifact_id")
    if action in (ACTION_RETRACT, ACTION_RECONCILE, ACTION_ADOPT):
        return attempt_id, None
    # regeneration entry(series/episode)是全新呼叫,不指名既有 attempt;
    # source_delete／notebook_share_with_pool 認的是別種身分(source_id／notebook_id),
    # 兩者都已經在各自的回傳欄位裡(`stale_source_ids`／呼叫端自己的 notebook_id)。
    return None, None


def attempt_capabilities(
    episode: dict,
    attempt: dict,
    attempt_id: str,
    *,
    reconciliation_window_closed: bool | None = None,
    candidate_selection_required: bool = False,
    post_retract: bool = False,
) -> dict:
    """**「這顆 attempt 現在能做什麼」的單一事實來源。**

    純函式,不打 RPC、不改狀態。所有給呼叫端的 `safe_next_action` 與「下一步」訊息都要
    從這裡產生,不要各自手寫。**`reconciliation_window_closed` 也遵守這條**:呼叫端
    自己算好「現在幾點」與候選窗有沒有關再傳進來,這裡不讀時鐘(P2 修復)——否則
    `podcast_episode_reconcile` 的零候選分支會繼續自己手寫 if/else 決定
    `safe_next_action`,正是 docs/gotchas-attempt.md 那條紅線要擋的第二個決策來源。

    **這是三態,不是布林**(第四輪修復):`None` = 呼叫端沒算過,`True`/`False` =
    明確算過的結果。`attempt_capabilities()` 有五個呼叫點,只有
    `podcast_episode_reconcile` 真的讀時鐘算過窗;另外四個(attempt 建立衝突、resume
    的兩個停點、`_reuse_frozen_input_attempt`)手上根本沒有 `dispatched_at`
    可以算,只是要餵一顆 attempt 進來問「現在能做什麼」。**這四個永遠吃預設值,而
    預設值的語意不能是「窗還沒關」這個具體宣稱**——上一輪用 `False` 當預設,結果
    `attempt_next_step()` 講出「候選窗還沒關,過幾分鐘後重呼有機會撈到」這種話,
    而真的算過窗的 `podcast_episode_reconcile` 對同一顆 attempt(dispatched_at 是
    3 天前)說「候選窗已經關了」——兩支工具對同一個狀態給互相矛盾的事實。`None`
    才是誠實的預設:「不知道,沒資訊可講」,`attempt_next_step()` 對 `None` 一律
    回到不含窗宣稱的措辭。`can_reconcile` 的計算不受影響——`not None` 與
    `not False` 都是 `True`,跟 v0.9.9 的行為一致(視為未關閉)。

    **為什麼收斂成一顆:同一個根因現形過五次。** 每次的形狀都是「指引在它自己產生的
    狀態下不可執行」——v0.9.1 FAIL-1(叫人跑一支在該狀態下自己也 permission denied 的
    工具)、v0.9.3(停點指向會拒收它的工具)、v0.9.4(拒絕訊息那句在該狀態下是假的)、
    v0.9.5 一次六條(其中三條是修上一條時種的)、以及第五次:把
    「可免旗標 retract」誤當成「可原樣重送」——`failed`/`removed` 兩者的答案相反,
    共用一個布林就必然教錯一邊。

    修法每次都是「補那一格」,而判斷分散在三個各自為政的布林
    (`_outcome_is_settled` / `attempt_admission.is_resendable_same_request` / `abandons_unauthorized_candidate`)
    加十幾處手寫訊息裡 —— 只要保持那個結構,第六次幾乎必然。這裡把**維度**與**結論**
    都攤開,由 `tests/test_attempt_capabilities.py` 對狀態組合的**笛卡爾積**逐格鎖住:
    想不到的組合也不會漏,那正是前幾輪突變驗證照不到的盲區。

    回傳的每個 key 都回答一個**具體可執行的問題**:

    - `authorization_basis`:免旗標 retract 的理由;`None` 代表需要 `abandon_in_flight`。
    - `feedback_upload_unresolved` / `feedback_upload_status`:回錄 source 已 dispatch
      但 `source_id` 還沒落盤(三種狀態,見 `unresolved_upload_descriptor`)。**這個維度
      不擋 retract**——擋住只是把死鎖換個地方(F2:cancellation 讓 checkpoint 永久停在
      `dispatching`,而唯一出口 `_reconcile_source_upload` 只從 finalize 進得去,等於
      「要作廢一顆輸入本來就錯的 attempt,得先把它完整 finalize」)。它的作用是讓
      `safe_next_action` 優先建議 resume;真要作廢就走 `abandon_in_flight`,義務由
      `cleanup_state` 接手。
    - `cleanup_state`:retract 之後的清理義務狀態。`pending_delete` = 身分已確定、
      有 id 要 `source_delete`;`reconcile_after` = 還不知道遠端有沒有孤兒,要等候選窗
      關上由生成前的 gate 對帳;`None` = 沒有義務。
    - `can_resend`:原樣重呼建立它的那支工具能不能沿用同一顆重送(**不等於**
      `authorization_basis` 非 None —— 那正是第五次現形的成因)。
    - `can_resume` / `can_reconcile`:有沒有 artifact 可續、要不要先對帳(候選窗關了
      就不算「可以對帳」——窗關代表未來任何 artifact 都會落在窗外,繼續對帳沒有用)。
    - `regeneration_entry`:作廢之後重生該用哪支工具。
    - `safe_next_action`(P2 修復):這個狀態下**單一一支**最該做的工具
      (`SAFE_NEXT_ACTIONS` 之一,或 `None` 代表歷史紀錄沒有可執行的下一步)。跟
      `attempt_next_step()` 的文字分支順序逐字對齊、由同一組笛卡爾積測試互相對照
      鎖住——那裡是「怎麼講」,這裡是「哪支工具」,兩者故意分開算但答案不准分岔。
      **P1 修復**:ADR-0009 amendment 明列的 active≠output 分岔(自己是 active,
      但另有一顆不同的 attempt 是 output)這一格,`ROLES` 笛卡爾積(`active`/
      `output`/`historical`)原本生不出來——can_resend/can_resume/can_reconcile
      對它可能仍算出 True,但 resume 與原樣重呼都會被既有 output 擋下來,唯一出口
      是先 retract 自己,見下面 `elif basis == "output_owner":` 那個分支。
    - `safe_next_attempt_id` / `safe_next_artifact_id`(P1 修復,見 `_safe_next_target`):
      `safe_next_action` 這支工具**要用誰的身分呼叫**——一般狀況下就是這顆
      attempt 自己(`attempt_id`)或它的 `remote.artifact_id`(resume 認的是
      artifact_id,不是 attempt_id);但 `post_retract` 委派給 sibling(見下面
      `replacement_caps`)時,身分整段換成替代 attempt 自己的,**不是**這顆(即將
      被 retract 或已被 retract 的那顆)。回傳裡若同時有 `attempt_id`(稽核主體)
      與這兩個欄位,呼叫端下一步要用的是這兩個欄位,不是 `attempt_id`。
    """
    dispatch_status = (attempt.get("dispatch") or {}).get("status")
    remote = attempt.get("remote") or {}
    remote_status = remote.get("status")
    is_active = attempt_id == episode.get("active_attempt_id")
    output_attempt_id = episode.get("output_attempt_id")
    is_output = attempt_id == output_attempt_id
    # 「已 dispatch、source_id 還沒落盤」——判準與清理義務對帳共用同一支
    # (`unresolved_upload_descriptor`),不在這裡重讀 checkpoint 發明第二套。
    unresolved_upload = unresolved_upload_descriptor(attempt)
    feedback_upload_unresolved = unresolved_upload is not None
    feedback_upload_status = feedback_upload(attempt).get("status")
    # T1:候選一律來自 `unresolved_upload`(`_reconcile_source_upload` 寫入的同一份
    # checkpoint),非空即代表狀態是 `reconciliation_ambiguous`(該函式把「有候選」與
    # 「狀態設成 ambiguous」綁在同一次 mutate 裡,兩者不會分岔)。
    feedback_upload_candidates = (
        list(unresolved_upload.get("candidate_source_ids") or [])
        if unresolved_upload is not None
        else []
    )

    # Not derived from `tools_podcast._series_redispatch_kind` on purpose: for
    # `prepared`+`failed` kind is "supersede" yet both of these are True.
    never_dispatched = dispatch_status in NEVER_DISPATCHED
    remote_terminal = remote_status in TERMINAL_REMOTE

    # 免旗標 retract 的理由。順序即優先序,第一個成立的就是稽核要記的那個。
    if is_output:
        basis = "output_owner"  # 正常的 QA 拒收,本來就不需要旗標
    elif not is_active:
        basis = None  # 歷史 attempt:誰都動不了它,旗標也無效
    elif output_attempt_id is not None:
        basis = "output_owner"  # active/output 分岔,投影屬於現任 output
    elif never_dispatched or remote_terminal:
        basis = "settled"  # manifest 自己就知道結果
    elif has_hard_output_evidence(episode):
        basis = "legacy_evidence"
    else:
        basis = None  # 真的不知道有沒有東西在跑 —— 要外部知識

    # **只有從沒送出去的才重送得了。** 遠端已終態(`failed`/`removed`)雖然同樣
    # 「結果已定」,重送卻是走 supersede 建新 attempt,不是沿用這顆 ——
    # `attempt_admission.is_resendable_same_request` 也只收 `NEVER_DISPATCHED`。
    can_resend = never_dispatched and not is_output
    can_resume = bool(remote.get("artifact_id")) and not is_output
    can_reconcile = (
        dispatch_status in RECONCILABLE_DISPATCH_STATES and not reconciliation_window_closed
    )
    regeneration_entry = regeneration_entry_point(attempt)
    preserves_existing_output = post_retract and episode.get("output_attempt_id") not in (
        None,
        attempt_id,
    )
    pending_source_cleanup = False
    source_cleanup_unresolved = False
    cleanup_obligations: list[dict] = []
    if post_retract:
        obligations = list_obligations(episode, fallback_notebook=episode.get("notebook_id"))
        pending_source_ids = {row["source_id"] for row in obligations}
        retracted_source_ids = (attempt.get("retraction") or {}).get("stale_source_ids") or []
        # **retract 可以作廢一顆 upload 還沒落盤的 attempt(abandon_in_flight),但不能
        # 忘掉它可能留下的孤兒。** 身分不確定的清理義務記在 tombstone 上,由生成前的
        # `assert_generation_allowed` 用候選窗對帳結案——所以這裡不是「還有東西要刪」
        # (那是 `pending_source_cleanup`),而是「還不知道有沒有東西要刪」。
        source_cleanup_unresolved = bool(
            (attempt.get("retraction") or {}).get("source_cleanup_unresolved")
            and feedback_upload_unresolved
        )
        # **這一刻真正要刪的是哪幾筆。** 兩個來源:①retract 當下就知道身分的
        # (`stale_source_ids`,仍在 episode 的 pending 裡);②gate 對帳之後才撈到的候選
        # —— 那些**只會進 episode 的 `pending_source_cleanup`,不回寫 tombstone**
        # (ADR-0009:tombstone 是稽核紀錄,不改寫;而「這一集有具體 id 要刪」本來就是
        # episode 級事實,複製進 retraction 只是多一個會漂移的副本,與 F2 已確立的
        # 「只記狀態、不複製 checkpoint」同一條)。
        # 少了②,retract 的冪等回傳會說 `reconcile_after`／`safe_next_action=None`,而同一
        # 時刻生成 gate 正拿著具體 id 擋著要人刪 —— 兩個入口對同一狀態指向不同動作。
        covered = {
            source_id for source_id in retracted_source_ids if source_id in pending_source_ids
        }
        if source_cleanup_unresolved:
            covered |= pending_source_ids
        # **回傳結構化的義務,不是只有 id。** `source_delete` 要 notebook_id + source_id
        # 兩個參數,只給 id 的話「下一步的公開回傳必須自足」這條紅線不成立。
        cleanup_obligations = sorted(
            (row for row in obligations if row["source_id"] in covered),
            key=lambda row: row["source_id"],
        )
        pending_source_cleanup = bool(cleanup_obligations)
    cleanup_state = None
    if pending_source_cleanup:
        cleanup_state = "pending_delete"
    elif source_cleanup_unresolved:
        cleanup_state = "reconcile_after"

    # F1(盲審 P1 regression,v0.9.10 之後現形):post_retract 分支原本無條件回
    # `regeneration_entry`，從不看 `episode["active_attempt_id"]`——如果 retract 之後
    # 重生已經成功建出替代 attempt(active，但還沒 promote 成 output），教它「用
    # regeneration_entry 重生」在這裡是死路：`_create_audio_attempt`／series 一看到
    # `active_attempt_id` 已經指向別顆就直接 raise「already has durable active
    # attempt」。改成看那顆替代 attempt**自己**的 capabilities，教它現在真正能做
    # 的事（resume／reconcile／retract 它自己……）。`preserves_existing_output` 只
    # 覆蓋替代版已經 promote 成 output 的情形，這裡補的是它還在飛、尚未 promote 的
    # 中間態。
    replacement_attempt_id = episode.get("active_attempt_id")
    replacement_caps: dict | None = None
    if post_retract and replacement_attempt_id not in (None, attempt_id):
        replacement_attempt = next(
            (
                row
                for row in episode.get("attempts", [])
                if row.get("attempt_id") == replacement_attempt_id
            ),
            None,
        )
        if replacement_attempt is not None:
            replacement_caps = attempt_capabilities(
                episode, replacement_attempt, replacement_attempt_id
            )

    # **`safe_next_action` 的優先序必須跟 `attempt_next_step()` 的分支順序逐字
    # 對齊**(見上方 docstring)。
    if post_retract:
        if pending_source_cleanup:
            safe_next_action = ACTION_SOURCE_DELETE
        elif source_cleanup_unresolved:
            # **不准假裝重生現在就做得到**:候選窗還沒對帳完,`regeneration_entry` 會被
            # `assert_generation_allowed` 擋成 ValueError。沒有可執行的單一工具,
            # 要等的事情由 `cleanup_state` 與 `attempt_next_step()` 講清楚。
            safe_next_action = None
        elif preserves_existing_output:
            safe_next_action = None
        elif replacement_caps is not None:
            safe_next_action = replacement_caps["safe_next_action"]
        else:
            safe_next_action = regeneration_entry
    elif not is_active and not is_output:
        safe_next_action = None  # 歷史紀錄,沒有可執行的下一步——要動的是 active/output
    elif feedback_upload_unresolved:
        # 歷史紀錄那格必須先判(上一個分支):一顆已被取代的 attempt 就算 upload 卡在
        # unresolved,能動的也不是它——教人 resume 一顆歷史 attempt 會被 output guard
        # 擋掉,又是一句「在它自己產生的狀態下不可執行」的指引。
        #
        # T1(第十三次現形):`reconciliation_ambiguous` 且候選非空時,resume 對同兩個
        # 候選只會再拋一次同一個 ambiguous(`_reconcile_source_upload` 重新算出一模一樣
        # 的 candidates),零前進——候選是 server 對帳算出來、呼叫端無法重建的身分,
        # 下一步是明確擇一 `podcast_attempt_adopt`,不是繼續 resume/reconcile 撞牆。
        # 候選為空(還沒撈到、或已經對帳成單一 id 落盤)時維持原判準。
        if feedback_upload_candidates:
            safe_next_action = ACTION_ADOPT
        else:
            safe_next_action = (
                ACTION_RESUME if can_resume else ACTION_RECONCILE if can_reconcile else None
            )
    elif is_output:
        safe_next_action = ACTION_RETRACT
    elif basis == "output_owner":
        # P1(Codex adversarial review 實跑驗證,ADR-0009 amendment 明列的
        # active≠output 分岔):`is_output` 已經在上面擋掉「自己就是 output」那格,
        # 走到這裡代表 `basis == "output_owner"` 只可能來自另一個分支——active=自己、
        # output=別顆(見上面 `elif output_attempt_id is not None:` 那行)。這顆是
        # 未經授權的 candidate,can_resend/can_resume/can_reconcile 對它可能仍算出
        # True(dispatch/remote 欄位形狀跟正常在飛的第一顆 attempt 分不出來),但
        # 實測兩條路都是死路:`_create_audio_attempt` 一看到 episode 已有
        # `output_attempt_id` 就在最前面整段拒收(連「原樣重呼」都進不去),
        # `_ensure_resume_attempt` 的兩條分支也都有同一道 output guard 拒收 resume。
        # 唯一走得通的出口是先 retract 自己——這裡優先序必須排在
        # can_resend/can_resume/can_reconcile 之前,否則會被那三個誤判的 True 蓋過去。
        safe_next_action = ACTION_RETRACT
    elif can_resend:
        # 原樣重呼＝再叫一次建立它的那支工具，跟「重生要用哪支工具」同一個判準。
        safe_next_action = regeneration_entry
    elif basis == "settled":
        safe_next_action = ACTION_RETRACT
    elif candidate_selection_required and can_reconcile:
        # 候選歸屬需要 manifest 之外的知識，但 output owner 等更高優先序
        # 仍必須先接手；這樣 guard 停點不會再蓋掉 active≠output 的正門。
        safe_next_action = ACTION_ADOPT
    elif can_resume:
        safe_next_action = ACTION_RESUME
    elif can_reconcile:
        safe_next_action = ACTION_RECONCILE
    else:
        safe_next_action = ACTION_RETRACT

    safe_next_attempt_id, safe_next_artifact_id = _safe_next_target(
        safe_next_action,
        attempt_id=attempt_id,
        remote=remote,
        replacement_caps=replacement_caps,
    )
    # **有替代 attempt 在飛 = 重生的意圖已經是它的**,與這一步要做什麼無關。綁在
    # `safe_next_action` 上會產生自相矛盾的 payload:`safe_next_action=source_delete`
    # 時欄位給被 retract 那顆的來源,而同一份回傳的 `next_step` 正說著「替代 attempt
    # 已經在飛,不能再走重生」。照欄位做的自動化拿到的正是這個欄位要防的靜默還原。
    regeneration_source_ids = (
        replacement_caps["regeneration_source_ids"]
        if replacement_caps is not None
        else (attempt.get("settings") or {}).get("source_ids")
    )

    return {
        "dispatch_status": dispatch_status,
        "remote_status": remote_status,
        "is_active": is_active,
        "is_output": is_output,
        "authorization_basis": basis,
        "needs_abandon_flag": basis is None and is_active,
        "feedback_upload_unresolved": feedback_upload_unresolved,
        "feedback_upload_status": feedback_upload_status,
        # T1(紅線⑫):`ACTION_ADOPT` 認的身分是候選 source id,不是 attempt_id/
        # artifact_id——`_safe_next_target` 那組欄位裝不下它,所以另開一個永遠存在的
        # 欄位(空清單 = 沒有候選要選),呼叫端不必去猜也不必翻 manifest。
        "candidate_source_ids": feedback_upload_candidates,
        "can_resend": can_resend,
        "can_resume": can_resume,
        "can_reconcile": can_reconcile,
        # 三態原始值(不是 can_reconcile 那個已經跟 dispatch_status 混在一起的布林)
        # 留給 `attempt_next_step()` 判斷要不要講窗宣稱——`None` 時不准講。
        "reconciliation_window_closed": reconciliation_window_closed,
        "candidate_selection_required": candidate_selection_required,
        "post_retract": post_retract,
        "pending_source_cleanup": pending_source_cleanup,
        # 具體要刪的義務,`[{"notebook_id", "source_id"}]`。**指引與回傳都必須列得出它們**
        # ——「把 stale_source_ids 全部 source_delete」在 gate 撈到候選之後是假的(那個
        # 欄位是空的,id 只在 episode 的 pending 裡);而只給 source_id 也不夠,
        # `source_delete` 兩個參數都要。
        "source_cleanup_obligations": cleanup_obligations,
        "source_cleanup_unresolved": source_cleanup_unresolved,
        "cleanup_state": cleanup_state,
        "preserves_existing_output": preserves_existing_output,
        # F1 修復:非 None 代表 retract 之後已經有替代 attempt 在飛（尚未 promote 成
        # output）。內部欄位，只給 `attempt_next_step()` 遞迴用，不對外洩漏——外層
        # 呼叫端只讀 `safe_next_action` 與 `attempt_next_step()` 的文字。
        "post_retract_replacement_caps": replacement_caps,
        "regeneration_entry": regeneration_entry,
        # **重生／原樣重呼要帶回的那組 `source_ids`,逐字列出來。**(Codex 獨立複審
        # 實跑抓到)`regeneration_hint` 說「必須帶回原本那組」,但公開回傳裡沒有那組
        # —— 呼叫端只能去翻 manifest,而 docs/gotchas-attempt.md 的自足性紅線正好禁止
        # 那件事。照著做不了的結果是靜默不指名:`podcast_episode` 的 `source_ids`
        # 預設 `None`,直接讀整本筆記本。`None` = manifest 沒記(例如 resume 建的
        # `{"origin": "explicit_resume"}`),那時 hint 會說「要自己指名」。
        # **交棒時這一格也要跟著換。** `safe_next_*` 三個欄位都由 `_safe_next_target()`
        # 整組換成替代 attempt 的,而這一格原本寫死讀 `attempt`(被 retract 的那顆)——
        # host 拒收 A(s1+s2)後改用 s1+s3 重生 B、B 撞配額停住,冪等重呼 retract(A)
        # 拿到的卻是 A 的 s1+s2,照著帶回去就把那次刻意的來源置換**靜默還原**
        # (v0.9.26,同一根因第十二次現形)。
        "regeneration_source_ids": regeneration_source_ids,
        "regeneration_hint": _regeneration_hint(
            attempt, resend_possible=can_resend and not post_retract
        ),
        "safe_next_action": safe_next_action,
        # P1 修復:委派給 sibling 時,呼叫端下一步要用的身分——不是 `attempt_id` 這個
        # 稽核主體(見上方 docstring)。
        "safe_next_attempt_id": safe_next_attempt_id,
        "safe_next_artifact_id": safe_next_artifact_id,
    }


def retract_hint(caps: dict) -> str:
    """要不要在 `podcast_attempt_retract` 前先查雲端,單一事實來源。

    `attempt_next_step` 的「遠端可能還有東西」分支與 `podcast_episode_reconcile`
    的零候選分支都要教「作廢建議」,而且答案必須跟著同一顆 caps 的
    `needs_abandon_flag` 走——各自手寫一份 if/else 正是這一輪 F3 修正自己種下的雷
    (兩份判準其中一份漏寫「不需要」三個字,而唯一的測試斷言對兩支都成立,照樣全綠)。
    """
    if caps["needs_abandon_flag"]:
        return (
            "確定那次生成要作廢的話,artifact_list 查過雲端之後帶 abandon_in_flight=true "
            f"呼叫 {ACTION_RETRACT}。"
        )
    return f"要作廢就直接 {ACTION_RETRACT}(**不需要** abandon_in_flight)。"


def cleanup_sentence(rows: list[dict]) -> str:
    """清理義務 → 「先 source_delete」那句指引的**唯一** renderer(retract 與 adopt 共用)。

    每筆讀它自己記錄的 notebook 身分,不准拿呼叫當下的 notebook 對全部蓋一遍。
    **身分不明時不准印出假的可執行呼叫**:legacy 純字串義務 + episode 沒有 notebook_id
    時是 `None`,印成 `source_delete(notebook_id=None, ...)` 照抄會失敗 —— 只講清楚缺什麼
    (生成前的 gate 會用同一個理由 fail-closed,兩邊說法一致)。
    """
    return (
        "先把這幾筆 source_delete 掉:"
        + "、".join(
            (
                f"source_delete(notebook_id={row['notebook_id']!r}, source_id={row['source_id']!r})"
                if row["notebook_id"] is not None
                else (
                    f"source_id={row['source_id']!r}(這一筆沒有記到 notebook 身分,"
                    "補上 episode／manifest 的 notebook_id 後重呼本工具才算得出來)"
                )
            )
            for row in rows
        )
        + "。"
    )


def attempt_next_step(caps: dict) -> str:
    """把 `attempt_capabilities` 的結論翻成一句**可執行**的話。

    每個分支教的動作都必須在該狀態下真的做得到 —— 這是整顆 helper 存在的理由。

    **每個教人「retract 之後重生」的分支都要附上 `caps["regeneration_hint"]`**
    (P2 修復)—— 少附的下場是 `_regeneration_hint()` 早算好的關鍵提醒(尤其是
    frozen bundle 那句「要用一份新的、尚未綁定的 bundle」)不會出現在指引裡,照做的人
    沿用舊 bundle 會被 tombstone 擋下來,retract 教的路等於死路。`_regeneration_hint()`
    對不需要提醒的形狀本來就回空字串,所以在每個分支**無條件**附加是安全的——
    `tests/test_attempt_capabilities.py` 的窮舉序列測試驗證這一點。

    **候選窗狀態說明也在這裡產生**(P2 修復):`podcast_episode_reconcile` 零候選
    出口原本自己手寫 if/else 講「窗開了/關了」,現在從 `caps["dispatch_status"]`
    (可對帳的狀態集合)與 `caps["can_reconcile"]`(已經算進 window_closed)推出來
    ——呼叫點只組裝回傳欄位,不再做任何判斷。

    **窗宣稱只能在明確算過時講**(第四輪修復):`caps["reconciliation_window_closed"]`
    是三態(`None`/`True`/`False`)。`can_reconcile` 分支只有在它是 `False`(明確
    算過、窗還沒關)時才附加窗狀態說明;`None`(五個呼叫點裡有四個從沒算過時間)時
    回到 v0.9.9 原文,不提窗的任何字——否則會跟真的算過窗、判定「已經關了」的
    `podcast_episode_reconcile` 對同一顆 attempt 打對台。

    **P1 修復:兩處窗宣稱都不准再講「候選窗」。**`can_reconcile` 依據的是
    `reconciliation_window_closed`(**關閉判斷窗**,含 `_RECONCILIATION_MIN_WINDOW`
    1 小時保守下限),跟 `podcast_episode_reconcile` 篩選候選用的候選窗(`promised`
    原始值,不套下限)是刻意拆開的兩個不同的窗(見檔案開頭 `_RECONCILIATION_MIN_WINDOW`
    的常數註解)。Codex 實跑抓到:`promised=60`、dispatch 在 1500 秒前、
    `wait_timeout=1` 重呼——候選窗早在 120 秒就關了,但 1 小時的關閉判斷窗還沒關,
    這裡卻回「候選窗還沒關」,把兩個窗混成一個。措辭改成講**這句話實際依據的那個
    窗**(關閉判斷窗),不再用「候選窗」這個已經另有所指的字面值。

    **窗已關的分支也拿掉了「重呼必然相同」的絕對宣稱**:原文說「再對帳一次也只會
    拿到一模一樣的回傳,繼續等沒有用」,但那只在呼叫端**沿用同一個 `wait_timeout`**
    時才成立——Codex 實跑對比:`wait_timeout=1` 得到這句話並宣稱死路,換成本 commit
    自己定義的救援值 `wait_timeout=10000` 重呼,同一顆 artifact 立刻被綁定。指引
    自己否定了自己提供的救援路徑。現在改成揭露這條路:**放大 `wait_timeout` 重呼
    有機會撈到更晚建立的 artifact**(候選篩選窗會跟著放大,關閉判斷窗的保守下限不受
    影響,見 `_promised_reconciliation_window_seconds` docstring)。
    """
    if caps["post_retract"]:
        # **要列得出 id,不能只報欄位名。** 「把 `stale_source_ids` 全部 source_delete」
        # 在 gate 對帳撈到候選之後是**假的**:那個欄位是空的(tombstone 不回寫),id 只在
        # episode 的 `pending_source_cleanup` 裡 —— 照字面做的呼叫端會拿到空清單而以為
        # 沒事要做,但生成 gate 正拿著同一批 id 擋著。身分不明的處理見 `cleanup_sentence`。
        cleanup = (
            cleanup_sentence(caps["source_cleanup_obligations"])
            if caps["pending_source_cleanup"]
            else ""
        )
        if caps["source_cleanup_unresolved"]:
            # **這個分支要排在重生之前**:義務沒結案時 `regeneration_entry` 會被
            # `assert_generation_allowed` 擋成 ValueError,教人重生就是教一條當下
            # 走不通的路。`safe_next_action` 在這一格是 `None`,兩邊一致。
            return (
                cleanup + "這顆的回錄 source 已經送出、但 source_id 沒落盤(upload 停在 "
                f"{caps['feedback_upload_status']!r}),notebook 裡可能多了一筆沒人記得的 "
                "media。清理義務已經記進 tombstone,不會消失:等候選窗關上後直接重呼 "
                f"{caps['regeneration_entry']},生成前的 gate 會自己去 notebook 對帳"
                "——撈到候選就把 id 排進這一集的 pending_source_cleanup 擋下來要你 "
                "source_delete(那時候重呼 podcast_attempt_retract 就會列出它們),"
                "確認零候選才放行。對帳失敗(認證/notebook 讀不到)時義務不會被清掉。"
                + caps["regeneration_hint"]
            )
        if caps["preserves_existing_output"]:
            return cleanup + "這一集的既有正式輸出不受影響，不必重生。"
        replacement_caps = caps["post_retract_replacement_caps"]
        if replacement_caps is not None:
            # F1 修復:retract 之後已經有替代 attempt 在飛（尚未 promote 成
            # output）——`regeneration_entry` 教的「重生」在這裡是死路
            # （`already has durable active attempt`）。改講那顆替代 attempt
            # 現在真正能做的事，遞迴复用同一顆事實來源，不重新發明一份判斷。
            return (
                cleanup
                + "替代 attempt 已經在飛（尚未成為正式輸出），不能再走「重生」："
                + attempt_next_step(replacement_caps)
            )
        return cleanup + f"用 {caps['regeneration_entry']} 重生。" + caps["regeneration_hint"]
    if not caps["is_active"] and not caps["is_output"]:
        return "它已經被取代,是歷史紀錄 —— 要動的是現在的 active／output attempt。"
    if caps["feedback_upload_unresolved"]:
        # 順序與 `safe_next_action` 逐字對齊(歷史紀錄先判)。這裡**不擋 retract**,
        # 只是把「先續完」排在前面:遠端可能已經多出一筆 media,續完是唯一能把它的
        # 身分認回來的路;真要作廢就照 `retract_hint()` 走旗標,義務會留在 tombstone。
        action = caps["safe_next_action"]
        if action == ACTION_ADOPT:
            # T1:候選已經是 server 對帳算出來的具體 id(`caps["candidate_source_ids"]`),
            # 「resume 會把身分對回來」這句話在這裡是假的——resume 對同兩個候選只會
            # 再拋一次同一個 reconciliation_ambiguous,零前進。改教明確擇一。
            recovery = (
                f"候選在 candidate_source_ids(逐字是 {caps['candidate_source_ids']!r}),"
                f"用 {ACTION_ADOPT} 擇一綁定——resume 對同樣的候選只會再拋一次同一個 "
                "reconciliation_ambiguous,零前進。"
            )
        elif action is not None:
            recovery = f"原呼叫中斷的話用 {action} 接續,它會把那筆 source 的身分對回來。"
        else:
            recovery = "先讓目前的 finalize 結案。"
        return (
            f"回錄 source 已經送出但 source_id 還沒落盤(upload 停在 "
            f"{caps['feedback_upload_status']!r}):" + recovery + retract_hint(caps)
        )
    if caps["is_output"]:
        return (
            "它是這一集的正式輸出:要作廢就直接 podcast_attempt_retract"
            "(**不需要** abandon_in_flight),照回傳的 stale_source_ids 逐一 source_delete,"
            f"再用 {caps['regeneration_entry']} 重生。" + caps["regeneration_hint"]
        )
    if caps["authorization_basis"] == "output_owner":
        # P1(見上面 `attempt_capabilities` 的 safe_next_action 分支,同一組笛卡爾
        # 積測試互相對照鎖住):走到這裡代表 active≠output 分岔——`is_output` 已在
        # 上面擋掉「自己就是 output」那格,這裡只可能是「自己是 active,但另有一顆
        # 不同的 attempt 是 output」。**不附 regeneration_hint**:retract 自己之後
        # episode 仍以既有 output 為準,不需要重生,附上「怎麼重生」的提醒反而是誤導。
        return (
            "這一集已經有另一顆 attempt 是正式輸出,這顆(active 但非 output)是未經"
            "授權的 candidate——resume 與對帳都會被既有輸出擋下來(ADR-0009 amendment):"
            "先 podcast_attempt_retract(**不需要** abandon_in_flight)作廢它就好,"
            "不必重生,既有輸出不受影響。"
        )
    if caps["can_resend"]:
        return (
            "參數完全相同就原樣重呼建立它的那支工具,沿用同一顆重送(不多燒配額);"
            f"要換 brief 或來源就先 podcast_attempt_retract(**不需要** abandon_in_flight),"
            f"再用 {caps['regeneration_entry']} 重生。" + caps["regeneration_hint"]
        )
    if caps["authorization_basis"] == "settled":
        # 遠端已終態:重送不是沿用這顆,而是作廢後重生(或讓 series 自動 supersede)。
        # **「也可以重呼 podcast_series」這句只在 regeneration_entry 真的是 series 時
        # 才加**——`_reuse_frozen_input_attempt` 這條路上 `input_bundle is not None`
        # 恆真,`regeneration_entry` 必定是 `podcast_episode`;若無條件教這句,series
        # 會走 supersede 分支、用 `_audio_settings()` 建一顆不指名來源的新 attempt,
        # 讀整本筆記本(含後面各集回錄)——v0.9.5 花整輪在防的內容錯置形狀。
        also_series = (
            f" 整季流程也可以直接重呼 {ACTION_SERIES} 讓它自動 supersede。"
            if caps["regeneration_entry"] == ACTION_SERIES
            else ""
        )
        return (
            "遠端已回報終態,這顆沒有東西可續也不能原樣重送:先 podcast_attempt_retract "
            f"(**不需要** abandon_in_flight),再用 {caps['regeneration_entry']} 重生。"
            + also_series
            + caps["regeneration_hint"]
        )
    # 以下都是「遠端可能還有東西」的狀態。**作廢建議一律跟著 `needs_abandon_flag`**,
    # 不能寫死 —— 有別的 output 接手、或 legacy 硬證據在場時,這顆的准入早就成立了,
    # 教人傳旗標等於教一個沒有作用的參數(窮舉測試一次抓出 75 個這種組合)。
    retract_advice = retract_hint(caps)
    # F2 修復:這裡的分支順序必須跟 `attempt_capabilities()` 的 `safe_next_action`
    # 優先序逐字對齊(見上方 docstring 的紅線)——`candidate_selection_required and
    # can_reconcile` 在那邊排在 `can_resume` 之前，這裡曾經反過來，於是同一顆 caps
    # 的 `safe_next_action` 教 adopt、`next_step` 的散文卻教 resume，兩個欄位對同一個
    # 狀態指向不同工具。
    if caps["candidate_selection_required"] and caps["can_reconcile"]:
        return (
            "manifest 無法判定候選 artifact 的歸屬:確認屬於這次 dispatch 就用 "
            f"{ACTION_ADOPT} 明確綁定;確認不屬於這次就不要 adopt。"
            + retract_advice
            + "作廢後再照 retract 回傳的入口重生。"
        )
    if caps["can_resume"]:
        return "遠端有 artifact:先 podcast_episode_resume 續完 finalize。" + retract_advice
    if caps["can_reconcile"]:
        if caps["reconciliation_window_closed"] is False:
            # 明確算過、窗還沒關——才有資格講「還有機會撈到」這句時間性宣稱。
            # **講的是關閉判斷窗,不是候選窗**(P1 修復,見上面 docstring):兩者是
            # 刻意拆開的不同窗,這句話依據的是前者。
            return (
                "受理結果不明:先 podcast_episode_reconcile 對帳(它可能已經在遠端跑完;"
                "這次對帳依據的關閉判斷窗還沒關,過幾分鐘後重呼有機會撈到,現在重呼"
                "未必是一模一樣的空結果)。" + retract_advice
            )
        # `None`:呼叫端沒算過窗(建立衝突／resume 停點／frozen 重呼卡住這四個
        # 呼叫點都是),不能宣稱「還沒關」——回到 v0.9.9 原文,一個字都不提窗。
        return (
            "受理結果不明:先 podcast_episode_reconcile 對帳(它可能已經在遠端跑完)。"
            + retract_advice
        )
    if caps["dispatch_status"] in RECONCILABLE_DISPATCH_STATES:
        # can_reconcile 在這裡已經是 False,而 dispatch_status 仍落在可對帳的集合裡,
        # 只可能是呼叫端傳了 `reconciliation_window_closed=True`(明確算過、窗真的
        # 關了——`None`/`False` 都會讓 can_reconcile 維持 True,走不到這裡)。把
        # `podcast_episode_reconcile` 零候選出口原本手寫的窗狀態說明收進來,呼叫點
        # 就不用再自己組。**故意不提 `podcast_episode_reconcile` 這個字面值**:窗
        # 關了之後這個工具名不准再出現在指引裡(見
        # test_every_state_combination_yields_executable_guidance 的 window_closed 斷言)。
        # **P1 修復(見上面 docstring)兩處**:①講的是關閉判斷窗,不是候選窗——這裡
        # 才套 `_RECONCILIATION_MIN_WINDOW` 保守下限,跟候選篩選窗是刻意拆開的兩個
        # 窗。②拿掉「重呼必然相同」的絕對宣稱:那只在沿用同一個 `wait_timeout` 時
        # 才成立,放大它是救援路徑的逃生口,不能被這句話堵死。
        return (
            "關閉判斷窗(dispatch 到 wait_timeout 與 1 小時保守下限取大者,含時鐘容錯)"
            "已經關了——沿用原本的 wait_timeout 重呼只會拿到一模一樣的空結果;但"
            "**放大 wait_timeout 重呼有機會撈到更晚建立的 artifact**,不是「繼續等"
            "就沒有用」。" + retract_advice
        )
    return retract_advice
