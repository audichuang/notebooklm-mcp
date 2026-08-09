# Changelog

各版本**改了什麼、為什麼**。AGENTS.md 只放**還在生效的紀律**;版本敘事、事故經過、
已經修掉的舊行為都放這裡,免得 AGENTS.md 被歷史撐爆。

深入的專題另有獨立文件:
[notebooklm-py 0.8.0 升級筆記](docs/notebooklm-py-0.8-upgrade.md)、[ADR](docs/adr/)。

---

## v0.8.2

v0.8.1 的真實驗收(stg 四個命題全綠 + prd 端到端四集回歸)之後的補完。

### 新增

- **`notebook_share_with_pool`** —— 把**既有** notebook 補分享給 pool 其餘帳號(EDITOR)。
  v0.8.1 的自動分享只對 `notebook_create` 生效,而正在跑的專案 notebook 都是既有的;
  沒有這個前置狀態,配額耗盡 failover 換帳號時會 `NotebookAccessDenied`,而在這支工具
  之前唯一的補法是自己寫 SDK 腳本。**冪等**(已有權限的帳號跳過),單帳號是 no-op。

### 修正

- `notebook_get` 的 docstring 講明 **`is_owner` 在 notebook 有共享者時一律回 `False`**
  (驗收 G-1:同一份 owner 憑證,移除共享者後同一欄位才變 `True`)。多帳號 pool 下自動
  分享是常態,這個欄位實務上恆為 `False`,**不能拿來判斷歸屬**。行為來自上游 SDK,
  沒有任何生產邏輯依賴它,所以照實轉發 + 文件說清楚,不悄悄拿掉欄位。
- 補上 v0.8.1 漏 commit 的 `uv.lock` 版本號。

### 驗收結果(v0.8.1,四個命題全綠)

- **重送/supersede 路徑也 failover、也記帳號**(F-4 的修正成立):同一 attempt 重呼後
  `errors[]` 3→6,新增兩筆新時間戳的 `dispatch_failover`,`attempts` 仍是 1、無 supersede。
- **下載用作用中帳號的身分**(F-1):把舊 notebook 的共享移除到只剩 owner,下載仍成功。
- **自動分享**(F-2)在 pool=5 下也成立(`shared_with` 回 4 個帳號)。
- **permission denied → `not_accepted`**,訊息指名要分享;不再往下 rotate。
- 走錯路兩條都安全:對 `not_accepted` 跑 reconcile 是純本機拒絕、manifest 一個位元沒動。

### 仍待確認(不在這一版)

F-3(finalize 失敗原因不進 `errors[]`)、F-5(暫時性失敗的 `remote.error` 只有一句
`failed`)。兩者都是既有行為,影響的是出事後的可查性,不是成功路徑。

---

## v0.8.1

v0.8.0 的真實驗收(stg 三個免費帳號,三個帳號全部打爆)抓到的三個 P0 + 一個設計缺口。
**核心命題成立**:EP04 撞到 A 的配額 → 1.43 秒同步拒絕 → 換到 B → 4.4 秒後受理。
notebook 的 owner 是 A,以 EDITOR 身分的 B 送出就過了 ⇒ **配額算發起者不算 owner**,
ADR-0010 標成「操作者拍板但未實測」的前提現在是事實,這一版不作廢。
兩次 failover(A→B、B→C)都拿到,per-process 輪替不回頭撞舊帳號也實測成立。

### 修正

- **下載身分綁死在 pool 最後一個槽位**(F-1,P0)。pool 建構把 `NOTEBOOKLM_AUTH_JSON`
  當暫存槽,迴圈結束後 env 停在最後一個憑證,而還原寫在 lifespan 最外層的 `finally`
  —— 那是 **server 關閉**才跑。而 notebooklm-py 的媒體下載**在下載當下重讀那個 env**,
  不是用 client 自己的 session(驗收已隔離重現:同一個 client 只要換掉 env,下載身分
  就跟著換)。結果:不管作用中的是哪個帳號,**下載永遠以最後一個槽位的身分發出**,
  notebook 沒分享給它就一律 401,而症狀出現在十幾分鐘後的 finalize。
  修法:憑證跟 client 一起存進 pool,`set_clients()`/`rotate_client()` 同步 env。
  只「建完還原成第一個」不夠 —— failover 換到 B 之後 artifact 屬於 B。
  **測試盲區一併修**:原測試在 `async with` **退出後**才斷言 env,那時最外層 finally
  已經還原過,bug 正是從這個縫溜過去的。
