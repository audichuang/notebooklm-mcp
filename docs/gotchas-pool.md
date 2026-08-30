# Gotchas — 多帳號 pool:憑證、lifespan、dispatch

動 `app.py` 的憑證/lifespan、`runtime.py`、或任何 dispatch/failover 路徑之前讀。
決策推導在 [ADR-0010](adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md)(含
v0.9.0 / v0.9.7 amendment);本檔是**動 code 時的紅線與測試鎖**。

**這一區的共同形狀:破功不會當場出錯。** cookie 被本 process 重鑄,是另外兩台 VM 下次啟動才掛;
帳號記錯,是兩個帳號都成功所以事後查不出來。所以每條都配著「哪個測試紅了就代表這條路又開了」。

## 一、inline cookie 重鑄:一律關掉

- **(0.8.0)這條紀律延伸到所有「在本 process 內重鑄 cookie」的機制,inline auth 一律關掉。**
  新的兩個是 **L3 headless re-auth**(`NOTEBOOKLM_HEADLESS_REAUTH`,`app.py` lifespan 會顯式
  刪掉這個 env)與 **master-token headless auth**(`headless` extra,**刻意不採用**)。
  理由與取捨見[升級筆記](notebooklm-py-0.8-upgrade.md)的「新能力」一節。
- **(0.8.1)L2 inline PSIDTS recovery 的觸發條件變寬了,而 env 那道保護對 pool 不適用。**
  `from_storage(path=…)` 現在走 `HealPolicy.HEAL_THEN_NAME_ONLY`,routing preflight 是舊 gate
  的**超集**:PSIDTS 存在、值非空,但**已過期**或 **scope 打不到 `accounts.google.com`**
  時也算失敗 → `_recover_psidts_inline` → 本 process 內一次 `RotateCookies`。
  `_recover_psidts_inline` 說「`NOTEBOOKLM_AUTH_JSON` 設了就 decline」**擋不到我們**:
  `_resolve_recovery_path` 是「明確 `path` 優先,env 根本不看」,而 pool 每個槽位正是
  `from_storage(path=str(path))`(單帳號走無 path 那條,env 分支生效,本來就 decline)。
  **擋法是 `app._lifespan` 持有每個槽位檔的 rotation flock**(`_rotation_lock_path` +
  `keepalive._file_lock_try_exclusive`)—— 那是 `_recover_psidts_inline` 的第 4 個前提。
  三件反直覺、都被實測釘住的事,動這塊之前先讀:
  ①**目標是「每個 process 都不 rotate」,不是「跨 process 協調 rotation」** ——
  每個 process 各自持有自己 `mkdtemp` 路徑的鎖、各自擋自己的 heal;lock path 不同是設計。
  獨立審查兩次讀成後者並判定失效,實測 `rotate_POST=0`。
  ②`_file_lock_try_exclusive` 在 lock 不可用時 **fail-open 回 `True`**,但保護仍成立 ——
  `storage_lock._acquire_once` 先取 per-path 的 **in-process `threading.Lock`** 並在整個
  yield 期間持有,同 process 的 heal 因此拿到 `CONTENDED`。**這條是承重牆**,由
  `tests/test_client_pool.py` 的 fail-open 絆線守著,紅了就要改成直接讀 `LockState`、只認 `HELD`。
  ③routability 判準(`_cookies.would_trigger_inline_heal`)**只用來發 warning,不是接受條件** ——
  上游講死了它問的是「能不能 refresh」不是「能不能 use」,拿它拒收會誤拒:
  實測 prd 槽位 1 的 PSIDTS scope 在 `.youtube.com`,不 routable 卻一直在服役。
  完整推導與被推翻的兩個修法見 [CHANGELOG](../CHANGELOG.md) v0.9.14。
  **`describe_inline_heal_reason` / `heal_warning_detail` 也在這條紅線之內**:它們把失敗
  拆成 `wrong_scope` / `expired` / `missing`,但**只換措辭,不換判準** —— 決策出口仍然只有
  `would_trigger_inline_heal` 一個。理由是那則 warning 把兩個原因寫在同一個「或」裡,曾被
  讀成「登入要死了」並引出一次不必要的重登建議(對過 Doppler 與真 RPC 後不成立);
  拆開是為了讓人分得出「refresh 做不到」與「不能用」。
  **要看「能不能用」一律走 `auth_check`(真 RPC),不要拿 routability 當健康度** ——
  而且預設模式只量作用中那一槽,涵蓋整個 pool 要 `auth_check(all_slots=True)`。
  `test_heal_reason_is_message_only_and_never_gates` 紅了就代表分類器被接到 gate 上了。
  ④**這條在 v0.9.14 真實驗收於 stg 上得證**:stg 天生 9 槽全 routable(沒有現成素材),
  在**本機 env 層**造一個 scope 錯的槽位(`.youtube.com`,cookie 值不動)之後,
  不持鎖的對照組 `_attempt_rotation` 被呼叫 **1 次**、持鎖的真 `_lifespan` **0 次**,
  事後 Doppler 9 槽逐欄未變。**負向結論一定要配這個對照組** —— 沒有它,「沒看到
  RotateCookies」與「觀測手段沒接上」長得一模一樣。

## 二、憑證落檔:預驗證必須與 strict loader 同語義

