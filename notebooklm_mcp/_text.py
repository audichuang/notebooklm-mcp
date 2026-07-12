"""共用文字處理(零依賴,供 tools_basic / tools_artifacts 匯入,避免循環 import)。"""
from __future__ import annotations

import re

# NotebookLM chat 的引用標記([1] / [3, 4] / [2-5]);show notes 發布前要清掉。
# 只此一份 regex,chat_ask 與 episode_set_description 共用,避免兩處漂移。
_CITATION_RE = re.compile(r"\[[\d,\s\-–]+\]")


def norm(text: str) -> str:
    """NotebookLM 對 CJK 擷取會插空格;關鍵詞比對前兩邊都拔掉全部空白。"""
    return "".join(text.split())
