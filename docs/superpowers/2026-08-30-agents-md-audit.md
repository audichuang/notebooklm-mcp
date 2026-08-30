# notebooklm-mcp context 檔審計 + session 收割(2026-08-30)

依 `agents-md-architecture` C(審計)+ D(對話收割)跑。**本檔只是報告,沒有改任何 context 檔**——修正要過訪談 gate,待決問題在最後。

## 範圍

**待審 corpus**:`AGENTS.md`(220 行)、`CLAUDE.md`(1 行 `@AGENTS.md`,import 正確)、repo 內 skill
`.claude/skills/acceptance-workspace`(自動觸發,是去重來源不是待審檔)。無 GEMINI.md、無 `.claude/rules/`。

**生效鏈與去重對照**:工作根 `../AGENTS.md`(61 行:`_research/` 唯讀、commit/push/deploy 與外部帳號寫入要先問)、
使用者層 `~/.claude/CLAUDE.md`(CodeGraph 用法)、auto memory `MEMORY.md`、MCP `notebooklm` 開場注入
(auth_check 先跑、streamable-http 無認證)、skill `audi-skill/notebooklm/SKILL.md`(390 行)+ 7 份 references、
`docs/`(6 份 gotchas 601 行、design-notes、auth-and-config、release-checklist、mcp-setup、notebooklm-py-0.8-upgrade、13 支 ADR)。

**事實驗證**:AGENTS.md 裡每個具名符號、路徑、pin 都對過 code/pyproject/docs(清單見「驗證為正確」)。

## 總評

**C+**。事實正確性很高(23 個具名符號全部存在、3 個 pin 與 pyproject 一致、所有引用的 docs/ADR/測試都在),
權限邊界由工作根涵蓋、無 secrets、CLAUDE.md wrapper 正確。問題集中在**一種形狀**:220 行裡約 90 行是
「只在動到某一塊時才需要的機制解釋」,而它們**沒有下層可接**——`docs/gotchas-sdk.md` 完全沒有 AGENTS 那五條
SDK 行為(render 丟 block、`source_ids` 快照、CJK 空格、引用標記、probe 假陽性),`gotchas-attempt` 沒有
`source_delete` 清理語意,`gotchas-publish` 沒有附件重入債。也就是:路由表(L180-189)設計對了,內容卻還留在
每次都載入的那層。另有 3 處同檔重複(naming、failover、pool 各寫兩次)與 1 處 Commands 自相矛盾。

## 發現

### HIGH

無「照做會出事」的事實錯。

### MEDIUM(誤導、重複、該降層)

- **M1 `AGENTS.md:12-13` vs `:99`,自相矛盾** —— Commands 教 `uv venv --python 3.12 && uv pip install -e ".[dev]"`,
  Gotchas 說「對齊要用 `uv sync --extra dev`,不是 `uv pip install -e .`」。前者正是 L17-18「fresh venv 首次 uv run 撞
  re-sync churn」與 mcp 版本漂移的機制來源。**修**:Commands 改 `uv sync --extra dev`(pyproject `requires-python
  ">=3.12,<3.13"` 讓 uv 自己選 3.12,今天實跑過);L17-18 churn 註解隨之刪;L12 的「3.14 bug」說明刪(pin 已強制)。
- **M2 `:36-39` 版本綁定的 workaround** —— 「CLI 2.1.201 的 `claude mcp add … --` 會把整串當 prompt,改用 add-json」
  是閘 3(保鮮)問題,且 `docs/mcp-setup.md` 已含 add-json 指令(2 處)。**修**:縮成一行「註冊進 Claude Code 見
  docs/mcp-setup.md」。
- **M3 `:89-99` mcp pin gotcha 11 行** —— 規則(上界、定期對帳 lock vs 實裝、對齊用 uv sync)值得留;
  「2026-08-09 又漂成…、1.29.0 多印一行 pydantic_settings warning」是類 5 變更日誌。今天又漂一次
  (1.29.0→1.29.1,`69fcdd1`),證明它是**發版時**的固定步驟 → 閘 2 降層到 `docs/release-checklist.md`。
  **修**:AGENTS 留 2 行(為何要上界 + 對帳步驤在 checklist),查法/對齊指令/歷史移到 checklist。
