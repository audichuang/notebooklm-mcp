# v0.9.0 驗收劇本:身分跟著 client 走

環境怎麼搭、測資怎麼設計、哪五類事只有真帳號測得到 —— 正本在
[acceptance-testing.md](acceptance-testing.md),**先讀那份**。這裡只回答一件事:
**v0.9.0 改了什麼,所以這一輪要驗什麼**。

## 為什麼這一版的驗收跟以往不同

前幾版驗的是「某條路徑會不會壞」。v0.9.0 改的是**身分放在哪裡** ——
從 process 全域 env 換成每個 client 自帶的 storage_state 檔。這帶來兩個以往劇本
完全沒有涵蓋的維度:

1. **並行**。整個修法的前提是「MCP 對每則 message `tg.start_soon`,一個全域槽不可能
   同時是兩個值」。**以往每一輪驗收都是單線跑的**,所以那個前提從來沒有被真的觸發過,
   而它正是這一版存在的理由。
2. **憑證落檔**。這是安全模型的改變(0700 目錄 + 0600 檔),而且給 SDK 真 path 會
   重新武裝 L2 PSIDTS recovery —— 一條會**在本 process 內重鑄 3 VM 共用 cookie** 的路。
   離線有 tripwire 守著等價前提,但「檔案真的被刪掉了嗎」只有真的跑一次才知道。

另外有**一條離線無法結案的前提**(§5),它是整個 diff 唯一一條會靜默改掉既有權限的
路徑,這一輪一定要收掉。

## 環境

照 [acceptance-testing.md](acceptance-testing.md) 搭,但這一版有兩點不同:

- **一定要用 `-c stg`**(7 免費 + 2 付費兜底)。§3/§4 會燒掉數次生成配額,而 §4 的並行
  情境需要**至少 3 個帳號**才排得出來。
- **`stg` 的槽位順序是刻意的**(免費在前、付費兜底最後)。若這一輪跑到動用 `_8`/`_9`,
  代表當天七個免費帳號已經打爆 —— 那兩個與 `prd` 共用,會吃掉隔天生產的份,**停下來
  隔天再跑**,不要硬撐。

```bash
uv tool install --python 3.12 --force "git+https://github.com/audichuang/notebooklm-mcp.git@v0.9.0"
~/.local/share/uv/tools/notebooklm-mcp/bin/python -c \
  "import importlib.metadata as m; print(m.version('notebooklm-mcp'))"   # 必須印 0.9.0
```

**驗版本號不能省**:uv 的 git cache 壞掉時會 `fatal: unable to read tree` 然後**裝成舊版**,
而它照樣印 `Installed 2 executables`(v0.8.1 實測踩過)。數字不對就
`uv cache clean notebooklm-mcp` 再 `--force --reinstall`。

---

## 0. 離線基線(不燒配額,先跑)

```bash
uv run pytest -q                          # 585 passed
uv run python scripts/check_skill_sync.py # 35 tools + 11 contract terms
```

**特別確認這一條是綠的**:

```bash
uv run pytest tests/test_client_pool.py::test_precheck_agrees_with_the_sdk_strict_loader -q
```

它釘住「落檔前的預驗證」與「SDK strict loader」判定一致 —— 整段「L2 PSIDTS recovery
不可達」的安全論證**只靠這個等價前提**,而第一版實作正是在這裡分岔的(空值 PSIDTS
預驗證放行、開檔 raise、真的發出一次 RotateCookies POST)。它紅 = 上游又讓兩者分岔了,
**在跑任何真帳號情境之前先停下來**。

---

## 1. 啟動期:憑證檔的生命週期(不燒生成配額)

用真憑證但不生成,所以這一節可以放心重跑。

**1-a 檔案權限與清理。** 啟動 server、掛著,另一個 shell 檢查:

```bash
doppler run -p notebooklm -c stg -- nblm-mcp --transport stdio &
ls -ld /tmp/notebooklm-mcp-auth-*          # 目錄必須是 drwx------ (0700)
ls -l  /tmp/notebooklm-mcp-auth-*/slot-*.json   # 檔案必須是 -rw------- (0600),數量 == 帳號數
```

正常結束 server 後,`ls -d /tmp/notebooklm-mcp-auth-*` 必須**找不到任何目錄**。

> ⚠️ **已知取捨,不是 bug**:`SIGKILL` / `SIGTERM` 不展開 `AsyncExitStack`,憑證會留在
> `$TMPDIR`。刻意**不加**「啟動時掃掉舊目錄」——同機並行的 MCP process 會互刪。
> 驗收時用正常結束來驗清理,別用 `kill -9` 然後回報 bug。

**1-b 空值憑證要在建 client 之前就大聲失敗。** 這是 §0 那條 tripwire 的真帳號對應面:

```bash
NOTEBOOKLM_AUTH_JSON='{"cookies":[{"name":"SID","value":"x","domain":".google.com"},
{"name":"__Secure-1PSIDTS","value":"","domain":".google.com"}]}' nblm-mcp --transport stdio
```

