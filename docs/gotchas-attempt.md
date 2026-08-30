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
  **這條紅線只管「訊息從哪裡來」,不管「回傳完不完整」——那是下一條的守備範圍**
  （第十一次現形正是在這條紅線徹底遵守之後才發生的:`safe_next_action` 確實是
  `_attempt_capabilities()` 算的,但公開回傳漏了那個動作要用的目標身分)。
  **「外部前置條件不算 attempt 狀態,所以可以寫死」是假的 —— 那正是第八次現形。**
  這句話曾經逐字寫在這裡,用來合理化 `podcast_series` 的認證停點硬寫
  `safe_next_action="podcast_series"`(理由是「重登後重入冪等的 series,再由它交給
  capabilities」)。**冪等重入的前提是 series 接得住那顆 attempt**,而指名了
  `source_ids` 或綁著 frozen bundle 的 attempt 它接不住:重呼會走 supersede 分支、
  建一顆不指名的新 attempt、改讀整本筆記本,然後回報 `complete=True`。
  外部前置條件決定的是**這次停在哪裡**(`observed_state`),不是**下一步呼叫誰** ——
  後者永遠是 attempt 狀態問題,永遠要問 capabilities。現在由 `_series_handoff_caps()`
  回答,而它的第一個判準是「series 會不會在這顆上重新 dispatch」:順序反了的話,
  一集**已完成**的 output attempt 也帶著 `source_ids`,會被交棒指引拖去 retract
  一個已經做好的正式輸出(突變驗證確認)。
- **紅線(第十一次現形):`safe_next_action` 委派給另一顆 attempt 時,目標身分要跟著換,
  不能只換動作名。** `podcast_attempt_retract` 的 `post_retract` 分支發現
  `active_attempt_id` 指向另一顆還在飛的 attempt B 時,會遞迴算 B 的 capabilities 並
  交棒 `safe_next_action`——但上一輪(F1)只交棒了動作名,回傳裡的 `attempt_id`
  依然是被 retract 的那一顆(A,稽核主體)。呼叫端只讀公開回傳、照著 `safe_next_action`
  執行,實際上是拿 A 的 id 去做 B 的事:`podcast_episode_reconcile(A)` 撞 tombstone、
  `podcast_attempt_retract(A)` 冪等重跑撞回一模一樣的回傳形成無限迴圈。
  修法是 `_attempt_capabilities()` 額外算 `safe_next_attempt_id` / `safe_next_artifact_id`
  （`_safe_next_target()`,單一映射:認 `safe_next_action` 的字面值對應哪種身分,
  不重覆那條「哪個動作」的優先序 if/else——重覆等於下一次改優先序時兩邊漏改一邊,
  就是第十二次現形的種子）。**任何委派/交棒場景(把 `safe_next_action` 換成別顆
  attempt 算出來的值)都要連目標身分一起換**,回傳的稽核欄位(如 `attempt_id`)與
  「照做要用的身分」允許不同,但後者必須有自己的欄位,不能要求呼叫端自己去猜或去翻
  manifest。
- **紅線:凡是宣稱「照著回傳的下一步做走得通」的 E2E 測試,執行下一步時只准使用
  公開回傳裡的值,不准從 manifest 或 fixture 私下取 attempt_id / artifact_id。**
  這是上一條紅線的檢驗端:第十一次現形能夠合併發版都沒被抓到,是因為那條 E2E
  測試雖然真的呼叫了公開工具,卻在執行下一步時繞過公開回傳去 `_episode(manifest_path)
  ["active_attempt_id"]` 私下取值——12k+ 測試全綠,沒有一個真的在驗「回傳自不自足」。
  同一輪盤點另外抓到兩處同型(`test_adopt_source_replacement_queues_stale_ids_for_
  cleanup`、`test_ambiguous_uploaded_source_candidate_can_be_adopted_before_rename`):
  `podcast_series` 停在 `reconciliation_ambiguous` 時的公開回傳早就帶了正確的
  `attempt_id`,測試卻另外從 manifest 讀一次來驅動下一步呼叫——兩條路徑的值當時
  剛好相同、不影響測試通不通過,但代表「回傳給不給得出這個 id」這件事從來沒被驗證過。
  **raise 型的停點是例外**:`podcast_episode`/`podcast_series` 因例外中斷時只拋
  `ValueError`/`TimeoutError` 等,不帶結構化 dict,manifest 是當下唯一能拿到 id 的
  地方,讀它不違反這條紅線(這條紅線管的是「有結構化回傳可用卻繞過去」)。
