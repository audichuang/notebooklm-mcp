# Changelog

各版本**改了什麼、為什麼**。AGENTS.md 只放**還在生效的紀律**;版本敘事、事故經過、
已經修掉的舊行為都放這裡,免得 AGENTS.md 被歷史撐爆。

深入的專題另有獨立文件:
[notebooklm-py 0.8.0 升級筆記](docs/notebooklm-py-0.8-upgrade.md)、[ADR](docs/adr/)。

---

## v0.9.1

v0.9.0 真實驗收(2026-08-09/10)抓到的兩個 FAIL。**都不在核心機制上** ——
核心改動的驗收結果寫在下面 v0.9.0 節的〈真實驗收〉。


驗收本身的結論在下方「驗收結果」節;這裡是**照著結論改的兩件事**。兩個 FAIL 都不在
核心機制上,但都是「照著文件做會走進死路或拿到錯東西」。

**`notebook_access_denied` 的指引在它自己產生的狀態下不可執行。** 停點完全正確,
但 `error` 叫呼叫端跑 `notebook_share_with_pool`,而那支工具用**作用中帳號**執行 ——
正是那個看不到 notebook 的帳號(配額 failover 剛換過去)。於是指引自己也 permission
denied,呼叫端只是從一個死路換到另一個。而這正是文件描述的典型情境:既有 notebook 的
owner 通常是 slot 1,pool 卻會 rotate 走。

修法是讓那支工具**自己找得動手的帳號**:先試作用中的(絕大多數就是它,零額外成本),
permission denied 才依槽位順序試其餘的,回傳多一個 `shared_by`。**只對 permission
denied 往下試** —— 網路錯誤、認證過期每個槽位都會遇到,一路吞下去只會把根因埋掉。
掃描全是唯讀 `get_status`,而且**不動輪替游標**(`_ACTIVE` 的語意是「配額走到哪」,
借去做別的事會讓 failover 的帳號記帳失去意義),所以 runtime 新增的是
`all_clients()` 而**不是**「切到某個槽位」的 API。全員都看不到時 fail-loud,訊息說出
唯一出路(用真正的擁有者帳號在網頁上分享)。順帶把 `NotebookAccessDenied` /
`is_permission_denied` 抽到 `_errors.py`:兩個模組要判同一件事,各寫一份等於埋一顆
「上游改了 `rpc_code` 只會有一處被改到」的地雷。

**`artifact_revise_slide` 的「artifact 不變、就地改版」是錯的。** 實測遠端會 fork 出
一顆 `<原標題> (2)`,傳進去那顆原封不動還在。**實作本來就是對的** —— 它一律用回傳的
id 而不假設相等,那個「不自己假設」的寫法救了這支工具:當初若照 docstring 寫死用輸入
id,下載到的會是**沒改過的舊那份**,而且看起來完全成功。修的是文件,外加回傳
`superseded_artifact_id` 讓呼叫端知道 id 換了、舊的還在(**刻意不自動刪**:遠端破壞性
動作,而且新的萬一有問題,舊的是唯一退路)。連續 revise 會堆出 `(2)`、`(3)`… 而標題
只差一個序號,正好放大文件自己承認的「`artifact_list` 分不出哪一集」風險。

### 一條未結案、已記進 AGENTS.md 的事實

`source_delete` 之後 `source_list` 確實看不到那筆,但 **`source_fulltext` 用同一個
`source_id` 在 55 分鐘後仍讀得回完整內容**。所以「從筆記本移除」與「後端不再持有」
不是同一件事,而 ADR-0009 那條「重生前必須刪掉舊回錄 source」的清理義務,**效果因此
沒有被證明**(判 INCONCLUSIVE,不寫成 PASS)。它擋得住可觀測的那一半(同名 source
出現兩筆)是確定的,所以那條 precondition 不鬆;要結案得用兩個內容互斥的來源做一次
生成對照。


## v0.9.0

一輪多 agent 深度審查的產出。**主軸是一件架構級的事:帳號身分不再放在 process 全域**
——其餘都是同一輪審查在 pool / failover / sharing / retract 四條路上找到的實際 bug。
minor bump 而非 patch,是因為憑證的存放方式變了(見下),即使對外 API 完全相容。

### 身分跟著 client 走,不再是 process 全域(ADR-0010 補記)

