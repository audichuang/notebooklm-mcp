#!/usr/bin/env bash
# __VERSION__ 的免費本機檢查:不打任何 RPC、不燒配額、不碰遠端。
#
# 先跑這支再進真實生成。它驗的是「純本機就能證明」的那些不變式——尤其是這一版
# 把帳號身分從 process 全域搬到 client 自帶的 storage_state 檔所牽動的部分。
# 全過再開始花配額。
#
#   bash local-checks.sh
set -uo pipefail

# 從已安裝 shim 的 shebang 取 python —— 那才是「真正會跑 nblm-mcp 的那個直譯器」,
# 檢查的就是實裝環境本身。刻意不用 `uv tool dir`:它會吐 ANSI 色碼,拼進路徑就爛掉。
SHIM="$(command -v nblm-mcp || true)"
if [ -z "$SHIM" ]; then
  echo "❌ PATH 上找不到 nblm-mcp —— 先照 SKILL.md §Auth 的 pin 安裝"; exit 1
fi
TOOL_PY="$(head -1 "$SHIM" | sed 's|^#!||')"
# uv 在安裝路徑太長(超過 shebang 的 127 字元上限)時,會改產 `#!/bin/sh` + 第二行
# `'''exec' '/…/python' "$0" "$@"` 的包裝。**只檢查 `-x` 會誤判**:`/bin/sh` 本身當然
# 可執行,於是 TOOL_PY 變成 sh,底下每一段 python 都會以 shell 語法被解讀,
# 全部失敗且錯誤訊息(`import: not found`)完全指不到真因。所以要真的問它是不是 python。
is_python() { [ -x "$1" ] && "$1" -c "import sys" >/dev/null 2>&1; }
is_python "$TOOL_PY" || TOOL_PY="$(sed -n "2s|.*'\\(/.*/python[0-9.]*\\)'.*|\\1|p" "$SHIM")"
if ! is_python "$TOOL_PY"; then
  echo "❌ 解析不出 nblm-mcp 用的 python(shim=$SHIM)"; exit 1
fi
export PYTHONWARNINGS=ignore   # pydantic_settings 的 forward-ref 警告與本檢查無關
pass=0; fail=0
ok()  { echo "  ✅ $1"; pass=$((pass+1)); }
bad() { echo "  ❌ $1"; fail=$((fail+1)); }

echo "=== 0. 實裝版本 ==="
read -r v sdk mcpv < <("$TOOL_PY" -c "
import importlib.metadata as m
print(m.version('notebooklm-mcp'), m.version('notebooklm-py'), m.version('mcp'))
" 2>/dev/null)
[ "$v" = "__VERSION_BARE__" ] && ok "notebooklm-mcp $v" \
  || bad "notebooklm-mcp 是 $v,不是 __VERSION_BARE__ —— uv 的 git cache 壞掉時會靜默裝成舊版(v0.8.1 踩過)。
       修:uv cache clean notebooklm-mcp && uv tool install --python 3.12 --force \\
           'git+https://github.com/audichuang/notebooklm-mcp.git@__VERSION__'"
# 預期的 SDK 版本從 repo 的 pyproject 下界推導,不要硬編 —— 硬編的那次(0.8.0)在
# 換版之後整個 §0 變成假紅,而 §0 的角色正是「壞了就別往下跑」的硬關卡。
sdk_floor="$("$TOOL_PY" - <<'PY' 2>/dev/null
import re, tomllib
from pathlib import Path
here = Path(__file__).resolve() if "__file__" in dir() else Path.cwd()
for base in (Path.cwd(), Path.cwd().parent):
    p = base / "notebooklm-mcp" / "pyproject.toml"
    if p.exists():
        deps = tomllib.loads(p.read_text(encoding="utf-8"))["project"]["dependencies"]
        for d in deps:
            if d.startswith("notebooklm-py"):
                m = re.search(r">=\s*([0-9][0-9.]*)", d)
                if m:
                    print(m.group(1))
        break
PY
)"
if [ -n "$sdk_floor" ]; then
  [ "$sdk" = "$sdk_floor" ] && ok "notebooklm-py $sdk(= pyproject 下界)" \
    || bad "notebooklm-py $sdk,但 pyproject 下界是 $sdk_floor —— 實裝與 pin 不同版"