- **紅線(第十二次現形):`safe_next_action` 認的身分不是 attempt_id 時,那個身分也要
  進公開回傳。** 前兩條講的都是「attempt 換了一顆」,而 `podcast_attempt_adopt` 必填
  `feedback_source_id` 或 `artifact_id` **之一**(它認的是候選的身分,不是 attempt 的),
  所以 `podcast_series` 停在回錄 source 上傳歧義(`observed_state="reconciliation_ambiguous"`、
  `safe_next_action="podcast_attempt_adopt"`)時只回動作名 = 呼叫端讀公開回傳執行不了。
  候選本來就存在 manifest 的 `finalize.feedback_source_upload.candidate_source_ids`
  (`_reconcile_source_upload` 寫入),**跟 artifact 對帳歧義的 `candidate_artifact_ids`
  是同一個家族,一併帶出**;`scripts/check_skill_sync.py` 的 `REQUIRED_CONTRACT_TERMS`
  兩個都鎖了。**同一輪要一起判斷的是另外兩個 `ACTION_ADOPT` 停點——它們刻意不帶候選,
  不是漏補**:`continuity_unverified`(已完成集的 feedback source 在遠端不見了)與
  `legacy_output_unverified`(舊 flat manifest 沒有明確 `feedback_source_id`)的出路是
  呼叫端自己 `source_list` 找出正確 id 再 adopt ——**不得以唯一同名來源推定 identity**,
  server 塞候選就是在幫它推定。判斷準則:候選是 server 自己在對帳時算出來、呼叫端無法
  重建的(時間窗)→ 必須帶出;身分本來就要人為指名的 → 不准帶。
