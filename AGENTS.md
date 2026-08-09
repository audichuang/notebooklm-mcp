# AGENTS.md — notebooklm-mcp

自建薄 MCP server(建在 `notebooklm-py` 之上)+ 確定性續集 podcast 工具。**本 repo 只含 MCP 程式碼**,
發布成 private repo `github.com/audichuang/notebooklm-mcp`,靠 `uv tool install` 裝成 console 命令
`notebooklm-mcp`(server)/ `notebooklm-cover`(封面 CLI)。薄 `SKILL.md` 路由層 + `references/`
**已拆到 skill repo `audi-skill/notebooklm`(docs-only)**;兩者是一組配置(見底部 Cross-Repo Sync Checklist)。
完整設計理由與 live 驗證踩坑見 `docs/superpowers/notebooklm-mcp-findings.md`(經驗庫,值得先讀)。

## Commands

```bash
# 本 repo 開發（必須用 Python 3.12；3.14 會觸發 SDK 的 inspect.signature bug）
uv venv --python 3.12 && uv pip install -e ".[dev]"

# 全套離線測試（mock client，不需網路/認證）
uv run pytest -q
#   註:fresh venv + uv pip install 後首次 uv run 可能撞暫時性 re-sync churn(ModuleNotFoundError),
#   再跑一次或 rm -rf .venv 重建即收斂。

# 消費端安裝（3 VM / podcast-lab 各裝一次；pin tag,不追 master；換成最新 tag）
uv tool install --python 3.12 "git+https://github.com/audichuang/notebooklm-mcp.git@v0.9.1"

# 跑 MCP server（裝好後零路徑命令；認證由 doppler 注入 NOTEBOOKLM_AUTH_JSON）
doppler run -p notebooklm -c prd -- nblm-mcp --transport stdio
#   HTTP 模式：--transport streamable-http --host 127.0.0.1 --port 8484
#   ⚠️ streamable-http / sse「無認證」——勿綁非 loopback host(同網段可驅動帳號)。
#   repo 內開發時亦可 uv run python -m notebooklm_mcp.server --transport stdio

# 註冊進 Claude Code（細節見 docs/mcp-setup.md）。CLI 2.1.201 的 `claude mcp add … -- …`
# 會把 `--` 後整串當 prompt，改用 add-json：
claude mcp add-json notebooklm -s local \
  '{"command":"doppler","args":["run","-p","notebooklm","-c","prd","--","nblm-mcp","--transport","stdio"]}'
```

### 認證（Doppler，3 VM 同步）

`notebooklm-py` 讀 `NOTEBOOKLM_AUTH_JSON`(Doppler 注入,**唯讀**,多 VM 不漂移)。
過期時在**有 GUI 的機器**重登再同步(無頭機跑不了 `notebooklm login`,缺 X server):

