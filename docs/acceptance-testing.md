# 真實驗收:怎麼搭一個「使用者實際會遇到的環境」

離線測試(`uv run pytest`)全綠**證明不了這個 MCP 會動**。它用 mock client,證明得了分類邏輯,
證明不了:配額真的被拒時 SDK 丟什麼、ASR 把逐字稿磨成什麼樣、五條救援路徑是不是其實都走不通。

**v0.7.1 那輪驗收抓到三個 bug(含一個死鎖),全部只在「真的撞到配額拒絕」時才出現** ——
離線測試在結構上不可能找到它們。所以每個 release 都要跑一次。

這份寫的是**怎麼把環境搭出來**。另外兩半各有正本,別在這裡重抄:

- **帳號隔離**(Doppler `stg`、salt 為什麼要不同、登入)→ [test-account.md](test-account.md)
- **驗證紀律**(每個 PASS 要在 ground truth 上看到、每個 FAIL 先排除 harness artifact)
  → `e2e-verify` skill

---

## 核心想法

**不要在 repo 裡「測試 MCP」,要搭一個跟使用者一模一樣的專案目錄,然後開一個新 session 進去用它。**

差別在於:repo 裡你知道太多。你知道哪支工具該傳什麼、知道 manifest 長怎樣。使用者(以及未來那個
agent)只有 skill 文件 + 工具回傳的訊息。**驗收要驗的正是那條資訊鏈** —— 死鎖之所以是死鎖,
就是因為 guard 訊息叫人去 reconcile,而 reconcile 是死的。

---

## 分工線:哪些事只有真帳號測得到

**契約與狀態機用單元測試守,語意與計費用真帳號抽驗。** 每次驗收完都該問一次
「這一輪抓到的東西,有沒有哪一件其實寫得成離線測試?」——**寫得成的就要補進 `tests/`**,
否則下一輪還要再花一次真實配額去發現同一件事(v0.8.1 那輪補了四條,見
`tests/test_pool_gaps.py`,而且做過突變驗證才收)。

模擬**永遠**覆蓋不到的只有五類:

1. **失敗的真實形狀** —— 配額拒絕是同步 raise 還是回 `task_id=""`?幾秒?
   (v0.8.0 實測:1.3~1.5 秒的同步 `RateLimitError`,而 AGENTS.md 當時的 gotcha 寫的是
   另一種形狀。**猜錯這件事整個 failover 設計就掛在錯的分支上**。)
2. **計費歸屬** —— 配額算發起者還是 notebook owner。無法從 API 形狀推導。
3. **ACL 語意** —— `rpc_code=7` 什麼時候真的被觸發、`is_owner` 在有共享者時的值。
4. **ASR 用詞** —— 主持人會說「鑰匙」而不是「金鑰」、把「佇列」講成「排隊」。
   探針詞的死活只有真的生一次才知道(踩過三次)。
5. **端到端時間與 timeout 邊界** —— `wait_timeout` 夠不夠、finalize 要多久。

**這五類只有在改到相關子系統時才需要抽驗一次,不必每個 release 全跑。** 反過來說,
只要改動碰到 pool / dispatch / 認證 / 發布,就一定要跑 —— 那四個地方的失敗模式全都
在上面這張表裡。

---

## 工作區長什麼樣

```
nblm-acceptance-vX.Y.Z/
├── CLAUDE.md                     ← 工作區政策(帳號 / 發布 / 登入的鐵律)
├── README.md                     ← 測試計畫:分階段 + 每階段驗什麼
├── .mcp.json                     ← 指向**測試帳號**(doppler -c stg)
├── .claude/skills/notebooklm/    ← 上游 skill 的**逐字**快照
├── local-checks.sh               ← 免費離線斷言,先跑這支
├── sources/                      ← 測資
└── output/                       ← manifest / mp3 / 封面落腳處
```

### 兩個不直覺、但關鍵的決定

**① skill 快照必須逐字等於上游 —— 不准為了方便改它。**

```bash
SRC=/home/user/research/audi-skill/notebooklm
cp "$SRC/SKILL.md" "$SRC/.mcp.example.json" .claude/skills/notebooklm/
cp -r "$SRC/references" .claude/skills/notebooklm/
diff -rq "$SRC" .claude/skills/notebooklm --exclude=.mcp.json   # 必須無輸出
```

快照裡的 §Auth 與 troubleshooting 範例寫的是 `-c prd`(**正式帳號 pool**)。看起來很想改成 `stg`,
**但改了就不是在驗使用者拿到的那份 skill 了** —— skill 自己有問題也測不出來。

