"""runtime 的單一槽位表:不經 `app._lifespan`,直接用 `Slot` 建表測介面。"""

import time

import pytest

from notebooklm_mcp import runtime
from notebooklm_mcp.runtime import Slot


@pytest.fixture(autouse=True)
def _clean():
    yield
    runtime.set_clients([])


def _table():
    # 槽位 2 啟動時就死了:不在 pool 裡,但編號與診斷留在表上。
    runtime.install_slots(
        [
            Slot(1, "A@x.com", "ca", {"slot": 1, "env": "E1"}),
            Slot(2, diag={"slot": 2, "env": "E2", "startup_error": "boom"}),
            Slot(3, "b@x.com", "cb", {"slot": 3, "env": "E3"}),
        ]
    )


def test_dead_slots_are_in_the_table_but_not_in_the_pool():
    _table()
    assert runtime.account_count() == 2
    assert runtime.all_accounts() == ["A@x.com", "b@x.com"]
    assert runtime.all_clients() == [("A@x.com", "ca"), ("b@x.com", "cb")]
    assert [s.number for s in runtime.slots_in_order()] == [1, 2, 3]
    dead = runtime.slots_in_order()[1]
    assert dead.dead and dead.env == "E2" and dead.startup_error == "boom"
    assert [r["slot"] for r in runtime.slot_diagnostics()] == [1, 2, 3]


def test_rotation_walks_live_slots_only_and_keeps_slot_numbers(monkeypatch):
    _table()
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    assert runtime.snapshot() == ("A@x.com", "ca")
    assert runtime.rotate_client() == "b@x.com"  # 跳過死槽位 2
    assert runtime.snapshot() == ("b@x.com", "cb")
    assert runtime.rotate_client() is None  # 兩個活槽位都進冷卻
    cooled = {s.number for s in runtime.slots_in_order() if s.cooled_at is not None}
    assert cooled == {1, 3}


def test_find_client_has_per_caller_case_semantics():
    _table()
    assert runtime.find_client("A@x.com") == ("A@x.com", "ca")
    assert runtime.find_client("a@x.com") is None  # 預設逐字相等
    assert runtime.find_client("a@x.com", casefold=True) == ("A@x.com", "ca")
    assert runtime.find_client("nobody@x.com", casefold=True) is None


def test_installing_a_new_table_resets_cursor_and_cooldown():
    _table()
    runtime.rotate_client()
    _table()
    assert runtime.active_account() == "A@x.com"
    assert all(s.cooled_at is None for s in runtime.slots_in_order())


def test_install_rejects_number_and_diag_slot_disagreement():
    with pytest.raises(ValueError):
        runtime.install_slots([Slot(5, "a@x", "ca", {"slot": 1})])


def test_rotate_refused_uses_the_same_exact_lookup_as_find_client():
    _table()
    runtime.rotate_client(refused="b@x.com")
    cooled = [s.number for s in runtime.slots_in_order() if s.cooled_at is not None]
    assert cooled == [3]
    # casefold 不影響 rotate:大小寫不同的 refused 找不到,退回冷卻 _ACTIVE(槽位 1)
    _table()
    runtime.rotate_client(refused="B@X.COM")
    assert [s.number for s in runtime.slots_in_order() if s.cooled_at is not None] == [1]
