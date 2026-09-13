# 認證與 Doppler config 佈局

`AGENTS.md` §Commands 的完整版。**設定帳號、換憑證、加/挪 pool 槽位、搭測試環境**時讀;
日常開發不需要 —— 每次跑 server 的那一行指令留在 AGENTS.md。

> ⚠️ 這份含**會出事的操作紀律**(salt 必須逐字相同、`secrets set --raw` 不可用 `upload`、
> branch config 會繼承 root)。動 Doppler 之前務必讀完對應那條,它們各自對應一次真實事故。

---

### 認證（Doppler，3 VM 同步）

`notebooklm-py` 讀 `NOTEBOOKLM_AUTH_JSON`(Doppler 注入,**唯讀**,多 VM 不漂移)。
過期時在**有 GUI 的機器**重登再同步(無頭機跑不了 `notebooklm login`,缺 X server):

```bash
uv sync --extra dev --extra login && uv run playwright install chromium   # 僅登入機需要
uv run notebooklm login                     # 開瀏覽器登入
bash scripts/sync-auth.sh --config prd      # 推到 Doppler，所有 VM 下次啟動即生效
#   ⚠️ `--config prd` 不能省:腳本預設寫 `dev`,而非互動執行(agent 跑 Bash)時它
#      只警告不擋 —— 憑證進了正式環境不讀的 config,畫面照印「✅ 同步完成」。
```

#### Config 佈局(2026-08-09)

| config | 用途 | 帳號 |
|---|---|---|
| `prd` | 正式環境 | 5 個**付費**帳號:`NOTEBOOKLM_AUTH_JSON` + `_2`…`_5` |
| `stg` | 測試環境 | 7 個**免費**帳號(`…JSON` + `_2`…`_7`)+ 2 個**付費兜底**(`_8`/`_9`,與 `prd` 的 `_4`/`_5` 同帳號) |
| `dev` / `dev_personal` | 舊的生產入口 | 單一主力帳號,**刻意原封不動**(遷移的退路;pool 等於沒開) |

`_2`/`_3`… 是 client pool 的憑證格式(**[ADR-0010](docs/adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md)**)。
SDK 只讀不帶後綴的那一個,所以多的那幾份在 pool 實作前不生效、也不影響任何機器 ——
`dev` → `prd` 因此可以逐台遷,漏改的機器照舊能跑(strangler pattern,這正是**不改名 `dev`** 的理由)。

**`stg` 的槽位順序是刻意的:免費在前、付費兜底在最後**。輪替是由前往後,所以正常一輪
驗收只吃免費配額;要動用到 `_8`/`_9` 表示當天已經把七個免費帳號全打爆了。**代價是那兩個
與 `prd` 共用帳號** —— 動用兜底會吃掉隔天生產可用的份,所以它排最後而不是排前面。
免費帳號一天各 3 次,7 個 ≈ 21 次:**一輪完整驗收(4 集回歸 + failover 情境)約 6~8 次**,
所以現在一天跑得完兩輪 —— 不必再等隔天配額重置(上一輪就卡在這裡,而且重置時間只能用猜的)。

**加帳號時編號必須連續,而且順序不能顛倒**:要把既有的往後挪(例如把 `_6`/`_7` 挪成
`_8`/`_9` 好空出位置),**先複製到新位置、再覆蓋舊位置** —— 任何一刻都不能出現空號,
否則 server 當場 raise(那個 guard 是刻意的,跳號在實務上都是 Doppler 打錯字)。
**不帶後綴的那個也算槽位**:它缺席而 `_2` 還在,同樣當場 raise(v0.9.0 補;舊版會靜默
退成單帳號、連 inline 模式的兩條 env 紀律一起關掉,而在有本機 storage_state 的登入機上
還會啟動成功)。

