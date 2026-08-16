# notebooklm-mcp v0.7.1 真實環境驗收 — FINDINGS

> 2026-08-16 從拋棄式工作區 `nblm-acceptance-v0.7.1/` 補收進版控(該工作區連同 145MB 測試媒體已刪)。內容一字未改。

環境:`doppler -p notebooklm -c stg`(獨立測試帳號)、`nblm-mcp` 0.7.1 / notebooklm-py 0.8.0
notebook:`ZZ-TEST v0.7.1 驗收` = `f2cef0bc-7eb1-462a-8662-754273fd529c`
日期:2026-08-08
notebook:配額測試用 `ZZ-TEST v0.7.1 配額拒絕` = `1c249dc4-3d1c-48f3-96d2-392d6fcb2bfa`
配額用量:**5 次成功生成**(EP01 音檔、EP02 音檔、EP01 重生音檔、EP01 簡報、EP01 講義)
+ 第二階段刻意把 audio 配額打爆,取得真實 `RateLimitError`(4 次被拒的 dispatch,不計成功配額)

## 驗收結果總表

| Phase | 結果 |
|---|---|
| 0 免費本機檢查 | **11 過 / 0 失敗**;repo `515 passed in 73.45s` |
| 1 便宜真 RPC | **全過**;發現 F-1 / F-2 / F-3 |
| 2 主線生成(EP01+EP02) | **全過**;`published_at` 回傳值與 manifest 一致 |
| 3 ⭐ retract → 重生 | **全過**,含 `published_at` 不漂(三個出口)、`source_ids` 硬隔離、fail-closed |
| 4 附件與發布 | **全過**,含公網讀回 4 項;發現 O-4 / O-5 |
| ⭐ 配額拒絕分類 | **已驗證,四項全對**;但同時挖出 F-7 / F-8 / F-9 / O-6 |

### ⭐ 配額拒絕:已驗到,分類四項全部正確

第二階段刻意把 audio 配額打爆(新建 notebook `1c249dc4-3d1c-48f3-96d2-392d6fcb2bfa`,
`podcast_series` 排 6 集 `short`),**第一集就被拒**。README 那張對照表逐項比對:

| README 說「應該是」 | 實測 | |
|---|---|---|
| `observed_state` = `not_accepted` | `not_accepted` | ✅ |
| `safe_next_action` = `podcast_series` | `podcast_series` | ✅ |
| manifest `remote.error` 帶得出拒絕原因 | `"API rate limit or quota exceeded. Please wait before retrying. (Upstream: Resource exhausted.)"` | ✅ |
| `remote.error_code` = 例外型別名 | `"RateLimitError"` | ✅ |

`dispatch.status=not_accepted`、`accepted_at=null`、`remote.artifact_id=null`
—— 確認**沒有建出任何 artifact**,拒絕發生在 dispatch。

**兩個呼叫點都驗了**(`_REFUSED_WITHOUT_DISPATCH` 在 `tools_podcast.py` 有兩處
獨立的 `except`):EP01 走 `podcast_series`、EP02 走 `podcast_episode`,
manifest 分類完全一致。低階 `generate_audio`(無 manifest)也 fail-loud,訊息同句。

**續跑語意也驗了**:重呼 `podcast_series` 就地重送**同一** `attempt_id`
(`attempt_count: 1`、`superseded_attempt_count: 0`),errors 累積兩筆都保留、
`dispatched_at` 更新到重試時間 —— 與 troubleshooting 表對 `not_accepted` 的描述一致,
沒有膨脹 attempt、沒有多燒配額。

**走錯路是安全的**:對 `not_accepted` attempt 呼叫 `podcast_episode_reconcile`
(README 標為「不應該是」的那條)fail-loud:`attempt state 'not_accepted' cannot be
reconciled`。

**但這一輪也因此挖出兩個真 bug** —— 見 [[F-7]](診斷被抹掉)與 [[F-8]](死鎖)。
它們只在「audio 配額耗盡」這個狀態下才會出現,所以前一輪沒撞到配額 = 沒機會發現。

