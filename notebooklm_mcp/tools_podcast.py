"""Composite podcast tools implementing sequential feedback.

Continuity comes from NotebookLM transcribing the prior episode mp3 that we
re-upload as a source. The generation loop is deterministic Python code.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from mcp.types import ToolAnnotations
# `RateLimitError` / `ArtifactFeatureUnavailableError` 的分類已隨 failover 迴圈移到
# `_failover.py`(本檔只透過 `_REFUSED_WITHOUT_DISPATCH` 這個別名用它們)。
from notebooklm.exceptions import NetworkError
from notebooklm.types import ArtifactType

from . import runtime
from ._sources import (
    TooManySourcesError,
    assert_source_count_is_safe,
    assert_sources_exist,
    to_source_ids,
)
from ._status import TerminalGenerationError, ensure_completed
from .audio_finalize import (
    UPLOAD_DISPATCH_WINDOW,
    RemoteArtifactUnverifiableError,
    claimed_source_ids,
    finalize_attempt,
    has_durable_output_evidence,
    has_hard_output_evidence,
    new_finalize_state,
    unresolved_upload_candidates,
    unresolved_upload_descriptor,
    upload_dispatch_window_closed,
)
from ._errors import NotebookAccessDenied
from ._failover import (
    REFUSED_WITHOUT_DISPATCH as _REFUSED_WITHOUT_DISPATCH,
    describe_refusal,
    dispatch_with_failover,
    rotate_for_quota,
)
from .app import mcp
from .auth_probe import _AuthProbeError, probe_auth
from .enums import to_audio_format, to_audio_length
from .generation_input import (
    load_frozen_generation_input,
    read_attempt_binding,
    rollback_attempt_binding,
    write_attempt_binding,
)
from .languages import resolve_language
from .manifest_store import (
    ManifestConflictError,
    ManifestPostCommitError,
    ManifestStore,
)
from .naming import episode_label as _episode_label

_TZ = timezone(timedelta(hours=8))
_RECONCILIATION_CLOCK_SKEW = timedelta(minutes=1)
# `podcast_episode_reconcile` 有兩個窗，保守的方向剛好相反，**不能共用同一個判準**
# (第四輪主迴圈裁決)：
#   - 關閉判斷：窗「大」才保守——窗小＝提前宣告「繼續等沒有用」＝教人 tombstone
#     一顆可能還在飛的 attempt（ADR-0009 禁止的因果改寫）。
#   - 候選篩選：窗「小」才保守——窗大＝把不屬於這次 dispatch 的 artifact 也算成
#     候選，`_bind_reconciled_artifact` 會把別人的音檔綁進這一集。
# 上一輪把兩者合併成 `_effective_reconciliation_window_seconds` 一顆值、兩邊都套
# 同一個保守下限，於是「legacy attempt 的候選窗被下限撐大而誤綁別人的 artifact」與
# 「持久化承諾比下限小時，關閉判斷不套下限而提前判關」兩個方向各壞一半。
# `_RECONCILIATION_MIN_WINDOW` 因此只在**關閉判斷**的呼叫點套用（見
# `_promised_reconciliation_window_seconds` 與下面兩個呼叫點的分工），候選篩選窗
# 一律用 promised 原始值、不套下限。
_RECONCILIATION_MIN_WINDOW = timedelta(seconds=3600)


def _promised_reconciliation_window_seconds(dispatch: dict, wait_timeout: float) -> float:
    """算出**那次 dispatch 原始承諾要等多久**（`promised`），不套任何保守下限。

    `promised = max(持久化值 if 有, 這次呼叫的 wait_timeout)`——只取大不取小：
    持久化值（`_claim_prepared_dispatch` 存的原始秒數，見該函式 docstring）是真的
    承諾，呼叫端不能用一次較小的 `wait_timeout` 把它讀小；但呼叫端也可以**顯式放大**
    `wait_timeout` 去撈遲到的 artifact（救援路徑的逃生口），這裡要讓那個放大生效，
    不能被持久化值蓋掉。讀不到持久化值（v0.9.9 以前建立的 legacy attempt）時就只剩
    這次呼叫的值。

    **回傳值直接拿去當候選篩選窗**（`candidate_window_end`）——候選篩選窗小才保守，
    這裡故意不套 `_RECONCILIATION_MIN_WINDOW`。要算關閉判斷窗的呼叫點，自己在外面
    套 `max(這個回傳值, _RECONCILIATION_MIN_WINDOW)`，別把下限塞進這支共用函式。
    """
    persisted = dispatch.get("wait_timeout")
    promised = float(wait_timeout)
    if isinstance(persisted, (int, float)) and not isinstance(persisted, bool):
        promised = max(promised, float(persisted))
    return promised


# 集合的定義與「為什麼刻意不長大」在 `_failover.py`(附件家族也走同一個迴圈,各留一份
# 等於埋一顆「上游改了同步拒絕的例外型別、只有一處被改到」的地雷)。這裡只是 import 時
# 改名成既有呼叫點與文件引用用的私有名,不再另外賦值一行。
_TRANSIENT_TRANSPORT_ERRORS = (TimeoutError, ConnectionError, NetworkError)

# `NotebookAccessDenied` / `is_permission_denied` 住在 `_errors.py`(v0.9.0 起
# `tools_basic.notebook_share_with_pool` 也要判同一件事)。權限分類本身已經由
# `_failover.dispatch_with_failover` 統一處理,本檔不再有呼叫點,所以 v0.9.16 起
# 不再保留 `_is_permission_denied` 別名。

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
        # `_is_resendable_same_request` 判成不同請求而拒絕(`already has durable
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
_TERMINAL_REMOTE = frozenset({"failed", "removed"})
# dispatch 從沒離開本機:契約保證伺服器沒建出 task(ADR-0010 紀律④的立論基礎)。
_NEVER_DISPATCHED = frozenset({"prepared", "not_accepted"})
# T10(測試債,wp-a2):`can_reconcile` 認的那三個「還可能對帳得到東西」的 dispatch
# 狀態——原本以字面 tuple 散落在本模組三處(`can_reconcile` 本身、
# `_attempt_next_step` 的窗關閉分支、`_unresolved_attempt_ids` 曾經另開的
# `_UNRESOLVED_DISPATCH_STATUSES`)與 `test_attempt_capabilities.py` 的 skip
# 白名單各抄一份;抽成具名常數讓它們共用同一份真相,別再各自維護一份等著漏改。
_RECONCILABLE_DISPATCH_STATES = frozenset(
    {"dispatching", "acceptance_unknown", "reconciliation_ambiguous"}
)


def _regeneration_entry_point(attempt: dict) -> str:
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
    """重生**這一顆**時要注意什麼 —— 與 `_regeneration_entry_point()` 同源判斷。

    v0.9.6 把入口收斂了,卻讓這句話在 retract 的回傳裡另外用 if/else 猜:「有 bundle」
    以外一律說「必須帶回原本那組 `source_ids`」。而入口的白名單**特意**把「認不出來的
    形狀」也導向 `podcast_episode`(保守要求明示),於是那句話對
    `settings={"origin": "explicit_resume"}` 的 attempt 是**假的** —— manifest 裡根本
    沒有那組 source_ids(v0.9.6 真實驗收 FINDING-4)。收斂做了一半就是這個下場:
    action 對了,附帶的話還是錯的。

    `resend_possible`(v0.9.10 修復):是不是也能**原樣重呼**這顆(`caps["can_resend"]`)。
    只有 `source_ids` 那句在兩種情境下的正確措辭不同 —— can_resend 為真時,呼叫端除了
    原樣重送,還有「retract 之後刻意換來源重生」這個選項,這時無條件講「必須帶回原本」
    會跟 `_attempt_next_step` 的 can_resend 分支(「要換來源就先 retract」)互相矛盾:
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
        # **列得出 id,不能只報欄位名。** 同一條紀律在 `_attempt_next_step()` 的清理義務
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

    這裡不重覆 `_attempt_capabilities` 那條「哪個動作」的優先序 if/else(重覆等於下一次
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


def _attempt_capabilities(
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
    明確算過的結果。`_attempt_capabilities()` 有五個呼叫點,只有
    `podcast_episode_reconcile` 真的讀時鐘算過窗;另外四個(attempt 建立衝突、resume
    的兩個停點、`_reuse_frozen_input_attempt`)手上根本沒有 `dispatched_at`
    可以算,只是要餵一顆 attempt 進來問「現在能做什麼」。**這四個永遠吃預設值,而
    預設值的語意不能是「窗還沒關」這個具體宣稱**——上一輪用 `False` 當預設,結果
    `_attempt_next_step()` 講出「候選窗還沒關,過幾分鐘後重呼有機會撈到」這種話,
    而真的算過窗的 `podcast_episode_reconcile` 對同一顆 attempt(dispatched_at 是
    3 天前)說「候選窗已經關了」——兩支工具對同一個狀態給互相矛盾的事實。`None`
    才是誠實的預設:「不知道,沒資訊可講」,`_attempt_next_step()` 對 `None` 一律
    回到不含窗宣稱的措辭。`can_reconcile` 的計算不受影響——`not None` 與
    `not False` 都是 `True`,跟 v0.9.9 的行為一致(視為未關閉)。

    **為什麼收斂成一顆:同一個根因現形過五次。** 每次的形狀都是「指引在它自己產生的
    狀態下不可執行」——v0.9.1 FAIL-1(叫人跑一支在該狀態下自己也 permission denied 的
    工具)、v0.9.3(停點指向會拒收它的工具)、v0.9.4(拒絕訊息那句在該狀態下是假的)、
    v0.9.5 一次六條(其中三條是修上一條時種的)、以及第五次:把
    「可免旗標 retract」誤當成「可原樣重送」——`failed`/`removed` 兩者的答案相反,
    共用一個布林就必然教錯一邊。

    修法每次都是「補那一格」,而判斷分散在三個各自為政的布林
    (`_outcome_is_settled` / `_is_resendable_same_request` / `abandons_unauthorized_candidate`)
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
      `_attempt_next_step()` 的文字分支順序逐字對齊、由同一組笛卡爾積測試互相對照
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
    feedback_upload_status = (
        attempt.get("finalize", {}).get("feedback_source_upload", {})
    ).get("status")
    # T1:候選一律來自 `unresolved_upload`(`_reconcile_source_upload` 寫入的同一份
    # checkpoint),非空即代表狀態是 `reconciliation_ambiguous`(該函式把「有候選」與
    # 「狀態設成 ambiguous」綁在同一次 mutate 裡,兩者不會分岔)。
    feedback_upload_candidates = (
        list(unresolved_upload.get("candidate_source_ids") or [])
        if unresolved_upload is not None
        else []
    )

    never_dispatched = dispatch_status in _NEVER_DISPATCHED
    remote_terminal = remote_status in _TERMINAL_REMOTE

    # 免旗標 retract 的理由。順序即優先序,第一個成立的就是稽核要記的那個。
    if is_output:
        basis = "output_owner"          # 正常的 QA 拒收,本來就不需要旗標
    elif not is_active:
        basis = None                    # 歷史 attempt:誰都動不了它,旗標也無效
    elif output_attempt_id is not None:
        basis = "output_owner"          # active/output 分岔,投影屬於現任 output
    elif never_dispatched or remote_terminal:
        basis = "settled"               # manifest 自己就知道結果
    elif has_hard_output_evidence(episode):
        basis = "legacy_evidence"
    else:
        basis = None                    # 真的不知道有沒有東西在跑 —— 要外部知識

    # **只有從沒送出去的才重送得了。** 遠端已終態(`failed`/`removed`)雖然同樣
    # 「結果已定」,重送卻是走 supersede 建新 attempt,不是沿用這顆 ——
    # `_is_resendable_same_request` 也只收 `_NEVER_DISPATCHED`。
    can_resend = never_dispatched and not is_output
    can_resume = bool(remote.get("artifact_id")) and not is_output
    can_reconcile = (
        dispatch_status in _RECONCILABLE_DISPATCH_STATES
        and not reconciliation_window_closed
    )
    regeneration_entry = _regeneration_entry_point(attempt)
    preserves_existing_output = (
        post_retract
        and episode.get("output_attempt_id") not in (None, attempt_id)
    )
    pending_source_cleanup = False
    source_cleanup_unresolved = False
    cleanup_obligations: list[dict] = []
    if post_retract:
        obligations = _cleanup_obligations(
            episode, fallback_notebook=episode.get("notebook_id")
        )
        pending_source_ids = {row["source_id"] for row in obligations}
        retracted_source_ids = (
            (attempt.get("retraction") or {}).get("stale_source_ids") or []
        )
        # **retract 可以作廢一顆 upload 還沒落盤的 attempt(abandon_in_flight),但不能
        # 忘掉它可能留下的孤兒。** 身分不確定的清理義務記在 tombstone 上,由生成前的
        # `_assert_source_cleanup_done` 用候選窗對帳結案——所以這裡不是「還有東西要刪」
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
            source_id
            for source_id in retracted_source_ids
            if source_id in pending_source_ids
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
            replacement_caps = _attempt_capabilities(
                episode, replacement_attempt, replacement_attempt_id
            )

    # **`safe_next_action` 的優先序必須跟 `_attempt_next_step()` 的分支順序逐字
    # 對齊**(見上方 docstring)。
    if post_retract:
        if pending_source_cleanup:
            safe_next_action = ACTION_SOURCE_DELETE
        elif source_cleanup_unresolved:
            # **不准假裝重生現在就做得到**:候選窗還沒對帳完,`regeneration_entry` 會被
            # `_assert_source_cleanup_done` 擋成 ValueError。沒有可執行的單一工具,
            # 要等的事情由 `cleanup_state` 與 `_attempt_next_step()` 講清楚。
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
                ACTION_RESUME
                if can_resume
                else ACTION_RECONCILE if can_reconcile else None
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
        # 留給 `_attempt_next_step()` 判斷要不要講窗宣稱——`None` 時不准講。
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
        # output）。內部欄位，只給 `_attempt_next_step()` 遞迴用，不對外洩漏——外層
        # 呼叫端只讀 `safe_next_action` 與 `_attempt_next_step()` 的文字。
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


def _retract_hint(caps: dict) -> str:
    """要不要在 `podcast_attempt_retract` 前先查雲端,單一事實來源。

    `_attempt_next_step` 的「遠端可能還有東西」分支與 `podcast_episode_reconcile`
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


def _attempt_next_step(caps: dict) -> str:
    """把 `_attempt_capabilities` 的結論翻成一句**可執行**的話。

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
        # 沒事要做,但生成 gate 正拿著同一批 id 擋著。
        # **身分不明時不准印出假的可執行呼叫。** legacy 純字串義務 + episode 沒有
        # notebook_id 時算出來的是 `None`,印成 `source_delete(notebook_id=None, ...)`
        # 照抄會失敗 —— 那又是一句「在它自己產生的狀態下不可執行」的指引。這種只講清楚
        # 缺什麼(生成前的 gate 會用同一個理由 fail-closed,兩邊說法一致)。
        cleanup = (
            "先把這幾筆 source_delete 掉:"
            + "、".join(
                (
                    f"source_delete(notebook_id={row['notebook_id']!r}, "
                    f"source_id={row['source_id']!r})"
                    if row["notebook_id"] is not None
                    else (
                        f"source_id={row['source_id']!r}(這一筆沒有記到 notebook 身分,"
                        "補上 episode／manifest 的 notebook_id 後重呼本工具才算得出來)"
                    )
                )
                for row in caps["source_cleanup_obligations"]
            )
            + "。"
            if caps["pending_source_cleanup"]
            else ""
        )
        if caps["source_cleanup_unresolved"]:
            # **這個分支要排在重生之前**:義務沒結案時 `regeneration_entry` 會被
            # `_assert_source_cleanup_done` 擋成 ValueError,教人重生就是教一條當下
            # 走不通的路。`safe_next_action` 在這一格是 `None`,兩邊一致。
            return (
                cleanup
                + "這顆的回錄 source 已經送出、但 source_id 沒落盤(upload 停在 "
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
                + _attempt_next_step(replacement_caps)
            )
        return (
            cleanup
            + f"用 {caps['regeneration_entry']} 重生。"
            + caps["regeneration_hint"]
        )
    if not caps["is_active"] and not caps["is_output"]:
        return "它已經被取代,是歷史紀錄 —— 要動的是現在的 active／output attempt。"
    if caps["feedback_upload_unresolved"]:
        # 順序與 `safe_next_action` 逐字對齊(歷史紀錄先判)。這裡**不擋 retract**,
        # 只是把「先續完」排在前面:遠端可能已經多出一筆 media,續完是唯一能把它的
        # 身分認回來的路;真要作廢就照 `_retract_hint()` 走旗標,義務會留在 tombstone。
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
            f"{caps['feedback_upload_status']!r}):" + recovery + _retract_hint(caps)
        )
    if caps["is_output"]:
        return (
            "它是這一集的正式輸出:要作廢就直接 podcast_attempt_retract"
            "(**不需要** abandon_in_flight),照回傳的 stale_source_ids 逐一 source_delete,"
            f"再用 {caps['regeneration_entry']} 重生。" + caps["regeneration_hint"]
        )
    if caps["authorization_basis"] == "output_owner":
        # P1(見上面 `_attempt_capabilities` 的 safe_next_action 分支,同一組笛卡爾
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
    retract_hint = _retract_hint(caps)
    # F2 修復:這裡的分支順序必須跟 `_attempt_capabilities()` 的 `safe_next_action`
    # 優先序逐字對齊(見上方 docstring 的紅線)——`candidate_selection_required and
    # can_reconcile` 在那邊排在 `can_resume` 之前，這裡曾經反過來，於是同一顆 caps
    # 的 `safe_next_action` 教 adopt、`next_step` 的散文卻教 resume，兩個欄位對同一個
    # 狀態指向不同工具。
    if caps["candidate_selection_required"] and caps["can_reconcile"]:
        return (
            "manifest 無法判定候選 artifact 的歸屬:確認屬於這次 dispatch 就用 "
            f"{ACTION_ADOPT} 明確綁定;確認不屬於這次就不要 adopt。"
            + retract_hint
            + "作廢後再照 retract 回傳的入口重生。"
        )
    if caps["can_resume"]:
        return "遠端有 artifact:先 podcast_episode_resume 續完 finalize。" + retract_hint
    if caps["can_reconcile"]:
        if caps["reconciliation_window_closed"] is False:
            # 明確算過、窗還沒關——才有資格講「還有機會撈到」這句時間性宣稱。
            # **講的是關閉判斷窗,不是候選窗**(P1 修復,見上面 docstring):兩者是
            # 刻意拆開的不同窗,這句話依據的是前者。
            return (
                "受理結果不明:先 podcast_episode_reconcile 對帳(它可能已經在遠端跑完;"
                "這次對帳依據的關閉判斷窗還沒關,過幾分鐘後重呼有機會撈到,現在重呼"
                "未必是一模一樣的空結果)。"
                + retract_hint
            )
        # `None`:呼叫端沒算過窗(建立衝突／resume 停點／frozen 重呼卡住這四個
        # 呼叫點都是),不能宣稱「還沒關」——回到 v0.9.9 原文,一個字都不提窗。
        return (
            "受理結果不明:先 podcast_episode_reconcile 對帳(它可能已經在遠端跑完)。"
            + retract_hint
        )
    if caps["dispatch_status"] in _RECONCILABLE_DISPATCH_STATES:
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
            "就沒有用」。"
            + retract_hint
        )
    return retract_hint


