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


def assert_not_retired(manifest: dict) -> None:
    """退役 manifest(頂層 `retired: true`)一律不得發布;放在讀完 manifest 之後、任何網路動作
    之前呼叫。

    **為什麼需要一個旗標而不是靠位置**(podcast-lab graphify 2026-08-30 審查):兩份退役
    快照搬進 `archive/` 之後仍通過 ManifestStore schema、`show_id` 仍能打到第一季的舊 feed;
    legacy 集(無 attempts)又不比 sha,`mp3_path` 哪天被「修好」就會把別節目的音檔以舊節目
    名義送上公網。目錄名與 README 只擋得住讀過它們的人。

    判準同 `is_withheld`:**欄位在不在**。缺席 = 照發;`true` = 退役;其他任何值(`false`、
    `null`、字串、`1`)raise,不 fall through —— 這個欄位唯一的用途就是「別發這份」,寫錯
    就靜默照發正好在它該生效時失效,而 feed host 永不刪檔。`is True` 不是 `== True`:
    `1 == True` 在 Python 成立。只管發布層:生成、retract、腳本刻意不看這個欄位。
    """
    if "retired" not in manifest:
        return
    flag = manifest["retired"]
    if flag is True:
        raise ValueError(
            "manifest is retired(頂層 `retired: true`):這是退役快照,不得作為 manifest_path"
            " 發布 —— 正本是該節目工作區裡現行的 series_manifest.json"
        )
    raise ValueError(
        f"manifest 頂層 retired 只能是 true,得到 {flag!r} —— 要發布就把整個欄位移除,"
        "不要填 false/null/字串(與 publication_state 同一條紀律:缺席才是照發)"
    )
