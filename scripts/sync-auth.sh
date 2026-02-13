#!/usr/bin/env bash
# sync-auth.sh — 將 notebooklm login 產生的認證資料同步到 Doppler
#
# 用法：
#   notebooklm login          # 先登入
#   bash scripts/sync-auth.sh # 推送到 Doppler
#
# 前置條件：
#   - doppler CLI 已安裝且已登入 (doppler me)
#   - notebooklm-py 已安裝且已登入 (notebooklm login)

set -euo pipefail

PROJECT="notebooklm"
CONFIG="dev"

# 解析 storage_state.json 路徑
STORAGE_PATH="${NOTEBOOKLM_HOME:-$HOME/.notebooklm}/storage_state.json"

if [[ ! -f "$STORAGE_PATH" ]]; then
  echo "❌ 找不到 $STORAGE_PATH"
  echo "   請先執行 notebooklm login"
  exit 1
fi

# 確認 Doppler 已登入
if ! doppler me &>/dev/null; then
  echo "❌ Doppler 未登入，請先執行 doppler login"
  exit 1
fi

# 確認專案存在，不存在就建立
if ! doppler secrets -p "$PROJECT" -c "$CONFIG" &>/dev/null 2>&1; then
  echo "📦 Doppler 專案 '$PROJECT' 不存在，正在建立..."
  doppler projects create "$PROJECT" --description "NotebookLM Google OAuth session"
fi

# 讀取 JSON 並推送到 Doppler
AUTH_JSON=$(cat "$STORAGE_PATH")

echo "📤 正在同步認證到 Doppler (project=$PROJECT, config=$CONFIG)..."
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