### 附帶發現:slides / report 與 audio 是**分開的配額池**

audio 全面被拒之後(4 次 dispatch 全 `RateLimitError`),`generate_slides` 在**同一本
notebook、同一時間**照樣受理並**完整跑完**:`output/quota/ep01-slides.pdf`,
11 頁 / 11,156,174 bytes,artifact `eeebe650-93e2-42e7-a016-4cee87d182f6`。
所以「一直生 slides 去撞配額」撞不到 audio 那道牆,也驗不到上面那套分類
(slides 在 `tools_artifacts.py`,不經過 `_REFUSED_WITHOUT_DISPATCH`,被拒只會單純 raise)。

### 發布產物(uploader 不刪檔,已永久上架)

```
feed:      https://podcast.example.com/feeds/FEED_TOKEN/feed.xml
show page: https://podcast.example.com/feeds/FEED_TOKEN/index.html
token:     aifq6wf3egpctcaadafrxsyp   (= HMAC(stg salt, "zz-test-v071"))
```
因 stg 的 `PODCAST_TOKEN_SALT` 與正式不同,落在完全不同的 URL 空間,不會與正式節目互撞。
`owner_email` 刻意填佔位值 `zz-test@example.com`,沒有把真實信箱推上公網。

### 證據檔(本機,可隨目錄一起刪)

| 路徑 | 內容 |
|---|---|
| `output/series_manifest.json` | 主線 manifest:EP01 retract + 重生、EP02、附件、show 設定 |
| `output/attempts/0cf07fab-…/ep01.mp3` | Phase 3 的取代版(頂層 `ep01.mp3` 是被拒收的首發版,兩者並存) |
| **`output/quota/series_manifest.json`** | **配額拒絕的完整證據**:EP01 保留乾淨的 `not_accepted` + `RateLimitError`;EP02 是被 [[F-7]] 抹成 `prepared`/`unknown`/`null` 的那筆,`errors[]` 仍留著真相 |
| `output/quota/ep01-slides.pdf` | audio 全拒時仍生成成功的 slides(配額池分開的證據) |

兩本測試 notebook(`ZZ-TEST v0.7.1 驗收`、`ZZ-TEST v0.7.1 配額拒絕`)已刪除,
`notebook_list` 確認只剩 stg 帳號原有的 18 本。刪除走 SDK 腳本(原因見 [[F-6]])。

---

## F-1 `strip_citations=True` 在 CJK 文本留下懸空空格 —— 判斷:**bug(低嚴重度,但落在公開輸出路徑)**

**症狀**
`chat_ask(strip_citations=True)` 清掉 `[1]` 標記後,標記前面的那個半形空格留在原地,
於是中文句子變成「難度非常高 。」「平方根的數學特性 :」——全形標點前多一個空格。

**重現步驟**(本輪實際觀察)
```
chat_ask(notebook_id=f2cef0bc…, question="為什麼抗碰撞強度只有雜湊輸出長度的一半?…",
         source_ids=[fca5dc63…, 23a49ada…])                     # 基準
  → answer 內含 "…需要約 \(2^{256}\) 次嘗試 [1]。"  ← 標記前有空格
chat_ask(同上, strip_citations=True, include_references=False)   # 對照
  → answer 內含 "…難度非常高 。" / "…數學特性 :" / "…機率就會變得非常高 。"
```

**根因**(`notebooklm_mcp/_text.py:8`)
```python
_CITATION_RE = re.compile(r"\[[\d,\s\-–]+\]")   # 只吃括號本身,不吃前置空白
```
`tools_basic.py:358` 與 `tools_artifacts.py:60` 都用它 `sub("")`。
`tools_artifacts.py:60` 後面接的 `.strip()` 只修字串頭尾,修不到句中。

**為什麼值得修**
`episode_set_description` 走同一個 regex,而那段文字會進 **公開 RSS 的 description**。
英文文本看不出來(", [1]." → ", ." 也怪但少見),中文散文每一句都中。

