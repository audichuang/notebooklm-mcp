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
uv tool install --python 3.12 "git+https://github.com/audichuang/notebooklm-mcp.git@v0.4.2"

# 跑 MCP server（裝好後零路徑命令；認證由 doppler 注入 NOTEBOOKLM_AUTH_JSON）
doppler run -p notebooklm -c dev -- notebooklm-mcp --transport stdio
#   HTTP 模式：--transport streamable-http --host 127.0.0.1 --port 8484
#   ⚠️ streamable-http / sse「無認證」——勿綁非 loopback host(同網段可驅動帳號)。
#   repo 內開發時亦可 uv run python -m notebooklm_mcp.server --transport stdio

# 註冊進 Claude Code（細節見 docs/mcp-setup.md）。CLI 2.1.201 的 `claude mcp add … -- …`
# 會把 `--` 後整串當 prompt，改用 add-json：
claude mcp add-json notebooklm -s local \
  '{"command":"doppler","args":["run","-p","notebooklm","-c","dev","--","notebooklm-mcp","--transport","stdio"]}'
```

### 認證（Doppler，3 VM 同步）

`notebooklm-py` 讀 `NOTEBOOKLM_AUTH_JSON`(Doppler 注入,**唯讀**,多 VM 不漂移)。
過期時在**有 GUI 的機器**重登再同步(無頭機跑不了 `notebooklm login`,缺 X server):

```bash
uv pip install -e ".[login]" && uv run playwright install chromium   # 僅登入機需要
notebooklm login                       # 開瀏覽器登入，看到 NotebookLM 首頁才按 ENTER
bash scripts/sync-auth.sh              # 推到 Doppler，所有 VM 下次啟動即生效
```

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
- `scripts/` — `check_skill_sync.py`(CI 用:MCP 工具名 ⟷ skill 文件同步硬檢查)、`sync-auth.sh`(登入機推 Doppler)
- `docs/adr/` — 能力邊界決策。**砍掉已規劃的 scope 也要留一支**:2026-06-07 redesign design doc
  的工具清單裡本來就有 `research_start` / `research_wait_import`,實作時掉了、沒有任何決策記錄,
  結果整個 research namespace 隱形了 38 集(ADR-0008 補記)
- `docs/superpowers/` — 設計/計畫/findings。**`SKILL.md` 路由層 + `references/`(工具參考 + 連續性提示詞)已不在本 repo**,在 skill repo `audi-skill/notebooklm`(見 Cross-Repo Sync Checklist)

**判斷只在「大綱」前置點**(Opus 規劃每集 brief,人核可);大綱定稿後是確定性腳本,迴圈內無 LLM。

### 發布(podcast RSS → Apple Podcast)

`publish_series` 讀 4 個 Doppler secret(同 `-p notebooklm -c dev` 注入):
`PODCAST_PUBLIC_BASE_URL`(公開 URL 根,無尾斜線)、`PODCAST_TOKEN_SALT`(token HMAC salt;
**洩漏會讓 feed URL 可被推算**)、`PODCAST_UPLOAD_URL`(NAS uploader 內網 base,無尾斜線)、
`PODCAST_UPLOAD_TOKEN`(bearer,**必須 = NAS `.env` 的 `UPLOAD_TOKEN`**)。MCP 不再掛載 NAS
檔案系統,改成內網 HTTP PUT 到 NAS uploader(讀寫分離)。
對外靜態服務是獨立 public repo [podcast-feed-host](https://github.com/audichuang/podcast-feed-host)
(Caddy 唯讀對外,由使用者既有的 Cloudflare Tunnel 指過來;寫端 uploader 僅內網,不進
tunnel;完整部署/驗收步驟在該 repo README)。feed identity = 穩定 `show_id`(**永不改**),
不綁 notebook_id。

## Gotchas(notebooklm-py 0.7.3,pin `>=0.7.3,<0.8`;與 GitHub HEAD 不同,以**實裝版本**為準)

- **`mcp[cli]` 必須有上界(`>=1.27,<2`)**:`uv tool install git+…` **不讀 `uv.lock`**,消費端
  每次安裝都自由解析成當下最新——曾經因為寫成 `>=1.0.0` 而出現「dev venv 鎖 1.27.2、四台
  生產實裝 1.28.1」的落差(測試與實跑不同版),且 mcp 2.0 一出就會被靜默吃進去。改版本時
  **對 lock 版本與消費端實裝版本各跑一次全套**,再更新這裡的下界。
- **上游/NotebookLM 行為突變時的情報站**:讀 `_research/notebooklm-mcp-cli` 既有 clone 的
  CHANGELOG.md 與 docs/KNOWN_ISSUES.md(jacob-bd,全生態追 Google 改版最快;bl 漂移、cookie
  語意、RPC schema 變動幾乎都最先出現在那),再對照 notebooklm-py 的 GitHub issues。
  **`_research/` 唯讀:連 `git pull` 都不做**,clone 更新請使用者自行決定。
- `from_storage()` 是**同步函式**,回傳可直接 `async with` 的 context →
  `async with NotebookLMClient.from_storage()`(0.4.x「coroutine 必須 await」慣用法已走入
  歷史)。MCP **不傳 `keepalive=`**:Doppler `NOTEBOOKLM_AUTH_JSON` 是 3 VM 共用、唯讀
  真相來源;RotateCookies 會把新 cookie 留在單一 process 記憶體卻寫不回 Doppler,下一個
  stdio process 反而拿舊 cookie 啟動。`app.py` 在 inline auth 模式會暫時設
  `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`,連 `from_storage()` 冷啟動的 poke 一起關掉。
  Doppler `notebooklm/dev` 也**常駐設了 `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`**:0.4.1 起
  fetch-token 路徑(`_auth/refresh.py` 的 `_fetch_tokens_with_jar`)無條件先打一次
  RotateCookies、無函式參數可關,少了這顆連 `doppler run -- notebooklm <cmd>` 的 CLI
  呼叫都會作廢一次共用 cookie。跨 session 老化照舊靠 GUI 機重登 + `sync-auth.sh`。
- 長跑工具(`podcast_episode`/`podcast_series`)在**本地驗證之後**有 `probe_auth` 認證預檢
  (輕量真 RPC;homepage probe 會 false-positive,jacob-bd #250);獨立工具版是 `auth_check`。
- `GenerationStatus` **無 `artifact_id`**;`task_id` 本身就是 artifact id(download/rename 用它)。
- **`status="removed"` ≠ `is_failed`(0.6.0 起)**:被伺服器下架的 artifact(NOT_FOUND
  輪詢耗盡,通常是每日配額)回 `status="removed"` 且 `is_failed=False`(0.4.x 是合成
  `"failed"`)。`ensure_completed` 一併擋 `is_removed` 才不會把配額下架當成功放行。
- `sources.delete` 是 **idempotent**(0.7.0 起):刪不存在的 source 也「成功」不 raise。
  `source_delete` 回的 `deleted` 只代表「呼叫後該 id 已不在筆記本」,**不保證它先前存在**
  (打錯 id 也回 deleted)。要確認刪掉某既有來源,先用 `source_list` 拿真實 `source_id`。
- **upload endpoint 的副檔名地雷**:`.json`/`.ts`/`.py`/`.yaml` 直接 400 Bad Request(上游只
  提前擋 HTML family:`_source/upload.py:262` 的 `_HTML_UPLOAD_SUFFIXES`)。v0.3.3 起
  `source_add_file` 自動把讀得開的小 UTF-8 文字檔複製成 `<原檔名>.md` 上傳並回
  `converted_from`(EP36 的 fixture-output.json 事故:每次新檔案副檔名都不同,pitfall 文件
  救不了)。`_NO_AUTO_WRAP_SUFFIXES` **不是** endpoint support allowlist——我們無法從外部
  證明那件事,它的語意只有「這些格式不該用改副檔名來處理」(文件/表格/圖片/媒體會丟掉
  原生語意;HTML 要保留上游的 ValidationError)。**刻意不用 `mimetypes.guess_type()`**:它讀
  `/etc/mime.types`,同一支 `.ts` 在有/無該檔的機器上分類不同,3 VM + podcast-lab 會不決定性。
  另外 **NUL byte 是合法 UTF-8**,`read_text()` 只擋掉無效序列的那一半 → 需要獨立的
  `"\x00" in text` guard。顯式 `mime_type` 一律優先(呼叫端比我們清楚那是什麼)。
  注意 `source_add_file` 只是 caller-facing 通用入口;`tools_podcast.py:726/845` 與
  `audio_finalize.py:623` 的已知 mp3 直接打 SDK,不經過它。
- **任何新的原子寫入一律重用 `_atomic`,別自己再寫一份**。`generation_input` 的 sidecar
  曾經自帶一個 `_fsync_directory`,結果把 v0.3.3 學過的三件事全漏了:mkstemp 的 0600 被帶到
  最終檔、commit point 之後的 fsync 放在 try 裡(一拋就把剛建立的綁定刪掉)、沒容忍
  `_DIR_FSYNC_UNSUPPORTED`。三件事都有測試鎖著——但鎖在簡報/講義那條路上,新路徑照樣漏。
  `fsync_parent` / `_NEW_FILE_MODE` / `_DIR_FSYNC_UNSUPPORTED` 是 package 內共用的。
  **注意 sidecar 用 `os.link` 而不是 `os.replace`**:它要的是「只在不存在時建立」
  (原本的 `O_EXCL` 語義,一個 bundle 只綁一次),`os.replace` 會靜默蓋掉既有綁定。
- **固定檔名的下載一律原子換檔**(`_atomic.download_atomically`):`ep{n:02d}-slides.pdf` /
  `-report.md` 原本直接寫最終路徑,重生中斷會讓 partial file 頂替上一版完整產物,而 manifest
  仍指向同一路徑、`publish_series` 的「存在且非空」檢查也抓不到。temp → 驗(非空 + PDF
  magic / UTF-8 可讀)→ fsync → `os.replace` → fsync parent,與音檔 finalize 同一 pattern
  (`fsync_parent` 已抽到 `_atomic.py`,兩邊共用)。
- `sources.add_file` 有 `title`(0.7.x),**但內部仍是 add→rename 兩步且改名失敗只 log 不
  raise** → podcast 流程維持顯式 add_file → rename 兩步(fail-loud);`source_add_file` 工具
  的 title= 有回傳後檢,未生效會 raise。
- `rename()` 的 `return_object` **預設 True 會再抓一次全量清單驗證、可能 raise not-found**
  → 我們所有 rename 呼叫點顯式傳 `return_object=False`(fire-and-forget,RPC 錯誤仍會 raise)。
- 0.7.0 起 source add API 尾端參數(`wait`/`wait_timeout`/`title` 等)**keyword-only**,
  位置呼叫直接 TypeError(contract 測試有鎖)。
- `wait_for_completion` 的 `poll_interval` 已移除(0.7.x);呼叫只用 `timeout=`。
- 改 contract 測試時對「**實裝版本**」跑,別信 `_research/` 的 HEAD clone。
- 命名鐵律(每集 mp3 回錄 + 工作室 artifact **完全同名** `EP{n:02d} 標題`)的正本在 skill
  §Episodic;**實作上唯一要記的是命名邏輯集中在 `_episode_label()`**,改流程時對照那裡,別各處自己拼字串。
- `get_fulltext` 會在 CJK 字元間插空格;關鍵字比對前先 `"".join(text.split())`
  (`_text.norm` 已封裝)。
- **`chat_ask` 回答夾帶引用標記**(`[1]`/`[3, 4]`/`[8-10]`)。工具已內建
  `strip_citations` 由 server 端清(`_text._CITATION_RE`),`episode_set_description` 也預設再清一次
  ——**新程式碼別再自己寫 regex**,要改清理規則改 `_CITATION_RE` 一處。
- **發布的 preflight 是硬契約,且一定在第一個 PUT 之前**(v0.3.3):`require_slides` /
  `require_report` 預設 True——manifest 沒回寫附件路徑就 raise。理由是三個生成是獨立背景
  呼叫、完成訊號分散,manifest 是唯一匯流點,舊行為「缺路徑靜默不附」讓「還在生成」與
  「使用者不要」無從區分(EP36 發布早於交付完成)。兩個獨立旗標,對齊 skill「能略過的只有
  簡報/研讀講義」。同一輪把附件檔案存在性、**每集 mp3 的 `_ensure_local_mp3` resolve**、
  **講義 HTML 預渲染**全部提前——舊版都在上傳迴圈內,後面某集失敗會讓前面幾集的
  mp3/封面已經落在 NAS 上(違反該迴圈上方註解自己宣告的不變式)。**只驗 `artifact_id` 不夠**:
  resolve 還要 `notebook_id`,且遠端下載本身可能失敗。**這叫 required-deliverable preflight
  gate,不是 await barrier**(分不出「manifest 有舊路徑、新版正在重生」),也**不保證零 orphan
  blob**(網路/ffmpeg/uploader 階段失敗仍會留未引用的 immutable blob,那是 media-first 發布
  的已知代價)。
- **附加簡報/講義**:`generate_slides`/`generate_report` 只吃**傳入的 `source_ids`**才聚焦原文;
  不傳則 SDK 用全部來源(v1 不自動排除音檔來源)。附件缺檔時 `publish_series` **fail-fast**。
  **順序鐵律**:uploader 白名單放寬 `.pdf`/`.html` 後**要先重部署 NAS**,再跑帶附件的發布,否則附件 PUT 404。
  講義是 Markdown(`download_report`),`notes_html` 渲染成 HTML 才 host;`.md` 只留本機。
- **`research.start` 只送 `[query, source_type] + notebook_id`**(`_research.py:374-379`):
  筆記本裡已有的來源**對搜尋內容毫無影響**,notebook_id 只決定 task 掛在哪、import 進哪。
  種子只能寫進 query 字串——這條 API 事實推導出的 caller 政策(scratch notebook、query
  recipe)正本在 skill `references/research.md`,別在這裡重抄。
  `mode="deep"` 只支援 `source="web"`;`wait_for_completion` 對 timeout 丟
  `ResearchTimeoutError`(TimeoutError 子類),對 **FAILED 是回傳而非 raise** → 工具端自己擋。
  匯入一律走 `import_sources_with_verification`:`IMPORT_RESEARCH` 在 deep 負載下常超過 30 秒、
  client 先 timeout 但伺服器已 commit,它用 source list 對帳只補送缺的那幾筆。
- **`select_cited_sources` 不是 `ResearchAPI` 的方法**,是 `notebooklm/research.py` 的
  module-level 純函式(不打 RPC)。ResearchAPI 本體只有 5 個 RPC 方法。cited 判定因此是純本地
  計算,我們只用 `extract_report_urls` / `normalize_citation_url` 算出事實標記回傳,不把
  cited-only 做成 MCP 參數(那會把選擇政策塞進 capability layer,違反 ADR-0007)。
- **兩顆 URL normalizer 不可混用**(SDK docstring 自己寫明 distinct,contract 測試有鎖):
  `research.normalize_citation_url` 給「報告 markdown 裡的引用」比對——strip 尾端標點、
  **保留 fragment**;`_research._normalize_import_verification_url` 給 import identity——
  **丟掉 fragment**(伺服器存的時候剝掉)、不 strip 標點。`research_import` 選來源必須用
  **後者**,否則 `#a`/`#b` 兩個候選在我們眼中是兩筆、在 SDK 的 timeout readback 對帳中是
  同一筆,筆數就對不起來。**兩顆都不 strip 前後空白**,呼叫端傳進來的 URL 要自己先 strip。
  後者是私有 API,靠 `test_import_identity_differs_from_citation_identity` 當 tripwire。
