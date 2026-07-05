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
| 內容 | `notebooklm_mcp/`(含 make_cover CLI)、`tests/`、`pyproject`、`AGENTS.md`、`docs/` | `SKILL.md` + `references/`(tool-reference、troubleshooting、episodic_prompts、series_example)+ tracked `.mcp.example.json`。**無程式碼** |
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
   - **先把 `feat/mcp-read-observability`(領先 master 7 commit)merge 進 master**,讓 default branch 就帶齊本輪工作 + 本 spec(否則新 repo 從 master 建會少掉全部)。
   - `mv` 資料夾 → `audiskill/notebooklm-mcp`(整個目錄搬,`.git` 隨行,歷史/追蹤不動)
   - `git rm` `SKILL.md` + `references/`(搬去 skill)
   - `pyproject`:`name` → `notebooklm-mcp`;**`requires-python` 收緊成 `>=3.12,<3.13`**(本機 `python3` 是 3.14,3.14 會炸 SDK 的 `inspect.signature`)。
   - **`notebooklm-cover`**:把 `scripts/make_cover.py` 的 CLI **移進 package**(如 `notebooklm_mcp/cover_cli.py:main`)再接 entry point——放 `scripts/` 底下 wheel 不會打包、git install 後 import 不到。
   - **remote**:`origin` 目前還是 `github.com/audichuang/notebooklm-skill.git`;要 `git remote set-url origin git@github.com:audichuang/notebooklm-mcp.git`(或新增再設 upstream),push 前 `git remote -v` 確認別推回舊 repo。
2. **skill**(`audi-skill/notebooklm` 瘦成 docs-only):
   - 移除 `notebooklm_mcp/`、`tests/`、`pyproject`、`uv.lock`、`scripts/`、`.venv`(**這份程式碼副本從此消失 → 飄移結構性根除**)
   - 保留/成為 `SKILL.md` + `references/`
   - **path-free `.mcp.json` 範本**:`audi-skill/notebooklm/.mcp.json` 目前**被 `.gitignore` 忽略**,push 不進去 → 改成 tracked 的 **`.mcp.example.json`**(明確無 secret),或調 `.gitignore` 放行該範本。
   - skill 的 `SKILL.md`/`references` 主要是「刪程式碼」;**保留現有檔與 history**,別刪掉重加(會覆蓋 skill-only 調整)。commit message 記明與 MCP repo 對應。
   - `sync-auth.sh` / 登入 extra 屬 MCP repo;skill 移除 `scripts/` 後,SKILL/troubleshooting 提到 `sync-auth.sh`、`notebooklm-cover` 的地方要改成「在 MCP repo checkout 執行」或指向 console script,別留死指令。
3. **消費端**(3 VM、podcast-lab):**逐一** runbook,**先裝新、驗證能連、最後才刪舊碼**:
   - podcast-lab 有**兩個** config 指向舊路徑:`.mcp.json`(Claude Code)**和 `.codex/config.toml`(Codex,第 24–28 行)**——兩個都要切。
   - 每處:`uv tool install --python 3.12 git+<ssh 或 https,對齊該機認證> notebooklm-mcp`(private repo:先 `git ls-remote` smoke 確認裝得到)→ 改 config 成零路徑 `notebooklm-mcp` → `/mcp` 重連驗證工具出現 → 才動下一台 / 才刪 skill 側舊碼。

### §4 跨 repo 同步耦合 → 寫進 AGENTS.md

唯一的配置耦合點:**skill 的 `references/tool-reference.md` + `SKILL.md` 工具表,記的是 MCP repo 的工具**。MCP 工具改了(新增/改簽章/改回傳),這兩處要跟著改。

交付項:把這條 checklist 寫進 **MCP repo 的 `AGENTS.md`**(以後 AI 進來會注意),文字大意:
> 改動 MCP 工具時,同步更新 skill(`audi-skill/notebooklm`)的 `SKILL.md` 工具表 + `references/tool-reference.md`。兩 repo 是一組配置。

## 驗收

1. **MCP repo**:`uv run pytest` 仍 138 passed;`git ls-remote` 從一台 VM 拉得到(private 認證 OK);`uv tool install --python 3.12 git+URL` 後 `python` 版本落在 3.12、`notebooklm-mcp --transport stdio` 起得來(smoke,無 traceback);`notebooklm-cover --manifest <sample>` 生得出圖並過 `validate_artwork`。
2. **skill**:只有 `SKILL.md` + `references/` + tracked `.mcp.example.json`,**無任何 `.py`**;`grep` 不到殘留的 `notebooklm_mcp`/`pytest` 檔。
3. **每個消費端**(3 VM + podcast-lab 的 `.mcp.json` 與 `.codex/config.toml`):改零路徑、重連、工具出現且可呼叫,才視為切換完成。
4. **(建議)新 repo CI**:Python 3.12 job 跑 pytest + build wheel + `uv tool install` smoke,擋掉 package/entry-point/版本問題。
5. **(建議)跨 repo 同步硬檢查**:從 MCP 匯出工具名/簽章,對照 skill `references/tool-reference.md`,PR/CI fail-fast——比 AGENTS.md 一句話更防漏(見 §4)。

## 版本策略

- 生產機 **pin 版本**(git tag / SHA):`uv tool install ... notebooklm-mcp@vX.Y.Z`,**不追 main**(追 main 會把未驗證 commit 直接推上 MCP server)。
- rollout:先 canary 一台 VM 驗證,再逐台 `uv tool upgrade`;rollback = 重裝上一個 known-good tag。

## 安全 / 回滾

- 資料夾 rename 前先確認工作區乾淨(或先 commit/stash);`mv` 後 `git status` 應無變化(歷史/追蹤不動)。
- skill 側刪程式碼前,先確認 MCP repo 已 push 且**每台消費端** `uv tool install` + 重連都驗證過,再刪(留退路)。
- 舊 config(`.mcp.json` + `.codex/config.toml`,帶絕對路徑)先留著,新命令驗證能連上才改;逐台切換期間新舊併存無妨(各自指自己的)。

## 非目標(YAGNI)

- 不發 PyPI(git install 夠用;要再說)。
- 不動 skill 的部署 / symlink 機制(使用者自理配置)。
- 不改 auth(仍走 Doppler `NOTEBOOKLM_AUTH_JSON`)。
