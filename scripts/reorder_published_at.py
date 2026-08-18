#!/usr/bin/env python3
"""一次性修復:把整季的 `published_at` 依集號重新配對,修掉「亂序生成 → RSS 排序錯位」。

背景(真實事故,saa-* 12 集):`published_at` 存的是**生成完成時間**,而整季幾乎都是
亂序生成的 —— EP01 最後才補(21:33,比 EP12 的 21:15 還晚),於是訂閱者的 App 裡
EP01 變成「最新一集」卡在列表第一個。`itunes:type=serial` 救不了:實測多數播放器
(含 Apple 自己的列表視圖)仍以 pubDate 為準。v0.9.19 起 `publish_series` 的 preflight
會擋下非單調的 manifest(在任何 PUT 之前),這支是它指向的修法。

做法:取出這一季**現有**的 published_at,排序後依集號重新指派 —— **不發明新時間戳**,
只是換配對,所以「這一季是什麼時候做的」這個稽核事實不變。**走 ManifestStore
(不手改 JSON)**,原子寫入 + revision 遞增,符合 AGENTS.md 的紅線。

範圍與刻意不做的:
- 只重排**會進 feed** 的集。判準用 `publish/state.is_withheld`(與 publish 端同一份),
  被扣下的集(deferred)完全不碰 —— 它的時間戳不影響排序。
- **要求每一集都有 published_at。** 缺值的集在 feed 裡走 `_fallback_pub_date`
  (2020 基準),與真實時間戳混在一起時「重排現有值」無法修好順序,那需要**發明**
  一個時間戳,不在這支的授權範圍內 —— 直接報錯讓人決定,不猜。
- 重排後若仍有兩集**同時間**(池子裡本來就有重複值),也報錯:嚴格遞增靠換配對修不了。
- **只改 episode 頂層 `published_at`,不動 attempt 歷史。** 重生過的集,首發時間的另一份
  記錄在 `retraction.retracted_output.published_at`(`_first_published_at` 讀它),那是撤回
  當時搶救的稽核快照,不該被這支改寫 —— 但它會讓
  `backfill_published_at.py` 想把重排的結果**改回去**,所以本支會對這種集印警告,而
  backfill 自己也加了一道「回填後若非單調就拒絕」的守衛。真正的最終防線是
  `publish_series` 的 preflight:任何路徑造成亂序都會在上傳前被擋下,重跑這支即可修好。

用法(dry-run 預設,只印不寫;加 --apply 才落地):
    uv run python scripts/reorder_published_at.py <series_manifest.json>
    uv run python scripts/reorder_published_at.py <series_manifest.json> --apply

冪等:已經遞增就不會動(連 revision 都不跳)。落地後重跑 `publish_series` 讓 feed.xml
重渲染(GUID 與媒體 content-hash 都不變 → Apple 視為同集更新,不會產生孤兒連結)。
"""
from __future__ import annotations

import os
import sys

from notebooklm_mcp.manifest_store import ManifestStore
from notebooklm_mcp.publish import state as state_mod
from notebooklm_mcp.tools_podcast import _first_published_at
from notebooklm_mcp.tools_publish import _parse_pub_date


class _NoChange(Exception):
    """mutator 內用的中止訊號:什麼都沒變就**不要寫盤**。

    `ManifestStore.update` 的 revision 是無條件 +1,而 `_write` 在 mutator **之後**才跑,
    所以從 mutator 拋出去就是「零寫入」離場。冪等重放平白 +1 會撞掉別的 writer 的
    `expected_revision` CAS。**不能改成先 `read()` 再決定要不要 `update()`** —— 兩次呼叫
    之間別的 writer 插進來,判斷就過期了(dry-run 印出來的那份 snapshot 同理:它只給人看,
    `--apply` 的判斷一律在鎖內重算)。"""


def _feed_episodes(manifest: dict) -> list[dict]:
    """會進 feed 的集,依集號排序。扣下狀態的判準(含未知值 fail-loud)與 publish 端
    共用 `state.is_withheld` —— 各寫一份就會漂,而漂掉的兩個方向都會出事。"""
    return sorted(
        (ep for ep in manifest.get("episodes", []) if not state_mod.is_withheld(ep)),
        key=lambda ep: int(ep["episode"]),
    )