- **`research_import` 必須自己驗 `status == completed`**:SDK 的 importer **完全不做**
  lifecycle 檢查,而 `failed` 的 task 仍可能留著已解析的 `sources` —— 少了這道 gate 就能
  繞過 `research_wait` 匯入半套或作廢的候選。identity 碰撞則**只檢查被選取的那些**:
  候選清單裡兩筆不相干的來源剛好 canonical 相同,不該讓一次合法 selection 整批失敗
  (v0.4.0 曾這樣過度 fail-closed)。
- **`import_sources_with_verification` 的 readback 只涵蓋 SDK 自己的 `RPCTimeoutError`**,
  **不涵蓋**外層 MCP client timeout / coroutine cancellation / server 被砍。外層結果不明時
  重呼 `research_import` 會重複匯入 —— 先 `source_list` 對帳。之所以只算 P2 而非 P1,是因為
  ADR-0008 把 research 綁在拋棄式 scratch notebook:對不清楚就丟掉整個 notebook,
  episode notebook 不受影響。
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
- **`generate_report` 的三種靜默吞噬**(`_artifact/payloads.py:219,538`):`custom` 沒給
  `custom_prompt` 會套通用預設句、靜態格式給了 `custom_prompt` 會被丟掉、`custom` 的
  `extra_instructions` 不串接。SDK 全都不 raise,要燒完一次配額拿到錯的講義才發現 →
  `_validate_report_prompt` 在打 RPC 前擋掉三種。
