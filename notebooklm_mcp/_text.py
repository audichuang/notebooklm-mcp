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
#   - `(?<![A-Za-z0-9_\]])` —— 緊貼**識別碼**的方括號是索引不是引用:`arr[0]`、`a[1][2]`
#     原本會被清成 `arr`、`a`,而句子仍然通順,所以沒有任何東西會提醒呼叫端內容被動過。
#     **界線刻意只列 ASCII**:Python 的 `\w` 涵蓋 CJK,用 `\w` 會連「重點[1]。」這種
#     最常見的中文引用形狀一起擋掉 —— 而這支的用途正是產繁中公開文案,那是比原本的
#     誤傷更糟的漏清(v0.9.26 獨立複審抓到,已加測試)。
#   - **跑到不動點為止**:連續引用 `[1][2]` 的第二個標記前面是 `]`,單趟會留下
#     `見[2]` 這種既沒清乾淨又被改壞的字串。第一趟清掉 `[1]` 之後 `[2]` 前面就變成
#     CJK/空白,第二趟才清得掉;而 `a[1][2]` 兩個都被識別碼擋住,第一趟就是不動點。
# ponytail: 行文中獨立出現的數值區間(`價格 [100-200] 元`)與真引用 `[2-5]` 完全同形,
#           分不出來也不打算分 —— 要保住就傳 `strip_citations=False`。
#           `Transformer[1]` 這種 ASCII 詞緊貼標記的也一併保留(與 `arr[0]` 同形)。
_CITATION_RE = re.compile(r"[ \t]*(?<![A-Za-z0-9_\]])\[[\d,\s\-–]+\]")


def strip_citations(text: str) -> str:
    """清掉 NotebookLM 的引用標記,**跑到不動點**(見 `_CITATION_RE` 上面的界線說明)。"""
    while True:
        stripped = _CITATION_RE.sub("", text)
        if stripped == text:
            return text
        text = stripped


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
#   - **只吃 `**` / `***`,不吃單星號**(v0.9.26)。`(?!\s)` / `(?<!\s)` 這道界線靠的是
#     空白,而 CJK 不用空格分詞 —— 於是「A*搜尋」與「B*樹」的兩個星號湊成一對,整段
#     被吃成「A搜尋」「B樹」(實跑重現)。事故本身(v0.9.14 FINDING-D)的形狀是
#     `**粗體**`,單星號從來不是;放掉 `*斜體*` 換掉整類演算法名/指標的誤傷划算得多,
#     與上面「只吃星號不吃底線」是同一個取捨。
_EMPHASIS_RE = re.compile(r"(?<!\*)(\*{2,3})(?!\s)([^\n*]+?)(?<!\s)\1(?!\*)")


def strip_inline_emphasis(text: str) -> str:
    """拿掉成對的星號強調,保留被包住的內容(`**粗體**` → `粗體`)。

    給公開文案用:`render()` 沒處理 inline 標記,而 show notes 會原樣進 RSS。
    孤星號(`2 * 3`、行首條列)與底線刻意不動 —— 見 `_EMPHASIS_RE` 上面的界線說明。
    """
    return _EMPHASIS_RE.sub(r"\2", text)


def norm(text: str) -> str:
    """NotebookLM 對 CJK 擷取會插空格;關鍵詞比對前兩邊都拔掉全部空白。"""
    return "".join(text.split())
