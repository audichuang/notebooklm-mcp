"""Fail when MCP tool names drift from the notebooklm skill docs."""
from __future__ import annotations

import asyncio
from pathlib import Path

from notebooklm_mcp import app


ROOT = Path(__file__).resolve().parents[1]
CI_SKILL_DIR = ROOT / "audi-skill" / "notebooklm"
LOCAL_SKILL_DIR = Path("/home/user/research/audi-skill/notebooklm")
SKILL_DIR = CI_SKILL_DIR if CI_SKILL_DIR.exists() else LOCAL_SKILL_DIR
SKILL_MD = SKILL_DIR / "SKILL.md"
TOOL_REFERENCE = SKILL_DIR / "references" / "tool-reference.md"


async def _tool_names() -> list[str]:
    tools = await app.mcp.list_tools()
    return sorted(tool.name for tool in tools)


#: Contract term coverage(**不是**完整的 schema↔docs sync)。工具名沒變的改動——新參數、
#: 新回傳欄位——原本完全逃得過本 checker:v0.3.3 加了 `converted_from` /
#: `require_slides` / `require_report`,忘記寫文件 CI 照樣綠。這裡只硬性要求
#: tool-reference 提到這幾個字,不做 signature 解析。加新參數時把它加進來。
REQUIRED_CONTRACT_TERMS = (
    "converted_from",
    "require_slides",
    "require_report",
    "custom_prompt",     # generate_report 的 custom 格式;工具名沒變,只有新參數
    "include_report",    # research_import:報告 entry 沒有 URL,只能靠這個旗標指名
    "input_bundle_path", # podcast_episode 的凍結輸入;相對路徑契約呼叫端猜不到
    "workspace_root",    # v0.9.24 podcast_episode:圍籬由 host 宣告,show-root manifest 必傳
    "max_report_chars",  # research_wait 預設不回報告本文
    "source_ids",        # v0.5.0 起 generate_audio/slides/report 與 podcast_episode 可指名來源
    "already_shared",    # notebook_share_with_pool 的回傳:VIEWER 不算「已分享」是硬契約
    "abandon_in_flight", # podcast_attempt_retract:呼叫端宣告的外部知識,狀態推導不出來
    "notebook_access_denied",  # series 安全停點裡「要人工補分享」的那一種,不可原樣重呼
    "too_many_sources",  # v0.9.3 的來源筆數守門停點;safe_next_action 分兩種,照做才走得出去
    "blocking_attempt_ids",  # v0.9.10 加的 reconcile 回傳欄位(F5/F6 盲審):
                              # 工具名沒變,checker 原本看不到——加進來讓它從此看得到
    "safe_next_attempt_id",  # podcast_attempt_retract:委派給 sibling 時,執行
                              # safe_next_action 要用的目標身分(不是 attempt_id)
    "safe_next_artifact_id", # 同上,safe_next_action 是 podcast_episode_resume 時
                              # 帶的 artifact_id
    "candidate_source_ids",  # podcast_series 的 reconciliation_ambiguous 停點(回錄
                              # source 上傳歧義):podcast_attempt_adopt 必填
                              # feedback_source_id/artifact_id 之一,單教動作名執行不了
    "was_present",           # source_delete:查無此 id 時不發破壞性 RPC 也不 raise,
                              # 清理迴圈可以重放。`deleted` 單獨看分不出這兩種情形
    "source_cleanup_obligations",  # podcast_attempt_retract:safe_next_action 是
                              # source_delete 時要用的**兩個**參數(notebook_id + source_id)。
                              # stale_source_ids 涵蓋不到 gate 對帳後才撈到的候選
    "next_step",             # reconcile／adopt／retract／series 停點都回這個欄位,而它是
                              # capabilities 那幾個內部維度(cleanup_state /
                              # feedback_upload_unresolved)**唯一**的對外通道
    "itunes_type",           # publish_series 的季級設定:缺這個宣告時 Apple 當 episodic,
                              # 連載節目的集序會整個顛倒(實測 SAA/SAP 兩個 feed 都中)
    "source_cleanup_unresolved",  # podcast_attempt_retract:upload 還沒落盤時作廢會留下
                              # 身分未定的清理義務,safe_next_action 是 null 而重生會被
                              # 生成前的 gate 擋住 —— 不寫進文件,呼叫端會誤判成死路
    "auth_expired",           # podcast_series 改以安全停點回報 cookie 失效,不再裸 raise
    "account",                # research_start 回傳、wait/import 要原樣帶回:research
                              # session 綁發起帳號,pool 換人只會拿到 no_research —— 而
                              # 那個失敗形狀是「輪詢滿 timeout」,不寫進文件沒人查得動
    "publication_state",      # v0.9.18:host 寫在 manifest 集層的「刻意不公開」。工具名與
                              # 參數都沒變,純粹是 manifest 欄位契約 —— 正好是本 checker
                              # 最容易漏的那一類。缺席=照發、未知值 raise 都要寫進文件
    "deferred_episodes",      # 同上的回傳側:episode_count 會少掉被扣下的集數,不寫進
                              # 文件呼叫端只會看到「發布漏集」
    "reorder_published_at.py",  # v0.9.19:preflight 會擋下 pubDate 非單調的 manifest,而
                              # **擋下之後的修法只存在文件裡**(它是 repo 裡的腳本,不是
                              # 工具)。少了這一句,呼叫端撞牆後最可能去手改 JSON
                              # (ADR-0009 禁止)或把 published_at 改成 now() —— 那會毀掉
                              # 首發時間的稽核
    "EP{n:02d}.",             # v0.9.20:serial 標題必須自己帶集號。字串含句點,才
                              # 跟命名鐵律的 `EP{n:02d} {title}`(空格、沒句點)分得開
    "all_slots",              # auth_check 預設**只量作用中那一槽**,而配額
                              # failover 會在生成中途換帳號 —— 真正被用到的槽位可能
                              # 從頭到尾沒被探過。不寫進文件,呼叫端會拿 1/N 的綠燈
                              # 當「長跑前已經 fail-fast 過了」
    "refreshable",            # 同一支的回傳:`usable`(真 RPC,現在能不能用)與
                              # `refreshable`(PSIDTS 能不能 refresh)**是兩件事**。
                              # 合成一盞燈的話,prd 槽位 1 那種「可服役但不可 refresh」
                              # 會被讀成故障 —— 那個誤讀真的發生過,還引出過重登建議
    "regeneration_source_ids",  # 40ee6e7 加進 retract/series 停點回傳,但文件漏記了
                              # 半個月 —— 兩份 host 紀錄如實引用它,反而被拿著文件當
                              # 否定證據的稽核「修正」掉。回傳欄位漏寫的代價不只是
                              # 查不到,是**正確的用法會被當成幻覺**
    "sha_unverified_episodes",  # publish preflight 的 mp3 provenance 閘:驗不動的集
                              # (legacy 無 attempts / 舊 finalize 沒記 sha)放行但列在
                              # 這裡 —— 「沒驗」不明講,就會被讀成「驗過了」
    "retired",                # v0.9.24:manifest 頂層旗標,publish_series 讀完就擋。與
                              # publication_state 同類 —— 工具名與參數都沒變、純 manifest
                              # 欄位,正是本 checker 最容易漏的那一類
)


