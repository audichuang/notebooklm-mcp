# 發版檢查表(cross-repo sync + pin sites)

`AGENTS.md` 原 §Cross-Repo Sync Checklist。**發 tag、改工具名/參數、同步 skill repo** 時讀。

> MCP repo 與 skill repo 是**一組配置**。這份的每一條都對應一次真實踩雷:推序錯會讓 CI 紅、
> pin 漏一處會讓某台機器裝到舊版、tag 之後不驗版本號會被 uv 的 git cache 騙。

---

## Cross-Repo Sync Checklist

MCP repo 與 skill repo 是一組配置。改動 MCP tools 時,同步更新 `/home/user/research/audi-skill/notebooklm/SKILL.md` 的工具表與 `/home/user/research/audi-skill/notebooklm/references/tool-reference.md`。

新增、移除或改名工具時,commit message 要明講 skill repo 是否已同步;若尚未同步,不要 push MCP release tag。

⚠️ **這道 CI 檢查只掛在本 repo 的 push 上,skill repo 那側沒有任何 workflow** —— 所以
**改完 skill repo 要自己回來跑一次** `uv run python scripts/check_skill_sync.py`。
2026-09-05 到 09-13 之間它就是這樣斷了 8 天:破壞來自 skill repo 的 commit,而那八天本 repo
一次 CI 都沒觸發(不是「跑了而且綠」)。一道只在被守的兩個 repo 之一上觸發的守門,對另一側
等於不存在。若本 repo 已落地 CI hard check,PR 必須等該檢查綠燈後才能 tag release。

**推送順序:先推 audi-skill、再推本 repo**(v0.2.9 教訓):CI 的 sync check 會 clone
**遠端** audi-skill 來驗——skill 只同步在本機、還沒推,MCP 先推就 CI 紅(missing 新工具名)。
反序踩到時把 audi-skill 推上去後 `gh run rerun <id>` 即綠,不用改 code。

### Release Pin Sites(發 tag 時只改**一處**版本號)

消費端裝 `@latest`,文件不再寫死 `v0.9.x`。發版要改的版本號只剩:

1. `pyproject.toml` 的 `version`(正本)
2. `uv.lock` 裡本套件的 `version` —— bump 之後跑一次 `uv lock`,**與 pyproject 同一個 release commit**。
   v0.9.23、v0.9.24 都是事後才用 chore commit 補(`uv run` 會自動重同步 lock 並弄髒 working tree)。

🔴 **發版之後才修的東西,不再發一版就等於沒出貨。** 消費端裝的是 **tag**,而
`scripts/retag-latest.sh` 把 `latest` 指到**最高的 semver**——所以修正 push 上 master、
CI 全綠、`latest` 還是紋風不動地指著那個有 bug 的版本。v0.9.27 就是為這件事而發的:
v0.9.26 的修正在 master 躺著,而四台機器裝到的全是壞的。**retag 救不了,只能再發一版。**

