#!/usr/bin/env python3
"""一次性回填:把重生過的集數被寫壞的 episode 級 `published_at` 復原成首發時間。

背景:v0.5.0 之前 `_promote_attempt_output` 會用「重生當下」的 wall clock 補回
`published_at`(bug),導致重生過的集在 RSS 裡 pubDate 漂移、排序錯位(saa-drill
EP05/EP09)。修好的 code 只影響**之後**的 promote;既有 manifest 要用這支回填。

做法:對每一集,用與 production 同一顆 `_first_published_at`(沿 attempts 建立順序找
第一筆非空的 `retraction.retracted_output.published_at`)算出真正的首發時間,若與
現值不同就改寫。**走 ManifestStore(不手改 JSON)**,原子寫入 + revision 遞增,
符合 AGENTS.md 的「manifest 只由工具/ManifestStore 寫入」紅線。

用法(dry-run 預設,只印不寫;加 --apply 才落地):
    uv run python scripts/backfill_published_at.py <series_manifest.json>
    uv run python scripts/backfill_published_at.py <series_manifest.json> --apply

冪等:跑第二次不會再改(值已正確)。回填後重跑 publish_series 讓 feed.xml 重渲染
(媒體 content-hash 不變,enclosure URL 不動)。
"""

from __future__ import annotations

import copy
import os
import sys

from notebooklm_mcp.manifest_store import ManifestStore
from notebooklm_mcp.publish import state as state_mod
from notebooklm_mcp.tools_podcast import _first_published_at
from notebooklm_mcp.tools_publish import _assert_pub_dates_ascend


def _plan(manifest: dict) -> list[tuple[int, str, str]]:
    """算出哪些集要改 published_at。只在「有首發審計記錄」且「現值與它不同」時動——
    沒重生過的集 first 為 None,不碰;已正確的集跳過(冪等)。"""
    planned: list[tuple[int, str, str]] = []
    for episode in manifest.get("episodes", []):
        first = _first_published_at(episode)
        current = episode.get("published_at")
        if first and current != first:
            planned.append((episode.get("episode"), current, first))
    return planned


def _breaks_ordering(manifest: dict, planned: list[tuple[int, str, str]]) -> str | None:
    """這次回填會不會讓 pubDate 變成**非單調**(於是整季發不出去)?會就回傳原因。

    這支腳本的用途是「重生不要讓 pubDate 漂」,而它取的正本是 attempt 首發歷史。但亂序
    生成的季度用 `reorder_published_at.py` 重新配對過頂層之後,那份歷史記著的仍是原本
    (同樣亂序的)時間 —— 照著回填就會把修好的排序再拆掉,而 v0.9.19 起
    `publish_series` 會在任何上傳之前擋下整季。兩支腳本互相打架,所以後跑的這一支要
    自己看得出來。**實際踩到**:podcast-lab 那份 50 集的 EP42/EP43,頂層修好之後歷史值
    仍是 07:18:43 / 07:17:36(EP42 晚於 EP43)。
    """
    simulated = copy.deepcopy(manifest)  # 只模擬,不動真 manifest
    by_n = {int(ep["episode"]): ep for ep in simulated.get("episodes", []) if "episode" in ep}
    for n, _current, first in planned:
        if n in by_n:
            by_n[n]["published_at"] = first
    feed_eps = [ep for ep in simulated.get("episodes", []) if not state_mod.is_withheld(ep)]
    try:
        _assert_pub_dates_ascend(feed_eps)
    except ValueError as exc:
        return str(exc)
    return None


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply = "--apply" in sys.argv
    if len(args) != 1:
        print(__doc__)
        return 2
    manifest_path = args[0]

    # ManifestStore.read() 對不存在的路徑會投影成空 manifest → 打錯路徑會靜默回「沒有需要
    # 回填」而不是報錯。先擋掉,免得回填悄悄跑在空 manifest 上。
    if not os.path.isfile(manifest_path):
        print(f"manifest 不存在(是不是路徑打錯?):{manifest_path}", file=sys.stderr)
        return 2

    store = ManifestStore(manifest_path)
    planned = _plan(store.read())
    if not planned:
        print("沒有需要回填的集數(要嘛沒重生過,要嘛 published_at 已正確)。")
        return 0

    print(f"{'將回填' if apply else '[dry-run] 會回填'}以下集數的 published_at:")
    for n, current, first in planned:
        print(f"  EP{n:02d}: {current!r}  →  {first!r}")

    # 回填前先問「回填完還發得出去嗎」:亂序生成的季度被 reorder 修過頂層之後,照歷史
    # 回填會把排序再拆掉,而那要等到下次 publish 才會發現(preflight 擋下整季)。
    broken = _breaks_ordering(store.read(), planned)
    if broken:
        sys.stdout.flush()  # 讓上面那份清單先落地,拒絕訊息才不會插到它前面
        print(
            "\n拒絕回填:這樣會讓 pubDate 不再隨集號遞增,整季會被 publish_series 擋下。\n"
            f"{broken}\n"
            "這通常代表頂層 published_at 已經被 scripts/reorder_published_at.py 重新配對過,"
            "而 attempt 首發歷史記著的是原本(也是亂序的)那組值 —— 那就不要回填。\n"
            "真的需要回填時,先決定要放棄哪一個不變式:重生不漂移,還是集序正確。",
            file=sys.stderr,
        )
        return 2

    if not apply:
        print("\n這是 dry-run。確認無誤後加 --apply 落地。")
        return 0

    # 在鎖內重算、回報實際落地筆數:dry-run 與 apply 之間 manifest 可能被別的 writer 改過,
    # 不能沿用上面印出的 planned 筆數(那是 lock 外的舊 snapshot)。
    applied: list[tuple[int, str, str]] = []

    def apply_mutation(manifest: dict) -> None:
        applied.clear()
        for episode in manifest.get("episodes", []):
            first = _first_published_at(episode)
            current = episode.get("published_at")
            if first and current != first:
                episode["published_at"] = first
                applied.append((episode.get("episode"), current, first))

    store.update(apply_mutation)
    print(f"\n完成,實際回填 {len(applied)} 集。接著重跑 publish_series 讓 feed.xml 重渲染。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
