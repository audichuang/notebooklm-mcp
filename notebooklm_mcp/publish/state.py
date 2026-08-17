"""集層 publication state 的白名單。純資料,無相依 —— 讀它的 `tools_publish` 與寫它的
`tools_artifacts` 共用同一份。

**為什麼獨立一個模組**:常數原本放在 `tools_publish`,而 `tools_artifacts` 去 import 它會
在「先 import tools_publish」的路徑上炸循環 import(`tools_publish` → `app` → 註冊
`tools_artifacts` → `tools_publish` 這時只初始化到一半,常數還不存在)。實際被
`tests/test_publish_tools.py` 的 module-level import 抓到。`publish/` 這一層是離線可測的
純邏輯、兩邊本來都已經在 import,放這裡沒有方向問題。

**各寫一份的後果**:「可以設的狀態」與「會被扣下的狀態」會各自漂 —— 設得進去、發布卻不認
(那一集照樣公開),或反過來。所以正本只有這一份。
"""
from __future__ import annotations

#: host 寫在 manifest episode 上的「刻意不公開」狀態:集數留在 manifest 當 audit,但不進
#: feed。**只管發布層** —— 不代表禁止重生(`podcast_series` 與 attempt 掃描刻意不看這個
#: 欄位)。契約寫在 docs/gotchas-publish.md,新增狀態時兩邊一起改。
WITHHELD_PUBLICATION_STATES = frozenset({"deferred"})