**同一個帳號不得佔兩個槽位** —— v0.9.0 起 server 啟動時比對 email,重複就 raise。
配額不會因此變多,而 failover 會寫下一筆謊報的 rotation(`from_account` → `to_account`
其實是同一個人),看起來像 pool 壞掉。最常見成因就是上面講的 branch config 繼承。
**這道檢查在兩個槽位都拿不到 email 時失效**(退成 `#1`/`#2` 天然不相撞)——保護正好
在認證退化時消失,別把它當成完備保證。

**三條會出事的紀律:**

- **`prd` 的 `PODCAST_TOKEN_SALT` 必須逐字等於 `dev` 的**。feed 公開路徑是 `HMAC(salt, show_id)`,
  換 salt = 已發布節目全部換 URL、訂閱者掉光。`stg` 則**刻意不同**(URL 空間隔離)。
  建 `prd` **不能用 `setup-test-config.sh`** —— 那支的預設行為是產生新 salt。
- **認證一律 `doppler secrets set --raw`,絕不用 `secrets upload`**。upload 會做變數插值,
  cookie 值裡的 `$` 被當 secret reference 吃掉:實測 16137 bytes 的 storage_state 上傳後
  變成 16103 bytes 的**無效 JSON**,而指令回報成功。複製後**驗 hash 還不夠**,要真的跑一次
  RPC(`await client.get_account_email()`)。
- **branch config 會繼承 root 的未覆寫 secret** —— 建 branch config 放 pool 憑證是陷阱:
  它會把 root 的 `_2`/`_3` 一起繼承進來,pool 於是輪到同一個帳號兩次,failover 看起來像壞掉。
  (`dev_alt` 就是這樣,pool 落地後已刪。**刪 config 會弄壞任何指向它的 MCP 註冊** ——
  刪之前先 `claude mcp list` 掃一遍每一台。)

**CLI 一律走 `uv run notebooklm`,不要另外裝全域版**:全域安裝會與 repo pin 的版本悄悄
漂開。2026-08 踩過一次:pipx 的全域版停在 **0.3.2**,`profile` 子指令不存在,而且對現行
認證一律回 `Authentication expired or invalid` —— 那個訊息會把人誤導成「NotebookLM 搬
網域了」,實際只是 CLI 太舊(**同一份 storage_state 用 0.7.3 就正常**)。那份全域安裝已
移除,PATH 上不再有 `notebooklm`;`uv run` 保證用的是 pin 的版本。

**改到 pool / dispatch / 認證 / 發布就要跑一次真實驗收** —— 離線測試用 mock client,
結構上找不到「配額真的被拒」那一類 bug(v0.7.1 抓到三個含一個死鎖;v0.8.0 抓到三個 P0)。
**怎麼搭環境、測資怎麼設計、以及「哪五類事只有真帳號測得到」的分工線**見
[docs/acceptance-testing.md](docs/acceptance-testing.md)(不變的方法論),
**這一版要驗什麼**則每版一份 —— v0.9.0 是 [docs/acceptance-v0.9.0.md](docs/acceptance-v0.9.0.md)
(✅ **已於 2026-08-09 跑完真實驗收**,35/35 工具覆蓋;結論見 CHANGELOG v0.9.0 §真實驗收。
那條離線證不了的前提**已結案**:`get_share_status` 的 `shared_users` **會**列 owner 且
`permission=OWNER`,所以 `_has_sufficient_permission` 的 OWNER 豁免正確,failover 之後
不會把 owner 降權)。
**驗收完要回收**:抓到的東西凡是寫得成離線測試的,一律補進 `tests/`(收之前先做突變驗證,
確認它真的會紅)——否則下一輪還要再燒一次真實配額去發現同一件事。

**真實驗收走測試帳號,不要打主力帳號**(會污染正式資料、且共用同一份每日生成配額):
獨立 Google 帳號 + Doppler `notebooklm/stg` + **另一組 `PODCAST_TOKEN_SALT`**(salt 不同 ⇒
測試 feed 落在完全不同的 URL 空間,而 uploader 不刪檔,所以「不要撞」比「事後清」重要)。
一次性設定與登入流程見 [docs/test-account.md](docs/test-account.md)。
