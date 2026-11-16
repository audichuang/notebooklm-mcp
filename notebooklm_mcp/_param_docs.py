"""MCP 參數的 schema 描述(`tools/list` 的 `description`)。

集中一張表,不在每個簽名寫 `Annotated[..., Field(...)]`:同一個參數在 28 支工具裡語意相同,
寫一次就好,簽名與直接呼叫也完全不動。`tests/test_tool_schema_docs.py` 鎖住不漏也不孤兒。

每句都算進 `tools/list` 預算(見 `tests/test_tool_payload_budget.py`),所以只寫呼叫端看名字猜不到的事;
怎麼選工具、事故與流程留給 docstring 與 skill。

`apply` 也順手做兩件註冊後才做得到的瘦身:工具描述去掉 docstring 縮排、dict 回傳改成緊湊 JSON。
"""

from __future__ import annotations

import functools
import inspect

import pydantic_core

_TIMEOUT = "等待上限(秒)"
_FEED = "首發必填,之後沿用 manifest"
_DURABLE = "manifest 路徑;省略 = 沒有 durable 記錄的 standalone"
_DURABLE_WAIT = "等待上限(秒);會持久化,影響之後的對帳窗"
_OVERVIEW_FORMAT = "deep-dive / brief / critique / debate"
_OVERVIEW_LENGTH = "short / default / long"
_WAIT = "`wait=True` 等吃完再回;False 立即回,再看 `source_list`"
_ACCOUNT = "`research_start` 回傳的 account,原樣傳"

COMMON: dict[str, str] = {
    "notebook_id": "筆記本 id",
    "manifest_path": "該節目 series_manifest.json 的路徑",
    "episode_n": "集號,從 1 起算",
    "source_ids": "只讀這幾筆來源;省略 = 全部來源",
    "language": "語言代碼;省略 = zh_Hant",
    "wait_timeout": _TIMEOUT,
    "artifact_id": "artifact id",
    "wait": _WAIT,
    "task_id": "start 工具回傳的 task_id",
    "source_id": "來源 id,取自 `source_list`",
    "output_dir": "本機輸出目錄",
    "audio_format": _OVERVIEW_FORMAT,
    "audio_length": _OVERVIEW_LENGTH,
    "attempt_id": "要操作的 attempt",
}

