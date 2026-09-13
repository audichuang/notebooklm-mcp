---
name: acceptance-workspace
description: 為一次改動建立「使用者實際會遇到的環境」與驗收計畫,**然後自己 headless 跑完**(不產出叫使用者去貼的 prompt)。Use when the user wants 真實驗收 / acceptance testing / 驗收環境, asks 這次改動要怎麼測, or right after tagging a release that touched pool / dispatch / 認證 / 發布.
---

# 驗收工作區

離線測試用 mock client,結構上證不了「配額真的被拒」「身分真的錯了」那一類事。這個 skill
產出的是一個**拋棄式專案目錄** —— MCP 配置、skill 快照、CLAUDE.md、計畫與本機檢查
—— 讓使用者開一個新 session 就站在使用者實際會遇到的環境裡。

**要驗的是兩件事,不是一件**:MCP 工具本身,**以及 agent 讀 skill 去用 MCP 那條路**。
後者是獨立 project 存在的理由 —— 真正的呼叫端讀的是 skill 文件,不是原始碼,而那條路
只有在裝好 skill 的環境裡才走得到。v0.9.0 那輪唯一的功能性 FAIL 正是這種:`podcast_series`
的停點指引呼叫端跑某支工具,而那支工具在該狀態下自己也 permission denied —— **從 MCP 端看,
回傳值完全正確**;只有照著指引做才會撞上死路。

**產出是環境與計畫,不是驗收本身。** 跑的是使用者開的新 session。

## 五個判準

貫穿每一步,先讀:

**影響半徑** — 從「這次動了什麼」推導「所以哪些東西可能壞」,而不是從「跑幾個代表性流程」
或「省幾次配額」推導。動到共用基礎設施(runtime、認證、協定層、被多處共用的 helper)時,
半徑涵蓋**一行都沒改的工具** —— 它們全都踩在那塊地基上。v0.9.0 那輪從配額預算推導,漏掉
「runtime 換了身分機制 ⇒ 46 個 `get_client()` 呼叫點全在半徑內」與「mcp 換版 ⇒ 協定層」
兩整塊。

**判別實驗** — 讓證據能區分新舊行為,而不只是顯示「沒壞」。造一個**舊版必敗**的情境:
v0.9.0 那輪最有價值的一項是同一個 process 兩個不同 storage_state 檔各建一個 client,
把其中一個帳號移出共享名單 → 它下載失敗、另一個成功;再把 process 全域憑證換成沒權限那份,
用有權限的 path 建的 client 仍成功。舊機制在這裡必敗,而且不燒配額。

**覆蓋自檢** — 用機械比對確認計畫涵蓋每一支工具,而不是靠讀過一遍的印象。v0.9.0 的計畫
第一版漏了 10 支,其中三支是斷線續跑的核心,而那一版正好改了 attempt 狀態機周邊。

**照著做** — 遇到文件或工具訊息給的指引,**執行它**,而不是讀完判斷它看起來對不對。
FAIL-1 的停點訊息措辭精準、工具名正確、被拒的帳號也寫出來了 —— 讀起來完美,跑下去是死路。
skill 文件的每一條「然後跑 X」都是一次可執行的斷言。

**inconclusive** — 判斷不了就標 inconclusive。負向檢查在探針全死時會自動成立,
所以「沒看到問題」與「證明沒問題」是兩件事。

## 步驟

### 1. 判斷這次要 full 還是 targeted

讀 `git diff <上一個 tag>..HEAD` 與 CHANGELOG，列出每個改動面的**影響半徑**。

- 動到共用基礎設施、認證、依賴大版跳躍 → **full**
- 只修了上一輪抓到的 FAIL、且改動面被離線測試涵蓋 → **targeted**(只驗那個修正
  是否真的解掉原始問題,其餘結論沿用上一輪)

**完成判準**:每個改動面都對應到一個 Phase，或有一句寫明為什麼不必測。

### 2. 建工作區

`template/` 是這個 skill 帶的骨架,直接複製再換佔位符:

```bash
SKILL_DIR=<本 skill 的目錄>            # .claude/skills/acceptance-workspace
WS=/home/user/research/audiskill/nblm-acceptance-<版本>
cp -r "$SKILL_DIR/template" "$WS"

# 佔位符：__VERSION__(v0.9.1)、__VERSION_BARE__(0.9.1)、
#         __TOOL_COUNT__(當下工具數)、__WORKSPACE_PATH__
grep -rl '__VERSION__\|__VERSION_BARE__\|__TOOL_COUNT__\|__WORKSPACE_PATH__' "$WS" \
  | xargs sed -i "s|__VERSION__|v0.9.1|g; s|__VERSION_BARE__|0.9.1|g; \
                  s|__TOOL_COUNT__|35|g; s|__WORKSPACE_PATH__|$WS|g"

# skill 快照（範本刻意不含，必須從上游現拉；git 不追蹤空目錄，所以自己 mkdir）
UP=/home/user/research/audi-skill/notebooklm
mkdir -p "$WS/.claude/skills/notebooklm" "$WS/sources" "$WS/output"
cp -r "$UP/SKILL.md" "$UP/references" "$WS/.claude/skills/notebooklm/"
cp "$UP/.mcp.example.json" "$WS/.claude/skills/notebooklm/" 2>/dev/null || true
```

