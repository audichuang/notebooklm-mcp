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
    可輪詢的 `task_id` 就走;deep 取 SDK `report_id`,fast 取 SDK `task_id`)/
    `research_wait`(可重入,
    回候選 + 報告)/ `research_import`(只匯入
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
    (`_reuse_frozen_input_attempt` 走 `find_attempt` 這個 tombstone gate、且擋
    `output_attempt_id` 已存在),所以斷線重跑不會重複建 attempt 或重燒配額;取代版要凍新
    bundle。coverage ledger 與 evidence manifest 不只驗 bytes/hash，還要 strict 驗
    `schema_version`、`qa_kind`、episode identity、disposition-specific row keys，以及 evidence
    artifact 的 unique id/confined relative path/lowercase SHA-256/positive byte count。
    `rollback_attempt_binding` 只刪「這次自己寫的那份 bytes」,清理失敗回 note 掛上
    原例外而**不 raise**(在 except handler 裡再拋會蓋掉真正該讀的錯誤)。
    **`input_bundle_path` 是相對於 workspace 的路徑**,絕對路徑、`..`、路徑上任何 symlink 一律拒。
    **workspace 由 host 用 `workspace_root` 宣告**(節目目錄,manifest 必須在它底下);省略時推成
    manifest 的祖父目錄,但祖父目錄裡還有別的 `series_manifest.json` 就 fail-closed(ADR-0012:
    manifest 放 show root 時祖父是整個多節目容器,別節目的 bundle 會過圍籬,2026-08-30 實測)。
    **manifest 的父目錄刻意不限定名稱**——三個 podcast 專案分別用 `manifest/`、`output/`、
    `season-01/output/`,曾經寫死 `manifest/` 讓功能只有一個專案能用;containment 由
    `relative_to(workspace)` 保證,目錄**名字與深度都不是安全邊界**。目前只有 `podcast_episode`
    支援,`podcast_series` 尚未接。
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
  - `languages.py`(白名單 + `zh_Hant` 預設)、`enums.py`(字串→int-enum)、`runtime.py`(client holder;單一槽位表 `Slot`:活/死槽位同表、冷卻在 `cooled_at`、`find_client(label, casefold=)` 與 `_find_slot` 是唯一的 label 反查,逐字或大小寫不分由呼叫端選)
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

### `attempt_state.py` —— attempt 狀態轉換

`dispatch.status` 轉換(含 `claim_prepared` / `reset_for_resend` 這一對)、artifact 綁定的 `remote` 寫入、failover 稽核(`record_dispatch_failover` 的 `dispatch.account` + `errors[]`)與 tombstone 的落筆(`record_retraction`)收在這裡。**仍在模組外**:audio_finalize 的 remote 終態與 finalize checkpoint 形狀、retract 的 tombstone 組裝、`active_attempt_id` 指標、`_create_audio_attempt` 的初始 prepared 骨架;episode 級 output 投影在 `output_projection.py`。轉換函式簽名是
`(manifest, episode_n, attempt_id, …)`,設計成在 `ManifestStore.update` 的 mutate 內呼叫
(鎖與 CAS 仍歸 `manifest_store`)。store-bound 的一面是 `AttemptRef(store, episode_n, attempt_id)`:
`read` / `find` / `mutate(fn(manifest, episode, attempt))` 與各轉換的同名方法,tombstone default-deny 在裡面;
`tools_podcast` 與 `audio_finalize` 共用(後者帶自己的 `retracted_hint`)。重點:

- `apply_accepted(attempt, artifact_id, origin, reset_error=)` 是「attempt 擁有 artifact、dispatch 已受理」
  的唯一欄位寫入;`bind_reconciled` / `bind_sdk_heuristic` / `bind_caller_verified` 各帶自己的守門(`new_resume_binding` 只建底稿,不帶守門),**守門差異是現況而非 bug 清單**:reconciled 不查來源狀態、sdk 允許四個來源且重複綁為 no-op、
  adopt 只認 `acceptance_unknown`/`reconciliation_ambiguous` 並清 error 欄位、`attempt_admission.decide()` 對每個 RESUME 請求都會掃 claims,只有 `new_resume_binding` 自己不再掃一次。
- `find_attempt` 的 `retracted_hint` 只換訊息尾巴(finalize 場景傳自己的);拒絕行為共用。
- 不收進來:`attempt_capabilities()` / `attempt_next_step()` 產生的指引(紅線)、promotion 的 legacy 衝突守門
  (要 caps)、reconcile 回傳點鎖外算 caps 的時機(F7,gotchas-attempt)。
- 測試:`tests/test_attempt_state.py` 以 from-state × event 表鎖轉換。

### `attempt_admission.py` —— attempt 建立/沿用的單一決策

四個入口(`_create_audio_attempt`、`_ensure_resume_attempt` 的 claimed 與新建分支、`_reuse_frozen_input_attempt`)
過去各抄一份守門。現在 `decide()` 依請求來源選分支,按 `_GUARDS` 表的**固定順序**跑守門,回
`Admit(kind)` 或 `Refusal(exc_type, message)`;呼叫端只負責寫(`attach_new_attempt` / reset / rearm)
或 `raise_()`。順序即「同時違反兩條時讀到哪一句」,不可調換。紀錄 literal 各一份
(`new_prepared_attempt` / `new_resume_attempt`)。依賴方向:本模組 → `attempt_state` / `audio_finalize` / `output_projection` /
`naming`,不 import `tools_podcast`(指引以 callable 注入,避免循環 import)。

刻意保留的差異(各入口原本就不同,統一是行為變更):

- 標題不可變:generate 只在 `retracted_attempt_ids` 存在時擋;resume(兩支)永遠比 episode 級標題;
  claimed 另比 attempt 級標題。
- notebook 身分:generate / resume 新建比 episode 級;claimed 與 frozen 只比 attempt 級。
- 既有輸出:generate 擋 pointer(真值)|硬證據|legacy preparatory;resume 新建擋 pointer(is-not-None)並
  對 legacy 硬證據只放行同一顆 artifact;claimed 只擋「output 屬於別顆」;frozen 只看 pointer;series 洩漏判斷
  用 `has_output_pointer_or_hard`。
- series 自檢三種 subject(active / output / legacy)訊息不同,legacy 只比標題。

測試:`tests/test_attempt_admission.py`,episode 狀態 × 來源 → 決策表,純 dict。

### `attempt_guidance.py` —— attempt 指引的單一來源

`attempt_capabilities()`(狀態 → `safe_next_action` 與各維度結論)、`attempt_next_step()`(結論 → 一句可執行的話)、
`retract_hint()` / `cleanup_sentence()`(retract 建議與清理句的唯一 renderer)、`regeneration_entry_point()`
(重生用哪支工具),以及 `ACTION_*` / `SAFE_NEXT_ACTIONS` 字面值。原本是 `tools_podcast` 的私有符號,搬出來只是
給紅線(gotchas-attempt)一個模組邊界,訊息文字逐字未變。純函式,依賴方向:本模組 → `attempt_state` /
`audio_finalize` / `cleanup_obligation` / `output_projection`,不 import `tools_podcast`。

- `attempt_admission` 仍不 import 本模組:指引由 `tools_podcast._admission_next_step` 注入(真 seam,別拆)。
- 留在 `tools_podcast` 的:`_series_handoff_caps` / `_series_owns_attempt` / `_SERIES_ARGUMENT_DRIFT_HINT`
  (series 層的「要不要交棒」政策,依賴 `_series_will_redispatch`,本身不產生狀態指引)。
- series active 分支的自動轉換表(`_series_redispatch_kind`:`rearm` → `supersede` → `send`,`None` = 收尾路徑)
  也留在 `tools_podcast`,**刻意不從它推導** caps 的 `never_dispatched` / `remote_terminal`(`prepared`+`failed`
  兩者不同),`defers_to_output_guard` 也刻意用原始 `TERMINAL_REMOTE` 判準(`not_accepted`+`failed` 不同)。
- 測試:`tests/test_attempt_capabilities.py` 對狀態組合的笛卡爾積逐格鎖住。

## cleanup_obligation.py:清理義務單一模組

義務(`episode["pending_source_cleanup"]`,每筆 `{source_id, notebook_id}`;legacy 純字串讀出時補 fallback)
與 tombstone 上的 `source_cleanup_unresolved` 兩種,舊版散在 retract / adopt / capabilities / gate 各自摸欄位。
現在形狀只在本模組;`tools_podcast` 只呼叫 `list_obligations` / `record_cleanup_obligation` /
`queue_pending_source_cleanup` 與 gate。Gate 只吃 `list_sources`(`client.sources.list`)當遠端 port,
所以可用假清單在介面層測試。四階段順序、CAS 的不對稱處置(保留加義務、丟掉結案旗標)原樣搬入,
理由見 `assert_generation_allowed` / `settle_cleanup_state` docstring。
series 路徑會先跑一次 gate、fresh 路徑再進 `_run_episode` 跑第二次(第二次只在仍有義務時才打 list)——刻意保留。

## canonical_audio.py:發布端的 canonical 音檔查詢

`canonical_audio(ep)` 回 `CanonicalAudio`(mp3_path / artifact_id / notebook_id / sha256,
或 `Unverifiable` 四種原因之一);`verify_file` 做 sha 比對。LEGACY / NO_SHA 放行並標進
`sha_unverified_episodes`,NO_OUTPUT_POINTER / DANGLING_POINTER fail-closed(訊息原樣)。
本模組不 raise 歧義,讓 `tools_publish` 維持「先 resolve 檔案、再驗出身」的原順序。
`_ensure_local_mp3` 重抓用 `runtime.get_client()`(當前帳號)而非記錄帳號,為既有行為,刻意未改。
