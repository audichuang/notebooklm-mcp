# Changelog

各版本**改了什麼、為什麼**。AGENTS.md 只放**還在生效的紀律**;版本敘事、事故經過、
已經修掉的舊行為都放這裡,免得 AGENTS.md 被歷史撐爆。

深入的專題另有獨立文件:
[notebooklm-py 0.8.0 升級筆記](docs/notebooklm-py-0.8-upgrade.md)、[ADR](docs/adr/)。

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
