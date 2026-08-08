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
uv pip install -e ".[login]" && uv run playwright install chromium   # 僅登入機需要,已完成
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
doppler run -p notebooklm -c stg -- notebooklm-mcp --transport stdio   # 起得來即可 Ctrl-C
# 或直接在驗收工作區開 Claude Code,跑 auth_check
```

## 平常怎麼用

驗收工作區 `../nblm-acceptance-v0.6.0/.mcp.json` 已經指向 `-c stg`,在那個目錄開 Claude Code
就是測試帳號。**正式流程(podcast-lab 等)仍然是 `-c dev`,不受影響。**

想手動跑單一指令:

```bash
doppler run -p notebooklm -c stg -- notebooklm list        # 測試帳號
doppler run -p notebooklm -c dev -- notebooklm list        # 主力帳號
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

## ⚠️ 2026-08 上游突變:登入偵測不到(Google 把 NotebookLM 搬網域)

**症狀**:`uv run notebooklm -p test login` 在瀏覽器登入完成後,終端機一直停在
`Waiting for login (up to 5 minutes)...`,沒有「按 ENTER」的提示,最後 timeout;
關掉瀏覽器則得到 `TargetClosedError`。

**根因**:0.7.3 的偵測條件是「這個分頁的網址變成 `notebooklm.google.com/**`」
(`playwright_login.py` 的 `page.wait_for_url`)。但 Google 已經把未認證的登入流程轉到
**`notebook.google.com`**(少了 `lm`)——實測 `curl -L https://notebooklm.google.com/`
的 `continue=` 參數就是 `https://notebook.google.com/`。登入後分頁停在新網域,
SDK 等的舊網域永遠不匹配。**0.8.0 的 host 白名單也還沒跟上**(一樣只有
`notebooklm.google.com` / `notebooklm.cloud.google.com`),所以升級解不了。
`NOTEBOOKLM_BASE_URL` 也不能指到新網域——它有白名單,會直接 raise。

**還沒壞的部分**:已認證的 RPC 仍走舊網域且正常(實測 `notebooks.list()` 回 289 本)。
所以這隻影響**登入**,不影響既有 session 的生成/發布。

### 解法 A(建議):用我們自己的登入腳本

`scripts/login_notebooklm.py` 是 `notebooklm login` 的暫時替代品:**只改掉那一行壞掉的
偵測**(改成「網址落在任一已知 NotebookLM host 就算登入」),其餘每一步都呼叫 SDK 自己的
helper(cookie domain 過濾、原子寫檔 0600、帳號 metadata),產出的 `storage_state.json`
與原生指令等價。

```bash
cd /home/user/research/audiskill/notebooklm-mcp
uv run python scripts/login_notebooklm.py --profile test
bash scripts/sync-auth.sh --profile test --config stg
```

在瀏覽器登入完成即可,**不必按任何鍵,也不用管網址是 notebooklm 還是 notebook**。

上游修好之後就刪掉這支 —— `tests/test_contracts.py` 有一條
`test_login_script_should_be_retired_once_upstream_knows_the_new_host`,上游把新 host
納入白名單那天它會紅,提醒我們退場。

### 解法 B(備援,完全不開瀏覽器等待)

`--browser-cookies` 直接從已安裝的瀏覽器讀 cookie,不啟動 Playwright、也就沒有那個等待:

```bash
# 1. 先在系統 Chrome 用「測試用」Google 帳號登入 NotebookLM
# 2. 再把 cookie 讀進 test profile
uv run notebooklm -p test login --browser-cookies chrome
bash scripts/sync-auth.sh --profile test --config stg
```
相依已裝(`notebooklm-py[cookies]` → rookiepy)。這台目前只有一個 Chrome profile
(`Default`);若之後有多個,用 `chrome::Profile 1` 指名,**免得抓到主力帳號的 cookie**。

### 要盯的後續

Google 若把 API 端點也搬到 `notebook.google.com`,`notebooklm-py` 會整個壞掉(我們的 MCP
跟著壞)。追蹤點照 AGENTS.md:`_research/notebooklm-mcp-cli` 的 CHANGELOG / KNOWN_ISSUES
(目前尚無記載)與 notebooklm-py 的 GitHub issues。
