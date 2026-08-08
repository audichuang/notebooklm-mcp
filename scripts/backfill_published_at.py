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

import os
import sys

from notebooklm_mcp.manifest_store import ManifestStore
from notebooklm_mcp.tools_podcast import _first_published_at


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