範本裡 `CLAUDE.md` 的鐵律與 `local-checks.sh` 的 §0–§7 是跨版本不變的回歸防護;
`README.md` 是**帶提示的骨架**,風險推導表與 Phase 每版重寫 —— 那正是這個 skill 的重點,
不該被範本填死。

skill 快照**逐字複製、保持與上游一致** —— 驗收的對象就是使用者實際拿到的那份 skill，
動了它就測不出 skill 自己的問題,而 skill × MCP 的搭配正是這輪要驗的兩件事之一。
快照的 §Auth 寫的是正式帳號 config，帳號政策由工作區的 CLAUDE.md 宣告(加法,不是修改)。
**複製時機在 skill repo 推完之後** —— 兩者是一組配置,快照落後就等於驗了舊文件。

**完成判準**:`diff -rq <上游 skill> "$WS/.claude/skills/notebooklm" --exclude=.mcp.json`
無輸出,且工作區內 `grep -r '__VERSION__\|__TOOL_COUNT__'` 無殘留佔位符。

### 3. 補 local-checks.sh 的 §8

範本的 §0–§7 是跨版本回歸防護,**§8 刻意留空** —— 沒有它,這支就只是在證明舊功能還在,
證不了新改動是對的。

用 fake 排出真實情境的**形狀**,斷言新舊行為的**差別**(判別實驗),而不是斷言「函式存在」。
範本的 §8 留了 v0.9.1 的真實例子供仿寫。

**完成判準**:實跑一次全綠，且 §8 至少有一條斷言在**改動前的程式碼上會紅**。

### 4. 寫 README 的分階段計畫

Phase 順序照「壞了會讓後面都不可信」排:協定層 → 認證層 → 讀取面回歸 → 主流程 →
狀態機全入口 → 這一版的核心改動 → skill × MCP 搭配。

每個 Phase 寫**要對帳的事實**(manifest 欄位值、檔案權限、實際錯誤訊息、工具回傳的 JSON)，
不是「確認正常」。

**skill × MCP 那個 Phase 用「照著做」寫,不是「讀過覺得對」**:挑出這一版改動觸及的每一條
skill 指引 —— 錯誤訊息裡的「請跑 X」、文件裡的續跑步驟、新參數的用法說明 —— 各製造一次
它描述的狀態，然後**照著執行**，看解不解得開。

**完成判準**:跑一次**覆蓋自檢** —— 逐一比對工具清單與 README，未點名的補進計畫，
或在計畫裡寫明「刻意不測 + 理由」。

```bash
for t in $(uv run --project ../notebooklm-mcp python -c "
import asyncio,sys; sys.path.insert(0,'../notebooklm-mcp')
from notebooklm_mcp import app
print(' '.join(sorted(x.name for x in asyncio.run(app.mcp.list_tools()))))"); do
  grep -q "\`$t\`" README.md || echo "未點名: $t"
done
```

### 5. 自己跑完,不要交 prompt

🔴 **不要把啟動 prompt 貼給使用者叫他開 session。** 那個做法已經退役(理由見
`docs/acceptance-testing.md` 與 `AGENTS.md` §Conventions):headless 每題約 $0.2,跑得起
n 次取平均與盲評,而交 prompt 只跑得到一次、拿不到數字、改一次就要再麻煩人一次。

```bash
cd <workspace> && doppler run -p notebooklm -c dev -- \
  claude -p "<驗收 brief>" --model sonnet --output-format json --dangerously-skip-permissions
```

brief 要含:這一輪的 scope 與**不做什麼**、結果寫進 `FINDINGS.md` 記可觀測事實、
判斷不了標 **inconclusive**。

**跑完自己清測試產物。只有不可逆的才停下來問使用者**:`publish_series`(會永久推 blob
到公網)、刪既有 notebook/source、動到別人已訂閱得到的東西。

**完成判準**:驗收已經跑完、結果已回報、測試產物已清乾淨 —— 而不是「使用者拿得到一段
可以開跑的 prompt」。

## 收工(驗收跑完之後)

驗收 session 交回結果時:

1. **抓到的東西凡是寫得成離線測試的一律補進 `tests/`,收之前先做突變驗證**
   (把修正改回舊行為，確認測試真的會紅) —— 否則下一輪要再燒一次配額發現同一件事。
2. 未結案的前提原樣記進程式碼或 AGENTS.md 並標明結案方式，讓下一個人知道它還沒結案。
3. FINDINGS 收進 `docs/acceptance-v<版本>-findings.md`，劇本加「已執行」標記指向它。
4. 修正若**碰到 runtime code**，發一個新 patch 版並照 AGENTS.md §Release Pin Sites 改六處。

## 這個 repo 的既有材料

- 環境怎麼搭、測資怎麼設計、「哪五類事只有真帳號測得到」的分工線 —— `docs/acceptance-testing.md`
- 上一輪的完整計畫與結果 —— `docs/acceptance-v0.9.0.md` 與 `docs/acceptance-v0.9.0-findings.md`
- 探針詞的死活(ASR 會打壞哪些中文詞、正反對照為什麼順序不能顛倒)—— `docs/acceptance-testing.md`
