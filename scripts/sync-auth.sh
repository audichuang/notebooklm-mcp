#!/usr/bin/env bash
# sync-auth.sh — 必須流程：將 notebooklm login 產生的認證資料同步到 Doppler
# 這是多台 VM 共用 NOTEBOOKLM_AUTH_JSON 唯讀認證的真相來源。
#
# ⚠️ 正式環境是 prd，而本腳本的 --config 預設是 dev。
#    正式 server 跑的是 `doppler run -p notebooklm -c prd`（見 AGENTS.md），所以
#    **更新正式憑證一定要寫 `--config prd`**；漏了它腳本仍然會印「✅ 同步完成」，
#    只是寫進一個正式環境不讀的 config —— 重啟後照樣壞，且沒有訊號指向原因。
#    預設值刻意不改成 prd：dev 是活的（AGENTS.md：prd 的 PODCAST_TOKEN_SALT 必須
#    逐字等於 dev 的），改預設會讓既有 dev 流程靜默改指向。改成不傳就出聲警告。
#
# 用法：
#   uv run notebooklm login                                 # 先登入（主力帳號）
#   bash scripts/sync-auth.sh --config prd                  # 推送到 Doppler notebooklm/prd（正式）
#
#   # 測試帳號（與主力帳號完全隔離，見 docs/test-account.md）
#   uv run notebooklm profile create test
#   uv run notebooklm -p test login           # 用「測試用 Google 帳號」登入
#   bash scripts/sync-auth.sh --profile test --config stg
#
# ⚠️ 只更新**不帶後綴**的 NOTEBOOKLM_AUTH_JSON（＝槽位 1）。多帳號 pool 的
#    NOTEBOOKLM_AUTH_JSON_2..N 要各自重登後 `doppler secrets set --raw` 寫回。
#    要知道是哪一槽壞了，對著跑起來的 server 跑 auth_check(all_slots=True)。
#
# 參數（都有預設值，不傳＝維持原本的主力帳號行為）：
#   --config <name>    Doppler config，預設 dev（正式環境要傳 prd）
#   --profile <name>   notebooklm named profile，預設用 default profile 的 storage_state
#   --storage <path>   直接指定 storage_state.json（覆蓋 --profile 推導）
#
# 前置條件：
#   - doppler CLI 已安裝且已登入 (doppler me)
#   - notebooklm-py 已安裝且該 profile 已登入

set -euo pipefail

PROJECT="notebooklm"
CONFIG="dev"
CONFIG_EXPLICIT=0
PROFILE=""
STORAGE_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)  CONFIG="$2";  CONFIG_EXPLICIT=1; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --storage) STORAGE_PATH="$2"; shift 2 ;;
    # 動態印「開頭那整段註解」,不寫死行號。舊寫法是 `sed -n '2,25p'`,而**加幾行說明
    # 就會把參數清單推出範圍** —— 實測過:補了 --config 的警語之後,--help 剛好停在
    # 「參數」這個標題上,`--config` / `--profile` / `--storage` 一個都印不出來。
    # 諷刺的是那次補的正是「--config 不能省」。用法說明會長,行號註定會腐化。
    -h|--help) awk 'NR>1 && /^#/ {print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "❌ 未知參數：$1（用 --help 看用法）"; exit 2 ;;
  esac
done

if [[ -z "$STORAGE_PATH" ]]; then
  STORAGE_PATH=$(uv run python -c \
    'import sys; from notebooklm.paths import get_storage_path; print(get_storage_path(sys.argv[1] or None))' \
    "$PROFILE")
fi

if [[ ! -f "$STORAGE_PATH" ]]; then
  echo "❌ 找不到 $STORAGE_PATH"
  if [[ -n "$PROFILE" ]]; then
    # 一律寫 `uv run notebooklm`:PATH 上不該有全域安裝(會與 repo pin 的版本漂開,
    # 2026-08 踩過 0.3.2 誤報 auth expired 那次),uv run 才保證是 pin 的版本。
    echo "   請先在 repo 目錄執行：uv run notebooklm profile create $PROFILE"
    echo "                         uv run notebooklm -p $PROFILE login"
  else
    echo "   請先在 repo 目錄執行：uv run notebooklm login"
  fi
  exit 1
fi

# 確認 Doppler 已登入
if ! doppler me &>/dev/null; then
  echo "❌ Doppler 未登入，請先執行 doppler login"
  exit 1
fi

# 確認專案存在，不存在就建立
if ! doppler secrets -p "$PROJECT" -c "$CONFIG" &>/dev/null 2>&1; then
  echo "❌ Doppler config 不存在：$PROJECT/$CONFIG"
  echo "   先建好 config（測試帳號用 stg，見 docs/test-account.md），或改傳 --config"
  exit 1
fi

# 沒傳 --config 時出聲。這條擋的是真實故障情境：認證掛了、人在急著重登，照著錯誤訊息
# 或舊筆記跑一次 bare 指令，寫進 dev、看到「✅ 同步完成」、重啟、還是壞 —— 而畫面上
# 沒有任何東西說錯在哪。互動時停下來問；非互動（CI／腳本）只警告不擋，才不會改壞既有自動化。
if [[ "$CONFIG_EXPLICIT" -eq 0 ]]; then
  echo "⚠️  沒有指定 --config，將寫入預設的 '$CONFIG'。"
  echo "    正式環境是 prd（server 跑 doppler run -c prd）；寫錯 config 不會報錯，只會靜默沒生效。"
  if [[ -t 0 ]]; then
    read -r -p "    確定要寫入 '$CONFIG' 嗎？(輸入 yes 繼續) " reply
    [[ "$reply" == "yes" ]] || { echo "已中止。正式環境請跑：bash scripts/sync-auth.sh --config prd"; exit 1; }
  fi
fi

# 讀取 JSON 並推送到 Doppler
AUTH_JSON=$(cat "$STORAGE_PATH")

echo "📤 正在同步認證到 Doppler (project=$PROJECT, config=$CONFIG, 來源=$STORAGE_PATH)..."
echo "$AUTH_JSON" | doppler secrets set NOTEBOOKLM_AUTH_JSON --raw -p "$PROJECT" -c "$CONFIG"

# 驗證
echo "✅ 同步完成！驗證中..."
STORED_LEN=$(doppler secrets get NOTEBOOKLM_AUTH_JSON -p "$PROJECT" -c "$CONFIG" --plain 2>/dev/null | wc -c | tr -d ' ')

if [[ "$STORED_LEN" -gt 100 ]]; then
  echo "✅ 驗證成功 (${STORED_LEN} bytes)"
  echo ""
  echo "現在可以用以下方式執行 notebooklm："
  echo "  doppler run -p $PROJECT -c $CONFIG -- notebooklm list"
else
  echo "⚠️  驗證可能有問題 (只有 ${STORED_LEN} bytes)，請手動檢查"
  exit 1
fi
