# v0.6.0 真實環境驗收結果(2026-08-08)

> 2026-08-16 從拋棄式工作區 `nblm-acceptance-v0.6.0/` 補收進版控(該工作區連同 33MB 測試媒體已刪)。內容一字未改。

Phase 0–5 全跑完。**28 條驗收項目過,2 個真問題**,3 次真實生成、1 次真實發布。

測試現場:notebook `34da93f9-1983-4f44-a4d0-1ec13233a50a`(ZZ-TEST,待手動刪)、
feed `https://podcast.example.com/feeds/FEED_TOKEN/feed.xml`(NAS blob 永久)。

---

## 問題 1(要修)`podcast_episode` 回傳的 `published_at` 與落地的 manifest 不一致

重生取代版時:

| 來源 | 值 |
|---|---|
| manifest episode 級(feed.xml 的真相) | `Sat, 08 Aug 2026 12:07:20 +0800` ← **首發時間,正確** |
| 工具回傳值 | `Sat, 08 Aug 2026 12:16:39 +0800` ← 重生當下,**錯** |

**根因**:`_finalize_episode` 產出 `output["published_at"] = format_datetime(now)`;
`_promote_attempt_output` 只在**寫 manifest 時**用
`episode.setdefault("published_at", _first_published_at(episode) or output["published_at"])`
蓋掉,但 `tools_podcast.py:1166` 是 `return output` —— 回的是寫入**前**那份 dict。
修復落在寫入點,沒傳播到回傳值(= AGENTS.md 自己列的頭號教訓「補一半」)。

**影響**:feed.xml 正確,不影響訂閱者。但相信回傳值的呼叫端(host agent 寫 show notes /
QA 紀錄 / 對帳)會拿到錯的 pubDate。

**修法方向**:promotion 之後把 manifest 實際落地的 `published_at` 讀回 `output`,
或讓 `_promote_attempt_output` 回傳生效值。注意 `podcast_series` 的 `run_results` 也吃這個 dict。

## 問題 2(要修,小)`episode_set_description` 的 docstring 過度承諾

docstring:「並前置驗 publish 的 preflight 條件…讓錯誤在寫入當下就爆,不留到發布才 fail」。

實測:擋掉「空字串」與「等於標題」,但**放行 markdown 圖片**
(`![封面](https://evil.example/x.png)`)寫進 manifest,要到 `publish_series` 才爆。

**影響**:失敗仍 fail-closed(零 byte 上傳,已實測),只是發生在整季生完之後。
**修法**:寫入時就跑 notes_html 的 allowlist,或把 docstring 改成只承諾它真的做的那兩條。

## 小瑕疵(不一定要修)

- HTML allowlist 的錯誤訊息**沒帶集號**:`episode notes HTML 含不在允許清單內的標籤…`。
  同檔的 cover 檢查有帶(`episode 1: cover_path is required`)。45 集的季度會找很久。
- 重呼 `podcast_episode` 的拒絕訊息 `P0 does not support implicit regeneration` 帶內部術語
  「P0」,而且**沒給出路**(要重生請先 `podcast_attempt_retract`)。同批其他訊息都有給出路。

## 素材設計的教訓(README 的 canary 要改)

回錄 source 是 **mp3 的 ASR**,拉丁字代號活不過 TTS→ASR:
`KESTREL`→`Castrial`/`Castro`、`MARLIN`→`馬令`/`馬的`、`冪等`→`密等性`。
`contains=["KESTREL"]` 對回錄逐字稿**永遠 false**,不能當隔離證據。

**改用主題詞做正反對照**(實測有效):EP02 逐字稿 `時鐘`/`節點`/`上游` 命中,
EP01 的 `TTL`/`快取`/`密等`/`指紋`/`付款`/`驚群`/`single` 全不命中 → 隔離成立。

---

## 仍未驗證(真實環境測不到)

原 handoff 第六節的 1–4 全部仍未驗:naive datetime 的 response-loss 路徑、
manifest post-commit fsync(需 NAS/overlay mount)、`podcast_attempt_adopt` 的清理義務
(湊不出 `reconciliation_ambiguous`,本輪 3 次生成都沒觸發)、**3 次以上重生的 pubDate**。

新增一項:`saa-drill` 的 manifest **這台機器上不存在**(`AWS/SAA/` 底下沒有 `podcast/`)。
全機唯一 manifest 是 podcast-lab 的 Audicast,需回填 5 集(EP37/40/41/42/43),
使用者決定**不做**。dry-run 已驗證可用且唯讀(md5 不變)。
