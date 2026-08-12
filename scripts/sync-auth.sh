#!/usr/bin/env bash
# sync-auth.sh — 必須流程：將 notebooklm login 產生的認證資料同步到 Doppler
# 這是多台 VM 共用 NOTEBOOKLM_AUTH_JSON 唯讀認證的真相來源。
#
# 用法：
#   notebooklm login                          # 先登入（主力帳號）
#   bash scripts/sync-auth.sh                 # 推送到 Doppler notebooklm/dev
#
#   # 測試帳號（與主力帳號完全隔離，見 docs/test-account.md）
#   notebooklm profile create test
#   notebooklm -p test login                  # 用「測試用 Google 帳號」登入
#   bash scripts/sync-auth.sh --profile test --config stg
#
# 參數（都有預設值，不傳＝維持原本的主力帳號行為）：
#   --config <name>    Doppler config，預設 dev
#   --profile <name>   notebooklm named profile，預設用 default profile 的 storage_state
#   --storage <path>   直接指定 storage_state.json（覆蓋 --profile 推導）
#
# 前置條件：
#   - doppler CLI 已安裝且已登入 (doppler me)
#   - notebooklm-py 已安裝且該 profile 已登入

set -euo pipefail

PROJECT="notebooklm"
CONFIG="dev"
PROFILE=""
STORAGE_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)  CONFIG="$2";  shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --storage) STORAGE_PATH="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
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
