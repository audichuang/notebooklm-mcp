# eval_harness — 用 `claude -p` 驗「呼叫端讀得到什麼」

驗的是**工具描述 / `_INSTRUCTIONS` / skill 文字改完,routing 會不會變差**。
`stub_server.py` 直接從 `notebooklm_mcp.app.mcp` 讀回**真實** schema,但 `call_tool`
只記錄參數並回一句 DRY RUN —— 受測 agent 看到的描述與正式 server 逐字相同,而
**零帳號風險、零配額**。

```bash
# 現在的 working tree,e0 跑第 1 次
./run.sh new e0-source-search "$(grep ^e0 evals.tsv | cut -f2)" 1

# 跟某個舊版本比(自動開 worktree)
./run.sh v0.9.26 e0-source-search "$(grep ^e0 evals.tsv | cut -f2)" 1

# 兩臂各跑兩次
for n in 1 2; do for arm in v0.9.26 new; do
  while IFS=$'\t' read -r id p; do ./run.sh "$arm" "$id" "$p" "$n" & done < evals.tsv
done; wait; done

python3 grade.py          # 盲評,印各臂通過率
```

結果落在 `$NBLM_EVAL_WORK`(預設 `/tmp/nblm-eval`)。
v0.9.26 的實跑紀錄與結論見 [`../../docs/acceptance-v0.9.26-eval.md`](../../docs/acceptance-v0.9.26-eval.md);
`results-v0.9.26.json` 是那次的逐題摘要(可重算)。

## 會讓數字說謊的坑(都實際踩過)

1. **`total_cost_usd` 不是受測 agent 的成本** —— 含它自己 spawn 的 subagent(v0.9.26 那次
   fable 佔原始總成本 61%),而「這次有沒有 spawn」是擲硬幣。用 `cost_sonnet_only`。
2. **受測目錄裡不能有答案** —— `evals/` 不可連進 box(`run.sh` 已跳過),cwd 要是空目錄。
3. **stub 不轉發 protocol 層 instructions** —— 所以這套**量不到 `_INSTRUCTIONS` 的效果**,
   別拿分數替它背書。
4. **冷啟動平行 → worktree 競態掉樣本** —— 上面「兩臂各跑兩次」那段對同一個 `arm` 平行
   丟進所有 `id`,而它們共用同一個 `$SRC`(`$WORK/worktree-$ARM`)。worktree 目錄還不存在
   時,`[ -d ] || git worktree add` 沒有互斥:10 個併發只有 1 個建得成,其餘 9 個撞
   `fatal: already exists` 直接 `exit 1`,那個臂的樣本數就少了 9 個。`run.sh` 已用
   `flock` 序列化這一段;`grade.py` 印各臂樣本數(`n=`)並在兩臂 id 集合不同時警告 ——
   但**先跑過一次讓 worktree 建好**(或確認 `n` 對齊)再信通過率,不然分數不可比較。
5. **user 層的 skill / hooks 會摻進兩臂** —— 不加 `--setting-sources project` 時,`~/.claude/skills/`
   裡**已安裝的那份 notebooklm** 跟 box 裡的受測版並存,transcript 顯示受測 agent 讀的是已安裝那份
   (`~/.claude/skills/notebooklm/references/…`),外加 user 的 SessionStart hook 注入。兩臂都摻到
   同一份 skill,差異被抹平。`run.sh` / `grade.py` 已加上;**2026-10-21 前的結果(含 v0.9.26 那份)
   都有這個污染**。驗法:transcript 裡 Skill 工具的 `Base directory` 要指向 box。
6. **受測 agent 讀得到真實資料** —— box 只隔離 cwd,prompt 提到 `~/podcast/audicast` 時它會真的去讀
   那份 manifest,於是同一題在兩臂各自撞到/沒撞到真實狀態(實測 e3 因此掉分,與 skill 無關)。
   n 小時任何單格翻轉先讀 `grade.json` 的 `evidence`,證據沒有指到 skill 文字就不算進差異。
7. **看 skill 有沒有載入再談 body** —— e0/e1 兩臂都沒呼叫 Skill 工具(把「用 notebooklm」讀成直接
   呼叫 MCP),那兩題量的是 description 不是 body。transcript 在
   `~/.claude/projects/*evalwork-box-<arm>-<id>-<n>/`,數 `tool_use` 的 `Skill`。
8. **搬移 / 瘦身 skill 文字後跑全量,不要只跑子集** —— 子集是照「這次改到哪」挑的,而被搬走或
   改寫的一句話傷到的往往是你沒挑的題:v0.9.38 瘦身時一句「`artifact_list` 不是救援入口」被套到
   slides,e20 從 8/9 掉到 4/9,變體子集剛好沒有 e20(子集 124/126 看起來還變好)。