- **(v0.9.0)pool 的憑證落檔,把好幾條原本靠「env 模式」早退的重鑄護欄一起降級了。**
  動 `app.py` 的憑證/lifespan 那一塊時三件事要一起看:①`_write_credential_file` 的預驗證
  **必須與 strict cookie loader 同語義**(必要 cookie 的 **value 也要非空**,不能只檢查 key
  在不在)—— 那是 L2 inline PSIDTS `RotateCookies` 唯一入口的門,而 L2 **不受
  `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 管**;②L3/L4 現在只靠「env 已刪」與「`master_token.json`
  不存在」擋著;③等價前提由
  `tests/test_client_pool.py::test_precheck_agrees_with_the_sdk_strict_loader` 守著,**它紅就
  代表這條路又開了**。破功的後果:3 VM 共用的 cookie 被本 process 重鑄、新的寫不回 Doppler,
  另外兩台下次啟動就掛,而本機正常啟動只有 debug 訊息。
  推導與取捨見 [ADR-0010](adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md) 的 v0.9.0 amendment。

## 三之〇、配額不是一個布林值,是 **per-kind**(v0.9.16 實測)

**這條直接影響整季怎麼排,而它此前沒有寫在任何地方** —— ADR-0010 的容量模型讀起來像
「一個帳號用完就換下一個」,實際不是:

| 桶 | RPC | v0.9.16 實測 |
|---|---|---|
| 簡報 / 講義 / 音檔生成 | `CREATE_ARTIFACT` | **免費帳號 slide_deck 每日 3 次**(2 個槽位一致) |
| 改版單頁 | `REVISE_SLIDE` | 生成桶耗盡後仍連過 9 次,**上限未知** |
| 音檔 | 同 `CREATE_ARTIFACT` 但**與 slides 分開計** | slides 被拒的**同一帳號、同一 process、相隔 2 分鐘**,audio dispatch 被受理 |

那個「同一帳號 slides 被拒 / audio 受理」是最硬的一條:**「這個帳號今天用完了」不是一個
布林值**,至少是 per-kind 的,所以「9 槽 × 3 = 每日 27 份簡報」這種算法才成立,而 audio
與 revise 各自另計。

⚠️ **兩個誠實的邊界,不要當成已證明的常數**:
- 「每日 3 次」是 **n=2 個槽位、同一天**量到的,一致但樣本小;也**沒有**驗過它是
  UTC 日界重置還是滑動視窗。要按它排整季之前先自己再量一次。
- 「拒絕不消耗配額」是**推論不是觀測**:耗盡後又送了 12 次 kickoff,拒絕依然是拒絕、
  artifact 差集全空 —— 這與「拒絕不計數」一致,但觀測不到計數器,分不出「沒被計」與
  「計了但早就在上限」。

## 三、dispatch 與 failover:四條實作紀律

- **多帳號 pool 動 dispatch/認證前必讀 [ADR-0010](adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md)**
  (含 v0.9.0 / v0.9.7 amendment)。**v0.9.7 起冷卻不是永久除名**:被拒的帳號 600 秒後會
  重新變成候選(真實驗收量到同帳號被拒 26 分鐘後又被受理),`rotate_client()` 的游標會
  環狀 wrap 回去,別再假設「一輪之內同一個帳號只會被拒一次」。四條實作紀律,每條都被
  真實事故驗證過、都有測試鎖:
  ①**`podcast_series` 有兩條 dispatch 路徑**(全新一集走 `_run_episode`、重送/supersede 是
  series 自己 inline),**兩條共用 `_dispatch_audio_with_failover`** —— v0.8.0 只補了一條,
  於是 pool 對「重試」這條最需要它的路完全無效。
  ②**身分跟著 client 走,不准放進 process 全域**:每個槽位各自一份 storage_state 檔,
  `from_storage(path=…)`。MCP 是並行的,一個全域槽不可能同時是兩個值,**加鎖也救不了**。
  ③**記帳與送出必須同源**:`runtime.snapshot()` 一次取 `(label, client)` 往下傳,
  `_dispatch_audio_with_failover` 回 `(artifact_id, account, client)`,failover 換帳號時
  一起換。分開讀 `active_account()` 與 `get_client()`,並行 rotate 會落在 await 的縫裡 ——
  manifest 記 A、實際 B 送出,而**兩個帳號都成功,所以事後查不出來**。
  ④failover **只掛在 `_REFUSED_WITHOUT_DISPATCH` 那一條 except**(契約保證沒建出 task)。
  **`ensure_started` 拋了不等於沒建出 task** —— 它的條件是 `is_failed or not task_id`,而
  「有 id + failed」在 SDK 路徑上可達,那種形狀 rotate 重送會產生**第二個 artifact**;
  只有真的沒有 id 才算零副作用。已 dispatch 之後換帳號 = ADR-0009 禁止的改寫因果紀錄。
  `tests/test_pool_gaps.py`(經突變驗證)是這區最敏感的守門員。

> 從 `AGENTS.md` 外移(2026-08-16):內容一字未改,只是改成**按需載入** —— 三條都只在動
> 憑證/lifespan/dispatch 時才用得到,而它們合計 59 行,占了主檔 §Gotchas 的四成。
> 主檔留一條紅線指過來。