else
  echo "  ℹ️  notebooklm-py $sdk(讀不到 ../notebooklm-mcp/pyproject.toml,略過比對)"
fi
echo "  ℹ️  mcp $mcpv(lock 也是 1.29.0;若這裡不同,測試與實跑就不同版)"

echo
echo "=== 1. 身分不再放 process 全域(v0.9.0 的核心)==="
detail=$("$TOOL_PY" - <<'PY' 2>&1
import ast, inspect
from notebooklm_mcp import runtime
bad = []
if hasattr(runtime, "_sync_auth_env"):
    bad.append("runtime 還有 _sync_auth_env —— 舊的『env 當身分』機制沒被移除")
if hasattr(runtime, "AUTH_JSON_ENV"):
    bad.append("runtime 還匯出 AUTH_JSON_ENV —— 身分不該再經過 env")
if not hasattr(runtime, "snapshot"):
    bad.append("runtime 少了 snapshot() —— 記帳與送出無法同源")
else:
    # 用 AST 而不是找字串:docstring 本身就會提到 await。snapshot() 必須是同步的,
    # 中間有任何 suspend 點就可能讓 label 與 client 跨到不同槽位。
    tree = ast.parse(inspect.getsource(runtime.snapshot).lstrip())
    if inspect.iscoroutinefunction(runtime.snapshot) or any(
        isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for n in ast.walk(tree)
    ):
        bad.append("snapshot() 內有 suspend 點 —— 取出的兩個值可能跨槽位")
print("\n".join(bad) if bad else "OK")
PY
)
[ "$detail" = "OK" ] && ok "_sync_auth_env 已移除、snapshot() 已就位且無 suspend 點" \
  || bad "身分機制沒換乾淨:$detail"

echo
echo "=== 2. dispatch 把「實際送出的帳號與 client」交還給呼叫端 ==="
# 只回 artifact_id 的話,呼叫端做 finalize 只能回頭讀全域 —— 而風險視窗幾乎全在
# finalize 那數十分鐘,不在 dispatch 那幾秒。
#
# ⚠️ **這一條原本是對 `tools_podcast` 的源碼做字面比對,而那是範本缺陷**
# (v0.9.25-rc 驗收抓到):v0.9.16 把 failover 迴圈抽到
# `_failover.dispatch_with_failover`,`return ensure_started(status), account, client`
# 那行跟著搬家,字面比對就從 v0.9.16 起一路假紅 —— 而它被逐字複製進每一個工作區。
# 改成驗**不變式**而不是驗那行字在哪個檔:
#   ① `_dispatch_audio_with_failover` 的 account / client 是 keyword-only
#   ② 它的回傳標註是三元組
#   ③ 「回傳三元組」這件事實際發生在**共用迴圈**裡 —— 直接 import 那支來驗,
#      迴圈再搬一次家也不會假紅(而它真的搬過)。
"$TOOL_PY" - <<'CHECK2' 2>/dev/null
import inspect
from notebooklm_mcp import tools_podcast as p

sig = inspect.signature(p._dispatch_audio_with_failover)
for name in ("account", "client"):
    param = sig.parameters.get(name)
    assert param is not None, f"少了 {name} 參數:{sig}"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"{name} 不是 keyword-only —— 位置呼叫會在上游插參數時靜默錯位"
    )
ann = str(sig.return_annotation)
assert ann.count(",") == 2 and "tuple" in ann.lower(), f"回傳不是三元組:{ann}"