BY_TOOL: dict[tuple[str, str], str] = {
    ("episode_set_description", "description"): "show notes 內容",
    ("episode_set_description", "strip_citations"): "清 `[n]` 引用與 inline 粗體",
    ("episode_set_publication_state", "state"): "`deferred`,或 null 解除",
    ("episode_set_publication_state", "reason"): "設 deferred 時必填,解除時不可傳",
    ("generate_slides", "instructions"): "給生成模型的額外要求",
    ("generate_slides", "slide_format"): "detailed / presenter",
    ("generate_slides", "slide_length"): "default / short",
    ("artifact_download_slides", "artifact_id"): "已生成的 slide_deck id",
    ("artifact_revise_slide", "artifact_id"): "先讀 manifest 的 `slides_artifact_id`",
    ("artifact_revise_slide", "slide_index"): "要改的頁,0 起算",
    ("artifact_revise_slide", "prompt"): "這一頁要怎麼改",
    ("generate_report", "report_format"): "study_guide / briefing_doc / blog_post / custom",
    ("generate_report", "extra_instructions"): "額外要求;custom 不吃",
    ("generate_report", "custom_prompt"): "custom 必填,其他 format 不可傳",
    ("artifact_download_report", "artifact_id"): "已生成的講義 id",
    ("artifact_download_report", "report_format"): "只影響 manifest 標記,傳當初的值",
    ("auth_check", "all_slots"): "量 pool 每一槽(N 次 RPC);預設只量作用中那槽",
    ("notebook_create", "title"): "筆記本標題",
    ("source_add_url", "url"): "網址或 YouTube 連結",
    ("source_add_text", "title"): "來源名稱",
    ("source_add_text", "content"): "來源全文",
    ("source_add_file", "file_path"): "本機檔案路徑",
    ("source_add_file", "mime_type"): "傳了就原樣送,不自動包成 .md",
    ("source_add_file", "title"): "來源名稱;省略 = 檔名",
    ("generate_audio", "instructions"): "給生成模型的額外要求",
    ("artifact_list", "kind"): "只列這一類;省略 = 全部",
    ("artifact_wait", "task_id"): "`generate_*` 回的 task_id,不是 notebook_id",
    ("artifact_wait", "timeout"): _TIMEOUT,
    ("artifact_download_audio", "output_path"): "下載到哪個檔案",
    ("artifact_download_audio", "artifact_id"): "省略 = 最新一顆已完成的音檔",
    ("artifact_rename", "new_title"): "新標題",
    ("chat_ask", "question"): "要問的問題",
    ("chat_ask", "conversation_id"): "續問同一串;省略 = 新對話",
    ("chat_ask", "strip_citations"): "清引用標記與多數 Markdown",
    ("chat_ask", "include_references"): "回傳 references;False 省 token",
    ("source_fulltext", "max_chars"): "回傳內容上限;0 = 只回字數與命中",
    ("source_fulltext", "contains"): "在全文精確比對的字串",
    ("source_search", "query"): "要找哪些段落在談的問題",
    ("source_search", "limit"): "全域前 N 段,不是每筆 N 段;省略 = 全部",
    ("podcast_episode", "title"): "本集標題;連載節目寫成 `EP{NN}. 正文`",
    ("podcast_episode", "brief"): "本集 brief;用 bundle 時傳 null",
    ("podcast_episode", "output_dir"): "音檔下載目錄",
    ("podcast_episode", "prior_mp3_path"): "前集音檔,standalone 才可用,集號須 >= 2",
    ("podcast_episode", "wait_timeout"): _DURABLE_WAIT,
    ("podcast_episode", "manifest_path"): _DURABLE,
    ("podcast_episode", "input_bundle_path"): "frozen bundle 路徑,相對於 workspace_root",
    ("podcast_episode", "workspace_root"): "bundle 圍籬;省略 = manifest 的祖父目錄",
    ("podcast_episode_reconcile", "attempt_id"): "錯誤或停點回傳的 attempt_id",
    ("podcast_episode_reconcile", "wait_timeout"): "傳更大的值會放大候選窗(秒)",
    ("podcast_episode_resume", "title"): "與該集 attempt 相同的標題",
    ("podcast_episode_resume", "artifact_id"): "取自 attempt 或 reconcile／adopt 結果,不可盲猜",
    ("podcast_episode_resume", "output_dir"): "音檔下載目錄",
    ("podcast_episode_resume", "wait_timeout"): _DURABLE_WAIT,
    ("podcast_episode_resume", "manifest_path"): _DURABLE,
    ("podcast_attempt_adopt", "attempt_id"): "帶 artifact_id 時必填;舊 flat 集不傳",
    ("podcast_attempt_adopt", "artifact_id"): "與 feedback_source_id 恰好擇一",
    ("podcast_attempt_adopt", "feedback_source_id"): "與 artifact_id 恰好擇一",
    ("podcast_attempt_retract", "attempt_id"): "被拒收的 attempt",
    ("podcast_attempt_retract", "reason"): "拒收原因,必填",
    ("podcast_attempt_retract", "abandon_in_flight"): "宣告你查過遠端;哪些狀態要傳見描述",
    ("podcast_series", "episodes"): "`[{title, brief}]` 完整清單,集號由索引決定",
    ("podcast_series", "output_dir"): "音檔下載目錄",
    ("podcast_series", "start"): "執行下界,從 1 起算;不是重生旗標",
    ("podcast_series", "wait_timeout"): _DURABLE_WAIT,
    ("publish_series", "show_id"): "節目永久 id,決定 feed URL;" + _FEED,
    ("publish_series", "show_title"): _FEED,
    ("publish_series", "show_description"): _FEED,
    ("publish_series", "author"): _FEED,
    ("publish_series", "owner_name"): _FEED,
    ("publish_series", "owner_email"): _FEED,
    ("publish_series", "artwork_path"): "節目封面路徑;" + _FEED,
    ("publish_series", "category"): "Apple 分類;未曾設定 = Technology",
    ("publish_series", "explicit"): "Apple 的 explicit 標記",
    ("publish_series", "itunes_type"): "episodic / serial",
    ("publish_series", "notebook_id"): "重抓 mp3 的 fallback;各集自己的優先",
    ("publish_series", "return_episodes"): "只過濾回傳,不縮小發布範圍",
    ("publish_series", "require_slides"): "False = 整季不做簡報",
    ("publish_series", "require_report"): "False = 整季不做講義",
    ("feed_info", "show_id"): "節目 show_id",
    ("research_start", "query"): "搜尋字串;關鍵詞都要寫進來",
    ("research_start", "source"): "web / drive(deep 只支援 web)",
    ("research_start", "mode"): "fast / deep",
    ("research_wait", "timeout"): _TIMEOUT,
    ("research_wait", "max_report_chars"): "回報告本文的上限;0 = 只回字數",
    ("research_wait", "account"): _ACCOUNT,
    ("research_import", "urls"): "`research_wait` 回的候選 URL,原樣傳",
    ("research_import", "include_report"): "另收 deep 報告本身",
    ("research_import", "max_elapsed"): "匯入後輪詢對帳的上限(秒)",
    ("research_import", "account"): _ACCOUNT,
}


def _compact_result(fn):
    """dict 回傳先序列化成緊湊 JSON:FastMCP 對非字串一律 `indent=2`,實測多 14% token。

    用同一個 `pydantic_core.to_json(fallback=str)`,型別處理與 CJK 輸出都跟原本一致;
    非 dict(含例外)原樣交回 FastMCP。
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if isinstance(result, dict):
            return pydantic_core.to_json(result, fallback=str).decode()
        return result

    return wrapper


def apply(tools) -> None:
    """把描述寫進註冊好的 tool schema,並拿掉 pydantic 自動產生的 `title`。

    只動給呼叫端看的 schema;參數驗證走 FastMCP 的 fn_metadata,不讀這份。
    """
    for tool in tools:
        tool.description = inspect.cleandoc(tool.description or "")
        tool.fn = _compact_result(tool.fn)
        schema = tool.parameters
        schema.pop("title", None)
        for name, prop in schema["properties"].items():
            prop.pop("title", None)
            text = BY_TOOL.get((tool.name, name)) or COMMON.get(name)
            if text:
                prop["description"] = text