- **`retry_failed` 與其他 generate 的錯誤契約不同**:它對伺服器端同步拒絕(rate limit /
  配額)是 **raise**,不像 `generate_*` / `revise_slide` 吞成 `status="failed"`(SDK 說明是
  ADR-0019「async kickoff」,新方法born on the right side)。`artifact_retry_failed` 因此
  不必為拒絕設計回傳碼,但仍保留 `ensure_started` 擋空 task_id。
- **未暴露的 artifact 型別是產品決策**:video / cinematic_video / infographic / quiz /
  flashcards / data_table / mind_map 的 `generate_*` 都**刻意不做**成 MCP tool(不出 YouTube 版;
  封面走 `notebooklm-cover` 的 HTML+Chrome 決定性管線,不能換成 infographic——發布端拿封面
  bytes 做 content-hash)。`artifact_list(kind=…)` 仍可列出它們,那只是讀取面。
- **封面圖 = 凍結的 HTML template + headless Chrome 光柵化**(`notebooklm-cover`,實作
  `cover_cli.py`)。設計固化在 `notebooklm_mcp/assets/cover_episode.html` / `cover_show.html`
  (由 **agy/Gemini 設計、產出自包含 HTML**,已 commit 進 repo);**生封面時不叫 agy**——工具只做
  「填佔位符 `__SHOW__`/`__EPNUM__`/`__TITLE__`/`__BYLINE__`/`__HUE__` → Chrome 截 3000² → RGB JPEG
  → `validate_artwork`」,離線、決定性、無 LLM。要換整體設計才再叫一次 agy 重生 template(手動、很少)。
  agy 這類 coding agent **仍不能直接吐點陣圖**,但**擅長出 HTML/CSS**,交給 Chrome 光柵化質感高一截、
  改版只改 template。**需系統有 headless Chrome**(`google-chrome`/`chromium`;`--chrome` 或
  `NOTEBOOKLM_COVER_CHROME` 指定)——只有「產封面的那台」需要,3 個認證 VM 不用。NotebookLM 下載的
  音檔是 **fragmented-MP4 / DASH**(AAC),雖常用 `.mp3` 副檔名卻不是 MP3；`_embed_cover`
  發布前會正向辨識並轉成 256 kbps true MP3，再寫 ID3/APIC(見下方 gotcha)。
