# 模組設計筆記

`AGENTS.md` §Architecture 的完整版。留在那裡的是**角色導覽 + 紅線**(每次開工都要知道的);
這裡是**動到那個模組才要知道的設計決策與實作細節**。

> **結構問題不要看這份** —— 有哪些檔、誰呼叫誰、某個符號的源碼,問 `codegraph`(這個 repo
> 有 `.codegraph/`)或直接讀 code,答案永遠最新。這份留的是 code 讀不出來的**為什麼**:
> 哪些是刻意的取捨、哪些看起來能改其實不能、哪一條是被真實事故換來的。


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

