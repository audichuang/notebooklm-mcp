# 設計:NotebookLM 拆 repo — MCP server 與 skill 分離

日期:2026-07-05 · 狀態:待實作(brainstorming 產出,已經使用者核可設計)

## 背景與問題

notebooklm 這個專案在本機有**多份完整副本**,各自帶一整份 `notebooklm_mcp/` 程式碼:
- `audiskill/notebooklm-skill`(開發原始 repo,自己的 git)
- `audi-skill/notebooklm`(已發布 skills 集合 repo 的子目錄,podcast-lab 的 MCP 指向這裡)
- podcast-lab 內的快照

痛點三連:
1. **難裝**:每處 `.mcp.json` 用 `uv run --directory <絕對路徑> python -m notebooklm_mcp.server` invoke 一份原始碼 checkout,綁死絕對路徑。
2. **路徑亂放**:每份副本、每台 VM、每個專案各一個絕對路徑,到處不一致。
3. **飄移**:多份程式碼副本,改一份忘了同步另一份。2026-07-05 實際踩到——在 dev repo 改完 6 個 commit,忘了 `audi-skill` 副本,事後手動補拷。

既有資產:`pyproject.toml` 已定義 console script `notebooklm-mcp = "notebooklm_mcp.server:main"`,`main()` 也已吃 `--transport`——**基礎設施早在,只是沒被用**。

## 目標

把「MCP server 程式碼」與「skill 指引層」拆成兩個**定位清楚**的 repo,使:
- 安裝 = 一行 `uv tool install`;`.mcp.json` **零路徑、到處一致**。
- 程式碼**只有一份來源** → 結構性根除飄移。

## 設計

### §1 定位切分

| | **MCP repo**(`notebooklm-mcp`,新獨立 GitHub repo) | **skill**(`audi-skill/notebooklm`) |
|---|---|---|
| 定位 | 工具後端:「是什麼、怎麼跑、有哪些工具的實作」 | 路由/指引層:「LLM 何時該用、用哪個工具、照什麼工作流」 |
| 內容 | `notebooklm_mcp/`、`tests/`、`pyproject`、`scripts/make_cover.py`、`AGENTS.md`、`docs/` | `SKILL.md` + `references/`(tool-reference、troubleshooting、episodic_prompts、series_example)+ path-free `.mcp.json` 範本。**無程式碼** |
| 裝法 | `uv tool install git+URL` → `notebooklm-mcp` 上 PATH | 跟其他 skill 一樣進 `audi-skill` 的部署流 |
| 擁有的「真相」 | 工具的**行為**(簽章/回傳/錯誤處理) | 工具的**用法**(工作流/時機/提示詞) |

原則:skill **自足**(references 全跟著走,LLM 讀 skill 不用跳 repo);MCP repo 只管程式。兩者是「一組」——skill 的 `.mcp.json` 用零路徑命令 `notebooklm-mcp`,前提是 MCP repo 已 `uv tool install`。配置串接由使用者自理;本設計只保證兩邊內容乾淨、定位不混。

### §2 發布 / 安裝

- MCP repo → 獨立 GitHub repo,`uv tool install "git+https://github.com/audichuang/notebooklm-mcp"`;更新 `uv tool upgrade`。
- console scripts:
  - `notebooklm-mcp`(server,已有)
  - `notebooklm-cover`(新增:把 `scripts/make_cover.py` 接成 entry point,裝 MCP 時一起上 PATH,SKILL 就叫 `notebooklm-cover --manifest ...`)
- 每處 `.mcp.json` 統一為零路徑:
  ```json
  {"mcpServers":{"notebooklm":{"command":"doppler","args":["run","-p","notebooklm","-c","dev","--","notebooklm-mcp","--transport","stdio"]}}}
  ```

### §3 遷移

1. **MCP repo**(沿用現有 `audiskill/notebooklm-skill`,**保 git 歷史**):
   - `mv` 資料夾 → `audiskill/notebooklm-mcp`(整個目錄搬,`.git` 隨行,歷史/追蹤不動)
   - `git rm` `SKILL.md` + `references/`(搬去 skill)
   - `pyproject` `name` → `notebooklm-mcp`;新增 `notebooklm-cover` entry point
   - 加 remote `github.com/audichuang/notebooklm-mcp`,push
2. **skill**(`audi-skill/notebooklm` 瘦成 docs-only):
   - 移除 `notebooklm_mcp/`、`tests/`、`pyproject`、`uv.lock`、`scripts/`、`.venv`(**這份程式碼副本從此消失 → 飄移結構性根除**)
   - 保留/成為 `SKILL.md` + `references/` + path-free `.mcp.json` 範本
   - commit + push `audi-skill`
3. **消費端**(3 VM、podcast-lab):`uv tool install git+URL` 一次;`.mcp.json` 改零路徑;`/mcp` 重連。

### §4 跨 repo 同步耦合 → 寫進 AGENTS.md

唯一的配置耦合點:**skill 的 `references/tool-reference.md` + `SKILL.md` 工具表,記的是 MCP repo 的工具**。MCP 工具改了(新增/改簽章/改回傳),這兩處要跟著改。

交付項:把這條 checklist 寫進 **MCP repo 的 `AGENTS.md`**(以後 AI 進來會注意),文字大意:
> 改動 MCP 工具時,同步更新 skill(`audi-skill/notebooklm`)的 `SKILL.md` 工具表 + `references/tool-reference.md`。兩 repo 是一組配置。

## 驗收

1. **MCP repo**:`uv run pytest` 仍 138 passed;`uv tool install git+URL` 後 `notebooklm-mcp --transport stdio` 起得來(smoke,無 traceback);`notebooklm-cover --manifest <sample>` 生得出圖並過 `validate_artwork`。
2. **skill**:只有 `SKILL.md` + `references/` + path-free `.mcp.json`,**無任何 `.py`**。
3. **一個消費端**(podcast-lab 或一台 VM):`.mcp.json` 改零路徑、`/mcp` 重連、工具出現且可呼叫。

## 安全 / 回滾

- 資料夾 rename 前先確認工作區乾淨(或先 commit/stash);`mv` 後 `git status` 應無變化(歷史/追蹤不動)。
- skill 側刪程式碼前,先確認 MCP repo 已 push 且 `uv tool install` 起得來,再刪(留退路)。
- 舊 `.mcp.json`(帶絕對路徑)先留著,新命令驗證能連上再改。

## 非目標(YAGNI)

- 不發 PyPI(git install 夠用;要再說)。
- 不動 skill 的部署 / symlink 機制(使用者自理配置)。
- 不改 auth(仍走 Doppler `NOTEBOOKLM_AUTH_JSON`)。