def _plan(manifest: dict) -> list[tuple[int, str, str]]:
    """每一集的 (集號, 現值, 重排後的值);已正確的集也留在清單裡由呼叫端過濾,這樣
    「沒有需要改的」與「manifest 沒有集數」不會同形。

    兩種修不了的形狀在這裡 raise。從 `update` 的 mutator 裡拋出時仍然安全:`_write` 在
    mutator 之後才跑,而鎖由 context manager 的 finally 釋放 —— 零寫入離場。"""
    eps = _feed_episodes(manifest)
    missing = [int(ep["episode"]) for ep in eps if not ep.get("published_at")]
    if missing:
        raise SystemExit(
            f"這些集沒有 published_at:{missing} —— 重排現有時間戳修不了混合狀態"
            "(缺值的集在 feed 裡走 2020 的 fallback)。先讓那幾集有真實時間戳再跑這支。"
        )
    pool = sorted((_parse_pub_date(ep["published_at"], int(ep["episode"])), ep["published_at"])
                  for ep in eps)
    dupes = [raw for i, (dt, raw) in enumerate(pool) if i and dt == pool[i - 1][0]]
    if dupes:
        raise SystemExit(
            f"時間戳有重複值 {dupes} —— 嚴格遞增靠換配對修不了,需要有人決定一個新時間。"
        )
    return [
        (int(ep["episode"]), ep["published_at"], raw)
        for ep, (_dt, raw) in zip(eps, pool)
    ]


def _history_warnings(manifest: dict, changed: list[tuple[int, str, str]]) -> list[str]:
    """重排後與 attempt 首發歷史不一致的集。

    這一支只改頂層欄位;重生過的集在 `retraction.retracted_output.published_at` 另有一份
    首發時間,而 `backfill_published_at.py` 會把頂層**改回**那個值 —— 兩支腳本互相打架。
    backfill 那邊已加守衛(回填後非單調就拒絕),這裡負責讓人當場看到是哪幾集。"""
    by_n = {int(ep["episode"]): ep for ep in _feed_episodes(manifest)}
    out = []
    for n, _cur, new in changed:
        first = _first_published_at(by_n[n])
        if first and first != new:
            out.append(f"  EP{n:02d}: attempt 首發歷史記著 {first!r},與重排後的 {new!r} 不同")
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply = "--apply" in sys.argv
    if len(args) != 1:
        print(__doc__)
        return 2
    manifest_path = args[0]

    # ManifestStore.read() 對不存在的路徑會投影成空 manifest → 打錯路徑會靜默回「沒有
    # 需要重排」而不是報錯(與 backfill_published_at.py 同一道防護)。
    if not os.path.isfile(manifest_path):
        print(f"manifest 不存在(是不是路徑打錯?):{manifest_path}", file=sys.stderr)
        return 2

    store = ManifestStore(manifest_path)

    def report(manifest: dict, changed: list[tuple[int, str, str]], *, applied: bool) -> None:
        print(f"{'已重排' if applied else '[dry-run] 會重排'}以下集數的 published_at:")
        for n, cur, new in changed:
            print(f"  EP{n:02d}: {cur!r}  →  {new!r}")
        warnings = _history_warnings(manifest, changed)
        if warnings:
            print("\n⚠️ 這幾集重生過,attempt 首發歷史與重排後的值不同:")
            print("\n".join(warnings))
            print("  → **不要再對這份 manifest 跑 backfill_published_at.py**,它會把這幾集"
                  "改回歷史值(那組值本身可能也是亂序的)。backfill 現在會自己擋下這種回填。")

    if not apply:
        # 這份 snapshot 是**鎖外**讀的,只用來給人看;--apply 的判斷一律在鎖內重算。
        manifest = store.read()
        changed = [(n, cur, new) for n, cur, new in _plan(manifest) if cur != new]
        if not changed:
            print("published_at 已隨集號遞增,不需要重排。")
            return 0
        report(manifest, changed, applied=False)
        print("\n這是 dry-run。確認無誤後加 --apply 落地。")
        return 0

    # 計畫與「要不要寫」全部在鎖內的 snapshot 上算:dry-run 與 --apply 之間 manifest 可能
    # 被別的 writer 改過(兩個方向都會錯:該修的沒修、或平白 +1 revision 撞掉別人的 CAS)。
    def mutate(manifest: dict) -> list[tuple[int, str, str]]:
        by_n = {int(ep["episode"]): ep for ep in _feed_episodes(manifest)}
        changed = [(n, cur, new) for n, cur, new in _plan(manifest) if cur != new]
        if not changed:
            raise _NoChange
        for n, _cur, new in changed:
            by_n[n]["published_at"] = new
        return changed

    try:
        manifest, changed = store.update(mutate)
    except _NoChange:
        print("published_at 已隨集號遞增,不需要重排。")
        return 0

    report(manifest, changed, applied=True)
    print(f"\n完成,實際重排 {len(changed)} 集。接著重跑 publish_series 讓 feed.xml 重渲染"
          "(GUID 不變 → Apple 視為同集更新)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