- **M4 `:100-105` 撞名 6 行** —— pyproject `[project.scripts]` 註解、`docs/notebooklm-py-0.8-upgrade.md` §撞名、
  release-checklist 步驟 2 三處都有。**修**:留 1 行(命令叫 `nblm-mcp`、別裝 `notebooklm-py[mcp]`、開發用
  `uv run python -m notebooklm_mcp.server`),指向 0.8-upgrade doc。
- **M5 `:110-114` pool 段 = 第三個指向** —— 表格 L63 與路由表 L186 已各指一次 gotchas-pool;這段是 gotchas-pool
  的摘要(閘 1「指向,別摘要」)。**修**:刪,把「破功不會當場出錯」那一句併進 L63 的 row。
- **M6 `:146-149` naming 段重複 `:67`** —— 同檔他段,內容一致。**修**:刪段,row 已夠。
- **M7 `:168-178` failover 段重複 `:69` + `_failover.py` docstring + ADR-0011 末段** —— 五條紅線正本在 docstring
  (驗過:5 條),「別把三個 callback 併成一個」在 ADR-0011。**修**:刪段,row L69 加半句「三個 callback 別併,ADR-0011 末段」。
- **M8 `:115-128, 150-154` 五條 SDK 行為(20 行)沒有下層** —— 是 `notebooklm-py` 的行為與我們的緩解,只在動
  `chat_ask` / `episode_set_description` / `source_ids` 時要讀;`docs/gotchas-sdk.md` **一條都沒有**。
  **修**:整段搬進 gotchas-sdk.md(它本來就是「notebooklm-py 的 API 契約」),AGENTS 留 2 行 invariant:
  「文字清理一律走 `_text`(`strip_inline_emphasis` / `_CITATION_RE` / `norm`),新程式碼別自己寫 regex」、
  「`Artifact.source_ids` 是生成當下快照,不可反查現存 source」。
  另修一處精度(類 6):L152「工具已內建 `strip_citations`」—— 它是**參數**,`chat_ask` 預設 `False`
  (`tools_basic.py:785`)、`episode_set_description` 預設 `True`;寫成「已內建」會讓人以為自動清。
- **M9 `:131-145` source_delete 15 行沒有下層** —— 「查無 id 不發 RPC、不 raise 以便清理迴圈重放、刪後
  `source_fulltext` 仍讀得到是預期、三個前提由 test_contracts 釘住」是 retract 清理義務的一部分,
  `gotchas-attempt.md` 沒有。**修**:搬進 gotchas-attempt(清理義務節),AGENTS 留 2 行。
- **M10 `:155-167` 附件 preflight + 重入債 13 行沒有下層** —— `gotchas-publish.md` 完全沒提 `get_or_none`
  preflight 與 slides/report 重入。**修**:搬進 gotchas-publish 附件節,AGENTS 留 2 行(「遠端 mutation 前用
  `artifacts.get_or_none` 便宜 preflight;重入是家族共有債,修法是 attachment attempt 不是單支拆 kickoff/finalize,ADR-0011」)。
- **M11 `:207-220` agent 分層 14 行** —— 閘 2(只在派 agent 做大修時)+ 類 10(寫死 `Sonnet`/`Opus`,現用 Fable 5,
  模型名必漂)。四條紀律各有事故背書,是金礦,但敘事佔 10 行。**修**:搬 `docs/agent-review-playbook.md`
  (角色名寫「實作者 / 獨立審查者 / 主迴圈裁決」,事故敘事保留),AGENTS 留 2 行。
  ⚠️ `MEMORY.md` 記載這段是**刻意**從 memory 固化進 AGENTS 的——搬不搬要問。
- **M12 `:193-200` 「每任務一 commit」8 行** —— 規則對、事故真(AI 真的犯過,反向檢查 1 → 留),但機制解釋
  (`-U0` 沒 context 行、行號偏移)5 行。**修**:壓成 3 行:規則 + 「別用 `git apply --unidiff-zero` 事後切,實測把
  helper 插進別的函式;`uv run pytest` 跑的是 working tree 所以照樣綠」。

### LOW