**根因是一個從一開始就站不住的前提。** ADR-0010 當初寫「`NOTEBOOKLM_AUTH_JSON` 必須
隨時等於作用中帳號」,而 v0.8.0 驗收 F-1 的修法(`_sync_auth_env`)也照著做。但
**MCP 是並行的**:`mcp/server/lowlevel/server.py` 對每則 incoming message 跑
`tg.start_soon(self._handle_message, …)`,而本 package 全域零鎖。SDK 的媒體下載又在
**下載當下**重讀憑證(`_artifact/downloads.py` 的 `self._cookie_loader(self._storage_path)`,
`_storage_path is None` 才回頭讀 env)。於是 EP05 正在 finalize(client 已 pin 住)、
EP06 撞配額 rotate,EP05 的下載就以別人的身分發出——**一個全域槽不可能同時是兩個值,
加鎖也救不了**,「隨時等於」這個要求本身無法達成。

改成每個槽位各自寫一份 **0600 storage_state 檔**(0700 `mkdtemp` 目錄,lifespan 每條退出
路徑都刪),用 `from_storage(path=…)` 建 client,SDK 會把路徑注進 download service
(`_client_assembly.py` → `ArtifactsAPI(storage_path=…)`)。`_sync_auth_env` 整支刪除,
**server 從此完全不寫這個 env**。

