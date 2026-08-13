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


def norm(text: str) -> str:
    """NotebookLM 對 CJK 擷取會插空格;關鍵詞比對前兩邊都拔掉全部空白。"""
    return "".join(text.split())