- **L1 `:80`** 「判斷只在大綱前置點」—— skill SKILL.md L86 + ADR-0007 已有,且是 host 流程不是本 repo 規則 → 刪。
- **L2 `:109`、`:206`** `_research/` 唯讀 —— 工作根 AGENTS L43/L52 已講兩次 → 兩處刪(L106-108 情報站指向保留)。
- **L3 `:71`** `publish/state.py` row 漏記 v0.9.24 的 `assert_not_retired` → 改「發布層 manifest 欄位閘的正本
  (`publication_state` 白名單、`retired`)」。
- **L4 `:78`** `scripts/` row 列 5 支腳本名(ls 就有)→ 只留意義:「backfill/reorder 是 `published_at` 一次性修復,
  dry-run 預設、走 ManifestStore」。
- **L5 `:19-25`** 「別同時跑兩個 uv run pytest」規則留;「要對特定版本跑測試開獨立 venv」→ 併進 checklist 的對帳步驟。
- **L6 `:50-53`** salt / `--raw` 兩條與 `docs/auth-and-config.md:55,58` 逐字重複,但兩條都是不可逆事故
  (訂閱者掉光 / 靜默吃掉 `$`)→ 反向檢查 2 留,刪括號內的機制解釋,各留一行。

### 驗證為正確的斷言(不用動)

`requires-python >=3.12,<3.13`、`notebooklm-py>=0.8.1,<0.9`、`mcp[cli]>=1.27,<2`、`nblm-mcp`/`notebooklm-cover`
兩支 script;`naming.episode_label` 與 `tools_podcast._episode_label`;`_text.norm` / `strip_inline_emphasis` /
`_CITATION_RE`(在 `_text.py:13`);`tools_basic._assert_no_dropped_blocks`;`_attempt_capabilities` /
`_attempt_next_step`;`artifacts.get_or_none` 三處呼叫(revise/retry/slides);`auth_probe.probe_auth`、`auth_check`;
`tools_podcast._dispatch_audio_with_failover:1404`;`_failover.py` docstring 五條紅線;
`tests/test_contracts.py:780 test_generation_takes_its_source_list_from_the_notebook_not_the_server`;
`_research/notebooklm-mcp-cli` 存在;`docs/superpowers/notebooklm-mcp-findings.md`(139 行)存在;
`docs/mcp-setup.md` 含 add-json;13 支 ADR 檔名與引用一致;CLAUDE.md 是真 `@import`。

## 十維度結論

事實正確性 ✅ 無 stale;**分層不重複** ❌ 3 處同檔重複 + 2 處抄工作根 + 1 處抄 skill;結構樹 ✅(表格是角色導覽);
**漏記** ⚠️ 1 處(retired);噪音 ⚠️ 1 處日期敘事(M3);**誤導** ⚠️ 2 處(M1 自相矛盾、M8 strip_citations);
secrets ✅;權限邊界 ✅(工作根涵蓋,本檔正確地不重抄);多檔同步 ✅;**模型世代** ⚠️ M11 寫死模型名。

## 逐行減法(區塊級)

原始 220 行 → **留約 100 / 降層 docs 約 75 / 刪約 45**(空行與標題計入留)。

| 行 | 內容 | 出局在 | 結局 |
|---|---|---|---|
| 1-7 | 定位 + 指向 | — | 留(壓到 5 行) |
| 12-18 | venv + churn 註解 | 閘 4(自相矛盾) | 改 `uv sync --extra dev`,churn 刪 |
| 19-25 | 別並行 pytest / 特定版本測法 | 閘 2 | 留 2 行;版本測法 → release-checklist |
| 27-34 | 消費端安裝 / 跑 server / 無認證紅線 | — | 留 |
| 36-39 | claude mcp add-json | 閘 1+3 | 1 行指向 mcp-setup.md |
| 43-53 | 認證與發版表 + 兩紅線 | 閘 1(但反向 2) | 表留;紅線各留 1 行 |
| 55-59 | codegraph + design-notes 指向 | — | 留 2 行 |
| 61-78 | 模組表 | — | 留;L71 補 retired、L78 去腳本名、L63/L69 各吸半句 |
| 80 | 判斷只在大綱 | 閘 1(skill) | 刪 |
| 82-85 | 文件分工 | — | 留 |
| 87-99 | mcp pin | 閘 2+類 5 | 留 2 行;其餘 → release-checklist |
| 100-105 | 撞名 | 閘 1 | 留 1 行 |
| 106-109 | 情報站 / `_research` 唯讀 | 閘 1(後半) | 留 2 行,唯讀刪 |
| 110-114 | pool 摘要 | 閘 1(同檔第三指向) | 刪 |
| 115-128 | render / source_ids | 閘 2 | → gotchas-sdk;留 2 行 invariant |
| 129-130 | probe_auth | 閘 2 | → gotchas-sdk |
| 131-145 | source_delete 語意 | 閘 2 | → gotchas-attempt;留 2 行 |
| 146-149 | naming | 閘 1(同檔) | 刪 |
| 150-154 | CJK / citations | 閘 2 | 併進 `_text` invariant 那行 |
| 155-167 | 附件 preflight / 重入債 | 閘 2 | → gotchas-publish;留 2 行 |
| 168-178 | failover | 閘 1(同檔+docstring+ADR) | 刪,L69 吸半句 |
| 180-189 | 按需 gotchas 路由表 | — | 留 |
| 193-200 | TDD / 一任務一 commit | 反向 1 | 留,壓 3 行 |
| 201-205 | commit 訊息 / 真實驗收判準 | — | 留 |
| 206 | `_research` | 閘 1(工作根) | 刪 |
| 207-220 | agent 分層 | 閘 2+類 10 | → docs/agent-review-playbook.md;留 2 行 |