- **重送/supersede 路徑沒 failover 也沒記帳號**(F-4,P0)。`podcast_series` 有兩條
  dispatch 路徑,v0.8.0 只補了「全新一集」那條。於是配額耗盡後隔天原樣重呼
  (**工具自己給的 `safe_next_action`**)不會換帳號,pool 對「重試」這條最需要它的路
  完全無效;`dispatch.account` 落地是 null,那一集永久答不出誰生的。
  修法:兩條路徑共用 `_dispatch_audio_with_failover`,只把例外翻成 series 的結構化
  安全停點。`_reset_attempt_for_resend` 也 pop 掉 `account`(留著會變成**過期值**,
  比缺漏危險)。續跑指引從 helper 拉回呼叫端 —— `_mark_not_accepted` 會把 `str(exc)`
  寫進 `remote.error`,寫死一種指引等於讓**錯的**工具名落進 manifest。
- **換到的帳號沒權限被誤標成 `acceptance_unknown`**(F-2,P0)。permission denied
  (`ClientError` rpc_code=7)掉進泛用 except → 「先對帳、禁止直接重生」,把一個
  **確定沒發出去**的請求叫去跑註定撈不到東西的 reconcile,正是 v0.7.1 那類死鎖的形狀。
  新增 `NotebookAccessDenied`,**刻意繼承 `RuntimeError`** —— 兩個 dispatch 呼叫端
  本來就把它當「沒建出 task 的乾淨終態」,分類自動正確,不必兩處各加分支。
  **不放進 `_REFUSED_WITHOUT_DISPATCH`**(那個集合的契約是配額/限流),**也不 rotate**
  (權限是設定問題,逐一試過去只會掩蓋根因)。

### 新增

- **`notebook_create` 在 pool 模式自動分享給其餘帳號**(EDITOR,`notify=False`)。
  failover 的前置狀態先前沒有任何機制建立 —— 驗收時是人工用 SDK 補上才走得動。
  回傳值新增 `shared_with`。**單帳號時完全不打 RPC**,現行機器行為不變。
  既有的 notebook(v0.8.1 之前建的、或手動建的)仍需自行分享一次。

### 待確認(不在這一版)

- finalize 階段的失敗原因沒有落進 `errors[]`(F-3):`download` 失敗時 manifest 只記
  `status="failed"`,「為什麼」只存在於工具回傳的例外訊息裡,session 一結束就沒了。
- 暫時性生成失敗的 `remote.error` 只有一句 `failed`(F-5)。配額拒絕那筆是完整的。

兩者都是既有行為、不是 v0.8.0 引入的回歸。

---

## v0.8.0

多帳號配額 pool。Google One 家庭方案下有 5 個付費帳號,但一個 process 只綁一份
`NOTEBOOKLM_AUTH_JSON`,某帳號當日配額用完整條生成線就停住 —— 即使 pool 裡還有 4 個
活的帳號。決策與已實測的前提見
[ADR-0010](docs/adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md)。

### 新增

- **lifespan 支援 N 個帳號**。Doppler 同一個 config 注入 `NOTEBOOKLM_AUTH_JSON` +
  `_2`/`_3`…,各建一個長駐 client。**`get_client()` 的語意刻意不變**(「當前作用中的
  那一個」)→ 46 個呼叫點、安裝指令、MCP 註冊、client 端一行都不用動,`dev` 這種
  單憑證 config 的行為完全照舊(遷移因此可以逐台進行,漏改的機器照樣能跑)。
  憑證輪流覆寫 env 再 `from_storage()`:SDK 只認不帶後綴的那個 key
  (`_auth/cookies.py`),而 `AuthTokens` 沒有 `from_json`。啟動期單線程、建完還原,
  憑證不必落到檔案系統。
- **編號缺號 fail-loud**:`_2` 沒設但 `_4` 有 = Doppler 打錯一個字的形狀。靜默跳過會讓
  那個付費帳號永遠不進 pool,而症狀只是「配額比預期早用完」,幾乎查不回根因。
- **配額被拒就換帳號,原地重送同一個 attempt**。實測配額耗盡是**同步拒絕**
  (1.35 秒、`dispatch.status=not_accepted`、`remote.artifact_id=null`),落在
  `_REFUSED_WITHOUT_DISPATCH` 這條「契約保證沒建出 task」的路上 → 重送是冪等的:
  **同一個 `attempt_id`,不 supersede、不新建 attempt、不多燒一次配額紀錄**。
  兩種形狀(0.8.0 起 raise、0.7.x 回 `task_id=""` 由 `ensure_started` 判定)收進
  同一個 `_dispatch_audio_with_failover`,分兩處各補一次正是本 repo 反覆出事的「補一半」。
