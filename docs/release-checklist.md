# 發版檢查表(cross-repo sync + pin sites)

`AGENTS.md` 原 §Cross-Repo Sync Checklist。**發 tag、改工具名/參數、同步 skill repo** 時讀。

> MCP repo 與 skill repo 是**一組配置**。這份的每一條都對應一次真實踩雷:推序錯會讓 CI 紅、
> pin 漏一處會讓某台機器裝到舊版、tag 之後不驗版本號會被 uv 的 git cache 騙。

---

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
   ⚠️ 那個工作樹**常有進行中的 EP 目錄與 manifest 改動**,只 commit 這一個檔
   (`git commit AGENTS.md -m …`),別 `git add -A`。

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