**反向檢查**:留下的每行都有事故背書(salt、`--raw`、無認證 host、並行 pytest、unidiff-zero、`_attempt_capabilities`
七次現形、ADR 砍 scope 38 集)、或是唯一記錄某個跨系統耦合的地方(skill repo 同步、`_research` 情報站、
`@latest` 不追 master)。沒有為了行數刪不變量。

## 最該優先修的 5 點

1. **M1** Commands 自相矛盾——零風險、今天已實跑 `uv sync --extra dev`。
2. **M8/M9/M10** 三段降層到對應 gotchas(共 ~48 行),同時把 gotchas-sdk 從「只有 SDK 契約」補成含行為緩解——路由表才名副其實。
3. **M5/M6/M7** 三處同檔重複刪除(~24 行)。
4. **M3 + D1/D2** mcp 對帳、uv.lock 併進 release-checklist(把今天踩的兩件事一起收)。
5. **M11** agent 分層搬 docs 並去模型名——要先問(MEMORY.md 說是刻意放 AGENTS)。

---

## D. 本 session 收割

**料源**:這場 6 個 commit(`d179783` retired gate、`61789f5` release、`3d9827a` checklist、`69fcdd1` lock;
audi-skill `30e2c13`;podcast-lab `51e7753`)、`/tmp/handoff-…md`、無 auto memory 寫入。前段對話仍可見,無 compact 缺口。

| # | 候選 | 去處 | 理由 |
|---|---|---|---|
| D1 | bump 版本後 `uv lock`,pyproject + uv.lock **同一個 release commit** | ✅ `docs/release-checklist.md` Pin Sites 加一行 | 連兩版(`ba62b63`、本次)都靠事後 chore 補;checklist 現在只寫 pyproject 一處。閘 2:發版時才用 → docs 不是 AGENTS |
| D2 | 發版時對帳 mcp lock vs 消費端實裝(`grep uv.lock` vs tool venv `importlib.metadata`),漂了就 `uv lock --upgrade-package mcp && uv sync --extra dev` + 全套 | ✅ release-checklist(與 M3 合併) | 今天第三次量到漂移;AGENTS L89-99 已有規則但埋在 11 行敘事裡,搬到會被逐條照做的 checklist |
| D3 | `latest` 是 annotated tag,驗指針比 `latest^{}` | 已寫(`3d9827a`) | 丟棄 |
| D4 | 推序:audi-skill 被 non-fast-forward 拒時,MCP push 不能跟著上;兩個 push 用 `&&` 串或分兩步 | 丟棄 | checklist 已有推序 + 「反序 rerun 即綠」;`;` vs `&&` 是通用 shell 紀律,不是 repo 特有雷 |
| D5 | retired gate 的設計取捨(旗標 vs allowlist vs 補 sha) | 已寫(ADR-0013、gotchas-publish、CHANGELOG) | 丟棄 |
| D6 | 標記退役 manifest 走 `ManifestStore.update`,repo `.venv` python 也 import 得到 | 已寫(ADR-0013 §4、skill tool-reference) | 丟棄 |
| D7 | gh 作用中帳號已切回 `audichuang` | 丟棄 | 一次性環境狀態,已寫進 handoff |
| D8 | P1-b legacy sha 接受現狀 | 已寫(ADR-0013「為什麼不做另外兩種」) | 丟棄 |
| D9 | 使用者糾正 | 無 | — |