帳號政策改放**工作區自己的 `CLAUDE.md`**:那是真實專案表達專案級政策的方式,是加法不是修改。
`.mcp.json` 只會從專案根載入,所以快照裡那份 `-c prd` 的 example 不會被吃到。

**② 每次重跑驗收都要重新複製快照。** 它是快照不是連結——寫這份文件時就發現上一輪的快照已經
過時了(skill 後來改過,快照沒跟上)。**先同步再開 session**,否則你驗的是舊文件。

---

## 測資怎麼設計:讓失敗變成可比對的事實

要驗的核心風險是**內容污染**(重生沒指名 `source_ids`、拒收的舊來源沒刪乾淨)。污染用「聽」的
無法證明,用字串比對就是客觀事實。

作法:**兩集,兩個互不重疊的領域**,每集兩份來源。生完之後 mp3 會自我回錄成 source,拿那份
**ASR 逐字稿**做正反對照。建完素材先驗證零交叉:

```bash
for w in 排隊 延遲 尾端 簽章 碰撞 生日; do
  printf "%-6s EP01=%s EP02=%s\n" "$w" \
    "$(cat sources/ep01-*.md | grep -c "$w")" "$(cat sources/ep02-*.md | grep -c "$w")"
done
```

### ⚠️ 對照必須兩段式,順序不能顛倒

1. **先證明探針還活著**:該集回錄至少命中 **2 個自家主題詞**。命中 0 個 = 探針失效,
   該輪判 **inconclusive**,**不可**宣告隔離通過。
2. **再看對方領域的詞有沒有洩進來**(命中 = 隔離失效)。

**為什麼順序是安全邊界**:負向檢查「沒命中對方的詞」在探針全死時會**自動成立** ——
測試空跑卻顯示通過。這個陷阱踩過兩次:

- 第一次用拉丁代號當 canary,`KESTREL` 過 TTS→ASR 變成 "Castrial",`contains=["KESTREL"]`
  永遠 false。
- 第二次改成中文詞,以為解決了 —— 實測**六個詞裡三個沒命中**:`雜湊函數` 被 ASR 寫成
  「雜**函**數」、`憑證鏈` 寫成「**評**證鏈」、`佇列` 被主持人講成「排隊」。

**實測存活**:`排隊` `延遲` `尾端` `吞吐` `負載` `百分位` `利用率` `簽章` `碰撞` `生日`
**實測會被打壞**:`佇列` `雜湊` `憑證` `摘要` `公鑰` `哈希` `證書` `隊列` `序列`

---

## 測試場景怎麼排:按「每次生成換到多少資訊」

**配額是唯一稀缺資源**,而且測試帳號的配額是獨立的一份。照這個順序,做到用完為止:

| 階段 | 配額 | 內容 |
|---|---|---|
| 0 | 0 | `local-checks.sh` —— 純本機斷言。**紅了就別浪費配額** |
| 1 | 0 | 便宜真 RPC:`auth_check` / 建 notebook / 加來源 / `chat_ask`。**`auth_check` 通過 = 線路協定沒壞的第一個證據** |
| 2 | 2 | 主線:`podcast_series` 兩集。驗命名鐵律、feedback source、污染對照 |
| **3** | **1** | ⭐ **retract → `source_delete` → 指名 `source_ids` 重生**。配額只夠做一件事就做這個 |
| 4 | 2+0 | 簡報/講義 → 封面 → `publish_series`(**發布前先問使用者**) |

**Phase 3 資訊量最高**,因為它一次壓到四件事:`published_at` 不能漂(要**同時**看 manifest
與工具回傳值——上一輪就是只驗了 manifest,回傳值錯了才漏掉)、標題不可改、取代版落點、
以及重生內容有沒有被後面集數污染。

### ⭐ 撞到配額不是中斷,是最重要的那個測試

v0.7.0 把配額拒絕從「回傳 failed status」改成「拋例外」。離線測試證明得了分類,
**證明不了真實拒絕長什麼樣**。真撞到時完整記錄並對照:

| 應該是 | 不應該是 |
|---|---|
| `observed_state` = `not_accepted` | `acceptance_unknown` |
| `safe_next_action` = 可直接重試的那支 | `podcast_episode_reconcile` |
| `remote.error` 帶得出拒絕原因 | 只有一句 `"failed"` |

**然後把情境走完,不要只驗那張表** —— v0.7.1 就是這樣挖出死鎖的:兩個呼叫點的分類是否一致、
低階 `generate_audio` 有沒有 fail-loud、續跑是否就地重送同一 attempt、**以及走錯路安不安全**。
最後一項最有價值:死鎖情境下使用者一定會亂試。