- **單集封面**:`notebooklm-cover --manifest <json> --show-name Audicast --byline audichuang [--output-dir <dir>]`
  批次讀 episodes 逐集填 episode template(集號決定色相 `(n*77)%360`、集標當大標、EP 徽章)、
  把絕對 `cover_path` 寫回 manifest,供 `publish_series` 吃(該集 `<item>` 掛 `itunes:image`,
  缺 `cover_path` 直接 fail,不 fallback 節目封面)。**節目封面**:`--show --output assets/cover.jpg
  --show-name Audicast --tagline "…" --byline audichuang`。單集一次性:`--output --episode EP0n --title "…"`。
  **決定性鐵律**:發布端用封面 bytes 做 content-hash,同一集必須永遠生同一張——所以色相只能是集號的
  函式(不可隨機);但 bytes 也吃 **Chrome/CJK 字型版本**,換版本會漂 → 固定在同一台機器產、產出的
  JPEG 即事實來源(可版控;等同舊 PIL 的字型 caveat)。`.jpg`/`.png` uploader 白名單本來就放行,
  **不用重部署 NAS**(不像加 `.pdf`/`.html` 那次)。
- **單集封面「app 讀不到」的真根因 = 音檔沒內嵌圖 + 格式假標**:feed 的 `<item>`
  itunes:image 我方掛得對、URL 也公網可達(實測 200),但 Apple/Spotify 常優先吃音檔內嵌圖。
  NotebookLM raw 檔是 fragmented MP4/AAC、常偽裝成 `.mp3`;若仍以 `audio/mpeg` 發布，副檔名/MIME/
  container/codec 四者矛盾。故 `publish_series` 的 `_embed_cover` seam 先用 ffprobe **正向辨識**:
  MP4/AAC → ffmpeg 轉 256 kbps、44.1 kHz stereo true MP3；既有 true MP3 不重編音訊；其他格式
  fail-closed。最後用 mutagen 寫單一 authoritative front-cover ID3/APIC。這讓公開 enclosure 的
  `.mp3` + `audio/mpeg` 與實際 MP3 完全一致且可 HTTP range seek。**代價**:首次啟用正規化會讓
  MP4 來源各集換一次 content-hash URL(舊 URL 因 uploader 不刪仍可用,訂閱者可能重抓)。
  exact output bytes/hash 只在相同 ffmpeg/libmp3lame 工具鏈穩定；升級會再次 churn enclosure URL，
  故產製環境要固定版本，計畫升級時要預期重驗。測試用假 audio bytes 由 autouse fixture 把
  `_embed_cover` 換成 no-op；真媒體 regression 鎖住 MP4→MP3、
  MP3 保留、ADTS 拒絕、端到端 uploaded bytes/副檔名/RSS MIME 一致與決定性。

## Conventions

- 全程繁體中文註解/文件。TDD:測試先紅再綠,每任務一 commit。
- commit 訊息寫清楚「症狀 + 根因 + 為何這樣修」(commit 與 docs 是團隊經驗庫)。
- 不污染 `_research/`(唯讀參考 clone)。

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

驗證:`grep -rn "notebooklm-mcp.git@v" --include="*.md" . ../podcast-lab ../../audi-skill | grep -v docs/superpowers`
(`docs/superpowers/` 的歷史計畫書刻意不改——那是當時的事實)。
