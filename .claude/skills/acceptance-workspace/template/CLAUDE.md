# 這個工作區的鐵律

**notebooklm-mcp __VERSION__ 的真實環境驗收工作區。拋棄式,不是任何專案的正式資料。**

<!-- targeted 輪次在這裡補一段:上一輪驗過什麼、哪些結論沿用、這輪只驗什麼。
     full 輪次刪掉這行註解即可。 -->

## 一、只准打測試帳號

`.mcp.json` 走 **`doppler -c stg`(獨立測試 Google 帳號 pool)**。

> ⚠️ `.claude/skills/notebooklm/` 是**上游 skill 的逐字快照**,它的 §Auth 與
> `references/troubleshooting.md` 寫的是 **`-c prd`** —— 那是**正式帳號 pool**,
> 對正式流程才正確。**在這個工作區一律用 `-c stg` 覆蓋它。**
> 快照刻意保持與上游一致(驗收的對象就是使用者實際拿到的那份 skill),
> 帳號政策由本檔宣告 —— 那是加法,不是修改。

```bash
doppler run -p notebooklm -c stg -- ...   # ✅ 測試帳號
doppler run -p notebooklm -c prd -- ...   # ❌ 正式帳號 pool,本工作區禁止
```

隔離為什麼有效(尤其 `PODCAST_TOKEN_SALT` 為何必須不同)見
`../notebooklm-mcp/docs/test-account.md`。

### 配額:不用省,但要知道自己在哪一格

**驗收的目標是覆蓋完整,不是省配額。** 撞到配額耗盡本來就是要測的東西(配額拒絕的
真實形狀只有真的撞到才知道,而整個 failover 設計掛在那個分支上),不要為了省而繞開。

一個事實供判斷狀況:`stg` 的最後兩個槽位是付費兜底,與 `prd` 共用同一組帳號。
輪替由前往後,所以輪到它們代表當天前面的都打完了 —— 那時**回報一聲**即可,
不必自己決定要不要停。

## 二、命令是 `nblm-mcp`,不是 `notebooklm-mcp`

`notebooklm-py` 自己也宣告了一支同名 script,同一個 tool venv 只留最後寫入的那份,
實測全新安裝 3/3 拿到上游那支(缺 `fastmcp` 直接爆)。

## 三、憑證會落到磁碟 —— 這是設計,不是洩漏

server 啟動時把 pool 的每個憑證各寫一份到 `/tmp/notebooklm-mcp-auth-*/slot-N.json`
(目錄 `0700`、檔案 `0600`),lifespan 結束時整個目錄刪除。

看到這些檔案**不要當成 bug 回報** —— 它是 v0.9.0 的核心改動(身分跟著 client 走,
不再放 process 全域 env)。要驗的是**權限對不對、以及正常結束後有沒有刪乾淨**。

> **已知取捨**:`kill -9` / `SIGTERM` 不展開 `AsyncExitStack`,憑證會留在 `$TMPDIR`。
> **而且不需要有人動手殺**(v0.9.25-rc 驗收釘死的因果):MCP client 關掉 stdin 後只等
> `PROCESS_TERMINATION_TIMEOUT = 2.0` 秒就自己送 SIGTERM,展開中位數 0.55 秒但**偶爾**
> 超過 2 秒 —— 實測殘留率約 **10~15%**,正常關 session 就會發生。
> 為什麼偶爾慢仍是 inconclusive。完整推導見 `../notebooklm-mcp/docs/gotchas-pool.md` §二之二。
> 刻意**不加**「啟動時掃掉舊目錄」——同機並行的 MCP process 會互刪。
> 驗清理請用**正常結束**。
>
> ⚠️ **這個殘留會累積,而且實測比預期頻繁**:光是搭環境的零星測試,一個下午就留下
> 3 個目錄、其中一個含真憑證(`timeout` 殺掉 `doppler run` 時,子 process 收不到信號)。
> **收工前一定要跑這段**,這是收尾義務不是選配:
>
> 🔴 **不要用 `fuser` / `lsof` 判斷「有沒有 process 持有」**(v0.9.3 驗收 FINDING-1
> 的判別實驗):server 寫完 slot 檔就關 FD(`open→write→close`),執行期**不持有任何
> open FD**,`/proc/<pid>/cwd` 也不在那裡。所以 `fuser "$d" || rm -rf "$d"` 對**每一個**
> 活著的 server 都判成「沒人持有」,把它正在用的憑證目錄刪掉 —— 這台當下常有 3 個
> server 在跑,其中一個是 `-c prd` 的正式帳號 pool。
>
> 改成「有活的就不自動刪,印對照表讓人判斷」:
>
> ```bash
> if pgrep -f nblm-mcp >/dev/null; then
>   echo "⚠️ 還有活著的 nblm-mcp —— 不自動刪。對照啟動時間自己挑:"
>   ps -eo pid,lstart,args | grep '[n]blm-mcp'
>   ls -ld --time-style=full-iso /tmp/notebooklm-mcp-auth-*
>   # 目錄是 server 啟動時建的:ctime 對得上某個活 process 的 lstart = 它還在用。
> else
>   rm -rf /tmp/notebooklm-mcp-auth-*      # 沒有活的,全部都是孤兒
> fi
> pgrep -af nblm-mcp     # 前一輪驗收留下的孤兒曾在這台活了 11.5 小時
> ```
>
> 展開真的跑完就會刪乾淨 —— 那條實測過(30 次自起實驗給 30 秒寬限時 0/3 殘留)。
> 壞的是兩件事:上面那個「安全刪除」的前提,以及**「正常結束」不保證展開跑得完**
> (2 秒寬限,見上)。

