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
)


def _missing_terms(path: Path, terms: tuple[str, ...]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [term for term in terms if f"`{term}`" not in text]


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
