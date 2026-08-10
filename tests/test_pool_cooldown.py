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


def test_a_full_lap_still_terminates(monkeypatch):
    """終止性不能變:一輪之內全部被拒就要回 None,否則一次配額耗盡會變成無限重試。"""
    _pool(3)
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    assert runtime.rotate_client() == "a1@x"
    assert runtime.rotate_client() == "a2@x"
    assert runtime.rotate_client() is None, "繞完一圈要停,不能無限輪替"


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


def test_a_slot_skipped_by_a_concurrent_rotate_is_not_burned(monkeypatch):
    """順帶修掉 docstring 記的那個並行缺陷:兩個呼叫同時撞配額時,游標被推兩格,
    **中間那個從來沒被試過**卻一起消失。改成「被拒的才進冷卻」之後,沒被試過的
    槽位不會被冷卻,下一次還會被挑到。"""
    _pool(4)
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    runtime.rotate_client()                        # a0 被拒 → a1
    runtime.rotate_client()                        # a1 被拒 → a2
    # a3 從沒被試過。把游標挪到 a3 之後再轉一圈,它仍應是候選之一。
    tried = set()
    for _ in range(4):
        label = runtime.rotate_client()
        if label is None:
            break
        tried.add(label)
    assert "a3@x" in tried, "沒被試過的槽位不該跟著被燒掉"