**建議**:`re.compile(r"[ \t]*\[[\d,\s\-–]+\]")`,或 sub 後補一輪 CJK 標點前空白清理。

**已在公開 RSS 上驗到(不是理論風險)**
本輪把 `chat_ask(strip_citations=True)` 的原始輸出照真實流程餵給
`episode_set_description` → `publish_series`,然後從公網抓回 feed.xml。EP01 的
`<description>` 實際含 **4 處**懸空空格:

```
… 這一恆等式推估延遲 。接著分析 … 而拉高尾端延遲 。在規模化 …
… 換取確定性的解方 。最後 … 從根本優化使用者體驗 。
```
(掃描結果:`['等式推估延遲 。', '拉高尾端延遲 。', '確定性的解方 。', '化使用者體驗 。']`)

podcast client 會照這樣顯示。EP02 的 description 是我**手動清掉空格後**才寫入的
—— 也就是說今天呼叫端必須自己補這一步,`strip_citations=True` 的承諾沒有走完。

---

## F-2 `max_chars=0` 必然回 `truncated=True`,該欄位在省 token 模式下無資訊量 —— 判斷:**預期行為,但介面有誤導風險(待確認)**

**症狀**
文件推薦的對帳姿勢 `max_chars=0, contains=[…]` 每次都回 `truncated: true`。
呼叫端若照字面理解「被截斷了,要調大 max_chars 重抓」,就會白花一趟 RPC 把全文灌回 context
——正好是這個模式想避免的事。

**實際觀察**
```
source_fulltext(…, source_id=fca5dc63…, max_chars=0, contains=[6 個關鍵詞])
→ {"char_count": 823, "hits": {...}, "truncated": true, "content": ""}
```

**根因**(`tools_basic.py:421-423`)`len(content) > max_chars` 在 max_chars=0 時恆真。
docstring 寫的是「`max_chars` 截斷時回 truncated=True」,所以嚴格說**行為與文件一致**。

**待確認**:是否要在 `max_chars == 0`(明示只要 metadata)時省略 `truncated`,
讓它只在「真的還有內容沒拿到」時才出現。不改也行,但 docstring 值得補一句。

---

## F-3 未傳 `conversation_id` 仍回同一個 conversation_id —— 判斷:**待確認(可能是 NotebookLM 每本一條 thread 的先天行為)**

**實際觀察**
兩次 `chat_ask` 都**沒有**傳 `conversation_id`,回傳卻是同一個
`9efe7891-48e0-46b4-9829-ad1fba6f7c42`。

docstring 說「pass conversation_id to continue a thread」,讀起來像「不傳 = 新開一條」。
若實際上同一本筆記本共用一條 thread,那第二次提問其實**帶著第一次的上下文**——
產 show notes 時可能吃到前一題的殘留語境(本輪第二次回答確實出現第一次沒有的
「簽章鏈的脆弱性」段落,但那也可能只是 LLM 的隨機性,無法只憑這點斷定)。

**待確認**:是 NotebookLM 端「每本一條對話」的先天限制,還是我們該在不傳
`conversation_id` 時顯式開新 thread。若是前者,docstring 應說明清楚,免得呼叫端
誤以為每次都是乾淨上下文。

---

## F-4 README 的污染對照「正向控制」在 TTS→ASR 下大半失效 —— 判斷:**驗收工具本身的 bug(README.md,不是 MCP 程式)。中等嚴重度:會讓未來某輪空跑卻看起來通過**

**症狀**
README 斷言「中文主題詞才活得下來」,並要求 EP01 回錄命中 `佇列/延遲/尾端`、
EP02 回錄命中 `雜湊/簽章/憑證`。實測**六個詞裡只有三個活下來**:

| 回錄 | 命中 | 沒命中 |
|---|---|---|
| EP01 延遲的形狀(14584 字) | `延遲` `尾端` | **`佇列`** |
| EP02 摘要與信任(9439 字) | `簽章` | **`雜湊`** **`憑證`** |

