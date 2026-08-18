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


def is_withheld(episode: dict) -> bool:
    """這一集是否被扣下(不進 feed)。未知值 raise,**不 fall through 成照發**。

    判準是**欄位在不在**,不是 `.get()` 的值:`{"publication_state": null}` 用
    `.get() is None` 會與「根本沒這個欄位」同形而照發,而 `null` 的意圖無從得知。
    缺席才是照發 —— 這是唯一一道「不得公開」的閘,而 feed host 永不刪檔。

    **判準要三處共用**(`publish_series` 的 preflight、`reorder_published_at.py`、
    `backfill_published_at.py`):漂掉的兩個方向都會出事 —— 發布端扣下、腳本卻把它算進
    排序(live 集拿到不屬於自己的時間),或反過來讓該扣下的集參與發布。
    """
    if "publication_state" not in episode:
        return False
    state = episode["publication_state"]
    # 先驗型別:unhashable(list/dict)直接 `in frozenset` 會漏一個 TypeError 出去,
    # 而 manifest 是信任邊界上的輸入,要回可讀的 ValueError。
    if isinstance(state, str) and state in WITHHELD_PUBLICATION_STATES:
        return True
    raise ValueError(
        f"episode {episode.get('episode')}: unknown publication_state {state!r} "
        f"(扣下的狀態只有 {sorted(WITHHELD_PUBLICATION_STATES)};要照發就別設這個欄位)"
    )