兩個代價寫在這裡免得日後有人以為是疏漏:①**憑證會落檔**,推翻了 ADR-0010 列的「不落檔」
優點——取捨是「能讀那個 0700 目錄的 user 本來就讀得到 `/proc/<pid>/environ`」;
②給了真路徑會**重新武裝 SDK 的 L2 inline PSIDTS `RotateCookies`**,而它**不受
`NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 管**。它唯一入口是 strict cookie loader 的
`ValueError`,所以落檔前先用 SDK 自己的 `extract_cookies_from_storage` 驗一次,那條路
就永遠到不了——「不在本 process 內重鑄 cookie」的紀律(3 VM 共用一份唯讀 Doppler 憑證)
維持不變,缺憑證仍然是啟動時的大聲失敗。

**單帳號路徑逐字保持舊碼**:沒有輪替就沒有 race,不必平白多一份憑證副本與一組新的 SDK 行為。

### 記帳與送出同源(同一個並行問題的第二面)

`_claim_prepared_dispatch(account=…)` 與真正的 `generate(client)` 原本是兩次獨立的全域讀取,
而 `_rotate_for_quota` 更是在 dispatch 的 await **之後**才讀 `from_account`——那一整趟 RPC
期間,另一個工具呼叫完全可能已經 rotate 過。結果是 manifest 記下一個**根本沒參與這次
dispatch 的帳號**,而且兩個帳號都成功,事後無從發現。ADR-0010 §Transparency 說 manifest
是唯一的稽核憑據,那份憑據於是失真。

新增 `runtime.snapshot()` 一次取 `(label, client)`;`_dispatch_audio_with_failover` 改吃
`account=`/`client=`,failover 換帳號時兩者一起換,全程不回頭讀全域。
`tests/test_pool_gaps.py::test_failover_credits_the_account_that_actually_dispatched`
用「generate 期間並行 rotate」重現,突變驗證下舊碼記成 `b@x`(旁觀者)而非 `a@x`(被拒者)。

### 配額 failover:`ensure_started` 拋了 ≠ 沒建出 task

紀律②的判準原本實作成「`ensure_started` 拋了就 rotate」,但它的條件是
`is_failed or not task_id`——**只有後半是零副作用契約**。SDK
`_artifact/generation.py` 明寫「有 artifact_id 就回 `GenerationStatus(task_id=artifact_id, …)`」,
而 `_ARTIFACT_STATUS_MAP` 含 `FAILED`,所以「有 id + failed」在上游路徑上可達。那種形狀
rotate 重送會建出**第二個 artifact**:manifest 只綁得到後者,前者不在 `artifact_ids_before`
基線裡,日後 reconcile 撞成 `reconciliation_ambiguous`,還多燒一次配額。現在只有真的
沒有 id 才 rotate,有 id 卻 failed 標成 `acceptance_unknown`(先對帳,不盲目重試)。

### `podcast_series` 的 client 陳舊了一整季

`_run_episode` 早有 `client = runtime.get_client()` 並附註「failover 可能已經換過帳號」,
series 的 inline 重送分支卻沒有——**同一個要求補了一條路、漏了另一條,正是它要修的 F-4
的同一個形狀**。生產上被 v0.8.1 的自動分享遮住(pool 全員看得到同一個 notebook),
但那是巧合不是設計:同一個陳舊 client 還被用在 source cleanup 複驗、drift 複驗、baseline
`artifacts.list`,以及 **`probe_auth`**——那個「長跑前 fail-fast」預檢因此驗的是**沒在用的
那個帳號的 cookie**,正好在最需要它的時候驗錯對象。

### 權限被拒的指引在 series 路徑整個蒸發

兩層:`_mark_not_accepted` 收到的是原始 `ClientError`,manifest 的 `remote.error` 只留
`"permission denied"`;而 `NotebookAccessDenied` 繼承 `RuntimeError`,被 series 收成結構化
partial,**而 partial 不帶訊息欄位**。加上游標對權限問題刻意不 rotate,呼叫端拿到
`safe_next_action="podcast_series"` → 原樣重呼 → 再撞同一個沒權限的帳號 → 回一模一樣的
partial,`attempt_count` 恆為 1(而註解正說那個數字是「看得出自己在原地打轉」的唯一依據)
——無限重試。現在例外在標 manifest **之前**就建好、訊息指名 `notebook_share_with_pool`,
series 的安全停點回 `observed_state="notebook_access_denied"` + `error`,讓呼叫端分得出
「等配額」與「要人去補分享」。

### `podcast_attempt_retract`:兩種「無路可走」的狀態(ADR-0009 補記)

真實事故:某集的 attempt **已被伺服器受理,但送進去的 brief 是失真版**。五條路全被擋死
(retract 撞「只有 promoted output 才能 retract」、乾淨 brief 撞「already has durable active
attempt」、原樣重呼被 `_is_resendable_same_request` 擋、`supersedes_attempt_id` 撞
「is not terminal」、adopt/reconcile 都要求已 finalize),唯一出路是讓錯的版本跑完 →
promote → retract → `source_delete` → 重生。

現在 `abandons_unauthorized_candidate` 多涵蓋兩種:①**有 legacy 硬證據但沒有
`output_attempt_id`**(v0.5.0 前的 manifest),且同一輪要把那些 legacy 欄位一起清掉——
否則只開了 retract 這道門、下一次生成改撞 `has_hard_output_evidence`,等於換個地方繼續
擋死(補一半的又一例);②新參數 **`abandon_in_flight=True`**。

②**刻意做成顯式旗標而不是狀態判斷**:「這顆的輸入是錯的」是 manifest 推導不出來的外部
知識——它與「第一次 dispatch、還在飛」逐欄位相同,而後者屬於 reconcile/resume。放寬
*狀態* 規則等於靜默併吞 reconcile 的守備範圍。旗標只放行 `active_attempt_id` 那一顆,
其餘 ADR-0009 不變式全部原封不動。**它省不了配額**(生成已經在燒,retract 是純本機、
取消不了遠端);省的是整輪 finalize 與那筆會污染後續 context 的回錄 source。遠端那個
artifact 成為孤兒,但 `_claimed_artifact_ids` 掃所有 attempt(含 retracted),不會被誤 claim。

### 分享:VIEWER 被當成「已分享」

`notebook_share_with_pool` 的 dedup key 只取 email、丟掉 `SharedUser.permission`,而
`add_user` 的**預設就是 VIEWER**——使用者在 NotebookLM 網頁手動分享過的既有 notebook
極可能正是 VIEWER。於是工具回「已分享」、`add_user` 呼叫數 0,而 pool 其實還是壞的,
症狀要等 failover 換過去才爆:**這支工具存在的唯一理由沒被滿足**。現在只認 EDITOR/OWNER。
把 OWNER 也算「已足夠」順帶擋掉另一件事:failover 之後 peers 會含 notebook 的 owner,
而 SDK 只擋 `permission == OWNER` 這個**參數**、不擋「對象就是 owner」,對他呼叫
`add_user(EDITOR)` 等於降權。

同一輪的其餘四條:`add_user` 回傳的 `ShareStatus` 原本被丟掉,而它是**零成本的後檢**
(那趟 `get_status` RPC 本來就打了),workspace 政策擋外部分享 / email 打錯字都不 raise
——現在未生效就 raise;`notebook_create` 改成**驗證先於變更**(`_pool_peers` 是純本機檢查
卻放在 `create()` 之後,而 label 退成 `#N` 是決定性失敗,呼叫端每重試一次就多一個雲端
孤兒 notebook);`except Exception` 補上 `CancelledError`(8 趟分享 RPC 被外層 60s timeout
砍掉時,舊碼讓 notebook id 隨著裸例外消失);pool 帳號去重。