**根因:ASR 把術語磨掉了。** 直接抓 EP02 逐字稿前 700 字看到的實際文本:

```
… 一 份 是 探 討 雜 函 數 盲 點 的 筆 記 ， 另 一 份 是 拆 解 數 位 簽 章 跟 評 證 鏈 的 …
```

- `雜湊函數` → 「雜**函**數」(掉字)
- `憑證鏈`  → 「**評**證鏈」(同音錯字)
- 另有 `網址列`→「網子列」、`數位信任`→「數位性人」、`深談`→「生前」

補測替代詞後確認 EP01 是**用詞替換**(`佇列` 被講成 `排隊`):
`排隊`✓ `吞吐`✓ `負載`✓ `百分位`✓ `利用率`✓ / `隊列`✗ `序列`✗。
EP02 則是 `碰撞`✓ `生日`✓,但 `哈希`✗ `摘要`✗ `公鑰`✗ `證書`✗ —— 純粹被 ASR 打壞。

**為什麼這是 bug 而不只是雜訊**
這一輪的**負向**檢查(證明隔離的那一半)兩邊都乾淨,結論可信 —— 但那是因為正向控制
**剛好還有幾個詞命中**。如果哪一輪 ASR 把六個詞全磨掉,`contains` 會全回 false,
於是「不命中對方領域的詞」自動成立,測試**空跑卻看起來通過**。README 提醒過拉丁
canary 的這個陷阱(`contains=["KESTREL"]` 永遠 false 等於什麼都沒驗),但同一個陷阱
對中文詞也成立,只是機率低一點。

**建議**:README 的驗收步驟改成兩段式,把正向控制升級為**前置條件** ——
先確認該集回錄至少命中 N 個自家主題詞(命中數為 0 就是探針失效,該輪判定 inconclusive,
不可宣告隔離通過),再看負向。並把關鍵詞換成本輪實測存活的那組:
EP01 用 `排隊/延遲/尾端/吞吐/負載/百分位/利用率`,EP02 用 `簽章/碰撞/生日`。

---

## O-1 `podcast_series` 不支援 per-episode `source_ids` —— 判斷:**預期行為(刻意),但 README Phase 2 的措辭會誤導**

程式碼明說(`tools_podcast.py:2483-2486`):整季共用一組 source_ids 沒有意義,而每集的
回錄 source 要跑到那一集才存在、規劃階段填不出來,所以 series 不開這個參數;帶
`source_ids` 的 attempt(來自 `podcast_episode` 重生)在 series 路徑會 fail-loud。

README Phase 2 寫「EP01 …— brief 綁 `ep01-*` 兩份」,容易被讀成有硬隔離。**實際上
Phase 2 的隔離只能靠 brief 引導**,硬隔離只存在於 Phase 3 的 `podcast_episode`。
本輪因此把兩集的 brief 都寫成明確排除對方領域,負向對照才乾淨。
建議 README 補一句「Phase 2 無硬隔離,brief 必須顯式排除對方領域」。

---

## O-2 本機 `output/ep01.mp3` 其實是 fragmented MP4/AAC —— 判斷:**已知且已處理,不是 bug(0.8.0 下前提依然成立)**

`ffprobe` 實測兩檔:`ftyp dash` / `iso6mp41`、`codec_name=aac`、
`format_long_name=QuickTime / MOV`、44100Hz stereo。

`tools_publish.py:168-169` 已明寫「NotebookLM 原始下載常是偽裝成 `.mp3` 的
fragmented MP4/AAC;這裡直接轉成 256 kbps MP3」——**轉檔在 publish 時發生,不在下載時**。
所以 README Phase 4 的「實際 container=mp3」驗的是**發布後**的產物,本機檔案是 M4A 屬正常。

記錄這筆的價值:確認 **0.8.0 升級後 NotebookLM 仍然吐 fragmented MP4**,
`tools_publish.py:196` 的 `mp4_aac` 分支(`"mp4" in formats and codec == "aac"`)
仍會被觸發、仍然必要。若哪天上游改吐真 MP3,那個分支會靜默停用而沒人知道。

