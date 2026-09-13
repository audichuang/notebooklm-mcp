"""盲評:把某一輪的回答逐條對 expectations 打分。評分者看不到是哪一版。

    python3 grade.py [arm ...]      # 預設評 $NBLM_EVAL_WORK/results 底下全部

分數只有在兩臂用**同一組 expectations**、且評分者拿不到版本資訊時才可比。
"""
import collections, concurrent.futures, glob, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("NBLM_EVAL_WORK", os.path.join(os.environ.get("TMPDIR", "/tmp"), "nblm-eval"))
EXPECTATIONS = json.load(open(os.path.join(HERE, "expectations.json"), encoding="utf-8"))
EMPTY_MCP = os.path.join(WORK, "empty-mcp.json")


def grade(path):
    s = json.load(open(path, encoding="utf-8"))
    items = EXPECTATIONS.get(s["id"])
    if not items:
        return None
    prompt = (
        "你是評分者。下面是一個 agent 對某個 NotebookLM 任務的**規劃回答**,以及一組要逐條檢查的期望。\n"
        "你**不知道**這是哪一個版本產生的,也不該猜。只看回答內容本身。\n\n## 回答\n"
        + s["answer"] + "\n\n## 要檢查的期望\n"
        + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
        + '\n\n## 輸出\n只輸出 JSON:{"expectations":[{"text":"期望原文","passed":true/false,"evidence":"證據片段"}]}\n'
          "每條都要有。passed 只有在回答**明確做到**時才 true;沒提到就是 false。"
    )
    r = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions",
         "--strict-mcp-config", "--mcp-config", EMPTY_MCP],
        capture_output=True, text=True, timeout=900)
    try:
        txt = json.loads(r.stdout)["result"]
        g = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception as e:
        g = {"expectations": [{"text": t, "passed": False, "evidence": f"GRADE FAIL {e}"} for t in items]}
    json.dump(g, open(path.replace(".summary.json", ".grade.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    passed = sum(1 for x in g["expectations"] if x.get("passed"))
    return path.split(os.sep)[-2], s["id"], passed, len(g["expectations"])


def main():
    os.makedirs(WORK, exist_ok=True)
    json.dump({"mcpServers": {}}, open(EMPTY_MCP, "w"))
    arms = sys.argv[1:] or [os.path.basename(d) for d in glob.glob(f"{WORK}/results/*") if os.path.isdir(d)]
    files = [f for a in arms for f in sorted(glob.glob(f"{WORK}/results/{a}/*.summary.json"))]
    if not files:
        sys.exit(f"沒有結果可評:{WORK}/results/{{{','.join(arms) or '…'}}}/")
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        res = [r for r in ex.map(grade, files) if r]
    agg = collections.defaultdict(lambda: [0, 0])
    for arm, _eid, p, t in res:
        agg[arm][0] += p
        agg[arm][1] += t
    for arm, (p, t) in sorted(agg.items()):
        print(f"{arm:<24}{p}/{t} = {100*p/t:.1f}%")


if __name__ == "__main__":
    main()