# 「回傳三元組」的實作住在共用迴圈,不在這支薄包裝裡。
from notebooklm_mcp._failover import dispatch_with_failover
loop_src = inspect.getsource(dispatch_with_failover)
assert "return ensure_started(status), account, client" in loop_src, (
    "共用 failover 迴圈不再把 (artifact_id, account, client) 交還給呼叫端"
)
print("OK")
CHECK2
[ "$?" = "0" ] && ok "dispatch 的 account/client 是 keyword-only、回三元組,且共用迴圈真的交還" \
  || bad "dispatch 的簽名或共用迴圈的回傳不對 —— finalize 會鬆開身分"

# 兩個呼叫端都不可以在 dispatch 之後回頭讀全域拿 client。
n=$("$TOOL_PY" -c "
import inspect, re
from notebooklm_mcp import tools_podcast as p
src = inspect.getsource(p)
print(len(re.findall(r'#\s*failover 可能已經換過帳號', src)))
" 2>/dev/null)
[ "$n" = "0" ] && ok "沒有殘留的「dispatch 後重讀全域」註解" \
  || bad "還有 $n 處舊的 client = runtime.get_client() 註解 —— 身分只釘到 dispatch"

echo
echo "=== 3. ⭐ 落檔預驗證必須與 SDK strict loader 同語義 ==="
# 這是整段「L2 PSIDTS recovery 不可達」安全論證的地基。兩顆驗證器對「空值 cookie」
# 曾經判定相反:extract 只看 name、strict 連 value 也要非空 —— 空值 PSIDTS 於是
# 預驗證放行、開檔 raise、真的發出一次 RotateCookies POST(實跑重現過)。
"$TOOL_PY" - <<'PY' 2>/dev/null
import json, tempfile, pathlib
from notebooklm_mcp.app import _write_credential_file
from notebooklm.auth import MINIMUM_REQUIRED_COOKIES

def cred(psidts):
    return json.dumps({"cookies": [
        {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
        {"name": "__Secure-1PSIDTS", "value": psidts, "domain": ".google.com", "path": "/"},
        {"name": "APISID", "value": "a", "domain": ".google.com", "path": "/"},
        {"name": "SAPISID", "value": "s", "domain": ".google.com", "path": "/"},
    ]})

with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d)
    # 好的憑證要收
    _write_credential_file(cred("ok"), d / "good.json", 1)
    # 空值 PSIDTS 一定要在寫檔之前就 raise
    try:
        _write_credential_file(cred(""), d / "blank.json", 2)
    except Exception:
        print("OK")
    else:
        print("FAIL:空值 PSIDTS 通過了預驗證 —— L2 RotateCookies 那條路是開的")
PY
r=$("$TOOL_PY" - <<'PY' 2>/dev/null
import json, tempfile, pathlib
from notebooklm_mcp.app import _write_credential_file
def cred(p):
    return json.dumps({"cookies":[
        {"name":"SID","value":"x","domain":".google.com","path":"/"},
        {"name":"__Secure-1PSIDTS","value":p,"domain":".google.com","path":"/"},
        {"name":"APISID","value":"a","domain":".google.com","path":"/"},
        {"name":"SAPISID","value":"s","domain":".google.com","path":"/"}]})
with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d)
    try:
        _write_credential_file(cred("ok"), d/"g.json", 1)
    except Exception as e:
        print("badgood"); raise SystemExit
    try:
        _write_credential_file(cred(""), d/"b.json", 2)
        print("leak")
    except Exception:
        print("ok")
PY
)
[ "$r" = "ok" ] && ok "空值 PSIDTS 在落檔前就被擋掉(strict 同語義)" \
  || bad "預驗證與 strict loader 分岔了(結果=$r)—— 停下來,別跑真帳號情境"