- **(0.8.0)生成 kickoff 的同步拒絕改成 raise,不再回 `status="failed"`**(ADR-0019 / #1342)
  ——這會**悄悄改變 attempt 的終態分類**,是本次升級唯一需要動邏輯的地方。
  `_REFUSED_WITHOUT_DISPATCH` 只收契約講死「沒有建出 task」的兩種例外(誤判代價不對稱,
  **別讓這個集合長大**)。動這條路徑時**四個 except 要一起看**(v0.8.0 寫成「三個」時漏算了
  它自己新增的那一個):kickoff、`ensure_started`、`podcast_series` 呼叫 `_run_episode` 那圈
  (漏了它,整季會把例外拋出去而不是回安全停點)、以及 **series 自己 inline 重送那圈**
  ——最後這個當初無條件回 `not_accepted`,而 manifest 可能寫的是 `acceptance_unknown`,
  回報與紀錄相反。姊妹分支早就有「讀 manifest 覆核、例外型別只是入場券」的寫法,
  照抄過去即可。完整推導見
  [升級筆記](notebooklm-py-0.8-upgrade.md)的 §1。
- **「從未 dispatch 的 attempt」由建立它的那支工具原樣重呼續推**(`_is_resendable_same_request`)。
  帶 `source_ids` 的 attempt `podcast_series` 接不了,只有 `podcast_episode` 能續。
  **「逐字相同」是安全邊界** —— 設定變了還沿用等於靜默換掉生成輸入。連帶三條:
  ①`_reset_attempt_for_resend` 要 dispatch 與 remote **一起**清(只清一半會讓 series 看到
  `remote.status="failed"` 而誤判該 supersede);②診斷靠 `errors[]`(只 append),不靠
  `remote`;③**驗證一律先於變更**。事故經過見 [CHANGELOG](../CHANGELOG.md) v0.7.2。
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
- **`_assert_source_cleanup_done` 的執行順序本身就是安全性質,動它之前先讀那支的 docstring。**
  四個階段:①驗身分 → ②一次 `sources.list`、純計算不中途拋 → ③單次 revision-CAS mutation
  → ④寫完才 raise。每一段都對應一個實際重現過的缺口:身分檢查排在篩選之後 → 會把**別本
  筆記本裡碰巧同名同時間窗**的來源寫進清理義務、再叫呼叫端刪掉它;迴圈裡直接 raise →
  前面幾顆撈到的候選永遠沒落盤;`_claimed_source_ids` 用 await 前的 snapshot 而寫入不帶
  `expected_revision` → 併發 finalizer 剛認領的**合法** continuity source 會被排進待刪清單;
  清除放在 raise 之後 → 已經刪掉的 id 卡在 `pending_source_cleanup`,冪等 retract 一直教人
  去刪一個不存在的東西。**CAS 只掛在「有新發現要寫」那條路**:沒有新發現時 settle 對並行
  天生安全,硬要 CAS 會把「await 期間又有一次 retract 追加新義務」這個本來就被正確吸收的
  情形變成硬失敗。
- **清理義務的 notebook 身分以 `attempt["notebook_id"]` 為權威**,episode/manifest 只是
  fallback ——`manifest_store._validate` 明文允許 tombstone 保留建立時綁的舊 notebook,
  所以「換一本 notebook 重生」不能把舊本的孤兒洗掉。
- **作廢一顆「回錄 upload 還沒落盤」的 attempt 之後,最多 12 分鐘不能重生。** upload 停在
  `dispatching`/`acceptance_unknown`/`reconciliation_ambiguous` 且 `source_id` 還沒落盤時,
  retract(要帶 `abandon_in_flight`)會在 tombstone 記一筆 `source_cleanup_unresolved`,
  而 `_assert_source_cleanup_done` 在候選窗(`UPLOAD_DISPATCH_WINDOW`,dispatch 起算
  11+1 分鐘)關上之前一律 fail-closed —— **即使已經把撈到的孤兒刪掉也一樣**,因為窗還開著
  時晚到的 upload 仍可能再冒一筆出來。`safe_next_action` 在這個狀態是 `None`,不是重生入口。
  **想避開這個等待就別急著 retract**:照 capabilities 指的 `podcast_episode_resume` 續完,
  它會把那筆 source 的身分認回來,義務當場消失。
  這個代價是刻意換來的:**舊版對這個狀態硬擋 retract(連旗標都擋)**,而它靠一次 client
  cancellation 就能永久存在(`except Exception` 收不到 `CancelledError`),唯一出口
  `_reconcile_source_upload` 只從 `finalize_attempt` 進得去 —— 等於「要作廢一顆輸入本來就
  錯的 attempt,得先把它完整 finalize、上傳、promote」,比旗標本來要避免的後果還多一輪
  遠端副作用。候選判準只有一份(`unresolved_upload_candidates`),finalize 對帳與清理義務
  共用;**再長出第二份就等於保證有一天只有一邊被修到**。
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

## `source_delete` 的清理語意(ADR-0009 清理義務的執行面;從 AGENTS.md 降層,2026-08-30)

SDK 的 `sources.delete` 雖是 idempotent(0.7.0 起),但它的 mutation payload 只有 `source_id`、`notebook_id`
只是 routing header;公開工具 `source_delete` 會先用 `source_list` 驗證該 id 屬於指定 notebook,
**查無此 id 就不發那個 destructive RPC**(否則打錯 notebook 會刪到別本的來源),回 `was_present=False`。

**刻意不 fail-loud**:`podcast_attempt_retract` 的清理契約要求呼叫端照回傳的 `source_cleanup_obligations`
「逐一 `source_delete`」,而 response 遺失後重放整個迴圈是預期操作 —— 對已刪掉的那一筆拋錯會讓自動化 host
停在半路,剩下的 id 從此沒人刪。安全性質留在「不打 RPC」、冪等留在「不 raise」,所以 `idempotentHint` 保留。
`deleted` 只代表「呼叫後該 id 已不在這個 notebook」,要區分「本來就不在」看 `was_present`;要確認刪掉某既有
來源,先用 `source_list` 取真實 `source_id`。

⚠️ **刪除後 `source_fulltext` 用同一個 `source_id` 仍讀得回全文(實測 55 分鐘後仍可)—— 那是預期的,不是清理失敗**:
它繞過 notebook 直接查 source 物件,而生成用的來源清單與 `source_list` 同走 `GET_NOTEBOOK`。所以 ADR-0009 的
清理義務有效。推導的三個前提由
`tests/test_contracts.py::test_generation_takes_its_source_list_from_the_notebook_not_the_server`
釘住(**它紅就代表推導失效、清理義務要重新論證**),完整論證在該測試的 docstring。
