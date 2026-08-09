# Gotchas — attempt 狀態機與 manifest

動 `tools_podcast.py` 的 attempt 建立/續推/promote,或 `manifest_store.py` 之前讀。不變式的正本是 [ADR-0001](adr/0001-persist-attempt-before-remote-side-effects.md) 與 [ADR-0009](adr/0009-retracted-attempts-are-tombstones.md)。

> 從 `AGENTS.md` 外移(2026-08-10):內容一字未改,只是改成**按需載入** —— 它們只在動到
> 這個子系統時才用得到,留在永遠載入的 AGENTS.md 只會擠掉真正每次都要看的那幾條。

---

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
