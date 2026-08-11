# Gotchas — attempt 狀態機與 manifest

動 `tools_podcast.py` 的 attempt 建立/續推/promote,或 `manifest_store.py` 之前讀。不變式的正本是 [ADR-0001](adr/0001-persist-attempt-before-remote-side-effects.md) 與 [ADR-0009](adr/0009-retracted-attempts-are-tombstones.md)。

> 從 `AGENTS.md` 外移(2026-08-10):內容一字未改,只是改成**按需載入** —— 它們只在動到
> 這個子系統時才用得到,留在永遠載入的 AGENTS.md 只會擠掉真正每次都要看的那幾條。

---

- **紅線:任何 `safe_next_action` 與狀態相關的指引訊息,一律由 `_attempt_capabilities()` /
  `_attempt_next_step()` 產生,不准手寫 if/else。** 這個根因已經現形七次,每次的形狀都是
  「指引在它自己產生的狀態下不可執行」——v0.9.1 FAIL-1(叫人跑一支在該狀態下自己也
  permission denied 的工具)→ v0.9.3(停點指向會拒收它的工具)→ v0.9.4 FINDING-2
  (拒絕訊息那句在該狀態下是假的)→ v0.9.5 一次六條(其中三條是修上一條時自己種的)
  → v0.9.6(把「可免旗標 retract」誤當「可原樣重送」,`failed`/`removed` 兩者答案相反,
  共用一個布林就必然教錯一邊)→ v0.9.7 FINDING-4 → **這一輪的 `_reuse_frozen_input_attempt`**
  (rearm 卡住時手寫 `f"frozen attempt is {status!r}; reconcile or resume it instead of
  redispatching"`,沒有查 `_attempt_capabilities` 就叫呼叫端去做在那個狀態下不一定做得到
  的事)。**每次的修法都是「補那一格」**——判斷分散在各自為政的布林 + 十幾處手寫訊息裡,
  永遠會有沒補到的下一格,這條紀律沒寫進常駐文件正是第七次現形的直接原因。
  `_attempt_capabilities()`(單一事實來源,對狀態組合的笛卡爾積由
  `tests/test_attempt_capabilities.py` 逐格鎖住)與 `_attempt_next_step()`(把結論翻成
  一句可執行的話)已經把這個結構收斂掉了:新程式碼要教呼叫端「下一步該做什麼」,
  **先查這兩個函式有沒有覆蓋這個狀態**,沒覆蓋就擴充它們的笛卡爾積或加新 key,
  不要在呼叫點旁邊再長一條 if/else——那條 if/else 就是第八次現形的種子。
