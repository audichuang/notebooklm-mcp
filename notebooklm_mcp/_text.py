"""共用文字處理(零依賴,供 tools_basic / tools_artifacts 匯入,避免循環 import)。"""
from __future__ import annotations

import re

# NotebookLM chat 的引用標記([1] / [3, 4] / [2-5]);show notes 發布前要清掉。
# 只此一份 regex,chat_ask 與 episode_set_description 共用,避免兩處漂移。
#
# 連標記**前面**的水平空白一起吃(v0.9.13 驗收:清完留下「…時間 。」,標點前多一格,
# 而這支的用途正是產公開文案)。兩個刻意的界線:
#   - 只吃前面 —— 兩邊都吃會把 `see [1] and` 黏成 `seeand`。
#   - 只吃 space/tab,不吃 `\s` —— 行首的引用會連著前一行的換行被清掉,markdown 結構拉平。
_CITATION_RE = re.compile(r"[ \t]*\[[\d,\s\-–]+\]")


# inline 的 markdown 強調。**這是 v0.9.14 真實驗收 FINDING-D**:skill
# `tool-reference.md` 寫「`strip_citations=true` 回的是純文字,沒有 Markdown 強調」,
# 但 `answer_document.render()` 只拿掉 **block 級**標記(`###` 標題、`*` 條列),
# inline 的 `**粗體**` 原樣留著 —— 而那串字正是
# chat_ask → episode_set_description → manifest → 公開 Apple Podcast `<description>`
# 的來源,`**` 會逐字出現在 RSS 裡(實測)。
#
# **只吃星號,不吃底線**:`_斜體_` 的形狀與識別碼撞得太兇
# (`NOTEBOOKLM_AUTH_JSON`、`source_id`、`snake_case`),清掉會毀掉正文。
# skill 文件那句「`_斜體_` 之類」要跟著改成精確描述,不要反過來讓實作去追文件。
#
# 界線(每一條都對應一種誤傷):
#   - `(?!\s)` / `(?<!\s)` —— 內容不可以用空白開頭或結尾,擋掉條列符號 `*   項目`。
#   - `(?<!\*)` / `(?!\*)` —— 不從一串星號的中間切,`***x***` 整組一起吃。
#   - 內容非空且不跨行:`[^\n*]` 讓 `a * b * c` 這種算術文字不被當成強調。
_EMPHASIS_RE = re.compile(r"(?<!\*)(\*{1,3})(?!\s)([^\n*]+?)(?<!\s)\1(?!\*)")


def strip_inline_emphasis(text: str) -> str:
    """拿掉成對的星號強調,保留被包住的內容(`**粗體**` → `粗體`)。

    給公開文案用:`render()` 沒處理 inline 標記,而 show notes 會原樣進 RSS。
    孤星號(`2 * 3`、行首條列)與底線刻意不動 —— 見 `_EMPHASIS_RE` 上面的界線說明。
    """
    return _EMPHASIS_RE.sub(r"\2", text)


def norm(text: str) -> str:
    """NotebookLM 對 CJK 擷取會插空格;關鍵詞比對前兩邊都拔掉全部空白。"""
    return "".join(text.split())