必須在**啟動時**就 raise 並指名槽位。**絕對不可以**看到它啟動成功 —— 那表示
RotateCookies 那條路又打開了,而後果是 3 VM 共用的 cookie 被這個 process 重鑄掉、
新的只活在 temp 檔裡寫不回 Doppler,**另外兩台下次啟動就掛**,而本機這邊只有 debug 訊息。

**1-c 兩個啟動 guard。** 各試一次,都必須當場 raise:

- 把 `stg` 的 `NOTEBOOKLM_AUTH_JSON`(不帶後綴那個)暫時清掉 → 應報「base 槽位不見了」
- 把 `_2` 設成與 base 相同的憑證 → 應報「pool 裡有重複帳號」

> `_reject_duplicate_accounts` **在兩個槽位都拿不到 email 時會靜默失效**(退回 `#1`/`#2`
> 天然不相撞)。這是已知限制,不必當 bug 回報 —— 但也別把它當成完備保證。

---

## 2. 單集正常路徑(燒 1 次)

`podcast_episode` 生一集完整跑完。這一步只回答「有沒有把好的改壞」,不看新功能。

**要對帳的事實**:mp3 落地且非空、manifest 的 `dispatch.account` 有值、
`published_at` 有值、回錄 source 與 artifact **完全同名** `EP{n:02d} 標題`。

---

## 3. failover 與記帳同源(燒數次)

把帳號打到配額耗盡再繼續生成(**這是最重要的那個測試**,理由見
acceptance-testing.md §撞到配額不是中斷)。

**要對帳的事實** —— 全部在 manifest 裡,不要靠「看起來對」:

| 檢查 | 期望 |
|---|---|
| `attempts` 筆數 | **1**(換帳號是原地重送,不新建 attempt、不 supersede) |
| `errors[]` 的 `dispatch_failover` | 鏈式接龍 `(A→B)`、`(B→C)`,不是每次都從 A 重來 |
| `dispatch.account` | **最後實際送出**的那個帳號 |
| 每筆 failover 的 `from_account` | 該次**實際被拒**的帳號 |

第四項是 v0.9.0 新修的:舊碼在 dispatch 的 await **之後**才讀 `from_account`,讀到的是
「此刻剛好輪到誰」。單線跑看不出差別 —— 所以這一項真正的驗證在 §4。

---

## 4. ⭐ 並行:這一版唯一的新維度

**以往每一輪驗收都是單線的,這條從來沒被驗過,而它是 v0.9.0 存在的理由。**

排法:開兩個 MCP client(或同一個 client 連發兩個工具呼叫,**不要等第一個回來**),
讓它們落在同一個 server process 上:

- **A 集**:一個能正常生成的 notebook,讓它跑進 finalize(等生成 → 下載 → 回錄上傳,
  數十分鐘 —— 這就是風險視窗)
- **B 集**:在 A 還在 finalize 時送出,而且要讓它**撞到配額**觸發 rotate

**要對帳的事實**:

1. **A 集的 mp3 下載成功**,而且 A 的 manifest `dispatch.account` 是 A 送出時那個帳號
   —— 不是被 B 換過去的那個。舊碼在這裡會讓 A 的下載以 B 的身分發出。
2. **A、B 兩集的 `dispatch.account` 不同**(B 換過帳號了),而且各自都指向真的送出它的那個。
3. **B 的 failover `from_account` 不是 A 的帳號** —— 那正是「讀到此刻輪到誰」的症狀。

> **注意**:如果 pool 全員都對兩個 notebook 有 EDITOR(v0.8.1 起自動分享會做到這件事),
> **身分錯了也會下載成功** —— 症狀退化成純稽核失真,只能從 manifest 看出來。想看到硬失敗
> (401),就故意讓 A 的 notebook **只分享給 A 自己**。這也順帶驗了 §5。

---

## 5. ⭐ sharing:結案一條離線證不了的前提(不燒生成配額)

**這是整個 diff 唯一一條會靜默改掉你既有權限的路徑,一定要收掉。**

`_has_sufficient_permission` 把 OWNER 算進「已足夠」,用來擋掉「failover 之後對 notebook
owner 送 `add_user(EDITOR)` 等於降權」。但前提 —— **`get_share_status` 回的 `shared_users`
會不會包含 owner 那一列** —— fake 證不了(它吐的就是測試自己 seed 的東西)。

**一次唯讀 RPC 就能結案**:

```
用 A 帳號建一個 notebook → 分享給 B(EDITOR)→ 以 A 的身分呼叫 sharing.get_status
→ 看 shared_users 裡有沒有 A 自己那一列、permission 是不是 OWNER
```

- **有 owner 那一列** → 前提成立,現行實作正確。把這個事實寫進
  `tools_basic._has_sufficient_permission` 的 docstring(把 `⚠️ 未被證明` 那段換掉)。