### 啟動期的四個 loader guard

①**base 槽位缺失**(`_2`/`_3` 在、不帶後綴的那個不在)舊碼直接 `return []`,而 `inline_auth`
又是用「base 在不在」判斷的——於是連 inline 模式一起關掉:`NOTEBOOKLM_DISABLE_KEEPALIVE_POKE`
沒設(冷啟動那顆 RotateCookies poke 會作廢 3 VM 共用的 cookie)、`NOTEBOOKLM_HEADLESS_REAUTH`
保持生效、pool 等於關掉,而在有本機 storage_state 的登入機上還會**啟動成功**、用舊身分跑。
最糟的失敗形狀:看起來好好的。②**重複帳號**(ADR-0010 自己點名的 `dev_alt` branch-config
繼承陷阱)此前沒有任何機器檢查,`rotate_client()` 照樣回報「換到第二格」、failover 照樣
寫一筆謊報的 rotation。③空字串檢查原本只做在 `_2..`,第 1 槽沒做。④`_account_label` 吞掉
`get_account_email()` 的例外——那是啟動時唯一的真 RPC,而 `_pool_peers` 之後看到 `#N` 會
要求人工介入,訊息裡卻說不出原因。改成 `logging.warning`,**刻意仍不 raise**(一個死憑證
讓整台 server 起不來更糟)。

### 第二輪:codex 對整個 diff 做對抗性審查後的修正

上面那些改完之後,把整份 diff 交給 codex 做深度審查。它抓到的第一條就是**這一輪自己新種的**:

**身分只釘到 dispatch,finalize 又鬆開。** `_dispatch_audio_with_failover` 內部把
`(account, client)` 換對了,卻**只回傳 `artifact_id`**,那一份直接丟掉;兩個呼叫端只好
回頭 `runtime.get_client()` —— 又變回「此刻游標指到誰」。而**風險視窗幾乎全落在沒修的
那一半**:dispatch 只有幾秒,finalize(等生成 → 下載 → 回錄上傳 → rename)是數十分鐘。
一處是既有的,另一處是這輪新種的 —— 一個以「殺掉分開讀全域」為目的的 changeset,
自己在 series inline 路徑上又種了一顆同型的。現在回 `(artifact_id, account, client)`。

**而且核心那一半原本沒有測試鎖住。** 把 `generate(client)` 改回 `generate(runtime.get_client())`,
全套照樣綠 —— 因為那條測試的 fake 在 `generate_audio` **內部**才 rotate,那個時序只驗得到
「記帳讀了誰」,永遠驗不到「generate 收到哪個 client」。真正的縫在 `snapshot()` 之後、
dispatch 之前那幾個 await(baseline `artifacts.list`、`_claim_prepared_dispatch`)。新測試
把 rotate 注進那個視窗,一條同時鎖住三個突變(dispatch 讀全域 / 記帳讀全域 / finalize 讀全域)。

**PSIDTS 空值繞過預驗證,`RotateCookies` 仍打得出去(實跑重現)。** 上面那段「先驗過 ⇒
L2 那條路永遠到不了」的論證,建立在兩顆驗證器條件相同 —— 而它們對**空值**的判定相反:
`extract_cookies_from_storage` 只看 name(空字串照樣算存在),strict loader 連 value 也要
非空。於是 `__Secure-1PSIDTS: ""` 預驗證通過 → 開檔 raise → recovery → **真的發出
RotateCookies POST**,而 server 這邊正常啟動、只有 debug 級訊息。預驗證改成 strict 同語義,
並補一條絆線測試同時釘住「SDK strict loader 的判定」與「兩者等價」——那個假設是整段安全
論證的地基,此前沒有任何東西守著。同時把 `NOTEBOOKLM_REFRESH_CMD` 加進 inline 模式的
env override:它會走到帶 recovery 的 loader,而 `docs/superpowers/specs/` 的設計文件正把
它列為「Doppler 過期自癒」方案,誰照做就打開這條路(該文件已加過時標註)。

