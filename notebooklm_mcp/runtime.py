"""Holds the long-lived NotebookLMClient(s) set up by the server lifespan.

Tool functions can fetch it without threading it through every call, and tests
can monkeypatch it with a fake client.

多帳號(ADR-0010):lifespan 可以裝進 N 個帳號的 client。`get_client()` 的語意
**刻意維持不變**——「當前作用中的那一個」——所以 46 個呼叫點一行都不用改,pool
在這個模組之上是隱形的。切換由 `rotate_client()` 顯式驅動,只有配額 failover
那一條路徑會用到。

**這裡沒有鎖,也不需要有。** MCP 是並行的(`mcp/server/lowlevel/server.py` 對每則
incoming message `tg.start_soon`),而 `_ACTIVE` 是全域的 —— 但 pool 的每個成員都是
等價的長駐 client,「作用中是誰」本身不是需要互斥保護的資源。真正會出事的是**呼叫端
把 label 和 client 分兩次讀**(中間夾著 await,並行的 rotate 落在縫裡 → manifest 記 A、
實際 B 送出)。那個縫由 `snapshot()` 一次取用關掉,見它的 docstring。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Slot:
    """pool 裡**一個設定槽位**的全部事實 —— 活著的和啟動時就死掉的都在同一張表上。

    以前這些事實分散在幾個平行結構(`(label, client)` 的 pool、診斷 list、冷卻 dict),
    讀的人得靠「位置對得上」或「先 A 後 B 的呼叫順序」把它們接回來
    (`auth_check` 曾用 `len(live) == len(clients)` 去賭兩邊同長)。現在一個槽位一筆。

    - ``number``:槽位編號(1 起算,對應 Doppler 的 `NOTEBOOKLM_AUTH_JSON[_N]`)。跳過第 3
      槽之後 pool 的第 3 個 client 是第 4 槽 —— 編號住在這裡,不是 pool 的 index。
    - ``label`` / ``client``:活槽位的 `(label, client)`;啟動時建不出 client 的槽位兩者
      皆 None(``dead``)。label 是帳號 email(拿不到退回 "#N"),用於稽核紀錄。
    - ``diag``:啟動時算好的**非機密**診斷(env 名稱、refreshable、PSIDTS domain,死槽位
      另帶 ``startup_error``)。**不含 cookie 值**;憑證不存在這裡(v0.9.0 移除的第三元素)。
    - ``cooled_at``:上次被伺服器拒絕的 `time.monotonic()`;**不是「今天已耗盡」**
      (v0.9.6 實測同帳號被拒 26 分鐘後又被受理,常只是瞬時限流)。
    """

    number: int
    label: str | None = None
    client: Any = None
    diag: dict[str, Any] = field(default_factory=dict)
    cooled_at: float | None = None

    @property
    def dead(self) -> bool:
        return self.client is None

    @property
    def env(self) -> str | None:
        return self.diag.get("env")

    @property
    def startup_error(self) -> str | None:
        return self.diag.get("startup_error")


# **唯一**的槽位表,依槽位順序,含啟動時就死掉的槽位。
_SLOTS: list[Slot] = []
# 作用中游標:index 進**活槽位**(`_live()`)的清單,不是進 `_SLOTS`。
_ACTIVE: int = 0
# 冷卻期取得比 v0.9.6 的觀測保守 —— 試錯成本是一次 RPC,丟錯成本是整個 process 少一個帳號。
_COOLDOWN_SECONDS: float = 600.0


def _live() -> list[Slot]:
    """活著的槽位(= 舊的 pool),依槽位順序。"""
    return [slot for slot in _SLOTS if not slot.dead]


def install_slots(slots: list[Slot]) -> None:
    """一次裝入整張槽位表(含死槽位)並把游標與冷卻重設 —— lifespan 走這條。

    新的一輪(server 重啟)= 新的 `Slot` 物件,冷卻紀錄自然不留。診斷與 client 同在
    一筆裡,所以不再有「診斷一定要在 `set_clients` 之後塞」的順序規則。
    """
    global _SLOTS, _ACTIVE
    for slot in slots:
        # Slot.number 與診斷列的 "slot" 是同一個事實的兩份副本;不一致 = 重登錯帳號。
        if "slot" in slot.diag and slot.diag["slot"] != slot.number:
            raise ValueError(f"slot number {slot.number} != diag slot {slot.diag['slot']}")
    _SLOTS = list(slots)
    _ACTIVE = 0


def set_clients(entries: list[tuple[str, Any]]) -> None:
    """裝入整個 pool 並把作用中的位置重設回第一個。entry 是 `(label, client)`。"""
    install_slots([Slot(i, label, client) for i, (label, client) in enumerate(entries, start=1)])


def slot_diagnostics() -> list[dict[str, Any]]:
    """啟動診斷,依槽位順序;沒記錄過就是空 list(單帳號路徑、測試的 fake client)。

    僅測試使用:production 已改讀 `slots_in_order()`。
    """
    return [dict(slot.diag) for slot in _SLOTS if slot.diag]


def slots_in_order() -> list[Slot]:
    """整張槽位表(活的與啟動時死掉的),依槽位編號排序 —— `auth_check(all_slots=True)` 用。

    編號相同時活槽位在前(穩定排序)。回的是表上的 `Slot` 本身(測試會直接設 `cooled_at`)。
    """
    return sorted(_SLOTS, key=lambda slot: (slot.number, slot.dead))


def _find_slot(label: str, casefold: bool = False) -> Slot | None:
    """用 label 找活槽位的 `Slot`;找不到回 None。label 反查的**唯一**實作。"""
    key = label.casefold() if casefold else label
    for slot in _live():
        if (slot.label.casefold() if casefold else slot.label) == key:
            return slot
    return None


def find_client(label: str, *, casefold: bool = False) -> tuple[str, Any] | None:
    """用 label 找活槽位的 `(label, client)`;找不到回 None。**不動作用中游標。**

    比對語意由呼叫端選:`casefold=False` 是逐字相等(research handle 的 `account`),
    `casefold=True` 是大小寫不分(`notebook_share_with_pool` 拿 owner email 去對 pool)。
    `rotate_client` 的 `refused` 也走同一個 `_find_slot`(逐字)。
    """
    slot = _find_slot(label, casefold)
    return (slot.label, slot.client) if slot else None


def set_client(client: Any) -> None:
    """單一 client 的舊介面(None = 清空)。測試與單帳號路徑都還在用。"""
    set_clients([] if client is None else [("#1", client)])


def get_client() -> Any:
    live = _live()
    if not live:
        raise RuntimeError("NotebookLM client not initialized (server lifespan not started)")
    return live[_ACTIVE].client


def snapshot() -> tuple[str | None, Any]:
    """一次取出作用中的 `(label, client)`——**記帳點專用**。

    `active_account()` 與 `get_client()` 分開讀是有縫的:`tools_podcast` 在
    `_claim_prepared_dispatch(..., account=…)`(await)與真正 `generate(client)`
    之間隔著至少一次 await,而 MCP 是並行的 —— 另一個工具呼叫在那個縫裡撞到配額
    並 `rotate_client()`,manifest 就會記下 A、實際卻由 B 送出。

    ADR-0010 §Transparency 說得很明白:client 不會被告知是誰服務了這次呼叫,
    **manifest 是唯一的稽核憑據**。記錯帳等於那個憑據失真,而且事後無法發現
    (兩個帳號都成功,只是掛在錯的名下)。

    這個函式內部沒有 await,所以取出的兩個值必然屬於同一個槽位;呼叫端拿著
    `client` 往下走,之後任何 rotate 都影響不到這一次的送出。
    """
    live = _live()
    if not live:
        raise RuntimeError("NotebookLM client not initialized (server lifespan not started)")
    slot = live[_ACTIVE]
    return slot.label, slot.client


def account_count() -> int:
    return len(_live())


def active_account() -> str | None:
    live = _live()
    return live[_ACTIVE].label if live else None


def all_accounts() -> list[str]:
    """pool 裡每個帳號的 label,依槽位順序。"""
    return [slot.label for slot in _live()]


def all_clients() -> list[tuple[str, Any]]:
    """pool 裡每個 `(label, client)`,依槽位順序 —— **不動作用中游標**。

    給「這件事只有某個特定帳號做得到」的操作用,目前唯一的呼叫端是
    `notebook_share_with_pool`:既有 notebook 只有能看到它的那個帳號分享得動,
    而作用中帳號在 failover 之後**正好是看不到它的那一個**(v0.9.0 真實驗收
    Phase 9-1 抓到:停點的指引叫人跑那支工具,那支工具自己也 permission denied)。

    **刻意不提供「切到某個槽位」的 API**:`_ACTIVE` 的語意是「配額輪替走到哪」,
    讓別的功能去挪它會讓 failover 的帳號記帳失去意義。要用特定帳號就直接拿它的
    client 呼叫,游標不動。
    """
    return [(slot.label, slot.client) for slot in _live()]


def rotate_client(refused: str | None = None, skip: frozenset[str] = frozenset()) -> str | None:
    """把**真正被拒**的那個槽位標成冷卻中,切到下一個**不在冷卻中且不在 `skip` 裡**
    的帳號;繞完一圈都沒有就回 None。

    Args:
        refused: 這次真正被拒的帳號 label —— 呼叫端從自己的 `snapshot()` 拿到的那個,
            不是「現在的 `_ACTIVE`」。**參數是 label 不是 index**:呼叫端手上只有
            `snapshot()` 給的 label(它是拿去送出 RPC 的那個身分的唯一線索),
            index 是這個模組的內部表示,呼叫端從來沒有、也不該持有它。
            `None`(或反查不到,例如 pool 已重裝)= 保守退回冷卻當前 `_ACTIVE`
            (舊行為,相容沒有 snapshot 可用的呼叫端)。
        skip: 呼叫端這一批 failover 已經試過的 label(`tools_podcast._dispatch_
            audio_with_failover` 的 `tried` 集合)。**排除要發生在這裡的掃描裡,
            不能只擋回傳值**(這一輪 P1 修復):舊版本這個函式不知道 `skip` 是誰,
            只回「游標後方第一個不在冷卻中的槽位」——呼叫端事後發現那個槽位剛好
            試過,只能整批放棄,但游標後面可能還有完全沒試過、也沒在冷卻中的帳號
            (真實復現:pool A/B/C/D,tried={A,B},並行 request 把游標推到 D、A 的
            冷卻剛好到期——不掃描時直接跳過 tried 的話,回的是已經試過的 A,C 就這樣
            白白被漏試)。掃描時 `skip` 與冷卻是**兩個獨立條件**,都要滿足才是候選;
            冷卻副作用(標記 `refused` 的槽位)不受 `skip` 影響,永遠做。

    **為什麼不能無條件冷卻 `_ACTIVE`(v0.9.7 修復的並行缺陷)**:呼叫端是拿早先
    `snapshot()` 取到的 `(label, client)` 去送出的,中間隔著至少一次 await。並行下
    pool [a0,a1,a2] 兩個呼叫都 snapshot 到 a0、都被拒:A 先 rotate(冷卻 slot0、
    `_ACTIVE` 變 1);B 接著 rotate 時如果照舊冷卻「現在的 `_ACTIVE`」,冷的會是
    **從沒被試過的 a1**,而真正被拒的 a0 這次反而沒被記上(它已經在 slot0 冷卻過一次,
    但下一輪 A 若也還沒認識 a1,a1 就這樣平白少了一次候選機會)。改成「用 `refused`
    的 label 反查槽位去冷卻」之後,兩次呼叫冷卻的都是 slot0(同一顆),`_ACTIVE`
    的搜尋起點仍然用「當下的游標」——這樣才會撿到 A 已經推進過的位置,兩個呼叫最終
    會分別落在 a1、a2 上,沒有槽位被錯殺。**現在只有真的被拒的槽位會進冷卻,跳過去的
    那個下次照樣是候選**——這句話在並行下才終於成立。

    **終止性只在單次呼叫內有保證**:迴圈最多繞 `len(live)` 步,全部冷卻中就回 None,
    不會無限迴圈。但**跨呼叫的收斂靠牆上時鐘**,不是這個函式能保證的:如果繞完一圈的
    耗時超過 `_COOLDOWN_SECONDS`,第一格的冷卻已經過期,下一輪又會被選中重試——這在
    正常配額 failover 的時間尺度下不是問題,但誠實地說,**這個函式本身不保證「同一批
    failover 不會重複試同一個已知失敗的帳號」**;真正防止在同一批 failover 內原地打轉
    的,是呼叫端(`tools_podcast._dispatch_audio_with_failover`)自己記的 tried set,
    不是這裡的冷卻表。

    冷卻用 `time.monotonic()`(不受系統時鐘調整影響)。`_COOLDOWN_SECONDS` 取得比
    v0.9.6 實測的 26 分鐘**保守**:寧可多花一次 RPC 去試、也不要白白丟掉一個還有額度
    的帳號 —— 試錯成本是一次 RPC,丟錯成本是整個 process 少一個帳號。

    仍然**沒有鎖**,理由見模組 docstring:真正會出事的是呼叫端把 label 與 client 分兩次
    讀,那個縫由 `snapshot()` 關掉,不是這裡。
    """
    global _ACTIVE
    live = _live()
    if not live:
        return None
    now = time.monotonic()
    # 反查 refused 對應的槽位;找不到(None、或 label 不在目前的 pool 裡,例如
    # pool 重裝過)就保守退回冷卻現在的 _ACTIVE —— 舊呼叫端相容。
    target = _find_slot(refused) if refused is not None else None
    (target or live[_ACTIVE]).cooled_at = now
    for step in range(1, len(live) + 1):
        candidate = (_ACTIVE + step) % len(live)
        slot = live[candidate]
        if slot.label in skip:
            continue
        if slot.cooled_at is None or now - slot.cooled_at >= _COOLDOWN_SECONDS:
            _ACTIVE = candidate
            return slot.label
    return None