```bash
uv pip install -e ".[login]" && uv run playwright install chromium   # 僅登入機需要
uv run python scripts/login_notebooklm.py   # 開瀏覽器登入,登入完成即可,不必按鍵
bash scripts/sync-auth.sh                   # 推到 Doppler，所有 VM 下次啟動即生效
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

**登入為什麼不用原生 `notebooklm login`**(2026-08):Google 把未認證的登入流程轉到
`notebook.google.com`(少了 `lm`),而 SDK 的偵測寫死等 `notebooklm.google.com/**`,
於是登入完成後永遠等不到、卡滿 5 分鐘 timeout。**我們已經升到 0.8.0,它的 host 白名單
仍然只有舊的兩個——升級沒有解掉這件事**。`scripts/login_notebooklm.py` **只改掉那一行偵測**
(兩個 host 都收),其餘全部重用 SDK helper,產出的 storage_state 與原生指令等價。
**已認證的 RPC 仍走舊網域且正常**,壞的只有登入這段。上游修好時
`tests/test_contracts.py::test_login_script_should_be_retired_once_upstream_knows_the_new_host`
會紅,提醒把這支刪掉。**若哪天 API 端點也搬家,notebooklm-py 會整個壞、我們跟著壞** —— 那是要盯的頭號上游風險。

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

## Architecture

- `notebooklm_mcp/`
  - `app.py` — 真正的 FastMCP app + 工具註冊 + lifespan(長駐單一 client,跨長生成不掉線)+ 多 transport
    main。獨立模組以確保「唯一 mcp instance」,不論用什麼方式啟動。帶 server-level `instructions`
    (骨架:主流程+env+鐵律,指回 skill;**刻意不複製 SKILL.md 以免漂移**)供無 skill 的原生 client。
  - `server.py` — thin launcher,只 `from .app import _lifespan, main, mcp`(可被當 `__main__` 跑)
  - `tools_basic.py` — notebook / source / `generate_audio` / artifact / `chat_ask`(薄包,`zh_Hant` 預設)。
    含讀取/觀測面:`artifact_list`(列筆記本現有 artifact,救援/對帳用)、`source_list`、
    `source_fulltext`、`notebook_get`;`chat_ask` 吃 `source_ids`(聚焦單集原文)/`conversation_id`
  - `_sources.py` — caller 指名的 `source_ids` 的形狀驗證 + 一次唯讀 list 對帳
    (`to_source_ids` / `assert_sources_exist`),`generate_audio` 與 `podcast_episode` 共用。
    SDK 不傳 `source_ids` 就抓筆記本全部來源,所以**回頭重生某一集時不指名,後面各集的
    題目與音檔回錄會洩進那一集**。能力在這裡,**選哪幾筆的政策留 host**(ADR-0007;算法
    正本在 skill §Episodic 的 QA 拒收流程)。`podcast_series` 刻意不開這個參數:整季共用
    一組沒有意義,而每集的回錄 source 要跑到那一集才存在、規劃階段填不出來;往前跑時
    筆記本本來就只有 ≤ 當集的來源。`podcast_episode` 的選取會進 `attempt["settings"]`
    (跟 language/format/length 同級),**只在非 None 時才有那個 key** —— 沒指名時 settings
    必須與加這個功能之前逐字相同,否則 `podcast_series` 的 prepared-attempt 等值比對會把
    既有 manifest 判成「設定變了」。
  - `tools_artifacts.py` — `generate_slides`(簡報 PDF)/ `generate_report`(研讀 Markdown)按需生,
    路徑回寫 `series_manifest.json`(供 publish 附連結);不碰音檔迴圈。生成之後的尾段
    (等完成→下載→回寫)抽成 `_finish_slides`/`_finish_report`,讓
    `artifact_download_slides`/`artifact_download_report` 能只走這段——外層 client timeout
    (`mcporter call` 預設 60s)砍掉生成呼叫時,雲端那份已生完,拿 `artifact_id` 救回來就好,
    別重生燒配額。另含
    `episode_set_description`(show notes 回寫 manifest,預設清引用標記;同 process 讀改寫)
  - `tools_research.py` — NotebookLM 內建 Web / Deep Research 的薄包:`research_start`(回
    `task_id` 就走)/ `research_wait`(可重入,回候選 + 報告)/ `research_import`(只匯入
    host 指名的 URL)。**三支分開不是為了彈性,是為了跟 ADR-0001 的 attempt/resume 紀律
    一致**:deep 動輒數十分鐘,start+wait 合一保證踩到外層 client timeout,而重跑合一版
    會再起一個新 task 燒配額;分開之後斷線只要重跑 `research_wait`。
    候選與匯入刻意分離(ADR-0008):`research_wait` 不匯入任何東西,`cited` 只是本地算出的
    事實標記(URL 有沒有出現在報告引用裡),cited-only / provenance / 去重等**篩選政策留在
    host**。指名了不在候選清單裡的 URL 直接 raise——靜默少匯入幾筆比爆掉危險。
  - `generation_input.py` — frozen generation-input bundle:把 runtime-brief / coverage-ledger /
    evidence-manifest 三份 bytes 連同 SHA-256 凍結,`podcast_episode(brief=null,
    input_bundle_path=…)` 逐檔驗雜湊後**只用凍結的 bytes 當 brief**,再寫
    `attempt-binding.json` sidecar 把 bundle 綁到 attempt。binding 同時保存 request SHA 與
    resolved manifest path 的 SHA，回答的是「哪一份 brief、在哪一個 manifest workspace，
    產出了哪一集」；`bound_at` 必須不早於 request 的 `frozen_at`。把已綁 bundle 複製到另一
    workspace、binding 時序倒置或 schema／identity 不符，都會在 `probe_auth`、cleanup 與 generation
    RPC 前拒絕，不能重用 attempt ID 再生一次。**驗證與綁定都在第一個遠端副作用之前**(ADR-0001),雜湊不符就在寫 manifest、
    打 RPC 之前 raise。重跑冪等:`read_attempt_binding` 讀回既有綁定沿用同一 attempt_id
    (`_reuse_frozen_input_attempt` 走 `_attempt_record` 這個 tombstone gate、且擋
    `output_attempt_id` 已存在),所以斷線重跑不會重複建 attempt 或重燒配額;取代版要凍新
    bundle。coverage ledger 與 evidence manifest 不只驗 bytes/hash，還要 strict 驗
    `schema_version`、`qa_kind`、episode identity、disposition-specific row keys，以及 evidence
    artifact 的 unique id/confined relative path/lowercase SHA-256/positive byte count。
    `rollback_attempt_binding` 只刪「這次自己寫的那份 bytes」,清理失敗回 note 掛上
    原例外而**不 raise**(在 except handler 裡再拋會蓋掉真正該讀的錯誤)。
    **`input_bundle_path` 是相對於 workspace 的路徑**(workspace = manifest 的祖父目錄),
    絕對路徑、`..`、路徑上任何 symlink 一律拒。**manifest 的父目錄刻意不限定名稱**——
    三個 podcast 專案分別用 `manifest/`(network-podcast)、`output/`(podcast-lab)、
    `season-01/output/`(data-structure-podcast),曾經寫死 `manifest/` 讓功能只有一個專案
    能用,而那個專案根本還沒有 bundle;containment 由 `relative_to(workspace)` 保證,
    目錄**名字不是安全邊界**。目前只有 `podcast_episode` 支援,`podcast_series` 尚未接。
  - `publish/notes_html.py` — report Markdown → 自包含 HTML;渲染後掃描 script/外部資源標記,命中 fail-closed
  - `tools_podcast.py` — manifest-backed audio attempt 的 durable generate／reconcile／explicit adopt／
    checkpointed finalize;`podcast_series` 只越過已完成 postconditions,standalone resume 是 fallback。
    `podcast_attempt_retract` 是 QA 拒收的受控 supersede(純本機、不打 RPC),**手改 manifest 不是
    替代方案**(EP35 真實事故:手寫 attempt 五個時間戳同一微秒、`artifact_ids_before` 填自己的
    artifact_id)。動這個模組前**先讀 [ADR-0009](docs/adr/0009-retracted-attempts-are-tombstones.md)**
    ——四個都有測試鎖、且各自被真實事故驗證過的不變式:證據要 pop 不能設 None、作廢 attempt 是
    三層 default-deny 的 tombstone、舊回錄 source 的刪除是生成與 resume 兩條路的 precondition、
    取代版不得改標題且下到 `attempts/<attempt_id>/`。**踩過的坑是「只補一條路徑」**:
    attempt 建立有兩個分支、清理義務有三個入口,補一半等於沒補。
  - `tools_publish.py` — `publish_series` / `feed_info`(把整季發布成 Apple 合規
    RSS feed;薄 I/O 編排,內網 HTTP PUT 到 NAS uploader,提交順序:媒體檔→show.json→feed.xml/index.html)
  - `publish/` — 純邏輯(離線可測):`identity.py`(HMAC→base32 決定性 token + `episode_guid`,無 registry)、
    `layout.py`(內容 hash + 媒體檔名)、
    `artwork.py`(Apple Show Cover 規格驗證)、`rss_models.py` / `feed.py`(RSS+iTunes XML + index.html)
  - `_status.py` — generation-status 防護:SDK 把失敗/限流回報成 `task_id=""` 而非丟例外,用前要先擋掉
  - `languages.py`(白名單 + `zh_Hant` 預設)、`enums.py`(字串→int-enum)、`runtime.py`(client holder)
  - `auth_probe.py` — `probe_auth` 輕量真 RPC 認證預檢(長跑前 fail-fast;`auth_check` 工具的底層)
  - `auth_cli.py` — 貼 storage_state JSON 建檔(headless 備援)
  - `cover_cli.py` — `notebooklm-cover` console script(封面 CLI)。填 `assets/*.html` template
    的佔位符 → headless Chrome 光柵化 → RGB JPEG → `validate_artwork`;template 由 agy 設計、已凍結
  - `assets/cover_episode.html` / `cover_show.html` — agy 設計、固化的封面 HTML template(隨 wheel 打包)
- `tests/test_contracts.py` — 用 `inspect.signature` 鎖住 `notebooklm-py` 公開 API,擋上游漂移(離線 tripwire)
- `scripts/` — `check_skill_sync.py`(CI 用:MCP 工具名 ⟷ skill 文件同步硬檢查)、`sync-auth.sh`
  (登入機推 Doppler;`--profile/--config` 可指向測試帳號)、`setup-test-config.sh`(建
  `notebooklm/stg` 測試 config)、`backfill_published_at.py`(published_at 一次性回填)
- `CHANGELOG.md` — **各版本改了什麼、為什麼、踩到什麼事故**。版本敘事一律寫在那裡,
  **不要回填進本檔** —— 本檔只放「還在生效的紀律」,歷史會把它撐爆
- `docs/notebooklm-py-0.8-upgrade.md` — 上游 SDK 升級筆記(0.7.3 → 0.8.0)。**下次升 major 前先讀
  末尾的「驗證方式」**:別只讀 changelog,行為變更不會出現在簽名裡(rename 的短路就是這樣溜過去的)
- `docs/adr/` — 能力邊界決策。**砍掉已規劃的 scope 也要留一支**:2026-06-07 redesign design doc
  的工具清單裡本來就有 `research_start` / `research_wait_import`,實作時掉了、沒有任何決策記錄,
  結果整個 research namespace 隱形了 38 集(ADR-0008 補記)
- `docs/superpowers/` — 設計/計畫/findings。**`SKILL.md` 路由層 + `references/`(工具參考 + 連續性提示詞)已不在本 repo**,在 skill repo `audi-skill/notebooklm`(見 Cross-Repo Sync Checklist)

**判斷只在「大綱」前置點**(Opus 規劃每集 brief,人核可);大綱定稿後是確定性腳本,迴圈內無 LLM。

### 發布(podcast RSS → Apple Podcast)

`publish_series` 讀 4 個 Doppler secret(同 `-p notebooklm -c prd` 注入):
`PODCAST_PUBLIC_BASE_URL`(公開 URL 根,無尾斜線)、`PODCAST_TOKEN_SALT`(token HMAC salt;
**洩漏會讓 feed URL 可被推算**)、`PODCAST_UPLOAD_URL`(NAS uploader 內網 base,無尾斜線)、
`PODCAST_UPLOAD_TOKEN`(bearer,**必須 = NAS `.env` 的 `UPLOAD_TOKEN`**)。MCP 不再掛載 NAS
檔案系統,改成內網 HTTP PUT 到 NAS uploader(讀寫分離)。
對外靜態服務是獨立 public repo [podcast-feed-host](https://github.com/audichuang/podcast-feed-host)
(Caddy 唯讀對外,由使用者既有的 Cloudflare Tunnel 指過來;寫端 uploader 僅內網,不進
tunnel;完整部署/驗收步驟在該 repo README)。feed identity = 穩定 `show_id`(**永不改**),
不綁 notebook_id。

## Gotchas(notebooklm-py 0.8.0,pin `>=0.8,<0.9`;與 GitHub HEAD 不同,以**實裝版本**為準)

- **`mcp[cli]` 必須有上界(`>=1.27,<2`)**:`uv tool install git+…` **不讀 `uv.lock`**,消費端
  每次安裝都自由解析成當下最新——曾經因為寫成 `>=1.0.0` 而出現「dev venv 鎖 1.27.2、四台
  生產實裝 1.28.1」的落差(測試與實跑不同版),且 mcp 2.0 一出就會被靜默吃進去。改版本時
  **對 lock 版本與消費端實裝版本各跑一次全套**,再更新這裡的下界。
  **這個落差會自己重新長出來,要定期對帳**:2026-08-09 又漂成「lock 1.28.1 / 實裝 1.29.0」,
  而且不是純帳面差異——1.29.0 啟動時多印一行 `pydantic_settings … IncompleteFieldDefinitionWarning:
  Field 'lifespan' has an incomplete definition`,1.28.1 完全不印。走的是 stderr 所以沒破壞
  stdio 協定,但它證明實裝版本有 CI 從沒跑過的行為(已把 lock 拉到 1.29.0 對齊並跑過全套)。
  查法:`grep -A1 'name = "mcp"' uv.lock` vs
  `~/.local/share/uv/tools/notebooklm-mcp/bin/python -c "import importlib.metadata as m; print(m.version('mcp'))"`。
  **對齊要用 `uv sync --extra dev`,不是 `uv pip install -e .`** —— 後者不會把 venv 拉到 lock 的版本。
- **(0.8.0)server 命令改叫 `nblm-mcp`,不是 `notebooklm-mcp`** —— 因為 `notebooklm-py`
  自己也宣告了一支同名 script,同一個 tool venv 只留最後寫入的那份,**實測全新安裝 3/3
  都是上游贏**(而上游那支缺 `fastmcp` 會直接 ModuleNotFoundError)。
  **發版前一定要真的跑一次 `<command> --help`** ——`uv tool list` 列的是被指名套件的
  entry point 名稱,不是 bin 裡的實際內容,看不出撞名。**別裝 `notebooklm-py[mcp]`**。
  repo 內開發一律 `uv run python -m notebooklm_mcp.server`。
- **上游/NotebookLM 行為突變時的情報站**:讀 `_research/notebooklm-mcp-cli` 既有 clone 的
  CHANGELOG.md 與 docs/KNOWN_ISSUES.md(jacob-bd,全生態追 Google 改版最快;bl 漂移、cookie
  語意、RPC schema 變動幾乎都最先出現在那),再對照 notebooklm-py 的 GitHub issues。
  **`_research/` 唯讀:連 `git pull` 都不做**,clone 更新請使用者自行決定。
- **(0.8.0)這條紀律延伸到所有「在本 process 內重鑄 cookie」的機制,inline auth 一律關掉。**
  新的兩個是 **L3 headless re-auth**(`NOTEBOOKLM_HEADLESS_REAUTH`,`app.py` lifespan 會顯式
  刪掉這個 env)與 **master-token headless auth**(`headless` extra,**刻意不採用**)。
  理由與取捨見[升級筆記](docs/notebooklm-py-0.8-upgrade.md)的「新能力」一節。
- **(v0.9.0)pool 的憑證落檔,把好幾條原本靠「env 模式」早退的重鑄護欄一起降級了。**
  動 `app.py` 的憑證/lifespan 那一塊時三件事要一起看:①`_write_credential_file` 的預驗證
  **必須與 strict cookie loader 同語義**(必要 cookie 的 **value 也要非空**,不能只檢查 key
  在不在)—— 那是 L2 inline PSIDTS `RotateCookies` 唯一入口的門,而 L2 **不受
  `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 管**;②L3/L4 現在只靠「env 已刪」與「`master_token.json`
  不存在」擋著;③等價前提由
  `tests/test_client_pool.py::test_precheck_agrees_with_the_sdk_strict_loader` 守著,**它紅就
  代表這條路又開了**。破功的後果:3 VM 共用的 cookie 被本 process 重鑄、新的寫不回 Doppler,
  另外兩台下次啟動就掛,而本機正常啟動只有 debug 訊息。
  推導與取捨見 [ADR-0010](docs/adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md) 的 v0.9.0 amendment。
- 長跑工具(`podcast_episode`/`podcast_series`)在**本地驗證之後**有 `probe_auth` 認證預檢
  (輕量真 RPC;homepage probe 會 false-positive,jacob-bd #250);獨立工具版是 `auth_check`。
- `sources.delete` 是 **idempotent**(0.7.0 起):刪不存在的 source 也「成功」不 raise。
  `source_delete` 回的 `deleted` 只代表「呼叫後該 id 已不在筆記本」,**不保證它先前存在**
  (打錯 id 也回 deleted)。要確認刪掉某既有來源,先用 `source_list` 拿真實 `source_id`。
  ⚠️ **刪除後 `source_fulltext` 用同一個 `source_id` 仍讀得回全文(實測 55 分鐘後仍可)
  —— 那是預期的,不是清理失敗**:它繞過 notebook 直接查 source 物件,而生成用的來源清單
  與 `source_list` 同走 `GET_NOTEBOOK`。所以 ADR-0009 的清理義務有效。推導的三個前提由
  `tests/test_contracts.py::test_generation_takes_its_source_list_from_the_notebook_not_the_server`
  釘住(**它紅就代表推導失效、清理義務要重新論證**),完整論證在該測試的 docstring。
- 改 contract 測試時對「**實裝版本**」跑,別信 `_research/` 的 HEAD clone。
- **多帳號 pool 動 dispatch/認證前必讀 [ADR-0010](docs/adr/0010-quota-failover-rides-the-zero-side-effect-refusal.md)**
  (含 v0.9.0 amendment)。四條實作紀律,每條都被真實事故驗證過、都有測試鎖:
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
- 命名鐵律(每集 mp3 回錄 + 工作室 artifact **完全同名** `EP{n:02d} 標題`)的正本在 skill
  §Episodic;**實作上唯一要記的是命名邏輯集中在 `_episode_label()`**,改流程時對照那裡,別各處自己拼字串。
- `get_fulltext` 會在 CJK 字元間插空格;關鍵字比對前先 `"".join(text.split())`
  (`_text.norm` 已封裝)。
- **`chat_ask` 回答夾帶引用標記**(`[1]`/`[3, 4]`/`[8-10]`)。工具已內建
  `strip_citations` 由 server 端清(`_text._CITATION_RE`),`episode_set_description` 也預設再清一次
  ——**新程式碼別再自己寫 regex**,要改清理規則改 `_CITATION_RE` 一處。
- **遠端 mutation 前要有便宜 preflight,但那不是 durable attempt**:`artifact_revise_slide`
  與 `artifact_retry_failed` 改的是**遠端狀態**(不像 download 類救援只寫本機檔),而兩支
  RPC 都只靠 `artifact_id` 定位、`notebook_id` 只是 routing header,錯配 ID 伺服器不會擋 →
  用 `artifacts.get_or_none`(它是 list 後比對 id,一次同時驗存在與歸屬)先驗 kind/status。
  `generate_slides` / `generate_report` / `revise_slide` 也在生成前先驗 `episode_n` 存在,
  免得打錯集號要燒完一次配額才 raise。
  **仍未解的是重入**:外層在 mutation 成功後、回寫前斷線,重跑會再 mutate 一次。這是整個
  slides/report 家族共有的架構債(`generate_slides` 逐字同形),要修得做成涵蓋 generate 與
  revise 的 attachment attempt(含 manifest 存 `slides_artifact_id` 才能驗 episode binding),
  不是替 revise 單獨拆 kickoff/finalize。

### 按需載入的 gotchas(只在動到那一塊時讀)

| 動到什麼 | 先讀 |
|---|---|
| research 三支工具 | [docs/gotchas-research.md](docs/gotchas-research.md) |
| 發布 / 附件 / 封面 | [docs/gotchas-publish.md](docs/gotchas-publish.md) |
| 直接呼叫 notebooklm-py | [docs/gotchas-sdk.md](docs/gotchas-sdk.md) |
| attempt 狀態機 / manifest | [docs/gotchas-attempt.md](docs/gotchas-attempt.md) |
| 寫檔 / 上傳來源 | [docs/gotchas-files.md](docs/gotchas-files.md) |

## Conventions

- 全程繁體中文註解/文件。TDD:測試先紅再綠,每任務一 commit。
- commit 訊息寫清楚「症狀 + 根因 + 為何這樣修」(commit 與 docs 是團隊經驗庫)。
- 不污染 `_research/`(唯讀參考 clone)。
- **大型修復輪派 agent 時:Sonnet 實作 → Opus 審查 → 主模型裁決**(採納/駁回 findings、
  最終驗證、commit 留在主迴圈)。這個分層被實績驗證過:Opus 抓到的正是本 repo 反覆出現的
  **「補一半」**——guard 放進上傳迴圈內、`except Exception` 漏掉 `CancelledError`、
  測試只鎖三個 handler 之一。兩條操作紀律:①**同一個檔案不可讓兩個 agent 並行編輯**,
  按檔案分區,小雜項主迴圈自己 inline 改;②修正輪要把「審查者的具體建議 + 裁決取捨」
  寫進 agent prompt,別讓它重新發明一次。

## Cross-Repo Sync Checklist

MCP repo 與 skill repo 是一組配置。改動 MCP tools 時,同步更新 `/home/user/research/audi-skill/notebooklm/SKILL.md` 的工具表與 `/home/user/research/audi-skill/notebooklm/references/tool-reference.md`。

新增、移除或改名工具時,commit message 要明講 skill repo 是否已同步;若尚未同步,不要 push MCP release tag。若本 repo 已落地 CI hard check,PR 必須等該檢查綠燈後才能 tag release。

**推送順序:先推 audi-skill、再推本 repo**(v0.2.9 教訓):CI 的 sync check 會 clone
**遠端** audi-skill 來驗——skill 只同步在本機、還沒推,MCP 先推就 CI 紅(missing 新工具名)。
反序踩到時把 audi-skill 推上去後 `gh run rerun <id>` 即綠,不用改 code。

### Release Pin Sites(發 tag 時**六處**一起改)

`uv tool install …@vX.Y.Z` 的版本號散在五個檔(加 `pyproject.toml` 共六處),
沒有單一來源可推導——曾漂成
podcast-lab v0.2.9 / README v0.2.4 / 實裝 v0.3.3 三套並存。發版時一次改完:

1. `pyproject.toml` 的 `version`(正本)
2. 本檔 §Commands 的安裝指令
3. `README.md`
4. `docs/mcp-setup.md`
5. `audi-skill/notebooklm/SKILL.md` §Auth
6. `../podcast-lab/AGENTS.md` §更新 notebooklm-mcp

**外加一件不算 pin 但一定要做的**:在 `CHANGELOG.md` 開一節寫「改了什麼、為什麼、
踩到什麼事故」。版本敘事只寫在那裡 —— **不要回填進本檔**。

**tag 之前**:CI 綠(它含 wheel 的 `uv tool install` + `--help` 冒煙),
**tag 之後**:照 pin 用 tag 真的裝一次再收工 —— v0.7.0 的撞名就是這一步才發現的。
**「裝完」要驗版本號,不能只看它印 `Installed 2 executables`**:uv 的 git cache 壞掉時會
`fatal: unable to read tree` 然後**裝成舊版**(v0.8.1 實測踩到)。收工前跑
`~/.local/share/uv/tools/notebooklm-mcp/bin/python -c "import importlib.metadata as m; print(m.version('notebooklm-mcp'))"`;
數字不對就 `uv cache clean notebooklm-mcp` 再 `--force --reinstall`。

驗證:`grep -rn "notebooklm-mcp.git@v" --include="*.md" . ../podcast-lab ../../audi-skill | grep -v docs/superpowers`
(`docs/superpowers/` 的歷史計畫書刻意不改——那是當時的事實)。