- **`dispatch.account`**(單帳號也記)+ **`errors[]` 的 `dispatch_failover`**
  (from/to/理由)。對 client 透明可以,對稽核紀錄不行 —— 沒有它「EP35 是誰生的」
  事後答不出來。**選 `errors[]` 不是隨便選的**:`_reset_attempt_for_resend` 會把
  dispatch/remote 清回 prepared,診斷放那裡會被抹掉。

### 邊界(刻意不做)

- **已 dispatch 之後的失敗不換帳號**。`status="removed"` 看起來像配額問題,但那時
  task 已存在,換帳號等於 retract + 取代版 —— 讓 MCP 在背後改寫 manifest 的因果紀錄,
  正是 ADR-0009 禁止的事。維持既有的結構化安全停點,`podcast_series` 那圈因此**一行未改**
  (它的判準是 `dispatch.status != "not_accepted"`,failover 對它完全透明)。
- **認證失效不 failover,大聲停住**。靜默吸收過期憑證的 pool 會一路吸到沒帳號可用。
- **`store is None`(standalone 無 manifest)不換帳號**:沒有地方寫稽核紀錄。
- **輪替是 per-process 持續,不是 per-attempt 重設**:今天耗盡的帳號今天就是耗盡了,
  每集都先撞一次等於每集多燒一趟 RPC、多一筆假的 failover 紀錄。

### 尚未驗證

**真實的 failover 一次都沒跑過** —— rotate 只在 mock client 上測過。`stg` 的三個
免費帳號就是為此存在(免費配額低,打得爆才測得到)。它同時會驗證「配額算**發起者**
不算 notebook owner」這個**操作者拍板但未實測**的假設 —— 若配額算 owner,換呼叫端
帳號毫無作用,這一版的功能全部作廢。

---

## v0.7.2

真實驗收(v0.7.1,測試帳號)抓到的三個 bug。**觸發條件都是「配額拒絕」**,而那正是
v0.7.0 換過契約的那條路徑 —— 離線測試證明得了分類,證明不了整個情境走得通。

### 修正

- **`podcast_episode` 的 attempt 撞到配額拒絕後不再死鎖**(驗收 F-8,中到高)。
  QA 拒收重生(`podcast_episode` + `source_ids`)剛好撞到配額時,該集**五條出路全被擋死**:
  重呼被 durable-attempt guard 擋、`reconcile` 拒收 `not_accepted`、`resume` 必填的
  `artifact_id` 是 null、`podcast_series` 因 settings 不符永久迴圈、`retract` 只認已
  promote 的 output。唯一脫出是手改 manifest —— ADR-0009 明令禁止。
  根因是 `supersedes_attempt_id` 只有 `podcast_series` 會傳,所以
  `tools_podcast.py` 的 guard 必定先 raise,底下的 `failed`/`removed` 白名單**從公開
  介面根本到不了**;而 `retract` 的 `abandons_unauthorized_candidate` 又要求該集已有
  promoted output,retract 流程剛好把它清掉了。
  **修法**:`_is_resendable_same_request` —— 從未 dispatch 的 attempt,只要這次請求
  (notebook / 標題 / brief 雜湊 / settings / frozen bundle)**逐字相同**就沿用它重送。
  選「讓建立它的那支工具自己重送」而不是新增作廢工具:attempt 是 `podcast_episode`
  建的就該由它推進,呼叫端的動作是**原樣重跑同一個呼叫**,與 series 的續跑語意一致。
  同 pattern 已存在於 `_reuse_frozen_input_attempt`。
- **失敗的 `podcast_series` 呼叫不再抹掉拒絕證據**(驗收 F-7,中)。
  `_rearm_not_accepted_attempt` 跑在 settings 驗證**之前**,於是一個註定失敗的呼叫
  仍會先把 `remote.error` / `error_code` / `dispatched_at` 清成 null。抹完的 manifest
  長得正好像「分類從未生效過」。改成**驗證先於變更**(`_assert_series_owns_attempt`
  提前到任何 mutation 之前)。
- **拒絕分支不再裸拋**(驗收 F-9,低)。`podcast_episode` 的 `not_accepted` 分支現在
  比照 8 行外的 `acceptance_unknown` 姊妹分支,把 `attempt_id` 與確切的續跑指令寫進
  例外訊息。docstring 承諾「依錯誤中的 `attempt_id` 續跑」,先前只有一個分支兌現。
