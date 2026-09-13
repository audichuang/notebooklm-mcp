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

## 三個會讓數字說謊的坑(都實際踩過)

1. **`total_cost_usd` 不是受測 agent 的成本** —— 含它自己 spawn 的 subagent(v0.9.26 那次
   fable 佔原始總成本 61%),而「這次有沒有 spawn」是擲硬幣。用 `cost_sonnet_only`。
2. **受測目錄裡不能有答案** —— `evals/` 不可連進 box(`run.sh` 已跳過),cwd 要是空目錄。
3. **stub 不轉發 protocol 層 instructions** —— 所以這套**量不到 `_INSTRUCTIONS` 的效果**,
   別拿分數替它背書。
