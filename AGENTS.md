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

# 消費端安裝（3 VM / podcast-lab 各裝一次；pin tag,不追 master）
uv tool install --python 3.12 "git+https://github.com/audichuang/notebooklm-mcp.git@v0.1.0"

# 跑 MCP server（裝好後零路徑命令；認證由 doppler 注入 NOTEBOOKLM_AUTH_JSON）
doppler run -p notebooklm -c dev -- notebooklm-mcp --transport stdio
#   HTTP 模式：--transport streamable-http --host 0.0.0.0 --port 8484
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
    main。獨立模組以確保「唯一 mcp instance」,不論用什麼方式啟動。
  - `server.py` — thin launcher,只 `from .app import _lifespan, main, mcp`(可被當 `__main__` 跑)
  - `tools_basic.py` — notebook / source / `generate_audio` / artifact / `chat_ask`(薄包,`zh_Hant` 預設)。
    含讀取/觀測面:`artifact_list`(列筆記本現有 artifact,救援/對帳用)、`source_list`、
    `source_fulltext`、`notebook_get`;`chat_ask` 吃 `source_ids`(聚焦單集原文)/`conversation_id`
  - `tools_artifacts.py` — `generate_slides`(簡報 PDF)/ `generate_report`(研讀 Markdown)按需生,
    路徑回寫 `series_manifest.json`(供 publish 附連結);不碰音檔迴圈
  - `publish/notes_html.py` — report Markdown → 自包含 HTML;渲染後掃描 script/外部資源標記,命中 fail-closed
  - `tools_podcast.py` — `podcast_episode`(單集 5 步)/ `podcast_series`(整季純程式碼迴圈)
  - `tools_publish.py` — `publish_series` / `feed_info`(把整季發布成 Apple 合規
    RSS feed;薄 I/O 編排,內網 HTTP PUT 到 NAS uploader,提交順序:媒體檔→show.json→feed.xml/index.html)
  - `publish/` — 純邏輯(離線可測):`identity.py`(HMAC→base32 決定性 token + `episode_guid`,無 registry)、
    `layout.py`(內容 hash + 媒體檔名)、
    `artwork.py`(Apple Show Cover 規格驗證)、`rss_models.py` / `feed.py`(RSS+iTunes XML + index.html)
  - `_status.py` — generation-status 防護:SDK 把失敗/限流回報成 `task_id=""` 而非丟例外,用前要先擋掉
  - `languages.py`(白名單 + `zh_Hant` 預設)、`enums.py`(字串→int-enum)、`runtime.py`(client holder)
  - `auth_cli.py` — 貼 storage_state JSON 建檔(headless 備援)
  - `cover_cli.py` — `notebooklm-cover` console script(封面 CLI,PIL);原 `scripts/make_cover.py` 移進套件,才能隨 `uv tool install` 上 PATH
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

## Gotchas(notebooklm-py 0.4.1,pin `>=0.4.1,<0.5`;與 GitHub HEAD 不同,以**實裝版本**為準)

- `from_storage()` 仍是 coroutine → `async with await NotebookLMClient.from_storage(keepalive=600)`
  (免 await 慣用法 v0.5.0 才有)。`keepalive=600` 是 0.4.1 新參數:session 內背景
  RotateCookies task(process-scoped,隨 server 生滅),長生成不因 `__Secure-1PSIDTS`
  過期中途死。**env-var 唯讀模式下只轉記憶體、不落盤**,跨 session 的 cookie 老化
  不變——2–4 週一次 GUI 機重登 + `sync-auth.sh` 的節奏照舊。網路擋
  `accounts.google.com` 時可設 `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` 關閉。
- `GenerationStatus` **無 `artifact_id`**;`task_id` 本身就是 artifact id(download/rename 用它)。
- `sources.add_file` **無 `title`** 參數;要命名來源得另呼叫 `sources.rename`。
- `artifacts.rename` / `sources.rename` **無 `return_object`** 參數(傳了會 TypeError)。
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
- **封面圖用 `notebooklm-cover` 生**(console script,實作 `notebooklm_mcp/cover_cli.py`,PIL,
  已固化排版 + 自帶 Apple 驗證器)。`agy` 之類 coding agent **不能直接出點陣圖**,只會幫你寫這種
  PIL code;要「AI 生成圖」得另接影像模型(Imagen/DALL·E)。NotebookLM 下載的音檔是 MPEG-4 容器但
  副檔名 `.mp3`、以 `audio/mpeg` 發布,Apple 可正常播(已實測訂閱+播放通過)。
- **單集封面(每集各自封面)**:`notebooklm-cover` 加 `--episode EP0n` 走「集號決定性 HSL 配色」
  (色相 `(n*77)%360`)+ 集標當大標 + EP 徽章 + 節目名副標;不給 `--episode` 則產出與舊版
  **byte 完全一致的節目封面**(向後相容)。`notebooklm-cover --manifest <json> --show-name .. --tag .. --byline .. --output-dir <dir>`
  批次讀 episodes 逐集生、把絕對 `cover_path` 寫回 manifest,供 `publish_series` 吃(該集 `<item>`
  掛 `itunes:image`,沒給 fallback 節目封面)。**決定性鐵律**:發布端用封面 bytes 做 content-hash,
  同一集必須永遠生同一張,所以配色只能是集號的函式,不可隨機。`.jpg`/`.png` uploader 白名單本來就放行,
  **不用重部署 NAS**(不像加 `.pdf`/`.html` 那次)。

## Conventions

- 全程繁體中文註解/文件。TDD:測試先紅再綠,每任務一 commit。
- commit 訊息寫清楚「症狀 + 根因 + 為何這樣修」(commit 與 docs 是團隊經驗庫)。
- 不污染 `_research/`(唯讀參考 clone)。

## Cross-Repo Sync Checklist

MCP repo 與 skill repo 是一組配置。改動 MCP tools 時,同步更新 `/home/user/research/audi-skill/notebooklm/SKILL.md` 的工具表與 `/home/user/research/audi-skill/notebooklm/references/tool-reference.md`。

新增、移除或改名工具時,commit message 要明講 skill repo 是否已同步;若尚未同步,不要 push MCP release tag。若本 repo 已落地 CI hard check,PR 必須等該檢查綠燈後才能 tag release。