### 同一個道理推廣:pool 的帳號狀態是驗收的**輸入**,不是通過條件

上面那條講的是配額,但它其實是一個更一般的東西的特例,而那個一般版被漏寫了很多輪:

**不要把「所有槽位都健康」當成認證那個 Phase 的過關標準。** 一個 5 槽位的 pool 裡有帳號
處於各種狀態(PSIDTS 過期、cookie scope 不對、配額打完、被踢出某本 notebook 的共享名單)
是**常態** —— 整個 failover 設計存在的理由就是它。健康的槽位證明不了 failover;
**不健康的槽位才是免費的判別實驗**,而且是不用燒配額就有的那種。

所以認證 Phase 要做的是:**先記錄每個槽位當下的狀態**,再拿那些狀態當素材,驗
「系統對每一種狀態的反應是不是正確的那一種」。三種狀態走**三條不同的路**,
混在一起就是這個 repo 反覆出事的形狀:

| 狀態 | 應該走 | 絕對不該走 |
|---|---|---|
| **配額耗盡 / 限流** | `_REFUSED_WITHOUT_DISPATCH`(`RateLimitError` / `ArtifactFeatureUnavailableError`)→ rotate 換帳號重送 | 當成永久失敗停下來 |
| **認證失效**(cookie 死) | `probe_auth` / `is_auth_error` → fail-loud 叫人重登 | **不該 rotate** —— 換帳號救不了「整批憑證來自同一個 Doppler snapshot」 |
| **權限不足**(這個帳號看不到那本 notebook) | `NotebookAccessDenied` → 停下來,指引 `notebook_share_with_pool` | **不該進 `_REFUSED_WITHOUT_DISPATCH`**(AGENTS.md 明令那個集合只放配額/限流,不准長大) |

**發現某個槽位不健康時,先記錄、再想能拿它測什麼,最後才考慮要不要修。**
修掉它等於銷毀素材 —— 下一輪要再等它自然壞掉才有得測。

(v0.9.14 的實例:prd 槽位 1 的 PSIDTS scope 在 `.youtube.com`,送不到
`accounts.google.com`。那個槽位一直靠 SID 等其他 cookie 撐,而它正是 0.8.1 下每次啟動
都會觸發 inline heal 的那一列 —— 它是「rotation flock 到底擋不擋得住」最現成的判別實驗,
而第一版驗收計畫卻把它寫成「決定這一項是正向還是 inconclusive 的障礙」。)

---

## 交棒給新 session

工作區備好之後,給使用者一段可直接貼的提示詞。要點只有四個:讀 `CLAUDE.md` 與 `README.md`、
配額順序、撞到配額要完整記錄、**發布前先問**。加上一條驗收紀律:

> 每一項寫下**實際觀察到什麼**,不要寫「應該沒問題」。工具回傳值與 manifest 落地內容是
> **兩個出口**,兩邊都要各自貼出來對照。

發現寫進 `FINDINGS.md`:症狀、重現步驟、根因行號、判斷(bug / 預期行為 / 待確認)、建議。
**不確定是不是 bug 的也要寫並標成待確認**,不要自己吞掉。

---

## 每一版「這輪要驗什麼」的劇本

本檔是**不變的方法論**(環境、分工線、測資設計)。「這一版改了什麼所以要驗什麼」
每版不同,各自一份:

- **[v0.9.0](acceptance-v0.9.0.md)** — 身分從 process 全域改成跟著 client 走。
  帶進**兩個以往劇本沒涵蓋的維度**:①**並行**(以往每輪驗收都是單線跑的,而並行正是
  這一版存在的理由)、②**憑證落檔**的生命週期與 L2 PSIDTS recovery。另含一條離線
  證不了、必須這輪收掉的前提(`get_share_status` 到底列不列 owner)。

## 每個 release 要更新什麼

1. `local-checks.sh` 的版本 pin(`0. 實裝版本` 那節)
2. 為這一版新增的行為加一條離線斷言(例:v0.7.2 加了 F-8 的沿用邊界檢查)
3. **重新複製 skill 快照**(見上面 ②)
4. README 的「這輪要驗什麼」改成這一版真正動到的東西 —— 不要每次都跑一樣的劇本,
   驗收要對準**這次改了什麼**

工作區是拋棄式的:測完刪掉 notebook、整個目錄可刪。發布過的 feed 留著沒關係
(salt 不同、與正式節目不同 URL 空間;uploader 不刪檔)。