**寫入提案(核可後)**:僅 D1 + D2,都進 `docs/release-checklist.md`,AGENTS.md 零新增行(M3 反而減行)。

## 待使用者決定(訪談 gate,核可才動 context 檔)

1. **M8/M9/M10 降層**:把 SDK 行為、source_delete 語意、附件重入債搬進對應 gotchas-*.md,AGENTS 各留 1-2 行 invariant?
   ——我推測可以,這正是路由表的設計意圖;風險是 gotchas 檔變長(sdk 53→~75、attempt 169→~185、publish 183→~197),都仍在按需載入層。
2. **M11 agent 分層**:搬 `docs/agent-review-playbook.md` 並改成角色名(不寫 Sonnet/Opus)?——我推測可以,
   但 MEMORY.md 記載這是刻意固化進 AGENTS 的決定,所以要問。替代:留在 AGENTS 但壓到 5 行 + 去模型名。
3. **M1 + M3 + D1 + D2**:Commands 改 `uv sync --extra dev`;mcp 對帳與 uv.lock 步驟收進 release-checklist?——推測可以,零風險。
4. **M5/M6/M7 + LOW 全部**:同檔重複與抄上層的刪除,一次改完逐條回報 diff?——推測可以。

## 已套用(同日,使用者四題全部核可)

- **AGENTS.md 220 → 115 行**(−48%)。每個發現的落點:
  - M1:Commands 改 `uv sync --extra dev`,churn 註解與 3.14 說明刪;版本測法一句指向 checklist。
  - M2:`claude mcp add-json` 四行 → 一句「見 docs/mcp-setup.md」。
  - M3 + D2:mcp pin 留 3 行(上界理由 + 漂移會長回來 + 對帳在 checklist);查法、對齊指令、兩次漂移史、特定版本測法
    → `docs/release-checklist.md` 新節「依賴版本對帳」。
  - M4:撞名 6 行 → 3 行,指向 `docs/notebooklm-py-0.8-upgrade.md` §撞名。
  - M5:pool 摘要段刪,「破功不會當場出錯」併進 `app.py` row。
  - M6:naming 段刪,「鐵律正本在 skill」與 `_episode_label` 別名併進 `naming.py` row。
  - M7:failover 段刪,「三個 callback 別併成一個」併進 `_failover.py` row。
  - M8:五條 SDK 行為 → `docs/gotchas-sdk.md` 新節(53→76 行);AGENTS 留 `_text` invariant(含 `strip_citations`
    預設 False 的精度修正)+ `source_ids` 快照兩條。
  - M9:source_delete 語意 → `docs/gotchas-attempt.md` 新節(169→187);AGENTS 留 3 行。
  - M10:附件 preflight/重入債 → `docs/gotchas-publish.md` 新節(183→198);AGENTS 留 3 行。
  - M11:agent 分層 → 新檔 `docs/agent-review-playbook.md`(27 行,角色名不寫模型名);AGENTS 留 2 行;
    `MEMORY.md` 指針同步改指 playbook。
  - M12:一任務一 commit 壓成 4 行,`unidiff-zero` 事故的完整機制留在 playbook §與 commit 紀律的關係。
  - L1(大綱)、L2(`_research` 兩處)刪;L3 `publish/state.py` row 補 `retired`;L4 `scripts/` row 去腳本名;
    L5 併進 checklist;L6 salt / `--raw` 各留一行。
  - D1:checklist Pin Sites 加第 2 點(uv.lock 同 commit)。
- 順手修 3 條**既有**壞連結(gotchas-sdk / gotchas-attempt 在 `docs/` 內寫 `docs/…` 與 `CHANGELOG.md` 前綴),
  連結檢查 6 檔全通。
- 反向檢查:留下的 115 行每行都有事故背書、是安全/不可逆紅線,或是唯一記錄跨系統耦合的地方;
  路由表 6 個下層檔現在**都接得住**它們指向的內容。
