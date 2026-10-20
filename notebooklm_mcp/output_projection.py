"""Episode 級 Output 投影(GLOSSARY「Output 投影與集的身分」)的單一模組。

投影是 episode 上指向「目前正式輸出」的那幾個欄位:promotion 寫入、retract 整批 pop、
各入口的「既有輸出」述詞讀它。過去鍵集合與 promotion 在 `attempt_state`、pop 寫在
retract 工具裡、述詞分散在 `audio_finalize` 與 `attempt_admission`,改一處漏一處。
現在鍵集合、寫(`apply_promotion`)、拔(`pop_output_projection`)、讀(述詞)都在這裡。

不放在這裡的東西:
- promotion 的守門(已 retract／owned-by-another／legacy 衝突)——legacy 那道要用
  `attempt_capabilities`,留在 `tools_podcast._promote_attempt_output`。
- retract 的 tombstone 組裝(要讀 finalize checkpoint,形狀只在 `audio_finalize`)與
  `active_attempt_id` 指標的處置——留在 `podcast_attempt_retract`。
- adopt legacy 分支對 `feedback_source_id` 的寫入(與 `previous_feedback_source_ids`
  歷史、清理義務交錯在同一段)——留在 `podcast_attempt_adopt`。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from .attempt_state import _refuse_retracted

# Episode 級 output 投影:promotion(本模組 `apply_promotion`)與 adopt 寫入,
# `podcast_attempt_retract` 整批 pop 進 `retraction.retracted_output`(ADR-0009:pop,
# 不設 None——殘留的 None 會擋住取代版用 setdefault 寫回真值)。`task_id` 是 legacy
# 路徑寫的同義欄位,新路徑不寫,但作廢時一樣要清。
OUTPUT_PROJECTION_KEYS = (
    "output_attempt_id",
    "artifact_id",
    "task_id",
    "mp3_path",
    "published_at",
    "feedback_source_id",
    "feedback_source_adopted_at",
)
# 集的身分:promotion 只 setdefault、retract 不碰——取代版不得改標題(ADR-0009)。
EPISODE_IDENTITY_KEYS = ("title", "label")
# 「已生成或發布」的 legacy 硬證據:投影的子集,刻意不是全部——`output_attempt_id`
# 由各入口自己看(pointer 的真值/is-not-None 語意各入口不同),`feedback_source_*`
# 是 continuity 證據不是輸出證據。子集關係由 tests/test_attempt_state.py 鎖住。
HARD_OUTPUT_EVIDENCE_KEYS = ("artifact_id", "task_id", "mp3_path", "published_at")


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
    `tools_podcast._promote_attempt_output`——legacy 那道要用 `attempt_capabilities`。
    """
    _refuse_retracted(attempt, attempt_id)
    attempt["remote"]["status"] = "completed"
    attempt["remote"]["observed_at"] = datetime.now(UTC).isoformat()
    episode["artifact_id"] = output["artifact_id"]
    episode["mp3_path"] = os.path.abspath(output["mp3_path"])
    episode["output_attempt_id"] = attempt_id
    for key in EPISODE_IDENTITY_KEYS:
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


def pop_output_projection(episode: dict) -> dict:
    """retract 正常分支:整批 pop 投影,回傳被拔下的值(存進 `retracted_output`)。

    pop 而非設 None:`apply_promotion` 用 setdefault 寫 published_at,留一個 None 值會讓
    取代版補不回真正的產製時間(publish 有 fallback、不會爆,只會靜默發出假日期,而
    podcast-lab 的完成門會先擋下來)。
    """
    return {key: episode.pop(key) for key in OUTPUT_PROJECTION_KEYS if key in episode}


def drop_own_residue(episode: dict, retracted_output: dict) -> None:
    """retract 冪等重呼:只清「值仍等於當初被作廢那份」的欄位。

    ownership(output 還是自己或空)由呼叫端先判——此時 output 可能已經是取代版 B,
    無條件重跑 pop 會毀掉取代版。
    """
    for key, value in retracted_output.items():
        if key in episode and episode[key] == value:
            del episode[key]


# ---- 既有輸出的述詞(各入口刻意不同,集中命名,不是統一) -------------------------------


def has_hard_output_evidence(episode: dict) -> bool:
    """已生成或發布的 legacy 證據；即使後來出現 attempt 也不可隱式覆寫。"""
    return any(
        isinstance(episode.get(field), str) and episode[field].strip()
        for field in HARD_OUTPUT_EVIDENCE_KEYS
    )


def has_durable_output_evidence(episode: dict) -> bool:
    """既有輸出留下任一耐久證據時，不得覆寫相容路徑。"""
    if has_hard_output_evidence(episode):
        return True
    # 被 retract 的那版本機 mp3 就是拒收證據,不能讓取代版蓋掉它——否則同一集有沒有
    # 保留證據會取決於「那時剛好生過封面沒有」,不可預測。取代版一律走
    # attempts/<attempt_id>/ 子目錄(見 audio_finalize 的 mp3_path 選擇)。
    if episode.get("retracted_attempt_ids"):
        return True
    cover = episode.get("cover_path")
    if isinstance(cover, str) and cover.strip():
        return True
    label = episode.get("label")
    return not episode.get("attempts") and isinstance(label, str) and bool(label.strip())


def has_output_pointer_or_hard(episode: dict) -> bool:
    """`output_attempt_id` 已指向某顆,或有 legacy 硬證據。series 的跨集洩漏判斷用。"""
    return episode.get("output_attempt_id") is not None or has_hard_output_evidence(episode)


def output_blocks_generate(episode: dict) -> bool:
    """generate 的「拒絕靜默覆蓋」:比 series 多一格 legacy preparatory,且 pointer 用真值。"""
    legacy_preparatory_output = not episode.get("attempts") and has_durable_output_evidence(episode)
    return bool(
        episode.get("output_attempt_id")
        or has_hard_output_evidence(episode)
        or legacy_preparatory_output
    )
