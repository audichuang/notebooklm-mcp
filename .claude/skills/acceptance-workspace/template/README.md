# notebooklm-mcp __VERSION__ 真實環境驗收

拋棄式測試工作區。**不是**任何專案的正式資料,測完整個刪掉。
帳號 / 命令 / 憑證落檔 / 發布的鐵律在 [CLAUDE.md](CLAUDE.md) —— **開工前先讀那份**。

```bash
cd __WORKSPACE_PATH__
claude
```

`.mcp.json`(測試帳號 pool)與 `.claude/skills/notebooklm/`(上游 skill 的**逐字快照**)
都備好了,開 session 就是完整的真實環境。

---

## 驗收 brief(給 `claude -p`,不是給使用者貼)

<!-- 這段是餵給 headless 的 brief,含:scope、不做什麼、紀律、指向完整劇本。
     跑法:cd <workspace> && doppler run -p notebooklm -c dev -- \
             claude -p "$(sed -n '/^> /,/^$/p' README.md)" --model sonnet \
             --output-format json --dangerously-skip-permissions
     🔴 不要把它貼給使用者叫他開 session —— 見 AGENTS.md §Conventions。 -->

> 這是 notebooklm-mcp **__VERSION__** 的真實環境驗收工作區。先讀 `CLAUDE.md`(鐵律)與
> 本檔的〈風險推導〉,再照 Phase 逐階段執行。
>
> <!-- 一句話說清楚這一版動了什麼、所以驗收的目標是什麼 -->
>
> **紀律**:
> - 每個 Phase 做完就把結果寫進 `FINDINGS.md`(沒有就建),記**可觀測的事實**
>   (manifest 欄位值、檔案權限、實際錯誤訊息、工具回傳的 JSON),不要只寫「通過」。
> - 判斷不了的標 **inconclusive**,不要猜成通過 —— 這比漏測更危險。
> - 遇到文件或工具訊息給的指引,**照著執行它**,不是讀完判斷它看起來對不對。
> - 遇到與文件不符的行為,先確認是不是 skill 快照過期,再判定是 bug。
> - `publish_series` 動到之前**先問我**(它會永久推 blob 上 NAS)。
> - 不用替我省配額 —— **覆蓋完整比省幾次生成重要**。真的撞到配額耗盡反而是好事,
>   照工具給的 `safe_next_action` 續跑即可。
> - 收工前跑〈收工〉的覆蓋自檢,確認每支工具都被碰過;刻意不測的要寫明理由。
>
> 完整劇本在 `../notebooklm-mcp/docs/acceptance-__VERSION__.md`;這一版改了什麼、
> 為什麼那樣改,在 `../notebooklm-mcp/CHANGELOG.md` 的對應節。
>
> 從 Phase 0 開始。

---

## 風險推導:這次改了什麼 → 所以要測什麼

<!-- 這張表是整份計畫的來源。從 git diff 與 CHANGELOG 推導，**不是**從
     「跑幾個代表性流程」或配額預算推導。動到共用基礎設施時，影響半徑涵蓋
     一行都沒改的工具 —— 它們全踩在那塊地基上。 -->

| 改動 | 影響半徑 | 為什麼可能壞 |
|---|---|---|
| | | |

### 「沒改到但被波及」的那一半

<!-- 動到 runtime / 認證 / 協定層時，這一段是必填。列出那些自己沒變、但依賴改動面的工具。 -->

---

## 素材

<!-- 只有要驗內容隔離時才需要。四份來源、兩個互不重疊的領域，生成後拿回錄的 ASR
     逐字稿做正反對照。

     ⚠️ 探針詞的死活要先證明，順序不能顛倒:先確認該集回錄命中 ≥2 個自家主題詞
     (命中 0 個 = 探針失效，該輪判 inconclusive)，再看對方領域的詞有沒有洩進來。
     負向檢查在探針全死時會自動成立 —— 這個陷阱踩過兩次。

     實測存活:排隊 延遲 尾端 吞吐 負載 百分位 簽章 碰撞 生日
     實測會被 ASR 打壞:佇列 雜湊 憑證 摘要 公鑰 哈希 證書 隊列 序列 -->

---

## Phase 0 — 本機檢查(不打 RPC)

```bash
bash local-checks.sh
```

驗的是**實裝環境本身**(從 `nblm-mcp` shim 的 shebang 取直譯器,不是 repo 的 venv)。
先確認版本號印 **__VERSION__**;數字不對就 `uv cache clean notebooklm-mcp` 再
`uv tool install --python 3.12 --force "git+…@__VERSION__"`。

<!-- 若這一版有「壞了就別往下跑」的硬關卡，在這裡點名是哪一條。 -->

---

## Phase 1..N

<!-- 順序照「壞了會讓後面都不可信」排:
     協定層 → 認證層 → 讀取面回歸 → 主流程 → 狀態機全入口 → 這版核心改動 → skill × MCP

     每個 Phase 寫**要對帳的事實**，不是「確認正常」。
     核心改動那幾個 Phase 標 ⭐，並盡量設計成**判別實驗** —— 讓證據能區分新舊行為，
     而不只是顯示「沒壞」(造一個舊版必敗的情境)。 -->

---

## Phase N — skill × MCP 搭配

真正的呼叫端(LLM)讀的是 **skill 文件**,不是原始碼 —— 這條路只有在裝好 skill 的環境裡
才走得到,也是獨立 project 存在的理由。

**用「照著做」寫,不是「讀過覺得對」**:挑出這一版改動觸及的每一條 skill 指引 ——
錯誤訊息裡的「請跑 X」、文件裡的續跑步驟、新參數的用法說明 —— 各製造一次它描述的狀態,
然後**照著執行**,看解不解得開。

<!-- v0.9.0 的實例:podcast_series 的停點指引呼叫端跑某支工具，而那支工具在該狀態下
     自己也 permission denied。從 MCP 端看回傳值完全正確，照著做才撞上死路。 -->

---

## 收工

0. **先自檢覆蓋率** —— 確認每支工具都真的被碰過:

   ```bash
   for t in $(python3 -c "
   import asyncio,sys; sys.path.insert(0,'../notebooklm-mcp')
   from notebooklm_mcp import app
   print(' '.join(sorted(x.name for x in asyncio.run(app.mcp.list_tools()))))"); do
     grep -q "$t" FINDINGS.md || echo "  ⚠️ 未記錄: $t"
   done
   ```

   漏掉的要嘛補測,要嘛在 `FINDINGS.md` 明寫「刻意不測 + 理由」——
   靜默跳過讀起來會像「已覆蓋」。

1. **把抓到的東西收回離線測試**:凡是寫得成離線測試的一律補進 `../notebooklm-mcp/tests/`,
   **收之前先做突變驗證**(把修正改回舊行為,確認測試真的會紅)。否則下一輪還要再燒一次
   配額發現同一件事。
2. **未結案的前提原樣記進程式碼或 AGENTS.md**,並標明結案方式。
3. **刪掉這輪建的 notebook**(標題含 `ZZ-TEST __VERSION__`),並清憑證殘留與 server 孤兒
   (指令見 CLAUDE.md §三)。
4. 把 `FINDINGS.md` 收進 `../notebooklm-mcp/docs/acceptance-__VERSION__-findings.md`,
   結論摘要進 CHANGELOG,並把 AGENTS.md 那句「尚未跑真實驗收」拿掉。