## 四、不要自己跑發布(`publish_series`)

需要它時停下來問使用者;headless 沒有人可問,就把狀況寫進 `FINDINGS.md` 後停在這一步。
它會把 blob **永久**推上 NAS(uploader 不刪檔)。測試 feed 因為 salt 不同,落在與正式
節目完全不同的 URL 空間,不會互撞 —— 但仍然是真實的對外寫入。

## 五、這一輪會**故意**製造壞狀態 —— 每一個都要收乾淨

計畫裡有幾個情境需要人為破壞環境(空值憑證、缺 base 槽位、重複帳號、
只分享給單一帳號的 notebook)。**每個都只在本機 shell 的環境變數層做,不要改 Doppler。**

若不得已改了 Doppler,**當場改回來並用一次真 RPC 驗證**(`auth_check`)——
複製後驗 hash 還不夠,要真的跑一次 RPC。

## 六、認證過期時怎麼重登

```bash
cd ../notebooklm-mcp
uv pip install -e ".[login]" && uv run playwright install chromium   # 僅登入機需要一次
uv run notebooklm login --profile test        # 要有 GUI;無頭機缺 X server 跑不動
bash scripts/sync-auth.sh --profile test --config stg
```

<!-- 這一節在 v0.9.14 之前寫的是 `scripts/login_notebooklm.py`,而那支在 v0.9.13
     就被刪了 —— 範本指向一個不存在的檔案,撐了整整一個版本沒人發現。
     改動到登入流程時記得回頭改這裡。 -->

## 七、pool 的帳號健康狀態是驗收的**輸入**,不是通過條件

**不要把「所有槽位都健康」當成認證那個 Phase 的過關標準。** 一個多槽位 pool 裡有帳號
處於各種狀態(PSIDTS 過期、cookie scope 不對、配額打完、被踢出某本 notebook 的共享名單)
是**常態** —— 整個 failover 設計存在的理由就是它。健康的槽位證明不了 failover;
**不健康的槽位才是免費的判別實驗**,而且不用燒配額。

所以認證 Phase 要做的是:**先記錄每個槽位當下的狀態**,再拿那些狀態當素材,驗
「系統對每一種狀態的反應是不是正確的那一種」。三種狀態走**三條不同的路**:

| 狀態 | 應該走 | 絕對不該走 |
|---|---|---|
| **配額耗盡 / 限流** | `_REFUSED_WITHOUT_DISPATCH`(`RateLimitError` / `ArtifactFeatureUnavailableError`)→ rotate 換帳號重送 | 當成永久失敗停下來 |
| **認證失效**(cookie 死) | `probe_auth` / `is_auth_error` → fail-loud 叫人重登 | **不該 rotate** —— 換帳號救不了「整批憑證來自同一個 Doppler snapshot」 |
| **權限不足**(看不到那本 notebook) | `NotebookAccessDenied` → 停下來,指引 `notebook_share_with_pool` | **不該進 `_REFUSED_WITHOUT_DISPATCH`**(那個集合的契約是配額/限流,見 `notebooklm_mcp/_errors.py`) |

**發現某個槽位不健康時,先記錄、再想能拿它測什麼,最後才考慮要不要修。**
修掉它等於銷毀素材 —— 下一輪要再等它自然壞掉才有得測。
完整推導見 `../notebooklm-mcp/docs/acceptance-testing.md` 的同名小節。

## 八、測完把 notebook 刪掉

標題含 `ZZ-TEST __VERSION__`,收尾時 `notebook_list` 找出來刪乾淨。
本機產物留著沒關係(整個目錄可刪)。