# 落檔的權限必須是 0600(目錄權限在 README §1-a 用真 server 驗)
perm=$("$TOOL_PY" - <<'PY' 2>/dev/null
import json, stat, tempfile, pathlib
from notebooklm_mcp.app import _write_credential_file
c = json.dumps({"cookies":[
    {"name":"SID","value":"x","domain":".google.com","path":"/"},
    {"name":"__Secure-1PSIDTS","value":"y","domain":".google.com","path":"/"},
    {"name":"APISID","value":"a","domain":".google.com","path":"/"},
    {"name":"SAPISID","value":"s","domain":".google.com","path":"/"}]})
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d)/"slot-1.json"
    _write_credential_file(c, p, 1)
    print(oct(stat.S_IMODE(p.stat().st_mode)))
PY
)
[ "$perm" = "0o600" ] && ok "憑證檔是 0600" || bad "憑證檔權限是 $perm,不是 0600"

echo
echo "=== 4. 這一版新增的對外契約 ==="
"$TOOL_PY" -c "
import inspect
from notebooklm_mcp import tools_podcast as p
sig = inspect.signature(p.podcast_attempt_retract.fn if hasattr(p.podcast_attempt_retract,'fn') else p.podcast_attempt_retract)
assert 'abandon_in_flight' in sig.parameters, sig
assert sig.parameters['abandon_in_flight'].default is False, '預設必須是 False'
" 2>/dev/null && ok "podcast_attempt_retract 有 abandon_in_flight(預設 False)" \
  || bad "podcast_attempt_retract 少了 abandon_in_flight 或預設不是 False"

"$TOOL_PY" -c "
import inspect
from notebooklm_mcp import tools_podcast as p
assert 'notebook_access_denied' in inspect.getsource(p), '找不到新的 observed_state'
" 2>/dev/null && ok "series 有 notebook_access_denied 停點" \
  || bad "找不到 notebook_access_denied —— 權限問題會退回無限重試"

echo
echo "=== 5. 分享的權限判準(v0.9.0 的 P0)==="
# add_user 的預設是 VIEWER,所以網頁上手動分享過的既有 notebook 極可能只是 VIEWER。
# 只比對 email 會把它當成「已分享」,而 pool 其實還是壞的。
"$TOOL_PY" - <<'PY' 2>/dev/null
import inspect
from notebooklm_mcp import tools_basic as b
from notebooklm.rpc.types import SharePermission
from types import SimpleNamespace
st = SimpleNamespace(shared_users=[SimpleNamespace(email="v@x", permission=SharePermission.VIEWER),
                                   SimpleNamespace(email="e@x", permission=SharePermission.EDITOR),
                                   SimpleNamespace(email="o@x", permission=SharePermission.OWNER)])
assert b._has_sufficient_permission(st, "v@x") is False, "VIEWER 被當成已分享"
assert b._has_sufficient_permission(st, "e@x") is True
assert b._has_sufficient_permission(st, "o@x") is True, "OWNER 應算已足夠(否則會把 owner 降權)"
assert b._has_sufficient_permission(st, "E@X") is True, "email 比對要 case-insensitive"
print("OK")
PY
[ "$?" = "0" ] && ok "VIEWER 不算已分享 / OWNER 算 / email 大小寫不敏感" \
  || bad "_has_sufficient_permission 的判準不對"

echo
echo "=== 6. SDK 契約(上游漂移絆線)==="
"$TOOL_PY" -c "
import inspect
from notebooklm._sharing import SharingAPI
from notebooklm.rpc.types import SharePermission
from notebooklm._types.sharing import SharedUser
p = inspect.signature(SharingAPI.add_user).parameters['permission']
assert p.default is SharePermission.VIEWER, '上游改了 add_user 的預設權限'
assert p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, 'permission 變成 keyword-only 了'
assert 'permission' in SharedUser.__dataclass_fields__, 'SharedUser 少了 permission'
" 2>/dev/null && ok "add_user 預設仍是 VIEWER、permission 仍可位置傳、SharedUser 有 permission" \
  || bad "上游 sharing API 漂移了 —— 分享的權限判準要重新推導"