**`notebook_create` / `notebook_share_with_pool` 三次分開讀全域。** `_pool_peers` 與
`create()` 之間沒有 await(乾淨),裂縫在下一步:`create` 是 await,之後 `_share_each` 才
第三次讀全域。並行呼叫在那個 await 裡 rotate A→B,`_share_each` 就拿 B 去分享一個 **B 還
看不到的 notebook** → 失敗,雲端留下只有 A 看得到的孤兒;而 `rotate_client()` 不回頭,
事後在同一個 process 補跑一樣是 B,只能重啟 server。

**其餘**:`abandon_in_flight` 讓「並行 retract 掉在飛的 attempt」變成**受支援操作**,
於是 except handler 裡的 `store.read()`/`_attempt_record` 多了一條會 raise 的路 —— 在
except handler 裡再拋會蓋掉真正該讀的錯誤,兩處都包起來(讀不到就把原例外原樣交出去)。
inline 重送分支的契約改成三態:同一個 `acceptance_unknown` 原本只因例外型別是不是
`RuntimeError` 就分岔,一邊裸拋(**前面幾集跑完的 `run_results` 整份丟掉**)、一邊回
partial,而 series 的契約正是「預期內的停止用回傳值表達」。email 比對加 `.casefold()`
(大小寫不同會讓後檢恆為 False → 每次都 raise 且留孤兒)。

**一條無法離線結案、已標記進程式碼的**:`_has_sufficient_permission` 把 OWNER 算進
「已足夠」,用來擋掉「failover 之後對 notebook owner 送 `add_user(EDITOR)` 等於降權」。
但前提 —— `get_share_status` 的 `shared_users` **會不會包含 owner 那一列** —— fake 證不了
(它吐的就是測試自己 seed 的東西)。若不包含,那是整個 diff 唯一一條會**靜默改掉使用者
既有權限**的路徑,而且後檢會判定「已生效」而放行。一次唯讀 `sharing.get_status` 對真帳號
即可結案,列進下一輪 stg 驗收。

### 測試

551 → 585。除了對應上述每條的迴歸測試,補掉三個結構性盲點:**pool 測試原本每個槽位裝的是
同一個 fake client 物件**,所以「這通 RPC 實際發給哪個 client」完全不可見(series 陳舊
client 那條就是從這個縫溜過去的);**`FakeSharing` 少了 `permission` 欄位**,正是 VIEWER
那個 P0 沒有任何測試會紅的直接原因(與 `Source.created_at` tz 是同一種「陷阱在 fake」);
**sharing API 零 contract 測試**,而 `SharePermission` 是 module-level import(上游搬家 =
server 起不來)、`permission` 用位置參數傳(0.7.0 對 source add 做過 keyword-only 那次的
同型風險)。另補「正常關閉時 N 個 client 都被關掉」——此前在 `set_clients` 前插一行
`stack.pop_all()`,38 條測試照樣全綠。

### 真實驗收(v0.9.0,2026-08-09,`stg` 9 帳號 pool,35/35 工具覆蓋)

劇本 [docs/acceptance-v0.9.0.md](docs/acceptance-v0.9.0.md)。**核心改動全綠,而且是用
「新舊行為的判別實驗」證的,不只是「沒壞」。**

- **身分跟著 client 走 —— 決定性證明(不燒配額)**:同一個 process、同一顆 audio artifact,
  兩個不同 storage_state 檔各建一個 client;把其中一個帳號 `remove_user` 出共享名單後,
  它的下載失敗、另一個成功。**再把 process 全域 `NOTEBOOKLM_AUTH_JSON` 換成沒權限那份憑證,
  用有權限的 path 建 client 仍下載成功** —— 舊機制(env 當身分)在這裡必敗。
- **並行時序真的排出來了**(以往每輪都是單線):A 集 `accepted` 在 slot 4 進入 finalize
  期間,B 集撞配額把全域游標推到 slot 5;A 的 `dispatch.account` 沒有被改寫,兩集各自
  指向真的送出它的帳號。三支寫 manifest 的附件工具同時在飛,沒有一次覆寫遺失。
