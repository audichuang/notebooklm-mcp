# v0.9.26 驗收紀錄 —— `claude -p` + sonnet,10 題 routing eval

**這份記的是量測本身怎麼做的、量到什麼、以及哪些數字不能信。** 版本敘事在 CHANGELOG;
harness 與重跑指令在 [`scripts/eval_harness/`](../scripts/eval_harness/)。

## 怎麼量的

- **sonnet**,每題 $0.2 等級 —— 所以跑得起「每題兩次取平均 + 盲評」。
- 每次跑在一個**只有 `.claude/skills/` 的空目錄**,skill 只連 `SKILL.md` 與 `references/`,
  **跳過 `evals/`**(那裡面就是題目與評分標準)。
- 舊版那一臂**從 git worktree 起跑**(`67a9854` / skill `8d88443^`),不是靠記憶描述舊版。
- MCP 走 dry-run stub:真實 schema、不執行任何呼叫 → 零帳號風險、零配額。
- 盲評:評分者只拿到回答與 expectations,**不知道是哪一版**。

## 結果

| eval | turns 舊→新 | cache_creation 舊→新 | cost(sonnet)舊→新 | 盲評 舊→新 |
|---|---|---|---|---|
| `e0-source-search` | 5.5 → 4.0 | 61,149.0 → 41,645.0 | 0.32 → 0.23 | 6/6 → 6/6 |
| `e1-fulltext-contains` | 6.0 → 5.0 | 57,024.5 → 37,709.0 | 0.33 → 0.23 | 6/6 → 6/6 |
| `e2-custom-report` | 14.0 → 8.5 | 80,941.5 → 60,988.0 | 0.59 → 0.38 | 6/6 → 6/6 |
| `e3-notebook-not-found` | 4.0 → 3.5 | 42,330.5 → 40,303.5 | 0.25 → 0.23 | 6/6 → 6/6 |
| `e4-retract-vs-unpublish` | 7.0 → 8.0 | 59,733.5 → 63,656.0 | 0.37 → 0.41 | 6/6 → 6/6 |
| `e5-series-vs-episode` | 4.5 → 4.0 | 52,255.0 → 39,952.0 | 0.30 → 0.23 | 5/6 → 4/6 |
| `e6-resume-timeout` | 7.0 → 6.0 | 87,480.0 → 53,442.5 | 0.49 → 0.35 | 4/6 → 6/6 |
| `e7-publish-serial` | 11.0 → 5.0 | 87,857.5 → 50,214.0 | 0.62 → 0.33 | 6/6 → 6/6 |
| `e8-auth-pool` | 5.0 → 4.5 | 73,407.5 → 41,781.0 | 0.36 → 0.23 | 6/6 → 6/6 |
| `e9-research` | 8.5 → 5.5 | 86,934.5 → 48,954.0 | 0.53 → 0.30 | 6/6 → 6/6 |

| 合計 | 舊 | 新 | |
|---|---|---|---|
| turns | 72.5 | 54.0 | **-25.5%** |
| cache_creation | 689,113.5 | 478,645.0 | **-30.5%** |
| cache_read | 3,986,537.5 | 2,344,378.0 | **-41.2%** |
| 成本(sonnet-only) | 4.2 | 2.9 | **-30.1%** |
| 盲評品質 | 57/60 | **58/60** | 品質沒換錢,一起上去 |

`e6`(中斷續跑)從 4/6 變 6/6 —— reconcile 與 retract 的規則本來被埋在事故敘事中間。

## 確定性量測(不靠 agent,可重跑)

| | 舊 | 新 |
|---|---|---|
| `tools/list` payload | 45,654 字元 | 37,165 字元 (-18.6%) |
| 37 支描述合計 | 24,013 字元 | 15,717 字元 (**-34.5%**) |
| `podcast_attempt_retract` 描述 | 3,981 字元 | 1,556 字元 (-61%) |

payload 另一半是 `inputSchema`,由參數名與型別推導(全 repo 沒有 `Field(description=…)`),
不刪參數就動不了。`tests/test_tool_payload_budget.py` 釘住上限。

## ⚠️ 哪些數字不能信(都實際踩過)

1. **`total_cost_usd` 不是受測 agent 的成本。** 含它自己 spawn 的 subagent —— 這次實際出現
   haiku-4.5 與 **fable-5.1**,後者佔原始總成本 **61%**,而「這次有沒有 spawn」基本上是擲硬幣
   (同一臂同一題兩次可差 7.7 倍)。上表已改用 `cost_sonnet_only`;原始值的 -31.1% 是運氣,
   乾淨值是 -30.1%。**表裡沒有任何一格「變貴」是真的** —— 那只是某次多 spawn 了一個。
2. **這套量不到 `_INSTRUCTIONS`。** `stub_server.py` 只代理 `list_tools`,不轉發 protocol 層
   instructions(stub 送 0 字元、正式 server 送 1,271)。所以 e5(series vs episode)與 e8
   (`all_slots`)的分數對 v0.9.26 的 `_INSTRUCTIONS` 修正**零資訊量**。
3. **兩臂同時換了 repo 與 skill**,所以省下的 turn/token **無法單獨歸因到描述瘦身** ——
   每題 cache_creation 差約 19.5k tokens,而描述只差 8,489 字元(≈2.7k tokens),
   差額主要來自 skill 側那條「路由層不再把 agent 推去讀 98KB」。

## 這次量測本身的教訓

- **evals 放進 skill 目錄 = 把答案發給受測者。** 做對的事(evals 跟 skill 一起版控)反而
  製造的陷阱 —— harness 必須顯式排除,`run.sh` 已跳過 `evals/`。
- **cwd 也要乾淨。** 早期版本把 harness 與結果放在同一層,受測 agent 掃到之後一次回答了
  全部十題,turn 數與成本全部失真。
- **先看 `modelUsage` 拆解再下結論**,不要直接用 `total_cost_usd`。

