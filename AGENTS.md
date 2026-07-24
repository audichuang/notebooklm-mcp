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
uv tool install --python 3.12 "git+https://github.com/audichuang/notebooklm-mcp.git@v0.3.0"

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
  - `tools_artifacts.py` — `generate_slides`(簡報 PDF)/ `generate_report`(研讀 Markdown)按需生,
    路徑回寫 `series_manifest.json`(供 publish 附連結);不碰音檔迴圈。另含
    `episode_set_description`(show notes 回寫 manifest,預設清引用標記;同 process 讀改寫)
  - `publish/notes_html.py` — report Markdown → 自包含 HTML;渲染後掃描 script/外部資源標記,命中 fail-closed
  - `tools_podcast.py` — `podcast_episode`(單集 5 步)/ `podcast_episode_resume`(斷線後續跑該集)/
    `podcast_series`(整季純程式碼迴圈)
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
- `sources.add_file` 有 `title`(0.7.x),**但內部仍是 add→rename 兩步且改名失敗只 log 不
  raise** → podcast 流程維持顯式 add_file → rename 兩步(fail-loud);`source_add_file` 工具
  的 title= 有回傳後檢,未生效會 raise。
- `rename()` 的 `return_object` **預設 True 會再抓一次全量清單驗證、可能 raise not-found**
  → 我們所有 rename 呼叫點顯式傳 `return_object=False`(fire-and-forget,RPC 錯誤仍會 raise)。
- 0.7.0 起 source add API 尾端參數(`wait`/`wait_timeout`/`title` 等)**keyword-only**,
  位置呼叫直接 TypeError(contract 測試有鎖)。
- `wait_for_completion` 的 `poll_interval` 已移除(0.7.x);呼叫只用 `timeout=`。
- 改 contract 測試時對「**實裝版本**」跑,別信 `_research/` 的 HEAD clone。
- 改 podcast 流程務必對照鐵律:**每集(含最後一集)都要上傳自己的 mp3 回筆記本並命名**
  `EP{n:02d} 標題`(例 `EP01 心法篇`,標題來自大綱的 `title`,必填非空)——與該集的工作室
  artifact **完全同名**(同一字串),讓兩區命名一致、記錄完整。命名邏輯集中在 `_episode_label()`。
- `get_fulltext` 會在 CJK 字元間插空格;關鍵字比對前先 `"".join(text.split())`。
- **`chat_ask` 回答夾帶引用標記**(`[1]`/`[3, 4]`/`[8-10]`);要當公開文字(如單集 show notes)
  前用 regex `\[[\d,\s\-–]+\]` 清掉。單集簡介 = manifest 該集加 `description`(見 skill repo `audi-skill/notebooklm` 的 SKILL §Publish)。
- **附加簡報/講義**:`generate_slides`/`generate_report` 只吃**傳入的 `source_ids`**才聚焦原文;
  不傳則 SDK 用全部來源(v1 不自動排除音檔來源)。附件缺檔時 `publish_series` **fail-fast**。
  **順序鐵律**:uploader 白名單放寬 `.pdf`/`.html` 後**要先重部署 NAS**,再跑帶附件的發布,否則附件 PUT 404。
  講義是 Markdown(`download_report`),`notes_html` 渲染成 HTML 才 host;`.md` 只留本機。
- quiz/flashcards 無 `--language`(在 brief 內指定);mind-map 無法指定語言。
- **發布單集也走 `podcast_series`**(episodes 放一集):`publish_series` 只吃
  `series_manifest.json`,`podcast_episode` 純單集不產 manifest。
- **X 長文(Article)餵不進來**:貼文只是 t.co 短連結,文章本體在 `x.com/i/article/…`
  需登入,`source_add_url` / WebFetch 都回 402。存成 PDF(Read 讀得出全文)或直接貼全文
  用 `source_add_text`。
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
  MP4 來源各集換一次 content-hash URL(舊 URL 因 uploader 不刪仍可用,訂閱者可能重抓)。測試用假
  audio bytes 由 autouse fixture 把 `_embed_cover` 換成 no-op；真媒體 regression 鎖住 MP4→MP3、
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