def _artifact_created_at_utc(value: object) -> datetime | None:
    """Normalize the SDK's local-naive artifact timestamp to aware UTC."""
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc)


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
        # 全文與雜湊並存:雜湊只能證明「不一樣」,證明不了「送了什麼」。實測一季五集的
        # 送出 brief 與磁碟 brief.md 全數 sha 不符(貼上剝檔尾換行+貼上瞬間手改),其中
        # 一集手改未記錄,事後永遠重建不出實際生成輸入——manifest 是唯一進版控的正本。
        "brief": brief,
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
                    # **指引按 attempt 的實際狀態產生。** 原本無條件教「reconcile or
                    # resume,不然就用 identical arguments 重送」—— 對 `not_accepted`
                    # 那三條全是死的:reconcile 明說該狀態不可對帳、resume 要
                    # artifact_id 而它是 null、而「逐字相同」在 brief 產生器改過之後
                    # 重現不了(真實事故:brief 經手動轉錄,三面堵死)。v0.9.3 開的
                    # 新出口(免旗標 retract)沒有寫進這句,等於開了門沒掛路標。
                    way_out = _attempt_next_step(
                        _attempt_capabilities(episode, prior, active_attempt_id)
                    )
                    raise ValueError(
                        f"episode {episode_n} already has durable active attempt "
                        f"{active_attempt_id!r} (dispatch="
                        f"{prior['dispatch'].get('status')!r}); {way_out}"
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
                # 這一支曾經無條件教「resume it」—— 但那顆 active 可能停在
                # `not_accepted`(沒有 artifact 可續),照做走不通。與新建分支共用
                # 同一顆指引產生器,「補一半」就不可能發生(v0.9.5 只修了新建那支)。
                _, current_active = _attempt_record(
                    manifest, episode_n, current_active_id
                )
                raise ValueError(
                    f"episode {episode_n} has another active attempt "
                    f"{current_active_id!r}; "
                    + _attempt_next_step(
                        _attempt_capabilities(episode, current_active, current_active_id)
                    )
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
        # T2(wp-a2,P1 的另一半):flat v1 legacy 集(episode 級 artifact_id/
        # mp3_path,沒有 attempts、也沒有 output_attempt_id 可查)resume 到**不同**
        # artifact 現在兩層守門:這裡 fail-fast(打錯 id 連 rename/download/回錄
        # 上傳三個遠端副作用都不發生),`_promote_attempt_output` 仍保留同一道檢查
        # 當 defense-in-depth(万一有第三條建立路徑繞過這裡)。**同一顆合法 artifact
        # 永遠放行**——那是唯一能幫 legacy 集建出可 retract attempt 的路(retract
        # amendment (2),`legacy_audio_missing` 停點靠它)。要換掉現有輸出:先
        # resume(合法 artifact)讓它變成 attempt,再 `podcast_attempt_retract`,
        # 才能重生或 resume 別的 artifact——不准跳過這一步直接 resume(別的 artifact)。
        legacy_artifact_id = episode.get("artifact_id") or episode.get("task_id")
        if (
            legacy_artifact_id
            and has_hard_output_evidence(episode)
            and legacy_artifact_id != artifact_id
        ):
            raise ValueError(
                f"episode {episode_n} has legacy output evidence bound to artifact "
                f"{legacy_artifact_id!r}; to bring it into the auditable attempt flow, "
                f"call podcast_episode_resume(artifact_id={legacy_artifact_id!r}) — "
                "resuming the SAME artifact is always allowed (the "
                "legacy_audio_missing stop depends on it). To replace the output: "
                f"resume(artifact_id={legacy_artifact_id!r}) to turn it into an "
                "attempt, then podcast_attempt_retract it, then regenerate — do not "
                "resume a different artifact directly"
            )
        active_attempt_id = episode.get("active_attempt_id")
        if active_attempt_id:
            # 同 `_create_audio_attempt` 那句的修正:對 `prepared`/`not_accepted`
            # (以及遠端已終態的)attempt,「reconcile or resume」兩條都走不了 ——
            # 沒有 artifact 可對帳、也沒有 artifact 可續。
            _, active = _attempt_record(manifest, episode_n, active_attempt_id)
            hint = _attempt_next_step(
                _attempt_capabilities(episode, active, active_attempt_id)
            )
            raise ValueError(
                f"episode {episode_n} has active attempt {active_attempt_id!r} "
                f"(dispatch={active.get('dispatch', {}).get('status')!r}); {hint}"
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
        # legacy_artifact_id 已經在上面算過(判斷「是否同一顆合法 artifact」要用),
        # 這裡不再重算第二份。
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
    *,
    wait_timeout: float,
) -> bool:
    """原子保存 baseline 並取得 prepared attempt 的 dispatch ownership。

    `account` 是「這次由哪個帳號送出」(ADR-0010 的 pool)。單帳號時它只是一條事實,
    多帳號 failover 時它會被 `_record_dispatch_failover` 更新成實際成功的那個 ——
    沒有它,「EP35 是誰生的」事後答不出來。

    `wait_timeout` 是**這次 dispatch 原始承諾要等多久**(呼叫端傳給
    `podcast_episode`/`podcast_series` 的那個秒數,不是之後 reconcile 時想等多久)。
    存原始秒數而不是算好的 window_end——window_end 還混進了時鐘容錯常數
    (`_RECONCILIATION_CLOCK_SKEW`),那個常數以後可能改,秒數不會變(P1 修復,見
    `_RECONCILIATION_MIN_WINDOW` 的常數註解)。**兩個呼叫端都要傳**
    (`_run_episode` 與 `podcast_series` 的 inline 重送分支)——只補一條正是
    docs/gotchas-attempt.md 紅線①點名的病灶。
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
                "wait_timeout": float(wait_timeout),
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
        # 兩種拒絕形狀的判讀共用 `_failover.describe_refusal`(附件家族的 recorder 也用
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
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    store.update(mutate)


def _attempt_failover_recorder(
    store: ManifestStore | None, episode_n: int, attempt_id: str
):
    """音檔家族的稽核面:把「A 拒絕 → 換 B 重送」寫進**這個 attempt** 的 `errors[]`。

    回 `None` 代表「這個呼叫端沒有稽核面」——`_failover` 收到 `None` 就一律不換帳號:
    沒有地方寫紀錄還靜默換帳號,違反 ADR-0010 自己立的「對 client 透明可以,對稽核
    紀錄不行」(standalone 沒有 manifest 的路徑就是這一種)。

    抽成一支是因為 `_rotate_for_quota` 與 `_dispatch_audio_with_failover` 都要綁同一份
    ——各自 inline 一個 closure 等於這條「稽核往哪寫」的決定有兩個版本。
    """
    if store is None:
        return None

    def record(reason: object, from_account: str | None, to_account: str) -> None:
        _record_dispatch_failover(
            store, episode_n, attempt_id, reason, from_account, to_account
        )

    return record


def _rotate_for_quota(
    store: ManifestStore | None,
    episode_n: int,
    attempt_id: str,
    reason: object,
    from_account: str | None,
    tried: set[str],
) -> tuple[str | None, object] | None:
    """音檔家族的 rotate = 共用輪替 + 這個 attempt 的稽核面。

    **輪替本身的紀律(`tried` 要擋在稽核寫入之前、`skip=` 要進掃描、`refused=` 要用
    呼叫端實際持有的 label)住在 `_failover.rotate_for_quota`,不在這裡** —— v0.9.16
    附件家族也接上 failover 時抽出去的:那三條每一條都是一次真實事故,各寫一份等於
    下一次修正只有一處被改到。這支只剩「稽核往哪寫」這一件家族專屬的事。
    """
    return rotate_for_quota(
        _attempt_failover_recorder(store, episode_n, attempt_id),
        reason,
        from_account,
        tried,
    )


async def _dispatch_audio_with_failover(
    store: ManifestStore | None,
    episode_n: int,
    attempt_id: str,
    generate,
    *,
    account: str | None,
    client: object,
    notebook_id: str | None = None,
) -> tuple[str, str | None, object]:
    """送出音檔生成並回傳 artifact_id;配額拒絕就換帳號**原地重送同一個 attempt**。

    **迴圈本身已抽到 `_failover.dispatch_with_failover`** —— v0.9.16 附件家族(簡報 /
    講義 / 改版單頁)也接上 failover 時抽的。那個檔案的模組 docstring 列著五條紅線
    (集合不准長大、有 id + failed 不准 rotate、權限不 rotate、終止性不靠時鐘、
    `tried` 擋在稽核寫入之前),每一條都是一次真實事故 —— **改那五條要去那裡改,
    不要在這裡加分支**,各寫一份正是本 repo 反覆出事的「補一半」。

    這支只剩音檔家族專屬的三件事:①稽核與終態標記寫進**這個 attempt**
    (`_rotate_for_quota` / `_mark_not_accepted` / `_mark_acceptance_unknown`);
    ②`store is None`(standalone)時退化成不換帳號;③維持既有的 positional 呼叫形狀。

    **重送是同一個 attempt** —— 同一個 `attempt_id`、不 supersede、不新建 manifest 紀錄。
    這刻意**不是**「用另一個帳號重新生成這一集」。

    **兩個呼叫端共用它**:`_run_episode`(全新一集)與 `podcast_series` 的重送/supersede
    分支(v0.8.0 只補了前者,於是 pool 對「重試」這條最需要它的路完全無效 —— 驗收 F-4)。

    **續跑指引由呼叫端各自加在例外訊息上**,不在這裡:`podcast_episode` 的續跑動作是
    原樣重呼自己,`podcast_series` 是重呼整季 —— 而 `_mark_not_accepted` 會把
    `str(exc)` 寫進 `remote.error` 與 `errors[]`,寫死一種指引等於讓**錯的**工具名
    落進 manifest,事後查錯的人會照著跑錯的東西。

    **`account` / `client` 由呼叫端用 `runtime.snapshot()` 一次取好傳進來**,函式內
    不再自己讀全域;**回 `(artifact_id, account, client)`,呼叫端必須接住後兩個往下用**
    (finalize 的身分必須是「這次是誰送的」而不是「此刻游標指到誰」)。兩條的完整推導在
    `_failover.dispatch_with_failover` 的 docstring。
    """
    return await dispatch_with_failover(
        generate,
        account=account,
        client=client,
        notebook_id=notebook_id,
        # `store is None` 時這裡是 `None`,共用迴圈就一律不換帳號(沒地方寫稽核紀錄,
        # ADR-0010)。`_rotate_for_quota` 綁的是同一支,不再各寫一份。
        record_failover=_attempt_failover_recorder(store, episode_n, attempt_id),
        # `account` 用不到 —— attempt 的 `dispatch.account` 已經記著這次是誰送的,
        # 而那個欄位在 failover 換帳號時就被 `_record_dispatch_failover` 更新過了。
        on_clean_refusal=(
            None
            if store is None
            else lambda reason, account: _mark_not_accepted(
                store, episode_n, attempt_id, reason
            )
        ),
        on_acceptance_unknown=(
            None
            if store is None
            else lambda reason, account: _mark_acceptance_unknown(
                store, episode_n, attempt_id, reason
            )
        ),
    )


def _mark_acceptance_unknown(
    store: ManifestStore,
    episode_n: int,
    attempt_id: str,
    error: object,
) -> None:
    """標成「受理不明」並留診斷。

    **`error` 不一定是例外。** 「有 `task_id` 卻 `is_failed`」那條分支傳進來的是
    **status 物件**(`_failover.dispatch_with_failover` 的 `on_acceptance_unknown(status)`),
    而舊版本用 `type(error).__name__` / `str(error)` 直接處理 —— 實測落盤的是
    `type: "GenerationStatus"` / `message: "<... object at 0x7f...>"`,**真正的原因
    (`status.error`)整條蒸發**,卡在 `acceptance_unknown` 的人打開 manifest 只看到一個
    記憶體地址。簽名原本宣告 `BaseException` 也是錯的(從來就有非例外的呼叫端)。
    改用 `_failover.describe_refusal`,與 `_record_dispatch_failover` / 附件家族同源。
    """

    def mutate(manifest: dict) -> None:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        if attempt["dispatch"]["status"] != "dispatching":
            return
        error_type, message = describe_refusal(error)
        attempt["dispatch"]["status"] = "acceptance_unknown"
        attempt["errors"].append(
            {
                "phase": "dispatch",
                "type": error_type,
                "message": message,
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


def _series_will_redispatch(attempt: dict) -> bool:
    """series 走到這顆 attempt 時,會不會產生**新的一次 dispatch**。

    三種狀態會(`prepared` 直接送、`not_accepted` 先 re-arm 再送、`failed`/`removed`
    建 superseding attempt 再送),其餘的 series 只是 finalize／對帳／跳過。接手守門
    與認證停點的交棒判準都要用這一個,不要各自列狀態字面值 —— 舊版接手守門只列了前
    兩種,`accepted`+`failed` 那格因此整條漏掉(見 `_series_handoff_caps`)。
    """
    dispatch_status = (attempt.get("dispatch") or {}).get("status")
    remote_status = (attempt.get("remote") or {}).get("status")
    return dispatch_status in _NEVER_DISPATCHED or remote_status in _TERMINAL_REMOTE


def _later_episode_has_output(snapshot: dict, episode_n: int, notebook_id: str) -> bool:
    """後集是否已有正式 output，或同本 notebook 仍可能存在其回錄 source。

    T3:retract 中段集之後重生時,`podcast_series` 若對這一集重新 dispatch(全新
    一集／not_accepted re-arm／failed-removed supersede,見 `_series_will_redispatch`
    的三種狀態),不指名來源就是讀整本筆記本——後面集數已經產出的回錄音檔會洩進
    這一集。**這個判斷 `_attempt_capabilities` 給不出來**:它只拿單一
    episode/attempt,看不到 sibling episode 的證據,跨集的事實只能在 series 這一層
    的迴圈頂端算。`has_hard_output_evidence`(不是更寬鬆的
    `has_durable_output_evidence`)才對——後者連 `retracted_attempt_ids` 都算數,
    retract 恰恰不會清掉這個欄位,用它來判斷「後面集數有沒有 output」會把「後面那集
    自己也曾經被 retract 過」誤判成「有 output」。

    回錄 source 比正式 output 先落盤；未 promote 的 active attempt 也要算，否則
    EP03 上傳回錄後中斷，EP02 重生會靜默讀到它。未確定結果的上傳同樣保守攔下。
    """
    return any(
        isinstance(row.get("episode"), int)
        and row["episode"] > episode_n
        and (
            row.get("output_attempt_id") is not None
            or has_hard_output_evidence(row)
            or any(
                attempt.get("attempt_id") == row.get("active_attempt_id")
                and attempt.get("notebook_id") == notebook_id
                and not attempt.get("retraction")
                and (
                    ((attempt.get("finalize") or {}).get("feedback_source_upload") or {}).get("source_id")
                    or unresolved_upload_descriptor(attempt) is not None
                )
                for attempt in row.get("attempts", [])
            )
        )
        for row in snapshot.get("episodes", [])
    )


# 形狀是 series 生得出來的、只有這次呼叫的參數對不上。**這不是 attempt 狀態問題**,
# 所以不從 `_attempt_capabilities()` 取(它在這一格會算出 `podcast_series`,貼上去等於
# 叫人拿同一組參數撞回同一道牆)。守門的例外訊息與認證停點的 `next_step` 共用這一份,
# 兩處講的是同一件事,不要各寫一句。
_SERIES_ARGUMENT_DRIFT_HINT = (
    "這一集的 attempt 是 podcast_series 建的,但**這次呼叫的 language／audio_format／"
    "audio_length 與它不同**。用原本那組參數重呼 podcast_series 就會續下去;真的要換"
    "設定就先 podcast_attempt_retract 作廢它再重生 —— 照這次的參數重呼會被 settings "
    "守門擋下來。"
)


def _series_owns_attempt(attempt: dict, series_settings: dict) -> bool:
    """這顆 attempt 是不是 `podcast_series` 自己建的 —— 兩個條件缺一不可。

    **只比 settings 等值會漏掉 frozen bundle**(Codex 獨立複審實跑抓到):一顆不指名
    `source_ids`、language/format/length 全用預設的 frozen attempt,它的 settings 與整季
    **逐字相同**,但生成輸入是 bundle,series 生不出來 —— `_regeneration_entry_point()`
    因此優先看 `input_bundle`。判成自己的就會 supersede 掉、改讀整本筆記本,與
    `source_ids` 那條同型的內容錯置,而且更難發現:兩邊的 settings 看起來一模一樣。
    """
    return (
        attempt.get("settings") == series_settings
        and _regeneration_entry_point(attempt) == ACTION_SERIES
    )


def _series_handoff_caps(
    episode: dict,
    attempt: dict,
    attempt_id: str,
) -> dict | None:
    """series 接不住這顆 attempt 時,回**它自己的 caps**(誰接得住由那裡說);接得住回 `None`。

    回 `None` 有三種,每一種都不是「交棒」:

    1. series 根本不會在它上面重新 dispatch(`_series_will_redispatch` 為假)——
       只等 finalize 的一集在這裡。**這格必須排第一**:一集已經產出正式輸出時它的
       output attempt 照樣帶著 `source_ids`,只看「settings 認不認得」會回
       `podcast_attempt_retract`,叫人作廢一集已經做好的東西(突變驗證確認)。
    2. 它就是這一集的 output —— series 走 output 分支,不會重新 dispatch 它。
       第 1 格擋掉的是「已完成」的正常形狀,這一格補的是「output 但遠端終態」那種
       畸形 manifest(第 1 格對它為真,但 series 一樣不會重生它)。
    3. 形狀是 series 生得出來的(`_regeneration_entry_point` 回 `podcast_series`)——
       settings 逐字相同就是它自己的;只有 language／format／length 對不上則是
       **呼叫端這次帶錯參數**,不是 attempt 歸屬問題。後者 caps 會算出 `podcast_series`
       (can_resend → regeneration_entry),拿它當指引等於叫人原地撞回同一道牆,
       正是紅線要擋的自我迴圈。

    兩個呼叫點傳進來的 `attempt_id` 都是 `active_attempt_id`(守門)或
    `active or output`(認證停點),所以「既非 active 也非 output 的歷史紀錄／tombstone」
    到不了這裡;真的到了的話 caps 會回 `safe_next_action=None`,呼叫端已各自處理。
    """
    if not _series_will_redispatch(attempt):
        return None
    if attempt_id == episode.get("output_attempt_id"):
        return None
    if _regeneration_entry_point(attempt) == ACTION_SERIES:
        return None
    return _attempt_capabilities(episode, attempt, attempt_id)


def _assert_series_owns_attempt(
    episode: dict,
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
    troubleshooting 對 not_accepted 的指示恰好是「重呼 podcast_series」。

    **指引由 `_attempt_capabilities()` 產生,不再手寫**(docs/gotchas-attempt.md 紅線)。
    舊版無條件說「原樣重呼 podcast_episode 就會沿用同一顆重送」,那句話只對
    `_NEVER_DISPATCHED` 成立;`accepted`+`failed` 的 `can_resend` 是 False,照做會撞
    `already has durable active attempt` —— 又一次「指引在它自己產生的狀態下不可執行」。
    那一格 caps 給的是「先 retract,再用 podcast_episode 帶回原本的 source_ids 重生」。
    """
    if _series_owns_attempt(attempt, series_settings):
        return
    caps = _series_handoff_caps(episode, attempt, attempt_id)
    owner_hint = (
        " " + _attempt_next_step(caps)
        if caps is not None
        else " " + _SERIES_ARGUMENT_DRIFT_HINT
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
    # P2(Codex 實跑驗證):`wait_timeout` 是**這次 dispatch** 的持久化承諾(見
    # `_validate_wait_timeout` docstring),reset 回 `prepared` 就代表這次 dispatch
    # 已經不存在了——留著上一輪的值會被下一次對帳誤當成「這次」的窗判準。實跑重現:
    # `wait_timeout=7200` 被拒 → reset 成 prepared → 用 `wait_timeout=60` 重試 →
    # manifest 卻是 `status=prepared`／`dispatched_at=None`／`wait_timeout=7200.0`
    # ——沒有當前 dispatch,卻留著上一輪的窗。**這裡是所有 dispatch-specific 欄位
    # 該清的地方,新增持久化欄位時記得跟上**——這正是「新增欄位卻沒跟著清理義務走」
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
) -> list[str]:
    def mutate(manifest: dict) -> list[str]:
        _, attempt = _attempt_record(manifest, episode_n, attempt_id)
        remote = attempt.get("remote")
        if not isinstance(remote, dict) or remote.get("artifact_id") is not None:
            raise ValueError("attempt already has a remote artifact mapping")
        if artifact_id in _claimed_artifact_ids(manifest, attempt_id):
            raise ValueError(f"artifact {artifact_id!r} was claimed during reconciliation")
        # F4(盲審 P3):`notebook_id` 的非空字串驗證已經在唯一呼叫端
        # `podcast_episode_reconcile` 進來前由 `_reconciliation_subject` 做過一次
        # ——逐位元組相同的 guard，而 `notebook_id` 是建立 attempt 時寫死、之後不會
        # 被任何 writer 改動的欄位（唯一寫入點在 `_create_audio_attempt` 一類的建立
        # 分支），這裡重驗建構上不可達。不重複驗證，直接沿用。
        notebook_id = attempt.get("notebook_id")
        blocking_attempt_ids = _unresolved_attempt_ids(
            manifest, notebook_id, excluding_attempt_id=attempt_id
        )
        if blocking_attempt_ids:
            # RPC 前的 snapshot 只是廉價早退；歸屬會在 await 期間變動，
            # 所以真正的綁定許可必須與 claimed 重驗共用這次原子 update。
            return blocking_attempt_ids
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
        return []

    _, blocking_attempt_ids = store.update(mutate)
    return blocking_attempt_ids


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


class PromotionRefusedError(ValueError):
    """T3(wp-a2):`_promote_attempt_output` 的三道 ValueError 守門(已 retract／
    owned-by-another／legacy 不同 artifact)收斂成這個具名例外,讓
    `podcast_series` 的兩個 promote 呼叫點能接住它翻成結構化 `partial(...)`
    (新契約詞 `observed_state="promotion_refused"`)——這三種情形都可能在
    series 正在跑整季期間發生(另一個 process 剛 retract 掉這顆、或 legacy 集
    撞到不同 artifact),裸拋會讓已完成的 run_results 一起丟掉,而且沒有
    `safe_next_action`/`start=` 逃生口。`podcast_episode`/`podcast_episode_resume`
    的呼叫點維持原樣 raise(它們本來就是 raise 型停點,訊息不變——是 ValueError
    子類,既有 `pytest.raises(ValueError)` 不必逐一改)。"""

    def __init__(self, message: str, *, episode_n: int, attempt_id: str) -> None:
        super().__init__(message)
        self.episode_n = episode_n
        self.attempt_id = attempt_id


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
            raise PromotionRefusedError(
                f"attempt {attempt_id!r} was retracted; it cannot be promoted to "
                f"episode {episode_n}'s output",
                episode_n=episode_n,
                attempt_id=attempt_id,
            )
        if episode.get("output_attempt_id") not in (None, attempt_id):
            raise PromotionRefusedError(
                f"episode {episode_n} output is owned by "
                f"{episode['output_attempt_id']!r}; refusing to promote {attempt_id!r}",
                episode_n=episode_n,
                attempt_id=attempt_id,
            )
        # T2(wp-a2)更新:這道現在是**第二層 defense-in-depth**,不是唯一擋點——
        # `_ensure_resume_attempt` 的新建分支已經對同一個條件 fail-fast(打錯
        # artifact_id 連 rename/download/回錄上傳三個遠端副作用都不會發生)。這裡
        # 留著是防第三條建立路徑繞過那道守門(例如 `_ensure_resume_attempt` 的
        # claimed 分支重用既有 attempt 時),promotion 仍是 episode 級投影欄位
        # 唯一的寫入點,是最後一道防線。**resume 到同一顆合法 artifact 必須繼續
        # 放行**(`legacy_artifact_id ==
        # output["artifact_id"]` 時不擋)——那是把這種 legacy episode 帶進可審計
        # 流程的唯一入口(T5 的 legacy_audio_missing 停點也靠它)。**只在
        # `legacy_artifact_id` 真的記著一顆具體 artifact 時才擋**:硬證據也可能只靠
        # `mp3_path`/`published_at`(手工整理的節目、從沒記過 artifact_id)——這種
        # episode 沒有「原本綁的是哪一顆」可言,promote 任何 artifact 都是**第一次**
        # 建立那個連結,不是換掉已知的一顆(唯讀迴歸測試
        # `test_legacy_partial_output_isolated_before_explicit_resume_promotion`
        # 鎖著這個形狀仍要放行)。
        legacy_artifact_id = episode.get("artifact_id") or episode.get("task_id")
        if (
            episode.get("output_attempt_id") is None
            and legacy_artifact_id
            and has_hard_output_evidence(episode)
            and legacy_artifact_id != output["artifact_id"]
        ):
            caps = _attempt_capabilities(episode, attempt, attempt_id)
            raise PromotionRefusedError(
                f"episode {episode_n} already has legacy output evidence bound to "
                f"artifact {legacy_artifact_id!r}; promoting attempt {attempt_id!r} "
                f"(artifact {output['artifact_id']!r}) would silently replace it "
                "without an audit trail. " + _retract_hint(caps) + " retract pops "
                "the legacy projection along with the stale feedback source, and "
                "only then may the new artifact become the output.",
                episode_n=episode_n,
                attempt_id=attempt_id,
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


def _validate_wait_timeout(wait_timeout: float) -> None:
    """`wait_timeout` 從「這次呼叫想等多久」升級成**持久化的安全參數**
    (`_claim_prepared_dispatch` 把它存進 `dispatch["wait_timeout"]`)之後,它就是
    之後**每一次** reconcile 的候選篩選窗與關閉判斷窗判準——一個沒有信任邊界檢查的
    呼叫端輸入,不該直接變成長期有效的安全設定。四個入口(`podcast_episode`／
    `podcast_series`／`podcast_episode_reconcile`／`podcast_episode_resume`)都要
    同一句驗證,別各寫一份等著漏一個(T7,wp-a2:`podcast_episode_resume` 是這四個
    裡最後補上的一個,同樣的「只補一個」病灶在第四輪修復時就已經點名過)。

    `math.isfinite` 擋 `nan`/`inf`:光靠 `<= 0` 擋不住 `nan`(`nan <= 0` 恆為
    `False`,NaN 比較永遠不成立)。`nan` 存進 `dispatch["wait_timeout"]` 後,
    `timedelta(seconds=nan)` 會在往後**每一次**對帳時炸掉——那顆 attempt 永久對帳
    不了,只剩 retract 一條路。
    """
    if (
        not isinstance(wait_timeout, (int, float))
        or isinstance(wait_timeout, bool)
        or not math.isfinite(wait_timeout)
        or wait_timeout <= 0
    ):
        raise ValueError("wait_timeout must be a finite number greater than zero")


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
    client: object,
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


#: 對帳結果寫回時撞上並行寫入的重試上限。撞這麼多次代表有別的東西在狂寫 manifest,
#: 那時候硬拚不會比讓呼叫端重跑更好。
_CLEANUP_SETTLE_RETRIES = 5


def _settle_cleanup_state(
    store: ManifestStore,
    settle,
    *,
    expected_revision: object,
    discovered: dict,
    settled_attempt_ids: set,
    checked_absent_by_episode: dict,
    notebook_id: str,
) -> None:
    """把這一輪算出來的清理狀態寫回去,**永遠帶 revision CAS**。

    **為什麼清除那一側也要 CAS**(盲審 P1,靜默遺失義務):上一版只在「有新發現」時才 CAS,
    理由是「沒有新發現時 settle 只做對並行天生安全的事」—— 那個論證只看了 manifest 側的
    競態,漏了**兩個 process 對遠端的觀測不同**這件事:

        A、B 同讀 revision R
        A 的 sources.list 回零候選(orphan 還沒出現在清單裡)、窗已關
        B 的 sources.list 撈到 orphan
        A 無條件寫入 → 清掉 source_cleanup_unresolved,成為 R+1
        B ── 下一輪連這筆記錄都不會收集(旗標沒了)→ 不算候選、不寫任何東西、**成功返回**

    結果是 orphan 從 manifest 完全消失,而且全程零錯誤、零告警,下一次生成直接放行。
    清旗標是破壞性方向,卻是唯一沒有保護的那條路。

    **只加 CAS 還不夠**:誰先 commit 誰贏,零候選那方先贏的話,正候選那方重跑也看不到旗標。
    所以衝突時的處置是不對稱的 ——

    - **保留「加義務」**(fail-closed 方向):重讀最新 manifest、**重驗 ownership**(await
      期間別集可能剛把某個 source 認領成它的合法 continuity source,那筆就不再是孤兒),
      再條件寫入。已觀測到的證據不因為衝突而消失。
    - **丟掉「結案 unresolved 旗標」**(`settled_attempt_ids`):那是拿可能過期的零候選觀測
      算出來的,而清錯就是永久遺失。下一輪 gate 會用新的 manifest 重算,代價只是多跑一次。
    - **保留 `checked_absent_by_episode`**:它逐筆比對「這次真的去 notebook 查過、確認不在」
      的那幾個 id,別的 writer 併發追加的是**別的** id,不會被它碰到 —— 這一個清除方向對
      並行是真的安全(「await 期間又一次 retract 追加新義務不得被吞掉」有既有測試鎖著)。
    """
    try:
        store.update(settle, expected_revision=expected_revision)
        return
    except ManifestConflictError:
        pass

    settled_attempt_ids.clear()
    if not discovered and not checked_absent_by_episode:
        return

    for _ in range(_CLEANUP_SETTLE_RETRIES):
        fresh = store.read()
        claimed = claimed_source_ids(fresh)
        for ep_n in list(discovered):
            still_orphan = [
                source_id
                for source_id in discovered[ep_n]
                if source_id not in claimed
            ]
            if still_orphan:
                discovered[ep_n] = still_orphan
            else:
                del discovered[ep_n]
        if not discovered and not checked_absent_by_episode:
            return
        try:
            store.update(settle, expected_revision=fresh.get("revision"))
            return
        except ManifestConflictError:
            continue
    raise ValueError(
        f"notebook {notebook_id!r} 的回錄 source 清理義務對帳期間 manifest 一直被改動,"
        f"連續 {_CLEANUP_SETTLE_RETRIES} 次寫不進去。已經撈到的候選還沒落盤,"
        "**不要當成沒事**:等並行的工作停下來後重呼同一支工具重新對帳。"
    )


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
    的拒收逐字稿讀進 context。canonical 不同的集互不影響(不同 notebook 的義務不該
    互相卡住)。

    **兩種義務都在這裡結案**(F2):
      (1) ``pending_source_cleanup``——身分已確定的 id,驗「已經不在 notebook」。
      (2) tombstone 上的 ``source_cleanup_unresolved``——retract 作廢了一顆 upload 還沒
          落盤的 attempt,遠端可能多出一筆沒人記得的 media。用
          ``unresolved_upload_candidates()``(與 finalize 對帳同一份判準)去撈。

    **執行順序本身就是安全性質**(盲審 P1×3 + P2×2 之後重寫):

    ① **身分先於一切。** 清理義務綁的是「當初上傳的那本 notebook」,而 (2) 的權威來源是
       **attempt 自己的 `notebook_id`** —— `manifest_store._validate` 明文允許 tombstone
       保留建立時綁的舊 notebook(否則 retract 之後想換 notebook 重生就寫不進去),所以
       episode 當下的 notebook 只是 fallback,不是判準。身分沒對上就**不篩選、不持久化、
       不回傳任何候選**:拿呼叫端隨手傳的 notebook 去撈,撈到的是**別本筆記本裡碰巧同名
       同時間窗**的 source,而我們會把它寫進清理義務、再叫呼叫端 `source_delete` —— 那是
       對無關 notebook 下破壞性指令,比漏掉義務更糟。
    ② **一次 list,純計算,不中途拋。** 掃描過程只累積結果(candidates / settled /
       waiting / checkpoint 壞掉的),**不在迴圈裡 raise** —— 前面幾顆已經撈到的候選還沒
       落盤,一拋就永遠回不來(那些 id 從此沒人記得)。
    ③ **單次 revision-CAS mutation,而衝突時「加義務」不准跟著陪葬。**
       `_claimed_source_ids` 是在 `await` **之前**的 snapshot 上算的,await 期間另一個
       finalizer 可能剛好把某個 source claim 成它那一集的合法 continuity source —— 拿舊
       ownership 寫新 manifest 就是把合法來源排進待刪清單。所以寫入一律帶
       `expected_revision`。**但衝突不能一律放棄**:那會讓「零候選那方先 commit、正候選
       那方撞衝突」變成孤兒永久失憶(見 `_settle_cleanup_state`)。處置是不對稱的 ——
       重驗 ownership 後把候選寫進去,清旗標那一側則丟掉、留給下一輪重算。
    ④ **寫完才 raise;完全沒變更就不寫。** 「已確認不在」的清除、撈到的新義務、結案的
       unresolved 旗標,全部在同一次 mutation 裡結算 —— 否則已經刪掉的 id 會卡在
       `pending_source_cleanup` 裡(因為這一輪先在 waiting 那裡拋了),而
       `podcast_attempt_retract` 的冪等回傳會繼續說 `source_delete`,指向一個早就不存在的
       東西。反過來,blocked/waiting 這種什麼都沒改的路徑**一次 `store.update` 都不准發**:
       `ManifestStore` 的 revision 是無條件 +1,無效寫盤還會平白撞掉別人的 discovery CAS。

    `sources.list` / 認證失敗一律往上拋 —— 義務不會被誤清。"""
    snapshot = store.read()
    snapshot_revision = snapshot.get("revision")
    episodes = snapshot.get("episodes", [])
    manifest_notebook_id = snapshot.get("notebook_id")

    def canonical_notebook(row: dict) -> object:
        return row.get("notebook_id") or manifest_notebook_id

    def cleanup_notebook(row: dict, attempt: dict) -> object:
        """清理義務綁的那本 notebook:**attempt 自己的優先**(見 docstring ①)。"""
        return attempt.get("notebook_id") or canonical_notebook(row)

    # (episode_n, attempt_id, attempt, upload)。attempt 整顆帶著走:候選判準要讀它的
    # finalize checkpoint(baseline／expected_title／dispatched_at),而那份資料刻意
    # 只有一份、留在 attempt 上,沒有複製進 retraction。
    unresolved_here: list[tuple[object, str, dict, dict]] = []
    unresolved_elsewhere: list[tuple[object, str, object]] = []
    for row in episodes:
        for candidate_attempt in row.get("attempts", []):
            retraction = candidate_attempt.get("retraction") or {}
            if not retraction.get("source_cleanup_unresolved"):
                continue
            upload = unresolved_upload_descriptor(candidate_attempt)
            if upload is None:
                # source_id 後來落盤了(adopt/對帳補回身分):義務改由既有的
                # stale_source_ids／pending 路徑處理,這裡不再管它。
                continue
            owner = cleanup_notebook(row, candidate_attempt)
            record = (row.get("episode"), candidate_attempt.get("attempt_id"))
            if owner == notebook_id:
                unresolved_here.append((*record, candidate_attempt, upload))
            else:
                # **別本 notebook 的義務不擋這一本的生成**(canonical 不同的集互不影響),
                # 但身分不明(`None`)的擋 —— 那種列無從證明它不是這一本的。
                if owner is None:
                    unresolved_elsewhere.append((*record, owner))

    # **每筆義務用它自己的 notebook 身分分流**(盲審 P1):
    #   == 這一本 → 這次要驗;
    #   != 這一本 → 別本的問題,不參與(目標這一集自己的另外擋,見下);
    #   None(legacy 純字串且該集連 canonical 都沒有)→ 身分不明,fail-closed。
    pending_here: list[tuple[object, str]] = []
    pending_identity_unknown: list[tuple[object, str]] = []
    for row in episodes:
        for obligation in _cleanup_obligations(
            row, fallback_notebook=canonical_notebook(row)
        ):
            owner = obligation["notebook_id"]
            if owner == notebook_id:
                pending_here.append((row.get("episode"), obligation["source_id"]))
            elif owner is None:
                pending_identity_unknown.append(
                    (row.get("episode"), obligation["source_id"])
                )

    # 目標這一集自己的義務若屬於**別本** notebook,不該在這裡「順便放行」:那筆 source
    # 仍在原本那本污染它的 context,而這次生成不會去碰它。明確擋下來並說出是哪一本,
    # 比靜默跳過誠實(判準是義務自己的身分,不再是 episode 當下的 canonical)。
    target_elsewhere = sorted(
        {
            row["notebook_id"]
            for episode_row in episodes
            if episode_row.get("episode") == episode_n
            for row in _cleanup_obligations(
                episode_row, fallback_notebook=canonical_notebook(episode_row)
            )
            if row["notebook_id"] not in (None, notebook_id)
        },
        key=str,
    )
    if target_elsewhere:
        raise ValueError(
            f"episode {episode_n} 的回錄 source 清理義務屬於 notebook {target_elsewhere!r},"
            f"不是 {notebook_id!r};不能從別本筆記本結案(那幾筆仍在原本那本污染 context)。"
        )

    # **身分不明的義務在打 RPC 之前就擋掉**(docstring ①):不篩選、不持久化、
    # 不回傳任何候選 —— 這一條必須排在 `sources.list` 之前,否則「撈到的候選」已經被算出來
    # 並寫進義務了,擋在後面沒有意義。
    if unresolved_elsewhere or pending_identity_unknown:
        details = ", ".join(
            [f"episode {ep_n}: attempt {att!r}" for ep_n, att, _ in unresolved_elsewhere]
            + [f"episode {ep_n}: source {sid!r}" for ep_n, sid in pending_identity_unknown]
        )
        raise ValueError(
            f"這幾筆回錄 source 清理義務沒有可核對的 notebook 身分:{details}。"
            "拿別本筆記本查到「沒有」不能當成義務結案,而拿它查到的候選更不能當成要刪的東西。"
            # **不要教「去 manifest 補 notebook_id」** —— `series_manifest.json` 只由工具
            # 寫入(手寫的紀錄繞過 artifact claim 唯一性與 advisory lock),而且沒有任何
            # 工具設得了那個欄位:那句話會把呼叫端導進一條它做不到、也不准做的路。
            # 這種狀態只出現在 v0.9.11 之前留下的純字串義務 + 該集連 canonical notebook
            # 都沒有的 legacy manifest 上,出路是人工判斷後用 source_delete 收掉。
            "這是 v0.9.11 之前的 legacy 義務(當時沒有記身分)且該集沒有 canonical "
            "notebook_id 才會出現。**不要手改 manifest**(它只由工具寫入):用 "
            "source_list 在候選的 notebook 裡找到這幾筆,確認之後 source_delete 掉;"
            "刪乾淨後這道 gate 就會放行。"
        )

    if not pending_here and not unresolved_here:
        return

    sources = await client.sources.list(notebook_id)
    live = {getattr(source, "id", None) for source in sources}
    # T5(P3):這一份是「已確定身分的 pending 義務」違規,不受下面 CAS 修剪影響
    # (那道修剪只管 `discovered`——見下方 `_settle_cleanup_state` 呼叫後的重算)。
    pending_violations = [
        (ep_n, source_id) for ep_n, source_id in pending_here if source_id in live
    ]

    # 純計算:全部累積,迴圈裡一律不 raise(docstring ②)。
    now = datetime.now(timezone.utc)
    discovered: dict[object, list[str]] = {}
    waiting: list[tuple[object, str]] = []
    unreconcilable: list[str] = []
    settled_attempt_ids: set[str] = set()
    for ep_n, unresolved_attempt_id, unresolved_attempt, upload in unresolved_here:
        try:
            candidates = unresolved_upload_candidates(
                snapshot, unresolved_attempt, unresolved_attempt_id, sources
            )
            window_closed = upload_dispatch_window_closed(upload, now=now)
        except ValueError as exc:
            # checkpoint 缺 expected_title／dispatched_at(手改過或跨版本的 manifest):
            # 對不出來就不能放行,但錯誤訊息要說得出是哪一顆,否則生成前突然冒出一句
            # 「dispatch time is missing」沒人查得動。**先記下來,不在這裡拋** ——
            # 前面幾顆撈到的候選還沒落盤。
            unreconcilable.append(
                f"episode {ep_n} attempt {unresolved_attempt_id!r}({exc})"
            )
            continue
        if candidates:
            discovered.setdefault(ep_n, []).extend(candidates)
            # T5(P3):**不在這裡就併進最終 violations** —— `_settle_cleanup_state`
            # 撞 CAS 衝突時會重驗 ownership、就地修剪 `discovered`(await 期間別的
            # finalizer 可能剛把某個候選 claim 成它自己的合法 continuity source,
            # 那筆就不再是孤兒)。若在這裡先併進一份定案的 `violations`,修剪只影響
            # `discovered`、不影響這份副本,回傳的錯誤訊息會繼續點名一筆已經被
            # 合法認領的 source,叫呼叫端去 `source_delete` 它。violations 的最終
            # 內容改到 `_settle_cleanup_state` 之後、單一位置,從(可能已修剪的)
            # `discovered` 重算。
        elif window_closed:
            settled_attempt_ids.add(unresolved_attempt_id)
        else:
            waiting.append((ep_n, unresolved_attempt_id))

    # 「這次真的查過、確認不在」的那幾筆。**只涵蓋身分等於這一本的義務** —— 別本的
    # 根本沒查過,不可能算「確認不在」(逐筆比對、不整欄 pop:await 期間可能又有一次
    # retract 追加新義務)。
    checked_absent_by_episode: dict[object, set[str]] = {}
    for ep_n, source_id in pending_here:
        if source_id not in live:
            checked_absent_by_episode.setdefault(ep_n, set()).add(source_id)

    def settle(manifest: dict) -> None:
        for row in manifest["episodes"]:
            for candidate_attempt in row.get("attempts", []):
                if candidate_attempt.get("attempt_id") in settled_attempt_ids:
                    # 窗已關 + 零候選 = 遠端真的沒有多出東西。義務結案,重生放行。
                    (candidate_attempt.get("retraction") or {}).pop(
                        "source_cleanup_unresolved", None
                    )
            found = discovered.get(row.get("episode"))
            if found:
                history = row.setdefault("previous_feedback_source_ids", [])
                for source_id in found:
                    if source_id not in history:
                        history.append(source_id)
                    # 候選是從**這一本**的 sources.list 撈出來的,身分就是它。
                    _record_cleanup_obligation(row, source_id, notebook_id)
            checked_absent = checked_absent_by_episode.get(row.get("episode"))
            if checked_absent:
                _drop_cleanup_obligations(row, checked_absent)

    # **完全沒有變更就不要寫**(盲審 P2):`ManifestStore.update` 的 revision 是無條件 +1,
    # 所以 blocked/waiting 這種「只是來查一下」的路徑照樣會 bump —— 除了無效寫盤,還會
    # 平白撞掉另一個 process 正在做的 discovery CAS。
    if discovered or settled_attempt_ids or checked_absent_by_episode:
        _settle_cleanup_state(
            store,
            settle,
            expected_revision=snapshot_revision,
            discovered=discovered,
            settled_attempt_ids=settled_attempt_ids,
            checked_absent_by_episode=checked_absent_by_episode,
            notebook_id=notebook_id,
        )

    # T5(P3):**單一位置**,用 CAS 衝突處置之後(可能已修剪)的 `discovered` 重算
    # 最終要回報的 violations——`_settle_cleanup_state` 撞衝突時會就地修剪
    # `discovered`(見上方呼叫點),這裡讀到的一定是修剪後的結果,不會把已被別的
    # finalizer 合法認領的 source 也點名進錯誤訊息。
    violations = list(pending_violations)
    for ep_n, source_ids in discovered.items():
        violations.extend((ep_n, source_id) for source_id in source_ids)

    if violations:
        details = ", ".join(f"episode {ep_n}: {sid}" for ep_n, sid in violations)
        raise ValueError(
            f"notebook {notebook_id!r} has retracted feedback sources still in the "
            f"notebook: {details}. Delete them first — "
            f"source_delete(notebook_id={notebook_id!r}, source_id=...) — then retry; "
            "leaving them creates two identically named sources."
        )
    if unreconcilable:
        raise ValueError(
            "這幾顆 retracted attempt 留下未結案的回錄 source 清理義務,但它們的 upload "
            f"checkpoint 對不出候選:{', '.join(unreconcilable)}。"
        )
    if waiting:
        details = ", ".join(f"episode {ep_n}: {att!r}" for ep_n, att in waiting)
        window_seconds = int(UPLOAD_DISPATCH_WINDOW.total_seconds())
        raise ValueError(
            f"notebook {notebook_id!r} has retracted attempts whose feedback source "
            f"upload was never resolved: {details}. 現在查到零候選,但候選窗還沒關"
            f"(dispatch 起算 {window_seconds} 秒),此刻的「沒有」不等於「不會出現」。"
            "等窗關上後重呼同一支工具,它會自己對帳:撈到就會要求 source_delete,"
            "確認零候選才放行。"
        )


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
    # **同一顆 attempt 餵進 `_attempt_capabilities` 就有正確答案,不要自己手寫指引。**
    # 舊訊息無條件教「reconcile or resume」,但可達狀態裡至少兩種兩條建議都走不通:
    # `accepted` + 遠端終態失敗(`remote.status` in failed/removed)時,reconcile 會被
    # :1987 的狀態檢查擋(它只收 acceptance_unknown/reconciliation_ambiguous),resume
    # 則因為終態已定而直接拋 `TerminalGenerationError`——呼叫端照著冪等契約原樣重呼,
    # 撞到這句話,再照做又是兩條死路。這是同一根因(指引在它自己產生的狀態下不可
    # 執行)的第七次現形,單一事實來源不容許再手寫一條 if/else。
    raise ValueError(
        f"frozen attempt is {status!r}; "
        + _attempt_next_step(_attempt_capabilities(episode, attempt, attempt_id))
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
    *,
    account: str | None,
    client: object,
    manifest_path: str | None = None,
    prepared_generation_input: dict | None = None,
    source_ids: list[str] | None = None,
) -> tuple[dict, str | None, object]:
    os.makedirs(output_dir, exist_ok=True)

    _validate_episode_args(episode_n, title, prior_mp3_path)
    resolved_language = resolve_language(language)
    resolved_audio_format = to_audio_format(audio_format)
    resolved_audio_length = to_audio_length(audio_length)
    selected_source_ids = to_source_ids(source_ids)
    # P2:`prior_mp3_path` 會把上一集回錄真的上傳進筆記本、改名,但 dispatch 只帶
    # `selected_source_ids`——那筆新上傳的 source 沒有任何 attempt 引用它,`ok=true`
    # 回傳,變成無人認領的孤兒,吃掉 ≤9 上限裡的一格卻沒人記得。這是 `podcast_episode`
    # 與 `podcast_series`(series 恆傳 `prior_mp3_path=None`,不受影響)唯一的交會點,
    # 必須排在任何副作用(對帳 RPC、上傳)之前 fail-closed,不能等副作用發生後才發現。
    if prior_mp3_path and selected_source_ids:
        raise ValueError(
            "prior_mp3_path 與指名 source_ids 不能同時提供:指名 `source_ids` 時"
            "把上一集的回錄 source id 直接放進 `source_ids`,不要再傳 "
            "`prior_mp3_path`;要靠 `prior_mp3_path` 自動上傳就不要指名"
        )
    settings = _audio_settings(
        resolved_language, audio_format, audio_length, selected_source_ids
    )
    # 兩道都在建 attempt 與任何副作用之前。**筆數先驗**:指名時它是純本地的 len(),
    # 超標可以連對帳那趟 RPC 都不打就秒退;沒指名時它自己去問筆記本現有幾筆。
    # `prior_mp3_path` 會在守門之後、生成之前再上傳一筆(下面那段 standalone 續集),
    # 所以**現在就要把它算進去**——否則 9 筆放行、上傳完以 10 筆送出。
    await assert_source_count_is_safe(
        client,
        notebook_id,
        selected_source_ids,
        pending_uploads=1 if prior_mp3_path else 0,
    )
    # 唯讀對帳:一筆打錯/已刪的 id 不會被伺服器擋下來。
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
                and not isinstance(error, ManifestPostCommitError)
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

    # caller 一次取好「通過 auth probe 的帳號 + 它的 client」並傳進來:
    # preflight / baseline / 記帳 / 實際送出四者必須同源。分開讀全域狀態時,並行的
    # 另一個工具呼叫在那個縫裡 rotate,就會變成 baseline 是 A 看到的、manifest 記 A、
    # 實際卻由 B 送出(ADR-0010 §Transparency:manifest 是唯一的稽核憑據)。
    dispatch_account, dispatch_client = account, client
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
            wait_timeout=wait_timeout,
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
            notebook_id=notebook_id,
        )
    except _REFUSED_WITHOUT_DISPATCH as exc:
        # 裸拋的話呼叫端只看到 SDK 的「rate limit exceeded」,不知道 attempt 已經被
        # 持久化、也不知道該怎麼續(v0.7.1 驗收 F-9)。docstring 承諾「依錯誤中的
        # attempt_id 續跑」,兩個分支都要兌現。
        if store is not None:
            # T8:`_REFUSED_WITHOUT_DISPATCH` 有兩個生產者(見 `_failover.py` 模組
            # docstring),「沒有建立任何 artifact」對它們的確定性不一樣——
            # decoder 的 `USER_DISPLAYABLE_ERROR` 是契約講死沒建出 task;transport
            # 層 429(`rpc_code=None`)只是請求已送達伺服器才被限流打回來,**幾乎
            # 必然**沒建出 task,不是硬保證。措辭如實反映這個差異,行為不變(續跑
            # 建議仍是同一句)。
            exc.args = (
                f"{exc}\n伺服器拒絕了這次生成(attempt_id={attempt_id!r},已標記 "
                "not_accepted)。契約保證沒有建立任何 artifact 的是 "
                "ArtifactFeatureUnavailableError;RateLimitError 的傳輸層 429 拒絕"
                "只是幾乎必然沒有建立(請求已送達伺服器才被限流打回來,理論上不"
                "排除極端情況伺服器已受理但回應遺失)。配額/限流回復後,用"
                "**完全相同的參數**重呼 podcast_episode 即可沿用同一個 attempt "
                "重送——不會新建 attempt。",
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
                client, notebook_id, episode_n, title, artifact_id, output_dir, wait_timeout
            )
        return output, dispatch_account, client
    except TerminalGenerationError:
        # 伺服器端終態(failed / removed,如每日配額耗盡):artifact 已被下架,resume
        # 也救不回——原樣往上拋,不誤導成「可續跑」。用專屬型別而非 except RuntimeError,
        # 才不會把下載/命名步驟意外的 RuntimeError 也當成不可續跑。
        raise
    # T6:`CancelledError` 是 `BaseException` 的直接子類,不是 `Exception`——只
    # `except Exception` 收不到它。姊妹的 dispatch 段(上面 `_dispatch_audio_with_
    # failover` 那圈)已經是 `except (Exception, asyncio.CancelledError)`,這裡
    # 沒有同步跟上:client 在 finalize(下載/回錄上傳/rename)期間被 cancel 時,
    # 底下附上 `podcast_episode_resume` 續跑呼叫 + `next_step` 的整段邏輯全部被
    # 跳過,例外裸拋出去——artifact 明明已在雲端生成完,呼叫端卻拿不到任何續跑
    # 指引。
    except (Exception, asyncio.CancelledError) as exc:
        # 其餘失敗(本地 wait 超時、下載中斷、網路斷)發生在生成之後,artifact 仍在雲端
        # 完好。就地改寫 exc.args 附上 artifact_id + 現成的 podcast_episode_resume 呼叫,
        # 再原樣 re-raise —— 保留原例外「型別與結構化欄位」(SDK 的 ArtifactTimeoutError
        # 等 constructor 需 notebook_id/task_id/timeout 多個必填參數,type(exc)(str) 會
        # 反而 TypeError 吞掉真錯;改寫 args 對內建與 SDK 例外都能把 hint 帶進 str(exc))。
        # 呼叫端據此續完(不重生、不燒 quota),不必再 artifact_list 撈 id。
        # **完整呼叫是 recovery payload,`next_step` 才是狀態權威 —— 兩者並存。**
        # 只留 next_step 的散文會丟掉 `podcast_episode_resume` 的五個必填參數
        # (notebook_id / episode_n / title / artifact_id / output_dir),而那份參數
        # 正是這整段存在的理由:呼叫端據此續完,不必再 artifact_list 撈 id。只留完整
        # 呼叫又會在「並行 retract 已經把這顆 tombstone 掉」時教一個做不到的動作。
        original_error = str(exc)
        manifest_argument = (
            f", manifest_path={manifest_path!r}" if manifest_path else ""
        )
        resume_call = (
            f"podcast_episode_resume(notebook_id={notebook_id!r}, "
            f"episode_n={episode_n!r}, title={title.strip()!r}, "
            f"artifact_id={artifact_id!r}, output_dir={output_dir!r}"
            f"{manifest_argument})"
        )
        head = (
            f"{original_error}\n音檔已在雲端生成(artifact_id={artifact_id!r})但後續步驟失敗。"
            f"既有 attempt_id={attempt_id!r}。續完(不會重新生成)的完整呼叫:{resume_call}"
        )
        if store is not None:
            # 同一條 finalize 例外可能仍是 accepted attempt,也可能已被並行 retract 成
            # tombstone(`abandon_in_flight` 讓那件事變成受支援的操作),所以現在該做
            # 什麼一律從 capabilities 算,不手寫。
            try:
                current = store.read()
                stopped_episode, stopped_attempt = _attempt_record(
                    current, episode_n, attempt_id, allow_retracted=True
                )
                caps = _attempt_capabilities(
                    stopped_episode,
                    stopped_attempt,
                    attempt_id,
                    post_retract=bool(stopped_attempt.get("retraction")),
                )
                next_step = (
                    "**只有下面這句仍指向 resume 時才執行上面那個呼叫**："
                    + _attempt_next_step(caps)
                )
            except Exception as state_error:
                # 讀不到最新狀態時**不能留空**(舊版在這裡回空字串,於是訊息以一個
                # 分號結尾、什麼都沒教)。完整呼叫照給,但要說清楚它未經狀態核對。
                next_step = (
                    f"無法計算最新狀態({state_error});執行上面那個呼叫之前先檢查 "
                    f"manifest_path={manifest_path!r} 裡 episode {episode_n} 的 "
                    f"attempt {attempt_id!r} 是否已被 retract。"
                )
            exc.args = (f"{head}\n{next_step}",)
        else:
            # standalone(沒有 manifest)只有 best-effort 保證,沒有狀態可以核對。
            exc.args = (head,)
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
    workspace_root: str | None = None,
) -> dict:
    """生成、命名、下載並回錄一集 podcast。**單集與整季的預設入口都是這支** —— ``podcast_series``
    只在「共用 notebook + 整季 <=5 集 + 不必指名來源」時用。

    ``brief`` 與 ``input_bundle_path`` **二擇一**:給 bundle 時 ``brief`` 必須是 ``None``,
    且須同時給 ``manifest_path`` 與 ``workspace_root``(bundle 路徑相對於它)。

    傳 ``manifest_path`` = durable attempt:timeout／斷線後依錯誤裡的 ``attempt_id`` 呼叫
    ``podcast_episode_reconcile``,**不可重送本工具重生**;finalize 中斷走
    ``podcast_episode_resume``。不傳則是 standalone best-effort(無 durable 對帳)。

    ``source_ids`` 指名這一集只讀哪幾筆來源(id 用 ``source_list`` 取),省略 = 讀筆記本
    全部來源。**回頭重生某一集時必須傳** —— 否則後續各集的題目與音檔回錄會洩進這一集。
    它算生成輸入,存進 attempt settings,resume 時不得改動。

    ⚠️ **帶進生成的來源 >= 10 筆會在任何副作用之前 raise**(指名與不指名同一把尺)。抓約
    6 筆:本集自己的來源 + 最近 5 集的音檔回錄,集號比本集大的回錄一律排除。

    命名:Studio artifact 與回錄 source 都是 ``EP{n:02d} 正文``(title 上匹配的
    ``EP{n:02d}. `` / ``EP{n:02d} `` 前綴會先剝掉)。
    """
    # 本地驗證先行(壞參數 ValueError 秒退,不浪費 RPC),再做認證預檢:
    # 單集也要等最多 20 分鐘,cookie 死了先秒退(見 auth_probe docstring)。
    _validate_episode_args(episode_n, title, prior_mp3_path)
    # wait_timeout 會被 `_claim_prepared_dispatch` 持久化,升級成之後每一次對帳的
    # 窗判準(第四輪修復,見 `_validate_wait_timeout` docstring)——沒有信任邊界
    # 檢查的呼叫端輸入不能直接變成安全參數。
    _validate_wait_timeout(wait_timeout)
    source_ids = to_source_ids(source_ids)
    if manifest_path and prior_mp3_path:
        raise ValueError(
            "prior_mp3_path with manifest_path is not checkpointed in P0; "
            "use an already verified notebook source or standalone best-effort"
        )
    prepared_generation_input = None
    if workspace_root is not None and input_bundle_path is None:
        raise ValueError("workspace_root only applies to input_bundle_path (frozen generation input)")
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
            workspace_root=workspace_root,
        )
        # Existing sidecars are replay authority. Validate their request/workspace
        # identity before auth or any cleanup/baseline RPC; a copied bound bundle
        # must be rejected without touching the provider.
        read_attempt_binding(prepared_generation_input)
        brief = prepared_generation_input["brief"]
    elif not isinstance(brief, str) or not brief.strip():
        raise ValueError("brief must be a non-empty string without input_bundle_path")
    account, client = runtime.snapshot()
    await probe_auth(client)
    output, _, _ = await _run_episode(
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
        account=account,
        client=client,
        manifest_path=manifest_path,
        prepared_generation_input=prepared_generation_input,
        source_ids=source_ids,
    )
    return output


# P1(Codex adversarial review 實跑驗證,主迴圈裁決採納):**時間窗本身判定不了
# artifact 歸屬。** 兩顆並行、各自 response lost 的 unresolved attempt,候選窗會
# 重疊——窗內出現的一顆 artifact 可能是「另一顆」的產物,不是「正在對帳的這一顆」的。
# 實跑重現:EP1 用 wait_timeout=7200 dispatch 後 response lost;EP2 在窗內另外
# dispatch,也 response lost、建出 artifact-B(同樣還沒 claim);對 EP1 reconcile 時,
# artifact-B 落在 EP1 的候選窗內、不在 EP1 的 baseline、也沒被 EP2 claim,於是被判成
# EP1 的「唯一候選」而誤綁——manifest 的稽核紀錄從此失真(這顆其實是 EP2 的)。
#
# 完整重新設計歸屬判定不在這輪範圍,這裡先加 Codex 驗過的保守 guard:唯一候選出現
# 時,若同一本 notebook 底下還有其他「未解決」的 attempt,代表這顆 artifact 有可能
# 是它的產物,不能自動綁定——停下來把候選交給呼叫端,要求明確指名
# (`podcast_attempt_adopt`)。**只有一顆 attempt 在飛的正常情境不受影響**:那時
# `_unresolved_attempt_ids` 排除自己之後找不到任何人,guard 不會觸發,自動綁定照舊。
#
# 「未解決」的判準:dispatch_status 落在對帳中的三個狀態(與 `can_reconcile` 判準
# 一致——`_NEVER_DISPATCHED` 從沒離開本機、`_TERMINAL_REMOTE` 已終態,兩者往後都不會
# 再冒出新 artifact,不算「未解決」)、且尚未 claim 到 artifact。這三個狀態下
# `remote.artifact_id` 理論上恆為 None(見下面 `podcast_episode_reconcile` 開頭的
# 一致性檢查:`remote_artifact_id is not None` 時 `dispatch_status` 必須是
# `"accepted"`),這裡仍顯式檢查而不是只憑 dispatch_status 假設,防呆成本很低。
# T10(測試債,wp-a2):這裡原本自己另開一份同樣的 frozenset(`_UNRESOLVED_
# DISPATCH_STATUSES`)——與 `can_reconcile` 判準「一致」這句話原本只是註解上的
# 承諾,程式碼層級是兩份獨立字面值。改成共用模組層的 `_RECONCILABLE_DISPATCH_
# STATES`(定義在 `_NEVER_DISPATCHED` 旁邊),別再各自維護一份。


def _unresolved_attempt_ids(
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
            if (
                attempt_id == excluding_attempt_id
                or attempt.get("notebook_id") != notebook_id
            ):
                continue
            remote = attempt.get("remote") or {}
            if remote.get("artifact_id") is not None:
                continue
            dispatch = attempt.get("dispatch") or {}
            if dispatch.get("status") in _RECONCILABLE_DISPATCH_STATES:
                unresolved.append(attempt_id)
    return sorted(unresolved)


@mcp.tool()
async def podcast_episode_reconcile(
    manifest_path: str,
    episode_n: int,
    attempt_id: str,
    wait_timeout: float = 1200.0,
) -> dict:
    """不重新生成,找回遺失的 ``generate_audio`` response —— 只補綁 artifact identity,
    不 wait／rename／download／finalize。

    只收養一筆「建立時間落在持久化 dispatch window、且尚未被任何 attempt 認領」的 audio
    artifact;零筆或多筆候選都安全停止,不猜。``wait_timeout`` 縮不小原 dispatch 承諾的窗,
    但**傳更大的值會放大候選窗**去撈更晚出現的 artifact —— 零候選不等於死路。

    ``reconciliation_ambiguous`` 且 ``blocking_attempt_ids`` 非空 = 有唯一候選,但同一本
    notebook 還有別的 attempt 也已送出未認領,歸屬判定不了所以不自動綁;外部確認後以
    ``podcast_attempt_adopt`` 從 ``candidate_artifact_ids`` 指名。
    ⚠️ 其中若有 ``abandon_in_flight`` 作廢出來的 attempt,它會**永久**留在這個清單裡,
    於是這本 notebook 之後**每一次**候選自動綁定都要走一次人工 adopt —— 那是帶那個旗標
    最貴的長期副作用。
    """
    if not isinstance(manifest_path, str) or not manifest_path:
        raise ValueError("manifest_path must be a non-empty string")
    if not isinstance(episode_n, int) or isinstance(episode_n, bool) or episode_n < 1:
        raise ValueError("episode_n must be an int >= 1")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("attempt_id must be a non-empty string")
    _validate_wait_timeout(wait_timeout)
    _require_existing_manifest(manifest_path)

    return await _podcast_episode_reconcile(
        runtime.get_client(),
        manifest_path,
        episode_n,
        attempt_id,
        wait_timeout,
    )


async def _podcast_episode_reconcile(
    client: object,
    manifest_path: str,
    episode_n: int,
    attempt_id: str,
    wait_timeout: float,
    *,
    auth_probed: bool = False,
) -> dict:
    """用呼叫端固定的 client 執行已驗證參數的 artifact 對帳。"""

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
        episode_row, _ = _attempt_record(snapshot, episode_n, attempt_id)
        caps = _attempt_capabilities(episode_row, attempt, attempt_id)
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "accepted",
            "artifact_id": remote_artifact_id,
            "safe_next_action": caps["safe_next_action"],
            "next_step": _attempt_next_step(caps),
        }
    if dispatch_status not in (
        "dispatching",
        "acceptance_unknown",
        "reconciliation_ambiguous",
    ):
        raise ValueError(
            f"attempt state {dispatch_status!r} cannot be reconciled"
        )
    if not auth_probed:
        await probe_auth(client)
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
        if dispatch_status not in (
            "acceptance_unknown",
            "reconciliation_ambiguous",
        ):
            raise ValueError(
                f"attempt state {dispatch_status!r} cannot be reconciled"
            )

    artifacts = await client.artifacts.list(
        notebook_id, artifact_type=ArtifactType.AUDIO
    )
    claimed = _claimed_artifact_ids(snapshot, attempt_id)
    window_start = dispatched_at - _RECONCILIATION_CLOCK_SKEW
    # **候選 artifact 的篩選窗**:用 promised(原始承諾與這次呼叫取大),**不套
    # `_RECONCILIATION_MIN_WINDOW` 保守下限**——這裡曾經跟下面的關閉判斷共用同一個
    # 套了下限的值,窗大了反而不保守:legacy attempt(沒有持久化承諾)會被下限撐大到
    # 1 小時,把不屬於這次 dispatch 的 artifact 也算成候選,`_bind_reconciled_artifact`
    # 綁進別人的音檔(第四輪核心裁決)。這裡若只信這次呼叫的 `wait_timeout`(不合併
    # 持久化值)也有問題:傳 1 秒會先把稍晚才出現的真 artifact 排除在候選窗外,零候選
    # 出口再誤判窗已關建議 retract。
    candidate_window_end = (
        dispatched_at
        + timedelta(
            seconds=_promised_reconciliation_window_seconds(
                attempt["dispatch"], wait_timeout
            )
        )
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
        if window_start <= created_at <= candidate_window_end:
            candidates.add(artifact_id)

    candidate_ids = sorted(candidates)
    if len(candidate_ids) == 1:
        # **時間窗判定不了歸屬,所有權才是**(P1,見上面 `_unresolved_attempt_ids`
        # 的推導)。同一本 notebook 底下還有別的 attempt 也還沒 claim 到 artifact 時,
        # 窗內這顆唯一候選有可能其實是它的產物——不自動綁定,停下來讓呼叫端指名。
        blocking_attempt_ids = _unresolved_attempt_ids(
            snapshot, notebook_id, excluding_attempt_id=attempt_id
        )
        if not blocking_attempt_ids:
            artifact_id = candidate_ids[0]
            blocking_attempt_ids = _bind_reconciled_artifact(
                store, episode_n, attempt_id, artifact_id
            )
            if not blocking_attempt_ids:
                latest = store.read()
                episode_row, accepted_attempt = _attempt_record(
                    latest, episode_n, attempt_id
                )
                caps = _attempt_capabilities(
                    episode_row, accepted_attempt, attempt_id
                )
                return {
                    "complete": False,
                    "episode_n": episode_n,
                    "attempt_id": attempt_id,
                    "observed_state": "accepted",
                    "artifact_id": artifact_id,
                    "safe_next_action": caps["safe_next_action"],
                    "next_step": _attempt_next_step(caps),
                }
        # 跟「真的有多筆候選」共用同一個安全停點(`reconciliation_ambiguous` +
        # `podcast_attempt_adopt`):道理相同,都是「manifest 自己分不出這顆屬於誰,
        # 需要呼叫端帶外部知識來指名」。
        _mark_reconciliation_ambiguous(
            store, episode_n, attempt_id, candidate_ids
        )
        latest = store.read()
        episode_row, ambiguous_attempt = _attempt_record(
            latest, episode_n, attempt_id
        )
        caps = _attempt_capabilities(
            episode_row,
            ambiguous_attempt,
            attempt_id,
            candidate_selection_required=True,
        )
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "reconciliation_ambiguous",
            "candidate_artifact_ids": candidate_ids,
            "blocking_attempt_ids": blocking_attempt_ids,
            "safe_next_action": caps["safe_next_action"],
            "next_step": _attempt_next_step(caps),
        }
    if len(candidate_ids) > 1:
        _mark_reconciliation_ambiguous(
            store, episode_n, attempt_id, candidate_ids
        )
        latest = store.read()
        episode_row, ambiguous_attempt = _attempt_record(
            latest, episode_n, attempt_id
        )
        caps = _attempt_capabilities(
            episode_row,
            ambiguous_attempt,
            attempt_id,
            candidate_selection_required=True,
        )
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "reconciliation_ambiguous",
            "candidate_artifact_ids": candidate_ids,
            # F6 修復:真的有 2 筆以上候選時直接判定 ambiguous，不會先跑
            # `_unresolved_attempt_ids` 那條早退路徑，所以這裡本來就沒有「被別的
            # attempt 卡住」這件事——固定回空陣列，讓兩個 `reconciliation_ambiguous`
            # 出口的 return shape 對稱，keying 在這個欄位上的 host 不會 KeyError。
            "blocking_attempt_ids": [],
            "safe_next_action": caps["safe_next_action"],
            "next_step": _attempt_next_step(caps),
        }

    latest = store.read()
    latest_attempt, _, _, _ = _reconciliation_subject(
        latest, episode_n, attempt_id
    )
    latest_artifact_id = latest_attempt["remote"].get("artifact_id")
    if latest_artifact_id is not None:
        episode_row, _ = _attempt_record(latest, episode_n, attempt_id)
        caps = _attempt_capabilities(episode_row, latest_attempt, attempt_id)
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "observed_state": "accepted",
            "artifact_id": latest_artifact_id,
            "safe_next_action": caps["safe_next_action"],
            "next_step": _attempt_next_step(caps),
        }
    # **零候選:出路要寫在回傳裡,不能只寫在 skill 散文。**(v0.9.6 驗收 FINDING-1)
    # 這條路上 `safe_next_action` 指回本工具自己,而連呼兩次的回傳**逐欄位相同** ——
    # 呼叫端照著做就是無限迴圈。而 skill 與 `partial()` 都說「原地打轉」的唯一依據是
    # `attempt_count` / `superseded_attempt_count` 持續增加,但 reconcile 不建 attempt,
    # 這兩個數字恆為 1 / 0,**偵測條件從不成立**。
    # 資訊本來就在 —— 同一顆 attempt 餵進 `_attempt_capabilities` 就會得到正確那句;
    # v0.9.6 只是沒把這個出口接上去(收斂做了一半)。
    episode_row = next(
        (row for row in latest.get("episodes", []) if row.get("episode") == episode_n),
        {},
    )
    # F3(主迴圈裁決,採納審查者的反駁):零候選有兩種成因——那次生成還沒出現
    # (候選窗還開著,晚幾分鐘再對帳就撈得到),或它根本沒被受理(候選窗已經關,
    # 未來任何 artifact 都會落在窗外)。**只有後者「繼續對帳沒有用」才成立**;
    # 窗還開著時「重呼會得到一模一樣的回傳」是假的,而 `ACTION_RETRACT` 要求的
    # 「外部知識」是 `artifact_list`——那正是這支工具自己剛打過的同一支 RPC,
    # 回傳必然同樣是空,`abandon_in_flight` 那道「呼叫端知道 manifest 推導不出來
    # 的事」的門就被降級成同義反覆。窗還沒關就 retract 的後果:acceptance_unknown
    # 但伺服器其實受理了、artifact 還在生成 → retract + 重生 → 幾分鐘後第一顆
    # artifact 出現變成雲端孤兒 → 下次對帳撞 reconciliation_ambiguous、還白燒一次
    # 配額——正是 ADR-0009 要擋的「把還在飛的因果紀錄提前寫成墓碑」。
    #
    # **這個窗(關閉判斷)跟上面的 `candidate_window_end`(篩選候選)故意不是同一個
    # 值**(第四輪核心裁決,兩個窗的保守方向相反,見上面 `_RECONCILIATION_MIN_WINDOW`
    # 的常數註解):這裡才套 `_RECONCILIATION_MIN_WINDOW` 保守下限——窗大才保守,
    # 窗小 = 提前宣告「繼續等沒有用」= tombstone 一顆可能還在飛的 attempt。
    # **呼叫端顯式放大 `wait_timeout` 可以放大這個窗(那是救援路徑的逃生口,見
    # `_promised_reconciliation_window_seconds`),但 floor 不會自動撐大候選窗**
    # ——這行只影響關閉判斷,不影響上面的 `candidate_window_end`。
    reconciliation_window_end = (
        dispatched_at
        + timedelta(
            seconds=max(
                _promised_reconciliation_window_seconds(
                    latest_attempt["dispatch"], wait_timeout
                ),
                _RECONCILIATION_MIN_WINDOW.total_seconds(),
            )
        )
        + _RECONCILIATION_CLOCK_SKEW
    )
    reconciliation_window_closed = (
        datetime.now(timezone.utc) > reconciliation_window_end
    )
    # **action 與窗狀態說明都不在這裡手寫**(P2 修復,docs/gotchas-attempt.md 的
    # 紅線):`_attempt_capabilities()` 直接產生 `safe_next_action`,
    # `_attempt_next_step()` 從 `dispatch_status`/`can_reconcile` 推出窗狀態說明。
    # 呼叫點只組裝回傳欄位,不再做任何判斷。
    caps = _attempt_capabilities(
        episode_row,
        latest_attempt,
        attempt_id,
        reconciliation_window_closed=reconciliation_window_closed,
    )
    return {
        "complete": False,
        "episode_n": episode_n,
        "attempt_id": attempt_id,
        "observed_state": latest_attempt["dispatch"]["status"],
        "candidate_artifact_ids": [],
        "safe_next_action": caps["safe_next_action"],
        "next_step": "這次對帳在遠端找到 **0 個候選**。" + _attempt_next_step(caps),
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
    """接續一個已在 NotebookLM 雲端啟動的音檔生成,續完後半段(等完成 → 命名 → 下載
    → 上傳回錄)而**不重新生成**。

    什麼時候用:對帳拿到 ``observed_state="accepted"``,或已有 ``artifact_id`` 但 wait／
    download／upload 中斷。``artifact_id`` 只能來自 durable attempt 或 reconcile／adopt 的
    結果,**不可拿 ``artifact_list`` 最新一筆盲猜**。

    傳 ``manifest_path`` 時各步驟依 checkpoint 冪等接續,重呼不會多一筆 source;**與生成
    同一道閘** —— ``podcast_attempt_retract`` 留下的清理義務未結案前 fail-closed。不傳時
    僅 standalone best-effort(legacy artifact 尚無 manifest 才用)。
    """
    # 本地驗證先行(壞參數 ValueError 秒退),再認證預檢——與 podcast_episode 同一
    # fail-fast 順序:認證錯誤不得蓋掉參數錯誤。
    _validate_episode_args(episode_n, title, None)
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id 必填(從 durable attempt 或 reconcile 結果取得)")
    # T7:四個吃 `wait_timeout` 的公開入口(podcast_episode／podcast_series／
    # podcast_episode_reconcile／這裡)本該一律驗證,這支之前漏了——`nan` 存進
    # `dispatch["wait_timeout"]`(見 `_claim_prepared_dispatch`)後,`timedelta
    # (seconds=nan)` 會在往後每一次對帳時炸掉;`0`/負數則直流 `wait_for_completion`
    # 永不逾時。見 `_validate_wait_timeout` docstring。
    _validate_wait_timeout(wait_timeout)
    if manifest_path:
        _require_existing_manifest(
            manifest_path,
            missing_hint=(
                "for a legacy artifact with no manifest yet, call this tool "
                "without manifest_path (standalone best-effort)"
            ),
        )
    client = runtime.get_client()
    await probe_auth(client)
    if manifest_path:
        store = ManifestStore(manifest_path)
        # 與 `_run_episode` 同一道 gate:retract 留下的清理義務未結案前不得繼續產出。
        # resume 也會 upload 回錄 source,漏這道就等於留一條繞過去的路(舊版真的漏了)。
        await _assert_source_cleanup_done(
            client, store, notebook_id, episode_n
        )
        attempt_id = _ensure_resume_attempt(
            store,
            notebook_id=notebook_id,
            episode_n=episode_n,
            title=title,
            artifact_id=artifact_id.strip(),
        )
        try:
            output = await finalize_attempt(
                client,
                store,
                episode_n=episode_n,
                attempt_id=attempt_id,
                output_dir=output_dir,
                wait_timeout=wait_timeout,
            )
        except RemoteArtifactUnverifiableError as exc:
            # T6:resume 路徑接住同一個具名例外,給結構化錯誤訊息而不是裸拋一句沒有
            # 下一步的話。**不能用 `_attempt_next_step(caps)` 整句**——這顆 attempt
            # 若還沒 promote(`is_output=False`),caps 的 `can_resume` 分支會教
            # 「先 podcast_episode_resume 續完」,而那正是剛失敗的呼叫,對自己產生
            # 的狀態不可執行(gotchas-attempt.md 紅線①)。改用
            # `_retract_hint(caps) + caps["regeneration_hint"]`——這兩塊只講
            # retract 與重生時要注意什麼,不論 is_output 真假都不會指回 resume 自己。
            try:
                current = store.read()
                stopped_episode, stopped_attempt = _attempt_record(
                    current, episode_n, attempt_id, allow_retracted=True
                )
                caps = _attempt_capabilities(
                    stopped_episode,
                    stopped_attempt,
                    attempt_id,
                    post_retract=bool(stopped_attempt.get("retraction")),
                )
                guidance = _retract_hint(caps) + caps["regeneration_hint"]
            except Exception as state_error:
                guidance = (
                    f"無法計算最新狀態({state_error});先確認 manifest_path="
                    f"{manifest_path!r} 裡 episode {episode_n} 的 attempt "
                    f"{attempt_id!r} 目前的狀態。"
                )
            exc.args = (f"{exc}\n{guidance}",)
            raise
        _promote_attempt_output(store, episode_n, attempt_id, output)
        return output

    output = await _finalize_episode(
        client, notebook_id, episode_n, title, artifact_id.strip(), output_dir, wait_timeout
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


def _cleanup_obligations(
    episode: dict, *, fallback_notebook: object = None
) -> list[dict]:
    """讀出這一集未結案的清理義務,**每一筆都帶自己的 notebook 身分**。

    身分要跟著義務走,不能拿 episode 當下的 notebook 回推(盲審 P1):`manifest_store`
    明文允許 retract 的 tombstone 保留建立時綁的舊 notebook(否則 retract 之後想換
    notebook 重生就寫不進去),於是同一集換本重生之後,舊義務的 source 其實躺在**別本**
    筆記本裡 —— 拿新本去查會「查無此 source」而把義務誤判成已結案,舊來源從此沒人記得,
    卻仍在原本那本污染後續每一集的 context。

    v0.9.11 之前寫進去的是純字串(沒有身分)。那些只能補上 `fallback_notebook`
    (通常是 episode 當下的 canonical)—— best effort,補不出來(`None`)就是身分不明,
    由呼叫端 fail-closed,**不准當成已結案**。
    """
    obligations: list[dict] = []
    for row in episode.get("pending_source_cleanup") or []:
        if isinstance(row, str) and row:
            obligations.append({"source_id": row, "notebook_id": fallback_notebook})
        elif isinstance(row, dict) and isinstance(row.get("source_id"), str):
            if row["source_id"]:
                obligations.append(
                    {
                        "source_id": row["source_id"],
                        "notebook_id": row.get("notebook_id"),
                    }
                )
    return obligations


def _record_cleanup_obligation(
    episode: dict, source_id: str, notebook_id: object
) -> None:
    """把一筆帶身分的義務排進這一集(同一個 source_id 已在就不重複加)。

    只在真的有東西要排時才建 key,維持「沒有義務就沒有這個欄位」的既有形狀。
    """
    pending = episode.setdefault("pending_source_cleanup", [])
    for row in pending:
        existing = row if isinstance(row, str) else (row or {}).get("source_id")
        if existing == source_id:
            return
    pending.append({"source_id": source_id, "notebook_id": notebook_id})


def _drop_cleanup_obligations(episode: dict, source_ids: set[str]) -> None:
    """清掉「這次真的查過、確認不在」的那幾筆(逐筆比對,不整欄 pop)。"""
    left = [
        row
        for row in episode.get("pending_source_cleanup") or []
        if (row if isinstance(row, str) else (row or {}).get("source_id"))
        not in source_ids
    ]
    if left:
        episode["pending_source_cleanup"] = left
    else:
        episode.pop("pending_source_cleanup", None)


def _queue_pending_source_cleanup(
    episode: dict, source_ids: list[str], *, exclude: str, notebook_id: object
) -> list[str]:
    """把 adopt 換掉的舊 source(s)排進這一集的 ``pending_source_cleanup``(去重、
    排除這次選中的那筆),讓 `_assert_source_cleanup_done` 在下一次生成前逼刪——
    否則同名重複 source 從此沒人記得,後續每集生成都讀到它。

    回傳的是**這一集尚未結案的全部義務**,不是只有這次新增的那幾筆:adopt 回應遺失時
    host 會冪等重呼,若第二次回傳空清單、`safe_next_action` 就會從 source_delete 翻回
    series,host 照著走卻被 gate 硬擋成 ValueError(狀態冪等、指引卻不冪等,等於把
    自動化 host 導進死路)。"""
    for source_id in dict.fromkeys(source_ids):  # 去重、保留順序
        if isinstance(source_id, str) and source_id and source_id != exclude:
            _record_cleanup_obligation(episode, source_id, notebook_id)
    return [row["source_id"] for row in _cleanup_obligations(episode)]


@mcp.tool()
async def podcast_attempt_adopt(
    manifest_path: str,
    episode_n: int,
    attempt_id: str | None = None,
    artifact_id: str | None = None,
    feedback_source_id: str | None = None,
) -> dict:
    """把呼叫端**明確指名、且重新讀遠端驗證過**的 identity 原子綁回既有產製記錄:不
    generate、不 upload、不 rename,也**不以唯一同名項目推定 identity**。

    ``artifact_id`` 與 ``feedback_source_id`` **恰好擇一**;帶 ``artifact_id`` 時
    ``attempt_id`` 必填。

    候選 id 取自停點回傳:``candidate_artifact_ids``(artifact 對帳歧義)或
    ``candidate_source_ids``(回錄 source 上傳歧義)。``continuity_unverified`` /
    ``legacy_output_unverified`` 刻意不附候選,要自己 ``source_list`` 找出正確 id。

    換掉 source 時,舊 id 與同輪落選候選會排進該集清理義務,回傳 ``stale_source_ids`` 且
    ``safe_next_action`` 變 ``source_delete``。綁定後照 ``safe_next_action`` 續推。
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

        def adopt_artifact(manifest: dict) -> dict:
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
            return _attempt_capabilities(current_episode, current, attempt_id)

        _, caps = store.update(adopt_artifact)
        return {
            "complete": False,
            "episode_n": episode_n,
            "attempt_id": attempt_id,
            "artifact_id": artifact_id,
            "observed_state": "accepted",
            "safe_next_action": caps["safe_next_action"],
            "next_step": _attempt_next_step(caps),
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

    def adopt_source(manifest: dict) -> tuple[list[str], dict | None, bool]:
        """回傳 `(stale_source_ids, caps, complete)`。

        T2:`caps` 是 legacy 路徑(`attempt_id is None`,沒有 attempt 可問)時的
        `None`——那條路只是把下一集的續集身分接回來,不動這一集自己的 finalize
        狀態,`complete` 直接沿用既有硬證據。其餘路徑在 mutate **內部**算
        `_attempt_capabilities`(比照同一支工具的 artifact 分支),讓外層只組欄位、
        不用手寫 `ACTION_RESUME if needs_rename else ACTION_SERIES` 這種與 caps
        對不上的猜測。
        """
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
            stale = _queue_pending_source_cleanup(
                current_episode,
                [previous] if isinstance(previous, str) else [],
                exclude=feedback_source_id,
                # 義務要記在**這筆 source 實際所在**的那本 notebook 上,不是 episode
                # 當下的 default —— adopt 認的就是這個 notebook 裡的 source。
                notebook_id=notebook_id,
            )
            # T4(P3):這裡不能拿 `notebook_id`(這次 adopt 的目標)去回推**全部**
            # 未結案義務的身分——這一集可能還留著別本 notebook 的舊義務(例如上次在
            # 別本筆記本 retract 留下的孤兒),`_cleanup_obligations` 逐筆讀回自己
            # 實際記錄的身分,只在真的沒身分(legacy 純字串)時才 fallback。
            obligations = _cleanup_obligations(
                current_episode, fallback_notebook=notebook_id
            )
            return stale, None, has_durable_output_evidence(current_episode), obligations

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
        stale = _queue_pending_source_cleanup(
            current_episode,
            [*stale_candidates, previous] if isinstance(previous, str) else stale_candidates,
            exclude=feedback_source_id,
            notebook_id=notebook_id,
        )
        # T4(P3):同上一個分支——逐筆讀回自己的身分,不拿這次 adopt 的 notebook_id
        # 回推全部。
        obligations = _cleanup_obligations(
            current_episode, fallback_notebook=notebook_id
        )
        caps = _attempt_capabilities(current_episode, current, attempt_id)
        return stale, caps, has_durable_output_evidence(current_episode), obligations

    _, (stale_source_ids, caps, complete, obligations) = store.update(adopt_source)
    result = {
        # T2:「complete」問的是「這一集有沒有已經 promote 過的 output」,不是
        # 「這次呼叫還需不需要 rename」——舊版 `not needs_rename` 在 attempt 還沒
        # promote 時一樣回 True,host 據此直接 publish_series 會撞
        # `_ensure_local_mp3` 缺 mp3_path/artifact_id。legacy 路徑(`caps is None`)
        # 進來的前提就是既有硬證據,這裡沿用 mutate 內算好的真值,不是重新假設。
        "complete": complete,
        "episode_n": episode_n,
        "attempt_id": attempt_id,
        "feedback_source_id": feedback_source_id,
        "observed_state": (
            "accepted" if needs_rename else "continuity_verified"
        ),
        "safe_next_action": caps["safe_next_action"] if caps is not None else ACTION_SERIES,
    }
    if caps is not None:
        # T2:`safe_next_artifact_id` 是 resume 真正要傳的那個參數(resume 認
        # artifact_id,不是 attempt_id)——舊版回傳漏了它,`needs_rename=True` 時
        # 呼叫端讀得到 `safe_next_action="podcast_episode_resume"` 卻沒有可以帶
        # 進去的 artifact_id,只能去翻 manifest(違反自足性紅線)。
        result["safe_next_attempt_id"] = caps["safe_next_attempt_id"]
        result["safe_next_artifact_id"] = caps["safe_next_artifact_id"]
        result["next_step"] = _attempt_next_step(caps)
    if stale_source_ids:
        result["stale_source_ids"] = stale_source_ids
        # **同一個義務的另一個入口也要自足。** retract 那邊補了結構化義務,adopt 這邊
        # 沒補的話,呼叫端拿到 `safe_next_action="source_delete"` 卻只有 source_id ——
        # 而這支工具的參數表裡根本沒有 notebook_id,第二個參數無處可拿(補一半的又一例)。
        # T4(P3):身分逐筆讀回 mutate 內算好的 `obligations`(來自
        # `_cleanup_obligations`),**不能**拿這次 adopt 的 `notebook_id` 對每一筆
        # 都蓋一遍——這一集可能還留著別本 notebook 的舊義務,蓋掉等於指引呼叫端去
        # 錯的 notebook 打 `source_delete`。
        result["source_cleanup_obligations"] = obligations
        result["safe_next_action"] = ACTION_SOURCE_DELETE
        # T2:override 之後 `next_step`(若有)仍講著 caps 的原始建議(resume／retract
        # ……),與新的 `safe_next_action=source_delete` 互相矛盾——這正是紅線①要擋的
        # 「兩個欄位對同一個狀態指不同工具」。清理句子必須排在最前面:
        # `_assert_source_cleanup_done` 同一道閘擋生成也擋 resume,所以「先刪」與
        # 「刪完之後 caps 教的那句」是同一個順序關係,不是互斥的兩個答案。
        cleanup_sentence = (
            "先把這幾筆 source_delete 掉:"
            + "、".join(
                f"source_delete(notebook_id={notebook_id!r}, source_id={sid!r})"
                for sid in stale_source_ids
            )
            + "。"
        )
        result["next_step"] = cleanup_sentence + result.get("next_step", "")
    return result


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True))
async def podcast_attempt_retract(
    manifest_path: str,
    episode_n: int,
    attempt_id: str,
    reason: str,
    abandon_in_flight: bool = False,
) -> dict:
    """QA 拒收的唯一正門:純本機 manifest mutation,不打 RPC、不動遠端或本機檔案(所以
    **取消不了已經在燒的遠端生成**)。``attempt_id`` 必須是該集的 ``output_attempt_id``,
    或掛在 ``active_attempt_id`` 但從未 promote 的 candidate;``reason`` 必填非空。重呼
    冪等;取代版不得改標題。

    **``abandon_in_flight=True`` 只在這三個狀態需要**:``acceptance_unknown`` /
    ``dispatching`` / ``accepted`` 且遠端未回終態 —— manifest 推導不出有沒有東西在跑,只有
    呼叫端握有外部知識(例如 ``artifact_list`` 實際查過雲端零 artifact,或明知送進去的
    brief 就是錯的)。其餘都不用旗標:``prepared`` / ``not_accepted``、``remote`` 已
    ``failed`` / ``removed``、它本身已是正式輸出、該集已有別顆 attempt 接手。已被 supersede
    的歷史 attempt 誰都動不了,傳旗標一樣被拒。

    **retract 後必須把回傳的 ``source_cleanup_obligations``(每筆帶 ``notebook_id`` +
    ``source_id``)逐一 ``source_delete``** —— 別只看 ``stale_source_ids``,生成前 gate 對帳
    才撈到的孤兒不在那裡。下一次生成**或 resume** 前會實查 notebook,還在就 fail-closed。

    ``source_cleanup_unresolved=True``(upload 已送出、``source_id`` 還沒落盤)時
    **照回傳的 ``safe_next_action`` 做,不要照這個狀態名猜**:gate 已經對帳到具體 id 就是
    ``source_delete``(義務在 ``source_cleanup_obligations`` 裡,照樣要逐筆刪);還撈不到
    id 才是 ``null`` —— 那種是**要等、不是死路**(候選窗自 dispatch 起算,窗關且零候選才放行;
    實際秒數由 ``UPLOAD_DISPATCH_WINDOW`` 決定,錯誤訊息會給)。要保住那筆 source 的身分就別 retract,改走
    ``podcast_episode_resume``。

    **``safe_next_action`` 不一定是重生**:該集已有別顆 attempt 成為正式輸出時是 ``null``;
    已有替代 attempt 在飛時指向**那一顆**現在能做的事。⚠️ **執行它一律用
    ``safe_next_attempt_id`` / ``safe_next_artifact_id``,不要用 ``attempt_id``**(那永遠是
    被 retract 的稽核主體)—— 拿錯會撞 tombstone 或冪等重跑成無限迴圈。重生指回
    ``podcast_episode`` 時,把 ``regeneration_source_ids`` 原樣帶進 ``source_ids``。

    完整狀態表、稽核欄位與回傳形狀見 ``references/tool-reference.md`` 與 ADR-0009。
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

    def mutate(manifest: dict) -> tuple[dict, dict]:
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
            # 不重寫 retracted_at／reason;重生入口每次從同一顆 capabilities 算，
            # 才不會讓冪等回傳與第一次的實際後狀態分岔。
            caps = _attempt_capabilities(
                episode,
                attempt,
                attempt_id,
                post_retract=True,
            )
            return (
                dict(existing),
                caps,
            )

        caps = _attempt_capabilities(episode, attempt, attempt_id)
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
        # (4) **dispatch 從未離開本機**:`prepared` / `not_accepted` 是契約保證沒有建出
        # 遠端 task 的狀態(`_REFUSED_WITHOUT_DISPATCH` 與 ADR-0010 紀律④整條立論都建在
        # 這上面)。作廢這種 attempt 是純本機動作、零遠端後果,而且**manifest 自己就知道**
        # ——不需要呼叫端提供任何外部知識,所以不該經過 `abandon_in_flight`。
        #
        # 上面 (2) 用「該集有沒有其他 output 證據」當准入判準,但真正的變因是「這顆有沒有
        # 可能在遠端留下東西」——**把 proxy 當判準**,於是零副作用的 attempt 被一起關在
        # 門外。真實後果撞過三次:一次 dispatch 被配額/502 拒絕後 attempt 停在
        # not_accepted,retract 拒收它、原樣重呼又要求逐字相同的 brief(重現不了就死結),
        # 呼叫端只能用低階 `generate_audio` + `podcast_attempt_adopt` 繞出去、多燒一次
        # 生成配額。**`acceptance_unknown` 不在這裡面**:那個是真的不知道有沒有受理,
        # 需要外部知識,仍然只能走 `abandon_in_flight`。
        # **授權依據 = 免旗標的理由,或呼叫端顯式宣告的旗標。** 這個值就是稽核要記的
        # 那一個(v0.9.5 只記「傳了什麼」,而結果已定的 attempt 就算傳 true 也是白傳,
        # 事後分不出哪一次真的動用了外部知識)。
        basis = caps["authorization_basis"]
        if basis is None and abandon_in_flight and caps["is_active"]:
            basis = "abandon_in_flight"
        abandons_unauthorized_candidate = (
            caps["is_active"] and not caps["is_output"] and basis is not None
        )
        if output_attempt_id != attempt_id and not abandons_unauthorized_candidate:
            # **先分 ownership,再談旗標。** `abandon_in_flight` 只放行
            # `active_attempt_id` 那一顆 —— 對一顆已經被 supersede 的歷史 attempt,
            # 傳 True 與傳 False 得到**同一句**話。v0.9.4 新寫的訊息卻無條件教它
            # 「帶旗標重呼」,那是永遠做不到的動作:修死路的那一版自己又給了一條死路。
            if attempt_id != active_attempt_id:
                raise ValueError(
                    f"attempt {attempt_id!r} 已經不是 episode {episode_n} 的 active 或 "
                    f"output attempt(現在 active={active_attempt_id!r}、"
                    f"output={output_attempt_id!r})—— 它是歷史紀錄,retract 動不了它,"
                    "`abandon_in_flight` 也只放行 active 那一顆。要作廢的是現在那顆的話,"
                    "用它的 id 重呼本工具。"
                )
            # **訊息要說得出正門**(v0.9.3 驗收 FINDING-2)。原本寫的是「only a promoted
            # output attempt can be retracted」—— 那句話在這個狀態下**是假的**:傳
            # `abandon_in_flight=True` 就 retract 得掉。只讀工具回傳的呼叫端會判定
            # 「這條路關著」,而那正是 v0.9.1 FAIL-1 的同型(指引在它自己產生的狀態下
            # 不可執行)。v0.9.3 修了 docstring 卻漏了這裡 —— 等於修了給人讀的那份、
            # 漏了給機器讀的那份,而真正的呼叫端讀的是這一句。
            status = attempt.get("dispatch", {}).get("status")
            raise ValueError(
                f"attempt {attempt_id!r} is not episode {episode_n}'s durable output "
                f"(dispatch.status={status!r}) —— 它可能還在遠端跑,所以預設不讓作廢。"
                "確定要作廢的話:先用 artifact_list(notebook_id, kind=\"audio\") 查雲端"
                "到底有沒有這一集的 artifact,再帶 abandon_in_flight=True 重呼本工具。"
                "那個旗標的意思就是「我查過了,manifest 推導不出來的那件事我知道」。"
                "(dispatch.status 是 prepared / not_accepted 時不需要旗標 —— 契約保證"
                "伺服器沒建出 task。)"
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
        # **身分不確定的義務也要記下來,不能因為記不住就擋住 retract。** upload 已經
        # dispatch 但 `source_id` 沒落盤時,遠端可能多出一筆沒人記得的 media —— 舊版的
        # 處置是硬擋 retract(連 `abandon_in_flight` 都擋),而那個狀態靠 cancellation
        # 就能永久存在,唯一出口 `_reconcile_source_upload` 只從 finalize 進得去,等於
        # 「要作廢一顆輸入本來就錯的 attempt,得先把它完整 finalize、上傳、promote」
        # ——比 `abandon_in_flight` 本來要避免的後果還多一輪遠端副作用。
        # 改成:放行 retract,把「還沒對帳完」記成 tombstone 上的義務,由生成前的
        # `_assert_source_cleanup_done` 用候選窗結案。已經算出來的候選(ambiguous)
        # 身分雖不確定但範圍確定,直接排進 pending 讓呼叫端全刪掉最保守。
        unresolved_upload = unresolved_upload_descriptor(attempt)
        candidate_source_ids = (
            [
                source_id
                for source_id in (unresolved_upload.get("candidate_source_ids") or [])
                if isinstance(source_id, str) and source_id
            ]
            if unresolved_upload is not None
            else []
        )
        stale_source_ids = [
            source_id
            for source_id in dict.fromkeys(
                [
                    retracted_output.get("feedback_source_id"),
                    upload.get("source_id"),
                    *candidate_source_ids,
                ]
            )
            if isinstance(source_id, str) and source_id
        ]
        if stale_source_ids:
            # 沿用 adopt 既有的 episode 級歷史欄位,呼叫端忽略回傳值時仍留得住。
            history = episode.setdefault("previous_feedback_source_ids", [])
            # 未完成的清理義務。這不只是提示:下一次生成／resume 前會真的去 notebook
            # 驗它已經不在(見 `_assert_source_cleanup_done`),還在就 fail-closed。
            # **身分綁 attempt 自己的 notebook**:tombstone 可以合法保留舊 notebook,
            # 而這筆 source 就躺在那一本裡 —— 記成 episode 當下的 default 的話,之後
            # 換本重生時會拿新本去查、查無此 source 就把義務誤清(盲審 P1)。
            obligation_notebook = attempt.get("notebook_id") or episode.get(
                "notebook_id"
            )
            for source_id in stale_source_ids:
                if source_id not in history:
                    history.append(source_id)
                _record_cleanup_obligation(episode, source_id, obligation_notebook)
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
            # **兩種 retract 在稽核紀錄上要分得出來**(v0.9.3 驗收 FINDING-3)。
            # 一種是結果已定的(`prepared`/`not_accepted`,或遠端已回報終態):純本機、
            # 零遠端後果、manifest 自己就知道;另一種是呼叫端**顯式宣告**了推導不出來
            # 的外部知識,而遠端可能真的有東西在燒。兩者原本的 `retraction` 欄位完全相同。
            # `reason` 必填的理由是「retract 是審計事件」(ADR-0009);同一個理由要求記下
            # 這次動用了哪一種權限(ADR-0010 §Transparency:manifest 是唯一的稽核憑據)。
            #
            # **兩個欄位要一起讀**:`abandon_in_flight` 是呼叫端**傳了什麼**,不是「特權
            # 有沒有生效」—— 結果已定的 attempt 就算傳 `True` 也是白傳(准入早就放行了)。
            # 判準是 `dispatch_status_at_retraction`:它落在結果已定的那組時,這次 retract
            # 沒有動用任何外部知識。連同當時的值一起存是為了讓這筆稽核**自我完整**
            # (不必回頭翻 attempt、也不受手改 manifest 影響),不是因為合法操作會改動它
            # —— tombstone 是 default-deny 的,`_attempt_record` 擋掉所有 attempt 級 writer。
            "abandon_in_flight": abandon_in_flight,
            # 「可能有孤兒回錄 source,但身分還對不出來」。**只記一個狀態旗標,不複製
            # `source_ids_before` / `expected_title` / `dispatched_at`** —— tombstone
            # 不刪 attempt,那三個欄位就一直躺在它的 finalize checkpoint 上,複製一份
            # 只是多一個會漂移的副本。對帳規則同理只有一份
            # (`unresolved_upload_candidates`)。
            "source_cleanup_unresolved": unresolved_upload is not None,
            "dispatch_status_at_retraction": attempt.get("dispatch", {}).get("status"),
            # **實際生效的授權依據**,這才是稽核該讀的那一個:`abandon_in_flight` 只是
            # 呼叫端傳了什麼,而結果已定的 attempt 就算傳 true 也是白傳 —— 兩種語意
            # 完全不同的 retract 會存成一模一樣的紀錄。值域:`settled`(manifest 自己
            # 知道結果)、`output_owner`(正式輸出或已有別顆接手)、`legacy_evidence`、
            # `abandon_in_flight`(真的動用了外部知識)。
            "authorization_basis": basis,
            "remote_status_at_retraction": (attempt.get("remote") or {}).get("status"),
        }
        attempt["retraction"] = retraction
        episode.setdefault("retracted_attempt_ids", []).append(attempt_id)
        caps = _attempt_capabilities(
            episode,
            attempt,
            attempt_id,
            post_retract=True,
        )
        return (
            dict(retraction),
            caps,
        )

    _, (retraction, caps) = ManifestStore(
        manifest_path
    ).update(mutate)
    return {
        **retraction,
        "observed_state": "retracted",
        "safe_next_action": caps["safe_next_action"],
        # **`safe_next_action == "source_delete"` 時,執行它要用這個。**
        # `stale_source_ids` 只涵蓋 retract 當下就知道身分的那幾筆;gate 對帳之後才撈到的
        # 候選只進 episode 的 pending(tombstone 不回寫),那時它是空的、真實 id 只在散文
        # 裡 —— 而 `source_delete` 還需要 notebook_id。兩者都在這裡。
        "source_cleanup_obligations": caps["source_cleanup_obligations"],
        # P1 修復:委派給 sibling 時這兩個欄位跟 `retraction["attempt_id"]`(稽核主體,
        # 即被 retract 的這一顆)不同——執行 `safe_next_action` 要用這兩個,不是
        # `attempt_id`(見上方 docstring)。
        "safe_next_attempt_id": caps["safe_next_attempt_id"],
        "safe_next_artifact_id": caps["safe_next_artifact_id"],
        # **`safe_next_action == "podcast_episode"` 時,執行它要用這個。**
        # 少了它,retract 的回傳教人「重生時必須帶回原本那組 source_ids」卻沒給那組,
        # 呼叫端只能省略 → `podcast_episode` 的預設是 `None` → 讀整本筆記本。整條
        # 官方復原路徑因此會靜默擴大生成輸入(Codex 獨立複審實跑抓到:認證失效 →
        # 停點指向 retract → retract 指向 podcast_episode → 兩次 dispatch 送出
        # `[['src-1'], None]`)。`None` 代表 manifest 真的沒記,`next_step` 會說要自己指名。
        "regeneration_source_ids": caps["regeneration_source_ids"],
        "next_step": _attempt_next_step(caps),
    }


def _active_attempt_or_reraise(current: dict, episode_n: int) -> str:
    """`podcast_series` 在候選集區間內、`_run_episode` 建立 attempt 之前就撞到的例外,
    要原樣浮上去給呼叫端看——沒有 default 的 `next()`／dict 下標會把 StopIteration／
    KeyError 蓋掉真正該讓人看到的原始例外。三個 except handler(TerminalGenerationError／
    RuntimeError／`_TRANSIENT_TRANSPORT_ERRORS`)結構完全相同,抽成這裡共用,「補一半」
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


def _classify_not_accepted_stop(exc: BaseException) -> tuple[str, str, dict]:
    """回 (observed_state, extra) 給 `partial()`,分辨『等配額』與『notebook 沒分享給
    這個帳號』——manifest 的 dispatch.status 對兩者都寫 not_accepted(契約上都是零
    副作用拒絕,這個分類本身沒有錯),但呼叫端拿到的結構化停點如果也只寫
    `not_accepted`,兩者長得一模一樣。權限問題不 rotate(見
    `_dispatch_audio_with_failover`),於是呼叫端拿工具自己給的 `safe_next_action`
    (`podcast_series`)原樣重呼,只會撞回同一個沒權限的帳號,而
    `attempt_count`/`superseded_attempt_count`(partial() 註解說的唯一『有沒有在原地
    打轉』依據)兩輪都是 1/0——跟等配額完全同一組數字。

    **`safe_next_action` 也跟著分岔**(v0.9.5):權限那一種回 `notebook_share_with_pool`。
    這裡曾經刻意不新增字面值,理由是「白名單是真工具名的集合,分辨兩者靠 observed_state
    與 error 訊息就好」—— 但那個理由只在「下一步仍然是 `podcast_series`」時成立。
    權限問題的下一步**真的是另一支工具**,而 `notebook_share_with_pool` 本來就是公開
    MCP 工具、完全符合白名單的意義。留著 `podcast_series` 的後果是同一份回傳裡兩個
    欄位互相矛盾:`error` 說去補分享,`safe_next_action` 說重呼 series,而 skill 教
    呼叫端「拿不準就直接照 safe_next_action 做」—— 只讀那個欄位的自動化會原地重試
    同一個沒權限的帳號。v0.9.3 為完全相同的理由加過 `ACTION_EPISODE` /
    `ACTION_RETRACT`,那個「不新增」的理由自己已經被推翻。
    """
    if isinstance(exc, NotebookAccessDenied):
        return "notebook_access_denied", ACTION_SHARE_WITH_POOL, {"error": str(exc)}
    return "not_accepted", ACTION_SERIES, {}


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
    """依 manifest 的安全續點,確定性地生成整季 podcast。

    ⚠️ **一次呼叫會連續生成到 ``episodes`` 清單結束或外層 timeout 砍掉**,不是推進一集就
    回來;timeout 只殺 MCP request,已送出的下一集在遠端照樣生完。**清單裡只放你真的要生成
    的集** —— 來源還沒進筆記本的集數先別放,否則它會拿「筆記本此刻的來源」生那一集。

    ``start`` 是執行下界、**不是重生旗標**:``episodes`` 仍須傳完整清單(集號由索引決定),
    ``start`` 之前的集不讀也不驗證。

    ⚠️ **這支不能指名來源**,每集都用筆記本當下的**全部**來源;生成前有 fail-closed 守門,
    >= 10 筆時**不外拋**,回安全停點 ``observed_state="too_many_sources"``(已跑完的集仍留在
    ``episodes``)。``safe_next_action`` **分兩種、照回傳的那個做**:這集還沒有 attempt →
    ``podcast_episode``(帶 ``source_ids``);已有 active attempt → ``podcast_attempt_retract``
    (那顆 settings 不指名來源,直接改呼單集入口會被 durable-active-attempt guard 擋)。
    實務上就是**從 EP06 起改用單集入口**;重呼本工具只會停在同一集。

    ⚠️ 停在 ``reconciliation_ambiguous`` / ``safe_next_action="podcast_attempt_adopt"`` 時,
    候選在 ``candidate_artifact_ids``(artifact 對帳)或 ``candidate_source_ids``(回錄
    source 上傳);adopt 必填其中一個 id,只讀動作名執行不了。"""
    # start 是執行下界，不是重生旗標；N 以前的 plan 是 caller 明示的 trust
    # boundary，不讀、不驗證。範圍錯誤仍須在任何遠端副作用前失敗。
    if start < 1:
        raise ValueError("start must be >= 1")
    if start > len(episodes):
        raise ValueError(f"start must be <= len(episodes) ({len(episodes)})")
    # wait_timeout 會被 `_claim_prepared_dispatch` 持久化,升級成之後每一次對帳的
    # 窗判準(第四輪修復,見 `_validate_wait_timeout` docstring)——這支工具是兩個
    # dispatch 入口之一(另一個是 `podcast_episode`),兩邊都要驗,漏一個正是
    # AGENTS.md 紀律①點名的病灶。
    _validate_wait_timeout(wait_timeout)

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
    account, client = runtime.snapshot()
    run_results: list[dict] = []
    # 整季共用一組生成設定,與集數無關 —— 接手守門與認證停點的交棒判準都要拿它比對,
    # 所以提到迴圈外算一次。
    series_settings = _audio_settings(
        resolve_language(language), audio_format, audio_length
    )

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

    def promotion_refused_stop(exc: PromotionRefusedError) -> dict:
        """T3(wp-a2):`_promote_attempt_output` 的兩個呼叫點共用同一份轉換——
        `PromotionRefusedError` 收斂了三道 ValueError 守門(已 retract／owned-by-
        another／legacy 不同 artifact),裸拋會讓已完成的 run_results 一起丟掉。
        新契約詞 `observed_state="promotion_refused"`。"""
        current = store.read()
        episode, attempt = _attempt_record(
            current, exc.episode_n, exc.attempt_id, allow_retracted=True
        )
        caps = _attempt_capabilities(
            episode, attempt, exc.attempt_id,
            post_retract=bool(attempt.get("retraction")),
        )
        return partial(
            exc.episode_n,
            exc.attempt_id,
            "promotion_refused",
            caps["safe_next_action"],
            next_step=_attempt_next_step(caps),
        )

    def reentry(episode: dict | None, attempt_id: str | None) -> dict:
        """認證恢復／連線恢復之後要呼哪一支工具,以及那句話怎麼講。

        **不能寫死 `podcast_series`。** 這一集的 attempt 可能指名了 `source_ids`(或
        綁著 frozen bundle),而 series 生不出那種 settings —— 照著寫死的欄位重呼,
        series 會把它 supersede 成不指名的新 attempt、改讀整本筆記本(含後面各集的
        回錄音檔),然後回報 `complete=True`。這正是 docs/gotchas-attempt.md 那條
        紅線要擋的形狀:**狀態相關的指引一律由 `_attempt_capabilities()` 產生**。

        認證本身確實是外部前提、不是 attempt 狀態,所以判準不是「這顆現在能做什麼」
        而是「series 重新進來時接不接得住它」——那由 `_series_handoff_caps()` 回答,
        與接手守門同一支,不會再有一邊漏補的第二個決策來源。
        """
        if episode is None or attempt_id is None:
            return {"safe_next_action": ACTION_SERIES}
        attempt = next(
            (
                row
                for row in episode.get("attempts", [])
                if row.get("attempt_id") == attempt_id
            ),
            None,
        )
        if attempt is None:
            return {"safe_next_action": ACTION_SERIES}
        caps = _series_handoff_caps(episode, attempt, attempt_id)
        if caps is None or caps["safe_next_action"] is None:
            # series 接得住 —— 但「接得住」不等於「照這次的參數重呼就會過」。
            # 參數漂移那一格(形狀是 series 的、language／format／length 不同)重呼
            # 會撞 settings 守門而裸拋:工具名對了、附帶條件沒講,呼叫端照做仍然
            # 走不通(Codex 獨立複審實跑抓到)。
            if _series_will_redispatch(attempt) and not _series_owns_attempt(
                attempt, series_settings
            ):
                return {
                    "safe_next_action": ACTION_SERIES,
                    "next_step": _SERIES_ARGUMENT_DRIFT_HINT,
                }
            return {"safe_next_action": ACTION_SERIES}
        return {
            "safe_next_action": caps["safe_next_action"],
            "next_step": _attempt_next_step(caps),
            # 交棒的下一步若是 `podcast_episode`,呼叫端要帶回原本那組來源才不會靜默
            # 改讀整本筆記本 —— 值必須在公開回傳裡(自足性紅線),不能要它翻 manifest。
            "regeneration_source_ids": caps["regeneration_source_ids"],
        }

    async def stop_if_auth_expired(
        episode_n: int,
        attempt_id: str | None,
        guard_client: object,
        episode: dict | None,
    ) -> dict | None:
        """把認證失效或暫時無法驗證轉成保留本次進度的安全停點。

        兩條例外的下一步是**同一個問題**(重登／重連之後 series 接不接得住這一集),
        所以共用 `reentry()`;差別只在 `observed_state` 要誠實區分「確認失效」與
        「這次驗不出來」。
        """
        try:
            await probe_auth(guard_client)
            return None
        except _AuthProbeError as exc:
            observed_state, error = "auth_expired", exc
        except _TRANSIENT_TRANSPORT_ERRORS as exc:
            observed_state, error = "verification_incomplete", exc
        handoff = reentry(episode, attempt_id)
        return partial(
            episode_n,
            attempt_id,
            observed_state,
            handoff.pop("safe_next_action"),
            error=str(error),
            **handoff,
        )

    async def refuse_if_too_many_sources(
        episode_n: int, attempt_id: str | None, guard_client: object
    ) -> dict | None:
        """**即將 dispatch 前**的來源筆數守門:回 partial 表示要停,回 None 表示放行。

        擺在 series 這一層而不是只擺在兩條 dispatch 點,是因為擋得夠早才不會留半成品:
        `not_accepted` 會被 re-arm 成 prepared、`failed/removed` 會建一顆 superseding
        attempt(`attempt_count` 因此 +1)。先改狀態再拒絕送出,留下的東西要另外收拾,
        而 `attempt_count` 是呼叫端判斷「有沒有在原地打轉」的唯一依據。

        **`safe_next_action` 依有沒有既有 attempt 分岔,這不是風格問題**:
        - 沒有 attempt(全新一集)→ `podcast_episode`,呼叫端帶 `source_ids` 直接進來即可。
        - 已有 attempt → **`podcast_attempt_retract`**。那顆 attempt 的 settings 是
          「不指名來源」,呼叫端拿指名版進來會被 `_is_resendable_same_request` 判成不同
          請求而拒絕(`already has durable active attempt`),不指名又撞回這道守門 ——
          **那是死路**,正是 v0.9.1 修過的「指引在它自己產生的狀態下不可執行」。

        權限錯誤也要在這裡分類:`_list_sources` 把它翻成 `NotebookAccessDenied`,而這道
        preflight 現在跑在 dispatch helper **之前** —— 不接住的話 series 會裸拋,連前面
        幾集跑完的 `run_results` 一起丟掉。
        """
        try:
            await assert_source_count_is_safe(guard_client, notebook_id)
        except TooManySourcesError as exc:
            if attempt_id is None:
                return partial(
                    episode_n, None, "too_many_sources", ACTION_EPISODE, error=str(exc)
                )
            return partial(
                episode_n,
                attempt_id,
                "too_many_sources",
                ACTION_RETRACT,
                # 「不需要旗標」這句話成立,靠的是**單向包含**:觸發守門的狀態
                # (`prepared`/`not_accepted`,或 remote 已 `failed`/`removed`)都會讓
                # `_attempt_capabilities` 給出 `authorization_basis="settled"`。
                # **不是等價** —— 免旗標 retract 還涵蓋 `output_owner` 與
                # `legacy_evidence` 兩種守門碰不到的情況;安全性只需要這個方向。
                # 動守門那一邊而不看這裡,指引就會變成死路(v0.9.4 就是:守門涵蓋了
                # failed/removed,而免旗標條件沒有,照做被拒)。
                error=(
                    f"{exc}\n這一集已經有 active attempt {attempt_id!r},而它的 settings "
                    "是「不指名來源」——直接改呼 podcast_episode(..., source_ids=[...]) 會被"
                    "拒絕(already has durable active attempt)。先 podcast_attempt_retract "
                    "掉它(純本機動作,不需要 abandon_in_flight:這顆要嘛從沒送出去、要嘛"
                    "遠端已經回報終態,manifest 自己就知道結果),再用 "
                    "podcast_episode(..., source_ids=[...]) 重生。"
                    # retract 不打 RPC(設計如此),所以它**看不到筆記本此刻已經超標**,
                    # 回傳的 safe_next_action 會依那顆 attempt 的 settings 說
                    # `podcast_series` —— 照做會再撞一次這道守門(會終止,但白跑一趟)。
                    # 講在這裡最便宜:擋下它的就是我們,我們知道筆記本超標(FINDING-2)。
                    "⚠️ retract 之後**不要**照它回傳的 safe_next_action 去呼 podcast_series"
                    " —— 它不打 RPC、看不到筆記本已經超標,照做會再被這道守門擋一次。"
                    "這個筆記本現在只能走指名版。"
                ),
            )
        except NotebookAccessDenied as exc:
            observed_state, action, extra = _classify_not_accepted_stop(exc)
            return partial(episode_n, attempt_id, observed_state, action, **extra)
        return None

    for episode_n in range(start, len(episodes) + 1):
        # series 在入口固定自己的帳號狀態；只有本呼叫的明確配額 failover 能更新它。
        # 每集重讀 global snapshot 會讓別的並行 request 在 EP1 期間 rotate 後，EP2 在
        # 沒有任何配額拒絕與稽核紀錄的情況下偷換帳號。
        plan = episodes[episode_n - 1]
        expected_title = plan["title"].strip()
        expected_brief_hash = hashlib.sha256(
            plan["brief"].encode("utf-8")
        ).hexdigest()
        snapshot = store.read()
        episode = next(
            (row for row in snapshot["episodes"] if row.get("episode") == episode_n),
            None,
        )
        attempt_id = (
            episode.get("active_attempt_id") or episode.get("output_attempt_id")
            if episode is not None
            else None
        )
        # 每集的第一個遠端操作先驗認證，涵蓋 cleanup、續跑、對帳與全新 dispatch；
        # 一集一次也取代各分支重複 probe，避免長季節中間失效時裸拋並丟掉既有結果。
        stop = await stop_if_auth_expired(episode_n, attempt_id, client, episode)
        if stop is not None:
            return stop
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

        # T3(P1,`scratchpad/verify-A1/r2_series_retract.py`):retract 中段集之後,
        # 這一集(還)沒有 output,但整季稍後的集數已經有——series 對它接下來要做的
        # 一定是重新 dispatch(全新一集 / not_accepted re-arm / failed-removed
        # supersede,`_series_will_redispatch()` 涵蓋的正是這三種),而 series 生不出
        # 帶 `source_ids` 的 settings,不指名就是讀整本筆記本,把後面集數的回錄音檔
        # 洩進這一集。**這一格 caps 修不了**——`_attempt_capabilities` 只拿單一
        # attempt/episode,看不到 sibling episode 的 output 證據,跨集判斷只能留在
        # series 這一層。
        if episode is not None:
            episode_has_output = episode.get(
                "output_attempt_id"
            ) is not None or has_hard_output_evidence(episode)
            active_attempt_id_for_leak_check = episode.get("active_attempt_id")
        else:
            episode_has_output = False
            active_attempt_id_for_leak_check = None
        if not episode_has_output:
            if active_attempt_id_for_leak_check is None:
                # 全新一集(或全部 attempt 都已是歷史紀錄):下一步必然是全新 dispatch。
                about_to_redispatch = True
                leak_action = ACTION_EPISODE
            else:
                _, attempt_for_leak_check = _attempt_record(
                    snapshot, episode_n, active_attempt_id_for_leak_check
                )
                about_to_redispatch = _series_will_redispatch(attempt_for_leak_check)
                # 已有 active attempt 時 `podcast_episode(source_ids=...)` 是死路
                # ——`_create_audio_attempt` 的 `_is_resendable_same_request` 會判定
                # settings 不同(series 建的 attempt 沒有 source_ids)而拒收
                # 「already has durable active attempt」。純本機的 retract 才是唯一
                # 打得通的出口:作廢它之後再用 podcast_episode 指名來源重生。
                leak_action = ACTION_RETRACT
            if about_to_redispatch and _later_episode_has_output(
                snapshot, episode_n, notebook_id
            ):
                if leak_action == ACTION_EPISODE:
                    next_step = (
                        f"episode {episode_n} 之後已有集數產出正式輸出或上傳回錄——這一集"
                        "還沒有 attempt,series 對它的下一步是全新 dispatch,不指名"
                        "來源就是讀整本筆記本,會把後面集數的回錄音檔洩進這一集。"
                        f"改用 {ACTION_EPISODE}(..., source_ids=[...]) 指名這一集"
                        "自己的來源(以及需要的回錄音檔)生成。"
                    )
                else:
                    next_step = (
                        f"episode {episode_n} 之後已有集數產出正式輸出或上傳回錄——這一集"
                        "現有的 attempt 即將被 series 重新 dispatch(not_accepted "
                        "re-arm 或 failed/removed supersede),不指名來源一樣是讀"
                        f"整本筆記本。先 {ACTION_RETRACT}(純本機,不需要 "
                        "abandon_in_flight)作廢它,再用 "
                        f"{ACTION_EPISODE}(..., source_ids=[...]) 指名來源重生;"
                        f"**不要**改呼 {ACTION_SERIES},它一樣會讀整本筆記本。"
                    )
                return partial(
                    episode_n,
                    active_attempt_id_for_leak_check,
                    "later_episode_has_output",
                    leak_action,
                    next_step=next_step,
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
                except RemoteArtifactUnverifiableError:
                    # T6(P2,`scratchpad/verify-A2/07_artifact_gone.py`):已完成集的
                    # 正式輸出在遠端被刪掉(或改名到面目全非)之後,drift 複驗撞到
                    # 這個具名例外——過去是裸 `RuntimeError`,連 `safe_next_action`
                    # 都沒有,already-succeeded 的 `run_results` 整批跟著呼叫端的
                    # 自動化一起炸掉,整季永久卡死。翻成結構化 partial:這顆是
                    # is_output,caps 給的是 `podcast_attempt_retract`(免旗標,純
                    # 本機);`next_step` 額外提 `start=` 逃生口——這一集卡住不代表
                    # 後面的集數也走不下去。新契約詞:
                    # `observed_state="output_unverifiable"`。
                    current = store.read()
                    current_episode, stopped_attempt = _attempt_record(
                        current, episode_n, output_attempt_id
                    )
                    caps = _attempt_capabilities(
                        current_episode, stopped_attempt, output_attempt_id
                    )
                    return partial(
                        episode_n,
                        output_attempt_id,
                        "output_unverifiable",
                        caps["safe_next_action"],
                        next_step=(
                            _attempt_next_step(caps)
                            + f" 這一集卡住不影響其他集:用 start={episode_n + 1} "
                            "可以先跳過它、讓後面的集數繼續推進。"
                        ),
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
                except _TRANSIENT_TRANSPORT_ERRORS:
                    # 已完成集的 drift 複驗途中傳輸失敗:manifest checkpoint 仍是真相,
                    # 回結構化 partial 讓呼叫端重跑同一組 series,別裸拋掉整季進度回報
                    # (與下方 active attempt 分支同一處置)。
                    return partial(
                        episode_n,
                        output_attempt_id,
                        "verification_incomplete",
                        ACTION_SERIES,
                    )
                try:
                    _promote_attempt_output(
                        store, episode_n, output_attempt_id, repaired
                    )
                except PromotionRefusedError as exc:
                    return promotion_refused_stop(exc)
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
                    # T5(P1,`scratchpad/verify-G/g2_legacy_audio_missing.py`):交棒
                    # resume 之前,`_ensure_resume_attempt` 的 seed 條件(把這筆
                    # source 的身分接回來、不重複上傳)要求
                    # `feedback_source_adopted_at` 也存在——只有
                    # `podcast_attempt_adopt` 會寫這個人工標記
                    # (`_promote_attempt_output` 明講不寫)。真實的 flat v1
                    # manifest(從沒跑過 adopt,只是 episode 級 `feedback_source_id`
                    # 剛好記著)沒有這個標記,照做 resume 會把它當全新 upload,
                    # 對同名 source 重複 add_file 一次。source_verified=True 在
                    # 上面已經用 id+label 對帳驗過這筆 source 有效,所以先明確
                    # `podcast_attempt_adopt`(帶 feedback_source_id,寫上標記)
                    # 就是安全的續集入口;adopt 完之後再由它自己的 caps 導向
                    # resume(見 T2)。**不能改弱 seed 條件本身**——語意是
                    # 「這筆 source 的身分經過明確確認」,不是「manifest 剛好有這個
                    # 欄位」。
                    adopted_at = episode.get("feedback_source_adopted_at")
                    if isinstance(adopted_at, str) and adopted_at:
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
                        "legacy_audio_missing",
                        ACTION_ADOPT,
                        feedback_source_id=legacy_source_id,
                        artifact_id=legacy_artifact_id,
                        next_step=(
                            f"這筆回錄 source({legacy_source_id!r})已經對帳驗證過,"
                            "但這一集還沒有明確 adopt 過(缺 "
                            "feedback_source_adopted_at)——直接 resume 會把它當成"
                            "全新 upload,對同名 source 重複上傳一次。先呼叫 "
                            f"{ACTION_ADOPT}(manifest_path=..., episode_n={episode_n}, "
                            f"feedback_source_id={legacy_source_id!r}) 明確綁定"
                            f"(不要傳 artifact_id——這裡的 artifact_id 只是告訴你"
                            "這一集對應哪顆音檔,adopt 認的是 feedback_source_id),"
                            f"adopt 完再照它回傳的 safe_next_action 續跑(通常是 "
                            f"{ACTION_RESUME},帶回上面同一個 artifact_id)。"
                        ),
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
                # 這三種狀態接下來都會產生新的 dispatch(`prepared` 直接送、
                # `not_accepted` 先 re-arm 再送、`failed`/`removed` 建 superseding
                # attempt 再送),所以兩道守門都擺在它們的**共同上游**(判準集中在
                # `_series_will_redispatch()`)、在任何 manifest mutation 之前。擺在
                # 各自的 dispatch 點會先 re-arm/先建 attempt 才拒絕,留下半成品狀態、
                # 還讓 `attempt_count` 白長一格(v0.7.1 驗收 F-7:抹完的 manifest 看
                # 起來就像分類從未生效過)。已經 dispatch 出去、只等 finalize 的
                # attempt 不在這裡面 —— 它不會再生成一次,擋它只會把一集卡在半路。
                #
                # **接手守門曾經只掛在 `prepared`/`not_accepted` 兩格**,於是
                # `accepted`+`failed`(送出去了、伺服器判定失敗)的指名來源 attempt
                # 直接落進下面的 supersede 分支,被換成不指名的新 attempt、改讀整本
                # 筆記本 —— 兩次 dispatch 送出 `[["src-1"], None]`,而工具回報
                # `complete=True`。`_attempt_next_step()` 的 settled 分支註解早就點名
                # 過這個形狀,但只防了訊息、沒防程式路徑。
                if _series_will_redispatch(attempt):
                    # **筆數守門排在歸屬之前。** 反過來(純本機檢查先跑、省一趟 RPC)看起來
                    # 更划算,但它把一條原本回結構化 `too_many_sources` 停點的路改成裸拋
                    # ——`accepted`+`failed` 的外來 attempt 撞上超標筆記本時,舊行為是
                    # 回 partial(已跑完的集仍在 `episodes` 裡),新行為會丟掉整批
                    # `run_results`(Codex 獨立複審實跑抓到)。兩道守門都不改 manifest,
                    # 順序只影響「先講哪個理由」,那就讓保住進度的那個先講。
                    stop = await refuse_if_too_many_sources(
                        episode_n, active_attempt_id, client
                    )
                    if stop is not None:
                        return stop
                    # **這一集已經有完成的輸出時,歸屬不是該講的那件事。**
                    # supersede 會走到 `_create_audio_attempt`,它有一道「拒絕靜默覆蓋
                    # 既有輸出」的守門 —— 那個理由更根本,而歸屬訊息教的
                    # 「retract 之後重生」對一集**已發布**的節目是破壞性建議。把歸屬
                    # 擺到共同上游時一併把它搶先了(`test_published_legacy_output_
                    # blocks_implicit_supersede_after_failed_resume` 逐字抓到)。
                    # 只讓終態那條路讓位:`prepared`/`not_accepted` 不經過
                    # `_create_audio_attempt`(re-arm 後直接送出),歸屬仍要在這裡驗,
                    # 否則等於為它們開一條沒有守門的路。
                    defers_to_output_guard = remote_state in _TERMINAL_REMOTE and (
                        has_hard_output_evidence(episode)
                    )
                    if not defers_to_output_guard:
                        _assert_series_owns_attempt(
                            episode,
                            attempt,
                            series_settings,
                            episode_n,
                            active_attempt_id,
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
                    episode, attempt = _attempt_record(
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
                    episode, attempt = _attempt_record(
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
                    # `episode` 必須跟著 `attempt` 一起從最新 snapshot 取(上面兩處都
                    # 已改成接住它)——用 supersede 之前的舊 dict,`active_attempt_id`
                    # 還指著被取代的那顆,caps 會把新 attempt 判成「歷史紀錄」。
                    _assert_series_owns_attempt(
                        episode, attempt, series_settings, episode_n, active_attempt_id
                    )
                    # baseline / 記帳 / 實際送出三者同源,理由同 `_run_episode`
                    # 那條路徑(並行 rotate 會讓 manifest 記 A、實際 B 送出)。
                    dispatch_account, dispatch_client = account, client
                    # 來源筆數守門**不在這裡**:它在上面 re-arm/supersede 的共同上游,
                    # 也就是任何 manifest mutation 之前(`refuse_if_too_many_sources`)。
                    # 曾經擺在這一行,結果是「先把 not_accepted re-arm 成 prepared、
                    # 先建好 superseding attempt,然後才拒絕送出」——半成品狀態留在
                    # manifest 裡,而呼叫端唯一的出路訊息還指向一支會拒收它的工具。
                    # **純本地轉換要在 durable claim 之前做完。** 兩個位置都錯過:
                    # 留在 closure 內的話,打錯 enum 的 `ValueError` 會落進共用迴圈的泛用
                    # except,把 attempt 推進 `acceptance_unknown` 而遠端一次都沒被碰到;
                    # 搬到 closure 外但在 `_claim_prepared_dispatch` **之後**,則會留下
                    # `dispatch.status="dispatching"` 這個同樣是假的 durable 狀態(獨立
                    # 複審用 probe 證明這條路真的可達 —— `_audio_settings` 只存原始字串,
                    # 所以 ownership 等值檢查放得過去)。擺在這裡,轉換失敗時 manifest
                    # 一個字都還沒被動。`_run_episode` 的 `resolved_audio_*` 同理。
                    resend_audio_format = to_audio_format(audio_format)
                    resend_audio_length = to_audio_length(audio_length)
                    baseline = await dispatch_client.artifacts.list(
                        notebook_id, artifact_type=ArtifactType.AUDIO
                    )
                    claimed = _claim_prepared_dispatch(
                        store,
                        episode_n,
                        active_attempt_id,
                        [row.id for row in baseline],
                        account=dispatch_account,
                        wait_timeout=wait_timeout,
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
                            audio_format=resend_audio_format,
                            audio_length=resend_audio_length,
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
                            notebook_id=notebook_id,
                        )
                        account = dispatch_account
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
                        observed_state, action, extra = _classify_not_accepted_stop(exc)
                        return partial(
                            episode_n,
                            active_attempt_id,
                            observed_state,
                            action,
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
                    reconciled = await _podcast_episode_reconcile(
                        client,
                        manifest_path,
                        episode_n=episode_n,
                        attempt_id=active_attempt_id,
                        wait_timeout=wait_timeout,
                        auth_probed=True,
                    )
                    if reconciled["observed_state"] != "accepted":
                        # P2:零候選那格的出路寫在 `next_step`(見
                        # `podcast_episode_reconcile` 的 F3 修法),那是逃離 self-loop
                        # 的唯一結構化指引——但 `partial(**extra)` 本來就吃額外欄位,
                        # 這裡只轉傳了 candidate_artifact_ids,漏轉 next_step 純粹是
                        # 沒接上(AGENTS.md 點名的「series 有兩條路徑」老病的又一次
                        # 現形:直接呼叫 reconcile 的呼叫端看得到 next_step,經由
                        # series 重包的這條路卻看不到)。
                        extra = {
                            "candidate_artifact_ids": reconciled.get(
                                "candidate_artifact_ids", []
                            )
                        }
                        if "next_step" in reconciled:
                            extra["next_step"] = reconciled["next_step"]
                        return partial(
                            episode_n,
                            active_attempt_id,
                            reconciled["observed_state"],
                            reconciled["safe_next_action"],
                            **extra,
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
                except _TRANSIENT_TRANSPORT_ERRORS:
                    current = store.read()
                    current_episode, stopped_attempt = _attempt_record(
                        current, episode_n, active_attempt_id
                    )
                    upload_state = stopped_attempt.get("finalize", {}).get(
                        "feedback_source_upload", {}
                    ).get("status")
                    # T1:三種 unresolved upload 狀態一律走 caps(單一事實來源),不再
                    # 各自手寫 ACTION_SERIES——`acceptance_unknown` 之前寫死 series,
                    # 但 caps 算出的 resume 才是這個狀態真正走得通的路(can_resume 為
                    # 真,續完 finalize 不必整季重跑);`reconciliation_ambiguous`
                    # 之前不帶候選,現在由 caps 一併帶出。remote_status 的 fallback
                    # (這三種以外的狀態)維持原樣,不擴大改動範圍。
                    if upload_state in (
                        "acceptance_unknown",
                        "reconciliation_ambiguous",
                    ):
                        caps = _attempt_capabilities(
                            current_episode, stopped_attempt, active_attempt_id
                        )
                        return partial(
                            episode_n,
                            active_attempt_id,
                            upload_state,
                            caps["safe_next_action"],
                            candidate_source_ids=caps["candidate_source_ids"],
                            next_step=_attempt_next_step(caps),
                        )
                    observed = stopped_attempt["remote"]["status"]
                    return partial(
                        episode_n,
                        active_attempt_id,
                        observed,
                        ACTION_SERIES,
                    )
                except RuntimeError:
                    current = store.read()
                    current_episode, stopped_attempt = _attempt_record(
                        current, episode_n, active_attempt_id
                    )
                    upload_state = stopped_attempt.get("finalize", {}).get(
                        "feedback_source_upload", {}
                    ).get("status")
                    # T1:刪掉手寫的 `ACTION_ADOPT if ambiguous else ACTION_SERIES`
                    # ——中央 caps 現在就會給正確答案(ambiguous+候選非空 → adopt,
                    # 且候選 / next_step 都由 caps 一併帶出),不必在呼叫點另組一份。
                    if upload_state in (
                        "acceptance_unknown",
                        "reconciliation_ambiguous",
                    ):
                        caps = _attempt_capabilities(
                            current_episode, stopped_attempt, active_attempt_id
                        )
                        return partial(
                            episode_n,
                            active_attempt_id,
                            upload_state,
                            caps["safe_next_action"],
                            candidate_source_ids=caps["candidate_source_ids"],
                            next_step=_attempt_next_step(caps),
                        )
                    raise
                try:
                    _promote_attempt_output(
                        store, episode_n, active_attempt_id, result
                    )
                except PromotionRefusedError as exc:
                    return promotion_refused_stop(exc)
                run_results.append(result)
                continue

        # candidate range 內完全沒有 attempt 才能產生新的遠端副作用。
        # 全新一集的守門。`_run_episode` 內部還有一道(那道是給 `podcast_episode` 直呼
        # 用的,series 走到那裡時會冗餘再打一次唯讀 list —— 一集要跑二十分鐘,一趟
        # list 換「兩個入口各自守得住」很划算)。這裡先擋是因為**例外分類走不通**:
        # 守門在建 attempt 之前拋,而下面的 `except (RuntimeError, ...)` 會先呼叫
        # `_active_attempt_or_reraise` —— 沒有 attempt 就原樣重拋,`NotebookAccessDenied`
        # 於是裸奔出去,連前面幾集的 run_results 都一起丟掉。
        stop = await refuse_if_too_many_sources(episode_n, None, client)
        if stop is not None:
            return stop
        try:
            result, account, client = await _run_episode(
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
                account=account,
                client=client,
                manifest_path=manifest_path,
            )
        except TooManySourcesError as exc:
            # 守門擋在建 attempt 之前,所以這一集**沒有** attempt —— 下面每個 handler
            # 都用的 `_active_attempt_or_reraise` 在這裡不適用,`attempt_id` 回 None。
            # 而**必須翻成結構化停點**:裸拋會把前面幾集已經跑完的 run_results 整份
            # 丟掉,那正是 F-4 修過的形狀(見 inline dispatch 分支那段長註解)。
            return partial(
                episode_n,
                None,
                "too_many_sources",
                ACTION_EPISODE,
                error=str(exc),
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
            observed_state, action, extra = _classify_not_accepted_stop(exc)
            return partial(
                episode_n,
                attempt_id,
                observed_state,
                action,
                **extra,
            )
        except _TRANSIENT_TRANSPORT_ERRORS:
            current = store.read()
            attempt_id = _active_attempt_or_reraise(current, episode_n)
            current_episode, stopped_attempt = _attempt_record(
                current, episode_n, attempt_id
            )
            if stopped_attempt["remote"].get("artifact_id") is None:
                # 這一格是「artifact 都還沒接受」——與回錄 source 上傳無關,維持原判準。
                observed = stopped_attempt["dispatch"]["status"]
                needs_artifact_reconcile = observed == "acceptance_unknown"
                action = ACTION_RECONCILE if needs_artifact_reconcile else ACTION_SERIES
                return partial(episode_n, attempt_id, observed, action)
            upload_state = stopped_attempt.get("finalize", {}).get(
                "feedback_source_upload", {}
            ).get("status")
            # T1:回錄 source 上傳卡在這三種 unresolved 狀態時走 caps,理由與上面
            # active-attempt 分支那兩個 handler 相同(單一事實來源、候選一併帶出)。
            if upload_state in ("acceptance_unknown", "reconciliation_ambiguous"):
                caps = _attempt_capabilities(
                    current_episode, stopped_attempt, attempt_id
                )
                return partial(
                    episode_n,
                    attempt_id,
                    upload_state,
                    caps["safe_next_action"],
                    candidate_source_ids=caps["candidate_source_ids"],
                    next_step=_attempt_next_step(caps),
                )
            observed = stopped_attempt["remote"]["status"]
            return partial(episode_n, attempt_id, observed, ACTION_SERIES)
        run_results.append(result)

    return {
        "notebook_id": notebook_id,
        "episodes": run_results,
        "manifest": manifest_path,
        "complete": True,
    }