---

## F-5 `podcast_attempt_retract` 報出 `stale_artifact_id` 但沒有任何工具能處置它 —— 判斷:**能力缺口(低到中),不是平台限制**

**症狀**
retract 回傳 `stale_artifact_id: "d0e5a566-…"`,呼叫端**無從處理**:34 支工具裡沒有
artifact 刪除能力(`artifact_list` / `artifact_rename` / `artifact_retry_failed` /
`artifact_revise_slide` / 三支 download / `artifact_wait`,沒有 delete)。
於是被拒收的 Studio artifact 留在筆記本裡,而且**與取代版同名**。

**實際觀察**(Phase 3 重生後 `artifact_list(kind="audio")`)
```
4cbb3a39-…  EP01 延遲的形狀   completed  created_at 2026-08-08T10:56:06Z   ← 取代版
d6ea8ebd-…  EP02 摘要與信任   completed  created_at 2026-08-08T10:39:00Z
d0e5a566-…  EP01 延遲的形狀   completed  created_at 2026-08-08T10:24:40Z   ← 被拒收的,還在
```
對照 `source_list`:回錄 source 只有**一筆** `EP01 延遲的形狀`(舊的已按義務刪掉)。
所以 source 面乾淨、artifact 面留了同名重複。

**上游其實做得到**
```
notebooklm/_artifacts.py:646:  async def delete(self, notebook_id: str, artifact_id: str) -> None:
```
notebooklm-py 0.8.0 有 `artifacts.delete`,我們沒有暴露。

**危害評估(刻意壓低)**
- **不會**打壞 finalize:`_artifact_title_state`(`audio_finalize.py:351-355`)按 **id** 比對,
  不按標題,所以 rename postcondition 不受同名影響。本輪實測 rename 全 completed。
- 真正的成本:(a) Studio UI 出現兩個同名單集,人眼分不出哪個是現行版;
  (b) retract 的契約報出一個呼叫端無法結案的 id;(c) 長期反覆拒收會在筆記本裡累積
  廢棄音檔(Studio 有數量上限)。

**建議**:補一支 `artifact_delete`,並把 retract 的 `safe_next_action` 串成
`source_delete` → `artifact_delete`;若刻意不做,至少在 tool-reference 說明
`stale_artifact_id` 僅供記錄、無法處置,免得呼叫端去找不存在的工具。

---

## F-6 MCP 沒有刪 notebook 的能力,本工作區的收尾紀律因此無法執行 —— 判斷:**能力缺口(低),與 F-5 同一類**

**症狀**
`CLAUDE.md` §五 要求「測完把 notebook 刪掉:`notebook_list` 找出來刪乾淨」,
但 34 支工具裡與 notebook 相關的只有 `notebook_create` / `notebook_get` / `notebook_list`
—— **沒有 delete**。唯一的 delete 是 `source_delete`。

實測列舉:
```
34 tools
['notebook_create', 'notebook_get', 'notebook_list', 'source_delete']
```

**上游做得到**:`notebooklm/_notebooks.py:745  async def delete(self, notebook_id: str) -> None`

**影響**:拋棄式驗收 notebook 只能從 web UI 手動刪,或另寫腳本直接打 SDK(本輪就是這樣
收尾的)。合併 [[F-5]] 看,是同一個模式:**上游有 delete、MCP 沒暴露、而我們自己的
流程文件假設刪得掉**。

**建議**:補 `notebook_delete`(標 `destructiveHint=True`,與 `source_delete` /
`podcast_attempt_retract` 同級),或把 CLAUDE.md 的收尾步驟改成明確的 out-of-band 腳本。

---

## F-7 失敗的 `podcast_series` 呼叫會先把 attempt 重設、才驗 settings —— 把配額拒絕的診斷資訊**抹成 null**。判斷:**bug(mutate-before-validate),中等嚴重度**

