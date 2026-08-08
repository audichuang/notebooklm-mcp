#!/usr/bin/env bash
# setup-test-config.sh — 建立「測試專用」的 Doppler config，與主力帳號完全隔離。
#
# 為什麼要隔離:真實驗收會建 notebook、上傳來源、生成音檔、retract、發布 feed。
# 全部打在主力帳號上會污染正式資料,而且燒的是同一份每日生成配額。
#
# 這支只負責**非認證**的設定;NOTEBOOKLM_AUTH_JSON 一定要由你本人登入測試帳號後,
# 用 sync-auth.sh 推上去(沒有人能替你登入 Google)。完整流程見 docs/test-account.md。
#
# 用法:
#   bash scripts/setup-test-config.sh              # 預設寫進 notebooklm/stg
#   bash scripts/setup-test-config.sh --config qa  # 換一個 config 名字
#
# 冪等:重跑只會覆寫同樣的值;PODCAST_TOKEN_SALT 只在「尚未設定」時才產生,
# 不會每次重跑都換掉(換 salt = 所有測試 feed 的 URL 全變)。

set -euo pipefail

PROJECT="notebooklm"
SOURCE_CONFIG="dev"      # 從這裡複製 NAS uploader 的連線資訊
TARGET_CONFIG="stg"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) TARGET_CONFIG="$2"; shift 2 ;;
    --from)   SOURCE_CONFIG="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "❌ 未知參數:$1"; exit 2 ;;
  esac
done

if [[ "$TARGET_CONFIG" == "$SOURCE_CONFIG" ]]; then
  echo "❌ 目標 config 不能等於來源 config($SOURCE_CONFIG)——那就沒有隔離了"
  exit 2
fi

doppler me &>/dev/null || { echo "❌ Doppler 未登入,先跑 doppler login"; exit 1; }
doppler secrets -p "$PROJECT" -c "$TARGET_CONFIG" &>/dev/null 2>&1 || {
  echo "❌ Doppler config 不存在:$PROJECT/$TARGET_CONFIG"
  echo "   在 Doppler 主控台建立它(或用既有的 stg),再重跑"
  exit 1
}

set_secret() {  # set_secret NAME VALUE —— 不回顯值
  printf '%s' "$2" | doppler secrets set "$1" --raw -p "$PROJECT" -c "$TARGET_CONFIG" >/dev/null
  echo "  ✅ $1"
}
copy_from_source() {
  local name="$1"
  local value
  value=$(doppler secrets get "$name" -p "$PROJECT" -c "$SOURCE_CONFIG" --plain 2>/dev/null || true)
  [[ -n "$value" ]] || { echo "  ⚠️  $SOURCE_CONFIG 沒有 $name,跳過(要發布測試就得自己補)"; return; }
  set_secret "$name" "$value"
}

echo "📦 設定 $PROJECT/$TARGET_CONFIG(測試專用)"

# 與 dev 同樣的理由:0.4.1 起 fetch-token 路徑會無條件先打一次 RotateCookies,
# 少了這顆連 CLI 呼叫都會作廢一次共用 cookie。
set_secret NOTEBOOKLM_DISABLE_KEEPALIVE_POKE 1

# NAS uploader 是同一台(沒有第二台可用),連線資訊直接沿用。
copy_from_source PODCAST_PUBLIC_BASE_URL
copy_from_source PODCAST_UPLOAD_URL
copy_from_source PODCAST_UPLOAD_TOKEN

# **關鍵隔離點**:token salt 一定要跟正式的不同。feed 的公開路徑是
# HMAC(salt, show_id) 決定的,salt 不同 → 測試 feed 落在完全不同的 URL 空間,
# 永遠不可能覆蓋或撞到正式節目的 feed。(uploader 不刪檔,所以「不要撞」比
# 「事後清掉」重要。)
EXISTING_SALT=$(doppler secrets get PODCAST_TOKEN_SALT -p "$PROJECT" -c "$TARGET_CONFIG" --plain 2>/dev/null || true)
if [[ -n "$EXISTING_SALT" ]]; then
  echo "  ↩︎  PODCAST_TOKEN_SALT 已存在,保留不動(換 salt 會讓既有測試 feed 全部換 URL)"
else
  set_secret PODCAST_TOKEN_SALT "$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | head -c 40)"
  echo "     (新產生的測試 salt,與正式 config 不同 → feed URL 空間完全隔離)"
fi

echo
echo "現況($PROJECT/$TARGET_CONFIG 的 secret 名稱):"
doppler secrets -p "$PROJECT" -c "$TARGET_CONFIG" --only-names 2>/dev/null | sed 's/^/  /'
echo
if doppler secrets get NOTEBOOKLM_AUTH_JSON -p "$PROJECT" -c "$TARGET_CONFIG" --plain &>/dev/null; then
  echo "✅ 認證也已就緒,可以開始測試。"
else
  echo "⚠️  還缺 NOTEBOOKLM_AUTH_JSON —— 這一步只能你本人做:"
  echo "     notebooklm profile create test"
  echo "     notebooklm -p test login          # 用「測試用」Google 帳號登入"
  echo "     bash scripts/sync-auth.sh --profile test --config $TARGET_CONFIG"
fi
