"""配額 failover 的槽位冷卻:`RateLimitError` **不等於**「今天已耗盡」。

v0.9.6 真實驗收量到的:同一個 slot 1 被拒後 **26 分鐘**在另一個 process 又被受理 ——
那是瞬時限流,不是當日配額用盡。而 `rotate_client()` 原本讓游標**單向前進、永不回頭**,
於是一次瞬時限流就讓那個帳號在該 process 餘生都不再被試;那一輪實測有**三個帳號**因此
提早退場,pool 的有效容量被白白吃掉,而 manifest 看起來一切正常(每次都成功 failover 了)。

原 docstring 把它留成已知取捨,理由是「代價只是那一輪少試一個帳號」—— 26 分鐘那個
觀測推翻了這個前提:代價是**那個 process 餘生**。
"""
import time

import pytest

from notebooklm_mcp import runtime


@pytest.fixture(autouse=True)
def _clean_pool():
    yield
    runtime.set_clients([])


def _pool(n):
    runtime.set_clients([(f"a{i}@x", object()) for i in range(n)])


def test_all_slots_cooled_within_one_lap_returns_none(monkeypatch):
    """這條只證得動「一輪之內全部被拒就回 None」——**不是**「繞圈保證終止」。

    `time.monotonic` 被凍成常數,所以冷卻永遠不會過期;這條測試證的是單次呼叫內
    迴圈有界(最多繞 `len(_POOL)` 步),不是跨呼叫的收斂性。真正的「同一批 failover
    不會原地打轉」靠呼叫端(`tools_podcast._dispatch_audio_with_failover`)的
    tried set,不是這裡的冷卻表——見 `rotate_client` docstring 的誠實聲明。
    """
    _pool(3)
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    assert runtime.rotate_client() == "a1@x"
    assert runtime.rotate_client() == "a2@x"
    assert runtime.rotate_client() is None, "一輪之內全部冷卻要回 None,不能無限輪替"


def test_a_throttled_slot_comes_back_after_the_cooldown(monkeypatch):
    """**這是這條 finding 的核心。** 被拒的槽位過了冷卻期要重新可用 ——
    否則瞬時限流 = 永久退場。"""
    _pool(2)
    now = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])

    assert runtime.rotate_client() == "a1@x"      # a0 被拒 → 冷卻
    assert runtime.rotate_client() is None        # a1 也被拒 → 兩個都在冷卻

    now["t"] += runtime._COOLDOWN_SECONDS + 1     # 等過冷卻期
    assert runtime.rotate_client() is not None, (
        "冷卻期過了還是不肯回頭試 —— 瞬時限流被當成當日耗盡"
    )


def test_a_slot_skipped_by_a_stale_snapshot_rotate_is_not_burned(monkeypatch):
    """真正的並行時序(不是連續兩次「當前 _ACTIVE 被拒」那種偽並行):

    兩個呼叫端在同一瞬間都用 `snapshot()` 拿到 a0(舊 label),各自送出、各自被拒。
    A 先呼叫 `rotate_client(refused="a0@x")`:a0 真的被拒 → 冷卻 slot0、游標推到 a1。
    B 接著呼叫時,**手上仍是自己 snapshot 到的舊 label "a0@x"**(不是 A 推進後的
    新游標 a1)—— 這正是 ADR-0010 §Transparency 講的縫:label 與 client 是兩次
    await 之外分別取得的,B 完全不知道 A 已經動過游標。

    缺陷版(`rotate_client` 不吃 `refused`、無條件冷卻 `_ACTIVE`)會在 B 呼叫時把
    **從沒被拒絕過的 a1** 一起冷卻掉;修復版用 `refused` 反查出真正該冷卻的 slot0,
    a1 應該完好無損地被下一次呼叫選中。

    ⚠️ **回傳值本身沒有鑑別力,不要只斷言它**:缺陷版燒掉 a1 之後,搜尋起點也跟著
    推到 slot1,下一格照樣是 a2 —— 兩版的 `second` 都是 `"a2@x"`。上一輪就是栽在
    這裡(斷言對缺陷版與修復版同時成立,突變後全套 2588 全綠)。真正的差異只在
    **a1 有沒有被寫進 `_COOLING`**,所以下面兩條斷言一條查狀態、一條查行為。
    """
    _pool(3)
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    stale_label = runtime.snapshot()[0]
    assert stale_label == "a0@x", "兩個呼叫端共享的舊 snapshot 應該是 a0"

    first = runtime.rotate_client(refused=stale_label)  # a0 真的被拒 → 冷卻 slot0,游標推到 a1
    assert first == "a1@x"

    # B 呼叫時仍舊拿著同一顆過時的 label —— 不是 A 推進後的新游標。
    second = runtime.rotate_client(refused=stale_label)
    assert second == "a2@x", "a1 從沒被拒絕過,不該被 B 的 stale snapshot 燒掉"

    # 狀態面:被冷卻的必須**只有** slot0(兩次被拒的都是它)。
    assert 1 not in runtime._COOLING, (
        f"a1 從沒被拒絕過卻被燒進冷卻表 —— refused 反查失效了 cooling={runtime._COOLING}"
    )

    # 行為面:a1 仍該是候選。缺陷版此刻 slot0/1/2 全在冷卻中,這一轉會回 None。
    assert runtime.rotate_client(refused="a2@x") == "a1@x", (
        "a1 應該還是候選 —— 它從頭到尾沒被任何一個呼叫端拒絕過"
    )