- **(0.8.0)生成 kickoff 的同步拒絕改成 raise,不再回 `status="failed"`**(ADR-0019 / #1342)
  ——這會**悄悄改變 attempt 的終態分類**,是本次升級唯一需要動邏輯的地方。
  `_REFUSED_WITHOUT_DISPATCH` 只收契約講死「沒有建出 task」的兩種例外(誤判代價不對稱,
  **別讓這個集合長大**)。動這條路徑時**四個 except 要一起看**(v0.8.0 寫成「三個」時漏算了
  它自己新增的那一個):kickoff、`ensure_started`、`podcast_series` 呼叫 `_run_episode` 那圈
  (漏了它,整季會把例外拋出去而不是回安全停點)、以及 **series 自己 inline 重送那圈**
  ——最後這個當初無條件回 `not_accepted`,而 manifest 可能寫的是 `acceptance_unknown`,
  回報與紀錄相反。姊妹分支早就有「讀 manifest 覆核、例外型別只是入場券」的寫法,
  照抄過去即可。完整推導見
  [升級筆記](docs/notebooklm-py-0.8-upgrade.md)的 §1。
- **「從未 dispatch 的 attempt」由建立它的那支工具原樣重呼續推**(`_is_resendable_same_request`)。
  帶 `source_ids` 的 attempt `podcast_series` 接不了,只有 `podcast_episode` 能續。
  **「逐字相同」是安全邊界** —— 設定變了還沿用等於靜默換掉生成輸入。連帶三條:
  ①`_reset_attempt_for_resend` 要 dispatch 與 remote **一起**清(只清一半會讓 series 看到
  `remote.status="failed"` 而誤判該 supersede);②診斷靠 `errors[]`(只 append),不靠
  `remote`;③**驗證一律先於變更**。事故經過見 [CHANGELOG](CHANGELOG.md) v0.7.2。
  **現況更新**:這支函式現在只做一件事——真的要重送時的逐欄位相等閘門(notebook /
  標題 / brief 雜湊 / settings / frozen bundle 綁定,少一個都不放行)。它**不再是**
  「能不能重送」這句指引訊息的來源——那個角色已經被上一條紅線講的 `_attempt_capabilities()`
  接手,它的 `can_resend` 欄位用的是更粗的判準(`never_dispatched and not is_output`),
  因為產生指引訊息的當下往往連呼叫端下一次會帶什麼請求都還不知道,沒東西可比對逐字
  相同。兩者故意不是同一個判準,別把它們合併成一個。
- **`published_at` 是「首發時間」不是「產製時間」**(v0.6.0):retract 必須把它 pop 進
  `retraction.retracted_output`(它是 `has_hard_output_evidence` 的硬證據,留著會擋死重生),
  但 promote 補回時要走 `_first_published_at()` ——沿 attempts 建立順序找**第一筆非空**的
  `retracted_output.published_at`(abandon 分支留的是空 dict,要跳過)。舊行為用
  `setdefault` 補成重生當下的 wall clock,GUID 不變(同集更新)pubDate 卻漂,episodic feed
  按 pubDate 倒序 → 重生集跳到列表最前(saa-drill EP05/EP09 實際事故)。**改 code 不會回溯
  既有 manifest**,用 `scripts/backfill_published_at.py`(dry-run 預設,走 ManifestStore)。
  **修正欄位時,manifest 與「回傳給呼叫端的那份 dict」是兩個出口**:v0.6.0 第一版只在
  `mutate` 裡蓋掉 manifest,四個呼叫點卻都是 `_promote_attempt_output(…, output);
  return output` —— feed 對、回傳值仍是重生時刻(真實驗收抓到:manifest 12:07:20、
  回傳值 12:16:39)。所以 `_promote_attempt_output` 會把生效值寫回 `output`,四條路徑
  (episode / resume / series 兩處)一起正確。**測試要同時斷言 manifest 與回傳值**,
  只驗前者正是這個 bug 溜過去的原因。
- **manifest 的 notebook 一致性只擋寫入、不擋讀取**(`_validate(..., on_write=True)`):
  episode 與其非 retracted attempt 的 `notebook_id` 必須一致,但 v0.5.0 之前建立 attempt
  沒有這道 guard,線上可能已有分裂的 manifest。讀取端也驗會讓它們每次 `read()` 都 raise,
  連 `podcast_attempt_retract`(修復正門)與回填腳本都打不開,唯一出路變成 ADR-0009 禁止的
  手改 JSON。**壞資料要讀得進來,才修得掉**;retracted tombstone 一律豁免(否則 retract 後
  換 notebook 重生就寫不進去)。
- **（F7,v0.9.10 盲審,只記不改)算 caps 的位置,adopt/retract 跟 reconcile 不一致**:
  `podcast_attempt_adopt`(`_attempt_capabilities` 在 `store.update` 的 mutate closure
  **裡面**算,拿 `store.update` 已經回傳的一致 manifest)與 `podcast_attempt_retract`
  同樣如此;但 `podcast_episode_reconcile` 的四個回傳點丟掉 `store.update` 回傳的一致
  manifest,改在鎖**外面**重新 `store.read()` + `_attempt_record()`。單 process 不可達
  (`store.update`/`store.read()`之間沒有東西會改這顆 attempt,需要第二個 writer
  process 才會讓兩次讀出現落差),所以這一輪沒有改 code——改了也證不了,需要兩
  process 的整合測試才驗得出來。之後要動 reconcile 的回傳點時,順手改成跟
  adopt/retract 一樣在鎖裡面算,別再長出第三種寫法。
- **(F8,v0.9.10 盲審,只記不改)`abandon_in_flight` retract 會永久停用該 notebook
  的候選自動綁定**:`_unresolved_attempt_ids()` 掃同一本 notebook 底下所有
  `dispatch.status` 落在 `dispatching`/`acceptance_unknown`/`reconciliation_ambiguous`
  且還沒 claim 到 artifact 的 attempt,**不排除已經 retract 的**——`podcast_attempt_retract`
  不會改動 `dispatch.status`,只加 `retraction` 欄位,所以一顆用
  `abandon_in_flight=True` 作廢的 attempt(它的 dispatch 永遠停在作廢當下那個「還在飛」
  的狀態)會被 `_unresolved_attempt_ids()` 永久算成「未解決」,擋住這本 notebook 之後
  **每一集**的唯一候選自動綁定(改停在 `reconciliation_ambiguous` 要求手動
  `podcast_attempt_adopt`)。這是刻意的設計取捨——否則遲到的 orphan artifact 可能被
  誤綁給不相干的新 attempt——不是 bug,但代價要記下來。否定答案(「確認這顆候選不屬於
  這次 dispatch」)的出路有端到端測試鎖著:
  `tests/test_audio_attempts.py::test_tombstone_blocker_offers_and_executes_the_negative_candidate_path`。