echo
echo "=== 7. 工具面完整 ==="
n=$("$TOOL_PY" -c "
import asyncio
from notebooklm_mcp import app
print(len(asyncio.run(app.mcp.list_tools())))
" 2>/dev/null)
[ "$n" = "__TOOL_COUNT__" ] && ok "__TOOL_COUNT__ 個 MCP 工具" || bad "工具數是 $n,預期 __TOOL_COUNT__"

echo
echo "=== 8. __VERSION__ 新增行為的斷言(每版必填)==="
# 這一節是範本刻意留空的地方:上面 0~7 是跨版本的回歸防護,這裡放**這一版改了什麼**。
# 沒有這一節，local-checks 就只是在證明舊功能還在，證不了新改動是對的。
#
# 寫法:用 fake 排出真實情境的形狀，斷言**新舊行為的差別**（判別實驗），
# 而不是斷言「函式存在」。下面是 v0.9.1 的真實例子，仿寫時整段換掉:
#
#   "$TOOL_PY" - <<'CHECK8' 2>/dev/null
#   import asyncio
#   from types import SimpleNamespace
#   from notebooklm.exceptions import ClientError
#   from notebooklm_mcp import runtime, tools_basic as b
#   # 排出「作用中帳號看不到 notebook」的狀態 —— 舊版在這裡會 permission denied
#   ...
#   out = asyncio.run(b.notebook_share_with_pool("nb-1"))
#   assert out["shared_by"] == "owner@x", out          # 新版:改由看得見的帳號執行
#   assert runtime.active_account() == before          # 且不動輪替游標
#   CHECK8
#   [ "$?" = "0" ] && ok "……" || bad "……"

echo
echo "=== 8z. skill 快照與上游一致(避免驗到舊文件)==="
# 這條是絆線,不是禮貌檢查:v0.9.14 實際踩過——skill repo rebase 帶進 2 個 commit 之後
# 沒重新複製快照,工作區驗的是落後兩版的 SKILL.md/qa.md,而漏掉的正好是
# 「講義會編出來源沒有的識別碼」與「重新發布不下架舊產物」兩條實質 QA 判準。
# skill 文件本來就寫了「複製時機在 skill repo 推完之後」,但那是靠人記得的規則。
UPSTREAM_SKILL="../audi-skill/notebooklm"
[ -d "$UPSTREAM_SKILL" ] || UPSTREAM_SKILL="../../audi-skill/notebooklm"
if [ -d "$UPSTREAM_SKILL" ]; then
  # 排除 evals/:那是 skill repo 的驗收素材,不是 skill 內容,快照本來就不該帶
  snap_diff="$(diff -rq "$UPSTREAM_SKILL" .claude/skills/notebooklm \
    --exclude=.mcp.json --exclude=evals 2>&1)"
  if [ -z "$snap_diff" ]; then
    ok "skill 快照與上游逐字一致"
  else
    bad "skill 快照落後上游 —— 你會驗到舊文件:
$snap_diff
       修:rm -rf .claude/skills/notebooklm && mkdir -p .claude/skills/notebooklm && \\
           cp -r $UPSTREAM_SKILL/SKILL.md $UPSTREAM_SKILL/references .claude/skills/notebooklm/"
  fi
  up_head="$(git -C "$UPSTREAM_SKILL/.." log --oneline -1 2>/dev/null)"
  echo "  ℹ️  上游 skill HEAD: ${up_head:-(讀不到)}"
else
  echo "  ℹ️  找不到上游 skill 目錄,略過快照比對"
fi

echo
echo "──────────────────────────────────────────"
echo "  通過 $pass / 失敗 $fail"
[ "$fail" -eq 0 ] && echo "  ✅ 本機檢查全過,可以開始花配額(README 的實跑情境)" \
                  || echo "  ❌ 先修掉上面的失敗,不要進真實生成"
exit "$fail"