**症狀**
對一個處於 `not_accepted` 的 attempt 呼叫 `podcast_series`,若 settings 不符,
它**先**把 dispatch/remote 重設成 `prepared`/`unknown`,**然後**才驗 settings 並拋錯。
呼叫失敗了,但狀態已經被改壞,而且被抹掉的正好是 README 要求必須帶得出來的那四個欄位。

**重現步驟**(本輪實測)
```
1. podcast_episode(episode_n=2, source_ids=[...], manifest_path=...)   # audio 配額已耗盡
   → raise;manifest 正確記下:
        dispatch.status   = not_accepted
        remote.status     = failed
        remote.error      = "API rate limit or quota exceeded. …(Upstream: Resource exhausted.)"
        remote.error_code = "RateLimitError"
        dispatch.dispatched_at = 2026-08-08T11:38:04Z
2. podcast_series(start=2, episodes=[...])       # 照 troubleshooting 表對 not_accepted 的指示
   → raise "episode 2 settings changed during a prepared attempt"
3. 再看 manifest:
        dispatch.status   = prepared        ← 被改
        remote.status     = unknown         ← 被改
        remote.error      = null            ← 被抹
        remote.error_code = null            ← 被抹
        dispatch.dispatched_at = null       ← 被抹
```

**為什麼嚴重**
- 抹掉後的 manifest 長得**正好像 README「不應該是」那一欄**(`remote.error=null`、
  `error_code=null`)。事後讀 manifest 的人分不出「配額拒絕」與「從沒送出去過」,
  會誤判成分類沒生效 —— 而它其實生效過,只是被後續呼叫毀掉。
- 抹掉 `dispatched_at` 之後,`podcast_episode_reconcile` 也跟著死:
  `attempt dispatched_at is required for reconciliation`。原本能走的路被這次失敗封掉。
- 唯一保住真相的是 `errors[]` 的歷史條目(仍在)。稽核軌跡對,現況欄位錯。
- 錯誤訊息本身也誤導:說「during a **prepared** attempt」,但呼叫進來時的狀態是
  `not_accepted`,`prepared` 是它自己剛剛改成的。

**根因**:`tools_podcast.py` 約 2455-2494 —— `_ensure_resume_attempt`(重設 dispatch 狀態)
跑在 `if dispatch_state == "prepared":` 區塊的 settings 比對**之前**。

**建議**:settings 比對移到任何 manifest mutation 之前(純讀 snapshot 就能比),
或在 raise 前把 remote 欄位回滾。至少不要把 `remote.error` / `error_code` /
`dispatched_at` 清成 null —— 那是診斷資產,不是暫存狀態。

---

## F-8 `podcast_episode` + `source_ids` 的 attempt 一旦撞到配額拒絕,**五支工具全部進不去**。判斷:**bug(死鎖),中等到高 —— 它正好卡在 README Phase 3 的主線上**

**症狀**
QA 拒收重生(`podcast_episode` 帶 `source_ids`)那一刻若剛好配額耗盡,該集就再也推不動。
本輪把五條路全試過:

| 出路 | 實際回應 |
|---|---|
| 重呼 `podcast_episode`(同參數) | `episode 2 already has durable active attempt '2a1a3274…'; reconcile or resume it before creating another attempt` |
| `podcast_episode_reconcile` | 拒絕前狀態:`attempt state 'not_accepted' cannot be reconciled`;被 [[F-7]] 抹過後:`attempt dispatched_at is required for reconciliation` |
| `podcast_episode_resume` | **結構上不可能**——必填 `artifact_id`,而 not_accepted 的 `remote.artifact_id` 是 `null`(什麼都沒 dispatch) |
| `podcast_series`(`start=2`) | `episode 2 settings changed during a prepared attempt`,**重現兩次,永久迴圈**(series 不吃 per-episode `source_ids`,見 [[O-1]]) |
| `podcast_attempt_retract` | `attempt '2a1a3274…' is not episode 2's durable output; only a promoted output attempt can be retracted` |

