#!/usr/bin/env bash
# Routing eval:用 claude -p + dry-run stub 驗「呼叫端讀得到什麼」,不碰真實帳號。
#
#   ./run.sh <arm> <eval_id> "<prompt>" <run_n>
#     arm = new            → 現在的 working tree
#     arm = <git-ish>      → 先開 worktree 跑那個版本(例:./run.sh v0.9.26 e0 "…" 1)
#
# 為什麼是 sonnet:每題約 $0.2,所以跑得起「每題 n 次取平均 + 盲評 + 每次改完重跑」。
# 三個會讓數字說謊的坑見 docs/acceptance-testing.md —— 尤其 **cost 欄不要直接用
# total_cost_usd**(含受測 agent 自己 spawn 的 subagent),要從 modelUsage 取主迴圈。
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SKILL_SRC="${NBLM_SKILL_DIR:-$HOME/research/audi-skill/notebooklm}"
WORK="${NBLM_EVAL_WORK:-${TMPDIR:-/tmp}/nblm-eval}"
mkdir -p "$WORK"

ARM="$1"; ID="$2"; PROMPT="$3"; N="${4:-1}"

if [ "$ARM" = "new" ]; then
  SRC="$REPO_ROOT"; SKILL="$SKILL_SRC"
else
  SRC="$WORK/worktree-$ARM"
  # README 推薦一次跑「兩臂 x n 次 x 全部 id」平行(見 README「三個坑」第四條):同一個
  # $ARM 的所有平行呼叫都搶同一個 $SRC,`[ -d ] || git worktree add` 沒有互斥就是
  # 9 個併發全撞 `fatal: already exists` → exit 1 → 9 個樣本消失。flock 序列化這一段,
  # 鎖內再檢查一次(冪等):第一個贏家建好之後,其餘拿到鎖的都會看到 -d 已成立而跳過。
  (
    flock -x 200
    [ -d "$SRC" ] || git -C "$REPO_ROOT" worktree add -q --detach "$SRC" "$ARM"
  ) 200>"$WORK/worktree-$ARM.lock" || exit 1
  SKILL="$SKILL_SRC"   # 要比舊版 skill 就自己開 audi-skill 的 worktree 再用 NBLM_SKILL_DIR 指過來
fi

OUT="$WORK/results/$ARM"; mkdir -p "$OUT"
# 🔴 受測 box 只放 SKILL.md 與 references/,**跳過 evals/** —— 那裡面就是題目與評分標準,
#    整包連進來等於把答案發給受測者(v0.9.26 踩過)。cwd 也必須是空目錄。
# 用 cp -r 而不是 symlink:受測 agent 帶 `--dangerously-skip-permissions`,對 box 裡
# 任何一個檔案的寫入都是真的寫入 —— symlink 直通活的 skill working tree,實測 agent
# 把答案「順手」寫回 SKILL.md,蓋掉正式檔案且 `rm -rf box` 救不回。複製一份是一次性成本。
BOX="$WORK/box/$ARM-$ID-$N"; rm -rf "$BOX"; mkdir -p "$BOX/.claude/skills/notebooklm"
for entry in "$SKILL"/*; do
  b="$(basename "$entry")"; [ "$b" = "evals" ] && continue
  cp -r "$entry" "$BOX/.claude/skills/notebooklm/$b"
done

LOG="$OUT/$ID-$N.calls.jsonl"; : > "$LOG"
CFG="$BOX/mcp.json"
python3 - "$CFG" "$REPO_ROOT" "$SRC" "$LOG" <<'PY'
import json, sys
cfg, root, src, log = sys.argv[1:5]
json.dump({"mcpServers": {"notebooklm": {"type": "stdio",
    "command": f"{root}/.venv/bin/python",
    "args": [f"{root}/scripts/eval_harness/stub_server.py"],
    "env": {"NBLM_REPO": src, "NBLM_EVAL_LOG": log}}}}, open(cfg, "w"))
PY

cd "$BOX"
timeout 900 claude -p "$PROMPT" --model sonnet --output-format json \
  --dangerously-skip-permissions --strict-mcp-config --mcp-config "$CFG" \
  > "$OUT/$ID-$N.json" 2>"$OUT/$ID-$N.err"
RC=$?
python3 - "$OUT/$ID-$N" "$RC" "$ARM" "$ID" <<'PY'
import json, sys
base, rc, arm, eid = sys.argv[1:5]
try: d = json.load(open(base + ".json", encoding="utf-8"))
except Exception as e:
    print(json.dumps({"arm": arm, "id": eid, "rc": rc, "error": str(e)})); sys.exit(0)
u = d.get("usage", {}); mu = d.get("modelUsage") or {}
calls = [json.loads(l) for l in open(base + ".calls.jsonl", encoding="utf-8") if l.strip()]
s = {"arm": arm, "id": eid, "rc": rc, "num_turns": d.get("num_turns"),
     "cache_creation": u.get("cache_creation_input_tokens"),
     "cache_read": u.get("cache_read_input_tokens"),
     "output_tokens": u.get("output_tokens"),
     # 主迴圈成本;total_cost_usd 含受測 agent 自己 spawn 的 subagent,是雜訊
     "cost_sonnet_only": round(sum(v.get("costUSD", 0) for k, v in mu.items() if "sonnet" in k), 4),
     "cost_raw_incl_subagents": d.get("total_cost_usd"),
     "models_seen": sorted(mu),
     "tools_called": [c["tool"] for c in calls],
     "answer": (d.get("result") or "")[:4000]}
print(json.dumps({k: v for k, v in s.items() if k != "answer"}, ensure_ascii=False))
open(base + ".summary.json", "w", encoding="utf-8").write(json.dumps(s, ensure_ascii=False, indent=2))
PY
