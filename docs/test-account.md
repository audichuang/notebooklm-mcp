# 測試帳號:把真實驗收與主力帳號切開

真實驗收會建 notebook、上傳來源、生成音檔、retract、發布 feed。全部打在主力帳號上會污染
正式資料,而且燒的是**同一份每日生成配額**。所以測試走一個獨立的 Google 帳號 +
獨立的 Doppler config。

## 隔離長什麼樣

| | 主力 | 測試 |
|---|---|---|
| Doppler config | `notebooklm/dev` | `notebooklm/stg` |
| 本機認證檔 | `~/.notebooklm/storage_state.json` | `~/.notebooklm/profiles/test/storage_state.json` |
| Google 帳號 | 你的主帳號 | 另開一個(測試用) |
| `PODCAST_TOKEN_SALT` | 正式 | **另一組隨機值** |

最後一列是關鍵:feed 的公開路徑是 `HMAC(salt, show_id)` 算出來的,salt 不同代表測試 feed
落在**完全不同的 URL 空間**,永遠不可能覆蓋或撞到正式節目。NAS uploader 不刪檔,所以
「不要撞」比「事後清掉」重要得多。

NAS uploader 只有一台,連線資訊(`PODCAST_UPLOAD_URL` / `PODCAST_UPLOAD_TOKEN` /
`PODCAST_PUBLIC_BASE_URL`)是沿用的 —— 隔離靠 salt,不靠不同的 uploader。

## 一次性設定

### 1. 非認證的設定(已完成,重跑冪等)

```bash
bash scripts/setup-test-config.sh          # 寫進 notebooklm/stg
```
會設好 `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE`、從 dev 複製 NAS 連線資訊、並產生一組**新的**
`PODCAST_TOKEN_SALT`(已存在就保留不動)。它**不碰** `NOTEBOOKLM_AUTH_JSON`。

### 2. 登入測試帳號(只有你本人能做,要有 GUI)

> ℹ️ **PATH 上已經沒有 `notebooklm` 了**(2026-08-08 移除)。原本 pipx 那份停在 0.3.2,
> 沒有 `profile` 子指令、而且對現行認證一律回 `Authentication expired or invalid` ——
> 那個錯誤訊息會把人誤導到「網域搬遷」上,實際只是 CLI 太舊。一律用 `uv run notebooklm`
> (在 repo 目錄下),版本才跟 MCP 實裝的一致。

```bash
cd /home/user/research/audiskill/notebooklm-mcp
uv sync --extra login && uv run playwright install chromium   # 僅登入機需要,已完成
uv run notebooklm profile create test                                # 已完成
uv run notebooklm -p test login     # 瀏覽器開啟 → 用「測試用」Google 帳號登入
                                    # 看到 NotebookLM 首頁才按 ENTER
bash scripts/sync-auth.sh --profile test --config stg
```

named profile 讓兩個帳號各自一個目錄(`~/.notebooklm/profiles/test/`),
**測試登入不會蓋掉主力帳號的本機認證**(主帳號在 `default` profile)。
用 `uv run notebooklm profile list` 可以看兩個 profile 各自的認證狀態。

### 3. 驗證

```bash
doppler run -p notebooklm -c stg -- nblm-mcp --transport stdio   # 起得來即可 Ctrl-C
# 或直接在驗收工作區開 Claude Code,跑 auth_check
```

## 平常怎麼用

驗收工作區的 `.mcp.json` 指向 `-c stg`,在那個目錄開 Claude Code 就是測試帳號。
**正式流程(podcast-lab 等)走 `-c prd`,不受影響。**

想手動跑單一指令:

```bash
doppler run -p notebooklm -c stg -- notebooklm list        # 測試帳號
doppler run -p notebooklm -c prd -- notebooklm list        # 正式(多帳號 pool)
```

## 認證過期時

跟主力帳號一樣的老化問題,重登一次即可:

```bash
cd /home/user/research/audiskill/notebooklm-mcp
uv run notebooklm -p test login
bash scripts/sync-auth.sh --profile test --config stg
```

## 注意

- **別把測試 config 的 salt 複製回 dev**,反之亦然 —— 那會讓兩邊的 feed URL 空間合併。
- `stg` 這個 config 名稱沿用 Doppler 專案裡本來就有的環境,不是新建的環境。要換名字用
  `--config <name>`(那個 config 必須先在 Doppler 主控台存在)。
- 測試帳號一樣有每日生成配額,而且是**獨立**的一份 —— 這正是切開的好處之一。

## 登入與同步

```bash
cd /home/user/research/audiskill/notebooklm-mcp
uv run notebooklm -p test login
bash scripts/sync-auth.sh --profile test --config stg
```

### 備援:直接讀既有瀏覽器 cookie

`--browser-cookies` 直接從已安裝的瀏覽器讀 cookie,不啟動 Playwright、也就沒有那個等待:

```bash
# 1. 先在系統 Chrome 用「測試用」Google 帳號登入 NotebookLM
# 2. 再把 cookie 讀進 test profile
uv run notebooklm -p test login --browser-cookies chrome
bash scripts/sync-auth.sh --profile test --config stg
```
相依已裝(`notebooklm-py[cookies]` → rookiepy)。這台目前只有一個 Chrome profile
(`Default`);若之後有多個,用 `chrome::Profile 1` 指名,**免得抓到主力帳號的 cookie**。
