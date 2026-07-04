# notebooklm-skill

自建的**薄 MCP server**(建在 [`notebooklm-py`](https://pypi.org/project/notebooklm-py/) `0.3.4` 之上)+ 薄 `SKILL.md` 路由層 + **確定性續集 podcast 工具**。讓 Claude Code 透過一個本機 MCP server 操作 NotebookLM:建立筆記本、加來源、生成 `zh_Hant` 優先的 audio overview、做 source-grounded 問答,以及跑「無 LLM 在迴圈內、可復現」的連續 podcast 工作流。

- 設計理由與 live 驗證踩坑:[`docs/superpowers/notebooklm-mcp-findings.md`](docs/superpowers/notebooklm-mcp-findings.md)(經驗庫,值得先讀)
- 工具路由與意圖判斷:[`SKILL.md`](SKILL.md)
- 完整工具參數/回傳:[`references/cli-reference.md`](references/cli-reference.md)
- 開發者導覽:[`CLAUDE.md`](CLAUDE.md)

---

## 需求

| 項目 | 說明 |
|------|------|
| **Python 3.12** | 必須用 3.12。**3.14 會觸發 SDK 的 `inspect.signature` bug**;`pyproject.toml` 雖寫 `>=3.11`,實務以 3.12 為準。 |
| **[uv](https://docs.astral.sh/uv/)** | 套件/虛擬環境管理。 |
| **[Doppler CLI](https://docs.doppler.com/docs/cli)** | 注入認證 `NOTEBOOKLM_AUTH_JSON`(多 VM 唯讀同步)。 |
| 有 GUI 的瀏覽器環境 | **僅「登入機」需要**,用來跑一次 `notebooklm login`;無頭 VM 不需要。 |

---

## 安裝

```bash
# 必須用 Python 3.12
uv venv --python 3.12
uv pip install -e ".[dev]"

# 離線測試(mock client,不需網路/認證)——應全數通過
uv run pytest -q
```

---

## 認證(Doppler,多 VM 唯讀同步)

`notebooklm-py` 讀環境變數 `NOTEBOOKLM_AUTH_JSON`;當它存在時 **SDK 跳過磁碟寫回(唯讀)**,所以多台 VM 各自消費同一份 Doppler secret 不會互相漂移。登入只在「有 GUI 的機器」做一次 → 推 Doppler → 所有無頭機消費。

### 首次設定 / 認證過期(在有 GUI 的機器)

```bash
uv pip install -e ".[login]"        # 僅登入機需要(瀏覽器 extra)
uv run playwright install chromium
notebooklm login                    # 開瀏覽器登入,看到 NotebookLM 首頁才按 ENTER
bash scripts/sync-auth.sh           # 推到 Doppler,所有 VM 下次啟動即生效
```

### 無頭機備援(貼上 storage_state JSON)

無頭 VM 跑不了 `notebooklm login`(缺 X server)。可在有登入的機器把 storage_state JSON 複製過來貼上:

```bash
uv run python -m notebooklm_mcp.auth_cli   # 貼 JSON 後按 Ctrl-D;預設寫到 ~/.notebooklm/storage_state.json(0600)
```

> 認證材料(`storage_state*.json`、`~/.notebooklm/`)已被 `.gitignore` 排除,**永不進 git**。

---

## 註冊進 Claude Code

每台機器各跑一份本機 MCP server process,由 `doppler run` 包住注入認證。

### 方式 A(推薦):`claude mcp add`

```bash
claude mcp add notebooklm -- doppler run -p notebooklm -c dev -- \
  uv run --directory "$(pwd)" python -m notebooklm_mcp.server --transport stdio
```

`--directory "$(pwd)"` 讓 server 不受啟動時的 cwd 影響(請在 repo 根目錄執行,或把它換成 repo 絕對路徑)。

### 方式 B:專案層級 `.mcp.json`(本機設定,**不進 git**)

`.mcp.json` 是**每台機器各自的本機設定、不含任何密鑰**(認證全靠 Doppler 注入),所以已加入 `.gitignore`、不被 git 追蹤。要用專案層級設定時,在 repo 根目錄手動建立:

```json
{
  "mcpServers": {
    "notebooklm": {
      "command": "doppler",
      "args": [
        "run", "-p", "notebooklm", "-c", "dev", "--",
        "uv", "run", "--directory", "/絕對路徑/notebooklm-skill",
        "python", "-m", "notebooklm_mcp.server", "--transport", "stdio"
      ]
    }
  }
}
```

> 記得填**絕對路徑**的 `--directory`(否則 server 會依當下 cwd 找專案而可能失敗)。多機之間請各自建立,不要 commit。

細節見 [`docs/mcp-setup.md`](docs/mcp-setup.md)。

---

## 手動跑 server

```bash
# stdio(Claude Code 用的模式)
doppler run -p notebooklm -c dev -- uv run python -m notebooklm_mcp.server --transport stdio

# HTTP 模式(僅限信任的私有網路 / 防火牆後 —— MCP server 自身無認證層)
doppler run -p notebooklm -c dev -- uv run python -m notebooklm_mcp.server \
  --transport streamable-http --host 0.0.0.0 --port 8484
```

啟動冒煙測試(預期無 traceback / 認證錯誤):

```bash
timeout 8 doppler run -p notebooklm -c dev -- \
  uv run python -m notebooklm_mcp.server --transport stdio < /dev/null
```

---

## 使用

意圖判斷與工具路由都在 [`SKILL.md`](SKILL.md);完整參數見 [`references/cli-reference.md`](references/cli-reference.md)。

### 簡單任務

1. `notebook_create` 或用既有 `notebook_id`
2. `source_add_url` / `source_add_text` / `source_add_file` 加來源(音檔來源傳 `mime_type="audio/mpeg"`)
3. `generate_audio`(預設 `zh_Hant`)→ `artifact_wait` → `artifact_download_audio` → `artifact_rename`
4. 問答:`chat_ask`

### 連續 podcast 系列

先用對話**規劃整季大綱**(這是唯一有 LLM 判斷的前置點),每集都要有 **`title`(必填非空)** 與 **`brief`**;大綱定稿後是純程式碼迴圈、無 LLM、可復現。

```jsonc
// episodes 形狀(經使用者核可後傳入 podcast_series)
[
  {"title": "心法篇", "brief": "本集任務、主持人人設、節目風格、承接要求…"},
  {"title": "實戰篇", "brief": "…"}
]
```

- 工作室 artifact 與自上傳來源都命名為 **`EP{n:02d} 標題`**(例 `EP01 心法篇`)——兩區完全同名。
- 每集生成後自上傳本集 mp3 為命名來源,下一集自動讀到它做承接(連續性來自 NotebookLM 轉錄前集音檔,不是我們的模型)。
- **續製**:傳「完整的 episodes 陣列」加 `start=N`;依賴筆記本既有的 `EP{N-1:02d} 標題` 來源(非本機 mp3)。`series_manifest.json` 會**合併保留**前面集數。
- **重生壞集(reject-then-delete)**:先 `source_delete` 移除該集來源,再 `podcast_episode` 重生,避免壞集被下一集繼承。

範例見 [`references/series_example.md`](references/series_example.md);brief / 提示詞模板見 [`references/episodic_prompts.md`](references/episodic_prompts.md);疑難排解見 [`references/troubleshooting.md`](references/troubleshooting.md)。

### 發布成 podcast RSS(Apple Podcast 訂閱)

一主題 = 一節目 = 一 feed。跑完 `podcast_series` 後,把整季發布成可訂閱的 RSS feed:

1. `publish_series(show_id, notebook_id, manifest_path, show_title, show_description, author, owner_name, owner_email, artwork_path)`
   - `show_id`:穩定 slug(feed identity,決定 URL,**永不改**;`[a-z0-9-]`)
   - `manifest_path`:`podcast_series` 產出的 `series_manifest.json` 路徑
   - `artwork_path`:正方形 1400–3000px、PNG/JPG、RGB、**無 alpha**(Apple Show Cover 規格)
   - `owner_email`:Apple 必填
2. 回傳 `feed_url` → 在 Apple Podcast「用 URL 加入節目」貼上訂閱。續製只要重跑 `publish_series`
   (同 URL / 同 GUID),Apple 自動抓新集。

需 4 個 Doppler secret(`PODCAST_PUBLIC_BASE_URL` / `PODCAST_TOKEN_SALT` /
`PODCAST_UPLOAD_URL` / `PODCAST_UPLOAD_TOKEN`);MCP 不掛載 NAS,改內網 HTTP PUT 到
NAS uploader(讀寫分離),讀站仍經 Cloudflare Tunnel 對外;托管與一次性部署見
[podcast-feed-host](https://github.com/audichuang/podcast-feed-host)。**重跑 `publish_series`
= 重發整季**(逐檔覆寫,GUID/URL 不變的集數視為同集更新)。

---

## 測試

```bash
uv run pytest -q     # 全離線(mock client),不需網路/認證
```

`tests/test_contracts.py` 用 `inspect.signature` **鎖住 `notebooklm-py` 的公開 API**,作為擋上游漂移的離線 tripwire。改契約測試時務必對「**實際安裝的 0.3.4**」跑,不要信 GitHub HEAD 的 clone。

---

## 專案結構

```
notebooklm_mcp/
  app.py           FastMCP app + tool 註冊 + lifespan(單一長駐 client)+ 多 transport main
  server.py        薄 launcher(re-export app),避免 python -m 的 __main__ 雙重 import
  tools_basic.py   notebook / source / generate_audio / artifact / chat_ask(薄包,zh_Hant 預設)
  tools_podcast.py podcast_episode(單集)/ podcast_series(整季純程式碼迴圈)
  _status.py       生成失敗 fail-fast 守衛(SDK 以 status 回報失敗,而非 raise)
  languages.py     語言白名單 + zh_Hant 預設
  enums.py         字串 → int-enum(audio format / length)
  runtime.py       長駐 client holder
  auth_cli.py      貼 storage_state JSON 建檔(無頭備援)
tests/             離線測試 + 契約 tripwire
references/        工具參考、podcast 範例、提示詞模板、疑難排解
docs/              設計 spec、實作計畫、findings 經驗庫、MCP setup
SKILL.md           薄路由層(供 Claude Code skill 使用)
```

深入設計與踩坑經驗見 [`docs/superpowers/notebooklm-mcp-findings.md`](docs/superpowers/notebooklm-mcp-findings.md) 與 [`CLAUDE.md`](CLAUDE.md) 的 Gotchas。
