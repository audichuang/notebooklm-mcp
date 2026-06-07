# CLAUDE.md — notebooklm-skill

自建薄 MCP server(建在 `notebooklm-py` 之上)+ 薄 `SKILL.md` 路由層 + 確定性續集 podcast 工具。
完整設計理由與 live 驗證踩坑見 `docs/superpowers/notebooklm-mcp-findings.md`(經驗庫,值得先讀)。

## Commands

```bash
# 安裝（必須用 Python 3.12；3.14 會觸發 SDK 的 inspect.signature bug）
uv venv --python 3.12 && uv pip install -e ".[dev]"

# 全套離線測試（mock client，不需網路/認證）
uv run pytest -q

# 跑 MCP server（認證由 doppler 注入 NOTEBOOKLM_AUTH_JSON）
doppler run -p notebooklm -c dev -- uv run python -m notebooklm_mcp.server --transport stdio
#   HTTP 模式：--transport streamable-http --host 0.0.0.0 --port 8484

# 註冊進 Claude Code（3 台 VM 各跑一份；細節見 docs/mcp-setup.md）
claude mcp add notebooklm -- doppler run -p notebooklm -c dev -- \
  uv run --directory <repo> python -m notebooklm_mcp.server --transport stdio
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
  - `server.py` — FastMCP app + lifespan(長駐單一 client,跨長生成不掉線)+ 多 transport main
  - `tools_basic.py` — notebook / source / `generate_audio` / artifact / `chat_ask`(薄包,`zh_Hant` 預設)
  - `tools_podcast.py` — `podcast_episode`(單集 5 步)/ `podcast_series`(整季純程式碼迴圈)
  - `languages.py`(白名單 + `zh_Hant` 預設)、`enums.py`(字串→int-enum)、`runtime.py`(client holder)
  - `auth_cli.py` — 貼 storage_state JSON 建檔(headless 備援)
- `tests/test_contracts.py` — 用 `inspect.signature` 鎖住 `notebooklm-py` 公開 API,擋上游漂移(離線 tripwire)
- `SKILL.md` 薄路由層;`references/` 工具參考 + 連續性提示詞模板;`docs/superpowers/` 設計/計畫/findings

**判斷只在「大綱」前置點**(Opus 規劃每集 brief,人核可);大綱定稿後是確定性腳本,迴圈內無 LLM。

## Gotchas(notebooklm-py 0.3.4,pin `>=0.3,<0.4`;與 GitHub HEAD 不同,以**實裝版本**為準)

- `from_storage()` 是 coroutine → `async with await NotebookLMClient.from_storage()`。
- `GenerationStatus` **無 `artifact_id`**;`task_id` 本身就是 artifact id(download/rename 用它)。
- `sources.add_file` **無 `title`** 參數;要命名來源得另呼叫 `sources.rename`。
- `artifacts.rename` / `sources.rename` **無 `return_object`** 參數(傳了會 TypeError)。
- 改 contract 測試時對「**實裝版本**」跑,別信 `_research/` 的 HEAD clone。
- 改 podcast 流程務必對照鐵律:**每集(含最後一集)都要上傳自己的 mp3 回筆記本並命名**
  `EP{n:02d}`——與該集的工作室 artifact **完全同名**(無後綴),讓兩區命名一致、記錄完整。
- `get_fulltext` 會在 CJK 字元間插空格;關鍵字比對前先 `"".join(text.split())`。
- quiz/flashcards 無 `--language`(在 brief 內指定);mind-map 無法指定語言。

## Conventions

- 全程繁體中文註解/文件。TDD:測試先紅再綠,每任務一 commit。
- commit 訊息寫清楚「症狀 + 根因 + 為何這樣修」(commit 與 docs 是團隊經驗庫)。
- 不污染 `_research/`(唯讀參考 clone)。