`vMAJOR.MINOR.PATCH` tag 是不可變錨點;`latest` 是 CI 維護的移動指針。
推上 `vX.Y.Z` 之後 `.github/workflows/retag-verified-latest.yml` 跑
`scripts/retag-latest.sh`,把 `latest` 指到**已通過 CI 的最高** semver 發版(不是剛推的那個,
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

1. 等 `retag-verified-latest` workflow 綠。**`latest` 是 annotated tag**,`git ls-remote origin
   refs/tags/latest` 印的是 tag 物件的 SHA,永遠對不上 commit —— 要比 peeled 那行:
   `git ls-remote --tags origin | grep 'latest^{}'` 的 SHA 必須等於 `git rev-parse vX.Y.Z^{}`
   (v0.9.24 實際踩到:沒 peel 就誤判成「指針沒動」)。沒動先確認目標 commit 的 CI 綠,
   再 `gh workflow run retag-verified-latest.yml`;腳本的 `--push` 必須同時帶已核對的
   release tag 與 commit,不能不帶參數直接選最高 tag。
2. 用 `@latest` 真的裝一次再收工 —— v0.7.0 的撞名就是這一步才發現的。
   ⚠️ **驗實裝要 `cd` 到中性目錄**(`/tmp` 之類)並印出 `module.__file__` 確認路徑:
   Python 把 cwd 排在 `sys.path` 最前面,在 repo 目錄下跑 tool venv 的 python 會載到
   **repo 副本**,行為看起來全對(v0.9.27 實際差點據此誤判成「已修好」)。
   **「裝完」要驗版本號,不能只看它印 `Installed 2 executables`**:uv 的 git cache
   對移動 tag 不敏感,`--force` 也可能裝成舊 SHA(v0.8.1 實測是 cache 壞掉裝成舊版)。
   收工前跑
   `~/.local/share/uv/tools/notebooklm-mcp/bin/python -c "import importlib.metadata as m; print(m.version('notebooklm-mcp'))"`;
   數字不對就 `uv cache clean notebooklm-mcp` 再 `--force --reinstall`。

⚠️ **「重裝回發布版」不等於回到基線 —— 依賴會往前跑。** pin 是**範圍**(`>=x,<y`),而
`uv tool install git+…` 不讀 `uv.lock`,所以上游發了新版之後重裝 `@latest`,拿到的是
「舊的我方程式碼 + 新的依賴」這個**從沒測過的組合**。v0.9.25-rc 驗收實際踩到:rc 測完把
`nblm-mcp` 還原成 v0.9.24,以為回到原點,實際是 `0.9.24 + notebooklm-py 0.8.2`,而四台機器
跑 `@latest` 全都一樣。**上游發版之後,「發我們的版」就從「讓機器裝得到新功能」變成
「讓機器離開未測組合」** —— 抬 pin 下界正是把它從意外解析變成明確宣告。

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


## 依賴版本對帳:lock vs 消費端實裝(發版時做)

`uv tool install git+…` **不讀 `uv.lock`**,所以 lock 鎖的版本與四台生產機實裝的版本會自己漂開,而且**不是純帳面
差異**:2026-08-09 量到 lock 1.28.1 / 實裝 1.29.0,1.29.0 啟動時多印一行
`pydantic_settings … IncompleteFieldDefinitionWarning: Field 'lifespan' has an incomplete definition`,1.28.1 完全不印
(走 stderr 沒破壞 stdio 協定,但證明實裝版本有 CI 從沒跑過的行為)。2026-08-30 又漂成 1.29.0 / 1.29.1。

```sh
diff <(uv export --frozen --no-dev --no-hashes --no-emit-project --no-header --no-annotate | grep -v '^#' | sed 's/ ;.*//' | sort) \
     <(uv pip compile pyproject.toml --python-version 3.12 --no-header --no-annotate -q | sort)
```

`--no-dev` 排除 dev group、`sed` 去掉環境 marker(`uv pip compile` 兩者都沒有),否則 lock 已對齊也會有雜訊。涵蓋全部依賴(只看 mcp 會漏掉像 filelock 3→4 這種傳遞依賴漂移)。只剩平台 marker(colorama/pywin32)的差異才算對齊;
有差就 `uv lock --upgrade && uv sync && uv run pytest -q`,全綠再 commit uv.lock;deps-watch 的 highest job 每週替你跑這件事。
必要時同步更新 `pyproject.toml` 的下界。**對齊要用 `uv sync`,不是 `uv pip install -e .`** —— 後者不會把
venv 拉到 lock 的版本。

**升 notebooklm-py 之前先跑 `scripts/compare_sdk_surface.py`**:兩版各一個獨立 venv,各跑一次再 `diff`。
上游 CHANGELOG 講的是「他們改了什麼」,不是「我們會不會壞」——0.8.2 那次兩者差很多,而且它挖出一筆
**跟該版本無關**的舊帳(`ReportFormat.CONCEPT_EXPLANATION` 白名單漏了),那只有逐項比對 enum 值才看得到。
用法與四個區塊各防什麼,寫在該檔的 docstring。**新增 SDK 呼叫點時把它加進腳本的 `_CALLS`** ——
那份清單就是「我們的依賴表面」的定義,沒加進去的呼叫,升級時不會被比對到。

surface 腳本只比簽名/enum/欄位,**簽名沒變、語意變了它看不到**。所以還要把兩個獨立 venv 的
`diff -r -x __pycache__ <old>/notebooklm/_web <new>/notebooklm/_web` 逐檔讀過,`_CALLS` 涉及的模組一定要讀
(0.8.4 的 set_users 就是這種,見 CHANGELOG v0.9.29)。

**要對某個特定依賴版本跑測試**(例如試 notebooklm-py 的新版):開一個獨立 venv、用它自己的 `bin/python -m pytest`。
別在共用 venv 上 `uv pip install X==版本` 之後跑 `uv run`(即使帶 `--no-sync`)—— 它可能把 venv 拉回 lock 的版本,
而你以為在測 X。

## ruff 升版

pyproject 的 `ruff==` 與 `.pre-commit-config.yaml` 的 rev 一起改;升完重跑 `ruff check .` 與 `ruff format --check .`;
格式變動要獨立成 commit,並把 SHA 加進 `.git-blame-ignore-revs`。

## deps-watch 紅了怎麼辦

- **pip-audit 紅**:抬 pyproject 的下界或上界(每條附理由)→ `uv lock` → 全套 → 發版。不發版等於沒修。
- **resolution/highest 紅**:消費端下次重裝就會拿到壞組合,收緊上界或修正後發版。
- **resolution/lowest-direct 紅**:下界宣告不實,抬下界。
- **第三方 warning 被 `filterwarnings=error` 升成錯誤**:用精確的 `ignore:<msg regex>:<Category>` 豁免並註明來源,不准全域放行。

## ci.yml 因基礎設施抖動變紅(apt mirror、runner)

ci.yml 有網路相依的 apt 步驟(裝 ffmpeg,已帶 `Acquire::Retries=3`),抖動時 retag 會 exit 1、`latest` 停在舊版
(後果是沒出貨,不是裝到壞版)。救法:`gh run rerun <ci run id>` 到綠 → `gh workflow run retag-verified-latest.yml`。
另注意:pre-commit 的 yaml/toml/私鑰/檔尾 hook 只在本機裝了 hook 時生效,CI 只強制 ruff。