- **沒有 owner 那一列** → **現行實作有 bug**:failover 之後 active=B,對 A 擁有的既有
  notebook 跑 `notebook_share_with_pool`,A 會落進 `todo`,B 就對 owner A 打
  `add_user(EDITOR)`。伺服器若照做就是降權,而後檢看到 A 是 EDITOR 會判定「已生效」通過
  ——**無聲、無痕**。要修的話得改成「明確查出 owner 並排除」。

**同一輪順帶驗 VIEWER 修正**(這是 v0.9.0 的 P0):

1. 在 NotebookLM **網頁上**手動把一個 notebook 分享給 pool 的另一個帳號 —— 網頁預設就是
   VIEWER,這正是真實世界最常見的既有狀態
2. 跑 `notebook_share_with_pool(notebook_id=…)`
3. **期望**:那個帳號出現在 `shared_with`(被重送成 EDITOR),**不是** `already_shared`

舊版會把它算成 `already_shared`、`add_user` 呼叫數 0、回報成功,而 pool 其實還是壞的
—— 症狀要等 failover 換過去才爆。**這一項紅就是整支工具白做**。

再補一條 fail-loud 驗證:用一個**不存在的 email**(或被 workspace 網域政策擋掉的外部
address)跑一次,必須 raise「呼叫成功但未生效」——上游對這種情況**不會** raise。

---

## 6. retract 的新出路(燒 1 次)

`abandon_in_flight` 是這一版新增的對外契約:

1. `podcast_episode` 送出一集,**在 finalize 完成之前**
2. `podcast_attempt_retract(..., reason="brief 失真", abandon_in_flight=True)`
3. 用修正過的 brief 重生同一集

**要對帳的事實**:步驟 2 成功(不 raise);被作廢的 attempt 留著 `retraction` 與理由;
步驟 3 合法產生新 attempt;**標題不變**;取代版的 mp3 落在 `attempts/<attempt_id>/`。

**反向也要驗**:不帶 `abandon_in_flight` 對同樣狀態呼叫,必須**照舊拒絕**
(那個狀態刻意留給 reconcile/resume,預設不開)。

> **它省不了配額** —— 生成已經在燒,retract 是純本機、取消不了遠端。遠端那個 artifact
> 會成為孤兒(不會被之後的 reconcile 誤 claim)。省的是整輪 finalize 與那筆會污染後續
> 各集 context 的回錄 source。

---

## 7. skill 層:文件與行為必須一致

MCP 與 skill 是**一組配置**。上面每一項都是從 MCP 端驗的,這一節從 **skill 端**再走一次
—— 因為真正的呼叫端(LLM)讀的是 skill 文件,不是原始碼。

在**有 skill 的 client**(podcast-lab / Claude Code 掛 `audi-skill/notebooklm`)裡:

1. **`notebook_access_denied` 的指引可執行**:製造一個「notebook 沒分享給 pool 全員」的
   狀態,跑 `podcast_series` 直到撞上它。檢查回傳的 `error` 欄位是否**指名
   `notebook_share_with_pool`**,然後**照著做**一次,確認真的解得開。
   舊版這裡是死路:游標不為權限問題輪替,原樣重呼只會再撞同一個帳號、回一模一樣的停點,
   而 `attempt_count` 恆為 1 —— 呼叫端看不出自己在原地打轉。
2. **`abandon_in_flight` 在 `references/tool-reference.md` 的說明足以讓 agent 正確使用**
   —— 特別是「省不了配額」與「只放行 active 那一顆」兩點有沒有被讀懂。
3. **VIEWER 那條警告有沒有出現在該出現的地方**(`notebook_share_with_pool` 那節)。

---

## 收工:把抓到的東西收回離線測試

**這是紀律,不是建議**(AGENTS.md §驗收完要回收)。這一輪抓到的每一件事,問一次
「寫得成離線測試嗎?」——寫得成的一律補進 `tests/`,而且**收之前先做突變驗證**
(把修正改回舊行為,確認測試真的會紅),否則下一輪還要再燒一次真實配額去發現同一件事。

v0.8.1 那輪補了四條(`tests/test_pool_gaps.py`),v0.9.0 這輪補的三個結構性盲點值得記著,
因為它們解釋了為什麼有些 bug 能溜過 500 多條測試:

- pool 測試每個槽位塞的是**同一個 fake client 物件** → 「這通 RPC 發給誰」這一維在測試裡
  根本不存在
- `FakeSharing` 少了 `permission` 欄位 → VIEWER 那個 P0 沒有任何測試會紅
- 把 rotate 注在**錯的時序**(`generate_audio` 內部)→ 只驗得到「記帳讀了誰」,
  永遠驗不到「generate 收到哪個 client」

三個都是同一種毛病:**fake 比真 SDK 寬鬆,或測試的時序繞開了真正的縫**。下次寫 pool
相關測試時先問這兩個問題。

## 測完別忘了

- 刪掉這一輪建的 notebook(AGENTS.md:真實驗收後要回收測試資料)
- 若 §5 得到了 owner 那一列的答案,**把結論寫回程式碼** —— 那個 `⚠️ 未被證明` 的標記
  就是為了讓下一個人知道它還沒結案