- **failover 記帳鏈式接龍**:`(pool-account-1→pool-account-2)`、`(pool-account-2→pool-account-3)`、`(pool-account-3→pool-account-4)`,
  每筆 `from_account` 都是**該次實際被拒**的帳號,`attempts` 全程 1。配額拒絕是同步
  `RateLimitError`,每次 rotate 約 0.7 秒(實測每帳號每天 3 次生成)。
- **五條 dispatch 路徑全部真的走到**,含 v0.8.0 漏補過的 series inline 重送;
  supersede 用**真的遠端 `artifacts.delete()`** 製造 `removed`,舊 attempt 完整保留。
- **reconcile 的三態全部走到**(零候選 / 唯一候選補綁 / `reconciliation_ambiguous` → adopt),
  dispatch 視窗判準與 artifact claim 唯一性都實測有效。`acceptance_unknown` 是用 MCP 協定層
  `notifications/cancelled` 逼出來的。
- **`abandon_in_flight` 含反向**:不帶旗標照舊拒絕;帶旗標成功,retraction 與理由留存,
  標題不變,取代版落在 `attempts/<attempt_id>/`。三支續跑工具對已 retract 的 attempt
  都回同一句可讀的 tombstone 拒絕。
- **啟動期 guard 6 種情境全部當場 raise 並指名槽位**;9 槽位 = 9 個 `0600` 檔於 `0700` 目錄,
  正常結束(stdin EOF)後目錄消失;`mcp` 1.29.0 的 pydantic 警告只在 stderr,
  stdout 非 JSON 行數 **0**。

**⛔ 抓到的兩個 FAIL(都是契約/指引與行為不符,不在核心機制上):**

1. **`notebook_access_denied` 的指引在它自己產生的狀態下不可執行。** 停點與 `error`
   都正確(指名 `notebook_share_with_pool`、寫出被拒帳號、`attempt_count=1`),但那支工具
   用**作用中帳號**執行 `sharing.get_status()`,而作用中帳號正是看不到這個 notebook 的那個
   → 同樣 permission denied。只有 owner 分享得動,而 MCP 沒有指定槽位的方式。
   文件描述的典型情境(既有 notebook + pool 已 rotate)**就是這個死路的常態**。
   至少要在錯誤訊息補一句「請用 owner 帳號分享(重啟 server 讓游標回 slot 1,或在網頁上手動
   分享)」;進階做法是 `get_status` 吃到 permission denied 時自動換槽位試一輪。
2. **`artifact_revise_slide` 的「artifact 不變」是錯的。** 實測回傳的 `artifact_id` 與輸入
   不同,遠端多出一顆 `System Latency Realities (2)`,舊的還在。實裝碼 docstring 與 skill
   `tool-reference.md` 都這樣寫,所以不是快照過期。它放大了文件自己承認的「artifact ↔ episode
   binding 未驗證、分不出是哪一集的 deck 就別猜」風險 —— 順手把 `slides_artifact_id`
   回寫 manifest 就能解掉(工具已經知道新 id)。

**兩個 INCONCLUSIVE(不寫成通過)**:① `artifact_retry_failed` 的成功路徑 —— 這一輪 11 次
生成沒有自然產生 `failed` artifact,而遠端刪除得到的是 `removed` 不是 `failed`,無法可靠製造;
只驗到負向 fail-loud。② `source_delete` 之後那筆 source 對**生成端 context** 的影響 ——
`source_list` 已看不到它,但 `source_fulltext` 55 分鐘後仍讀得回全文,所以 retract 流程
「清掉 stale 回錄 source 以免污染後續各集」的效果未被證明。

**`publish_series` 的成功路徑依使用者要求不測**(uploader 不刪檔、上傳即永久)。
已驗三層 preflight 全部 fail-closed 在第一個 PUT 之前(`cover_path` → `description` →
`slides_pdf_path`),並用 feed URL `HTTP 404` 證明沒有任何 blob 上傳。

**附帶觀察**:`serverInfo.version` 回的是 `mcp` SDK 版本(`1.29.0`)而非 `notebooklm-mcp`
版本 —— 本 repo 最痛的失敗模式正是「uv git cache 壞掉靜默裝成舊版」,而呼叫端從 handshake
看不出來;建議 `FastMCP(version=importlib.metadata.version("notebooklm-mcp"))`。
沒權限的帳號下載 artifact 時,SDK 回的是 `ArtifactNotReadyError`(「還沒生好」)而非權限錯誤
—— 呼叫端很容易誤判成「再等一下」而無限重試。

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