**為什麼 guard 的建議是死的**
`tools_podcast.py:218-236` 那道 guard:`supersedes_attempt_id != active_attempt_id` 就 raise,
而 **`supersedes_attempt_id` 沒有暴露成 `podcast_episode` 的工具參數**(schema 裡沒有),
所以永遠是 `None` → line 220 必定先 raise → 下面 229-232 那個
`remote.status in ("failed","removed")` 的白名單**從公開介面根本到不了**。
也就是說即使在 [[F-7]] 抹掉之前(當時 `remote.status` 就是 `failed`、剛好在白名單裡),
`podcast_episode` 也退不出來。

**為什麼這條路很現實**
README Phase 3 的正門就是 `podcast_episode` + `source_ids`,而它發生的時機**必然是
剛剛才生完整季、配額最緊的時候**。且 troubleshooting 表對 `not_accepted` 的指示是
「重呼 `podcast_series`」—— 對這種帶 `source_ids` 的 attempt 恰好是**唯一會把狀態改壞
的那個動作**([[F-7]])。文件把人往坑裡指。

**目前唯一脫出方式**:手改 manifest —— 而 ADR-0009 明令禁止,`manifest_store.py:146`
的註解也是這個意思(「正門與 backfill 腳本都打不開,唯一出路變成 ADR-0009 明令禁止的…」)。

**建議(任一即可解)**
1. 讓 `podcast_series` 在 attempt 帶 `source_ids` 時**沿用**它續生(而不是 fail-loud),
   或至少給出「這集要用 `podcast_episode` 續」的明確指引;
2. 允許 `podcast_attempt_retract` 作廢「掛在 `active_attempt_id` 但從未 dispatch 成功」
   的 attempt —— docstring 已經宣稱支援「未授權 candidate」,但實作只認 promoted output;
3. 或補一支明確的 `podcast_attempt_abandon`(僅限 `dispatch.status` 非 accepted 的 attempt)。

---

## F-9 `podcast_episode` 的 `not_accepted` 分支 bare raise,與 8 行下方的姊妹分支不對稱 —— 判斷:**bug(呼叫端指引缺失),低**

`tools_podcast.py:1178-1194` 同一個 try 的兩個 except 待遇不同:

```python
except _REFUSED_WITHOUT_DISPATCH as exc:
    if store is not None:
        _mark_not_accepted(store, episode_n, attempt_id, exc)
    raise                                    # ← 裸拋,沒有 attempt_id、沒有下一步
except (Exception, asyncio.CancelledError) as exc:
    if store is not None:
        _mark_acceptance_unknown(store, episode_n, attempt_id, exc)
        exc.args = (f"{exc}\n生成受理結果不明(attempt_id={attempt_id!r});"
                    "先對帳,禁止直接重生:"
                    f"podcast_episode_reconcile(manifest_path=…, episode_n=…, attempt_id=…)",)
    raise                                    # ← 把 attempt_id 與確切指令塞進訊息
```

**實測呼叫端只看到**:`API rate limit or quota exceeded. Please wait before retrying.
(Upstream: Resource exhausted.)` —— 沒有 attempt_id、沒說 attempt 已被持久化、
沒說下一步該做什麼。對照 `podcast_series` 在同一情境回的是結構化
`observed_state` + `safe_next_action` + `attempt_id`。

而 `podcast_episode` 的 docstring 還寫著「timeout/斷線後須依**錯誤中的** `attempt_id`
呼叫 `podcast_episode_reconcile`」—— 這個承諾只有 `acceptance_unknown` 分支兌現。

**建議**:比照下面那個分支補 `exc.args`,寫明 attempt_id 與 safe next action。
考慮到 [[F-8]],正確的 next action 目前其實**不存在**,所以這條要跟 F-8 一起修。

---

## O-6 `remote.status="failed"` 與 `dispatch.status="not_accepted"` 在 troubleshooting 表裡續跑語意相反 —— 判斷:**行為正確,文件會誤導(低)**