- **`podcast_series` 接不了的 attempt 會指名誰接得了**(驗收 O-6)。訊息從「settings
  changed」改成明講「這個 attempt 帶 per-episode `source_ids`,請用 `podcast_episode`
  原樣重呼」。舊訊息把人往死路指。

### 內部

- 新增 `_reset_attempt_for_resend`:`_rearm_not_accepted_attempt` 與同請求沿用分支共用。
  **dispatch 與 remote 必須一起清** —— 只清一半會讓 series 看到 `remote.status="failed"`
  而誤判該 supersede。診斷靠 `errors[]`(只 append)保存,不靠 `remote`。

---

## v0.7.1

- **認證失效訊息不再指向壞掉的登入指令**。`probe_auth` 的 `RELOGIN_HINT` 原本叫人跑
  `notebooklm login`,但 Google 已把登入流程搬到 `notebook.google.com`,SDK(含 0.8.0)
  偵測還沒跟上,照著跑會卡滿 5 分鐘。**這段訊息出現的時機正是使用者最會照著做的時候**,
  指錯等於把三十秒的修復變成五分鐘的困惑,而且會把人誤導到「NotebookLM 搬家了」。
  改指向 `scripts/login_notebooklm.py`,並有逐行 tripwire 鎖著。

---

## v0.7.0 — 依賴升到 notebooklm-py 0.8.0(**breaking ×2**)

完整推導見 [升級筆記](docs/notebooklm-py-0.8-upgrade.md)。

### Breaking

- **server 命令 `notebooklm-mcp` → `nblm-mcp`**。`notebooklm-py` 0.8.0 自己也宣告了一支
  同名 console script,同一個 uv tool venv 只留最後寫入的那份 —— 實測**全新安裝 3/3 拿到
  上游那支**(缺 `fastmcp` 直接 ModuleNotFoundError),等於裝完就是壞的。
  消費端必須改呼叫名並重下 `claude mcp add-json` 註冊。
  **這件事讀 changelog 讀不出來、`uv tool list` 也看不出來,只有真的跑一次 `--help` 才會發現。**
- **依賴 `notebooklm-py>=0.8,<0.9`**(ADR-0019 錯誤契約:缺席與拒絕改 raise)。

### 行為修正

- 生成 kickoff 的配額/限流拒絕在 0.8.0 改成 raise,attempt 終態不再被誤標成
  `acceptance_unknown`;`podcast_series` 也不再把原始例外拋給呼叫端(先前會繞過
  `except RuntimeError` 的安全網,「預期內的停止用回傳值表達」的契約整個破掉)。
- `rename(return_object=False)` 不再短路,新增的 raise 路徑已在 `audio_finalize` 吸收。
- lifespan 在 inline auth 模式額外壓掉 0.8.0 新增的 **L3 headless re-auth**
  (與 keepalive 同一類災難:在本 process 重鑄 cookie、寫不回 Doppler)。
- `Source.created_at` 由 naive 翻回 aware UTC;正規化器兩種都吃,但 **fake 必須跟著
  實裝版本走**,否則重演「測試綠、production 濾光」。

---

## v0.6.0

- **`published_at` 是「首發時間」不是「產製時間」**。retract 必須把它 pop 進
  `retraction.retracted_output`,promote 補回時走 `_first_published_at()` 沿 attempts
  建立順序找第一筆非空值。舊行為用 `setdefault` 補成重生當下的 wall clock,GUID 不變
  但 pubDate 漂 → episodic feed 按 pubDate 倒序,重生集跳到最前(saa-drill EP05/EP09 實際事故)。
  **改 code 不回溯既有 manifest**,用 `scripts/backfill_published_at.py`。
- 真實驗收再抓到**同一個 bug 只修一半**:`manifest` 與「回傳給呼叫端的那份 dict」是兩個
  出口,第一版只蓋掉前者(實測 manifest 12:07:20、回傳值 12:16:39)。修在
  `_promote_attempt_output` 這個匯流點,四條路徑一起正確。**測試要同時斷言兩者** ——
  只驗 manifest 正是它溜過去的原因。
- 冪等、資料完整性與前置驗證的一輪硬化;發布路徑的內容防護與原子性。

---

## v0.5.0 及更早

見 git log。`source_ids`(v0.5.0)、發布路徑硬化、generation-input bundle 等。
