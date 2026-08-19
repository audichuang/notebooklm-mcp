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

### Release Pin Sites(發 tag 時只改**一處**版本號)

消費端裝 `@latest`,文件不再寫死 `v0.9.x`。發版要改的版本號只剩:

1. `pyproject.toml` 的 `version`(正本)

`vMAJOR.MINOR.PATCH` tag 是不可變錨點;`latest` 是 CI 維護的移動指針。
推上 `vX.Y.Z` 之後 `.github/workflows/retag-latest.yml` 跑
`scripts/retag-latest.sh`,把 `latest` 指到**最高**的 semver 發版(不是剛推的那個,
所以誤推舊 tag 不會把指針往回拉)。預發版 `v0.9.20-rc1` 不算。

**skill 不是 pin 點。** `audi-skill/notebooklm` 的安裝指令在 `references/setup.md`,
寫的是 `@latest`。打 tag、等 CI 移動指針之後,消費端重跑 setup 那段即跟上。

⚠️ **`../podcast-lab/AGENTS.md` 曾是 pin 點,現在不是,別把它加回來**:那份已改成
「不寫版本號、安裝指令或方法清單」。它現在唯一會出現的版本號在 gitignored 的快照裡,
見下面那條 ⚠️。

**外加一件不算 pin 但一定要做的**:在 `CHANGELOG.md` 開一節寫「改了什麼、為什麼、
踩到什麼事故」。版本敘事只寫在那裡 —— **不要回填進本檔**。

**tag 之前**:CI 綠(它含 wheel 的 `uv tool install` + `--help` 冒煙),
**tag 之後**:

1. 等 `retag-latest` workflow 綠。`git ls-remote origin refs/tags/latest` 的 SHA
   必須等於 `git rev-parse vX.Y.Z^{}`。沒動就 `gh workflow run retag-latest.yml`,
   或本機 `bash scripts/retag-latest.sh --push`。
2. 用 `@latest` 真的裝一次再收工 —— v0.7.0 的撞名就是這一步才發現的。
   **「裝完」要驗版本號,不能只看它印 `Installed 2 executables`**:uv 的 git cache
   對移動 tag 不敏感,`--force` 也可能裝成舊 SHA(v0.8.1 實測是 cache 壞掉裝成舊版)。
   收工前跑
   `~/.local/share/uv/tools/notebooklm-mcp/bin/python -c "import importlib.metadata as m; print(m.version('notebooklm-mcp'))"`;
   數字不對就 `uv cache clean notebooklm-mcp` 再 `--force --reinstall`。

**editable 裝的 tool venv 完全不受這條保護**:`uv tool install -e <repo>` 之後 metadata 版本
與**依賴**都停在安裝當時,而 `uv tool list` 印得一切正常 —— 2026-08-16 實測本機停在
`0.9.12` + `notebooklm-py 0.8.0`,那時 repo 已 `0.9.15`、pin 已是 `>=0.8.1`(**實裝連自己的
pin 都不滿足**,而 editable 的 code 卻是最新的,所以症狀是「跑起來像新版、依賴卻是舊的」)。
本機也算消費端:要跟上就重跑 `@latest` 安裝,別指望 editable 自動生效。

活文件不准再寫死 `git+…@v0.`。驗證:

```
grep -rEn "notebooklm-mcp\.git@v[0-9]" --include="*.md" . ../podcast-lab \
  | grep -vE 'docs/superpowers|\.superpowers|CHANGELOG.md|docs/acceptance-|docs/notebooklm-py'
```

(`docs/superpowers/` 的歷史計畫書刻意不改——那是當時的事實;CHANGELOG / acceptance
是版本敘事,本來就會出現舊 pin)。

⚠️ **那個 grep 會撈到「看起來像 pin、其實不是」的東西,別跟著改**:
`.superpowers/sdd/task-*.md`(v0.2.0)與 `docs/acceptance-*.md` 是歷史紀錄,同
`docs/superpowers/` 的道理。`../podcast-lab/.agents/skills/notebooklm/` 是
**gitignored、可重生**的本機 docs 快照,正本在 `audi-skill/notebooklm/`;
它現在也不寫死版本(見 `references/setup.md`)。要更新是重生快照,不是編輯它。