配額拒絕時 manifest 同時記著 `dispatch.status=not_accepted` 與 `remote.status=failed`。
skill 的 troubleshooting 表把這兩個值映到**相反**的續跑動作:

| 表上的值 | 表上說怎麼做 |
|---|---|
| `not_accepted` | 重呼 `podcast_series`:就地重送**同一** attempt_id |
| `failed` / `removed` | 重呼 `podcast_series` 會 **supersede 成新 attempt** 重送 |

**實測行為跟著 `dispatch.status` 走,是對的**:重呼 series 後 `attempt_count: 1`、
`superseded_attempt_count: 0`,同一個 attempt_id 就地重送,沒有 supersede。

但人工 triage 時最常看的是 `remote.status`(它才是「遠端怎麼了」),讀到 `failed`
會照第二列推論「會 supersede」,結論相反。建議 troubleshooting 表註明
**續跑語意一律看 `dispatch.status`**,`remote.status` 只描述遠端結果。

---

## O-4 簡報 PDF 抽不出文字,純文字污染掃描會**空跑** —— 判斷:**預期行為(NotebookLM 產圖片式簡報),但驗收步驟要改**

`pdftotext ep01-slides.pdf` 只吐 **14 bytes**(全是換頁符),14 頁一個字都抽不到。
於是關鍵詞掃描正負向**全部回 0**:

```
雜湊:0 簽章:0 憑證:0 碰撞:0 私鑰:0     ← 看起來「沒污染」
延遲:0 佇列:0 尾端:0 吞吐:0 百分位:0   ← 但正向控制也 0,證明探針無效
```

這是 [[F-4]] 描述的同一個陷阱在附件上重演:**沒有正向控制就無法區分「乾淨」與「沒驗到」**。
本輪改用 `pdftoppm -png` 渲染第 1 / 7 / 14 頁後目視確認(第 1 頁「系統延遲的殘酷真相:
佇列、尾端與規模的暴政」、第 7 頁「扇出效應 (Fan-out):1% 的機率,63% 的災難」、
第 14 頁「排隊讓系統看起來還活著…」),全部是延遲主題、無密碼學內容,才算驗到。

**建議**:README Phase 4 註明簡報必須渲染成圖檢查,不能用 `pdftotext`。
研讀講義(`ep01-report.md`)是純文字,掃描有效——本輪實測密碼學詞 0 命中、
正向控制 `延遲 22 / 佇列 7 / 尾端 8 / 吞吐 2 / 百分位 3 / 利用率 6`。
(附帶反證 [[F-4]]:書面產物完整保留「佇列」7 次,所以音檔回錄缺「佇列」確定是 ASR 造成。)

---

## O-5 照 README Phase 4 的步驟走,`publish_series` 會被自己的 preflight 擋下 —— 判斷:**README 缺一句話**

`require_slides` / `require_report` 的預設是 **True**(`tools_publish.py:340-347`
`saved_show.get(..., True)`),而且是**季級**政策 —— preflight 驗 manifest 裡**每一集**。
README Phase 4 只叫人為 **EP01** 產簡報與講義,所以 EP02 兩項皆缺,直接發布會 raise:

```
episode 2: slides_pdf_path 未回寫(簡報可能還在生成中)…
使用者明講整季不做這項才傳 require_slides=False
```

本輪按使用者裁示傳 `require_slides=False, require_report=False` 才發布成功。

**建議**:README Phase 4 第 4 步補「發布時要傳 `require_slides=False, require_report=False`
(本測試只為 EP01 產附件)」,否則下一輪會在這裡卡住並誤以為是 bug。

另記:首次發布還要**節目層**封面(`artwork_path`)與 show 七欄,README 沒提;
本輪補跑 `notebooklm-cover --show` 產 `covers/show.jpg`。

---

## O-3 nit:`tools_podcast.py:778-785` 有整段重複的註解

同一段四行註解貼了兩次,第二份把「仍」寫成 `still`(「回傳值 **still** 帶著…」)。
純 cosmetic,刪掉後半段即可。