def _missing(tool_names: list[str], path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [name for name in tool_names if f"`{name}`" not in text]


#: `SKILL.md` 是**每次載入的路由層**,它教錯 = 主流程錯,references 修得再對也沒用。
#: 但它刻意精簡,不該背全部契約詞 —— 所以只硬性要求「照做會出錯」的那幾個。
#: (v0.9.12 教訓:`source_cleanup_obligations` 只補進 tool-reference,SKILL.md 仍教
#: `stale_source_ids`,而那個欄位在 gate 對帳後是空的 —— checker 只掃 tool-reference,
#: 整條從這個洞掉出去,CI 照樣綠。)
SKILL_MD_REQUIRED_TERMS = (
    "source_cleanup_obligations",
    "itunes_type",
    "next_step",
    "abandon_in_flight",
    "auth_expired",
    # `auth_check` 的射程。只補進 tool-reference 的話,路由層仍教「長流程前跑
    # auth_check」,agent 照做會拿**1/N 的綠燈**當「整個 pool 已經 fail-fast 過了」——
    # 而那正是這次改動要修掉的誤讀。與上面 v0.9.12 是同一個洞,所以釘在同一個地方。
    "all_slots",
    # v0.9.18:§Publish 的交付清單要求「每集音檔全綠才可發布」,deferred 集永遠不會全綠
    # —— 只補 tool-reference 的話,agent 會照主路由層停在清單那一步,根本走不到已經
    # 會正確跳過它的 publisher。這正是上面那個 v0.9.12 教訓的同一個洞。
    "publication_state",
    # v0.9.19:交付清單是 host 每次發布前逐集核對的地方,而「pubDate 隨集號遞增」是
    # **生成階段**就該守住的事(亂序生成之後才發現,已經要重排 11 集)。只補 tool-reference
    # 的話,清單全綠 → 發布 → 被 preflight 擋下,那一輪的生成順序已經無從補救。
    "published_at",
    # v0.9.20:title 生成時鎖死,缺前綴不能事後補。只補 tool-reference 的話,host 照
    # §Episodic 用裸標題生完整季,發布才被擋,整季 title 全部改不了。
    "EP{NN}.",
)


def _missing_terms(path: Path, terms: tuple[str, ...]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [term for term in terms if f"`{term}`" not in text]


#: **移除**一個參數/回傳欄位時,把它的名字放進來。上面兩張表只驗「必要詞存在」,從不驗
#: 「已刪掉的契約不存在」—— 於是 v0.9.18 真的踩到:`has_output` 回傳欄位在複審後被刪掉
#: (它會說謊),runtime 改乾淨了,skill 的 tool-reference 卻還留著範例與「照它判斷是否
#: 下架」的說明。照文件寫 `result["has_output"]` 直接 KeyError,而 CI 全綠。
#: 這裡刻意只做字串黑名單、不做 schema parser:被刪掉的欄位名是有限且已知的清單,
#: 而真正的失敗模式是「忘了刪文件」,不是「文件寫錯型別」。
REMOVED_CONTRACT_TERMS = (
    "has_output",
)


def _stale_terms(path: Path, terms: tuple[str, ...]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [term for term in terms if term in text]


async def main() -> int:
    missing_files = [path for path in (SKILL_MD, TOOL_REFERENCE) if not path.exists()]
    if missing_files:
        for path in missing_files:
            print(f"missing skill file: {path}")
        return 1

    names = await _tool_names()
    failures: list[str] = []
    for path in (SKILL_MD, TOOL_REFERENCE):
        missing = _missing(names, path)
        if missing:
            failures.append(f"{path}: missing {', '.join(missing)}")

    for path, terms in (
        (TOOL_REFERENCE, REQUIRED_CONTRACT_TERMS),
        (SKILL_MD, SKILL_MD_REQUIRED_TERMS),
    ):
        missing_terms = _missing_terms(path, terms)
        if missing_terms:
            failures.append(
                f"{path}: missing contract term(s) {', '.join(missing_terms)}"
            )

    for path in (SKILL_MD, TOOL_REFERENCE):
        stale = _stale_terms(path, REMOVED_CONTRACT_TERMS)
        if stale:
            failures.append(
                f"{path}: still documents removed contract term(s) {', '.join(stale)}"
            )

    if failures:
        print("\n".join(failures))
        return 1

    print(
        f"skill docs cover {len(names)} MCP tools "
        f"+ {len(REQUIRED_CONTRACT_TERMS)} contract terms "
        f"({len(SKILL_MD_REQUIRED_TERMS)} of them also required in SKILL.md)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
